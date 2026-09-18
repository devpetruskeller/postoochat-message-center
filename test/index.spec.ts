import { env, createExecutionContext, waitOnExecutionContext } from "cloudflare:test";
import { beforeEach, describe, expect, it } from "vitest";
import worker from "../src";

describe("Message Center catalog API", () => {
	beforeEach(async () => {
		await env.DB.exec("CREATE TABLE IF NOT EXISTS message_center_catalog (id INTEGER PRIMARY KEY CHECK (id = 1), payload TEXT NOT NULL, updated_at TEXT NOT NULL)");
		await env.DB.exec("DELETE FROM message_center_catalog");
	});

	it("persists an explicit Telegram channel override", async () => {
		const ctx = createExecutionContext();
		const catalog = { taxonomy: {}, messages: [{ name: "START_HERE", id: 1, default: { title: "Start" }, overrides: {} }] };
		await worker.fetch(new Request("http://example.com/api/catalog", { method: "PUT", body: JSON.stringify(catalog) }), env, ctx);
		const response = await worker.fetch(new Request("http://example.com/api/messages/channel-override", {
			method: "PUT", body: JSON.stringify({ name: "START_HERE", channel: "telegram", override: { title: "Telegram Start" } }),
		}), env, ctx);
		await waitOnExecutionContext(ctx);
		expect(response.status).toBe(200);
		const saved = await response.json<{ message: { overrides: { telegram: { title: string } } } }>();
		expect(saved.message.overrides.telegram.title).toBe("Telegram Start");
	});
});
