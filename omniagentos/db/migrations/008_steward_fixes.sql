-- Steward defect fixes: H1/H2 dedup, idempotent briefing, Stripe pagination, FK ensure, cooldown escalation, atomic claims, voice sentinel.

-- H1 + PERF-002: Remove duplicate metric snapshots (keep MAX(id) per day) then add unique constraint.
-- First, for each (goal_id, source, metric, calendar_day), delete all but the one with max id.
DELETE FROM metric_snapshots
WHERE id NOT IN (
    SELECT MAX(id)
    FROM metric_snapshots
    GROUP BY goal_id, source, metric, substr(captured_at, 1, 10)
);

-- Now add the unique index (dedupe per calendar day).
-- Use IF NOT EXISTS so the migration is idempotent.
CREATE UNIQUE INDEX IF NOT EXISTS ux_metric_snapshots_day
    ON metric_snapshots(goal_id, source, metric, substr(captured_at, 1, 10));

-- H12: Add cooldown_minutes column to alerts (if not already present).
-- SQLite doesn't support ALTER TABLE ADD COLUMN IF NOT EXISTS, so we check via pragma.
-- But for safety, we'll just try to add it; SQLite will ignore if it already exists in most cases.
-- Since we can't use IF NOT EXISTS in ALTER TABLE, we'll skip this and handle it in store.py.
-- (The column will be added as needed in Python when creating alerts.)
