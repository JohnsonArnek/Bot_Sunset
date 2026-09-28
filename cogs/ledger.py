"""
Ledger cog — Block delivery tracking for nation leaders.

When a land accepts a rotation offer the bot automatically records
that the nation leader owes them N blocks. The leader (or staff) marks
blocks as delivered via /ledger deliver. /ledger status gives a full
at-a-glance view of who is still owed blocks.
"""

import discord
from discord import app_commands
from discord.ext import commands

import database as db


async def _is_staff(interaction: discord.Interaction) -> bool:
    if not interaction.guild:
        return False
    if interaction.user.guild_permissions.administrator:
        return True
    staff_role_id = await db.get_staff_role_id(interaction.guild_id)
    if staff_role_id == 0:
        owner_id = interaction.guild.owner_id or (
            interaction.guild.owner.id if interaction.guild.owner else None
        )
        return interaction.user.id == owner_id
    return any(r.id == staff_role_id for r in getattr(interaction.user, "roles", []))


def _entry_line(entry: dict) -> str:
    """Format a single ledger entry into a short log line."""
    emoji = "📥" if entry["type"] == "owed" else "✅"
    date = entry["created_at"][:10] if entry["created_at"] else "?"
    blocks = entry["blocks"]
    note = f" — {entry['note']}" if entry.get("note") else ""
    return f"`{date}` {emoji} **{'Owed' if entry['type'] == 'owed' else 'Delivered'}** {blocks} block(s){note}"


