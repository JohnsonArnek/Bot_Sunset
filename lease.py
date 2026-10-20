"""Postgres leader leases. All ownership decisions use the database clock."""

import asyncio
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

import asyncpg


class LeaseLost(RuntimeError):
    pass


class LeaseStore:
    def __init__(self, pool, group, instance, ttl=90):
        self.pool = pool
        self.group = group
        self.instance = instance
        # A service name alone cannot distinguish overlapping Render deploys.
        self.owner = uuid.uuid4().hex
        self.ttl = ttl
        self.generation = None
        self.deadline = 0.0
        self.lease_until = None
        self.revoked = False

    @classmethod
    async def connect(cls, url, group, instance):
        pool = await asyncpg.create_pool(
            url, min_size=1, max_size=2, timeout=10, command_timeout=10,
            statement_cache_size=0,
        )
        return cls(pool, group, instance)

    async def initialize(self):
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                # Serializes first boot DDL, including on transaction poolers.
                await conn.execute("SELECT pg_advisory_xact_lock(728461903)")
                await conn.execute(
                    Path(__file__).with_name("failover_schema.sql").read_text()
                )
                await conn.execute(
                    "INSERT INTO bot_leases (bot_group) VALUES ($1) "
                    "ON CONFLICT DO NOTHING", self.group,
                )

    def valid(self):
        return not self.revoked and self.generation is not None and time.monotonic() < self.deadline

    def _accept(self, row, started):
        if row is None:
            return False
        # Starting the deadline BEFORE the query accounts for network latency.
        # Stop ten seconds before the database could grant a successor.
        self.deadline = started + self.ttl - 10
        self.generation = row["generation"]
        self.lease_until = row["lease_until"].isoformat()
        return self.valid()

    async def acquire(self):
        started = time.monotonic()
        row = await self.pool.fetchrow(
            """
            UPDATE bot_leases SET owner = $2, instance_id = $3,
                generation = generation + 1,
                lease_until = clock_timestamp() + $4 * interval '1 second',
                healthy = false, last_heartbeat = clock_timestamp()
            WHERE bot_group = $1
              AND (owner IS NULL OR lease_until <= clock_timestamp())
              AND NOT EXISTS (
                  SELECT 1 FROM bot_instance_cooldowns
                  WHERE bot_group = $1 AND instance_id = $3
                    AND retry_at > clock_timestamp()
              )
            RETURNING generation, lease_until
            """, self.group, self.owner, self.instance, self.ttl,
        )
        return self._accept(row, started)

    async def renew(self, healthy):
        if not self.valid():
            return False
        started = time.monotonic()
        row = await self.pool.fetchrow(
            """
            UPDATE bot_leases
            SET lease_until = clock_timestamp() + $4 * interval '1 second',
                healthy = $5, last_heartbeat = clock_timestamp()
            WHERE bot_group = $1 AND owner = $2 AND generation = $3
              AND lease_until > clock_timestamp()
            RETURNING generation, lease_until
            """, self.group, self.owner, self.generation, self.ttl, healthy,
        )
        return self._accept(row, started)

    async def cooldown(self, seconds, reason):
        await self.pool.execute(
            """
            INSERT INTO bot_instance_cooldowns
                (bot_group, instance_id, retry_at, reason)
            VALUES ($1, $2, clock_timestamp() + $3 * interval '1 second', $4)
            ON CONFLICT (bot_group, instance_id) DO UPDATE
            SET retry_at = GREATEST(bot_instance_cooldowns.retry_at, EXCLUDED.retry_at),
                reason = EXCLUDED.reason
            """, self.group, self.instance, seconds, reason,
        )

    async def release(self):
        await self.pool.execute(
            """
            UPDATE bot_leases SET owner = NULL, instance_id = NULL,
                healthy = false, lease_until = clock_timestamp()
            WHERE bot_group = $1 AND owner = $2 AND generation = $3
            """, self.group, self.owner, self.generation,
        )

    @asynccontextmanager
    async def fence(self, conn):
        """Keep takeover out until this application SQL operation has finished."""
        if not self.valid():
            raise LeaseLost("Local leader lease expired")
        async with conn.transaction():
            row = await conn.fetchrow(
                """
                SELECT owner, generation, lease_until > clock_timestamp() AS valid
                FROM bot_leases WHERE bot_group = $1 FOR SHARE
                """, self.group,
            )
            if (not self.valid() or row is None or not row["valid"]
                    or row["owner"] != self.owner
                    or row["generation"] != self.generation):
                raise LeaseLost("Database leader lease changed")
            yield


async def bounded(awaitable):
    """Bound pool acquisition as well as the SQL itself."""
    async with asyncio.timeout(12):
        return await awaitable
