"""
Database schema and async helper functions supporting both SQLite and PostgreSQL.
Uses aiosqlite for local storage and asyncpg for PostgreSQL cloud database.
All data is partitioned by Discord guild_id (per-server isolation).
"""

import os
import re
import logging
import aiosqlite
import asyncpg
from datetime import datetime, timezone

log = logging.getLogger("claims-bot")

DATABASE_URL = os.environ.get("DATABASE_URL")
IS_POSTGRES = DATABASE_URL is not None and (DATABASE_URL.startswith("postgres://") or DATABASE_URL.startswith("postgresql://"))
DB_PATH = "claims.db"

DEFAULT_CONFIG = {
    "full_block_cost": "50",
    "staff_role_id": "0",
    "weekly_reset_day": "0",
    "weekly_blocks": "7",
    "distribution_day": "0",
    "distribution_hour": "0",
    "last_distribution": "",
    "max_queue_chunks": "1",
    "reserve_mode": "topup",
    "weekly_reserve_blocks": "2",
}


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


# ── Connection Pool ───────────────────────────────────────────────────
_pool = None


async def _get_pool():
    global _pool
    if _pool is None:
        _pool = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=5)
    return _pool

class DBConnection:
    def __init__(self):
        self.sqlite_conn = None
        self.pg_conn = None

    async def __aenter__(self):
        if IS_POSTGRES:
            pool = await _get_pool()
            self.pg_conn = await pool.acquire()
            return self
        else:
            self.sqlite_conn = await aiosqlite.connect(DB_PATH)
            self.sqlite_conn.row_factory = aiosqlite.Row
            return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        if self.pg_conn:
            pool = await _get_pool()
            await pool.release(self.pg_conn)
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
    """Create all tables if they don't exist and run schema migrations. Called on startup."""
    if IS_POSTGRES:
        conn = await asyncpg.connect(DATABASE_URL)
        try:
            # ── Create tables (only runs if they don't exist yet) ──
            await conn.execute(
                """
                CREATE TABLE IF NOT EXISTS lands (
                    id            SERIAL PRIMARY KEY,
                    guild_id      BIGINT  NOT NULL DEFAULT 0,
                    name          TEXT    NOT NULL,
                    owner_id      BIGINT  NOT NULL,
                    chunks        INTEGER NOT NULL DEFAULT 0,
                    bonus_members INTEGER NOT NULL DEFAULT 0,
                    created_at    TEXT    NOT NULL
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

                CREATE TABLE IF NOT EXISTS config (
                    guild_id BIGINT NOT NULL,
                    key      TEXT   NOT NULL,
                    value    TEXT   NOT NULL,
                    PRIMARY KEY (guild_id, key)
                );

                CREATE TABLE IF NOT EXISTS reserve (
                    guild_id        BIGINT PRIMARY KEY,
                    total_blocks    INTEGER NOT NULL DEFAULT 0,
                    protected_min   INTEGER NOT NULL DEFAULT 3,
                    leftover_blocks INTEGER NOT NULL DEFAULT 0
                );

                CREATE TABLE IF NOT EXISTS auctions (
                    id             SERIAL PRIMARY KEY,
                    guild_id       BIGINT  NOT NULL,
                    status         TEXT    NOT NULL DEFAULT 'active',
                    blocks_offered INTEGER NOT NULL DEFAULT 0,
                    started_at     TEXT    NOT NULL,
                    ends_at        TEXT    NOT NULL
                );

                CREATE TABLE IF NOT EXISTS auction_bids (
                    id          SERIAL PRIMARY KEY,
                    auction_id  INTEGER NOT NULL REFERENCES auctions(id) ON DELETE CASCADE,
                    land_id     INTEGER NOT NULL REFERENCES lands(id) ON DELETE CASCADE,
                    bid_amount  INTEGER NOT NULL,
                    bid_at      TEXT    NOT NULL,
                    UNIQUE(auction_id, land_id)
                );

                CREATE TABLE IF NOT EXISTS rotation_order (
                    id         SERIAL PRIMARY KEY,
                    guild_id   BIGINT  NOT NULL,
                    land_id    INTEGER NOT NULL REFERENCES lands(id) ON DELETE CASCADE,
                    position   INTEGER NOT NULL,
                    UNIQUE(guild_id, land_id)
                );

                CREATE TABLE IF NOT EXISTS rotation_offers (
                    id          SERIAL PRIMARY KEY,
                    guild_id    BIGINT  NOT NULL,
                    land_id     INTEGER NOT NULL REFERENCES lands(id) ON DELETE CASCADE,
                    message_id  BIGINT,
                    status      TEXT    NOT NULL DEFAULT 'pending',
                    offered_at  TEXT    NOT NULL,
                    resolved_at TEXT
                );
                """
            )

            # ── Migrations for existing databases ──
            migrations = [
                "ALTER TABLE lands ADD COLUMN IF NOT EXISTS guild_id BIGINT DEFAULT 0",
                "ALTER TABLE lands ADD COLUMN IF NOT EXISTS bonus_members INTEGER DEFAULT 0",
                "ALTER TABLE lands DROP CONSTRAINT IF EXISTS lands_name_key",
                "ALTER TABLE lands DROP CONSTRAINT IF EXISTS lands_owner_id_key",
                "ALTER TABLE reserve ADD COLUMN IF NOT EXISTS leftover_blocks INTEGER DEFAULT 0",
            ]
            for sql in migrations:
                try:
                    await conn.execute(sql)
                except Exception as e:
                    log.warning(f"Migration skipped: {e}")
        finally:
            await conn.close()
    else:
        async with aiosqlite.connect(DB_PATH) as db:
            await db.executescript(
                """
                CREATE TABLE IF NOT EXISTS lands (
                    id            INTEGER PRIMARY KEY AUTOINCREMENT,
                    guild_id      INTEGER NOT NULL DEFAULT 0,
                    name          TEXT    NOT NULL COLLATE NOCASE,
                    owner_id      INTEGER NOT NULL,
                    chunks        INTEGER NOT NULL DEFAULT 0,
                    bonus_members INTEGER NOT NULL DEFAULT 0,
                    created_at    TEXT    NOT NULL
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
                    guild_id       INTEGER PRIMARY KEY,
                    total_blocks   INTEGER NOT NULL DEFAULT 0,
                    protected_min  INTEGER NOT NULL DEFAULT 3,
                    leftover_blocks INTEGER NOT NULL DEFAULT 0
                );

                CREATE TABLE IF NOT EXISTS config (
                    guild_id INTEGER NOT NULL,
                    key      TEXT    NOT NULL,
                    value    TEXT    NOT NULL,
                    PRIMARY KEY (guild_id, key)
                );

                CREATE TABLE IF NOT EXISTS auctions (
                    id             INTEGER PRIMARY KEY AUTOINCREMENT,
                    guild_id       INTEGER NOT NULL,
                    status         TEXT    NOT NULL DEFAULT 'active',
                    blocks_offered INTEGER NOT NULL DEFAULT 0,
                    started_at     TEXT    NOT NULL,
                    ends_at        TEXT    NOT NULL
                );

                CREATE TABLE IF NOT EXISTS auction_bids (
                    id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    auction_id  INTEGER NOT NULL REFERENCES auctions(id) ON DELETE CASCADE,
                    land_id     INTEGER NOT NULL REFERENCES lands(id) ON DELETE CASCADE,
                    bid_amount  INTEGER NOT NULL,
                    bid_at      TEXT    NOT NULL,
                    UNIQUE(auction_id, land_id)
                );

                CREATE TABLE IF NOT EXISTS rotation_order (
                    id         INTEGER PRIMARY KEY AUTOINCREMENT,
                    guild_id   INTEGER NOT NULL,
                    land_id    INTEGER NOT NULL REFERENCES lands(id) ON DELETE CASCADE,
                    position   INTEGER NOT NULL,
                    UNIQUE(guild_id, land_id)
                );

                CREATE TABLE IF NOT EXISTS rotation_offers (
                    id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    guild_id    INTEGER NOT NULL,
                    land_id     INTEGER NOT NULL REFERENCES lands(id) ON DELETE CASCADE,
                    message_id  INTEGER,
                    status      TEXT    NOT NULL DEFAULT 'pending',
                    offered_at  TEXT    NOT NULL,
                    resolved_at TEXT
                );
                """
            )
            # Soft migrations
            for migration in [
                "ALTER TABLE lands ADD COLUMN guild_id INTEGER DEFAULT 0",
                "ALTER TABLE lands ADD COLUMN bonus_members INTEGER DEFAULT 0",
                "ALTER TABLE reserve ADD COLUMN leftover_blocks INTEGER DEFAULT 0",
            ]:
                try:
                    await db.execute(migration)
                except Exception:
                    pass
            await db.commit()


