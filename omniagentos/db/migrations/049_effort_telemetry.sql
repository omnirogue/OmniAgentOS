-- Effort/usage telemetry: make the effort-vs-outcome tradeoff measurable.
--
-- Before this, a finished run recorded `cost_usd` and `model` and nothing else,
-- so the question "is xhigh worth it over medium" had no answer in our own data
-- -- the independent variable (effort) was not stored at all, and neither were
-- the token counts or wall-clock that would show what it bought.
--
-- Two grains, because the interesting metric spans both:
--   sessions        one CLI invocation
--   swarm_attempts  one try at a work package; `seq` is the retry counter and a
--                   change of model/tier across seq is an escalation
-- Cost-to-green (total spend until a board task reaches end_reason='completed',
-- INCLUDING the retries and escalations) is the number that makes the
-- non-monotonicity visible: cost-per-run hides that a cheap attempt which fails
-- twice and then escalates costs more than one expensive attempt that lands.
--
-- `usage_source` is load-bearing, not decoration. AgentUsage distinguishes a
-- provider's own report from our token ESTIMATOR, and folding an estimate into
-- this dataset would poison the exact comparison it exists to support. Rows
-- carry NULL token counts rather than guesses; only 'cli-report' is evidence.

ALTER TABLE sessions ADD COLUMN effort TEXT;
ALTER TABLE sessions ADD COLUMN input_tokens INTEGER;
ALTER TABLE sessions ADD COLUMN output_tokens INTEGER;
ALTER TABLE sessions ADD COLUMN wall_ms INTEGER;
ALTER TABLE sessions ADD COLUMN usage_source TEXT;

ALTER TABLE swarm_attempts ADD COLUMN effort TEXT;
ALTER TABLE swarm_attempts ADD COLUMN cost_usd REAL;
ALTER TABLE swarm_attempts ADD COLUMN input_tokens INTEGER;
ALTER TABLE swarm_attempts ADD COLUMN output_tokens INTEGER;
ALTER TABLE swarm_attempts ADD COLUMN wall_ms INTEGER;
ALTER TABLE swarm_attempts ADD COLUMN usage_source TEXT;

-- Cost-to-green walks every attempt of a board task in seq order.
CREATE INDEX IF NOT EXISTS idx_swarm_attempts_task_seq
    ON swarm_attempts(board_task_id, seq);
