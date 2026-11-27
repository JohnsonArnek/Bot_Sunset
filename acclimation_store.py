"""Guild-scoped appointments, deeds and quest rewards, committed atomically."""

from contextlib import asynccontextmanager
from datetime import datetime, timezone

import database as db


MEDALS = ((10, "Sunrise Copper Medal — Third Class"),
          (30, "Sunrise Copper Medal — Second Class"),
          (50, "Sunrise Copper Medal — First Class"))
TABLES = ("guilds", "officials", "appointments", "quests", "awards", "tribune_days", "submissions")
QUEST_TYPES = {
    "general": "Once per member",
    "unique": "One completion across the server",
    "repeatable": "Every approved completion earns the reward",
}
QUEST_TYPE_COLUMN = "TEXT NOT NULL DEFAULT 'general' CHECK (quest_type IN ('general','unique','repeatable'))"
COMPLETION_COLUMN = "INTEGER NOT NULL DEFAULT 1 CHECK (completion_no >= 1)"


def _awards_table(name, quests, identity):
    return f"""CREATE TABLE IF NOT EXISTS {name} (
        id {identity}, guild_id BIGINT NOT NULL, request_id BIGINT NOT NULL,
        recipient_id BIGINT NOT NULL, issuer_id BIGINT NOT NULL,
        amount INTEGER NOT NULL CHECK (amount BETWEEN 1 AND 1000000),
        deed TEXT NOT NULL, created_at TEXT NOT NULL,
        source TEXT NOT NULL CHECK (source IN ('hand', 'tribune', 'quest')),
        quest_id BIGINT, voided_by BIGINT, voided_at TEXT, void_reason TEXT,
        completion_no {COMPLETION_COLUMN},
        UNIQUE (guild_id, id), UNIQUE (guild_id, request_id),
        FOREIGN KEY (guild_id, quest_id) REFERENCES {quests} (guild_id, id),
        CHECK ((source = 'quest' AND quest_id IS NOT NULL) OR (source != 'quest' AND quest_id IS NULL))
    )"""


