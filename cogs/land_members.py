"""Manage the roster of the settlement selected on a land profile."""

import logging

import discord

import database as db
from cogs.report_view import NO_MENTIONS, clip


log = logging.getLogger(__name__)


async def is_staff(interaction):
    if not interaction.guild:
        return False
    if interaction.user.guild_permissions.administrator:
        return True
    role_id = await db.get_staff_role_id(interaction.guild_id)
    if not role_id:
        owner_id = interaction.guild.owner_id or (interaction.guild.owner.id if interaction.guild.owner else None)
        return interaction.user.id == owner_id
    return any(role.id == role_id for role in getattr(interaction.user, "roles", ()))


async def apply_change(interaction, land_id, user_id, action):
    land, total, was_officer = await db.change_land_roster(
        interaction.guild_id, land_id, interaction.user.id, staff=await is_staff(interaction),
        user_id=user_id, action=action)
    who = f"<@{user_id}>"
    name = clip(discord.utils.escape_markdown(land["name"]), 256)
    if action == "remove":
        title, message, colour = "Member removed", f"Removed {who} from **{name}**.", discord.Colour.orange()
        if was_officer:
            message += " Their officer appointment has also ended."
    elif action == "promote":
        title, message, colour = "Officer appointed", f"{who} is now an officer of **{name}**.", discord.Colour.blue()
        message += " They will be pinged alongside the owner for turn offers and can confirm claims."
    else:
        title, message, colour = "Member added", f"Added {who} to **{name}**.", discord.Colour.green()
    if action == "promote" or (action == "remove" and was_officer):
        rotation = interaction.client.get_cog("rotation")
        if rotation:
            try:
                await rotation.refresh_allowed_reactors(interaction.guild_id)
            except Exception:
                log.exception("Could not refresh rotation responders after a roster change")
                message += " The current rotation offer could not refresh its responders."
    receipt = discord.Embed(title=title, description=message, colour=colour)
    receipt.add_field(name="Total members", value=f"{total:,}")
    if action == "promote":
        receipt.set_footer(text=f"Appointed by {interaction.user.display_name}")
    elif action == "add":
        receipt.set_footer(text="+2 queue score per member")
    return receipt


async def member_card(interaction, *, land_id, back, user_id=None):
    land = await db.get_land_by_id(land_id)
    if not land or land["guild_id"] != interaction.guild_id:
        raise ValueError("This settlement is no longer registered here.")
    if interaction.user.id != land["owner_id"] and not await is_staff(interaction):
        raise ValueError("Only the settlement owner or staff can manage this roster.")
    members = await db.get_members(land_id)
    officers = await db.get_officers(land_id)
    embed = discord.Embed(title=clip(f"{land['name']} · Member management", 256), colour=discord.Colour.teal())
    member_label = "member" if len(members) == 1 else "members"
    officer_label = "officer" if len(officers) == 1 else "officers"
    embed.add_field(name="Discord roster", value=f"{len(members):,} {member_label} · {len(officers):,} {officer_label}")
    view = MemberManagement(interaction, land_id, back, user_id)
    if user_id is None:
        embed.description = "Choose a server member to see which actions you can take."
    else:
        if user_id == land["owner_id"]:
            status = "Settlement owner"
        elif user_id in members or user_id in officers:
            if user_id in officers:
                status = "Officer · removing this member also ends their appointment."
            else:
                status = "Member"
                view.promote.disabled = False
            view.remove.disabled = user_id not in members
        else:
            other = await db.get_land_for_user(interaction.guild_id, user_id)
            if other:
                name = clip(discord.utils.escape_markdown(other["name"]), 256)
                status = f"Registered with **{name}** · promoting them also adds them here and keeps their existing registration."
            else:
                status = "Unaffiliated · promoting this person also adds them to the roster."
                view.add.disabled = False
            view.promote.disabled = False
        embed.description = f"<@{user_id}>\n{status}"
    return dict(embed=embed, view=view)


class MemberManagement(discord.ui.View):
    def __init__(self, interaction, land_id, back, user_id):
        super().__init__(timeout=900)
        self.reader = interaction.user.id
        self.guild = interaction.guild_id
        self.channel = interaction.channel_id
        self.land_id, self.back, self.user_id = land_id, back, user_id
        self.busy = False
        if user_id is not None:
            self.person.placeholder = "Choose another server member"

    async def interaction_check(self, interaction):
        if (interaction.user.id == self.reader and interaction.guild_id == self.guild
                and interaction.channel_id == self.channel and not self.busy and not self.is_finished()):
            return True
        await interaction.response.send_message("Open Member management from your settlement profile.", ephemeral=True)
        return False

    async def change_selected(self, interaction, action):
        if self.user_id is None:
            raise ValueError("Choose a server member first.")
        if action != "remove" and interaction.guild.get_member(self.user_id) is None:
            try:
                await interaction.guild.fetch_member(self.user_id)
            except discord.NotFound:
                raise ValueError("This person has left the server. Choose another member.") from None
        receipt = await apply_change(interaction, self.land_id, self.user_id, action)
        return receipt.description

    async def update(self, interaction, *, user_id=None, action=None, back=False):
        if not await self.interaction_check(interaction):
            return
        self.busy = True
        notice = None
        try:
            await interaction.response.defer()
            if action:
                user_id = self.user_id
                try:
                    notice = await self.change_selected(interaction, action)
                except ValueError as exc:
                    notice = str(exc)
            if back:
                card = await self.back(interaction)
            else:
                card = await member_card(interaction, land_id=self.land_id, back=self.back, user_id=user_id)
        except ValueError as exc:
            card = dict(embed=None, view=None)
            notice = str(exc)
        except Exception:
            log.exception("Could not update settlement member management")
            card = dict(embed=None, view=None)
            notice = ((notice + "\n") if notice else "") + "Reopen the settlement profile to check the latest roster."
        try:
            await interaction.edit_original_response(content=clip(notice, 2000) if notice else None,
                                                     **card, allowed_mentions=NO_MENTIONS)
        finally:
            self.stop()

    @discord.ui.select(cls=discord.ui.UserSelect, placeholder="Choose a server member", row=0)
    async def person(self, interaction, select):
        target = select.values[0]
        if getattr(getattr(target, "guild", None), "id", None) != self.guild:
            return await interaction.response.send_message("Choose a member of this server.", ephemeral=True)
        await self.update(interaction, user_id=target.id)

    @discord.ui.button(label="Add member", style=discord.ButtonStyle.success, disabled=True, row=1)
    async def add(self, interaction, button):
        await self.update(interaction, action="add")

    @discord.ui.button(label="Remove member", style=discord.ButtonStyle.danger, disabled=True, row=1)
    async def remove(self, interaction, button):
        await self.update(interaction, action="remove")

    @discord.ui.button(label="Promote to officer", style=discord.ButtonStyle.primary, disabled=True, row=1)
    async def promote(self, interaction, button):
        await self.update(interaction, action="promote")

    @discord.ui.button(label="Back to settlement", row=2)
    async def profile(self, interaction, button):
        await self.update(interaction, back=True)
