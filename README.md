# Sunset bot

## Strategic war reserves (Discord administrators)

Start with `/war stock-set location:North Keep hay:7 diamonds:4 blocks logs:3`. Each supplied
material replaces its own balance. Omitted materials keep their previous
balances (zero in a new depot). Location names ignore capitalization and repeated spaces. Use
`/war stock-list` to see balances, or `/war stock-remove` to retire a depot.
Use `/war stock-list location:North Keep` for one settlement's reserves. The
ledger leads with double chests, single chests, stacks and loose remainders;
diamonds and iron use **containers of blocks**. Exact individual-item totals
remain visible in parentheses. For example, 12,000 hay reads **3 double chests +
25 stacks + 32 loose hay**; 31,104 diamonds reads **1 double chest of blocks**.
The full ledger shows three depots per page, plus the server totals.
Only Discord Administrators can use these commands, including reads and
estimates. Replies start private, and each server has its own stock and rules.
Use **Share estimate in this channel** on a conquest, logistics, or targeted
defence estimate to publish its calculation and assumptions. Only the requesting
administrator can share it, within five minutes. Shared posts omit depot names,
on-hand balances, shortages and the private sufficiency verdict. Logistics shares
the calculated space, loads and timing, excluding its inventory manifest.
Sharing preserves the original estimate even if rules or stock change afterwards.

For `stock-set`, `stock-add` and `stock-subtract`, enter hay, diamonds, iron, arrows or logs in
whichever units you counted. A bare number means individual items. A stack
or chest means **full of that one resource**, using the server's current
packing rules. Combine quantities with `+` for a partial chest or stack.

| What you counted at North Keep | Command | Balance after this step (default capacities) |
| --- | --- | --- |
| Initial count: a double chest of hay and two stacks of diamonds | `/war stock-set location:North Keep hay:1 double chest diamonds:2 stacks` | 3,456 hay, 128 diamonds |
| Delivery: three stacks of hay plus five loose bales | `/war stock-add location:North Keep hay:3 stacks + 5` | 3,653 hay, 128 diamonds |
| Used one chest of hay and ten diamonds | `/war stock-subtract location:North Keep hay:1 chest diamonds:10` | 1,925 hay, 118 diamonds |

Use `stock-set` for a recount (specify at least one material; zero clears it).
Add and subtract take any combination of materials and require an existing depot.
If any amount is unavailable or would exceed the depot limit, no balance
changes. A withdrawal to zero keeps the depot listed; `stock-remove`
deletes the depot. Balances are saved as individual items, so changing stack
or chest sizes later does not change stock already recorded.

**Iron and defence.** Iron is recorded in ingots. One block is always nine
ingots. For example, `/war stock-add location:North Keep iron:2 blocks + 5 ingots arrows:1 stack`
adds 23 ingots and, with the default stack size, 64 arrows. For iron, a
bare stack or chest counts ingots; use `1 stack of blocks` or `1 chest of blocks`
for compacted stock. `/war defence location:North Keep` shows transferable
defence: 23 ingots make two whole blocks for **200 defence**, leaving five
ingots. Add `points:250` to see that a third block is needed (four more ingots).
Omit the location to pool iron across depots. The check never deducts iron.
Set the rate with `/war config defence_per_block:125`; the default is 100.

**Diamond blocks** also count as nine individual diamonds. All three stock
commands accept `diamonds:2 blocks + 3`, `diamonds:1 stack of blocks`, or
`diamonds:1 chest of diamond blocks`. A bare stack or chest still means loose
diamonds. Block inputs are converted to individual diamonds for accounting;
diamond blocks do not transfer defence. Freight compacts diamonds and iron into
whole blocks and counts any loose remainder separately, regardless of input units.

**Logs** supply the sticks for diamond axes. The defaults are two sticks per
axe, four sticks per two planks, and four planks per log. Whole crafting batches
are pooled across the group, then rounded up to whole logs. Ten axes need 20
sticks, consuming 10 planks, so bring **three logs** (two planks remain unused).
Use `logs:2 stacks + 3` with any stock command. Configure the recipe with
`sticks_per_axe`, `sticks_per_batch`, `planks_per_batch`, and `planks_per_log`
under `/war config`. Existing depots start with zero logs until counted.

**Arrows** appear in depot balances for accounting only. They do not affect
defence, conquest, transport, or storage estimates. Iron does not change the
time or materials needed to conquer a settlement.

