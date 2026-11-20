"""Administrator-only strategic reserves and settlement assault planning."""

import asyncio
import logging
from typing import Literal

import discord
from discord import app_commands
from discord.ext import commands

import war_store as stock
from cogs.report_view import NO_MENTIONS, ReportView, clip
from cogs.war_share import ShareEstimate, message_payload
from war_display import duration, inventory, quantity
from cogs.war_plans import Route, SHARE_FOOTER, label, load_plan, whole_number
from war_planner import IRON_PER_BLOCK, defence_transfer, stock_quantity


Quantity = app_commands.Range[str, 1, 100]
PositiveCount = app_commands.Range[int, 1, 1_000_000]
Rate = app_commands.Range[float, 0.001, 1_000_000.0]
Minutes = app_commands.Range[float, 0.0, 1_000_000.0]
Location = app_commands.Range[str, 1, 64]
Mode = Literal["donkey", "chest_boat", "chest_minecart"]


async def reply(interaction, text):
    await interaction.followup.send(**message_payload(text), ephemeral=True, allowed_mentions=NO_MENTIONS)


async def send_report(interaction, embed, *, share_text=None, view=None):
    if share_text is not None:
        view = ShareEstimate(interaction, share_text)
        embed.set_footer(text=SHARE_FOOTER)
    controls = {"view": view} if view is not None else {}
    await interaction.followup.send(embed=embed, ephemeral=True, allowed_mentions=NO_MENTIONS, **controls)


def depot_embed(location, row, rules, change=None):
    embed = discord.Embed(title=clip(f"Depot · {label(location)}", 256), description=change, colour=discord.Colour.teal())
    for resource in stock.RESOURCES:
        embed.add_field(name=resource.title(), value=quantity(row[resource], resource, rules), inline=True)
    return embed


