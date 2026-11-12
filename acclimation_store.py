"""Guild-scoped appointments, deeds and quest rewards, committed atomically."""

from contextlib import asynccontextmanager
from datetime import datetime, timezone

import database as db


MEDALS = ((10, "Sunrise Copper Medal — Third Class"),
          (30, "Sunrise Copper Medal — Second Class"),
          (50, "Sunrise Copper Medal — First Class"))
TABLES = ("guilds", "officials", "appointments", "quests", "awards", "tribune_days")


def schema(postgres):
    prefix = "sunset_acclimations." if postgres else "acclimation_"
    identity = "BIGSERIAL PRIMARY KEY" if postgres else "INTEGER PRIMARY KEY AUTOINCREMENT"
    sql = f"""
        CREATE TABLE IF NOT EXISTS {prefix}guilds (guild_id BIGINT PRIMARY KEY);
        CREATE TABLE IF NOT EXISTS {prefix}officials (
            guild_id BIGINT NOT NULL, user_id BIGINT NOT NULL,
            rank TEXT NOT NULL CHECK (rank IN ('hand', 'tribune')),
            appointed_by BIGINT NOT NULL, created_at TEXT NOT NULL,
            PRIMARY KEY (guild_id, user_id)
        );
        CREATE TABLE IF NOT EXISTS {prefix}appointments (
            id {identity}, guild_id BIGINT NOT NULL, user_id BIGINT NOT NULL,
            rank TEXT NOT NULL CHECK (rank IN ('hand', 'tribune', 'dismissed')),
            actor_id BIGINT NOT NULL, created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS {prefix}quests (
            id {identity}, guild_id BIGINT NOT NULL,
            request_id BIGINT NOT NULL, title TEXT NOT NULL, details TEXT NOT NULL,
            reward INTEGER NOT NULL CHECK (reward BETWEEN 1 AND 1000000),
            status TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'closed')),
            created_by BIGINT NOT NULL, created_at TEXT NOT NULL,
            closed_by BIGINT, closed_at TEXT,
            UNIQUE (guild_id, id), UNIQUE (guild_id, request_id)
        );
        CREATE TABLE IF NOT EXISTS {prefix}awards (
            id {identity}, guild_id BIGINT NOT NULL, request_id BIGINT NOT NULL,
            recipient_id BIGINT NOT NULL, issuer_id BIGINT NOT NULL,
            amount INTEGER NOT NULL CHECK (amount BETWEEN 1 AND 1000000),
            deed TEXT NOT NULL, created_at TEXT NOT NULL,
            source TEXT NOT NULL CHECK (source IN ('hand', 'tribune', 'quest')),
            quest_id BIGINT, voided_by BIGINT, voided_at TEXT, void_reason TEXT,
            UNIQUE (guild_id, id), UNIQUE (guild_id, request_id),
            UNIQUE (guild_id, quest_id, recipient_id),
            FOREIGN KEY (guild_id, quest_id) REFERENCES {prefix}quests (guild_id, id),
            CHECK ((source = 'quest' AND quest_id IS NOT NULL) OR (source != 'quest' AND quest_id IS NULL))
        );
        CREATE TABLE IF NOT EXISTS {prefix}tribune_days (
            guild_id BIGINT NOT NULL, issuer_id BIGINT NOT NULL, day TEXT NOT NULL,
            award_id BIGINT NOT NULL,
            PRIMARY KEY (guild_id, issuer_id, day),
            FOREIGN KEY (guild_id, award_id) REFERENCES {prefix}awards (guild_id, id)
        );
        CREATE INDEX IF NOT EXISTS acclimation_history_idx ON {prefix}awards (guild_id, recipient_id, id);
        CREATE INDEX IF NOT EXISTS acclimation_quests_idx ON {prefix}quests (guild_id, status, id);
        CREATE INDEX IF NOT EXISTS acclimation_appointments_idx ON {prefix}appointments (guild_id, id);
    """
    if postgres:
        sql = "CREATE SCHEMA IF NOT EXISTS sunset_acclimations; REVOKE ALL ON SCHEMA sunset_acclimations FROM PUBLIC; " + sql
        sql += " ".join(f"ALTER TABLE {prefix}{table} ENABLE ROW LEVEL SECURITY;" for table in TABLES)
    return sql


def _table(name):
    return ("sunset_acclimations." if db.IS_POSTGRES else "acclimation_") + name


def utc_now():
    return datetime.now(timezone.utc)


def clean(text, maximum):
    text = text.strip()
    if not text or len(text) > maximum or any(ord(c) < 32 and c not in "\n\t" for c in text):
        raise ValueError(f"Enter meaningful text of 1–{maximum} characters.")
    return text


def positive(amount):
    if not isinstance(amount, int) or not 1 <= amount <= 1_000_000:
        raise ValueError("Use a whole amount between 1 and 1,000,000.")


