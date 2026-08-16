-- OmniAgentOS SQLite schema v1 (FROZEN, Wave 0).
-- p01-db copies this verbatim into omniagentos/db/migrations/001_init.sql and
-- builds the migrations runner + DAL around it. Schema changes go through the lead.
--
-- Connection pragmas (set by the DAL on every connection, not in DDL):
--   PRAGMA journal_mode=WAL; PRAGMA busy_timeout=5000; PRAGMA foreign_keys=ON;
--   PRAGMA synchronous=NORMAL;
-- Multi-process discipline: writers use BEGIN IMMEDIATE and keep transactions short.
-- All timestamps are UTC ISO-8601 strings "YYYY-MM-DDTHH:MM:SSZ" (contracts.utc_now_iso).

CREATE TABLE schema_migrations (
    version     INTEGER PRIMARY KEY,
    applied_at  TEXT NOT NULL
);

CREATE TABLE disciplines (
    id              TEXT PRIMARY KEY,           -- slug, e.g. 'research-briefs'
    name            TEXT NOT NULL,
    metric_contract TEXT NOT NULL DEFAULT '{}', -- JSON
    status          TEXT NOT NULL DEFAULT 'active',
    created_at      TEXT NOT NULL
);

CREATE TABLE tasks (
    id              TEXT PRIMARY KEY,           -- 'tsk_' + ulid/uuid
    discipline_id   TEXT REFERENCES disciplines(id),
    title           TEXT NOT NULL,
    input_json      TEXT NOT NULL DEFAULT '{}',
    acceptance_json TEXT NOT NULL DEFAULT '{}',
    state           TEXT NOT NULL DEFAULT 'draft',  -- contracts.TaskState
    risk            TEXT NOT NULL DEFAULT 'low',
    deadline        TEXT,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);
CREATE INDEX idx_tasks_state ON tasks(state);

CREATE TABLE runs (
    id               TEXT PRIMARY KEY,          -- 'run_' + ulid/uuid
    task_id          TEXT NOT NULL REFERENCES tasks(id),
    discipline_id    TEXT REFERENCES disciplines(id),
    arm              TEXT,                      -- contracts.Arm or NULL
    harness          TEXT NOT NULL,             -- contracts.HarnessType
    harness_version  TEXT NOT NULL DEFAULT '',
    env_hash         TEXT NOT NULL DEFAULT '',
    harness_params   TEXT NOT NULL DEFAULT '{}',-- JSON
    agent            TEXT,
    model            TEXT,
    attempt          INTEGER NOT NULL DEFAULT 1,
    state            TEXT NOT NULL DEFAULT 'queued', -- contracts.RunState
    worker_id        TEXT,                      -- claiming worker; NULL when unclaimed
    plan_json        TEXT NOT NULL DEFAULT '[]',-- ordered step specs
    output_text      TEXT,
    output_json      TEXT,
    error            TEXT,
    wall_ms          INTEGER,
    turns            INTEGER,
    input_tokens     INTEGER,
    output_tokens    INTEGER,
    cost_usd         REAL,
    usage_estimated  INTEGER NOT NULL DEFAULT 1,   -- bool: any usage field estimated
    usage_source     TEXT NOT NULL DEFAULT 'estimator',
    budget_json      TEXT NOT NULL DEFAULT '{}',   -- contracts.BudgetSpec
    cancel_requested INTEGER NOT NULL DEFAULT 0,   -- set by API; runner honors between steps AND while parked
    trace_id         TEXT NOT NULL,
    session_ref      TEXT,                      -- provider resume handle
    manifest_path    TEXT,                      -- JSONL ledger file holding this run's line
    vault_note_path  TEXT,
    queued_at        TEXT NOT NULL,
    started_at       TEXT,
    finished_at      TEXT,
    created_at       TEXT NOT NULL,
    updated_at       TEXT NOT NULL
);
CREATE INDEX idx_runs_state ON runs(state);
CREATE INDEX idx_runs_task ON runs(task_id);
CREATE INDEX idx_runs_worker ON runs(worker_id);

CREATE TABLE steps (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id          TEXT NOT NULL REFERENCES runs(id),
    seq             INTEGER NOT NULL,
    name            TEXT NOT NULL,
    action_class    TEXT NOT NULL DEFAULT 'sandboxed_creation', -- contracts.ActionClass
    status          TEXT NOT NULL DEFAULT 'pending',            -- contracts.StepStatus
    checkpoint_json TEXT NOT NULL DEFAULT '{}',  -- written BEFORE executing
    result_json     TEXT,                        -- written AFTER completing
    error           TEXT,
    idempotency_key TEXT,                        -- for side-effecting steps
    started_at      TEXT,
    finished_at     TEXT,
    UNIQUE(run_id, seq)
);
CREATE INDEX idx_steps_run ON steps(run_id);

