"""Claim-banner creation liabilities, funded separately from delivery."""

import bank_store as bank
import database as db


def table(name):
    return ("sunset_banners." if db.IS_POSTGRES else "banner_") + name


def schema(postgres):
    p = "sunset_banners." if postgres else "banner_"
    b = "sunset_bank." if postgres else "bank_"
    sql = f"""
        CREATE TABLE IF NOT EXISTS {p}batches (
            id BIGINT PRIMARY KEY, guild_id BIGINT NOT NULL,
            land_account_id BIGINT NOT NULL, treasury_account_id BIGINT NOT NULL,
            blocks BIGINT NOT NULL CHECK (blocks BETWEEN 1 AND 2000000000),
            unit_cost BIGINT NOT NULL CHECK (unit_cost BETWEEN 1 AND {bank.MAX_AMOUNT}),
            land_unit BIGINT NOT NULL CHECK (land_unit BETWEEN 0 AND unit_cost),
            prepaid_unit BIGINT NOT NULL CHECK (prepaid_unit BETWEEN 0 AND land_unit),
            UNIQUE (guild_id,id),
            FOREIGN KEY (guild_id,id) REFERENCES {b}operations (guild_id,id),
            FOREIGN KEY (guild_id,land_account_id) REFERENCES {b}accounts (guild_id,id),
            FOREIGN KEY (guild_id,treasury_account_id) REFERENCES {b}accounts (guild_id,id)
        );
        CREATE TABLE IF NOT EXISTS {p}creations (
            operation_id BIGINT PRIMARY KEY, guild_id BIGINT NOT NULL,
            batch_id BIGINT NOT NULL, blocks BIGINT NOT NULL CHECK (blocks>0),
            physical_land_payment BIGINT NOT NULL CHECK (physical_land_payment>=0),
            FOREIGN KEY (guild_id,operation_id) REFERENCES {b}operations (guild_id,id),
            FOREIGN KEY (guild_id,batch_id) REFERENCES {p}batches (guild_id,id)
        );
        CREATE INDEX IF NOT EXISTS banner_land_idx ON {p}batches (guild_id,land_account_id,id);
        CREATE INDEX IF NOT EXISTS banner_creation_batch_idx ON {p}creations (guild_id,batch_id);
        CREATE TABLE IF NOT EXISTS {p}production_weeks (
            guild_id BIGINT NOT NULL, week_start TEXT NOT NULL,
            produced BIGINT NOT NULL CHECK (produced BETWEEN 0 AND 2000000000),
            PRIMARY KEY (guild_id,week_start)
        );
        CREATE TABLE IF NOT EXISTS {p}sales (
            guild_id BIGINT NOT NULL, source TEXT NOT NULL CHECK (source IN ('checkout','allocation','grant')),
            source_id BIGINT NOT NULL, week_start TEXT NOT NULL,
            kind TEXT NOT NULL, blocks BIGINT NOT NULL CHECK (blocks>0),
            released BIGINT NOT NULL CHECK (released BETWEEN 0 AND blocks),
            unit_cost BIGINT NOT NULL CHECK (unit_cost BETWEEN 1 AND {bank.MAX_AMOUNT}),
            proceeds BIGINT NOT NULL CHECK (proceeds>=0),
            payment TEXT NOT NULL CHECK (payment IN ('manual','bank')),
            PRIMARY KEY (guild_id,source,source_id),
            FOREIGN KEY (guild_id,week_start) REFERENCES {p}production_weeks (guild_id,week_start)
        );
        CREATE INDEX IF NOT EXISTS banner_sales_week_idx ON {p}sales (guild_id,week_start);
    """
    if postgres:
        sql = "CREATE SCHEMA IF NOT EXISTS sunset_banners; REVOKE ALL ON SCHEMA sunset_banners FROM PUBLIC; " + sql
        for name in ("batches", "creations", "production_weeks", "sales"):
            sql += f"ALTER TABLE {p}{name} ENABLE ROW LEVEL SECURITY; REVOKE ALL ON {p}{name} FROM PUBLIC; "
        for role in ("anon", "authenticated"):
            sql += f"""DO $$ BEGIN IF EXISTS (SELECT FROM pg_roles WHERE rolname='{role}') THEN
                REVOKE ALL ON SCHEMA sunset_banners FROM {role};
                REVOKE ALL ON ALL TABLES IN SCHEMA sunset_banners FROM {role};
            END IF; END $$;"""
    return sql


