"""Private account screens for players, settlement leaders and bank administrators."""

from functools import partial
import logging
from typing import NamedTuple

import discord

import bank_store as store
from cogs.report_view import NO_MENTIONS, clip


def account_name(row):
    if row["kind"] == "member":
        return f"<@{row['subject_id']}>"
    return discord.utils.escape_markdown(row["label"])[:100]


def account_text(row, page=1, *, statement=False):
    lines = [f"**{account_name(row)} · account #{row['id']}**", f"Balance: **{row['balance']:,} ems**"]
    if row["banners"]:
        b = row["banners"]
        share = b["land_share"] if row["kind"] == "land" else b["central_share"]
        needed = b["land_share"]-b["prepaid"] if row["kind"] == "land" else b["central_share"]+b["prepaid"]
        lines.append(f"**Creation funding for sold banners** · {b['pending']:,} awaiting settlement\n"
                     f"This budget's share: **{share:,} ems** · bank funding still needed: **{needed:,} ems**\n"
                     f"Land contributions already transferred to treasury: {b['prepaid']:,} ems.\n"
                     "Funds are not reserved. Physical land payment can be confirmed by an administrator at creation.")
        if b["premium_due"]:
            lines.append(f"Manual reserve premiums due to the treasury: **{b['premium_due']:,} ems**.")
    if statement:
        lines.append(f"**Statement · page {page} · 3 entries per page**")
        for e in row["entries"]:
            lines.append(f"**#{e['operation_id']} · {e['delta']:+,} ems** · {e['action']} · {e['created_at'][:10]} UTC\n"
                         f"By <@{e['actor_id']}> · balance after entry: {e['balance_after']:,} ems\n"
                         f"{discord.utils.escape_markdown(e['reason'])}")
        if not row["entries"]:
            lines.append("No entries on this page.")
    else:
        lines.append("Balances use whole emeralds. An administrator records deposits and withdrawals.")
    return "\n\n".join(lines)


def adjustment_receipt(row):
    return (f"**{'Already recorded' if row['duplicate'] else 'Recorded'} · transaction #{row['operation_id']}**\n"
            f"{account_kind(row)} · {account_name(row)} · account #{row['account_id']}\n"
            f"Change: **{row['delta']:+,} ems** · balance after transaction: **{row['balance']:,} ems**\n"
            f"Reason: {discord.utils.escape_markdown(row['reason'])}")


class Directory(NamedTuple):
    section: str = "accounts"
    page: int = 1
    return_page: int = 1


class PublicBankButton(discord.ui.DynamicItem[discord.ui.Button], template=r"sunset:bank:(?P<section>mine|settlements)"):
    """Long-lived public report buttons always open a new private screen for the clicker."""
    def __init__(self, section):
        self.section = section
        super().__init__(discord.ui.Button(label="My account" if section == "mine" else "Settlement bank",
                                          custom_id=f"sunset:bank:{section}"))

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls(match["section"])

    async def callback(self, interaction):
        if interaction.guild_id is None:
            return await interaction.response.send_message("Open the bank in your server.", ephemeral=True)
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            if self.section == "mine":
                card = await account_card(interaction, statement=False)
            else:
                card = await directory_card(interaction, directory=Directory("settlements"))
        except Exception:
            logging.getLogger(__name__).exception("Could not open private bank screen")
            return await interaction.followup.send("Could not open the bank. Try /bank balance.", ephemeral=True)
        await interaction.followup.send(**card, ephemeral=True, allowed_mentions=NO_MENTIONS)


def public_links():
    view = discord.ui.View(timeout=None)
    view.add_item(PublicBankButton("mine"))
    view.add_item(PublicBankButton("settlements"))
    return view


def holder_name(interaction, row, member_name=None):
    if row["kind"] != "member":
        return row["label"]
    if member_name:
        return member_name
    if interaction.user.id == row["subject_id"] and getattr(interaction.user, "display_name", None):
        return interaction.user.display_name
    guild = getattr(interaction, "guild", None)
    member = guild.get_member(row["subject_id"]) if guild and hasattr(guild, "get_member") else None
    return member.display_name if member else row["label"]


def account_kind(row):
    if row.get("archived"):
        return "Archived settlement"
    return {"member": "Player", "land": "Settlement", "treasury": "Treasury"}[row["kind"]]


def stable_target(row):
    if row.get("archived"):
        return {"account_id": row["id"]}
    if row["kind"] == "land":
        return {"land_id": row["subject_id"]}
    if row["kind"] == "member":
        return {"member": row["subject_id"]}
    return {"treasury": True}


class BankView(discord.ui.View):
    def __init__(self, interaction):
        super().__init__(timeout=900)
        self.guild, self.reader = interaction.guild_id, interaction.user.id

    async def interaction_check(self, interaction):
        if interaction.guild_id == self.guild and interaction.user.id == self.reader:
            return True
        await interaction.response.send_message("Open your own banking screen.", ephemeral=True)
        return False

    async def show(self, interaction, render):
        if not await self.interaction_check(interaction):
            return
        await interaction.response.defer()
        try:
            card = await render(interaction)
        except ValueError as exc:
            card = dict(content=str(exc), embed=None, view=None)
        await interaction.edit_original_response(**({"content": None} | card), allowed_mentions=NO_MENTIONS)
        self.stop()

    async def on_error(self, interaction, error, item):
        logging.getLogger(__name__).exception("Banking screen failed", exc_info=error)
        send = interaction.followup.send if interaction.response.is_done() else interaction.response.send_message
        await send("Could not load this banking screen. Reopen /bank balance.", ephemeral=True)


