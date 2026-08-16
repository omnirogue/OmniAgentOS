-- Broker call tokens (additive migration 006).
--
-- An agent must be able to invoke a capability, but it must NOT be able to tell
-- the broker what its own grant is. Passing the grant to the harness in an
-- environment variable would be exactly that mistake: any agent holding `shell`
-- could rewrite OMNI_GRANT and hand itself stripe_acmeuni.refund.
--
-- So the agent receives an opaque, per-run bearer token and nothing else. The
-- broker resolves token -> run -> agent -> grant server-side, from the database.
-- The token names the agent; it does not describe its powers. Forging a wider
-- grant would require forging another run's token, and stealing one only ever
-- yields the access that run already had.
--
-- Tokens are single-run and expire. Never edits earlier migrations.

CREATE TABLE broker_tokens (
    token      TEXT PRIMARY KEY,        -- opaque; secrets.token_urlsafe(32)
    run_id     TEXT NOT NULL,
    agent_id   TEXT NOT NULL,
    issued_at  TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    revoked    INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX idx_broker_tokens_run ON broker_tokens(run_id);

-- Every brokered call, logged. This is the answer to "what did my agents actually
-- do last night?" -- which is the question you ask after leaving loops running
-- unattended. Append-only; never records a credential, only the capability, the
-- method+path, and the outcome.
CREATE TABLE broker_calls (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id        TEXT NOT NULL,
    agent_id      TEXT NOT NULL,
    capability_id TEXT NOT NULL,
    method        TEXT NOT NULL DEFAULT '',
    path          TEXT NOT NULL DEFAULT '',
    allowed       INTEGER NOT NULL,      -- 0 = denied
    reason        TEXT NOT NULL DEFAULT '',  -- denial reason, or '' when allowed
    status        INTEGER,               -- upstream HTTP status when the call ran
    ts            TEXT NOT NULL
);

CREATE INDEX idx_broker_calls_run ON broker_calls(run_id, ts);
CREATE INDEX idx_broker_calls_cap ON broker_calls(capability_id, ts);
