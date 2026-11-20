"""Weekly queue allocation, including the handoff of an owner's declined blocks."""

from datetime import datetime, timedelta

import bank_store as bank
import banner_account
import banner_store
import database as db
import models


DETAIL = ("SELECT r.*, l.name AS land_name,l.chunks,l.owner_id,l.guild_id "
          "FROM claim_requests r JOIN lands l ON l.id=r.land_id ")


async def ranked(conn, guild, at):
    rows = await conn.fetchall(
        "SELECT r.*,l.name AS land_name,l.chunks,l.owner_id,COALESCE(l.bonus_members,0) + "
        "(SELECT COUNT(*) FROM members m WHERE m.land_id=l.id) AS members "
        "FROM claim_requests r JOIN lands l ON l.id=r.land_id WHERE l.guild_id=? AND r.status='pending' "
        "ORDER BY r.requested_at,r.id", (guild,))
    full = await banner_store.unit_cost(conn, guild)
    for row in rows:
        row["days"] = models.days_in_queue(row["requested_at"], at)
        row["score"] = models.queue_score(row["chunks"], row["members"], row["days"],
                                          row["purpose"] == "vault", row["purpose"] == "builds")
        row["price"] = models.block_price(row["chunks"], full)
    return sorted(rows, key=lambda row: -row["score"])


