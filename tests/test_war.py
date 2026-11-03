import asyncio
import os
import sqlite3
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
from war_planner import MAX_DEPOT_ITEMS, Packing, WarRules, defence_transfer, estimate, pack, stock_quantity


class Estimates(unittest.TestCase):
    def test_iron_input_and_defence_thresholds(self):
        rules = WarRules()
        self.assertEqual(stock_quantity("2 blocks + 5 ingots", rules, iron=True), 23)
        self.assertEqual(stock_quantity("1 chest of iron blocks + 1 stack", rules, iron=True), 27*64*9+64)
        for invalid in ("3 blocks", "1 stack of blocks"):
            with self.assertRaises(ValueError):
                stock_quantity(invalid, rules)  # iron units are not valid for other resources
        self.assertEqual(defence_transfer(8, rules).capacity, 0)
        self.assertEqual(defence_transfer(9, rules).capacity, 100)
        self.assertEqual(defence_transfer(17, rules, 101).missing_ingots, 1)
        self.assertEqual(defence_transfer(18, rules, 200).missing_ingots, 0)

    def test_depot_input_uses_full_containers_and_loose_remainders(self):
        rules = WarRules(stack_size=16, chest_slots=9, double_chest_slots=18)
        self.assertEqual(stock_quantity("1 DOUBLE Chest + 2 Single Chests + 3 stacks + 5 items", rules), 629)
        self.assertEqual(stock_quantity("1 chest + 1", rules), 145)
        self.assertEqual(stock_quantity("2,000,000,000", rules), MAX_DEPOT_ITEMS)
        for text in ("", "1.5 stacks", "-2", "5 +", "1 donkey", "1,23", "2000000001", "2000000000 chests"):
            with self.subTest(text=text), self.assertRaises(ValueError):
                stock_quantity(text, rules)

    def test_pack_empty_mixed_and_container_boundaries(self):
        rules = WarRules()
        for hay, diamonds, expected in [
            (0, 0, Packing(0, 0, 0, 0, 0)),
            (1, 1, Packing(2, 1, 1, 1, 1)),
            (960, 0, Packing(15, 1, 1, 1, 1)),
            (960, 1, Packing(16, 2, 1, 1, 1)),
            (1728, 0, Packing(27, 2, 1, 1, 1)),
            (1729, 0, Packing(28, 2, 2, 2, 1)),
            (3456, 0, Packing(54, 4, 2, 2, 1)),
            (3457, 0, Packing(55, 4, 3, 3, 2)),
        ]:
            with self.subTest(hay=hay, diamonds=diamonds):
                self.assertEqual(pack(rules, hay=hay, diamonds=diamonds), expected)

    def test_crafted_axes_take_individual_slots_and_capacities_are_independent(self):
        rules = WarRules(stack_size=16, donkey_slots=6, chest_boat_slots=8, chest_slots=10, double_chest_slots=12)
        self.assertEqual(pack(rules, axes=15, bread=17), Packing(17, 3, 3, 2, 2))
        with self.assertRaises(ValueError):
            WarRules(stack_size=65)

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


