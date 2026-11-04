"""Administrator-only strategic reserves and settlement assault planning."""

from decimal import Decimal
from typing import Literal

import discord
from discord import app_commands
from discord.ext import commands

import war_store as stock
from cogs.war_share import ShareEstimate, message_payload
from war_display import assumptions, duration, inventory, quantity
from war_planner import IRON_PER_BLOCK, block_shipment, defence_transfer, delivery_time, estimate, pack, plan_transport, stock_quantity


Quantity = app_commands.Range[str, 1, 100]
PositiveCount = app_commands.Range[int, 1, 1_000_000]
Rate = app_commands.Range[float, 0.001, 1_000_000.0]
Minutes = app_commands.Range[float, 0.0, 1_000_000.0]
Location = app_commands.Range[str, 1, 64]
Mode = Literal["donkey", "chest_boat", "chest_minecart"]


async def reply(interaction, text, *, share_text=None):
    options = {}
    if share_text is not None:
        text += "\n\nShare publishes this estimate and its assumptions. Private stock checks and manifests are excluded."
        options["view"] = ShareEstimate(interaction, share_text)
    payload = message_payload(text)
    # Keep short replies as text; longer estimates use Discord's larger embed limit.
    content = payload.pop("content", None)
    await interaction.followup.send(content, **payload, **options, ephemeral=True,
                                    allowed_mentions=discord.AllowedMentions.none())


def label(location):
    return discord.utils.escape_markdown(stock.clean_location(location))


def supply(resource, needed, held, rules):
    balance = held - needed
    detail = (f"Left: {quantity(balance, resource, rules)}" if balance >= 0
              else f"Short: {quantity(-balance, resource, rules)}")
    return f"{resource.title()} available: {quantity(held, resource, rules)} · {detail}"


def units(n, singular, plural):
    return f"{n:,} {singular if n == 1 else plural}"


def transport(cargo, journey):
    if journey.mode == "chest_minecart":
        return (f"{units(cargo.slots, 'slot', 'slots')} · "
                f"{units(journey.carriers, 'chest minecart', 'chest minecarts')} needed · "
                f"{units(journey.trips, 'rail dispatch', 'rail dispatches')}. "
                "Prepared rail route and enough carts assumed; all cargo sent together, no return trips.")
    single, plural = {"donkey": ("donkey", "donkeys"), "chest_boat": ("chest boat", "chest boats")}[journey.mode]
    return (f"{units(cargo.slots, 'slot', 'slots')} · {units(journey.carriers, single, plural)} "
            f"({journey.slots_per_carrier} slots each) · {journey.loads:,} carrier loads · "
            f"**{units(journey.trips, 'outbound trip', 'outbound trips')}**, "
            f"{units(max(0, journey.trips - 1), 'return', 'returns')}.")


def packing_assumptions(rules):
    return (f"Packing: {rules.stack_size}/stack; slots per donkey {rules.donkey_slots}, "
            f"chest boat {rules.chest_boat_slots}, chest {rules.chest_slots}, "
            f"double chest {rules.double_chest_slots}, chest minecart {rules.chest_minecart_slots}. "
            "Diamonds/iron compact into blocks; loose remainders use separate slots.\n"
            f"Each trip: {duration(rules.loading_minutes * 60)} loading + "
            f"{duration(rules.unloading_minutes * 60)} unloading for the whole convoy. "
            "Supplies pooled at departure; crafting time excluded.")


def storage(cargo):
    return (f"{units(cargo.single_chests, 'single chest', 'single chests')} or "
            f"{units(cargo.double_chests, 'double chest', 'double chests')}")