-- Side-effect receipts: key inserted BEFORE executing the effect, result recorded
-- after. A reclaimed step whose key exists WITH result_json → effect already ran,
-- skip re-execution. Key exists WITHOUT result_json → effect state unknown; the
-- runner must resolve per contracts/statemachine.md §idempotency before re-running.
CREATE TABLE idempotency (
    key         TEXT PRIMARY KEY,
    run_id      TEXT NOT NULL,
    step_name   TEXT NOT NULL,
    result_json TEXT,
    created_at  TEXT NOT NULL,
    completed_at TEXT
);

CREATE TABLE events (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,  -- SSE cursor
    ts           TEXT NOT NULL,
    type         TEXT NOT NULL,          -- contracts.Events.*
    actor        TEXT NOT NULL,          -- 'api' | 'runner:<worker>' | 'scheduler' | 'user'
    action       TEXT NOT NULL,
    target_type  TEXT NOT NULL DEFAULT '',
    target_id    TEXT NOT NULL DEFAULT '',
    payload_json TEXT NOT NULL DEFAULT '{}',
    trace_id     TEXT NOT NULL DEFAULT ''
);
CREATE INDEX idx_events_ts ON events(ts);
CREATE INDEX idx_events_type ON events(type);

CREATE TABLE approvals (
    id              TEXT PRIMARY KEY,     -- contracts.new_id('apr')
    run_id          TEXT REFERENCES runs(id),
    task_id         TEXT REFERENCES tasks(id),
    step_seq        INTEGER,              -- the step this approval gates (NULL = whole run); matching key is (run_id, step_seq)
    action_class    TEXT NOT NULL,        -- contracts.ActionClass
    proposed_action TEXT NOT NULL,        -- plain language
    params_json     TEXT NOT NULL DEFAULT '{}',
    risk            TEXT NOT NULL DEFAULT '',
    evidence        TEXT NOT NULL DEFAULT '',
    state           TEXT NOT NULL DEFAULT 'pending', -- contracts.ApprovalState
    decided_by      TEXT,
    decision_note   TEXT,
    decided_at      TEXT,
    expires_at      TEXT,
    created_at      TEXT NOT NULL
);
CREATE INDEX idx_approvals_state ON approvals(state);

CREATE TABLE artifacts (
    id          TEXT PRIMARY KEY,
    run_id      TEXT NOT NULL REFERENCES runs(id),
    type        TEXT NOT NULL DEFAULT 'file',
    uri         TEXT NOT NULL,
    sha256      TEXT NOT NULL DEFAULT '',
    bytes       INTEGER NOT NULL DEFAULT 0,
    created_at  TEXT NOT NULL
);
CREATE INDEX idx_artifacts_run ON artifacts(run_id);

CREATE TABLE budgets (
    id            TEXT PRIMARY KEY,       -- scope key, e.g. 'global', 'run:<id>'
    scope_type    TEXT NOT NULL,          -- 'global' | 'discipline' | 'task' | 'run'
    scope_id      TEXT NOT NULL DEFAULT '',
    wall_ms_max   INTEGER,
    tokens_max    INTEGER,
    cost_usd_max  REAL,
    used_wall_ms  INTEGER NOT NULL DEFAULT 0,
    used_tokens   INTEGER NOT NULL DEFAULT 0,
    used_cost_usd REAL NOT NULL DEFAULT 0,
    updated_at    TEXT NOT NULL
);

CREATE TABLE heartbeats (
    worker_id      TEXT PRIMARY KEY,
    pid            INTEGER NOT NULL,
    started_at     TEXT NOT NULL,
    last_beat_at   TEXT NOT NULL,
    current_run_id TEXT
);

-- Global pause: single row, id=1. paused=1 → runner claims nothing and stops
-- before the next step of in-flight runs (state preserved).
CREATE TABLE pause (
    id         INTEGER PRIMARY KEY CHECK (id = 1),
    paused     INTEGER NOT NULL DEFAULT 0,
    reason     TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL
);

-- Seeds
INSERT INTO pause (id, paused, reason, updated_at) VALUES (1, 0, '', '1970-01-01T00:00:00Z');
INSERT INTO disciplines (id, name, metric_contract, status, created_at) VALUES
  ('research-briefs', 'Research briefs', '{}', 'active', '1970-01-01T00:00:00Z'),
  ('code-changes', 'Bounded code changes', '{}', 'active', '1970-01-01T00:00:00Z');
