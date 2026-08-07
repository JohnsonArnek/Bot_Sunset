"""
Distribution cog — Automated weekly block distribution.

Runs a background loop every 5 minutes to check if it's time for
the weekly distribution. When triggered:
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
            if await self._should_run():
                await self._run_distribution()
        except Exception as e:
            log.error(f"Distribution loop error: {e}", exc_info=True)

    @distribution_loop.before_loop
    async def before_distribution_loop(self):
        await self.bot.wait_until_ready()

    # ── Timing check ──────────────────────────────────────────────────

    async def _should_run(self) -> bool:
        """Check if we're past the configured distribution time and haven't run yet this week."""
        now = datetime.now(timezone.utc)

        dist_day = int(await db.get_config("distribution_day") or "0")
        dist_hour = int(await db.get_config("distribution_hour") or "0")
        last_run = await db.get_config("last_distribution") or ""

        # Are we on or past the distribution day/hour this week?
        if now.weekday() < dist_day:
            return False
        if now.weekday() == dist_day and now.hour < dist_hour:
            return False

        # Have we already run this week?
        if last_run:
            try:
                last_dt = datetime.fromisoformat(last_run).replace(tzinfo=timezone.utc)
                # Calculate the start of the current distribution window
                days_since_dist = (now.weekday() - dist_day) % 7
                window_start = now.replace(hour=dist_hour, minute=0, second=0, microsecond=0)
                from datetime import timedelta
                window_start -= timedelta(days=days_since_dist)
                if last_dt >= window_start:
                    return False  # Already ran this window
            except (ValueError, TypeError):
                pass  # Invalid timestamp, run anyway

        return True

    # ── Main distribution logic ───────────────────────────────────────

    async def _run_distribution(self):
        log.info("=== Starting weekly distribution ===")

        # Step 0: Expire old approved requests (older than 7 days)
        expired_count = await self._expire_old_approvals()

        # Step 1: Generate blocks
        weekly_blocks = int(await db.get_config("weekly_blocks") or "7")
        remaining = weekly_blocks
        log.info(f"Generated {weekly_blocks} new blocks")

        # Step 2: Top up reserve if below protected minimum
        reserve = await db.get_reserve()
        protected_min = reserve["protected_min"]
        current_reserve = reserve["total_blocks"]

        if current_reserve < protected_min:
            needed = protected_min - current_reserve
            top_up = min(needed, remaining)
            await db.add_reserve_blocks(top_up)
            remaining -= top_up
            log.info(f"Topped up reserve: +{top_up} blocks (was {current_reserve}, now {current_reserve + top_up})")

        # Step 3: Process the queue
        approved_count, partial_count = await self._process_queue(remaining)
        # Recalculate remaining after queue processing
        remaining_after_queue = remaining - approved_count

        # Step 4: Leftover → reserve
        if remaining_after_queue > 0:
            await db.add_reserve_blocks(remaining_after_queue)
            log.info(f"Added {remaining_after_queue} leftover blocks to reserve")

        # Mark this distribution as complete
        now = datetime.now(timezone.utc).isoformat()
        await db.set_config("last_distribution", now)

        log.info(
            f"=== Distribution complete: {weekly_blocks} generated, "
            f"{expired_count} expired, {approved_count} approved blocks used, "
            f"{partial_count} partial fills, {remaining_after_queue} → reserve ==="
        )

        # Post summary to all guilds (first text channel the bot can write to)
        await self._post_summary(weekly_blocks, expired_count, approved_count, partial_count, remaining_after_queue)

    # ── Expire old approvals ──────────────────────────────────────────

    async def _expire_old_approvals(self) -> int:
        """Expire approved requests older than 7 days. Return chunks to reserve."""
        expired_requests = await db.get_expired_approved_requests(days=7)
        total_returned = 0

        for req in expired_requests:
            await db.update_request_status(req["id"], "expired")
            await db.add_reserve_blocks(req["chunks_requested"])
            total_returned += req["chunks_requested"]

            # DM the land owner
            await self._notify_owner(
                req["owner_id"],
                f"⏰ Your approved claim for **{req['land_name']}** "
                f"({req['chunks_requested']} chunk(s)) has **expired** because it wasn't "
                f"purchased within 7 days. The chunks have been returned to the reserve.\n"
                f"You can submit a new `/claim request` to re-enter the queue."
            )
            log.info(f"Expired request #{req['id']} for {req['land_name']} ({req['chunks_requested']} chunks)")

        return total_returned

    # ── Process queue ─────────────────────────────────────────────────

    async def _process_queue(self, available: int) -> tuple[int, int]:
        """
        Auto-approve queue entries by score. Returns (total_blocks_used, partial_count).
        Supports partial fills.
        """
        if available <= 0:
            return 0, 0

        requests = await db.get_all_pending_requests()
        if not requests:
            return 0, 0

        full_cost = await db.get_full_block_cost()

        # Score and sort
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
                # Full approval
                await db.update_request_status(req["id"], "approved")
                available -= requested
                total_used += requested

                await self._notify_owner(
                    req["owner_id"],
                    f"✅ Your claim request for **{req['land_name']}** has been "
                    f"**approved** for **{requested}** chunk(s)!\n"
                    f"Use `/claim buy` within 7 days to complete the purchase."
                )
                log.info(f"Approved request #{req['id']} for {req['land_name']} ({requested} chunks)")
            else:
                # Partial fill
                original_requested = requested
                await db.update_request_chunks(req["id"], available)
                await db.update_request_status(req["id"], "approved")
                total_used += available
                partial_count += 1

                await self._notify_owner(
                    req["owner_id"],
                    f"⚠️ Your claim request for **{req['land_name']}** has been "
                    f"**partially approved** — **{available}** of {original_requested} chunk(s).\n"
                    f"Use `/claim buy` within 7 days to claim the approved chunks.\n"
                    f"You can submit a new `/claim request` for the remaining "
                    f"{original_requested - available} chunk(s) next week."
                )
                log.info(
                    f"Partially approved request #{req['id']} for {req['land_name']} "
                    f"({available}/{original_requested} chunks)"
                )
                available = 0

        return total_used, partial_count

    # ── Notifications ─────────────────────────────────────────────────

    async def _notify_owner(self, owner_id: int, message: str):
        """Send a DM to a land owner. Fails silently if DMs are disabled."""
        try:
            user = await self.bot.fetch_user(owner_id)
            if user:
                await user.send(message)
        except (discord.Forbidden, discord.HTTPException):
            log.warning(f"Could not DM user {owner_id}")

    async def _post_summary(self, generated: int, expired: int, approved: int, partial: int, to_reserve: int):
        """Post a distribution summary embed to each guild."""
        reserve = await db.get_reserve()

        embed = discord.Embed(
            title="📦 Weekly Distribution Complete",
            colour=discord.Colour.gold(),
            timestamp=datetime.now(timezone.utc),
        )
        embed.add_field(name="🆕 Blocks Generated", value=str(generated), inline=True)
        embed.add_field(name="✅ Queue Approved", value=f"{approved} block(s)", inline=True)
        embed.add_field(name="⚠️ Partial Fills", value=str(partial), inline=True)
        embed.add_field(name="⏰ Expired Approvals", value=str(expired), inline=True)
        embed.add_field(name="📥 Added to Reserve", value=str(to_reserve), inline=True)
        embed.add_field(name="🔒 Reserve Total", value=str(reserve["total_blocks"]), inline=True)
        embed.set_footer(text="Automated weekly distribution")

        for guild in self.bot.guilds:
            # Find the first text channel the bot can send to
            for channel in guild.text_channels:
                if channel.permissions_for(guild.me).send_messages:
                    try:
                        await channel.send(embed=embed)
                    except discord.HTTPException:
                        continue
                    break  # Only post once per guild


async def setup(bot: commands.Bot):
    await bot.add_cog(DistributionCog(bot))
