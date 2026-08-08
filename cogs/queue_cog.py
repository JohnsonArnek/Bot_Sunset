"""
Queue cog — /queue
Displays all pending claim requests ordered by computed score for the current server.
"""

import discord
from discord import app_commands
from discord.ext import commands

import database as db
import models


class QueueCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(name="queue", description="View the current claim request queue")
    @app_commands.guild_only()
    async def queue(self, interaction: discord.Interaction):
        await interaction.response.defer()

        requests = await db.get_all_pending_requests(interaction.guild_id)

        if not requests:
            embed = discord.Embed(
                title="📭 Claim Queue",
                description="The queue is empty — no pending requests on this server.",
                colour=discord.Colour.light_grey(),
            )
            return await interaction.followup.send(embed=embed)

        full_cost = await db.get_full_block_cost(interaction.guild_id)

        # Build scored entries
        entries = []
        for req in requests:
            member_count = await db.get_member_count(req["land_id"])
            days = models.days_in_queue(req["requested_at"])
            is_vault = req["purpose"] == "vault"
            is_builds = req["purpose"] == "builds"
            score = models.queue_score(
                chunks=req["chunks"],
                members=member_count,
                days_waiting=days,
                vault=is_vault,
                builds=is_builds,
            )
            price = models.block_price(req["chunks"], full_cost)
            entries.append(
                {
                    "land_name": req["land_name"],
                    "score": score,
                    "days": days,
                    "members": member_count,
                    "chunks": req["chunks"],
                    "purpose": req["purpose"],
                    "chunks_requested": req["chunks_requested"],
                    "price": price,
                    "owner_id": req["owner_id"],
                }
            )

        # Sort by score descending
        entries.sort(key=lambda e: e["score"], reverse=True)

        # Build embed
        embed = discord.Embed(
            title="📋 Claim Queue",
            description="Ordered by priority score (highest first).",
            colour=discord.Colour.blue(),
        )

        for i, entry in enumerate(entries[:25], start=1):  # Discord embed limit
            purpose_emoji = {"vault": "🏦", "builds": "🏗️", "other": "📦"}.get(
                entry["purpose"], "📦"
            )
            price_str = f"{entry['price']} ems" if entry["price"] > 0 else "Free"

            embed.add_field(
                name=f"{'👑 ' if i == 1 else ''}{i}. {entry['land_name']}",
                value=(
                    f"**Score:** {entry['score']}  •  "
                    f"**Purpose:** {purpose_emoji} {entry['purpose'].title()}\n"
                    f"📊 {entry['chunks']} chunks  •  "
                    f"👥 {entry['members']} members  •  "
                    f"⏳ {entry['days']}d waiting\n"
                    f"💰 {price_str}  •  "
                    f"Requesting {entry['chunks_requested']} block(s)"
                ),
                inline=False,
            )

        total = len(entries)
        if total > 25:
            embed.set_footer(text=f"Showing top 25 of {total} requests.")
        else:
            embed.set_footer(text=f"{total} request(s) in queue.")

        await interaction.followup.send(embed=embed)


async def setup(bot: commands.Bot):
    await bot.add_cog(QueueCog(bot))
