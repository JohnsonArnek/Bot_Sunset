-- Run as postgres in Supabase SQL Editor after failover_schema.sql.
-- Enable pg_net and pg_cron first. This file installs code but starts no jobs.
BEGIN;

CREATE SCHEMA IF NOT EXISTS sunset_monitor;
REVOKE ALL ON SCHEMA sunset_monitor FROM PUBLIC, anon, authenticated, service_role;

CREATE TABLE IF NOT EXISTS sunset_monitor.services (
    bot_group text NOT NULL,
    instance_id text NOT NULL,
    base_url text NOT NULL CHECK (
        base_url ~ '^https://[a-z0-9][a-z0-9-]*[.]onrender[.]com$'
    ),
    priority integer NOT NULL DEFAULT 0,
    enabled boolean NOT NULL DEFAULT true,
    PRIMARY KEY (bot_group, instance_id)
);

CREATE TABLE IF NOT EXISTS sunset_monitor.state (
    bot_group text PRIMARY KEY,
    enabled boolean NOT NULL DEFAULT true,
    last_tick_at timestamptz,
    last_leader_instance text,
    wake_instance text,
    wake_started_at timestamptz,
    request_id bigint,
    request_instance text,
    request_generation bigint,
    request_started_at timestamptz,
    http_ok_at timestamptz,
    http_ok_instance text,
    http_ok_generation bigint
);

ALTER TABLE sunset_monitor.services ENABLE ROW LEVEL SECURITY;
ALTER TABLE sunset_monitor.state ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON ALL TABLES IN SCHEMA sunset_monitor FROM PUBLIC, anon, authenticated, service_role;

CREATE OR REPLACE FUNCTION sunset_monitor.tick(p_group text)
RETURNS void
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = ''
AS $$
DECLARE
    s sunset_monitor.state%ROWTYPE;
    leader public.bot_leases%ROWTYPE;
    response net._http_response%ROWTYPE;
    target sunset_monitor.services%ROWTYPE;
    candidates text[];
    chosen text;
    live_lease boolean;
    queued_id bigint;
    checked_at timestamptz := clock_timestamp();
