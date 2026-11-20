"""Public settlement profiles with private, settlement-specific actions."""

from functools import partial
import logging

import discord

import bank_store as bank
import database as db
import models
import weekly_claims as weekly
from cogs.bank import account_text
from cogs.claims import submit_request
from cogs.ledger import can_read_land, settlement_embed
from cogs.queue_cog import queue_card
from cogs.report_view import NO_MENTIONS, clip
from cogs.weekly_offers import offer_card


async def selected_land(guild, land_id):
    land = await db.get_land_by_id(land_id)
    if not land or land["guild_id"] != guild:
        raise ValueError("This settlement is no longer registered here.")
    return land


async def open_card(interaction, render, *, replace=False):
    """Public controls open a private copy. Only a private card can be replaced."""
    await interaction.response.defer(ephemeral=True, thinking=not replace)
    try:
        card = await render(interaction)
    except ValueError as exc:
        card = dict(content=str(exc), embed=None, view=None)
    except Exception:
        logging.getLogger(__name__).exception("Could not load settlement screen for guild %s", interaction.guild_id)
        card = dict(content="Could not load this screen. Open /land info to try again.", embed=None, view=None)
    if replace:
        await interaction.edit_original_response(**card, allowed_mentions=NO_MENTIONS)
    else:
        await interaction.followup.send(**card, ephemeral=True, allowed_mentions=NO_MENTIONS)


class PageView(discord.ui.View):
    def __init__(self, render, page, more, *, private):
        super().__init__(timeout=900)
        self.render, self.page, self.private = render, page, private
        self.previous.disabled = page == 1
        self.next_page.disabled = not more
        if self.previous.disabled and self.next_page.disabled:
            self.clear_items()

    @discord.ui.button(label="Previous", row=1)
    async def previous(self, interaction, button):
        await open_card(interaction, partial(self.render, page=self.page-1), replace=self.private)

    @discord.ui.button(label="Next", row=1)
    async def next_page(self, interaction, button):
        await open_card(interaction, partial(self.render, page=self.page+1), replace=self.private)


async def bank_card(interaction, *, land_id, page=1):
    row = await bank.account(interaction.guild_id, interaction.user.id, land_id=land_id, page=page,
                             admin=interaction.permissions.administrator)
    embed = discord.Embed(title="Settlement bank", description=account_text(row, page, statement=True),
                          colour=discord.Colour.gold())
    view = PageView(partial(bank_card, land_id=land_id), page, row["has_more"], private=True)
    return dict(embed=embed, view=view if view.children else None)


async def delivery_card(interaction, *, land_id):
    land = await selected_land(interaction.guild_id, land_id)
    if not await can_read_land(interaction, land):
        raise ValueError("Only the settlement owner, officers or staff can view its delivery ledger.")
    rows = await db.get_all_ledger_status(interaction.guild_id, land_id)
    if not rows:
        raise ValueError("This settlement is no longer registered here.")
    return dict(embed=await settlement_embed(rows[0]))


