"""Reader-bound quest screens and confirmations."""

from functools import partial
import logging

import discord

import acclimation_store as store
from cogs.acclimation_display import award_text, safe
from cogs.report_view import NO_MENTIONS


log = logging.getLogger("claims-bot")
TEMPORARY = "The quest board could not be loaded. Please try /quest list again shortly."
UNAVAILABLE = "That quest is unavailable on this server. Use /quest list to browse current quests."


def can_manage(interaction, row):
    return interaction.permissions.administrator or row["reader_rank"] == "hand"


class QuestScreen(discord.ui.View):
    def __init__(self, interaction):
        super().__init__(timeout=600)
        self.owner, self.guild, self.channel = interaction.user.id, interaction.guild_id, interaction.channel_id
        self.source = interaction
        self.busy = False

    async def interaction_check(self, interaction):
        if (interaction.user.id != self.owner or interaction.guild_id != self.guild or interaction.channel_id != self.channel
                or self.is_finished() or self.busy):
            await interaction.response.send_message("Use your latest quest board, or open your own with /quest list.",
                                                   ephemeral=True, allowed_mentions=NO_MENTIONS)
            return False
        return True

    async def replace(self, interaction, render_next, *, commit=None):
        if not await self.interaction_check(interaction):
            return
        self.busy = True
        notice = next_view = None
        try:
            await interaction.response.defer()
            if commit:
                try:
                    notice = await commit(interaction)
                except ValueError as exc:
                    notice = str(exc)
            embed, next_view = await render_next(interaction)
            await interaction.edit_original_response(content=notice, embed=embed, view=next_view, allowed_mentions=NO_MENTIONS)
        except Exception as exc:
            if next_view:
                next_view.stop()
            message = UNAVAILABLE if isinstance(exc, LookupError) else TEMPORARY
            if not isinstance(exc, LookupError):
                log.warning("Quest screen unavailable (%s)", type(exc).__name__)
            if notice:
                message = f"{notice}\nReopen the quest for its latest status."
            try:
                await interaction.edit_original_response(content=message, embed=None, view=None, allowed_mentions=NO_MENTIONS)
            except (discord.HTTPException, OSError, TimeoutError):
                log.warning("Quest screen update could not be displayed")
        finally:
            self.stop()

    async def on_timeout(self):
        self.stop()
        for item in self.children:
            item.disabled = True
        try:
            await self.source.edit_original_response(view=self)
        except (discord.HTTPException, OSError, TimeoutError):
            log.info("Expired quest screen could not be updated")


class DeedForm(discord.ui.Modal, title="Record the deed"):
    deed = discord.ui.TextInput(label="What did this player do?", style=discord.TextStyle.paragraph, max_length=500)

    def __init__(self, picker, member_id):
        super().__init__(timeout=600)
        self.picker, self.member_id = picker, member_id

    async def on_submit(self, interaction):
        review = partial(action_card, quest_id=self.picker.quest_id, action="complete", back=self.picker.back,
                         member_id=self.member_id, deed=self.deed.value)
        await self.picker.replace(interaction, review)


class QuestActionView(QuestScreen):
    def __init__(self, interaction, quest_id, back):
        super().__init__(interaction)
        self.quest_id, self.back = quest_id, back
        self.remove_item(self.member)
        self.remove_item(self.confirm)
        self.commit = None

    def confirm_with(self, label, commit, style):
        self.commit = commit
        self.confirm.label, self.confirm.style = label, style
        self.add_item(self.confirm)

    @discord.ui.select(cls=discord.ui.UserSelect, placeholder="Choose the player who completed this quest", row=0)
    async def member(self, interaction, select):
        if not await self.interaction_check(interaction):
            return
        person = select.values[0]
        if getattr(getattr(person, "guild", None), "id", None) != self.guild or person.bot:
            return await interaction.response.send_message("Choose a player from this server.", ephemeral=True)
        if person.id == self.owner:
            return await interaction.response.send_message("Another Hand or administrator must approve your own deed.", ephemeral=True)
        await interaction.response.send_modal(DeedForm(self, person.id))

    @discord.ui.button(label="Confirm", row=1)
    async def confirm(self, interaction, button):
        if self.commit:
            await self.replace(interaction, self.back, commit=self.commit)

    @discord.ui.button(label="Back to quest", row=1)
    async def cancel(self, interaction, button):
        await self.replace(interaction, self.back)


async def record_completion(interaction, *, quest_id, member_id, deed, reward, completion_no):
    member = interaction.guild.get_member(member_id)
    if member is None:
        try:
            member = await interaction.guild.fetch_member(member_id)
        except discord.NotFound:
            raise ValueError("That player has left the server.") from None
    if member.bot:
        raise ValueError("Choose a player from this server.")
    award = await store.complete_quest(interaction.guild_id, interaction.user.id, quest_id, member_id, deed,
                                       admin=interaction.permissions.administrator, request_id=interaction.id,
                                       expected_reward=reward, expected_completion=completion_no)
    return award_text(award)


