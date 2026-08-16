-- M5 — durable per-account weekly attempt quotas.
--
-- Existing accounts begin with the configurable default ceiling and zero
-- recorded usage. quota_reset_at is initialized lazily on the first quota
-- read, which avoids inventing a window boundary during migration.
ALTER TABLE claude_accounts ADD COLUMN weekly_attempt_quota INTEGER NOT NULL DEFAULT 250;
ALTER TABLE claude_accounts ADD COLUMN weekly_attempts_used INTEGER NOT NULL DEFAULT 0;
ALTER TABLE claude_accounts ADD COLUMN quota_reset_at TEXT;
