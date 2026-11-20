"""Bank invariants and claim checkout on SQLite and disposable PostgreSQL."""

import asyncio
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch
from uuid import uuid4

import discord

import database as db
import bank_store as bank
import weekly_claims as weekly
from cogs.queue_cog import queue_card
from cogs.weekly_offers import OfferAction, notify_offers, offer_card, review_request
from cogs.bank import BankCog
from cogs.claims import ClaimsCog
from cogs.distribution import DistributionCog
from cogs.land import LandCog
from cogs.settlement_profile import directory_card, profile_card


AT = datetime(2026, 11, 13, 12, tzinfo=timezone.utc)


def interaction(guild, user=10, admin=False, request_id=100):
    return SimpleNamespace(id=request_id, guild_id=guild, channel_id=123, user=SimpleNamespace(id=user, roles=[]),
                           permissions=discord.Permissions(administrator=admin),
                           response=AsyncMock(), followup=AsyncMock())


class BankContract:
    async def test_settlement_profile_request_offer_purchase_delivery(self):
        land_id = await db.create_land(self.guild, "Coppercross", 10, 4)
        owner = interaction(self.guild)
        owner.guild = SimpleNamespace(id=self.guild, name="Server", owner_id=900)
        owner.user.guild_permissions = owner.permissions
        cog = LandCog(None)
        await cog.land_info.callback(cog, owner)
        card = owner.followup.send.await_args.kwargs
        actions = {button.label: button for button in card["view"].children}
        await actions["Request banners"].callback(owner)
        options = owner.followup.send.await_args.kwargs
        self.assertTrue(options["ephemeral"])
        options["view"].choose._values = ["builds"]
        await options["view"].choose.callback(owner)
        pending = await db.get_pending_request(land_id)
        self.assertEqual((pending["chunks_requested"], pending["purpose"]), (1, "builds"))
        queue = await profile_card(owner, land_id=land_id)
        position = next(btn for btn in queue["view"].children if btn.label == "View queue position")
        await position.callback(owner)
        self.assertIn("▸ 1. Coppercross", str(owner.followup.send.await_args.kwargs["embed"].to_dict()))
        await weekly.review(self.guild, pending["id"], True)
        # An older request form cannot submit a new request over this offer.
        await options["view"].choose.callback(owner)
        self.assertIsNone(await db.get_pending_request(land_id))
        offered = await profile_card(owner, land_id=land_id)
        self.assertIn("7 ems total", offered["embed"].description)
        review = next(btn for btn in offered["view"].children if btn.label == "Review offer")
        visitor = interaction(self.guild, user=44)
        visitor.guild = owner.guild
        await review.callback(visitor)
        self.assertIsNone(visitor.followup.send.await_args.kwargs["view"])
        await review.callback(owner)
        choices = owner.followup.send.await_args.kwargs["view"].children
        owner.edit_original_response = AsyncMock()
        await next(btn for btn in choices if btn.action=="manual").callback(owner)
        purchased = await profile_card(owner, land_id=land_id)
        self.assertIn("purchase is recorded", purchased["embed"].description)
        delivery = next(btn for btn in purchased["view"].children if btn.label == "View delivery")
        await delivery.callback(owner)
        private = owner.followup.send.await_args.kwargs
        self.assertTrue(private["ephemeral"])
        self.assertIn("Manual payment due", private["embed"].description)
        visitor.user.guild_permissions = visitor.permissions
        await delivery.callback(visitor)
        self.assertIsNone(visitor.followup.send.await_args.kwargs["embed"])

    async def test_profile_bank_checks_current_officer_and_selected_land_on_each_page(self):
        land = await self.land()
        reader = interaction(self.guild, user=44)
        profile = await profile_card(reader, land_id=land["id"])
        button = next(btn for btn in profile["view"].children if btn.label=="Settlement bank")
        for n in range(4):
            await self.deposit(10, request_id=100+n, land_name=land["name"])
        self.assertNotIn("Balance:", str(profile["embed"].to_dict()))
        await button.callback(reader)
        self.assertIsNone(reader.followup.send.await_args.kwargs["embed"])
        await db.add_officer(land["id"], 44)
        await button.callback(reader)
        private = reader.followup.send.await_args.kwargs
        self.assertTrue(private["ephemeral"])
        self.assertIn("40 ems", private["embed"].description)
        self.assertFalse(private["view"].next_page.disabled)
        reader.edit_original_response = AsyncMock()
        await private["view"].next_page.callback(reader)
        self.assertIn("page 2", reader.edit_original_response.await_args.kwargs["embed"].description)
        await db.remove_officer(land["id"], 44)
        await private["view"].next_page.callback(reader)
        self.assertIsNone(reader.edit_original_response.await_args.kwargs["embed"])
        with self.assertRaises(ValueError):
            await bank.account(self.guild+1, 900, admin=True, land_id=land["id"])

    async def test_directory_selection_and_back_preserve_page_without_editing_public_message(self):
        reader = interaction(self.guild, user=44)
        for n in range(12):
            await self.land(owner=50+n, name=f"Land {n:02d}")
        directory = await directory_card(reader)
        await directory["view"].next_page.callback(reader)
        private_page = reader.followup.send.await_args.kwargs
        self.assertTrue(private_page["ephemeral"])
        select = next(item for item in private_page["view"].children if isinstance(item,discord.ui.Select))
        self.assertEqual([o.label for o in select.options], ["Land 10", "Land 11"])
        select._values = [select.options[1].value]
        reader.edit_original_response = AsyncMock()
        await select.callback(reader)
        selected = reader.edit_original_response.await_args.kwargs
        self.assertEqual(selected["embed"].title, "Land 11")
        back = next(button for button in selected["view"].children if button.label=="Back to list")
        await back.callback(reader)
        self.assertIn("Page 2/2", reader.edit_original_response.await_args.kwargs["embed"].footer.text)

    async def test_weekly_offer_decline_reallocates_once_and_rolls_remaining_into_leftovers(self):
        first = await self.land(name="First", owner=10)
        second = await self.land(name="Second", owner=20)
        third = await self.land(name="Third", owner=30)
        # The oldest request outranks the other two. Four of seven blocks are available.
        first_req = await self.approval(first, blocks=5, age=10, status="pending")
        await self.approval(second, blocks=1, age=2, status="pending")
        await self.approval(third, blocks=2, status="pending")
        runs = await asyncio.gather(weekly.distribute(self.guild, AT), weekly.distribute(self.guild, AT))
        self.assertEqual(sum(run is not None for run in runs), 1)
        result = next(run for run in runs if run)
        self.assertEqual((result["offered"], result["partial"], result["leftover"]), (4, 1, 0))
        await db.set_config(self.guild, "distribution_day", "5")
        self.assertIsNone(await weekly.distribute(self.guild, AT + timedelta(days=1)))
        offers = await weekly.offers(self.guild, owner=10)
        self.assertEqual([(r["chunks_requested"], r["approved_at"]) for r in offers], [(4, AT.isoformat())])
        with self.assertRaises(ValueError):
            await weekly.decline(self.guild, 20, first_req["id"])
        results = await asyncio.gather(*(weekly.decline(self.guild, 10, first_req["id"]) for _ in range(2)),
                                       return_exceptions=True)
        self.assertIn((3, 1), results)
        self.assertEqual(sum(isinstance(r, ValueError) for r in results), 1)
        self.assertEqual([r["land_name"] for r in await weekly.offers(self.guild)], ["Second", "Third"])
        reserve = await db.get_reserve(self.guild)
        self.assertEqual((reserve["total_blocks"], reserve["leftover_blocks"]), (3, 1))
        self.assertEqual((await self.state(first))["deliveries"], 0)

    async def test_owner_offer_binds_land_quote_expiry_and_receipt(self):
        await self.land()
        target = await self.land(name="Second land")
        request = await self.approval(target, blocks=2, status="pending")
        await weekly.review(self.guild, request["id"], True)
        args = dict(request_id=41, payment="bank", allocation_id=request["id"], quote=(2,7))
        for guild, owner, error in [(self.guild+1,10,"owner"), (self.guild,11,"owner"), (self.guild,10,"Insufficient")]:
            with self.assertRaisesRegex(ValueError, error):
                await bank.buy_claim(guild, owner, **args)
        await self.deposit(30, land_name="Second land")
        with self.assertRaisesRegex(ValueError, "price changed"):
            await bank.buy_claim(self.guild, 10, **{**args, "quote": (2, 16)})
        with patch.object(bank, "now", return_value=AT+timedelta(days=7)):
            with self.assertRaisesRegex(ValueError, "expired"):
                await bank.buy_claim(self.guild, 10, **args)
        with patch.object(bank, "now", return_value=AT+timedelta(days=1)):
            with self.assertRaisesRegex(ValueError, "Monday"):
                await bank.buy_claim(self.guild, 10, **args)
        paid = await bank.buy_claim(self.guild, 10, **args)
        self.assertEqual((paid["land_id"], paid["balance"]), (target["id"], 16))
        self.assertTrue((await bank.buy_claim(self.guild, 10, **args))["duplicate"])
        with self.assertRaises(ValueError):
            await weekly.decline(self.guild, 10, request["id"])
        self.assertEqual((await self.state(target))["deliveries"], 1)

    async def test_buy_and_decline_compete_for_same_offer_and_honor_weekly_allowance(self):
        used = await self.land(name="Already bought", owner=12)
        await self.approval(used)
        await bank.buy_claim(self.guild, 12, request_id=20)
        await self.approval(used, status="pending", age=9)
        land = await self.land()
        request = await self.approval(land, status="pending")
        await weekly.review(self.guild, request["id"], True)
        waiting = await self.land(name="Next", owner=22)
        await self.approval(waiting, status="pending")
        results = await asyncio.gather(bank.buy_claim(self.guild, 10, request_id=21,
            allocation_id=request["id"], quote=(1, 7)), weekly.decline(self.guild, 10, request["id"]),
            return_exceptions=True)
        self.assertEqual(sum(isinstance(result, ValueError) for result in results), 1)
        if isinstance(results[0], dict):
            self.assertEqual((results[0]["payment"], results[0]["total"]), ("manual", 7))
            self.assertIsNone(await db.get_approved_request(waiting["id"]))
            self.assertEqual((await self.state(land))["deliveries"], 1)
        else:
            self.assertEqual(results[1], (1, 0))
            self.assertIsNotNone(await db.get_approved_request(waiting["id"]))
            self.assertEqual((await self.state(land))["deliveries"], 0)
        self.assertIsNotNone(await db.get_pending_request(used["id"]))

    async def test_expiry_returns_allocated_stock_once_seven_days_after_approval(self):
        land = await self.land()
        request = await self.approval(land, blocks=2, age=20, status="pending")
        await weekly.review(self.guild, request["id"], True)
        async with bank.write(self.guild) as conn:
            self.assertEqual(await weekly.expire(conn, self.guild, AT + timedelta(days=6)), [])
            expired = await weekly.expire(conn, self.guild, AT + timedelta(days=7))
            self.assertEqual([row["id"] for row in expired], [request["id"]])
            self.assertEqual(await weekly.expire(conn, self.guild, AT + timedelta(days=8)), [])
        self.assertEqual((await db.get_reserve(self.guild))["total_blocks"], 2)
        self.assertEqual((await self.state(land))["deliveries"], 0)

    async def test_rotation_bypass_empty_queue_recovery_and_persistent_offer_buttons(self):
        await db.set_config(self.guild, "claim_mode", "rotation")
        land = await self.land(chunks=0)
        request = await db.create_claim_request(land["id"], 1, "builds")
        self.assertIsNone(await db.create_claim_request(land["id"], 1, "vault"))
        self.assertIsNone(await weekly.distribute(self.guild, AT))
        self.assertIsNotNone(await db.get_pending_request(land["id"]))
        await weekly.review(self.guild, request, True)
        options = await queue_card(self.guild)
        self.assertIn("My allocation", [button.label for button in options["view"].children])
        rows = await weekly.offers(self.guild, owner=10)
        view = offer_card(SimpleNamespace(id=self.guild, name="Guild"), rows[0])["view"]
        self.assertTrue(view.is_persistent())
        items = view.children
        self.assertEqual([item.action for item in items], ["manual", "decline"])
        item = items[0]
        match = OfferAction.__discord_ui_compiled_template__.fullmatch(item.custom_id)
        restored = await OfferAction.from_custom_id(None, item.item, match)
        click = interaction(None, request_id=807)
        click.edit_original_response = AsyncMock()
        await restored.callback(click)
        self.assertIn("Free allocation", click.edit_original_response.await_args.kwargs["content"])
        self.assertEqual((await self.state(land))["deliveries"], 1)

    async def land(self, owner=10, chunks=3, name="Test Keep", guild=None):
        async with db.DBConnection() as conn:
            return await conn.execute_returning_one(
                "INSERT INTO lands (guild_id,name,owner_id,chunks,created_at) VALUES (?,?,?,?,?) RETURNING *",
                (guild or self.guild, name, owner, chunks, AT.isoformat()))

    async def approval(self, land, blocks=1, *, age=0, status="approved"):
        async with db.DBConnection() as conn:
            return await conn.execute_returning_one(
                "INSERT INTO claim_requests (land_id,chunks_requested,purpose,requested_at,status) VALUES (?,?,?,?,?) RETURNING *",
                (land["id"], blocks, "builds", (AT-timedelta(days=age)).isoformat(), status))

    async def deposit(self, amount, request_id=1, **target):
        return await bank.adjust(self.guild, 900, amount, "Counted deposit", admin=True, request_id=request_id, **target)

    async def balance(self, **target):
        return (await bank.account(self.guild, 900, admin=True, **target))["balance"]

    async def state(self, land):
        async with db.DBConnection() as conn:
            return await conn.fetchone("SELECT chunks,(SELECT COUNT(*) FROM purchases WHERE land_id=lands.id) AS purchases,"
                                      "(SELECT COUNT(*) FROM block_ledger WHERE land_id=lands.id) AS deliveries FROM lands WHERE id=?", (land["id"],))

    async def test_zero_accounts_admin_only_movements_and_private_reads(self):
        self.assertEqual((await bank.account(self.guild, 10, admin=False))["balance"], 0)
        with self.assertRaises(ValueError):
            await bank.account(self.guild, 10, admin=False, member=11)
        with self.assertRaises(ValueError):
            await bank.adjust(self.guild, 10, 50, "Self deposit", admin=False, request_id=1, member=10)
        with self.assertRaises(ValueError):
            await bank.accounts(self.guild, admin=False)
        with self.assertRaises(ValueError):
            await bank.account(self.guild, 10, admin=False, treasury=True)
        row = await self.deposit(50, member=10)
        self.assertEqual(await self.balance(member=10), 50)
        other = await bank.account(self.guild+1, 10, admin=False)
        self.assertEqual(other["balance"], 0)
        with self.assertRaises(ValueError):
            await bank.account(self.guild+1, 900, admin=True, account_id=row["account_id"])
        for target in ({}, dict(member=10, land_name="Test Keep")):
            with self.assertRaises(ValueError):
                await self.deposit(1, request_id=2, **target)
        for amount in (0, -1, 1.5, True, bank.MAX_AMOUNT+1):
            with self.assertRaises(ValueError):
                await self.deposit(amount, request_id=3, member=10)

    async def test_adjustments_are_deduplicated_and_cannot_overdraw(self):
        rows = await asyncio.gather(*(self.deposit(20, member=10) for _ in range(2)))
        self.assertEqual(rows[0]["operation_id"], rows[1]["operation_id"])
        self.assertEqual(sum(r["duplicate"] for r in rows), 1)
        with self.assertRaises(ValueError):
            await self.deposit(21, member=10)
        results = await asyncio.gather(*(bank.adjust(self.guild, 900, 15, "Approved withdrawal", admin=True,
                                                   request_id=n, withdraw=True, member=10) for n in (2,3)), return_exceptions=True)
        self.assertEqual(sum(isinstance(r, ValueError) for r in results), 1)
        self.assertEqual(await self.balance(member=10), 5)
        account = await bank.account(self.guild, 10, admin=False)
        self.assertEqual([e["delta"] for e in account["entries"]], [-15,20])
        self.assertTrue(all(e["actor_id"] == 900 and e["reason"] for e in account["entries"]))
        with patch.object(bank, "MAX_BALANCE", 5), self.assertRaises(ValueError):
            await self.deposit(1, request_id=4, member=10)

    async def test_land_ownership_officers_deletion_and_new_land_accounts(self):
        land = await self.land()
        await db.add_officer(land["id"], 11)
        await self.deposit(30, land_name="Test Keep")
        row = await bank.account(self.guild, 11, admin=False, land_name="Test Keep")
        self.assertEqual(row["balance"], 30)
        with self.assertRaises(ValueError):
            await bank.account(self.guild, 12, admin=False, land_name="Test Keep")
        await db.update_land_owner(land["id"], 12)
        with self.assertRaises(ValueError):
            await bank.account(self.guild, 10, admin=False, land_name="Test Keep")
        self.assertEqual((await bank.account(self.guild, 12, admin=False, land_name="Test Keep"))["balance"], 30)
        await db.delete_land(land["id"])
        self.assertEqual(await self.balance(account_id=row["id"]), 30)
        self.assertTrue(next(a for a in await bank.accounts(self.guild, admin=True) if a["id"]==row["id"])["archived"])
        await self.land(owner=12)
        self.assertEqual(await self.balance(land_name="Test Keep"), 0)

    async def test_paid_claim_full_quantity_and_retry_are_one_transaction(self):
        land = await self.land()
        approved = await self.approval(land, blocks=2)
        await self.deposit(20, land_name="Test Keep")
        result = await bank.buy_claim(self.guild, 10, request_id=2, payment="bank")
        self.assertEqual((result["unit_price"], result["total"], result["balance"]), (7,14,6))
        self.assertEqual(await self.balance(treasury=True), 14)
        self.assertEqual(await self.state(land), dict(chunks=5,purchases=1,deliveries=1))
        again = await bank.buy_claim(self.guild, 10, request_id=2, payment="bank")
        self.assertTrue(again["duplicate"])
        self.assertEqual(again["operation_id"], result["operation_id"])
        self.assertEqual(await self.balance(land_name="Test Keep"), 6)
        self.assertFalse(await db.update_request_status(approved["id"], "expired"))
        self.assertFalse(await db.update_request_status(approved["id"], "approved"))
        with self.assertRaises(ValueError):
            await bank.buy_claim(self.guild, 10, request_id=2, payment="manual")
        async with db.DBConnection() as conn:
            price = await conn.fetchone("SELECT price_paid FROM purchases WHERE land_id=?", (land["id"],))
            net = await conn.fetchone(f"SELECT SUM(delta) AS net FROM {bank.table('entries')} WHERE guild_id=? AND operation_id=?", (self.guild,result["operation_id"]))
        self.assertEqual(price["price_paid"], 14)
        self.assertEqual(net["net"], 0)

    async def test_shortage_does_not_consume_approval_allowance_or_personal_funds(self):
        land = await self.land()
        await self.approval(land)
        await self.deposit(100, member=10)
        await self.deposit(6, request_id=2, land_name="Test Keep")
        with self.assertRaisesRegex(ValueError, "Insufficient settlement"):
            await bank.buy_claim(self.guild, 10, request_id=3, payment="bank")
        self.assertEqual(await self.state(land), dict(chunks=3,purchases=0,deliveries=0))
        self.assertIsNotNone(await db.get_approved_request(land["id"]))
        self.assertEqual(await self.balance(member=10), 100)
        self.assertEqual(await self.balance(land_name="Test Keep"), 6)
        await self.deposit(1, request_id=4, land_name="Test Keep")
        self.assertEqual((await bank.buy_claim(self.guild, 10, request_id=3, payment="bank"))["balance"], 0)

    async def test_purchase_failures_roll_back_every_write_and_bank_entry(self):
        land = await self.land()
        await self.approval(land)
        await self.deposit(20, land_name="Test Keep")
        original = db.DBConnection.execute

        async def fail_credit(conn, query, params=()):
            if f"INSERT INTO {bank.table('entries')}" in query and params[3] > 0:
                raise RuntimeError("Synthetic treasury write failure")
            await original(conn, query, params)

        with patch.object(db.DBConnection, "execute", fail_credit), self.assertRaises(RuntimeError):
            await bank.buy_claim(self.guild, 10, request_id=2, payment="bank")
        self.assertEqual(await self.state(land), dict(chunks=3,purchases=0,deliveries=0))
        self.assertEqual(await self.balance(land_name="Test Keep"), 20)
        self.assertEqual(await self.balance(treasury=True), 0)
        self.assertIsNotNone(await db.get_approved_request(land["id"]))
        self.assertFalse((await bank.buy_claim(self.guild, 10, request_id=2, payment="bank"))["duplicate"])

    async def test_concurrent_manual_bank_and_withdrawal_keep_invariants(self):
        land = await self.land()
        await self.approval(land)
        await self.deposit(7, land_name="Test Keep")
        results = await asyncio.gather(bank.buy_claim(self.guild, 10, request_id=2, payment="bank"),
                                       bank.buy_claim(self.guild, 10, request_id=3, payment="manual"), return_exceptions=True)
        self.assertEqual(sum(isinstance(r,ValueError) for r in results), 1)
        winner = next(r for r in results if isinstance(r,dict))
        self.assertEqual(await self.state(land), dict(chunks=4,purchases=1,deliveries=1))
        self.assertEqual(await self.balance(land_name="Test Keep"), 0 if winner["payment"]=="bank" else 7)
        second = await self.land(owner=20,name="Second Keep")
        await self.approval(second)
        await self.deposit(7, request_id=4, land_name="Second Keep")
        results = await asyncio.gather(bank.buy_claim(self.guild, 20, request_id=5, payment="bank"),
            bank.adjust(self.guild,900,7,"Approved cash withdrawal",admin=True,request_id=6,withdraw=True,land_name="Second Keep"),return_exceptions=True)
        self.assertEqual(sum(isinstance(r,ValueError) for r in results), 1)
        self.assertEqual(await self.balance(land_name="Second Keep"), 0)

    async def test_overlapping_allocation_does_not_overwrite_paid_claim_chunks(self):
        land=await self.land()
        await self.approval(land)
        await self.deposit(20,land_name="Test Keep")
        await asyncio.gather(bank.buy_claim(self.guild,10,request_id=2,payment="bank"), db.add_land_chunks(land["id"],1))
        self.assertEqual((await self.state(land))["chunks"],5)
        self.assertEqual(await self.balance(land_name="Test Keep"),13)

    async def test_eligibility_free_price_manual_mode_and_week_reset(self):
        land = await self.land(chunks=0)
        await self.approval(land)
        for actor in (11,900):
            with self.assertRaises(ValueError):
                await bank.buy_claim(self.guild,actor,request_id=10,payment="bank")
        with patch.object(bank,"now",return_value=AT+timedelta(days=1)), self.assertRaises(ValueError):
            await bank.buy_claim(self.guild,10,request_id=10,payment="bank")
        free = await bank.buy_claim(self.guild,10,request_id=10,payment="bank")
        self.assertEqual((free["total"],free["balance"]),(0,0))
        await self.approval(land)
        with self.assertRaises(ValueError):
            await bank.buy_claim(self.guild,10,request_id=11)
        with patch.object(bank,"now",return_value=AT+timedelta(days=3)):
            self.assertEqual((await bank.buy_claim(self.guild,10,request_id=11))["payment"],"manual")
        self.assertEqual(await self.balance(treasury=True),0)

    async def test_no_approval_expiry_bad_price_and_manual_pool_purchases(self):
        land = await self.land(chunks=26)
        with self.assertRaises(ValueError):
            await bank.buy_claim(self.guild,10,request_id=1)
        await self.approval(land,age=8,status="expired")
        with self.assertRaises(ValueError):
            await bank.buy_claim(self.guild,10,request_id=1)
        await self.approval(land)
        await db.set_config(self.guild,"full_block_cost","-1")
        with self.assertRaises(ValueError):
            await bank.buy_claim(self.guild,10,request_id=1)
        await db.set_config(self.guild,"full_block_cost","50")
        await db.update_reserve_blocks(self.guild,4)
        await db.update_leftover_blocks(self.guild,1)
        self.assertEqual((await bank.buy_claim(self.guild,10,request_id=2,kind="reserve"))["total"],75)
        self.assertEqual((await bank.buy_claim(self.guild,10,request_id=3,kind="leftover"))["total"],50)
        with self.assertRaises(ValueError):
            await bank.buy_claim(self.guild,10,request_id=4,kind="reserve")
        with self.assertRaises(ValueError):
            await bank.buy_claim(self.guild,10,request_id=4,kind="leftover",payment="bank")
        self.assertEqual(await self.balance(treasury=True),0)


