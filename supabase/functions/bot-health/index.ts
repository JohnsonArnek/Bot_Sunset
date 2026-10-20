import { createHealthHandler, selectServerKey } from "./handler.mjs";

Deno.serve(createHealthHandler({
  supabaseUrl: Deno.env.get("SUPABASE_URL"),
  apiKey: selectServerKey(
    Deno.env.get("SUPABASE_SECRET_KEYS"),
    Deno.env.get("SUPABASE_SERVICE_ROLE_KEY"),
  ),
  group: Deno.env.get("BOT_GROUP") || "sunset",
}));
