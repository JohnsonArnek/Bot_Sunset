"""Private member submissions and the Hand review queue."""

from functools import partial

import discord

import quest_submissions as submissions
from cogs.acclimation_display import safe
from cogs.quest_actions import QuestScreen, record_completion
from cogs.report_view import ReportView, clip


STATUS = {"pending": "Awaiting review", "approved": "Approved", "returned": "Returned with a note",
          "closed": "Closed", "revised": "Resubmitted"}


class SubmitForm(discord.ui.Modal, title="Submit a quest deed"):
    deed = discord.ui.TextInput(label="What did you do?", style=discord.TextStyle.paragraph, max_length=500)
    link = discord.ui.TextInput(label="Discord message link (optional)", required=False, max_length=200)

    def __init__(self, parent, quest_id, back, prior=None):
        super().__init__(timeout=600)
        self.parent, self.quest_id, self.back = parent, quest_id, back
        self.revision_of = prior["id"] if prior else None
        if prior:
            self.title = "Revise your deed"
            self.deed.default, self.link.default = prior["deed"], prior["evidence"]

    async def on_submit(self, interaction):
        async def save(i):
            row = await submissions.submit(i.guild_id, i.user.id, self.quest_id, self.deed.value, self.link.value,
                                           request_id=i.id, revision_of=self.revision_of)
            return f"Submission #{row['id']} · {STATUS[row['status']]}."
        await self.parent.replace(interaction, self.back, commit=save)


class ReturnForm(discord.ui.Modal, title="Return deed with a note"):
    note = discord.ui.TextInput(label="What should the member clarify?", style=discord.TextStyle.paragraph, max_length=400)

    def __init__(self, parent):
        super().__init__(timeout=600)
        self.parent = parent

    async def on_submit(self, interaction):
        async def save(i):
            await submissions.return_deed(i.guild_id, i.user.id, self.parent.row["id"], self.note.value,
                                          admin=i.permissions.administrator)
            return "Deed returned. The member can read your note and revise their submission."
        await self.parent.replace(interaction, self.parent.back, commit=save)


class SubmissionView(QuestScreen):
    def __init__(self, interaction, row, review, back):
        super().__init__(interaction)
        self.row, self.back = row, back
        if not (row and review and row["status"] == "pending"):
            self.remove_item(self.approve)
            self.remove_item(self.send_back)
        if not (row and not review and row["status"] == "returned" and row["quest_status"] == "open"):
            self.remove_item(self.revise)

    @discord.ui.button(label="Approve", style=discord.ButtonStyle.success)
    async def approve(self, interaction, button):
        row = self.row
        commit = partial(record_completion, quest_id=row["quest_id"], member_id=row["member_id"], deed=row["deed"],
                         reward=row["reward"], submission_id=row["id"])
        await self.replace(interaction, self.back, commit=commit)

    @discord.ui.button(label="Return with a note")
    async def send_back(self, interaction, button):
        if await self.interaction_check(interaction):
            await interaction.response.send_modal(ReturnForm(self))

    @discord.ui.button(label="Revise deed", style=discord.ButtonStyle.primary)
    async def revise(self, interaction, button):
        if await self.interaction_check(interaction):
            await interaction.response.send_modal(SubmitForm(self, self.row["quest_id"], self.back, self.row))

    @discord.ui.button(label="Back")
    async def cancel(self, interaction, button):
        await self.replace(interaction, self.back)


class SubmissionList(ReportView):
    def __init__(self, interaction, render, page, pages, choices, back):
        super().__init__(interaction, render, page, pages, choices=choices, prompt="Choose a submission")
        if back is None:
            self.remove_item(self.exit_list)

    @discord.ui.button(label="Back to quests", row=2)
    async def exit_list(self, interaction, button):
        await self.navigate(interaction, 1, "quests")


async def submission_screen(interaction, page=1, selection=None, *, review=False, quest_id=None, back=None, detail_back=None):
    if selection == "quests" and back:
        return await back(interaction)
    render = partial(submission_screen, review=review, quest_id=quest_id, back=back)
    guild, actor, admin = interaction.guild_id, interaction.user.id, interaction.permissions.administrator
    try:
        if selection is None:
            rows, page, pages = await submissions.listing(guild, actor, admin=admin, review=review, page=page, quest_id=quest_id)
            title = "Deeds awaiting review" if review else "My submissions"
            lines = [f"**#{r['id']} · {safe(r['title'])}**\n" +
                     (f"<@{r['member_id']}>" if review else STATUS[r["status"]]) for r in rows]
            embed = discord.Embed(title=title, description="\n\n".join(lines) or "No submissions here.", colour=discord.Colour.gold())
            embed.set_footer(text=f"Page {page}/{pages}")
            return embed, SubmissionList(interaction, render, page, pages, [(r["id"], f"#{r['id']} · {r['title']}") for r in rows], back)
        row = await submissions.detail(guild, actor, selection, admin=admin, review=review)
    except ValueError as exc:
        return discord.Embed(description=str(exc)), None
    view = SubmissionView(interaction, row, review, detail_back or partial(render, page=page))
    if row is None:
        return discord.Embed(description="This submission is unavailable."), view
    embed = discord.Embed(title=clip(f"Submission #{row['id']} · {safe(row['title'])}", 256),
                          description=STATUS[row["status"]], colour=discord.Colour.gold())
    embed.add_field(name="Submitted by", value=f"<@{row['member_id']}>")
    embed.add_field(name="Deed", value=safe(row["deed"]), inline=False)
    if row["evidence"]:
        embed.add_field(name="Evidence", value=f"[Open Discord message]({row['evidence']})")
    if row["note"]:
        embed.add_field(name="Note", value=safe(row["note"]), inline=False)
    if row["award_id"]:
        embed.add_field(name="Recorded award", value=f"#{row['award_id']}")
    if review and row["status"] == "pending":
        effect = " Approval closes this unique quest for everyone." if row["quest_type"] == "unique" else ""
        embed.add_field(name="Approval", value=f"Award **{row['reward']:,} Acclimations** for this deed.{effect}", inline=False)
    return embed, view
