"""Readable quantities for estimates; conversions never change inventory."""

from decimal import Decimal


def duration(seconds):
    remaining = Decimal(seconds).quantize(Decimal("0.001"))
    parts = []
    for size, suffix in ((86400, "d"), (3600, "h"), (60, "m")):
        count, remaining = divmod(remaining, size)
        if count:
            parts.append(f"{int(count):,}{suffix}")
    if remaining or not parts:
        parts.append(format(remaining, "f").rstrip("0").rstrip(".") + "s")
    return " ".join(parts)


def quantity(count, resource, rules):
    label = "iron ingots" if resource == "iron" else resource
    result = f"{count:,} {label}"
    rest = count
    parts = []
    for size, name in ((rules.stack_size * rules.double_chest_slots, "double chest"),
                       (rules.stack_size * rules.chest_slots, "single chest"),
                       (rules.stack_size, "stack")):
        # Capacities are independently configurable; never overcount by rounding.
        full, rest = divmod(rest, size)
        if full:
            parts.append(f"{full:,} {name}{'s' if full != 1 else ''}")
    if parts:
        if rest:
            parts.append(f"{rest} loose")
        result += " (" + " + ".join(parts) + ")"
    if resource in ("diamonds", "iron") and count >= 9:
        blocks, loose = divmod(count, 9)
        result += f"; {blocks:,} block{'s' if blocks != 1 else ''}"
        if loose:
            result += f" + {loose} loose"
    return result


def assumptions(rules):
    return (
        "**Assumptions**\n"
        f"Axe: {rules.diamonds_per_axe} diamonds + {rules.sticks_per_axe} sticks; "
        f"{rules.axe_durability} hits; {rules.damage_per_hit} HP/hit; {rules.rehit_seconds}s between hits.\n"
        f"Wood: {rules.planks_per_log} planks/log; {rules.sticks_per_batch} sticks per {rules.planks_per_batch} planks. "
        f"Food: {rules.bread_per_hay} bread/hay; {rules.hunger_per_bread} hunger/bread; "
        f"{rules.hunger_loss} hunger lost per {rules.hits_per_hunger_loss} completed hits per player.\n"
        "Fresh equipment, uninterrupted evenly split hits, whole tools and loaves per attacker. "
        "Crafting rounds up to whole batches and logs. First hits are immediate. "
        "Crafting and eating time excluded. No stock deducted."
    )
