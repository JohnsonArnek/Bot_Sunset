# Sunset bot: Render failover

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