class SelectAccount(discord.ui.Select):
    def __init__(self, choices, directory):
        self.directory = directory
        self.targets = {str(i): target for i, (_, target) in enumerate(choices)}
        super().__init__(row=0, placeholder="Choose a settlement" if directory.section == "settlements" else "Open a listed account",
                         options=[discord.SelectOption(label=clip(label, 100), value=str(i))
                                  for i, (label, _) in enumerate(choices)])

    async def callback(self, interaction):
        if self.directory.section == "accounts" and not interaction.permissions.administrator:
            return await interaction.response.send_message("Only Discord administrators can browse the directory.", ephemeral=True)
        await self.view.show(interaction, partial(account_card, origin=self.directory, **self.targets[self.values[0]]))


class DirectoryView(BankView):
    def __init__(self, interaction, directory, choices, more):
        super().__init__(interaction)
        self.directory = directory
        if choices:
            self.add_item(SelectAccount(choices, directory))
        self.previous.disabled = directory.page == 1
        self.next_page.disabled = not more
        if self.previous.disabled and self.next_page.disabled:
            self.remove_item(self.previous)
            self.remove_item(self.next_page)
        if directory.section == "settlements":
            self.remove_item(self.player)
            self.remove_item(self.settlements)
        if directory.section == "accounts" or not interaction.permissions.administrator:
            self.remove_item(self.accounts)

    @discord.ui.select(cls=discord.ui.UserSelect, placeholder="Choose a player", row=1)
    async def player(self, interaction, select):
        member = select.values[0]
        if not interaction.permissions.administrator:
            return await interaction.response.send_message("Only Discord administrators can select another player.", ephemeral=True)
        if member.bot or getattr(getattr(member, "guild", None), "id", None) != self.guild:
            return await interaction.response.send_message("Choose a player from this server.", ephemeral=True)
        await self.show(interaction, partial(account_card, member=member.id, member_name=member.display_name, origin=self.directory))

    @discord.ui.button(label="Choose a settlement", row=2, style=discord.ButtonStyle.primary)
    async def settlements(self, interaction, button):
        await self.show(interaction, partial(directory_card, directory=Directory("settlements", 1, self.directory.page)))

    @discord.ui.button(label="Back to accounts", row=2)
    async def accounts(self, interaction, button):
        await self.show(interaction, partial(directory_card, directory=Directory(page=self.directory.return_page)))

    @discord.ui.button(label="My account", row=2)
    async def mine(self, interaction, button):
        await self.show(interaction, account_card)

    @discord.ui.button(label="Previous", row=3)
    async def previous(self, interaction, button):
        await self.show(interaction, partial(directory_card, directory=self.directory._replace(page=self.directory.page - 1)))

    @discord.ui.button(label="Next", row=3)
    async def next_page(self, interaction, button):
        await self.show(interaction, partial(directory_card, directory=self.directory._replace(page=self.directory.page + 1)))


async def directory_card(interaction, *, directory=Directory()):
    guild, admin = interaction.guild_id, interaction.permissions.administrator
    if directory.section == "settlements":
        rows, more = await store.settlement_page(guild, interaction.user.id, admin=admin, page=directory.page)
        choices = [(r["name"], {"land_id": r["id"]}) for r in rows]
        lines = [discord.utils.escape_markdown(clip(r["name"], 100)) for r in rows]
        title = "Settlement banks"
        empty = "You have no settlement accounts on this page."
    else:
        rows, more = await store.accounts_page(guild, admin=admin, page=directory.page)
        choices = [(f"#{r['id']} · {account_kind(r)} · {holder_name(interaction, r)}", stable_target(r)) for r in rows]
        lines = [f"#{r['id']} · {account_kind(r)} · {account_name(r)} · **{r['balance']:,} ems**" for r in rows]
        title = "Bank administration"
        empty = "There are no accounts on this page."
    embed = discord.Embed(title=title, description="\n".join(lines) or empty, colour=discord.Colour.gold())
    embed.set_footer(text=f"Page {directory.page} · Private")
    return dict(embed=embed, view=DirectoryView(interaction, directory, choices, more))