# ── Config helpers ────────────────────────────────────────────────────

async def get_config(guild_id: int, key: str) -> str:
    async with DBConnection() as conn:
        row = await conn.fetchone(
            "SELECT value FROM config WHERE guild_id = ? AND key = ?",
            (guild_id, key),
        )
        if row:
            return row["value"]
        return DEFAULT_CONFIG.get(key, "")


async def set_config(guild_id: int, key: str, value: str):
    async with DBConnection() as conn:
        if IS_POSTGRES:
            await conn.execute(
                "INSERT INTO config (guild_id, key, value) VALUES (?, ?, ?) "
                "ON CONFLICT (guild_id, key) DO UPDATE SET value = EXCLUDED.value",
                (guild_id, key, value),
            )
        else:
            await conn.execute(
                "INSERT INTO config (guild_id, key, value) VALUES (?, ?, ?) "
                "ON CONFLICT(guild_id, key) DO UPDATE SET value = excluded.value",
                (guild_id, key, value),
            )


async def get_full_block_cost(guild_id: int) -> int:
    val = await get_config(guild_id, "full_block_cost")
    return int(val) if val else 50


async def get_staff_role_id(guild_id: int) -> int:
    val = await get_config(guild_id, "staff_role_id")
    return int(val) if val else 0


# ── Land helpers ──────────────────────────────────────────────────────

