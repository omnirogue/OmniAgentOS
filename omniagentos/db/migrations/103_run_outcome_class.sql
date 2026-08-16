-- Migration 103: the run-outcome taxonomy becomes DATA.
--
-- Before this migration a routine run had exactly two states in the schema —
-- accepted or not — so "parked awaiting a human", "idle, nothing to do" and
-- "blocked, cannot proceed" all collapsed into whichever boolean the caller
-- happened to pick, plus free-text prose in `notes`. That collapse is the
-- defect: a loop that parked every tick forever scored 100% acceptance and was
-- indistinguishable from a loop that was actually working.
--
-- Two columns, both additive:
--
-- routine_runs.outcome_class  'favourable' | 'neutral' | 'adverse' (NULL =
--     not judged yet, i.e. the run is still pending, or the row predates this
--     migration). Paired with the existing machine-readable `stop_reason`, an
--     operator can now ask "which of my loops are parked on a human, and which
--     are blocked on a dead credential" in SQL instead of by reading prose.
--
-- routines.neutral_runs       Count of settled runs classified neutral. The
--     acceptance denominator is (total_runs - neutral_runs); total_runs keeps
--     counting every firing because the max_iterations hard cap reads it.
--
-- Deliberately NOT backfilled. Historical rows recorded a park as
-- accepted=1/stop_reason='' — indistinguishable in the data from a genuine
-- completion — so any backfill would be a guess, and a guess written into the
-- audit trail is worse than a gap. Existing rollups therefore stay as they are
-- (favourably biased, so no routine is retroactively paused) and the honest
-- numbers accrue from the next tick onward.
--
-- No CHECK constraint on outcome_class: adding one would mean rebuilding
-- routine_runs (SQLite cannot ALTER a CHECK in place), and the enum is enforced
-- at the single write path in omniagentos/scheduler/store.py, which is the same
-- place that would have to be edited to defeat a CHECK anyway.

ALTER TABLE routine_runs ADD COLUMN outcome_class TEXT;

ALTER TABLE routines ADD COLUMN neutral_runs INTEGER NOT NULL DEFAULT 0;

-- The one question an operator asks of this table: "show me every run of this
-- routine that produced no judgeable result, newest first".
CREATE INDEX IF NOT EXISTS idx_routine_runs_outcome
    ON routine_runs(routine_id, outcome_class, created_at);
