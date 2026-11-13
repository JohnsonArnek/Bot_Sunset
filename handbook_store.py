"""Private, per-server handbook articles and live reader permissions."""

import json
import re
from datetime import datetime, timezone

import database as db


BOOKS = {
    "citizens": "Citizens",
    "settlements": "Settlement leadership",
    "tribunes": "Tribunes",
    "hands": "Hands",
    "civil": "Civil administration",
    "war": "War and logistics",
    "admin": "Bot administration",
}


def schema(postgres):
    table = "sunset_handbooks.articles" if postgres else "handbook_articles"
    sql = f"""
        CREATE TABLE IF NOT EXISTS {table} (
            guild_id BIGINT NOT NULL CHECK (guild_id > 0),
            slug TEXT NOT NULL, book TEXT NOT NULL
                CHECK (book IN ('citizens','settlements','tribunes','hands','civil','war','admin')),
            title TEXT NOT NULL, summary TEXT NOT NULL, pages TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            PRIMARY KEY (guild_id, slug)
        );
        CREATE INDEX IF NOT EXISTS handbook_book_idx ON {table} (guild_id, book, title);
    """
    if postgres:
        sql = "CREATE SCHEMA IF NOT EXISTS sunset_handbooks; REVOKE ALL ON SCHEMA sunset_handbooks FROM PUBLIC; " + sql
        sql += "ALTER TABLE sunset_handbooks.articles ENABLE ROW LEVEL SECURITY; REVOKE ALL ON sunset_handbooks.articles FROM PUBLIC;"
        # Supabase roles may have global default grants; also support plain Postgres.
        sql += """
            DO $$ BEGIN
                IF EXISTS (SELECT FROM pg_roles WHERE rolname='anon') THEN
                    REVOKE ALL ON SCHEMA sunset_handbooks FROM anon;
                    REVOKE ALL ON sunset_handbooks.articles FROM anon;
                END IF;
                IF EXISTS (SELECT FROM pg_roles WHERE rolname='authenticated') THEN
                    REVOKE ALL ON SCHEMA sunset_handbooks FROM authenticated;
                    REVOKE ALL ON sunset_handbooks.articles FROM authenticated;
                END IF;
            END $$;
        """
    return sql


def table():
    return "sunset_handbooks.articles" if db.IS_POSTGRES else "handbook_articles"


def units(text):
    return len(text.encode("utf-16-le")) // 2


def validate(article):
    """Bound authored text to Discord limits before saving it."""
    if (not isinstance(article, dict) or not isinstance(article.get("slug"), str)
            or not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", article["slug"])):
        raise ValueError("Use a lowercase, hyphenated article slug.")
    if len(article["slug"]) > 64 or article.get("book") not in BOOKS:
        raise ValueError("Invalid article slug or handbook.")
    for key, maximum in (("title", 80), ("summary", 90)):
        value = article.get(key)
        if not isinstance(value, str) or not value.strip() or units(value) > maximum or "\n" in value:
            raise ValueError(f"Article {key} must be one line, 1–{maximum} characters.")
    pages = article.get("pages")
    if not isinstance(pages, list) or not 1 <= len(pages) <= 20:
        raise ValueError("Supply 1–20 article pages.")
    if any(not isinstance(p, str) or not p.strip() or units(p) > 3500 for p in pages):
        raise ValueError("Each page must contain 1–3,500 characters (emoji may count twice).")
    return {key: article[key] for key in ("slug", "book", "title", "summary", "pages")}


async def import_articles(guild, articles):
    """Trusted operator import. All input validates before a transactional upsert."""
    if not isinstance(guild, int) or guild <= 0:
        raise ValueError("A Discord server ID is required.")
    records = [validate(article) for article in articles]
    if not records or len({r["slug"] for r in records}) != len(records):
        raise ValueError("Supply articles with unique slugs.")
    now = datetime.now(timezone.utc).isoformat()
    async with db.DBConnection() as conn, conn.transaction():
        for r in records:
            await conn.execute(
                f"INSERT INTO {table()} (guild_id,slug,book,title,summary,pages,updated_at) VALUES (?,?,?,?,?,?,?) "
                "ON CONFLICT (guild_id,slug) DO UPDATE SET book=excluded.book,title=excluded.title,"
                "summary=excluded.summary,pages=excluded.pages,updated_at=excluded.updated_at",
                (guild, r["slug"], r["book"], r["title"], r["summary"], json.dumps(r["pages"], ensure_ascii=False), now))


async def allowed_books(interaction):
    """No stored session grants: check the invoking member and current bot data."""
    if not interaction.guild or interaction.guild_id is None:
        return set()
    if interaction.permissions.administrator:
        return set(BOOKS)
    books = {"citizens"}
    guild, user = interaction.guild_id, interaction.user.id
    staff_role = await db.get_staff_role_id(guild)
    staff = (staff_role and any(r.id == staff_role for r in interaction.user.roles)) or (
        not staff_role and user == interaction.guild.owner_id)
    if staff:
        books.update(("civil", "settlements"))
    prefix = "sunset_acclimations." if db.IS_POSTGRES else "acclimation_"
    async with db.DBConnection() as conn:
        if not staff:
            leader = await conn.fetchone(
                "SELECT l.id FROM lands l WHERE l.guild_id=? AND (l.owner_id=? OR EXISTS "
                "(SELECT 1 FROM officers o WHERE o.land_id=l.id AND o.user_id=?)) LIMIT 1", (guild, user, user))
            if leader:
                books.add("settlements")
        official = await conn.fetchone(f"SELECT rank FROM {prefix}officials WHERE guild_id=? AND user_id=?", (guild, user))
        if official:
            books.add("hands" if official["rank"] == "hand" else "tribunes")
    return books


def _scope(books):
    allowed = sorted(set(books) & BOOKS.keys())
    return ",".join("?" for _ in allowed), allowed


async def catalog(guild, books):
    placeholders, allowed = _scope(books)
    if not allowed:
        return []
    async with db.DBConnection() as conn:
        return await conn.fetchall(
            f"SELECT slug,book,title,summary FROM {table()} WHERE guild_id=? AND book IN ({placeholders}) ORDER BY title,slug",
            (guild, *allowed))


async def article(guild, books, slug):
    placeholders, allowed = _scope(books)
    if not allowed:
        return None
    async with db.DBConnection() as conn:
        row = await conn.fetchone(
            f"SELECT * FROM {table()} WHERE guild_id=? AND book IN ({placeholders}) AND slug=?", (guild, *allowed, slug))
    if row:
        row["pages"] = json.loads(row["pages"])
        validate(row)
    return row