class AccountView(BankView):
    def __init__(self, interaction, row, page, statement, origin, member_name):
        super().__init__(interaction)
        self.target = stable_target(row)
        self.page, self.statement, self.origin = page, statement, origin
        self.member_name = member_name
        self.holder = f"{account_kind(row)} #{row['id']} · {holder_name(interaction, row, member_name)}"
        self.toggle.label = "Balance" if statement else "Statement"
        self.previous.disabled = page <= 1
        self.next_page.disabled = not row["has_more"]
        if not statement or (self.previous.disabled and self.next_page.disabled):
            self.remove_item(self.previous)
            self.remove_item(self.next_page)
        if not interaction.permissions.administrator:
            for item in (self.deposit, self.withdraw, self.admin):
                self.remove_item(item)
        if origin:
            self.back.label = "Back to settlements" if origin.section == "settlements" else "Back to accounts"
            self.remove_item(self.admin)
        else:
            self.remove_item(self.back)
        if row["kind"] == "member" and row["subject_id"] == self.reader:
            self.remove_item(self.mine)
        else:
            self.remove_item(self.settlements)

    def render(self, *, page=1, statement=True):
        return partial(account_card, page=page, statement=statement, origin=self.origin,
                       member_name=self.member_name, **self.target)

    @discord.ui.button(label="Statement", row=0, style=discord.ButtonStyle.primary)
    async def toggle(self, interaction, button):
        await self.show(interaction, self.render(statement=not self.statement))

    @discord.ui.button(label="Previous", row=0)
    async def previous(self, interaction, button):
        await self.show(interaction, self.render(page=self.page-1))

    @discord.ui.button(label="Next", row=0)
    async def next_page(self, interaction, button):
        await self.show(interaction, self.render(page=self.page+1))

    @discord.ui.button(label="My account", row=2)
    async def mine(self, interaction, button):
        await self.show(interaction, account_card)

    @discord.ui.button(label="Settlement bank", row=2)
    async def settlements(self, interaction, button):
        await self.show(interaction, partial(directory_card, directory=Directory("settlements")))

    @discord.ui.button(label="Administer accounts", row=2)
    async def admin(self, interaction, button):
        await self.show(interaction, directory_card)

    @discord.ui.button(label="Back to accounts", row=2)
    async def back(self, interaction, button):
        await self.show(interaction, partial(directory_card, directory=self.origin))

    async def form(self, interaction, withdraw):
        if not await self.interaction_check(interaction):
            return
        if not interaction.permissions.administrator:
            return await interaction.response.send_message("Only Discord administrators can change balances.", ephemeral=True)
        await interaction.response.send_modal(AdjustmentForm(self, interaction.id, withdraw))

    @discord.ui.button(label="Deposit", style=discord.ButtonStyle.success, row=1)
    async def deposit(self, interaction, button):
        await self.form(interaction, False)

    @discord.ui.button(label="Withdraw", row=1)
    async def withdraw(self, interaction, button):
        await self.form(interaction, True)


class AdjustmentForm(discord.ui.Modal):
    amount = discord.ui.TextInput(label="Amount in emeralds", min_length=1, max_length=10)
    reason = discord.ui.TextInput(label="Reason", max_length=250, style=discord.TextStyle.paragraph)

    def __init__(self, screen, request_id, withdraw):
        verb = "Withdraw" if withdraw else "Deposit"
        super().__init__(title=clip(f"{verb} · {screen.holder}", 45), timeout=300)
        self.screen, self.request_id, self.withdraw = screen, request_id, withdraw

    async def on_submit(self, interaction):
        if not await self.screen.interaction_check(interaction):
            return
        if self.screen.is_finished():
            return await interaction.response.send_message("This account screen has closed. Check its statement before opening a new form.", ephemeral=True)
        value = self.amount.value.strip()
        if not value.isascii() or not value.isdecimal():
            return await interaction.response.send_message("Enter a whole number of emeralds.", ephemeral=True)
        await interaction.response.defer()
        try:
            receipt = await store.adjust(self.screen.guild, interaction.user.id, int(value), self.reason.value,
                                         admin=interaction.permissions.administrator, request_id=self.request_id,
                                         withdraw=self.withdraw, **self.screen.target)
        except ValueError as exc:
            return await interaction.followup.send(str(exc), ephemeral=True, allowed_mentions=NO_MENTIONS)
        notice = adjustment_receipt(receipt)
        try:
            card = await self.screen.render()(interaction)
            await interaction.edit_original_response(content=notice, **card, allowed_mentions=NO_MENTIONS)
            self.screen.stop()
        except Exception:
            logging.getLogger(__name__).exception("Could not refresh bank screen after transaction %s", receipt["operation_id"])
            await interaction.followup.send(notice, ephemeral=True, allowed_mentions=NO_MENTIONS)

    async def on_error(self, interaction, error):
        logging.getLogger(__name__).exception("Bank form failed", exc_info=error)
        send = interaction.followup.send if interaction.response.is_done() else interaction.response.send_message
        await send("Check the account statement before trying again.", ephemeral=True)


async def account_card(interaction, *, page=1, statement=True, origin=None, member_name=None, **target):
    row = await store.account(interaction.guild_id, interaction.user.id, admin=interaction.permissions.administrator,
                              page=page, **target)
    title = {"land": "Settlement bank", "member": "Personal bank account", "treasury": "Central treasury"}[row["kind"]]
    if row["archived"]:
        title = "Archived settlement bank"
    embed = discord.Embed(title=title, description=account_text(row, page, statement=statement), colour=discord.Colour.gold())
    return dict(embed=embed, view=AccountView(interaction, row, page, statement, origin, member_name))
