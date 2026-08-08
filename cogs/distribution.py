"""
Distribution cog — Automated weekly block distribution.

Runs a background loop every 5 minutes to check if it's time for
the weekly distribution on each connected Discord server (guild).
When triggered per guild:
  1. Expire old approved-but-unpurchased requests (→ chunks back to reserve)
  2. Generate N new blocks
  3. Top up reserve if below protected minimum
  4. Process the queue (highest score first, partial fills allowed)
  5. Leftover blocks → reserve
"""

import logging
from datetime import datetime, timezone

import discord
from discord.ext import commands, tasks

import database as db
import models

log = logging.getLogger("claims-bot")


class DistributionCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def cog_load(self):
        self.distribution_loop.start()

    async def cog_unload(self):
        self.distribution_loop.cancel()

    # ── Background loop ───────────────────────────────────────────────

    @tasks.loop(minutes=5)
    async def distribution_loop(self):
        try:
            for guild in self.bot.guilds:
                if await self._should_run(guild.id):
                    await self._run_distribution(guild)
        except Exception as e:
            log.error(f"Distribution loop error: {e}", exc_info=True)

    @distribution_loop.before_loop
    async def before_distribution_loop(self):
        await self.bot.wait_until_ready()

    # ── Timing check ──────────────────────────────────────────────────

    async def _should_run(self, guild_id: int) -> bool:
        """Check if we're past the configured distribution time for this guild and haven't run yet this week."""
        now = datetime.now(timezone.utc)

        dist_day = int(await db.get_config(guild_id, "distribution_day") or "0")
        dist_hour = int(await db.get_config(guild_id, "distribution_hour") or "0")
        last_run = await db.get_config(guild_id, "last_distribution") or ""

        if now.weekday() < dist_day:
            return False
        if now.weekday() == dist_day and now.hour < dist_hour:
            return False

        if last_run:
            try:
                last_dt = datetime.fromisoformat(last_run).replace(tzinfo=timezone.utc)
                days_since_dist = (now.weekday() - dist_day) % 7
                window_start = now.replace(hour=dist_hour, minute=0, second=0, microsecond=0)
                from datetime import timedelta
                window_start -= timedelta(days=days_since_dist)
                if last_dt >= window_start:
                    return False
            except (ValueError, TypeError):
                pass

        return True

    # ── Main distribution logic ───────────────────────────────────────

    async def _run_distribution(self, guild: discord.Guild):
        guild_id = guild.id
        log.info(f"=== Starting weekly distribution for guild: {guild.name} ({guild_id}) ===")

        # Step 0: Move unbought leftover blocks from previous week into the Reserve Pool!
        reserve = await db.get_reserve(guild_id)
        old_leftovers = reserve.get("leftover_blocks", 0)
        if old_leftovers > 0:
            await db.add_reserve_blocks(guild_id, old_leftovers)
            await db.update_leftover_blocks(guild_id, 0)
            log.info(f"[{guild.name}] Moved {old_leftovers} unbought leftover blocks to Reserve")

        # Step 1: Expire old approved requests (older than 7 days)
        expired_count = await self._expire_old_approvals(guild)

        # Step 2: Generate weekly blocks
        weekly_blocks = int(await db.get_config(guild_id, "weekly_blocks") or "7")
        remaining = weekly_blocks

        # Step 3: Handle reserve allocation mode (topup vs fixed)
        reserve_mode = await db.get_config(guild_id, "reserve_mode") or "topup"
        reserve_allocated = 0

        if reserve_mode.lower() == "fixed":
            fixed_amount = int(await db.get_config(guild_id, "weekly_reserve_blocks") or "2")
            allocation = min(fixed_amount, remaining)
            await db.add_reserve_blocks(guild_id, allocation)
            remaining -= allocation
            reserve_allocated = allocation
            log.info(f"[{guild.name}] Fixed reserve allocation: +{allocation} blocks")
        else:
            # "topup" mode
            protected_min = reserve["protected_min"]
            current_reserve = reserve["total_blocks"] + old_leftovers
            if current_reserve < protected_min:
                needed = protected_min - current_reserve
                top_up = min(needed, remaining)
                await db.add_reserve_blocks(guild_id, top_up)
                remaining -= top_up
                reserve_allocated = top_up
                log.info(f"[{guild.name}] Topped up reserve: +{top_up} blocks")

        # Step 4: Process the queue
        approved_count, partial_count = await self._process_queue(guild, remaining)
        remaining_after_queue = remaining - approved_count

        # Step 5: Deposit remaining unused generated blocks into Leftover Pool (NOT reserve)!
        if remaining_after_queue > 0:
            await db.add_leftover_blocks(guild_id, remaining_after_queue)
            log.info(f"[{guild.name}] Deposited {remaining_after_queue} blocks into Leftover Pool")

        # Mark this distribution as complete for this guild
        now = datetime.now(timezone.utc).isoformat()
        await db.set_config(guild_id, "last_distribution", now)

        log.info(
            f"=== [{guild.name}] Distribution complete: {weekly_blocks} generated, "
            f"{old_leftovers} old leftovers → reserve, {expired_count} expired, "
            f"{approved_count} approved blocks used, {partial_count} partial fills, "
            f"{remaining_after_queue} → leftover pool ==="
        )

        # Post summary to the guild
        await self._post_summary(guild, weekly_blocks, old_leftovers, expired_count, approved_count, partial_count, remaining_after_queue)

    # ── Expire old approvals ──────────────────────────────────────────

    async def _expire_old_approvals(self, guild: discord.Guild) -> int:
        """Expire approved requests older than 7 days for this guild."""
        expired_requests = await db.get_expired_approved_requests(guild.id, days=7)
        total_returned = 0

        for req in expired_requests:
            await db.update_request_status(req["id"], "expired")
            await db.add_reserve_blocks(guild.id, req["chunks_requested"])
            total_returned += req["chunks_requested"]

            await self._notify_owner(
                req["owner_id"],
                f"⏰ Your approved claim for **{req['land_name']}** on **{guild.name}** "
                f"({req['chunks_requested']} chunk(s)) has **expired** because it wasn't "
                f"purchased within 7 days. The chunks have been returned to the reserve.\n"
                f"You can submit a new `/claim request` to re-enter the queue."
            )
            log.info(f"[{guild.name}] Expired request #{req['id']} for {req['land_name']}")

        return total_returned

    # ── Process queue ─────────────────────────────────────────────────

    async def _process_queue(self, guild: discord.Guild, available: int) -> tuple[int, int]:
        """Auto-approve queue entries by score for this guild."""
        if available <= 0:
            return 0, 0

        requests = await db.get_all_pending_requests(guild.id)
        if not requests:
            return 0, 0

        full_cost = await db.get_full_block_cost(guild.id)

        scored = []
        for req in requests:
            member_count = await db.get_member_count(req["land_id"])
            days = models.days_in_queue(req["requested_at"])
            score = models.queue_score(
                chunks=req["chunks"],
                members=member_count,
                days_waiting=days,
                vault=(req["purpose"] == "vault"),
                builds=(req["purpose"] == "builds"),
            )
            scored.append((score, req, member_count))

        scored.sort(key=lambda x: x[0], reverse=True)

        total_used = 0
        partial_count = 0

        for score, req, member_count in scored:
            if available <= 0:
                break

            requested = req["chunks_requested"]

            if requested <= available:
                await db.update_request_status(req["id"], "approved")
                available -= requested
                total_used += requested

                await self._notify_owner(
                    req["owner_id"],
                    f"✅ Your claim request for **{req['land_name']}** on **{guild.name}** has been "
                    f"**approved** for **{requested}** chunk(s)!\n"
                    f"Use `/claim buy` within 7 days to complete the purchase."
                )
            else:
                original_requested = requested
                await db.update_request_chunks(req["id"], available)
                await db.update_request_status(req["id"], "approved")
                total_used += available
                partial_count += 1

                await self._notify_owner(
                    req["owner_id"],
                    f"⚠️ Your claim request for **{req['land_name']}** on **{guild.name}** has been "
                    f"**partially approved** — **{available}** of {original_requested} chunk(s).\n"
                    f"Use `/claim buy` within 7 days to claim the approved chunks."
                )
                available = 0

        return total_used, partial_count

    # ── Notifications ─────────────────────────────────────────────────

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
        embed.add_field(name="✅ Queue Approved", value=f"{approved} block(s)", inline=True)
        embed.add_field(name="⚠️ Partial Fills", value=str(partial), inline=True)
        embed.add_field(name="⏰ Expired Approvals", value=str(expired), inline=True)
        embed.add_field(name="🛒 Leftover Pool (Normal Price)", value=f"{reserve.get('leftover_blocks', 0)} block(s)", inline=True)
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
