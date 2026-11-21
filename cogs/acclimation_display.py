"""Shared text for Acclimation records and quest receipts."""

import discord

import acclimation_store as store


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
    lines.append("Use /acclimation history to read the deeds behind the total.")
    return "\n".join(lines)
