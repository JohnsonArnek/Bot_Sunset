"""Readable quantities for estimates; conversions never change inventory."""

from decimal import Decimal, localcontext


def duration(seconds):
    with localcontext() as context:
        context.prec = max(28, Decimal(seconds).adjusted() + 5)
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
    """Lead with full containers; metals use containers of blocks, then loose units."""
    label = "iron ingots" if resource == "iron" else resource
    singular = {"iron": "iron ingot", "diamonds": "diamond", "logs": "log", "arrows": "arrow"}.get(resource, label)
    total = f"{count:,} {singular if count == 1 else label}"
    compressed = resource in ("diamonds", "iron")
    if compressed and count < 9:
        return total
    rest, loose = divmod(count, 9) if compressed else (count, 0)
    parts = []
    for size, name in ((rules.stack_size * rules.double_chest_slots, "double chest"),
                       (rules.stack_size * rules.chest_slots, "single chest"),
                       (rules.stack_size, "stack")):
        # Capacities are independently configurable; never overcount by rounding.
        full, rest = divmod(rest, size)
        if full:
            parts.append(f"{full:,} {name}{'s' if full != 1 else ''}" + (" of blocks" if compressed else ""))
    if compressed:
        if rest:
            parts.append(f"{rest} block{'s' if rest != 1 else ''}")
        if loose:
            parts.append(f"{loose} loose {singular if loose == 1 else label}")
    elif rest and parts:
        parts.append(f"{rest} loose {singular if rest == 1 else label}")
    if not parts:
        return total
    return " + ".join(parts) + f" ({total})"


def inventory(row, rules):
    return "\n".join(f"{key.title()}: {quantity(row[key], key, rules)}"
                     for key in ("hay", "diamonds", "iron", "arrows", "logs"))


def assumptions(rules):
    return (
        "**Assumptions**\n"
        f"**Axe** · {rules.diamonds_per_axe} diamonds + {rules.sticks_per_axe} sticks · "
        f"{rules.axe_durability} hits · {rules.damage_per_hit} HP/hit · {rules.rehit_seconds}s between hits\n"
        f"**Wood** · {rules.planks_per_log} planks/log · {rules.sticks_per_batch} sticks per {rules.planks_per_batch} planks\n"
        f"**Food** · {rules.bread_per_hay} bread/hay · {rules.hunger_per_bread} hunger/bread · "
        f"{rules.hunger_loss} hunger per {rules.hits_per_hunger_loss} completed hits per player\n"
        "Attackers start with fresh gear, split uninterrupted hits evenly and land the first hits immediately. "
        "Tools, loaves and crafting batches round up. Timing excludes crafting and eating."
    )
