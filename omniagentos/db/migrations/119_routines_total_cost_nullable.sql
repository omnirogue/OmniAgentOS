-- ISSUE-8: routines.total_cost_usd stop claiming a precise $0.00 when the
-- true cost is unknown.
--
-- routines.total_cost_usd was `NOT NULL DEFAULT 0` (migration 015) and is
-- structurally incapable of expressing "we don't know". `record_run` /
-- `settle_run` (omniagentos/scheduler/store.py) accumulate it as a running
-- sum, and the one production settlement caller
-- (omniagentos/scheduler/routines_settle.py) already has an honest signal for
-- this: a dispatched run's `runs.cost_usd` (nullable since migration 001) is
-- NULL whenever the provider never reported a cost. That NULL used to be
-- folded into a zero-delta ("add nothing"), which is indistinguishable in the
-- data from a genuinely free run. Live proof at the time this was found: four
-- of six routines in production, across 1,415 accepted runs, have never once
-- had a reported cost, and the dashboard's Cost column showed "$0.00" for
-- every one of them — "we don't know" rendered as "it was free".
--
-- This migration only changes the CONSTRAINT: total_cost_usd becomes
-- nullable, NULL = "at least one contributing run's cost is unknown, so this
-- total cannot be trusted". The DEFAULT stays 0, unchanged, on purpose: a
-- brand-new routine that has fired zero times has a genuinely exact zero
-- cost, which is a different fact from "unknown" and must keep rendering as
-- $0.00. `omniagentos/scheduler/store.py`'s record_run/settle_run are updated
-- in the same change to write NULL (and keep it NULL on every subsequent
-- rollup, once poisoned) instead of silently continuing to add a zero delta.
--
-- DELIBERATELY NO BACKFILL. Existing total_cost_usd values are, right now,
-- indistinguishable in the data between "genuinely zero" and "quietly
-- undercounted by every run whose cost went unreported before this fix
-- existed" — exactly the situation migrations 103 and 104 refused to guess
-- at for `outcome_class` / `self_reported_status`, for the same reason: a
-- guess written into an audit trail (or, here, a cost rollup) is worse than a
-- gap. Historical rows keep whatever total they already have; only cost
-- reported as unknown FROM HERE ON poisons the rollup to NULL.
--
-- SQLite cannot drop a NOT NULL constraint in place (ALTER TABLE has no DROP
-- CONSTRAINT). routine_runs holds a FK to routines, so the child is rebuilt
-- first, then the parent, then indexes — same pattern as migrations 089 and
-- 091, whose accumulated column history this rebuild's column list mirrors
-- exactly (verified against a fully-migrated database's `PRAGMA table_info`).
-- ORDINAL: 119, computed from the filesystem (highest prefix on disk was 118
-- immediately before this file was written).

CREATE TABLE routines_119 (
    id                        TEXT PRIMARY KEY,
    name                      TEXT NOT NULL UNIQUE,
    description               TEXT DEFAULT '',
    trigger_type              TEXT NOT NULL CHECK (trigger_type IN ('cron', 'event')),
    trigger_config_json       TEXT NOT NULL DEFAULT '{}',
    task_template_json        TEXT NOT NULL DEFAULT '{}',
    gate_type                 TEXT NOT NULL CHECK (
        gate_type IN ('exit_code', 'test_command', 'metric_threshold', 'merge_candidate')
    ),
    gate_config_json          TEXT NOT NULL DEFAULT '{}',
    hard_cap_type             TEXT NOT NULL CHECK (
        hard_cap_type IN ('max_iterations', 'budget_usd', 'human_checkpoint')
    ),
    hard_cap_value            REAL NOT NULL CHECK (hard_cap_value > 0),
    notification_target_json  TEXT NOT NULL DEFAULT '{}',
    status                    TEXT NOT NULL DEFAULT 'active' CHECK (
        status IN ('active', 'disabled', 'auto_paused')
    ),
    auto_pause_reason         TEXT DEFAULT '',
    total_runs                INTEGER NOT NULL DEFAULT 0,
    accepted_runs             INTEGER NOT NULL DEFAULT 0,
    acceptance_rate           REAL,
    total_cost_usd            REAL DEFAULT 0,
    cost_per_accepted_change  REAL,
    created_at                TEXT NOT NULL,
    updated_at                TEXT NOT NULL,
    last_fired                TEXT,
    scope                     TEXT,
    purpose                   TEXT,
    revision                  INTEGER NOT NULL DEFAULT 0,
    neutral_runs              INTEGER NOT NULL DEFAULT 0,
    project_id                TEXT REFERENCES projects(id)
);

INSERT INTO routines_119 (
    id, name, description, trigger_type, trigger_config_json, task_template_json,
    gate_type, gate_config_json, hard_cap_type, hard_cap_value, notification_target_json,
    status, auto_pause_reason, total_runs, accepted_runs, acceptance_rate, total_cost_usd,
    cost_per_accepted_change, created_at, updated_at, last_fired, scope, purpose, revision,
    neutral_runs, project_id
)
SELECT
    id, name, description, trigger_type, trigger_config_json, task_template_json,
    gate_type, gate_config_json, hard_cap_type, hard_cap_value, notification_target_json,
    status, auto_pause_reason, total_runs, accepted_runs, acceptance_rate, total_cost_usd,
    cost_per_accepted_change, created_at, updated_at, last_fired, scope, purpose, revision,
    neutral_runs, project_id
FROM routines;

CREATE TABLE routine_runs_119 (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    routine_id            TEXT NOT NULL REFERENCES routines_119(id),
    run_id                TEXT,
    iteration             INTEGER NOT NULL DEFAULT 1,
    gate_passed           INTEGER,
    accepted              INTEGER,
    cost_usd              REAL NOT NULL DEFAULT 0,
    stop_reason           TEXT DEFAULT '',
    notes                 TEXT DEFAULT '',
    started_at            TEXT,
    finished_at           TEXT,
    created_at            TEXT NOT NULL,
    outcome_class         TEXT,
    self_reported_status  TEXT
);

INSERT INTO routine_runs_119 (
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
DROP TABLE routines;
ALTER TABLE routines_119 RENAME TO routines;
ALTER TABLE routine_runs_119 RENAME TO routine_runs;

CREATE INDEX idx_routines_status ON routines(status);
CREATE INDEX IF NOT EXISTS idx_routines_project ON routines(project_id, status);
CREATE INDEX idx_routine_runs_routine_created ON routine_runs(routine_id, created_at);
CREATE INDEX IF NOT EXISTS idx_routine_runs_outcome
    ON routine_runs(routine_id, outcome_class, created_at);
