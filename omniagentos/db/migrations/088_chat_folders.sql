-- 088_chat_folders.sql
-- Chat folders become first-class: a registry keyed by the free-text folder
-- name that chats already carry in meta_json.folder. The registry adds
-- identity on top of those names — a color and a manual sort position —
-- without changing how membership is stored.
--
-- color holds a palette TOKEN NAME (gray|red|orange|yellow|green|teal|blue|
-- violet), never hex: the dashboard maps tokens to theme CSS variables so
-- both themes stay correct. Enforced in ChatStore, not by CHECK, so the
-- palette can grow without another migration.
--
-- Folders that exist only as free text on chats (never customized) have no
-- row here; readers union both sources and default missing rows to 'gray'.

CREATE TABLE IF NOT EXISTS chat_folders (
  name TEXT PRIMARY KEY,
  color TEXT NOT NULL DEFAULT 'gray',
  position INTEGER,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
