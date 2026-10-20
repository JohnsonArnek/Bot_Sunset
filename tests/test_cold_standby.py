"""Real Postgres tests of scheduling decisions with a stubbed pg_net transport."""

import asyncio
import os
from pathlib import Path
import unittest
import uuid

import asyncpg

from lease import LeaseStore

ROOT = Path(__file__).resolve().parents[1]


@unittest.skipUnless(os.environ.get("TEST_DATABASE_URL"), "TEST_DATABASE_URL is not set")
class ColdStandbyTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.pool = await asyncpg.create_pool(
            os.environ["TEST_DATABASE_URL"], min_size=1, max_size=5,
            command_timeout=5, statement_cache_size=0,
        )
        if await self.pool.fetchval("SELECT EXISTS (SELECT 1 FROM pg_extension WHERE extname='pg_net')"):
            await self.pool.close()
            self.skipTest("Use a disposable plain PostgreSQL DB; pg_net will be stubbed")
        self.group = "cold-test-" + uuid.uuid4().hex
        self.a = LeaseStore(self.pool, self.group, "a")
        self.b = LeaseStore(self.pool, self.group, "b")
        await self.a.initialize()
        # Supabase's built-in roles don't exist on a plain PostgreSQL install.
        for role in ("anon", "authenticated", "service_role"):
            if not await self.pool.fetchval("SELECT EXISTS (SELECT 1 FROM pg_roles WHERE rolname=$1)", role):
                await self.pool.execute(f"CREATE ROLE {role} NOLOGIN")
        await self.pool.execute((ROOT / "tests/pg_net_stub.sql").read_text())
        await self.pool.execute((ROOT / "supabase/cold_standby.sql").read_text())
        await self.pool.execute(
            "INSERT INTO sunset_monitor.state (bot_group) VALUES ($1)", self.group,
        )
        await self.pool.executemany(
            "INSERT INTO sunset_monitor.services (bot_group,instance_id,base_url,priority) "
            "VALUES ($1,$2,$3,$4)",
            [(self.group, "a", "https://primary.onrender.com", 0),
             (self.group, "b", "https://backup.onrender.com", 1)],
        )

    async def asyncTearDown(self):
        await self.pool.execute("DELETE FROM sunset_monitor.state WHERE bot_group=$1", self.group)
        await self.pool.execute("DELETE FROM sunset_monitor.services WHERE bot_group=$1", self.group)
        await self.pool.execute("DELETE FROM bot_instance_cooldowns WHERE bot_group=$1", self.group)
        await self.pool.execute("DELETE FROM bot_leases WHERE bot_group=$1", self.group)
        await self.pool.close()

    async def tick(self):
        # Simulate the next cron invocation without waiting a wall-clock minute.
        await self.pool.execute(
            "UPDATE sunset_monitor.state SET last_tick_at=now()-interval '60 seconds' "
            "WHERE bot_group=$1", self.group,
        )
        await self.pool.execute("SELECT sunset_monitor.tick($1)", self.group)
        return await self.pool.fetchval(
            "SELECT r.url FROM sunset_monitor.state s "
            "JOIN net.sunset_test_requests r ON r.id=s.request_id WHERE s.bot_group=$1",
            self.group,
        )

    async def respond(self, content='{"alive":true}', code=200):
        await self.pool.execute(
            "INSERT INTO net._http_response (id,status_code,content,timed_out) "
            "SELECT request_id,$2,$3,false FROM sunset_monitor.state WHERE bot_group=$1",
            self.group, code, content,
        )

    async def health(self):
        return await self.pool.fetchval("SELECT public.sunset_bot_health($1)", self.group)

    async def make_healthy(self):
        await self.a.acquire()
        await self.a.renew(True)
        await self.tick()
        await self.respond()
        await self.tick()
        self.assertTrue(await self.health())

    async def test_healthy_primary_and_monitor_reads_never_contact_backup(self):
        await self.make_healthy()
        count = await self.pool.fetchval("SELECT count(*) FROM net.sunset_test_requests")
        for _ in range(3):
            self.assertTrue(await self.health())
        self.assertEqual(count, await self.pool.fetchval("SELECT count(*) FROM net.sunset_test_requests"))
        self.assertEqual(await self.tick(), "https://primary.onrender.com/health")

    async def test_healthy_backup_is_kept_without_failback(self):
        await self.b.acquire()
        await self.b.renew(True)
        self.assertEqual(await self.tick(), "https://backup.onrender.com/health")

    async def test_expired_primary_wakes_backup_without_changing_ownership(self):
        await self.a.acquire()
        await self.tick()
        await self.pool.execute(
            "UPDATE bot_leases SET lease_until=now()-interval '1 second' WHERE bot_group=$1",
            self.group,
        )
        self.assertEqual(await self.tick(), "https://backup.onrender.com/health")
        self.assertEqual(await self.pool.fetchval(
            "SELECT owner FROM bot_leases WHERE bot_group=$1", self.group,
        ), self.a.owner)

    async def test_wake_attempt_has_grace_period_then_rotates(self):
        self.assertEqual(await self.tick(), "https://primary.onrender.com/health")
        self.assertEqual(await self.tick(), "https://primary.onrender.com/health")
        await self.pool.execute(
            "UPDATE sunset_monitor.state SET wake_started_at=now()-interval '181 seconds' "
            "WHERE bot_group=$1", self.group,
        )
        self.assertEqual(await self.tick(), "https://backup.onrender.com/health")

    async def test_cooldowns_skip_blocked_instances_and_stop_when_both_blocked(self):
        await self.a.cooldown(1800, "ip_block")
        self.assertEqual(await self.tick(), "https://backup.onrender.com/health")
        await self.b.cooldown(1800, "ip_block")
        self.assertIsNone(await self.tick())
        self.assertFalse(await self.health())

    async def test_starting_or_unknown_live_leader_never_wakes_a_peer(self):
        await self.a.acquire()  # healthy=false during startup.
        self.assertEqual(await self.tick(), "https://primary.onrender.com/health")
        await self.pool.execute(
            "UPDATE bot_leases SET instance_id='unknown' WHERE bot_group=$1", self.group,
        )
        self.assertIsNone(await self.tick())

    async def test_health_requires_fresh_scheduler_http_and_same_generation(self):
        await self.make_healthy()
        for column in ("last_tick_at", "http_ok_at"):
            await self.pool.execute(
                f"UPDATE sunset_monitor.state SET {column}=now()-interval '151 seconds' "
                "WHERE bot_group=$1", self.group,
            )
            self.assertFalse(await self.health())
            await self.pool.execute(
                f"UPDATE sunset_monitor.state SET {column}=now() WHERE bot_group=$1", self.group,
            )
        await self.a.renew(False)
        self.assertFalse(await self.health())
        await self.a.renew(True)
        self.assertTrue(await self.health())
        await self.pool.execute(
            "UPDATE bot_leases SET generation=generation+1 WHERE bot_group=$1", self.group,
        )
        self.assertFalse(await self.health())

    async def test_loading_page_does_not_count_as_application_health(self):
        await self.a.acquire()
        await self.a.renew(True)
        await self.tick()
        await self.respond("<html>Render is starting your service</html>")
        await self.tick()
        self.assertFalse(await self.health())

    async def test_concurrent_ticks_queue_only_one_request(self):
        before = await self.pool.fetchval("SELECT count(*) FROM net.sunset_test_requests")
        await asyncio.gather(*[
            self.pool.execute("SELECT sunset_monitor.tick($1)", self.group) for _ in range(2)
        ])
        self.assertEqual(
            await self.pool.fetchval("SELECT count(*) FROM net.sunset_test_requests"),
            before + 1,
        )

    async def test_disabled_scheduler_does_not_ping_and_reports_unhealthy(self):
        await self.make_healthy()
        await self.pool.execute(
            "UPDATE sunset_monitor.state SET enabled=false WHERE bot_group=$1", self.group,
        )
        count = await self.pool.fetchval("SELECT count(*) FROM net.sunset_test_requests")
        await self.tick()
        self.assertEqual(count, await self.pool.fetchval("SELECT count(*) FROM net.sunset_test_requests"))
        self.assertFalse(await self.health())

    async def test_public_roles_cannot_read_configuration_or_run_actions(self):
        async with self.pool.acquire() as conn:
            for role in ("anon", "authenticated", "service_role"):
                await conn.execute(f"SET ROLE {role}")
                try:
                    with self.assertRaises(asyncpg.InsufficientPrivilegeError):
                        await conn.fetch("SELECT * FROM sunset_monitor.services")
                    with self.assertRaises(asyncpg.InsufficientPrivilegeError):
                        await conn.execute("SELECT sunset_monitor.tick($1)", self.group)
                    if role == "service_role":
                        self.assertFalse(await conn.fetchval("SELECT public.sunset_bot_health($1)", self.group))
                    else:
                        with self.assertRaises(asyncpg.InsufficientPrivilegeError):
                            await conn.fetchval("SELECT public.sunset_bot_health($1)", self.group)
                finally:
                    await conn.execute("RESET ROLE")
