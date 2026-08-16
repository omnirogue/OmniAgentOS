-- NSC-C02-05: measured request-to-ready provisioning intervals.
CREATE TABLE provisioning_latency_events (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  attempt_id    TEXT NOT NULL,
  resource_kind TEXT NOT NULL,
  requested_at  TEXT NOT NULL,
  ready_at      TEXT NOT NULL,
  latency_ms    INTEGER NOT NULL CHECK(latency_ms >= 0),
  outcome       TEXT NOT NULL CHECK(outcome IN ('ready', 'failed', 'timeout')),
  detail_json   TEXT NOT NULL DEFAULT '{}',
  created_at    TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);
CREATE INDEX idx_provisioning_latency_attempt
  ON provisioning_latency_events(attempt_id, requested_at);