def schema(postgres):
    prefix = "sunset_acclimations." if postgres else "acclimation_"
    identity = "BIGSERIAL PRIMARY KEY" if postgres else "INTEGER PRIMARY KEY AUTOINCREMENT"
    sql = f"""
        CREATE TABLE IF NOT EXISTS {prefix}guilds (guild_id BIGINT PRIMARY KEY);
        CREATE TABLE IF NOT EXISTS {prefix}officials (
            guild_id BIGINT NOT NULL, user_id BIGINT NOT NULL,
            rank TEXT NOT NULL CHECK (rank IN ('hand', 'tribune')),
            appointed_by BIGINT NOT NULL, created_at TEXT NOT NULL,
            PRIMARY KEY (guild_id, user_id)
        );
        CREATE TABLE IF NOT EXISTS {prefix}appointments (
            id {identity}, guild_id BIGINT NOT NULL, user_id BIGINT NOT NULL,
            rank TEXT NOT NULL CHECK (rank IN ('hand', 'tribune', 'dismissed')),
            actor_id BIGINT NOT NULL, created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS {prefix}quests (
            id {identity}, guild_id BIGINT NOT NULL,
            request_id BIGINT NOT NULL, title TEXT NOT NULL, details TEXT NOT NULL,
            reward INTEGER NOT NULL CHECK (reward BETWEEN 1 AND 1000000),
            quest_type {QUEST_TYPE_COLUMN},
            status TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'closed')),
            created_by BIGINT NOT NULL, created_at TEXT NOT NULL,
            closed_by BIGINT, closed_at TEXT,
            UNIQUE (guild_id, id), UNIQUE (guild_id, request_id)
        );
        {_awards_table(prefix + 'awards', prefix + 'quests', identity)};
        CREATE TABLE IF NOT EXISTS {prefix}submissions (
            id {identity}, guild_id BIGINT NOT NULL, quest_id BIGINT NOT NULL, member_id BIGINT NOT NULL,
            request_id BIGINT NOT NULL, deed TEXT NOT NULL, evidence TEXT, submitted_at TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','approved','returned','revised','closed')),
            reviewer_id BIGINT, reviewed_at TEXT, note TEXT, award_id BIGINT,
            revision_of BIGINT, UNIQUE(guild_id,id), UNIQUE(guild_id,request_id), UNIQUE(guild_id,revision_of),
            UNIQUE(guild_id,award_id), CHECK ((status='approved') = (award_id IS NOT NULL)),
            FOREIGN KEY(guild_id,quest_id) REFERENCES {prefix}quests(guild_id,id),
            FOREIGN KEY(guild_id,award_id) REFERENCES {prefix}awards(guild_id,id),
            FOREIGN KEY(guild_id,revision_of) REFERENCES {prefix}submissions(guild_id,id)
        );
        CREATE INDEX IF NOT EXISTS acclimation_review_idx ON {prefix}submissions(guild_id,status,id);
        CREATE INDEX IF NOT EXISTS acclimation_member_deeds_idx ON {prefix}submissions(guild_id,member_id,quest_id,id);
        CREATE TABLE IF NOT EXISTS {prefix}tribune_days (
            guild_id BIGINT NOT NULL, issuer_id BIGINT NOT NULL, day TEXT NOT NULL,
            award_id BIGINT NOT NULL,
            PRIMARY KEY (guild_id, issuer_id, day),
            FOREIGN KEY (guild_id, award_id) REFERENCES {prefix}awards (guild_id, id)
        );
        CREATE INDEX IF NOT EXISTS acclimation_history_idx ON {prefix}awards (guild_id, recipient_id, id);
        CREATE INDEX IF NOT EXISTS acclimation_quests_idx ON {prefix}quests (guild_id, status, id);
        CREATE INDEX IF NOT EXISTS acclimation_appointments_idx ON {prefix}appointments (guild_id, id);
    """
    if postgres:
        sql = "CREATE SCHEMA IF NOT EXISTS sunset_acclimations; REVOKE ALL ON SCHEMA sunset_acclimations FROM PUBLIC; " + sql
        sql += f"""
            ALTER TABLE {prefix}quests ADD COLUMN IF NOT EXISTS quest_type {QUEST_TYPE_COLUMN};
            ALTER TABLE {prefix}awards ADD COLUMN IF NOT EXISTS completion_no {COMPLETION_COLUMN};
            ALTER TABLE {prefix}awards DROP CONSTRAINT IF EXISTS awards_guild_id_quest_id_recipient_id_key;
        """
        sql += " ".join(f"ALTER TABLE {prefix}{table} ENABLE ROW LEVEL SECURITY;" for table in TABLES)
    return sql + f"""CREATE UNIQUE INDEX IF NOT EXISTS acclimation_completion_idx
        ON {prefix}awards (guild_id,quest_id,recipient_id,completion_no) WHERE quest_id IS NOT NULL;"""


async def migrate_sqlite_quest_types(conn):
    # The startup connection has foreign keys off. Keep the referenced table's
    # final name and IDs stable when replacing SQLite's old inline UNIQUE rule.
    await conn.execute("BEGIN IMMEDIATE")
    try:
        async with conn.execute("PRAGMA table_info(acclimation_quests)") as cursor:
            columns = {row[1] for row in await cursor.fetchall()}
        if columns and "quest_type" not in columns:
            await conn.execute(f"ALTER TABLE acclimation_quests ADD COLUMN quest_type {QUEST_TYPE_COLUMN}")
            await conn.execute(_awards_table("acclimation_awards_new", "acclimation_quests", "INTEGER PRIMARY KEY AUTOINCREMENT"))
            fields = "id,guild_id,request_id,recipient_id,issuer_id,amount,deed,created_at,source,quest_id,voided_by,voided_at,void_reason"
            await conn.execute(f"INSERT INTO acclimation_awards_new ({fields}) SELECT {fields} FROM acclimation_awards")
            await conn.execute("UPDATE sqlite_sequence SET seq=MAX(seq,(SELECT seq FROM sqlite_sequence WHERE name='acclimation_awards')) "
                               "WHERE name='acclimation_awards_new' AND EXISTS (SELECT 1 FROM sqlite_sequence WHERE name='acclimation_awards')")
            await conn.execute("DROP TABLE acclimation_awards")
            await conn.execute("ALTER TABLE acclimation_awards_new RENAME TO acclimation_awards")
        await conn.commit()
    except BaseException:
        await conn.rollback()
        raise


