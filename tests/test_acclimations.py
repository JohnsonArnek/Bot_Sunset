"""Acclimation accounting contracts on SQLite and disposable PostgreSQL."""

import asyncio
import os
import tempfile
import unittest
from datetime import datetime, timezone, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import discord
from discord import app_commands

import database as db
import acclimation_store as store
from cogs.acclimations import AcclimationCog, QuestCog, award_text, medal


def response_text(interaction):
    payload = interaction.followup.send.await_args.kwargs
    return payload.get("content") or payload["embed"].description


class AccountingContract:
    async def appoint(self, member, rank="hand", guild=None):
        await store.appoint(guild or self.guild, 900, member, rank, admin=True)

    async def test_appointments_require_admin_and_are_audited_and_scoped(self):
        with self.assertRaises(ValueError):
            await store.appoint(self.guild, 1, 2, "hand", admin=False)
        await self.appoint(1)
        await self.appoint(1, "tribune")
        self.assertEqual((await store.profile(self.guild, 1))["rank"], "tribune")
        self.assertIsNone((await store.profile(self.guild + 1, 1))["rank"])
        await store.appoint(self.guild, 900, 1, "dismissed", admin=True)
        self.assertIsNone((await store.profile(self.guild, 1))["rank"])
        self.assertEqual([r["rank"] for r in await store.appointments(self.guild)], ["dismissed", "tribune", "hand"])

    async def test_hands_are_unlimited_and_deeds_and_requests_are_preserved(self):
        await self.appoint(1)
        first = await store.award(self.guild, 1, 2, 10, "Built a bridge", request_id=101)
        replay = await store.award(self.guild, 1, 2, 10, "Built a bridge", request_id=101)
        self.assertEqual(first["id"], replay["id"])
        self.assertTrue(replay["duplicate"])
        await store.award(self.guild, 1, 2, 25, "Supplied the march", request_id=102)
        self.assertEqual((await store.profile(self.guild, 2))["total"], 35)
        self.assertEqual((await store.profile(self.guild + 1, 2))["total"], 0)
        history = await store.history(self.guild, 2)
        self.assertEqual([r["deed"] for r in history], ["Supplied the march", "Built a bridge"])
        with self.assertRaises(ValueError):
            await store.award(self.guild, 1, 3, 10, "Different recipient", request_id=101)
        with self.assertRaises(ValueError):
            await store.award(self.guild, 1, 1, 1, "Self award", request_id=103)

    async def test_tribune_limit_concurrent_requests_and_midnight_reset(self):
        await self.appoint(1, "tribune")
        now = datetime(2026, 11, 12, 23, 59, 59, tzinfo=timezone.utc)
        with patch.object(store, "utc_now", return_value=now):
            with self.assertRaises(ValueError):
                await store.award(self.guild, 1, 2, 2, "Too many", request_id=100)
            results = await asyncio.gather(
                store.award(self.guild, 1, 2, 1, "First deed", request_id=101),
                store.award(self.guild, 1, 3, 1, "Second deed", request_id=102), return_exceptions=True)
            self.assertEqual(sum(isinstance(r, ValueError) for r in results), 1)
            winner = next(r for r in results if isinstance(r, dict))
            replay = await store.award(self.guild, 1, winner["recipient_id"], 1, winner["deed"], request_id=winner["request_id"])
            self.assertTrue(replay["duplicate"])
            self.assertEqual((await store.profile(self.guild, 1))["remaining"], 0)
            # Dismissal, reappointment and correction must not replenish today's allowance.
            await store.void_award(self.guild, 900, winner["id"], "Incorrect deed", admin=True)
            await store.appoint(self.guild, 900, 1, "dismissed", admin=True)
            await self.appoint(1, "tribune")
            with self.assertRaises(ValueError):
                await store.award(self.guild, 1, 4, 1, "Third deed", request_id=103)
            await self.appoint(5, "tribune")
            await store.award(self.guild, 5, 4, 1, "Other official", request_id=104)
            await self.appoint(1, "tribune", guild=self.guild + 1)
            await store.award(self.guild + 1, 1, 4, 1, "Other server", request_id=104)
        with patch.object(store, "utc_now", return_value=now + timedelta(seconds=1)):
            self.assertEqual((await store.profile(self.guild, 1))["remaining"], 1)
            await store.award(self.guild, 1, 4, 1, "Next day", request_id=105)
        with patch.object(store, "utc_now", return_value=now + timedelta(days=5)):
            with self.assertRaises(ValueError):
                await store.award(self.guild, 1, 4, 5, "No accumulated allowance", request_id=106)

    async def test_failed_award_rolls_back_ledger_and_allowance(self):
        await self.appoint(1, "tribune")
        original = db.DBConnection.execute

        async def fail_usage(conn, query, params=()):
            if "INSERT INTO" in query and "tribune_days" in query:
                raise RuntimeError("simulated write failure")
            await original(conn, query, params)

        with patch.object(db.DBConnection, "execute", fail_usage), self.assertRaises(RuntimeError):
            await store.award(self.guild, 1, 2, 1, "A deed", request_id=100)
        self.assertEqual((await store.profile(self.guild, 2))["total"], 0)
        self.assertEqual((await store.profile(self.guild, 1))["remaining"], 1)
        row = await store.award(self.guild, 1, 2, 1, "A deed", request_id=100)
        self.assertEqual(row["total"], 1)

    async def test_quest_authority_rewards_and_duplicate_completions(self):
        await self.appoint(1)
        await self.appoint(2, "tribune")
        with self.assertRaises(ValueError):
            await store.create_quest(self.guild, 2, "Unauthorized", "A task", 10, admin=False, request_id=100)
        quest = await store.create_quest(self.guild, 1, "Build Coppercross", "Build the monument", 10, admin=False, request_id=101)
        again = await store.create_quest(self.guild, 1, "Build Coppercross", "Build the monument", 10, admin=False, request_id=101)
        self.assertEqual(quest["id"], again["id"])
        with self.assertRaises(ValueError):
            await store.complete_quest(self.guild, 2, quest["id"], 3, "Attempt", admin=False, request_id=102)
        with self.assertRaises(ValueError):
            await store.complete_quest(self.guild, 1, quest["id"], 1, "Own quest", admin=False, request_id=102)
        results = await asyncio.gather(
            store.complete_quest(self.guild, 1, quest["id"], 2, "Built the monument", admin=False, request_id=103),
            store.complete_quest(self.guild, 900, quest["id"], 2, "Built the monument", admin=True, request_id=104),
            return_exceptions=True)
        self.assertEqual(sum(isinstance(r, ValueError) for r in results), 1)
        row = next(r for r in results if isinstance(r, dict))
        self.assertEqual((await store.profile(self.guild, 2))["total"], 10)
        self.assertEqual((await store.profile(self.guild, 2))["remaining"], 1)
        self.assertEqual(row["source"], "quest")
        await store.void_award(self.guild, 900, row["id"], "Wrong completion", admin=True)
        with self.assertRaises(ValueError):
            await store.complete_quest(self.guild, 1, quest["id"], 2, "Again", admin=False, request_id=105)
        await store.complete_quest(self.guild, 1, quest["id"], 3, "Another helper", admin=False, request_id=106)
        await store.close_quest(self.guild, 1, quest["id"], admin=False)
        self.assertEqual((await store.get_quest(self.guild, quest["id"]))["status"], "closed")
        self.assertEqual(await store.quests(self.guild), [])
        with self.assertRaises(ValueError):
            await store.complete_quest(self.guild, 1, quest["id"], 4, "Too late", admin=False, request_id=107)
        self.assertIsNone(await store.get_quest(self.guild + 1, quest["id"]))
        with self.assertRaises(ValueError):
            await store.complete_quest(self.guild + 1, 900, quest["id"], 4, "Cross guild", admin=True, request_id=108)

    async def test_voiding_recalculates_eligibility_but_keeps_the_story(self):
        await self.appoint(1)
        one = await store.award(self.guild, 1, 2, 9, "First deeds", request_id=101)
        self.assertEqual(await store.honours(self.guild), [])
        two = await store.award(self.guild, 1, 2, 41, "Many further deeds", request_id=102)
        self.assertIn("Third Class", award_text(two))
        self.assertIn("Second Class", award_text(two))
        self.assertIn("First Class", award_text(two))
        self.assertEqual((await store.honours(self.guild))[0]["total"], 50)
        with self.assertRaises(ValueError):
            await store.void_award(self.guild, 1, two["id"], "Not an admin", admin=False)
        with self.assertRaises(ValueError):
            await store.void_award(self.guild + 1, 900, two["id"], "Wrong guild", admin=True)
        await store.void_award(self.guild, 900, two["id"], "Counted twice", admin=True)
        self.assertEqual((await store.profile(self.guild, 2))["total"], 9)
        self.assertEqual(await store.honours(self.guild), [])
        rows = await store.history(self.guild, 2)
        self.assertEqual(rows[0]["deed"], "Many further deeds")
        self.assertEqual(rows[0]["void_reason"], "Counted twice")
        self.assertEqual(rows[1]["id"], one["id"])

    async def test_dismissed_official_cannot_award_or_manage_quests(self):
        await self.appoint(1)
        quest = await store.create_quest(self.guild, 1, "Quest", "Details", 3, admin=False, request_id=101)
        await store.appoint(self.guild, 900, 1, "dismissed", admin=True)
        with self.assertRaises(ValueError):
            await store.award(self.guild, 1, 2, 1, "Deed", request_id=102)
        with self.assertRaises(ValueError):
            await store.close_quest(self.guild, 1, quest["id"], admin=False)
        await store.close_quest(self.guild, 900, quest["id"], admin=True)

    async def test_same_request_concurrently_credits_only_once(self):
        await self.appoint(1)
        rows = await asyncio.gather(*(store.award(self.guild, 1, 2, 10, "One deed", request_id=101) for _ in range(2)))
        self.assertEqual(rows[0]["id"], rows[1]["id"])
        self.assertEqual(sum(not r["duplicate"] for r in rows), 1)
        self.assertEqual((await store.profile(self.guild, 2))["total"], 10)


