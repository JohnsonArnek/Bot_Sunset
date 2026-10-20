import assert from "node:assert/strict";
import { test } from "node:test";
import { createHealthHandler, selectServerKey } from "../supabase/functions/bot-health/handler.mjs";

const config = { supabaseUrl: "https://test.supabase.co", apiKey: "sb_secret_test" };

test("monitor reads only the configured group's database health, never a bot URL", async () => {
  const calls = [];
  const handler = createHealthHandler(config, async (...args) => {
    calls.push(args);
    return Response.json(true);
  });
  const result = await handler(new Request("https://test/bot-health?target=https://backup.onrender.com"));
  assert.equal(result.status, 200);
  assert.deepEqual(await result.json(), { healthy: true });
  assert.equal(result.headers.get("Cache-Control"), "no-store");
  assert.equal(calls.length, 1);
  assert.equal(calls[0][0], "https://test.supabase.co/rest/v1/rpc/sunset_bot_health");
  assert.deepEqual(JSON.parse(calls[0][1].body), { p_group: "sunset" });
  assert.equal(calls[0][1].headers.apikey, "sb_secret_test");
  assert.equal(calls[0][1].headers.Authorization, undefined);
});

test("legacy projects can use the injected service role JWT", async () => {
  const handler = createHealthHandler({ ...config, apiKey: "legacy-jwt" }, async (_, options) => {
    assert.equal(options.headers.Authorization, "Bearer legacy-jwt");
    return Response.json(true);
  });
  assert.equal((await handler(new Request("https://test"))).status, 200);
  assert.equal(selectServerKey('{"default":"sb_secret_new"}', "old"), "sb_secret_new");
  assert.equal(selectServerKey(undefined, "old"), "old");
  assert.equal(selectServerKey("invalid", "old"), "old");
});

test("unhealthy, unexpected and upstream error responses all fail closed", async () => {
  for (const value of [false, null, "true", 1, { healthy: true }]) {
    const handler = createHealthHandler(config, async () => Response.json(value));
    assert.equal((await handler(new Request("https://test"))).status, 503);
  }
  const handler = createHealthHandler(config, async () => Response.json(true, { status: 500 }));
  assert.equal((await handler(new Request("https://test"))).status, 503);
});

test("timeouts and malformed JSON do not expose upstream details or secrets", async () => {
  for (const fetcher of [
    async () => { throw new Error("private database credentials"); },
    async () => new Response("private upstream data"),
  ]) {
    const response = await createHealthHandler(config, fetcher)(new Request("https://test"));
    assert.equal(response.status, 503);
    assert.deepEqual(await response.json(), { healthy: false });
  }
});

test("missing configuration makes no network request", async () => {
  const response = await createHealthHandler({}, () => assert.fail("unexpected network request"))(
    new Request("https://test"),
  );
  assert.equal(response.status, 503);
});

test("HEAD works for monitors and mutations are rejected before database access", async () => {
  const response = await createHealthHandler(config, async () => Response.json(true))(
    new Request("https://test", { method: "HEAD" }),
  );
  assert.equal(response.status, 200);
  assert.equal(await response.text(), "");
  for (const method of ["POST", "PUT", "DELETE"]) {
    const response = await createHealthHandler(config, () => assert.fail("unexpected network request"))(
      new Request("https://test", { method }),
    );
    assert.equal(response.status, 405);
  }
});
