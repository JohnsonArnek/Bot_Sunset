"""Weekly banner production estimates and the public, server-wide sales account."""

import json
from datetime import date, timedelta

import bank_store as bank
import banner_store as banners
import database as db


def week_of(at):
    return (at.date() - timedelta(days=at.weekday())).isoformat()


async def remaining(conn, guild):
    row = await conn.fetchone(
        f"SELECT COALESCE((SELECT SUM(produced) FROM {banners.table('production_weeks')} WHERE guild_id=?),0)"
        f" - COALESCE((SELECT SUM(released) FROM {banners.table('sales')} WHERE guild_id=?),0) AS blocks",
        (guild, guild))
    return int(row["blocks"])


async def record_sale(conn, guild, week, source, source_id, kind, blocks, cost, proceeds, payment, *, released=None):
    # Sales from opening reserves or additional authorisations may exceed the assumed production.
    # Release only stock already on the books. Future weeks start with their full production.
    if released is None:
        released = min(blocks, await remaining(conn, guild))
    await conn.execute(
        f"INSERT INTO {banners.table('sales')} "
        "(guild_id,source,source_id,week_start,kind,blocks,released,unit_cost,proceeds,payment) "
        "VALUES (?,?,?,?,?,?,?,?,?,?)",
        (guild, source, source_id, week, kind, blocks, released, cost, proceeds, payment))


async def import_opening_sales(conn, guild, week, cost):
    """Import only the opening week's actual trades. Never replay money or delivery entries."""
    end = (date.fromisoformat(week) + timedelta(weeks=1)).isoformat()
    checkouts = await conn.fetchall(
        f"SELECT id,created_at,receipt FROM {bank.table('operations')} WHERE guild_id=? "
        "AND action IN ('claim-normal','claim-leftover','claim-reserve') AND created_at>=? AND created_at<?",
        (guild, week, end))
    sales = []
    for row in checkouts:
        receipt = json.loads(row["receipt"])
        if receipt["kind"] in ("auction", "rotation"):
            continue  # Their original purchase rows are included below.
        sales.append((row["created_at"], "checkout", row["id"], receipt["kind"], receipt["blocks"],
                      receipt.get("banner", {}).get("unit_cost", cost), receipt["total"], receipt["payment"]))
    allocations = await conn.fetchall(
        "SELECT p.id,p.type,p.price_paid,p.purchased_at FROM purchases p JOIN lands l ON l.id=p.land_id "
        "WHERE l.guild_id=? AND p.purchased_at>=? AND p.purchased_at<? AND p.type IN ('auction','rotation','strategic')",
        (guild, week, end))
    for row in allocations:
        sales.append((row["purchased_at"], "allocation", row["id"], row["type"], 1, cost, row["price_paid"], "manual"))
    for _, *sale in sorted(sales):
        await record_sale(conn, guild, week, *sale)


async def ensure_week(conn, guild, at, *, repaired_quantity=None):
    """Called under the guild bank lock. A restart catches up every intervening UTC week."""
    week = week_of(at)
    last = await conn.fetchone(
        f"SELECT week_start FROM {banners.table('production_weeks')} WHERE guild_id=? ORDER BY week_start DESC LIMIT 1",
        (guild,))
    if last and last["week_start"] >= week:
        return week
    setting = await conn.fetchone("SELECT value FROM config WHERE guild_id=? AND key='weekly_blocks'", (guild,))
    try:
        quantity = int(setting["value"] if setting else db.DEFAULT_CONFIG["weekly_blocks"])
        if not 0 <= quantity <= 2_000_000_000:
            raise ValueError
    except ValueError:
        if repaired_quantity is None:
            raise ValueError("Set weekly_blocks to a whole number from 0 through 2,000,000,000.") from None
        quantity = repaired_quantity
    cursor = date.fromisoformat(last["week_start"]) + timedelta(weeks=1) if last else date.fromisoformat(week)
    while cursor.isoformat() <= week:
        await conn.execute(f"INSERT INTO {banners.table('production_weeks')} (guild_id,week_start,produced) VALUES (?,?,?)",
                           (guild, cursor.isoformat(), quantity))
        cursor += timedelta(weeks=1)
    if not last:
        await import_opening_sales(conn, guild, week, await banners.unit_cost(conn, guild))
    return week


