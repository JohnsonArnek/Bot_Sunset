import asyncio
import os
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import discord
from discord import app_commands

import database
import war_store as stock
from cogs.war import WarCog
from war_planner import WarRules, estimate


class Estimates(unittest.TestCase):
    def test_five_equipped_attackers(self):
        plan = estimate(2400, 5, WarRules())
        self.assertEqual((plan.hits, plan.volleys, plan.seconds), (600, 120, Decimal("59.5")))
        self.assertEqual((plan.axes, plan.diamonds, plan.bread, plan.hay), (10, 30, 20, 7))

    def test_uneven_last_volley_and_one_broken_axe(self):
        plan = estimate(481, 2, WarRules())  # 61 and 60 swings, not two lots of 61
        self.assertEqual((plan.hits, plan.seconds, plan.axes, plan.diamonds), (121, 30, 3, 9))
        self.assertEqual((plan.bread, plan.hay), (4, 2))

    def test_first_hit_and_individual_food_threshold(self):
        one = estimate(1, 100, WarRules())
        self.assertEqual((one.attackers, one.axes, one.seconds, one.hay), (1, 1, 0, 0))
        self.assertEqual(estimate(16, 1, WarRules()).bread, 0)
        self.assertEqual(estimate(17, 1, WarRules()).bread, 1)
        # Four people with five hits each need four individual loaves.
        self.assertEqual(estimate(80, 4, WarRules()).hay, 2)

    def test_custom_rules_change_tools_food_and_timing(self):
        rules = WarRules.from_settings(dict(diamonds_per_axe=2, axe_durability=3, damage_per_hit="2.5",
                                       rehit_seconds="1.25", bread_per_hay=2, hunger_per_bread="1.5",
                                       hits_per_hunger_loss=2, hunger_loss="1.5"))
        plan = estimate(21, 2, rules)  # nine hits split 5 + 4
        self.assertEqual((plan.diamonds, plan.bread, plan.hay, plan.seconds), (8, 4, 2, 5))
        with self.assertRaises(ValueError):
            WarRules.from_settings({"hunger_per_bread": "0"})


class WarFlow(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.enterContext(patch.object(database, "DB_PATH", str(Path(temp.name) / "test.db")))
        self.enterContext(patch.object(database, "IS_POSTGRES", False))
        await database.init_db()
        self.cog = WarCog()
        self.guild = 1001
        self.admin = SimpleNamespace(guild_id=self.guild, permissions=discord.Permissions(administrator=True),
                                     response=AsyncMock(), followup=AsyncMock())

    async def test_every_command_enforces_admin_even_if_visibility_overridden(self):
        group = self.cog.__cog_app_commands_group__
        self.assertTrue(group.default_permissions.administrator)
        self.assertTrue(group.guild_only)
        for command in group.commands:
            for guild, perms, error in [(self.guild, discord.Permissions(manage_guild=True),
                                         app_commands.MissingPermissions),
                                        (None, discord.Permissions(administrator=True), app_commands.NoPrivateMessage)]:
                with self.subTest(command=command.name, guild=guild):
                    visitor = SimpleNamespace(guild_id=guild, permissions=perms, response=AsyncMock())
                    with self.assertRaises(error):
                        await command._check_can_run(visitor)
                    visitor.response.defer.assert_not_awaited()

    async def test_conquest_uses_only_requested_supply_and_never_spends_it(self):
        await stock.set_stock(self.guild, "North Keep", 7, 29)
        await stock.set_stock(self.guild, "Quarry", 0, 1)
        await stock.set_stock(self.guild+1, "North Keep", 99, 99)
        cmd = self.cog.conquerable
        self.assertTrue(await cmd._check_can_run(self.admin))
        self.admin.response.defer.assert_awaited_once_with(ephemeral=True)
        for location, expected in [(" NORTH  KEEP ", "short 1"), (None, "Enough supplies"),
                                    ("absent", "No such reserve location")]:
            await cmd.callback(self.cog, self.admin, 2400, 5, location)
            response = self.admin.followup.send.await_args
            self.assertIn(expected, response.args[0])
            self.assertTrue(response.kwargs["ephemeral"])
        self.assertEqual(await stock.available(self.guild), dict(hay=7, diamonds=30))
        await stock.set_stock(self.guild, "north keep", 0, 0)
        self.assertEqual(len(await stock.stock_page(self.guild, 1)), 2)
        await stock.remove_stock(self.guild, "North Keep")
        self.assertIsNone(await stock.available(self.guild, "North Keep"))
        self.assertEqual(await stock.available(self.guild+1, "North Keep"), dict(hay=99, diamonds=99))

    async def test_partial_rule_edits_are_guild_scoped_and_reject_invalid_batch(self):
        await stock.configure(self.guild, {"damage_per_hit": "2.5"})
        await stock.configure(self.guild, {"hunger_loss": 0})
        with self.assertRaises(ValueError):
            await stock.configure(self.guild, {"damage_per_hit": 16, "rehit_seconds": "0.0001"})
        rules = await stock.rules_for(self.guild)
        self.assertEqual((rules.damage_per_hit, rules.hunger_loss), (Decimal("2.5"), 0))
        self.assertEqual(await stock.rules_for(self.guild+1), WarRules())


@unittest.skipUnless(os.environ.get("TEST_DATABASE_URL"), "needs a disposable PostgreSQL database")
class WarPostgres(unittest.IsolatedAsyncioTestCase):
    async def test_persistence_and_private_schema(self):
        url = os.environ["TEST_DATABASE_URL"]
        self.enterContext(patch.object(database, "DATABASE_URL", url))
        self.enterContext(patch.object(database, "IS_POSTGRES", True))
        self.enterContext(patch.object(database, "_pool", None))
        self.enterContext(patch.object(database, "_pool_lock", asyncio.Lock()))
        await database.init_db()
        self.addAsyncCleanup(database._pool.close)
        guild = uuid4().int >> 66
        await stock.set_stock(guild, "Main", 5, 3)
        await stock.set_stock(guild, "main", 7, 9)
        self.assertEqual(await stock.available(guild), dict(hay=7, diamonds=9))
        rules = await stock.configure(guild, {"axe_durability": 120, "rehit_seconds": "0.75"})
        self.assertEqual((rules.axe_durability, rules.rehit_seconds), (120, Decimal("0.75")))
        async with database.DBConnection() as conn:
            row = await conn.fetchone("""
                SELECT bool_and(c.relrowsecurity) AS rls, count(*) AS tables,
                       has_schema_privilege('public', 'sunset_war', 'USAGE') AS public_usage
                FROM pg_class c WHERE c.relnamespace='sunset_war'::regnamespace AND c.relkind='r'
            """)
        self.assertEqual(row, dict(rls=True, tables=2, public_usage=False))
