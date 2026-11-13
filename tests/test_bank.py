"""Bank invariants and claim checkout on SQLite and disposable PostgreSQL."""

import asyncio
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import discord

import database as db
import bank_store as bank
from cogs.bank import BankCog
from cogs.claims import ClaimsCog
from cogs.distribution import DistributionCog


AT = datetime(2026, 11, 13, 12, tzinfo=timezone.utc)


def interaction(guild, user=10, admin=False, request_id=100):
    return SimpleNamespace(id=request_id, guild_id=guild, user=SimpleNamespace(id=user, roles=[]),
                           permissions=discord.Permissions(administrator=admin),
                           response=AsyncMock(), followup=AsyncMock())


class BankContract:
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
