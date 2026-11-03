"""Settlement assault estimates. Quantities are for a fully supplied attack."""

from dataclasses import dataclass
from decimal import Decimal
from math import ceil


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

    def __post_init__(self):
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