def _table(name):
    return ("sunset_acclimations." if db.IS_POSTGRES else "acclimation_") + name


def utc_now():
    return datetime.now(timezone.utc)


def clean(text, maximum):
    text = text.strip()
    if not text or len(text) > maximum or any(ord(c) < 32 and c not in "\n\t" for c in text):
        raise ValueError(f"Enter meaningful text of 1–{maximum} characters.")
    return text


def positive(amount):
    if not isinstance(amount, int) or not 1 <= amount <= 1_000_000:
        raise ValueError("Use a whole amount between 1 and 1,000,000.")


@asynccontextmanager
async def _write(guild):
    async with db.DBConnection() as conn, conn.transaction():
        # Serialize mutations within a guild, including authority changes. Keep
        # this lock inside the leader fence and never call Discord while holding it.
        await conn.execute(f"INSERT INTO {_table('guilds')} (guild_id) VALUES (?) ON CONFLICT DO NOTHING", (guild,))
        if db.IS_POSTGRES:
            await conn.fetchone(f"SELECT guild_id FROM {_table('guilds')} WHERE guild_id=? FOR UPDATE", (guild,))
        yield conn


async def _rank(conn, guild, user):
    row = await conn.fetchone(f"SELECT rank FROM {_table('officials')} WHERE guild_id=? AND user_id=?", (guild, user))
    return row["rank"] if row else None


async def _quest_authority(conn, guild, actor, admin):
    if not admin and await _rank(conn, guild, actor) != "hand":
        raise ValueError("Only appointed Hands or Discord administrators can manage quests and approve rewards.")


async def appoint(guild, actor, member, rank, *, admin):
    if not admin:
        raise ValueError("Only Discord administrators can deputize or dismiss Hands and Tribunes.")
    if rank not in ("hand", "tribune", "dismissed"):
        raise ValueError("Choose Hand or Tribune.")
    async with _write(guild) as conn:
        now = utc_now().isoformat()
        old = await _rank(conn, guild, member)
        if old == rank or (old is None and rank == "dismissed"):
            raise ValueError("That member already has this appointment status.")
        if rank == "dismissed":
            await conn.execute(f"DELETE FROM {_table('officials')} WHERE guild_id=? AND user_id=?", (guild, member))
        else:
            await conn.execute(f"INSERT INTO {_table('officials')} (guild_id,user_id,rank,appointed_by,created_at) "
                               "VALUES (?,?,?,?,?) ON CONFLICT (guild_id,user_id) DO UPDATE SET "
                               "rank=excluded.rank,appointed_by=excluded.appointed_by,created_at=excluded.created_at",
                               (guild, member, rank, actor, now))
        await conn.execute(f"INSERT INTO {_table('appointments')} (guild_id,user_id,rank,actor_id,created_at) "
                           "VALUES (?,?,?,?,?)", (guild, member, rank, actor, now))


async def officials(guild, page=1):
    async with db.DBConnection() as conn:
        return await conn.fetchall(f"SELECT * FROM {_table('officials')} WHERE guild_id=? ORDER BY rank,user_id LIMIT 10 OFFSET ?",
                                   (guild, (page - 1) * 10))


