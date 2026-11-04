"""Settlement assault estimates. Quantities are for a fully supplied attack."""

from dataclasses import dataclass
from decimal import Decimal, localcontext
from math import ceil
import re


@dataclass(frozen=True)
class WarRules:
    diamonds_per_axe: int = 3
    axe_durability: int = 60
    damage_per_hit: Decimal = Decimal("4")
    rehit_seconds: Decimal = Decimal("0.5")
    bread_per_hay: int = 3
    hunger_per_bread: Decimal = Decimal("3")
    hits_per_hunger_loss: int = 5
    hunger_loss: Decimal = Decimal("0.5")
    stack_size: int = 64
    donkey_slots: int = 15
    chest_boat_slots: int = 27
    chest_minecart_slots: int = 27
    chest_slots: int = 27
    double_chest_slots: int = 54
    defence_per_block: int = 100
    loading_minutes: Decimal = Decimal("10")
    unloading_minutes: Decimal = Decimal("10")
    sticks_per_axe: int = 2
    sticks_per_batch: int = 4
    planks_per_batch: int = 2
    planks_per_log: int = 4

    def __post_init__(self):
        if self.stack_size > 64:
            raise ValueError("A stack can hold at most 64 items.")
        for key, value in vars(self).items():
            if not 0 <= value <= 1_000_000 or (value == 0 and key not in {"hunger_loss", "loading_minutes", "unloading_minutes"}):
                raise ValueError(f"{key} is outside the allowed range.")
            if isinstance(value, Decimal) and value != value.quantize(Decimal("0.001")):
                raise ValueError(f"{key} accepts at most three decimal places.")

    @classmethod
    def from_settings(cls, settings):
        return cls(**{
            key: type(default)(str(settings[key]))
            for key, default in vars(cls()).items() if key in settings
        })


def _round_up(quantity: int, capacity: int) -> int:
    return (quantity + capacity - 1) // capacity


@dataclass(frozen=True)
class DeliveryTime:
    loading: Decimal
    travel: Decimal
    unloading: Decimal

    @property
    def seconds(self) -> Decimal:
        return self.with_combat(Decimal(0))

    def with_combat(self, combat: Decimal) -> Decimal:
        with localcontext() as context:
            context.prec = 64  # Covers derived trip counts across all configurable resource limits.
            return self.loading + self.travel + self.unloading + combat


def delivery_time(rules: WarRules, travel_minutes: float, trips: int) -> DeliveryTime:
    """Seconds until the last load is unloaded; only intermediate trips return to the depot."""
    one_way = Decimal(str(travel_minutes))
    if not one_way.is_finite() or not 0 <= one_way <= 1_000_000 or trips < 1:
        raise ValueError("Enter nonnegative one-way travel minutes and at least one trip.")
    with localcontext() as context:
        context.prec = 64
        return DeliveryTime(60 * trips * rules.loading_minutes, 60 * (2 * trips - 1) * one_way,
                            60 * trips * rules.unloading_minutes)


MAX_DEPOT_ITEMS = 2_000_000_000
IRON_PER_BLOCK = 9
_STOCK_TERM = re.compile(
    r"([0-9]{1,3}(?:,[0-9]{3})+|[0-9]+)\s*"
    r"(items?|(?:iron\s+)?ingots?|(?:(?:iron|diamond)\s+)?blocks?|stacks?|(?:single\s+|double\s+)?chests?)?"
    r"(?:\s+of\s+((?:(?:iron|diamond)\s+)?blocks?))?",
    re.IGNORECASE,
)


def stock_quantity(text: str, rules: WarRules, *, iron: bool = False, diamonds: bool = False) -> int:
    """Convert full containers and loose items of one resource to an exact count."""
    total = 0
    if len(text) > 100:
        raise ValueError("Keep each quantity to 100 characters or fewer.")
    for term in text.split("+"):
        match = _STOCK_TERM.fullmatch(term.strip())
        if not match:
            raise ValueError("Use whole quantities, e.g. 64, 3 stacks, or 1 double chest + 5 items.")
        unit = " ".join((match[2] or "item").lower().split())
        packed_unit = " ".join((match[3] or "").lower().split())
        for supplied in (unit, packed_unit):
            if (supplied.startswith("iron ") and not iron) or (supplied.startswith("diamond ") and not diamonds):
                raise ValueError("The block material must match the resource being recorded.")
        unit = unit.removeprefix("iron ").removeprefix("diamond ").removesuffix("s")
        packed_blocks = bool(match[3])
        if not iron and unit == "ingot":
            raise ValueError("Only iron can be counted in ingots.")
        if not (iron or diamonds) and (packed_blocks or unit == "block"):
            raise ValueError("Only iron and diamonds can be counted in blocks.")
        if packed_blocks and unit not in ("stack", "chest", "single chest", "double chest"):
            raise ValueError("Use a stack or chest for packed blocks, e.g. 1 stack of blocks.")
        size = {"item": 1, "ingot": 1, "block": IRON_PER_BLOCK, "stack": rules.stack_size,
                "chest": rules.chest_slots * rules.stack_size,
                "single chest": rules.chest_slots * rules.stack_size,
                "double chest": rules.double_chest_slots * rules.stack_size}[unit]
        total += int(match[1].replace(",", "")) * size * (IRON_PER_BLOCK if packed_blocks else 1)
        if total > MAX_DEPOT_ITEMS:
            raise ValueError(f"The maximum is {MAX_DEPOT_ITEMS:,} items per resource per depot.")
    return total