async def create_land(guild_id: int, name: str, owner_id: int, chunks: int = 0) -> int:
    now = datetime.now(timezone.utc).isoformat()
    async with DBConnection() as conn:
        row = await conn.fetchone(
            "INSERT INTO lands (guild_id, name, owner_id, chunks, created_at) VALUES (?, ?, ?, ?, ?) RETURNING id",
            (guild_id, name, owner_id, chunks, now),
        )
        return row["id"]


async def get_land_by_owner(guild_id: int, owner_id: int) -> dict | None:
    async with DBConnection() as conn:
        return await conn.fetchone(
            "SELECT * FROM lands WHERE guild_id = ? AND owner_id = ?",
            (guild_id, owner_id),
        )


async def get_land_by_name(guild_id: int, name: str) -> dict | None:
    async with DBConnection() as conn:
        return await conn.fetchone(
            "SELECT * FROM lands WHERE guild_id = ? AND LOWER(name) = LOWER(?)",
            (guild_id, name),
        )


async def get_land_by_id(land_id: int) -> dict | None:
    async with DBConnection() as conn:
        return await conn.fetchone("SELECT * FROM lands WHERE id = ?", (land_id,))


async def update_land_chunks(land_id: int, new_chunks: int):
    async with DBConnection() as conn:
        await conn.execute("UPDATE lands SET chunks = ? WHERE id = ?", (new_chunks, land_id))


async def get_land_for_user(guild_id: int, user_id: int) -> dict | None:
    """Get a land where the user is either owner or member in this guild."""
    land = await get_land_by_owner(guild_id, user_id)
    if land:
        return land
    async with DBConnection() as conn:
        return await conn.fetchone(
            "SELECT l.* FROM lands l JOIN members m ON l.id = m.land_id "
            "WHERE l.guild_id = ? AND m.user_id = ?",
            (guild_id, user_id),
        )


async def get_all_lands(guild_id: int) -> list[dict]:
    """Get all registered lands in this guild ordered by name."""
    async with DBConnection() as conn:
        return await conn.fetchall(
            "SELECT * FROM lands WHERE guild_id = ? ORDER BY name ASC",
            (guild_id,),
        )


