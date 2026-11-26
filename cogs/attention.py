"""The reader's actionable offers, deeds and settlement ledger entries."""

from functools import partial

import discord

import database as db
import quest_submissions as deeds
import weekly_claims as weekly
from cogs.ledger import _is_staff, ledger_report
from cogs.quest_reviews import submission_screen
from cogs.report_view import ReportView, clip
from cogs.weekly_offers import offer_card


async def attention_card(interaction, page=1, selection=None):
    guild, reader = interaction.guild_id, interaction.user.id
    admin = interaction.permissions.administrator
    if selection is not None:
        kind, number = selection
        if kind in ("review", "revise"):
            return await submission_screen(interaction, selection=number, review=kind == "review",
                                           detail_back=partial(attention_card, page=page))
        if kind == "ledger":
            return await ledger_report(interaction, 1, number)
    offers = await weekly.offers(guild, owner=reader)
    if selection is not None:
        row = next((r for r in offers if r["id"] == number), None)
        if row is not None and kind == "offer":
            card = offer_card(interaction.guild, row)
            return card["embed"], card["view"]
    items = [(("offer", r["id"]), f"Weekly offer · {r['land_name']}", f"{r['chunks_requested']:,} banners") for r in offers]
    for row in await deeds.returned_for(guild, reader):
        items.append((("revise", row["id"]), f"Revise deed #{row['id']} · {row['title']}", "Read the reviewer's note"))
    for row in await deeds.pending_for_review(guild, reader, admin=admin):
        items.append((("review", row["id"]), f"Review deed #{row['id']} · {row['title']}", "Awaiting your decision"))
    if await _is_staff(interaction):
        for land in await db.get_all_ledger_status(guild):
            work = []
            if land["owner_id"] is not None and land["outstanding"] > 0:
                work.append(f"{land['outstanding']:,} awaiting delivery")
            due = land["payments"].get("due_ems", 0)
            if admin and due > 0:
                work.append(f"{due:,} ems due")
            if work:
                items.append((("ledger", land["land_id"]), f"Settlement ledger · {land['land_name']}", " · ".join(work)))
    pages = max(1, (len(items) + 9) // 10)
    page = min(max(1, page), pages)
    rows = items[(page - 1) * 10:page * 10]
    lines = [f"**{discord.utils.escape_markdown(clip(title, 100))}**\n{summary}" for _, title, summary in rows]
    embed = discord.Embed(title="Needs my attention", description="\n\n".join(lines) or "You're all caught up.", colour=discord.Colour.teal())
    embed.set_footer(text=f"Page {page}/{pages} · Private to you")
    return embed, ReportView(interaction, attention_card, page, pages, choices=[(key, title) for key, title, _ in rows],
                             prompt="Open an item")
