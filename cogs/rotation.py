"""
Rotation cog — Turn-based daily claim block rotation.

Servers with `claim_mode = "rotation"` use this instead of the weekly
distribution system.  Every day at the configured hour, the bot offers
a claim block to the settlement at the top of the rotation.  The leader
reacts ✅ to accept or ❌ to pass.  If they pass (or time out), the
offer cascades to the next settlement in the list.
"""

import asyncio
import logging
from datetime import datetime, timezone

import discord
from discord import app_commands
from discord.ext import commands, tasks

import database as db
import models

log = logging.getLogger("claims-bot")

# Emoji constants
ACCEPT_EMOJI = "✅"
PASS_EMOJI = "❌"


# ── Helpers ───────────────────────────────────────────────────────────

async def _is_staff(interaction: discord.Interaction) -> bool:
    staff_role_id = await db.get_staff_role_id(interaction.guild_id)
    if staff_role_id and any(r.id == staff_role_id for r in interaction.user.roles):
        return True
    if interaction.user.guild_permissions.administrator:
        return True
    return False


async def _get_rotation_channel(guild: discord.Guild) -> discord.TextChannel | None:
    """Get the channel for rotation messages."""
    channel_id = await db.get_config(guild.id, "rotation_channel_id")
    if channel_id and channel_id != "0":
        ch = guild.get_channel(int(channel_id))
        if ch:
            return ch
    # Fallback: first text channel the bot can send to
    for ch in guild.text_channels:
        if ch.permissions_for(guild.me).send_messages:
            return ch
    return None


