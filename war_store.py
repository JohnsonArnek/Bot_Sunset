"""Guild-scoped storage for the admin war commands, using the bot's fenced connection."""

import database as db
from war_planner import WarRules


def _prefix():
    return "sunset_war." if db.IS_POSTGRES else ""


def clean_location(name):
    name = " ".join(name.split())
    if not 1 <= len(name) <= 64 or not name.isprintable():
        raise ValueError("Use a location name of 1–64 printable characters.")
    return name


async def set_stock(guild, location, hay, diamonds):
    name = clean_location(location)
    async with db.DBConnection() as conn:
        await conn.execute(
            f"INSERT INTO {_prefix()}war_stock (guild_id, location_key, location, hay, diamonds) "
            "VALUES (?, ?, ?, ?, ?) ON CONFLICT (guild_id, location_key) DO UPDATE SET "
            "location=excluded.location, hay=excluded.hay, diamonds=excluded.diamonds",
            (guild, name.casefold(), name, hay, diamonds),
        )
    return name


async def remove_stock(guild, location):
    # DBConnection.execute commits on SQLite. Avoid DELETE ... RETURNING via
    # fetchone, which is read-only there and would roll back on close.
    async with db.DBConnection() as conn:
        await conn.execute(f"DELETE FROM {_prefix()}war_stock WHERE guild_id=? AND location_key=?",
                           (guild, clean_location(location).casefold()))


async def stock_page(guild, page, size=8):
    async with db.DBConnection() as conn:
        return await conn.fetchall(
            f"SELECT location, hay, diamonds FROM {_prefix()}war_stock WHERE guild_id=? "
            "ORDER BY location_key LIMIT ? OFFSET ?", (guild, size, (page - 1) * size),
        )


async def available(guild, location=None):
    async with db.DBConnection() as conn:
        if location is not None:
            return await conn.fetchone(
                f"SELECT hay, diamonds FROM {_prefix()}war_stock WHERE guild_id=? AND location_key=?",
                (guild, clean_location(location).casefold()),
            )
        return await conn.fetchone(
            f"SELECT COALESCE(SUM(hay),0) AS hay, COALESCE(SUM(diamonds),0) AS diamonds "
            f"FROM {_prefix()}war_stock WHERE guild_id=?", (guild,),
        )


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