async def weekly_production(guild):
    async with bank.write(guild) as conn:
        week = await ensure_week(conn, guild, bank.now())
        row = await conn.fetchone(f"SELECT produced FROM {banners.table('production_weeks')} WHERE guild_id=? AND week_start=?",
                                  (guild, week))
        return row["produced"]


async def record_allocation_sale(conn, guild, land_id, kind, price, blocks, at, *, grant_id=None, cost=None, actor=None):
    week = await ensure_week(conn, guild, at)
    cost = await banners.unit_cost(conn, guild) if cost is None else cost
    for _ in range(blocks):
        row = await conn.execute_returning_one("INSERT INTO purchases (land_id,type,price_paid,purchased_at) "
                                              "VALUES (?,?,?,?) RETURNING id", (land_id,kind,price,at.isoformat()))
    await record_sale(conn, guild, week, "grant" if grant_id else "allocation", grant_id or row["id"],
                      kind, blocks, cost, blocks*price, "manual")
    if kind in ("rotation", "auction"):
        land = await conn.fetchone("SELECT name FROM lands WHERE guild_id=? AND id=?", (guild,land_id))
        land_unit = min(price, cost)
        receipt = dict(land_id=land_id, land_name=land["name"], blocks=blocks, kind=kind, payment="manual",
                       unit_price=price, total=price*blocks, premium_unit=price-land_unit,
                       banner=dict(unit_cost=cost, land_unit=land_unit, prepaid_unit=0))
        # Discord request IDs are positive. Scheduled allocations use the negative purchase ID.
        action = "claim-reserve" if kind == "auction" else "claim-normal"
        purchase = await bank._record(conn, guild, actor or 0, -row["id"], action,
                                      f"{kind.title()} banner allocation", receipt, [])
        await banners.record_batch(conn, guild, purchase)


async def allocate(conn, guild, land_id, kind, price, blocks, *, actor, note=None, grant_id=None, cost=None):
    """Post the land allocation, its sale and delivery obligation together."""
    at = bank.now()
    land = await conn.execute_returning_one(
        "UPDATE lands SET chunks=chunks+? WHERE guild_id=? AND id=? RETURNING chunks", (blocks, guild, land_id))
    if not land:
        raise ValueError("This settlement no longer exists.")
    await record_allocation_sale(conn,guild,land_id,kind,price,blocks,at,grant_id=grant_id,cost=cost,actor=actor)
    if note is not None:
        await conn.execute("INSERT INTO block_ledger (land_id,guild_id,type,blocks,note,recorded_by,created_at) "
                           "VALUES (?,?,'owed',?,?,?,?)", (land_id, guild, blocks, note, actor, at.isoformat()))
    return land["chunks"]


async def strategic_grant(guild, land_id, actor, request_id):
    async with bank.write(guild) as conn:
        prior = await conn.fetchone(f"SELECT 1 FROM {banners.table('sales')} WHERE guild_id=? AND source='grant' AND source_id=?",
                                    (guild, request_id))
        if prior:
            raise ValueError("This strategic grant was already recorded.")
        reserve = await conn.execute_returning_one(
            "UPDATE reserve SET total_blocks=total_blocks-1 WHERE guild_id=? AND total_blocks>protected_min RETURNING total_blocks",
            (guild,))
        if not reserve:
            raise ValueError("No reserve blocks are available above the protected minimum.")
        chunks = await allocate(conn, guild, land_id, "strategic", 0, 1, actor=actor, grant_id=request_id)
        return chunks, reserve["total_blocks"]


async def accept_rotation(guild, offer_id, land_id, blocks, price, quoted_cost, actor):
    if blocks < 1:
        raise ValueError("The rotation quantity must be positive.")
    async with bank.write(guild) as conn:
        at = bank.now()
        offer = await conn.execute_returning_one(
            "UPDATE rotation_offers SET status='accepted',resolved_at=? WHERE guild_id=? AND id=? AND land_id=? "
            "AND status='pending' RETURNING id", (at.isoformat(), guild, offer_id, land_id))
        if not offer:
            raise ValueError("This rotation offer is already resolved.")
        chunks = await allocate(conn, guild, land_id, "rotation", price, blocks, cost=quoted_cost, actor=actor,
                                note=f"Rotation {at.date().isoformat()}")
        await conn.execute("UPDATE rotation_order SET position=(SELECT MAX(position)+1 FROM rotation_order WHERE guild_id=?) "
                           "WHERE guild_id=? AND land_id=?", (guild, guild, land_id))
        return chunks


