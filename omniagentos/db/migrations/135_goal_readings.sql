CREATE TABLE goal_readings (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    goal_id     TEXT NOT NULL REFERENCES goals(id),
    -- A TEXT cycle would store fine under SQLite's type affinity and then
    -- TypeError the sustain predicate mid-graduation; backstop the seam.
    cycle       INTEGER NOT NULL CHECK (typeof(cycle) = 'integer' AND cycle >= 0),
    -- NULL MEANS NO READING WAS CAPTURED THIS CYCLE (ABSENCE). NEVER COERCE IT TO 0.
    value       REAL,
    -- An absent value must always have met = 0; the store write seam enforces
    -- this, and this CHECK enforces it at the database layer too.
    met         INTEGER NOT NULL DEFAULT 0 CHECK (value IS NOT NULL OR met = 0),
    captured_at TEXT NOT NULL,
    -- One row per (goal, cycle): a retried write upserts rather than creating a
    -- duplicate row that could forge a sustained-consecutive streak.
    UNIQUE (goal_id, cycle)
);

CREATE INDEX IF NOT EXISTS idx_goal_readings_goal_cycle
    ON goal_readings(goal_id, cycle);
