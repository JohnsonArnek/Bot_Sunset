-- Applied automatically before election; safe to re-run.
CREATE TABLE IF NOT EXISTS bot_leases (
    bot_group text PRIMARY KEY,
    owner text,
    instance_id text,
    generation bigint NOT NULL DEFAULT 0,
    lease_until timestamptz NOT NULL DEFAULT '-infinity',
    healthy boolean NOT NULL DEFAULT false,
    last_heartbeat timestamptz
);

CREATE TABLE IF NOT EXISTS bot_instance_cooldowns (
    bot_group text NOT NULL,
    instance_id text NOT NULL,
    retry_at timestamptz NOT NULL,
    reason text NOT NULL,
    PRIMARY KEY (bot_group, instance_id)
);

-- These control tables must not be writable through Supabase's public Data API.
-- The server connection must use the table owner (or a suitable BYPASSRLS role).
ALTER TABLE bot_leases ENABLE ROW LEVEL SECURITY;
ALTER TABLE bot_instance_cooldowns ENABLE ROW LEVEL SECURITY;
