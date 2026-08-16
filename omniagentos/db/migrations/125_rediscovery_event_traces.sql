-- NSC-C02-03: durable evidence that an attempt had to rediscover context.
CREATE TABLE rediscovery_event_traces (
  id              INTEGER PRIMARY KEY AUTOINCREMENT,
  attempt_id      TEXT NOT NULL,
  context_key     TEXT NOT NULL,
  first_seen_at   TEXT NOT NULL,
  rediscovered_at TEXT NOT NULL,
  source_digest   TEXT NOT NULL CHECK(length(source_digest) = 64),
  trace_json      TEXT NOT NULL,
  created_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
  UNIQUE(attempt_id, context_key, rediscovered_at)
);
CREATE INDEX idx_rediscovery_event_traces_attempt
  ON rediscovery_event_traces(attempt_id, rediscovered_at);

