-- 086_control_plane.sql — W1.1 control-plane schema (spec §13).
--
-- Package: W1.1 (control-plane schema). Lands the durable surfaces for
-- context packages, verification/audit/acceptance reports, routing history,
-- conditional swarm deps, execution-correlated events, and task-contract
-- tables previously owned only by TaskContractStore ensure-schema.
--
-- Explicit rejections (do NOT create these here):
--   * `executions` is NOT created — execution state stays fragmented across
--     swarm_attempts / runs / steps / sessions; unifying it is out of plan.
--   * `benchmarks` / `baseline_results` / `improvement_proposals` are NOT
--     created — already covered by the lab subsystem in 003_lab.sql.

-- ---------------------------------------------------------------------------
-- 1. Context packages
-- ---------------------------------------------------------------------------
CREATE TABLE context_packages (
    id                TEXT PRIMARY KEY,
    task_id           TEXT NOT NULL,
    manifest_json     TEXT NOT NULL,
    content_hash      TEXT NOT NULL,
    completeness_json TEXT NOT NULL DEFAULT '{}',
    status            TEXT NOT NULL CHECK (status IN ('building','ready','failed')),
    created_at        TEXT NOT NULL,
    updated_at        TEXT NOT NULL
);
CREATE INDEX idx_context_packages_task_status ON context_packages(task_id, status);

-- ---------------------------------------------------------------------------
-- 2. Verification reports
-- Verdict vocabulary is the *judgment* tri-state (pass/fail/insufficient_evidence).
-- Reviewer *infrastructure* failure is not a report row at all.
-- ---------------------------------------------------------------------------
CREATE TABLE verification_reports (
    id                 TEXT PRIMARY KEY,
    task_id            TEXT,
    artifact_hash      TEXT NOT NULL,
    verdict            TEXT NOT NULL CHECK (verdict IN ('pass','fail','insufficient_evidence')),
    verifier           TEXT NOT NULL DEFAULT '',
    reviewer_lineage   TEXT NOT NULL DEFAULT '',
    implementer_lineage TEXT NOT NULL DEFAULT '',
    findings_json      TEXT NOT NULL DEFAULT '[]',
    evidence_json      TEXT NOT NULL DEFAULT '{}',
    created_at         TEXT NOT NULL
);
CREATE INDEX idx_verification_reports_artifact ON verification_reports(artifact_hash);
CREATE INDEX idx_verification_reports_task ON verification_reports(task_id, created_at);

-- ---------------------------------------------------------------------------
-- 3. Audit reports
-- ---------------------------------------------------------------------------
CREATE TABLE audit_reports (
    id               TEXT PRIMARY KEY,
    scope_type       TEXT NOT NULL CHECK (scope_type IN ('task','project','run')),
    scope_id         TEXT NOT NULL,
    result           TEXT NOT NULL CHECK (result IN ('pass','fail','inconclusive')),
    categories_json  TEXT NOT NULL DEFAULT '{}',
    trace_refs_json  TEXT NOT NULL DEFAULT '[]',
    created_at       TEXT NOT NULL
);
CREATE INDEX idx_audit_reports_scope ON audit_reports(scope_type, scope_id);

-- ---------------------------------------------------------------------------
-- 4. Acceptance records
-- ---------------------------------------------------------------------------
CREATE TABLE acceptance_records (
    id                      TEXT PRIMARY KEY,
    project_id              TEXT NOT NULL,
    release_candidate_hash  TEXT NOT NULL,
    disposition             TEXT NOT NULL CHECK (disposition IN ('PASS','FAIL','PASS_WITH_ACCEPTED_EXCEPTIONS')),
    exceptions_json         TEXT NOT NULL DEFAULT '[]',
    evidence_json           TEXT NOT NULL DEFAULT '{}',
    created_at              TEXT NOT NULL
);
CREATE INDEX idx_acceptance_records_project ON acceptance_records(project_id, created_at);

-- ---------------------------------------------------------------------------
-- 5. Routing history
-- Writers land in W2.4; landing the surface without day-one consumers follows
-- the 058 execution_decisions precedent.
-- ---------------------------------------------------------------------------
CREATE TABLE routing_history (
    id             TEXT PRIMARY KEY,
    task_id        TEXT,
    decision_kind  TEXT NOT NULL,
    features_json  TEXT NOT NULL DEFAULT '{}',
    route_json     TEXT NOT NULL DEFAULT '{}',
    cost_json      TEXT NOT NULL DEFAULT '{}',
    outcome_json   TEXT,
    decided_at     TEXT NOT NULL,
    outcome_at     TEXT
);
CREATE INDEX idx_routing_history_kind ON routing_history(decision_kind, decided_at);
CREATE INDEX idx_routing_history_task ON routing_history(task_id);

-- ---------------------------------------------------------------------------
-- 6. Conditional swarm deps (inert until W3.1 reads it)
-- ---------------------------------------------------------------------------
ALTER TABLE swarm_deps ADD COLUMN condition TEXT NOT NULL DEFAULT 'VERIFIED';

-- ---------------------------------------------------------------------------
-- 7. Execution correlation on events (nullable; W2.6 populates them)
-- ---------------------------------------------------------------------------
ALTER TABLE events ADD COLUMN execution_id TEXT;
ALTER TABLE events ADD COLUMN sequence INTEGER;
CREATE INDEX idx_events_execution_sequence ON events(execution_id, sequence);

-- ---------------------------------------------------------------------------
-- 8. Task contracts — mirror of omniagentos/taskcontract/store.py::_SCHEMA
-- Existing DBs no-op (IF NOT EXISTS); fresh DBs now get them from migrate.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS task_contracts (
    id              TEXT PRIMARY KEY,
    contract_hash   TEXT NOT NULL,
    state           TEXT NOT NULL,
    lane            TEXT NOT NULL,
    ref_id          TEXT,
    task_id         TEXT,
    lineage_id      TEXT,
    version         INTEGER NOT NULL DEFAULT 1,
    body_json       TEXT NOT NULL,
    metadata_json   TEXT NOT NULL DEFAULT '{}',
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL,
    UNIQUE(contract_hash, lane, ref_id)
);
CREATE INDEX IF NOT EXISTS idx_task_contracts_hash ON task_contracts(contract_hash);
CREATE INDEX IF NOT EXISTS idx_task_contracts_ref ON task_contracts(lane, ref_id, state);
CREATE INDEX IF NOT EXISTS idx_task_contracts_task ON task_contracts(task_id, updated_at);

CREATE TABLE IF NOT EXISTS task_contract_transitions (
    id              TEXT PRIMARY KEY,
    contract_id     TEXT NOT NULL,
    contract_hash   TEXT NOT NULL,
    from_state      TEXT NOT NULL,
    to_state        TEXT NOT NULL,
    reason          TEXT NOT NULL DEFAULT '',
    actor           TEXT NOT NULL DEFAULT 'system',
    evidence_json   TEXT NOT NULL DEFAULT '{}',
    created_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_task_contract_tx_contract
    ON task_contract_transitions(contract_id, created_at);
