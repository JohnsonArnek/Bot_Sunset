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

        for i in range(start_idx, len(order)):
            entry = order[i]
            land_id = entry["land_id"]
            land_name = entry["land_name"]
            owner_id = entry["owner_id"]
            chunks = entry["chunks"]
            price = models.block_price(chunks, full_block_cost)
            tier_label = models.price_tier_label(chunks, full_block_cost)

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

            # Send the offer and ping the owner
            msg = await channel.send(content=f"<@{owner_id}>", embed=embed)
            await msg.add_reaction(ACCEPT_EMOJI)
            await msg.add_reaction(PASS_EMOJI)

            # Record the offer in the DB
            offer_id = await db.create_rotation_offer(guild.id, land_id, msg.id)

            # Wait for the owner's reaction
            result = await self._wait_for_reaction(msg, owner_id, timeout_minutes * 60)

            if result == "accept":
                # Process the purchase
                for _ in range(daily_blocks):
                    chunks += 1
                    await db.update_land_chunks(land_id, chunks)
                    await db.record_purchase(land_id, "rotation", price)

                await db.update_rotation_offer(offer_id, "accepted")
                await db.move_to_bottom(guild.id, land_id)

                confirm_embed = discord.Embed(
                    title="✅ Block Claimed!",
                    description=(
                        f"**{land_name}** accepted the offer.\n"
                        f"• +{daily_blocks} block(s) → now **{chunks}** chunks\n"
                        f"• Price: **{price} ems** per block\n"
                        f"• **{land_name}** moves to the bottom of the rotation."
                    ),
                    colour=discord.Colour.green(),
                )
                await channel.send(embed=confirm_embed)
                return  # Done for today

            else:
                # Pass or timeout — mark and continue to next
                await db.update_rotation_offer(offer_id, "passed" if result == "pass" else "timeout")
                skip_embed = discord.Embed(
                    description=(
                        f"{'❌' if result == 'pass' else '⏰'} "
                        f"**{land_name}** {'passed' if result == 'pass' else 'timed out'}. "
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

    async def _wait_for_reaction(
        self, message: discord.Message, owner_id: int, timeout_seconds: float
    ) -> str:
        """Wait for the owner to react. Returns 'accept', 'pass', or 'timeout'."""
        def check(payload: discord.RawReactionActionEvent):
            return (
                payload.message_id == message.id
                and payload.user_id == owner_id
                and str(payload.emoji) in (ACCEPT_EMOJI, PASS_EMOJI)
            )

        try:
            payload = await self.bot.wait_for(
                "raw_reaction_add", check=check, timeout=timeout_seconds
            )
            if str(payload.emoji) == ACCEPT_EMOJI:
                return "accept"
            else:
                return "pass"
        except asyncio.TimeoutError:
            return "timeout"

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
            lines.append(
                f"**{entry['position']}.** {entry['land_name']} — "
                f"{entry['chunks']} chunks, {price} ems — <@{entry['owner_id']}>"
            )

        embed = discord.Embed(
            title="🔄 Rotation Order",
            description="\n".join(lines),
            colour=discord.Colour.blue(),
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

    @app_commands.command(name="skip", description="[Staff] Skip the current pending offer")
    @app_commands.guild_only()
    async def rotation_skip(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        if not await _is_staff(interaction):
            return await interaction.followup.send("🔒 Staff only.", ephemeral=True)

        pending = await db.get_pending_rotation_offer(interaction.guild_id)
        if not pending:
            return await interaction.followup.send("📭 No pending rotation offer to skip.", ephemeral=True)

        await db.update_rotation_offer(pending["id"], "skipped")

        # Cancel the active cascade task if running
        task = self._cascade_tasks.get(interaction.guild_id)
        if task and not task.done():
            task.cancel()

        await interaction.followup.send(
            f"⏭️ Skipped offer to **{pending['land_name']}**. "
            f"Use `/rotation trigger` to re-run today's rotation.",
            ephemeral=True,
        )

    @app_commands.command(name="trigger", description="[Staff] Manually trigger today's rotation now")
    @app_commands.guild_only()
    async def rotation_trigger(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        if not await _is_staff(interaction):
            return await interaction.followup.send("🔒 Staff only.", ephemeral=True)

        # Check if there's already a pending offer
        pending = await db.get_pending_rotation_offer(interaction.guild_id)
        if pending:
            return await interaction.followup.send(
                f"⚠️ There's already a pending offer to **{pending['land_name']}**. "
                f"Use `/rotation skip` first if you want to restart.",
                ephemeral=True,
            )

        await interaction.followup.send("🔄 Starting rotation...", ephemeral=True)
        await self._start_rotation(interaction.guild)


async def setup(bot: commands.Bot):
    await bot.add_cog(RotationCog(bot))
