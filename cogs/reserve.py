"""
Reserve cog — /reserve, /reserve add, /claim strategic
"""

import discord
from discord import app_commands
from discord.ext import commands

import database as db
import models


async def _is_staff(interaction: discord.Interaction) -> bool:
    staff_role_id = await db.get_staff_role_id()
    if staff_role_id == 0:
        return interaction.user.id == interaction.guild.owner_id
    return any(r.id == staff_role_id for r in interaction.user.roles)


class ReserveCog(commands.GroupCog, name="reserve"):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        super().__init__()

    @app_commands.command(name="view", description="View the current reserve status")
    async def reserve_view(self, interaction: discord.Interaction):
        await interaction.response.defer()

        reserve = await db.get_reserve()
        available = max(0, reserve["total_blocks"] - reserve["protected_min"])

        embed = discord.Embed(
            title="🔒 Reserve Status",
            colour=discord.Colour.dark_purple(),
        )
        embed.add_field(name="Total Blocks", value=str(reserve["total_blocks"]), inline=True)
        embed.add_field(name="Protected (Not For Sale)", value=str(reserve["protected_min"]), inline=True)
        embed.add_field(name="Available For Sale", value=str(available), inline=True)
        embed.set_footer(text="Reserve blocks can be purchased at 1.5× normal price via /claim reserve")
        await interaction.followup.send(embed=embed)

    @app_commands.command(name="add", description="[Staff] Add blocks to the reserve pool")
    @app_commands.describe(amount="Number of blocks to add")
    async def reserve_add(self, interaction: discord.Interaction, amount: int):
        await interaction.response.defer()

        if not await _is_staff(interaction):
            return await interaction.followup.send("🔒 Staff only.", ephemeral=True)
        if amount == 0:
            return await interaction.followup.send("❌ Amount cannot be 0.", ephemeral=True)

        reserve = await db.get_reserve()
        if reserve["total_blocks"] + amount < 0:
            return await interaction.followup.send(
                f"❌ Cannot remove {abs(amount)} blocks — current reserve is only {reserve['total_blocks']}.",
                ephemeral=True,
            )

        await db.add_reserve_blocks(amount)
        new_reserve = await db.get_reserve()

        action_str = f"Added **{amount}** block(s) to" if amount > 0 else f"Removed **{abs(amount)}** block(s) from"
        embed = discord.Embed(
            title="✅ Reserve Updated",
            colour=discord.Colour.green() if amount > 0 else discord.Colour.orange(),
            description=f"{action_str} the reserve.",
        )
        embed.add_field(name="New Total", value=str(new_reserve["total_blocks"]), inline=True)
        embed.set_footer(text=f"Updated by {interaction.user.display_name}")
        await interaction.followup.send(embed=embed)

    @app_commands.command(name="strategic", description="[Staff] Give a reserve block to a land for free")
    @app_commands.describe(land_name="Land to receive the block", reason="Reason for strategic distribution")
    async def strategic(self, interaction: discord.Interaction, land_name: str, reason: str = "Strategic distribution"):
        await interaction.response.defer()

        if not await _is_staff(interaction):
            return await interaction.followup.send("🔒 Staff only.", ephemeral=True)

        land = await db.get_land_by_name(land_name)
        if not land:
            return await interaction.followup.send(f"❌ Land **{land_name}** not found.", ephemeral=True)

        reserve = await db.get_reserve()
        available = reserve["total_blocks"] - reserve["protected_min"]
        if available <= 0:
            return await interaction.followup.send(
                f"🚫 No reserve blocks available (protected minimum: {reserve['protected_min']}).",
                ephemeral=True,
            )

        new_chunks = land["chunks"] + 1
        await db.update_land_chunks(land["id"], new_chunks)
        await db.update_reserve_blocks(reserve["total_blocks"] - 1)
        await db.record_purchase(land["id"], "strategic", 0)

        embed = discord.Embed(
            title="🎯 Strategic Block Distributed",
            colour=discord.Colour.dark_gold(),
        )
        embed.add_field(name="Land", value=land["name"], inline=True)
        embed.add_field(name="New Chunks", value=str(new_chunks), inline=True)
        embed.add_field(name="Reserve Remaining", value=str(reserve["total_blocks"] - 1), inline=True)
        embed.add_field(name="Reason", value=reason, inline=False)
        embed.set_footer(text=f"Distributed by {interaction.user.display_name}")
        await interaction.followup.send(embed=embed)


async def setup(bot: commands.Bot):
    await bot.add_cog(ReserveCog(bot))
