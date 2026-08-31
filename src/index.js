const json = (value, status = 200) => new Response(JSON.stringify(value), {
  status,
  headers: { "content-type": "application/json; charset=utf-8", "cache-control": "no-store" },
});

const emptyCatalog = () => ({ taxonomy: {}, messages: [] });

async function loadCatalog(db) {
  const chunks = await db.prepare("SELECT payload FROM message_center_catalog_chunks ORDER BY chunk_index").all();
  if (chunks.results?.length) {
    try { return JSON.parse(chunks.results.map((chunk) => chunk.payload).join("")); } catch { return emptyCatalog(); }
  }
  const row = await db.prepare("SELECT payload FROM message_center_state WHERE state_key = 'catalog'").first();
  if (!row) return emptyCatalog();
  try { return JSON.parse(row.payload); } catch { return emptyCatalog(); }
}

async function saveCatalog(db, catalog) {
  const payload = JSON.stringify(catalog);
  const size = 24000;
  const statements = [db.prepare("DELETE FROM message_center_catalog_chunks")];
  for (let offset = 0, index = 0; offset < payload.length; offset += size, index += 1) {
    statements.push(db.prepare("INSERT INTO message_center_catalog_chunks (chunk_index, payload) VALUES (?, ?)").bind(index, payload.slice(offset, offset + size)));
  }
  await db.batch(statements);
}

function authorized(request, env) {
  const expected = env.MESSAGE_CENTER_ADMIN_TOKEN;
  return Boolean(expected) && request.headers.get("authorization") === `Bearer ${expected}`;
}

function collectorAuthorized(request, env) {
  return Boolean(env.PT_MESSAGE_COLLECTOR_TOKEN) && request.headers.get("authorization") === `Bearer ${env.PT_MESSAGE_COLLECTOR_TOKEN}`;
}

function exportPayload(catalog) {
  return { generated_at: new Date().toISOString(), messages: catalog.messages || [] };
}

function slugify(value) {
  return String(value || "").toLowerCase().replace(/[^a-z0-9]+/g, "_").replace(/^_+|_+$/g, "") || "message";
}

function normaliseCatalog(raw) {
  const messages = Array.isArray(raw?.messages) ? raw.messages : [];
  const taxonomy = raw?.taxonomy && typeof raw.taxonomy === "object" ? structuredClone(raw.taxonomy) : {};
  for (const message of messages) {
    message.group = slugify(message.group || "postoochat");
    message.category = String(message.category || "").trim();
    taxonomy[message.group] ||= [""];
    if (!taxonomy[message.group].includes(message.category)) taxonomy[message.group].push(message.category);
  }
  for (const group of Object.keys(taxonomy)) taxonomy[group] = [...new Set(taxonomy[group].map((value) => String(value || "").trim()))];
  return { taxonomy, messages };
}

function messageByName(catalog, name) {
  return catalog.messages.find((message) => message?.name === name);
}

function interpolate(value, variables) {
  if (typeof value === "string") return value.replace(/{{\s*([a-zA-Z0-9_]+)\s*}}/g, (_, key) => variables?.[key] ?? "");
  if (Array.isArray(value)) return value.map((item) => interpolate(item, variables));
  if (value && typeof value === "object") return Object.fromEntries(Object.entries(value).map(([key, item]) => [key, interpolate(item, variables)]));
  return value;
}

function resolvePreview(message, channel, variables) {
  const base = message?.default && typeof message.default === "object" ? message.default : {};
  const override = message?.overrides?.[channel] && typeof message.overrides[channel] === "object" ? message.overrides[channel] : {};
  return interpolate({ ...base, ...override, assets: message?.assets || [], links: message?.links || [] }, variables || {});
}

