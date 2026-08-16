-- 058_execution_contract.sql — TN.0, the single Tier-P schema commit.
--
-- Deliberately over-complete. Several columns and one whole table have NO
-- consumer on day one; that is the point. omniagentos/db/migrations is a Tier-P
-- directory, so anything schema-shaped that misses this commit costs a second
-- L4 ceremony (three distinct model families, unanimous, plus human). Landing
-- the whole surface once is cheaper than landing it twice.
--
-- Enum CHECK lists below duplicate the StrEnums in omniagentos/contracts.py by
-- hand — the post-011 migration idiom (001-010 used trailing comments instead).
-- There is no generator and no drift test; tests/db owns adding one.
--
-- Contains:
--   1. execution_decisions   — one row per decide_execution() call
--   2. scope_verifications   — one row per declared-vs-actual settle
--   3. agent_interactions    — mid-run operator<->agent channel (TN.10)
--   4. work_artifacts        — typed work-deliverable registry (TN.1/TN.3/TN.5)
--   5. campaign_grants       — bounded standing grants (TN.7)
--   6. grant_actions         — every action taken under a grant
--   7. column additions on runs / board_tasks

-- ---------------------------------------------------------------------------
-- 1. Deterministic routing decisions. Follows the 054_dispatch_gate.sql idiom:
-- log every classifier decision so the policy can be validated against outcomes
-- later (T7.1 replays these against swarm_attempts to check the tier floors are
-- not systematically over-spending). Writers MUST be best-effort and swallow
-- every exception, exactly like dispatch/log.py:record_decision — telemetry may
-- never block a dispatch.
-- ---------------------------------------------------------------------------
CREATE TABLE execution_decisions (
  id                TEXT PRIMARY KEY,
  created_at        TEXT NOT NULL,
  lane              TEXT NOT NULL,
  ref_id            TEXT,
  task_id           TEXT,
  run_id            TEXT,
  action_class      TEXT,
  task_risk         TEXT,
  task_mode         TEXT,
  declared_files    INTEGER NOT NULL DEFAULT 0,
  declared_creates  INTEGER NOT NULL DEFAULT 0,
  breadth_bucket    TEXT,
  tier              TEXT CHECK (tier IS NULL OR tier IN ('cheap','standard','strong','max')),
  effort            TEXT CHECK (effort IS NULL OR effort IN ('minimal','low','medium','high','xhigh')),
  max_tool_turns    INTEGER,
  scope_enforcement TEXT CHECK (scope_enforcement IN ('off','observe','enforce')),
  reasons_json      TEXT NOT NULL DEFAULT '[]',
  policy_version    TEXT NOT NULL DEFAULT '',
  applied           INTEGER NOT NULL DEFAULT 0 CHECK (applied IN (0,1))
);
CREATE INDEX idx_execution_decisions_created ON execution_decisions(created_at);
CREATE INDEX idx_execution_decisions_ref ON execution_decisions(ref_id);

-- ---------------------------------------------------------------------------
-- 2. Declared-vs-actual outcomes. execution_level is NOT reliability's
-- RiskResult.level — see the ScopeVerdictModel docstring. keyword_hit is
-- recorded but advisory here.
-- ---------------------------------------------------------------------------
CREATE TABLE scope_verifications (
  id                       TEXT PRIMARY KEY,
  created_at               TEXT NOT NULL,
  lane                     TEXT NOT NULL,
  ref_id                   TEXT,
  task_id                  TEXT,
  base_ref                 TEXT,
  head_ref                 TEXT,
  observed_count           INTEGER NOT NULL DEFAULT 0,
  source                   TEXT NOT NULL DEFAULT 'unobserved',
  ok                       INTEGER NOT NULL DEFAULT 1 CHECK (ok IN (0,1)),
  execution_level          INTEGER NOT NULL DEFAULT 1 CHECK (execution_level BETWEEN 1 AND 4),
  tier                     TEXT,
  undeclared_count         INTEGER NOT NULL DEFAULT 0,
  undeclared_json          TEXT NOT NULL DEFAULT '[]',
  missing_creates_json     TEXT NOT NULL DEFAULT '[]',
  missing_must_modify_json TEXT NOT NULL DEFAULT '[]',
  keyword_hit              INTEGER NOT NULL DEFAULT 0 CHECK (keyword_hit IN (0,1)),
  enforcement              TEXT NOT NULL DEFAULT 'off' CHECK (enforcement IN ('off','observe','enforce')),
  action_taken             TEXT
);
CREATE INDEX idx_scope_verifications_ref ON scope_verifications(ref_id);
CREATE INDEX idx_scope_verifications_created ON scope_verifications(created_at);

