-- sessions.cost_usd: stop claiming $0.00 when the provider never reported cost.
--
-- Migration 011 declared `cost_usd REAL NOT NULL DEFAULT 0`. Every session row
-- therefore starts as a measured zero, and providers that report tokens but no
-- dollar figure (codex exec --json) leave that seed in place because
-- record_session_usage skips None fields. A NULL is honest missing data; a
-- zero is a claim. The budget accumulator already treats missing cost as 0.0
-- (`float(session.get("cost_usd") or 0.0)`), so making the column nullable
-- corrects telemetry without changing enforcement.
--
-- SQLite cannot DROP NOT NULL in place. session_messages holds a FK to
-- sessions, so the child is rebuilt first, then the parent, then indexes.
-- Existing 0.0 values are preserved (some are real reported zeros); only the
-- constraint and default change. Application code clears the seed to NULL when
-- it records a tokens-only usage source.

CREATE TABLE sessions_088 (
    id                 TEXT PRIMARY KEY,
    source             TEXT NOT NULL CHECK (source IN ('bridge', 'external')),
    project_dir        TEXT NOT NULL,
    provider           TEXT NOT NULL DEFAULT 'claude',
    session_ref        TEXT,
    state              TEXT NOT NULL DEFAULT 'starting' CHECK (
        state IN (
            'starting', 'running', 'awaiting_approval', 'resuming',
            'completed', 'failed', 'cancelled', 'killed'
        )
    ),
    pid                INTEGER,
    model              TEXT,
    title              TEXT,
    budget_usd_max     REAL,
    cost_usd           REAL DEFAULT NULL,
    kill_requested     INTEGER NOT NULL DEFAULT 0 CHECK (kill_requested IN (0, 1)),
    last_activity_at   TEXT,
    created_at         TEXT NOT NULL,
    updated_at         TEXT NOT NULL,
    killed_by          TEXT,
    todos_json         TEXT,
    files_json         TEXT,
    granted_roots      TEXT,
    error              TEXT,
    prompt             TEXT,
    account_id         TEXT,
    output_text        TEXT,
    idle_minutes       REAL,
    effort             TEXT,
    input_tokens       INTEGER,
    output_tokens      INTEGER,
    wall_ms            INTEGER,
    usage_source       TEXT,
    scope_json         TEXT
);

INSERT INTO sessions_088 (
    id, source, project_dir, provider, session_ref, state, pid, model, title,
    budget_usd_max, cost_usd, kill_requested, last_activity_at, created_at,
    updated_at, killed_by, todos_json, files_json, granted_roots, error, prompt,
    account_id, output_text, idle_minutes, effort, input_tokens, output_tokens,
    wall_ms, usage_source, scope_json
)
SELECT
    id, source, project_dir, provider, session_ref, state, pid, model, title,
    budget_usd_max, cost_usd, kill_requested, last_activity_at, created_at,
    updated_at, killed_by, todos_json, files_json, granted_roots, error, prompt,
    account_id, output_text, idle_minutes, effort, input_tokens, output_tokens,
    wall_ms, usage_source, scope_json
FROM sessions;

CREATE TABLE session_messages_088 (
    id         TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    message    TEXT NOT NULL,
    queued_at  TEXT NOT NULL,
    applied_at TEXT,
    created_by TEXT,
    FOREIGN KEY (session_id) REFERENCES sessions_088(id)
);

INSERT INTO session_messages_088 (
    id, session_id, message, queued_at, applied_at, created_by
)
SELECT id, session_id, message, queued_at, applied_at, created_by
FROM session_messages;

DROP TABLE session_messages;
DROP TABLE sessions;
ALTER TABLE sessions_088 RENAME TO sessions;
ALTER TABLE session_messages_088 RENAME TO session_messages;

CREATE INDEX idx_sessions_state ON sessions(state);
CREATE INDEX idx_sessions_source ON sessions(source);
CREATE INDEX idx_sessions_account_state ON sessions(account_id, state);
CREATE INDEX IF NOT EXISTS idx_sessions_created ON sessions(created_at DESC, id DESC);
CREATE INDEX idx_session_messages_pending ON session_messages(session_id, applied_at, queued_at);
