"""Public settlement book catalogues, maintained by current land leadership."""

import unicodedata
from datetime import datetime, timezone

import database as db


PAGE_SIZE = 3
MAX_BOOKS = 500


def table(name):
    return ("sunset_libraries." if db.IS_POSTGRES else "library_") + name


def schema(postgres):
    p = "sunset_libraries." if postgres else "library_"
    identity = "BIGSERIAL PRIMARY KEY" if postgres else "INTEGER PRIMARY KEY AUTOINCREMENT"
    lands = "public.lands" if postgres else "lands"
    sql = f"""
        CREATE TABLE IF NOT EXISTS {p}libraries (
            land_id INTEGER PRIMARY KEY REFERENCES {lands}(id) ON DELETE CASCADE,
            guild_id BIGINT NOT NULL, name TEXT NOT NULL, location TEXT NOT NULL,
            description TEXT NOT NULL, updated_by BIGINT NOT NULL, updated_at TEXT NOT NULL,
            UNIQUE (guild_id,land_id)
        );
        CREATE TABLE IF NOT EXISTS {p}books (
            id {identity}, guild_id BIGINT NOT NULL, land_id INTEGER NOT NULL,
            title TEXT NOT NULL, author TEXT NOT NULL, title_key TEXT NOT NULL, author_key TEXT NOT NULL,
            copies INTEGER NOT NULL CHECK (copies BETWEEN 0 AND 1000000),
            description TEXT NOT NULL, updated_by BIGINT NOT NULL, updated_at TEXT NOT NULL,
            FOREIGN KEY (guild_id,land_id) REFERENCES {p}libraries(guild_id,land_id) ON DELETE CASCADE,
            UNIQUE (guild_id,land_id,title_key,author_key)
        );
        CREATE INDEX IF NOT EXISTS library_catalog_idx ON {p}books(guild_id,land_id,title_key,id);
    """
    if postgres:
        sql = "CREATE SCHEMA IF NOT EXISTS sunset_libraries; REVOKE ALL ON SCHEMA sunset_libraries FROM PUBLIC; " + sql
        for name in ("libraries", "books"):
            sql += f"ALTER TABLE {p}{name} ENABLE ROW LEVEL SECURITY; REVOKE ALL ON {p}{name} FROM PUBLIC; "
        for role in ("anon", "authenticated"):
            sql += f"""DO $$ BEGIN IF EXISTS (SELECT FROM pg_roles WHERE rolname='{role}') THEN
                REVOKE ALL ON SCHEMA sunset_libraries FROM {role};
                REVOKE ALL ON ALL TABLES IN SCHEMA sunset_libraries FROM {role};
            END IF; END $$;"""
    return sql


def clean(value, label, maximum, *, optional=False):
    if not isinstance(value,str):
        raise ValueError(f"Supply {label} as text.")
    value = " ".join(value.split())
    if (not value and not optional) or len(value.encode("utf-16-le"))//2 > maximum:
        raise ValueError(f"{label} must contain {'0' if optional else '1'}–{maximum} characters (emoji may count twice).")
    return value


def key(text):
    return unicodedata.normalize("NFKC",text).casefold()


def now():
    return datetime.now(timezone.utc).isoformat()


def offset(page):
    if type(page) is not int or not 1 <= page <= 1_000_000:
        raise ValueError("Choose a page between 1 and 1,000,000.")
    return (page-1)*PAGE_SIZE


