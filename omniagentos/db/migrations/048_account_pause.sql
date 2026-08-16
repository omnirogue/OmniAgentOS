-- Operator-initiated temporary pause for a provider account.
--
-- Deliberately a SEPARATE column from ``cooldown_until`` rather than a reuse of it.
-- ``cooldown_until`` is PROVIDER state, owned by routing.limit_state: the
-- STRUCTURED-FIRST invariant means any clean completion calls
-- ``report_outcome('ok')``, which unconditionally NULLs ``cooldown_until`` so a
-- recovered 429 blip can't leave a healthy account cooling. A pause stored there
-- would be wiped by the next successful session on that account -- silently, and
-- most likely in exactly the case the operator cared about (pausing an account
-- that is currently busy).
--
-- Three orthogonal levers, one owner each:
--   enabled = 0     operator, permanent      (also the auth-failure stop-the-line)
--   cooldown_until  limit_state, automatic   (rate limit / quota / overload)
--   paused_until    operator, self-expiring  (this column)
ALTER TABLE claude_accounts ADD COLUMN paused_until TEXT;

-- Operator's stated reason, shown in the UI while the pause is in force.
ALTER TABLE claude_accounts ADD COLUMN pause_reason TEXT;
