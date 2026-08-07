"""
Database schema and async helper functions supporting both SQLite and PostgreSQL.
Uses aiosqlite for local storage and asyncpg for PostgreSQL cloud database.
"""

import os
import re
import aiosqlite
import asyncpg
from datetime import datetime, timezone

DATABASE_URL = os.environ.get("DATABASE_URL")
IS_POSTGRES = DATABASE_URL is not None and (DATABASE_URL.startswith("postgres://") or DATABASE_URL.startswith("postgresql://"))
DB_PATH = "claims.db"


def _format_query(query: str) -> str:
    """Helper to transform SQLite query syntax to PostgreSQL syntax if needed."""
    if not IS_POSTGRES:
        return query
    
    # 1. Replace ? with $1, $2, $3...
    count = [0]
    def repl(m):
        count[0] += 1
        return f"${count[0]}"
    q = re.sub(r'\?', repl, query)
    
    # 2. Replace INSERT OR IGNORE with INSERT ... ON CONFLICT DO NOTHING
    q = re.sub(r'(?i)\bINSERT OR IGNORE\b', 'INSERT', q)
    if 'ON CONFLICT' not in q and 'INSERT' in q.upper():
        if 'INSERT OR IGNORE' in query.upper():
            q += " ON CONFLICT DO NOTHING"
            
    # 3. Strip COLLATE NOCASE (Postgres will use case-insensitive schema comparison or LOWER)
    q = q.replace("COLLATE NOCASE", "")
    
    return q


class DBConnection:
    def __init__(self):
        self.sqlite_conn = None
        self.pg_conn = None

    async def __aenter__(self):
        if IS_POSTGRES:
            self.pg_conn = await asyncpg.connect(DATABASE_URL)
            return self
        else:
            self.sqlite_conn = await aiosqlite.connect(DB_PATH)
            self.sqlite_conn.row_factory = aiosqlite.Row
            return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        if self.pg_conn:
            await self.pg_conn.close()
        if self.sqlite_conn:
            await self.sqlite_conn.close()

    async def execute(self, query: str, params: tuple = ()):
        q = _format_query(query)
        if IS_POSTGRES:
            await self.pg_conn.execute(q, *params)
        else:
            await self.sqlite_conn.execute(q, params)
            await self.sqlite_conn.commit()

    async def fetchone(self, query: str, params: tuple = ()) -> dict | None:
        q = _format_query(query)
        if IS_POSTGRES:
            row = await self.pg_conn.fetchrow(q, *params)
            return dict(row) if row else None
        else:
            async with self.sqlite_conn.execute(q, params) as cur:
                row = await cur.fetchone()
                return dict(row) if row else None

    async def fetchall(self, query: str, params: tuple = ()) -> list[dict]:
        q = _format_query(query)
        if IS_POSTGRES:
            rows = await self.pg_conn.fetch(q, *params)
            return [dict(r) for r in rows]
        else:
            async with self.sqlite_conn.execute(q, params) as cur:
                rows = await cur.fetchall()
                return [dict(r) for r in rows]


