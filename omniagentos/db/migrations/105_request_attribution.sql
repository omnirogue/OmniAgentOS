-- Migration 105: Truthful request attribution (U-E7)
--
-- Kill the hardcoded `requested_by="owner"` on agent requests. Store `from_agent_id`
-- separately to record the actual emitting principal; `requested_by` becomes display
-- attribution using canonical spellings: `lane:*`, `loop:*`, `job:*`, `human:*`, `system`.
--
-- Additive migration: adds `from_agent_id` column to agent_requests.
--
-- Canonical spellings enforced at write path; a later migration in U-E1 will add
-- a CHECK constraint. Non-canonical spellings like `agent:<id>` are rejected by
-- the store layer validation before insert.
--
-- Missing authenticated identity on an internal event → `system` + trace log.
-- External request claiming identity different from channel principal → rejected.

ALTER TABLE agent_requests
ADD COLUMN from_agent_id TEXT DEFAULT 'system';
-- ^-- stores the authenticated/emitting principal in canonical form
--    (lane:*, loop:*, job:*, human:*, system); defaults to 'system' for legacy rows
--    and internal system events with no authenticated agent.

ALTER TABLE agent_requests
ADD COLUMN from_lane TEXT;
-- ^-- optional: the lane/channel that emitted this request (for auditability).

ALTER TABLE agent_requests
ADD COLUMN session_id TEXT;
-- ^-- optional: session context if this request came from a session principal.

ALTER TABLE agent_requests
ADD COLUMN run_id TEXT;
-- ^-- optional: run context if this request was made during a specific run.
