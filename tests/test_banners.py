import asyncio
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

import database as db
import bank_store as bank
import banner_store as banners
from cogs.banners import BannerCog
from test_bank import AT, BankContract, interaction


class BannerContract:
    land = BankContract.land
    approval = BankContract.approval
    deposit = BankContract.deposit
    balance = BankContract.balance
    state = BankContract.state

    async def purchase(self, *, chunks=3, blocks=1, payment="manual", funds=0):
        land = await self.land(chunks=chunks)
        await self.approval(land,blocks=blocks)
        if funds:
            await self.deposit(funds,land_name="Test Keep")
        receipt = await bank.buy_claim(self.guild,10,request_id=2,payment=payment)
        return land,receipt

    async def create(self, receipt, *, blocks=1, request_id=10, land_payment="bank"):
        return await banners.create(self.guild,900,receipt["operation_id"],blocks,"Crafted and counted",
                                    admin=True,request_id=request_id,land_payment=land_payment)

    async def totals(self):
        return (await banners.status(self.guild,900,admin=True))["totals"]

    async def test_fixed_split_and_insufficient_land_never_increases_subsidy(self):
        land,r = await self.purchase()
        await self.deposit(100,request_id=3,treasury=True)
        self.assertEqual(await self.totals(),dict(pending=1,total=37,land_share=7,central_share=30,prepaid=0,destroyed=0))
        with self.assertRaisesRegex(ValueError,"Land needs 7"):
            await self.create(r)
        self.assertEqual(await self.balance(treasury=True),100)
        self.assertEqual((await self.totals())["pending"],1)
        await self.deposit(7,request_id=4,land_name="Test Keep")
        made = await self.create(r)
        self.assertEqual((made["land_debit"],made["treasury_debit"],made["total"]),(7,30,37))
        self.assertEqual(await self.balance(land_name="Test Keep"),0)
        self.assertEqual(await self.balance(treasury=True),70)
        self.assertEqual((await self.totals())["destroyed"],37)
        self.assertEqual((await self.state(land))["deliveries"],1)
        self.assertEqual((await db.get_ledger_balance(land["id"]))["outstanding"],1)

    async def test_prepaid_bank_contribution_is_only_debited_once(self):
        _,r = await self.purchase(payment="bank",funds=7)
        self.assertEqual((await self.totals())["prepaid"],7)
        with self.assertRaisesRegex(ValueError,"Central treasury needs 37"):
            await self.create(r)
        await self.deposit(30,request_id=3,treasury=True)
        with self.assertRaisesRegex(ValueError,"already paid"):
            await self.create(r,land_payment="physical")
        made=await self.create(r)
        self.assertEqual((made["land_debit"],made["treasury_debit"]),(0,37))
        self.assertEqual(await self.balance(land_name="Test Keep"),0)
        self.assertEqual(await self.balance(treasury=True),0)
        self.assertTrue((await self.create(r))["duplicate"])
        with self.assertRaisesRegex(ValueError,"Only 0"):
            await self.create(r,request_id=11)
        async with db.DBConnection() as conn:
            burn=await conn.fetchone(f"SELECT SUM(delta) AS delta FROM {bank.table('entries')} WHERE operation_id=?",(made["operation_id"],))
        self.assertEqual(burn["delta"],-37)

    async def test_physical_land_payment_only_burns_fixed_subsidy_from_bank(self):
        _,r=await self.purchase(blocks=2)
        await self.deposit(60,request_id=3,treasury=True)
        made=await self.create(r,blocks=2,land_payment="physical")
        self.assertEqual((made["land_debit"],made["physical"],made["treasury_debit"],made["total"]),(0,14,60,74))
        self.assertEqual(await self.balance(land_name="Test Keep"),0)
        self.assertEqual(await self.balance(treasury=True),0)
        row=await banners.show(self.guild,10,r["operation_id"],admin=False)
        self.assertEqual(row["history"][0]["physical_land_payment"],14)
        with self.assertRaisesRegex(ValueError,"different"):
            await self.create(r,blocks=2,land_payment="bank")

    async def test_partial_creation_concurrency_replay_and_account_liabilities(self):
        _,r=await self.purchase(blocks=3)
        await self.deposit(21,request_id=3,land_name="Test Keep")
        await self.deposit(90,request_id=4,treasury=True)
        results=await asyncio.gather(self.create(r,blocks=2),self.create(r,blocks=2))
        self.assertEqual(sum(x["duplicate"] for x in results),1)
        self.assertEqual((await self.totals())["pending"],1)
        account=await bank.account(self.guild,10,admin=False,land_name="Test Keep")
        self.assertEqual((account["balance"],account["banners"]["land_share"]),(7,7))
        results=await asyncio.gather(self.create(r,request_id=11),self.create(r,request_id=12),return_exceptions=True)
        self.assertEqual(sum(isinstance(x,ValueError) for x in results),1)
        self.assertEqual((await self.totals())["destroyed"],111)
        self.assertEqual(await self.balance(treasury=True),0)

    async def test_free_land_and_excess_sale_price_keep_total_cost_at_37(self):
        _,r=await self.purchase(chunks=0)
        self.assertEqual((r["banner"]["land_unit"],(await self.totals())["central_share"]),(0,37))
        await self.deposit(37,request_id=3,treasury=True)
        self.assertEqual((await self.create(r))["land_debit"],0)
        land=await self.land(owner=20,chunks=26,name="Large Keep")
        await self.approval(land)
        await self.deposit(50,request_id=4,land_name="Large Keep")
        r=await bank.buy_claim(self.guild,20,request_id=5,payment="bank")
        made=await self.create(r,request_id=11)
        self.assertEqual((made["central_share"],made["land_share"]),(0,37))
        self.assertEqual(await self.balance(treasury=True),13)

    async def test_cost_snapshots_permissions_guild_isolation_and_archived_land(self):
        land,r=await self.purchase()
        for func in (banners.cost(self.guild,admin=False,amount=99),
                     banners.status(self.guild,10,admin=False),
                     banners.status(self.guild,11,admin=False,land_name="Test Keep"),
                     banners.show(self.guild,11,r["operation_id"],admin=False),
                     banners.create(self.guild,10,r["operation_id"],1,"No",admin=False,request_id=11),
                     banners.show(self.guild+1,900,r["operation_id"],admin=True)):
            with self.assertRaises(ValueError):
                await func
        await db.add_officer(land["id"],11)
        self.assertEqual((await banners.show(self.guild,11,r["operation_id"],admin=False))["unit_cost"],37)
        await banners.cost(self.guild,admin=True,amount=40)
        second=await self.land(owner=20,name="Second")
        await self.approval(second)
        r2=await bank.buy_claim(self.guild,20,request_id=3)
        self.assertEqual(r2["banner"]["unit_cost"],40)
        await db.delete_land(land["id"])
        with self.assertRaises(ValueError):
            await banners.show(self.guild,10,r["operation_id"],admin=False)
        self.assertEqual((await banners.show(self.guild,900,r["operation_id"],admin=True))["unit_cost"],37)
        await self.deposit(30,request_id=4,treasury=True)
        self.assertEqual((await self.create(r,land_payment="physical"))["total"],37)

    async def test_creation_and_checkout_failures_roll_back_all_effects(self):
        land,r=await self.purchase(funds=7)
        await self.deposit(30,request_id=3,treasury=True)
        original=db.DBConnection.execute
        async def fail(conn,query,params=()):
            if f"INSERT INTO {banners.table('creations')}" in query:
                raise RuntimeError("Creation audit write failure")
            await original(conn,query,params)
        with patch.object(db.DBConnection,"execute",fail),self.assertRaises(RuntimeError):
            await self.create(r)
        self.assertEqual(await self.balance(land_name="Test Keep"),7)
        self.assertEqual(await self.balance(treasury=True),30)
        self.assertEqual((await self.totals())["pending"],1)
        self.assertFalse((await self.create(r))["duplicate"])
        other=await self.land(owner=20,name="Other")
        await self.approval(other)
        with patch.object(banners,"record_batch",side_effect=RuntimeError("Batch write failure")),self.assertRaises(RuntimeError):
            await bank.buy_claim(self.guild,20,request_id=20)
        self.assertEqual(await self.state(other),dict(chunks=3,purchases=0,deliveries=0))
        self.assertIsNotNone(await db.get_approved_request(other["id"]))


