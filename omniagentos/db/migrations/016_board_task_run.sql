-- Intake loop (additive migration 016): link a self-assignable board_task to the
-- control-plane run that actually executes it.
--
-- A task dispatched from chat becomes BOTH a board_task (the live kanban card the
-- operator watches) AND a control-plane task+run (the fenced execution the runner
-- performs). This nullable column is the bridge: the live board reconciles each
-- card's column (To-Do / In-Progress / Done) from its linked run's state.
--
-- Existing rows are untouched: a NULL board_tasks.run_id means "no linked run"
-- (a hand-created board card), which the reconciler leaves exactly as-is.
ALTER TABLE board_tasks ADD COLUMN run_id TEXT;

CREATE INDEX IF NOT EXISTS idx_board_run ON board_tasks(run_id);