class RequestQuantity(discord.ui.Modal, title="Request banners"):
    quantity = discord.ui.TextInput(label="Number of banners", default="1", max_length=10)

    def __init__(self, land, purpose):
        super().__init__()
        self.land, self.purpose = land, purpose

    async def on_submit(self, interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            count = int(self.quantity.value)
        except ValueError:
            return await interaction.followup.send("Enter a whole number of banners.", ephemeral=True)
        await submit_request(interaction, self.land, count, self.purpose, profile_view=request_receipt(self.land["id"]))


class RequestPurpose(discord.ui.View):
    def __init__(self, land, maximum):
        super().__init__(timeout=300)
        self.land, self.maximum = land, maximum

    @discord.ui.select(placeholder="Choose the purpose", options=[
        discord.SelectOption(label=label, value=label.lower()) for label in ("Vault", "Builds", "Other")])
    async def choose(self, interaction, select):
        purpose = select.values[0]
        if self.maximum == 1:
            await interaction.response.defer(ephemeral=True, thinking=True)
            await submit_request(interaction, self.land, 1, purpose, profile_view=request_receipt(self.land["id"]))
        else:
            await interaction.response.send_modal(RequestQuantity(self.land, purpose))


async def request_card(interaction, *, land_id):
    land = await selected_land(interaction.guild_id, land_id)
    if land["owner_id"] != interaction.user.id and interaction.user.id not in await db.get_members(land_id):
        raise ValueError("Only the settlement owner or a registered member can request banners here.")
    if await db.get_config(interaction.guild_id, "claim_mode") == "rotation":
        raise ValueError("This settlement receives banners through the daily rotation.")
    if await db.get_pending_request(land_id):
        raise ValueError("A request is already waiting. Refresh the profile to view its queue position.")
    maximum = int(await db.get_config(interaction.guild_id, "max_queue_chunks"))
    prompt = "Choose a purpose to request one banner." if maximum == 1 else f"Choose a purpose, then enter up to {maximum:,} banners."
    name = clip(discord.utils.escape_markdown(land["name"]), 256)
    return dict(content=f"**{name}** · {prompt}", view=RequestPurpose(land, maximum))


async def owner_offer(interaction, *, land_id, request_id):
    rows = await weekly.offers(interaction.guild_id, land_id=land_id, owner=interaction.user.id)
    row = next((r for r in rows if r["id"] == request_id), None)
    if not row:
        raise ValueError("You have no open offer for this request. Refresh the settlement profile.")
    card = offer_card(interaction.guild, row)
    return dict(embed=card["embed"], view=card["view"])


async def queue_position(interaction, *, land_id):
    card = await queue_card(interaction.guild_id, land_id=land_id, private=True)
    return dict(embed=card["embed"], view=card["view"])


def mentions(ids, limit=18):
    text = ", ".join(f"<@{user}>" for user in ids[:limit])
    return text + (f" · +{len(ids) - limit} more" if len(ids) > limit else "")


async def roster_card(interaction, *, land_id, page=1):
    land = await selected_land(interaction.guild_id, land_id)
    officers = set(await db.get_officers(land_id))
    members = set(await db.get_members(land_id))
    people = sorted(officers | members, key=lambda uid: (uid not in officers, uid))
    pages = max(1, (len(people) + 24) // 25)
    page = min(max(page, 1), pages)
    lines = [f"<@{uid}>" + (" · Officer" if uid in officers else "") for uid in people[(page - 1) * 25:page * 25]]
    embed = discord.Embed(title=clip(f"{land['name']} · roster", 256), description="\n".join(lines) or "No registered members.")
    embed.set_footer(text=f"Page {page}/{pages}")
    view = PageView(partial(roster_card, land_id=land_id), page, page < pages, private=True)
    return dict(embed=embed, view=view if view.children else None)


class ProfileAction(discord.ui.Button):
    def __init__(self, label, render, *, primary=False, row=0, replace=False):
        super().__init__(label=label, row=row, style=discord.ButtonStyle.primary if primary else discord.ButtonStyle.secondary)
        self.render, self.replace = render, replace

    async def callback(self, interaction):
        await open_card(interaction, self.render, replace=self.replace)


def request_receipt(land_id):
    view = discord.ui.View(timeout=900)
    view.add_item(ProfileAction("View queue position", partial(queue_position, land_id=land_id), primary=True))
    view.add_item(ProfileAction("Open settlement", partial(profile_card, land_id=land_id, private=True)))
    return view


async def profile_card(interaction, *, land_id, directory_page=None, private=False):
    guild = interaction.guild_id
    land = await selected_land(guild, land_id)
    full = await db.get_full_block_cost(guild)
    officers, members = await db.get_officers(land_id), await db.get_members(land_id)
    member_count = len(members) + (land["bonus_members"] or 0)
    rotation = await db.get_config(guild, "claim_mode") == "rotation"
    offers = await weekly.offers(guild, land_id=land_id)
    pending = await db.get_pending_request(land_id)
    used = await db.get_normal_purchase_this_week(land_id, at=bank.now())
    embed = discord.Embed(title=clip(land["name"], 256), colour=discord.Colour.teal())
    view = discord.ui.View(timeout=900)
    status = []
    if offers:
        row = offers[0]
        blocks, price = row["chunks_requested"], row["price"]
        noun = "banner" if blocks == 1 else "banners"
        status.append(f"**{blocks:,} {noun} offered** · {price:,} ems each · **{blocks*price:,} ems total**")
        view.add_item(ProfileAction("Review offer", partial(owner_offer, land_id=land_id, request_id=row["id"]), primary=True))
    if pending:
        days = models.days_in_queue(pending["requested_at"])
        score = models.queue_score(land["chunks"], member_count, days,
                                   pending["purpose"] == "vault", pending["purpose"] == "builds")
        status.append(f"**In the queue** · {pending['chunks_requested']:,} requested for {pending['purpose']} · score {score} · {days}d waiting")
        view.add_item(ProfileAction("View queue position", partial(queue_position, land_id=land_id), primary=not offers))
    if used:
        status.append("This week’s banner purchase is recorded. Open delivery for handover details.")
    if not offers and not pending and not used and not rotation:
        status.append("Weekly request open · choose a purpose to enter the queue.")
        view.add_item(ProfileAction("Request banners", partial(request_card, land_id=land_id), primary=True))
    if rotation:
        status.append("This settlement uses the daily rotation.")
    view.add_item(ProfileAction("View delivery", partial(delivery_card, land_id=land_id), primary=bool(used and not offers and not pending)))
    view.add_item(ProfileAction("Settlement bank", partial(bank_card, land_id=land_id)))
    embed.description = "\n".join(status)
    embed.add_field(name="Land", value=f"{land['chunks']:,} chunks · {member_count:,} members")
    embed.add_field(name="Banner prices", value=f"{models.price_tier_label(land['chunks'], full)} · reserve {models.reserve_price(full):,} ems")
    embed.add_field(name="Owner", value=f"<@{land['owner_id']}>", inline=False)
    if officers:
        embed.add_field(name="Officers", value=mentions(officers), inline=False)
    if members:
        embed.add_field(name="Members", value=mentions(members), inline=False)
    if max(len(officers), len(members)) > 18:
        view.add_item(ProfileAction("Full roster", partial(roster_card, land_id=land_id), row=1))
    refresh = partial(profile_card, land_id=land_id, directory_page=directory_page, private=True)
    view.add_item(ProfileAction("Refresh", refresh, row=1, replace=private))
    if directory_page is not None:
        view.add_item(ProfileAction("Back to list", partial(directory_card, page=directory_page, private=True), row=1, replace=private))
    embed.set_footer(text="Actions open privately · Controls last 15 minutes · /land info")
    return dict(embed=embed, view=view)


class LandSelect(discord.ui.Select):
    def __init__(self, lands, page, private):
        super().__init__(placeholder="Open a settlement", row=0, options=[
            discord.SelectOption(label=clip(land["name"], 100), value=str(land["id"])) for land in lands])
        self.page, self.private = page, private

    async def callback(self, interaction):
        await open_card(interaction, partial(profile_card, land_id=int(self.values[0]), directory_page=self.page,
                                              private=True), replace=self.private)


async def directory_card(interaction, *, page=1, private=False):
    guild = interaction.guild_id
    async with db.DBConnection() as conn:
        count = await conn.fetchone("SELECT COUNT(*) AS n FROM lands WHERE guild_id=?", (guild,))
        pages = max(1, (count["n"] + 9) // 10)
        page = min(max(1, page), pages)
        rows = await conn.fetchall(
            "SELECT l.id,l.name,l.owner_id,l.chunks,COALESCE(l.bonus_members,0) + "
            "(SELECT COUNT(*) FROM members m WHERE m.land_id=l.id) AS member_count "
            "FROM lands l WHERE guild_id=? ORDER BY LOWER(name),id LIMIT 10 OFFSET ?", (guild, (page - 1) * 10))
    embed = discord.Embed(title="Registered settlements", colour=discord.Colour.teal())
    if not rows:
        embed.description = "No settlements registered yet."
        return dict(embed=embed, view=None)
    for row in rows:
        embed.add_field(name=clip(row["name"], 256), value=f"<@{row['owner_id']}> · {row['chunks']:,} chunks · {row['member_count']:,} members", inline=False)
    embed.set_footer(text=f"Page {page}/{pages} · {count['n']} settlements")
    view = PageView(partial(directory_card, private=True), page, page < pages, private=private)
    view.add_item(LandSelect(rows, page, private))
    return dict(embed=embed, view=view)
