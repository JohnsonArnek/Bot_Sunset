"""
Ledger cog — Block delivery tracking for nation leaders.

When a land accepts a rotation offer the bot automatically records
that the nation leader owes them N blocks. The leader (or staff) marks
blocks as delivered via /ledger deliver. /ledger status gives a full
at-a-glance view of who is still owed blocks.
"""

import asyncio
import logging
from functools import partial

import discord
from discord import app_commands
from discord.ext import commands

import database as db
import banner_store
from cogs.bank import AccountID, reply
from cogs.banners import payment_text
from cogs.report_view import NO_MENTIONS, ReportView, clip


async def _is_staff(interaction: discord.Interaction) -> bool:
    if not interaction.guild:
        return False
    if interaction.user.guild_permissions.administrator:
        return True
    staff_role_id = await db.get_staff_role_id(interaction.guild_id)
    if staff_role_id == 0:
        owner_id = interaction.guild.owner_id or (
            interaction.guild.owner.id if interaction.guild.owner else None
        )
        return interaction.user.id == owner_id
    return any(r.id == staff_role_id for r in getattr(interaction.user, "roles", []))


def _entry_line(entry: dict) -> str:
    """Format a single ledger entry into a short log line."""
    emoji = "📥" if entry["type"] == "owed" else "✅"
    date = entry["created_at"][:10] if entry["created_at"] else "?"
    blocks = entry["blocks"]
    note = f" — {discord.utils.escape_markdown(entry['note'])}" if entry.get("note") else ""
    return f"`{date}` {emoji} **{'Owed' if entry['type'] == 'owed' else 'Delivered'}** {blocks} block(s){note}"


async def can_read_land(interaction, land):
    return (await _is_staff(interaction) or interaction.user.id == land["owner_id"] or
            interaction.user.id in await db.get_officers(land["id"]))


async def settlement_choices(interaction, current):
    if interaction.guild_id is None:
        return []
    try:
        async with asyncio.timeout(2):
            staff = await _is_staff(interaction)
            if getattr(getattr(interaction, "command", None), "name", None) == "owe" and not staff:
                return []
            rows = await db.ledger_land_names(interaction.guild_id, interaction.user.id, current, staff=staff)
    except Exception as exc:
        logging.getLogger(__name__).warning("Settlement suggestions unavailable (%s)", type(exc).__name__)
        return []
    return [app_commands.Choice(name=r["name"], value=r["name"]) for r in rows if len(r["name"].encode("utf-16-le")) <= 200]


async def cash_receipt_choices(interaction, current):
    if interaction.guild_id is None or not interaction.permissions.administrator:
        return []
    try:
        async with asyncio.timeout(2):
            rows = await banner_store.unpaid_receipts(
                interaction.guild_id,interaction.namespace.land_name,str(current or ""),admin=interaction.permissions.administrator)
    except Exception as exc:
        logging.getLogger(__name__).warning("Cash receipt suggestions unavailable (%s)", type(exc).__name__)
        return []
    return [app_commands.Choice(name=f"#{r['id']} · {r['unpaid']:,} unpaid · {r['sale_unit']:,} ems each", value=r["id"])
            for r in rows]


async def history_report(land_id, limit, interaction, page, selection):
    land = await db.get_land_by_id(land_id)
    if not land or land["guild_id"] != interaction.guild_id or not await can_read_land(interaction, land):
        raise PermissionError
    history = await db.get_ledger_history(land_id, limit=limit)
    balance = await db.get_ledger_balance(land_id)
    # Keep complete notes, even when a single historic note needs more than one page.
    pages = [""]
    entries = 0
    for row in history:
        line = _entry_line(row)
        for offset in range(0, len(line), 1800):
            chunk = line[offset:offset+1800]
            if pages[-1] and (entries == 5 or len(pages[-1])+len(chunk)+2 > 1800):
                pages.append("")
                entries = 0
            pages[-1] += ("\n\n" if pages[-1] else "") + chunk
            entries += 1
    page = min(max(page, 1), len(pages))
    embed = discord.Embed(title=clip(f"Delivery history · {land['name']}", 256),
                          description=pages[page-1] or "No delivery entries recorded.", colour=discord.Colour.blue())
    outstanding = balance["outstanding"]
    delivery = f"{outstanding:,} awaiting delivery" if outstanding >= 0 else f"{-outstanding:,} delivery credit"
    embed.set_footer(text=f"Page {page}/{len(pages)} · Latest {len(history)} entries · {delivery}")
    return embed, ReportView(interaction, partial(history_report, land_id, limit), page, len(pages))


