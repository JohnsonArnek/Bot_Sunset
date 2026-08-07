"""
Sunset cog — /sunset help
Explains all the bot commands to the users.
"""

import discord
from discord import app_commands
from discord.ext import commands


class SunsetCog(commands.GroupCog, name="sunset"):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        super().__init__()

    @app_commands.command(name="help", description="Explains all the commands for the Sunset Land Claims bot")
    async def help_command(self, interaction: discord.Interaction):
        embed = discord.Embed(
            title="🏰 Sunset Claims Bot — Commands Guide",
            description="Here is the complete list of available commands. Staff-only commands are marked with 🔒.",
            colour=discord.Colour.orange(),
        )

        embed.add_field(
            name="🏰 Land Commands",
            value=(
                "• `/land create <name> <owner> [chunks]` — [Staff] Create a new land.\n"
                "• `/land info [name]` — View detailed info, price tier, queue position.\n"
                "• `/land member_add <user>` — Add a member to your land (+2 queue points).\n"
                "• `/land member_remove <user>` — Remove a member from your land."
            ),
            inline=False,
        )

        embed.add_field(
            name="📋 Claiming & Queue",
            value=(
                "• `/claim request <purpose> [chunks]` — Submit a claim block request to the queue.\n"
                "• `/claim buy` — Buy your weekly normal claim block (Mon–Fri only, 1/week).\n"
                "• `/claim reserve` — Buy a reserve block at 1.5× price (bypasses queue).\n"
                "• `/queue` — View the current ranked queue (highest points first)."
            ),
            inline=False,
        )

        embed.add_field(
            name="🔒 Staff Administration",
            value=(
                "• `/land set_chunks <land_name> <chunks>` — Set a land's chunk count manually.\n"
                "• `/claim approve <land>` — Approve a pending claim request.\n"
                "• `/claim deny <land> [reason]` — Deny a request and notify the owner.\n"
                "• `/reserve add <amount>` — Add blocks to the reserve pool.\n"
                "• `/reserve strategic <land> [reason]` — Free reserve block for strategic use.\n"
                "• `/config list` — List all current config settings.\n"
                "• `/config set <key> <value>` — Modify a config setting."
            ),
            inline=False,
        )

        embed.add_field(
            name="ℹ️ Info",
            value=(
                "• `/reserve view` — View current reserve blocks and protected minimum.\n"
                "• `/sunset help` — Show this help message."
            ),
            inline=False,
        )

        await interaction.response.send_message(embed=embed)


async def setup(bot: commands.Bot):
    await bot.add_cog(SunsetCog(bot))