`/war conquerable hp:2400 players:5` compares the required supplies with all
recorded locations. Add `location:North Keep` to check only that depot. With
the defaults, five attackers land 600 hits in 120 volleys. From the first hit
to the last takes **59.5 seconds**. Each attacker needs two axes and four
loaves, consuming **30 diamonds, 7 hay and 3 logs**. The shipment carries **four
diamond blocks (36 diamonds), 7 hay and 3 logs** and leaves six spare diamonds
after crafting at the objective. The stock check covers this whole-block shipment,
so having only 30 diamonds reports a shortage of six. Planning never deducts stock.

Assumptions appear first in each estimate. Resource quantities, including depot
balances, use full stacks, single/double chests and loose remainders; iron and
diamond containers hold blocks. Times use days, hours,
minutes and seconds as appropriate (for example, `1d 2h 15m`). Conquest shows
carrier loads and storage for hay, logs and diamond blocks. Axes and bread are
crafted at the destination and are not freight options.

**Time from depot to objective.** Add `travel_minutes` (one way) to either
`/war conquerable` or `/war logistics`. For example,
`/war conquerable hp:2400 players:5 location:North Keep mode:donkey travel_minutes:12`
shows 10 min loading + 12 min travel + 10 min unloading = **32 min until
supplies are ready**. With 59.5 seconds of combat, that's **32 min 59.5 s
from loading to conquest**, assuming the attack starts once all supplies arrive.
Omitting travel time skips route timing; entering zero still includes handling.

Loading and unloading default to **10 minutes each** per convoy trip. Set
them independently with `/war config loading_minutes:5 unloading_minutes:3`;
zero and fractional minutes are supported. Trips are **calculated**, not entered.
Choose `mode:donkey` (default), `mode:chest_boat`, or `mode:chest_minecart`.
Donkey/boat modes default to one carrier; set `carriers:2` for two travelling
together. Trips = cargo slots divided by total convoy slots, rounded up. Each
outbound trip loads and unloads once, with return travel between trips only.
Enter the one-way travel time for your chosen transport mode.

Chest minecart mode assumes a prepared rail route and enough carts to send all
cargo in **one dispatch**, with no returning carts needed. The bot calculates
the cart count (27 slots each by default); omit `carriers` in this mode. Loading
and unloading apply once to the whole dispatch. These are planning assumptions,
not a simulation of rail throughput or individual cart launch times.

For 55 cargo slots, one donkey needs four trips; one chest boat needs three;
two donkeys need two; rail needs three chest minecarts in one dispatch. At 12
minutes one way and default handling times, those take **2h 44m**, **2h**, **1h
16m**, and **32m**, respectively. With pooled stock, timing starts after supplies
are assembled at departure; collection from other depots is excluded.

Use `/war logistics location:North Keep` for the space and loads needed to
move that depot's hay, logs, diamond blocks and iron blocks (plus loose remainders). Omit the location for the
minimum if all depots are consolidated. Each slot holds up to **64 of one
item**: 960 hay fills 15 slots, but 960 hay **and one diamond** needs 16.
That's **two trips with one donkey or one trip with a chest boat**, and fits in **one single
chest** (16 of its 27 slots). With no cargo, all estimates are zero.

| Carrier or storage | Default slots | Hay if filled with hay only |
| --- | ---: | ---: |
| Donkey | 15 | 960 |
| Boat with chest | 27 | 1,728 |
| Minecart with chest | 27 per cart | 1,728 per cart |
| Single chest | 27 | 1,728 |
| Double chest | 54 | 3,456 |

One load means one carrier making one trip. `/war conquerable` sizes the
shipment of **hay, logs and whole diamond blocks**, rounding diamonds up to a
whole block. The five-attacker example occupies three slots and fits on one
donkey. `/war logistics` packs all recorded stock exactly; full blocks and loose
diamonds/ingots occupy separate slots, with no rounding up of stock. To change
capacities, use `/war config` with `stack_size` (1–64), `donkey_slots`,
`chest_boat_slots`, `chest_minecart_slots`, `chest_slots`, or `double_chest_slots`.

Use `/war config` to see the current rules or, for example,
`/war config rehit_seconds:0.75 axe_durability:80` to update just those values.
Every value below is configurable per server:

| Axe and attack | Default | Food | Default |
| --- | ---: | --- | ---: |
| Diamonds per axe | 3 | Loaves per hay bale | 3 |
| Durability (hits per axe) | 60 | Hunger restored per loaf | 3 |
| Damage per hit | 4 HP | Hits per hunger charge | 5 |
| Delay between hits | 0.5 s | Hunger lost per charge | 0.5 |

