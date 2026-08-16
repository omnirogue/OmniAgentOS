-- 002_machine_roles.sql — dispatcher-only enforcement for the shared queue (D8a).
-- This migration set is INDEPENDENT of omniagentos/db/migrations/ (see SPEC-shared-queue §1.2):
-- the queue DB must contain ONLY wq_* tables and schema_migrations, and its version numbers
-- are its own sequence. 002 here has nothing to do with the packaged 002.
--
-- the operator's ruling 2026-08-13: personal machines are DISPATCHERS — outgoing only. They submit
-- and observe; execution happens on fleet workers. 001 has no way to say that: every row in
-- wq_machines is equally entitled to claim, so a laptop on which someone runs the worker by
-- accident joins the pool silently and starts executing other people's briefs on a box that
-- was never meant to run them.
--
-- APPEND-ONLY, and 001_workqueue.sql is FROZEN — its own header says so, and the migrator
-- checksums what it applied, so editing an applied file is a corruption report rather than
-- an upgrade. Hence an ALTER here rather than two more columns up there.

-- role: the AUTHZ column. Server-derived at enrollment from
-- configs/workqueue.yaml:worker_allowlist — a client-supplied `role` in the POST /v1/machines
-- body is stripped, never honoured, so a machine can never elevate itself.
--
-- DEFAULT 'worker' is deliberate and is the whole no-behaviour-change story: every row that
-- already exists on the primary keeps claiming exactly as it did before this ran. Demoting
-- the rows that should NOT claim is an operator step (RUNBOOK §12 preflight, before the
-- enforcement flag is flipped) — a migration that silently drained the live fleet would be
-- an outage wearing a schema change's clothes.
ALTER TABLE wq_machines ADD COLUMN role TEXT NOT NULL DEFAULT 'worker'
  CHECK (role IN ('worker','dispatcher'));

-- device_class: INFORMATIONAL / AUDIT ONLY, and never a security control.
-- Free text for the operator reading `wq machines` ('personal-laptop', 'fleet-worker',
-- 'prod-host'): it records what a box IS, it never decides what a box MAY DO. Nothing in
-- the claim or enroll path may branch on it — tests/workqueue/test_machine_roles.py asserts
-- that no authz decision reads this column, precisely so a later "it's basically the same
-- thing" refactor cannot quietly promote a self-declared string into a permission.
ALTER TABLE wq_machines ADD COLUMN device_class TEXT;
