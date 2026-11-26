import asyncio
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, PropertyMock, patch
from uuid import uuid4

import discord

import acclimation_store as store
import database as db
from cogs import quest_browser as browser
from cogs.acclimations import QuestCog


def interaction(user=10,guild=100,channel=50):
    return SimpleNamespace(id=100,user=SimpleNamespace(id=user),guild_id=guild,channel_id=channel,
        permissions=discord.Permissions.none(),response=AsyncMock(),followup=AsyncMock(),edit_original_response=AsyncMock())


class QueryContract:
    async def quest(self,title="Repair roads",details="Northern road repairs",guild=None,request_id=1):
        return await store.create_quest(guild or self.guild,900,title,details,3,admin=True,request_id=request_id)

    async def completion_review(self, i, quest, recipient, deed):
        _, detail = await browser.render(i, quest_id=quest["id"])
        await detail.complete.callback(i)
        picker = i.edit_original_response.await_args.kwargs["view"]
        person = SimpleNamespace(id=recipient, guild=i.guild, bot=False)
        i.guild.get_member.return_value = person
        with patch.object(type(picker.member), "values", new_callable=PropertyMock, return_value=[person]):
            await picker.member.callback(i)
        form = i.response.send_modal.await_args.args[0]
        form.deed._value = deed
        await form.on_submit(i)
        return i.edit_original_response.await_args.kwargs

    async def test_unique_claim_race_closes_once_and_replay_survives_closure(self):
        quest = await store.create_quest(self.guild, 900, "Find the crown", "Recover the lost crown", 7,
                                         admin=True, request_id=1, quest_type="unique")
        async def finish(member):
            return await store.complete_quest(self.guild, 900, quest["id"], member, "Recovered the crown",
                                              admin=True, request_id=member)
        results = await asyncio.gather(finish(21), finish(22), return_exceptions=True)
        self.assertEqual(sum(isinstance(r, ValueError) for r in results), 1)
        winner = next(r for r in results if isinstance(r, dict))
        self.assertTrue(winner["closed_unique_quest"])
        replay = await finish(winner["recipient_id"])
        self.assertEqual(replay["id"], winner["id"])
        self.assertTrue(replay["duplicate"])
        await store.void_award(self.guild, 900, winner["id"], "Correction", admin=True)
        self.assertEqual((await store.get_quest(self.guild, quest["id"]))["status"], "closed")
        self.assertEqual((await store.quest_board(self.guild, 21))["total"], 0)
        with self.assertRaises(ValueError):
            await finish(23)

    async def test_repeatable_keeps_each_deed_the_board_and_retries_consistent(self):
        quest = await store.create_quest(self.guild, 900, "Stock the depot", "Deliver a load", 3,
                                         admin=True, request_id=1, quest_type="repeatable")
        staff = interaction(900, self.guild)
        staff.guild = SimpleNamespace(id=self.guild, get_member=Mock())
        staff.permissions = discord.Permissions(administrator=True)
        staff.id = 51
        first = await self.completion_review(staff, quest, 21, "Delivered oak")
        await first["view"].confirm.callback(staff)
        replay = await store.complete_quest(self.guild, 900, quest["id"], 21, "Delivered oak", admin=True, request_id=51)
        self.assertTrue(replay["duplicate"])
        second = await self.completion_review(staff, quest, 21, "Delivered birch")
        stale = await self.completion_review(staff, quest, 21, "Delivered birch")
        self.assertIn("#2", second["embed"].description)
        staff.id = 52
        await second["view"].confirm.callback(staff)
        staff.id = 53
        await stale["view"].confirm.callback(staff)
        self.assertIn("Another completion", staff.edit_original_response.await_args.kwargs["content"])
        await store.void_award(self.guild, 900, replay["id"], "Partial load", admin=True)
        board = await store.quest_board(self.guild, 21, unrewarded=True)
        self.assertEqual((board["total"], len(board["rows"]), board["rows"][0]["completion_no"]), (1, 1, 2))
        await store.close_quest(self.guild, 900, quest["id"], admin=True)
        player = interaction(21, self.guild)
        _, view = await browser.render(player, quest_id=quest["id"])
        self.assertEqual(view.deed.label, "View my deeds")
        await view.deed.callback(player)
        latest = player.edit_original_response.await_args.kwargs
        self.assertIn("Delivered birch", str(latest["embed"].to_dict()))
        await latest["view"].older.callback(player)
        older = player.edit_original_response.await_args.kwargs
        self.assertIn("Delivered oak", str(older["embed"].to_dict()))
        self.assertIn("Partial load", str(older["embed"].to_dict()))
        self.assertTrue(older["view"].older.disabled)
        await older["view"].newer.callback(player)
        self.assertTrue(player.edit_original_response.await_args.kwargs["view"].newer.disabled)
        self.assertEqual((await store.profile(self.guild, 21))["total"], 3)

    async def test_type_filter_combines_with_eligibility_and_survives_back(self):
        await self.quest(title="Road survey")
        q = await store.create_quest(self.guild, 900, "Road cargo", "Deliver a load", 1,
                                     admin=True, request_id=2, quest_type="repeatable")
        await store.complete_quest(self.guild, 900, q["id"], 21, "First load", admin=True, request_id=3)
        player = interaction(21, self.guild)
        _, view = await browser.render(player, query="Road", unrewarded=True)
        menu = next(c for c in view.children if isinstance(c, browser.QuestTypeSelect))
        menu._values = ["repeatable"]
        await menu.callback(player)
        listing = player.edit_original_response.await_args.kwargs
        self.assertNotIn("Road survey", listing["embed"].description)
        select = next(c for c in listing["view"].children if isinstance(c, browser.QuestSelect))
        self.assertEqual(len(select.options), 1)
        select._values = [str(q["id"])]
        await select.callback(player)
        await player.edit_original_response.await_args.kwargs["view"].back.callback(player)
        returned = player.edit_original_response.await_args.kwargs["view"]
        self.assertEqual((returned.quest_type, returned.query, returned.unrewarded), ("repeatable", "Road", True))

    async def test_quest_controls_record_once_close_separately_and_show_only_my_deed(self):
        quest = await self.quest()
        hand = interaction(20, self.guild)
        hand.guild = SimpleNamespace(id=self.guild, get_member=Mock())
        await store.appoint(self.guild, 900, 20, "hand", admin=True)
        recipient = interaction(21, self.guild)
        _, before = await browser.render(recipient, quest_id=quest["id"])
        self.assertFalse(any(b in before.children for b in (before.complete, before.close, before.deed)))
        review = await self.completion_review(hand, quest, 21, "Rebuilt the north bridge")
        self.assertIn("3 Acclimations", review["embed"].description)
        self.assertIn("Rebuilt the north bridge", str(review["embed"].to_dict()))
        self.assertEqual((await store.profile(self.guild, 21))["total"], 0)
        confirm = review["view"].confirm
        await confirm.callback(interaction(25, self.guild))
        self.assertEqual((await store.profile(self.guild, 21))["total"], 0)
        await confirm.callback(hand)
        saved = hand.edit_original_response.await_args.kwargs
        self.assertIn("Acclimation recorded", saved["content"])
        self.assertEqual((await store.get_quest(self.guild, quest["id"]))["status"], "open")
        await confirm.callback(hand)
        self.assertEqual((await store.profile(self.guild, 21))["total"], 3)
        await saved["view"].close.callback(hand)
        closure = hand.edit_original_response.await_args.kwargs["view"]
        self.assertEqual(closure.confirm.label, "Confirm closure")
        await closure.cancel.callback(hand)
        self.assertEqual((await store.get_quest(self.guild, quest["id"]))["status"], "open")
        await hand.edit_original_response.await_args.kwargs["view"].close.callback(hand)
        await hand.edit_original_response.await_args.kwargs["view"].confirm.callback(hand)
        closed = hand.edit_original_response.await_args.kwargs["view"]
        self.assertNotIn(closed.close, closed.children)
        self.assertNotIn(closed.complete, closed.children)
        _, earned = await browser.render(recipient, quest_id=quest["id"])
        self.assertIn(earned.deed, earned.children)
        await earned.deed.callback(recipient)
        deed = str(recipient.edit_original_response.await_args.kwargs["embed"].to_dict())
        self.assertIn("Rebuilt the north bridge", deed)
        self.assertIn("Your current total", deed)
        self.assertIsNone(await store.quest_deed(self.guild, hand.user.id, quest["id"]))
        self.assertIsNone(await store.quest_deed(self.guild + 1, 21, quest["id"]))
        self.assertNotIn(closed.deed, closed.children)
        award = (await store.history(self.guild, 21))[0]
        await store.void_award(self.guild, 900, award["id"], "Entered twice in error", admin=True)
        _, after_void = await browser.render(recipient, quest_id=quest["id"])
        await after_void.deed.callback(recipient)
        corrected = str(recipient.edit_original_response.await_args.kwargs["embed"].to_dict())
        self.assertIn("Voided award", corrected)
        self.assertIn("0 Acclimations", corrected)

    async def test_stale_completion_checks_appointment_reward_and_quest_status(self):
        quest = await self.quest()
        actor = interaction(20, self.guild)
        actor.guild = SimpleNamespace(id=self.guild, get_member=Mock())
        await store.appoint(self.guild, 900, 20, "hand", admin=True)
        review = await self.completion_review(actor, quest, 21, "Finished")
        await store.appoint(self.guild, 900, 20, "dismissed", admin=True)
        await review["view"].confirm.callback(actor)
        self.assertIn("Only appointed", actor.edit_original_response.await_args.kwargs["content"])
        actor.permissions = discord.Permissions(administrator=True)
        review = await self.completion_review(actor, quest, 21, "Finished")
        async with db.DBConnection() as conn:
            await conn.execute(f"UPDATE {store._table('quests')} SET reward=8 WHERE id=? AND guild_id=?", (quest["id"], self.guild))
        await review["view"].confirm.callback(actor)
        self.assertIn("reward changed", actor.edit_original_response.await_args.kwargs["content"])
        review = await self.completion_review(actor, quest, 21, "Finished")
        await store.close_quest(self.guild, 900, quest["id"], admin=True)
        await review["view"].confirm.callback(actor)
        self.assertIn("No open quest", actor.edit_original_response.await_args.kwargs["content"])
        self.assertEqual((await store.profile(self.guild, 21))["total"], 0)

    async def test_picker_rejects_bots_self_and_foreign_members_and_cancel_invalidates_form(self):
        quest = await self.quest()
        actor = interaction(900, self.guild)
        actor.permissions = discord.Permissions(administrator=True)
        actor.guild = SimpleNamespace(id=self.guild)
        _, detail = await browser.render(actor, quest_id=quest["id"])
        await detail.complete.callback(actor)
        picker = actor.edit_original_response.await_args.kwargs["view"]
        for uid, guild_id, bot in ((900, self.guild, False), (33, self.guild, True), (33, self.guild+1, False)):
            person = SimpleNamespace(id=uid, guild=SimpleNamespace(id=guild_id), bot=bot)
            with patch.object(type(picker.member), "values", new_callable=PropertyMock, return_value=[person]):
                await picker.member.callback(actor)
            actor.response.send_modal.assert_not_awaited()
        player = SimpleNamespace(id=33, guild=actor.guild, bot=False)
        with patch.object(type(picker.member), "values", new_callable=PropertyMock, return_value=[player]):
            await picker.member.callback(actor)
        form = actor.response.send_modal.await_args.args[0]
        await picker.cancel.callback(actor)
        actor.edit_original_response.reset_mock()
        form.deed._value = "Stale form"
        await form.on_submit(actor)
        actor.edit_original_response.assert_not_awaited()
        self.assertIsNone(await store.quest_deed(self.guild, 33, quest["id"]))

    async def test_pages_search_and_closure_are_server_scoped(self):
        for n in range(12):
            await self.quest(title=f"Road work {n}",request_id=n+1)
        await self.quest(title="Other server road",guild=self.guild+1)
        first=await store.quest_board(self.guild,10,query="road")
        self.assertEqual((first["total"],first["pages"],len(first["rows"])),(12,2,10))
        last=await store.quest_board(self.guild,10,page=99,query="road")
        self.assertEqual((last["page"],len(last["rows"])),(2,2))
        for row in last["rows"]:
            await store.close_quest(self.guild,900,row["id"],admin=True)
        refreshed=await store.quest_board(self.guild,10,page=2,query="road")
        self.assertEqual((refreshed["page"],refreshed["total"]),(1,10))
        self.assertIsNone(await store.quest_for_reader(self.guild+1,10,first["rows"][0]["id"]))

    async def test_reward_filter_includes_voided_claims_as_used(self):
        q=await self.quest()
        other=await self.quest(title="Another task",request_id=2)
        award=await store.complete_quest(self.guild,900,q["id"],10,"Finished the road",admin=True,request_id=3)
        row=await store.quest_for_reader(self.guild,10,q["id"])
        self.assertEqual(row["award_id"],award["id"])
        self.assertIsNone((await store.quest_for_reader(self.guild,11,q["id"]))["award_id"])
        await store.void_award(self.guild,900,award["id"],"Correction",admin=True)
        board=await store.quest_board(self.guild,10,unrewarded=True)
        self.assertEqual([r["id"] for r in board["rows"]],[other["id"]])
        self.assertIsNotNone((await store.quest_for_reader(self.guild,10,q["id"]))["award_voided_at"])
        self.assertEqual((await store.quest_board(self.guild,11,unrewarded=True))["total"],2)

    async def test_literal_search_ids_requirements_and_bounded_suggestions(self):
        q=await self.quest(title="100%_repair!",details="Bring stone to the monument")
        for term in ("%_", "MONUMENT",str(q["id"]),f"#{q['id']}"):
            self.assertEqual((await store.quest_board(self.guild,10,query=term))["total"],1)
        for n in range(26):
            await self.quest(title=f"Quest {n}",request_id=n+2)
        await store.close_quest(self.guild,900,q["id"],admin=True)
        suggestions=await store.quest_suggestions(self.guild,"")
        self.assertEqual(len(suggestions),25)
        self.assertTrue(all(r["status"]=="open" for r in suggestions))
        self.assertEqual((await store.quest_suggestions(self.guild,"%_"))[0]["status"],"closed")
        self.assertEqual(await store.quest_suggestions(self.guild+1,""),[])
        with self.assertRaises(ValueError):
            await store.quest_board(self.guild,10,query="x"*81)


