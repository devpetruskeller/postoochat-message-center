const json = (value, status = 200) => new Response(JSON.stringify(value), {
  status,
  headers: { "content-type": "application/json; charset=utf-8", "cache-control": "no-store" },
});

const emptyCatalog = () => ({ taxonomy: {}, messages: [] });

async function loadCatalog(db) {
  const row = await db.prepare("SELECT payload FROM message_center_state WHERE state_key = 'catalog'").first();
  if (!row) return emptyCatalog();
  try { return JSON.parse(row.payload); } catch { return emptyCatalog(); }
}

async function saveCatalog(db, catalog) {
  await db.prepare(`INSERT INTO message_center_state (state_key, payload, updated_at)
    VALUES ('catalog', ?, strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
    ON CONFLICT(state_key) DO UPDATE SET payload = excluded.payload, updated_at = excluded.updated_at`)
    .bind(JSON.stringify(catalog)).run();
}

function authorized(request, env) {
  const expected = env.MESSAGE_CENTER_ADMIN_TOKEN;
  return Boolean(expected) && request.headers.get("authorization") === `Bearer ${expected}`;
}

function exportPayload(catalog) {
  return { generated_at: new Date().toISOString(), messages: catalog.messages || [] };
}

export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    if (url.pathname === "/health") return json({ ok: true, service: "postoochat-message-center" });
    if (!authorized(request, env)) return json({ error: "unauthorized" }, 401);

    if (url.pathname === "/api/catalog" && request.method === "GET") return json(await loadCatalog(env.DB));
    if (url.pathname === "/api/export" && request.method === "GET") return json(exportPayload(await loadCatalog(env.DB)));

    if (url.pathname === "/api/catalog" && request.method === "PUT") {
      const catalog = await request.json();
      if (!catalog || !Array.isArray(catalog.messages) || typeof catalog.taxonomy !== "object") {
        return json({ error: "invalid_catalog" }, 422);
      }
      await saveCatalog(env.DB, catalog);
      return json({ ok: true, exported_count: catalog.messages.length });
    }
    return json({ error: "not_found" }, 404);
  },
};
