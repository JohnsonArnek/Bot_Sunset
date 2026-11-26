"""Personal honours, deed history and direct recognition from Start here."""

from functools import partial

import discord

import acclimation_store as store
from cogs.acclimation_display import award_amount, award_text, check_recipient, deeds_text, medal, progress, safe
from cogs.private_screen import PrivateScreen


class HonoursView(PrivateScreen):
    def __init__(self, interaction, *, history_page=0, more=False, can_give=False):
        super().__init__(interaction)
        self.page = history_page
        if history_page:
            self.remove_item(self.deeds)
            self.previous.disabled = history_page == 1
            self.next_page.disabled = not more
        else:
            self.remove_item(self.home)
            self.remove_item(self.previous)
            self.remove_item(self.next_page)
        if not can_give:
            self.remove_item(self.give)

    @discord.ui.button(label="My deeds")
    async def deeds(self, interaction, button):
        await self.replace(interaction, partial(honours_card, page=1))

    @discord.ui.button(label="Give Acclimation", style=discord.ButtonStyle.primary)
    async def give(self, interaction, button):
        await self.replace(interaction, recipient_card)

    @discord.ui.button(label="My honours")
    async def home(self, interaction, button):
        await self.replace(interaction, honours_card)

    @discord.ui.button(label="Newer deeds")
    async def previous(self, interaction, button):
        await self.replace(interaction, partial(honours_card, page=self.page - 1))

    @discord.ui.button(label="Older deeds")
    async def next_page(self, interaction, button):
        await self.replace(interaction, partial(honours_card, page=self.page + 1))


async def honours_card(interaction, page=0):
    guild, member = interaction.guild_id, interaction.user.id
    if page:
        rows = await store.history(guild, member, page, include_next=True)
        embed = discord.Embed(title="My deeds", description=deeds_text(member, page, rows[:2]), colour=discord.Colour.gold())
        return embed, HonoursView(interaction, history_page=page, more=len(rows) > 2)
    profile = await store.profile(guild, member)
    total, rank = profile["total"], profile["rank"]
    embed = discord.Embed(title="My honours", description=f"**{total:,} Acclimations**\n{medal(total)}\n{progress(total)}",
                          colour=discord.Colour.gold())
    if rank == "tribune":
        allowance = "Available" if profile["remaining"] else "Already given"
        embed.add_field(name="Today's Tribune award", value=f"{allowance} · resets at 00:00 UTC")
    view = HonoursView(interaction, can_give=rank in ("hand", "tribune"))
    if rank == "tribune" and not profile["remaining"]:
        view.give.disabled = True
    return embed, view


class AwardView(PrivateScreen):
    def __init__(self, interaction, *, rank=None, award=None):
        super().__init__(interaction)
        self.rank, self.award = rank, award
        if award is None:
            self.remove_item(self.confirm)
        else:
            self.remove_item(self.recipient)

    @discord.ui.select(cls=discord.ui.UserSelect, placeholder="Choose the member to recognise", row=0)
    async def recipient(self, interaction, select):
        if not await self.interaction_check(interaction):
            return
        person = select.values[0]
        if getattr(getattr(person, "guild", None), "id", None) != self.guild or person.bot or person.id == self.owner:
            return await interaction.response.send_message("Choose another player from this server.", ephemeral=True)
        await interaction.response.send_modal(AwardForm(self, person.id))

    @discord.ui.button(label="Confirm award", style=discord.ButtonStyle.success, row=1)
    async def confirm(self, interaction, button):
        async def commit(i):
            member, amount, deed, request_id = self.award
            await check_recipient(i.guild, member)
            row = await store.award(i.guild_id, i.user.id, member, amount, deed, request_id=request_id)
            return award_text(row)
        await self.replace(interaction, honours_card, commit=commit)

    @discord.ui.button(label="Back to my honours", row=1)
    async def back(self, interaction, button):
        await self.replace(interaction, honours_card)


async def recipient_card(interaction):
    profile = await store.profile(interaction.guild_id, interaction.user.id)
    rank = profile["rank"]
    if rank not in ("hand", "tribune") or (rank == "tribune" and not profile["remaining"]):
        return await honours_card(interaction)
    return discord.Embed(title="Give Acclimation", description="Choose the member whose deed you want to recognise."), AwardView(interaction, rank=rank)


class AwardForm(discord.ui.Modal, title="Recognise a deed"):
    deed = discord.ui.TextInput(label="What did this member do?", style=discord.TextStyle.paragraph, max_length=500)
    amount = discord.ui.TextInput(label="Acclimations", default="1", max_length=7)

    def __init__(self, picker, member):
        super().__init__(timeout=600)
        self.picker, self.member = picker, member
        if picker.rank == "tribune":
            self.remove_item(self.amount)

    async def on_submit(self, interaction):
        if not await self.picker.interaction_check(interaction):
            return
        try:
            amount = 1 if self.picker.rank == "tribune" else award_amount(self.amount.value)
            deed = store.clean(self.deed.value, 500)
        except ValueError as exc:
            return await interaction.response.send_message(str(exc), ephemeral=True)
        async def review(i):
            embed = discord.Embed(title="Confirm recognition", description=f"Give **{amount:,} Acclimations** to <@{self.member}>?")
            embed.add_field(name="Deed", value=safe(deed), inline=False)
            return embed, AwardView(i, award=(self.member, amount, deed, i.id))
        await self.picker.replace(interaction, review)