@dataclass(frozen=True)
class DefenceTransfer:
    blocks: int
    remaining_ingots: int
    capacity: int
    required_blocks: int
    missing_ingots: int


def defence_transfer(iron: int, rules: WarRules, points: int = 0) -> DefenceTransfer:
    blocks, remainder = divmod(iron, IRON_PER_BLOCK)
    needed = _round_up(points, rules.defence_per_block)
    return DefenceTransfer(blocks, remainder, blocks * rules.defence_per_block,
                           needed, max(0, needed * IRON_PER_BLOCK - iron))


@dataclass(frozen=True)
class Packing:
    slots: int
    donkey_loads: int
    chest_boat_loads: int
    single_chests: int
    double_chests: int


def block_shipment(diamonds: int) -> int:
    """Individual diamonds in the whole blocks sent for a conquest."""
    return _round_up(diamonds, IRON_PER_BLOCK) * IRON_PER_BLOCK


def pack(rules: WarRules, *, hay: int = 0, diamonds: int = 0, iron: int = 0, logs: int = 0) -> Packing:
    """Compact metals; count any loose remainder in its own slot. Never mix items."""
    if min(hay, diamonds, iron, logs) < 0:
        raise ValueError("Cargo quantities cannot be negative.")
    slots = sum(_round_up(n, rules.stack_size) for n in (hay, logs))
    for count in (diamonds, iron):
        blocks, loose = divmod(count, IRON_PER_BLOCK)
        slots += _round_up(blocks, rules.stack_size) + _round_up(loose, rules.stack_size)
    return Packing(
        slots=slots,
        donkey_loads=_round_up(slots, rules.donkey_slots),
        chest_boat_loads=_round_up(slots, rules.chest_boat_slots),
        single_chests=_round_up(slots, rules.chest_slots),
        double_chests=_round_up(slots, rules.double_chest_slots),
    )


@dataclass(frozen=True)
class TransportPlan:
    mode: str
    carriers: int
    slots_per_carrier: int
    loads: int
    trips: int


def plan_transport(cargo: Packing, rules: WarRules, mode: str, carriers: int = 1) -> TransportPlan:
    capacities = {"donkey": rules.donkey_slots, "chest_boat": rules.chest_boat_slots,
                  "chest_minecart": rules.chest_minecart_slots}
    if mode not in capacities or carriers < 1:
        raise ValueError("Choose a transport mode and at least one carrier.")
    capacity = capacities[mode]
    loads = _round_up(cargo.slots, capacity)
    if mode == "chest_minecart":
        if carriers != 1:
            raise ValueError("Minecart mode supplies enough carts for one dispatch. Omit carriers.")
        return TransportPlan(mode, loads, capacity, loads, int(loads > 0))
    return TransportPlan(mode, carriers, capacity, loads, _round_up(loads, carriers))


@dataclass(frozen=True)
class Assault:
    hits: int
    attackers: int
    volleys: int
    seconds: Decimal
    axes: int
    diamonds: int
    bread: int
    hay: int
    sticks: int
    planks: int
    logs: int


def estimate(hp: int, players: int, rules: WarRules) -> Assault:
    if hp < 1 or players < 1:
        raise ValueError("Settlement HP and player count must be positive.")
    hits = ceil(hp / rules.damage_per_hit)
    attackers = min(players, hits)
    base, extra = divmod(hits, attackers)
    volleys = base + int(extra > 0)
    axes = bread = 0
    # Evenly spread the hits. Tools and loaves cannot be split between attackers.
    for swings, count in ((base, attackers - extra), (base + 1, extra)):
        axes += count * (-(-swings // rules.axe_durability))
        hunger = (swings // rules.hits_per_hunger_loss) * rules.hunger_loss
        bread += count * ceil(hunger / rules.hunger_per_bread)
    sticks = axes * rules.sticks_per_axe
    planks = _round_up(sticks, rules.sticks_per_batch) * rules.planks_per_batch
    logs = _round_up(planks, rules.planks_per_log)
    return Assault(hits, attackers, volleys, (volleys - 1) * rules.rehit_seconds,
                  axes, axes * rules.diamonds_per_axe, bread, -(-bread // rules.bread_per_hay),
                  sticks, planks, logs)