async def unavailable_lands(conn, guild, at):
    monday = (at - timedelta(days=at.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
    rows = await conn.fetchall(
        "SELECT l.id FROM lands l WHERE l.guild_id=? AND (EXISTS "
        "(SELECT 1 FROM claim_requests r WHERE r.land_id=l.id AND r.status='approved') OR EXISTS "
        "(SELECT 1 FROM purchases p WHERE p.land_id=l.id AND p.type='normal' AND p.purchased_at>=?))",
        (guild, monday.isoformat()))
    return {row["id"] for row in rows}


async def allocate(conn, guild, available, at, exclude=None):
    """Return leftover quantity and number of partial fills. Caller owns the guild lock."""
    if not available:
        return 0, 0
    skip = await unavailable_lands(conn, guild, at)
    skip.add(exclude)
    partial = 0
    for row in await ranked(conn, guild, at):
        if row["land_id"] in skip:
            continue
        quantity = min(available, row["chunks_requested"])
        changed = await conn.execute_returning_one(
            "UPDATE claim_requests SET status='approved',approved_at=?,original_blocks=chunks_requested,chunks_requested=? "
            "WHERE id=? AND status='pending' RETURNING id", (at.isoformat(), quantity, row["id"]))
        if changed:
            skip.add(row["land_id"])
            partial += int(quantity < row["chunks_requested"])
            available -= quantity
            if available == 0:
                break
    return available, partial


async def reserve_row(conn, guild):
    await conn.execute("INSERT INTO reserve (guild_id,total_blocks,protected_min,leftover_blocks) "
                       "VALUES (?,0,3,0) ON CONFLICT DO NOTHING", (guild,))
    lock = " FOR UPDATE" if db.IS_POSTGRES else ""
    return await conn.fetchone("SELECT * FROM reserve WHERE guild_id=?" + lock, (guild,))


async def expire(conn, guild, at):
    cutoff = (at - timedelta(days=7)).isoformat()
    rows = await conn.fetchall(DETAIL + "WHERE l.guild_id=? AND r.status='approved' "
                               "AND COALESCE(r.approved_at,r.requested_at)<=?", (guild, cutoff))
    expired = []
    for row in rows:
        if await conn.execute_returning_one("UPDATE claim_requests SET status='expired' WHERE id=? "
                                            "AND status='approved' RETURNING id", (row["id"],)):
            expired.append(row)
    if expired:
        await reserve_row(conn, guild)
        await conn.execute("UPDATE reserve SET total_blocks=total_blocks+? WHERE guild_id=?",
                           (sum(row["chunks_requested"] for row in expired), guild))
    return expired


def distribution_due(settings, at):
    day, hour = int(settings["distribution_day"]), int(settings["distribution_hour"])
    start = (at - timedelta(days=at.weekday()) + timedelta(days=day)).replace(
        hour=hour, minute=0, second=0, microsecond=0)
    if at < start or settings.get("claim_mode") == "rotation":
        return False
    try:
        last = datetime.fromisoformat(settings["last_distribution"])
    except (ValueError, TypeError):
        return True
    return banner_account.week_of(last) < banner_account.week_of(at)


async def distribute(guild, at):
    """Commit all inventory movements and the distribution marker together."""
    async with bank.write(guild) as conn:
        rows = await conn.fetchall("SELECT key,value FROM config WHERE guild_id=?", (guild,))
        settings = {**db.DEFAULT_CONFIG, **{row["key"]: row["value"] for row in rows}}
        if not distribution_due(settings, at):
            return None
        week = await banner_account.ensure_week(conn, guild, at)
        production = await conn.fetchone(
            f"SELECT produced FROM {banner_store.table('production_weeks')} WHERE guild_id=? AND week_start=?",
            (guild, week))
        generated = production["produced"]
        expired = await expire(conn, guild, at)
        reserve = await reserve_row(conn, guild)
        carried = reserve["leftover_blocks"]
        target = (int(settings["weekly_reserve_blocks"]) if settings["reserve_mode"].lower() == "fixed"
                  else reserve["protected_min"] - reserve["total_blocks"] - carried)
        saved = min(generated, max(0, target))
        leftover, partial = await allocate(conn, guild, generated - saved, at)
        await conn.execute("UPDATE reserve SET total_blocks=total_blocks+?,leftover_blocks=? WHERE guild_id=?",
                           (carried + saved, leftover, guild))
        await conn.execute("INSERT INTO config (guild_id,key,value) VALUES (?,'last_distribution',?) "
                           "ON CONFLICT (guild_id,key) DO UPDATE SET value=excluded.value", (guild, at.isoformat()))
        return dict(generated=generated, carried=carried, expired=expired, offered=generated-saved-leftover,
                    partial=partial, leftover=leftover)


async def decline(guild, owner, request):
    async with bank.write(guild) as conn:
        at = bank.now()
        lock = " FOR UPDATE" if db.IS_POSTGRES else ""
        row = await conn.fetchone(DETAIL + "WHERE l.guild_id=? AND l.owner_id=? AND r.id=?" + lock,
                                  (guild, owner, request))
        if not row or row["status"] != "approved":
            raise ValueError("You have no open allocation for this request.")
        if row["approved_at"] and at >= datetime.fromisoformat(row["approved_at"]) + timedelta(days=7):
            raise ValueError("This allocation has expired.")
        await conn.execute("UPDATE claim_requests SET status='declined' WHERE id=?", (request,))
        remaining, _ = await allocate(conn, guild, row["chunks_requested"], at, exclude=row["land_id"])
        if remaining:
            await reserve_row(conn, guild)
            await conn.execute("UPDATE reserve SET leftover_blocks=leftover_blocks+? WHERE guild_id=?", (remaining, guild))
        return row["chunks_requested"] - remaining, remaining


async def review(guild, request, approve):
    async with bank.write(guild) as conn:
        at = bank.now()
        row = await conn.fetchone(DETAIL + "WHERE l.guild_id=? AND r.id=? AND r.status='pending'", (guild, request))
        if not row:
            raise ValueError("This request has already been reviewed. Reopen /queue for the current list.")
        if approve and row["land_id"] in await unavailable_lands(conn, guild, at):
            raise ValueError("This settlement already holds an allocation or used its weekly purchase.")
        if not await conn.execute_returning_one(
                "UPDATE claim_requests SET status=?,approved_at=? WHERE id=? AND status='pending' RETURNING id",
                ("approved" if approve else "denied", at.isoformat() if approve else None, request)):
            raise ValueError("This request has already been reviewed.")
        return row


async def offers(guild, *, owner=None, unnotified=False):
    async with db.DBConnection() as conn:
        sql = DETAIL + "WHERE l.guild_id=? AND r.status='approved'"
        params = (guild,)
        if owner is not None:
            sql += " AND l.owner_id=?"
            params += (owner,)
        if unnotified:
            sql += " AND r.approved_at IS NOT NULL AND r.offer_notified_at IS NULL"
        sql += " AND (r.approved_at IS NULL OR r.approved_at>?) ORDER BY r.requested_at,r.id"
        rows = await conn.fetchall(sql, params + ((bank.now()-timedelta(days=7)).isoformat(),))
        full = await banner_store.unit_cost(conn, guild)
        for row in rows:
            row["price"] = models.block_price(row["chunks"], full)
        return rows
