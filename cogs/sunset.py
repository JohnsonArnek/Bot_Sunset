"""
Sunset cog — /sunset help
Permission-aware private handbooks and administrator utilities.
"""

import discord
from discord import app_commands
from discord.ext import commands

from cogs.handbook_browser import open_help, choices


class SunsetCog(commands.GroupCog, name="sunset"):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        super().__init__()

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
