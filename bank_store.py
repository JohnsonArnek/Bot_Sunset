"""Server-scoped emerald accounts, an append-only journal and atomic checkout."""

import json
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone

import database as db
import models


MAX_BALANCE = 1_000_000_000_000
MAX_AMOUNT = 1_000_000_000
TABLES = ("guilds", "accounts", "operations", "entries")


def table(name):
    return ("sunset_bank." if db.IS_POSTGRES else "bank_") + name


def schema(postgres):
    p = "sunset_bank." if postgres else "bank_"
    identity = "BIGSERIAL PRIMARY KEY" if postgres else "INTEGER PRIMARY KEY AUTOINCREMENT"
    sql = f"""
        CREATE TABLE IF NOT EXISTS {p}guilds (guild_id BIGINT PRIMARY KEY);
        CREATE TABLE IF NOT EXISTS {p}accounts (
            id {identity}, guild_id BIGINT NOT NULL,
            kind TEXT NOT NULL CHECK (kind IN ('member','land','treasury')),
            subject_id BIGINT NOT NULL, label TEXT NOT NULL, created_at TEXT NOT NULL,
            UNIQUE (guild_id,kind,subject_id), UNIQUE (guild_id,id),
            CHECK ((kind='treasury' AND subject_id=0) OR (kind<>'treasury' AND subject_id>0))
        );
        CREATE TABLE IF NOT EXISTS {p}operations (
            id {identity}, guild_id BIGINT NOT NULL, request_id BIGINT NOT NULL,
            actor_id BIGINT NOT NULL, action TEXT NOT NULL
                CHECK (action IN ('deposit','withdraw','claim-normal','claim-reserve','claim-leftover')),
            reason TEXT NOT NULL, created_at TEXT NOT NULL, receipt TEXT NOT NULL,
            UNIQUE (guild_id,request_id), UNIQUE (guild_id,id)
        );
        CREATE TABLE IF NOT EXISTS {p}entries (
            id {identity}, guild_id BIGINT NOT NULL, operation_id BIGINT NOT NULL,
            account_id BIGINT NOT NULL, delta BIGINT NOT NULL CHECK (delta<>0),
            balance_after BIGINT NOT NULL CHECK (balance_after BETWEEN 0 AND {MAX_BALANCE}),
            FOREIGN KEY (guild_id,operation_id) REFERENCES {p}operations (guild_id,id),
            FOREIGN KEY (guild_id,account_id) REFERENCES {p}accounts (guild_id,id),
            UNIQUE (guild_id,operation_id,account_id)
        );
        CREATE INDEX IF NOT EXISTS bank_account_entries_idx ON {p}entries (guild_id,account_id,id);
    """
    if postgres:
        sql = "CREATE SCHEMA IF NOT EXISTS sunset_bank; REVOKE ALL ON SCHEMA sunset_bank FROM PUBLIC; " + sql
        sql += " ".join(f"ALTER TABLE {p}{t} ENABLE ROW LEVEL SECURITY; REVOKE ALL ON {p}{t} FROM PUBLIC;" for t in TABLES)
        for role in ("anon", "authenticated"):
            sql += f"""DO $$ BEGIN IF EXISTS (SELECT FROM pg_roles WHERE rolname='{role}') THEN
                REVOKE ALL ON SCHEMA sunset_bank FROM {role};
                REVOKE ALL ON ALL TABLES IN SCHEMA sunset_bank FROM {role};
            END IF; END $$;"""
    return sql


def now():
    return datetime.now(timezone.utc)


@asynccontextmanager
async def write(guild):
    async with db.DBConnection() as conn, conn.transaction():
        await conn.execute(f"INSERT INTO {table('guilds')} (guild_id) VALUES (?) ON CONFLICT DO NOTHING", (guild,))
        if db.IS_POSTGRES:
            await conn.fetchone(f"SELECT guild_id FROM {table('guilds')} WHERE guild_id=? FOR UPDATE", (guild,))
        yield conn


def require_admin(admin):
    if not admin:
        raise ValueError("Only Discord administrators can administer bank funds or inspect other accounts.")


