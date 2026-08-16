-- NSC-C31-01/02: auditable Pareto reports retaining every comparison axis.
CREATE TABLE allocation_frontier_reports (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  report_key TEXT NOT NULL UNIQUE,
  created_at TEXT NOT NULL
);

CREATE TABLE allocation_frontier_points (
  report_id            INTEGER NOT NULL REFERENCES allocation_frontier_reports(id),
  config_id            TEXT NOT NULL,
  formation_selections INTEGER NOT NULL CHECK(formation_selections >= 0),
  swarm_attempts       INTEGER NOT NULL CHECK(swarm_attempts >= 0),
  session_cost_usd     REAL NOT NULL CHECK(session_cost_usd >= 0),
  completion_seconds   REAL NOT NULL CHECK(completion_seconds >= 0),
  quality_score        REAL NOT NULL,
  dominated            INTEGER NOT NULL CHECK(dominated IN (0, 1)),
  PRIMARY KEY(report_id, config_id)
);
CREATE INDEX idx_allocation_frontier_points_status
  ON allocation_frontier_points(report_id, dominated);

