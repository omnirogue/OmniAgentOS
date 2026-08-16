-- Board task archive support (additive migration 035): add support for archiving
-- completed board tasks with automatic 7-day retention purge.
--
-- Archived cards are hidden from the live board by default (archived_at IS NULL).
-- The retention routine deletes rows older than the cutoff window to keep the
-- active board lean. Hand-created cards with no archived_at remain visible.

ALTER TABLE board_tasks ADD COLUMN archived_at TEXT;

CREATE INDEX idx_board_archived ON board_tasks(archived_at, created_at);
