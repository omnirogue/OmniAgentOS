-- 071_hot_lookup_indexes.sql
-- S14C / L-10 (remaining L14-owned hot lookups): indexes not covered by S14A
-- migration 070 (portfolio/board listing). Parent L14 claims 071 so it orders
-- safely after S14A's 070 without reusing that version.
--
-- Hot paths (production SQL, asserted via EXPLAIN QUERY PLAN):
--   1. approvals by (run_id, step_seq) — SqliteStore.get_approval_for
--   2. swarm_attempts by session_id   — swarm/dal attempt-for-session
--   3. idempotency by run_id          — SqliteStore.idem_for_run

CREATE INDEX IF NOT EXISTS idx_approvals_run_step
    ON approvals(run_id, step_seq);

-- Equality on session_id + ORDER BY started_at DESC, seq DESC LIMIT 1.
-- Partial index excludes NULL session rows (unbound attempts).
CREATE INDEX IF NOT EXISTS idx_swarm_attempts_session
    ON swarm_attempts(session_id, started_at DESC, seq DESC)
    WHERE session_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_idempotency_run
    ON idempotency(run_id, created_at ASC, key ASC);
