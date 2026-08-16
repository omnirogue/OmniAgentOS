-- U-A1: mandatory broker-owned intent/final audit spine.
--
-- Append-only expansion of migration 006's broker_calls table.  Existing
-- columns remain readable by older dashboard and loop code; the new columns
-- carry the metadata-only S3-W4 contract.  No request/response payload,
-- authentication material, raw header/query, or exception text has a column.

ALTER TABLE broker_calls ADD COLUMN call_id TEXT NOT NULL DEFAULT '';
ALTER TABLE broker_calls ADD COLUMN request_id TEXT NOT NULL DEFAULT '';
ALTER TABLE broker_calls ADD COLUMN task_id TEXT NOT NULL DEFAULT '';
ALTER TABLE broker_calls ADD COLUMN session_id TEXT NOT NULL DEFAULT '';
ALTER TABLE broker_calls ADD COLUMN holder TEXT NOT NULL DEFAULT '';
ALTER TABLE broker_calls ADD COLUMN action_mode TEXT NOT NULL DEFAULT '';
ALTER TABLE broker_calls ADD COLUMN connector TEXT NOT NULL DEFAULT '';
ALTER TABLE broker_calls ADD COLUMN env_name TEXT NOT NULL DEFAULT '';
ALTER TABLE broker_calls ADD COLUMN credential_id TEXT NOT NULL DEFAULT '';
ALTER TABLE broker_calls ADD COLUMN key_version_id TEXT NOT NULL DEFAULT '';
ALTER TABLE broker_calls ADD COLUMN grant_id TEXT NOT NULL DEFAULT '';
ALTER TABLE broker_calls ADD COLUMN grant_issuer TEXT NOT NULL DEFAULT '';
ALTER TABLE broker_calls ADD COLUMN grant_expires_at TEXT NOT NULL DEFAULT '';
ALTER TABLE broker_calls ADD COLUMN registry_digest TEXT NOT NULL DEFAULT '';
ALTER TABLE broker_calls ADD COLUMN target_host TEXT NOT NULL DEFAULT '';
ALTER TABLE broker_calls ADD COLUMN request_schema_hash TEXT NOT NULL DEFAULT '';
ALTER TABLE broker_calls ADD COLUMN decision TEXT NOT NULL DEFAULT '';
ALTER TABLE broker_calls ADD COLUMN reason_code TEXT NOT NULL DEFAULT '';
ALTER TABLE broker_calls ADD COLUMN upstream_status_class TEXT NOT NULL DEFAULT '';
ALTER TABLE broker_calls ADD COLUMN latency_ms INTEGER NOT NULL DEFAULT 0;
ALTER TABLE broker_calls ADD COLUMN response_size_class TEXT NOT NULL DEFAULT '';
ALTER TABLE broker_calls ADD COLUMN redaction_count INTEGER NOT NULL DEFAULT 0;
ALTER TABLE broker_calls ADD COLUMN budget_receipt_id TEXT NOT NULL DEFAULT '';
ALTER TABLE broker_calls ADD COLUMN correlation_id TEXT NOT NULL DEFAULT '';

CREATE INDEX idx_broker_calls_call ON broker_calls(call_id, id);
CREATE INDEX idx_broker_calls_request ON broker_calls(request_id, ts);
