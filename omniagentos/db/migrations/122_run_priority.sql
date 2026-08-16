-- Priority-aware run scheduling. Lower values are claimed first:
-- 0=bottleneck, 1=fix, 2=normal, 3=background.
--
-- Existing rows deliberately inherit priority 2 so upgrading preserves the
-- historical FIFO queue as normal work. The claim query applies aging at read
-- time; this index narrows the candidate set and supplies its stable base order.
ALTER TABLE runs ADD COLUMN priority INTEGER NOT NULL DEFAULT 2
CHECK (priority BETWEEN 0 AND 3);

CREATE INDEX IF NOT EXISTS idx_runs_state_priority_queued
ON runs(state, priority, queued_at);