async def _account(conn, guild, kind, subject, label):
    await conn.execute(f"INSERT INTO {table('accounts')} (guild_id,kind,subject_id,label,created_at) VALUES (?,?,?,?,?) "
                       "ON CONFLICT (guild_id,kind,subject_id) DO NOTHING", (guild, kind, subject, label, now().isoformat()))
    return await conn.fetchone(f"SELECT * FROM {table('accounts')} WHERE guild_id=? AND kind=? AND subject_id=?", (guild, kind, subject))


async def _balance(conn, guild, account_id):
    row = await conn.fetchone(f"SELECT COALESCE(SUM(delta),0) AS balance FROM {table('entries')} WHERE guild_id=? AND account_id=?", (guild, account_id))
    return int(row["balance"])


async def _target(conn, guild, actor, *, admin, member=None, land_name=None, account_id=None, treasury=False):
    if sum(x is not None for x in (member, land_name, account_id)) + bool(treasury) > 1:
        raise ValueError("Choose one account: member, land_name, account_id or treasury.")
    if treasury:
        require_admin(admin)
        return await _account(conn, guild, "treasury", 0, "Central treasury")
    if account_id is not None:
        require_admin(admin)
        row = await conn.fetchone(f"SELECT * FROM {table('accounts')} WHERE guild_id=? AND id=?", (guild, account_id))
        if not row:
            raise ValueError("No account with that ID exists on this server.")
        return row
    if land_name is not None:
        land = await conn.fetchone("SELECT * FROM lands WHERE guild_id=? AND LOWER(name)=LOWER(?)", (guild, land_name.strip()))
        if not land:
            raise ValueError("Land not found on this server.")
        if not admin and actor != land["owner_id"]:
            officer = await conn.fetchone("SELECT 1 FROM officers WHERE land_id=? AND user_id=?", (land["id"], actor))
            if not officer:
                raise ValueError("Only the land owner, its officers or Discord administrators can read this account.")
        return await _account(conn, guild, "land", land["id"], land["name"])
    member = actor if member is None else member
    if member != actor:
        require_admin(admin)
    return await _account(conn, guild, "member", member, f"Member {member}")


async def account(guild, actor, *, admin, page=1, **target):
    import banner_store
    async with write(guild) as conn:
        row = await _target(conn, guild, actor, admin=admin, **target)
        balance = await _balance(conn, guild, row["id"])
        entries = await conn.fetchall(
            f"SELECT e.delta,e.balance_after,o.id AS operation_id,o.actor_id,o.action,o.reason,o.created_at "
            f"FROM {table('entries')} e JOIN {table('operations')} o ON o.guild_id=e.guild_id AND o.id=e.operation_id "
            "WHERE e.guild_id=? AND e.account_id=? ORDER BY e.id DESC LIMIT 3 OFFSET ?", (guild, row["id"], (page-1)*3))
        banners = await banner_store.summary(conn, guild, row["id"]) if row["kind"] in ("land", "treasury") else None
        return row | dict(balance=balance, entries=entries, banners=banners)


async def accounts(guild, *, admin, page=1):
    require_admin(admin)
    async with write(guild) as conn:
        # Every registered land is available even before its first transaction.
        await conn.execute(f"INSERT INTO {table('accounts')} (guild_id,kind,subject_id,label,created_at) "
                           "SELECT guild_id,'land',id,name,? FROM lands WHERE guild_id=? ON CONFLICT DO NOTHING", (now().isoformat(), guild))
        await _account(conn, guild, "treasury", 0, "Central treasury")
        return await conn.fetchall(
            f"SELECT a.*,COALESCE((SELECT SUM(e.delta) FROM {table('entries')} e WHERE e.guild_id=a.guild_id AND e.account_id=a.id),0) AS balance,"
            "CASE WHEN a.kind='land' AND NOT EXISTS (SELECT 1 FROM lands l WHERE l.guild_id=a.guild_id AND l.id=a.subject_id) "
            f"THEN 1 ELSE 0 END AS archived FROM {table('accounts')} a WHERE a.guild_id=? ORDER BY a.id LIMIT 10 OFFSET ?",
            (guild, (page-1)*10))


async def _replay(conn, guild, actor, request_id, action):
    row = await conn.fetchone(f"SELECT * FROM {table('operations')} WHERE guild_id=? AND request_id=?", (guild, request_id))
    if row:
        if row["actor_id"] != actor or row["action"] != action:
            raise ValueError("This interaction already recorded a different transaction.")
        return json.loads(row["receipt"]) | dict(operation_id=row["id"], duplicate=True)


