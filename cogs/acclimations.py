"""Acclimations, appointed officials, deeds, medal eligibility and quests."""

from typing import Literal

import discord
from discord import app_commands
from discord.ext import commands

import acclimation_store as store
from cogs.acclimation_display import award_text, deeds_text, medal, progress, safe
from cogs.war_share import message_payload
from cogs.quest_browser import open_board, choices as quest_choices


Amount = app_commands.Range[int, 1, 1_000_000]
RecordID = app_commands.Range[int, 1, 2**53 - 1]
Page = app_commands.Range[int, 1, 1_000_000]
Deed = app_commands.Range[str, 1, 500]


async def reply(interaction, text):
    await interaction.followup.send(**message_payload(text), ephemeral=True,
                                    allowed_mentions=discord.AllowedMentions.none())


async def perform(interaction, operation, render):
    try:
        result = await operation
    except ValueError as exc:
        return await reply(interaction, str(exc))
    await reply(interaction, render(result))


async def member_check(interaction, member):
    if member.bot:
        await reply(interaction, "Choose a person, not a bot account.")
        return False
    return True


def quest_text(row):
    if row is None:
        return "No quest with that ID exists on this server."
    return (f"**Quest #{row['id']}: {safe(row['title'])}** · {row['status']}\n"
            f"{safe(row['details'])}\n**{row['quest_type'].title()}** · {store.QUEST_TYPES[row['quest_type']]}\n"
            f"Reward: **{row['reward']:,} Acclimations** per approved completion.\n"
            f"Issued by <@{row['created_by']}> on {row['created_at'][:10]} (UTC).\n"
            "Hands and administrators can open the quest and choose Record completion, or use /quest complete.")


class GuildReplies:
    async def interaction_check(self, interaction):
        if interaction.guild_id is None:
            raise app_commands.NoPrivateMessage()
        await interaction.response.defer(ephemeral=True)
        return True


@app_commands.guild_only()
class AcclimationCog(GuildReplies, commands.GroupCog, name="acclimation", group_description="Recognise deeds, appoint officials and view Sunrise honours"):
    @app_commands.command(description="[Admin] Appoint a Hand or Tribune for this server")
    @app_commands.describe(member="Person to appoint", rank="Hand: unlimited awards; Tribune: one Acclimation per UTC day")
    async def deputize(self, interaction: discord.Interaction, member: discord.Member, rank: Literal["hand", "tribune"]):
        if not await member_check(interaction, member):
            return
        await perform(interaction, store.appoint(interaction.guild_id, interaction.user.id, member.id, rank,
                                                admin=interaction.permissions.administrator),
                      lambda _: f"<@{member.id}> is now a **{rank.title()}**. This appointment grants Acclimation authority only.")

    @app_commands.command(description="[Admin] Remove a Hand or Tribune appointment; preserve deeds and daily usage")
    async def dismiss(self, interaction: discord.Interaction, member: discord.Member):
        await perform(interaction, store.appoint(interaction.guild_id, interaction.user.id, member.id, "dismissed",
                                                admin=interaction.permissions.administrator),
                      lambda _: f"<@{member.id}>'s appointment has ended. Their deeds and today's used allowance remain recorded.")

    @app_commands.command(description="List this server's appointed Hands and Tribunes")
    async def officials(self, interaction: discord.Interaction, page: Page = 1):
        rows = await store.officials(interaction.guild_id, page)
        text = "\n".join(f"<@{r['user_id']}> · **{r['rank'].title()}** · appointed {r['created_at'][:10]} UTC" for r in rows)
        await reply(interaction, f"**Hands and Tribunes · page {page}**\n" + (text or "No appointments on this page."))

    @app_commands.command(name="appointment-history", description="[Admin] Read the record of appointments and dismissals")
    async def appointment_history(self, interaction: discord.Interaction, page: Page = 1):
        if not interaction.permissions.administrator:
            return await reply(interaction, "Only Discord administrators can read appointment history.")
        rows = await store.appointments(interaction.guild_id, page)
        text = "\n".join(f"{r['created_at'][:10]} UTC · <@{r['user_id']}>: {r['rank']} by <@{r['actor_id']}>" for r in rows)
        await reply(interaction, f"**Appointment history · page {page}**\n" + (text or "No entries on this page."))

    @app_commands.command(description="[Hand/Tribune] Award Acclimations and record the deed")
    @app_commands.describe(member="Person being recognised", deed="The story of what they did", amount="Tribunes award exactly 1; Hands can award more")
    async def award(self, interaction: discord.Interaction, member: discord.Member, deed: Deed, amount: Amount = 1):
        if not await member_check(interaction, member):
            return
        await perform(interaction, store.award(interaction.guild_id, interaction.user.id, member.id, amount, deed,
                                              request_id=interaction.id), award_text)

    @app_commands.command(description="View Acclimations, medal eligibility and the next milestone")
    async def profile(self, interaction: discord.Interaction, member: discord.Member = None):
        member = member or interaction.user
        row = await store.profile(interaction.guild_id, member.id)
        lines = [f"**Acclimations · <@{member.id}>**", f"Total: **{row['total']:,}**", medal(row["total"]), progress(row["total"])]
        if row["rank"]:
            lines.append(f"Appointment: **{row['rank'].title()}**")
        if row["rank"] == "tribune":
            lines.append(f"Awards available today: {row['remaining']} · resets at 00:00 UTC, no carry-over.")
        lines.append("Medals show ceremony eligibility; Discord roles and in-game items are not granted automatically.")
        await reply(interaction, "\n".join(lines))

    @app_commands.command(description="Read a member's deeds, award dates and corrections")
    async def history(self, interaction: discord.Interaction, member: discord.Member = None, page: Page = 1):
        member = member or interaction.user
        rows = await store.history(interaction.guild_id, member.id, page)
        await reply(interaction, deeds_text(member.id, page, rows))

    @app_commands.command(description="View the Sunrise Copper Medal eligibility board")
    async def honours(self, interaction: discord.Interaction, page: Page = 1):
        rows = await store.honours(interaction.guild_id, page)
        lines = [f"**Sunrise honours · page {page}**", "Ceremony eligibility: Third Class 10 · Second Class 30 · First Class 50."]
        lines.extend(f"<@{r['recipient_id']}> · **{r['total']:,}** · {medal(r['total'])}" for r in rows)
        if not rows:
            lines.append("No qualifying members on this page yet.")
        await reply(interaction, "\n".join(lines))

    @app_commands.command(description="[Admin] Void a mistaken award while preserving its deed and correction reason")
    async def void(self, interaction: discord.Interaction, award_id: RecordID, reason: app_commands.Range[str, 1, 250]):
        await perform(interaction, store.void_award(interaction.guild_id, interaction.user.id, award_id, reason,
                                                  admin=interaction.permissions.administrator),
                      lambda r: f"Award #{r['id']} voided. <@{r['recipient_id']}>'s total is now **{r['total']:,}**. "
                                "The original deed and correction remain in history; daily allowances and quest claims are not reset.")

    @app_commands.command(description="Explain Acclimations, daily allowances and Sunrise medal thresholds")
    async def rules(self, interaction: discord.Interaction):
        await reply(interaction, "**Acclimations**\n"
                    "Administrators appoint Hands and Tribunes with /acclimation deputize. These are bot appointments, not Discord permissions.\n"
                    "Hands may award any number; each Tribune may award one Acclimation total per UTC day (no carry-over). "
                    "Every award needs a deed, and another official must recognise your own deeds.\n"
                    "Hands and administrators approve quest rewards. General quests pay once per member, unique quests close "
                    "on the first approved completion, and repeatable quests pay for each newly approved deed. "
                    "Quest rewards do not use the recipient's Tribune allowance.\n"
                    "Sunrise Copper Medal eligibility: 10 Third Class, 30 Second Class, 50 First Class. "
                    "Arrange ceremonies and items in-game; /acclimation honours and history provide the names and deeds.\n"
                    "Awards are honours, not spendable currency. All commands reply privately; records are scoped to this server.")


