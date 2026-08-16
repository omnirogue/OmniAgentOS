-- Setpoint contract for goals.target_json (existing column, JSON):
-- {
--   "metric_source": "<registered metric source id>",
--   "comparator": ">=" | "<=" | "==",
--   "target": <number>,
--   "sustain": {"periods": <int>, "window": "<duration string, e.g. '1h'>"},
--   "effort": {
--     "max_cycles": <int|null>,
--     "max_work_items": <int|null>,
--     "budget_usd": <number|null>,
--     "deadline": "<ISO8601|null>"
--   },
--   "hold_mode": "<string, e.g. 'freeze' | 'coast'>",
--   "preconditions": [<string, ...>]
-- }

ALTER TABLE goals ADD COLUMN parent_goal_id TEXT;
ALTER TABLE goals ADD COLUMN routine_id TEXT;
ALTER TABLE goals ADD COLUMN origin TEXT;
ALTER TABLE goals ADD COLUMN graduated_at TEXT;
ALTER TABLE goals ADD COLUMN blocked_reason TEXT;

CREATE INDEX IF NOT EXISTS idx_goals_parent ON goals(parent_goal_id);
