"""Public weekly queue, private owner checkout and staff review."""

import discord
from discord import app_commands
from discord.ext import commands

import bank_store as bank
import database as db
import weekly_claims as weekly
from cogs.weekly_offers import NO_MENTIONS, is_staff, review_request, send_offer_cards


PAGE_SIZE = 8


class Denial(discord.ui.Modal, title="Deny claim request"):
    reason = discord.ui.TextInput(label="Reason for the settlement leader", max_length=500,
                                  style=discord.TextStyle.paragraph)

    def __init__(self, request):
        super().__init__()
        self.request = request

    async def on_submit(self, interaction):
        await review_request(interaction, self.request, approve=False, reason=str(self.reason))


class Review(discord.ui.View):
    def __init__(self, request):
        super().__init__(timeout=600)
        self.request = request

    @discord.ui.button(label="Approve allocation", style=discord.ButtonStyle.success)
    async def approve(self, interaction, button):
        await review_request(interaction, self.request, approve=True)

    @discord.ui.button(label="Deny with reason", style=discord.ButtonStyle.secondary)
    async def deny(self, interaction, button):
        if not await is_staff(interaction):
            return await interaction.response.send_message("Staff only.", ephemeral=True)
        await interaction.response.send_modal(Denial(self.request))


class RequestSelect(discord.ui.Select):
    def __init__(self, rows):
        super().__init__(placeholder="Staff · choose a request to review", row=1, options=[
            discord.SelectOption(label=row["land_name"][:100], value=str(row["id"]),
                description=f"#{row['id']} · {row['chunks_requested']} banners · score {row['score']}"[:100])
            for row in rows])

    async def callback(self, interaction):
        if not await is_staff(interaction):
            return await interaction.response.send_message("Staff only.", ephemeral=True)
        request = int(self.values[0])
        async with db.DBConnection() as conn:
            row = await conn.fetchone(weekly.DETAIL + "WHERE l.guild_id=? AND r.id=? AND r.status='pending'",
                                      (interaction.guild_id, request))
        if not row:
            return await interaction.response.send_message("This request has already been reviewed.", ephemeral=True)
        text = (f"**{discord.utils.escape_markdown(row['land_name'])}** · request #{request}\n"
                f"{row['chunks_requested']} banners for {row['purpose']}. Approval sends the owner an offer.")
        await interaction.response.send_message(text, view=Review(request), ephemeral=True, allowed_mentions=NO_MENTIONS)


class QueueView(discord.ui.View):
    def __init__(self, rows, page, pages, private):
        super().__init__(timeout=900)
        self.page, self.private = page, private
        if rows:
            self.add_item(RequestSelect(rows))
        if pages == 1:
            self.remove_item(self.previous)
            self.remove_item(self.next_page)
        else:
            self.previous.disabled = page == 0
            self.next_page.disabled = page + 1 == pages

    async def navigate(self, interaction, page):
        await interaction.response.defer(ephemeral=True, thinking=not self.private)
        card = await queue_card(interaction.guild_id, page, private=True)
        if self.private:
            return await interaction.edit_original_response(**card)
        await interaction.followup.send(**card, ephemeral=True)

    @discord.ui.button(label="My allocation", style=discord.ButtonStyle.primary)
    async def mine(self, interaction, button):
        await interaction.response.defer(ephemeral=True, thinking=True)
        rows = await weekly.offers(interaction.guild_id, owner=interaction.user.id)
        if not rows:
            return await interaction.followup.send("You have no open allocation as a settlement owner.", ephemeral=True)
        await send_offer_cards(interaction, rows)

    @discord.ui.button(label="Previous")
    async def previous(self, interaction, button):
        await self.navigate(interaction, self.page - 1)

    @discord.ui.button(label="Next")
    async def next_page(self, interaction, button):
        await self.navigate(interaction, self.page + 1)


async def queue_card(guild, page=0, *, private=False, land_id=None):
    async with db.DBConnection() as conn:
        entries = await weekly.ranked(conn, guild, bank.now())
    pages = max(1, (len(entries) + PAGE_SIZE - 1) // PAGE_SIZE)
    if land_id is not None:
        position = next((i for i, row in enumerate(entries) if row["land_id"] == land_id), None)
        if position is None:
            raise ValueError("This settlement has left the queue. Refresh its profile to see the current offer.")
        page = position // PAGE_SIZE
    page = min(max(page, 0), pages-1)
    offset = page * PAGE_SIZE
    rows = entries[offset:offset + PAGE_SIZE]
    embed = discord.Embed(title="Weekly claim queue", colour=discord.Colour.blue(),
        description="Pending requests in priority order." if rows else "No requests are waiting for allocation.")
    for pos, row in enumerate(rows, offset+1):
        marker = "▸ " if row["land_id"] == land_id else ""
        embed.add_field(name=f"{marker}{pos}. {row['land_name']}"[:256], inline=False, value=(
            f"**{row['chunks_requested']} banners** · {row['price']} ems each · {row['purpose'].title()}\n"
            f"Score {row['score']} · {row['days']}d waiting · {row['members']} members · {row['chunks']} chunks"))
    embed.set_footer(text=f"Page {page+1}/{pages} · {len(entries)} pending · Owners can open My allocation")
    return dict(embed=embed, view=QueueView(rows, page, pages, private), allowed_mentions=NO_MENTIONS)


class QueueCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @app_commands.command(name="queue", description="View the weekly claim queue and act on your allocation")
    @app_commands.guild_only()
    async def queue(self, interaction: discord.Interaction):
        await interaction.response.defer()
        await interaction.followup.send(**await queue_card(interaction.guild_id))


async def setup(bot: commands.Bot):
    await bot.add_cog(QueueCog(bot))
