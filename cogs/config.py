"""
Config cog — /config set, /config get, /config list
Staff-only bot configuration management.
All settings are scoped per Discord server (guild).
"""

import discord
from discord import app_commands
from discord.ext import commands

import database as db

VALID_KEYS = {
    "full_block_cost": "Price for the 26+ chunks tier (integer ems)",
    "staff_role_id": "Discord role ID for staff permissions",
    "weekly_reset_day": "Day of week for weekly reset (0=Mon, 6=Sun)",
    "weekly_blocks": "Blocks generated per weekly distribution (default 7)",
    "distribution_day": "Day of week for distribution (0=Mon, 6=Sun)",
    "distribution_hour": "Hour (UTC) for distribution (0-23)",
    "max_queue_chunks": "Max chunks a land can request per queue submission (default 1)",
    "reserve_mode": "Reserve policy mode: 'topup' or 'fixed' (default topup)",
    "weekly_reserve_blocks": "Fixed blocks allocated to reserve in fixed mode (default 2)",
    "protected_min": "Minimum protected reserve blocks not for sale (default 3)",
    "auction_start_day": "Day of week for auction start (0=Mon, 5=Sat, default 5)",
    "auction_end_day": "Day of week for auction end (0=Mon, 6=Sun, default 6)",
    "auction_enabled": "Enable/disable weekend reserve block auction ('true' or 'false', default true)",
    "claim_mode": "Claim system mode: 'weekly' (queue+distribution) or 'rotation' (daily turn-based)",
    "rotation_hour": "Hour (UTC) when daily rotation triggers (0-23, default 12)",
    "rotation_timeout_minutes": "Minutes to wait for reaction before auto-skip (default 720 = 12h)",
    "rotation_channel_id": "Channel ID for rotation messages (default: first writable channel)",
    "rotation_daily_blocks": "Blocks generated per daily rotation (default 1)",
}


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


class ConfigCog(commands.GroupCog, name="config"):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        super().__init__()

    @app_commands.command(name="set", description="[Staff] Set a bot config value for this server")
    @app_commands.describe(key="Config key to set", value="New value")
    @app_commands.choices(
        key=[app_commands.Choice(name=k, value=k) for k in VALID_KEYS]
    )
    @app_commands.guild_only()
    async def config_set(self, interaction: discord.Interaction, key: app_commands.Choice[str], value: str):
        await interaction.response.defer()

        if not await _is_staff(interaction):
            return await interaction.followup.send("🔒 Staff only.", ephemeral=True)

        await db.set_config(interaction.guild_id, key.value, value)
        if key.value == "protected_min":
            try:
                await db.update_protected_min(interaction.guild_id, int(value))
            except ValueError:
                pass

        embed = discord.Embed(title="⚙️ Config Updated", colour=discord.Colour.blurple())
        embed.add_field(name="Key", value=f"`{key.value}`", inline=True)
        embed.add_field(name="Value", value=f"`{value}`", inline=True)
        embed.add_field(name="Description", value=VALID_KEYS[key.value], inline=False)
        embed.set_footer(text=f"Set by {interaction.user.display_name}")
        await interaction.followup.send(embed=embed)

    @app_commands.command(name="get", description="[Staff] View a bot config value for this server")
    @app_commands.describe(key="Config key to read")
    @app_commands.choices(
        key=[app_commands.Choice(name=k, value=k) for k in VALID_KEYS]
    )
    @app_commands.guild_only()
    async def config_get(self, interaction: discord.Interaction, key: app_commands.Choice[str]):
        await interaction.response.defer(ephemeral=True)

        if not await _is_staff(interaction):
            return await interaction.followup.send("🔒 Staff only.", ephemeral=True)

        value = await db.get_config(interaction.guild_id, key.value)
        embed = discord.Embed(title="⚙️ Config Value", colour=discord.Colour.blurple())
        embed.add_field(name="Key", value=f"`{key.value}`", inline=True)
        embed.add_field(name="Value", value=f"`{value}`" if value else "*Not set*", inline=True)
        embed.add_field(name="Description", value=VALID_KEYS[key.value], inline=False)
        await interaction.followup.send(embed=embed, ephemeral=True)

    @app_commands.command(name="list", description="[Staff] List all config keys and values for this server")
    @app_commands.guild_only()
    async def config_list(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)

        if not await _is_staff(interaction):
            return await interaction.followup.send("🔒 Staff only.", ephemeral=True)

        embed = discord.Embed(title="⚙️ All Config", colour=discord.Colour.blurple())
        for key, desc in VALID_KEYS.items():
            value = await db.get_config(interaction.guild_id, key)
            embed.add_field(name=f"`{key}`", value=f"**{value}**\n_{desc}_", inline=False)
        await interaction.followup.send(embed=embed, ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(ConfigCog(bot))
