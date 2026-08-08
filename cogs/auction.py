"""
Auction cog — Weekend Reserve Block Auction System.

Reserve blocks above the protected minimum are auctioned during the weekend.
- Starts: Saturday 00:00 UTC (configurable)
- Ends: Sunday 23:59 UTC (configurable)
- Minimum bid: 1.5× full block cost (max block price)
- Top N highest bids win 1 reserve block each.
"""

import math
import logging
from datetime import datetime, timezone, timedelta

import discord
from discord import app_commands
from discord.ext import commands, tasks

import database as db

log = logging.getLogger("claims-bot")


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


class AuctionCog(commands.GroupCog, name="auction"):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        super().__init__()

    async def cog_load(self):
        self.auction_loop.start()

    async def cog_unload(self):
        self.auction_loop.cancel()

    # ── Background loop ───────────────────────────────────────────────

    @tasks.loop(minutes=5)
    async def auction_loop(self):
        try:
            for guild in self.bot.guilds:
                await self._check_guild_auction(guild)
        except Exception as e:
            log.error(f"Auction loop error: {e}", exc_info=True)

    @auction_loop.before_loop
    async def before_auction_loop(self):
        await self.bot.wait_until_ready()

    async def _check_guild_auction(self, guild: discord.Guild):
        guild_id = guild.id
        enabled = (await db.get_config(guild_id, "auction_enabled") or "true").lower() == "true"
        if not enabled:
            return

        now = datetime.now(timezone.utc)
        start_day = int(await db.get_config(guild_id, "auction_start_day") or "5")  # 5 = Saturday
        end_day = int(await db.get_config(guild_id, "auction_end_day") or "6")      # 6 = Sunday

        active = await db.get_active_auction(guild_id)

        # Check if we should end an active auction whose window has passed
        if active:
            try:
                ends_dt = datetime.fromisoformat(active["ends_at"]).replace(tzinfo=timezone.utc)
                if now >= ends_dt:
                    await self._resolve_auction(guild, active)
                    return
            except (ValueError, TypeError):
                pass

        # Check if we should auto-start an auction on the weekend
        if not active and now.weekday() >= start_day and now.weekday() <= end_day:
            reserve = await db.get_reserve(guild_id)
            available = reserve["total_blocks"] - reserve["protected_min"]
            if available > 0:
                # Calculate end of weekend (end of Sunday / start of Monday)
                days_until_end = (end_day - now.weekday()) % 7
                end_time = (now + timedelta(days=days_until_end)).replace(hour=23, minute=59, second=59, microsecond=0)
                
                auction_id = await db.create_auction(
                    guild_id=guild_id,
                    blocks_offered=available,
                    started_at=now.isoformat(),
                    ends_at=end_time.isoformat(),
                )
                log.info(f"[{guild.name}] Started weekend reserve auction #{auction_id} ({available} blocks)")
                await self._announce_auction_start(guild, available, end_time)

    async def _resolve_auction(self, guild: discord.Guild, auction: dict):
        guild_id = guild.id
        auction_id = auction["id"]
        blocks_offered = auction["blocks_offered"]

        bids = await db.get_auction_bids(auction_id)
        await db.close_auction(auction_id)

        log.info(f"=== [{guild.name}] Resolving Auction #{auction_id} ({len(bids)} bid(s), {blocks_offered} block(s) offered) ===")

        winning_bids = bids[:blocks_offered]
        winners_info = []

        for bid in winning_bids:
            land_id = bid["land_id"]
            land_name = bid["land_name"]
            owner_id = bid["owner_id"]
            amount = bid["bid_amount"]

            # Give land +1 chunk and deduct 1 from reserve pool
            old_chunks = bid["chunks"]
            await db.update_land_chunks(land_id, old_chunks + 1)
            await db.add_reserve_blocks(guild_id, -1)
            await db.record_purchase(land_id, "auction", amount)

            winners_info.append(f"🏰 **{land_name}** (<@{owner_id}>) — **{amount} ems** (+1 chunk)")

            # Notify owner
            try:
                user = await self.bot.fetch_user(owner_id)
                if user:
                    await user.send(
                        f"🎉 **Congratulations!** Your land **{land_name}** won a reserve block in the "
                        f"**{guild.name}** weekend auction with a winning bid of **{amount} ems**!"
                    )
            except Exception:
                pass

        # Post auction summary
        embed = discord.Embed(
            title="🔨 Reserve Block Auction Ended!",
            colour=discord.Colour.gold(),
            timestamp=datetime.now(timezone.utc),
        )
        embed.add_field(name="📦 Blocks Offered", value=str(blocks_offered), inline=True)
        embed.add_field(name="🙋 Total Bids", value=str(len(bids)), inline=True)
        embed.add_field(name="🏆 Winning Bids", value=str(len(winning_bids)), inline=True)

        if winners_info:
            embed.add_field(name="🎉 Winners", value="\n".join(winners_info), inline=False)
        else:
            embed.add_field(name="🎉 Winners", value="No bids were placed this weekend.", inline=False)

        embed.set_footer(text=f"Auction #{auction_id} resolved for {guild.name}")

        for channel in guild.text_channels:
            if channel.permissions_for(guild.me).send_messages:
                try:
                    await channel.send(embed=embed)
                except discord.HTTPException:
                    continue
                break

    async def _announce_auction_start(self, guild: discord.Guild, blocks: int, end_time: datetime):
        full_cost = await db.get_full_block_cost(guild.id)
        start_price = math.ceil(full_cost * 1.5)

        embed = discord.Embed(
            title="🔨 Weekend Reserve Block Auction Live!",
            description=(
                f"**{blocks} reserve block(s)** are now up for auction!\n\n"
                f"• **Starting Bid:** `{start_price} ems` (1.5× max block price)\n"
                f"• **Ends:** <t:{int(end_time.timestamp())}:F> (<t:{int(end_time.timestamp())}:R>)\n"
                f"• **Place Bid:** `/auction bid land_name: \"...\" amount: <ems>`"
            ),
            colour=discord.Colour.purple(),
            timestamp=datetime.now(timezone.utc),
        )
        embed.set_footer(text=f"Weekend Reserve Auction for {guild.name}")

        for channel in guild.text_channels:
            if channel.permissions_for(guild.me).send_messages:
                try:
                    await channel.send(embed=embed)
                except discord.HTTPException:
                    continue
                break

    # ── Slash Commands ────────────────────────────────────────────────

    @app_commands.command(name="status", description="View active auction status and top bids")
    @app_commands.guild_only()
    async def auction_status(self, interaction: discord.Interaction):
        await interaction.response.defer()

        active = await db.get_active_auction(interaction.guild_id)
        if not active:
            embed = discord.Embed(
                title="🔨 Reserve Block Auction",
                description="There is currently **no active auction** running on this server.\nAuctions automatically start every weekend when reserve blocks above the protected minimum are available.",
                colour=discord.Colour.light_grey(),
            )
            return await interaction.followup.send(embed=embed)

        bids = await db.get_auction_bids(active["id"])
        full_cost = await db.get_full_block_cost(interaction.guild_id)
        start_price = math.ceil(full_cost * 1.5)
        ends_dt = datetime.fromisoformat(active["ends_at"]).replace(tzinfo=timezone.utc)

        embed = discord.Embed(
            title=f"🔨 Active Reserve Auction (ID #{active['id']})",
            colour=discord.Colour.gold(),
        )
        embed.add_field(name="📦 Reserve Blocks Offered", value=str(active["blocks_offered"]), inline=True)
        embed.add_field(name="💰 Min Starting Bid", value=f"{start_price} ems", inline=True)
        embed.add_field(name="⏰ Time Remaining", value=f"<t:{int(ends_dt.timestamp())}:R>", inline=True)

        if bids:
            top_lines = []
            for i, b in enumerate(bids, 1):
                winner_tag = " 🏆 (Winning)" if i <= active["blocks_offered"] else ""
                top_lines.append(f"`#{i}` **{b['land_name']}** — **{b['bid_amount']} ems** (<@{b['owner_id']}>){winner_tag}")
            embed.add_field(name=f"📋 Bids ({len(bids)})", value="\n".join(top_lines[:15]), inline=False)
        else:
            embed.add_field(name="📋 Bids", value="No bids placed yet! Use `/auction bid` to enter.", inline=False)

        embed.set_footer(text="Use /auction bid land_name: \"...\" amount: <ems> to place a bid")
        await interaction.followup.send(embed=embed)

    @app_commands.command(name="bid", description="Place or increase a bid for a reserve block")
    @app_commands.describe(land_name="Name of your land", amount="Your bid amount in ems")
    @app_commands.guild_only()
    async def auction_bid(self, interaction: discord.Interaction, land_name: str, amount: int):
        await interaction.response.defer()

        active = await db.get_active_auction(interaction.guild_id)
        if not active:
            return await interaction.followup.send("❌ There is no active auction right now.", ephemeral=True)

        land = await db.get_land_by_name(interaction.guild_id, land_name)
        if not land:
            return await interaction.followup.send(f"❌ Land **{land_name}** not found on this server.", ephemeral=True)

        # Ensure caller is owner or staff
        if land["owner_id"] != interaction.user.id and not await _is_staff(interaction):
            return await interaction.followup.send("❌ You are not the owner of this land.", ephemeral=True)

        full_cost = await db.get_full_block_cost(interaction.guild_id)
        min_bid = math.ceil(full_cost * 1.5)

        if amount < min_bid:
            return await interaction.followup.send(
                f"❌ Bid too low! Starting minimum bid for reserve blocks is **{min_bid} ems** (1.5× max block price).",
                ephemeral=True,
            )

        # Check existing bid for this land
        bids = await db.get_auction_bids(active["id"])
        existing_bid = next((b for b in bids if b["land_id"] == land["id"]), None)
        if existing_bid and amount <= existing_bid["bid_amount"]:
            return await interaction.followup.send(
                f"❌ Your bid must be higher than your current bid of **{existing_bid['bid_amount']} ems**.",
                ephemeral=True,
            )

        await db.place_auction_bid(active["id"], land["id"], amount)

        # Fetch updated rank
        updated_bids = await db.get_auction_bids(active["id"])
        rank = next((i + 1 for i, b in enumerate(updated_bids) if b["land_id"] == land["id"]), 1)
        is_winning = rank <= active["blocks_offered"]

        embed = discord.Embed(
            title="🔨 Bid Placed!",
            colour=discord.Colour.green(),
            description=(
                f"Bid of **{amount} ems** placed for **{land['name']}**!\n"
                f"• Current Rank: `#{rank}` of {len(updated_bids)}\n"
                f"• Status: {'🏆 **In Winning Position**' if is_winning else '⚠️ **Outside Winning Range**'}"
            ),
        )
        embed.set_footer(text=f"Auction #{active['id']} • {active['blocks_offered']} block(s) available")
        await interaction.followup.send(embed=embed)

    @app_commands.command(name="start", description="[Staff] Manually open a reserve block auction")
    @app_commands.describe(blocks="Number of blocks to auction (leave empty for all available reserve)")
    @app_commands.guild_only()
    async def auction_start(self, interaction: discord.Interaction, blocks: int = None):
        await interaction.response.defer()

        if not await _is_staff(interaction):
            return await interaction.followup.send("🔒 Staff only.", ephemeral=True)

        active = await db.get_active_auction(interaction.guild_id)
        if active:
            return await interaction.followup.send(
                f"❌ An auction (ID #{active['id']}) is already active on this server.", ephemeral=True
            )

        reserve = await db.get_reserve(interaction.guild_id)
        available = reserve["total_blocks"] - reserve["protected_min"]
        if blocks is None:
            blocks = available

        if blocks <= 0:
            return await interaction.followup.send(
                f"❌ Cannot start auction with {blocks} blocks (available reserve: {available}).", ephemeral=True
            )

        now = datetime.now(timezone.utc)
        end_time = now + timedelta(days=2)

        auction_id = await db.create_auction(
            guild_id=interaction.guild_id,
            blocks_offered=blocks,
            started_at=now.isoformat(),
            ends_at=end_time.isoformat(),
        )

        await self._announce_auction_start(interaction.guild, blocks, end_time)

        embed = discord.Embed(
            title="🔨 Auction Started",
            colour=discord.Colour.purple(),
            description=f"Opened Auction #{auction_id} offering **{blocks}** reserve block(s).",
        )
        await interaction.followup.send(embed=embed)

    @app_commands.command(name="end", description="[Staff] Manually close and resolve active auction")
    @app_commands.guild_only()
    async def auction_end(self, interaction: discord.Interaction):
        await interaction.response.defer()

        if not await _is_staff(interaction):
            return await interaction.followup.send("🔒 Staff only.", ephemeral=True)

        active = await db.get_active_auction(interaction.guild_id)
        if not active:
            return await interaction.followup.send("❌ No active auction to end.", ephemeral=True)

        await self._resolve_auction(interaction.guild, active)
        await interaction.followup.send(f"✅ Auction #{active['id']} closed and resolved successfully.")

    @app_commands.command(name="cancel", description="[Staff] Cancel active auction without distributing blocks")
    @app_commands.guild_only()
    async def auction_cancel(self, interaction: discord.Interaction):
        await interaction.response.defer()

        if not await _is_staff(interaction):
            return await interaction.followup.send("🔒 Staff only.", ephemeral=True)

        active = await db.get_active_auction(interaction.guild_id)
        if not active:
            return await interaction.followup.send("❌ No active auction to cancel.", ephemeral=True)

        await db.cancel_auction(active["id"])
        await interaction.followup.send(f"⚠️ Auction #{active['id']} cancelled. No blocks were distributed.")


async def setup(bot: commands.Bot):
    await bot.add_cog(AuctionCog(bot))
