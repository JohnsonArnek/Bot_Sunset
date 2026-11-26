"""Reader-scoped quest submissions. Rewards use the existing guild transaction."""

import re

import acclimation_store as store
import database as db


def evidence_link(guild, text):
    if not text or not text.strip():
        return None
    match = re.fullmatch(r"https://(?:(?:canary|ptb)\.)?discord\.com/channels/([0-9]+)/([0-9]+)/([0-9]+)/?", text.strip())
    if not match or match[1] != str(guild):
        raise ValueError("Paste a Discord message link from this server, or leave the link blank.")
    return "https://discord.com/channels/" + "/".join(match.groups())


def _joined():
    return (f" FROM {store._table('submissions')} s JOIN {store._table('quests')} q "
            "ON q.guild_id=s.guild_id AND q.id=s.quest_id ")


async def submit(guild, member, quest_id, deed, evidence, *, request_id, revision_of=None):
    deed, evidence = store.clean(deed, 500), evidence_link(guild, evidence)
    table = store._table("submissions")
    async with store._write(guild) as conn:
        saved = await conn.fetchone(f"SELECT * FROM {table} WHERE guild_id=? AND request_id=?", (guild, request_id))
        if saved:
            if (saved["member_id"], saved["quest_id"], saved["revision_of"], saved["deed"], saved["evidence"]) != (
                    member, quest_id, revision_of, deed, evidence):
                raise ValueError("This request already belongs to another submission.")
            return saved
        quest = await conn.fetchone(f"SELECT quest_type FROM {store._table('quests')} WHERE guild_id=? AND id=? AND status='open'",
                                    (guild, quest_id))
        if not quest:
            raise ValueError("This quest is closed or unavailable.")
        if quest["quest_type"] != "repeatable":
            earned = await conn.fetchone(f"SELECT id FROM {store._table('awards')} WHERE guild_id=? AND quest_id=? AND recipient_id=?",
                                         (guild, quest_id, member))
            if earned:
                raise ValueError("Your completion is already recorded for this quest.")
            pending = await conn.fetchone(f"SELECT id FROM {table} WHERE guild_id=? AND quest_id=? AND member_id=? AND status='pending'",
                                          (guild, quest_id, member))
            if pending:
                raise ValueError("Your deed is already awaiting review.")
            if revision_of is None and await conn.fetchone(f"SELECT id FROM {table} WHERE guild_id=? AND quest_id=? "
                                                          "AND member_id=? AND status='returned'", (guild, quest_id, member)):
                raise ValueError("Open your returned deed in My submissions and choose Revise deed.")
        if revision_of is not None:
            revised = await conn.execute_returning_one(f"UPDATE {table} SET status='revised' WHERE guild_id=? AND id=? "
                                                      "AND quest_id=? AND member_id=? AND status='returned' RETURNING id",
                                                      (guild, revision_of, quest_id, member))
            if not revised:
                raise ValueError("This deed can no longer be revised. Reopen My submissions.")
        return await conn.execute_returning_one(f"INSERT INTO {table} "
            "(guild_id,quest_id,member_id,request_id,deed,evidence,submitted_at,revision_of) VALUES (?,?,?,?,?,?,?,?) RETURNING *",
            (guild, quest_id, member, request_id, deed, evidence, store.utc_now().isoformat(), revision_of))


async def review_count(guild, actor, *, admin):
    """None distinguishes readers without review access from an empty queue."""
    async with db.DBConnection() as conn:
        if not admin and await store._rank(conn, guild, actor) != "hand":
            return None
        row = await conn.fetchone(f"SELECT COUNT(*) AS n FROM {store._table('submissions')} "
                                  "WHERE guild_id=? AND member_id!=? AND status='pending'", (guild, actor))
        return row["n"]


async def listing(guild, actor, *, admin, review, page=1, quest_id=None):
    async with db.DBConnection() as conn:
        if review:
            await store._quest_authority(conn, guild, actor, admin)
        where = "WHERE s.guild_id=? AND " + ("s.member_id!=? AND s.status='pending'" if review else "s.member_id=?")
        params = (guild, actor)
        if quest_id is not None:
            where += " AND s.quest_id=?"
            params += (quest_id,)
        base = _joined() + where
        count = await conn.fetchone("SELECT COUNT(*) AS n" + base, params)
        pages = max(1, (count["n"] + 9) // 10)
        page = max(1, min(page, pages))
        rows = await conn.fetchall("SELECT s.id,s.status,s.member_id,q.title" + base +
                                   (" ORDER BY s.id" if review else " ORDER BY s.id DESC") + " LIMIT 10 OFFSET ?",
                                   params + ((page - 1) * 10,))
        return rows, page, pages


async def detail(guild, actor, number, *, admin, review):
    async with db.DBConnection() as conn:
        if review:
            await store._quest_authority(conn, guild, actor, admin)
        return await conn.fetchone("SELECT s.*,q.title,q.reward,q.quest_type,q.status AS quest_status" + _joined() +
                                   "WHERE s.guild_id=? AND s.id=? AND " + ("s.member_id!=?" if review else "s.member_id=?"),
                                   (guild, number, actor))


async def returned_for(guild, member):
    async with db.DBConnection() as conn:
        return await conn.fetchall("SELECT s.id,q.title" + _joined() + "WHERE s.guild_id=? AND s.member_id=? "
                                   "AND s.status='returned' AND q.status='open' ORDER BY s.id", (guild, member))


async def pending_for_review(guild, actor, *, admin):
    async with db.DBConnection() as conn:
        if not admin and await store._rank(conn, guild, actor) != "hand":
            return []
        return await conn.fetchall("SELECT s.id,q.title" + _joined() + "WHERE s.guild_id=? AND s.member_id!=? "
                                   "AND s.status='pending' ORDER BY s.id", (guild, actor))


async def return_deed(guild, actor, number, note, *, admin):
    note = store.clean(note, 400)
    async with store._write(guild) as conn:
        await store._quest_authority(conn, guild, actor, admin)
        row = await conn.execute_returning_one(f"UPDATE {store._table('submissions')} SET status='returned',note=?,"
            "reviewer_id=?,reviewed_at=? WHERE guild_id=? AND id=? AND member_id!=? AND status='pending' RETURNING id",
            (note, actor, store.utc_now().isoformat(), guild, number, actor))
        if not row:
            raise ValueError("This deed is no longer available for you to review.")
