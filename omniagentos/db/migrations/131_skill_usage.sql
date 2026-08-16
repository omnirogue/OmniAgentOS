-- Migration 131: skill-usage telemetry.
--
-- Today nothing records which skills get injected into a brief, so skill
-- replay (an extracted skill actually being reused by a later run/worker)
-- cannot be proven from data. This table is the append-only record of every
-- injection: one row per skill that made it into a resolved/rendered brief,
-- written by the SAME two call sites that already resolve skill content
-- (`runner/core.py` and `swarm/spawn.py`), never a derived/reconstructed log.
--
-- Additive only: one new table, no existing writer changes shape.
CREATE TABLE IF NOT EXISTS skill_usage (
    id             INTEGER PRIMARY KEY,
    run_id         TEXT NOT NULL,
    skill_id       TEXT NOT NULL,
    skill_version  TEXT,
    brief_kind     TEXT NOT NULL,
    injected_at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_skill_usage_run_id ON skill_usage(run_id);