async def _record(conn, guild, actor, request_id, action, reason, receipt, entries):
    op = await conn.execute_returning_one(
        f"INSERT INTO {table('operations')} (guild_id,request_id,actor_id,action,reason,created_at,receipt) VALUES (?,?,?,?,?,?,?) RETURNING id",
        (guild, request_id, actor, action, reason, now().isoformat(), json.dumps(receipt)))
    for account_id, delta, balance_after in entries:
        if delta:
            await conn.execute(f"INSERT INTO {table('entries')} (guild_id,operation_id,account_id,delta,balance_after) VALUES (?,?,?,?,?)",
                               (guild, op["id"], account_id, delta, balance_after))
    return receipt | dict(operation_id=op["id"], duplicate=False)


async def adjust(guild, actor, amount, reason, *, admin, request_id, withdraw=False, **target):
    require_admin(admin)
    if type(amount) is not int or not 1 <= amount <= MAX_AMOUNT:
        raise ValueError(f"Use a whole amount between 1 and {MAX_AMOUNT:,} ems.")
    reason = reason.strip()
    if not 1 <= len(reason) <= 250:
        raise ValueError("Record a reason of 1–250 characters.")
    if not any(target.get(key) is not None for key in ("member", "land_name", "account_id")) and not target.get("treasury"):
        raise ValueError("Explicitly choose the account to deposit into or withdraw from.")
    action = "withdraw" if withdraw else "deposit"
    async with write(guild) as conn:
        row = await _target(conn, guild, actor, admin=True, **target)
        prior = await _replay(conn, guild, actor, request_id, action)
        if prior:
            if prior.get("account_id") != row["id"] or prior.get("amount") != amount or prior.get("reason") != reason:
                raise ValueError("This interaction already recorded a different adjustment.")
            return prior
        delta = -amount if withdraw else amount
        balance = await _balance(conn, guild, row["id"])
        after = balance + delta
        if after < 0:
            raise ValueError(f"Insufficient funds: this account holds {balance:,} ems. Nothing was withdrawn.")
        if after > MAX_BALANCE:
            raise ValueError(f"An account may hold at most {MAX_BALANCE:,} ems.")
        receipt = dict(account_id=row["id"], kind=row["kind"], subject_id=row["subject_id"], label=row["label"],
                       amount=amount, delta=delta, balance=after, reason=reason)
        return await _record(conn, guild, actor, request_id, action, reason, receipt, [(row["id"], delta, after)])