class RotationCog(commands.GroupCog, name="rotation"):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        super().__init__()
        # Track active cascade tasks per guild so we don't run multiples
        self._cascade_tasks: dict[int, asyncio.Task] = {}
        self._skip_events: dict[int, asyncio.Event] = {}
        self._allowed_reactors: dict[int, set[int]] = {}

    async def cog_load(self):
        self.rotation_loop.start()

    async def cog_unload(self):
        self.rotation_loop.cancel()
        for t in self._cascade_tasks.values():
            t.cancel()

    # ── Background loop: check once per 5 min ─────────────────────────

    @tasks.loop(minutes=5)
    async def rotation_loop(self):
        now = datetime.now(timezone.utc)
        today_str = now.strftime("%Y-%m-%d")

        for guild in self.bot.guilds:
            try:
                mode = await db.get_config(guild.id, "claim_mode")
                if mode != "rotation":
                    continue

                # Is rotation paused?
                paused = await db.get_config(guild.id, "rotation_paused")
                if paused == "true":
                    continue

                # Already ran today?
                last = await db.get_last_rotation_date(guild.id)
                if last == today_str:
                    continue

                # Is it time yet?
                rotation_hour = int(await db.get_config(guild.id, "rotation_hour") or "12")
                if now.hour < rotation_hour:
                    continue

                # Already a pending offer?
                pending = await db.get_pending_rotation_offer(guild.id)
                if pending:
                    continue

                await self._start_rotation(guild)

            except Exception as e:
                log.error(f"Rotation loop error for {guild.name}: {e}", exc_info=True)

    @rotation_loop.before_loop
    async def before_rotation_loop(self):
        await self.bot.wait_until_ready()

    # ── Core rotation logic ───────────────────────────────────────────

    async def _start_rotation(self, guild: discord.Guild, start_position: int = 0):
        """Begin today's rotation from a given position in the order."""
        order = await db.get_rotation_order(guild.id)
        if not order:
            log.info(f"[{guild.name}] No lands in rotation, skipping.")
            return

        channel = await _get_rotation_channel(guild)
        if not channel:
            log.warning(f"[{guild.name}] No writable channel for rotation.")
            return

        # Start cascade from the given position
        task = asyncio.create_task(self._cascade_offers(guild, channel, order, start_position))
        self._cascade_tasks[guild.id] = task

    async def _cascade_offers(
        self,
        guild: discord.Guild,
        channel: discord.TextChannel,
        order: list[dict],
        start_idx: int,
    ):
        """Offer the block to each land in order until someone accepts or all pass."""
        full_block_cost = await db.get_full_block_cost(guild.id)
        timeout_minutes = int(await db.get_config(guild.id, "rotation_timeout_minutes") or "720")
        daily_blocks = int(await db.get_config(guild.id, "rotation_daily_blocks") or "1")

        skip_event = asyncio.Event()
        self._skip_events[guild.id] = skip_event

        try:
            for i in range(start_idx, len(order)):
                skip_event.clear()
                entry = order[i]
                land_id = entry["land_id"]

                # Fetch fresh land data dynamically to get updated owner/chunks
                fresh_land = await db.get_land_by_id(land_id)
                land_name = fresh_land["name"] if fresh_land else entry["land_name"]
                owner_id = fresh_land["owner_id"] if fresh_land else entry["owner_id"]
                officer_ids = await db.get_officers(land_id)
                chunks = fresh_land["chunks"] if fresh_land else entry["chunks"]
                price = models.block_price(chunks, full_block_cost)
                tier_label = models.price_tier_label(chunks, full_block_cost)

                # Set live allowed reactors for this guild
                self._allowed_reactors[guild.id] = {owner_id, *officer_ids}

                # Mention owner + any officers
                pings = [f"<@{owner_id}>"] + [f"<@{uid}>" for uid in officer_ids]
                pings_content = " ".join(pings)

                # Build the offer embed
                embed = discord.Embed(
                    title="🔄 Daily Rotation — Claim Block Offer",
                    description=(
                        f"**{land_name}**, it's your turn!\n\n"
                        f"You have **{daily_blocks}** claim block(s) available today.\n"
                        f"Current chunks: **{chunks}** — Price: **{price} ems** ({tier_label})\n\n"
                        f"React {ACCEPT_EMOJI} to **accept** or {PASS_EMOJI} to **pass**.\n"
                        f"⏰ You have **{timeout_minutes} minutes** to respond."
                    ),
                    colour=discord.Colour.gold(),
                )
                embed.set_footer(text=f"Position #{i + 1} of {len(order)} in rotation")

                # Send the offer and ping the owner and officers
                msg = await channel.send(content=pings_content, embed=embed)
                await msg.add_reaction(ACCEPT_EMOJI)
                await msg.add_reaction(PASS_EMOJI)

                # Record the offer in the DB
                offer_id = await db.create_rotation_offer(guild.id, land_id, msg.id)

                # Wait for reaction or staff skip (checks allowed reactors dynamically)
                result, reactor_id = await self._wait_for_reaction(guild.id, msg, timeout_minutes * 60, skip_event)

                if result == "accept":
                    # Process the purchase
                    for _ in range(daily_blocks):
                        chunks += 1
                        await db.update_land_chunks(land_id, chunks)
                        await db.record_purchase(land_id, "rotation", price)

                    await db.update_rotation_offer(offer_id, "accepted")
                    await db.move_to_bottom(guild.id, land_id)

                    reactor_str = f" (via <@{reactor_id}>)" if reactor_id else ""
                    confirm_embed = discord.Embed(
                        title="✅ Block Claimed!",
                        description=(
                            f"**{land_name}** accepted the offer{reactor_str}.\n"
                            f"• +{daily_blocks} block(s) → now **{chunks}** chunks\n"
                            f"• Price: **{price} ems** per block\n"
                            f"• **{land_name}** moves to the bottom of the rotation."
                        ),
                        colour=discord.Colour.green(),
                    )
                    await channel.send(embed=confirm_embed)
                    return  # Done for today

                elif result == "skip":
                    await db.update_rotation_offer(offer_id, "skipped")
                    skip_embed = discord.Embed(
                        description=f"⏭️ **{land_name}**'s turn was skipped by staff. Offering to the next settlement...",
                        colour=discord.Colour.light_grey(),
                    )
                    await channel.send(embed=skip_embed)

                else:
                    # Pass or timeout — mark and continue to next
                    await db.update_rotation_offer(offer_id, "passed" if result == "pass" else "timeout")
                    reactor_str = f" (via <@{reactor_id}>)" if reactor_id and result == "pass" else ""
                    skip_embed = discord.Embed(
                        description=(
                            f"{'❌' if result == 'pass' else '⏰'} "
                            f"**{land_name}** {'passed' + reactor_str if result == 'pass' else 'timed out'}. "
                            f"Offering to the next settlement..."
                        ),
                        colour=discord.Colour.light_grey(),
                    )
                    await channel.send(embed=skip_embed)

            # Everyone passed — add block to leftover pool
            await db.add_leftover_blocks(guild.id, daily_blocks)
            no_claim_embed = discord.Embed(
                title="📭 No One Claimed Today's Block",
                description=(
                    f"All {len(order)} settlements passed or timed out.\n"
                    f"**{daily_blocks}** block(s) added to the Leftover Pool."
                ),
                colour=discord.Colour.dark_grey(),
            )
            await channel.send(embed=no_claim_embed)

        except asyncio.CancelledError:
            log.info(f"[{guild.name}] Rotation cascade cancelled.")
            raise

    async def refresh_allowed_reactors(self, guild_id: int):
        """Update allowed reactors dynamically if an offer is currently pending."""
        pending = await db.get_pending_rotation_offer(guild_id)
        if pending:
            officer_ids = await db.get_officers(pending["land_id"])
            fresh_land = await db.get_land_by_id(pending["land_id"])
            owner_id = fresh_land["owner_id"] if fresh_land else pending["owner_id"]
            self._allowed_reactors[guild_id] = {owner_id, *officer_ids}

    async def _wait_for_reaction(
        self, guild_id: int, message: discord.Message, timeout_seconds: float, skip_event: asyncio.Event = None
    ) -> tuple[str, int | None]:
        """Wait for the owner or an officer to react, or staff to skip."""
        def check(payload: discord.RawReactionActionEvent):
            allowed = self._allowed_reactors.get(guild_id, set())
            return (
                payload.message_id == message.id
                and payload.user_id in allowed
                and str(payload.emoji) in (ACCEPT_EMOJI, PASS_EMOJI)
            )

        reaction_task = asyncio.create_task(self.bot.wait_for("raw_reaction_add", check=check))
        tasks = [reaction_task]
        skip_task = None
        if skip_event:
            skip_task = asyncio.create_task(skip_event.wait())
            tasks.append(skip_task)

        done, pending = await asyncio.wait(tasks, timeout=timeout_seconds, return_when=asyncio.FIRST_COMPLETED)
        for t in pending:
            t.cancel()

        if not done:
            return "timeout", None

        if skip_task and skip_task in done:
            return "skip", None

        if reaction_task in done:
            try:
                payload = reaction_task.result()
                if str(payload.emoji) == ACCEPT_EMOJI:
                    return "accept", payload.user_id
                else:
                    return "pass", payload.user_id
            except Exception:
                return "timeout", None

        return "timeout", None

    # ── Slash commands ────────────────────────────────────────────────

    @app_commands.command(name="order", description="View the current rotation order")
    @app_commands.guild_only()
    async def rotation_order(self, interaction: discord.Interaction):
        await interaction.response.defer()

        order = await db.get_rotation_order(interaction.guild_id)
        if not order:
            return await interaction.followup.send("📭 No lands in the rotation yet.")

        full_block_cost = await db.get_full_block_cost(interaction.guild_id)

        lines = []
        for entry in order:
            price = models.block_price(entry["chunks"], full_block_cost)
            officer_ids = await db.get_officers(entry["land_id"])
            officer_str = f" (Officers: {', '.join(f'<@{uid}>' for uid in officer_ids)})" if officer_ids else ""
            lines.append(
                f"**{entry['position']}.** {entry['land_name']} — "
                f"{entry['chunks']} chunks, {price} ems — <@{entry['owner_id']}>{officer_str}"
            )

        paused = await db.get_config(interaction.guild_id, "rotation_paused")
        status_tag = " ⏸️ [PAUSED]" if paused == "true" else ""
        embed = discord.Embed(
            title=f"🔄 Rotation Order{status_tag}",
            description="\n".join(lines),
            colour=discord.Colour.orange() if paused == "true" else discord.Colour.blue(),
        )
        last = await db.get_last_rotation_date(interaction.guild_id)
        if last:
            embed.set_footer(text=f"Last rotation: {last}")

        await interaction.followup.send(embed=embed)

    @app_commands.command(name="add", description="[Staff] Add a land to the rotation")
    @app_commands.describe(
        land_name="Name of the land to add",
        position="Position in the rotation (optional, defaults to end)",
    )
    @app_commands.guild_only()
    async def rotation_add(
        self,
        interaction: discord.Interaction,
        land_name: str,
        position: int = None,
    ):
        await interaction.response.defer(ephemeral=True)
        if not await _is_staff(interaction):
            return await interaction.followup.send("🔒 Staff only.", ephemeral=True)

        land = await db.get_land_by_name(interaction.guild_id, land_name)
        if not land:
            return await interaction.followup.send(f"❌ Land **{land_name}** not found.", ephemeral=True)

        await db.add_to_rotation(interaction.guild_id, land["id"], position)
        order = await db.get_rotation_order(interaction.guild_id)
        # Find actual position
        actual_pos = next((e["position"] for e in order if e["land_id"] == land["id"]), "?")

        await interaction.followup.send(
            f"✅ **{land['name']}** added to the rotation at position **#{actual_pos}**.",
            ephemeral=True,
        )

    @app_commands.command(name="remove", description="[Staff] Remove a land from the rotation")
    @app_commands.describe(land_name="Name of the land to remove")
    @app_commands.guild_only()
    async def rotation_remove(self, interaction: discord.Interaction, land_name: str):
        await interaction.response.defer(ephemeral=True)
        if not await _is_staff(interaction):
            return await interaction.followup.send("🔒 Staff only.", ephemeral=True)

        land = await db.get_land_by_name(interaction.guild_id, land_name)
        if not land:
            return await interaction.followup.send(f"❌ Land **{land_name}** not found.", ephemeral=True)

        removed = await db.remove_from_rotation(interaction.guild_id, land["id"])
        if not removed:
            return await interaction.followup.send(
                f"⚠️ **{land['name']}** was not in the rotation.", ephemeral=True
            )

        await interaction.followup.send(
            f"✅ **{land['name']}** removed from the rotation.", ephemeral=True
        )

    @app_commands.command(name="move", description="[Staff] Move a land to a specific position")
    @app_commands.describe(
        land_name="Name of the land to move",
        position="New position number",
    )
    @app_commands.guild_only()
    async def rotation_move(self, interaction: discord.Interaction, land_name: str, position: int):
        await interaction.response.defer(ephemeral=True)
        if not await _is_staff(interaction):
            return await interaction.followup.send("🔒 Staff only.", ephemeral=True)

        land = await db.get_land_by_name(interaction.guild_id, land_name)
        if not land:
            return await interaction.followup.send(f"❌ Land **{land_name}** not found.", ephemeral=True)

        await db.set_rotation_position(interaction.guild_id, land["id"], position)
        await interaction.followup.send(
            f"✅ **{land['name']}** moved to position **#{position}**.", ephemeral=True
        )

    @app_commands.command(name="skip", description="[Staff] Skip the current land's turn and offer to the next in line")
    @app_commands.guild_only()
    async def rotation_skip(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        if not await _is_staff(interaction):
            return await interaction.followup.send("🔒 Staff only.", ephemeral=True)

        pending = await db.get_pending_rotation_offer(interaction.guild_id)
        skip_event = self._skip_events.get(interaction.guild_id)

        if not pending:
            return await interaction.followup.send("📭 No active pending rotation offer to skip.", ephemeral=True)

        if skip_event:
            skip_event.set()
        else:
            await db.update_rotation_offer(pending["id"], "skipped")

        await interaction.followup.send(
            f"⏭️ Skipped offer for **{pending['land_name']}**. Offering to the next settlement in line...",
            ephemeral=True,
        )

    async def _do_cancel(self, interaction: discord.Interaction):
        """Shared cancel/end logic — caller must defer first."""
        task = self._cascade_tasks.get(interaction.guild_id)
        has_task = task and not task.done()
        pending = await db.get_pending_rotation_offer(interaction.guild_id)

        if not has_task and not pending:
            return await interaction.followup.send("📭 No active rotation selection is running.", ephemeral=True)

        if has_task:
            task.cancel()

        if pending:
            await db.update_rotation_offer(pending["id"], "cancelled")

        channel = await _get_rotation_channel(interaction.guild)
        if channel:
            if pending and pending.get("message_id"):
                try:
                    msg = await channel.fetch_message(pending["message_id"])
                    await msg.clear_reactions()
                except Exception:
                    pass

            cancel_embed = discord.Embed(
                title="🛑 Rotation Selection Ended",
                description=(
                    f"Today's rotation selection was cancelled by staff ({interaction.user.mention}).\n"
                    f"No further offers will be sent today unless `/rotation trigger` is run."
                ),
                colour=discord.Colour.red(),
            )
            await channel.send(embed=cancel_embed)

        await interaction.followup.send("🛑 Active rotation selection ended and cancelled.", ephemeral=True)

    @app_commands.command(name="cancel", description="[Staff] End and cancel the active rotation selection for today")
    @app_commands.guild_only()
    async def rotation_cancel(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        if not await _is_staff(interaction):
            return await interaction.followup.send("🔒 Staff only.", ephemeral=True)
        await self._do_cancel(interaction)

    @app_commands.command(name="end", description="[Staff] End and cancel the active rotation selection for today")
    @app_commands.guild_only()
    async def rotation_end(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        if not await _is_staff(interaction):
            return await interaction.followup.send("🔒 Staff only.", ephemeral=True)
        await self._do_cancel(interaction)

    @app_commands.command(name="pause", description="[Staff] Pause the daily rotation system and stop any active selection")
    @app_commands.guild_only()
    async def rotation_pause(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        if not await _is_staff(interaction):
            return await interaction.followup.send("🔒 Staff only.", ephemeral=True)

        await db.set_config(interaction.guild_id, "rotation_paused", "true")

        # Stop active cascade task if running
        task = self._cascade_tasks.get(interaction.guild_id)
        has_task = task and not task.done()
        if has_task:
            task.cancel()

        pending = await db.get_pending_rotation_offer(interaction.guild_id)
        if pending:
            await db.update_rotation_offer(pending["id"], "paused")

        channel = await _get_rotation_channel(interaction.guild)
        if channel:
            if pending and pending.get("message_id"):
                try:
                    msg = await channel.fetch_message(pending["message_id"])
                    await msg.clear_reactions()
                except Exception:
                    pass

            embed = discord.Embed(
                title="⏸️ Daily Rotation Paused",
                description=(
                    f"The daily rotation has been **paused** by staff ({interaction.user.mention}).\n"
                    f"Any active selection has been stopped, and automatic daily runs are halted.\n"
                    f"Use `/rotation resume` when ready to unpause."
                ),
                colour=discord.Colour.orange(),
            )
            await channel.send(embed=embed)

        await interaction.followup.send("⏸️ Daily rotation paused and active selections stopped.", ephemeral=True)

    @app_commands.command(name="resume", description="[Staff] Resume the daily rotation system")
    @app_commands.describe(trigger_now="Start a rotation round right now (default: False)")
    @app_commands.guild_only()
    async def rotation_resume(self, interaction: discord.Interaction, trigger_now: bool = False):
        await interaction.response.defer(ephemeral=True)
        if not await _is_staff(interaction):
            return await interaction.followup.send("🔒 Staff only.", ephemeral=True)

        await db.set_config(interaction.guild_id, "rotation_paused", "false")

        channel = await _get_rotation_channel(interaction.guild)
        if channel:
            embed = discord.Embed(
                title="▶️ Daily Rotation Resumed",
                description=(
                    f"The daily rotation has been **resumed** by staff ({interaction.user.mention}).\n"
                    f"Automatic daily turn distribution is now active."
                ),
                colour=discord.Colour.green(),
            )
            await channel.send(embed=embed)

        if trigger_now:
            await interaction.followup.send("▶️ Daily rotation resumed. Starting a round now...", ephemeral=True)
            await self._start_rotation(interaction.guild)
        else:
            await interaction.followup.send("▶️ Daily rotation resumed. It will trigger at the next scheduled time.", ephemeral=True)

    async def _do_reping(self, interaction: discord.Interaction):
        """Shared reping/resend logic — caller must defer first."""
        pending = await db.get_pending_rotation_offer(interaction.guild_id)
        if not pending:
            return await interaction.followup.send("📭 No active rotation offer is currently waiting for a response.", ephemeral=True)

        land_id = pending["land_id"]
        land_name = pending["land_name"]

        # Cancel current cascade task
        task = self._cascade_tasks.get(interaction.guild_id)
        if task and not task.done():
            task.cancel()

        # Mark old offer as replaced
        await db.update_rotation_offer(pending["id"], "replaced")

        # Clean up old message
        channel = await _get_rotation_channel(interaction.guild)
        if channel and pending.get("message_id"):
            try:
                old_msg = await channel.fetch_message(pending["message_id"])
                await old_msg.clear_reactions()
                old_embed = old_msg.embeds[0] if old_msg.embeds else None
                if old_embed:
                    old_embed.title = "🔄 Daily Rotation — (Re-sent Below)"
                    old_embed.colour = discord.Colour.dark_grey()
                    await old_msg.edit(content=None, embed=old_embed)
            except Exception:
                pass

        # Find position of this land in rotation order
        order = await db.get_rotation_order(interaction.guild_id)
        start_idx = 0
        for idx, entry in enumerate(order):
            if entry["land_id"] == land_id:
                start_idx = idx
                break

        # Restart rotation from this exact land
        await self._start_rotation(interaction.guild, start_position=start_idx)

        await interaction.followup.send(
            f"🔄 Re-sent and re-pinged offer for **{land_name}** with updated leaders/officers!",
            ephemeral=True,
        )

    @app_commands.command(name="reping", description="[Staff] Re-send and re-ping the current turn offer with updated leaders/officers")
    @app_commands.guild_only()
    async def rotation_reping(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        if not await _is_staff(interaction):
            return await interaction.followup.send("🔒 Staff only.", ephemeral=True)
        await self._do_reping(interaction)

    @app_commands.command(name="resend", description="[Staff] Re-send and re-ping the current turn offer with updated leaders/officers")
    @app_commands.guild_only()
    async def rotation_resend(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        if not await _is_staff(interaction):
            return await interaction.followup.send("🔒 Staff only.", ephemeral=True)
        await self._do_reping(interaction)

    @app_commands.command(name="trigger", description="[Staff] Manually trigger today's rotation now")
    @app_commands.describe(force="Force start rotation even if an offer is already pending (default: False)")
    @app_commands.guild_only()
    async def rotation_trigger(self, interaction: discord.Interaction, force: bool = False):
        await interaction.response.defer(ephemeral=True)
        if not await _is_staff(interaction):
            return await interaction.followup.send("🔒 Staff only.", ephemeral=True)

        pending = await db.get_pending_rotation_offer(interaction.guild_id)
        if pending and not force:
            return await interaction.followup.send(
                f"⚠️ There's already a pending offer to **{pending['land_name']}**.\n"
                f"• To re-ping this offer with updated officers, use `/rotation reping`.\n"
                f"• To skip to the next settlement, use `/rotation skip`.\n"
                f"• To cancel it completely, use `/rotation cancel`.\n"
                f"• Or re-run with `/rotation trigger force:True`.",
                ephemeral=True,
            )

        if pending and force:
            task = self._cascade_tasks.get(interaction.guild_id)
            if task and not task.done():
                task.cancel()
            await db.update_rotation_offer(pending["id"], "cancelled")

        await interaction.followup.send("🔄 Starting rotation...", ephemeral=True)
        await self._start_rotation(interaction.guild)


async def setup(bot: commands.Bot):
    await bot.add_cog(RotationCog(bot))
