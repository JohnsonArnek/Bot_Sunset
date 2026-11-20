"""Discord controls for owner checkout and staff review of weekly claims."""

import asyncio
from datetime import datetime, timedelta
import logging

import discord

import bank_store as bank
import database as db
import weekly_claims as weekly


log = logging.getLogger("claims-bot")
NO_MENTIONS = discord.AllowedMentions.none()


class OfferAction(discord.ui.DynamicItem[discord.ui.Button],
                  template=r"claimoffer:(?P<guild>[0-9]+):(?P<request>[0-9]+):(?P<blocks>[0-9]+):(?P<unit>[0-9]+):(?P<action>bank|manual|decline)"):
    def __init__(self, guild, request, blocks, unit, action):
        self.guild, self.request, self.blocks, self.unit, self.action = guild, request, blocks, unit, action
        labels = {"bank": "Pay from settlement bank", "manual": "Pay in person" if unit else "Claim free banners",
                  "decline": "Decline allocation"}
        styles = {"bank": discord.ButtonStyle.success, "manual": discord.ButtonStyle.primary,
                  "decline": discord.ButtonStyle.secondary}
        super().__init__(discord.ui.Button(label=labels[action], style=styles[action],
                         custom_id=f"claimoffer:{guild}:{request}:{blocks}:{unit}:{action}"))

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls(*(int(match[key]) for key in ("guild", "request", "blocks", "unit")), match["action"])

    async def callback(self, interaction):
        if interaction.guild_id not in (None, self.guild):
            return await interaction.response.send_message("Open your allocation in its own server.", ephemeral=True)
        await interaction.response.defer()
        try:
            if self.action == "decline":
                reassigned, leftover = await weekly.decline(self.guild, interaction.user.id, self.request)
                text = (f"Allocation #{self.request} declined. **{reassigned:,}** banners offered down the queue"
                        f" · **{leftover:,}** returned to the leftover pool.")
            else:
                receipt = await bank.buy_claim(self.guild, interaction.user.id, request_id=interaction.id,
                    payment=self.action, allocation_id=self.request, quote=(self.blocks, self.unit))
                if not receipt["total"]:
                    payment = "Free allocation"
                elif self.action == "bank":
                    payment = f"Bank paid · **{receipt['total']:,} ems**"
                else:
                    payment = f"Due in person · **{receipt['total']:,} ems**"
                text = (f"Purchase **#{receipt['operation_id']}** · {discord.utils.escape_markdown(receipt['land_name'])}\n"
                        f"{payment}\n**{receipt['blocks']:,}** banners awaiting delivery.")
        except ValueError as exc:
            return await interaction.followup.send(str(exc), ephemeral=True, allowed_mentions=NO_MENTIONS)
        try:
            await interaction.edit_original_response(content=text, embed=None, view=None, allowed_mentions=NO_MENTIONS)
        except discord.HTTPException:
            await interaction.followup.send(text, ephemeral=True, allowed_mentions=NO_MENTIONS)
        if self.action == "decline":
            guild = interaction.client.get_guild(self.guild)
            if guild:
                await notify_offers(interaction.client, guild)


def offer_card(guild, row):
    blocks, unit = row["chunks_requested"], row["price"]
    name = discord.utils.escape_markdown(row["land_name"])
    embed = discord.Embed(title="Weekly banner allocation", colour=discord.Colour.gold(),
        description=f"**{name}** · {guild.name}\n**{blocks:,} banners** × {unit:,} ems = **{blocks*unit:,} ems**")
    if row["original_blocks"] and blocks < row["original_blocks"]:
        embed.description += f"\nPartial allocation · {blocks:,} of {row['original_blocks']:,} requested."
    timing = "Accept Monday–Friday (UTC)."
    if row["approved_at"]:
        deadline = datetime.fromisoformat(row["approved_at"]) + timedelta(days=7)
        timing += f" Offer expires {discord.utils.format_dt(deadline, 'F')}."
    embed.add_field(name="Your decision", inline=False, value=(
        ("Bank payment transfers the total now. In-person payment records the emeralds due at pickup.\n" if unit else "")
        + "Accept to add the banners to the delivery ledger. Decline to offer them down the queue.\n" + timing))
    embed.set_footer(text=f"Request #{row['id']} · Reopen with /queue → My allocation")
    view = discord.ui.View(timeout=None)
    for action in (("bank", "manual", "decline") if unit else ("manual", "decline")):
        view.add_item(OfferAction(guild.id, row["id"], blocks, unit, action))
    return dict(embed=embed, view=view, allowed_mentions=NO_MENTIONS)


async def send_offer_cards(interaction, rows):
    for row in rows:
        await interaction.followup.send(**offer_card(interaction.guild, row), ephemeral=True)


async def notify_offers(bot, guild):
    # Both a decline callback and the distribution loop may wake the sender.
    if not hasattr(bot, "_weekly_notice_lock"):
        bot._weekly_notice_lock = asyncio.Lock()
    async with bot._weekly_notice_lock:
        for row in await weekly.offers(guild.id, unnotified=True):
            try:
                user = bot.get_user(row["owner_id"]) or await bot.fetch_user(row["owner_id"])
                await user.send(**offer_card(guild, row))
            except (discord.Forbidden, discord.NotFound):
                pass  # The owner can still open /queue → My allocation.
            except discord.HTTPException:
                log.warning("Will retry DM for weekly request %s", row["id"])
                continue
            async with db.DBConnection() as conn:
                await conn.execute("UPDATE claim_requests SET offer_notified_at=? WHERE id=? AND status='approved'",
                                   (bank.now().isoformat(), row["id"]))


async def is_staff(interaction):
    if not interaction.guild:
        return False
    if interaction.user.guild_permissions.administrator:
        return True
    staff_role_id = await db.get_staff_role_id(interaction.guild_id)
    if not staff_role_id:
        return interaction.user.id == interaction.guild.owner_id
    return any(role.id == staff_role_id for role in interaction.user.roles)


async def review_request(interaction, request=None, *, name=None, approve, reason=None, private=True):
    await interaction.response.defer(ephemeral=private, thinking=not private)
    if not await is_staff(interaction):
        return await interaction.followup.send("Staff only.", ephemeral=True)
    if name is not None:
        land = await db.get_land_by_name(interaction.guild_id, name)
        pending = await db.get_pending_request(land["id"]) if land else None
        if not pending:
            return await interaction.followup.send("No pending request for that settlement.", ephemeral=True)
        request = pending["id"]
    try:
        row = await weekly.review(interaction.guild_id, request, approve)
    except ValueError as exc:
        return await interaction.followup.send(str(exc), ephemeral=True)
    decision = "approved" if approve else "denied"
    text = (f"Request **#{request}** · {discord.utils.escape_markdown(row['land_name'])} · "
            f"**{row['chunks_requested']}** banners {decision}.")
    if not approve:
        text += f"\nReason: {reason}"
    if private:
        await interaction.edit_original_response(content=text, view=None, allowed_mentions=NO_MENTIONS)
    else:
        await interaction.followup.send(text, allowed_mentions=NO_MENTIONS)
    if approve:
        await notify_offers(interaction.client, interaction.guild)
    else:
        try:
            owner = await interaction.client.fetch_user(row["owner_id"])
            await owner.send(f"{interaction.guild.name}\n{text}", allowed_mentions=NO_MENTIONS)
        except discord.HTTPException:
            log.info("Could not DM denial for weekly request %s", request)