async def delete_land(land_id: int):
    """Delete a land by ID (cascades to members, requests, purchases, and rotation)."""
    async with DBConnection() as conn:
        row = await conn.fetchone("SELECT guild_id FROM lands WHERE id = ?", (land_id,))
        guild_id = row["guild_id"] if row else None
        await conn.execute("DELETE FROM rotation_order WHERE land_id = ?", (land_id,))
        await conn.execute("DELETE FROM lands WHERE id = ?", (land_id,))
    if guild_id:
        await renumber_rotation(guild_id)


# ── Member helpers ────────────────────────────────────────────────────

async def add_member(land_id: int, user_id: int):
    async with DBConnection() as conn:
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
        m_row = await conn.fetchone("SELECT COUNT(*) as count FROM members WHERE land_id = ?", (land_id,))
        l_row = await conn.fetchone("SELECT bonus_members FROM lands WHERE id = ?", (land_id,))
        discord_count = m_row["count"] if m_row else 0
        bonus_count = (l_row["bonus_members"] if l_row and "bonus_members" in l_row and l_row["bonus_members"] else 0)
        return discord_count + bonus_count


async def update_bonus_members(land_id: int, bonus_members: int):
    async with DBConnection() as conn:
        await conn.execute("UPDATE lands SET bonus_members = ? WHERE id = ?", (bonus_members, land_id))


async def get_members(land_id: int) -> list[int]:
    async with DBConnection() as conn:
        rows = await conn.fetchall("SELECT user_id FROM members WHERE land_id = ?", (land_id,))
        return [r["user_id"] for r in rows]


# ── Claim request helpers ─────────────────────────────────────────────

async def create_claim_request(land_id: int, chunks_requested: int, purpose: str) -> int | None:
    now = datetime.now(timezone.utc).isoformat()
    async with DBConnection() as conn:
        existing = await conn.fetchone(
            "SELECT id FROM claim_requests WHERE land_id = ? AND status = 'pending'",
            (land_id,),
        )
        if existing:
            return None
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


async def get_all_pending_requests(guild_id: int) -> list[dict]:
    async with DBConnection() as conn:
        return await conn.fetchall(
            "SELECT cr.*, l.name as land_name, l.chunks, l.owner_id "
            "FROM claim_requests cr "
            "JOIN lands l ON cr.land_id = l.id "
            "WHERE l.guild_id = ? AND cr.status = 'pending' "
            "ORDER BY cr.requested_at ASC",
            (guild_id,),
        )


async def update_request_status(request_id: int, status: str):
    async with DBConnection() as conn:
        await conn.execute("UPDATE claim_requests SET status = ? WHERE id = ?", (status, request_id))


async def update_request_chunks(request_id: int, new_chunks: int):
    async with DBConnection() as conn:
        await conn.execute(
            "UPDATE claim_requests SET chunks_requested = ? WHERE id = ?",
            (new_chunks, request_id),
        )


async def get_expired_approved_requests(guild_id: int, days: int = 7) -> list[dict]:
    from datetime import timedelta
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    async with DBConnection() as conn:
        return await conn.fetchall(
            "SELECT cr.*, l.name as land_name, l.chunks, l.owner_id "
            "FROM claim_requests cr "
            "JOIN lands l ON cr.land_id = l.id "
            "WHERE l.guild_id = ? AND cr.status = 'approved' AND cr.requested_at < ? "
            "ORDER BY cr.requested_at ASC",
            (guild_id, cutoff),
        )


# ── Purchase helpers ──────────────────────────────────────────────────

async def record_purchase(land_id: int, purchase_type: str, price_paid: int):
    now = datetime.now(timezone.utc).isoformat()
    async with DBConnection() as conn:
        await conn.execute(
            "INSERT INTO purchases (land_id, type, price_paid, purchased_at) VALUES (?, ?, ?, ?)",
            (land_id, purchase_type, price_paid, now),
        )


async def get_normal_purchase_this_week(land_id: int) -> dict | None:
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

