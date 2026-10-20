"""
Land Claims Queue Bot — Entry Point

Loads config, initialises the database, registers cog extensions,
and syncs slash commands automatically to all guilds the bot is in.
"""

import os
import json
import asyncio
import hashlib
import logging
import threading
from pathlib import Path
from http.server import BaseHTTPRequestHandler, HTTPServer

import discord
from discord import app_commands
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
    "cogs.auction",
    "cogs.rotation",
    "cogs.ledger",
]


async def load_extensions():
    for ext in EXTENSIONS:
        try:
            await bot.load_extension(ext)
            log.info(f"Loaded extension: {ext}")
        except Exception as e:
            log.error(f"Failed to load extension {ext}: {e}")


# ── Discord API helpers ───────────────────────────────────────────────

def is_cloudflare_ban(error: discord.HTTPException) -> bool:
    """Return True when Discord's edge has temporarily banned this IP (error 1015).

    A normal Discord 429 has a JSON body with ``retry_after``. A Cloudflare ban
    returns an HTML page instead and retrying immediately only extends the ban.
    """
    body = (getattr(error, "text", "") or "").lower()
    return error.status == 429 and ("cloudflare" in body or "<html" in body)


def retry_after_seconds(error: discord.HTTPException, default: float) -> float:
    """Read ``retry_after`` from the JSON body or the Retry-After header."""
    value = getattr(error, "retry_after", None)
    if not value:
        try:
            value = error.response.headers.get("Retry-After")
        except (AttributeError, TypeError):
            value = None
    try:
        return max(float(value), 1.0) if value else default
    except (TypeError, ValueError):
        return default


def _command_fingerprint() -> str:
    """Build a stable hash of the registered slash-command tree."""

    def serialize(command):
        data = {
            "name": command.name,
            "description": getattr(command, "description", "") or "",
            "nsfw": getattr(command, "nsfw", None),
            "guild_only": getattr(command, "guild_only", None),
            "default_permissions": str(getattr(command, "default_permissions", None)),
            "params": [
                {
                    "name": param.name,
                    "description": getattr(param, "description", "") or "",
                    "type": str(param.type),
                    "required": param.required,
                    "choices": sorted(
                        (str(choice.name), str(choice.value))
                        for choice in getattr(param, "choices", [])
                    ),
                }
                for param in getattr(command, "parameters", [])
            ],
        }
        children = getattr(command, "commands", None)
        if children:
            data["children"] = sorted(
                (serialize(child) for child in children),
                key=lambda item: item["name"],
            )
        return data

    payload = sorted(
        (serialize(command) for command in bot.tree.get_commands()),
        key=lambda item: item["name"],
    )
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()


async def sync_commands():
    """Sync commands only when the tree changed, and only for guilds that are stale.

    The bot used to re-sync every guild on every process start. Render restarts
    and wakes the service often, so that created unnecessary API calls — exactly
    the kind of traffic that contributes to Discord's IP-level rate limits.
    Sync state is stored in the database under guild_id 0.
    """
    fingerprint = _command_fingerprint()

    raw_state = await db.get_config(0, "command_sync_state")
    try:
        state = json.loads(raw_state) if raw_state else {}
    except (TypeError, ValueError):
        state = {}

    stale = [guild for guild in bot.guilds if state.get(str(guild.id)) != fingerprint]
    if not stale:
        log.info("Slash commands already up to date — skipping sync.")
        return

    for index, guild in enumerate(stale):
        try:
            bot.tree.clear_commands(guild=guild)
            bot.tree.copy_global_to(guild=guild)
            synced = await bot.tree.sync(guild=guild)
            log.info(f"Synced {len(synced)} commands to {guild.name} ({guild.id})")
        except discord.HTTPException as e:
            if is_cloudflare_ban(e):
                log.error(
                    "Cannot sync commands: this server's outbound IP is temporarily "
                    "banned by Discord/Cloudflare (error 1015). Stopping sync; it "
                    "will retry on the next start."
                )
                return
            if e.status == 429:
                wait = retry_after_seconds(e, 30)
                log.warning(f"Rate limited syncing {guild.name}; waiting {wait:.0f}s")
                await asyncio.sleep(wait + 2)
                continue
            log.warning(f"Could not sync commands for {guild.name}: {e}")
            continue
        except Exception as e:
            log.warning(f"Could not sync commands for {guild.name}: {e}")
            continue

        state[str(guild.id)] = fingerprint
        await db.set_config(0, "command_sync_state", json.dumps(state))

        if index < len(stale) - 1:
            await asyncio.sleep(5)  # 5s between guilds to stay well under rate limits


# ── Events & Error Handling ───────────────────────────────────────────

_synced = False

@bot.event
async def on_ready():
    global _synced
    log.info(f"Logged in as {bot.user} (ID: {bot.user.id})")

    if not _synced:
        # Mark before syncing so a reconnect during a long sync can't start a second one.
        _synced = True
        try:
            await sync_commands()
        except Exception:
            log.exception("Command sync failed")
    else:
        log.info("Reconnected — skipping command sync (already handled this session).")

    log.info(f"Ready! Serving {len(bot.guilds)} guild(s).")


@bot.event
async def on_guild_join(guild: discord.Guild):
    """Give a newly joined guild its commands without re-syncing every other guild."""
    log.info(f"Joined guild {guild.name} ({guild.id}) — syncing commands.")
    try:
        await sync_commands()
    except Exception:
        log.exception(f"Command sync failed for new guild {guild.id}")


@bot.tree.error
async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    command_name = interaction.command.name if interaction.command else "unknown"
    original = getattr(error, "original", error)

    if isinstance(original, discord.HTTPException) and original.status == 429:
        # Trying to send a reply here would just generate another rate-limited
        # request. Log it and let the interaction expire instead.
        log.warning(f"/{command_name} was rate limited (429); skipping error reply.")
        return

    log.error(f"App command error in /{command_name}: {error}", exc_info=error)
    try:
        if interaction.response.is_done():
            await interaction.followup.send(f"❌ An error occurred: `{error}`", ephemeral=True)
        else:
            await interaction.response.send_message(f"❌ An error occurred: `{error}`", ephemeral=True)
    except Exception:
        pass


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

    # Retry login if Discord rate-limits us.
    # A Cloudflare 1015 response is an IP-level ban, not a normal bot rate limit:
    # retrying quickly only extends it, so wait at least 30 minutes in that case.
    attempt = 0
    while True:
        try:
            await bot.start(TOKEN)
            return  # Clean shutdown
        except discord.LoginFailure:
            log.error("Discord rejected the bot token. Check the DISCORD_TOKEN variable.")
            raise
        except discord.HTTPException as e:
            if e.status != 429:
                raise

            attempt += 1
            if is_cloudflare_ban(e):
                wait = retry_after_seconds(e, 30 * 60)
                log.error(
                    "Discord/Cloudflare is blocking this server's outbound IP "
                    f"(429, error 1015). This is an IP-level ban, usually caused by "
                    "other services sharing the same hosting egress IP. Waiting "
                    f"{wait / 60:.0f} min before retrying (attempt {attempt}). "
                    "If this keeps happening, move the Render service to another "
                    "region or use a dedicated/proxied outbound IP."
                )
            else:
                wait = retry_after_seconds(e, 30)
                log.warning(
                    f"Discord login rate-limited (attempt {attempt}). "
                    f"Waiting {wait:.0f}s before retrying..."
                )
            await asyncio.sleep(wait)
        except KeyboardInterrupt:
            return


if __name__ == "__main__":
    asyncio.run(main())