async def appointments(guild, page=1):
    async with db.DBConnection() as conn:
        return await conn.fetchall(f"SELECT * FROM {_table('appointments')} WHERE guild_id=? ORDER BY id DESC LIMIT 10 OFFSET ?",
                                   (guild, (page - 1) * 10))


async def _total(conn, guild, member):
    row = await conn.fetchone(f"SELECT COALESCE(SUM(amount),0) AS total FROM {_table('awards')} "
                             "WHERE guild_id=? AND recipient_id=? AND voided_at IS NULL", (guild, member))
    return row["total"]


async def profile(guild, member):
    async with db.DBConnection() as conn:
        total = await _total(conn, guild, member)
        rank = await _rank(conn, guild, member)
        used = await conn.fetchone(f"SELECT award_id FROM {_table('tribune_days')} WHERE guild_id=? AND issuer_id=? AND day=?",
                                  (guild, member, utc_now().date().isoformat()))
    return dict(total=total, rank=rank, remaining=0 if used else 1)


async def _record(conn, guild, actor, member, amount, deed, request_id, source, quest_id, now, completion_no=1):
    previous = await _total(conn, guild, member)
    row = await conn.execute_returning_one(
        f"INSERT INTO {_table('awards')} (guild_id,request_id,recipient_id,issuer_id,amount,deed,created_at,source,quest_id,completion_no) "
        "VALUES (?,?,?,?,?,?,?,?,?,?) RETURNING *", (guild, request_id, member, actor, amount, deed, now, source, quest_id, completion_no))
    return row | dict(previous=previous, total=previous + amount, duplicate=False)


async def _replay(conn, guild, request_id, actor, member, quest_id=None):
    row = await conn.fetchone(f"SELECT * FROM {_table('awards')} WHERE guild_id=? AND request_id=?", (guild, request_id))
    if row:
        if row["issuer_id"] != actor or row["recipient_id"] != member or row["quest_id"] != quest_id:
            raise ValueError("This request was already used for another award.")
        return row | dict(total=await _total(conn, guild, member), duplicate=True)


async def award(guild, actor, member, amount, deed, *, request_id):
    positive(amount)
    deed = clean(deed, 500)
    if actor == member:
        raise ValueError("You cannot award yourself Acclimations; another appointed official must recognise the deed.")
    async with _write(guild) as conn:
        rank = await _rank(conn, guild, actor)
        if rank is None:
            raise ValueError("You must be deputized as a Hand or Tribune before awarding Acclimations.")
        existing = await _replay(conn, guild, request_id, actor, member)
        if existing:
            return existing
        now = utc_now()
        if rank == "tribune":
            if amount != 1:
                raise ValueError("A Tribune can award exactly one Acclimation per day.")
            used = await conn.fetchone(f"SELECT award_id FROM {_table('tribune_days')} WHERE guild_id=? AND issuer_id=? AND day=?",
                                      (guild, actor, now.date().isoformat()))
            if used:
                raise ValueError("Your Tribune award for today is already used. It resets at 00:00 UTC; unused days do not accumulate.")
        row = await _record(conn, guild, actor, member, amount, deed, request_id, rank, None, now.isoformat())
        if rank == "tribune":
            await conn.execute(f"INSERT INTO {_table('tribune_days')} (guild_id,issuer_id,day,award_id) VALUES (?,?,?,?)",
                               (guild, actor, now.date().isoformat(), row["id"]))
        return row


async def void_award(guild, actor, award_id, reason, *, admin):
    if not admin:
        raise ValueError("Only Discord administrators can void an award.")
    reason = clean(reason, 250)
    async with _write(guild) as conn:
        row = await conn.execute_returning_one(f"UPDATE {_table('awards')} SET voided_by=?,voided_at=?,void_reason=? "
                                              "WHERE guild_id=? AND id=? AND voided_at IS NULL RETURNING *",
                                              (actor, utc_now().isoformat(), reason, guild, award_id))
        if row is None:
            raise ValueError("No active award with that ID exists on this server.")
        # Retain daily quotas and quest completion uniqueness after a correction.
        return row | dict(total=await _total(conn, guild, row["recipient_id"]))