async def get_reserve(guild_id: int) -> dict:
    async with DBConnection() as conn:
        row = await conn.fetchone("SELECT * FROM reserve WHERE guild_id = ?", (guild_id,))
        if not row:
            if IS_POSTGRES:
                await conn.execute(
                    "INSERT INTO reserve (guild_id, total_blocks, protected_min, leftover_blocks) VALUES (?, 0, 3, 0) "
                    "ON CONFLICT (guild_id) DO NOTHING",
                    (guild_id,),
                )
            else:
                await conn.execute(
                    "INSERT OR IGNORE INTO reserve (guild_id, total_blocks, protected_min, leftover_blocks) VALUES (?, 0, 3, 0)",
                    (guild_id,),
                )
            row = await conn.fetchone("SELECT * FROM reserve WHERE guild_id = ?", (guild_id,))
        return row


async def update_reserve_blocks(guild_id: int, new_total: int):
    async with DBConnection() as conn:
        await get_reserve(guild_id)
        await conn.execute(
            "UPDATE reserve SET total_blocks = ? WHERE guild_id = ?",
            (new_total, guild_id),
        )


async def add_reserve_blocks(guild_id: int, amount: int):
    async with DBConnection() as conn:
        await get_reserve(guild_id)
        await conn.execute(
            "UPDATE reserve SET total_blocks = total_blocks + ? WHERE guild_id = ?",
            (amount, guild_id),
        )


async def update_leftover_blocks(guild_id: int, new_total: int):
    async with DBConnection() as conn:
        await get_reserve(guild_id)
        await conn.execute(
            "UPDATE reserve SET leftover_blocks = ? WHERE guild_id = ?",
            (new_total, guild_id),
        )


async def add_leftover_blocks(guild_id: int, amount: int):
    async with DBConnection() as conn:
        await get_reserve(guild_id)
        await conn.execute(
            "UPDATE reserve SET leftover_blocks = leftover_blocks + ? WHERE guild_id = ?",
            (amount, guild_id),
        )


async def update_protected_min(guild_id: int, protected_min: int):
    async with DBConnection() as conn:
        await get_reserve(guild_id)
        await conn.execute(
            "UPDATE reserve SET protected_min = ? WHERE guild_id = ?",
            (protected_min, guild_id),
        )


# ── Auction helpers ───────────────────────────────────────────────────

async def create_auction(guild_id: int, blocks_offered: int, started_at: str, ends_at: str) -> int:
    async with DBConnection() as conn:
        if IS_POSTGRES:
            row = await conn.fetchone(
                "INSERT INTO auctions (guild_id, status, blocks_offered, started_at, ends_at) "
                "VALUES (?, 'active', ?, ?, ?) RETURNING id",
                (guild_id, blocks_offered, started_at, ends_at),
            )
            return row["id"]
        else:
            await conn.execute(
                "INSERT INTO auctions (guild_id, status, blocks_offered, started_at, ends_at) "
                "VALUES (?, 'active', ?, ?, ?)",
                (guild_id, blocks_offered, started_at, ends_at),
            )
            row = await conn.fetchone("SELECT last_insert_rowid() as id")
            return row["id"]


async def get_active_auction(guild_id: int) -> dict | None:
    async with DBConnection() as conn:
        return await conn.fetchone(
            "SELECT * FROM auctions WHERE guild_id = ? AND status = 'active' ORDER BY id DESC LIMIT 1",
            (guild_id,),
        )


async def place_auction_bid(auction_id: int, land_id: int, bid_amount: int):
    now = datetime.now(timezone.utc).isoformat()
    async with DBConnection() as conn:
        if IS_POSTGRES:
            await conn.execute(
                "INSERT INTO auction_bids (auction_id, land_id, bid_amount, bid_at) VALUES (?, ?, ?, ?) "
                "ON CONFLICT (auction_id, land_id) DO UPDATE SET bid_amount = EXCLUDED.bid_amount, bid_at = EXCLUDED.bid_at",
                (auction_id, land_id, bid_amount, now),
            )
        else:
            await conn.execute(
                "INSERT INTO auction_bids (auction_id, land_id, bid_amount, bid_at) VALUES (?, ?, ?, ?) "
                "ON CONFLICT(auction_id, land_id) DO UPDATE SET bid_amount = excluded.bid_amount, bid_at = excluded.bid_at",
                (auction_id, land_id, bid_amount, now),
            )


