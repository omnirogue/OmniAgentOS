-- 036_claude_accounts.sql
-- Multiple Claude accounts, so the session spawner can rotate across them and get
-- past a single account's rate limit (Anthropic limits are the binding constraint).
--
-- An account is primarily a CONFIG DIR (the CLI's CLAUDE_CONFIG_DIR): the OAuth login
-- lives in that dir, so NO secret is stored here -- only the path. A token/api-key
-- account keeps its secret OUTSIDE the DB (var/secrets, 0600) and references it by
-- secret_ref. Nothing in this table is itself a credential.
CREATE TABLE IF NOT EXISTS claude_accounts (
    id            TEXT PRIMARY KEY,
    label         TEXT NOT NULL,
    auth_type     TEXT NOT NULL DEFAULT 'config_dir',   -- config_dir | oauth_token | api_key
    config_dir    TEXT,                                 -- CLAUDE_CONFIG_DIR (config_dir accounts)
    email         TEXT,                                 -- auto-detected from the dir's oauthAccount
    secret_ref    TEXT,                                 -- var/secrets filename (token/api_key only)
    enabled       INTEGER NOT NULL DEFAULT 1,           -- included in spawn rotation
    is_default    INTEGER NOT NULL DEFAULT 0,           -- the account used when rotation is off/empty
    status        TEXT NOT NULL DEFAULT 'unknown',      -- unknown | ok | error | rate_limited
    status_detail TEXT,
    last_used_at  TEXT,
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL
);

-- One row per config dir (auto-detection is idempotent via INSERT OR IGNORE).
CREATE UNIQUE INDEX IF NOT EXISTS idx_claude_accounts_config_dir
    ON claude_accounts(config_dir) WHERE config_dir IS NOT NULL;
