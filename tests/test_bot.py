import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import discord
from discord.ext import commands

import bot as entry
import database
from failover import Runtime


class BotTests(unittest.IsolatedAsyncioTestCase):
    async def test_existing_cogs_start_loops_after_client_initialization(self):
        client = commands.Bot(command_prefix="!", intents=discord.Intents.default())
        runtime = Runtime(client, enabled=True)
        with patch.object(entry, "bot", client), patch.object(entry, "runtime", runtime):
            async with client:
                await entry.load_extensions()
                await asyncio.sleep(0)
                for cog_name, loop_name in (
                    ("DistributionCog", "distribution_loop"),
                    ("auction", "auction_loop"),
                    ("rotation", "rotation_loop"),
                ):
                    task = getattr(client.get_cog(cog_name), loop_name).get_task()
                    self.assertFalse(task.done(), cog_name)
                self.assertEqual(len(client.extensions), len(entry.EXTENSIONS))

    async def test_command_sync_uses_database_and_skips_unchanged_commands(self):
        with tempfile.TemporaryDirectory() as temp:
            with patch.object(database, "IS_POSTGRES", False), \
                    patch.object(database, "DB_PATH", str(Path(temp) / "test.db")):
                await database.init_db()
                guild = SimpleNamespace(id=123, name="test")
                client = Mock()
                client.guilds = [guild]
                client.tree.sync = AsyncMock(return_value=["command"])
                runtime = Runtime(client)
                with patch.object(entry, "bot", client), \
                        patch.object(entry, "runtime", runtime), \
                        patch.object(entry, "_command_fingerprint", return_value="test-hash"):
                    await entry.sync_commands()
                    await entry.sync_commands()
                client.tree.sync.assert_awaited_once_with(guild=guild)
                self.assertIn("test-hash", await database.get_config(0, "command_sync_state"))
