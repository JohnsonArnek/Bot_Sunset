"""Private transport and conquest reports for the Minecraft war planner."""

from dataclasses import dataclass, replace
from decimal import Decimal

import discord

import war_store as stock
from cogs.report_view import NO_MENTIONS, clip
from cogs.war_share import ShareEstimate
from war_display import assumptions, duration, quantity
from war_planner import block_shipment, delivery_time, estimate, pack, plan_transport


def label(location):
    return discord.utils.escape_markdown(stock.clean_location(location))


@dataclass(frozen=True)
class Route:
    """Keep the donkey/boat fleet size when comparing with a rail dispatch."""
    mode: str = "donkey"
    fleet: int = 1
    minutes: float | None = None

    @property
    def carriers(self):
        return 1 if self.mode == "chest_minecart" else self.fleet


@dataclass(frozen=True)
class Shipment:
    location: str | None
    hay: int
    diamonds: int
    logs: int
    iron: int = 0
    combat: Decimal | None = None


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
    return (f"**Packing** · {rules.stack_size}/stack · slots per donkey {rules.donkey_slots}, "
            f"chest boat {rules.chest_boat_slots}, chest minecart {rules.chest_minecart_slots}, "
            f"chest {rules.chest_slots}, double chest {rules.double_chest_slots}. "
            "Diamonds and iron travel in blocks with separate slots for loose items.\n"
            f"**Each trip** · load {duration(rules.loading_minutes * 60)} + unload "
            f"{duration(rules.unloading_minutes * 60)} for the convoy. Supplies start together. Travel time excludes crafting.")


def storage(cargo):
    return (f"{units(cargo.single_chests, 'single chest', 'single chests')} or "
            f"{units(cargo.double_chests, 'double chest', 'double chests')}")


def route_summary(rules, travel_minutes, journey, *, combat=None):
    if journey.trips == 0:
        return "No cargo to deliver: 0m loading, travel or unloading."
    if travel_minutes is None:
        return "One-way travel time has not been entered."
    trips = journey.trips
    route = delivery_time(rules, travel_minutes, trips)
    lines = [f"**Delivery: {duration(route.seconds)}** · {units(trips, 'outbound trip', 'outbound trips')}, "
             f"{units(trips - 1, 'return', 'returns')} · {duration(Decimal(str(travel_minutes)) * 60)} one way",
             f"Load {duration(route.loading)} + travel {duration(route.travel)} + unload {duration(route.unloading)}."]
    if combat is not None:
        lines.append(f"**Depot to conquest if supplied: {duration(route.with_combat(combat))}** "
                     f"(delivery + {duration(combat)} combat).")
    return "\n".join(lines)


SHARE_FOOTER = "Estimate only. Share publishes the calculation and its assumptions. Depot names and stock checks stay private."


def transport_card(interaction, rules, shipment, route):
    cargo = pack(rules, hay=shipment.hay, diamonds=shipment.diamonds, iron=shipment.iron, logs=shipment.logs)
    journey = plan_transport(cargo, rules, route.mode, route.carriers)
    timing = route_summary(rules, route.minutes, journey, combat=shipment.combat)
    scope = label(shipment.location) if shipment.location is not None else "all depots, if pooled"
    conditions = f"**Assumptions**\n{packing_assumptions(rules)}\nArrows excluded (inventory only)."
    loads = transport(cargo, journey)
    space = storage(cargo)
    public = f"{conditions}\n**Packing estimate**\nTransport: {loads}\nStorage: {space}.\n{timing}"
    resources = ("hay", "diamonds", "logs") if shipment.combat is not None else ("hay", "diamonds", "iron", "logs")
    manifest = "\n".join(f"{k.title()}: {quantity(getattr(shipment, k), k, rules)}" for k in resources)
    title = "Conquest shipment" if shipment.combat is not None else "Transport estimate"
    embed = discord.Embed(title=title, description=conditions, colour=discord.Colour.teal())
    embed.add_field(name="Transport", value=loads, inline=False)
    embed.add_field(name="Storage", value=space, inline=False)
    embed.add_field(name="Journey time", value=timing, inline=False)
    embed.add_field(name=clip(f"{'Required cargo' if shipment.combat is not None else 'Private manifest'} · {scope}", 256), value=manifest, inline=False)
    embed.set_footer(text=SHARE_FOOTER)
    return embed, TransportView(interaction, public, rules, shipment, route)