async def unit_cost(conn, guild):
    row = await conn.fetchone("SELECT value FROM config WHERE guild_id=? AND key='banner_creation_cost'", (guild,))
    try:
        cost = int(row["value"] if row else db.DEFAULT_CONFIG["banner_creation_cost"])
    except (ValueError, TypeError):
        cost = 0
    if not 1 <= cost <= bank.MAX_AMOUNT:
        raise ValueError("Ask an administrator to correct /banner cost before purchasing.")
    return cost


async def cost(guild, *, admin, amount=None):
    from banner_account import ensure_week
    bank.require_admin(admin)
    async with bank.write(guild) as conn:
        if amount is not None:
            if type(amount) is not int or not 1 <= amount <= bank.MAX_AMOUNT:
                raise ValueError(f"Creation cost must be 1–{bank.MAX_AMOUNT:,} whole emeralds.")
            # Preserve the old estimate for opening-week sales that predate cost snapshots.
            try:
                await unit_cost(conn, guild)
            except ValueError:
                pass  # An invalid old setting can still be corrected.
            else:
                await ensure_week(conn, guild, bank.now())
            await conn.execute("INSERT INTO config (guild_id,key,value) VALUES (?,'banner_creation_cost',?) "
                               "ON CONFLICT (guild_id,key) DO UPDATE SET value=excluded.value", (guild,str(amount)))
        return await unit_cost(conn, guild)


async def record_batch(conn, guild, receipt):
    """Save the settlement and creation terms of a banner purchase."""
    land = await bank._account(conn, guild, "land", receipt["land_id"], receipt["land_name"])
    treasury = await bank._account(conn, guild, "treasury", 0, "Central treasury")
    terms = receipt["banner"]
    await conn.execute(f"INSERT INTO {table('batches')} (id,guild_id,land_account_id,treasury_account_id,blocks,unit_cost,land_unit,prepaid_unit) "
                       "VALUES (?,?,?,?,?,?,?,?)", (receipt["operation_id"],guild,land["id"],treasury["id"],receipt["blocks"],
                                                   terms["unit_cost"],terms["land_unit"],terms["prepaid_unit"]))


def receipt_field(key):
    if db.IS_POSTGRES:
        return f"(CAST(o.receipt AS jsonb)->>'{key}')"
    return f"json_extract(o.receipt,'$.{key}')"


def batch_query():
    return (f"SELECT b.*,a.label AS land_name,a.subject_id AS land_id,o.created_at,"
            f"{receipt_field('payment')} AS payment,CAST({receipt_field('unit_price')} AS BIGINT) AS sale_unit,"
            "CASE WHEN s.kind='reserve' AND s.payment='manual' THEN (s.proceeds/s.blocks)-b.unit_cost "
            f"ELSE COALESCE(CAST({receipt_field('premium_unit')} AS BIGINT),0) END AS premium_unit,"
            f"CAST(COALESCE((SELECT SUM(c.blocks) FROM {table('creations')} c WHERE c.guild_id=b.guild_id AND c.batch_id=b.id "
            "AND c.physical_land_payment>0),0) AS BIGINT) AS physical_blocks,"
            f"CAST(COALESCE((SELECT SUM(c.blocks) FROM {table('creations')} c WHERE c.guild_id=b.guild_id AND c.batch_id=b.id),0) AS BIGINT) AS created "
            f"FROM {table('batches')} b JOIN {bank.table('accounts')} a ON a.guild_id=b.guild_id AND a.id=b.land_account_id "
            f"JOIN {bank.table('operations')} o ON o.guild_id=b.guild_id AND o.id=b.id "
            f"LEFT JOIN {table('sales')} s ON s.guild_id=b.guild_id AND s.source='checkout' AND s.source_id=b.id ")


