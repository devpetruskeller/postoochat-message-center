import seedCatalog from "../messages.json";

export type Channel = "telegram" | "whatsapp";

export interface MessageRecord {
	name: string;
	id: number;
	group?: string;
	category?: string;
	default?: Record<string, unknown>;
	overrides?: Partial<Record<Channel, Record<string, unknown>>>;
	assets?: unknown[];
	links?: unknown[];
	[key: string]: unknown;
}

export interface Catalog {
	taxonomy: Record<string, string[]>;
	messages: MessageRecord[];
}

interface Env {
	DB: D1Database;
	ASSETS: Fetcher;
	MESSAGE_CENTER_ADMIN_TOKEN?: string;
	PT_MESSAGE_COLLECTOR_TOKEN?: string;
	PT_MESSAGE_COLLECTOR_WEBHOOK?: string;
	PT_MESSAGE_STUDIO_PUBLIC_URL?: string;
}

const EMPTY_CATALOG: Catalog = { taxonomy: {}, messages: [] };

function json(value: unknown, status = 200): Response {
	return new Response(JSON.stringify(value), {
		status,
		headers: { "content-type": "application/json; charset=utf-8", "cache-control": "no-store" },
	});
}

function unauthorized(): Response {
	return json({ error: "unauthorized" }, 401);
}

function isAuthorized(request: Request, env: Env): boolean {
	const token = env.MESSAGE_CENTER_ADMIN_TOKEN?.trim();
	// A token is mandatory before deployment. Allowing its absence locally keeps
	// first-run catalog import simple and does not expose a deployed worker.
	return !token || request.headers.get("authorization") === `Bearer ${token}`;
}

function collectorAuthorized(request: Request, env: Env): boolean {
	const token = env.PT_MESSAGE_COLLECTOR_TOKEN?.trim();
	return Boolean(token) && request.headers.get("authorization") === `Bearer ${token}`;
}

function normaliseCatalog(input: unknown): Catalog {
	const raw = input && typeof input === "object" ? input as Partial<Catalog> : {};
	const messages = Array.isArray(raw.messages) ? raw.messages.filter((message): message is MessageRecord =>
		Boolean(message) && typeof message === "object" && typeof message.name === "string" && message.name.trim().length > 0,
	) : [];
	const taxonomy = raw.taxonomy && typeof raw.taxonomy === "object" ? structuredClone(raw.taxonomy) as Record<string, string[]> : {};
	for (const message of messages) {
		message.group = String(message.group || "postoochat").trim() || "postoochat";
		message.category = String(message.category || "").trim();
		message.overrides = message.overrides && typeof message.overrides === "object" ? message.overrides : {};
		message.assets = Array.isArray(message.assets) ? message.assets : [];
		message.links = Array.isArray(message.links) ? message.links : [];
		taxonomy[message.group] ||= [];
		if (!taxonomy[message.group].includes(message.category)) taxonomy[message.group].push(message.category);
	}
	return { taxonomy, messages };
}

const SEED_CATALOG: Catalog = normaliseCatalog(seedCatalog);

async function loadCatalog(db: D1Database): Promise<Catalog> {
	const row = await db.prepare("SELECT payload FROM message_center_catalog WHERE id = 1").first<{ payload: string }>();
	if (!row) return saveCatalog(db, structuredClone(SEED_CATALOG));
	try { return normaliseCatalog(JSON.parse(row.payload)); } catch { return structuredClone(EMPTY_CATALOG); }
}

async function saveCatalog(db: D1Database, catalog: Catalog): Promise<Catalog> {
	const normalised = normaliseCatalog(catalog);
	await db.prepare("INSERT INTO message_center_catalog (id, payload, updated_at) VALUES (1, ?, strftime('%Y-%m-%dT%H:%M:%fZ', 'now')) ON CONFLICT(id) DO UPDATE SET payload = excluded.payload, updated_at = excluded.updated_at")
		.bind(JSON.stringify(normalised)).run();
	return normalised;
}

