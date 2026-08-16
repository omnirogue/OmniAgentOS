CREATE TABLE events (
  ts REAL NOT NULL,
  session_id TEXT,
  kind TEXT NOT NULL CHECK(kind IN ('notification', 'permission', 'rate_limit', 'spawn', 'error', 'health', 'completion')),
  detail TEXT,
  FOREIGN KEY (session_id) REFERENCES sessions(session_id)
);
CREATE INDEX idx_events_ts ON events(ts);
CREATE INDEX idx_events_session ON events(session_id);
CREATE INDEX idx_events_kind ON events(kind);
CREATE TABLE sessions (
  session_id TEXT PRIMARY KEY,
  cli TEXT NOT NULL CHECK(cli IN ('claude', 'codex', 'kimi', 'gemini', 'grok')),
  account TEXT,
  run_id TEXT,
  parent_session_id TEXT,
  lane TEXT,
  cwd TEXT,
  git_branch TEXT,
  models TEXT,
  start_ts REAL,
  end_ts REAL,
  wall_s REAL,
  active_s REAL,
  human_s REAL,
  n_err INTEGER DEFAULT 0,
  n_compact INTEGER DEFAULT 0,
  tokens_in INTEGER,
  tokens_out INTEGER,
  tokens_cached INTEGER,
  cost_usd REAL,
  rate_limit_max_pct REAL,
  outcome TEXT CHECK(outcome IN ('success', 'success?', 'failed', 'failed?', 'cancelled', 'unknown', 'unknown?')),
  outcome_note TEXT,
  capture_method TEXT DEFAULT NULL,
  created_ts REAL NOT NULL
);
CREATE INDEX idx_sessions_cli ON sessions(cli);
CREATE INDEX idx_sessions_run_id ON sessions(run_id);
CREATE INDEX idx_sessions_parent ON sessions(parent_session_id);
CREATE INDEX idx_sessions_start_ts ON sessions(start_ts);
CREATE INDEX idx_sessions_outcome ON sessions(outcome);
CREATE VIEW lane_health AS
SELECT
  cli,
  COUNT(DISTINCT session_id) AS active_sessions,
  COALESCE(MAX(COALESCE(end_ts, start_ts)), 0) AS last_activity_ts,
  CASE
    WHEN MAX(COALESCE(end_ts, start_ts)) IS NULL THEN 'DOWN'
    WHEN MAX(COALESCE(end_ts, start_ts)) < strftime('%s', 'now') - 1800 THEN 'STALE'
    ELSE 'UP'
  END AS status
FROM sessions
GROUP BY cli;