The estimate splits hits as evenly as possible among simultaneous attackers.
The first volley lands immediately. Each attacker starts with fresh axes and
is provisioned to replace the hunger lost after each complete group of hits;
partial groups incur no charge. Axes and loaves round up **per attacker**.
Loaves from a bale can be distributed across the group. Combat time excludes
travel and cargo handling, which are shown separately when requested. Neither
estimate includes crafting, missed hits, eating time, or existing equipment or food.

Storage is created during normal bot startup on both SQLite and PostgreSQL.
On PostgreSQL the bot owns two RLS-enabled tables in the private `sunset_war`
schema; no public Data API access or new environment variables are required.

## Render failover

Two Render web services can share the same Discord token and Supabase database.
Set `FAILOVER_ENABLED=true` on **both**. Only the elected leader connects to
Discord, loads the cogs and runs scheduled jobs. A standby serves HTTP and waits.
There is no automatic failback: a healthy backup stays leader.

## Deploy the pair

1. Deploy this version to the main service first. Do not start a second copy
   while the main service is still running the old, uncoordinated code.
2. Use Python 3.11 or newer. Build: `pip install -r requirements.txt`.
   Start: `python bot.py`. Render health check path: `/health`.
3. In both services set the same `DISCORD_TOKEN`, PostgreSQL `DATABASE_URL`,
   `BOT_GROUP=sunset` and `FAILOVER_ENABLED=true`. SQLite cannot coordinate
   services. Use the existing Supabase database so the bot keeps its data.
4. Give each service a stable, unique ID: `BOT_INSTANCE_ID=render-a` on the
   primary and `BOT_INSTANCE_ID=render-b` on the backup. Each process also gets
   an internal unique ID, so overlapping deploys cannot share ownership.
5. Create the backup in a **different Render region**. It may be in your second
   account/workspace; the code does not depend on which account owns it.
6. Check `/status` on both once during setup. Expect one `leader` and one
   `standby`. The leader's `/ready` becomes HTTP 200 after Discord is connected
   and a read-only REST probe succeeds. Standby `/ready` returns 503 by design.