def land_status(land):
    if land["owner_id"] is None:
        delivery = "**Archived settlement** · delivery history unavailable"
    elif land["outstanding"] < 0:
        delivery = f"Delivery credit · {-land['outstanding']:,} banners"
    else:
        delivery = f"**Awaiting delivery** · {land['outstanding']:,} banners"
    payments = land["payments"]
    lines = [delivery, payment_text(payments)]
    unknown = land["total_owed"] - payments.get("tracked", 0)
    if unknown > 0:
        lines.append(f"Payment unverified · {unknown:,} banners recorded for delivery without a payment receipt")
    return "\n".join(lines)


async def settlement_embed(land):
    name = discord.utils.escape_markdown(land["land_name"])
    payments = land["payments"]
    embed = discord.Embed(title=clip(f"Settlement ledger · {name}", 256), description=land_status(land),
                          colour=discord.Colour.orange() if payments.get("due_ems") or land["outstanding"] > 0 else discord.Colour.green())
    if land["owner_id"] is not None:
        embed.add_field(name="Allocated for delivery", value=f"{land['total_owed']:,}")
        embed.add_field(name="Delivered", value=f"{land['total_delivered']:,}")
        history = await db.get_ledger_history(land["land_id"], limit=5)
        if history:
            embed.add_field(name="Recent activity", value="\n".join(clip(_entry_line(e), 192) for e in history), inline=False)
    embed.set_footer(text="Payment counts include delivered banners. Use /banner status for purchase receipts.")
    return embed


