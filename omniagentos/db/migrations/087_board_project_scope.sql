-- 087_board_project_scope.sql
-- Chat v2 (P0-7): board cards gain a real project scope so /api/board?project_id=
-- filters server-side and chat companions/spawns inherit their chat's project.
-- Pre-087 cards with no run and no chat stay NULL on purpose (no heuristic
-- title backfill — honesty over completeness); scoped views disclose this.

ALTER TABLE board_tasks ADD COLUMN project_id TEXT REFERENCES projects(id);
CREATE INDEX IF NOT EXISTS idx_board_tasks_project ON board_tasks(project_id);

-- Backfill 1: runner-lane cards via the only link that exists today
-- (board_tasks.run_id → runs.task_id → tasks.project_id).
UPDATE board_tasks SET project_id = (
  SELECT t.project_id FROM runs r JOIN tasks t ON t.id = r.task_id
  WHERE r.id = board_tasks.run_id
) WHERE run_id IS NOT NULL AND project_id IS NULL;

-- Backfill 2: chat companions (and their spawned sub-tasks are handled at
-- write time from now on) inherit their chat's project.
UPDATE board_tasks SET project_id = (
  SELECT c.project_id FROM chats c WHERE c.board_task_id = board_tasks.id
) WHERE origin = 'chat' AND project_id IS NULL;