class LedgerCog(commands.GroupCog, name="ledger"):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        super().__init__()

    # ── /ledger status ────────────────────────────────────────────────

    @app_commands.command(
        name="status",
        description="View the block delivery backlog for all lands in this server",
    )
    @app_commands.guild_only()
    async def ledger_status(self, interaction: discord.Interaction):
        await interaction.response.defer()

        if not await _is_staff(interaction):
            return await interaction.followup.send("🔒 Staff only.", ephemeral=True)

        lands = await db.get_all_ledger_status(interaction.guild_id)
        if not lands:
            return await interaction.followup.send(
                "📭 No lands found. Create some with `/land create` and run the rotation.",
                ephemeral=True,
            )

        # Split into owed vs clear
        owed_lines = []
        clear_lines = []
        for land in lands:
            outstanding = land["outstanding"]
            name = land["land_name"]
            owner = f"<@{land['owner_id']}>"
            if outstanding > 0:
                block_word = "block" if outstanding == 1 else "blocks"
                owed_lines.append(f"🔴 **{name}** — {outstanding} {block_word} owed  ({owner})")
            elif outstanding < 0:
                block_word = "block" if abs(outstanding) == 1 else "blocks"
                clear_lines.append(f"🟡 **{name}** — {abs(outstanding)} {block_word} credit  ({owner})")
            else:
                clear_lines.append(f"🟢 **{name}** — All clear")

        description_parts = []
        if owed_lines:
            description_parts.append("\n".join(owed_lines))
        if clear_lines:
            description_parts.append("\n".join(clear_lines))

        description = "\n\n".join(description_parts) if description_parts else "No data yet."

        embed = discord.Embed(
            title="📦 Block Delivery Status",
            description=description,
            colour=discord.Colour.red() if owed_lines else discord.Colour.green(),
        )
        total_outstanding = sum(max(0, l["outstanding"]) for l in lands)
        embed.set_footer(text=f"Total blocks owed across all lands: {total_outstanding}")

        await interaction.followup.send(embed=embed)

    # ── /ledger balance ───────────────────────────────────────────────

    @app_commands.command(
        name="balance",
        description="View the delivery balance for a specific land",
    )
    @app_commands.describe(land_name="Land name (leave empty to check your own land)")
    @app_commands.guild_only()
    async def ledger_balance(self, interaction: discord.Interaction, land_name: str = None):
        await interaction.response.defer()

        is_staff = await _is_staff(interaction)

        if land_name:
            if not is_staff:
                # Non-staff: only allowed to look up their own land by name
                own_land = await db.get_land_for_user(interaction.guild_id, interaction.user.id)
                if not own_land or own_land["name"].lower() != land_name.lower():
                    return await interaction.followup.send(
                        "🔒 You can only view your own land's balance.", ephemeral=True
                    )
            land = await db.get_land_by_name(interaction.guild_id, land_name)
        else:
            land = await db.get_land_for_user(interaction.guild_id, interaction.user.id)

        if not land:
            return await interaction.followup.send(
                "❌ Land not found. Specify a land name or make sure you belong to one.",
                ephemeral=True,
            )

        # Check officer/owner access for non-staff
        if not is_staff:
            officer_ids = await db.get_officers(land["id"])
            if interaction.user.id != land["owner_id"] and interaction.user.id not in officer_ids:
                return await interaction.followup.send(
                    "🔒 Only the land owner, their officers, or staff can view this.", ephemeral=True
                )

        balance = await db.get_ledger_balance(land["id"])
        history = await db.get_ledger_history(land["id"], limit=5)

        outstanding = balance["outstanding"]
        if outstanding > 0:
            status_line = f"🔴 **{outstanding} block(s) still owed** — leader needs to deliver"
            colour = discord.Colour.red()
        elif outstanding < 0:
            status_line = f"🟡 **{abs(outstanding)} block(s) credit** — over-delivered"
            colour = discord.Colour.yellow()
        else:
            status_line = "🟢 **All clear — no outstanding blocks**"
            colour = discord.Colour.green()

        embed = discord.Embed(
            title=f"📦 Ledger — {land['name']}",
            description=status_line,
            colour=colour,
        )
        embed.add_field(name="📥 Total Owed (ever)", value=str(balance["owed"]), inline=True)
        embed.add_field(name="✅ Total Delivered", value=str(balance["delivered"]), inline=True)
        embed.add_field(name="📦 Outstanding", value=str(max(0, outstanding)), inline=True)

        if history:
            log_lines = [_entry_line(e) for e in history]
            embed.add_field(
                name="📋 Recent Activity (last 5)",
                value="\n".join(log_lines),
                inline=False,
            )

        embed.set_footer(text=f"Owner: {land['owner_id']} • Use /ledger history for full log")
        await interaction.followup.send(embed=embed)

    # ── /ledger deliver ───────────────────────────────────────────────

    @app_commands.command(
        name="deliver",
        description="Mark blocks as delivered to a land (clears their owed balance)",
    )
    @app_commands.describe(
        land_name="The land that was delivered to",
        blocks="How many blocks were delivered (default: 1)",
        note="Optional note (e.g. 'delivered on 28/09, 150 ems collected')",
    )
    @app_commands.guild_only()
    async def ledger_deliver(
        self,
        interaction: discord.Interaction,
        land_name: str,
        blocks: int = 1,
        note: str = None,
    ):
        await interaction.response.defer()

        is_staff = await _is_staff(interaction)
        land = await db.get_land_by_name(interaction.guild_id, land_name)
        if not land:
            return await interaction.followup.send(
                f"❌ Land **{land_name}** not found.", ephemeral=True
            )

        # Allow: staff, land owner, or land officers
        officer_ids = await db.get_officers(land["id"])
        allowed = is_staff or interaction.user.id == land["owner_id"] or interaction.user.id in officer_ids
        if not allowed:
            return await interaction.followup.send(
                "🔒 Only the land owner, their officers, or staff can mark deliveries.", ephemeral=True
            )

        if blocks < 1:
            return await interaction.followup.send("❌ Blocks must be at least 1.", ephemeral=True)

        await db.add_ledger_delivered(
            land["id"], interaction.guild_id,
            blocks=blocks,
            note=note,
            recorded_by=interaction.user.id,
        )

        balance = await db.get_ledger_balance(land["id"])
        outstanding = balance["outstanding"]

        if outstanding > 0:
            remaining = f"🔴 {outstanding} block(s) still owed."
        elif outstanding < 0:
            remaining = f"🟡 {abs(outstanding)} block(s) over-delivered (credit)."
        else:
            remaining = "🟢 All clear — fully delivered!"

        embed = discord.Embed(
            title="✅ Delivery Recorded",
            description=(
                f"**{blocks}** block(s) marked as delivered to **{land['name']}**.\n"
                f"{remaining}"
            ),
            colour=discord.Colour.green() if outstanding <= 0 else discord.Colour.orange(),
        )
        if note:
            embed.add_field(name="Note", value=note, inline=False)
        embed.set_footer(text=f"Recorded by {interaction.user.display_name}")

        await interaction.followup.send(embed=embed)

    # ── /ledger owe ───────────────────────────────────────────────────

    @app_commands.command(
        name="owe",
        description="[Staff] Manually add an owed entry for a land (corrections/edge cases)",
    )
    @app_commands.describe(
        land_name="The land that is owed blocks",
        blocks="How many blocks to record as owed",
        note="Optional note explaining the reason",
    )
    @app_commands.guild_only()
    async def ledger_owe(
        self,
        interaction: discord.Interaction,
        land_name: str,
        blocks: int = 1,
        note: str = None,
    ):
        await interaction.response.defer(ephemeral=True)

        if not await _is_staff(interaction):
            return await interaction.followup.send("🔒 Staff only.", ephemeral=True)

        land = await db.get_land_by_name(interaction.guild_id, land_name)
        if not land:
            return await interaction.followup.send(
                f"❌ Land **{land_name}** not found.", ephemeral=True
            )

        if blocks < 1:
            return await interaction.followup.send("❌ Blocks must be at least 1.", ephemeral=True)

        await db.add_ledger_owed(
            land["id"], interaction.guild_id,
            blocks=blocks,
            note=note or "Manual entry by staff",
            recorded_by=interaction.user.id,
        )

        balance = await db.get_ledger_balance(land["id"])
        await interaction.followup.send(
            f"📥 Recorded **{blocks}** block(s) owed to **{land['name']}**. "
            f"Outstanding balance: **{balance['outstanding']}** block(s).",
            ephemeral=True,
        )

    # ── /ledger history ───────────────────────────────────────────────

    @app_commands.command(
        name="history",
        description="View the full ledger history for a land",
    )
    @app_commands.describe(
        land_name="Land name (leave empty for your own land)",
        limit="How many entries to show (default 10, max 25)",
    )
    @app_commands.guild_only()
    async def ledger_history(
        self,
        interaction: discord.Interaction,
        land_name: str = None,
        limit: int = 10,
    ):
        await interaction.response.defer()

        is_staff = await _is_staff(interaction)
        limit = max(1, min(25, limit))

        if land_name:
            if not is_staff:
                own_land = await db.get_land_for_user(interaction.guild_id, interaction.user.id)
                if not own_land or own_land["name"].lower() != land_name.lower():
                    return await interaction.followup.send(
                        "🔒 You can only view your own land's history.", ephemeral=True
                    )
            land = await db.get_land_by_name(interaction.guild_id, land_name)
        else:
            land = await db.get_land_for_user(interaction.guild_id, interaction.user.id)

        if not land:
            return await interaction.followup.send(
                "❌ Land not found. Specify a land name or make sure you belong to one.",
                ephemeral=True,
            )

        # Access check for non-staff
        if not is_staff:
            officer_ids = await db.get_officers(land["id"])
            if interaction.user.id != land["owner_id"] and interaction.user.id not in officer_ids:
                return await interaction.followup.send(
                    "🔒 Only the land owner, their officers, or staff can view this.", ephemeral=True
                )

        history = await db.get_ledger_history(land["id"], limit=limit)
        balance = await db.get_ledger_balance(land["id"])

        if not history:
            return await interaction.followup.send(
                f"📭 No ledger entries yet for **{land['name']}**."
            )

        log_lines = [_entry_line(e) for e in history]
        outstanding = balance["outstanding"]

        embed = discord.Embed(
            title=f"📋 Ledger History — {land['name']}",
            description="\n".join(log_lines),
            colour=discord.Colour.blue(),
        )
        status = (
            f"🔴 {outstanding} owed"
            if outstanding > 0
            else (f"🟡 {abs(outstanding)} credit" if outstanding < 0 else "🟢 All clear")
        )
        embed.set_footer(text=f"Showing last {len(history)} entries • Balance: {status}")

        await interaction.followup.send(embed=embed)


async def setup(bot: commands.Bot):
    await bot.add_cog(LedgerCog(bot))
