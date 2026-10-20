-- Disposable test databases only. Models pg_net's asynchronous queue boundary
-- without sending network traffic or needing Supabase's extension binary.
CREATE SCHEMA IF NOT EXISTS net;
CREATE TABLE IF NOT EXISTS net.sunset_test_requests (
    id bigserial PRIMARY KEY,
    url text NOT NULL,
    timeout_milliseconds integer NOT NULL
);
CREATE TABLE IF NOT EXISTS net._http_response (
    id bigint,
    status_code integer,
    content_type text,
    headers jsonb,
    content text,
    timed_out boolean,
    error_msg text,
    created timestamptz NOT NULL DEFAULT now()
);
CREATE OR REPLACE FUNCTION net.http_get(
    url text, params jsonb DEFAULT '{}'::jsonb,
    headers jsonb DEFAULT '{}'::jsonb, timeout_milliseconds integer DEFAULT 2000
) RETURNS bigint LANGUAGE sql AS $$
    INSERT INTO net.sunset_test_requests (url, timeout_milliseconds)
    VALUES (url, timeout_milliseconds) RETURNING id;
$$;
