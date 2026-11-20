"""
Distribution cog — Automated weekly block distribution.

Runs a background loop every 5 minutes to check if it's time for
the weekly distribution on each connected Discord server (guild).
When triggered per guild:
  1. Expire old approved-but-unpurchased requests (→ chunks back to reserve)
  2. Generate N new blocks
  3. Top up reserve if below protected minimum
  4. Process the queue (highest score first, partial fills allowed)
  5. Unallocated blocks enter the leftover pool
"""

import logging
from datetime import datetime, timezone

import discord
from discord.ext import commands, tasks

import database as db
import banner_account
import weekly_claims as weekly
import bank_store as bank
from cogs.weekly_offers import notify_offers

log = logging.getLogger("claims-bot")


class DistributionCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.accounting_weeks = {}

    async def cog_load(self):
        self.distribution_loop.start()

    async def cog_unload(self):
        self.distribution_loop.cancel()

    # ── Background loop ───────────────────────────────────────────────

    @tasks.loop(minutes=5)
    async def distribution_loop(self):
        for guild in self.bot.guilds:
            try:
                week = banner_account.week_of(datetime.now(timezone.utc))
                if self.accounting_weeks.get(guild.id) != week:
                    await banner_account.weekly_production(guild.id)
                    self.accounting_weeks[guild.id] = week
                result = await weekly.distribute(guild.id, bank.now())
                if result:
                    await self._notify_expired(guild, result["expired"])
                    await self._post_summary(guild, result["generated"], result["carried"],
                        sum(r["chunks_requested"] for r in result["expired"]), result["offered"],
                        result["partial"], result["leftover"])
                else:
                    await self._expire_old_approvals(guild)
                await notify_offers(self.bot, guild)
            except Exception:
                log.exception("Weekly distribution failed for guild %s", guild.id)

    @distribution_loop.before_loop
    async def before_distribution_loop(self):
        await self.bot.wait_until_ready()

    async def _expire_old_approvals(self, guild):
        if not await db.get_expired_approved_requests(guild.id):
            return 0
        async with bank.write(guild.id) as conn:
            expired = await weekly.expire(conn, guild.id, bank.now())
        await self._notify_expired(guild, expired)
        return sum(row["chunks_requested"] for row in expired)

    async def _notify_expired(self, guild, rows):
        for row in rows:
            await self._notify_owner(row["owner_id"],
                f"Your allocation of {row['chunks_requested']} banners for **{row['land_name']}** "
                f"on **{guild.name}** expired and returned to reserve. You can enter the next queue with /claim request.")

    async def _notify_owner(self, owner_id: int, message: str):
        try:
            user = await self.bot.fetch_user(owner_id)
            if user:
                await user.send(message)
        except (discord.Forbidden, discord.HTTPException):
            log.warning(f"Could not DM user {owner_id}")

    async def _post_summary(self, guild: discord.Guild, generated: int, old_leftovers: int, expired: int, approved: int, partial: int, to_leftover: int):
        reserve = await db.get_reserve(guild.id)

        embed = discord.Embed(
            title="📦 Weekly Distribution Complete",
            colour=discord.Colour.gold(),
            timestamp=datetime.now(timezone.utc),
        )
        embed.add_field(name="🆕 Blocks Generated", value=str(generated), inline=True)
        embed.add_field(name="📨 Offered to settlements", value=f"{approved} block(s)", inline=True)
        embed.add_field(name="⚠️ Partial Fills", value=str(partial), inline=True)
        embed.add_field(name="⏰ Expired blocks", value=str(expired), inline=True)
        embed.add_field(name="🛒 Leftover Pool (Normal Price)", value=f"{to_leftover} block(s)", inline=True)
        embed.add_field(name="🔒 Reserve Total (1.5× Price)", value=f"{reserve['total_blocks']} block(s)", inline=True)
        if old_leftovers > 0:
            embed.add_field(name="🔄 Old Leftovers → Reserve", value=f"{old_leftovers} block(s)", inline=False)
        embed.set_footer(text=f"Automated weekly distribution for {guild.name}")

        for channel in guild.text_channels:
            if channel.permissions_for(guild.me).send_messages:
                try:
                    await channel.send(embed=embed)
                except discord.HTTPException:
                    continue
                break


async def setup(bot: commands.Bot):
    await bot.add_cog(DistributionCog(bot))
