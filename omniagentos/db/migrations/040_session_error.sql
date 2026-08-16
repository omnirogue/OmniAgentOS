-- 040_session_error.sql
-- Persist auth-failure + terminal-error text for board visibility.
--
-- Migration versions are recorded atomically by omniagentos.db.migrate, so this
-- additive ALTER runs exactly once for each database.
ALTER TABLE sessions ADD COLUMN error TEXT;
