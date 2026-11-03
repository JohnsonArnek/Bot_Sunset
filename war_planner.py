"""Settlement assault estimates. Quantities are for a fully supplied attack."""

from dataclasses import dataclass
from decimal import Decimal
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
    chest_slots: int = 27
    double_chest_slots: int = 54
    defence_per_block: int = 100

    def __post_init__(self):
        if self.stack_size > 64:
            raise ValueError("A stack can hold at most 64 items.")
        for key, value in vars(self).items():
            if not 0 <= value <= 1_000_000 or (value == 0 and key != "hunger_loss"):
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


MAX_DEPOT_ITEMS = 2_000_000_000
IRON_PER_BLOCK = 9
_STOCK_TERM = re.compile(
    r"([0-9]{1,3}(?:,[0-9]{3})+|[0-9]+)\s*"
    r"(items?|(?:iron\s+)?ingots?|(?:iron\s+)?blocks?|stacks?|(?:single\s+|double\s+)?chests?)?"
    r"(?:\s+of\s+(?:iron\s+)?(blocks?))?",
    re.IGNORECASE,
)


def stock_quantity(text: str, rules: WarRules, *, iron: bool = False) -> int:
    """Convert full containers and loose items of one resource to an exact count."""
    total = 0
    if len(text) > 100:
        raise ValueError("Keep each quantity to 100 characters or fewer.")
    for term in text.split("+"):
        match = _STOCK_TERM.fullmatch(term.strip())
        if not match:
            raise ValueError("Use whole quantities, e.g. 64, 3 stacks, or 1 double chest + 5 items.")
        unit = " ".join((match[2] or "item").lower().split()).removeprefix("iron ").removesuffix("s")
        packed_blocks = bool(match[3])
        if not iron and (packed_blocks or unit in ("ingot", "block")):
            raise ValueError("Only iron can be counted in ingots or blocks.")
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


def pack(rules: WarRules, *, hay: int = 0, diamonds: int = 0, iron: int = 0, axes: int = 0, bread: int = 0) -> Packing:
    """Minimum space when consolidated: partial stacks of different items never mix."""
    if min(hay, diamonds, iron, axes, bread) < 0:
        raise ValueError("Cargo quantities cannot be negative.")
    slots = axes + sum(_round_up(n, rules.stack_size) for n in (hay, diamonds, iron, bread))
    return Packing(
        slots=slots,
        donkey_loads=_round_up(slots, rules.donkey_slots),
        chest_boat_loads=_round_up(slots, rules.chest_boat_slots),
        single_chests=_round_up(slots, rules.chest_slots),
        double_chests=_round_up(slots, rules.double_chest_slots),
    )


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
    return Assault(hits, attackers, volleys, (volleys - 1) * rules.rehit_seconds,
                  axes, axes * rules.diamonds_per_axe, bread, -(-bread // rules.bread_per_hay))