class AcclimationSQLite(AccountingContract, unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.enterContext(patch.object(db, "IS_POSTGRES", False))
        self.enterContext(patch.object(db, "DB_PATH", str(Path(temp.name) / "test.db")))
        self.guild = 1000
        await db.init_db()

    async def test_schema_startup_is_idempotent(self):
        await self.appoint(1)
        await store.award(self.guild, 1, 2, 1, "Preserved deed", request_id=101)
        await db.init_db()
        self.assertEqual((await store.profile(self.guild, 2))["total"], 1)


@unittest.skipUnless(os.environ.get("TEST_DATABASE_URL"), "needs disposable PostgreSQL")
class AcclimationPostgres(AccountingContract, unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.enterContext(patch.object(db, "IS_POSTGRES", True))
        self.enterContext(patch.object(db, "DATABASE_URL", os.environ["TEST_DATABASE_URL"]))
        self.enterContext(patch.object(db, "_pool", None))
        self.enterContext(patch.object(db, "_pool_lock", asyncio.Lock()))
        self.guild = uuid4().int >> 66
        await db.init_db()
        self.addAsyncCleanup(db._pool.close)

    async def test_private_schema_and_rls(self):
        async with db.DBConnection() as conn:
            row = await conn.fetchone("SELECT bool_and(relrowsecurity) AS rls,count(*) AS tables, "
                                     "has_schema_privilege('public','sunset_acclimations','USAGE') AS public_access "
                                     "FROM pg_class WHERE relnamespace='sunset_acclimations'::regnamespace AND relkind='r'")
        self.assertEqual(row, dict(rls=True, tables=6, public_access=False))


class AcclimationCommands(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.enterContext(patch.object(db, "IS_POSTGRES", False))
        self.enterContext(patch.object(db, "DB_PATH", str(Path(temp.name) / "test.db")))
        await db.init_db()
        self.cog, self.quest = AcclimationCog(), QuestCog()
        self.actor = SimpleNamespace(id=1, bot=False)
        self.member = SimpleNamespace(id=2, bot=False)
        self.interaction = SimpleNamespace(id=100, guild_id=10, user=self.actor,
                                           permissions=discord.Permissions(administrator=True),
                                           response=AsyncMock(), followup=AsyncMock())

    async def test_commands_check_authority_and_do_not_grant_discord_roles(self):
        i = self.interaction
        for cog in (self.cog, self.quest):
            self.assertTrue(cog.__cog_app_commands_group__.guild_only)
            visitor = SimpleNamespace(guild_id=None, response=AsyncMock())
            with self.assertRaises(app_commands.NoPrivateMessage):
                await cog.interaction_check(visitor)
            visitor.response.defer.assert_not_awaited()
        await self.cog.interaction_check(i)
        i.response.defer.assert_awaited_once_with(ephemeral=True)
        await self.cog.deputize.callback(self.cog, i, self.actor, "hand")
        self.assertIn("now a **Hand**", response_text(i))
        i.permissions = discord.Permissions(manage_guild=True)
        await self.cog.deputize.callback(self.cog, i, self.member, "tribune")
        self.assertIn("Only Discord administrators", response_text(i))
        await self.cog.award.callback(self.cog, i, self.member, "Built a bridge", 10)
        self.assertIn("Third Class", response_text(i))
        await self.cog.profile.callback(self.cog, i, self.member)
        self.assertIn("Total: **10**", response_text(i))
        self.assertTrue(i.followup.send.await_args.kwargs["ephemeral"])
        self.assertEqual(i.followup.send.await_args.kwargs["allowed_mentions"].to_dict(), discord.AllowedMentions.none().to_dict())
        await self.cog.award.callback(self.cog, i, SimpleNamespace(id=3, bot=True), "Bot deed")
        self.assertIn("not a bot account", response_text(i))

    async def test_quest_commands_and_history_render_safely(self):
        i = self.interaction
        await self.quest.create.callback(self.quest, i, "Repair **roads**", "Repair the road to Coppercross", 3)
        self.assertIn("Repair \\*\\*roads\\*\\*", response_text(i))
        quest = (await store.quests(10))[0]
        i.id += 1
        await self.quest.complete.callback(self.quest, i, quest["id"], self.member, "Repaired the northern road @everyone")
        self.assertIn("3", response_text(i))
        await self.cog.history.callback(self.cog, i, self.member)
        self.assertIn("Repaired the northern road", response_text(i))
        self.assertIn("UTC", response_text(i))
        await self.quest.close.callback(self.quest, i, quest["id"])
        await self.quest.quest_list.callback(self.quest, i)
        self.assertIn("No open quests", response_text(i))

    async def test_medal_boundaries(self):
        for total, expected in [(9, "No medal"), (10, "Third Class"), (29, "Third Class"),
                                (30, "Second Class"), (49, "Second Class"), (50, "First Class")]:
            self.assertIn(expected, medal(total))

    async def test_long_histories_fit_discord(self):
        await store.appoint(10, 1, 1, "hand", admin=True)
        for number in (1, 2):
            row = await store.award(10, 1, 2, 1, "*" * 500, request_id=number)
            await store.void_award(10, 1, row["id"], "*" * 250, admin=True)
        await self.cog.history.callback(self.cog, self.interaction, self.member)
        self.assertLessEqual(len(response_text(self.interaction)), 4096)