async def check_atomic_depot_changes(test, guild):
    await stock.set_stock(guild, "Depot", 0, 4)
    results = await asyncio.gather(*(stock.adjust_stock(guild, "depot", 60, 0) for _ in range(2)))
    test.assertEqual(sorted(row["hay"] for row in results), [60, 120])
    withdrawals = await asyncio.gather(*(stock.adjust_stock(guild, "Depot", -80, -2) for _ in range(2)),
                                       return_exceptions=True)
    test.assertEqual(sum(isinstance(r, ValueError) for r in withdrawals), 1)
    test.assertEqual(await stock.available(guild, "Depot"), dict(hay=40, diamonds=2, iron=0, arrows=0))
    for h, d in [(-1, -3), (MAX_DEPOT_ITEMS, 1), (0, 0)]:
        with test.assertRaises(ValueError):
            await stock.adjust_stock(guild, "Depot", h, d)
    test.assertEqual(await stock.available(guild, "Depot"), dict(hay=40, diamonds=2, iron=0, arrows=0))
    await stock.adjust_stock(guild, "Depot", -40, -2)
    test.assertEqual(await stock.available(guild, "Depot"), dict(hay=0, diamonds=0, iron=0, arrows=0))
    await stock.set_stock(guild, "Depot", iron=18, arrows=16)
    await stock.set_stock(guild, "Depot", hay=2)
    with test.assertRaises(ValueError):
        await stock.adjust_stock(guild, "Depot", iron=-9, arrows=-17)
    test.assertEqual(await stock.available(guild, "Depot"), dict(hay=2, diamonds=0, iron=18, arrows=16))
    await stock.adjust_stock(guild, "Depot", arrows=-16, iron=-9)
    test.assertEqual(await stock.available(guild, "Depot"), dict(hay=2, diamonds=0, iron=9, arrows=0))


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

    async def test_atomic_depot_changes(self):
        await check_atomic_depot_changes(self, self.guild)

    async def test_legacy_sqlite_depot_upgrades_without_recount(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        legacy = str(Path(temp.name) / "legacy.db")
        conn = sqlite3.connect(legacy)
        try:
            conn.executescript("""
                CREATE TABLE war_stock (guild_id BIGINT NOT NULL, location_key TEXT NOT NULL,
                    location TEXT NOT NULL, hay INTEGER NOT NULL, diamonds INTEGER NOT NULL,
                    PRIMARY KEY (guild_id, location_key));
                INSERT INTO war_stock VALUES (1001, 'east', 'East', 5, 8);
            """)
        finally:
            conn.close()
        with patch.object(database, "DB_PATH", legacy):
            await database.init_db()
            await database.init_db()
            self.assertEqual(await stock.available(self.guild, "East"), dict(hay=5, diamonds=8, iron=0, arrows=0))

    async def test_iron_and_arrow_commands_and_planning_scope(self):
        async def run(cmd, *args, **kwargs):
            await cmd.callback(self.cog, self.admin, *args, **kwargs)
            return self.admin.followup.send.await_args.args[0]
        await run(self.cog.stock_set, "Keep", iron="2 blocks + 5", arrows="1 stack")
        await run(self.cog.stock_add, "Keep", iron="4 ingots")
        await run(self.cog.stock_subtract, "Keep", arrows="3")
        await stock.set_stock(self.guild, "Mine", iron=9)
        await stock.set_stock(self.guild+1, "Keep", iron=90)
        await run(self.cog.config, defence_per_block=75)
        self.assertIn("300 points", await run(self.cog.defence))
        scoped = await run(self.cog.defence, points=226, location="Keep")
        self.assertIn("225 points", scoped)
        self.assertIn("short 9 ingots", scoped)
        self.assertIn("enough iron", await run(self.cog.defence, points=225, location="Keep"))
        self.assertIn("No such", await run(self.cog.defence, location="unknown"))
        self.assertIn("61 arrows", await run(self.cog.stock_list))
        self.assertIn("1 slot ·", await run(self.cog.logistics, location="Keep"))
        assault = await run(self.cog.conquerable, 2400, 5)
        await run(self.cog.stock_set, "Keep", arrows="1 double chest", iron="1 chest of blocks")
        self.assertEqual(await run(self.cog.conquerable, 2400, 5), assault)

    async def test_set_deliver_withdraw_and_reject_without_partial_changes(self):
        await stock.configure(self.guild, {"stack_size": 16, "chest_slots": 9})
        await self.cog.stock_set.callback(self.cog, self.admin, "Base", "2 stacks + 1", "1 chest")
        await self.cog.stock_add.callback(self.cog, self.admin, "BASE", diamonds="1 stack")
        await self.cog.stock_subtract.callback(self.cog, self.admin, "base", hay="1 stack")
        self.assertIn("17 hay · 160 diamonds", self.admin.followup.send.await_args.args[0])
        self.assertTrue(self.admin.followup.send.await_args.kwargs["ephemeral"])
        for hay, diamonds in [("18", "10"), ("1", "0.5 stacks"), (None, None)]:
            await self.cog.stock_subtract.callback(self.cog, self.admin, "Base", hay, diamonds)
            self.assertEqual(await stock.available(self.guild, "Base"), dict(hay=17, diamonds=160, iron=0, arrows=0))
        await stock.set_stock(self.guild+1, "Other", 10, 10)
        await self.cog.stock_add.callback(self.cog, self.admin, "Other", "1 stack")
        self.assertIn("No such depot", self.admin.followup.send.await_args.args[0])
        self.assertIsNone(await stock.available(self.guild, "Other"))
        self.assertEqual(await stock.available(self.guild+1, "Other"), dict(hay=10, diamonds=10, iron=0, arrows=0))

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
        self.assertEqual(await stock.available(self.guild), dict(hay=7, diamonds=30, iron=0, arrows=0))
        await stock.set_stock(self.guild, "north keep", 0, 0)
        self.assertEqual(len(await stock.stock_page(self.guild, 1)), 2)
        await stock.remove_stock(self.guild, "North Keep")
        self.assertIsNone(await stock.available(self.guild, "North Keep"))
        self.assertEqual(await stock.available(self.guild+1, "North Keep"), dict(hay=99, diamonds=99, iron=0, arrows=0))

    async def test_partial_rule_edits_are_guild_scoped_and_reject_invalid_batch(self):
        await stock.configure(self.guild, {"damage_per_hit": "2.5"})
        await stock.configure(self.guild, {"hunger_loss": 0})
        with self.assertRaises(ValueError):
            await stock.configure(self.guild, {"damage_per_hit": 16, "rehit_seconds": "0.0001"})
        rules = await stock.rules_for(self.guild)
        self.assertEqual((rules.damage_per_hit, rules.hunger_loss), (Decimal("2.5"), 0))
        self.assertEqual(await stock.rules_for(self.guild+1), WarRules())

    async def test_logistics_uses_scoped_stock_and_assault_uses_only_its_requirements(self):
        await stock.set_stock(self.guild, "Keep", 960, 0)
        await stock.set_stock(self.guild, "Mine", 1, 30)
        await stock.set_stock(self.guild+1, "Mine", 99999, 99999)
        cmd = self.cog.logistics
        for location, expected in [("Keep", "15 slots · 1 donkey load"), (None, "17 slots · 2 donkey loads"),
                                   ("Missing", "No such reserve location")]:
            await cmd.callback(self.cog, self.admin, location)
            response = self.admin.followup.send.await_args
            self.assertIn(expected, response.args[0])
            self.assertTrue(response.kwargs["ephemeral"])
        await stock.configure(self.guild, {"donkey_slots": 2, "chest_slots": 8})
        await cmd.callback(self.cog, self.admin, "Keep")
        self.assertIn("8 donkey loads", self.admin.followup.send.await_args.args[0])
        self.assertIn("2 single chests", self.admin.followup.send.await_args.args[0])
        await self.cog.conquerable.callback(self.cog, self.admin, 2400, 5)
        manifest = self.admin.followup.send.await_args.args[0]
        self.assertIn("Raw hay and diamonds: 2 slots · 1 donkey load", manifest)
        self.assertIn("Crafted axes and required loaves: 11 slots · 6 donkey loads", manifest)


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
        self.assertEqual(await stock.available(guild), dict(hay=7, diamonds=9, iron=0, arrows=0))
        rules = await stock.configure(guild, {"axe_durability": 120, "rehit_seconds": "0.75"})
        self.assertEqual((rules.axe_durability, rules.rehit_seconds), (120, Decimal("0.75")))
        async with database.DBConnection() as conn:
            row = await conn.fetchone("""
                SELECT bool_and(c.relrowsecurity) AS rls, count(*) AS tables,
                       has_schema_privilege('public', 'sunset_war', 'USAGE') AS public_usage
                FROM pg_class c WHERE c.relnamespace='sunset_war'::regnamespace AND c.relkind='r'
            """)
        self.assertEqual(row, dict(rls=True, tables=2, public_usage=False))
        await check_atomic_depot_changes(self, guild)