async def managed_land(conn, guild, actor, admin, *, land_name=None, land_id=None):
    lock = " FOR UPDATE" if db.IS_POSTGRES else ""
    if land_id is not None:
        rows = await conn.fetchall("SELECT * FROM lands WHERE guild_id=? AND id=?"+lock,(guild,land_id))
    elif land_name is not None:
        rows = await conn.fetchall("SELECT * FROM lands WHERE guild_id=? AND LOWER(name)=LOWER(?)"+lock,(guild,land_name.strip()))
    else:
        rows = await conn.fetchall("SELECT * FROM lands WHERE guild_id=? AND (owner_id=? OR EXISTS "
            "(SELECT 1 FROM officers WHERE land_id=lands.id AND user_id=?)) ORDER BY id LIMIT 2"+lock,(guild,actor,actor))
    if not rows:
        raise ValueError("Land not found. Choose land_name for a registered settlement you manage.")
    if len(rows)>1:
        raise ValueError("You manage multiple settlements; choose land_name.")
    land = rows[0]
    if not admin and land["owner_id"] != actor:
        officer = await conn.fetchone("SELECT 1 FROM officers WHERE land_id=? AND user_id=?",(land["id"],actor))
        if not officer:
            raise ValueError("Only this land's owner, its officers or Discord administrators can manage its public library.")
    return land


async def setup(guild, actor, name, location, *, admin, land_name=None, description=None):
    name = clean(name,"Library name",80)
    location = clean(location,"Location",200)
    if description is not None:
        description = clean(description,"Description",300,optional=True)
    async with db.DBConnection() as conn, conn.transaction():
        land = await managed_land(conn,guild,actor,admin,land_name=land_name)
        existing = await conn.fetchone(f"SELECT description FROM {table('libraries')} WHERE guild_id=? AND land_id=?",(guild,land["id"]))
        text = description if description is not None else existing["description"] if existing else ""
        await conn.execute(f"INSERT INTO {table('libraries')} (guild_id,land_id,name,location,description,updated_by,updated_at) "
                           "VALUES (?,?,?,?,?,?,?) ON CONFLICT (land_id) DO UPDATE SET name=excluded.name,location=excluded.location,"
                           "description=excluded.description,updated_by=excluded.updated_by,updated_at=excluded.updated_at",
                           (guild,land["id"],name,location,text,actor,now()))
        return land["name"]


async def save_book(guild, actor, *, admin, book_id=None, land_name=None, title=None, author=None, copies=None, description=None):
    if book_id is not None and all(v is None for v in (title,author,copies,description)):
        raise ValueError("Choose at least one book field to update.")
    async with db.DBConnection() as conn, conn.transaction():
        existing = None
        if book_id is not None:
            existing = await conn.fetchone(f"SELECT * FROM {table('books')} WHERE guild_id=? AND id=?",(guild,book_id))
            if not existing:
                raise ValueError("Book not found on this server.")
        land = await managed_land(conn,guild,actor,admin,land_name=land_name,land_id=existing["land_id"] if existing else None)
        if existing:
            # Refresh after obtaining the land lock; another editor may have saved first.
            existing = await conn.fetchone(f"SELECT * FROM {table('books')} WHERE guild_id=? AND id=?",(guild,book_id))
            if not existing:
                raise ValueError("This book was removed before your edit. Refresh the catalogue.")
        library = await conn.fetchone(f"SELECT land_id FROM {table('libraries')} WHERE guild_id=? AND land_id=?",(guild,land["id"]))
        if not library:
            raise ValueError("Set up this settlement's library with /library set first.")
        defaults = existing or dict(title="",author="Unknown",copies=1,description="")
        title = clean(defaults["title"] if title is None else title,"Book title",80)
        author = clean(defaults["author"] if author is None else author,"Author",80)
        description = clean(defaults["description"] if description is None else description,"Book description",300,optional=True)
        copies = defaults["copies"] if copies is None else copies
        if type(copies) is not int or not 0 <= copies <= 1_000_000:
            raise ValueError("Copies must be a whole number between 0 and 1,000,000.")
        duplicate = await conn.fetchone(f"SELECT id FROM {table('books')} WHERE guild_id=? AND land_id=? AND title_key=? AND author_key=?",
                                         (guild,land["id"],key(title),key(author)))
        if duplicate and duplicate["id"] != book_id:
            raise ValueError(f"That title and author are already listed as book #{duplicate['id']}. Use /library edit to update its copies.")
        values = (title,author,key(title),key(author),copies,description,actor,now())
        if existing:
            await conn.execute(f"UPDATE {table('books')} SET title=?,author=?,title_key=?,author_key=?,copies=?,description=?,updated_by=?,updated_at=? "
                               "WHERE guild_id=? AND id=?",values+(guild,book_id))
            return book_id
        count = await conn.fetchone(f"SELECT COUNT(*) AS total FROM {table('books')} WHERE guild_id=? AND land_id=?",(guild,land["id"]))
        if count["total"] >= MAX_BOOKS:
            raise ValueError(f"A library may list up to {MAX_BOOKS} titles. Edit or remove an existing entry first.")
        row = await conn.execute_returning_one(f"INSERT INTO {table('books')} "
            "(title,author,title_key,author_key,copies,description,updated_by,updated_at,guild_id,land_id) VALUES (?,?,?,?,?,?,?,?,?,?) RETURNING id",
            values+(guild,land["id"]))
        return row["id"]


