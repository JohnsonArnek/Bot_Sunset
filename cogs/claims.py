"""
Claims cog — /claim request, /claim buy, /claim reserve, /claim approve, /claim deny
All commands are scoped per Discord server (guild).
"""

import discord
from discord import app_commands
from discord.ext import commands

import database as db
import models
import bank_store


async def _is_staff(interaction: discord.Interaction) -> bool:
    """Check if the invoking user has staff permissions on this server."""
    if not interaction.guild:
        return False
    if interaction.user.guild_permissions.administrator:
        return True
    staff_role_id = await db.get_staff_role_id(interaction.guild_id)
    if staff_role_id == 0:
        owner_id = interaction.guild.owner_id or (interaction.guild.owner.id if interaction.guild.owner else None)
        return interaction.user.id == owner_id
    return any(r.id == staff_role_id for r in getattr(interaction.user, "roles", []))


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

        max_chunks = int(await db.get_config(interaction.guild_id, "max_queue_chunks") or "1")
        if chunks > max_chunks:
            return await interaction.followup.send(
                f"❌ You can only request up to **{max_chunks}** chunk(s) per submission on this server.",
                ephemeral=True,
            )

        request_id = await db.create_claim_request(land["id"], chunks, purpose.value)
        if not request_id:
            return await interaction.followup.send(
                "⚠️ Your land already has a pending claim request. "
                "Wait for it to be processed or cancel it first.",
                ephemeral=True,
            )

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

    async def _checkout(self, interaction, *, kind="normal", payment="manual"):
        private = payment == "bank"
        await interaction.response.defer(ephemeral=private)
        try:
            receipt = await bank_store.buy_claim(interaction.guild_id, interaction.user.id,
                                                request_id=interaction.id, payment=payment, kind=kind)
        except ValueError as exc:
            return await interaction.followup.send(str(exc), ephemeral=True,
                                                   allowed_mentions=discord.AllowedMentions.none())
        embed = discord.Embed(title="Purchase already recorded" if receipt["duplicate"] else "Claim purchase recorded",
                              colour=discord.Colour.gold())
        embed.add_field(name="Land", value=discord.utils.escape_markdown(receipt["land_name"])[:240], inline=True)
        embed.add_field(name="Blocks", value=str(receipt["blocks"]), inline=True)
        embed.add_field(name="Total chunks after purchase", value=str(receipt["chunks"]), inline=True)
        embed.add_field(name="Price", value=f"{receipt['blocks']:,} × {receipt['unit_price']:,} = **{receipt['total']:,} ems**", inline=False)
        embed.add_field(name="Payment", value="Bank transfer to central treasury" if private else "Manual payment (no bank debit)", inline=False)
        if private:
            embed.add_field(name="Settlement balance after payment", value=f"{receipt['balance']:,} ems", inline=False)
        embed.set_footer(text=f"Receipt #{receipt['operation_id']} · Delivery is recorded separately in the block ledger")
        await interaction.followup.send(embed=embed, ephemeral=private, allowed_mentions=discord.AllowedMentions.none())

    @app_commands.command(name="buy", description="Buy your land's weekly claim blocks using manual payment or its bank account")
    @app_commands.guild_only()
    @app_commands.describe(payment="Bank transfer pays immediately from the settlement account when funds are sufficient")
    @app_commands.choices(payment=[app_commands.Choice(name="Manual payment", value="manual"),
                                  app_commands.Choice(name="Bank transfer", value="bank")])
    async def claim_buy(self, interaction: discord.Interaction, payment: str = "manual"):
        await self._checkout(interaction, payment=payment)

    @app_commands.command(name="reserve", description="Buy a reserve claim block (manual payment, 1.5× price, bypasses queue)")
    @app_commands.guild_only()
    async def claim_reserve(self, interaction: discord.Interaction):
        await self._checkout(interaction, kind="reserve")

    @app_commands.command(name="buy_leftover", description="Buy a block from the Leftover Pool at normal price using manual payment")
    @app_commands.guild_only()
    async def claim_buy_leftover(self, interaction: discord.Interaction):
        await self._checkout(interaction, kind="leftover")

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

        if not await db.update_request_status(pending["id"], "approved"):
            return await interaction.followup.send("This request changed; refresh before acting.", ephemeral=True)

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

        if not await db.update_request_status(pending["id"], "denied"):
            return await interaction.followup.send("This request changed; refresh before acting.", ephemeral=True)

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