def route_summary(rules, travel_minutes, journey, *, combat=None):
    if journey.trips == 0:
        return "No cargo to deliver: 0m loading, travel or unloading."
    if travel_minutes is None:
        return "Add travel_minutes for depot-to-objective timing."
    trips = journey.trips
    route = delivery_time(rules, travel_minutes, trips)
    lines = [f"**Delivery: {duration(route.seconds)}** · {units(trips, 'outbound trip', 'outbound trips')}, "
             f"{units(trips - 1, 'return', 'returns')} · {duration(Decimal(str(travel_minutes)) * 60)} one way",
             f"Load {duration(route.loading)} + travel {duration(route.travel)} + unload {duration(route.unloading)}."]
    if combat is not None:
        lines.append(f"**Depot to conquest if supplied: {duration(route.with_combat(combat))}** "
                     f"(delivery + {duration(combat)} combat).")
    return "\n".join(lines)


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
    return {key: stock_quantity(text, rules, iron=key == "iron", diamonds=key == "diamonds")
            for key, text in entries.items() if text is not None}


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
    async def stock_set(self, interaction: discord.Interaction, location: Location, hay: Quantity = None,
                        diamonds: Quantity = None, iron: Quantity = None, arrows: Quantity = None, logs: Quantity = None):
        try:
            counts = await depot_counts(interaction.guild_id, hay=hay, diamonds=diamonds, iron=iron, arrows=arrows, logs=logs)
            row = await stock.set_stock(interaction.guild_id, location, **counts)
        except ValueError as exc:
            return await reply(interaction, str(exc))
        rules = await stock.rules_for(interaction.guild_id)
        await reply(interaction, f"**{label(row['location'])}** now holds:\n{inventory(row, rules)}")

    @app_commands.command(name="stock-add", description="Add a delivery of resources to an existing depot")
    @depot_options
    async def stock_add(self, interaction: discord.Interaction, location: Location,
                        hay: Quantity = None, diamonds: Quantity = None, iron: Quantity = None, arrows: Quantity = None,
                        logs: Quantity = None):
        await self._adjust(interaction, location, withdraw=False, hay=hay, diamonds=diamonds, iron=iron, arrows=arrows, logs=logs)

    @app_commands.command(name="stock-subtract", description="Withdraw resources from an existing depot")
    @depot_options
    async def stock_subtract(self, interaction: discord.Interaction, location: Location,
                             hay: Quantity = None, diamonds: Quantity = None, iron: Quantity = None, arrows: Quantity = None,
                             logs: Quantity = None):
        await self._adjust(interaction, location, withdraw=True, hay=hay, diamonds=diamonds, iron=iron, arrows=arrows, logs=logs)

    async def _adjust(self, interaction, location, *, withdraw, **entries):
        try:
            counts = await depot_counts(interaction.guild_id, **entries)
            sign = -1 if withdraw else 1
            balance = await stock.adjust_stock(interaction.guild_id, location, **{k: sign * n for k, n in counts.items()})
        except ValueError as exc:
            return await reply(interaction, str(exc))
        rules = await stock.rules_for(interaction.guild_id)
        changed = " and ".join(quantity(n, k, rules) for k, n in counts.items() if n)
        await reply(interaction, f"{'Withdrew' if withdraw else 'Added'} {changed}. "
                    f"**{label(balance['location'])}** now holds:\n{inventory(balance, rules)}")

    @app_commands.command(name="stock-remove", description="Remove one location and its stock from this server's reserves")
    async def stock_remove(self, interaction: discord.Interaction, location: Location):
        try:
            await stock.remove_stock(interaction.guild_id, location)
        except ValueError as exc:
            return await reply(interaction, str(exc))
        await reply(interaction, f"No reserves are recorded at **{label(location)}**.")

    @app_commands.command(name="stock-list", description="Show reserve locations and their current balances")
    @app_commands.describe(location="Show only this depot or settlement; omit to show paginated reserves")
    async def stock_list(self, interaction: discord.Interaction, page: PositiveCount = 1, location: Location = None):
        rules = await stock.rules_for(interaction.guild_id)
        if location is not None:
            try:
                row = await stock.available(interaction.guild_id, location)
            except ValueError as exc:
                return await reply(interaction, str(exc))
            if row is None:
                return await reply(interaction, "No such reserve location on this server. See /war stock-list.")
            return await reply(interaction, f"**{label(location)}**\n{inventory(row, rules)}")
        rows = await stock.stock_page(interaction.guild_id, page)
        if not rows:
            return await reply(interaction, "No locations on this page. Use /war stock-set to record supplies.")
        totals = await stock.available(interaction.guild_id)
        lines = [f"**All reserves:**\n{inventory(totals, rules)}", f"Locations · page {page} (3 per page)"]
        lines.extend(f"**{label(r['location'])}**\n{inventory(r, rules)}" for r in rows)
        lines.append("Use /war logistics for transport and chest space, or /war defence for iron transfer capacity.")
        await reply(interaction, "\n".join(lines))

    @app_commands.command(description="Estimate transport loads and chest space for the supplies in stock")
    @app_commands.describe(location="Depot to pack; omit to estimate all supplies consolidated in one place")
    @route_options
    async def logistics(self, interaction: discord.Interaction, location: Location = None,
                        travel_minutes: Minutes = None, mode: Mode = "donkey", carriers: PositiveCount = 1):
        try:
            held = await stock.available(interaction.guild_id, location)
        except ValueError as exc:
            return await reply(interaction, str(exc))
        if held is None:
            return await reply(interaction, "No such reserve location on this server. See /war stock-list.")
        rules = await stock.rules_for(interaction.guild_id)
        cargo = pack(rules, hay=held["hay"], diamonds=held["diamonds"], iron=held["iron"], logs=held["logs"])
        try:
            journey = plan_transport(cargo, rules, mode, carriers)
            route = route_summary(rules, travel_minutes, journey)
        except ValueError as exc:
            return await reply(interaction, str(exc))
        scope = label(location) if location is not None else "all depots, if pooled"
        public = (f"**Assumptions**\n{packing_assumptions(rules)}\n"
                  "Arrows excluded (inventory only).\n"
                  f"**Packing estimate**\nTransport: {transport(cargo, journey)}\nStorage: {storage(cargo)}.\n{route}")
        manifest = "\n".join(f"{k.title()}: {quantity(held[k], k, rules)}" for k in ("hay", "diamonds", "iron", "logs"))
        await reply(interaction, public + f"\n**Private manifest: {scope}**\n{manifest}", share_text=public)

    @app_commands.command(description="Estimate transferable defence from iron; optionally check a target amount")
    @app_commands.describe(location="Use this depot; omit to pool all iron on this server",
                           points="Defence points you want to transfer")
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
        lines = [f"**Transferable defence: {plan.capacity:,} points** · {scope}",
                 quantity(held["iron"], "iron", rules)]
        if points is not None:
            verdict = f"short {plan.missing_ingots:,} ingots" if plan.missing_ingots else "enough iron"
            lines.append(f"For {points:,} defence: {plan.required_blocks:,} blocks "
                         f"({plan.required_blocks * IRON_PER_BLOCK:,} ingots) · {verdict}.")
            public += (f"\n**Defence target: {points:,} points**\nRequired: "
                       f"{quantity(plan.required_blocks * IRON_PER_BLOCK, 'iron', rules)}.")
        await reply(interaction, public + "\n**Private stock check**\n" + "\n".join(lines),
                    share_text=public if points is not None else None)

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
        await reply(interaction,
                    f"**War rules for this server**\n"
                    f"Axe: {rules.diamonds_per_axe} diamonds + {rules.sticks_per_axe} sticks · {rules.axe_durability} hits · "
                    f"{rules.damage_per_hit} HP per hit · {rules.rehit_seconds} s between hits.\n"
                    f"Wood: {rules.planks_per_log} planks per log · "
                    f"{rules.sticks_per_batch} sticks per {rules.planks_per_batch} planks.\n"
                    f"Food: {rules.bread_per_hay} bread per hay · {rules.hunger_per_bread} hunger per bread. "
                    f"Each attacker loses {rules.hunger_loss} hunger every {rules.hits_per_hunger_loss} hits.\n"
                    f"Packing: {rules.stack_size} per stack · slots per donkey: {rules.donkey_slots}, "
                    f"chest boat: {rules.chest_boat_slots}, single chest: {rules.chest_slots}, "
                    f"double chest: {rules.double_chest_slots}, chest minecart: {rules.chest_minecart_slots}.\n"
                    f"Iron: {rules.defence_per_block:,} defence per block ({IRON_PER_BLOCK} ingots).\n"
                    f"Per trip: {duration(rules.loading_minutes * 60)} loading · "
                    f"{duration(rules.unloading_minutes * 60)} unloading.")

    @app_commands.command(description="Estimate the time and check whether recorded supplies cover a settlement assault")
    @app_commands.describe(hp="Remaining settlement HP", players="Number of simultaneous attackers",
                           location="Use only this location; omit to pool all reserves")
    @route_options
    async def conquerable(self, interaction: discord.Interaction, hp: app_commands.Range[int, 1, 2_000_000_000],
                          players: PositiveCount, location: Location = None,
                          travel_minutes: Minutes = None, mode: Mode = "donkey", carriers: PositiveCount = 1):
        try:
            held = await stock.available(interaction.guild_id, location)
        except ValueError as exc:
            return await reply(interaction, str(exc))
        if held is None:
            return await reply(interaction, "No such reserve location on this server. See /war stock-list.")
        rules = await stock.rules_for(interaction.guild_id)
        plan = estimate(hp, players, rules)
        shipped_diamonds = block_shipment(plan.diamonds)
        raw = pack(rules, hay=plan.hay, diamonds=shipped_diamonds, logs=plan.logs)
        try:
            journey = plan_transport(raw, rules, mode, carriers)
            route = route_summary(rules, travel_minutes, journey, combat=plan.seconds)
        except ValueError as exc:
            return await reply(interaction, str(exc))
        enough = (held["diamonds"] >= shipped_diamonds and held["hay"] >= plan.hay and held["logs"] >= plan.logs)
        verdict = "Enough supplies" if enough else "Insufficient supplies"
        scope = label(location) if location is not None else "all reserve locations"
        public = (f"{assumptions(rules)}\n{packing_assumptions(rules)}\n"
                  f"**Conquest estimate: {hp:,} HP · {players:,} players**\n"
                  f"{plan.attackers:,} active attackers · {plan.hits:,} hits across {plan.volleys:,} volleys\n"
                  f"**Time if equipped: {duration(plan.seconds)}** from first hit to last.\n"
                  "Ship hay, logs and whole diamond blocks; craft at the objective.\n"
                  f"**Shipment**\nDiamonds: {quantity(shipped_diamonds, 'diamonds', rules)} "
                  f"({plan.diamonds:,} used for {plan.axes:,} axes; {shipped_diamonds - plan.diamonds} spare diamonds)\n"
                  f"{quantity(plan.logs, 'logs', rules)} ({plan.planks:,} planks consumed; {plan.sticks:,} sticks needed)\n"
                  f"{quantity(plan.hay, 'hay', rules)} → {quantity(plan.bread, 'bread', rules)} needed\n"
                  f"**Transport and storage**\n{transport(raw, journey)}\nStorage: {storage(raw)}.\n{route}")
        private = (f"\n**Private stock check: {verdict}** · {scope}\n"
                   f"{supply('diamonds', shipped_diamonds, held['diamonds'], rules)}\n"
                   f"{supply('hay', plan.hay, held['hay'], rules)}\n"
                   f"{supply('logs', plan.logs, held['logs'], rules)}")
        await reply(interaction, public + private, share_text=public)


async def setup(bot: commands.Bot):
    await bot.add_cog(WarCog())