async def depot_report(interaction, page, selection):
    if interaction.guild_id is None or not interaction.permissions.administrator:
        raise PermissionError
    guild = interaction.guild_id
    rules = await stock.rules_for(guild)
    if selection is not None:
        row = await stock.available(guild, selection)
        if row is not None:
            return depot_embed(selection, row, rules), DepotActions(interaction, page, selection)
    overview = await stock.stock_overview(guild)
    pages = max(1, (overview["depots"]+2)//3)
    page = min(max(1, page), pages)
    rows = await stock.stock_page(guild, page)
    embed = discord.Embed(title="Strategic reserves", colour=discord.Colour.teal())
    if rows:
        if selection is not None:
            embed.description = "That depot is no longer on file."
        embed.add_field(name="All depots combined", value=inventory(overview, rules), inline=False)
        for row in rows:
            embed.add_field(name=clip(label(row["location"]), 256), value=inventory(row, rules), inline=False)
    else:
        embed.description = "No depots recorded. Use `/war stock-set` to enter the first stock count."
    embed.set_footer(text=f"{overview['depots']:,} depots · Page {page}/{pages}")
    return embed, ReportView(interaction, depot_report, page, pages, prompt="Inspect a depot",
                             choices=[(r["location"], r["location"]) for r in rows])


async def depot_choices(interaction, current):
    if interaction.guild_id is None or not interaction.permissions.administrator:
        return []
    try:
        async with asyncio.timeout(2):
            rows = await stock.matching_locations(interaction.guild_id, current)
    except Exception as exc:
        logging.getLogger(__name__).warning("Depot suggestions unavailable (%s)", type(exc).__name__)
        return []
    return [app_commands.Choice(name=r["location"], value=r["location"]) for r in rows
            if len(r["location"].encode("utf-16-le")) <= 200]


route_options = app_commands.describe(
    travel_minutes="One-way depot-to-objective travel in minutes; omit to skip timing",
    mode="Transport mode; chest minecarts send all cargo in one rail dispatch",
    carriers="Donkeys or chest boats travelling together (default 1); omit for minecarts",
)


depot_options = app_commands.describe(
    location="Depot name or coordinates",
    hay="Hay quantity, e.g. 2 single chests + 3 stacks + 5",
    diamonds="Diamonds, e.g. 30, 4 stacks, 2 blocks + 3, or 1 chest of diamond blocks",
    iron="Iron ingots, e.g. 2 blocks + 5 ingots, 1 stack, or 1 chest of blocks",
    arrows="Arrows for inventory only, e.g. 2 stacks + 4",
    logs="Logs for crafting axe handles, e.g. 3, 2 stacks, or 1 double chest",
)


async def depot_counts(guild_id, **entries):
    rules = await stock.rules_for(guild_id)
    counts = {key: stock_quantity(text, rules, iron=key == "iron", diamonds=key == "diamonds")
              for key, text in entries.items() if text is not None}
    return counts, rules


async def adjust_depot(guild, location, withdraw, entries):
    counts, rules = await depot_counts(guild, **entries)
    deltas = {k: -n if withdraw else n for k, n in counts.items()}
    balance = await stock.adjust_stock(guild, location, **deltas)
    changed = " and ".join(quantity(n, k, rules) for k, n in counts.items() if n)
    return depot_embed(balance["location"], balance, rules, f"{'Withdrew' if withdraw else 'Added'} {changed}.")


class DepotActions(ReportView):
    def __init__(self, interaction, page, location):
        super().__init__(interaction, depot_report, page, 1, selection=location)

    async def can_act(self, interaction):
        if not await self.interaction_check(interaction):
            return False
        if not interaction.permissions.administrator:
            await interaction.response.send_message("Only Discord administrators can manage depots or plan shipments.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="Add supplies", style=discord.ButtonStyle.success, row=0)
    async def add(self, interaction, button):
        if await self.can_act(interaction):
            await interaction.response.send_modal(StockForm(self, withdraw=False))

    @discord.ui.button(label="Withdraw supplies", row=0)
    async def withdraw(self, interaction, button):
        if await self.can_act(interaction):
            await interaction.response.send_modal(StockForm(self, withdraw=True))

    @discord.ui.button(label="Plan transport", style=discord.ButtonStyle.primary, row=0)
    async def transport(self, interaction, button):
        await self.open_plan(interaction)

    @discord.ui.button(label="Conquest estimate", row=0)
    async def conquest(self, interaction, button):
        if await self.can_act(interaction):
            await interaction.response.send_modal(ConquestForm(self))

    async def open_plan(self, interaction, objective=None):
        if not await self.can_act(interaction):
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            embed, view = await load_plan(interaction, self.selection, Route(), objective)
        except ValueError as exc:
            return await reply(interaction, str(exc))
        await interaction.edit_original_response(embed=embed, view=view, allowed_mentions=NO_MENTIONS)


class StockForm(discord.ui.Modal):
    def __init__(self, depot, *, withdraw):
        verb = "Withdraw" if withdraw else "Add"
        super().__init__(title=clip(f"{verb} supplies · {depot.selection}", 45), timeout=300)
        self.depot, self.withdraw = depot, withdraw
        examples = {"hay": "2 single chests + 3 stacks", "diamonds": "1 chest of diamond blocks",
                    "iron": "2 blocks + 5 ingots", "arrows": "2 stacks + 4", "logs": "1 double chest"}
        self.fields = {}
        for resource in stock.RESOURCES:
            field = discord.ui.TextInput(label=resource.title(), placeholder=examples[resource], required=False, max_length=100)
            self.fields[resource] = field
            self.add_item(field)

    async def on_submit(self, interaction):
        depot = self.depot
        if not await depot.can_act(interaction):
            return
        depot.busy = True
        try:
            await interaction.response.defer()
            entries = {key: field.value.strip() for key, field in self.fields.items() if field.value.strip()}
            try:
                embed = await adjust_depot(depot.guild, depot.selection, self.withdraw, entries)
            except ValueError as exc:
                return await reply(interaction, str(exc))
            # Retire every form opened from the old view before sending the receipt.
            depot.stop()
            view = DepotActions(interaction, depot.page, depot.selection)
            try:
                await interaction.edit_original_response(content=None, embed=embed, view=view, allowed_mentions=NO_MENTIONS)
            except (discord.HTTPException, OSError, TimeoutError):
                view.stop()
                await interaction.followup.send(embed=embed, ephemeral=True, allowed_mentions=NO_MENTIONS)
        finally:
            depot.busy = False

    async def on_error(self, interaction, error):
        self.depot.stop()
        logging.getLogger(__name__).error("Depot stock form failed", exc_info=error)
        send = interaction.followup.send if interaction.response.is_done() else interaction.response.send_message
        await send("Reopen /war stock-list and check the depot balance before entering the change again.", ephemeral=True)


class ConquestForm(discord.ui.Modal, title="Estimate a conquest"):
    hp = discord.ui.TextInput(label="Remaining settlement HP", max_length=10)
    players = discord.ui.TextInput(label="Players", max_length=7)

    def __init__(self, depot):
        super().__init__(timeout=300)
        self.depot = depot

    async def on_submit(self, interaction):
        if not await self.depot.can_act(interaction):
            return
        try:
            hp = whole_number(self.hp.value, "settlement HP", 2_000_000_000)
            players = whole_number(self.players.value, "players", 1_000_000)
        except ValueError as exc:
            return await interaction.response.send_message(str(exc), ephemeral=True)
        await self.depot.open_plan(interaction, (hp, players))


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

    @app_commands.command(name="stock-set", description="Set the specified resource balances at a depot; omitted resources keep their counts")
    @depot_options
    @app_commands.autocomplete(location=depot_choices)
    async def stock_set(self, interaction: discord.Interaction, location: Location, hay: Quantity = None,
                        diamonds: Quantity = None, iron: Quantity = None, arrows: Quantity = None, logs: Quantity = None):
        try:
            counts, rules = await depot_counts(interaction.guild_id, hay=hay, diamonds=diamonds, iron=iron, arrows=arrows, logs=logs)
            row = await stock.set_stock(interaction.guild_id, location, **counts)
        except ValueError as exc:
            return await reply(interaction, str(exc))
        await send_report(interaction, depot_embed(row["location"], row, rules, "Stock count recorded."),
                          view=DepotActions(interaction, 1, row["location"]))

    @app_commands.command(name="stock-add", description="Add a delivery of resources to an existing depot")
    @depot_options
    @app_commands.autocomplete(location=depot_choices)
    async def stock_add(self, interaction: discord.Interaction, location: Location,
                        hay: Quantity = None, diamonds: Quantity = None, iron: Quantity = None, arrows: Quantity = None,
                        logs: Quantity = None):
        await self._adjust(interaction, location, withdraw=False, hay=hay, diamonds=diamonds, iron=iron, arrows=arrows, logs=logs)

    @app_commands.command(name="stock-subtract", description="Withdraw resources from an existing depot")
    @depot_options
    @app_commands.autocomplete(location=depot_choices)
    async def stock_subtract(self, interaction: discord.Interaction, location: Location,
                             hay: Quantity = None, diamonds: Quantity = None, iron: Quantity = None, arrows: Quantity = None,
                             logs: Quantity = None):
        await self._adjust(interaction, location, withdraw=True, hay=hay, diamonds=diamonds, iron=iron, arrows=arrows, logs=logs)

    async def _adjust(self, interaction, location, *, withdraw, **entries):
        try:
            embed = await adjust_depot(interaction.guild_id, location, withdraw, entries)
        except ValueError as exc:
            return await reply(interaction, str(exc))
        await send_report(interaction, embed, view=DepotActions(interaction, 1, location))

    @app_commands.command(name="stock-remove", description="Remove one location and its stock from this server's reserves")
    @app_commands.autocomplete(location=depot_choices)
    async def stock_remove(self, interaction: discord.Interaction, location: Location):
        try:
            await stock.remove_stock(interaction.guild_id, location)
        except ValueError as exc:
            return await reply(interaction, str(exc))
        await reply(interaction, f"No reserves are recorded at **{label(location)}**.")

    @app_commands.command(name="stock-list", description="Show reserve locations and their current balances")
    @app_commands.describe(location="Show only this depot or settlement; omit to show paginated reserves")
    @app_commands.autocomplete(location=depot_choices)
    async def stock_list(self, interaction: discord.Interaction, page: PositiveCount = 1, location: Location = None):
        if location is not None:
            try:
                location = stock.clean_location(location)
            except ValueError as exc:
                return await reply(interaction, str(exc))
        embed, view = await depot_report(interaction, page, location)
        if location is not None and view.selection is None:
            view.stop()
            return await reply(interaction, "No such reserve location on this server. See /war stock-list.")
        await send_report(interaction, embed, view=view)

    @app_commands.command(description="Estimate transport loads and chest space for the supplies in stock")
    @app_commands.describe(location="Depot to pack; omit to estimate all supplies consolidated in one place")
    @route_options
    @app_commands.autocomplete(location=depot_choices)
    async def logistics(self, interaction: discord.Interaction, location: Location = None,
                        travel_minutes: Minutes = None, mode: Mode = "donkey", carriers: PositiveCount = 1):
        try:
            embed, view = await load_plan(interaction, location, Route(mode, carriers, travel_minutes))
        except ValueError as exc:
            return await reply(interaction, str(exc))
        await send_report(interaction, embed, view=view)

    @app_commands.command(description="Estimate transferable defence from iron; optionally check a target amount")
    @app_commands.describe(location="Use this depot; omit to pool all iron on this server",
                           points="Defence points you want to transfer")
    @app_commands.autocomplete(location=depot_choices)
    async def defence(self, interaction: discord.Interaction, points: app_commands.Range[int, 1, 2**53 - 1] = None,
                      location: Location = None):
        try:
            held = await stock.available(interaction.guild_id, location)
        except ValueError as exc:
            return await reply(interaction, str(exc))
        if held is None:
            return await reply(interaction, "No such reserve location on this server. See /war stock-list.")
        rules = await stock.rules_for(interaction.guild_id)
        plan = defence_transfer(held["iron"], rules, points or 0)
        scope = label(location) if location is not None else "all depots, if pooled"
        public = (f"**Assumptions**\n{IRON_PER_BLOCK} iron ingots per block; "
                  f"{rules.defence_per_block:,} defence per whole iron block. Diamond blocks are accounting only.\n"
                  "Estimate only; no stock deducted.")
        embed = discord.Embed(title="Defence transfer", description=public, colour=discord.Colour.teal())
        embed.add_field(name=f"Transferable defence: {plan.capacity:,} points",
                        value=f"{scope}\n{quantity(held['iron'], 'iron', rules)}", inline=False)
        if points is not None:
            verdict = f"short {plan.missing_ingots:,} ingots" if plan.missing_ingots else "enough iron"
            embed.colour = discord.Colour.orange() if plan.missing_ingots else discord.Colour.green()
            embed.add_field(name=f"Target · {points:,} points",
                            value=f"{plan.required_blocks:,} blocks ({plan.required_blocks * IRON_PER_BLOCK:,} ingots) · **{verdict}**",
                            inline=False)
            public += (f"\n**Defence target: {points:,} points**\nRequired: "
                       f"{quantity(plan.required_blocks * IRON_PER_BLOCK, 'iron', rules)}.")
        await send_report(interaction, embed, share_text=public if points is not None else None)

    @app_commands.command(description="Show this server's war rules, or update only the options you supply")
    @app_commands.describe(
        diamonds_per_axe="Diamonds needed to craft one axe", axe_durability="Hits before a fresh axe breaks",
        damage_per_hit="Settlement HP removed per hit", rehit_seconds="Seconds between consecutive hits",
        bread_per_hay="Loaves crafted from one hay bale", hunger_per_bread="Hunger points restored per loaf",
        hits_per_hunger_loss="Hits per attacker before each hunger charge",
        hunger_loss="Hunger points lost per charge (0 disables food requirements)",
        stack_size="Items per stack for depot input and packing (maximum 64)",
        defence_per_block="Defence transferable per full iron block (9 ingots)",
        donkey_slots="Stacks per donkey load", chest_boat_slots="Stacks per chest-boat load",
        chest_minecart_slots="Stacks per chest minecart (default 27); minecart count is calculated",
        chest_slots="Slots per single chest", double_chest_slots="Slots per double chest",
        loading_minutes="Minutes to load one convoy at the depot per outbound trip (default 10)",
        unloading_minutes="Minutes to unload one convoy at the objective per outbound trip (default 10)",
        sticks_per_axe="Sticks required for one diamond axe (default 2)",
        sticks_per_batch="Sticks crafted per batch (default 4)",
        planks_per_batch="Planks consumed per stick-crafting batch (default 2)",
        planks_per_log="Planks crafted per log (default 4)",
    )
    async def config(self, interaction: discord.Interaction, diamonds_per_axe: PositiveCount = None,
                     axe_durability: PositiveCount = None, damage_per_hit: Rate = None,
                     rehit_seconds: Rate = None, bread_per_hay: PositiveCount = None,
                     hunger_per_bread: Rate = None, hits_per_hunger_loss: PositiveCount = None,
                     hunger_loss: app_commands.Range[float, 0.0, 1_000_000.0] = None,
                     stack_size: app_commands.Range[int, 1, 64] = None, donkey_slots: PositiveCount = None,
                     chest_boat_slots: PositiveCount = None, chest_slots: PositiveCount = None,
                     double_chest_slots: PositiveCount = None, defence_per_block: PositiveCount = None,
                     loading_minutes: Minutes = None, unloading_minutes: Minutes = None,
                     sticks_per_axe: PositiveCount = None, sticks_per_batch: PositiveCount = None,
                     planks_per_batch: PositiveCount = None, planks_per_log: PositiveCount = None,
                     chest_minecart_slots: PositiveCount = None):
        options = dict(diamonds_per_axe=diamonds_per_axe, axe_durability=axe_durability,
                       damage_per_hit=damage_per_hit, rehit_seconds=rehit_seconds,
                       bread_per_hay=bread_per_hay, hunger_per_bread=hunger_per_bread,
                       hits_per_hunger_loss=hits_per_hunger_loss, hunger_loss=hunger_loss, stack_size=stack_size,
                       donkey_slots=donkey_slots, chest_boat_slots=chest_boat_slots,
                       chest_slots=chest_slots, double_chest_slots=double_chest_slots, defence_per_block=defence_per_block,
                       loading_minutes=loading_minutes, unloading_minutes=unloading_minutes,
                       sticks_per_axe=sticks_per_axe, sticks_per_batch=sticks_per_batch,
                       planks_per_batch=planks_per_batch, planks_per_log=planks_per_log,
                       chest_minecart_slots=chest_minecart_slots)
        try:
            rules = await stock.configure(interaction.guild_id, {k: v for k, v in options.items() if v is not None})
        except ValueError as exc:
            return await reply(interaction, str(exc))
        embed = discord.Embed(title="Military rules", colour=discord.Colour.teal())
        changed = [key for key, value in options.items() if value is not None]
        if changed:
            embed.description = "Updated " + ", ".join(f"`{key}`" for key in changed) + "."
        embed.add_field(name="Diamond axes", value=f"{rules.diamonds_per_axe} diamonds + {rules.sticks_per_axe} sticks per axe\n"
                        f"{rules.axe_durability} hits · {rules.damage_per_hit} HP/hit · {rules.rehit_seconds}s between hits")
        embed.add_field(name="Wood", value=f"{rules.planks_per_log} planks/log\n{rules.sticks_per_batch} sticks per {rules.planks_per_batch} planks")
        embed.add_field(name="Food", value=f"{rules.bread_per_hay} bread/hay · {rules.hunger_per_bread} hunger/bread\n"
                        f"{rules.hunger_loss} hunger lost per {rules.hits_per_hunger_loss} hits")
        embed.add_field(name="Transport and storage", value=f"{rules.stack_size} items/stack\nSlots per carrier · donkey {rules.donkey_slots}, "
                        f"chest boat {rules.chest_boat_slots}, chest minecart {rules.chest_minecart_slots}\n"
                        f"Slots per chest · single {rules.chest_slots}, double {rules.double_chest_slots}", inline=False)
        embed.add_field(name="Iron defence", value=f"{rules.defence_per_block:,} points/block · {IRON_PER_BLOCK} ingots/block")
        embed.add_field(name="Handling per trip", value=f"Load {duration(rules.loading_minutes * 60)} · unload {duration(rules.unloading_minutes * 60)}")
        await send_report(interaction, embed)

    @app_commands.command(description="Estimate the time and check whether recorded supplies cover a settlement assault")
    @app_commands.describe(hp="Remaining settlement HP", players="Number of simultaneous attackers",
                           location="Use only this location; omit to pool all reserves")
    @route_options
    @app_commands.autocomplete(location=depot_choices)
    async def conquerable(self, interaction: discord.Interaction, hp: app_commands.Range[int, 1, 2_000_000_000],
                          players: PositiveCount, location: Location = None,
                          travel_minutes: Minutes = None, mode: Mode = "donkey", carriers: PositiveCount = 1):
        try:
            embed, view = await load_plan(interaction, location, Route(mode, carriers, travel_minutes), (hp, players))
        except ValueError as exc:
            return await reply(interaction, str(exc))
        await send_report(interaction, embed, view=view)


async def setup(bot: commands.Bot):
    await bot.add_cog(WarCog())
