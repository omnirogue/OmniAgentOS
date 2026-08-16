-- Allow merge_candidate as a routines.gate_type so the scheduler can produce
-- the signed, candidate-bound receipt that scripts/merge-gate.sh consumes.
--
-- SQLite cannot ALTER a CHECK constraint in place. Rebuild routines with the
-- expanded enum. routine_runs holds a FK to routines, and migrate() applies
-- each migration under an already-open BEGIN IMMEDIATE — SQLite therefore
-- ignores PRAGMA foreign_keys=OFF inside that transaction. Rebuild the child
-- first (FK points at the new parent name), then drop child + parent, then
-- rename — same pattern as migration 089.

CREATE TABLE routines_091 (
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
    total_cost_usd            REAL NOT NULL DEFAULT 0,
    cost_per_accepted_change  REAL,
    created_at                TEXT NOT NULL,
    updated_at                TEXT NOT NULL,
    last_fired                TEXT
);

INSERT INTO routines_091 (
    id, name, description, trigger_type, trigger_config_json, task_template_json,
    gate_type, gate_config_json, hard_cap_type, hard_cap_value, notification_target_json,
    status, auto_pause_reason, total_runs, accepted_runs, acceptance_rate, total_cost_usd,
    cost_per_accepted_change, created_at, updated_at, last_fired
)
SELECT
    id, name, description, trigger_type, trigger_config_json, task_template_json,
    gate_type, gate_config_json, hard_cap_type, hard_cap_value, notification_target_json,
    status, auto_pause_reason, total_runs, accepted_runs, acceptance_rate, total_cost_usd,
    cost_per_accepted_change, created_at, updated_at, last_fired
FROM routines;

CREATE TABLE routine_runs_091 (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    routine_id    TEXT NOT NULL REFERENCES routines_091(id),
    run_id        TEXT,
    iteration     INTEGER NOT NULL DEFAULT 1,
    gate_passed   INTEGER,
    accepted      INTEGER,
    cost_usd      REAL NOT NULL DEFAULT 0,
    stop_reason   TEXT DEFAULT '',
    notes         TEXT DEFAULT '',
    started_at    TEXT,
    finished_at   TEXT,
    created_at    TEXT NOT NULL
);

INSERT INTO routine_runs_091 (
    id, routine_id, run_id, iteration, gate_passed, accepted, cost_usd,
    stop_reason, notes, started_at, finished_at, created_at
)
SELECT
    id, routine_id, run_id, iteration, gate_passed, accepted, cost_usd,
    stop_reason, notes, started_at, finished_at, created_at
FROM routine_runs;

DROP TABLE routine_runs;
DROP TABLE routines;
ALTER TABLE routines_091 RENAME TO routines;
ALTER TABLE routine_runs_091 RENAME TO routine_runs;

CREATE INDEX idx_routines_status ON routines(status);
CREATE INDEX idx_routine_runs_routine_created ON routine_runs(routine_id, created_at);
