"""
Claims cog — /claim request, /claim buy, /claim reserve, /claim approve, /claim deny
All commands are scoped per Discord server (guild).
"""

import discord
from discord import app_commands
from discord.ext import commands
from datetime import datetime, timezone

import database as db
import models


async def _is_staff(interaction: discord.Interaction) -> bool:
    """Check if the invoking user has the configured staff role on this server."""
    staff_role_id = await db.get_staff_role_id(interaction.guild_id)
    if staff_role_id == 0:
        return interaction.user.id == interaction.guild.owner_id
    return any(r.id == staff_role_id for r in interaction.user.roles)


class ClaimsCog(commands.GroupCog, name="claim"):
    """Slash commands under /claim."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        super().__init__()

    # ── /claim request ────────────────────────────────────────────────

    @app_commands.command(name="request", description="Request a claim block for your land")
    @app_commands.describe(
        chunks="Number of claim blocks to request (default 1)",
        purpose="What will you use this block for?",
    )
    @app_commands.choices(
        purpose=[
            app_commands.Choice(name="Vault", value="vault"),
            app_commands.Choice(name="Builds", value="builds"),
            app_commands.Choice(name="Other", value="other"),
        ]
    )
    @app_commands.guild_only()
    async def claim_request(
        self,
        interaction: discord.Interaction,
        purpose: app_commands.Choice[str],
        chunks: int = 1,
    ):
        await interaction.response.defer()

        land = await db.get_land_for_user(interaction.guild_id, interaction.user.id)
        if not land:
            return await interaction.followup.send(
                "❌ You don't belong to any land on this server. Use `/land create` first.", ephemeral=True
            )

        # Check for existing pending request
        existing = await db.get_pending_request(land["id"])
        if existing:
            return await interaction.followup.send(
                "⚠️ Your land already has a pending claim request. "
                "Wait for it to be processed or cancel it first.",
                ephemeral=True,
            )

        if chunks < 1:
            return await interaction.followup.send(
                "❌ You must request at least 1 chunk.", ephemeral=True
            )

        request_id = await db.create_claim_request(land["id"], chunks, purpose.value)
        full_cost = await db.get_full_block_cost(interaction.guild_id)
        price = models.block_price(land["chunks"], full_cost)

        embed = discord.Embed(
            title="📋 Claim Request Submitted",
            colour=discord.Colour.green(),
        )
        embed.add_field(name="Land", value=land["name"], inline=True)
        embed.add_field(name="Chunks Requested", value=str(chunks), inline=True)
        embed.add_field(name="Purpose", value=purpose.name, inline=True)
        embed.add_field(name="Price Per Block", value=f"{price} ems" if price > 0 else "Free", inline=True)
        embed.add_field(name="Request ID", value=f"#{request_id}", inline=True)
        embed.set_footer(text="Use /queue to see your position.")

        await interaction.followup.send(embed=embed)

    # ── /claim buy ────────────────────────────────────────────────────

    @app_commands.command(name="buy", description="Buy your land's weekly normal claim block")
    @app_commands.guild_only()
    async def claim_buy(self, interaction: discord.Interaction):
        await interaction.response.defer()

        land = await db.get_land_for_user(interaction.guild_id, interaction.user.id)
        if not land:
            return await interaction.followup.send(
                "❌ You don't belong to any land on this server.", ephemeral=True
            )

        # Only the owner can purchase
        if land["owner_id"] != interaction.user.id:
            return await interaction.followup.send(
                "❌ Only the land owner can purchase claim blocks.", ephemeral=True
            )

        # Weekday check (Mon=0 ... Fri=4)
        now = datetime.now(timezone.utc)
        if now.weekday() >= 5:
            return await interaction.followup.send(
                "🚫 Normal claim blocks can only be purchased **Monday–Friday**. "
                "Use `/claim reserve` for weekend purchases.",
                ephemeral=True,
            )

        # Weekly purchase restriction
        existing_purchase = await db.get_normal_purchase_this_week(land["id"])
        if existing_purchase:
            return await interaction.followup.send(
                "⚠️ Your land has already purchased a normal block this week. "
                "The limit resets every Monday.",
                ephemeral=True,
            )

        # Check approved request exists
        approved = await db.get_approved_request(land["id"])
        if not approved:
            pending = await db.get_pending_request(land["id"])
            if pending:
                return await interaction.followup.send(
                    "❌ Your claim request hasn't been approved yet. Staff must approve it first.",
                    ephemeral=True,
                )
            return await interaction.followup.send(
                "❌ You don't have an approved claim request. Use `/claim request` first.",
                ephemeral=True,
            )

        # Calculate price
        full_cost = await db.get_full_block_cost(interaction.guild_id)
        price = models.block_price(land["chunks"], full_cost)

        # Process purchase
        new_chunks = land["chunks"] + approved["chunks_requested"]
        await db.update_land_chunks(land["id"], new_chunks)
        await db.record_purchase(land["id"], "normal", price)
        await db.update_request_status(approved["id"], "purchased")

        embed = discord.Embed(
            title="✅ Claim Block Purchased",
            colour=discord.Colour.gold(),
        )
        embed.add_field(name="Land", value=land["name"], inline=True)
        embed.add_field(name="Chunks Added", value=str(approved["chunks_requested"]), inline=True)
        embed.add_field(name="Total Chunks", value=str(new_chunks), inline=True)
        embed.add_field(name="Price Paid", value=f"{price} ems" if price > 0 else "Free", inline=True)
        embed.set_footer(text="Next normal purchase available after Monday reset.")

        await interaction.followup.send(embed=embed)

    # ── /claim reserve ────────────────────────────────────────────────

    @app_commands.command(name="reserve", description="Buy a reserve claim block (1.5× price, bypasses queue)")
    @app_commands.guild_only()
    async def claim_reserve(self, interaction: discord.Interaction):
        await interaction.response.defer()

        land = await db.get_land_for_user(interaction.guild_id, interaction.user.id)
        if not land:
            return await interaction.followup.send(
                "❌ You don't belong to any land on this server.", ephemeral=True
            )

        if land["owner_id"] != interaction.user.id:
            return await interaction.followup.send(
                "❌ Only the land owner can purchase claim blocks.", ephemeral=True
            )

        # Check reserve availability
        reserve = await db.get_reserve(interaction.guild_id)
        available = reserve["total_blocks"] - reserve["protected_min"]
        if available <= 0:
            return await interaction.followup.send(
                f"🚫 No reserve blocks available for sale. "
                f"({reserve['total_blocks']} in reserve, {reserve['protected_min']} protected)",
                ephemeral=True,
            )

        # Calculate reserve price (1.5×)
        full_cost = await db.get_full_block_cost(interaction.guild_id)
        price = models.reserve_price(land["chunks"], full_cost)

        # Process purchase
        new_chunks = land["chunks"] + 1
        await db.update_land_chunks(land["id"], new_chunks)
        await db.update_reserve_blocks(interaction.guild_id, reserve["total_blocks"] - 1)
        await db.record_purchase(land["id"], "reserve", price)

        embed = discord.Embed(
            title="🔒 Reserve Block Purchased",
            colour=discord.Colour.purple(),
            description="This purchase bypasses the queue.",
        )
        embed.add_field(name="Land", value=land["name"], inline=True)
        embed.add_field(name="Total Chunks", value=str(new_chunks), inline=True)
        embed.add_field(name="Price Paid", value=f"{price} ems (1.5× rate)", inline=True)
        embed.add_field(
            name="Reserve Remaining",
            value=f"{reserve['total_blocks'] - 1} ({reserve['protected_min']} protected)",
            inline=True,
        )
        embed.set_footer(text="Excess emeralds help subsidise smaller lands.")

        await interaction.followup.send(embed=embed)

    # ── /claim approve ────────────────────────────────────────────────

    @app_commands.command(name="approve", description="[Staff] Approve a land's claim request")
    @app_commands.describe(land_name="Name of the land to approve")
    @app_commands.guild_only()
    async def claim_approve(self, interaction: discord.Interaction, land_name: str):
        await interaction.response.defer()

        if not await _is_staff(interaction):
            return await interaction.followup.send(
                "🔒 Staff only.", ephemeral=True
            )

        land = await db.get_land_by_name(interaction.guild_id, land_name)
        if not land:
            return await interaction.followup.send(
                f"❌ Land **{land_name}** not found on this server.", ephemeral=True
            )

        pending = await db.get_pending_request(land["id"])
        if not pending:
            return await interaction.followup.send(
                f"❌ **{land_name}** has no pending claim request.", ephemeral=True
            )

        await db.update_request_status(pending["id"], "approved")

        full_cost = await db.get_full_block_cost(interaction.guild_id)
        price = models.block_price(land["chunks"], full_cost)

        embed = discord.Embed(
            title="✅ Claim Request Approved",
            colour=discord.Colour.green(),
        )
        embed.add_field(name="Land", value=land["name"], inline=True)
        embed.add_field(name="Chunks Requested", value=str(pending["chunks_requested"]), inline=True)
        embed.add_field(name="Purpose", value=pending["purpose"].title(), inline=True)
        embed.add_field(name="Price", value=f"{price} ems" if price > 0 else "Free", inline=True)
        embed.set_footer(text=f"Approved by {interaction.user.display_name}")

        await interaction.followup.send(embed=embed)

        # Notify the land owner
        try:
            owner = await interaction.guild.fetch_member(land["owner_id"])
            if owner:
                await owner.send(
                    f"✅ Your claim request for **{land['name']}** on **{interaction.guild.name}** has been approved! "
                    f"Use `/claim buy` to complete the purchase."
                )
        except discord.Forbidden:
            pass

    # ── /claim deny ───────────────────────────────────────────────────

    @app_commands.command(name="deny", description="[Staff] Deny a land's claim request")
    @app_commands.describe(land_name="Name of the land to deny", reason="Reason for denial")
    @app_commands.guild_only()
    async def claim_deny(
        self,
        interaction: discord.Interaction,
        land_name: str,
        reason: str = "No reason given.",
    ):
        await interaction.response.defer()

        if not await _is_staff(interaction):
            return await interaction.followup.send(
                "🔒 Staff only.", ephemeral=True
            )

        land = await db.get_land_by_name(interaction.guild_id, land_name)
        if not land:
            return await interaction.followup.send(
                f"❌ Land **{land_name}** not found on this server.", ephemeral=True
            )

        pending = await db.get_pending_request(land["id"])
        if not pending:
            return await interaction.followup.send(
                f"❌ **{land_name}** has no pending claim request.", ephemeral=True
            )

        await db.update_request_status(pending["id"], "denied")

        embed = discord.Embed(
            title="❌ Claim Request Denied",
            colour=discord.Colour.red(),
        )
        embed.add_field(name="Land", value=land["name"], inline=True)
        embed.add_field(name="Reason", value=reason, inline=False)
        embed.set_footer(text=f"Denied by {interaction.user.display_name}")

        await interaction.followup.send(embed=embed)

        # Notify the land owner
        try:
            owner = await interaction.guild.fetch_member(land["owner_id"])
            if owner:
                await owner.send(
                    f"❌ Your claim request for **{land['name']}** on **{interaction.guild.name}** was denied.\n"
                    f"**Reason:** {reason}"
                )
        except discord.Forbidden:
            pass


async def setup(bot: commands.Bot):
    await bot.add_cog(ClaimsCog(bot))
