"""Guild-scoped storage for the admin war commands, using the bot's fenced connection."""

import database as db
from war_planner import MAX_DEPOT_ITEMS, WarRules


RESOURCES = ("hay", "diamonds", "iron", "arrows")
_COLUMNS = ", ".join(RESOURCES)


def balances(row):
    return " · ".join(f"{row[k]:,} {'iron ingots' if k == 'iron' else k}" for k in RESOURCES)


def _prefix():
    return "sunset_war." if db.IS_POSTGRES else ""


def clean_location(name):
    name = " ".join(name.split())
    if not 1 <= len(name) <= 64 or not name.isprintable():
        raise ValueError("Use a location name of 1–64 printable characters.")
    return name


async def set_stock(guild, location, hay=None, diamonds=None, iron=None, arrows=None):
    name = clean_location(location)
    amounts = dict(zip(RESOURCES, (hay, diamonds, iron, arrows)))
    supplied = [key for key in RESOURCES if amounts[key] is not None]
    if not supplied:
        raise ValueError("Enter at least one resource to set.")
    if not all(0 <= amounts[key] <= MAX_DEPOT_ITEMS for key in supplied):
        raise ValueError(f"Depot balances must be between 0 and {MAX_DEPOT_ITEMS:,} items.")
    updates = ", ".join(f"{key}=excluded.{key}" for key in supplied)
    async with db.DBConnection() as conn:
        return await conn.execute_returning_one(
            f"INSERT INTO {_prefix()}war_stock (guild_id, location_key, location, {_COLUMNS}) "
            "VALUES (?, ?, ?, ?, ?, ?, ?) ON CONFLICT (guild_id, location_key) DO UPDATE SET "
            f"location=excluded.location, {updates} RETURNING location, {_COLUMNS}",
            (guild, name.casefold(), name, *(amounts[k] or 0 for k in RESOURCES)),
        )


async def adjust_stock(guild, location, hay=0, diamonds=0, iron=0, arrows=0):
    """Apply all deltas or none; return this operation's resulting balance."""
    name = clean_location(location)
    deltas = (hay, diamonds, iron, arrows)
    if not any(deltas):
        raise ValueError("Enter a nonzero quantity of at least one resource.")
    if max(map(abs, deltas)) > MAX_DEPOT_ITEMS:
        raise ValueError(f"The maximum change is {MAX_DEPOT_ITEMS:,} items per resource.")
    table = _prefix() + "war_stock"
    key = (guild, name.casefold())
    updates = ", ".join(f"{k}={k}+?" for k in RESOURCES)
    guards = " AND ".join(f"{k} BETWEEN ? AND ?" for k in RESOURCES)
    bounds = tuple(n for d in deltas for n in (max(0, -d), MAX_DEPOT_ITEMS - max(0, d)))
    async with db.DBConnection() as conn:
        # Compare the existing balances before adding, so even PostgreSQL INTEGER
        # cannot overflow. Concurrent adjustments serialize on this depot row.
        row = await conn.execute_returning_one(
            f"UPDATE {table} SET {updates} WHERE guild_id=? AND location_key=? "
            f"AND {guards} RETURNING location, {_COLUMNS}", (*deltas, *key, *bounds),
        )
        if row is not None:
            return row
        held = await conn.fetchone(f"SELECT {_COLUMNS} FROM {table} WHERE guild_id=? AND location_key=?", key)
    if held is None:
        raise ValueError("No such depot on this server. Use /war stock-set to record it first.")
    reason = "Not enough stock" if min(deltas) < 0 else f"Depot limit: {MAX_DEPOT_ITEMS:,} per resource"
    raise ValueError(f"{reason}. Nothing changed. Current balance: {balances(held)}.")


async def remove_stock(guild, location):
    # DBConnection.execute commits on SQLite. Avoid DELETE ... RETURNING via
    # fetchone, which is read-only there and would roll back on close.
    async with db.DBConnection() as conn:
        await conn.execute(f"DELETE FROM {_prefix()}war_stock WHERE guild_id=? AND location_key=?",
                           (guild, clean_location(location).casefold()))


async def stock_page(guild, page, size=6):
    async with db.DBConnection() as conn:
        return await conn.fetchall(
            f"SELECT location, {_COLUMNS} FROM {_prefix()}war_stock WHERE guild_id=? "
            "ORDER BY location_key LIMIT ? OFFSET ?", (guild, size, (page - 1) * size),
        )


async def available(guild, location=None):
    async with db.DBConnection() as conn:
        if location is not None:
            return await conn.fetchone(
                f"SELECT {_COLUMNS} FROM {_prefix()}war_stock WHERE guild_id=? AND location_key=?",
                (guild, clean_location(location).casefold()),
            )
        totals = ", ".join(f"COALESCE(SUM({k}),0) AS {k}" for k in RESOURCES)
        return await conn.fetchone(f"SELECT {totals} FROM {_prefix()}war_stock WHERE guild_id=?", (guild,))


async def rules_for(guild):
    async with db.DBConnection() as conn:
        rows = await conn.fetchall(f"SELECT key, value FROM {_prefix()}war_settings WHERE guild_id=?",
                                   (guild,))
    return WarRules.from_settings({r["key"]: r["value"] for r in rows})


async def configure(guild, changes):
    # One upsert for the supplied keys only: independent admin edits are not lost.
    rules = WarRules.from_settings(changes)
    if changes:
        values = ", ".join("(?, ?, ?)" for _ in changes)
        params = tuple(v for key in changes for v in (guild, key, str(getattr(rules, key))))
        async with db.DBConnection() as conn:
            await conn.execute(
                f"INSERT INTO {_prefix()}war_settings (guild_id, key, value) VALUES {values} "
                "ON CONFLICT (guild_id, key) DO UPDATE SET value=excluded.value", params,
            )
    return await rules_for(guild)
