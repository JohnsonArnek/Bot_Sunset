"""One public entry point per channel, with private destinations for each reader."""

from datetime import timedelta
import logging

import discord

import database as db
from cogs import handbook_browser, quest_browser
from cogs.attention import attention_card
from cogs.bank_screens import account_card
from cogs.report_view import NO_MENTIONS
from cogs.settlement_profile import directory_card, profile_card


log = logging.getLogger(__name__)
SECTIONS = {
    "settlement": ("My settlement", "🏰"),
    "account": ("My account", "💰"),
    "quests": ("Open quests", "📜"),
    "handbooks": ("Handbooks", "📚"),
    "attention": ("Needs my attention", "📌"),
}


class StartButton(discord.ui.DynamicItem[discord.ui.Button],
                  template=r"sunset:start:(?P<section>settlement|account|quests|handbooks|attention)"):
    def __init__(self, section):
        label, emoji = SECTIONS[section]
        style = discord.ButtonStyle.primary if section == "settlement" else discord.ButtonStyle.secondary
        super().__init__(discord.ui.Button(label=label, emoji=emoji, custom_id=f"sunset:start:{section}", style=style))
        self.section = section

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls(match["section"])

    async def callback(self, interaction):
        if not interaction.guild:
            return await interaction.response.send_message("Open the Start here panel inside your server.", ephemeral=True)
        await interaction.response.defer(ephemeral=True, thinking=True)
        message = getattr(interaction, "message", None)
        if message and is_panel(message, interaction.client.user.id) and not has_button(message, "attention"):
            try:
                await message.edit(**panel_card())
            except discord.HTTPException:
                log.info("Could not refresh an existing Start here panel")
        if self.section == "quests":
            return await quest_browser.open_board(interaction)
        try:
            if self.section == "settlement":
                land = await db.get_land_for_user(interaction.guild_id, interaction.user.id, include_officers=True)
                if land:
                    card = await profile_card(interaction, land_id=land["id"], private=True)
                else:
                    card = await directory_card(interaction, private=True)
            elif self.section == "account":
                card = await account_card(interaction)
            elif self.section == "attention":
                embed, view = await attention_card(interaction)
                card = dict(embed=embed, view=view)
            else:
                embed, view = await handbook_browser.render(interaction)
                card = dict(embed=embed, view=view)
        except Exception:
            log.exception("Could not open Start here destination %s", self.section)
            return await interaction.followup.send("This screen could not be opened. Try the button again shortly.", ephemeral=True)
        await interaction.followup.send(**card, ephemeral=True, allowed_mentions=NO_MENTIONS)


def panel_card():
    embed = discord.Embed(title="Sunset · Start here", colour=discord.Colour.teal(),
                          description="Each button opens a private screen for you. New to Sunset? Open **Handbooks**.")
    view = discord.ui.View(timeout=None)
    for section in SECTIONS:
        view.add_item(StartButton(section))
    return dict(embed=embed, view=view, allowed_mentions=NO_MENTIONS)


def has_button(message, section):
    return any(getattr(button, "custom_id", None) == f"sunset:start:{section}"
               for row in message.components for button in getattr(row, "children", ()))


def is_panel(message, bot_id):
    return message.author.id == bot_id and has_button(message, "settlement")


async def ensure_panel(interaction):
    channel = interaction.channel
    key = f"start_panel:{channel.id}"
    saved = await db.get_config(interaction.guild_id, key)
    message = None
    attempt = interaction.id
    if saved.startswith("pending:"):
        # Recover a send that succeeded before the bot could save Discord's message ID.
        after = discord.Object(id=int(saved.removeprefix("pending:")))
        deadline = after.created_at + timedelta(minutes=15)
        if discord.utils.utcnow() < deadline:
            attempt = after.id  # Resend with the same Discord nonce if the outcome was ambiguous.
        async for candidate in channel.history(after=after, before=deadline, oldest_first=True, limit=None):
            if is_panel(candidate, interaction.client.user.id):
                message = candidate
                break
    elif saved:
        try:
            candidate = await channel.fetch_message(int(saved))
        except discord.NotFound as exc:
            if exc.code != 10008:  # Only a deleted message should cause a replacement.
                raise
        else:
            if is_panel(candidate, interaction.client.user.id):
                message = candidate

    if message is not None and not has_button(message, "attention"):
        await message.edit(**panel_card())
    if message is None:
        pending = f"pending:{attempt}"
        if pending != saved:
            await db.set_config(interaction.guild_id, key, pending)
        message = await channel.send(**panel_card(), nonce=f"sp:{attempt}")
    if str(message.id) != saved:
        await db.set_config(interaction.guild_id, key, str(message.id))
    if not message.pinned:
        try:
            await message.pin(reason="Set up the Sunset Start here panel")
        except discord.Forbidden:
            return f"[Start here]({message.jump_url}) is posted. Pin it manually, or grant the bot **Pin Messages** and run this command again."
        except discord.HTTPException:
            return f"[Start here]({message.jump_url}) is posted. Discord could not pin it. Check the channel pins and run this command again."
    return f"[Start here]({message.jump_url}) is pinned in this channel."