async def ledger_report(interaction, page, selection):
    if not await _is_staff(interaction):
        raise PermissionError
    guild = interaction.guild_id
    if selection is not None:
        rows = await db.get_all_ledger_status(guild, selection)
        if rows:
            return await settlement_embed(rows[0]), ReportView(interaction, ledger_report, page, 1, selection=selection)
    lands = await db.get_all_ledger_status(guild)
    pages = max(1, (len(lands)+4)//5)
    page = max(1, min(page, pages))
    owed = sum(max(0, land["outstanding"]) for land in lands)
    due = sum(land["payments"].get("due_ems", 0) for land in lands)
    unpaid = sum(land["payments"].get("due_blocks", 0) for land in lands)
    embed = discord.Embed(title="Banner ledger", colour=discord.Colour.orange() if due or owed else discord.Colour.green())
    if selection is not None:
        embed.description = "That settlement is no longer on file."
    embed.add_field(name="Awaiting delivery", value=f"**{owed:,} banners**")
    embed.add_field(name="Manual payment due", value=f"**{due:,} ems** on {unpaid:,} banners")
    rows = lands[(page-1)*5:page*5]
    for land in rows:
        paid = land["payments"]
        delivery = (f"{land['outstanding']:,} to deliver" if land["outstanding"] >= 0
                    else f"{-land['outstanding']:,} delivery credit")
        if land["owner_id"] is None:
            delivery = "Archived settlement"
        line = f"**{paid.get('due_ems', 0):,} ems due** · {delivery}"
        if paid:
            line += (f"\n{paid['bank_blocks']:,} bank paid · {paid['physical_blocks']:,} physically paid"
                     f" · {paid['free_blocks']:,} free")
        unknown = land["total_owed"] - paid.get("tracked", 0)
        if unknown > 0:
            line += f"\nPayment unverified for {unknown:,} banners"
        embed.add_field(name=clip(discord.utils.escape_markdown(land["land_name"]), 256), value=line, inline=False)
    if not rows:
        embed.description = "No settlements recorded. Register a settlement with `/land create`."
    embed.set_footer(text=f"Page {page}/{pages} · Admins select cash_receipt in /ledger deliver for cash at pickup, "
                          "or /banner create to settle a delivery already recorded.")
    return embed, ReportView(interaction, ledger_report, page, pages, prompt="Open a settlement",
                             choices=[(land["land_id"], land["land_name"]) for land in rows])


class LedgerCog(commands.GroupCog, name="ledger"):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        super().__init__()

    # ── /ledger status ────────────────────────────────────────────────

    @app_commands.command(
        name="status",
        description="[Staff] Count banners awaiting delivery, bank-paid banners and outstanding manual payments",
    )
    @app_commands.guild_only()
    async def ledger_status(self, interaction: discord.Interaction, page: app_commands.Range[int,1,1_000_000] = 1):
        await interaction.response.defer(ephemeral=True)

        try:
            embed, view = await ledger_report(interaction, page, None)
        except PermissionError:
            return await interaction.followup.send("🔒 Staff only.", ephemeral=True)
        await interaction.followup.send(embed=embed, view=view, ephemeral=True, allowed_mentions=NO_MENTIONS)

    # ── /ledger balance ───────────────────────────────────────────────

    @app_commands.command(
        name="balance",
        description="View a land's banner payments, outstanding liability and delivery balance",
    )
    @app_commands.describe(land_name="Land name (leave empty to check your own land)")
    @app_commands.guild_only()
    @app_commands.autocomplete(land_name=settlement_choices)
    async def ledger_balance(self, interaction: discord.Interaction, land_name: str = None):
        await interaction.response.defer(ephemeral=True)

        if land_name:
            land = await db.get_land_by_name(interaction.guild_id, land_name)
        else:
            land = await db.get_land_for_user(interaction.guild_id, interaction.user.id)

        if not land:
            return await interaction.followup.send(
                "❌ Land not found. Specify a land name or make sure you belong to one.",
                ephemeral=True,
            )

        if not await can_read_land(interaction, land):
            return await interaction.followup.send("Only the land owner, officers or staff can read this balance.", ephemeral=True)

        rows = await db.get_all_ledger_status(interaction.guild_id, land["id"])
        if not rows:
            return await interaction.followup.send("This settlement is no longer registered.", ephemeral=True)
        embed = await settlement_embed(rows[0])
        await interaction.followup.send(embed=embed, ephemeral=True, allowed_mentions=NO_MENTIONS)

    # ── /ledger deliver ───────────────────────────────────────────────

    @app_commands.command(
        name="deliver",
        description="Record banner delivery to a land. Admins can also confirm cash received at pickup.",
    )
    @app_commands.describe(
        land_name="The land that was delivered to",
        blocks="How many blocks were delivered (default: 1)",
        note="Optional note for the settlement's delivery history",
        cash_receipt="[Admin] Select a purchase to confirm its cash payment. Leave blank for delivery only.",
    )
    @app_commands.guild_only()
    @app_commands.autocomplete(land_name=settlement_choices, cash_receipt=cash_receipt_choices)
    async def ledger_deliver(
        self,
        interaction: discord.Interaction,
        land_name: str,
        blocks: int = 1,
        note: str = None,
        cash_receipt: AccountID = None,
    ):
        cash = cash_receipt is not None
        await interaction.response.defer(ephemeral=cash)
        if cash and not interaction.permissions.administrator:
            return await reply(interaction,"Only Discord administrators can confirm physical emerald payments.")

        land = await db.get_land_by_name(interaction.guild_id, land_name)
        if not land:
            return await interaction.followup.send(
                f"❌ Land **{land_name}** not found.", ephemeral=True
            )

        if cash:
            try:
                receipt = await banner_store.create(
                    interaction.guild_id, interaction.user.id, cash_receipt, blocks, "Physical emeralds received at pickup",
                    admin=interaction.permissions.administrator, request_id=interaction.id, land_payment="physical",
                    delivery_land_id=land["id"], delivery_note=note)
            except ValueError as exc:
                return await reply(interaction,str(exc))
            status = "Delivery already recorded" if receipt["duplicate"] else "Delivery and payment recorded"
            noun = "banner" if blocks == 1 else "banners"
            return await reply(interaction,f"**{status} · purchase #{cash_receipt}**\n"
                               f"{discord.utils.escape_markdown(land['name'])} received **{blocks:,} {noun}**. "
                               f"Physical payment confirmed: **{receipt['physical']:,} ems**.\n"
                               f"Creation cost settled: {receipt['total']:,} ems · treasury change: {-receipt['treasury_debit']:+,} ems.\n"
                               f"Still to settle on this purchase: {receipt['remaining']:,}.")

        if not await can_read_land(interaction, land):
            return await interaction.followup.send(
                "🔒 Only the land owner, their officers, or staff can mark deliveries.", ephemeral=True
            )

        if blocks < 1:
            return await interaction.followup.send("❌ Blocks must be at least 1.", ephemeral=True)

        await db.add_ledger_delivered(
            land["id"], interaction.guild_id,
            blocks=blocks,
            note=note,
            recorded_by=interaction.user.id,
        )

        balance = await db.get_ledger_balance(land["id"])
        outstanding = balance["outstanding"]

        if outstanding > 0:
            remaining = f"🔴 {outstanding} block(s) still owed."
        elif outstanding < 0:
            remaining = f"🟡 {abs(outstanding)} block(s) over-delivered (credit)."
        else:
            remaining = "🟢 All clear — fully delivered!"

        embed = discord.Embed(
            title="Delivery recorded",
            description=f"**{blocks:,} banners** delivered to {clip(discord.utils.escape_markdown(land['name']), 240)}.\n{remaining}",
            colour=discord.Colour.green() if outstanding <= 0 else discord.Colour.orange(),
        )
        if note:
            embed.add_field(name="Note", value=clip(discord.utils.escape_markdown(note), 1024), inline=False)
        embed.set_footer(text=f"Recorded by {interaction.user.display_name} · Payment is tracked in /ledger balance.")

        await interaction.followup.send(embed=embed, allowed_mentions=NO_MENTIONS)

    # ── /ledger owe ───────────────────────────────────────────────────

    @app_commands.command(
        name="owe",
        description="[Staff] Manually add an owed entry for a land (corrections/edge cases)",
    )
    @app_commands.describe(
        land_name="The land that is owed blocks",
        blocks="How many blocks to record as owed",
        note="Optional note explaining the reason",
    )
    @app_commands.guild_only()
    @app_commands.autocomplete(land_name=settlement_choices)
    async def ledger_owe(
        self,
        interaction: discord.Interaction,
        land_name: str,
        blocks: int = 1,
        note: str = None,
    ):
        await interaction.response.defer(ephemeral=True)

        if not await _is_staff(interaction):
            return await interaction.followup.send("🔒 Staff only.", ephemeral=True)

        land = await db.get_land_by_name(interaction.guild_id, land_name)
        if not land:
            return await interaction.followup.send(
                f"❌ Land **{land_name}** not found.", ephemeral=True
            )

        if blocks < 1:
            return await interaction.followup.send("❌ Blocks must be at least 1.", ephemeral=True)

        await db.add_ledger_owed(
            land["id"], interaction.guild_id,
            blocks=blocks,
            note=note or "Manual entry by staff",
            recorded_by=interaction.user.id,
        )

        balance = await db.get_ledger_balance(land["id"])
        outstanding = balance["outstanding"]
        delivery = f"{outstanding:,} awaiting delivery" if outstanding >= 0 else f"{-outstanding:,} delivery credit"
        embed = discord.Embed(title="Delivery obligation recorded", colour=discord.Colour.orange(),
                              description=f"**{blocks:,} banners** allocated to {clip(discord.utils.escape_markdown(land['name']), 240)}.")
        embed.add_field(name="Delivery balance", value=delivery)
        if note:
            embed.add_field(name="Note", value=clip(discord.utils.escape_markdown(note), 1024), inline=False)
        await interaction.followup.send(embed=embed, ephemeral=True, allowed_mentions=NO_MENTIONS)

    # ── /ledger history ───────────────────────────────────────────────

    @app_commands.command(
        name="history",
        description="View the full ledger history for a land",
    )
    @app_commands.describe(
        land_name="Land name (leave empty for your own land)",
        limit="How many entries to show (default 10, max 25)",
    )
    @app_commands.guild_only()
    @app_commands.autocomplete(land_name=settlement_choices)
    async def ledger_history(
        self,
        interaction: discord.Interaction,
        land_name: str = None,
        limit: int = 10,
    ):
        await interaction.response.defer(ephemeral=True)
        land = (await db.get_land_by_name(interaction.guild_id, land_name) if land_name
                else await db.get_land_for_user(interaction.guild_id, interaction.user.id))
        if not land:
            return await interaction.followup.send("No settlement found. You can specify its name.", ephemeral=True)
        try:
            embed, view = await history_report(land["id"], max(1, min(25, limit)), interaction, 1, None)
        except PermissionError:
            return await interaction.followup.send("Only the land owner, officers or staff can read this history.", ephemeral=True)
        await interaction.followup.send(embed=embed, view=view, ephemeral=True, allowed_mentions=NO_MENTIONS)


async def setup(bot: commands.Bot):
    await bot.add_cog(LedgerCog(bot))