async def init_db():
    """Create all tables if they don't exist. Called once on bot startup."""
    if IS_POSTGRES:
        async with asyncpg.connect(DATABASE_URL) as conn:
            await conn.execute(
                """
                CREATE TABLE IF NOT EXISTS lands (
                    id          SERIAL PRIMARY KEY,
                    name        TEXT    NOT NULL UNIQUE,
                    owner_id    BIGINT  NOT NULL UNIQUE,
                    chunks      INTEGER NOT NULL DEFAULT 0,
                    created_at  TEXT    NOT NULL
                );

                CREATE TABLE IF NOT EXISTS members (
                    id       SERIAL PRIMARY KEY,
                    land_id  INTEGER NOT NULL REFERENCES lands(id) ON DELETE CASCADE,
                    user_id  BIGINT  NOT NULL,
                    UNIQUE(land_id, user_id)
                );

                CREATE TABLE IF NOT EXISTS claim_requests (
                    id               SERIAL PRIMARY KEY,
                    land_id          INTEGER NOT NULL REFERENCES lands(id) ON DELETE CASCADE,
                    chunks_requested INTEGER NOT NULL DEFAULT 1,
                    purpose          TEXT    NOT NULL DEFAULT 'other',
                    requested_at     TEXT    NOT NULL,
                    status           TEXT    NOT NULL DEFAULT 'pending'
                );

                CREATE TABLE IF NOT EXISTS purchases (
                    id            SERIAL PRIMARY KEY,
                    land_id       INTEGER NOT NULL REFERENCES lands(id) ON DELETE CASCADE,
                    type          TEXT    NOT NULL,
                    price_paid    INTEGER NOT NULL DEFAULT 0,
                    purchased_at  TEXT    NOT NULL
                );

                CREATE TABLE IF NOT EXISTS reserve (
                    id             INTEGER PRIMARY KEY CHECK (id = 1),
                    total_blocks   INTEGER NOT NULL DEFAULT 10,
                    protected_min  INTEGER NOT NULL DEFAULT 3
                );

                CREATE TABLE IF NOT EXISTS config (
                    key   TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );

                -- Seed reserve row if missing
                INSERT INTO reserve (id, total_blocks, protected_min)
                VALUES (1, 0, 3)
                ON CONFLICT (id) DO NOTHING;

                -- Seed default config
                INSERT INTO config (key, value) VALUES ('full_block_cost', '50') ON CONFLICT DO NOTHING;
                INSERT INTO config (key, value) VALUES ('staff_role_id', '0') ON CONFLICT DO NOTHING;
                INSERT INTO config (key, value) VALUES ('weekly_reset_day', '0') ON CONFLICT DO NOTHING;
                """
            )
    else:
        async with aiosqlite.connect(DB_PATH) as db:
            await db.executescript(
                """
                CREATE TABLE IF NOT EXISTS lands (
                    id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    name        TEXT    NOT NULL UNIQUE COLLATE NOCASE,
                    owner_id    INTEGER NOT NULL UNIQUE,
                    chunks      INTEGER NOT NULL DEFAULT 0,
                    created_at  TEXT    NOT NULL
                );

                CREATE TABLE IF NOT EXISTS members (
                    id       INTEGER PRIMARY KEY AUTOINCREMENT,
                    land_id  INTEGER NOT NULL REFERENCES lands(id) ON DELETE CASCADE,
                    user_id  INTEGER NOT NULL,
                    UNIQUE(land_id, user_id)
                );

                CREATE TABLE IF NOT EXISTS claim_requests (
                    id               INTEGER PRIMARY KEY AUTOINCREMENT,
                    land_id          INTEGER NOT NULL REFERENCES lands(id) ON DELETE CASCADE,
                    chunks_requested INTEGER NOT NULL DEFAULT 1,
                    purpose          TEXT    NOT NULL DEFAULT 'other',
                    requested_at     TEXT    NOT NULL,
                    status           TEXT    NOT NULL DEFAULT 'pending'
                );

                CREATE TABLE IF NOT EXISTS purchases (
                    id            INTEGER PRIMARY KEY AUTOINCREMENT,
                    land_id       INTEGER NOT NULL REFERENCES lands(id) ON DELETE CASCADE,
                    type          TEXT    NOT NULL,
                    price_paid    INTEGER NOT NULL DEFAULT 0,
                    purchased_at  TEXT    NOT NULL
                );

                CREATE TABLE IF NOT EXISTS reserve (
                    id             INTEGER PRIMARY KEY CHECK (id = 1),
                    total_blocks   INTEGER NOT NULL DEFAULT 10,
                    protected_min  INTEGER NOT NULL DEFAULT 3
                );

                CREATE TABLE IF NOT EXISTS config (
                    key   TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );

                -- Seed reserve row if missing
                INSERT OR IGNORE INTO reserve (id, total_blocks, protected_min)
                VALUES (1, 0, 3);

                -- Seed default config
                INSERT OR IGNORE INTO config (key, value) VALUES ('full_block_cost', '50');
                INSERT OR IGNORE INTO config (key, value) VALUES ('staff_role_id', '0');
                INSERT OR IGNORE INTO config (key, value) VALUES ('weekly_reset_day', '0');
                """
            )
            await db.commit()


# ── Config helpers ────────────────────────────────────────────────────

async def get_config(key: str) -> str | None:
    async with DBConnection() as conn:
        row = await conn.fetchone("SELECT value FROM config WHERE key = ?", (key,))
        return row["value"] if row else None