@asynccontextmanager
async def _write(guild):
    async with db.DBConnection() as conn, conn.transaction():
        # Serialize mutations within a guild, including authority changes. Keep
        # this lock inside the leader fence and never call Discord while holding it.
        await conn.execute(f"INSERT INTO {_table('guilds')} (guild_id) VALUES (?) ON CONFLICT DO NOTHING", (guild,))
        if db.IS_POSTGRES:
            await conn.fetchone(f"SELECT guild_id FROM {_table('guilds')} WHERE guild_id=? FOR UPDATE", (guild,))
        yield conn


async def _rank(conn, guild, user):
    row = await conn.fetchone(f"SELECT rank FROM {_table('officials')} WHERE guild_id=? AND user_id=?", (guild, user))
    return row["rank"] if row else None


async def _quest_authority(conn, guild, actor, admin):
    if not admin and await _rank(conn, guild, actor) != "hand":
        raise ValueError("Only appointed Hands or Discord administrators can manage quests and approve rewards.")


async def appoint(guild, actor, member, rank, *, admin):
    if not admin:
        raise ValueError("Only Discord administrators can deputize or dismiss Hands and Tribunes.")
    if rank not in ("hand", "tribune", "dismissed"):
        raise ValueError("Choose Hand or Tribune.")
    async with _write(guild) as conn:
        now = utc_now().isoformat()
        old = await _rank(conn, guild, member)
        if old == rank or (old is None and rank == "dismissed"):
            raise ValueError("That member already has this appointment status.")
        if rank == "dismissed":
            await conn.execute(f"DELETE FROM {_table('officials')} WHERE guild_id=? AND user_id=?", (guild, member))
        else:
            await conn.execute(f"INSERT INTO {_table('officials')} (guild_id,user_id,rank,appointed_by,created_at) "
                               "VALUES (?,?,?,?,?) ON CONFLICT (guild_id,user_id) DO UPDATE SET "
                               "rank=excluded.rank,appointed_by=excluded.appointed_by,created_at=excluded.created_at",
                               (guild, member, rank, actor, now))
        await conn.execute(f"INSERT INTO {_table('appointments')} (guild_id,user_id,rank,actor_id,created_at) "
                           "VALUES (?,?,?,?,?)", (guild, member, rank, actor, now))


async def officials(guild, page=1):
    async with db.DBConnection() as conn:
        return await conn.fetchall(f"SELECT * FROM {_table('officials')} WHERE guild_id=? ORDER BY rank,user_id LIMIT 10 OFFSET ?",
                                   (guild, (page - 1) * 10))


async def appointments(guild, page=1):
    async with db.DBConnection() as conn:
        return await conn.fetchall(f"SELECT * FROM {_table('appointments')} WHERE guild_id=? ORDER BY id DESC LIMIT 10 OFFSET ?",
                                   (guild, (page - 1) * 10))


async def _total(conn, guild, member):
    row = await conn.fetchone(f"SELECT COALESCE(SUM(amount),0) AS total FROM {_table('awards')} "
                             "WHERE guild_id=? AND recipient_id=? AND voided_at IS NULL", (guild, member))
    return row["total"]


async def profile(guild, member):
    async with db.DBConnection() as conn:
        total = await _total(conn, guild, member)
        rank = await _rank(conn, guild, member)
        used = await conn.fetchone(f"SELECT award_id FROM {_table('tribune_days')} WHERE guild_id=? AND issuer_id=? AND day=?",
                                  (guild, member, utc_now().date().isoformat()))
    return dict(total=total, rank=rank, remaining=0 if used else 1)


async def _record(conn, guild, actor, member, amount, deed, request_id, source, quest_id, now):
    previous = await _total(conn, guild, member)
    row = await conn.execute_returning_one(
        f"INSERT INTO {_table('awards')} (guild_id,request_id,recipient_id,issuer_id,amount,deed,created_at,source,quest_id) "
        "VALUES (?,?,?,?,?,?,?,?,?) RETURNING *", (guild, request_id, member, actor, amount, deed, now, source, quest_id))
    return row | dict(previous=previous, total=previous + amount, duplicate=False)


async def _replay(conn, guild, request_id, actor, member, quest_id=None):
    row = await conn.fetchone(f"SELECT * FROM {_table('awards')} WHERE guild_id=? AND request_id=?", (guild, request_id))
    if row:
        if row["issuer_id"] != actor or row["recipient_id"] != member or row["quest_id"] != quest_id:
            raise ValueError("This request was already used for another award.")
        return row | dict(total=await _total(conn, guild, member), duplicate=True)


