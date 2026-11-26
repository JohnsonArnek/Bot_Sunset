import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import discord

import database as db
import handbook_store
import acclimation_store as acclimations
import quest_submissions
from cogs.attention import attention_card
from cogs.start_panel import StartButton, panel_card
from cogs.sunset import SunsetCog


def reader(user_id=10, *, guild=100, channel=None, roles=()):
    channel = channel or Mock(spec=discord.TextChannel, id=250)
    permissions = discord.Permissions.none()
    return SimpleNamespace(
        id=discord.utils.time_snowflake(discord.utils.utcnow()),
        guild=SimpleNamespace(id=guild, owner_id=900, name="Test server"),
        guild_id=guild, channel=channel, channel_id=channel.id, client=SimpleNamespace(user=SimpleNamespace(id=99)),
        user=SimpleNamespace(id=user_id, display_name="Reader", guild_permissions=permissions,
                             roles=[SimpleNamespace(id=role) for role in roles]),
        permissions=permissions, response=AsyncMock(), followup=AsyncMock(), edit_original_response=AsyncMock())


def posted_message(message_id, *, author=99):
    button = SimpleNamespace(custom_id="sunset:start:settlement")
    message = SimpleNamespace(id=message_id, author=SimpleNamespace(id=author), pinned=False,
                              components=[SimpleNamespace(children=[button])], jump_url=f"https://discord.com/channels/100/250/{message_id}")
    message.pin = AsyncMock(side_effect=lambda **kwargs: setattr(message, "pinned", True))
    message.edit = AsyncMock()
    return message


class StartPanelTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.enterContext(patch.object(db, "IS_POSTGRES", False))
        self.enterContext(patch.object(db, "DB_PATH", str(Path(temp.name) / "panel.db")))
        await db.init_db()

    async def test_attention_routes_current_owner_reviewer_and_ledger_staff(self):
        await db.create_land(100, "Hill", 10)
        land = await db.get_land_by_owner(100, 10)
        req = await db.create_claim_request(land["id"], 1, "Builds")
        await db.update_request_status(req, "approved")
        await db.add_ledger_owed(land["id"], 100, 1)
        await db.set_config(100, "staff_role_id", "77")
        q = await acclimations.create_quest(100, 900, "Deliver oak", "Bring supplies", 2, admin=True, request_id=30)
        deed = await quest_submissions.submit(100, 10, q["id"], "Brought oak", None, request_id=31)
        await acclimations.appoint(100, 900, 11, "hand", admin=True)
        owner, hand, staff = reader(10), reader(11), reader(12, roles=(77,))
        self.assertIn("Weekly offer", (await attention_card(owner))[0].description)
        self.assertEqual((await attention_card(owner, selection=("offer", req)))[0].title, "Weekly banner allocation")
        self.assertNotIn("Weekly offer", (await attention_card(hand))[0].description)
        self.assertIn("Review deed", (await attention_card(hand))[0].description)
        self.assertIn("Settlement ledger", (await attention_card(staff))[0].description)
        self.assertIn("Hill", (await attention_card(staff, selection=("ledger", land["id"])))[0].title)
        self.assertNotIn("Brought oak", str((await attention_card(staff, selection=("review", deed["id"])))[0].to_dict()))
        self.assertIn("caught up", (await attention_card(reader(13)))[0].description)
        await quest_submissions.return_deed(100, 11, deed["id"], "Please give a location", admin=False)
        self.assertIn("Revise deed", (await attention_card(owner))[0].description)
        self.assertIn("Please give a location", str((await attention_card(owner, selection=("revise", deed["id"])))[0].to_dict()))

    async def test_persistent_buttons_open_private_screens_for_the_current_reader(self):
        client = Mock()
        cog = SunsetCog(client)
        await cog.cog_load()
        client.add_dynamic_items.assert_called_once_with(StartButton)
        await db.create_land(100, "Hill", 10)
        await db.create_land(200, "Foreign", 22)
        await handbook_store.import_articles(100, [dict(slug="example", book="citizens", title="Example",
                                                       summary="Test article", pages=["Test page"])])
        panel = panel_card()["view"]
        self.assertTrue(panel.is_persistent())
        self.assertEqual([b.item.label for b in panel.children], ["My settlement", "My account", "Honours", "Open quests", "Needs my attention", "Handbooks"])
        self.assertEqual([len(row["components"]) for row in panel.to_components()], [3, 3])
        for button, title in zip(panel.children, ("Hill", "Personal bank account", "My honours", "Open quest board", "Needs my attention", "Sunset handbooks")):
            i = reader()
            i.message = posted_message(501)
            i.message.components[0].children.append(SimpleNamespace(custom_id="sunset:start:attention"))
            match = StartButton.__discord_ui_compiled_template__.fullmatch(button.custom_id)
            restored = await StartButton.from_custom_id(i, button.item, match)
            await restored.callback(i)
            i.response.defer.assert_awaited_once_with(ephemeral=True, thinking=True)
            card = i.followup.send.await_args.kwargs
            self.assertTrue(card["ephemeral"])
            self.assertEqual(card["embed"].title, title)
            self.assertEqual(card["allowed_mentions"].to_dict(), discord.AllowedMentions.none().to_dict())
            i.edit_original_response.assert_not_awaited()
            i.message.edit.assert_awaited_once()
            self.assertIn("sunset:start:honours", [b.custom_id for b in i.message.edit.await_args.kwargs["view"].children])
        visitor = reader(22)
        await panel.children[0].callback(visitor)
        card = visitor.followup.send.await_args.kwargs
        self.assertEqual(card["embed"].title, "Registered settlements")
        self.assertNotIn("Foreign", str(card["embed"].to_dict()))
        self.assertTrue(card["view"].private)
        land = await db.get_land_by_owner(100, 10)
        await db.add_officer(land["id"], 22)
        await panel.children[0].callback(visitor)
        self.assertEqual(visitor.followup.send.await_args.kwargs["embed"].title, "Hill")
        await cog.cog_unload()
        client.remove_dynamic_items.assert_called_once_with(StartButton)

    async def test_staff_setup_reuses_panel_after_pin_failure_and_recovers_deletion(self):
        cog = SunsetCog(Mock())
        await db.set_config(100, "staff_role_id", "77")
        i = reader()
        channel = i.channel
        await cog.panel.callback(cog, i)
        channel.send.assert_not_awaited()
        i.user.roles = [SimpleNamespace(id=77)]
        original, replacement = posted_message(501), posted_message(502)
        original.pin.side_effect = discord.Forbidden(Mock(status=403, reason="Forbidden"), "Missing permissions")
        channel.send.side_effect = [original, replacement]
        channel.fetch_message.return_value = original
        await cog.panel.callback(cog, i)
        self.assertIn("Pin Messages", i.followup.send.await_args.args[0])
        self.assertIn(original.jump_url, i.followup.send.await_args.args[0])
        original.pin.side_effect = lambda **kwargs: setattr(original, "pinned", True)
        restarted = SunsetCog(Mock())
        await asyncio.gather(restarted.panel.callback(restarted, i), restarted.panel.callback(restarted, i))
        channel.send.assert_awaited_once()
        self.assertEqual(original.pin.await_count, 2)
        self.assertEqual(await db.get_config(100, "start_panel:250"), "501")
        self.assertIn("sunset:start:attention", [b.custom_id for b in original.edit.await_args.kwargs["view"].children])
        channel.fetch_message.side_effect = discord.NotFound(Mock(status=404, reason="Not Found"),
                                                           {"message": "Unknown Message", "code": 10008})
        await restarted.panel.callback(restarted, i)
        self.assertEqual(channel.send.await_count, 2)
        self.assertTrue(replacement.pinned)
        self.assertEqual(await db.get_config(100, "start_panel:250"), "502")

    async def test_interrupted_setup_adopts_only_the_bots_own_panel(self):
        i = reader(900)
        marker = i.id
        await db.set_config(100, "start_panel:250", f"pending:{marker}")
        impostor, ordinary, panel = posted_message(501, author=123), posted_message(502), posted_message(503)
        ordinary.components = []
        async def history(**kwargs):
            self.assertEqual(kwargs["after"].id, marker)
            for message in (impostor, ordinary, panel):
                yield message
        i.channel.history = history
        cog = SunsetCog(Mock())
        await cog.panel.callback(cog, i)
        i.channel.send.assert_not_awaited()
        impostor.pin.assert_not_awaited()
        ordinary.pin.assert_not_awaited()
        self.assertTrue(panel.pinned)
        self.assertEqual(await db.get_config(100, "start_panel:250"), str(panel.id))

    async def test_simultaneous_first_setup_posts_once_and_a_failed_lookup_does_not_replace_it(self):
        cog = SunsetCog(Mock())
        first = reader(900)
        second = reader(900, channel=first.channel)
        panel = posted_message(606)
        first.channel.send.return_value = panel
        first.channel.fetch_message.return_value = panel
        await asyncio.gather(cog.panel.callback(cog, first), cog.panel.callback(cog, second))
        first.channel.send.assert_awaited_once()
        panel.pin.assert_awaited_once()
        self.assertIn(panel.jump_url, first.followup.send.await_args.args[0])
        self.assertIn(panel.jump_url, second.followup.send.await_args.args[0])
        first.channel.fetch_message.side_effect = discord.Forbidden(Mock(status=403, reason="Forbidden"), "Missing access")
        await cog.panel.callback(cog, first)
        first.channel.send.assert_awaited_once()
        self.assertIn("permissions", first.followup.send.await_args.args[0])
