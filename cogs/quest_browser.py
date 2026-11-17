"""A private, reader-bound quest board. Navigation never grants quest rewards."""

import asyncio
import logging

import discord
from discord import app_commands

import acclimation_store as store


log = logging.getLogger("claims-bot")
TEMPORARY = "The quest board could not be loaded. Please try /quest list again shortly."
UNAVAILABLE = "That quest is unavailable on this server. Use /quest list to browse current quests."


def label(value, maximum=100):
    value = " ".join(value.split())
    while len(value.encode("utf-16-le"))//2 > maximum:
        value = value[:-1]
    return value


def safe(value):
    return discord.utils.escape_markdown(value)


async def choices(interaction, current):
    if interaction.guild_id is None:
        return []
    try:
        async with asyncio.timeout(2):
            rows = await store.quest_suggestions(interaction.guild_id,str(current))
        return [app_commands.Choice(name=label(f"#{r['id']} · {r['title']} · {r['status']}"),value=str(r["id"])) for r in rows]
    except Exception as exc:
        log.warning("Quest suggestions unavailable (%s)",type(exc).__name__)
        return []


class QuestSelect(discord.ui.Select):
    def __init__(self, rows):
        super().__init__(placeholder="Choose a quest to read its details",row=0,options=[
            discord.SelectOption(label=label(f"#{r['id']} · {r['title']}"),value=str(r["id"]),
                description=f"{r['reward']:,} Acclimations · {'reward recorded' if r['award_id'] else 'not yet rewarded'}")
            for r in rows])

    async def callback(self, interaction):
        await self.view.navigate(interaction,quest_id=int(self.values[0]))


class FilterSelect(discord.ui.Select):
    def __init__(self, unrewarded):
        super().__init__(placeholder="Which open quests?",row=1,options=[
            discord.SelectOption(label="All open quests",value="all",default=not unrewarded),
            discord.SelectOption(label="Not yet rewarded",value="unrewarded",default=unrewarded)])

    async def callback(self, interaction):
        await self.view.navigate(interaction,unrewarded=self.values[0]=="unrewarded",page=1,quest_id=None)


class QuestView(discord.ui.View):
    def __init__(self, interaction, *, page=1, pages=1, query="", unrewarded=False, quest_id=None, rows=()):
        super().__init__(timeout=600)
        self.owner,self.guild,self.channel = interaction.user.id,interaction.guild_id,interaction.channel_id
        self.source = interaction
        self.page,self.query,self.unrewarded,self.quest_id = page,query,unrewarded,quest_id
        self.busy = False
        self.previous.disabled = quest_id is not None or page <= 1
        self.next_page.disabled = quest_id is not None or page >= pages
        self.back.disabled = quest_id is None
        if rows:
            self.add_item(QuestSelect(rows))
        self.add_item(FilterSelect(unrewarded))

    async def interaction_check(self, interaction):
        if (interaction.user.id != self.owner or interaction.guild_id != self.guild or interaction.channel_id != self.channel
                or self.is_finished() or self.busy):
            await interaction.response.send_message("Use your latest quest board, or open your own with /quest list.",
                                                    ephemeral=True,allowed_mentions=discord.AllowedMentions.none())
            return False
        return True

    async def navigate(self, interaction, **target):
        if not await self.interaction_check(interaction):
            return
        self.busy = True  # Before the first network await: stale clicks cannot overwrite a newer panel.
        next_view = None
        try:
            await interaction.response.defer()
            state = dict(page=self.page,query=self.query,unrewarded=self.unrewarded,quest_id=self.quest_id) | target
            embed,next_view = await render(interaction,**state)
            await interaction.edit_original_response(content=None,embed=embed,view=next_view,
                                                     allowed_mentions=discord.AllowedMentions.none())
        except Exception as exc:
            if next_view:
                next_view.stop()
            message = UNAVAILABLE if isinstance(exc,LookupError) else TEMPORARY
            if not isinstance(exc,LookupError):
                log.warning("Quest navigation unavailable (%s)",type(exc).__name__)
            try:
                await interaction.edit_original_response(content=message,embed=None,view=None,
                                                         allowed_mentions=discord.AllowedMentions.none())
            except (discord.HTTPException,OSError,TimeoutError):
                log.warning("Quest navigation error could not be displayed")
        finally:
            self.stop()

    async def on_timeout(self):
        self.stop()
        for item in self.children:
            item.disabled = True
        try:
            await self.source.edit_original_response(view=self)
        except (discord.HTTPException,OSError,TimeoutError):
            log.info("Expired quest board could not be updated")

    @discord.ui.button(label="Previous",row=2)
    async def previous(self, interaction, button):
        await self.navigate(interaction,page=self.page-1,quest_id=None)

    @discord.ui.button(label="Next",row=2)
    async def next_page(self, interaction, button):
        await self.navigate(interaction,page=self.page+1,quest_id=None)

    @discord.ui.button(label="Back to list",row=2)
    async def back(self, interaction, button):
        await self.navigate(interaction,quest_id=None)

    @discord.ui.button(label="Refresh",row=2)
    async def refresh(self, interaction, button):
        await self.navigate(interaction)