async def award(guild, actor, member, amount, deed, *, request_id):
    positive(amount)
    deed = clean(deed, 500)
    if actor == member:
        raise ValueError("You cannot award yourself Acclimations; another appointed official must recognise the deed.")
    async with _write(guild) as conn:
        rank = await _rank(conn, guild, actor)
        if rank is None:
            raise ValueError("You must be deputized as a Hand or Tribune before awarding Acclimations.")
        existing = await _replay(conn, guild, request_id, actor, member)
        if existing:
            return existing
        now = utc_now()
        if rank == "tribune":
            if amount != 1:
                raise ValueError("A Tribune can award exactly one Acclimation per day.")
            used = await conn.fetchone(f"SELECT award_id FROM {_table('tribune_days')} WHERE guild_id=? AND issuer_id=? AND day=?",
                                      (guild, actor, now.date().isoformat()))
            if used:
                raise ValueError("Your Tribune award for today is already used. It resets at 00:00 UTC; unused days do not accumulate.")
        row = await _record(conn, guild, actor, member, amount, deed, request_id, rank, None, now.isoformat())
        if rank == "tribune":
            await conn.execute(f"INSERT INTO {_table('tribune_days')} (guild_id,issuer_id,day,award_id) VALUES (?,?,?,?)",
                               (guild, actor, now.date().isoformat(), row["id"]))
        return row


async def void_award(guild, actor, award_id, reason, *, admin):
    if not admin:
        raise ValueError("Only Discord administrators can void an award.")
    reason = clean(reason, 250)
    async with _write(guild) as conn:
        row = await conn.execute_returning_one(f"UPDATE {_table('awards')} SET voided_by=?,voided_at=?,void_reason=? "
                                              "WHERE guild_id=? AND id=? AND voided_at IS NULL RETURNING *",
                                              (actor, utc_now().isoformat(), reason, guild, award_id))
        if row is None:
            raise ValueError("No active award with that ID exists on this server.")
        # Retain daily quotas and quest completion uniqueness after a correction.
        return row | dict(total=await _total(conn, guild, row["recipient_id"]))


async def history(guild, member, page=1):
    async with db.DBConnection() as conn:
        return await conn.fetchall(f"SELECT * FROM {_table('awards')} WHERE guild_id=? AND recipient_id=? "
                                   "ORDER BY id DESC LIMIT 2 OFFSET ?", (guild, member, (page - 1) * 2))


async def honours(guild, page=1):
    async with db.DBConnection() as conn:
        return await conn.fetchall(f"SELECT recipient_id,SUM(amount) AS total FROM {_table('awards')} "
                                   "WHERE guild_id=? AND voided_at IS NULL GROUP BY recipient_id HAVING SUM(amount)>=10 "
                                   "ORDER BY total DESC,recipient_id LIMIT 10 OFFSET ?", (guild, (page - 1) * 10))


async def create_quest(guild, actor, title, details, reward, *, admin, request_id):
    title, details = clean(title, 80), clean(details, 700)
    positive(reward)
    async with _write(guild) as conn:
        await _quest_authority(conn, guild, actor, admin)
        existing = await conn.fetchone(f"SELECT * FROM {_table('quests')} WHERE guild_id=? AND request_id=?", (guild, request_id))
        if existing:
            return existing
        return await conn.execute_returning_one(f"INSERT INTO {_table('quests')} "
                                               "(guild_id,request_id,title,details,reward,created_by,created_at) "
                                               "VALUES (?,?,?,?,?,?,?) RETURNING *",
                                               (guild, request_id, title, details, reward, actor, utc_now().isoformat()))


async def get_quest(guild, quest_id):
    async with db.DBConnection() as conn:
        return await conn.fetchone(f"SELECT * FROM {_table('quests')} WHERE guild_id=? AND id=?", (guild, quest_id))


async def quests(guild, page=1):
    async with db.DBConnection() as conn:
        return await conn.fetchall(f"SELECT * FROM {_table('quests')} WHERE guild_id=? AND status='open' "
                                   "ORDER BY id DESC LIMIT 5 OFFSET ?", (guild, (page - 1) * 5))


async def close_quest(guild, actor, quest_id, *, admin):
    async with _write(guild) as conn:
        await _quest_authority(conn, guild, actor, admin)
        row = await conn.execute_returning_one(f"UPDATE {_table('quests')} SET status='closed',closed_by=?,closed_at=? "
                                              "WHERE guild_id=? AND id=? AND status='open' RETURNING *",
                                              (actor, utc_now().isoformat(), guild, quest_id))
        if row is None:
            raise ValueError("No open quest with that ID exists on this server.")
        return row


async def complete_quest(guild, actor, quest_id, member, deed, *, admin, request_id):
    deed = clean(deed, 500)
    if actor == member:
        raise ValueError("Another Hand or administrator must approve your own quest completion.")
    async with _write(guild) as conn:
        await _quest_authority(conn, guild, actor, admin)
        existing = await _replay(conn, guild, request_id, actor, member, quest_id)
        if existing:
            return existing
        quest = await conn.fetchone(f"SELECT * FROM {_table('quests')} WHERE guild_id=? AND id=? AND status='open'", (guild, quest_id))
        if not quest:
            raise ValueError("No open quest with that ID exists on this server.")
        completed = await conn.fetchone(f"SELECT id FROM {_table('awards')} WHERE guild_id=? AND quest_id=? AND recipient_id=?",
                                       (guild, quest_id, member))
        if completed:
            raise ValueError("This member already received this quest's reward (including any later void).")
        return await _record(conn, guild, actor, member, quest["reward"], deed, request_id, "quest", quest_id, utc_now().isoformat())
