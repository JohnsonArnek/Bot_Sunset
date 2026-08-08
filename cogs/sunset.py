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
                "• `/land delete <name>` — [Staff] Delete a registered land.\n"
                "• `/land info [name]` — View detailed info, price tier, queue position.\n"
                "• `/land list` — List all registered lands, their owners, chunks, and members.\n"
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
                "• `/claim buy_leftover` — Buy a block from the Leftover Pool at normal price.\n"
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
                "• `/reserve add <amount>` — Add/remove blocks from the reserve pool.\n"
                "• `/reserve strategic <land> [reason]` — Free reserve block for strategic use.\n"
                "• `/config list` — List all current config settings.\n"
                "• `/config set <key> <value>` — Modify a config setting."
            ),
            inline=False,
        )

        embed.add_field(
            name="📦 Weekly Distribution (Automated)",
            value=(
                "Every week the bot automatically:\n"
                "1️⃣ Moves previous week's unbought leftovers → Reserve Pool\n"
                "2️⃣ Generates new blocks (default: 7)\n"
                "3️⃣ Allocates Reserve (`topup` or `fixed` mode)\n"
                "4️⃣ Approves top queue requests (partial fills allowed)\n"
                "5️⃣ Sends remaining blocks → Leftover Pool (normal price for 1 week)\n"
                "⏰ Approved requests expire after 7 days if not bought"
            ),
            inline=False,
        )

        embed.add_field(
            name="🔨 Reserve Block Auctions",
            value=(
                "• `/auction status` — View active weekend auction status & top bids.\n"
                "• `/auction bid <land> <amount>` — Place or raise a bid for a reserve block.\n"
                "• `/auction start` 🔒 — Manually open a reserve block auction.\n"
                "• `/auction end` 🔒 — Close & resolve the active auction.\n"
                "• `/auction cancel` 🔒 — Cancel current auction without resolving."
            ),
            inline=False,
        )

        embed.add_field(
            name="ℹ️ Info",
            value=(
                "• `/reserve view` — View Leftover Pool & Reserve status.\n"
                "• `/sunset help` — Show this help message.\n"
                "• Config keys: `weekly_blocks`, `max_queue_chunks`, `reserve_mode`, `weekly_reserve_blocks`, `protected_min`, `auction_enabled`"
            ),
            inline=False,
        )

        await interaction.response.send_message(embed=embed)

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