async def render(interaction, *, page=1, query="", unrewarded=False, quest_id=None):
    if interaction.guild_id is None:
        raise LookupError()
    if quest_id is not None:
        row = await store.quest_for_reader(interaction.guild_id,interaction.user.id,quest_id)
        if row is None:
            raise LookupError()
        embed = discord.Embed(title=f"Quest #{row['id']} · {safe(row['title'])}",description=safe(row["details"]),
                              colour=discord.Colour.orange() if row["status"]=="open" else discord.Colour.light_grey())
        embed.add_field(name="Status",value=row["status"].title())
        embed.add_field(name="Reward",value=f"{row['reward']:,} Acclimations per person")
        if row["award_id"]:
            personal = f"Reward already recorded · award #{row['award_id']}."
            if row["award_voided_at"]:
                personal += " That award was voided; this quest cannot reward you again."
        elif row["status"]=="closed":
            personal = "No reward recorded for you. This quest is closed to further completions."
        else:
            personal = "Not yet rewarded. A Hand or administrator must approve your completed deed."
        embed.add_field(name="Your progress",value=personal,inline=False)
        embed.add_field(name="Issued by",value=f"<@{row['created_by']}> · {row['created_at'][:10]} UTC",inline=False)
        embed.set_footer(text="Private to you · Rewards are once per person · Use Back to list · Menus expire after 10 minutes")
        return embed,QuestView(interaction,page=page,query=query,unrewarded=unrewarded,quest_id=quest_id)
    result = await store.quest_board(interaction.guild_id,interaction.user.id,page=page,query=query,unrewarded=unrewarded)
    lines = ["Choose a quest from the dropdown to read its requirements."]
    if result["query"]:
        lines.append(f"Search: **{safe(result['query'])}**")
    lines.append("Showing: **Not yet rewarded**" if unrewarded else "Showing: **All open quests**")
    for row in result["rows"]:
        status = " · reward recorded" if row["award_id"] else ""
        lines.append(f"**#{row['id']} · {safe(row['title'])}**\n{row['reward']:,} Acclimations{status}")
    if not result["rows"]:
        lines.append("No open quests match these filters." if query or unrewarded else "No open quests right now.")
    embed = discord.Embed(title="Open quest board",description="\n\n".join(lines),colour=discord.Colour.orange())
    embed.set_footer(text=f"Page {result['page']}/{result['pages']} · {result['total']} quests · Private to you · Menus expire after 10 minutes")
    return embed,QuestView(interaction,page=result["page"],pages=result["pages"],query=result["query"],
                          unrewarded=unrewarded,rows=result["rows"])


async def open_board(interaction, **target):
    # QuestCog's shared interaction check already deferred this reply privately.
    try:
        embed,view = await render(interaction,**target)
    except LookupError:
        return await interaction.followup.send(UNAVAILABLE,ephemeral=True,allowed_mentions=discord.AllowedMentions.none())
    except Exception as exc:
        log.warning("Quest board unavailable (%s)",type(exc).__name__)
        return await interaction.followup.send(TEMPORARY,ephemeral=True,allowed_mentions=discord.AllowedMentions.none())
    await interaction.followup.send(embed=embed,view=view,ephemeral=True,allowed_mentions=discord.AllowedMentions.none())