async def settle_auction(guild, auction_id, actor):
    async with bank.write(guild) as conn:
        auction = await conn.execute_returning_one("UPDATE auctions SET status='ended' WHERE guild_id=? AND id=? "
                                                 "AND status='active' RETURNING blocks_offered", (guild, auction_id))
        if not auction:
            return None
        reserve = await conn.fetchone("SELECT total_blocks,protected_min FROM reserve WHERE guild_id=?" +
                                      (" FOR UPDATE" if db.IS_POSTGRES else ""), (guild,))
        available = max(0,reserve["total_blocks"]-reserve["protected_min"]) if reserve else 0
        bids = await conn.fetchall("SELECT b.land_id,b.bid_amount,l.name AS land_name,l.owner_id FROM auction_bids b "
                                   "JOIN lands l ON l.id=b.land_id WHERE b.auction_id=? AND l.guild_id=? "
                                   "ORDER BY b.bid_amount DESC,b.bid_at,b.id", (auction_id,guild))
        winners = bids[:min(available, auction["blocks_offered"])]
        if winners:
            await conn.execute("UPDATE reserve SET total_blocks=total_blocks-? WHERE guild_id=?", (len(winners),guild))
        for bid in winners:
            price = bid["bid_amount"]
            await allocate(conn,guild,bid["land_id"],"auction",price,1,actor=actor,note=f"Auction win (bid {price} ems)")
        return len(bids), winners


async def set_weekly_production(guild, quantity):
    if not 0 <= quantity <= 2_000_000_000:
        raise ValueError("Weekly production must be between 0 and 2,000,000,000 banners.")
    async with bank.write(guild) as conn:
        await ensure_week(conn, guild, bank.now(), repaired_quantity=quantity)
        await conn.execute("INSERT INTO config (guild_id,key,value) VALUES (?,'weekly_blocks',?) "
                           "ON CONFLICT (guild_id,key) DO UPDATE SET value=excluded.value", (guild, str(quantity)))


async def statement(guild, *, all_time=False):
    async with bank.write(guild) as conn:
        week = await ensure_week(conn, guild, bank.now())
        cost = await banners.unit_cost(conn, guild)
        scope = "guild_id=?" if all_time else "guild_id=? AND week_start=?"
        params = (guild,) if all_time else (guild, week)
        prod = await conn.fetchone(f"SELECT MIN(week_start) AS since,SUM(produced) AS produced "
                                   f"FROM {banners.table('production_weeks')} WHERE {scope}", params)
        rows = await conn.fetchall(
            "SELECT category,SUM(blocks) AS sold,SUM(proceeds) AS revenue,SUM(blocks*unit_cost) AS cost,"
            "SUM(released) AS released,SUM(CASE WHEN payment='bank' THEN proceeds ELSE 0 END) AS transferred "
            "FROM (SELECT s.*,CASE WHEN kind IN ('reserve','auction') THEN 'reserve' "
            "WHEN proceeds=blocks*unit_cost THEN 'full' WHEN proceeds<blocks*unit_cost THEN 'reduced' "
            f"ELSE 'other' END AS category FROM {banners.table('sales')} s WHERE {scope}) grouped GROUP BY category",
            params)
        unsold = await remaining(conn, guild)
        result = await conn.fetchone(f"SELECT COALESCE(SUM(proceeds-blocks*unit_cost),0) AS net "
                                     f"FROM {banners.table('sales')} WHERE guild_id=?", (guild,))
        treasury = await bank._account(conn, guild, "treasury", 0, "Central treasury")
        liability = unsold*cost
        return dict(week=week, since=prod["since"], produced=int(prod["produced"]), cost=cost, unsold=unsold,
                    liability=liability, position=int(result["net"])-liability, treasury=await bank._balance(conn, guild, treasury["id"]),
                    payments=await banners.payment_counts(conn,guild),
                    sales={r["category"]: {k:int(v) for k,v in r.items() if k != "category"} for r in rows})
