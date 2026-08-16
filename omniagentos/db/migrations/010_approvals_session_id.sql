-- W1 Session Bridge (lead base-commit, DR-005): additive nullable link from an
-- approval to the interactive session that raised it. NULL for every existing
-- run-scoped approval, so this is backward-compatible: existing code never reads
-- the column, and the runner's approval path is unaffected. The sessions table
-- itself is created by a later migration owned by the Session-Bridge core package.
ALTER TABLE approvals ADD COLUMN session_id TEXT;

-- Point-lookup + queue-filter support: the decide route resolves an approval by
-- id directly (no O(500) scan, DR-009) and the Sessions UI filters by session.
CREATE INDEX IF NOT EXISTS idx_approvals_session_id ON approvals(session_id);
