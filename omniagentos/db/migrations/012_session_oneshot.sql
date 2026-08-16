-- W1 Session Bridge repair2 (GROUP A / SEC-006 / T-DESIGN-001):
-- Durable single-use marker for the hook-eval one-shot authorization. An APPROVED,
-- human-gated, unexpired session approval authorizes EXACTLY ONE re-issued tool
-- call whose action_hash matches; consuming it stamps consumed_at atomically so a
-- second identical call finds it consumed and re-classifies (no replay).
-- Additive, nullable column; existing approvals stay unconsumed (NULL).
ALTER TABLE approvals ADD COLUMN consumed_at TEXT;
