-- 070_portfolio_board_indexes.sql
-- S14A / L-10 (project+archive portion): hot-path indexes for portfolio health
-- and board archive listing. Parent L14 owns 068/069; this satellite starts at 070.
--
-- Intentionally omits idx_runs_task_state: existing idx_runs_task already covers
-- the runs→tasks join key used by portfolio rollups.
--
-- Replaces the thinner idx_board_archived (035) with:
--   1. idx_board_tasks_archived_created: compound for active listing (archived_at IS NULL)
--   2. idx_board_tasks_archived_listing: PARTIAL for archived listing (archived_at IS NOT NULL)
-- The partial index is key: a regular compound index on (archived_at, created_at DESC, id DESC)
-- cannot provide ORDER BY when filtering by IS NOT NULL (range scan on first column).

DROP INDEX IF EXISTS idx_board_archived;

CREATE INDEX IF NOT EXISTS idx_board_tasks_status_run
    ON board_tasks(status, run_id);

CREATE INDEX IF NOT EXISTS idx_approvals_state_run
    ON approvals(state, run_id);

-- Active-card listing: WHERE archived_at IS NULL ORDER BY created_at DESC, id DESC
-- Works because archived_at=NULL is an equality match on the first column.
CREATE INDEX IF NOT EXISTS idx_board_tasks_archived_created
    ON board_tasks(archived_at, created_at DESC, id DESC);

-- Active by status: WHERE status = ? AND archived_at IS NULL ORDER BY ...
CREATE INDEX IF NOT EXISTS idx_board_tasks_status_archived
    ON board_tasks(status, archived_at, created_at DESC, id DESC);

-- PARTIAL index for archived listing: WHERE archived_at IS NOT NULL ORDER BY created_at DESC, id DESC
-- A regular index cannot serve ORDER BY after an IS NOT NULL filter (range scan).
-- The partial index covers only archived rows, pre-sorted by (created_at DESC, id DESC).
CREATE INDEX IF NOT EXISTS idx_board_tasks_archived_listing
    ON board_tasks(created_at DESC, id DESC)
    WHERE archived_at IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_projects_created_id
    ON projects(created_at DESC, id DESC);
