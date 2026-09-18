import { readFile, writeFile } from "node:fs/promises";

const catalog = JSON.parse(await readFile(new URL("../messages.json", import.meta.url), "utf8"));
const payload = JSON.stringify(catalog).replaceAll("'", "''");
const sql = `INSERT INTO message_center_catalog (id, payload, updated_at)\nVALUES (1, '${payload}', strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))\nON CONFLICT(id) DO UPDATE SET payload = excluded.payload, updated_at = excluded.updated_at;\n`;
await writeFile(new URL("../.d1-catalog-import.sql", import.meta.url), sql);
