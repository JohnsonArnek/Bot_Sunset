"""Run only against a disposable database via TEST_DATABASE_URL."""

import asyncio
import os
import unittest
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import asyncpg
import discord

import database
from failover import Runtime
from lease import LeaseLost, LeaseStore


@unittest.skipUnless(os.environ.get("TEST_DATABASE_URL"), "TEST_DATABASE_URL is not set")
class PostgresTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.pool = await asyncpg.create_pool(
            os.environ["TEST_DATABASE_URL"], min_size=1, max_size=5,
            command_timeout=5, statement_cache_size=0,
        )
        self.group = "test-" + uuid.uuid4().hex
        self.a = LeaseStore(self.pool, self.group, "a")
        self.b = LeaseStore(self.pool, self.group, "b")
        await asyncio.gather(self.a.initialize(), self.b.initialize())

    async def asyncTearDown(self):
        await self.pool.execute("DELETE FROM bot_instance_cooldowns WHERE bot_group=$1", self.group)
        await self.pool.execute("DELETE FROM bot_leases WHERE bot_group=$1", self.group)
        await self.pool.close()

    async def expire(self):
        await self.pool.execute(
            "UPDATE bot_leases SET lease_until=clock_timestamp()-interval '1 second' "
            "WHERE bot_group=$1", self.group,
        )

    async def test_simultaneous_start_has_exactly_one_winner(self):
        results = await asyncio.gather(self.a.acquire(), self.b.acquire())
        self.assertEqual(results.count(True), 1)

    async def test_overlapping_deploys_with_same_instance_id_are_distinct(self):
        self.b.instance = "a"
        self.assertNotEqual(self.a.owner, self.b.owner)
        self.assertTrue(await self.a.acquire())
        self.assertFalse(await self.b.acquire())

    async def test_stale_owner_cannot_renew_release_or_write_after_takeover(self):
        self.assertTrue(await self.a.acquire())
        await self.expire()
        self.assertTrue(await self.b.acquire())
        self.assertGreater(self.b.generation, self.a.generation)
        self.assertFalse(await self.a.renew(True))
        await self.a.release()
        self.assertTrue(await self.b.renew(True))
        async with self.pool.acquire() as conn:
            with self.assertRaises(LeaseLost):
                async with self.a.fence(conn):
                    self.fail("Stale owner passed fencing")
            async with self.b.fence(conn):
                self.assertEqual(await conn.fetchval("SELECT 1"), 1)

    async def test_expired_owner_cannot_resurrect_without_new_election(self):
        await self.a.acquire()
        await self.expire()
        self.assertFalse(await self.a.renew(True))
        async with self.pool.acquire() as conn:
            with self.assertRaises(LeaseLost):
                async with self.a.fence(conn):
                    pass

    async def test_cooldown_survives_restart_and_does_not_block_peer(self):
        await self.a.acquire()
        await self.a.cooldown(1800, "discord_ip_block")
        await self.a.release()
        restarted = LeaseStore(self.pool, self.group, "a")
        self.assertFalse(await restarted.acquire())
        self.assertTrue(await self.b.acquire())
        await self.b.release()
        await self.pool.execute(
            "UPDATE bot_instance_cooldowns SET retry_at=clock_timestamp()-interval '1 second' "
            "WHERE bot_group=$1", self.group,
        )
        self.assertTrue(await restarted.acquire())

    async def test_takeover_waits_for_inflight_application_transaction(self):
        await self.a.acquire()
        await self.pool.execute(
            "UPDATE bot_leases SET lease_until=clock_timestamp()+interval '1 second' "
            "WHERE bot_group=$1", self.group,
        )
        async with self.pool.acquire() as conn:
            async with self.a.fence(conn):
                # Let the committed lease expire while this SQL transaction
                # still owns its shared lock; the successor must wait.
                await asyncio.sleep(1.1)
                takeover = asyncio.create_task(self.b.acquire())
                await asyncio.sleep(0.1)
                self.assertFalse(takeover.done())
            self.assertTrue(await takeover)

    async def test_unhealthy_flag_does_not_steal_a_live_startup_lease(self):
        await self.a.acquire()
        self.assertFalse(await self.b.acquire())
        self.assertTrue(await self.a.renew(False))
        self.assertFalse(await self.b.acquire())

    async def test_application_database_helpers_are_fenced(self):
        await self.a.acquire()
        await self.a.cooldown(1800, "original")
        with patch.object(database, "IS_POSTGRES", True), \
                patch.object(database, "_pool", self.pool), \
                patch.object(database, "_pool_lock", asyncio.Lock()), \
                patch.object(database, "leader_lease", self.a):
            async with database.DBConnection() as conn:
                await conn.execute(
                    "UPDATE bot_instance_cooldowns SET reason=? WHERE bot_group=?",
                    ("leader_write", self.group),
                )
                await self.expire()
                self.assertTrue(await self.b.acquire())
                with self.assertRaises(LeaseLost):
                    await conn.execute(
                        "UPDATE bot_instance_cooldowns SET reason=? WHERE bot_group=?",
                        ("stale_write", self.group),
                    )
                with self.assertRaises(LeaseLost):
                    await conn.fetchone("SELECT 1")
                with self.assertRaises(LeaseLost):
                    await conn.fetchall("SELECT 1")
        self.assertEqual(await self.pool.fetchval(
            "SELECT reason FROM bot_instance_cooldowns WHERE bot_group=$1", self.group,
        ), "leader_write")

    async def test_supervisor_persists_cooldown_and_releases_after_real_db_shutdown(self):
        bot = Mock()
        bot.is_ready.return_value = False
        bot.close = AsyncMock()
        runtime = Runtime(bot, enabled=True, instance="a", group=self.group)

        async def start():
            raise discord.HTTPException(
                SimpleNamespace(status=429, reason="blocked", headers={}),
                "<html>Cloudflare error 1015</html>",
            )

        with patch.object(database, "DATABASE_URL", os.environ["TEST_DATABASE_URL"]), \
                patch.object(database, "leader_lease", None):
            self.assertEqual(await runtime.run("unused", start), 1)
        bot.close.assert_awaited_once()
        row = await self.pool.fetchrow(
            "SELECT owner FROM bot_leases WHERE bot_group=$1", self.group,
        )
        self.assertIsNone(row["owner"])
        self.assertFalse(await self.a.acquire())  # Same service remains cooling down.
        self.assertTrue(await self.b.acquire())