async def buy_claim(guild, actor, *, request_id, payment="manual", kind="normal"):
    import banner_store
    if kind not in ("normal", "reserve", "leftover") or payment not in ("manual", "bank") or (payment == "bank" and kind != "normal"):
        raise ValueError("Bank transfer is available for /claim buy. Other claim purchases use manual payment.")
    async with write(guild) as conn:
        prior = await _replay(conn, guild, actor, request_id, f"claim-{kind}")
        if prior:
            if prior["payment"] != payment:
                raise ValueError("This interaction already used a different payment method.")
            return prior
        lock = " FOR UPDATE" if db.IS_POSTGRES else ""
        land = await conn.fetchone("SELECT * FROM lands WHERE guild_id=? AND owner_id=? ORDER BY id LIMIT 1" + lock, (guild, actor))
        if not land:
            raise ValueError("Only the registered land owner can purchase claim blocks.")
        at = now()
        blocks, approved = 1, None
        if kind == "normal":
            if at.weekday() >= 5:
                raise ValueError("Normal claim purchases are available Monday–Friday (UTC).")
            monday = (at - timedelta(days=at.weekday())).replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
            used = await conn.fetchone("SELECT id FROM purchases WHERE land_id=? AND type='normal' AND purchased_at>=? LIMIT 1", (land["id"], monday))
            if used:
                raise ValueError("This land already used its normal purchase this week. The allowance resets Monday at 00:00 UTC.")
            approved = await conn.fetchone("SELECT * FROM claim_requests WHERE land_id=? AND status='approved' ORDER BY requested_at,id LIMIT 1" + lock, (land["id"],))
            if not approved:
                raise ValueError("No approved claim request is available. Submit /claim request and wait for approval.")
            # Eligibility follows the saved status. The expiry worker uses a
            # conditional update so its earlier scan cannot undo this checkout.
            blocks = approved["chunks_requested"]
        else:
            await conn.execute("INSERT INTO reserve (guild_id,total_blocks,protected_min,leftover_blocks) VALUES (?,0,3,0) ON CONFLICT DO NOTHING", (guild,))
            reserve = await conn.fetchone("SELECT * FROM reserve WHERE guild_id=?" + lock, (guild,))
            available = reserve["leftover_blocks"] if kind == "leftover" else reserve["total_blocks"] - reserve["protected_min"]
            if available < 1:
                raise ValueError("No blocks are available in that pool (protected reserves are not for sale).")
        config = await conn.fetchone("SELECT value FROM config WHERE guild_id=? AND key='full_block_cost'", (guild,))
        try:
            full_price = int(config["value"] if config else db.DEFAULT_CONFIG["full_block_cost"])
        except (ValueError, TypeError):
            raise ValueError("Ask an administrator to correct the full_block_cost setting.") from None
        if not 0 <= full_price <= MAX_AMOUNT or not 1 <= blocks <= 2_000_000_000 or not 0 <= land["chunks"] <= 2_000_000_000 - blocks:
            raise ValueError("The configured price or recorded chunk count is invalid; ask an administrator to review it.")
        unit = models.reserve_price(land["chunks"], full_price) if kind == "reserve" else models.block_price(land["chunks"], full_price)
        total = unit * blocks
        if total > 2_000_000_000:
            raise ValueError("This purchase exceeds the supported transaction amount.")
        banner = None
        if kind == "normal":
            creation_cost = await banner_store.unit_cost(conn, guild)
            land_unit = min(unit, creation_cost)
            banner = dict(unit_cost=creation_cost, land_unit=land_unit,
                          prepaid_unit=land_unit if payment == "bank" else 0)
        entries, after = [], None
        if payment == "bank":
            payer = await _account(conn, guild, "land", land["id"], land["name"])
            treasury = await _account(conn, guild, "treasury", 0, "Central treasury")
            balance = await _balance(conn, guild, payer["id"])
            treasury_balance = await _balance(conn, guild, treasury["id"])
            if balance < total:
                raise ValueError(f"Insufficient settlement funds: need {total:,} ems; available {balance:,}. Nothing was purchased or charged.")
            if treasury_balance + total > MAX_BALANCE:
                raise ValueError("The central treasury cannot accept this payment. Ask an administrator to review its balance.")
            after = balance - total
            entries = [(payer["id"], -total, after), (treasury["id"], total, treasury_balance + total)]
        if approved:
            changed = await conn.execute_returning_one("UPDATE claim_requests SET status='purchased' WHERE id=? AND status='approved' RETURNING id", (approved["id"],))
            if not changed:
                raise ValueError("The request changed before checkout. Nothing was purchased or charged.")
        if kind != "normal":
            column = "leftover_blocks" if kind == "leftover" else "total_blocks"
            await conn.execute(f"UPDATE reserve SET {column}={column}-1 WHERE guild_id=?", (guild,))
        await conn.execute("UPDATE lands SET chunks=chunks+? WHERE id=? AND guild_id=?", (blocks, land["id"], guild))
        await conn.execute("INSERT INTO purchases (land_id,type,price_paid,purchased_at) VALUES (?,?,?,?)", (land["id"], kind, total, at.isoformat()))
        note = f"{kind.title()} claim: {blocks} block(s) x {unit} ems = {total} ems; {'bank paid' if payment == 'bank' else 'manual payment'}"
        await conn.execute("INSERT INTO block_ledger (land_id,guild_id,type,blocks,note,recorded_by,created_at) VALUES (?,?,'owed',?,?,?,?)",
                           (land["id"], guild, blocks, note, actor, at.isoformat()))
        receipt = dict(land_id=land["id"], land_name=land["name"], blocks=blocks, chunks=land["chunks"] + blocks,
                       unit_price=unit, total=total, payment=payment, balance=after, kind=kind)
        if banner:
            receipt["banner"] = banner
        result = await _record(conn, guild, actor, request_id, f"claim-{kind}", note, receipt, entries)
        if banner:
            await banner_store.record_batch(conn, guild, result)
        return result
