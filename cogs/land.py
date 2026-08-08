"""
Land cog — /land create, /land info, /land list, /land member_add, /land member_remove, /land set_chunks
All commands are scoped per Discord server (guild).
"""

import discord
from discord import app_commands
from discord.ext import commands

import database as db
import models


async def _is_staff(interaction: discord.Interaction) -> bool:
    if not interaction.guild:
        return False
    if interaction.user.guild_permissions.administrator:
        return True
    staff_role_id = await db.get_staff_role_id(interaction.guild_id)
    if staff_role_id == 0:
        owner_id = interaction.guild.owner_id or (interaction.guild.owner.id if interaction.guild.owner else None)
        return interaction.user.id == owner_id
    return any(r.id == staff_role_id for r in getattr(interaction.user, "roles", []))


class LandCog(commands.GroupCog, name="land"):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        super().__init__()

    @app_commands.command(name="create", description="[Staff] Create a new land")
    @app_commands.describe(name="Name for the land", owner="Discord member owning this land", chunks="Starting chunk count (default 0)")
    @app_commands.guild_only()
    async def land_create(self, interaction: discord.Interaction, name: str, owner: discord.Member, chunks: int = 0):
        await interaction.response.defer()

        if not await _is_staff(interaction):
            return await interaction.followup.send("🔒 Staff only.", ephemeral=True)

        if chunks < 0:
            return await interaction.followup.send("❌ Chunks cannot be negative.", ephemeral=True)

        existing = await db.get_land_by_owner(interaction.guild_id, owner.id)
        if existing:
            return await interaction.followup.send(
                f"❌ {owner.mention} already owns **{existing['name']}** on this server.", ephemeral=True
            )
        name_check = await db.get_land_by_name(interaction.guild_id, name)
        if name_check:
            return await interaction.followup.send(
                f"❌ A land named **{name}** already exists on this server.", ephemeral=True
            )

        land_id = await db.create_land(interaction.guild_id, name, owner.id, chunks)
        embed = discord.Embed(title="🏰 Land Created", colour=discord.Colour.green())
        embed.add_field(name="Name", value=name, inline=True)
        embed.add_field(name="Owner", value=owner.mention, inline=True)
        embed.add_field(name="Chunks", value=str(chunks), inline=True)
        embed.set_footer(text=f"Land ID: {land_id}")
        await interaction.followup.send(embed=embed)

    @app_commands.command(name="info", description="View info about a land")
    @app_commands.describe(name="Land name (leave empty for yours)")
    @app_commands.guild_only()
    async def land_info(self, interaction: discord.Interaction, name: str = None):
        await interaction.response.defer()

        land = await db.get_land_by_name(interaction.guild_id, name) if name else await db.get_land_for_user(interaction.guild_id, interaction.user.id)
        if not land:
            return await interaction.followup.send("❌ Land not found on this server.", ephemeral=True)

        member_count = await db.get_member_count(land["id"])
        members_list = await db.get_members(land["id"])
        full_cost = await db.get_full_block_cost(interaction.guild_id)
        price = models.block_price(land["chunks"], full_cost)
        tier = models.price_tier_label(land["chunks"], full_cost)
        res_price = models.reserve_price(land["chunks"], full_cost)

        pending = await db.get_pending_request(land["id"])
        if pending:
            days = models.days_in_queue(pending["requested_at"])
            score = models.queue_score(land["chunks"], member_count, days,
                                       pending["purpose"] == "vault", pending["purpose"] == "builds")
            queue_info = f"{pending['purpose'].title()} • Score: {score} • {days}d waiting • {pending['status'].title()}"
        else:
            queue_info = "No pending request"

        weekly = await db.get_normal_purchase_this_week(land["id"])
        weekly_status = "✅ Available" if not weekly else "🔒 Used this week"

        member_mentions = [f"<@{uid}>" for uid in members_list]

        embed = discord.Embed(title=f"🏰 {land['name']}", colour=discord.Colour.teal())
        embed.add_field(name="👑 Owner", value=f"<@{land['owner_id']}>", inline=True)
        embed.add_field(name="📊 Chunks", value=str(land["chunks"]), inline=True)
        embed.add_field(name="👥 Members", value=str(member_count), inline=True)
        embed.add_field(name="💰 Price Tier", value=tier, inline=True)
        embed.add_field(name="🔒 Reserve Price", value=f"{res_price} ems" if res_price else "Free", inline=True)
        embed.add_field(name="🛒 Weekly Purchase", value=weekly_status, inline=True)
        embed.add_field(name="📋 Queue", value=queue_info, inline=False)
        if member_mentions:
            embed.add_field(name="Members", value=", ".join(member_mentions), inline=False)
        await interaction.followup.send(embed=embed)

    @app_commands.command(name="list", description="List all registered lands on this server")
    @app_commands.guild_only()
    async def land_list(self, interaction: discord.Interaction):
        await interaction.response.defer()

        lands = await db.get_all_lands(interaction.guild_id)
        if not lands:
            embed = discord.Embed(
                title="🏰 Registered Lands",
                description="No lands registered on this server yet.",
                colour=discord.Colour.light_grey(),
            )
            return await interaction.followup.send(embed=embed)

        embed = discord.Embed(
            title="🏰 Registered Lands",
            description=f"Total Lands: **{len(lands)}**",
            colour=discord.Colour.teal(),
        )

        for land in lands[:25]:  # Discord embed limit
            member_count = await db.get_member_count(land["id"])
            embed.add_field(
                name=f"🏰 {land['name']}",
                value=(
                    f"👑 **Owner:** <@{land['owner_id']}>\n"
                    f"📊 **Chunks:** {land['chunks']}  •  👥 **Members:** {member_count}"
                ),
                inline=False,
            )

        if len(lands) > 25:
            embed.set_footer(text=f"Showing top 25 of {len(lands)} lands.")

        await interaction.followup.send(embed=embed)

    @app_commands.command(name="member_add", description="Add a member to a land (Staff can specify land_name)")
    @app_commands.describe(user="The user to add", land_name="Target land name (Staff only; default: your land)")
    @app_commands.guild_only()
    async def member_add(self, interaction: discord.Interaction, user: discord.Member, land_name: str = None):
        await interaction.response.defer()

        if land_name:
            if not await _is_staff(interaction):
                return await interaction.followup.send("🔒 Only staff can specify a land_name for other lands.", ephemeral=True)
            land = await db.get_land_by_name(interaction.guild_id, land_name)
            if not land:
                return await interaction.followup.send(f"❌ Land **{land_name}** not found on this server.", ephemeral=True)
        else:
            land = await db.get_land_by_owner(interaction.guild_id, interaction.user.id)
            if not land:
                return await interaction.followup.send("❌ You must be a land owner on this server (or specify land_name if Staff).", ephemeral=True)

        if user.id == land["owner_id"]:
            return await interaction.followup.send(f"❌ {user.mention} is already the owner of **{land['name']}**.", ephemeral=True)

        existing = await db.get_land_for_user(interaction.guild_id, user.id)
        if existing:
            return await interaction.followup.send(
                f"❌ {user.mention} already belongs to **{existing['name']}** on this server.", ephemeral=True)

        await db.add_member(land["id"], user.id)
        count = await db.get_member_count(land["id"])
        embed = discord.Embed(title="👥 Member Added", colour=discord.Colour.green(),
                              description=f"{user.mention} joined **{land['name']}**.")
        embed.add_field(name="Total Members", value=str(count))
        embed.set_footer(text="+2 queue score per member")
        await interaction.followup.send(embed=embed)

    @app_commands.command(name="member_remove", description="Remove a member from a land (Staff can specify land_name)")
    @app_commands.describe(user="The user to remove", land_name="Target land name (Staff only; default: your land)")
    @app_commands.guild_only()
    async def member_remove(self, interaction: discord.Interaction, user: discord.Member, land_name: str = None):
        await interaction.response.defer()

        if land_name:
            if not await _is_staff(interaction):
                return await interaction.followup.send("🔒 Only staff can specify a land_name for other lands.", ephemeral=True)
            land = await db.get_land_by_name(interaction.guild_id, land_name)
            if not land:
                return await interaction.followup.send(f"❌ Land **{land_name}** not found on this server.", ephemeral=True)
        else:
            land = await db.get_land_by_owner(interaction.guild_id, interaction.user.id)
            if not land:
                return await interaction.followup.send("❌ You must be a land owner on this server (or specify land_name if Staff).", ephemeral=True)

        removed = await db.remove_member(land["id"], user.id)
        if not removed:
            return await interaction.followup.send(
                f"❌ {user.mention} is not in **{land['name']}**.", ephemeral=True)
        count = await db.get_member_count(land["id"])
        embed = discord.Embed(title="👥 Member Removed", colour=discord.Colour.orange(),
                              description=f"{user.mention} removed from **{land['name']}**.")
        embed.add_field(name="Members Remaining", value=str(count))
        await interaction.followup.send(embed=embed)

    @app_commands.command(name="set_chunks", description="[Staff] Set a land's chunk count manually")
    @app_commands.describe(land_name="Name of the land", chunks="New chunk count")
    @app_commands.guild_only()
    async def set_chunks(self, interaction: discord.Interaction, land_name: str, chunks: int):
        await interaction.response.defer()

        if not await _is_staff(interaction):
            return await interaction.followup.send("🔒 Staff only.", ephemeral=True)

        if chunks < 0:
            return await interaction.followup.send("❌ Chunks cannot be negative.", ephemeral=True)

        land = await db.get_land_by_name(interaction.guild_id, land_name)
        if not land:
            return await interaction.followup.send(f"❌ Land **{land_name}** not found on this server.", ephemeral=True)

        old_chunks = land["chunks"]
        await db.update_land_chunks(land["id"], chunks)

        embed = discord.Embed(title="⚙️ Land Chunks Updated", colour=discord.Colour.orange())
        embed.add_field(name="Land", value=land["name"], inline=True)
        embed.add_field(name="Old Chunks", value=str(old_chunks), inline=True)
        embed.add_field(name="New Chunks", value=str(chunks), inline=True)
        embed.set_footer(text=f"Updated by {interaction.user.display_name}")
        await interaction.followup.send(embed=embed)

    @app_commands.command(name="add_chunks", description="[Staff] Increase or decrease a land's chunk count by X")
    @app_commands.describe(land_name="Name of the land", amount="Amount to add (use negative number to decrease, e.g. 3 or -2)")
    @app_commands.guild_only()
    async def add_chunks(self, interaction: discord.Interaction, land_name: str, amount: int):
        await interaction.response.defer()

        if not await _is_staff(interaction):
            return await interaction.followup.send("🔒 Staff only.", ephemeral=True)

        land = await db.get_land_by_name(interaction.guild_id, land_name)
        if not land:
            return await interaction.followup.send(f"❌ Land **{land_name}** not found on this server.", ephemeral=True)

        old_chunks = land["chunks"]
        new_chunks = max(0, old_chunks + amount)
        await db.update_land_chunks(land["id"], new_chunks)

        change_str = f"+{amount}" if amount > 0 else str(amount)
        embed = discord.Embed(
            title="⚙️ Land Chunks Adjusted",
            colour=discord.Colour.blue() if amount > 0 else discord.Colour.orange()
        )
        embed.add_field(name="Land", value=land["name"], inline=True)
        embed.add_field(name="Adjustment", value=change_str, inline=True)
        embed.add_field(name="Old Chunks", value=str(old_chunks), inline=True)
        embed.add_field(name="New Chunks", value=str(new_chunks), inline=True)
        embed.set_footer(text=f"Adjusted by {interaction.user.display_name}")
        await interaction.followup.send(embed=embed)

    @app_commands.command(name="delete", description="[Staff] Delete a registered land")
    @app_commands.describe(land_name="Name of the land to delete")
    @app_commands.guild_only()
    async def land_delete(self, interaction: discord.Interaction, land_name: str):
        await interaction.response.defer()

        if not await _is_staff(interaction):
            return await interaction.followup.send("🔒 Staff only.", ephemeral=True)

        land = await db.get_land_by_name(interaction.guild_id, land_name)
        if not land:
            return await interaction.followup.send(f"❌ Land **{land_name}** not found on this server.", ephemeral=True)

        await db.delete_land(land["id"])

        embed = discord.Embed(title="🗑️ Land Deleted", colour=discord.Colour.red())
        embed.add_field(name="Name", value=land["name"], inline=True)
        embed.add_field(name="Owner", value=f"<@{land['owner_id']}>", inline=True)
        embed.add_field(name="Chunks Had", value=str(land["chunks"]), inline=True)
        embed.set_footer(text=f"Deleted by {interaction.user.display_name}")
        await interaction.followup.send(embed=embed)


async def setup(bot: commands.Bot):
    await bot.add_cog(LandCog(bot))