class QuestBrowserSQLite(QueryContract,unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        temp=tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.enterContext(patch.object(db,"IS_POSTGRES",False))
        self.enterContext(patch.object(db,"DB_PATH",str(Path(temp.name)/"test.db")))
        self.guild=100
        await db.init_db()

    async def panel(self,i=None,**target):
        embed,view=await browser.render(i or interaction(),**target)
        self.addCleanup(view.stop)
        return embed,view

    def edited(self,i):
        payload=i.edit_original_response.await_args.kwargs
        if payload.get("view"):
            self.addCleanup(payload["view"].stop)
        return payload

    async def test_select_details_back_pagination_and_filter_preserve_context(self):
        for n in range(12):
            await self.quest(title=f"Road {n}",request_id=n+1)
        i=interaction()
        _,view=await self.panel(i,page=2,query="Road",unrewarded=True)
        self.assertFalse(view.previous.disabled)
        self.assertTrue(view.next_page.disabled)
        select=next(c for c in view.children if isinstance(c,browser.QuestSelect))
        select._values=[select.options[0].value]
        await select.callback(i)
        payload=self.edited(i)
        self.assertTrue(view.is_finished())
        self.assertEqual(payload["embed"].description,"Northern road repairs")
        detail=payload["view"]
        self.assertEqual((detail.page,detail.query,detail.unrewarded),(2,"Road",True))
        await detail.back.callback(i)
        listed=self.edited(i)["view"]
        self.assertEqual((listed.page,listed.query,listed.unrewarded),(2,"Road",True))
        await listed.previous.callback(i)
        page_one=self.edited(i)["view"]
        self.assertTrue(page_one.previous.disabled)
        self.assertFalse(page_one.next_page.disabled)
        filters=next(c for c in page_one.children if isinstance(c,browser.FilterSelect))
        filters._values=["all"]
        await filters.callback(i)
        self.assertFalse(self.edited(i)["view"].unrewarded)
        self.assertEqual((await store.profile(self.guild,10))["total"],0)

    async def test_refresh_reads_closed_and_changed_reward_status(self):
        q=await self.quest()
        i=interaction()
        _,view=await self.panel(i,quest_id=q["id"])
        award=await store.complete_quest(self.guild,900,q["id"],10,"Done",admin=True,request_id=2)
        await store.void_award(self.guild,900,award["id"],"Correction",admin=True)
        await store.close_quest(self.guild,900,q["id"],admin=True)
        await view.refresh.callback(i)
        payload=self.edited(i)
        self.assertEqual(payload["embed"].fields[0].value,"Closed")
        self.assertIn("voided",str(payload["embed"].to_dict()))
        await payload["view"].back.callback(i)
        self.assertIn("No open quests",self.edited(i)["embed"].description)

    async def test_reader_guild_channel_boundaries_expiry_and_stale_clicks(self):
        await self.quest()
        i=interaction()
        _,view=await self.panel(i)
        for outsider in (interaction(user=11),interaction(guild=101),interaction(channel=51)):
            await view.refresh.callback(outsider)
            outsider.edit_original_response.assert_not_awaited()
            self.assertTrue(outsider.response.send_message.await_args.kwargs["ephemeral"])
        await view.on_timeout()
        self.assertTrue(all(c.disabled for c in view.children))
        i.edit_original_response.reset_mock()
        await view.refresh.callback(i)
        i.edit_original_response.assert_not_awaited()

    async def test_concurrent_clicks_cannot_overwrite_the_new_panel(self):
        await self.quest()
        i=interaction()
        _,view=await self.panel(i)
        entered,release=asyncio.Event(),asyncio.Event()
        async def defer():
            entered.set()
            await release.wait()
        i.response.defer.side_effect=defer
        first=asyncio.create_task(view.navigate(i))
        await entered.wait()
        second=interaction()
        await view.navigate(second)
        second.edit_original_response.assert_not_awaited()
        release.set()
        await first
        i.edit_original_response.assert_awaited_once()
        self.edited(i)

    async def test_failures_clear_controls_and_do_not_expose_diagnostics(self):
        q=await self.quest()
        i=interaction()
        _,view=await self.panel(i)
        with patch.object(store,"quest_board",side_effect=RuntimeError("private diagnostic")):
            await view.refresh.callback(i)
            payload=self.edited(i)
            self.assertEqual(payload["content"],browser.TEMPORARY)
            self.assertIsNone(payload["view"])
            self.assertIsNone(payload["embed"])
            await browser.open_board(i)
            self.assertEqual(i.followup.send.await_args.args,(browser.TEMPORARY,))
        _,view=await self.panel(i)
        await view.navigate(i,quest_id=q["id"]+9999)
        self.assertEqual(self.edited(i)["content"],browser.UNAVAILABLE)
        with patch.object(store,"quest_suggestions",side_effect=RuntimeError("private diagnostic")):
            self.assertEqual(await browser.choices(i,""),[])

    async def test_commands_are_private_autocomplete_accepts_titles_and_old_ids(self):
        q=await self.quest()
        i=interaction()
        cog=QuestCog()
        await cog.interaction_check(i)
        i.response.defer.assert_awaited_once_with(ephemeral=True)
        await cog.quest_list.callback(cog,i,query="repair")
        self.addCleanup(i.followup.send.await_args.kwargs["view"].stop)
        self.assertTrue(i.followup.send.await_args.kwargs["ephemeral"])
        self.assertEqual(i.followup.send.await_args.kwargs["allowed_mentions"].to_dict(),discord.AllowedMentions.none().to_dict())
        choices=await cog.show_choices(i,"repair")
        self.assertEqual(choices[0].value,str(q["id"]))
        self.assertIn(q["title"],choices[0].name)
        self.assertEqual(await browser.choices(interaction(guild=None),""),[])
        for value in (str(q["id"]),q["id"],f"#{q['id']}"):
            await cog.show.callback(cog,i,value)
            self.addCleanup(i.followup.send.await_args.kwargs["view"].stop)
            self.assertIn(str(q["id"]),i.followup.send.await_args.kwargs["embed"].title)
        await cog.show.callback(cog,i,"unselected title")
        self.assertIn("Choose a suggested quest",i.followup.send.await_args.kwargs["content"])
        self.assertEqual(cog.show.to_dict(cog.__cog_app_commands_group__)["options"][0]["type"],3)

    async def test_unicode_and_escaped_text_fit_all_discord_limits(self):
        for n in range(10):
            await self.quest(title=("😀" if n%2 else "*")*80,details="*"*700,request_id=n+1)
        embed,view=await self.panel()
        for embed in (embed,(await self.panel(quest_id=(await store.quests(self.guild))[0]["id"]))[0]):
            self.assertLessEqual(len(embed.description.encode("utf-16-le"))//2,4096)
            self.assertLessEqual(len(embed),6000)
            self.assertLessEqual(len(embed.title.encode("utf-16-le"))//2,256)
        select=next(c for c in view.children if isinstance(c,browser.QuestSelect))
        self.assertEqual(len(select.options),10)
        for option in select.options:
            self.assertLessEqual(len(option.label.encode("utf-16-le"))//2,100)
        for choice in await browser.choices(interaction(),""):
            self.assertLessEqual(len(choice.name.encode("utf-16-le"))//2,100)


@unittest.skipUnless(os.environ.get("TEST_DATABASE_URL"),"needs disposable PostgreSQL")
class QuestBrowserPostgres(QueryContract,unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.enterContext(patch.object(db,"IS_POSTGRES",True))
        self.enterContext(patch.object(db,"DATABASE_URL",os.environ["TEST_DATABASE_URL"]))
        self.enterContext(patch.object(db,"_pool",None))
        self.enterContext(patch.object(db,"_pool_lock",asyncio.Lock()))
        self.guild=uuid4().int>>66
        await db.init_db()
        self.addAsyncCleanup(db._pool.close)
