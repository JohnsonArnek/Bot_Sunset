"""Settlement actions on the private staff ledger."""

import logging
from typing import NamedTuple

import discord

import banner_store
import database as db
from cogs.report_view import NO_MENTIONS, ReportView, clip


class SettlementSelection(NamedTuple):
    land_id: int
    receipt_page: int = 1
    receipt_id: int | None = None


class ReceiptSelect(discord.ui.Select):
    def __init__(self, rows, selected):
        super().__init__(placeholder="Choose an unpaid purchase", row=0, options=[
            discord.SelectOption(label=f"Purchase #{r['id']}", value=str(r["id"]), default=r["id"] == selected,
                                 description=f"{r['unpaid']:,} unpaid · {r['sale_unit']:,} ems per banner")
            for r in rows])

    async def callback(self, interaction):
        selected = self.view.selection._replace(receipt_id=int(self.values[0]))
        await self.view.navigate(interaction, self.view.page, selected)


class SettlementActions(ReportView):
    def __init__(self, interaction, render, staff_check, directory_page, land, receipts, purchase, pickup, page, pages):
        selected = SettlementSelection(land["land_id"], page, purchase["id"] if purchase else None)
        super().__init__(interaction, render, directory_page, 1, selection=selected)
        self.staff_check = staff_check
        self.land = land
        self.purchase = purchase
        self.pickup_limit = pickup
        for control in (self.back, self.refresh):
            self.remove_item(control)
            control.row = 4
            self.add_item(control)
        if len(receipts) > 1:
            self.add_item(ReceiptSelect(receipts, selected.receipt_id))
        if land["owner_id"] is None or land["outstanding"] <= 0:
            self.remove_item(self.deliver)
        if not pickup:
            self.remove_item(self.pickup)
        if not purchase:
            self.remove_item(self.collect)
        elif purchase["unpaid"] == 1:
            self.collect.label = f"Confirm cash only · {purchase['sale_unit']:,} ems"
        if pages == 1:
            self.remove_item(self.older)
            self.remove_item(self.newer)
        else:
            self.older.disabled = page >= pages
            self.newer.disabled = page <= 1

    async def permitted(self, interaction, action):
        return (action == "delivery" or interaction.permissions.administrator) and await self.staff_check(interaction)

    async def open_form(self, interaction, action):
        if not await self.interaction_check(interaction):
            return
        if not await self.permitted(interaction, action):
            return await interaction.response.send_message("You no longer have permission for this action.", ephemeral=True)
        await interaction.response.send_modal(SettlementForm(self, action))

    @discord.ui.button(label="Record delivery", row=2, style=discord.ButtonStyle.primary)
    async def deliver(self, interaction, button):
        await self.open_form(interaction, "delivery")

    @discord.ui.button(label="Deliver and collect cash", row=2, style=discord.ButtonStyle.success)
    async def pickup(self, interaction, button):
        await self.open_form(interaction, "pickup")

    @discord.ui.button(label="Confirm cash only", row=2)
    async def collect(self, interaction, button):
        await self.open_form(interaction, "payment")

    @discord.ui.button(label="Newer receipts", row=3)
    async def newer(self, interaction, button):
        await self.navigate(interaction, self.page, SettlementSelection(self.selection.land_id, self.selection.receipt_page - 1))

    @discord.ui.button(label="Older receipts", row=3)
    async def older(self, interaction, button):
        await self.navigate(interaction, self.page, SettlementSelection(self.selection.land_id, self.selection.receipt_page + 1))


class SettlementForm(discord.ui.Modal):
    def __init__(self, report, action):
        self.report = report
        self.action = action
        purchase = report.purchase
        if action == "delivery":
            title = f"Record delivery · {report.land['land_name']}"
            label = "Banners delivered"
            self.limit = report.land["outstanding"]
        else:
            verb = "Cash pickup" if action == "pickup" else "Confirm cash only"
            title = f"{verb} · purchase #{purchase['id']}"
            label = f"Banners paid at {purchase['sale_unit']:,} ems each"
            self.limit = report.pickup_limit if action == "pickup" else purchase["unpaid"]
        super().__init__(title=clip(title, 45), timeout=240)
        self.quantity = discord.ui.TextInput(label=label, default="1", max_length=10,
                                             placeholder=f"1–{self.limit:,}")
        self.note = discord.ui.TextInput(label="Optional ledger note", required=False, max_length=250)
        self.add_item(self.quantity)
        self.add_item(self.note)

    async def on_submit(self, interaction):
        report = self.report
        if not await report.interaction_check(interaction):
            return
        quantity = self.quantity.value.strip()
        if not quantity.isascii() or not quantity.isdecimal() or not 1 <= int(quantity) <= self.limit:
            return await interaction.response.send_message(f"Enter a whole quantity from 1 to {self.limit:,}.", ephemeral=True)
        blocks = int(quantity)
        note = self.note.value.strip() or None
        report.busy = True
        try:
            await interaction.response.defer()
            if not await report.permitted(interaction, self.action):
                return await interaction.followup.send("You no longer have permission for this action.", ephemeral=True)
            if self.action == "delivery":
                await db.add_ledger_delivered(report.selection.land_id, report.guild, blocks, note, interaction.user.id,
                                              expected_revision=report.land["ledger_revision"])
                noun = "banner" if blocks == 1 else "banners"
                notice = f"Recorded delivery of {blocks:,} {noun}."
            else:
                pickup = self.action == "pickup"
                receipt = await banner_store.create(
                    report.guild, interaction.user.id, report.purchase["id"], blocks,
                    note or "Physical emeralds received", admin=interaction.permissions.administrator,
                    request_id=interaction.id, land_payment="physical", expected_created=report.purchase["created"],
                    delivery_land_id=report.selection.land_id if pickup else None, delivery_note=note if pickup else None,
                    expected_revision=report.land["ledger_revision"] if pickup else None)
                effect = f"{blocks:,} delivered" if pickup else "payment only"
                notice = f"Purchase #{receipt['purchase_id']} · {receipt['physical']:,} ems received · {effect}."
            await report.update_message(interaction, report.page, report.selection, notice)
        except ValueError as exc:
            await interaction.followup.send(str(exc), ephemeral=True, allowed_mentions=NO_MENTIONS)
        finally:
            report.busy = False

    async def on_error(self, interaction, error):
        logging.getLogger(__name__).error("Settlement action failed", exc_info=error)
        send = interaction.followup.send if interaction.response.is_done() else interaction.response.send_message
        await send("Refresh the settlement and check recent activity before trying again.", ephemeral=True)
