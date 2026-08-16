-- 094_provider_call_usage.sql (renumbered for main; original 092 in mission-restore-final branch to avoid collision with 092_jira_project_key)
--
-- One initiated provider call owns one caller-generated call_id.  Exact money
-- is stored as both provider decimal text and integer nano-USD; estimates carry
-- only a conservative upper bound; unknowns remain NULL rather than becoming
-- counterfeit zero-cost calls.

CREATE TABLE provider_call_usage (
    id                           INTEGER PRIMARY KEY AUTOINCREMENT,
    call_id                      TEXT NOT NULL UNIQUE,
    request_id                   TEXT NOT NULL,
    execution_id                 TEXT NOT NULL,
    run_id                       TEXT,
    campaign_id                  TEXT,
    reservation_id               TEXT,
    task_id                      TEXT,
    attempt_id                   TEXT,
    session_id                   TEXT,
    work_id                      TEXT,
    root_trace_id                TEXT,
    stage                        TEXT NOT NULL CHECK (
        stage IN (
            'planner', 'clarifier', 'planner_retry', 'worker', 'worker_retry',
            'reviewer', 'reviewer_retry', 'escalation', 'integrator',
            'integrator_retry'
        )
    ),
    attempt_index                INTEGER NOT NULL DEFAULT 0 CHECK (attempt_index >= 0),
    provider                     TEXT NOT NULL,
    transport                    TEXT NOT NULL,
    requested_model              TEXT NOT NULL,
    effective_model              TEXT NOT NULL,
    model_lineage                TEXT NOT NULL,
    billing_provider             TEXT NOT NULL,
    adapter_key                  TEXT NOT NULL,
    provider_request_id          TEXT,
    request_state                TEXT NOT NULL CHECK (
        request_state IN ('not_sent', 'sent', 'indeterminate')
    ),
    provider_outcome             TEXT,
    input_tokens                 INTEGER CHECK (input_tokens IS NULL OR input_tokens >= 0),
    output_tokens                INTEGER CHECK (output_tokens IS NULL OR output_tokens >= 0),
    total_tokens                 INTEGER CHECK (total_tokens IS NULL OR total_tokens >= 0),
    cost_usd_decimal             TEXT,
    cost_usd_nanos               INTEGER CHECK (
        cost_usd_nanos IS NULL OR cost_usd_nanos >= 0
    ),
    cost_upper_bound_usd_nanos   INTEGER CHECK (
        cost_upper_bound_usd_nanos IS NULL OR cost_upper_bound_usd_nanos >= 0
    ),
    cost_quality                 TEXT NOT NULL CHECK (
        cost_quality IN ('exact', 'estimated', 'unknown')
    ),
    cost_source                  TEXT NOT NULL,
    pricing_revision             TEXT,
    created_at                   TEXT NOT NULL,
    settled_at                   TEXT,
    CHECK (
        (cost_quality = 'exact'
            AND cost_usd_decimal IS NOT NULL
            AND cost_usd_nanos IS NOT NULL)
        OR
        (cost_quality = 'estimated'
            AND cost_upper_bound_usd_nanos IS NOT NULL
            AND cost_usd_decimal IS NULL
            AND cost_usd_nanos IS NULL)
        OR
        (cost_quality = 'unknown'
            AND cost_usd_decimal IS NULL
            AND cost_usd_nanos IS NULL)
    )
);

CREATE INDEX idx_provider_call_usage_run
    ON provider_call_usage(run_id, created_at, call_id);
CREATE INDEX idx_provider_call_usage_execution
    ON provider_call_usage(execution_id, created_at, call_id);
CREATE INDEX idx_provider_call_usage_campaign
    ON provider_call_usage(campaign_id, created_at, call_id);
CREATE INDEX idx_provider_call_usage_reservation
    ON provider_call_usage(reservation_id)
    WHERE reservation_id IS NOT NULL;
