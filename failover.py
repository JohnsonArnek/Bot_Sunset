"""One Discord session per process, supervised by a renewable Postgres lease."""

import asyncio
import json
import logging
import math
import os
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

import aiohttp

import database
from lease import LeaseStore, bounded

log = logging.getLogger("claims-bot")


def is_cloudflare_ban(error):
    body = (getattr(error, "text", "") or "").lower()
    return getattr(error, "status", None) == 429 and (
        "cloudflare" in body or "<html" in body or "error 1015" in body
    )


def retry_after_seconds(error, default):
    value = getattr(error, "retry_after", None)
    if value is None:
        value = getattr(getattr(error, "response", None), "headers", {}).get("Retry-After")
    try:
        seconds = float(value)
        return max(seconds, 1.0) if math.isfinite(seconds) else default
    except (TypeError, ValueError):
        return default


class Runtime:
    def __init__(self, bot, enabled=False, instance=None, group="sunset"):
        self.bot = bot
        self.enabled = enabled
        self.instance = instance or "standalone"
        self.group = group
        self.lease = None
        self.role = "starting"
        self.database_ok = False
        self.last_rest_success = None
        self.rest_success_at = 0.0
        self.last_error = None
        self.failure = asyncio.Event()
        self.cooldown_seconds = 60
        self.server = None
        self._watchdog_stop = threading.Event()

    def serving(self):
        return (
            self.role == "leader" and not self.failure.is_set()
            and (not self.enabled or self.lease is not None and self.lease.valid())
            and self.database_ok and self.bot.is_ready()
            and time.monotonic() - self.rest_success_at < 120
        )

    def snapshot(self):
        return {
            "instance_id": self.instance, "failover_enabled": self.enabled,
            "role": self.role, "ready": self.serving(),
            "database_connected": self.database_ok,
            "discord_ready": self.bot.is_ready(),
            "lease_until": self.lease.lease_until if self.lease else None,
            "generation": self.lease.generation if self.lease else None,
            "last_discord_rest_success": self.last_rest_success,
            # Only controlled reason codes, never exception bodies or credentials.
            "last_error": self.last_error,
        }

    def rest_succeeded(self):
        self.rest_success_at = time.monotonic()
        self.last_rest_success = datetime.now(timezone.utc).isoformat()

    def fail(self, reason, cooldown=60):
        if self.enabled:
            if not self.failure.is_set():
                self.last_error = reason
            self.cooldown_seconds = max(self.cooldown_seconds, cooldown)
            self.failure.set()

    def discord_error(self, error):
        if is_cloudflare_ban(error):
            self.fail("discord_ip_block", max(1800, retry_after_seconds(error, 1800)))

    def start_http(self, port):
        runtime = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                path = urlsplit(self.path).path
                if path in ("/", "/health"):
                    code, payload = 200, {"alive": True}
                elif path in ("/status", "/ready"):
                    payload = runtime.snapshot()
                    code = 200 if path == "/status" or payload["ready"] else 503
                else:
                    code, payload = 404, {"error": "not_found"}
                data = json.dumps(payload).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                try:
                    self.wfile.write(data)
                except (BrokenPipeError, ConnectionResetError):
                    pass

            def log_message(self, *args):
                pass

        # Binding failures must fail the deployment, not leave an invisible bot.
        self.server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def _watch_lease(self):
        while not self._watchdog_stop.wait(0.5):
            if time.monotonic() >= self.lease.deadline:
                # Also works if the asyncio event loop is stuck. Do not release:
                # let the DB lease expire after this process is dead.
                log.critical("Lease safety deadline reached; terminating process")
                os._exit(1)

    async def _renew(self):
        while True:
            await asyncio.sleep(20)
            try:
                if not await bounded(self.lease.renew(self.serving())):
                    self.fail("lease_lost")
                    return
                self.database_ok = True
            except Exception:
                self.database_ok = False
                self.fail("database_unavailable")
                return

    async def _probe(self, session, token):
        """A read-only probe; ordinary 429s defer probing, never trigger takeover."""
        try:
            async with session.get(
                "https://discord.com/api/v10/users/@me",
                headers={"Authorization": f"Bot {token}"},
                allow_redirects=False,
            ) as response:
                body = await response.text()
                if response.status == 200:
                    self.rest_succeeded()
                    return 60, True
                if response.status == 429:
                    error = type("ResponseError", (), {
                        "status": 429, "text": body, "response": response,
                    })()
                    if is_cloudflare_ban(error):
                        self.discord_error(error)
                        return 60, False
                    try:
                        error.retry_after = json.loads(body).get("retry_after")
                    except (ValueError, AttributeError):
                        pass
                    return retry_after_seconds(error, 60) + 1, None
                if response.status == 401:
                    self.fail("discord_token_rejected", 3600)
                return 60, False
        except (aiohttp.ClientError, TimeoutError):
            return 60, False

    async def _monitor_discord(self, token):
        disconnected_at = time.monotonic()
        next_probe = 0.0
        failures = 0
        ever_ready = False
        async with aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=10)
        ) as session:
            while True:
                now = time.monotonic()
                if self.bot.is_ready():
                    ever_ready = True
                    disconnected_at = now
                    if now >= next_probe:
                        delay, success = await self._probe(session, token)
                        next_probe = time.monotonic() + delay
                        if success is True:
                            failures = 0
                        elif success is False:
                            failures += 1
                        if failures >= 3 and self.enabled:
                            self.fail("discord_rest_unavailable", 300)
                            return
                elif self.enabled and now - disconnected_at > (90 if ever_ready else 120):
                    self.fail("discord_gateway_unavailable", 300)
                    return
                await asyncio.sleep(2)

    async def run(self, token, start_bot):
        """Wait as standby, run once as leader, then exit for Render to restart."""
        baseline = asyncio.all_tasks()
        while True:
            try:
                if self.lease is None:
                    self.lease = await bounded(LeaseStore.connect(
                        database.DATABASE_URL, self.group, self.instance,
                    ))
                await bounded(self.lease.initialize())
                break
            except Exception as error:
                self.database_ok = False
                self.last_error = "database_unavailable"
                log.warning("Failover database unavailable (%s)", type(error).__name__)
                await asyncio.sleep(10)
        self.role = "standby"
        while True:
            try:
                acquired = await bounded(self.lease.acquire())
                self.database_ok = True
                self.last_error = None
                if acquired:
                    break
            except Exception as error:
                self.database_ok = False
                self.last_error = "database_unavailable"
                log.warning("Election unavailable (%s)", type(error).__name__)
            await asyncio.sleep(10)

        self.role = "leader"
        database.leader_lease = self.lease
        threading.Thread(target=self._watch_lease, daemon=True).start()
        log.info("Leader acquired: %s, generation %s", self.instance, self.lease.generation)
        active = asyncio.create_task(start_bot())
        renewer = asyncio.create_task(self._renew())
        monitor = asyncio.create_task(self._monitor_discord(token))
        failed = asyncio.create_task(self.failure.wait())
        try:
            done, _ = await asyncio.wait(
                [active, renewer, monitor, failed], return_when=asyncio.FIRST_COMPLETED,
            )
            if active in done:
                error = active.exception()
                if error is not None:
                    self.discord_error(error)
                    if getattr(error, "status", None) == 429 and not is_cloudflare_ban(error):
                        # A token-wide rate limit must apply to both regions.
                        # Keep ownership while waiting out Discord's Retry-After.
                        self.last_error = "discord_rate_limited"
                        self.role = "rate_limited"
                        monitor.cancel()
                        await asyncio.sleep(retry_after_seconds(error, 60))
                    elif type(error).__name__ == "LoginFailure":
                        self.fail("discord_token_rejected", 3600)
                    else:
                        self.fail("discord_session_failed")
                    log.warning("Discord session ended (%s)", type(error).__name__)
            elif not self.failure.is_set():
                self.fail("supervisor_failed")
        finally:
            self.role = "stopping"
            # Disable further application database operations immediately.
            self.lease.revoked = True
            if self.failure.is_set():
                try:
                    await bounded(self.lease.cooldown(
                        self.cooldown_seconds, self.last_error,
                    ))
                except Exception:
                    log.warning("Could not persist instance cooldown")
            # Close gateway and unload cogs BEFORE allowing a successor.
            try:
                await bounded(self.bot.close())
            except Exception:
                log.error("Discord shutdown failed; leaving lease to expire")
                os._exit(1)
            pending = asyncio.all_tasks() - baseline - {asyncio.current_task()}
            for task in pending:
                task.cancel()
            if pending:
                _, unfinished = await asyncio.wait(pending, timeout=5)
                if unfinished:
                    log.error("Tasks did not stop; leaving lease to expire")
                    os._exit(1)
            try:
                await bounded(self.lease.release())
            except Exception:
                log.warning("Could not release lease; it will expire")
            self._watchdog_stop.set()
            await bounded(self.lease.pool.close())
        # The next process gets fresh Discord clients, cogs and event tasks.
        return 1
