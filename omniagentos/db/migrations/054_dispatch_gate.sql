-- FAST DISPATCH decision log (PKG-FAST-DISPATCH). Every decide() at intake --
-- whether it changed routing or not -- appends one row here. This is the
-- future fine-tune corpus for the three-gate classifier: brief_head + the
-- gate/decision/confidence that fired, plus whether it actually re-routed
-- (applied=1). Additive and best-effort: record_decision swallows every error
-- so telemetry can never block a dispatch (see omniagentos/dispatch/log.py).
CREATE TABLE dispatch_decisions (
    id           TEXT PRIMARY KEY,
    created_at   TEXT NOT NULL,
    brief_hash   TEXT NOT NULL,
    brief_head   TEXT NOT NULL,             -- first 160 chars of the brief
    decision     TEXT NOT NULL,             -- solo_fast|solo_standard|solo_complex|swarm|fallthrough
    gate         TEXT NOT NULL,             -- rules|semantic|llm|fallthrough
    risk_flagged INTEGER NOT NULL DEFAULT 0,
    confidence   REAL,
    latency_ms   REAL,
    applied      INTEGER NOT NULL DEFAULT 0, -- 1 when the gate actually changed routing
    dispatch_kind TEXT,                      -- session|swarm|... when known
    ref_id       TEXT                        -- session or swarm_run id when known
);

CREATE INDEX idx_dispatch_decisions_created ON dispatch_decisions(created_at);
