-- Coordinator lease fencing (m10): adopt_run bumps the generation; the
-- coordinator heartbeat becomes a conditional UPDATE matching its own
-- generation. A displaced coordinator (its run adopted by another process
-- after a stale heartbeat) sees rowcount=0 on its next beat and stops
-- cleanly — two coordinators can no longer interleave git mutations on the
-- same run even if the original was merely wedged, not dead.
ALTER TABLE swarm_runs ADD COLUMN lease_generation INTEGER NOT NULL DEFAULT 0;
