-- Add provenance tracking to distinguish real runs from simulated and test runs.
--
-- THE PROBLEM: Completion-rate numbers are upper-bound estimates because
-- swarm_attempts, swarm_runs, and sessions have no provenance column.
-- Real runs, benchmark runs, and pytest artifacts are indistinguishable.
--
-- SOLUTION: Add a `source` column (values: 'real' | 'simulated' | 'test')
-- to track the origin of each run. This is orthogonal to the sessions.source
-- column (which records 'bridge' vs 'external' session origin).
--
-- BACKFILL DISCIPLINE: Historical rows are backfilled to 'real' as the default,
-- with the understanding that they are unpartitioned (we cannot retroactively
-- determine which were benchmarks or test artifacts). This is the honest
-- approach: we label them as if they were production, but any analysis must
-- account for the fact that some portion are not.
--
-- WRITE PATH RESPONSIBILITY: The application code MUST set source='test' for
-- rows inserted by pytest fixtures and source='simulated' for benchmark runs.
-- The migration itself provides only the DEFAULT for new production rows.
-- See omniagentos/swarm/dal.py:open_run and open_attempt; omniagentos/db/store.py
-- for session insert paths; and tests conftest fixtures for the test-time overrides.

ALTER TABLE swarm_attempts ADD COLUMN source TEXT NOT NULL DEFAULT 'real';
ALTER TABLE swarm_runs ADD COLUMN source TEXT NOT NULL DEFAULT 'real';
ALTER TABLE sessions ADD COLUMN run_source TEXT NOT NULL DEFAULT 'real';

-- The sessions table already has a 'source' column for a different purpose
-- (session communication origin: 'bridge' vs 'external'). The new column
-- is named 'run_source' to avoid collision.