def payment_counts_query(*, by_land=False, filter_land=False):
    """Group whole banners by the evidence of payment on the receipt and its creations."""
    terms = batch_query() + "WHERE b.guild_id=?"
    if filter_land:
        terms += " AND a.subject_id=?"
    classified = (
        "SELECT land_id,land_name,blocks,sale_unit,land_unit+premium_unit AS collectible,"
        "CASE WHEN payment='manual' AND sale_unit>0 THEN blocks-created ELSE 0 END AS due,"
        "CASE WHEN payment='bank' AND sale_unit>0 THEN blocks ELSE 0 END AS transfer,"
        "CASE WHEN payment='manual' AND sale_unit>0 THEN created-physical_blocks ELSE 0 END AS debited,"
        "physical_blocks,CASE WHEN sale_unit=0 THEN blocks ELSE 0 END AS free "
        f"FROM ({terms}) terms")
    # A legacy bank receipt still proves payment even when it predates creation tracking.
    legacy_land = f"CAST({receipt_field('land_id')} AS BIGINT)"
    legacy_price = f"CAST({receipt_field('unit_price')} AS BIGINT)"
    legacy_blocks = f"CAST({receipt_field('blocks')} AS BIGINT)"
    legacy = (f"SELECT {legacy_land} AS land_id,{receipt_field('land_name')} AS land_name,{legacy_blocks} AS blocks,"
              f"{legacy_price} AS sale_unit,0 AS collectible,0 AS due,CASE WHEN {legacy_price}>0 THEN {legacy_blocks} ELSE 0 END AS transfer,"
              f"0 AS debited,0 AS physical_blocks,CASE WHEN {legacy_price}=0 THEN {legacy_blocks} ELSE 0 END AS free "
              f"FROM {bank.table('operations')} o WHERE o.guild_id=? AND o.action IN ('claim-normal','claim-reserve','claim-leftover') "
              f"AND ({receipt_field('payment')}='bank' OR {legacy_price}=0) AND {legacy_land} IS NOT NULL "
              f"AND NOT EXISTS (SELECT 1 FROM {table('batches')} b WHERE b.guild_id=o.guild_id AND b.id=o.id)")
    if filter_land:
        legacy += f" AND {legacy_land}=?"
    groups = "land_id,MAX(land_name) AS land_name," if by_land else ""
    return (
        f"SELECT {groups}COALESCE(SUM(blocks),0) AS tracked,"
        "COALESCE(SUM(due),0) AS due_blocks,COALESCE(SUM(due*collectible),0) AS due_ems,"
        "COALESCE(SUM(transfer+debited),0) AS bank_blocks,"
        "COALESCE(SUM(transfer*sale_unit+debited*collectible),0) AS bank_ems,"
        "COALESCE(SUM(physical_blocks),0) AS physical_blocks,"
        "COALESCE(SUM(physical_blocks*collectible),0) AS physical_ems,COALESCE(SUM(free),0) AS free_blocks,"
        "COALESCE(SUM((due+debited+physical_blocks)*(sale_unit-collectible)),0) AS unverified_ems "
        f"FROM ({classified} UNION ALL {legacy}) payments" + (" GROUP BY land_id" if by_land else ""))


async def payment_counts(conn, guild, land_id=None):
    params = (guild, land_id) if land_id is not None else (guild,)
    row = await conn.fetchone(payment_counts_query(filter_land=land_id is not None), params*2)
    return {k: int(v) for k,v in row.items()}


async def allow_land(conn, guild, actor, land_id, admin):
    if admin:
        return
    allowed = await conn.fetchone("SELECT id FROM lands WHERE guild_id=? AND id=? AND (owner_id=? OR EXISTS "
                                 "(SELECT 1 FROM officers WHERE land_id=lands.id AND user_id=?))", (guild,land_id,actor,actor))
    if not allowed:
        raise ValueError("Only this land's owner, its officers or Discord administrators can view its banner costs.")


async def summary(conn, guild, account_id=None):
    query = batch_query() + "WHERE b.guild_id=?"
    params = (guild,)
    if account_id is not None:
        query += " AND (b.land_account_id=? OR b.treasury_account_id=?)"
        params += (account_id,account_id)
    row = await conn.fetchone(
        "SELECT COALESCE(SUM(pending),0) AS pending,COALESCE(SUM(pending*unit_cost),0) AS total,"
        "COALESCE(SUM(pending*land_unit),0) AS land_share,COALESCE(SUM(pending*(unit_cost-land_unit)),0) AS central_share,"
        "COALESCE(SUM(pending*prepaid_unit),0) AS prepaid,"
        "COALESCE(SUM(pending*premium_unit),0) AS premium_due,"
        "COALESCE(SUM(created*unit_cost),0) AS destroyed FROM (SELECT terms.*,blocks-created AS pending FROM ("
        + query + ") terms) s", params)
    return {key: int(value) for key,value in row.items()}


