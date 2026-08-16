-- Swarm run params (m6, merge-model Phase 2): durable per-run execution
-- parameters, starting with the RESOLVED worktree_mode. Recorded once at
-- activation so resume/adoption in another process (different env/config)
-- treats the run under the mode it actually started in — a worktree-mode run
-- resumed by a coordinator without OMNIAGENTOS_SWARM_WORKTREES must still
-- run its missing-worktree checks and worktree GC, and a Phase-1 run must
-- never flip to worktree mode mid-flight. Additive; '{}' means "nothing
-- recorded" (pre-upgrade runs fall back to ambient resolution once).
ALTER TABLE swarm_runs ADD COLUMN params_json TEXT NOT NULL DEFAULT '{}';
