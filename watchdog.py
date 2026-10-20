"""Run on the independent pinger host, never on either sleeping bot service."""

import asyncio
import json
import logging
import os
import time
from urllib.parse import urlsplit

import aiohttp
import asyncpg

log = logging.getLogger("watchdog")


def parse_services(raw):
    services = json.loads(raw)
    if not isinstance(services, dict) or len(services) < 2:
        raise ValueError("BOT_SERVICES must map at least two instance IDs to HTTPS URLs")
    for instance, url in services.items():
        parts = urlsplit(url)
        if (not instance or parts.scheme != "https" or not parts.netloc
                or parts.username or parts.password or parts.query or parts.fragment):
            raise ValueError("BOT_SERVICES must contain instance IDs and HTTPS base URLs")
    return {instance: url.rstrip("/") for instance, url in services.items()}


class Watchdog:
    def __init__(self, pool, services, group="sunset"):
        self.pool = pool
        self.services = services
        self.group = group
        self.candidate_index = 0

    async def tick(self, session):
        """Read Supabase first so checking health does not wake a cold standby."""
        async with asyncio.timeout(15):
            row = await self.pool.fetchrow(
                """
                SELECT instance_id, healthy,
                       owner IS NOT NULL AND lease_until > clock_timestamp() AS leased,
                       last_heartbeat > clock_timestamp() - interval '45 seconds' AS fresh
                FROM bot_leases WHERE bot_group = $1
                """, self.group,
            )
            cooling = await self.pool.fetch(
                "SELECT instance_id FROM bot_instance_cooldowns "
                "WHERE bot_group = $1 AND retry_at > clock_timestamp()", self.group,
            )
        if row and row["leased"]:
            instance = row["instance_id"]
            if instance not in self.services:
                return {"healthy": False, "reason": "unknown_leader"}
            healthy = bool(row["healthy"] and row["fresh"])
        else:
            excluded = {r["instance_id"] for r in cooling}
            candidates = [name for name in self.services if name not in excluded]
            if not candidates:
                return {"healthy": False, "reason": "all_instances_cooling_down"}
            # Rotate if a suspended/broken service cannot wake or win a lease.
            instance = candidates[self.candidate_index % len(candidates)]
            self.candidate_index += 1
            healthy = False
        try:
            async with session.get(
                self.services[instance] + "/health", allow_redirects=False,
            ) as response:
                # Render's wake-up/loading response is not application health.
                payload = await response.json()
                alive = response.status == 200 and payload.get("alive") is True
        except (aiohttp.ClientError, TimeoutError, ValueError, AttributeError):
            alive = False
        return {
            "healthy": healthy and alive, "instance_id": instance,
            "reason": "serving" if healthy and alive else "waiting_for_healthy_leader",
        }


async def notify(session, url, text):
    """Optional generic JSON webhook, configured by the operator."""
    async with session.post(
        url, json={"text": text}, allow_redirects=False,
    ) as response:
        response.raise_for_status()


async def main():
    services = parse_services(os.environ["BOT_SERVICES"])
    database_url = os.environ.get("WATCHDOG_DATABASE_URL") or os.environ["DATABASE_URL"]
    group = os.environ.get("BOT_GROUP", "sunset")
    alert_url = os.environ.get("ALERT_WEBHOOK_URL")
    healthchecks_url = os.environ.get("HEALTHCHECKS_URL")
    pool = await asyncpg.create_pool(
        database_url, min_size=0, max_size=1, timeout=10,
        command_timeout=10, statement_cache_size=0,
    )
    watchdog = Watchdog(pool, services, group)
    unhealthy_since = None
    alerted = False
    previous = None
    async with aiohttp.ClientSession(
        timeout=aiohttp.ClientTimeout(total=20)
    ) as session:
        try:
            while True:
                try:
                    result = await watchdog.tick(session)
                except Exception as error:
                    # Don't log connection strings or server responses.
                    result = {"healthy": False, "reason": "database_unavailable"}
                    log.warning("Database check failed (%s)", type(error).__name__)
                if result != previous:
                    log.info("Watchdog: %s", result)
                    previous = result
                try:
                    if result["healthy"]:
                        unhealthy_since = None
                        if healthchecks_url:
                            async with session.get(
                                healthchecks_url, allow_redirects=False,
                            ) as response:
                                response.raise_for_status()
                        if alerted and alert_url:
                            await notify(session, alert_url, f"{group}: bot service recovered.")
                        alerted = False
                    else:
                        if unhealthy_since is None:
                            unhealthy_since = time.monotonic()
                        if time.monotonic() - unhealthy_since >= 300 and not alerted and alert_url:
                            await notify(
                                session, alert_url,
                                f"{group}: no verified healthy bot for 5 minutes. "
                                f"Reason: {result['reason']}. Check both Render services and Supabase.",
                            )
                            alerted = True
                except (aiohttp.ClientError, TimeoutError):
                    log.warning("Monitoring delivery failed; will retry next tick")
                await asyncio.sleep(60)
        finally:
            await pool.close()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
