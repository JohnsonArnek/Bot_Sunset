# Cold standby with a hosted uptime monitor

Use this setup when the pinger is a hosted service such as UptimeBot or
UptimeRobot, rather than a machine where you can run Python.

Supabase Cron checks the database once a minute and makes **one** Render HTTP
request: to the current leader, or to a candidate when no valid lease exists.
The other service receives no routine requests and can sleep. Your uptime
monitor checks a small Supabase Edge Function, which only reads database status.
It cannot wake either Render service.

No Render API key, monitoring-service API key, third always-on service, or new
monitoring-provider account is needed. Supabase provides its scheduler and
asynchronous HTTP extension. [Cron documentation](https://supabase.com/docs/guides/cron)
[pg_net documentation](https://supabase.com/docs/guides/database/extensions/pg_net)

## 1. Prepare the Render services

Follow the [main deployment instructions](../README.md#deploy-the-pair). Deploy
the updated primary first, then create the backup in another region with
`BOT_INSTANCE_ID=render-b`. Both use the same Discord token, Supabase DB,
`BOT_GROUP=sunset`, and `FAILOVER_ENABLED=true`.

Keep the existing primary keep-alive monitor until the scheduler is working.
Do not add a recurring monitor for the backup. Creating/deploying it wakes it
temporarily; that is expected. Render's platform health check remains `/health`.

## 2. Install the scheduler code in Supabase

In Supabase SQL Editor, run as `postgres`:

```sql
CREATE EXTENSION IF NOT EXISTS pg_cron;
CREATE EXTENSION IF NOT EXISTS pg_net;
```

The bot creates the shared lease tables on its first failover-enabled startup.
If it has not started yet, run [`failover_schema.sql`](../failover_schema.sql).
Then run [`cold_standby.sql`](cold_standby.sql) in the same project.
That script creates a private monitoring schema and functions, but starts no job.
It is safe to run again.

Replace the two example URLs below with your actual Render URLs, without a
trailing slash. Run this only after both services exist:

```sql
INSERT INTO sunset_monitor.services (bot_group, instance_id, base_url, priority)
VALUES
  ('sunset', 'render-a', 'https://replace-primary.onrender.com', 0),
  ('sunset', 'render-b', 'https://replace-backup.onrender.com', 1)
ON CONFLICT (bot_group, instance_id) DO UPDATE
SET base_url = EXCLUDED.base_url, priority = EXCLUDED.priority, enabled = true;

INSERT INTO sunset_monitor.state (bot_group) VALUES ('sunset')
ON CONFLICT DO NOTHING;

SELECT cron.schedule(
  'sunset-cold-standby',
  '* * * * *',
  'SELECT sunset_monitor.tick_all()'
);
```

This is the only wake/keep-alive job. Rerunning `cron.schedule` with the same
name updates that job. Do not also run `watchdog.py`, ping both bot URLs, or add
another scheduled wake-up job.

If a leader is already active, its identity wins over the configured priorities.
After an expired lease, the scheduler first tries a service other than the last
leader, skipping services in cooldown. It gives that candidate three minutes
to boot before trying another. A returning primary does not displace a healthy
backup. The existing lease mechanism still decides ownership.

## 3. Deploy the monitor endpoint

From this repository, with Supabase CLI signed in:

```text
supabase functions deploy bot-health --project-ref YOUR_PROJECT_REF
```

The checked-in `supabase/config.toml` makes only `bot-health` public. It accepts
GET/HEAD and returns only `{"healthy":true}` (HTTP 200) or `{"healthy":false}`
(HTTP 503). It has no action endpoints and exposes no URLs, bot tokens or DB
credentials. Its database RPC is restricted to the server role.

Supabase injects the project's API URL and server keys into the function; you
do not paste them into UptimeBot. The code prefers the `default` secret key and
supports the older injected service-role key. If using a custom `BOT_GROUP`,
set that same value in this function's Supabase secrets.
[Supabase environment variables](https://supabase.com/docs/guides/functions/secrets)

Your monitoring URL is:

```text
https://YOUR_PROJECT_REF.supabase.co/functions/v1/bot-health
```

## 4. Move the uptime monitor to the Supabase URL

Wait two scheduler runs and confirm the endpoint returns HTTP 200. In your
uptime service, replace the old primary Render URL with this Supabase URL and
enable email alerts. It needs no authentication header. Use an HTTP GET check
and regard only HTTP 200 as healthy.

Remove/disable any remaining monitors pointing at **either Render bot URL**.
Do not use `/ready` on the standby as an uptime monitor; an inactive standby is
expected to return 503.

Now let the backup idle without opening its URL. Check Render's dashboard to
confirm it has spun down. The dashboard view itself is fine; HTTP requests to
the service are what wake it.

## What the monitor verifies

HTTP 200 requires a live, healthy leader lease with a recent heartbeat, a recent
successful application HTTP response from that same leadership generation, and
a recent scheduler run. A dead scheduler, unreachable Supabase, expired lease
or failed HTTP probes produces 503. Checking this endpoint never sends a
Render request.

During a normal cold takeover, the endpoint will temporarily report 503.
Configure a notification delay/grace period of roughly five minutes if your
monitor supports it; otherwise brief failover alerts are expected. Detection
and delivery timing also depend on the monitor's interval. Email delivery is
independent of the bot's Discord access.

## Usage and recovery time

Sleeping services do not consume Render free instance hours. Render decides
when to sleep them: its documented idle period is 15 minutes and wake-up is
about a minute. A deployment, accidental direct monitor, or takeover still
consumes some standby hours. This reduces ongoing usage; it does not make all
standby activity free. Supabase DB/Edge usage also remains subject to your plan.
[Render free service documentation](https://render.com/docs/free)

Allow several minutes for lease expiry, the next scheduler tick, Render waking,
Discord login and the next health confirmation. Existing Supabase/Discord
outages still affect both services. No automatic failback is performed.

## Inspect or stop the scheduler

```sql
SELECT * FROM sunset_monitor.state;
SELECT * FROM cron.job_run_details
WHERE jobid = (SELECT jobid FROM cron.job WHERE jobname = 'sunset-cold-standby')
ORDER BY start_time DESC LIMIT 10;
```

To disable only this group:

```sql
UPDATE sunset_monitor.state SET enabled = false WHERE bot_group = 'sunset';
```

To stop the whole job:

```sql
SELECT cron.unschedule('sunset-cold-standby');
```

If you stop it, restore a keep-alive monitor on the current leader if you want
that service to remain awake. Supabase retains cron execution history; monitor
that history's storage and apply your project's retention policy as appropriate.