Copy the **Session pooler** connection string from Supabase's Connect dialog
for IPv4 connections; include `sslmode=require` (or configure `verify-full` with
the Supabase CA). This code disables asyncpg's statement cache for compatibility
with transaction pooling too. It uses transaction-level locks, not session
advisory locks. [Supabase connection documentation](https://supabase.com/docs/guides/database/connecting-to-postgres)

The server DB user must own/create the control tables and bypass their RLS
(the usual Supabase `postgres` server connection does). The code creates only
`bot_leases` and `bot_instance_cooldowns` for coordination and enables RLS without
public policies on them. Never put the DB password or bot token in the repository.
The environment template is [`.env.example`](.env.example); it is not auto-loaded.

## Cold standby with UptimeBot or another hosted monitor

Use the [Supabase cold-standby setup](supabase/COLD_STANDBY.md) to avoid keeping
the backup awake. Supabase Cron pings only the leader and wakes a candidate
when needed. Point the uptime monitor at the included read-only Supabase health
endpoint; monitoring it never wakes either Render bot. No third always-on
service or Render API key is required.

The scheduler/health endpoint must be installed separately using that guide.
They are not activated by deploying the Python bot. Do not also run the Python
watchdog below or retain direct uptime monitors on either Render service.

## Other hosting options

**Warm:** keep pinging `/health` on both services from your existing pinger.
The backup stays awake but disconnected from Discord. After a crash it can
acquire the lease within about 90–100 seconds. Detected failures can release
sooner, after shutdown. Both services consume running hours.

**Cold with a Python host:** run `python watchdog.py` on an independent machine
with the same dependencies, `BOT_GROUP`, database access and this mapping:

```text
BOT_SERVICES={"render-a":"https://PRIMARY.onrender.com","render-b":"https://BACKUP.onrender.com"}
WATCHDOG_DATABASE_URL=your-Supabase-Postgres-connection-string
```

Remove unconditional pings to both bot URLs. The watchdog reads Supabase every
minute, pings only the current lease holder, and wakes an eligible service when
there is no valid lease. It alternates wake attempts if a service cannot start.
The database election remains the authority; a pinger cannot force two leaders.
The same logic can be integrated into an existing Python pinger by constructing
`watchdog.Watchdog(pool, services, group)` and calling `await watchdog.tick(session)`.
Give its aiohttp session a finite timeout, such as 20 seconds.

Render says free services sleep after 15 minutes without inbound traffic, take
about a minute to wake on HTTP, and use no free instance hours while asleep.
There is **no application sleep switch** here: Render controls spin-down.
Visiting or monitoring the backup URL wakes/keeps it awake. A cold failover can
take several minutes (lease expiry, watchdog interval, wake-up, Discord login).
Verify actual sleep behavior in the Render dashboard before relying on the
hour savings. [Render free service documentation](https://render.com/docs/free)

There is a 750-hour allowance per workspace per month. Two continuously running
services in one workspace exceed it; cold standby still has startup and overlap
hours, so it is not a zero-overhead guarantee. Region affects the shared outbound
IP ranges; a second account alone does not provide an independent IP range.
[Render outbound IP documentation](https://render.com/docs/outbound-ip-addresses)

### Notifications when neither service is healthy

On the watchdog host, optionally set:

- `HEALTHCHECKS_URL`: a Healthchecks ping URL, called only after a healthy leader
  and its HTTP server are verified. Configure a one-minute period and a generous
  grace period (for example five minutes) in Healthchecks and enable email alerts.
  This also detects a dead watchdog.
- `ALERT_WEBHOOK_URL`: an independent notification endpoint accepting JSON
  `{"text":"..."}`. The watchdog sends one outage notification after five minutes
  without a verified healthy leader, and one recovery notification. Use a
  non-Discord delivery service. No alerts are sent unless you configure a URL.

`WATCHDOG_DATABASE_URL` may use a dedicated read-only DB role granted SELECT on
the two control tables. Because RLS is enabled, that role also needs SELECT RLS
policies; a table owner connection works for initial setup. Do not grant the
watchdog INSERT/UPDATE/DELETE access.

## Failure behavior and limits

- A 90-second Postgres lease renews every 20 seconds. Acquisition, renewal and
  release check process identity and generation, using the database clock.
  An expired lease cannot be renewed.
- Every application SQL operation checks ownership in a transaction and holds a
  shared row lock until that operation finishes, excluding takeover during it.
  A separate watchdog thread kills a stuck process before its local lease safety
  deadline. If Supabase is unreachable, the leader stops; safety takes priority
  over availability.
- IP-block HTML/Cloudflare 429 responses during login, command sync or command
  errors request failover. A read-only Discord REST probe every minute also
  catches failures swallowed by background tasks. The affected instance has a
  persistent cooldown of at least 30 minutes, respecting a longer Retry-After.
  A restart cannot clear that cooldown.
- Ordinary JSON 429 responses defer probes and retain Discord's retry behavior.
  Login rate limits retain the lease while waiting. They do not trigger region
  hopping to bypass token limits.
- Three failed REST probes, a 90-second gateway outage, or failure to become
  ready in 120 seconds causes shutdown and a cooldown. Cleanup closes Discord,
  unloads cogs and cancels outstanding tasks before releasing the lease.
  The process exits with code 1 so Render restarts it with fresh Discord state;
  during cooldown it waits as standby.
- `/health` is liveness, not bot readiness. `/status` reports role, lease,
  DB connectivity and last successful Discord REST probe; `/ready` returns 503
  unless this instance can serve. These endpoints expose no credentials.
- This is failover, not exactly-once processing. An interaction or multi-step
  scheduled job interrupted by a crash can be partly applied. Discord messages
  already in flight cannot be revoked; review interrupted distributions/auctions
  before retrying them. Pending rotation offers are not automatically resumed.
  Supabase and Discord remain shared dependencies.

To test deployment, stop the current leader in Render and verify the other
service takes over and responds to a command. Restart the old leader: it should
remain standby. For cold mode, let the standby actually sleep, then repeat.
Use simulated responses in automated tests; do not deliberately provoke an IP ban.

## Development checks

```text
python -m unittest discover -s tests -v
node --test tests/test_health_endpoint.mjs
```

Postgres integration tests require `TEST_DATABASE_URL` pointing to a **disposable
test database**, not Supabase production. They exercise concurrent election,
expiry, cooldown, stale-owner fencing and lock ordering. Without that variable,
those tests are explicitly skipped.
The Supabase scheduler SQL tests use real Postgres with a stubbed `pg_net`
queue/response adapter; they do not make external requests. The Edge handler is
tested locally with mocked Supabase responses. Hosted scheduling, deployment
and Render sleep/wake behavior still require the deployment smoke test.
Single-instance development still works with `FAILOVER_ENABLED=false` (default)
and SQLite.