function messageByName(catalog: Catalog, name: string): MessageRecord | undefined {
	return catalog.messages.find((message) => message.name === name);
}

function groupKey(value: string): string {
	return value.trim().toLowerCase().replace(/[^a-z0-9]+/g, "_").replace(/^_+|_+$/g, "");
}

function exportMessage(message: MessageRecord, channel: Channel) {
	const base = message.default && typeof message.default === "object" ? message.default : {};
	const override = message.overrides?.[channel] && typeof message.overrides[channel] === "object" ? message.overrides[channel] : {};
	const assets = message.assets || [];
	const actions = Array.isArray(override.actions) ? override.actions : Array.isArray(base.actions) ? base.actions : [];
	const resolvedActions = actions.map((action) => {
		if (!action || typeof action !== "object") return action;
		const raw = action as Record<string, unknown>;
		const assetId = typeof raw.assetId === "string" ? raw.assetId : "";
		const asset = assetId ? assets.find((candidate) => candidate && typeof candidate === "object" && (candidate as Record<string, unknown>).assetId === assetId) : undefined;
		const nextMessageId = raw.nextMessageId;
		const target = asset && typeof asset === "object"
			? { type: "asset", assetId, mediaType: (asset as Record<string, unknown>).type, url: (asset as Record<string, unknown>).url }
			: nextMessageId != null ? { type: "message", messageId: nextMessageId } : undefined;
		return target ? { ...raw, target } : raw;
	});
	const variant = { ...base, ...override, actions: resolvedActions } as Record<string, unknown>;
	const parseMode = String(variant.parse_mode || "");
	const renderPart = (part: unknown) => {
		const value = part && typeof part === "object" ? part as Record<string, unknown> : {};
		let text = String(value.text || "");
		const formats = Array.isArray(value.format) ? value.format.map(String) : [];
		if (formats.includes("uppercase")) text = text.toUpperCase();
		if (formats.includes("lowercase")) text = text.toLowerCase();
		if (formats.includes("capitalize")) text = text.replace(/\b\w/g, (letter) => letter.toUpperCase());
		const html = channel === "telegram" && parseMode.toLowerCase() === "html";
		if (html) text = text.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
		for (const [format, before, after] of [["bold", html ? "<b>" : "*", html ? "</b>" : "*"], ["italic", html ? "<i>" : "_", html ? "</i>" : "_"], ["strike", html ? "<s>" : "~", html ? "</s>" : "~"], ["code", html ? "<code>" : "`", html ? "</code>" : "`"]]) if (formats.includes(format)) text = `${before}${text.trim()}${after}`;
		if (formats.includes("underline") && html) text = `<u>${text.trim()}</u>`;
		if (formats.includes("quote")) text = `> ${text}`;
		return text;
	};
	const content = [variant.body, ...(Array.isArray(variant.blocks) ? variant.blocks : [])];
	let renderedResult = content.map((block) => {
		const value = block && typeof block === "object" ? block as Record<string, unknown> : {};
		if (Array.isArray(value.parts) && value.parts.length) return value.parts.map(renderPart).join("");
		return renderPart(value);
	}).filter((text) => text.trim()).join("\n\n");
	const actionsMenu = variant.actions_menu && typeof variant.actions_menu === "object" ? variant.actions_menu as Record<string, unknown> : {};
	if (channel === "whatsapp" && actionsMenu.enabled === true && resolvedActions.length) {
		const instruction = String(actionsMenu.instruction || "Reply with one of the following options.");
		const itemFormat = String(actionsMenu.item_format || "{index}. {label}");
		const menu = resolvedActions.map((action, index) => {
			const value = action && typeof action === "object" ? action as Record<string, unknown> : {};
			return itemFormat.replaceAll("{index}", String(index + 1)).replaceAll("{label}", String(value.label || value.key || "")).replaceAll("{key}", String(value.key || ""));
		}).join("\n");
		renderedResult = [renderedResult, instruction, menu].filter(Boolean).join("\n\n");
	}
	const variables: Record<string, string> = {};
	if (variant.title) variables.title = String(variant.title);
	for (const key of Object.keys(message.variables || {})) variables[key] = `{{${key}}}`;
	return {
		id: message.id, name: message.name, suite_key: String(message.suite_key || ""), group: String(message.group || "postoochat"), category: String(message.category || ""), channel,
		variables, rendered_result: renderedResult, description: String(message.description || ""), assets, links: message.links || [], updated_at: new Date().toISOString(),
		...(variant.title ? { title: String(variant.title) } : {}), ...(variant.delivery ? { delivery: String(variant.delivery) } : {}), ...(variant.parse_mode ? { parse_mode: String(variant.parse_mode) } : {}),
		binding: variant.binding && typeof variant.binding === "object" ? variant.binding : {}, actions: resolvedActions,
		...(channel === "telegram" ? { inline_buttons_enabled: variant.inline_buttons_enabled !== false } : { template_enabled: variant.template_enabled === true, ...(Object.keys(actionsMenu).length ? { actions_menu: actionsMenu } : {}) }),
	};
}