BEGIN
    -- Serializes concurrent/manual ticks without locking bot ownership.
    SELECT * INTO s FROM sunset_monitor.state
    WHERE bot_group = p_group FOR UPDATE;
    IF NOT FOUND OR NOT s.enabled THEN
        RETURN;
    END IF;
    -- Guard against duplicate cron jobs or repeated manual calls.
    IF s.last_tick_at > checked_at - interval '50 seconds' THEN
        RETURN;
    END IF;

    -- Inspect the previous async HTTP response before queuing the next one.
    -- A Render loading page is not an application health response.
    IF s.request_id IS NOT NULL THEN
        SELECT * INTO response FROM net._http_response WHERE id = s.request_id;
        IF FOUND AND response.status_code = 200
                AND NOT coalesce(response.timed_out, false)
                AND response.error_msg IS NULL THEN
            BEGIN
                IF response.content::jsonb -> 'alive' = 'true'::jsonb THEN
                    s.http_ok_at := s.request_started_at;
                    s.http_ok_instance := s.request_instance;
                    s.http_ok_generation := s.request_generation;
                END IF;
            EXCEPTION WHEN invalid_text_representation THEN
                NULL;
            END;
        END IF;
    END IF;

    SELECT * INTO leader FROM public.bot_leases WHERE bot_group = p_group;
    live_lease := coalesce(
        leader.owner IS NOT NULL AND leader.lease_until > checked_at, false
    );
    IF live_lease THEN
        -- Never wake the other service while a valid leader is starting,
        -- reconnecting, or waiting out a token-wide rate limit.
        chosen := leader.instance_id;
        s.last_leader_instance := chosen;
        s.wake_instance := NULL;
        s.wake_started_at := NULL;
    ELSE
        SELECT array_agg(v.instance_id ORDER BY v.priority, v.instance_id)
        INTO candidates
        FROM sunset_monitor.services AS v
        WHERE v.bot_group = p_group AND v.enabled AND NOT EXISTS (
            SELECT 1 FROM public.bot_instance_cooldowns AS c
            WHERE c.bot_group = p_group AND c.instance_id = v.instance_id
                AND c.retry_at > checked_at
        );
        IF coalesce(array_length(candidates, 1), 0) > 0 THEN
            IF s.wake_instance = ANY(candidates)
                    AND s.wake_started_at > checked_at - interval '3 minutes' THEN
                -- Give one cold service time to boot; don't wake both in turn
                -- merely because the first has not connected to Discord yet.
                chosen := s.wake_instance;
            ELSE
                SELECT candidate INTO chosen
                FROM unnest(candidates) AS candidate
                WHERE candidate IS DISTINCT FROM coalesce(
                    s.wake_instance, s.last_leader_instance
                )
                LIMIT 1;
                chosen := coalesce(chosen, candidates[1]);
                s.wake_instance := chosen;
                s.wake_started_at := checked_at;
            END IF;
        END IF;
    END IF;

    SELECT * INTO target FROM sunset_monitor.services
    WHERE bot_group = p_group AND instance_id = chosen AND enabled;
    IF FOUND THEN
        queued_id := net.http_get(
            url := target.base_url || '/health',
            timeout_milliseconds := 20000
        );
    END IF;

    UPDATE sunset_monitor.state
    SET last_tick_at = checked_at,
        last_leader_instance = s.last_leader_instance,
        wake_instance = s.wake_instance, wake_started_at = s.wake_started_at,
        request_id = queued_id, request_instance = target.instance_id,
        request_generation = CASE WHEN live_lease THEN leader.generation END,
        request_started_at = CASE WHEN queued_id IS NOT NULL THEN checked_at END,
        http_ok_at = s.http_ok_at, http_ok_instance = s.http_ok_instance,
        http_ok_generation = s.http_ok_generation
    WHERE bot_group = p_group;
END;
$$;

CREATE OR REPLACE FUNCTION sunset_monitor.tick_all()
RETURNS void
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = ''
AS $$
DECLARE
    group_name text;
BEGIN
    FOR group_name IN SELECT bot_group FROM sunset_monitor.state WHERE enabled LOOP
        PERFORM sunset_monitor.tick(group_name);
    END LOOP;
END;
$$;

-- The only exposed RPC is read-only and returns a boolean. It cannot wake,
-- elect, release or reconfigure a bot. Only the Edge Function's server key
-- can call it, not an anonymous Supabase Data API client.
CREATE OR REPLACE FUNCTION public.sunset_bot_health(p_group text DEFAULT 'sunset')
RETURNS boolean
LANGUAGE sql
SECURITY DEFINER
SET search_path = ''
AS $$
    SELECT EXISTS (
        SELECT 1
        FROM public.bot_leases AS l
        JOIN sunset_monitor.state AS s ON s.bot_group = l.bot_group
        JOIN sunset_monitor.services AS v
          ON v.bot_group = l.bot_group AND v.instance_id = l.instance_id
        WHERE l.bot_group = p_group AND s.enabled AND v.enabled
          AND l.owner IS NOT NULL AND l.lease_until > clock_timestamp()
          AND l.healthy AND l.last_heartbeat > clock_timestamp() - interval '45 seconds'
          AND s.last_tick_at > clock_timestamp() - interval '150 seconds'
          AND s.http_ok_at > clock_timestamp() - interval '150 seconds'
          AND s.http_ok_instance = l.instance_id AND s.http_ok_generation = l.generation
    );
$$;

REVOKE ALL ON ALL FUNCTIONS IN SCHEMA sunset_monitor FROM PUBLIC, anon, authenticated, service_role;
REVOKE ALL ON FUNCTION public.sunset_bot_health(text) FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.sunset_bot_health(text) TO service_role;
NOTIFY pgrst, 'reload schema';
COMMIT;
