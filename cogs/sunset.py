"""
Sunset cog — /sunset help
Permission-aware private handbooks and administrator utilities.
"""

import asyncio
import logging
from weakref import WeakValueDictionary

import discord
from discord import app_commands
from discord.ext import commands

from cogs.handbook_browser import open_help, choices
from cogs.land_members import is_staff
from cogs.start_panel import StartButton, ensure_panel


class SunsetCog(commands.GroupCog, name="sunset"):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.panel_locks = WeakValueDictionary()
        super().__init__()

    async def cog_load(self):
        self.bot.add_dynamic_items(StartButton)

    async def cog_unload(self):
        self.bot.remove_dynamic_items(StartButton)

    @app_commands.command(name="panel", description="[Staff] Post and pin the Sunset Start here panel in this channel")
    @app_commands.guild_only()
    async def panel(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        if not interaction.guild or not isinstance(interaction.channel, discord.abc.Messageable):
            return await interaction.followup.send("Run this command in the server channel where you want the panel.", ephemeral=True)
        try:
            async with self.panel_locks.setdefault(interaction.channel_id, asyncio.Lock()):
                if not await is_staff(interaction):
                    return await interaction.followup.send("Only server staff can set up the Start here panel.", ephemeral=True)
                notice = await ensure_panel(interaction)
        except discord.Forbidden:
            notice = "The bot needs access to view this channel, read its message history, send messages and embed links. Check its permissions and retry."
        except Exception:
            logging.getLogger(__name__).exception("Start here setup failed in channel %s", interaction.channel_id)
            notice = "Setup could not finish. Run this command again here to recover the panel."
        await interaction.followup.send(notice, ephemeral=True, allowed_mentions=discord.AllowedMentions.none())

    @app_commands.command(name="help", description="Open your private handbooks or find an article")
    @app_commands.guild_only()
    @app_commands.describe(topic="Search articles available to you, or leave empty to browse your library")
    async def help_command(self, interaction: discord.Interaction, topic: app_commands.Range[str, 1, 64] = None):
        await open_help(interaction, topic)

    @help_command.autocomplete("topic")
    async def help_topics(self, interaction: discord.Interaction, current: str):
        return await choices(interaction, current)

    @app_commands.command(name="reset", description="[Staff] Clear duplicate commands and re-sync bot")
    @app_commands.guild_only()
    async def reset_commands(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)

        if not interaction.user.guild_permissions.administrator:
            return await interaction.followup.send("🔒 Admin only.", ephemeral=True)

        # Clear guild-specific command overrides
        self.bot.tree.clear_commands(guild=interaction.guild)
        await self.bot.tree.sync(guild=interaction.guild)

        # Re-sync global commands
        synced = await self.bot.tree.sync()

        await interaction.followup.send(
            f"✅ **Reset complete!**\n"
            f"• Cleared guild command overrides for **{interaction.guild.name}**\n"
            f"• Re-synced **{len(synced)}** global command(s)\n"
            f"• Commands may take up to 1 minute to update in Discord.",
            ephemeral=True,
        )

    @app_commands.command(name="nuke", description="[Staff] Wipe ALL bot data for this server")
    @app_commands.guild_only()
    async def nuke_guild(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)

        if not interaction.user.guild_permissions.administrator:
            return await interaction.followup.send("🔒 Admin only.", ephemeral=True)

        import database as db

        # Delete all lands (cascades to members, claim_requests, purchases)
        lands = await db.get_all_lands(interaction.guild_id)
        for land in lands:
            await db.delete_land(land["id"])

        # Reset reserve
        await db.update_reserve_blocks(interaction.guild_id, 0)
        await db.update_leftover_blocks(interaction.guild_id, 0)

        await interaction.followup.send(
            f"💥 **Server data nuked!**\n"
            f"• Deleted **{len(lands)}** land(s) and all associated data\n"
            f"• Reset reserve and leftover pools to 0\n"
            f"• Config settings preserved. Use `/config set` to change them.",
            ephemeral=True,
        )


async def setup(bot: commands.Bot):
    await bot.add_cog(SunsetCog(bot))