function collectorExport(catalog: Catalog) {
	const allowedGroups = new Set(["postoochat_suite"]);
	const allowedCategories = new Set(["General"]);
	return {
		source: "message_center_v2",
		collector: "postoochat",
		sent_at: new Date().toISOString(),
		app_directory: [],
		messages: catalog.messages
			.filter((message) => allowedGroups.has(String(message.group)) && allowedCategories.has(String(message.category)))
			.flatMap((message) => [exportMessage(message, "telegram"), exportMessage(message, "whatsapp")]),
	};
}

export default {
	async fetch(request: Request, env: Env): Promise<Response> {
		const url = new URL(request.url);
		if (!url.pathname.startsWith("/api/")) {
			const asset = await env.ASSETS.fetch(request);
			if (url.pathname !== "/" || !asset.headers.get("content-type")?.includes("text/html")) return asset;
			const html = (await asset.text()).replace("</head>", '<link rel="stylesheet" href="/studio.css"><script defer src="/studio-shell.js"></script></head>');
			const headers = new Headers(asset.headers);
			headers.delete("content-length");
			headers.set("cache-control", "no-store");
			return new Response(html, { status: asset.status, headers });
		}
		if (url.pathname === "/api/export/collect/postoochat" && request.method === "GET") {
			if (!collectorAuthorized(request, env)) return unauthorized();
			return json(collectorExport(await loadCatalog(env.DB)));
		}
		if (!isAuthorized(request, env)) return unauthorized();

		if (url.pathname === "/api/health") return json({ ok: true, service: "message-center-v2" });
		if (url.pathname === "/api/catalog" && request.method === "GET") return json(await loadCatalog(env.DB));
		if (url.pathname === "/api/catalog" && request.method === "PUT") return json(await saveCatalog(env.DB, await request.json()));
		if (url.pathname === "/api/taxonomy" && request.method === "POST") {
			const body = await request.json<{ action?: string; group?: string; category?: string }>();
			const catalog = await loadCatalog(env.DB);
			const group = groupKey(String(body.group || ""));
			const category = String(body.category || "").trim();
			if (body.action === "create_group" && group) catalog.taxonomy[group] ||= [];
			else if (body.action === "create_category" && group && category) {
				catalog.taxonomy[group] ||= [];
				if (!catalog.taxonomy[group].includes(category)) catalog.taxonomy[group].push(category);
			} else return json({ error: "invalid_taxonomy_request" }, 400);
			return json(await saveCatalog(env.DB, catalog));
		}

		if (url.pathname === "/api/messages" && request.method === "POST") {
			const body = await request.json<{ name?: string; group?: string; category?: string }>();
			const name = String(body.name || "").trim().toUpperCase().replace(/[^A-Z0-9_]+/g, "_").replace(/^_+|_+$/g, "");
			const group = groupKey(String(body.group || ""));
			const category = String(body.category || "").trim();
			if (!name || !group || !category) return json({ error: "name_group_and_category_required" }, 400);
			const catalog = await loadCatalog(env.DB);
			if (messageByName(catalog, name)) return json({ error: "message_name_already_exists" }, 409);
			catalog.taxonomy[group] ||= [];
			if (!catalog.taxonomy[group].includes(category)) catalog.taxonomy[group].push(category);
			const message: MessageRecord = {
				name, id: Date.now(), group, category,
				description: "Describe what this message is for.",
				default: { title: "New message", body: { id: "body", type: "text", parts: [{ text: "Write your message here.", format: [] }], required: true }, blocks: [], actions: [] },
				overrides: {}, assets: [], links: [],
			};
			catalog.messages.push(message);
			await saveCatalog(env.DB, catalog);
			return json({ message });
		}

		if (url.pathname.startsWith("/api/messages/") && request.method === "DELETE") {
			const name = decodeURIComponent(url.pathname.slice("/api/messages/".length));
			const catalog = await loadCatalog(env.DB);
			const index = catalog.messages.findIndex((message) => message.name === name);
			if (index === -1) return json({ error: "message_not_found" }, 404);
			catalog.messages.splice(index, 1);
			await saveCatalog(env.DB, catalog);
			return json({ ok: true });
		}

		if (url.pathname === "/api/messages/channel-override" && request.method === "PUT") {
			const body = await request.json<{ name?: string; channel?: Channel; override?: Record<string, unknown> }>();
			if (!body.name || !["telegram", "whatsapp"].includes(String(body.channel)) || !body.override || typeof body.override !== "object") {
				return json({ error: "name_channel_and_override_required" }, 400);
			}
			const catalog = await loadCatalog(env.DB);
			const message = messageByName(catalog, body.name);
			if (!message) return json({ error: "message_not_found" }, 404);
			message.overrides ||= {};
			message.overrides[body.channel!] = structuredClone(body.override);
			await saveCatalog(env.DB, catalog);
			return json({ ok: true, message });
		}

		if (url.pathname === "/api/export" && request.method === "GET") {
			const catalog = await loadCatalog(env.DB);
			return json({ source: "message_center_v2", sent_at: new Date().toISOString(), app_directory: [], messages: catalog.messages.flatMap((message) => [exportMessage(message, "telegram"), exportMessage(message, "whatsapp")]) });
		}
		if (url.pathname === "/api/export-webhook" && request.method === "POST") {
			if (!env.PT_MESSAGE_COLLECTOR_WEBHOOK?.trim() || !env.PT_MESSAGE_COLLECTOR_TOKEN?.trim()) return json({ error: "collector_configuration_missing" }, 409);
			const payload = collectorExport(await loadCatalog(env.DB));
			try {
				const response = await fetch(env.PT_MESSAGE_COLLECTOR_WEBHOOK, {
					method: "POST",
					headers: { authorization: `Bearer ${env.PT_MESSAGE_COLLECTOR_TOKEN}`, "content-type": "application/json" },
					body: JSON.stringify({ event: "message_export.ready", source: payload.source, collector: "postoochat", sent_at: payload.sent_at, rendered_count: payload.messages.length, collection_url: `${(env.PT_MESSAGE_STUDIO_PUBLIC_URL?.trim() || url.origin).replace(/\/+$/, "")}/api/export/collect/postoochat` }),
				});
				if (!response.ok) return json({ error: "collector_notification_failed", status: response.status }, 502);
				return json({ ok: true, collector: "postoochat", rendered_count: payload.messages.length });
			} catch (error) {
				return json({ error: "collector_notification_failed", detail: error instanceof Error ? error.message : String(error) }, 502);
			}
		}
		return json({ error: "not_found" }, 404);
	},
} satisfies ExportedHandler<Env>;
