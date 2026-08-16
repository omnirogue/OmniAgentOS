-- Operator guidance is durable so a bridge can apply it at the next safe turn.
CREATE TABLE session_messages (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    message TEXT NOT NULL,
    queued_at TEXT NOT NULL,
    applied_at TEXT,
    created_by TEXT,
    FOREIGN KEY (session_id) REFERENCES sessions(id)
);
CREATE INDEX idx_session_messages_pending ON session_messages(session_id, applied_at, queued_at);
