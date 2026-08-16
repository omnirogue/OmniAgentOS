-- W1 Session Bridge core (IF-1): durable state for supervised and observed
-- interactive sessions. This migration is additive; existing run machinery is
-- deliberately untouched.

CREATE TABLE sessions (
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
    cost_usd           REAL NOT NULL DEFAULT 0,
    kill_requested     INTEGER NOT NULL DEFAULT 0 CHECK (kill_requested IN (0, 1)),
    last_activity_at   TEXT,
    created_at         TEXT NOT NULL,
    updated_at         TEXT NOT NULL
);

CREATE INDEX idx_sessions_state ON sessions(state);
CREATE INDEX idx_sessions_source ON sessions(source);
