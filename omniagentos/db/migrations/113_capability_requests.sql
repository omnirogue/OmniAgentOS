-- U-E1 — the durable, IMMUTABLE CapabilityRequest envelope.
--
-- ORDINAL: renumbered 110 -> 113 at Phase-2 integration (PLAN.md §4 trap rule
-- 3). Two lanes claimed 110; 109 and 110 were already applied live by
-- lane/cap-skills. This file stays immediately above its own lane's U-E2
-- decision table (112) — the pairing, not the number, is what matters. The
-- `schema_migrations` reconciliation owed by this renumber is a verified no-op;
-- the full record is on the commit allocating 111.
--
-- What a worker asked for, when, as whom, and why. Nothing about the decision
-- lives here: that is migration 112's `capability_decisions`, joined by
-- `request_id`. One UUID therefore threads capability_requests ->
-- capability_decisions -> agent_capabilities / capability_grant_log (106) ->
-- broker_calls (107), which is what makes "what did agent X ask for, who
-- decided, and what happened next" one query instead of a reconstruction.
--
-- IMMUTABLE MEANS IMMUTABLE. There is no UPDATE path at all (see the trigger
-- below). A renewed need mints a new request id; editing this row would erase
-- the decision latency and the attribution that the whole spine is for.
--
-- WHAT IS DELIBERATELY ABSENT
-- No granted_by, risk, action_class, credential, grant id, or expiry column.
-- The emitter never supplies any of those and there is nowhere for a forged one
-- to land: the service derives identity from the authenticated channel and the
-- decision row owns everything the policy decided.

CREATE TABLE capability_requests (
    request_id      TEXT PRIMARY KEY NOT NULL,

    -- THE canonical identity spelling, enforced here rather than trusted from
    -- the write path (invariant 1 / K2-8). Same grammar as
    -- `omniagentos/reliability/store.py:_CANONICAL_IDENTITY_RE`, expressed with
    -- GLOB because SQLite has no REGEXP, and with FULLMATCH semantics: a prefix
    -- test would admit `system_impostor`, and an unanchored one would admit an
    -- empty subject. `agent:bob`, `system_impostor`, `lane:`, `human:-x` and
    -- `lane:a:b` must all FAIL this CHECK.
    from_agent_id   TEXT NOT NULL CHECK (
        from_agent_id = 'system'
        OR (
            (from_agent_id GLOB 'lane:*' OR from_agent_id GLOB 'loop:*'
             OR from_agent_id GLOB 'job:*' OR from_agent_id GLOB 'human:*')
            AND substr(from_agent_id, instr(from_agent_id, ':') + 1) GLOB '[A-Za-z0-9]*'
            AND substr(from_agent_id, instr(from_agent_id, ':') + 1)
                NOT GLOB '*[^A-Za-z0-9._-]*'
        )
    ),

    -- The emitting channel. Also a holder string, so it carries the same
    -- grammar: one spelling everywhere, or the §8 conformance signal is
    -- counting two vocabularies and calling it one.
    from_lane       TEXT NOT NULL CHECK (
        from_lane = 'system'
        OR (
            (from_lane GLOB 'lane:*' OR from_lane GLOB 'loop:*'
             OR from_lane GLOB 'job:*' OR from_lane GLOB 'human:*')
            AND substr(from_lane, instr(from_lane, ':') + 1) GLOB '[A-Za-z0-9]*'
            AND substr(from_lane, instr(from_lane, ':') + 1)
                NOT GLOB '*[^A-Za-z0-9._-]*'
        )
    ),

    session_id      TEXT,
    run_id          TEXT,
    task_id         TEXT,
    requested_at    TEXT NOT NULL,

    -- Bounded and non-empty: a rationale is evidence of need, and an unbounded
    -- one is an injection surface pointed at every operator who reads it.
    rationale       TEXT NOT NULL CHECK (length(rationale) BETWEEN 1 AND 500),

    -- The discriminated union, discriminant hoisted into its own column so the
    -- closed set is a CHECK and not a convention buried in JSON.
    request_kind    TEXT NOT NULL
        CHECK (request_kind IN ('tool', 'skill', 'key_scope', 'extra_agent')),
    request_json    TEXT NOT NULL CHECK (json_valid(request_json)),

    -- (tier, effort, capabilities[]) carried DOWN a cascade rung as evidence of
    -- need. It is never authority: the delta still traverses this whole spine.
    inherited_floor_json TEXT
        CHECK (inherited_floor_json IS NULL OR json_valid(inherited_floor_json))
);

CREATE INDEX idx_capability_requests_agent ON capability_requests(from_agent_id, requested_at);
CREATE INDEX idx_capability_requests_kind ON capability_requests(request_kind, requested_at);

CREATE TRIGGER capability_request_is_immutable
BEFORE UPDATE ON capability_requests
BEGIN
    SELECT RAISE(ABORT, 'a capability request envelope is immutable; mint a new request');
END;
