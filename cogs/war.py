"""Administrator-only strategic reserves and settlement assault planning."""

import discord
from discord import app_commands
from discord.ext import commands

import war_store as stock
from war_planner import estimate, pack


Count = app_commands.Range[int, 0, 2_000_000_000]
PositiveCount = app_commands.Range[int, 1, 1_000_000]
Rate = app_commands.Range[float, 0.001, 1_000_000.0]
Location = app_commands.Range[str, 1, 64]


async def reply(interaction, text):
    await interaction.followup.send(text, ephemeral=True, allowed_mentions=discord.AllowedMentions.none())


def label(location):
    return discord.utils.escape_markdown(stock.clean_location(location))


def supply(name, needed, held):
    balance = held - needed
    detail = f"{balance:,} left" if balance >= 0 else f"short {-balance:,}"
    return f"{name}: need **{needed:,}** · have **{held:,}** · {detail}"


def units(n, singular, plural):
    return f"{n:,} {singular if n == 1 else plural}"


def transport(cargo):
    return (f"{units(cargo.slots, 'slot', 'slots')} · "
            f"{units(cargo.donkey_loads, 'donkey load', 'donkey loads')} or "
            f"{units(cargo.chest_boat_loads, 'chest-boat load', 'chest-boat loads')}")


@app_commands.guild_only()
@app_commands.default_permissions(administrator=True)
class WarCog(commands.GroupCog, name="war", group_description="Manage strategic reserves and plan a settlement assault"):
    async def interaction_check(self, interaction):
        # Discord's default command visibility can be overridden by guild settings.
        # Enforce Administrator here before any command can read or write reserves.
        if interaction.guild_id is None:
            raise app_commands.NoPrivateMessage()
        if not interaction.permissions.administrator:
            raise app_commands.MissingPermissions(["administrator"])
        await interaction.response.defer(ephemeral=True)
        return True

    @app_commands.command(name="stock-set", description="Record the actual stock at a named location, replacing its previous counts")
    @app_commands.describe(location="Depot name or coordinates", hay="Whole hay bales here", diamonds="Loose diamonds here")
    async def stock_set(self, interaction: discord.Interaction, location: Location, hay: Count, diamonds: Count):
        try:
            name = await stock.set_stock(interaction.guild_id, location, hay, diamonds)
        except ValueError as exc:
            return await reply(interaction, str(exc))
        await reply(interaction, f"**{label(name)}** now holds {hay:,} hay and {diamonds:,} diamonds.")

    @app_commands.command(name="stock-remove", description="Remove one location and its stock from this server's reserves")
    async def stock_remove(self, interaction: discord.Interaction, location: Location):
        try:
            await stock.remove_stock(interaction.guild_id, location)
        except ValueError as exc:
            return await reply(interaction, str(exc))
        await reply(interaction, f"No reserves are recorded at **{label(location)}**.")

    @app_commands.command(name="stock-list", description="Show reserve locations and their current balances")
    async def stock_list(self, interaction: discord.Interaction, page: PositiveCount = 1):
        rows = await stock.stock_page(interaction.guild_id, page)
        if not rows:
            return await reply(interaction, "No locations on this page. Use /war stock-set to record supplies.")
        totals = await stock.available(interaction.guild_id)
        lines = [f"**All reserves:** {totals['hay']:,} hay · {totals['diamonds']:,} diamonds",
                 f"Locations · page {page} (8 per page)"]
        lines.extend(f"**{label(r['location'])}**: {r['hay']:,} hay · {r['diamonds']:,} diamonds" for r in rows)
        lines.append("Use /war logistics for transport and chest space.")
        await reply(interaction, "\n".join(lines))

    @app_commands.command(description="Estimate transport loads and chest space for the supplies in stock")
    @app_commands.describe(location="Depot to pack; omit to estimate all supplies consolidated in one place")
    async def logistics(self, interaction: discord.Interaction, location: Location = None):
        try:
            held = await stock.available(interaction.guild_id, location)
        except ValueError as exc:
            return await reply(interaction, str(exc))
        if held is None:
            return await reply(interaction, "No such reserve location on this server. See /war stock-list.")
        rules = await stock.rules_for(interaction.guild_id)
        cargo = pack(rules, **held)
        scope = label(location) if location is not None else "all depots, if pooled"
        await reply(interaction,
                    f"**Packing estimate: {scope}**\n"
                    f"{held['hay']:,} hay · {held['diamonds']:,} diamonds · up to {rules.stack_size} of one item per slot\n"
                    f"Transport: {transport(cargo)}.\n"
                    f"Storage: {units(cargo.single_chests, 'single chest', 'single chests')} "
                    f"({rules.chest_slots} slots each) or {units(cargo.double_chests, 'double chest', 'double chests')} "
                    f"({rules.double_chest_slots} slots each).")

    @app_commands.command(description="Show this server's war rules, or update only the options you supply")
    @app_commands.describe(
        diamonds_per_axe="Diamonds needed to craft one axe", axe_durability="Hits before a fresh axe breaks",
        damage_per_hit="Settlement HP removed per hit", rehit_seconds="Seconds between consecutive hits",
        bread_per_hay="Loaves crafted from one hay bale", hunger_per_bread="Hunger points restored per loaf",
        hits_per_hunger_loss="Hits per attacker before each hunger charge",
        hunger_loss="Hunger points lost per charge (0 disables food requirements)",
        stack_size="Items per stack for hay, loose diamonds and bread (maximum 64)",
        donkey_slots="Stacks per donkey load", chest_boat_slots="Stacks per chest-boat load",
        chest_slots="Slots per single chest", double_chest_slots="Slots per double chest",
    )
    async def config(self, interaction: discord.Interaction, diamonds_per_axe: PositiveCount = None,
                     axe_durability: PositiveCount = None, damage_per_hit: Rate = None,
                     rehit_seconds: Rate = None, bread_per_hay: PositiveCount = None,
                     hunger_per_bread: Rate = None, hits_per_hunger_loss: PositiveCount = None,
                     hunger_loss: app_commands.Range[float, 0.0, 1_000_000.0] = None,
                     stack_size: app_commands.Range[int, 1, 64] = None, donkey_slots: PositiveCount = None,
                     chest_boat_slots: PositiveCount = None, chest_slots: PositiveCount = None,
                     double_chest_slots: PositiveCount = None):
        options = dict(diamonds_per_axe=diamonds_per_axe, axe_durability=axe_durability,
                       damage_per_hit=damage_per_hit, rehit_seconds=rehit_seconds,
                       bread_per_hay=bread_per_hay, hunger_per_bread=hunger_per_bread,
                       hits_per_hunger_loss=hits_per_hunger_loss, hunger_loss=hunger_loss, stack_size=stack_size,
                       donkey_slots=donkey_slots, chest_boat_slots=chest_boat_slots,
                       chest_slots=chest_slots, double_chest_slots=double_chest_slots)
        try:
            rules = await stock.configure(interaction.guild_id, {k: v for k, v in options.items() if v is not None})
        except ValueError as exc:
            return await reply(interaction, str(exc))
        await reply(interaction,
                    f"**War rules for this server**\n"
                    f"Axe: {rules.diamonds_per_axe} diamonds · {rules.axe_durability} hits · "
                    f"{rules.damage_per_hit} HP per hit · {rules.rehit_seconds} s between hits.\n"
                    f"Food: {rules.bread_per_hay} bread per hay · {rules.hunger_per_bread} hunger per bread. "
                    f"Each attacker loses {rules.hunger_loss} hunger every {rules.hits_per_hunger_loss} hits.\n"
                    f"Packing: {rules.stack_size} per stack · slots per donkey: {rules.donkey_slots}, "
                    f"chest boat: {rules.chest_boat_slots}, single chest: {rules.chest_slots}, "
                    f"double chest: {rules.double_chest_slots}.")

    @app_commands.command(description="Estimate the time and check whether recorded supplies cover a settlement assault")
    @app_commands.describe(hp="Remaining settlement HP", players="Number of simultaneous attackers",
                           location="Use only this location; omit to pool all reserves")
    async def conquerable(self, interaction: discord.Interaction, hp: app_commands.Range[int, 1, 2_000_000_000],
                          players: PositiveCount, location: Location = None):
        try:
            held = await stock.available(interaction.guild_id, location)
        except ValueError as exc:
            return await reply(interaction, str(exc))
        if held is None:
            return await reply(interaction, "No such reserve location on this server. See /war stock-list.")
        rules = await stock.rules_for(interaction.guild_id)
        plan = estimate(hp, players, rules)
        raw = pack(rules, hay=plan.hay, diamonds=plan.diamonds)
        gear = pack(rules, axes=plan.axes, bread=plan.bread)
        enough = held["diamonds"] >= plan.diamonds and held["hay"] >= plan.hay
        verdict = "Enough supplies" if enough else "Insufficient supplies"
        scope = label(location) if location is not None else "all reserve locations"
        await reply(interaction,
                    f"**{verdict}** · {scope}\n"
                    f"{hp:,} HP · {plan.attackers:,} active attackers · {plan.hits:,} hits across {plan.volleys:,} volleys\n"
                    f"**Time if equipped: {plan.seconds:,f} s** from first hit to last.\n"
                    f"{supply('Diamonds', plan.diamonds, held['diamonds'])} ({plan.axes:,} axes)\n"
                    f"{supply('Hay', plan.hay, held['hay'])} ({plan.bread:,} loaves needed)\n"
                    f"**Transport for this assault** (choose one manifest):\n"
                    f"Raw hay and diamonds: {transport(raw)}.\n"
                    f"Crafted axes and required loaves: {transport(gear)}.\n"
                    "Assumes uninterrupted attacks, fresh axes and enough bread per attacker to restore lost hunger. "
                    "Travel, crafting and eating time excluded. No stock deducted.")


async def setup(bot: commands.Bot):
    await bot.add_cog(WarCog())
