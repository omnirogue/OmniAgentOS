-- 001_workqueue.sql — the shared work queue's own database (var/workqueue.sqlite3).
-- This migration set is INDEPENDENT of omniagentos/db/migrations/ (see SPEC-shared-queue §1.2):
-- the queue DB must contain ONLY wq_* tables and schema_migrations. Applying the packaged
-- 121-migration set here would recreate the exact numbering collision this file escapes.
--
-- EDITED IN PLACE 2026-08-11 (submitted_by): this migration has never been applied to a
-- deployed database — the pool has not shipped — so the column was added to 001 rather
-- than as a 002 ALTER. Once var/workqueue.sqlite3 exists on the primary, this file is
-- FROZEN and every further change is a new numbered migration; the migrator checksums
-- what it applied, so editing an applied file is a corruption report, not an upgrade.

CREATE TABLE schema_migrations (
  version    INTEGER PRIMARY KEY,
  applied_at TEXT NOT NULL,
  checksum   TEXT
);

CREATE TABLE wq_units (
  id               TEXT PRIMARY KEY,               -- 'wq_' + ULID
  idempotency_key  TEXT NOT NULL UNIQUE,           -- re-enqueue of the same key is a no-op, not a duplicate
  created_at       TEXT NOT NULL,                  -- UTC ISO-8601
  updated_at       TEXT NOT NULL,

  -- ---- WHAT ------------------------------------------------------------
  repo_url         TEXT NOT NULL,
  repo_slug        TEXT NOT NULL,
  base_sha         TEXT NOT NULL,                  -- 40-hex. NEVER a branch name (moved merge base = mechanics refusal class).
  base_ref         TEXT NOT NULL DEFAULT 'main',   -- provenance only, never resolved at claim time
  branch           TEXT NOT NULL,                  -- branch the worker creates and pushes: wq/<slug>-<MMDD>
  owned_paths      TEXT NOT NULL,                  -- JSON array of repo-relative globs this unit may write
  brief_inline     TEXT,
  brief_path       TEXT,
  submitted_by     TEXT NOT NULL DEFAULT '',       -- who offloaded it: 'owner'/'bob'/'alice'/agent id (env WQ_USER at enqueue). '' = unattributed.

  -- ---- HOW IT RUNS -----------------------------------------------------
  agent_profile    TEXT NOT NULL,                  -- key into configs/workqueue.yaml:profiles — NOT a shell command
  timeout_s        INTEGER NOT NULL DEFAULT 3600 CHECK (timeout_s BETWEEN 60 AND 21600),
  labels           TEXT NOT NULL DEFAULT '[]',     -- JSON array; a machine must declare all of these to claim

  -- ---- HOW IT IS JUDGED ------------------------------------------------
  risk_class       TEXT NOT NULL CHECK (risk_class IN ('mechanical','standard','sensitive')),
  acceptance_cmd   TEXT NOT NULL,                  -- exact command, run in the unit worktree, exit 0 = pass
  acceptance_gate  TEXT,                           -- name in configs/gates.d/*.yaml; NULL = raw command

  -- ---- LIFECYCLE -------------------------------------------------------
  state            TEXT NOT NULL DEFAULT 'queued'
                   CHECK (state IN ('queued','claimed','running','review','done','parked','cancelled')),
  priority         INTEGER NOT NULL DEFAULT 2 CHECK (priority BETWEEN 0 AND 4),  -- 0 = highest
  attempt          INTEGER NOT NULL DEFAULT 0 CHECK (attempt >= 0),
  max_attempts     INTEGER NOT NULL DEFAULT 3 CHECK (max_attempts BETWEEN 1 AND 5),
  instrument_retries INTEGER NOT NULL DEFAULT 0 CHECK (instrument_retries >= 0),

  -- ---- LEASE (fencing; shape copied from 085_lab_jobs.sql) --------------
  lease_owner      TEXT,                           -- '<machine_id>:<worker_id>'
  lease_expires_at TEXT,
  lease_generation INTEGER NOT NULL DEFAULT 0 CHECK (lease_generation >= 0),

  -- ---- TERMINAL --------------------------------------------------------
  terminal_reason  TEXT CHECK (terminal_reason IS NULL OR terminal_reason IN (
                     'accepted','attempts-exhausted','storm-parked','terminal-instrument',
                     'cancelled','superseded','unclaimable-no-capable-machine')),
  result_branch    TEXT,
  result_sha       TEXT,
  finished_at      TEXT,
  cancel_requested INTEGER NOT NULL DEFAULT 0 CHECK (cancel_requested IN (0,1)),
  park_remedy      TEXT,                           -- what actually unblocks a parked unit, in words
  alerted_at       TEXT,                           -- one-alert guard for non-refusal parks
  not_before       TEXT                            -- instrument-retry backoff gate: not claimable until this UTC time
);
CREATE INDEX idx_wq_units_claimable ON wq_units(state, priority, created_at);
CREATE INDEX idx_wq_units_lease     ON wq_units(lease_expires_at)
  WHERE state IN ('claimed','running');
CREATE INDEX idx_wq_units_idem      ON wq_units(idempotency_key);

CREATE TABLE wq_attempts (
  id               INTEGER PRIMARY KEY AUTOINCREMENT,
  unit_id          TEXT NOT NULL REFERENCES wq_units(id),
  attempt          INTEGER NOT NULL,
  lease_generation INTEGER NOT NULL,
  machine_id       TEXT NOT NULL,
  worker_id        TEXT NOT NULL,
  started_at       TEXT NOT NULL,
  finished_at      TEXT,
  input_key        TEXT,        -- the §4 fingerprint this attempt was graded on
  outcome          TEXT CHECK (outcome IS NULL OR outcome IN (
                     'pass','candidate-defect','instrument-error','environment',
                     'contention-flake','unchanged-retry','storm-parked',
                     'lease-lost','abandoned','timeout')),
  exit_code        INTEGER,     -- the PROCESS exit code. Branch on this, never on a parsed receipt field.
  gate_exit_code   INTEGER,     -- the wrapped gate's own code; may be NULL
  retryable        INTEGER,     -- 0 / 1 / NULL
  remedy           TEXT,        -- what actually unblocks it, in words
  head_sha         TEXT,
  log_path         TEXT,        -- absolute path ON THE MACHINE THAT RAN IT
  UNIQUE (unit_id, attempt)     -- two machines cannot both record attempt N — the double-execution alarm
);
CREATE INDEX idx_wq_attempts_unit ON wq_attempts(unit_id, attempt);
CREATE INDEX idx_wq_attempts_key  ON wq_attempts(input_key);

CREATE TABLE wq_machines (
  machine_id     TEXT PRIMARY KEY,     -- `scutil --get LocalHostName` on macOS; `hostname -s` on Linux
  hostname       TEXT NOT NULL,
  os             TEXT NOT NULL DEFAULT 'darwin' CHECK (os IN ('darwin','linux')),
  ssh_target     TEXT,                 -- for the enrollment preflight only
  labels         TEXT NOT NULL DEFAULT '[]',
  max_concurrent INTEGER NOT NULL DEFAULT 1 CHECK (max_concurrent BETWEEN 0 AND 16),
  drain          INTEGER NOT NULL DEFAULT 0 CHECK (drain IN (0,1)),
  enrolled_at    TEXT NOT NULL,
  last_seen_at   TEXT,
  agent_version  TEXT,                 -- git sha of the worker code on that box
  notes          TEXT,

  -- ---- CAPACITY TELEMETRY (the operator 2026-08-11: the pool must know every box's cores/load) ----
  ncpu           INTEGER,              -- logical cores
  perf_cores     INTEGER,              -- performance cores (== ncpu on Linux)
  mem_gb         REAL,                 -- physical memory
  last_load1     REAL,                 -- 1-minute load average at last beat
  last_load5     REAL,
  last_mem_free_gb REAL,
  ceiling_fraction REAL NOT NULL DEFAULT 0.75,  -- claims pause when load1 > ceiling_fraction * ncpu
  telemetry_at   TEXT                  -- when the capacity numbers were last refreshed
);

CREATE TABLE wq_workers (
  worker_id       TEXT PRIMARY KEY,    -- '<machine_id>:<pid>:<boot-nonce>' — pid alone is meaningless across machines
  machine_id      TEXT NOT NULL REFERENCES wq_machines(machine_id),
  pid             INTEGER NOT NULL,
  started_at      TEXT NOT NULL,
  last_beat_at    TEXT NOT NULL,
  current_unit_id TEXT
);

CREATE TABLE wq_refusals (
  input_key     TEXT NOT NULL,
  gate          TEXT NOT NULL,
  count         INTEGER NOT NULL DEFAULT 0,
  first_seen_at TEXT NOT NULL,
  last_seen_at  TEXT NOT NULL,
  refusal_class TEXT NOT NULL,   -- the ORIGINAL cause. Never overwritten by unchanged-retry/storm-parked.
  retryable     INTEGER NOT NULL,
  remedy        TEXT NOT NULL,
  last_unit_id  TEXT,
  parked_at     TEXT,
  alerted_at    TEXT,            -- non-NULL == already alerted. This is the ONE-alert guard.
  PRIMARY KEY (input_key, gate)
);
