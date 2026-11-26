"""A private, reader-bound quest board. Navigation never grants quest rewards."""

import asyncio
from functools import partial
import logging

import discord
from discord import app_commands

import acclimation_store as store
import quest_submissions as submissions
from cogs.acclimation_display import safe
from cogs.quest_actions import QuestScreen, TEMPORARY, UNAVAILABLE, action_card, can_manage, deed_card
from cogs.quest_reviews import STATUS, SubmitForm, submission_screen


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
                description=f"{r['quest_type'].title()} · {r['reward']:,} Acclimations")
            for r in rows])

    async def callback(self, interaction):
        await self.view.navigate(interaction,quest_id=int(self.values[0]))


class FilterSelect(discord.ui.Select):
    def __init__(self, unrewarded):
        super().__init__(placeholder="Which open quests?",row=1,options=[
            discord.SelectOption(label="All open quests",value="all",default=not unrewarded),
            discord.SelectOption(label="Available to me",value="unrewarded",default=unrewarded)])

    async def callback(self, interaction):
        await self.view.navigate(interaction,unrewarded=self.values[0]=="unrewarded",page=1,quest_id=None)


class QuestTypeSelect(discord.ui.Select):
    def __init__(self, kind):
        options = [discord.SelectOption(label="All quest types", value="all", default=kind is None)]
        options.extend(discord.SelectOption(label=name.title(), value=name, description=rule, default=name == kind)
                       for name, rule in store.QUEST_TYPES.items())
        super().__init__(placeholder="Filter by quest type", row=2, options=options)

    async def callback(self, interaction):
        selected = self.values[0]
        await self.view.navigate(interaction, quest_type=None if selected == "all" else selected, page=1, quest_id=None)


class QuestView(QuestScreen):
    def __init__(self, interaction, *, page=1, pages=1, query="", unrewarded=False, quest_id=None, rows=(),
                 manager=False, deed_count=0, quest_type=None, can_submit=False, has_submissions=False, review_count=None):
        super().__init__(interaction)
        self.page,self.query,self.unrewarded,self.quest_id = page,query,unrewarded,quest_id
        self.quest_type = quest_type
        if not can_submit:
            self.remove_item(self.submit)
        if quest_id is not None and not has_submissions:
            self.remove_item(self.mine)
        if review_count is None:
            self.remove_item(self.reviews)
        elif review_count:
            self.reviews.label = f"Needs review ({review_count})"
        if not manager:
            self.remove_item(self.complete)
            self.remove_item(self.close)
        if deed_count > 1:
            self.deed.label = "View my deeds"
        if not deed_count:
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
        if quest_id is None:
            self.add_item(QuestTypeSelect(quest_type))

    def destination(self, **target):
        state = dict(page=self.page, query=self.query, unrewarded=self.unrewarded,
                     quest_type=self.quest_type, quest_id=self.quest_id) | target
        return partial(render, **state)

    async def navigate(self, interaction, **target):
        await self.replace(interaction, self.destination(**target))

    async def open_action(self, interaction, action):
        await self.replace(interaction, partial(action_card, quest_id=self.quest_id, back=self.destination(), action=action))

    @discord.ui.button(label="Submit deed", style=discord.ButtonStyle.primary, row=0)
    async def submit(self, interaction, button):
        if await self.interaction_check(interaction):
            await interaction.response.send_modal(SubmitForm(self, self.quest_id, self.destination()))

    @discord.ui.button(label="My submissions", row=4)
    async def mine(self, interaction, button):
        await self.replace(interaction, partial(submission_screen, quest_id=self.quest_id, back=self.destination()))

    @discord.ui.button(label="Needs review", row=4)
    async def reviews(self, interaction, button):
        await self.replace(interaction, partial(submission_screen, review=True, back=self.destination()))

    @discord.ui.button(label="Record completion", style=discord.ButtonStyle.primary, row=0)
    async def complete(self, interaction, button):
        await self.open_action(interaction, "complete")

    @discord.ui.button(label="Close quest", row=0)
    async def close(self, interaction, button):
        await self.open_action(interaction, "close")

    @discord.ui.button(label="View my deed", row=0)
    async def deed(self, interaction, button):
        await self.replace(interaction, partial(deed_card, quest_id=self.quest_id, back=self.destination()))

    @discord.ui.button(label="Previous",row=3)
    async def previous(self, interaction, button):
        await self.navigate(interaction,page=self.page-1,quest_id=None)

    @discord.ui.button(label="Next",row=3)
    async def next_page(self, interaction, button):
        await self.navigate(interaction,page=self.page+1,quest_id=None)

    @discord.ui.button(label="Back to list",row=3)
    async def back(self, interaction, button):
        await self.navigate(interaction,quest_id=None)

    @discord.ui.button(label="Refresh",row=3)
    async def refresh(self, interaction, button):
        await self.navigate(interaction)