async def history(guild, member, page=1, *, include_next=False):
    async with db.DBConnection() as conn:
        return await conn.fetchall(f"SELECT * FROM {_table('awards')} WHERE guild_id=? AND recipient_id=? "
                                   "ORDER BY id DESC LIMIT ? OFFSET ?", (guild, member, 3 if include_next else 2, (page - 1) * 2))


async def honours(guild, page=1):
    async with db.DBConnection() as conn:
        return await conn.fetchall(f"SELECT recipient_id,SUM(amount) AS total FROM {_table('awards')} "
                                   "WHERE guild_id=? AND voided_at IS NULL GROUP BY recipient_id HAVING SUM(amount)>=10 "
                                   "ORDER BY total DESC,recipient_id LIMIT 10 OFFSET ?", (guild, (page - 1) * 10))


async def create_quest(guild, actor, title, details, reward, *, admin, request_id, quest_type="general"):
    title, details = clean(title, 80), clean(details, 700)
    positive(reward)
    if quest_type not in QUEST_TYPES:
        raise ValueError("Choose a general, unique or repeatable quest.")
    async with _write(guild) as conn:
        await _quest_authority(conn, guild, actor, admin)
        existing = await conn.fetchone(f"SELECT * FROM {_table('quests')} WHERE guild_id=? AND request_id=?", (guild, request_id))
        if existing:
            return existing
        return await conn.execute_returning_one(f"INSERT INTO {_table('quests')} "
                                               "(guild_id,request_id,title,details,reward,created_by,created_at,quest_type) "
                                               "VALUES (?,?,?,?,?,?,?,?) RETURNING *",
                                               (guild, request_id, title, details, reward, actor, utc_now().isoformat(), quest_type))


async def get_quest(guild, quest_id):
    async with db.DBConnection() as conn:
        return await conn.fetchone(f"SELECT * FROM {_table('quests')} WHERE guild_id=? AND id=?", (guild, quest_id))


async def quests(guild, page=1):
    async with db.DBConnection() as conn:
        return await conn.fetchall(f"SELECT * FROM {_table('quests')} WHERE guild_id=? AND status='open' "
                                   "ORDER BY id DESC LIMIT 5 OFFSET ?", (guild, (page - 1) * 5))


QUEST_PAGE_SIZE = 10


def _latest_award_join():
    awards = _table("awards")
    return (f" LEFT JOIN {awards} a ON a.id=(SELECT h.id FROM {awards} h WHERE h.guild_id=q.guild_id "
            "AND h.quest_id=q.id AND h.recipient_id=? ORDER BY h.completion_no DESC LIMIT 1) ")


def _quest_search(query):
    query = " ".join((query or "").split())
    if len(query) > 80:
        raise ValueError("Keep the quest search to 80 characters or fewer.")
    if not query:
        return query, "", ()
    pattern = "%" + query.lower().replace("!", "!!").replace("%", "!%").replace("_", "!_") + "%"
    return query, " AND (LOWER(q.title) LIKE ? ESCAPE '!' OR LOWER(q.details) LIKE ? ESCAPE '!' OR CAST(q.id AS TEXT)=?)", (pattern,pattern,query.lstrip("#"))