async def close_quest(interaction, *, quest_id):
    await store.close_quest(interaction.guild_id, interaction.user.id, quest_id, admin=interaction.permissions.administrator)
    return f"Quest #{quest_id} closed. Earlier deeds and rewards remain in the record."


class QuestDeedsView(QuestScreen):
    def __init__(self, interaction, quest_id, back, award):
        super().__init__(interaction)
        self.quest_id, self.back = quest_id, back
        self.number = award["completion_no"] if award else 0
        if award and award["latest"] > 1:
            self.older.disabled = self.number <= 1
            self.newer.disabled = self.number >= award["latest"]
        else:
            self.remove_item(self.older)
            self.remove_item(self.newer)

    async def go(self, interaction, number):
        await self.replace(interaction, partial(deed_card, quest_id=self.quest_id, back=self.back, completion_no=number))

    @discord.ui.button(label="Older deed")
    async def older(self, interaction, button):
        await self.go(interaction, self.number - 1)

    @discord.ui.button(label="Newer deed")
    async def newer(self, interaction, button):
        await self.go(interaction, self.number + 1)

    @discord.ui.button(label="Back to quest")
    async def cancel(self, interaction, button):
        await self.replace(interaction, self.back)


async def deed_card(interaction, *, quest_id, back, completion_no=None):
    award = await store.quest_deed(interaction.guild_id, interaction.user.id, quest_id, completion_no)
    view = QuestDeedsView(interaction, quest_id, back, award)
    embed = discord.Embed(title="Your quest deed", colour=discord.Colour.gold())
    if award is None:
        embed.description = "You have no recorded deed for this quest."
        return embed, view
    embed.title = f"Quest #{quest_id} · {safe(award['title'])}"
    embed.add_field(name="Your deed", value=safe(award["deed"]), inline=False)
    embed.add_field(name="Recorded reward", value=f"{award['amount']:,} Acclimations")
    embed.add_field(name="Your current total", value=f"{award['total']:,} Acclimations")
    embed.add_field(name="Recorded by", value=f"<@{award['issuer_id']}> · {award['created_at'][:10]} UTC", inline=False)
    if award["voided_at"]:
        embed.add_field(name="Voided award", value=safe(award["void_reason"] or "Excluded from the total."), inline=False)
    embed.set_footer(text=f"Completion {award['completion_no']:,} of {award['latest']:,} · Award #{award['id']}")
    return embed, view


async def action_card(interaction, *, quest_id, action, back, member_id=None, deed=None):
    row = await store.quest_for_reader(interaction.guild_id, interaction.user.id, quest_id)
    if row is None:
        raise LookupError()
    view = QuestActionView(interaction, quest_id, back)
    embed = discord.Embed(title=f"Quest #{quest_id} · {safe(row['title'])}", colour=discord.Colour.orange())
    if row["status"] != "open":
        embed.description = "This quest has already closed."
    elif not can_manage(interaction, row):
        embed.description = "Only appointed Hands and Discord administrators can manage this quest."
    elif action == "close":
        embed.description = "Close this quest to future completions? Earlier deeds and rewards stay in the record."
        view.confirm_with("Confirm closure", partial(close_quest, quest_id=quest_id), discord.ButtonStyle.danger)
    elif member_id is None:
        embed.description = f"Choose the player who earned **{row['reward']:,} Acclimations**, then write down their deed."
        view.add_item(view.member)
    else:
        target = await store.quest_for_reader(interaction.guild_id, member_id, quest_id)
        if target is None or target["status"] != "open":
            embed.description = "This quest is no longer open."
        elif target["award_id"] is not None and target["quest_type"] != "repeatable":
            embed.description = f"<@{member_id}> already received this quest’s reward (award #{target['award_id']})."
        else:
            try:
                deed = store.clean(deed, 500)
            except ValueError as exc:
                embed.description = str(exc)
            else:
                embed.description = f"Award **{row['reward']:,} Acclimations** to <@{member_id}> for this deed?"
                number = (target["completion_no"] or 0) + 1
                if target["quest_type"] == "unique":
                    embed.description += " This will close the quest for everyone."
                elif target["quest_type"] == "repeatable":
                    embed.description += f" This is their completion **#{number}**."
                embed.add_field(name="Deed", value=safe(deed), inline=False)
                commit = partial(record_completion, quest_id=quest_id, member_id=member_id, deed=deed,
                                 reward=row["reward"], completion_no=number)
                view.confirm_with("Confirm reward", commit, discord.ButtonStyle.success)
    return embed, view
