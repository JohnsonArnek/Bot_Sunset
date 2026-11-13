"""Private handbook navigation. Views retain IDs only, never cached grants or text."""

import asyncio
import logging

import discord
from discord import app_commands

import handbook_store as store


log = logging.getLogger("claims-bot")
UNAVAILABLE = "That article or handbook is unavailable to you here. Run /sunset help to refresh your library."
TEMPORARY = "The handbook could not be loaded. Please try /sunset help again shortly."


def label(text, maximum=100):
    while store.units(text) > maximum:
        text = text[:-1]
    return text


async def choices(interaction, current):
    try:
        async with asyncio.timeout(2):
            books = await store.allowed_books(interaction)
            rows = await store.catalog(interaction.guild_id, books)
        query = current.casefold().strip()
        return [app_commands.Choice(name=label(f"{store.BOOKS[r['book']]} · {r['title']}"), value=r["slug"])
                for r in rows if query in f"{r['slug']} {r['title']} {r['summary']}".casefold()][:25]
    except Exception as exc:
        log.warning("Handbook suggestions unavailable (%s)", type(exc).__name__)
        return []


class BookSelect(discord.ui.Select):
    def __init__(self, books):
        super().__init__(placeholder="Choose a handbook", row=0, options=[
            discord.SelectOption(label=name, value=key) for key, name in store.BOOKS.items() if key in books])

    async def callback(self, interaction):
        await self.view.navigate(interaction, book=self.values[0])


class ArticleSelect(discord.ui.Select):
    def __init__(self, rows):
        super().__init__(placeholder="Choose an article", row=1, options=[
            discord.SelectOption(label=r["title"], value=r["slug"], description=r["summary"]) for r in rows])

    async def callback(self, interaction):
        await self.view.navigate(interaction, slug=self.values[0])


class HandbookView(discord.ui.View):
    def __init__(self, interaction, books, *, book=None, slug=None, page=0, count=1, rows=()):
        super().__init__(timeout=600)
        self.owner = interaction.user.id
        self.guild = interaction.guild_id
        self.channel = interaction.channel_id
        self.book, self.slug, self.page = book, slug, page
        self.previous.disabled = page == 0
        self.next_page.disabled = page + 1 >= count
        self.contents.disabled = book is None
        self.add_item(BookSelect(books))
        if rows:
            self.add_item(ArticleSelect(rows))

    async def interaction_check(self, interaction):
        if (interaction.user.id != self.owner or interaction.guild_id != self.guild
                or interaction.channel_id != self.channel or self.is_finished()):
            await interaction.response.send_message("Open your own current handbook with /sunset help.", ephemeral=True,
                                                    allowed_mentions=discord.AllowedMentions.none())
            return False
        return True

    async def navigate(self, interaction, **target):
        # Check here too so callbacks cannot accidentally bypass the ownership gate.
        if not await self.interaction_check(interaction):
            return
        await interaction.response.defer()
        try:
            embed, view = await render(interaction, **target)
            await interaction.edit_original_response(content=None, embed=embed, view=view,
                                                     allowed_mentions=discord.AllowedMentions.none())
        except LookupError:
            await interaction.edit_original_response(content=UNAVAILABLE, embed=None, view=None)
        except Exception as exc:
            log.warning("Handbook navigation unavailable (%s)", type(exc).__name__)
            await interaction.edit_original_response(content=TEMPORARY, embed=None, view=None)
        self.stop()

    @discord.ui.button(label="Previous", row=2)
    async def previous(self, interaction, button):
        await self.navigate(interaction, book=self.book, slug=self.slug, page=self.page - 1)

    @discord.ui.button(label="Next", row=2)
    async def next_page(self, interaction, button):
        await self.navigate(interaction, book=self.book, slug=self.slug, page=self.page + 1)

    @discord.ui.button(label="Contents", row=2)
    async def contents(self, interaction, button):
        await self.navigate(interaction, book=self.book)

    @discord.ui.button(label="Library", row=2)
    async def library(self, interaction, button):
        await self.navigate(interaction)


async def render(interaction, *, book=None, slug=None, page=0):
    books = await store.allowed_books(interaction)
    if not books or (book is not None and book not in books):
        raise LookupError()
    rows = await store.catalog(interaction.guild_id, books)
    # Only handbooks with articles appear. No restricted titles, counts or summaries.
    published = {r["book"] for r in rows}
    if slug:
        article = await store.article(interaction.guild_id, books, slug)
        if article is None:
            raise LookupError()
        book = article["book"]
        count = len(article["pages"])
        page = max(0, min(page, count - 1))
        embed = discord.Embed(title=article["title"], description=article["pages"][page], colour=discord.Colour.orange())
        embed.set_author(name=store.BOOKS[book])
        embed.set_footer(text=f"Page {page + 1}/{count} · Updated {article['updated_at'][:10]} UTC · /sunset help topic:{slug}")
        return embed, HandbookView(interaction, published | {book}, book=book, slug=slug, page=page, count=count)
    if book:
        articles = [r for r in rows if r["book"] == book]
        count = max(1, (len(articles) + 9) // 10)
        page = max(0, min(page, count - 1))
        visible = articles[page * 10:page * 10 + 10]
        description = "\n\n".join(f"**{discord.utils.escape_markdown(r['title'])}**\n{discord.utils.escape_markdown(r['summary'])}" for r in visible)
        embed = discord.Embed(title=store.BOOKS[book], description=description or "No articles have been published here yet.",
                              colour=discord.Colour.orange())
        embed.set_footer(text=f"Contents {page + 1}/{count} · Private to you · Menus expire after 10 minutes of inactivity")
        return embed, HandbookView(interaction, published | {book}, book=book, page=page, count=count, rows=visible)
    embed = discord.Embed(title="Sunset handbooks", colour=discord.Colour.orange(), description=(
        "Choose a handbook, then an article. Your library follows your current responsibilities on this server.\n\n" +
        "\n".join(f"• **{name}**" for key, name in store.BOOKS.items() if key in published)))
    if not published:
        embed.description = "No handbook articles have been published for this server yet. Ask a bot administrator to load the guides."
    embed.set_footer(text="Private to you · /sunset help topic: searches your articles · Menus expire after 10 minutes of inactivity")
    return embed, HandbookView(interaction, published) if published else None


async def open_help(interaction, topic=None):
    await interaction.response.defer(ephemeral=True)
    if not interaction.guild:
        return await interaction.followup.send("Open /sunset help inside your Discord server.", ephemeral=True)
    try:
        embed, view = await render(interaction, slug=topic)
    except LookupError:
        return await interaction.followup.send(UNAVAILABLE, ephemeral=True)
    except Exception as exc:
        log.warning("Handbook unavailable (%s)", type(exc).__name__)
        return await interaction.followup.send(TEMPORARY, ephemeral=True)
    await interaction.followup.send(embed=embed, view=view, ephemeral=True, allowed_mentions=discord.AllowedMentions.none())