def conquest_card(interaction, rules, held, location, hp, players, route):
    plan = estimate(hp, players, rules)
    shipped_diamonds = block_shipment(plan.diamonds)
    raw = pack(rules, hay=plan.hay, diamonds=shipped_diamonds, logs=plan.logs)
    journey = plan_transport(raw, rules, route.mode, route.carriers)
    timing_summary = route_summary(rules, route.minutes, journey, combat=plan.seconds)
    enough = (held["diamonds"] >= shipped_diamonds and held["hay"] >= plan.hay and held["logs"] >= plan.logs)
    verdict = "Enough supplies" if enough else "Insufficient supplies"
    scope = label(location) if location is not None else "all reserve locations"
    conditions = f"{assumptions(rules)}\n{packing_assumptions(rules)}"
    title = f"Conquest estimate · {hp:,} HP · {players:,} players"
    timing = (f"**Time if equipped: {duration(plan.seconds)}** from first hit to last.\n"
              f"{plan.attackers:,} active attackers · {plan.hits:,} hits across {plan.volleys:,} volleys")
    manifest = (f"Diamonds: {quantity(shipped_diamonds, 'diamonds', rules)} "
                f"({plan.diamonds:,} used for {plan.axes:,} axes, {shipped_diamonds - plan.diamonds} spare diamonds)\n"
                f"{quantity(plan.logs, 'logs', rules)} ({plan.planks:,} planks consumed, {plan.sticks:,} sticks needed)\n"
                f"{quantity(plan.hay, 'hay', rules)} → {quantity(plan.bread, 'bread', rules)} needed\n"
                "Craft axes and bread at the objective.")
    loads = f"{transport(raw, journey)}\nStorage: {storage(raw)}."
    public = f"{conditions}\n**{title}**\n{timing}\n**Shipment**\n{manifest}\n**Transport and storage**\n{loads}\n{timing_summary}"
    private = "\n".join(supply(k, needed, held[k], rules) for k, needed in
                        (("diamonds", shipped_diamonds), ("hay", plan.hay), ("logs", plan.logs)))
    embed = discord.Embed(title=title, description=conditions,
                          colour=discord.Colour.green() if enough else discord.Colour.orange())
    embed.add_field(name=f"Private stock check · {verdict}", value=f"{scope}\n{private}", inline=False)
    embed.add_field(name="Combat time", value=timing, inline=False)
    embed.add_field(name="Shipment", value=manifest, inline=False)
    embed.add_field(name="Transport and storage", value=loads, inline=False)
    embed.add_field(name="Journey time", value=timing_summary, inline=False)
    shipment = Shipment(location, plan.hay, shipped_diamonds, plan.logs, combat=plan.seconds)
    embed.set_footer(text=SHARE_FOOTER)
    return embed, ConquestView(interaction, public, rules, shipment, route)


async def load_plan(interaction, location, route, objective=None):
    if route.mode == "chest_minecart" and route.fleet != 1:
        raise ValueError("Minecart mode supplies enough carts for one dispatch. Omit carriers.")
    held = await stock.available(interaction.guild_id, location)
    if held is None:
        raise ValueError("No such reserve location on this server. See /war stock-list.")
    rules = await stock.rules_for(interaction.guild_id)
    if objective is not None:
        return conquest_card(interaction, rules, held, location, *objective, route)
    shipment = Shipment(location, held["hay"], held["diamonds"], held["logs"], held["iron"])
    return transport_card(interaction, rules, shipment, route)


def whole_number(raw, label, maximum):
    text = raw.strip()
    if not text.isascii() or not text.isdecimal() or not 1 <= int(text) <= maximum:
        raise ValueError(f"Enter {label} as a whole number from 1 to {maximum:,}.")
    return int(text)


class ShipmentView(ShareEstimate):
    def __init__(self, interaction, public, rules, shipment, route):
        super().__init__(interaction, public)
        self.rules, self.shipment, self.route = rules, shipment, route


class ConquestView(ShipmentView):
    @discord.ui.button(label="Plan this shipment", style=discord.ButtonStyle.primary, row=1)
    async def plan(self, interaction, button):
        if not await self.interaction_check(interaction):
            return
        embed, view = transport_card(interaction, self.rules, self.shipment, self.route)
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True, allowed_mentions=NO_MENTIONS)


class TransportView(ShipmentView):
    def __init__(self, interaction, public, rules, shipment, route):
        super().__init__(interaction, public, rules, shipment, route)
        for option in self.mode.options:
            option.default = option.value == route.mode

    @discord.ui.select(placeholder="Change transport", row=1, options=[
        discord.SelectOption(label="Donkey", value="donkey"),
        discord.SelectOption(label="Chest boat", value="chest_boat"),
        discord.SelectOption(label="Chest minecart", value="chest_minecart")])
    async def mode(self, interaction, select):
        await self.change(interaction, replace(self.route, mode=select.values[0]))

    @discord.ui.button(label="Edit journey", style=discord.ButtonStyle.primary, row=2)
    async def journey(self, interaction, button):
        if await self.interaction_check(interaction):
            await interaction.response.send_modal(JourneyForm(self))

    async def change(self, interaction, route):
        if not await self.interaction_check(interaction):
            return
        if route == self.route:
            await interaction.response.defer()
            return
        self.busy = True
        try:
            embed, view = transport_card(interaction, self.rules, self.shipment, route)
            await interaction.response.edit_message(embed=embed, view=view, allowed_mentions=NO_MENTIONS)
            self.stop()
        finally:
            self.busy = False


class JourneyForm(discord.ui.Modal, title="Plan the journey"):
    def __init__(self, view):
        super().__init__(timeout=300)
        self.report = view
        route = view.route
        self.minutes = discord.ui.TextInput(label="One-way travel time in minutes", max_length=32, required=False,
                                           default="" if route.minutes is None else str(route.minutes), placeholder="e.g. 12.5")
        self.add_item(self.minutes)
        self.fleet = None
        if route.mode != "chest_minecart":
            label = "Donkeys travelling together" if route.mode == "donkey" else "Chest boats travelling together"
            self.fleet = discord.ui.TextInput(label=label, max_length=7, default=str(route.fleet))
            self.add_item(self.fleet)

    async def on_submit(self, interaction):
        if not await self.report.interaction_check(interaction):
            return
        raw = self.minutes.value.strip()
        try:
            minutes = float(raw) if raw else None
            if minutes is not None and not 0 <= minutes <= 1_000_000:
                raise ValueError
        except ValueError:
            return await interaction.response.send_message("Enter one-way travel time between 0 and 1,000,000 minutes.", ephemeral=True)
        try:
            fleet = self.report.route.fleet if self.fleet is None else whole_number(self.fleet.value, "the carrier count", 1_000_000)
        except ValueError as exc:
            return await interaction.response.send_message(str(exc), ephemeral=True)
        await self.report.change(interaction, replace(self.report.route, fleet=fleet, minutes=minutes))