-- ---------------------------------------------------------------------------
-- 3. Mid-run operator<->agent channel. Lets an operator steer a running session
-- ("tighten the intro") without killing it, and lets an agent ask instead of
-- guessing or parking.
--
-- TRIMMED per operator decision 2026-07-24: NO ack lifecycle
-- (accepted/rejected/deferred with reasons) and NO applications table recording
-- each delivery as included/skipped/expired. That machinery exists to prove to a
-- third party what an agent was told; a single operator reading the transcript
-- does not need it. delivered_at gives delivery-once semantics, which is the
-- part that affects behaviour.
-- ---------------------------------------------------------------------------
CREATE TABLE agent_interactions (
  id              TEXT PRIMARY KEY,
  created_at      TEXT NOT NULL,
  updated_at      TEXT NOT NULL,
  work_ref_type   TEXT NOT NULL,
  work_ref_id     TEXT NOT NULL,
  session_id      TEXT,
  direction       TEXT NOT NULL CHECK (direction IN ('user_to_agent','agent_to_user')),
  kind            TEXT NOT NULL CHECK (kind IN ('nudge','question','answer')),
  blocking_policy TEXT NOT NULL DEFAULT 'none' CHECK (blocking_policy IN ('none','checkpoint','wait')),
  status          TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active','delivered','answered','canceled','expired')),
  parent_id       TEXT REFERENCES agent_interactions(id),
  author          TEXT,
  body            TEXT NOT NULL DEFAULT '',
  metadata_json   TEXT NOT NULL DEFAULT '{}',
  delivered_at    TEXT,
  answered_at     TEXT,
  expires_at      TEXT
);
CREATE INDEX idx_agent_interactions_ref ON agent_interactions(work_ref_type, work_ref_id, status);
CREATE INDEX idx_agent_interactions_session ON agent_interactions(session_id, status);
-- Pending blocking interactions are the ones a running job must poll for.
CREATE INDEX idx_agent_interactions_pending ON agent_interactions(status, blocking_policy)
  WHERE status = 'active';

