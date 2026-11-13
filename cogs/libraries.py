"""Public library discovery and private catalogue management."""

import discord
from discord import app_commands
from discord.ext import commands

import library_store as store


Name = app_commands.Range[str,1,80]
Location = app_commands.Range[str,1,200]
Description = app_commands.Range[str,0,300]
Copies = app_commands.Range[int,0,1_000_000]
Page = app_commands.Range[int,1,1_000_000]
BookID = app_commands.Range[int,1,2**53-1]


def text(value):
    return discord.utils.escape_markdown(value)


def page_footer(embed, page, total):
    pages = max(1,(total+store.PAGE_SIZE-1)//store.PAGE_SIZE)
    embed.set_footer(text=f"Page {page}/{pages} · {total} entries · use page to browse")


async def begin(interaction, *, public=False):
    if interaction.guild_id is None:
        raise app_commands.NoPrivateMessage()
    await interaction.response.defer(ephemeral=not public)


async def send(interaction, *, content=None, embed=None, public=False):
    await interaction.followup.send(content=content,embed=embed,ephemeral=not public,
                                    allowed_mentions=discord.AllowedMentions.none())


@app_commands.guild_only()
class LibraryCog(commands.GroupCog, name="library", group_description="Find settlement libraries and their books"):
    @app_commands.command(name="set", description="Set your settlement's public library name and location")
    @app_commands.describe(location="Public directions, coordinates and dimension",land_name="Settlement you manage; omit if you manage only one")
    async def set_library(self, interaction: discord.Interaction, name: Name, location: Location,
                          land_name: str = None, description: Description = None):
        await begin(interaction)
        try:
            land = await store.setup(interaction.guild_id,interaction.user.id,name,location,
                                      admin=interaction.permissions.administrator,land_name=land_name,description=description)
        except ValueError as exc:
            return await send(interaction,content=str(exc))
        await send(interaction,content=f"Public library saved for **{text(land)[:100]}**. Everyone on this server can browse it with /library view.")

    @app_commands.command(name="list", description="Browse the public libraries listed by settlements on this server")
    async def list_libraries(self, interaction: discord.Interaction, page: Page = 1):
        await begin(interaction,public=True)
        result = await store.catalog(interaction.guild_id,page=page)
        embed = discord.Embed(title="Settlement libraries",colour=discord.Colour.teal())
        for row in result["libraries"]:
            embed.add_field(name=text(row["name"]),value=f"**{text(row['land_name'])[:100]}** · {row['titles']:,} titles\n"
                            f"Location: {text(row['location'])}\nUse /library view with this land_name.",inline=False)
        if not result["libraries"]:
            embed.description = "No libraries on this page. Settlement owners and officers can register one with /library set."
        page_footer(embed,page,result["total"])
        await send(interaction,embed=embed,public=True)

    @app_commands.command(description="Read a settlement's public library location and book catalogue")
    async def view(self, interaction: discord.Interaction, land_name: str, page: Page = 1):
        await begin(interaction,public=True)
        try:
            row = await store.view(interaction.guild_id,land_name,page=page)
        except ValueError as exc:
            return await send(interaction,content=str(exc),public=True)
        embed = discord.Embed(title=text(row["name"]),colour=discord.Colour.teal(),
            description=f"**{text(row['land_name'])[:100]}**\nLocation: {text(row['location'])}\n\n{text(row['description'])}")
        for book in row["books"]:
            embed.add_field(name=f"#{book['id']} · {text(book['title'])}",
                            value=f"By {text(book['author'])} · **{book['copies']:,} copies listed**\n{text(book['description'])}",inline=False)
        if not row["books"]:
            embed.add_field(name="Books",value="No books on this page.",inline=False)
        page_footer(embed,page,row["titles"])
        await send(interaction,embed=embed,public=True)

    @app_commands.command(description="Search public library book titles and authors across this server")
    async def search(self, interaction: discord.Interaction, query: Name, page: Page = 1):
        await begin(interaction,public=True)
        try:
            result = await store.search(interaction.guild_id,query,page=page)
        except ValueError as exc:
            return await send(interaction,content=str(exc),public=True)
        embed = discord.Embed(title="Find a book",description=f"Search: **{text(query)}**",colour=discord.Colour.teal())
        for book in result["books"]:
            embed.add_field(name=f"#{book['id']} · {text(book['title'])}",value=(
                f"By {text(book['author'])} · **{book['copies']:,} copies listed**\n"
                f"{text(book['library_name'])} · {text(book['land_name'])[:100]}\nLocation: {text(book['location'])}"),inline=False)
        if not result["books"]:
            embed.add_field(name="Results",value="No matching books on this page.",inline=False)
        page_footer(embed,page,result["total"])
        await send(interaction,embed=embed,public=True)

    @app_commands.command(description="Add a book title, author and copy count to a settlement library you manage")
    async def add(self, interaction: discord.Interaction, title: Name, author: Name = "Unknown", copies: Copies = 1,
                  description: Description = "", land_name: str = None):
        await begin(interaction)
        try:
            book_id = await store.save_book(interaction.guild_id,interaction.user.id,admin=interaction.permissions.administrator,
                                            title=title,author=author,copies=copies,description=description,land_name=land_name)
        except ValueError as exc:
            return await send(interaction,content=str(exc))
        await send(interaction,content=f"Added public book **#{book_id}**. Use /library edit to change its details or copies.")

    @app_commands.command(description="Update a book's public details or copy count in a library you manage")
    async def edit(self, interaction: discord.Interaction, book_id: BookID, title: Name = None, author: Name = None,
                   copies: Copies = None, description: Description = None):
        await begin(interaction)
        try:
            await store.save_book(interaction.guild_id,interaction.user.id,admin=interaction.permissions.administrator,
                                  book_id=book_id,title=title,author=author,copies=copies,description=description)
        except ValueError as exc:
            return await send(interaction,content=str(exc))
        await send(interaction,content=f"Updated public book **#{book_id}**. Copies is the new total, not an adjustment.")

    @app_commands.command(description="Remove a book listing from a settlement library you manage")
    async def remove(self, interaction: discord.Interaction, book_id: BookID):
        await begin(interaction)
        try:
            title = await store.remove(interaction.guild_id,interaction.user.id,book_id,admin=interaction.permissions.administrator)
        except ValueError as exc:
            return await send(interaction,content=str(exc))
        await send(interaction,content=f"Removed book **#{book_id} · {text(title)}** from the public catalogue.")


async def setup(bot: commands.Bot):
    await bot.add_cog(LibraryCog())