async def set_config(key: str, value: str):
    async with DBConnection() as conn:
        await conn.execute(
            "INSERT INTO config (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )


async def get_full_block_cost() -> int:
    val = await get_config("full_block_cost")
    return int(val) if val else 50


async def get_staff_role_id() -> int:
    val = await get_config("staff_role_id")
    return int(val) if val else 0


# ── Land helpers ──────────────────────────────────────────────────────

async def create_land(name: str, owner_id: int, chunks: int = 0) -> int:
    now = datetime.now(timezone.utc).isoformat()
    async with DBConnection() as conn:
        row = await conn.fetchone(
            "INSERT INTO lands (name, owner_id, chunks, created_at) VALUES (?, ?, ?, ?) RETURNING id",
            (name, owner_id, chunks, now),
        )
        return row["id"]


async def get_land_by_owner(owner_id: int) -> dict | None:
    async with DBConnection() as conn:
        return await conn.fetchone("SELECT * FROM lands WHERE owner_id = ?", (owner_id,))


async def get_land_by_name(name: str) -> dict | None:
    async with DBConnection() as conn:
        # Use case-insensitive LOWER check for both SQL engines
        return await conn.fetchone("SELECT * FROM lands WHERE LOWER(name) = LOWER(?)", (name,))


async def get_land_by_id(land_id: int) -> dict | None:
    async with DBConnection() as conn:
        return await conn.fetchone("SELECT * FROM lands WHERE id = ?", (land_id,))


async def update_land_chunks(land_id: int, new_chunks: int):
    async with DBConnection() as conn:
        await conn.execute("UPDATE lands SET chunks = ? WHERE id = ?", (new_chunks, land_id))


async def get_land_for_user(user_id: int) -> dict | None:
    """Get a land where the user is either owner or member."""
    land = await get_land_by_owner(user_id)
    if land:
        return land
    async with DBConnection() as conn:
        return await conn.fetchone(
            "SELECT l.* FROM lands l JOIN members m ON l.id = m.land_id WHERE m.user_id = ?",
            (user_id,),
        )


# ── Member helpers ────────────────────────────────────────────────────

async def add_member(land_id: int, user_id: int):
    async with DBConnection() as conn:
        # Insert with conflict handling
        if IS_POSTGRES:
            await conn.execute(
                "INSERT INTO members (land_id, user_id) VALUES (?, ?) ON CONFLICT DO NOTHING",
                (land_id, user_id),
            )
        else:
            await conn.execute(
                "INSERT OR IGNORE INTO members (land_id, user_id) VALUES (?, ?)",
                (land_id, user_id),
            )


async def remove_member(land_id: int, user_id: int) -> bool:
    async with DBConnection() as conn:
        row = await conn.fetchone("SELECT 1 FROM members WHERE land_id = ? AND user_id = ?", (land_id, user_id))
        if not row:
            return False
        await conn.execute("DELETE FROM members WHERE land_id = ? AND user_id = ?", (land_id, user_id))
        return True


async def get_member_count(land_id: int) -> int:
    async with DBConnection() as conn:
        row = await conn.fetchone("SELECT COUNT(*) as count FROM members WHERE land_id = ?", (land_id,))
        return row["count"] if row else 0


async def get_members(land_id: int) -> list[int]:
    async with DBConnection() as conn:
        rows = await conn.fetchall("SELECT user_id FROM members WHERE land_id = ?", (land_id,))
        return [r["user_id"] for r in rows]


# ── Claim request helpers ─────────────────────────────────────────────

async def create_claim_request(land_id: int, chunks_requested: int, purpose: str) -> int:
    now = datetime.now(timezone.utc).isoformat()
    async with DBConnection() as conn:
        row = await conn.fetchone(
            "INSERT INTO claim_requests (land_id, chunks_requested, purpose, requested_at, status) "
            "VALUES (?, ?, ?, ?, 'pending') RETURNING id",
            (land_id, chunks_requested, purpose, now),
        )
        return row["id"]


async def get_pending_request(land_id: int) -> dict | None:
    async with DBConnection() as conn:
        return await conn.fetchone(
            "SELECT * FROM claim_requests WHERE land_id = ? AND status = 'pending' "
            "ORDER BY requested_at ASC LIMIT 1",
            (land_id,),
        )


async def get_approved_request(land_id: int) -> dict | None:
    async with DBConnection() as conn:
        return await conn.fetchone(
            "SELECT * FROM claim_requests WHERE land_id = ? AND status = 'approved' "
            "ORDER BY requested_at ASC LIMIT 1",
            (land_id,),
        )


async def get_all_pending_requests() -> list[dict]:
    async with DBConnection() as conn:
        return await conn.fetchall(
            "SELECT cr.*, l.name as land_name, l.chunks, l.owner_id "
            "FROM claim_requests cr "
            "JOIN lands l ON cr.land_id = l.id "
            "WHERE cr.status = 'pending' "
            "ORDER BY cr.requested_at ASC"
        )


async def update_request_status(request_id: int, status: str):
    async with DBConnection() as conn:
        await conn.execute("UPDATE claim_requests SET status = ? WHERE id = ?", (status, request_id))


# ── Purchase helpers ──────────────────────────────────────────────────

async def record_purchase(land_id: int, purchase_type: str, price_paid: int):
    now = datetime.now(timezone.utc).isoformat()
    async with DBConnection() as conn:
        await conn.execute(
            "INSERT INTO purchases (land_id, type, price_paid, purchased_at) VALUES (?, ?, ?, ?)",
            (land_id, purchase_type, price_paid, now),
        )


async def get_normal_purchase_this_week(land_id: int) -> dict | None:
    """Check if this land has made a normal purchase since the last weekly reset."""
    from datetime import timedelta
    now = datetime.now(timezone.utc)
    days_since_monday = now.weekday()
    last_monday = now.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=days_since_monday)
    reset_iso = last_monday.isoformat()

    async with DBConnection() as conn:
        return await conn.fetchone(
            "SELECT * FROM purchases WHERE land_id = ? AND type = 'normal' AND purchased_at >= ? "
            "ORDER BY purchased_at DESC LIMIT 1",
            (land_id, reset_iso),
        )


# ── Reserve helpers ───────────────────────────────────────────────────

async def get_reserve() -> dict:
    async with DBConnection() as conn:
        return await conn.fetchone("SELECT * FROM reserve WHERE id = 1")


async def update_reserve_blocks(new_total: int):
    async with DBConnection() as conn:
        await conn.execute("UPDATE reserve SET total_blocks = ? WHERE id = 1", (new_total,))


async def add_reserve_blocks(amount: int):
    async with DBConnection() as conn:
        await conn.execute("UPDATE reserve SET total_blocks = total_blocks + ? WHERE id = 1", (amount,))
