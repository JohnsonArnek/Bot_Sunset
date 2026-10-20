import asyncio
import json
import subprocess
import sys
import time
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import aiohttp
import discord

import database
from failover import Runtime, is_cloudflare_ban, retry_after_seconds
from lease import LeaseStore
from watchdog import Watchdog, parse_services


def http_error(body, retry=None):
    headers = {} if retry is None else {"Retry-After": str(retry)}
    return discord.HTTPException(
        SimpleNamespace(status=429, reason="rate limited", headers=headers), body,
    )


class RateLimitTests(unittest.TestCase):
    def test_only_edge_blocks_are_failover_signals(self):
        runtime = Runtime(Mock(), enabled=True)
        runtime.discord_error(http_error({"message": "Rate limited", "retry_after": 4}))
        self.assertFalse(runtime.failure.is_set())
        runtime.discord_error(http_error("<html>Cloudflare error 1015</html>", 7200))
        self.assertTrue(runtime.failure.is_set())
        self.assertEqual(runtime.last_error, "discord_ip_block")
        self.assertEqual(runtime.cooldown_seconds, 7200)
        runtime.fail("discord_session_failed")
        self.assertEqual(runtime.last_error, "discord_ip_block")

    def test_bad_retry_headers_have_safe_default(self):
        for header in ("nan", "inf", "bad"):
            self.assertEqual(retry_after_seconds(http_error("rate limit", header), 60), 60)
        self.assertFalse(is_cloudflare_ban(http_error({"message": "slow down"})))

    def test_late_acquisition_response_does_not_grant_local_authority(self):
        lease = LeaseStore(None, "test", "render-a")
        row = {"generation": 1, "lease_until": SimpleNamespace(isoformat=lambda: "later")}
        self.assertFalse(lease._accept(row, time.monotonic() - 100))
        self.assertFalse(lease.valid())

    def test_service_mapping_rejects_credentials_and_non_https(self):
        for raw in ('[]', '{"a":"http://a","b":"https://b"}',
                    '{"a":"https://user:pass@a","b":"https://b"}'):
            with self.assertRaises(ValueError):
                parse_services(raw)

    def test_safety_watchdog_exits_even_without_an_async_event_loop(self):
        result = subprocess.run(
            [sys.executable, "-c",
             "from failover import Runtime; from types import SimpleNamespace; "
             "r=Runtime(None); r.lease=SimpleNamespace(deadline=0); r._watch_lease()"],
            capture_output=True, timeout=15,
        )
        self.assertEqual(result.returncode, 1)


class HttpTests(unittest.IsolatedAsyncioTestCase):
    async def test_liveness_and_readiness_are_distinct(self):
        bot = Mock()
        bot.is_ready.return_value = True
        runtime = Runtime(bot, enabled=True, instance="a")
        runtime.lease = SimpleNamespace(
            valid=lambda: True, lease_until="future", generation=1,
        )
        runtime.role = "standby"
        runtime.database_ok = True
        runtime.rest_succeeded()
        runtime.start_http(0)
        url = f"http://127.0.0.1:{runtime.server.server_port}"
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url + "/health") as response:
                    self.assertEqual(response.status, 200)
                    self.assertEqual(await response.json(), {"alive": True})
                async with session.get(url + "/ready") as response:
                    self.assertEqual(response.status, 503)
                runtime.role = "leader"
                async with session.get(url + "/ready") as response:
                    self.assertEqual(response.status, 200)
                    self.assertNotIn("token", json.dumps(await response.json()))
                runtime.rest_success_at -= 180
                async with session.get(url + "/ready") as response:
                    self.assertEqual(response.status, 503)
                async with session.get(url + "/unknown") as response:
                    self.assertEqual(response.status, 404)
        finally:
            await asyncio.to_thread(runtime.server.shutdown)
            runtime.server.server_close()


class Response:
    def __init__(self, status=200, body=None, text=None, headers=None):
        self.status = status
        self.body = {"alive": True} if body is None else body
        self.raw = text if text is not None else json.dumps(self.body)
        self.headers = headers or {}

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass

    async def json(self):
        return self.body

    async def text(self):
        return self.raw


class WatchdogTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.pool = SimpleNamespace(fetchrow=AsyncMock(), fetch=AsyncMock(return_value=[]))
        self.watchdog = Watchdog(self.pool, {"a": "https://a", "b": "https://b"})
        self.session = Mock()
        self.session.get.return_value = Response()

    async def test_healthy_backup_is_only_pinged_service(self):
        self.pool.fetchrow.return_value = {
            "leased": True, "instance_id": "b", "healthy": True, "fresh": True,
        }
        self.assertTrue((await self.watchdog.tick(self.session))["healthy"])
        self.session.get.assert_called_once_with("https://b/health", allow_redirects=False)

    async def test_expired_lease_wakes_one_eligible_service(self):
        self.pool.fetchrow.return_value = {"leased": False}
        self.pool.fetch.return_value = [{"instance_id": "a"}]
        result = await self.watchdog.tick(self.session)
        self.assertFalse(result["healthy"])
        self.session.get.assert_called_once_with("https://b/health", allow_redirects=False)

    async def test_broken_wake_target_is_rotated(self):
        self.pool.fetchrow.return_value = None
        self.session.get.side_effect = TimeoutError
        await self.watchdog.tick(self.session)
        await self.watchdog.tick(self.session)
        self.assertEqual([c.args[0] for c in self.session.get.call_args_list],
                         ["https://a/health", "https://b/health"])

    async def test_starting_leader_does_not_wake_standby(self):
        self.pool.fetchrow.return_value = {
            "leased": True, "instance_id": "a", "healthy": False, "fresh": True,
        }
        self.assertFalse((await self.watchdog.tick(self.session))["healthy"])
        self.session.get.assert_called_once_with("https://a/health", allow_redirects=False)

    async def test_all_cooling_down_does_not_ping_either(self):
        self.pool.fetchrow.return_value = None
        self.pool.fetch.return_value = [{"instance_id": "a"}, {"instance_id": "b"}]
        result = await self.watchdog.tick(self.session)
        self.assertEqual(result["reason"], "all_instances_cooling_down")
        self.session.get.assert_not_called()

    async def test_database_failure_does_not_wake_any_service(self):
        self.pool.fetchrow.side_effect = TimeoutError
        with self.assertRaises(TimeoutError):
            await self.watchdog.tick(self.session)
        self.session.get.assert_not_called()


class SupervisorTests(unittest.IsolatedAsyncioTestCase):
    async def test_standby_does_not_start_discord_or_load_cogs(self):
        polled = asyncio.Event()

        async def unavailable():
            polled.set()
            return False

        lease = SimpleNamespace(
            initialize=AsyncMock(), acquire=AsyncMock(side_effect=unavailable),
        )
        runtime = Runtime(Mock(), enabled=True)
        start = AsyncMock()
        with patch("failover.LeaseStore.connect", new=AsyncMock(return_value=lease)):
            task = asyncio.create_task(runtime.run("unused", start))
            await polled.wait()
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        start.assert_not_awaited()
        self.assertEqual(runtime.role, "standby")

    async def test_normal_probe_rate_limit_preserves_leadership(self):
        runtime = Runtime(Mock(), enabled=True)
        session = Mock()
        session.get.return_value = Response(status=429, body={"retry_after": 120})
        delay, success = await runtime._probe(session, "test-token")
        self.assertEqual(delay, 121)
        self.assertIsNone(success)
        self.assertFalse(runtime.failure.is_set())

    async def test_probe_detects_edge_block_even_with_connected_gateway(self):
        runtime = Runtime(Mock(), enabled=True)
        session = Mock()
        session.get.return_value = Response(status=429, text="<html>Cloudflare 1015</html>")
        await runtime._probe(session, "test-token")
        self.assertTrue(runtime.failure.is_set())
        self.assertEqual(runtime.last_error, "discord_ip_block")

    async def test_lease_renewal_failure_stops_serving(self):
        bot = Mock()
        runtime = Runtime(bot, enabled=True)
        runtime.lease = SimpleNamespace(renew=AsyncMock(side_effect=TimeoutError))
        with patch("failover.asyncio.sleep", new=AsyncMock()):
            await runtime._renew()
        self.assertFalse(runtime.database_ok)
        self.assertTrue(runtime.failure.is_set())

    async def test_cooldown_and_shutdown_precede_release(self):
        events = []
        child_started = asyncio.Event()

        async def child():
            child_started.set()
            try:
                await asyncio.Future()
            finally:
                events.append("child_stopped")

        async def start():
            asyncio.create_task(child())
            await child_started.wait()
            raise http_error("<html>Cloudflare error 1015</html>")

        async def mark_close():
            events.append("closed")

        async def mark_cooldown(*args):
            events.append("cooldown")

        async def mark_release():
            events.append("released")

        bot = Mock()
        bot.close = AsyncMock(side_effect=mark_close)
        bot.is_ready.return_value = False
        runtime = Runtime(bot, enabled=True, instance="a")
        lease = SimpleNamespace(
            initialize=AsyncMock(), acquire=AsyncMock(return_value=True),
            generation=1, deadline=time.monotonic() + 80, revoked=False,
            cooldown=AsyncMock(side_effect=mark_cooldown),
            release=AsyncMock(side_effect=mark_release),
            pool=SimpleNamespace(close=AsyncMock()),
        )
        with patch("failover.LeaseStore.connect", new=AsyncMock(return_value=lease)), \
                patch.object(runtime, "_watch_lease"), \
                patch.object(database, "leader_lease", None):
            result = await runtime.run("test-token", start)
        self.assertEqual(result, 1)
        self.assertEqual(events, ["cooldown", "closed", "child_stopped", "released"])
        self.assertTrue(lease.revoked)
        lease.cooldown.assert_awaited_once_with(1800, "discord_ip_block")


if __name__ == "__main__":
    unittest.main()
