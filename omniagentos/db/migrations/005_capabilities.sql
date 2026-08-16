-- OmniAgentOS capability grants (additive migration 005).
--
-- Migration 004 gave agents a `trust_level` column, but nothing ever read it --
-- an agent's trust badge gated exactly nothing, because the runner only consulted
-- the *task's* tools_allowed list. This migration makes agent access real.
--
-- A row here is a standing grant: "agent X may use capability Y". The capability
-- id is namespaced (`stripe_acmeuni.read`, `piedpiper_acmeuni.note_write`) and is validated
-- against configs/connectors.yaml at write time -- the DB stores the grant, the
-- registry defines what a grant means, and the broker decides whether a given
-- call may proceed.
--
-- The agent grant is the CEILING. A task may narrow it; nothing may widen it.
-- Never edits earlier migrations. Applied after 004 by migrate_connection().

CREATE TABLE agent_capabilities (
    agent_id      TEXT NOT NULL REFERENCES agents(id) ON DELETE CASCADE,
    capability_id TEXT NOT NULL,          -- e.g. 'stripe_acmeuni.read'; validated vs registry
    granted_at    TEXT NOT NULL,
    granted_by    TEXT NOT NULL DEFAULT 'operator',
    note          TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (agent_id, capability_id)
);

CREATE INDEX idx_agent_caps_agent ON agent_capabilities(agent_id);
CREATE INDEX idx_agent_caps_cap ON agent_capabilities(capability_id);

-- Append-only audit of every grant change. Answering "when did this agent get the
-- ability to move an ad budget, and who gave it that?" must not depend on the
-- current state of agent_capabilities, so it gets its own table that is never
-- updated or deleted from.
CREATE TABLE capability_grant_log (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    agent_id      TEXT NOT NULL,
    capability_id TEXT NOT NULL,
    action        TEXT NOT NULL,          -- 'grant' | 'revoke'
    action_class  TEXT NOT NULL DEFAULT '',  -- snapshot: class at time of grant
    actor         TEXT NOT NULL DEFAULT 'operator',
    note          TEXT NOT NULL DEFAULT '',
    ts            TEXT NOT NULL
);

CREATE INDEX idx_grant_log_agent ON capability_grant_log(agent_id, ts);
