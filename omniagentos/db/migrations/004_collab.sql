-- OmniAgentOS collaboration schema (additive migration 004): agent registry + a
-- self-assignable task board + direct/group messaging + a persisted conversation log.
-- Never edits earlier migrations. Applied after 003 by migrate_connection().

CREATE TABLE agents (
    id          TEXT PRIMARY KEY,       -- new_id('agt')
    name        TEXT NOT NULL,
    lineage     TEXT NOT NULL DEFAULT '',
    model       TEXT,
    expertise_json TEXT NOT NULL DEFAULT '[]',  -- skill/discipline tags for self-assignment
    trust_level TEXT NOT NULL DEFAULT 'T1',
    status      TEXT NOT NULL DEFAULT 'idle',
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL,
    UNIQUE(name)
);

CREATE TABLE board_tasks (
    id                 TEXT PRIMARY KEY,  -- new_id('btk')
    title              TEXT NOT NULL,
    description        TEXT NOT NULL DEFAULT '',
    required_expertise_json TEXT NOT NULL DEFAULT '[]',
    discipline         TEXT,
    priority           TEXT NOT NULL DEFAULT 'normal',
    status             TEXT NOT NULL DEFAULT 'open',
    claimed_by         TEXT,             -- agent id
    claim_version      INTEGER NOT NULL DEFAULT 0,  -- CAS: exactly one agent wins a claim
    result_ref         TEXT,
    created_at         TEXT NOT NULL,
    updated_at         TEXT NOT NULL
);
CREATE INDEX idx_board_status ON board_tasks(status, priority);

CREATE TABLE channels (
    id         TEXT PRIMARY KEY,         -- new_id('chn')
    name       TEXT NOT NULL,
    kind       TEXT NOT NULL,            -- direct | group | broadcast
    topic      TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);

CREATE TABLE channel_members (
    channel_id TEXT NOT NULL REFERENCES channels(id),
    agent_id   TEXT NOT NULL,
    PRIMARY KEY (channel_id, agent_id)
);

-- The conversation log: every message persisted, queryable, and rendered to the vault.
CREATE TABLE messages (
    id         TEXT PRIMARY KEY,         -- new_id('msg')
    channel_id TEXT NOT NULL REFERENCES channels(id),
    from_agent TEXT NOT NULL,            -- agent id | 'system' | 'operator'
    to_agent   TEXT,                     -- for direct messages
    kind       TEXT NOT NULL DEFAULT 'message',
    body       TEXT NOT NULL,
    ref        TEXT,                     -- linked task/run/experiment id
    ts         TEXT NOT NULL
);
CREATE INDEX idx_messages_channel ON messages(channel_id, ts);
