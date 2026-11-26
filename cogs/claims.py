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
from cogs.weekly_offers import OfferAction, review_request
from cogs.report_view import clip


def purchase_embed(receipt):
    bank_paid = receipt["payment"] == "bank"
    embed = discord.Embed(title="Purchase already recorded" if receipt["duplicate"] else "Claim purchase recorded",
                          colour=discord.Colour.gold())
    embed.add_field(name="Land", value=discord.utils.escape_markdown(receipt["land_name"])[:240], inline=True)
    embed.add_field(name="Blocks", value=str(receipt["blocks"]), inline=True)
    embed.add_field(name="Total chunks after purchase", value=str(receipt["chunks"]), inline=True)
    embed.add_field(name="Price", value=f"{receipt['blocks']:,} × {receipt['unit_price']:,} = **{receipt['total']:,} ems**", inline=False)
    payment = "Bank transfer to central treasury" if bank_paid else "Manual payment (no bank debit)"
    embed.add_field(name="Payment", value=payment if receipt["total"] else "Free", inline=False)
    if bank_paid:
        embed.add_field(name="Settlement balance after payment", value=f"{receipt['balance']:,} ems", inline=False)
    if receipt.get("banner"):
        b = receipt["banner"]
        quantity = receipt["blocks"]
        if "sale_result" in receipt:
            released = receipt["released_blocks"]
            embed.add_field(name="Public banner account", value=(
                f"Unsold liability cleared **{released:,} × {b['unit_cost']:,} = {released*b['unit_cost']:,} ems**\n"
                f"Sale result **{receipt['sale_result']:+,} ems** · see /bank public"), inline=False)
        embed.add_field(name="Creation funding to settle", value=(
            f"{quantity:,} × {b['unit_cost']:,} = **{quantity*b['unit_cost']:,} ems** to be destroyed at creation.\n"
            f"Land share: {quantity*b['land_unit']:,} · central subsidy: {quantity*(b['unit_cost']-b['land_unit']):,} ems.\n"
            f"Land contribution prepaid by bank: {quantity*b['prepaid_unit']:,} ems.\n"
            f"Track with /banner show purchase_id:{receipt['operation_id']}"), inline=False)
    embed.set_footer(text=f"Receipt #{receipt['operation_id']} · Delivery is recorded separately in the block ledger")
    return embed


async def submit_request(interaction, land, chunks, purpose, *, profile_view=None):
    from_profile = profile_view is not None
    try:
        request_id = await db.create_claim_request(land["id"], chunks, purpose, guild=interaction.guild_id,
                                                   actor=interaction.user.id, weekly_only=from_profile)
    except ValueError as exc:
        return await interaction.followup.send(str(exc), ephemeral=True)
    if not request_id:
        return await interaction.followup.send("This settlement already has a pending request.", ephemeral=True)

    full_cost = await db.get_full_block_cost(interaction.guild_id)
    price = models.block_price(land["chunks"], full_cost)

    embed = discord.Embed(
        title="📋 Claim Request Submitted",
        colour=discord.Colour.green(),
    )
    embed.add_field(name="Land", value=clip(land["name"], 1024), inline=True)
    embed.add_field(name="Chunks Requested", value=str(chunks), inline=True)
    embed.add_field(name="Purpose", value=purpose.title(), inline=True)
    embed.add_field(name="Price Per Block", value=f"{price} ems" if price > 0 else "Free", inline=True)
    embed.add_field(name="Request ID", value=f"#{request_id}", inline=True)
    if not from_profile:
        embed.set_footer(text="Use /queue to see your position.")

    await interaction.followup.send(embed=embed, view=profile_view, ephemeral=from_profile, allowed_mentions=discord.AllowedMentions.none())


class ClaimsCog(commands.GroupCog, name="claim"):
    """Slash commands under /claim."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        super().__init__()

    async def cog_load(self):
        self.bot.add_dynamic_items(OfferAction)

    async def cog_unload(self):
        self.bot.remove_dynamic_items(OfferAction)

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

        await submit_request(interaction, land, chunks, purpose.value)

    async def _checkout(self, interaction, *, kind="normal", payment="manual"):
        private = payment == "bank"
        await interaction.response.defer(ephemeral=private)
        try:
            receipt = await bank_store.buy_claim(interaction.guild_id, interaction.user.id,
                                                request_id=interaction.id, payment=payment, kind=kind)
        except ValueError as exc:
            return await interaction.followup.send(str(exc), ephemeral=True,
                                                   allowed_mentions=discord.AllowedMentions.none())
        embed = purchase_embed(receipt)
        await interaction.followup.send(embed=embed, ephemeral=private, allowed_mentions=discord.AllowedMentions.none())

    @app_commands.command(name="buy", description="Buy your land's weekly claim blocks using manual payment or its bank account")
    @app_commands.guild_only()
    @app_commands.describe(payment="Bank transfer pays immediately from the settlement account when funds are sufficient")
    @app_commands.choices(payment=[app_commands.Choice(name="Manual payment", value="manual"),
                                  app_commands.Choice(name="Bank transfer", value="bank")])
    async def claim_buy(self, interaction: discord.Interaction, payment: str = "manual"):
        await self._checkout(interaction, payment=payment)

    @app_commands.command(name="reserve", description="Buy a reserve claim at 1.5× the full banner price, rounded up")
    @app_commands.choices(payment=[app_commands.Choice(name="Manual payment", value="manual"),
                                  app_commands.Choice(name="Bank transfer", value="bank")])
    @app_commands.guild_only()
    async def claim_reserve(self, interaction: discord.Interaction, payment: str = "manual"):
        await self._checkout(interaction, kind="reserve", payment=payment)

    @app_commands.command(name="buy_leftover", description="Buy a leftover block at your settlement's normal price")
    @app_commands.choices(payment=[app_commands.Choice(name="Manual payment", value="manual"),
                                  app_commands.Choice(name="Bank transfer", value="bank")])
    @app_commands.guild_only()
    async def claim_buy_leftover(self, interaction: discord.Interaction, payment: str = "manual"):
        await self._checkout(interaction, kind="leftover", payment=payment)

    # ── /claim approve ────────────────────────────────────────────────

    @app_commands.command(name="approve", description="[Staff] Approve a land's claim request")
    @app_commands.describe(land_name="Name of the land to approve")
    @app_commands.guild_only()
    async def claim_approve(self, interaction: discord.Interaction, land_name: str):
        await review_request(interaction, name=land_name, approve=True, private=False)

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
        await review_request(interaction, name=land_name, approve=False, reason=reason, private=False)

async def setup(bot: commands.Bot):
    await bot.add_cog(ClaimsCog(bot))
