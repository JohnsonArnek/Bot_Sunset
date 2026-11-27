"""Private owner checkout for banners outside the weekly allocation."""

from functools import partial

import discord

import bank_store as bank
from cogs.claims import purchase_embed
from cogs.private_screen import PrivateScreen
from cogs.report_view import clip


async def pool_card(interaction, land_id, back):
    try:
        name, offers = await bank.other_banner_offers(interaction.guild_id, interaction.user.id, land_id)
    except ValueError as exc:
        return discord.Embed(description=str(exc)), None
    name = clip(discord.utils.escape_markdown(name), 256)
    embed = discord.Embed(title="Other banner purchases", description=name, colour=discord.Colour.gold())
    for kind, price in offers.items():
        embed.add_field(name=f"{kind.title()} banner", value=f"{price:,} ems · 1 banner")
    if not offers:
        embed.add_field(name="Availability", value="No reserve or leftover banners are available for purchase.")
    return embed, PoolView(interaction, land_id, name, offers, back)


class PoolView(PrivateScreen):
    def __init__(self, interaction, land_id, name, offers, back):
        super().__init__(interaction)
        self.land_id, self.name, self.offers, self.back = land_id, name, offers, back
        if "reserve" not in offers:
            self.remove_item(self.reserve)
        if "leftover" not in offers:
            self.remove_item(self.leftover)

    async def choose(self, interaction, kind):
        unit = self.offers[kind]

        async def select_payment(i):
            if unit == 0:
                return confirmation_card(i, self, kind, "manual")
            embed = discord.Embed(title=f"Buy {kind} banner", description=f"**{self.name}** · 1 banner · **{unit:,} ems**")
            embed.add_field(name="Payment", value="Bank transfer pays from the settlement account now. Pay in person records payment due at pickup.")
            return embed, CheckoutView(i, self, kind)

        await self.replace(interaction, select_payment)

    @discord.ui.button(label="Buy reserve banner", style=discord.ButtonStyle.primary)
    async def reserve(self, interaction, button):
        await self.choose(interaction, "reserve")

    @discord.ui.button(label="Buy leftover banner", style=discord.ButtonStyle.primary)
    async def leftover(self, interaction, button):
        await self.choose(interaction, "leftover")

    @discord.ui.button(label="Back to settlement")
    async def home(self, interaction, button):
        async def profile(i):
            card = await self.back(i)
            return card["embed"], card["view"]
        await self.replace(interaction, profile)


def confirmation_card(interaction, pool, kind, payment):
    price = pool.offers[kind]
    embed = discord.Embed(title="Confirm banner purchase", colour=discord.Colour.gold(),
                          description=f"**{pool.name}** · 1 {kind} banner")
    embed.add_field(name="Total", value=f"{price:,} ems")
    method = "Bank transfer from the settlement account" if payment == "bank" else "Pay in person at pickup"
    embed.add_field(name="Payment", value=method if price else "Free")
    return embed, CheckoutView(interaction, pool, kind, payment)


class CheckoutView(PrivateScreen):
    temporary = "The purchase status could not be displayed. Check View delivery on your settlement before buying again."

    def __init__(self, interaction, pool, kind, payment=None):
        super().__init__(interaction)
        self.pool, self.kind, self.payment = pool, kind, payment
        self.price = pool.offers[kind]
        self.request_id = interaction.id
        if payment is None:
            self.remove_item(self.confirm)
        else:
            self.remove_item(self.bank)
            self.remove_item(self.manual)

    async def review(self, interaction, payment):
        async def render(i):
            return confirmation_card(i, self.pool, self.kind, payment)
        await self.replace(interaction, render)

    @discord.ui.button(label="Bank transfer", style=discord.ButtonStyle.primary)
    async def bank(self, interaction, button):
        await self.review(interaction, "bank")

    @discord.ui.button(label="Pay in person")
    async def manual(self, interaction, button):
        await self.review(interaction, "manual")

    @discord.ui.button(label="Confirm purchase", style=discord.ButtonStyle.success)
    async def confirm(self, interaction, button):
        receipt = None

        async def checkout(i):
            nonlocal receipt
            receipt = await bank.buy_claim(i.guild_id, i.user.id, request_id=self.request_id, payment=self.payment,
                                           kind=self.kind, expected_land_id=self.pool.land_id, quote=(1, self.price))
            return f"Purchase recorded · receipt #{receipt['operation_id']}."

        async def result(i):
            if receipt is not None:
                return purchase_embed(receipt), None
            return await pool_card(i, self.pool.land_id, self.pool.back)

        await self.replace(interaction, result, commit=checkout)

    @discord.ui.button(label="Back to available banners", row=1)
    async def back(self, interaction, button):
        await self.replace(interaction, partial(pool_card, land_id=self.pool.land_id, back=self.pool.back))
