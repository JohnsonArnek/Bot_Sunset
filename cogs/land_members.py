"""Manage the roster of the settlement selected on a land profile."""

import asyncio
import logging

import aiohttp
import discord

import database as db
from cogs.report_view import NO_MENTIONS, clip


log = logging.getLogger(__name__)
SAVED_PAGE_SIZE = 10
LOOKUP_ERRORS = (discord.HTTPException, discord.RateLimited, aiohttp.ClientError, OSError, asyncio.TimeoutError)


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


async def saved_name(interaction, user_id, names):
    """Resolve only the visible page. Older registrations can consist of just an ID."""
    user = interaction.guild.get_member(user_id) or interaction.client.get_user(user_id)
    if user:
        return user.display_name
    if user_id not in names:
        try:
            user = await asyncio.wait_for(interaction.client.fetch_user(user_id), timeout=3)
            names[user_id] = user.display_name
        except LOOKUP_ERRORS:
            names[user_id] = f"Discord ID {user_id}"
    return names[user_id]


async def server_membership(guild, user_id):
    if guild.get_member(user_id) is None:
        try:
            await asyncio.wait_for(guild.fetch_member(user_id), timeout=3)
        except discord.NotFound as exc:
            if exc.code in (10007, 10013):  # Unknown member or unknown user
                return "No longer in this Discord server"
            return "Server membership unverified"
        except LOOKUP_ERRORS:
            return "Server membership unverified"
    return "In this Discord server"


class SavedRosterSelect(discord.ui.Select):
    def __init__(self, options):
        super().__init__(placeholder="Choose a saved roster entry", options=options, row=0)

    async def callback(self, interaction):
        selected = self.values[0]
        if selected not in {option.value for option in self.options}:
            return await interaction.response.send_message("Choose an entry on this page.", ephemeral=True)
        await self.view.update(interaction, user_id=int(selected), saved_page=self.view.saved_page)


async def saved_roster_card(interaction, embed, view, roster_ids, officers, page):
    pages = max(1, (len(roster_ids) + SAVED_PAGE_SIZE - 1) // SAVED_PAGE_SIZE)
    view.saved_page = min(max(page, 1), pages)
    start = (view.saved_page - 1) * SAVED_PAGE_SIZE
    visible = roster_ids[start:start + SAVED_PAGE_SIZE]
    labels = await asyncio.gather(*(saved_name(interaction, uid, view.names) for uid in visible))
    options = []
    embed.description = ("Choose a saved entry, including anyone who has left the server. "
                         "Review the person before pressing **Remove member**." if visible
                         else "There are no member or officer entries to remove.")
    for uid, label in zip(visible, labels):
        role = "Officer" if uid in officers else "Member"
        selected = uid == view.user_id
        status = "In server" if interaction.guild.get_member(uid) else "Check server status"
        options.append(discord.SelectOption(label=clip(label, 100), value=str(uid),
                                            description=f"{role} · {status} · ID {uid}", default=selected))
        if selected:
            name = discord.utils.escape_markdown(label)
            membership = await server_membership(interaction.guild, uid)
            embed.description = f"**{name}** · {role}\n{membership} · Discord ID `{uid}`"
            if uid in officers:
                embed.description += "\nRemoving this officer also ends their appointment."
            view.remove.disabled = False
    for item in (view.person, view.add, view.promote):
        view.remove_item(item)
    if options:
        view.add_item(SavedRosterSelect(options))
    view.saved.label = "Server member search"
    if pages > 1:
        view.previous.disabled = view.saved_page == 1
        view.next_page.disabled = view.saved_page == pages
    else:
        view.remove_item(view.previous)
        view.remove_item(view.next_page)
    embed.set_footer(text=f"Saved roster · Page {view.saved_page}/{pages}")
    return dict(embed=embed, view=view)


async def member_card(interaction, *, land_id, back, user_id=None, saved_page=None, names=None):
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
    view = MemberManagement(interaction, land_id, back, user_id, names)
    roster = (set(members) | set(officers)) - {land["owner_id"]}
    if saved_page is not None:
        # Put people missing from the server cache first. Selecting one verifies their membership.
        ordered = sorted(roster, key=lambda uid: (interaction.guild.get_member(uid) is not None, uid))
        return await saved_roster_card(interaction, embed, view, ordered, set(officers), saved_page)
    view.remove_item(view.previous)
    view.remove_item(view.next_page)
    view.saved.disabled = not roster
    if user_id is None:
        embed.description = "Choose a server member to see which actions you can take."
        if roster:
            embed.description += " For someone who has left, open **Saved roster**."
    else:
        if user_id == land["owner_id"]:
            status = "Settlement owner"
        elif user_id in members or user_id in officers:
            if user_id in officers:
                status = "Officer · removing this member also ends their appointment."
            else:
                status = "Member"
                view.promote.disabled = False
            view.remove.disabled = False
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
    def __init__(self, interaction, land_id, back, user_id, names):
        super().__init__(timeout=900)
        self.reader = interaction.user.id
        self.guild = interaction.guild_id
        self.channel = interaction.channel_id
        self.land_id, self.back, self.user_id = land_id, back, user_id
        self.saved_page = None
        self.names = names if names is not None else {}
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
            raise ValueError("Choose a person first.")
        if self.saved_page is not None and (action != "remove" or self.remove.disabled):
            raise ValueError("Select a saved roster entry before removing it.")
        if action != "remove" and interaction.guild.get_member(self.user_id) is None:
            try:
                await interaction.guild.fetch_member(self.user_id)
            except discord.NotFound:
                raise ValueError("This person has left the server. Choose another member.") from None
        receipt = await apply_change(interaction, self.land_id, self.user_id, action)
        if self.saved_page is not None:
            return f"{receipt.description}\nDiscord ID `{self.user_id}`"
        return receipt.description

    async def update(self, interaction, *, user_id=None, action=None, back=False, saved_page=None):
        if not await self.interaction_check(interaction):
            return
        self.busy = True
        notice = None
        try:
            await interaction.response.defer()
            if action:
                user_id = self.user_id
                saved_page = self.saved_page
                try:
                    notice = await self.change_selected(interaction, action)
                    if saved_page is not None:
                        user_id = None
                except ValueError as exc:
                    notice = str(exc)
            if back:
                card = await self.back(interaction)
            else:
                card = await member_card(interaction, land_id=self.land_id, back=self.back, user_id=user_id,
                                         saved_page=saved_page, names=self.names)
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

    @discord.ui.button(label="Saved roster", row=2)
    async def saved(self, interaction, button):
        await self.update(interaction, saved_page=1 if self.saved_page is None else None)

    @discord.ui.button(label="Back to settlement", row=2)
    async def profile(self, interaction, button):
        await self.update(interaction, back=True)

    @discord.ui.button(label="Previous", row=3)
    async def previous(self, interaction, button):
        await self.update(interaction, saved_page=self.saved_page - 1)

    @discord.ui.button(label="Next", row=3)
    async def next_page(self, interaction, button):
        await self.update(interaction, saved_page=self.saved_page + 1)
