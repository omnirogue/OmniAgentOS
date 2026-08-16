-- 042_reliability_company.sql
-- OmniAgentOS V2: Self-improving reliability system schema.
--
-- Additive: new tables for event tracking, improvement proposals, audit lifecycle,
-- agent organization. Agents table gains org/role/harness columns.
-- Hash-chained reliability_log (append-only, no UPDATE/DELETE store methods).
-- All timestamps UTC ISO-8601. Short BEGIN IMMEDIATE writes with busy-retry (codex contract §5b).

CREATE TABLE org_units (
  id TEXT PRIMARY KEY,
  name TEXT NOT NULL UNIQUE,
  kind TEXT NOT NULL CHECK (kind IN ('company','department','team')),
  parent_id TEXT REFERENCES org_units(id),
  charter TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active','retired')),
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

ALTER TABLE agents ADD COLUMN org_unit_id TEXT REFERENCES org_units(id);
ALTER TABLE agents ADD COLUMN org_role TEXT NOT NULL DEFAULT 'specialist';    -- cto|vp|manager|specialist|judge
ALTER TABLE agents ADD COLUMN title TEXT NOT NULL DEFAULT '';
ALTER TABLE agents ADD COLUMN charter TEXT NOT NULL DEFAULT '';
ALTER TABLE agents ADD COLUMN schedule_json TEXT NOT NULL DEFAULT '{}';       -- {"cadence":"twice_daily","callable":true}
ALTER TABLE agents ADD COLUMN harness TEXT;                                   -- adapter key, e.g. cli-codex
ALTER TABLE agents ADD COLUMN enabled INTEGER NOT NULL DEFAULT 1;
ALTER TABLE agents ADD COLUMN vault_note_path TEXT;

CREATE TABLE reliability_events (
  id TEXT PRIMARY KEY,
  failure_class TEXT NOT NULL,          -- taxonomy.FailureClass values
  severity TEXT NOT NULL CHECK (severity IN ('info','warning','critical')),
  signature TEXT NOT NULL,              -- dedup fingerprint: class|source|error-hash
  occurrence_key TEXT NOT NULL UNIQUE,  -- stable per-occurrence key (signature|ref_id|first-seen bucket): overlapping watch runs cannot double-insert (ON CONFLICT IGNORE)
  source TEXT NOT NULL,                 -- detector rule id
  ref_type TEXT, ref_id TEXT,           -- run|task|session|approval|account|routine
  evidence_json TEXT NOT NULL DEFAULT '{}',
  status TEXT NOT NULL DEFAULT 'open'
    CHECK (status IN ('open','recovering','recovered','proposed','resolved','ignored')),
  recovery_json TEXT NOT NULL DEFAULT '{}',
  improvement_id TEXT, audit_id TEXT,
  detected_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE INDEX idx_relev_sig ON reliability_events(signature, detected_at DESC);
CREATE INDEX idx_relev_status ON reliability_events(status, severity);

CREATE TABLE improvements (
  id TEXT PRIMARY KEY,
  origin TEXT NOT NULL CHECK (origin IN ('realtime','audit','department','cto','weekly','human','agent_request')),
  kind TEXT NOT NULL CHECK (kind IN ('fix','optimization','architecture','new_agent','skill','docs','config')),
  title TEXT NOT NULL, summary TEXT NOT NULL DEFAULT '',
  root_cause TEXT NOT NULL DEFAULT '',
  proposal_json TEXT NOT NULL DEFAULT '{}',   -- see §5 proposal contract
  risk_level INTEGER NOT NULL DEFAULT 2 CHECK (risk_level BETWEEN 1 AND 4),
  status TEXT NOT NULL DEFAULT 'proposed' CHECK (status IN
    ('proposed','testing','judging','panel_blocked','awaiting_human','approved','applying','applied',
     'monitoring','confirmed','rejected','rolling_back','rolled_back','failed','superseded')),
  version INTEGER NOT NULL DEFAULT 0,      -- CAS counter: every transition = UPDATE ... WHERE status=? AND version=?
  stage_started_at TEXT, stage_deadline TEXT, attempt INTEGER NOT NULL DEFAULT 0,
  last_error_json TEXT NOT NULL DEFAULT '{}',
  ranking_score REAL NOT NULL DEFAULT 0,
  sandbox_json TEXT NOT NULL DEFAULT '{}',
  votes_summary_json TEXT NOT NULL DEFAULT '{}',
  rollback_point_id TEXT, applied_task_id TEXT,
  applied_sha TEXT,                     -- immutable commit SHA once applied (authoritative)
  monitor_until TEXT,                   -- observation-window end
  memory_refs_json TEXT NOT NULL DEFAULT '[]',
  decided_by TEXT, created_by TEXT NOT NULL DEFAULT 'system',
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL, applied_at TEXT, resolved_at TEXT
);
CREATE INDEX idx_imp_status ON improvements(status, created_at DESC);

CREATE TABLE improvement_votes (
  id TEXT PRIMARY KEY,
  improvement_id TEXT NOT NULL REFERENCES improvements(id),
  panel_attempt_id TEXT NOT NULL,          -- idempotency: retried judges cannot double-vote
  judge_agent TEXT NOT NULL, model_family TEXT NOT NULL, model TEXT NOT NULL DEFAULT '',
  verdict TEXT NOT NULL CHECK (verdict IN ('approve','reject','approve_with_conditions','needs_human')),
  scores_json TEXT NOT NULL DEFAULT '{}',   -- root_cause,fix_correctness,regression_risk,security_risk,reliability,quality,cost,speed,architecture,test_coverage,reversibility 0-10
  reasoning TEXT NOT NULL DEFAULT '', conditions TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL,
  UNIQUE(improvement_id, panel_attempt_id, model_family)
);
CREATE INDEX idx_vote_imp ON improvement_votes(improvement_id);

CREATE TABLE rollback_points (
  id TEXT PRIMARY KEY, improvement_id TEXT,
  kind TEXT NOT NULL CHECK (kind IN ('git','db','config','composite')),
  git_ref TEXT, snapshot_path TEXT, notes TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL, restored_at TEXT
);

CREATE TABLE reliability_audits (
  id TEXT PRIMARY KEY,
  kind TEXT NOT NULL CHECK (kind IN ('watch','twice_daily','daily_summary','weekly_architecture','on_demand')),
  status TEXT NOT NULL DEFAULT 'queued' CHECK (status IN ('queued','running','completed','failed')),
  window_start TEXT NOT NULL, window_end TEXT NOT NULL,
  stats_json TEXT NOT NULL DEFAULT '{}', findings INTEGER NOT NULL DEFAULT 0,
  report_note_path TEXT, started_at TEXT NOT NULL, finished_at TEXT
);

CREATE TABLE scorecards (
  id TEXT PRIMARY KEY,
  subject_type TEXT NOT NULL,           -- agent|model|harness|department|skill|routine
  subject_id TEXT NOT NULL,
  window TEXT NOT NULL CHECK (window IN ('day','week')),
  period_start TEXT NOT NULL,
  metrics_json TEXT NOT NULL DEFAULT '{}',
  computed_at TEXT NOT NULL,
  UNIQUE(subject_type, subject_id, window, period_start)
);

CREATE TABLE autonomy_settings (
  id TEXT PRIMARY KEY,
  scope_type TEXT NOT NULL CHECK (scope_type IN ('global','department','agent','kind')),
  scope_id TEXT NOT NULL DEFAULT '',
  mode TEXT NOT NULL CHECK (mode IN ('approve','auto')),
  max_auto_risk INTEGER NOT NULL DEFAULT 0 CHECK (max_auto_risk BETWEEN 0 AND 2),
  updated_by TEXT NOT NULL, updated_at TEXT NOT NULL,
  UNIQUE(scope_type, scope_id)
);

CREATE TABLE agent_requests (
  id TEXT PRIMARY KEY,
  description TEXT NOT NULL, requested_by TEXT NOT NULL DEFAULT 'owner',
  status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN
    ('pending','designing','awaiting_approval','approved','created','rejected','failed')),
  design_json TEXT NOT NULL DEFAULT '{}',
  improvement_id TEXT, agent_id TEXT,
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);

CREATE TABLE reliability_state (      -- cursors + observation windows + apply mutex
  key TEXT PRIMARY KEY, value_json TEXT NOT NULL, updated_at TEXT NOT NULL
);

CREATE TABLE reliability_log (        -- hash-chained, append-only (§6); no UPDATE/DELETE in store
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  entity_type TEXT NOT NULL,          -- improvement|autonomy|governance|event|audit
  entity_id TEXT NOT NULL,
  from_status TEXT, to_status TEXT NOT NULL,
  actor TEXT NOT NULL,                -- pipeline|watch|audit|human:<name>|judge:<family>
  detail_json TEXT NOT NULL DEFAULT '{}',
  ts TEXT NOT NULL,
  prev_hash TEXT NOT NULL, hash TEXT NOT NULL
);
CREATE INDEX idx_rlog_entity ON reliability_log(entity_type, entity_id);

-- Seed global autonomy setting (default: approve mode, max_auto_risk=0)
INSERT OR IGNORE INTO autonomy_settings
  (id, scope_type, scope_id, mode, max_auto_risk, updated_by, updated_at)
VALUES
  ('aut_global', 'global', '', 'approve', 0, 'migration', datetime('now'));
