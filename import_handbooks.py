"""Trusted operator tool; article JSON stays outside the public repository."""

import argparse
import asyncio
import json
from pathlib import Path

import database as db
import handbook_store as store


async def run(guild, path, check):
    records = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(records, list):
        raise ValueError("Expected a JSON array of articles.")
    records = [store.validate(article) for article in records]
    if not records or len({r["slug"] for r in records}) != len(records):
        raise ValueError("Supply articles with unique slugs.")
    if not check:
        try:
            await db.init_db()
            await store.import_articles(guild, records)
        finally:
            if db._pool is not None:
                await db._pool.close()
    print(f"{'Validated' if check else 'Imported'} {len(records)} articles.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("path", type=Path, help="Private JSON file; do not commit article text")
    parser.add_argument("--guild", type=int, required=True, help="Target Discord server ID")
    parser.add_argument("--check", action="store_true", help="Validate without connecting to a database")
    args = parser.parse_args()
    asyncio.run(run(args.guild, args.path, args.check))
