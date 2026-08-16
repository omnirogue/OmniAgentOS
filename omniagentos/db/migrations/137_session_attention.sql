-- 137_session_attention.sql
-- Attributed attention routing for Claude Code hook events.
--
-- Notification / permission_request / Stop / SessionEnd hooks write a last-
-- write-wins snapshot onto the session row so the dashboard can show which
-- session needs the operator, and why. Additive + nullable: every existing
-- session simply has no attention yet. SELECT * and the DAL read projection
-- carry the new columns through with no query-shape change beyond listing them.
--
-- attention_state   "needs_input" | NULL (cleared on stop/terminalization)
-- attention_reason  operator-visible message from the hook payload
-- attention_since   ISO 8601 UTC timestamp of the last write
ALTER TABLE sessions ADD COLUMN attention_state TEXT;
ALTER TABLE sessions ADD COLUMN attention_reason TEXT;
ALTER TABLE sessions ADD COLUMN attention_since TEXT;

CREATE INDEX IF NOT EXISTS idx_sessions_attention_state ON sessions(attention_state);
