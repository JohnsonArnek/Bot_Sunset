"""
Land cog — /land create, /land info, /land member_add, /land member_remove
"""

import discord
from discord import app_commands
from discord.ext import commands

import database as db
import models


async def _is_staff(interaction: discord.Interaction) -> bool:
    staff_role_id = await db.get_staff_role_id()
    if staff_role_id == 0:
        return interaction.user.id == interaction.guild.owner_id
    return any(r.id == staff_role_id for r in interaction.user.roles)


class LandCog(commands.GroupCog, name="land"):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        super().__init__()

    @app_commands.command(name="create", description="[Staff] Create a new land")
    @app_commands.describe(name="Name for the land", owner="Discord member owning this land", chunks="Starting chunk count (default 0)")
    async def land_create(self, interaction: discord.Interaction, name: str, owner: discord.Member, chunks: int = 0):
        if not await _is_staff(interaction):
            return await interaction.response.send_message("🔒 Staff only.", ephemeral=True)

        if chunks < 0:
            return await interaction.response.send_message("❌ Chunks cannot be negative.", ephemeral=True)

        existing = await db.get_land_by_owner(owner.id)
        if existing:
            return await interaction.response.send_message(
                f"❌ {owner.mention} already owns **{existing['name']}**.", ephemeral=True
            )
        name_check = await db.get_land_by_name(name)
        if name_check:
            return await interaction.response.send_message(
                f"❌ A land named **{name}** already exists.", ephemeral=True
            )

        land_id = await db.create_land(name, owner.id, chunks)
        embed = discord.Embed(title="🏰 Land Created", colour=discord.Colour.green())
        embed.add_field(name="Name", value=name, inline=True)
        embed.add_field(name="Owner", value=owner.mention, inline=True)
        embed.add_field(name="Chunks", value=str(chunks), inline=True)
        embed.set_footer(text=f"Land ID: {land_id}")
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="info", description="View info about a land")
    @app_commands.describe(name="Land name (leave empty for yours)")
    async def land_info(self, interaction: discord.Interaction, name: str = None):
        land = await db.get_land_by_name(name) if name else await db.get_land_for_user(interaction.user.id)
        if not land:
            return await interaction.response.send_message("❌ Land not found.", ephemeral=True)

        member_count = await db.get_member_count(land["id"])
        members_list = await db.get_members(land["id"])
        full_cost = await db.get_full_block_cost()
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
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="member_add", description="Add a member to your land")
    @app_commands.describe(user="The user to add")
    async def member_add(self, interaction: discord.Interaction, user: discord.Member):
        land = await db.get_land_by_owner(interaction.user.id)
        if not land:
            return await interaction.response.send_message("❌ You must be a land owner.", ephemeral=True)
        if user.id == interaction.user.id:
            return await interaction.response.send_message("❌ You're already the owner.", ephemeral=True)
        existing = await db.get_land_for_user(user.id)
        if existing:
            return await interaction.response.send_message(
                f"❌ {user.mention} already belongs to **{existing['name']}**.", ephemeral=True)

        await db.add_member(land["id"], user.id)
        count = await db.get_member_count(land["id"])
        embed = discord.Embed(title="👥 Member Added", colour=discord.Colour.green(),
                              description=f"{user.mention} joined **{land['name']}**.")
        embed.add_field(name="Total Members", value=str(count))
        embed.set_footer(text="+2 queue score per member")
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="member_remove", description="Remove a member from your land")
    @app_commands.describe(user="The user to remove")
    async def member_remove(self, interaction: discord.Interaction, user: discord.Member):
        land = await db.get_land_by_owner(interaction.user.id)
        if not land:
            return await interaction.response.send_message("❌ You must be a land owner.", ephemeral=True)
        removed = await db.remove_member(land["id"], user.id)
        if not removed:
            return await interaction.response.send_message(
                f"❌ {user.mention} is not in **{land['name']}**.", ephemeral=True)
        count = await db.get_member_count(land["id"])
        embed = discord.Embed(title="👥 Member Removed", colour=discord.Colour.orange(),
                              description=f"{user.mention} removed from **{land['name']}**.")
        embed.add_field(name="Members Remaining", value=str(count))
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="set_chunks", description="[Staff] Set a land's chunk count manually")
    @app_commands.describe(land_name="Name of the land", chunks="New chunk count")
    async def set_chunks(self, interaction: discord.Interaction, land_name: str, chunks: int):
        if not await _is_staff(interaction):
            return await interaction.response.send_message("🔒 Staff only.", ephemeral=True)

        if chunks < 0:
            return await interaction.response.send_message("❌ Chunks cannot be negative.", ephemeral=True)

        land = await db.get_land_by_name(land_name)
        if not land:
            return await interaction.response.send_message(f"❌ Land **{land_name}** not found.", ephemeral=True)

        old_chunks = land["chunks"]
        await db.update_land_chunks(land["id"], chunks)

        embed = discord.Embed(title="⚙️ Land Chunks Updated", colour=discord.Colour.orange())
        embed.add_field(name="Land", value=land["name"], inline=True)
        embed.add_field(name="Old Chunks", value=str(old_chunks), inline=True)
        embed.add_field(name="New Chunks", value=str(chunks), inline=True)
        embed.set_footer(text=f"Updated by {interaction.user.display_name}")
        await interaction.response.send_message(embed=embed)



async def setup(bot: commands.Bot):
    await bot.add_cog(LandCog(bot))