class BannerSQLite(BannerContract,unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        temp=tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.enterContext(patch.object(db,"IS_POSTGRES",False))
        self.enterContext(patch.object(db,"DB_PATH",str(Path(temp.name)/"test.db")))
        self.enterContext(patch.object(bank,"now",return_value=AT))
        self.guild=100
        await db.init_db()

    async def test_commands_private_and_creation_requires_administrator(self):
        _,r=await self.purchase()
        cog=BannerCog()
        i=interaction(self.guild)
        await cog.interaction_check(i)
        i.response.defer.assert_awaited_once_with(ephemeral=True)
        await cog.status.callback(cog,i,land_name="Test Keep")
        self.assertTrue(i.followup.send.await_args.kwargs["ephemeral"])
        await cog.create.callback(cog,i,r["operation_id"],1,"Unapproved")
        self.assertIn("Only Discord administrators",i.followup.send.await_args.kwargs["content"])
        self.assertEqual((await self.totals())["pending"],1)


@unittest.skipUnless(os.environ.get("TEST_DATABASE_URL"),"needs disposable PostgreSQL")
class BannerPostgres(BannerContract,unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.enterContext(patch.object(db,"IS_POSTGRES",True))
        self.enterContext(patch.object(db,"DATABASE_URL",os.environ["TEST_DATABASE_URL"]))
        self.enterContext(patch.object(db,"_pool",None))
        self.enterContext(patch.object(db,"_pool_lock",asyncio.Lock()))
        self.enterContext(patch.object(bank,"now",return_value=AT))
        self.guild=uuid4().int>>66
        await db.init_db()
        self.addAsyncCleanup(db._pool.close)

    async def test_schema_is_private_and_rls_enabled(self):
        async with db.DBConnection() as conn:
            row=await conn.fetchone("SELECT bool_and(relrowsecurity) AS rls,count(*) AS tables,"
                "has_schema_privilege('public','sunset_banners','USAGE') AS public_access "
                "FROM pg_class WHERE relnamespace='sunset_banners'::regnamespace AND relkind='r'")
        self.assertEqual(row,dict(rls=True,tables=2,public_access=False))
