"""Resolve a handbook shortcut against the current reader's server records."""

from functools import partial

import database as db
import weekly_claims as weekly
from cogs.bank_screens import account_card
from cogs.land_members import member_card
from cogs.report_view import NO_MENTIONS
from cogs.settlement_profile import directory_card, profile_card
from cogs.weekly_offers import send_offer_cards


async def send_card(interaction, render):
    card = await render(interaction)
    await interaction.followup.send(**card, ephemeral=True, allowed_mentions=NO_MENTIONS)


async def resolve_shortcut(interaction, book, slug, books):
    """Reading an article checks availability. Only invoking its callback opens the screen."""
    if (book, slug) == ("citizens", "personal-banking"):
        return "My account", partial(send_card, render=account_card)
    if (book, slug) == ("settlements", "managing-settlement-members"):
        if "civil" in books:  # Configured staff or Discord administrators.
            roster = partial(directory_card, private=True, manage_rosters=True)
        else:
            land = await db.get_land_by_owner(interaction.guild_id, interaction.user.id)
            if not land:
                return None
            profile = partial(profile_card, land_id=land["id"], private=True)
            roster = partial(member_card, land_id=land["id"], back=profile)
        return "Manage roster", partial(send_card, render=roster)
    if (book, slug) == ("settlements", "buying-settlement-blocks"):
        offers = await weekly.offers(interaction.guild_id, owner=interaction.user.id)
        if offers:
            return "Review my offer", partial(send_offer_cards, rows=offers)
    return None