async def status(guild, actor, *, admin, land_name=None, page=1):
    async with bank.write(guild) as conn:
        account_id = None
        if land_name is not None:
            account = await bank._target(conn,guild,actor,admin=admin,land_name=land_name)
            account_id = account["id"]
        elif not admin:
            raise ValueError("Choose land_name to view your settlement's banner costs.")
        where, params = " WHERE b.guild_id=?", (guild,)
        if account_id is not None:
            where += " AND b.land_account_id=?"
            params += (account_id,)
        rows = await conn.fetchall(batch_query()+where+" ORDER BY b.id DESC LIMIT 3 OFFSET ?", params+((page-1)*3,))
        return dict(totals=await summary(conn,guild,account_id),batches=rows,
                    payments=await payment_counts(conn,guild,account["subject_id"] if account_id is not None else None))


async def show(guild, actor, purchase_id, *, admin):
    async with bank.write(guild) as conn:
        row = await conn.fetchone(batch_query()+"WHERE b.guild_id=? AND b.id=?", (guild,purchase_id))
        if not row:
            raise ValueError("No tracked banner purchase with that receipt ID exists on this server.")
        await allow_land(conn,guild,actor,row["land_id"],admin)
        history = await conn.fetchall(
            f"SELECT c.*,o.actor_id,o.created_at,o.reason FROM {table('creations')} c JOIN {bank.table('operations')} o "
            "ON o.guild_id=c.guild_id AND o.id=c.operation_id WHERE c.guild_id=? AND c.batch_id=? ORDER BY c.operation_id DESC LIMIT 3",
            (guild,purchase_id))
        return row | dict(history=history)


async def create(guild, actor, purchase_id, blocks, reason, *, admin, request_id, land_payment="bank"):
    bank.require_admin(admin)
    if type(blocks) is not int or not 1 <= blocks <= 2_000_000_000:
        raise ValueError("Choose a positive whole number of banners to mark created.")
    reason = reason.strip()
    if not 1 <= len(reason) <= 250 or land_payment not in ("bank", "physical"):
        raise ValueError("Supply a 1–250 character reason and choose bank or physical land payment.")
    async with bank.write(guild) as conn:
        prior = await bank._replay(conn,guild,actor,request_id,"withdraw")
        if prior:
            if any(prior.get(k) != v for k,v in dict(receipt_type="banner-creation",purchase_id=purchase_id,
                    blocks=blocks,reason=reason,land_payment=land_payment).items()):
                raise ValueError("This interaction already recorded a different transaction.")
            return prior
        row = await conn.fetchone(batch_query()+"WHERE b.guild_id=? AND b.id=?", (guild,purchase_id))
        if not row:
            raise ValueError("No tracked banner purchase with that receipt ID exists on this server.")
        remaining = row["blocks"]-row["created"]
        if blocks > remaining:
            raise ValueError(f"Only {remaining:,} banners remain uncreated for this purchase. Nothing was debited.")
        if row["prepaid_unit"] and land_payment == "physical":
            raise ValueError("This land already paid by bank transfer. Use bank to spend that prepaid contribution once.")
        premium = row["premium_unit"]*blocks
        land_due = (row["land_unit"]-row["prepaid_unit"])*blocks + premium
        physical = land_due if land_payment == "physical" else 0
        land_debit = land_due-physical
        central_share = (row["unit_cost"]-row["land_unit"])*blocks
        treasury_debit = central_share+row["prepaid_unit"]*blocks - premium
        entries = []
        for account_id,amount,label in ((row["land_account_id"],land_debit,"Land"),
                                        (row["treasury_account_id"],treasury_debit,"Central treasury")):
            if not amount:
                continue
            balance = await bank._balance(conn,guild,account_id)
            if balance-amount > bank.MAX_BALANCE:
                raise ValueError("The central treasury cannot accept this reserve premium.")
            if balance < amount:
                raise ValueError(f"{label} needs {amount:,} ems; available {balance:,}. Nothing was created or debited. "
                                 "The central subsidy does not increase to cover a land's shortage.")
            entries.append((account_id,-amount,balance-amount))
        receipt = dict(receipt_type="banner-creation",purchase_id=purchase_id,blocks=blocks,reason=reason,
                       land_payment=land_payment,land_name=row["land_name"],total=blocks*row["unit_cost"],
                       land_share=blocks*row["land_unit"],central_share=central_share,physical=physical,
                       land_debit=land_debit,treasury_debit=treasury_debit,remaining=remaining-blocks)
        result = await bank._record(conn,guild,actor,request_id,"withdraw",
                                   f"Banner creation for purchase #{purchase_id}: {reason}",receipt,entries)
        await conn.execute(f"INSERT INTO {table('creations')} (operation_id,guild_id,batch_id,blocks,physical_land_payment) VALUES (?,?,?,?,?)",
                           (result["operation_id"],guild,purchase_id,blocks,physical))
        return result