async def get_auction_bids(auction_id: int) -> list[dict]:
    """Get all bids for an auction ordered highest bid first."""
    async with DBConnection() as conn:
        return await conn.fetchall(
            "SELECT b.*, l.name as land_name, l.owner_id, l.chunks FROM auction_bids b "
            "JOIN lands l ON b.land_id = l.id WHERE b.auction_id = ? ORDER BY b.bid_amount DESC, b.bid_at ASC",
            (auction_id,),
        )


async def close_auction(auction_id: int):
    async with DBConnection() as conn:
        await conn.execute("UPDATE auctions SET status = 'ended' WHERE id = ?", (auction_id,))


async def cancel_auction(auction_id: int):
    async with DBConnection() as conn:
        await conn.execute("UPDATE auctions SET status = 'cancelled' WHERE id = ?", (auction_id,))


# ── Rotation helpers ──────────────────────────────────────────────────

async def renumber_rotation(guild_id: int):
    """Strictly cleans and renumbers all lands in the rotation to 1, 2, ..., N."""
    async with DBConnection() as conn:
        # Delete any rotation entries pointing to lands that don't exist anymore
        await conn.execute(
            "DELETE FROM rotation_order WHERE guild_id = ? AND land_id NOT IN (SELECT id FROM lands WHERE guild_id = ?)",
            (guild_id, guild_id),
        )
        rows = await conn.fetchall(
            "SELECT id FROM rotation_order WHERE guild_id = ? ORDER BY position ASC, id ASC",
            (guild_id,),
        )
        for idx, r in enumerate(rows, start=1):
            await conn.execute(
                "UPDATE rotation_order SET position = ? WHERE id = ?",
                (idx, r["id"]),
            )


async def get_rotation_order(guild_id: int) -> list[dict]:
    """Get all lands in the rotation for this guild, ordered by position (auto-renumbered)."""
    await renumber_rotation(guild_id)
    async with DBConnection() as conn:
        return await conn.fetchall(
            "SELECT r.*, l.name as land_name, l.owner_id, l.chunks FROM rotation_order r "
            "JOIN lands l ON r.land_id = l.id WHERE r.guild_id = ? ORDER BY r.position ASC",
            (guild_id,),
        )


async def add_to_rotation(guild_id: int, land_id: int, position: int = None):
    """Add a land to the rotation. If position is None, add to the end."""
    async with DBConnection() as conn:
        if position is None or position < 1:
            row = await conn.fetchone(
                "SELECT COALESCE(MAX(position), 0) + 1 as next_pos FROM rotation_order WHERE guild_id = ?",
                (guild_id,),
            )
            position = row["next_pos"]
        else:
            # Shift existing entries down to make room
            await conn.execute(
                "UPDATE rotation_order SET position = position + 1 WHERE guild_id = ? AND position >= ?",
                (guild_id, position),
            )
        if IS_POSTGRES:
            await conn.execute(
                "INSERT INTO rotation_order (guild_id, land_id, position) VALUES (?, ?, ?) "
                "ON CONFLICT (guild_id, land_id) DO UPDATE SET position = EXCLUDED.position",
                (guild_id, land_id, position),
            )
        else:
            await conn.execute(
                "INSERT OR REPLACE INTO rotation_order (guild_id, land_id, position) VALUES (?, ?, ?)",
                (guild_id, land_id, position),
            )
    await renumber_rotation(guild_id)


async def remove_from_rotation(guild_id: int, land_id: int) -> bool:
    """Remove a land from the rotation and auto-renumber remaining lands."""
    async with DBConnection() as conn:
        row = await conn.fetchone(
            "SELECT 1 FROM rotation_order WHERE guild_id = ? AND land_id = ?",
            (guild_id, land_id),
        )
        if not row:
            return False
        await conn.execute(
            "DELETE FROM rotation_order WHERE guild_id = ? AND land_id = ?",
            (guild_id, land_id),
        )
    await renumber_rotation(guild_id)
    return True


