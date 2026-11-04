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
from cogs.war_share import ShareEstimate, message_payload
from war_display import duration, quantity
from war_planner import MAX_DEPOT_ITEMS, Packing, WarRules, block_shipment, defence_transfer, delivery_time, estimate, pack, plan_transport, stock_quantity


def response_text(call):
    return (call.args[0] if call.args and call.args[0] is not None
            else call.kwargs.get("content") or call.kwargs["embed"].description)


class Estimates(unittest.TestCase):
    def test_diamond_blocks_and_container_inputs_keep_base_counts(self):
        rules = WarRules()
        self.assertEqual(stock_quantity("2 diamond blocks + 5", rules, diamonds=True), 23)
        self.assertEqual(stock_quantity("1 double chest of diamond blocks + 2 stacks + 1", rules, diamonds=True),
                         54 * 64 * 9 + 129)
        self.assertEqual(stock_quantity("1 stack of blocks", rules, diamonds=True), 576)
        for text, flags in [("1 iron block", {"diamonds": True}),
                            ("1 chest of diamond blocks", {"iron": True}),
                            ("1 ingot", {"diamonds": True}), ("1 block", {})]:
            with self.subTest(text=text), self.assertRaises(ValueError):
                stock_quantity(text, rules, **flags)

    def test_wood_crafting_rounds_whole_batches_then_logs(self):
        # One hit per attacker means one axe each; handles share crafting batches.
        for axes, planks, logs in [(1, 2, 1), (2, 2, 1), (3, 4, 1), (4, 4, 1), (5, 6, 2), (10, 10, 3)]:
            with self.subTest(axes=axes):
                plan = estimate(axes * 4, axes, WarRules())
                self.assertEqual((plan.axes, plan.sticks, plan.planks, plan.logs), (axes, axes * 2, planks, logs))
        custom = WarRules(sticks_per_axe=3, sticks_per_batch=2, planks_per_batch=3, planks_per_log=2)
        plan = estimate(4, 1, custom)
        self.assertEqual((plan.sticks, plan.planks, plan.logs), (3, 6, 3))
        self.assertEqual(pack(WarRules(), hay=1, diamonds=1, logs=1).slots, 3)

    def test_readable_units_preserve_remainders(self):
        for seconds, expected in [(0, "0s"), (60, "1m"), (3600, "1h"), (86400, "1d"),
                                  (Decimal("90061.25"), "1d 1h 1m 1.25s")]:
            self.assertEqual(duration(seconds), expected)
        self.assertEqual(quantity(5251, "hay", WarRules()), "1 double chest + 1 single chest + 1 stack + 3 loose hay (5,251 hay)")
        self.assertEqual(quantity(65, "diamonds", WarRules()), "7 blocks + 2 loose diamonds (65 diamonds)")
        self.assertEqual(quantity(18, "iron", WarRules()), "2 blocks (18 iron ingots)")
        self.assertEqual(quantity(12000, "hay", WarRules()), "3 double chests + 25 stacks + 32 loose hay (12,000 hay)")
        self.assertEqual(quantity((54 * 64 + 27 * 64 + 64 + 3) * 9 + 2, "diamonds", WarRules()),
                         "1 double chest of blocks + 1 single chest of blocks + 1 stack of blocks + 3 blocks + 2 loose diamonds (47,261 diamonds)")

    def test_transport_trips_follow_mode_capacity_and_fleet(self):
        rules = WarRules()
        for slots, donkey, boat, carts in [(0, 0, 0, 0), (15, 1, 1, 1), (16, 2, 1, 1),
                                           (27, 2, 1, 1), (28, 2, 2, 2), (55, 4, 3, 3)]:
            with self.subTest(slots=slots):
                cargo = pack(rules, hay=slots * 64)
                self.assertEqual(plan_transport(cargo, rules, "donkey").trips, donkey)
                self.assertEqual(plan_transport(cargo, rules, "chest_boat").trips, boat)
                rail = plan_transport(cargo, rules, "chest_minecart")
                self.assertEqual((rail.carriers, rail.trips), (carts, int(slots > 0)))
        cargo = pack(rules, hay=55 * 64)
        self.assertEqual(plan_transport(cargo, rules, "donkey", 2).trips, 2)
        self.assertEqual(plan_transport(cargo, rules, "chest_boat", 3).trips, 1)
        custom = WarRules(chest_minecart_slots=10)
        self.assertEqual(plan_transport(cargo, custom, "chest_minecart").carriers, 6)
        with self.assertRaises(ValueError):
            plan_transport(cargo, rules, "chest_minecart", 2)

    def test_compact_packing_and_whole_block_shipments(self):
        rules = WarRules()
        self.assertEqual(pack(rules, diamonds=576).slots, 1)
        self.assertEqual(pack(rules, diamonds=577).slots, 2)  # one stack of blocks + one loose diamond
        self.assertEqual(pack(rules, diamonds=576, iron=576, hay=64, logs=64).slots, 4)
        self.assertEqual(pack(rules, diamonds=8, iron=8).slots, 2)
        for needed, shipped in [(0, 0), (1, 9), (9, 9), (10, 18), (30, 36)]:
            self.assertEqual(block_shipment(needed), shipped)

    def test_delivery_counts_only_intermediate_return_legs(self):
        route = delivery_time(WarRules(), 12, 2)
        self.assertEqual((route.loading, route.travel, route.unloading, route.seconds),
                         (1200, 2160, 1200, 4560))  # two outbound legs, one return
        rules = WarRules.from_settings({"loading_minutes": 0, "unloading_minutes": "0.25"})
        self.assertEqual(delivery_time(rules, 1.5, 1).seconds, 105)
        with self.assertRaises(ValueError):
            WarRules.from_settings({"loading_minutes": "-1"})

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

    def test_compacted_metal_and_container_capacities_are_independent(self):
        rules = WarRules(stack_size=16, donkey_slots=6, chest_boat_slots=8, chest_slots=10, double_chest_slots=12)
        self.assertEqual(pack(rules, diamonds=144 * 15, hay=17), Packing(17, 3, 3, 2, 2))
        with self.assertRaises(ValueError):
            WarRules(stack_size=65)

    def test_five_equipped_attackers(self):
        plan = estimate(2400, 5, WarRules())
        self.assertEqual((plan.hits, plan.volleys, plan.seconds), (600, 120, Decimal("59.5")))
        self.assertEqual((plan.axes, plan.diamonds, plan.bread, plan.hay), (10, 30, 20, 7))
        self.assertEqual((plan.sticks, plan.planks, plan.logs), (20, 10, 3))

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
    test.assertEqual(await stock.available(guild, "Depot"), dict(hay=40, diamonds=2, iron=0, arrows=0, logs=0))
    for h, d in [(-1, -3), (MAX_DEPOT_ITEMS, 1), (0, 0)]:
        with test.assertRaises(ValueError):
            await stock.adjust_stock(guild, "Depot", h, d)
    test.assertEqual(await stock.available(guild, "Depot"), dict(hay=40, diamonds=2, iron=0, arrows=0, logs=0))
    await stock.adjust_stock(guild, "Depot", -40, -2)
    test.assertEqual(await stock.available(guild, "Depot"), dict(hay=0, diamonds=0, iron=0, arrows=0, logs=0))
    await stock.set_stock(guild, "Depot", iron=18, arrows=16)
    await stock.set_stock(guild, "Depot", hay=2)
    with test.assertRaises(ValueError):
        await stock.adjust_stock(guild, "Depot", iron=-9, arrows=-17)
    test.assertEqual(await stock.available(guild, "Depot"), dict(hay=2, diamonds=0, iron=18, arrows=16, logs=0))
    await stock.adjust_stock(guild, "Depot", arrows=-16, iron=-9)
    test.assertEqual(await stock.available(guild, "Depot"), dict(hay=2, diamonds=0, iron=9, arrows=0, logs=0))
    await stock.set_stock(guild, "Depot", logs=3)
    with test.assertRaises(ValueError):
        await stock.adjust_stock(guild, "Depot", diamonds=9, logs=-4)
    test.assertEqual(await stock.available(guild, "Depot"), dict(hay=2, diamonds=0, iron=9, arrows=0, logs=3))
    await stock.adjust_stock(guild, "Depot", diamonds=9, logs=-3)
    test.assertEqual(await stock.available(guild, "Depot"), dict(hay=2, diamonds=9, iron=9, arrows=0, logs=0))


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
                                     response=AsyncMock(), followup=AsyncMock(), user=SimpleNamespace(id=42),
                                     channel_id=123, channel=AsyncMock(), message=AsyncMock())

    async def test_atomic_depot_changes(self):
        await check_atomic_depot_changes(self, self.guild)

    async def test_stock_ledger_uses_container_units_for_each_depot_and_totals(self):
        await stock.set_stock(self.guild, "Keep", hay=12000, diamonds=31104, iron=15552, logs=65)
        await stock.set_stock(self.guild, "Outpost", hay=64, diamonds=576)
        await stock.set_stock(self.guild + 1, "Hidden", hay=100000)
        await self.cog.stock_list.callback(self.cog, self.admin, location="Keep")
        scoped = response_text(self.admin.followup.send.await_args)
        self.assertIn("Hay: 3 double chests + 25 stacks + 32 loose hay (12,000 hay)", scoped)
        self.assertIn("Diamonds: 1 double chest of blocks (31,104 diamonds)", scoped)
        self.assertIn("Iron: 1 single chest of blocks (15,552 iron ingots)", scoped)
        self.assertNotIn("Outpost", scoped)
        await self.cog.stock_list.callback(self.cog, self.admin)
        total = response_text(self.admin.followup.send.await_args)
        self.assertIn("1 double chest of blocks + 1 stack of blocks (31,680 diamonds)", total)
        self.assertIn("Outpost", total)
        self.assertNotIn("Hidden", total)
        self.assertEqual((await stock.available(self.guild, "Keep"))["diamonds"], 31104)
        # A full page of maximum balances and long escaped location names must fit Discord.
        for n in range(4):
            await stock.set_stock(self.guild, str(n) + "*" * 63, **{k: MAX_DEPOT_ITEMS for k in stock.RESOURCES})
        await self.cog.stock_list.callback(self.cog, self.admin)
        self.assertLessEqual(len(response_text(self.admin.followup.send.await_args)), 4096)

    async def test_whole_block_shipment_checks_surplus_and_rail_delivers_once(self):
        await stock.set_stock(self.guild, "Keep", hay=7, diamonds=30, logs=3)
        await self.cog.conquerable.callback(self.cog, self.admin, 2400, 5, location="Keep")
        text = response_text(self.admin.followup.send.await_args)
        self.assertIn("4 blocks (36 diamonds)", text)
        self.assertIn("6 spare diamonds", text)
        self.assertIn("Insufficient supplies", text)
        self.assertIn("Short: 6 diamonds", text)
        self.assertNotIn("Crafted axes and required loaves", text)
        self.assertEqual((await stock.available(self.guild, "Keep"))["diamonds"], 30)
        await stock.set_stock(self.guild, "Rail", hay=55 * 64)
        await self.cog.config.callback(self.cog, self.admin, chest_minecart_slots=20)
        await self.cog.logistics.callback(self.cog, self.admin, "Rail", travel_minutes=12, mode="chest_minecart")
        text = response_text(self.admin.followup.send.await_args)
        self.assertIn("3 chest minecarts needed · 1 rail dispatch", text)
        self.assertIn("Delivery: 32m", text)
        self.assertIn("no return trips", text)
        self.assertEqual((await stock.rules_for(self.guild + 1)).chest_minecart_slots, 27)

    async def test_diamond_and_log_commands_and_log_shortage(self):
        await self.cog.stock_set.callback(self.cog, self.admin, "Hidden Depot", hay="7", diamonds="4 blocks", logs="2")
        await self.cog.conquerable.callback(self.cog, self.admin, 2400, 5, location="Hidden Depot")
        before = response_text(self.admin.followup.send.await_args)
        self.assertIn("Insufficient supplies", before)
        self.assertIn("Logs available: 2 logs · Short: 1 logs", before)
        await self.cog.stock_add.callback(self.cog, self.admin, "Hidden Depot", logs="1 stack", diamonds="1 stack of blocks")
        await self.cog.stock_subtract.callback(self.cog, self.admin, "Hidden Depot", logs="63", diamonds="64 blocks")
        await self.cog.conquerable.callback(self.cog, self.admin, 2400, 5, location="Hidden Depot")
        self.assertIn("Enough supplies", response_text(self.admin.followup.send.await_args))
        self.assertEqual(await stock.available(self.guild, "Hidden Depot"),
                         dict(hay=7, diamonds=36, iron=0, arrows=0, logs=3))
        await self.cog.config.callback(self.cog, self.admin, sticks_per_axe=4, planks_per_log=2)
        rules = await stock.rules_for(self.guild)
        self.assertEqual((rules.sticks_per_axe, rules.planks_per_log), (4, 2))
        self.assertEqual(await stock.rules_for(self.guild + 1), WarRules())
        await self.cog.defence.callback(self.cog, self.admin)
        self.assertIn("Transferable defence: 0 points", response_text(self.admin.followup.send.await_args))

    async def test_sharing_keeps_a_snapshot_without_private_stock(self):
        await stock.set_stock(self.guild, "Secret Depot", hay=887, diamonds=991, logs=777)
        await self.cog.conquerable.callback(self.cog, self.admin, 2400, 5, location="Secret Depot", travel_minutes=12)
        response = self.admin.followup.send.await_args
        self.assertTrue(response_text(response).startswith("**Assumptions**"))
        self.assertIn("Secret Depot", response_text(response))
        view = response.kwargs["view"]
        self.assertIsInstance(view, ShareEstimate)
        for secret in ("Secret Depot", "887", "991", "777", "Enough supplies", "Private stock check"):
            self.assertNotIn(secret, view.text)
        self.assertIn("4 blocks (36 diamonds)", view.text)
        self.assertIn("3 logs", view.text)
        self.assertIn("32m 59.5s", view.text)
        await stock.configure(self.guild, {"damage_per_hit": 2})
        await asyncio.gather(view.share.callback(self.admin), view.share.callback(self.admin))
        self.admin.channel.send.assert_awaited_once()
        sent = self.admin.channel.send.await_args
        self.assertEqual(response_text(sent), view.text)
        self.assertEqual(sent.kwargs["allowed_mentions"].to_dict(), discord.AllowedMentions.none().to_dict())
        self.assertTrue(view.share.disabled)

    async def test_share_rechecks_requester_admin_guild_and_channel(self):
        for changed in ({"user": SimpleNamespace(id=43)}, {"permissions": discord.Permissions()},
                        {"guild_id": self.guild + 1}, {"channel_id": 999}):
            with self.subTest(changed=changed):
                view = ShareEstimate(self.admin, "estimate")
                visitor = SimpleNamespace(**(vars(self.admin) | changed))
                await view.share.callback(visitor)
                visitor.channel.send.assert_not_awaited()
                self.assertFalse(view.attempted)

    async def test_share_failed_send_does_not_post_again(self):
        view = ShareEstimate(self.admin, "estimate")
        self.admin.channel.send.side_effect = discord.Forbidden(SimpleNamespace(status=403, reason="Forbidden"), "denied")
        await view.share.callback(self.admin)
        self.assertIn("Could not confirm sharing", response_text(self.admin.followup.send.await_args))
        await view.share.callback(self.admin)
        self.admin.channel.send.assert_awaited_once()

    async def test_large_estimate_fits_discord_and_shares_only_public_calculation(self):
        await stock.configure(self.guild, {
            "damage_per_hit": "0.001", "axe_durability": 1, "diamonds_per_axe": 1_000_000,
            "rehit_seconds": "1000000", "hunger_per_bread": "0.001", "hunger_loss": "1000000",
            "hits_per_hunger_loss": 1, "bread_per_hay": 1, "sticks_per_axe": 1_000_000,
            "planks_per_batch": 1_000_000, "sticks_per_batch": 1, "planks_per_log": 1,
            "stack_size": 1, "loading_minutes": 1_000_000, "unloading_minutes": 1_000_000,
        })
        for command, args, kwargs in [(self.cog.conquerable, (2_000_000_000, 1), {"travel_minutes": 1_000_000, "mode": "donkey"}),
                                      (self.cog.logistics, (), {}), (self.cog.defence, (), {"points": 2**53 - 1})]:
            await command.callback(self.cog, self.admin, *args, **kwargs)
            call = self.admin.followup.send.await_args
            private = response_text(call)
            public = call.kwargs["view"].text
            for text in (private, public):
                payload = message_payload(text)
                self.assertLessEqual(len(text), 4096 if "embed" in payload else 2000)
            self.assertNotIn("Private", public)

    async def test_delivery_timing_in_commands(self):
        async def run(cmd, *args, **kwargs):
            await cmd.callback(self.cog, self.admin, *args, **kwargs)
            return response_text(self.admin.followup.send.await_args)
        await stock.set_stock(self.guild, "Depot", hay=7, diamonds=30)
        view = await run(self.cog.conquerable, 2400, 5, location="Depot", travel_minutes=12)
        self.assertIn("Delivery: 32m", view)
        self.assertIn("Depot to conquest if supplied: 32m 59.5s", view)
        self.assertNotIn("Depot to conquest", await run(self.cog.conquerable, 2400, 5))
        await run(self.cog.config, loading_minutes=0, unloading_minutes=1.25)
        self.assertIn("Delivery: 1m 15s", await run(self.cog.logistics, "Depot", travel_minutes=0))
        await stock.set_stock(self.guild, "Convoy", hay=960, diamonds=9)  # 16 slots: two donkey trips
        self.assertIn("Delivery: 8m 30s", await run(self.cog.logistics, "Convoy", travel_minutes=2))
        self.assertIn("2 outbound trips", await run(self.cog.logistics, "Convoy"))
        self.assertIn("Delivery: 3m 15s", await run(self.cog.logistics, "Convoy", travel_minutes=2, mode="chest_boat"))
        self.assertIn("Delivery: 3m 15s", await run(self.cog.logistics, "Convoy", travel_minutes=2, carriers=2))
        self.assertEqual((await stock.rules_for(self.guild + 1)).loading_minutes, 10)
        await stock.set_stock(self.guild, "Empty", arrows=100)
        self.assertIn("No cargo to deliver", await run(self.cog.logistics, "Empty", travel_minutes=12))

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
            self.assertEqual(await stock.available(self.guild, "East"), dict(hay=5, diamonds=8, iron=0, arrows=0, logs=0))

    async def test_iron_and_arrow_commands_and_planning_scope(self):
        async def run(cmd, *args, **kwargs):
            await cmd.callback(self.cog, self.admin, *args, **kwargs)
            return response_text(self.admin.followup.send.await_args)
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
        self.assertIn("1 stack + 1 loose hay (17 hay)", response_text(self.admin.followup.send.await_args))
        self.assertIn("1 stack of blocks + 1 block + 7 loose diamonds (160 diamonds)", response_text(self.admin.followup.send.await_args))
        self.assertTrue(self.admin.followup.send.await_args.kwargs["ephemeral"])
        for hay, diamonds in [("18", "10"), ("1", "0.5 stacks"), (None, None)]:
            await self.cog.stock_subtract.callback(self.cog, self.admin, "Base", hay, diamonds)
            self.assertEqual(await stock.available(self.guild, "Base"), dict(hay=17, diamonds=160, iron=0, arrows=0, logs=0))
        await stock.set_stock(self.guild+1, "Other", 10, 10)
        await self.cog.stock_add.callback(self.cog, self.admin, "Other", "1 stack")
        self.assertIn("No such depot", response_text(self.admin.followup.send.await_args))
        self.assertIsNone(await stock.available(self.guild, "Other"))
        self.assertEqual(await stock.available(self.guild+1, "Other"), dict(hay=10, diamonds=10, iron=0, arrows=0, logs=0))

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
        await stock.set_stock(self.guild, "North Keep", 7, 29, logs=3)
        await stock.set_stock(self.guild, "Quarry", 0, 7)
        await stock.set_stock(self.guild+1, "North Keep", 99, 99)
        cmd = self.cog.conquerable
        self.assertTrue(await cmd._check_can_run(self.admin))
        self.admin.response.defer.assert_awaited_once_with(ephemeral=True)
        for location, expected in [(" NORTH  KEEP ", "Short: 7 diamonds"), (None, "Enough supplies"),
                                    ("absent", "No such reserve location")]:
            await cmd.callback(self.cog, self.admin, 2400, 5, location)
            response = self.admin.followup.send.await_args
            self.assertIn(expected, response_text(response))
            self.assertTrue(response.kwargs["ephemeral"])
        self.assertEqual(await stock.available(self.guild), dict(hay=7, diamonds=36, iron=0, arrows=0, logs=3))
        await stock.set_stock(self.guild, "north keep", 0, 0)
        self.assertEqual(len(await stock.stock_page(self.guild, 1)), 2)
        await stock.remove_stock(self.guild, "North Keep")
        self.assertIsNone(await stock.available(self.guild, "North Keep"))
        self.assertEqual(await stock.available(self.guild+1, "North Keep"), dict(hay=99, diamonds=99, iron=0, arrows=0, logs=0))

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
        for location, expected in [("Keep", "15 slots · 1 donkey"), (None, "18 slots · 1 donkey"),
                                   ("Missing", "No such reserve location")]:
            await cmd.callback(self.cog, self.admin, location)
            response = self.admin.followup.send.await_args
            self.assertIn(expected, response_text(response))
            self.assertTrue(response.kwargs["ephemeral"])
        await stock.configure(self.guild, {"donkey_slots": 2, "chest_slots": 8})
        await cmd.callback(self.cog, self.admin, "Keep")
        self.assertIn("8 carrier loads", response_text(self.admin.followup.send.await_args))
        self.assertIn("2 single chests", response_text(self.admin.followup.send.await_args))
        await self.cog.conquerable.callback(self.cog, self.admin, 2400, 5)
        manifest = response_text(self.admin.followup.send.await_args)
        self.assertIn("3 slots · 1 donkey (2 slots each) · 2 carrier loads", manifest)
        self.assertNotIn("Crafted axes and required loaves", manifest)
        self.assertIn("4 blocks (36 diamonds)", manifest)


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
        self.assertEqual(await stock.available(guild), dict(hay=7, diamonds=9, iron=0, arrows=0, logs=0))
        rules = await stock.configure(guild, {"axe_durability": 120, "rehit_seconds": "0.75", "chest_minecart_slots": 40})
        self.assertEqual((rules.axe_durability, rules.rehit_seconds, rules.chest_minecart_slots), (120, Decimal("0.75"), 40))
        async with database.DBConnection() as conn:
            row = await conn.fetchone("""
                SELECT bool_and(c.relrowsecurity) AS rls, count(*) AS tables,
                       has_schema_privilege('public', 'sunset_war', 'USAGE') AS public_usage
                FROM pg_class c WHERE c.relnamespace='sunset_war'::regnamespace AND c.relkind='r'
            """)
        self.assertEqual(row, dict(rls=True, tables=2, public_usage=False))
        await check_atomic_depot_changes(self, guild)
