-- Horizon 4 Steward foundation.

CREATE TABLE goals (
    id              TEXT PRIMARY KEY,
    name            TEXT NOT NULL UNIQUE,
    description     TEXT DEFAULT '',
    discipline_id   TEXT,
    north_star_json TEXT NOT NULL,
    target_json     TEXT DEFAULT '{}',
    keywords_json   TEXT DEFAULT '[]',
    priority        INTEGER DEFAULT 100,
    status          TEXT DEFAULT 'active',
    created_at      TEXT,
    updated_at      TEXT
);

CREATE TABLE metric_snapshots (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    goal_id     TEXT REFERENCES goals(id),
    source      TEXT,
    metric      TEXT,
    value       REAL,
    unit        TEXT DEFAULT '',
    window      TEXT DEFAULT 'day',
    captured_at TEXT,
    meta_json   TEXT DEFAULT '{}'
);
CREATE INDEX idx_metric_snapshots_goal_metric_captured
    ON metric_snapshots(goal_id, metric, captured_at);
CREATE INDEX idx_metric_snapshots_source_metric_captured
    ON metric_snapshots(source, metric, captured_at);

CREATE TABLE goal_facts (
    goal_id   TEXT,
    fact_id   INTEGER,
    linked_by TEXT,
    linked_at TEXT,
    PRIMARY KEY(goal_id, fact_id)
);

CREATE TABLE comms_messages (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    source           TEXT,
    external_id      TEXT,
    thread_id        TEXT DEFAULT '',
    sender           TEXT DEFAULT '',
    recipients_json  TEXT DEFAULT '[]',
    sent_at          TEXT,
    subject          TEXT DEFAULT '',
    body_text        TEXT,
    raw_json         TEXT,
    attachments_json TEXT DEFAULT '[]',
    kb_status        TEXT DEFAULT 'none',
    episode_id       INTEGER,
    created_at       TEXT,
    UNIQUE(source, external_id)
);
CREATE INDEX idx_comms_messages_source_sent ON comms_messages(source, sent_at);
CREATE INDEX idx_comms_messages_thread ON comms_messages(thread_id);
CREATE INDEX idx_comms_messages_kb_status ON comms_messages(kb_status);

CREATE TABLE comms_sources (
    name         TEXT PRIMARY KEY,
    kind         TEXT,
    status       TEXT DEFAULT 'pending_setup',
    config_json  TEXT DEFAULT '{}',
    last_poll_at TEXT,
    last_error   TEXT DEFAULT ''
);

CREATE TABLE alerts (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    rule          TEXT,
    severity      TEXT,
    title         TEXT,
    body          TEXT,
    evidence_json TEXT DEFAULT '{}',
    cooldown_key  TEXT,
    state         TEXT DEFAULT 'open',
    created_at    TEXT,
    acked_at      TEXT
);
CREATE INDEX idx_alerts_cooldown_created ON alerts(cooldown_key, created_at);
CREATE INDEX idx_alerts_state ON alerts(state);

CREATE TABLE suggestions (
    id                 TEXT PRIMARY KEY,
    title              TEXT,
    rationale          TEXT,
    evidence_json      TEXT DEFAULT '[]',
    proposed_plan_json TEXT,
    risk_class         TEXT,
    source             TEXT,
    goal_id            TEXT,
    alert_id           INTEGER,
    outcome_json       TEXT DEFAULT '{}',
    state              TEXT DEFAULT 'open',
    created_at         TEXT,
    decided_at         TEXT,
    decided_by         TEXT,
    task_id            TEXT,
    run_id             TEXT
);
CREATE INDEX idx_suggestions_state_created ON suggestions(state, created_at);

CREATE TABLE briefings (
    id                TEXT PRIMARY KEY,
    briefing_date     TEXT UNIQUE,
    vault_path        TEXT,
    summary_json      TEXT,
    deliveries_json   TEXT DEFAULT '[]',
    voice_artifact_id TEXT,
    acked_at          TEXT,
    created_at        TEXT
);
