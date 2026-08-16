-- W5: Routine builder ("loops with teeth").
--
-- Every routine MUST declare a trigger, a task template, an OBJECTIVE gate
-- (never a model opinion), and a hard stop-condition on top of the gate. The
-- CHECK constraints below are a floor, not the ceiling — omniagentos.scheduler
-- .routines.validate_routine() enforces the same rules (plus config-shape
-- checks CHECK can't express) before a row ever reaches this table.

CREATE TABLE routines (
    id                        TEXT PRIMARY KEY,
    name                      TEXT NOT NULL UNIQUE,
    description               TEXT DEFAULT '',

    -- Trigger: cron schedule or event name. trigger_config_json carries the
    -- concrete cron expression / event type + filter.
    trigger_type              TEXT NOT NULL CHECK (trigger_type IN ('cron', 'event')),
    trigger_config_json       TEXT NOT NULL DEFAULT '{}',

    -- Task template instantiated on each firing (discipline, title, input,
    -- acceptance criteria template).
    task_template_json        TEXT NOT NULL DEFAULT '{}',

    -- Objective gate: goal-met is decided by a gate, never a model opinion.
    gate_type                 TEXT NOT NULL CHECK (gate_type IN ('exit_code', 'test_command', 'metric_threshold')),
    gate_config_json          TEXT NOT NULL DEFAULT '{}',

    -- Hard stop-condition #2 (on top of the gate): max iterations, a budget
    -- ceiling, or a mandatory human checkpoint.
    hard_cap_type             TEXT NOT NULL CHECK (hard_cap_type IN ('max_iterations', 'budget_usd', 'human_checkpoint')),
    hard_cap_value            REAL NOT NULL CHECK (hard_cap_value > 0),

    -- Where results/failures/auto-pause notices are sent.
    notification_target_json  TEXT NOT NULL DEFAULT '{}',

    status                    TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'disabled', 'auto_paused')),
    auto_pause_reason         TEXT DEFAULT '',

    -- Denormalized rollups fed by routine_runs, kept current on every
    -- record_run() call so the auto-pause check and the dashboard list don't
    -- need to aggregate routine_runs on every read.
    total_runs                INTEGER NOT NULL DEFAULT 0,
    accepted_runs             INTEGER NOT NULL DEFAULT 0,
    acceptance_rate           REAL,
    total_cost_usd            REAL NOT NULL DEFAULT 0,
    cost_per_accepted_change  REAL,

    created_at                TEXT NOT NULL,
    updated_at                TEXT NOT NULL
);
CREATE INDEX idx_routines_status ON routines(status);

-- One row per routine firing/iteration. Feeds the routines rollup columns
-- above (cost_per_accepted_change, acceptance_rate) and is the audit trail
-- an operator can read to see why a routine auto-paused.
CREATE TABLE routine_runs (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    routine_id    TEXT NOT NULL REFERENCES routines(id),
    run_id        TEXT,
    iteration     INTEGER NOT NULL DEFAULT 1,
    -- gate_passed: raw objective-gate result. accepted: whether the change
    -- counts toward acceptance rate (normally == gate_passed, but a human
    -- checkpoint rejection can mark a gate-passing run as not-accepted).
    gate_passed   INTEGER,
    accepted      INTEGER,
    cost_usd      REAL NOT NULL DEFAULT 0,
    stop_reason   TEXT DEFAULT '',
    notes         TEXT DEFAULT '',
    started_at    TEXT,
    finished_at   TEXT,
    created_at    TEXT NOT NULL
);
CREATE INDEX idx_routine_runs_routine_created ON routine_runs(routine_id, created_at);
