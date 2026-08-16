-- routines-fire: tracks when a routine last actually fired (created a task+run),
-- so the tick entrypoint (omniagentos.scheduler.routines_tick) can decide
-- whether a cron/event trigger is DUE right now without re-scanning
-- routine_runs on every tick.
--
-- Purely additive: one new NULLable column on the existing routines table.
-- NULL means "never fired" (a brand-new routine), which is distinct from any
-- real timestamp — the tick's DUE-check treats the two cases differently
-- (a never-fired cron routine is due as soon as its schedule next matches;
-- an event-triggered routine that has never fired is due on the first
-- matching event ever recorded).
--
-- Numbered 022 to follow 021_banking_credit.sql.

ALTER TABLE routines ADD COLUMN last_fired TEXT;
