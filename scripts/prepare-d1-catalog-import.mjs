import { readFile, writeFile } from "node:fs/promises";

const catalog = JSON.stringify(JSON.parse(await readFile(new URL("../messages.json", import.meta.url), "utf8")));
const chunks = [];
for (let index = 0; index * 24000 < catalog.length; index += 1) chunks.push(catalog.slice(index * 24000, (index + 1) * 24000));
const escaped = (value) => value.replace(/'/g, "''");
const sql = ["DELETE FROM message_center_catalog_chunks;", ...chunks.map((chunk, index) => `INSERT INTO message_center_catalog_chunks (chunk_index, payload) VALUES (${index}, '${escaped(chunk)}');`)].join("\n") + "\n";
await writeFile(new URL("../.d1-catalog-import.sql", import.meta.url), sql, "utf8");