async def render(interaction, *, page=1, query="", unrewarded=False, quest_id=None, quest_type=None):
    if interaction.guild_id is None:
        raise LookupError()
    if quest_id is not None:
        row = await store.quest_for_reader(interaction.guild_id,interaction.user.id,quest_id)
        if row is None:
            raise LookupError()
        embed = discord.Embed(title=f"Quest #{row['id']} · {safe(row['title'])}",description=safe(row["details"]),
                              colour=discord.Colour.orange() if row["status"]=="open" else discord.Colour.light_grey())
        embed.add_field(name="Status",value=row["status"].title())
        kind = row["quest_type"]
        embed.add_field(name="Type",value=f"{kind.title()} · {store.QUEST_TYPES[kind]}")
        embed.add_field(name="Reward",value=f"{row['reward']:,} Acclimations per completion")
        if row["award_id"]:
            personal = f"Latest award #{row['award_id']}."
            if kind == "repeatable":
                personal = f"Recorded completions: **{row['completion_no']:,}**. " + personal
                if row["status"] == "open":
                    personal += " You can complete this quest again."
            if row["award_voided_at"]:
                personal += " That award was voided."
        elif row["status"]=="closed":
            personal = "No reward recorded for you. This quest is closed to further completions."
        else:
            personal = "Not yet rewarded. A Hand or administrator must approve your completed deed."
        embed.add_field(name="Your progress",value=personal,inline=False)
        if row["submission_status"]:
            embed.add_field(name="Your latest submission", value=f"#{row['submission_id']} · {STATUS[row['submission_status']]}", inline=False)
        embed.add_field(name="Issued by",value=f"<@{row['created_by']}> · {row['created_at'][:10]} UTC",inline=False)
        embed.set_footer(text="Private to you · Menus expire after 10 minutes")
        return embed,QuestView(interaction,page=page,query=query,unrewarded=unrewarded,quest_id=quest_id,
                               manager=row["status"] == "open" and can_manage(interaction, row),
                               deed_count=row["completion_no"] or 0, quest_type=quest_type,
                               has_submissions=row["submission_id"] is not None,
                               can_submit=row["status"] == "open" and (kind == "repeatable" or
                                   (not row["award_id"] and row["submission_status"] not in ("pending","returned"))))
    result = await store.quest_board(interaction.guild_id,interaction.user.id,page=page,query=query,
                                     unrewarded=unrewarded,quest_type=quest_type)
    lines = ["Choose a quest from the dropdown to read its requirements."]
    if result["query"]:
        lines.append(f"Search: **{safe(result['query'])}**")
    lines.append("Showing: **Available to me**" if unrewarded else "Showing: **All open quests**")
    if quest_type:
        lines.append(f"Quest type: **{quest_type.title()}**")
    for row in result["rows"]:
        status = f" · completed ×{row['completion_no']}" if row["award_id"] else ""
        lines.append(f"**#{row['id']} · {safe(row['title'])}**\n{row['quest_type'].title()} · {row['reward']:,} Acclimations{status}")
    if not result["rows"]:
        lines.append("No open quests match these filters." if query or unrewarded or quest_type else "No open quests right now.")
    embed = discord.Embed(title="Open quest board",description="\n\n".join(lines),colour=discord.Colour.orange())
    embed.set_footer(text=f"Page {result['page']}/{result['pages']} · {result['total']} quests · Private to you · Menus expire after 10 minutes")
    reviews = await submissions.review_count(interaction.guild_id, interaction.user.id, admin=interaction.permissions.administrator)
    return embed,QuestView(interaction,page=result["page"],pages=result["pages"],query=result["query"],
                          unrewarded=unrewarded,rows=result["rows"],quest_type=quest_type,review_count=reviews)


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
