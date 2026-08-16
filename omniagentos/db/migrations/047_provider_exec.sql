-- WP3 provider-exec session fields.
--
-- output_text makes the bridge-session terminal contract identical to the
-- orchestrator executor contract. idle_minutes is the per-session override the
-- A2 reaper already reserves a seam for.

ALTER TABLE sessions ADD COLUMN output_text TEXT;
ALTER TABLE sessions ADD COLUMN idle_minutes REAL;
