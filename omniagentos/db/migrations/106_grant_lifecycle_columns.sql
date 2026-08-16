-- Scoped lifecycle metadata for current agent capability grants.
--
-- Existing grants retain their read-only semantics.  Audit rows are not rewritten:
-- the nullable log columns only record lifecycle facts for grants issued after
-- this migration.

ALTER TABLE agent_capabilities ADD COLUMN mode TEXT DEFAULT 'read';
ALTER TABLE agent_capabilities ADD COLUMN expires_at TEXT;
ALTER TABLE agent_capabilities ADD COLUMN issued_by TEXT;
ALTER TABLE agent_capabilities ADD COLUMN request_id TEXT;

-- Keep lifecycle facts with the append-only audit event that created the grant.
-- Existing historical events intentionally remain NULL in these columns.
ALTER TABLE capability_grant_log ADD COLUMN mode TEXT;
ALTER TABLE capability_grant_log ADD COLUMN expires_at TEXT;
ALTER TABLE capability_grant_log ADD COLUMN issued_by TEXT;
ALTER TABLE capability_grant_log ADD COLUMN request_id TEXT;
