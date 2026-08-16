-- NSC-C17-05: complete, comparable phase baseline receipts.
CREATE TABLE northstar_phase_baselines (
  id                 INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id             TEXT NOT NULL,
  baseline_key       TEXT NOT NULL,
  total_seconds      REAL NOT NULL CHECK(total_seconds >= 0),
  phase_seconds_json TEXT NOT NULL,
  anchors_json       TEXT NOT NULL,
  created_at         TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
  UNIQUE(run_id, baseline_key)
);
CREATE INDEX idx_northstar_phase_baselines_key
  ON northstar_phase_baselines(baseline_key, created_at);

