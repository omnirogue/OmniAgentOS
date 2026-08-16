-- ISSUE-8 follow-up (Sol review, seam 2): routine_runs.cost_usd stops
-- claiming a precise $0.00 for a run whose true cost is unknown.
--
-- Migration 119 made the PARENT rollup (routines.total_cost_usd) nullable,
-- but left the CHILD audit row (routine_runs.cost_usd, migration 015) at its
-- original `NOT NULL DEFAULT 0`. That gap relocated ISSUE-8 rather than
-- closing it: `RoutinesStore.settle_run`'s `cost_unknown=True` path (the
-- dispatched-settlement seam) correctly poisons the PARENT to NULL, but the
-- CHILD row it settled kept whatever provisional cost `record_run` had
-- written for it (0.0) — so `list_recent_runs` (`GET /api/routines/runs`)
-- and `list_runs` (`GET /api/routines/{id}/runs`) still served an exact
-- $0.00 for that run, and the dashboard's RecentRunsPanel rendered it as
-- one, straight from the audit trail this time instead of the rollup.
--
-- This migration only changes the CONSTRAINT: routine_runs.cost_usd becomes
-- nullable, NULL = "this run's true cost was never reported" — the same
-- semantic migration 119 gave the parent. The DEFAULT stays 0, unchanged:
-- `record_run`'s provisional placeholder for a run that has fired but not
-- yet settled is a genuinely known, exact zero (no cost has accrued yet),
-- not an unknown one. `omniagentos/scheduler/store.py`'s `record_run` and
-- `settle_run` are updated in the same change (Sol review) to write NULL
-- here — never a manufactured 0.0 — whenever the run's own cost is unknown:
-- `record_run` when the public POST /routines/{id}/runs route omits
-- `cost_usd` (the request model no longer defaults it to 0.0 either), and
-- `settle_run` when `cost_unknown=True`.
--
-- DELIBERATELY NO BACKFILL, same rationale as 119 (and 103/104 before it):
-- an existing 0 predating this migration is indistinguishable between
-- "genuinely free" and "quietly undercounted", so historical rows keep
-- exactly the value they already have.
--
-- SQLite cannot drop a NOT NULL constraint in place (ALTER TABLE has no DROP
-- CONSTRAINT). Nothing declares a foreign key onto routine_runs.id, so —
-- unlike 119, whose rebuild of `routines` forced `routine_runs` to be
-- rebuilt too (its own FK points AT routines) — this migration only needs
-- to rebuild routine_runs itself. AUTOINCREMENT and both existing indexes
-- are preserved by the same copy-drop-rename sequence 089/091/119 already
-- established (see tests/db/test_migration_120_routine_runs_cost_usd_nullable.py
-- for the explicit id-continuity and index-survival assertions).
--
-- A separate migration rather than amending 119 in place: 119 was already
-- committed to this lane's own history by the time this gap was found in
-- review, and rewriting an already-committed migration's content requires
-- either an interactive rebase or a forced history rewrite — both outside
-- what this lane is permitted to do to its own git history. A forward-only
-- migration is the documented-reconciliation alternative the review itself
-- offered, and it leaves 119 internally consistent: 119 never claimed
-- routine_runs.cost_usd was nullable, only routines.total_cost_usd.
--
-- ORDINAL: 120, computed from the filesystem (highest prefix on disk was 119
-- immediately before this file was written).

CREATE TABLE routine_runs_120 (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    routine_id            TEXT NOT NULL REFERENCES routines(id),
    run_id                TEXT,
    iteration             INTEGER NOT NULL DEFAULT 1,
    gate_passed           INTEGER,
    accepted              INTEGER,
    cost_usd              REAL DEFAULT 0,
    stop_reason           TEXT DEFAULT '',
    notes                 TEXT DEFAULT '',
    started_at            TEXT,
    finished_at           TEXT,
    created_at            TEXT NOT NULL,
    outcome_class         TEXT,
    self_reported_status  TEXT
);

INSERT INTO routine_runs_120 (
    id, routine_id, run_id, iteration, gate_passed, accepted, cost_usd,
    stop_reason, notes, started_at, finished_at, created_at, outcome_class,
    self_reported_status
)
SELECT
    id, routine_id, run_id, iteration, gate_passed, accepted, cost_usd,
    stop_reason, notes, started_at, finished_at, created_at, outcome_class,
    self_reported_status
FROM routine_runs;

DROP TABLE routine_runs;
ALTER TABLE routine_runs_120 RENAME TO routine_runs;

CREATE INDEX idx_routine_runs_routine_created ON routine_runs(routine_id, created_at);
CREATE INDEX IF NOT EXISTS idx_routine_runs_outcome
    ON routine_runs(routine_id, outcome_class, created_at);