class BankSQLite(BankContract, unittest.IsolatedAsyncioTestCase):
    async def test_profile_request_modal_rechecks_member_quantity_and_claim_mode(self):
        land = await self.land()
        await db.add_member(land["id"], 44)
        await db.set_config(self.guild, "max_queue_chunks", "4")
        member = interaction(self.guild, user=44)
        card = await profile_card(member, land_id=land["id"])
        request = next(btn for btn in card["view"].children if btn.label == "Request banners")
        await request.callback(member)
        choose = member.followup.send.await_args.kwargs["view"].choose
        choose._values = ["vault"]
        await choose.callback(member)
        modal = member.response.send_modal.await_args.args[0]
        modal.quantity._value = "3"
        await db.remove_member(land["id"], 44)
        await modal.on_submit(member)
        self.assertIsNone(await db.get_pending_request(land["id"]))
        await db.add_member(land["id"], 44)
        await db.set_config(self.guild, "max_queue_chunks", "2")
        await modal.on_submit(member)
        self.assertIsNone(await db.get_pending_request(land["id"]))
        await db.set_config(self.guild, "claim_mode", "rotation")
        modal.quantity._value = "2"
        await modal.on_submit(member)
        self.assertIsNone(await db.get_pending_request(land["id"]))
        rotation = await profile_card(member, land_id=land["id"])
        self.assertNotIn("Request banners", [btn.label for btn in rotation["view"].children])
        await db.set_config(self.guild, "claim_mode", "weekly")
        await modal.on_submit(member)
        saved = await db.get_pending_request(land["id"])
        self.assertEqual((saved["chunks_requested"], saved["purpose"]), (2, "vault"))
        labels = [btn.label for btn in member.followup.send.await_args.kwargs["view"].children]
        self.assertIn("View queue position", labels)

    async def test_staff_review_and_closed_dms_leave_the_offer_available_to_the_owner(self):
        land = await self.land()
        request = await db.create_claim_request(land["id"], 2, "vault")
        guild = SimpleNamespace(id=self.guild, name="Guild", owner_id=90)
        click = interaction(self.guild, user=911)
        click.guild = guild
        click.user.guild_permissions = discord.Permissions.none()
        click.edit_original_response = AsyncMock()
        user = SimpleNamespace(send=AsyncMock(side_effect=discord.Forbidden(
            SimpleNamespace(status=403, reason="Forbidden"), "DMs closed")))
        bot = SimpleNamespace(get_user=Mock(return_value=user))
        click.client = bot
        await review_request(click, request, approve=True)
        self.assertIsNotNone(await db.get_pending_request(land["id"]))
        click.user.guild_permissions = discord.Permissions(administrator=True)
        await review_request(click, request, approve=True)
        user.send.assert_awaited_once()
        self.assertIn("approved", click.edit_original_response.await_args.kwargs["content"])
        await notify_offers(bot, guild)
        user.send.assert_awaited_once()
        card = await queue_card(self.guild)
        owner = interaction(self.guild)
        owner.guild = guild
        await card["view"].mine.callback(owner)
        kwargs = owner.followup.send.await_args.kwargs
        self.assertTrue(kwargs["ephemeral"])
        self.assertEqual([item.action for item in kwargs["view"].children], ["bank", "manual", "decline"])
        cash = kwargs["view"].children[1]
        owner.edit_original_response = AsyncMock()
        await cash.callback(owner)
        receipt = owner.edit_original_response.await_args.kwargs["content"]
        self.assertIn("Due in person · **14 ems**", receipt)
        self.assertIn("**2** banners awaiting delivery", receipt)

    async def asyncSetUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.enterContext(patch.object(db,"IS_POSTGRES",False))
        self.enterContext(patch.object(db,"DB_PATH",str(Path(temp.name)/"test.db")))
        self.enterContext(patch.object(bank,"now",return_value=AT))
        self.guild = 100
        await db.init_db()

    async def test_commands_are_private_and_admin_decisions_are_enforced(self):
        cog = BankCog()
        i = interaction(self.guild)
        await cog.interaction_check(i)
        i.response.defer.assert_awaited_once_with(ephemeral=True)
        await cog.balance.callback(cog,i)
        self.assertTrue(i.followup.send.await_args.kwargs["ephemeral"])
        await cog.deposit.callback(cog,i,5,"Unapproved",member=SimpleNamespace(id=10,bot=False))
        self.assertIn("Only Discord administrators",i.followup.send.await_args.kwargs["content"])
        self.assertEqual(await self.balance(member=10),0)
        i.permissions=discord.Permissions(administrator=True)
        await cog.deposit.callback(cog,i,10,"Verified deposit",member=SimpleNamespace(id=10,bot=False))
        self.assertEqual(await self.balance(member=10),10)
        self.assertEqual(i.followup.send.await_args.kwargs["allowed_mentions"].to_dict(),discord.AllowedMentions.none().to_dict())
        await cog.deposit.callback(cog,i,10,"Bot",member=SimpleNamespace(id=11,bot=True))
        self.assertIn("bot account",i.followup.send.await_args.kwargs["content"])

    async def test_claim_bank_receipt_is_private_and_default_does_not_debit(self):
        land = await self.land()
        await self.approval(land)
        await self.deposit(10,land_name="Test Keep")
        cog=ClaimsCog(None)
        i=interaction(self.guild,request_id=2)
        await cog.claim_buy.callback(cog,i,"bank")
        self.assertTrue(i.followup.send.await_args.kwargs["ephemeral"])
        self.assertIn("bank",str(i.followup.send.await_args.kwargs["embed"].to_dict()).lower())
        other=await self.land(owner=20,name="Other")
        await self.approval(other)
        j=interaction(self.guild,user=20,request_id=3)
        await cog.claim_buy.callback(cog,j)
        self.assertFalse(j.followup.send.await_args.kwargs["ephemeral"])
        self.assertNotIn("Settlement balance",str(j.followup.send.await_args.kwargs["embed"].to_dict()))

    async def test_stale_expiry_scan_does_not_return_paid_blocks_to_reserve(self):
        land=await self.land()
        approved=await self.approval(land)
        await bank.buy_claim(self.guild,10,request_id=1)
        cog=DistributionCog(None)
        cog._notify_owner=AsyncMock()
        with patch.object(db,"get_expired_approved_requests",return_value=[approved]):
            returned=await cog._expire_old_approvals(SimpleNamespace(id=self.guild,name="Test"))
        self.assertEqual(returned,0)
        cog._notify_owner.assert_not_awaited()
        self.assertEqual((await db.get_reserve(self.guild))["total_blocks"],0)

    async def test_partial_approval_is_atomic_and_cannot_reopen_a_purchase(self):
        land=await self.land()
        pending=await self.approval(land,blocks=3,status="pending")
        self.assertTrue(await db.approve_partial_request(pending["id"],2))
        result=await bank.buy_claim(self.guild,10,request_id=1)
        self.assertEqual(result["total"],14)
        self.assertFalse(await db.approve_partial_request(pending["id"],3))
        await db.update_request_chunks(pending["id"],3)
        async with db.DBConnection() as conn:
            row=await conn.fetchone("SELECT status,chunks_requested FROM claim_requests WHERE id=?",(pending["id"],))
        self.assertEqual(row,dict(status="purchased",chunks_requested=2))


@unittest.skipUnless(os.environ.get("TEST_DATABASE_URL"),"needs disposable PostgreSQL")
class BankPostgres(BankContract, unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.enterContext(patch.object(db,"IS_POSTGRES",True))
        self.enterContext(patch.object(db,"DATABASE_URL",os.environ["TEST_DATABASE_URL"]))
        self.enterContext(patch.object(db,"_pool",None))
        self.enterContext(patch.object(db,"_pool_lock",asyncio.Lock()))
        self.enterContext(patch.object(bank,"now",return_value=AT))
        self.guild=uuid4().int>>66
        await db.init_db()
        self.addAsyncCleanup(db._pool.close)

    async def test_private_schema_and_rls(self):
        async with db.DBConnection() as conn:
            row=await conn.fetchone("SELECT bool_and(relrowsecurity) AS rls,count(*) AS tables,"
                                   "has_schema_privilege('public','sunset_bank','USAGE') AS public_access "
                                   "FROM pg_class WHERE relnamespace='sunset_bank'::regnamespace AND relkind='r'")
        self.assertEqual(row,dict(rls=True,tables=4,public_access=False))
