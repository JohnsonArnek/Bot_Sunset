"""
Land Claims Queue Bot — Entry Point

Loads config, initialises the database, registers cog extensions,
and syncs slash commands automatically to all guilds the bot is in.
"""

import os
import json
import asyncio
import logging
import threading
from pathlib import Path
from http.server import BaseHTTPRequestHandler, HTTPServer

import discord
from discord.ext import commands

import database

# ── Logging ───────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("claims-bot")

# ── Load config ───────────────────────────────────────────────────────

TOKEN = os.environ.get("DISCORD_TOKEN")

if not TOKEN:
    CONFIG_PATH = Path(__file__).parent / "config.json"
    if CONFIG_PATH.exists():
        try:
            with open(CONFIG_PATH, "r") as f:
                config = json.load(f)
                TOKEN = config.get("token")
        except Exception as e:
            log.warning(f"Failed to read config.json: {e}")

if not TOKEN or TOKEN == "YOUR_BOT_TOKEN_HERE":
    raise SystemExit(
        "⚠️  Please set the DISCORD_TOKEN environment variable or fill in config.json before running."
    )

# ── Bot setup ─────────────────────────────────────────────────────────

intents = discord.Intents.default()
intents.members = True  # Needed for member lookups

bot = commands.Bot(
    command_prefix="!",  # Slash commands are primary; prefix is fallback
    intents=intents,
    help_command=None,
)

# ── Cog loading ───────────────────────────────────────────────────────

EXTENSIONS = [
    "cogs.claims",
    "cogs.queue_cog",
    "cogs.land",
    "cogs.reserve",
    "cogs.config",
    "cogs.sunset",
    "cogs.distribution",
]


async def load_extensions():
    for ext in EXTENSIONS:
        try:
            await bot.load_extension(ext)
            log.info(f"Loaded extension: {ext}")
        except Exception as e:
            log.error(f"Failed to load extension {ext}: {e}")


# ── Events ────────────────────────────────────────────────────────────

@bot.event
async def on_ready():
    log.info(f"Logged in as {bot.user} (ID: {bot.user.id})")
    # Sync slash commands to every guild the bot is in (instant availability)
    for guild in bot.guilds:
        try:
            bot.tree.copy_global_to(guild=guild)
            synced = await bot.tree.sync(guild=guild)
            log.info(f"Synced {len(synced)} command(s) to guild: {guild.name} ({guild.id})")
        except Exception as e:
            log.error(f"Failed to sync to {guild.name}: {e}")
    log.info(f"Ready! Serving {len(bot.guilds)} guild(s).")


# ── Web Server for Health Checks ──────────────────────────────────────

class HealthCheckHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-type", "text/plain")
        self.end_headers()
        self.wfile.write(b"OK")
        
    def log_message(self, format, *args):
        # Silence HTTP request logs to keep terminal output clean
        return


def run_health_server():
    port = int(os.environ.get("PORT", 8080))
    log.info(f"Starting health check web server on port {port}...")
    try:
        server = HTTPServer(("0.0.0.0", port), HealthCheckHandler)
        server.serve_forever()
    except Exception as e:
        log.error(f"Health server failed to start: {e}")


# ── Main ──────────────────────────────────────────────────────────────

async def main():
    # Start health check server for hosting platforms in the background
    threading.Thread(target=run_health_server, daemon=True).start()
    
    await database.init_db()
    log.info("Database initialised.")
    await load_extensions()
    await bot.start(TOKEN)


if __name__ == "__main__":
    asyncio.run(main())
