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
}


async def _is_staff(interaction: discord.Interaction) -> bool:
    staff_role_id = await db.get_staff_role_id(interaction.guild_id)
    if staff_role_id == 0:
        return interaction.user.id == interaction.guild.owner_id
    return any(r.id == staff_role_id for r in interaction.user.roles)


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