export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    if (url.pathname === "/health") return json({ ok: true, service: "postoochat-message-center" });
    if (!url.pathname.startsWith("/api/")) return env.ASSETS.fetch(request);
    if (url.pathname === "/api/export/collect/postoochat" && request.method === "GET") {
      if (!collectorAuthorized(request, env)) return json({ error: "unauthorized" }, 401);
      return json(exportPayload(await loadCatalog(env.DB)));
    }
    if (!authorized(request, env)) return json({ error: "unauthorized" }, 401);

    if (url.pathname === "/api/catalog" && request.method === "GET") return json(normaliseCatalog(await loadCatalog(env.DB)));
    if (url.pathname === "/api/export" && request.method === "GET") return json(exportPayload(await loadCatalog(env.DB)));

    if (url.pathname === "/api/message" && request.method === "GET") {
      const message = messageByName(await loadCatalog(env.DB), url.searchParams.get("name") || "");
      return message ? json(message) : json({ error: "message_not_found" }, 404);
    }

    if (url.pathname === "/api/catalog" && request.method === "PUT") {
      const catalog = normaliseCatalog(await request.json());
      if (!catalog || !Array.isArray(catalog.messages) || typeof catalog.taxonomy !== "object") {
        return json({ error: "invalid_catalog" }, 422);
      }
      await saveCatalog(env.DB, catalog);
      return json({ ok: true, exported_count: catalog.messages.length });
    }

    if (url.pathname === "/api/preview" && request.method === "POST") {
      const body = await request.json();
      const message = body.message || {};
      const channel = String(body.channel || "telegram");
      const binding = message?.overrides?.[channel]?.binding || {};
      return json({ message, channel, resolved: resolvePreview(message, channel, body.variables || {}), bindingText: Object.entries(binding).map(([key, value]) => `${key}=${value}`).join(", ") || "no binding", detectedFormats: [] });
    }

    if (url.pathname === "/api/messages/create" && request.method === "POST") {
      const body = await request.json();
      const message = body.message;
      if (!message?.name) return json({ error: "message_name_required" }, 400);
      const catalog = normaliseCatalog(await loadCatalog(env.DB));
      if (messageByName(catalog, message.name)) return json({ error: "message_name_exists" }, 409);
      catalog.messages.push(message);
      const saved = normaliseCatalog(catalog); await saveCatalog(env.DB, saved);
      return json({ ok: true, message, exported_count: saved.messages.length }, 201);
    }

    if (url.pathname === "/api/messages/save" && request.method === "POST") {
      const body = await request.json();
      const catalog = normaliseCatalog(await loadCatalog(env.DB));
      const index = catalog.messages.findIndex((message) => message?.name === body.selected_name);
      if (index < 0) return json({ error: "message_not_found" }, 404);
      if (!body.message?.name) return json({ error: "message_name_required" }, 400);
      catalog.messages[index] = body.message;
      const saved = normaliseCatalog(catalog); await saveCatalog(env.DB, saved);
      return json({ ok: true, message: body.message, exported_count: saved.messages.length });
    }

    if (url.pathname === "/api/messages/delete" && request.method === "POST") {
      const body = await request.json(); const catalog = normaliseCatalog(await loadCatalog(env.DB));
      const index = catalog.messages.findIndex((message) => message?.name === body.selected_name);
      if (index < 0) return json({ error: "message_not_found" }, 404);
      const [deleted] = catalog.messages.splice(index, 1); const saved = normaliseCatalog(catalog); await saveCatalog(env.DB, saved);
      return json({ ok: true, deleted_name: deleted.name, next_selected_name: saved.messages[index]?.name || saved.messages.at(-1)?.name || "", exported_count: saved.messages.length });
    }

    if (url.pathname === "/api/taxonomy" && request.method === "POST") {
      const body = await request.json(); const catalog = normaliseCatalog(await loadCatalog(env.DB));
      const group = slugify(body.group || ""); const category = String(body.category || "").trim(); const key = slugify(body.new_key || "");
      if (body.action === "create_group" && group) catalog.taxonomy[group] ||= [""];
      else if (body.action === "create_category" && group && key) { catalog.taxonomy[group] ||= [""]; catalog.taxonomy[group].push(key); }
      else if (body.action === "rename_group" && group && key) { if (catalog.taxonomy[key] && key !== group) return json({ error: "group_name_exists" }, 409); catalog.taxonomy[key] = catalog.taxonomy[group] || [""]; delete catalog.taxonomy[group]; for (const item of catalog.messages) if (item.group === group) item.group = key; }
      else if (body.action === "rename_category" && group && category && key) { catalog.taxonomy[group] = (catalog.taxonomy[group] || []).map((item) => item === category ? key : item); for (const item of catalog.messages) if (item.group === group && item.category === category) item.category = key; }
      else if (body.action === "remove_category" && group && category) { catalog.taxonomy[group] = (catalog.taxonomy[group] || []).filter((item) => item !== category); for (const item of catalog.messages) if (item.group === group && item.category === category) item.category = ""; }
      else return json({ error: "invalid_taxonomy_action" }, 400);
      const saved = normaliseCatalog(catalog); await saveCatalog(env.DB, saved); return json({ ok: true, ...saved });
    }
    if (url.pathname === "/api/export-webhook" && request.method === "POST") {
      if (!env.PT_MESSAGE_COLLECTOR_WEBHOOK || !env.PT_MESSAGE_COLLECTOR_TOKEN) return json({ error: "collector_configuration_missing" }, 409);
      const payload = exportPayload(await loadCatalog(env.DB));
      const response = await fetch(env.PT_MESSAGE_COLLECTOR_WEBHOOK, {
        method: "POST",
        headers: { "authorization": `Bearer ${env.PT_MESSAGE_COLLECTOR_TOKEN}`, "content-type": "application/json" },
        body: JSON.stringify({ event: "message_export.ready", source: "message_center", collector: "postoochat", generated_at: payload.generated_at, rendered_count: payload.messages.length, collection_url: `${url.origin}/api/export/collect/postoochat` }),
      });
      return json({ ok: response.ok, exported_count: payload.messages.length, notified_count: response.ok ? 1 : 0, failed_count: response.ok ? 0 : 1 }, response.ok ? 200 : 502);
    }
    return json({ error: "not_found" }, 404);
  },
};
