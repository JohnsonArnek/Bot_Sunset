"""Shared text for Acclimation records and quest receipts."""

import discord

import acclimation_store as store


def award_amount(text):
    text = text.strip()
    if not text.isascii() or not text.isdecimal() or len(text) > 7:
        raise ValueError("Enter a whole reward between 1 and 1,000,000 Acclimations.")
    amount = int(text)
    store.positive(amount)
    return amount


async def check_recipient(guild, member_id):
    member = guild.get_member(member_id)
    if member is None:
        try:
            member = await guild.fetch_member(member_id)
        except discord.NotFound:
            raise ValueError("That player has left the server.") from None
    if member.bot:
        raise ValueError("Choose a player from this server.")


def safe(text):
    return discord.utils.escape_markdown(text)


def medal(total):
    qualified = [name for threshold, name in store.MEDALS if total >= threshold]
    return qualified[-1] if qualified else "No medal eligibility yet"


def award_text(row):
    duplicate = "Already recorded" if row["duplicate"] else "Acclimation recorded"
    quest = f" · quest #{row['quest_id']}" if row["quest_id"] is not None else ""
    lines = [f"**{duplicate} · award #{row['id']}**{quest}",
             f"<@{row['recipient_id']}> received **{row['amount']:,}** from <@{row['issuer_id']}>.",
             f"Deed: {safe(row['deed'])}", f"Current total: **{row['total']:,}** · {medal(row['total'])}"]
    if not row["duplicate"]:
        reached = [name for threshold, name in store.MEDALS if row["previous"] < threshold <= row["total"]]
        if reached:
            lines.append("**Ready for a ceremony:** " + "; ".join(reached) + ".")
    if row["voided_at"]:
        lines.append("This award was subsequently voided; it is excluded from the total.")
    if row.get("closed_unique_quest"):
        lines.append("This unique quest is now closed.")
    lines.append("Use /acclimation history to read the deeds behind the total.")
    return "\n".join(lines)


def progress(total):
    next_medal = next(((n, name) for n, name in store.MEDALS if n > total), None)
    if next_medal is None:
        return "All three medal thresholds reached."
    number, name = next_medal
    return f"{number - total:,} more for {name} ({number})."


def deeds_text(member_id, page, rows):
    lines = [f"**Deeds of <@{member_id}> · page {page} (2 per page)**"]
    for row in rows:
        quest = f" · quest #{row['quest_id']}" if row["quest_id"] is not None else ""
        lines.append(f"**#{row['id']} · {row['amount']:,} Acclimations** · {row['created_at'][:10]} UTC · "
                     f"<@{row['issuer_id']}> ({row['source']}){quest}\n{safe(row['deed'])}")
        if row["voided_at"]:
            lines.append(f"Voided {row['voided_at'][:10]} UTC by <@{row['voided_by']}>: {safe(row['void_reason'])}")
    if not rows:
        lines.append("No deeds on this page.")
    return "\n\n".join(lines)
