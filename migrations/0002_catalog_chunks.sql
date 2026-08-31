CREATE TABLE IF NOT EXISTS message_center_catalog_chunks (
  chunk_index INTEGER PRIMARY KEY,
  payload TEXT NOT NULL,
  updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);