async def remove(guild, actor, book_id, *, admin):
    async with db.DBConnection() as conn, conn.transaction():
        book = await conn.fetchone(f"SELECT * FROM {table('books')} WHERE guild_id=? AND id=?",(guild,book_id))
        if not book:
            raise ValueError("Book not found on this server.")
        await managed_land(conn,guild,actor,admin,land_id=book["land_id"])
        changed = await conn.execute_returning_one(f"DELETE FROM {table('books')} WHERE guild_id=? AND id=? RETURNING id",(guild,book_id))
        if not changed:
            raise ValueError("This book was already removed.")
        return book["title"]


def library_query():
    return (f"SELECT c.*,l.name AS land_name,(SELECT COUNT(*) FROM {table('books')} b "
            "WHERE b.guild_id=c.guild_id AND b.land_id=c.land_id) AS titles "
            f"FROM {table('libraries')} c JOIN lands l ON l.id=c.land_id AND l.guild_id=c.guild_id ")


async def catalog(guild, *, page=1):
    skip = offset(page)
    async with db.DBConnection() as conn:
        count = await conn.fetchone(f"SELECT COUNT(*) AS total FROM {table('libraries')} c JOIN lands l "
                                   "ON l.id=c.land_id AND l.guild_id=c.guild_id WHERE c.guild_id=?",(guild,))
        rows = await conn.fetchall(library_query()+"WHERE c.guild_id=? ORDER BY LOWER(l.name),l.id LIMIT ? OFFSET ?",(guild,PAGE_SIZE,skip))
        return dict(total=count["total"],libraries=rows)


async def view(guild, land_name, *, page=1):
    skip = offset(page)
    async with db.DBConnection() as conn:
        library = await conn.fetchone(library_query()+"WHERE c.guild_id=? AND LOWER(l.name)=LOWER(?)",(guild,land_name.strip()))
        if not library:
            raise ValueError("This settlement has not listed a public library on this server.")
        books = await conn.fetchall(f"SELECT * FROM {table('books')} WHERE guild_id=? AND land_id=? ORDER BY title_key,author_key,id LIMIT ? OFFSET ?",
                                    (guild,library["land_id"],PAGE_SIZE,skip))
        return library | dict(books=books)


async def search(guild, query, *, page=1):
    skip = offset(page)
    query = clean(query,"Search",80)
    pattern = "%"+key(query).replace("!","!!").replace("%","!%").replace("_","!_")+"%"
    base = (f"FROM {table('books')} b JOIN {table('libraries')} c ON c.guild_id=b.guild_id AND c.land_id=b.land_id "
            "JOIN lands l ON l.id=c.land_id AND l.guild_id=c.guild_id "
            "WHERE b.guild_id=? AND (b.title_key LIKE ? ESCAPE '!' OR b.author_key LIKE ? ESCAPE '!')")
    params = (guild,pattern,pattern)
    async with db.DBConnection() as conn:
        count = await conn.fetchone("SELECT COUNT(*) AS total "+base,params)
        rows = await conn.fetchall("SELECT b.*,c.name AS library_name,c.location,l.name AS land_name "+base+
                                  " ORDER BY b.title_key,b.author_key,b.id LIMIT ? OFFSET ?",params+(PAGE_SIZE,skip))
        return dict(total=count["total"],books=rows)