async def move_to_bottom(guild_id: int, land_id: int):
    """Move a land to the bottom of the rotation and renumber."""
    async with DBConnection() as conn:
        row = await conn.fetchone(
            "SELECT position FROM rotation_order WHERE guild_id = ? AND land_id = ?",
            (guild_id, land_id),
        )
        if not row:
            return
        max_row = await conn.fetchone(
            "SELECT COALESCE(MAX(position), 0) as max_pos FROM rotation_order WHERE guild_id = ?",
            (guild_id,),
        )
        max_pos = max_row["max_pos"]
        await conn.execute(
            "UPDATE rotation_order SET position = ? WHERE guild_id = ? AND land_id = ?",
            (max_pos + 1, guild_id, land_id),
        )
    await renumber_rotation(guild_id)


async def set_rotation_position(guild_id: int, land_id: int, new_position: int):
    """Move a land to a specific position in the rotation."""
    await remove_from_rotation(guild_id, land_id)
    await add_to_rotation(guild_id, land_id, new_position)


async def create_rotation_offer(guild_id: int, land_id: int, message_id: int = None) -> int:
    """Create a new rotation offer record."""
    now = datetime.now(timezone.utc).isoformat()
    async with DBConnection() as conn:
        if IS_POSTGRES:
            row = await conn.fetchone(
                "INSERT INTO rotation_offers (guild_id, land_id, message_id, status, offered_at) "
                "VALUES (?, ?, ?, 'pending', ?) RETURNING id",
                (guild_id, land_id, message_id, now),
            )
            return row["id"]
        else:
            await conn.execute(
                "INSERT INTO rotation_offers (guild_id, land_id, message_id, status, offered_at) "
                "VALUES (?, ?, ?, 'pending', ?)",
                (guild_id, land_id, message_id, now),
            )
            row = await conn.fetchone("SELECT last_insert_rowid() as id")
            return row["id"]


async def update_rotation_offer(offer_id: int, status: str, message_id: int = None):
    """Update a rotation offer's status and optionally its message_id."""
    now = datetime.now(timezone.utc).isoformat()
    async with DBConnection() as conn:
        if message_id is not None:
            await conn.execute(
                "UPDATE rotation_offers SET status = ?, message_id = ?, resolved_at = ? WHERE id = ?",
                (status, message_id, now, offer_id),
            )
        else:
            await conn.execute(
                "UPDATE rotation_offers SET status = ?, resolved_at = ? WHERE id = ?",
                (status, now, offer_id),
            )


async def get_pending_rotation_offer(guild_id: int) -> dict | None:
    """Get the currently pending rotation offer for a guild."""
    async with DBConnection() as conn:
        return await conn.fetchone(
            "SELECT o.*, l.name as land_name, l.owner_id, l.chunks FROM rotation_offers o "
            "JOIN lands l ON o.land_id = l.id "
            "WHERE o.guild_id = ? AND o.status = 'pending' ORDER BY o.id DESC LIMIT 1",
            (guild_id,),
        )


async def get_last_rotation_date(guild_id: int) -> str | None:
    """Get the date string (YYYY-MM-DD) of the last rotation offer for this guild."""
    async with DBConnection() as conn:
        row = await conn.fetchone(
            "SELECT offered_at FROM rotation_offers WHERE guild_id = ? ORDER BY id DESC LIMIT 1",
            (guild_id,),
        )
        if row and row["offered_at"]:
            return row["offered_at"][:10]  # YYYY-MM-DD
        return None


async def get_rotation_offers_today(guild_id: int, date_str: str) -> list[dict]:
    """Get all rotation offers for a guild on a specific date."""
    async with DBConnection() as conn:
        return await conn.fetchall(
            "SELECT o.*, l.name as land_name, l.owner_id, l.chunks FROM rotation_offers o "
            "JOIN lands l ON o.land_id = l.id "
            "WHERE o.guild_id = ? AND o.offered_at LIKE ? ORDER BY o.id ASC",
            (guild_id, date_str + "%"),
        )