@app_commands.guild_only()
class QuestCog(GuildReplies, commands.GroupCog, name="quest", group_description="Issue quests and approve Acclimation rewards"):
    @app_commands.command(description="[Hand/Admin] Issue a quest with an Acclimation reward")
    @app_commands.rename(quest_type="type")
    @app_commands.describe(quest_type="Who can earn the reward and how often (default: General)")
    @app_commands.choices(quest_type=[app_commands.Choice(name=f"{kind.title()} · {rule}", value=kind)
                                     for kind, rule in store.QUEST_TYPES.items()])
    async def create(self, interaction: discord.Interaction, title: app_commands.Range[str, 1, 80],
                     details: app_commands.Range[str, 1, 700], reward: Amount, quest_type: str = "general"):
        await perform(interaction, store.create_quest(interaction.guild_id, interaction.user.id, title, details, reward,
                                                    admin=interaction.permissions.administrator, request_id=interaction.id,
                                                    quest_type=quest_type), quest_text)

    @app_commands.command(name="list", description="Browse open quests with selectable details, filters and page buttons")
    @app_commands.describe(query="Optional words from the title or requirements, or a quest ID")
    async def quest_list(self, interaction: discord.Interaction, page: Page = 1,
                         query: app_commands.Range[str,1,80] = None):
        await open_board(interaction,page=page,query=query or "")

    @app_commands.command(description="Read a quest's requirements and reward")
    @app_commands.describe(quest_id="Type a title and choose a suggestion, or enter a quest ID")
    async def show(self, interaction: discord.Interaction, quest_id: app_commands.Range[str,1,80]):
        try:
            number = int(str(quest_id).strip().lstrip("#"))
            if not 1 <= number <= 2**53-1:
                raise ValueError()
        except ValueError:
            return await reply(interaction,"Choose a suggested quest, or enter its numeric ID.")
        await open_board(interaction,quest_id=number)

    @show.autocomplete("quest_id")
    async def show_choices(self, interaction: discord.Interaction, current: str):
        return await quest_choices(interaction,current)

    @app_commands.command(description="[Hand/Admin] Approve a member's quest completion and record their deed")
    async def complete(self, interaction: discord.Interaction, quest_id: RecordID, member: discord.Member, deed: Deed):
        if not await member_check(interaction, member):
            return
        await perform(interaction, store.complete_quest(interaction.guild_id, interaction.user.id, quest_id, member.id, deed,
                                                      admin=interaction.permissions.administrator, request_id=interaction.id), award_text)

    @app_commands.command(description="[Hand/Admin] Close a quest to further completions; preserve existing deeds")
    async def close(self, interaction: discord.Interaction, quest_id: RecordID):
        await perform(interaction, store.close_quest(interaction.guild_id, interaction.user.id, quest_id,
                                                   admin=interaction.permissions.administrator),
                      lambda r: f"Quest #{r['id']} closed. Existing rewards and deeds remain recorded.")


async def setup(bot: commands.Bot):
    await bot.add_cog(AcclimationCog())
    await bot.add_cog(QuestCog())
