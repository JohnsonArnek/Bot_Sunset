// No Render requests here. Uptime monitors must never wake the standby.
export function selectServerKey(secretKeys, legacyKey) {
  try {
    const key = JSON.parse(secretKeys ?? "{}").default;
    if (typeof key === "string" && key.startsWith("sb_secret_")) return key;
  } catch { /* Older projects may only have the injected legacy key. */ }
  return legacyKey;
}

export function createHealthHandler({ supabaseUrl, apiKey, group = "sunset" }, fetcher = fetch) {
  return async function handle(request) {
    const headers = { "Content-Type": "application/json", "Cache-Control": "no-store" };
    if (!["GET", "HEAD"].includes(request.method)) {
      return new Response(null, { status: 405, headers: { ...headers, Allow: "GET, HEAD" } });
    }
    let healthy = false;
    if (supabaseUrl && apiKey) {
      try {
        const response = await fetcher(`${supabaseUrl}/rest/v1/rpc/sunset_bot_health`, {
          method: "POST",
          headers: {
            apikey: apiKey,
            // New secret keys are not JWTs and belong only in apikey.
            ...(apiKey.startsWith("sb_secret_") ? {} : { Authorization: `Bearer ${apiKey}` }),
            "Content-Type": "application/json",
          },
          body: JSON.stringify({ p_group: group }),
          signal: AbortSignal.timeout(4000),
          redirect: "error",
        });
        healthy = response.ok && await response.json() === true;
      } catch {
        // Missing DB, stopped project, timeout, invalid payload: fail closed.
        // Never return upstream responses or credentials to the public monitor.
      }
    }
    return new Response(request.method === "HEAD" ? null : JSON.stringify({ healthy }), {
      status: healthy ? 200 : 503,
      headers,
    });
  };
}