async def quest_board(guild, member, *, page=1, query="", unrewarded=False, quest_type=None):
    """Page open quests with this reader's eligibility and latest completion."""
    if type(page) is not int or page < 1:
        page = 1
    query, search, params = _quest_search(query)
    base = (f"FROM {_table('quests')} q" + _latest_award_join() + "WHERE q.guild_id=? AND q.status='open'" + search
            + (" AND (a.id IS NULL OR q.quest_type='repeatable')" if unrewarded else ""))
    params = (member,guild) + params
    if quest_type is not None:
        base += " AND q.quest_type=?"
        params += (quest_type,)
    if unrewarded:
        base += (f" AND (q.quest_type='repeatable' OR NOT EXISTS (SELECT 1 FROM {_table('submissions')} s "
                 "WHERE s.guild_id=q.guild_id AND s.quest_id=q.id AND s.member_id=? AND s.status='pending'))")
        params += (member,)
    async with db.DBConnection() as conn:
        count = await conn.fetchone("SELECT COUNT(*) AS total " + base,params)
        pages = max(1,(count["total"]+QUEST_PAGE_SIZE-1)//QUEST_PAGE_SIZE)
        page = min(page,pages)
        rows = await conn.fetchall("SELECT q.*,a.id AS award_id,a.voided_at AS award_voided_at,a.completion_no " + base +
                                  " ORDER BY q.id DESC LIMIT ? OFFSET ?",params+(QUEST_PAGE_SIZE,(page-1)*QUEST_PAGE_SIZE))
        return dict(rows=rows,total=count["total"],page=page,pages=pages,query=query)


async def quest_for_reader(guild, member, quest_id):
    async with db.DBConnection() as conn:
        return await conn.fetchone(f"SELECT q.*,o.rank AS reader_rank,a.id AS award_id,a.voided_at AS award_voided_at,a.completion_no,"
                                   "s.id AS submission_id,s.status AS submission_status "
                                   f"FROM {_table('quests')} q" + _latest_award_join() +
                                   f"LEFT JOIN {_table('officials')} o ON o.guild_id=q.guild_id AND o.user_id=? "
                                   f"LEFT JOIN {_table('submissions')} s ON s.id=(SELECT MAX(h.id) FROM {_table('submissions')} h "
                                   "WHERE h.guild_id=q.guild_id AND h.quest_id=q.id AND h.member_id=?) "
                                   "WHERE q.guild_id=? AND q.id=?",(member,member,member,guild,quest_id))


async def quest_deed(guild, member, quest_id, completion_no=None):
    awards = _table("awards")
    condition = ""
    params = (guild, member, quest_id)
    if completion_no is not None:
        condition = " AND a.completion_no=?"
        params += (completion_no,)
    async with db.DBConnection() as conn:
        return await conn.fetchone(
            f"SELECT a.id,a.deed,a.amount,a.issuer_id,a.created_at,a.voided_at,a.void_reason,a.completion_no,q.title,"
            f"(SELECT MAX(h.completion_no) FROM {awards} h WHERE h.guild_id=a.guild_id "
            f"AND h.quest_id=a.quest_id AND h.recipient_id=a.recipient_id) AS latest,"
            f"(SELECT COALESCE(SUM(h.amount),0) FROM {awards} h WHERE h.guild_id=a.guild_id "
            f"AND h.recipient_id=a.recipient_id AND h.voided_at IS NULL) AS total FROM {awards} a "
            f"JOIN {_table('quests')} q ON q.guild_id=a.guild_id AND q.id=a.quest_id "
            f"WHERE a.guild_id=? AND a.recipient_id=? AND a.quest_id=?{condition} "
            "ORDER BY a.completion_no DESC LIMIT 1", params)


async def quest_suggestions(guild, query=""):
    _, search, params = _quest_search(query)
    async with db.DBConnection() as conn:
        return await conn.fetchall(f"SELECT q.id,q.title,q.status FROM {_table('quests')} q WHERE q.guild_id=?" + search +
                                   " ORDER BY CASE WHEN q.status='open' THEN 0 ELSE 1 END,q.id DESC LIMIT 25",(guild,)+params)


async def close_quest(guild, actor, quest_id, *, admin):
    async with _write(guild) as conn:
        await _quest_authority(conn, guild, actor, admin)
        row = await conn.execute_returning_one(f"UPDATE {_table('quests')} SET status='closed',closed_by=?,closed_at=? "
                                              "WHERE guild_id=? AND id=? AND status='open' RETURNING *",
                                              (actor, utc_now().isoformat(), guild, quest_id))
        if row is None:
            raise ValueError("No open quest with that ID exists on this server.")
        await _end_submissions(conn, guild, quest_id, "The quest was closed by staff.")
        return row


async def _end_submissions(conn, guild, quest_id, reason, member=None):
    await conn.execute(f"UPDATE {_table('submissions')} SET status='closed', note=COALESCE(note,?) "
                       "WHERE guild_id=? AND quest_id=? AND status IN ('pending','returned')" +
                       (" AND member_id=?" if member is not None else ""),
                       (reason, guild, quest_id) + ((member,) if member is not None else ()))


async def complete_quest(guild, actor, quest_id, member, deed, *, admin, request_id, expected_reward=None, expected_completion=None,
                         submission_id=None):
    deed = clean(deed, 500)
    if actor == member:
        raise ValueError("Another Hand or administrator must approve your own quest completion.")
    async with _write(guild) as conn:
        await _quest_authority(conn, guild, actor, admin)
        existing = await _replay(conn, guild, request_id, actor, member, quest_id)
        submissions = _table("submissions")
        if existing:
            if submission_id is not None and not await conn.fetchone(
                    f"SELECT id FROM {submissions} WHERE guild_id=? AND id=? AND award_id=?", (guild, submission_id, existing["id"])):
                raise ValueError("This approval request already belongs to another deed.")
            return existing
        if submission_id is not None:
            pending = await conn.fetchone(f"SELECT id FROM {submissions} WHERE guild_id=? AND id=? AND quest_id=? "
                                          "AND member_id=? AND deed=? AND status='pending'", (guild, submission_id, quest_id, member, deed))
            if not pending:
                raise ValueError("This submission is no longer awaiting review.")
        else:
            # A witnessed completion can also settle the matching submitted deed.
            matches = await conn.fetchall(f"SELECT id FROM {submissions} WHERE guild_id=? AND quest_id=? AND member_id=? "
                                          "AND deed=? AND status='pending' ORDER BY id LIMIT 2", (guild, quest_id, member, deed))
            if len(matches) > 1:
                raise ValueError("Several submissions have this wording. Open Needs review to approve the intended deed.")
            pending = matches[0] if matches else None
        quest = await conn.fetchone(f"SELECT * FROM {_table('quests')} WHERE guild_id=? AND id=? AND status='open'", (guild, quest_id))
        if not quest:
            raise ValueError("No open quest with that ID exists on this server.")
        if expected_reward is not None and quest["reward"] != expected_reward:
            raise ValueError("The quest reward changed. Review the new amount before recording the completion.")
        completed = await conn.fetchone(f"SELECT MAX(completion_no) AS latest FROM {_table('awards')} "
                                        "WHERE guild_id=? AND quest_id=? AND recipient_id=?", (guild, quest_id, member))
        number = (completed["latest"] or 0) + 1
        if number > 1 and quest["quest_type"] != "repeatable":
            raise ValueError("This member already received this quest's reward (including any later void).")
        if expected_completion is not None and expected_completion != number:
            raise ValueError("Another completion was recorded for this player. Review their latest deed before awarding again.")
        now = utc_now().isoformat()
        award = await _record(conn, guild, actor, member, quest["reward"], deed, request_id, "quest", quest_id, now, number)
        if pending:
            await conn.execute(f"UPDATE {submissions} SET status='approved',reviewer_id=?,reviewed_at=?,award_id=? WHERE guild_id=? AND id=?",
                               (actor, now, award["id"], guild, pending["id"]))
        if quest["quest_type"] == "unique":
            await conn.execute(f"UPDATE {_table('quests')} SET status='closed',closed_by=?,closed_at=? WHERE guild_id=? AND id=?",
                               (actor, now, guild, quest_id))
            award["closed_unique_quest"] = True
            await _end_submissions(conn, guild, quest_id, "The unique quest was completed.")
        elif quest["quest_type"] == "general":
            await _end_submissions(conn, guild, quest_id, "Your completion was recorded by staff.", member)
        return award
