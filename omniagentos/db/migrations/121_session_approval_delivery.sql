-- Durable delivery outcome for session-bound approvals.  A pending approval is
-- not evidence that a human was actually reachable: the supervisor uses these
-- fields to distinguish delivered-but-undecided from never-delivered parks.
-- Existing rows are deliberately ``unattempted`` (unknown => not delivered),
-- never retrospectively promoted to delivered.
ALTER TABLE approvals ADD COLUMN delivery_state TEXT NOT NULL DEFAULT 'unattempted';
ALTER TABLE approvals ADD COLUMN delivery_attempted_at TEXT;
ALTER TABLE approvals ADD COLUMN delivered_at TEXT;

CREATE INDEX IF NOT EXISTS idx_approvals_session_delivery
ON approvals(session_id, delivery_state);
