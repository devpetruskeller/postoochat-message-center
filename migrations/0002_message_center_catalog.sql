-- Production previously applied a legacy migration named 0001_catalog.sql.
-- Keep this migration separate so existing D1 databases receive the v2 table.
CREATE TABLE IF NOT EXISTS message_center_catalog (
  id INTEGER PRIMARY KEY CHECK (id = 1),
  payload TEXT NOT NULL,
  updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);
