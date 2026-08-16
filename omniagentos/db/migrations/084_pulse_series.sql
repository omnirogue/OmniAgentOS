-- 084: pulse_series — daily snapshot time-series backing the Observatory /pulse page.
-- One row per (metric, date). Upserted daily by the pulse aggregator so trend
-- charts on /pulse can read a cheap time-series instead of recomputing from
-- live tables. metric is a dotted namespace, e.g. skills.total, loops.fires.

CREATE TABLE pulse_series (
  metric TEXT    NOT NULL,
  date   TEXT    NOT NULL,
  value  REAL    NOT NULL DEFAULT 0,
  PRIMARY KEY (metric, date)
);

CREATE INDEX idx_pulse_series_date ON pulse_series(date DESC);