-- ---------------------------------------------------------------------------
-- 4. Typed work-artifact registry. Filesystem artifact roots hold the BYTES;
-- this holds the identity, so manifests, promote sources, the web-fetch cache
-- and interaction bodies can all be addressed by type. version/lineage_id
-- support the TN.12 revision loop: a revise decision produces v2 sharing v1's
-- lineage, and promote always targets the approved version rather than whatever
-- is newest on disk.
--
-- NAMED work_artifacts, NOT artifacts, deliberately. A Wave-0 `artifacts` table
-- already exists (contracts/schema.sql:142 / 001_init.sql:142) and is frozen. It
-- is also structurally unable to serve this purpose: `run_id TEXT NOT NULL
-- REFERENCES runs(id)` means every row must belong to a run, whereas work-mode
-- deliverables belong to board tasks, sessions and campaigns. The two coexist —
-- the Wave-0 table stays the run-scoped file registry it always was.
-- ---------------------------------------------------------------------------
CREATE TABLE work_artifacts (
  id             TEXT PRIMARY KEY,
  created_at     TEXT NOT NULL,
  artifact_type  TEXT NOT NULL CHECK (artifact_type IN (
                   'prompt','response','task_spec','review','summary','diff','log',
                   'human_answer','report','nudge','plan_primary','plan_redteam',
                   'plan_synthesis','web_fetch_cache','deliverable','other')),
  task_mode      TEXT CHECK (task_mode IS NULL OR task_mode IN (
                   'code','report','content','image','video','intake_processing')),
  work_ref_type  TEXT,
  work_ref_id    TEXT,
  storage_kind   TEXT NOT NULL DEFAULT 'file_path' CHECK (storage_kind IN ('inline','file_path','url')),
  file_path      TEXT,
  url            TEXT,
  content_inline TEXT,
  mime_type      TEXT,
  sha256         TEXT,
  byte_size      INTEGER,
  version        INTEGER NOT NULL DEFAULT 1,
  lineage_id     TEXT,          -- stable across revisions; v1's id by convention
  approved_at    TEXT,
  external_id    TEXT,          -- ad id / broadcast id once published (T6.10 attribution)
  metadata_json  TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX idx_work_artifacts_ref ON work_artifacts(work_ref_type, work_ref_id, created_at);
CREATE INDEX idx_work_artifacts_type ON work_artifacts(artifact_type, created_at);
CREATE INDEX idx_work_artifacts_lineage ON work_artifacts(lineage_id, version);
CREATE INDEX idx_work_artifacts_sha ON work_artifacts(sha256);

-- ---------------------------------------------------------------------------
-- 5. Standing campaign grants — the "approve once per campaign, then auto"
-- mechanism, built WITHOUT relaxing approvals.consumed_at.
--
-- The one-shot approval (migration 012 anti-replay) authorizes THE GRANT; the
-- grant authorizes the sends. consumed_at semantics stay intact for everything
-- not under a grant.
--
-- A grant that cannot be exhausted is not a grant: max_actions, max_spend_usd
-- and expires_at are all required-in-spirit bounds, and revoked_at kills it
-- immediately. connectors/broker.py refuses CONSEQUENTIAL/IRREVERSIBLE
-- unconditionally in code today; TN.7 extends authorize() to accept a live
-- grant, and that change is the two-gate security kernel — human-authored only.
-- ---------------------------------------------------------------------------
CREATE TABLE campaign_grants (
  id                TEXT PRIMARY KEY,
  created_at        TEXT NOT NULL,
  label             TEXT NOT NULL DEFAULT '',
  capability        TEXT NOT NULL,        -- dotted connector capability, e.g. gmail.send
  target_set_json   TEXT NOT NULL DEFAULT '[]',  -- recipient list / ad account / channel
  project_id        TEXT,
  approval_id       TEXT,                 -- the one-shot approval that authorized THIS grant
  plan_approval_state TEXT NOT NULL DEFAULT 'not_required'
                      CHECK (plan_approval_state IN ('not_required','pending','approved','rejected')),
  max_actions       INTEGER,
  max_spend_usd     REAL,
  actions_used      INTEGER NOT NULL DEFAULT 0,
  spend_used_usd    REAL NOT NULL DEFAULT 0,
  expires_at        TEXT,
  revoked_at        TEXT,
  revoke_reason     TEXT,
  metadata_json     TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX idx_campaign_grants_capability ON campaign_grants(capability, revoked_at);
CREATE INDEX idx_campaign_grants_expiry ON campaign_grants(expires_at) WHERE revoked_at IS NULL;

-- Every action under a grant is still logged. "Approve once" must never mean
-- "unlogged" — this table is what makes a grant auditable after the fact, and
-- what anomaly break-out reads to detect a run of sends outside the target set.
CREATE TABLE grant_actions (
  id            TEXT PRIMARY KEY,
  created_at    TEXT NOT NULL,
  grant_id      TEXT NOT NULL REFERENCES campaign_grants(id),
  capability    TEXT NOT NULL,
  target        TEXT,
  spend_usd     REAL NOT NULL DEFAULT 0,
  outcome       TEXT NOT NULL DEFAULT 'ok' CHECK (outcome IN ('ok','failed','broke_out','refused')),
  ref_type      TEXT,
  ref_id        TEXT,
  detail        TEXT NOT NULL DEFAULT ''
);
CREATE INDEX idx_grant_actions_grant ON grant_actions(grant_id, created_at);

-- ---------------------------------------------------------------------------
-- 7. Column additions.
--   runs.base_sha    — the diff base for live declared-vs-actual verification.
--                      NULL (non-git working_dir) disables verification.
--   runs.project_id  — denormalized from tasks so the budget fan-out at
--                      runner/core.py:1760 can add a project:<id> scope without
--                      a second query per usage record, and so every evaluation
--                      query is project-scopable.
--   board_tasks.task_mode — lets a routine template provision report/content
--                      artifact roots (routines_tick._task_kwargs carries none
--                      today, which is why cron -> report dead-ends).
-- ---------------------------------------------------------------------------
ALTER TABLE runs ADD COLUMN base_sha TEXT;
ALTER TABLE runs ADD COLUMN project_id TEXT;
ALTER TABLE board_tasks ADD COLUMN task_mode TEXT;

CREATE INDEX idx_runs_project ON runs(project_id);
