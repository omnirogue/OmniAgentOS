-- U-E2 — the capability decision row: terminal-state CAS, the 90-second caller
-- wall, and the `grant_decision_ms` latency telemetry the floor SLO is measured
-- against.
--
-- ORDINAL: renumbered 109 -> 112 at Phase-2 integration (PLAN.md §4 trap rule
-- 3). Three lanes claimed 109; 109 and 110 were already applied live by
-- lane/cap-skills, and cap-us2 took 111 ahead of this lane by merge order. The
-- `schema_migrations` reconciliation owed by this renumber is a verified no-op
-- -- no database has a row for these bytes, and the live one has no
-- `capability_decisions` table. The full record, including the procedure that
-- WOULD be owed had it been applied, is on the commit allocating 111.
--
-- U-E2 keeps its position AHEAD of U-E1's envelope (113), per PLAN.md §4's
-- `provision/service.py` serialization (E2 -> E1): this table is the one the
-- policy service writes into, and the envelope references it by `request_id`.
--
-- WHY THIS IS ITS OWN TABLE AND NOT A COLUMN ON THE ENVELOPE
-- The request envelope (U-E1, migration 113) is IMMUTABLE: once a worker has
-- emitted `tool{echo.ping}` with a rationale, nothing may rewrite what it asked
-- for. The decision is the opposite shape -- it is written exactly once, later,
-- by whoever wins the compare-and-swap. Keeping them in one row would mean the
-- envelope's immutability trigger and the decision's CAS trigger fight over the
-- same UPDATE. Two tables joined by `request_id` lets each carry the guarantee
-- it actually needs, and the join is the one UUID that already threads
-- `agent_capabilities`, `capability_grant_log`, and `broker_calls` (migrations
-- 106/107). U-E2 lands first, so it owns this table; U-E1 fills the envelope
-- beside it.
--
-- THE TWO CLOCKS ARE VISIBLE IN THE SCHEMA
--   * `opened_at` -> the caller-visible 90s park wall is measured from here, so
--     it survives a process restart instead of living in one caller's memory.
--   * `grant_decision_ms` -> how long the DECISION took (floor SLO: p50 <100ms,
--     p95 <500ms). It is not the park duration and must never be read as one.
-- The operator decision window (minutes) is deliberately NOT stored here: a late
-- approval mints a FRESH request rather than resurrecting a timed-out one, and a
-- column for it would invite exactly the resurrection this design forbids.

CREATE TABLE capability_decisions (
    request_id         TEXT PRIMARY KEY NOT NULL,

    -- The holder the grant would be issued to. One identity spelling, program
    -- wide: `system` or `lane:|loop:|job:|human:` + a non-empty subject that
    -- starts alphanumeric. SQLite has no REGEXP, so the grammar of
    -- `omniagentos/reliability/store.py:_CANONICAL_IDENTITY_RE` is expressed
    -- with GLOB. FULLMATCH semantics, not prefix: `system_impostor`, `lane:`,
    -- `human:-x`, `lane:a:b` and `agent:bob` all fail this CHECK.
    holder_agent_id    TEXT NOT NULL CHECK (
        holder_agent_id = 'system'
        OR (
            (holder_agent_id GLOB 'lane:*' OR holder_agent_id GLOB 'loop:*'
             OR holder_agent_id GLOB 'job:*' OR holder_agent_id GLOB 'human:*')
            AND substr(holder_agent_id, instr(holder_agent_id, ':') + 1) GLOB '[A-Za-z0-9]*'
            AND substr(holder_agent_id, instr(holder_agent_id, ':') + 1)
                NOT GLOB '*[^A-Za-z0-9._-]*'
        )
    ),

    subject_kind       TEXT NOT NULL
        CHECK (subject_kind IN ('tool', 'skill', 'key_scope', 'extra_agent')),
    subject_id         TEXT NOT NULL CHECK (length(subject_id) > 0),

    -- `pending_operator` is the ONLY non-terminal state. The four terminal
    -- states are the complete set an agent may ever observe.
    state              TEXT NOT NULL DEFAULT 'pending_operator' CHECK (
        state IN ('pending_operator', 'granted', 'denied', 'hard_rejected', 'timed_out')
    ),
    reason_code        TEXT NOT NULL DEFAULT '',

    -- The balance rule as a constraint: every row carries a machine-actionable
    -- next move, and `await_operator` -- the only "keep waiting" answer -- is
    -- reachable ONLY while the row is still pending. "Pending" can therefore
    -- never be a terminal next_action, by schema rather than by convention.
    next_action        TEXT NOT NULL DEFAULT 'await_operator' CHECK (
        next_action IN ('retry_with_grant', 'escalate_tier', 'split_bounded',
                        'continue_degraded', 'await_operator', 'stop_terminal')
    ),

    opened_at          TEXT NOT NULL,
    decided_at         TEXT,
    grant_decision_ms  INTEGER CHECK (grant_decision_ms IS NULL OR grant_decision_ms >= 0),

    -- Only the floor or a NAMED human may issue a grant (access matrix §5.4).
    -- A config file, a lane, or an anonymous "operator" string cannot.
    granted_by         TEXT CHECK (
        granted_by IS NULL OR granted_by = 'autogrant' OR granted_by GLOB 'human:*'
    ),
    expires_at         TEXT,

    -- What the decision actually covers. `key_scope` expands to many capability
    -- ids, so both are arrays; `excluded_ids_json` NAMES the members a mixed
    -- group refused, which is what makes the receipt honest instead of silent.
    granted_ids_json   TEXT NOT NULL DEFAULT '[]' CHECK (json_valid(granted_ids_json)),
    excluded_ids_json  TEXT NOT NULL DEFAULT '[]' CHECK (json_valid(excluded_ids_json)),

    CHECK (state = 'pending_operator'
           OR (decided_at IS NOT NULL AND grant_decision_ms IS NOT NULL)),
    CHECK (state = 'pending_operator' OR next_action != 'await_operator'),
    CHECK (state != 'granted'
           OR (granted_by IS NOT NULL
               AND expires_at IS NOT NULL
               AND json_array_length(granted_ids_json) > 0))
);

CREATE INDEX idx_capability_decisions_state ON capability_decisions(state, opened_at);
CREATE INDEX idx_capability_decisions_holder ON capability_decisions(holder_agent_id, opened_at);

-- A terminal decision is final. The CAS itself is `UPDATE ... WHERE state =
-- 'pending_operator'`, which already makes a racing writer lose; this trigger is
-- the second, non-bypassable half -- a caller that forgets the WHERE clause, or
-- an operator route that "corrects" a timed-out row into a grant, aborts instead
-- of resurrecting it. A late approval mints a FRESH request id.
CREATE TRIGGER capability_decision_terminal_is_final
BEFORE UPDATE ON capability_decisions
WHEN OLD.state != 'pending_operator'
BEGIN
    SELECT RAISE(ABORT, 'capability decision is terminal; mint a fresh request');
END;

-- What was asked, and by whom, cannot drift once the row exists.
CREATE TRIGGER capability_decision_subject_is_immutable
BEFORE UPDATE ON capability_decisions
WHEN NEW.request_id IS NOT OLD.request_id
  OR NEW.holder_agent_id IS NOT OLD.holder_agent_id
  OR NEW.subject_kind IS NOT OLD.subject_kind
  OR NEW.subject_id IS NOT OLD.subject_id
  OR NEW.opened_at IS NOT OLD.opened_at
BEGIN
    SELECT RAISE(ABORT, 'capability decision subject and holder are immutable');
END;
