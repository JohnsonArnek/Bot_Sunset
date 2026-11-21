"""A private, reader-bound quest board. Navigation never grants quest rewards."""

import asyncio
from functools import partial
import logging

import discord
from discord import app_commands

import acclimation_store as store
from cogs.acclimation_display import safe
from cogs.quest_actions import QuestScreen, TEMPORARY, UNAVAILABLE, action_card, can_manage, deed_card


log = logging.getLogger("claims-bot")


def label(value, maximum=100):
    value = " ".join(value.split())
    while len(value.encode("utf-16-le"))//2 > maximum:
        value = value[:-1]
    return value


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


class QuestView(QuestScreen):
    def __init__(self, interaction, *, page=1, pages=1, query="", unrewarded=False, quest_id=None, rows=(),
                 manager=False, has_deed=False):
        super().__init__(interaction)
        self.page,self.query,self.unrewarded,self.quest_id = page,query,unrewarded,quest_id
        if not manager:
            self.remove_item(self.complete)
            self.remove_item(self.close)
        if not has_deed:
            self.remove_item(self.deed)
        elif not manager:
            self.deed.style = discord.ButtonStyle.primary
        self.previous.disabled = quest_id is not None or page <= 1
        self.next_page.disabled = quest_id is not None or page >= pages
        self.back.disabled = quest_id is None
        if quest_id is not None:
            self.remove_item(self.previous)
            self.remove_item(self.next_page)
        if rows:
            self.add_item(QuestSelect(rows))
        self.add_item(FilterSelect(unrewarded))

    def destination(self, **target):
        state = dict(page=self.page, query=self.query, unrewarded=self.unrewarded, quest_id=self.quest_id) | target
        return partial(render, **state)

    async def navigate(self, interaction, **target):
        await self.replace(interaction, self.destination(**target))

    async def open_action(self, interaction, action):
        await self.replace(interaction, partial(action_card, quest_id=self.quest_id, back=self.destination(), action=action))

    @discord.ui.button(label="Record completion", style=discord.ButtonStyle.primary, row=0)
    async def complete(self, interaction, button):
        await self.open_action(interaction, "complete")

    @discord.ui.button(label="Close quest", row=0)
    async def close(self, interaction, button):
        await self.open_action(interaction, "close")

    @discord.ui.button(label="View my deed", row=0)
    async def deed(self, interaction, button):
        await self.replace(interaction, partial(deed_card, quest_id=self.quest_id, back=self.destination()))

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
        return embed,QuestView(interaction,page=page,query=query,unrewarded=unrewarded,quest_id=quest_id,
                               manager=row["status"] == "open" and can_manage(interaction, row),
                               has_deed=row["award_id"] is not None)
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
    # Both the quest command and the Start here button acknowledge this reply privately.
    try:
        embed,view = await render(interaction,**target)
    except LookupError:
        return await interaction.followup.send(UNAVAILABLE,ephemeral=True,allowed_mentions=discord.AllowedMentions.none())
    except Exception as exc:
        log.warning("Quest board unavailable (%s)",type(exc).__name__)
        return await interaction.followup.send(TEMPORARY,ephemeral=True,allowed_mentions=discord.AllowedMentions.none())
    await interaction.followup.send(embed=embed,view=view,ephemeral=True,allowed_mentions=discord.AllowedMentions.none())
