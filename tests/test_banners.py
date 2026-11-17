import asyncio
import json
from datetime import timedelta
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

import database as db
import bank_store as bank
import banner_store as banners
import banner_account as public
from cogs.bank import BankCog
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

    async def test_public_price_results_stock_and_cash(self):
        opening = await public.statement(self.guild)
        self.assertEqual((opening["produced"], opening["unsold"], opening["liability"]), (7, 7, 259))
        land, reduced = await self.purchase(blocks=2, payment="bank", funds=14)
        await bank.buy_claim(self.guild,10,request_id=2,payment="bank")
        large = await self.land(owner=20, chunks=26, name="Large Keep")
        await self.approval(large)
        full = await bank.buy_claim(self.guild,20,request_id=3)
        await db.update_reserve_blocks(self.guild,4)
        reserve = await bank.buy_claim(self.guild,10,request_id=4,kind="reserve")
        self.assertEqual([r["sale_result"] for r in (full,reduced,reserve)], [0,-60,19])
        self.assertEqual(reduced["released_blocks"]*reduced["banner"]["unit_cost"],74)
        r = await public.statement(self.guild)
        self.assertEqual((r["unsold"],r["liability"],r["treasury"]),(3,111,14))
        self.assertEqual(r["position"],-152)
        self.assertEqual(r["sales"]["reduced"]["transferred"],14)
        self.assertEqual(r["sales"]["reserve"]["transferred"],0)
        receipt = await self.create(reserve,land_payment="physical")
        self.assertEqual((receipt["physical"], receipt["treasury_debit"]), (56,-19))
        self.assertTrue((await self.create(reserve,land_payment="physical"))["duplicate"])
        after = await public.statement(self.guild)
        self.assertEqual((after["liability"],after["treasury"],after["sales"]),(111,33,r["sales"]))
        self.assertEqual((await public.statement(self.guild+1))["liability"],259)

    async def test_reserve_bank_sale_and_unfunded_attempt(self):
        await self.land(chunks=0)
        await db.update_reserve_blocks(self.guild,4)
        with self.assertRaisesRegex(ValueError,"Insufficient"):
            await bank.buy_claim(self.guild,10,request_id=2,kind="reserve",payment="bank")
        self.assertEqual((await public.statement(self.guild))["unsold"],7)
        await self.deposit(56,land_name="Test Keep")
        r = await bank.buy_claim(self.guild,10,request_id=2,kind="reserve",payment="bank")
        await self.create(r)
        self.assertEqual(await self.balance(treasury=True),19)
        self.assertEqual((await public.statement(self.guild))["sales"]["reserve"]["transferred"],56)

    async def test_manual_reserve_collects_full_price_from_land_at_settlement(self):
        await self.land(chunks=0)
        await db.update_reserve_blocks(self.guild,4)
        await self.deposit(37,land_name="Test Keep")
        r = await bank.buy_claim(self.guild,10,request_id=2,kind="reserve")
        self.assertEqual((await self.totals())["premium_due"],19)
        with self.assertRaisesRegex(ValueError,"Land needs 56"):
            await self.create(r)
        await self.deposit(19,request_id=3,land_name="Test Keep")
        settled = await self.create(r)
        self.assertEqual((settled["land_debit"],settled["treasury_debit"]),(56,-19))
        self.assertEqual((await self.balance(treasury=True),await self.balance(land_name="Test Keep")),(19,0))
        self.assertEqual((await self.totals())["premium_due"],0)

    async def test_production_carry_and_sales_beyond_the_weekly_allowance(self):
        await self.purchase(blocks=9,chunks=26)
        await public.set_weekly_production(self.guild,3)
        opening = await public.statement(self.guild)
        self.assertEqual((opening["unsold"], opening["sales"]["full"]["released"]), (0,7))
        with patch.object(bank,"now",return_value=AT+timedelta(weeks=2)):
            await asyncio.gather(public.weekly_production(self.guild), public.weekly_production(self.guild))
            week = await public.statement(self.guild)
            lifetime = await public.statement(self.guild,all_time=True)
            self.assertEqual((week["produced"],week["sales"],week["unsold"]),(3,{},6))
            self.assertEqual((lifetime["produced"],lifetime["sales"]["full"]["sold"]),(13,9))
            await banners.cost(self.guild,admin=True,amount=50)
            r = await public.statement(self.guild,all_time=True)
            self.assertEqual((r["liability"],r["sales"]["full"]["cost"]),(300,333))

    async def test_opening_week_imports_actual_old_prices_without_recharging(self):
        land = await self.land()
        previous = dict(kind="normal", payment="manual", total=100, blocks=2, banner=dict(unit_cost=37))
        async with db.DBConnection() as conn:
            await conn.execute(f"INSERT INTO {bank.table('operations')} (guild_id,request_id,actor_id,action,reason,created_at,receipt) "
                               "VALUES (?,?,?,'claim-normal','legacy',?,?)", (self.guild,1,10,AT.isoformat(),json.dumps(previous)))
            for at in (AT,AT-timedelta(weeks=1)):
                await conn.execute("INSERT INTO purchases (land_id,type,price_paid,purchased_at) VALUES (?,'rotation',7,?)",
                                   (land["id"],at.isoformat()))
        await db.record_purchase(land["id"],"strategic",0)
        report = await public.statement(self.guild)
        self.assertEqual((report["unsold"], report["treasury"]), (3,0))
        self.assertEqual(report["sales"]["other"]["revenue"],100)
        self.assertEqual((report["sales"]["reduced"]["sold"],report["sales"]["reduced"]["revenue"]), (2,7))
        self.assertEqual((await self.totals())["pending"],0)

    async def test_rotation_auction_and_strategic_grants_book_each_allocation_once(self):
        land = await self.land()
        land_id = land["id"]
        offer = await db.create_rotation_offer(self.guild,land_id)
        with patch.object(public,"record_sale",side_effect=RuntimeError("failed journal")), self.assertRaises(RuntimeError):
            await public.accept_rotation(self.guild,offer,land_id,2,7,37,900)
        self.assertEqual((await self.state(land))["chunks"],3)
        self.assertEqual(await public.accept_rotation(self.guild,offer,land_id,2,7,37,900),5)
        with self.assertRaises(ValueError):
            await public.accept_rotation(self.guild,offer,land_id,2,7,37,900)
        await db.update_reserve_blocks(self.guild,5)
        await public.strategic_grant(self.guild,land_id,900,77)
        with self.assertRaises(ValueError):
            await public.strategic_grant(self.guild,land_id,900,77)
        auction = await db.create_auction(self.guild,2,AT.isoformat(),AT.isoformat())
        await db.place_auction_bid(auction,land_id,60)
        await public.settle_auction(self.guild,auction,900)
        self.assertIsNone(await public.settle_auction(self.guild,auction,900))
        r = await public.statement(self.guild)
        self.assertEqual((r["unsold"],r["sales"]["reserve"]["revenue"],r["sales"]["reduced"]["sold"]),(3,60,3))
        self.assertEqual((await db.get_reserve(self.guild))["total_blocks"],3)
        self.assertEqual(await self.state(land),dict(chunks=7,purchases=4,deliveries=2))

    async def test_fixed_split_and_insufficient_land_never_increases_subsidy(self):
        land,r = await self.purchase()
        await self.deposit(100,request_id=3,treasury=True)
        self.assertEqual(await self.totals(),dict(pending=1,total=37,land_share=7,central_share=30,prepaid=0,destroyed=0,premium_due=0))
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

    async def test_free_land_subsidy_and_full_price_sale_at_cost(self):
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
        self.assertEqual(await self.balance(treasury=True),0)
        self.assertEqual(await self.balance(land_name="Large Keep"),13)

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

    async def test_public_command_publishes_only_aggregate_figures(self):
        await self.purchase(payment="bank", funds=20)
        cog, i = BankCog(), interaction(self.guild)
        i.command = cog.public_account
        await cog.interaction_check(i)
        await cog.public_account.callback(cog,i)
        i.response.defer.assert_awaited_once_with(ephemeral=False)
        reply = i.followup.send.await_args.kwargs
        self.assertFalse(reply["ephemeral"])
        contents = str(reply["embed"].to_dict())
        self.assertIn("222 ems liability", contents)
        self.assertNotIn("Test Keep", contents)
        self.assertNotIn("Counted deposit", contents)


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
        self.assertEqual(row,dict(rls=True,tables=4,public_access=False))
