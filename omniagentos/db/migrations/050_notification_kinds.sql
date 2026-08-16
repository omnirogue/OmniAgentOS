-- Add the 'swarm_failed' notification kind.
--
-- The swarm terminal-failure bell previously reused kind='blocked' on
-- ref_type='board_task' -- the exact (kind, ref) pair longhaul's transient
-- capacity-wait notification uses on the same cards. Because the swarm bell's
-- dedupe is kind-aware and READ-AGNOSTIC (exactly one row per card/run,
-- forever), sharing the kind let either emitter permanently suppress the
-- other. The failure bell now gets its own kind so the two can never collide.
--
-- The kind constraint is an inline column CHECK, so widening it requires the
-- standard SQLite table rebuild (indexes are dropped with the old table and
-- recreated verbatim). No FK references notifications; data copies verbatim.

CREATE TABLE notifications_new (
    id           TEXT PRIMARY KEY,               -- contracts.new_id('ntf')
    kind         TEXT NOT NULL CHECK (
        kind IN ('approval', 'alert', 'escalation', 'done', 'blocked', 'info', 'swarm_failed')
    ),
    title        TEXT NOT NULL,
    body         TEXT NOT NULL DEFAULT '',
    severity     TEXT NOT NULL DEFAULT 'info',    -- info | warning | high | critical
    ref_type     TEXT,                            -- approval | run | session | alert | NULL
    ref_id       TEXT,                            -- id of the referenced entity (nullable)
    created_at   TEXT NOT NULL,                   -- contracts.utc_now_iso()
    read_at      TEXT,                            -- NULL until the operator reads it
    acted_at     TEXT,                            -- NULL until acted on (e.g. approved from panel)
    payload_json TEXT NOT NULL DEFAULT '{}'       -- extra context (approval_id, action_class, ...)
);

INSERT INTO notifications_new
    (id, kind, title, body, severity, ref_type, ref_id,
     created_at, read_at, acted_at, payload_json)
SELECT id, kind, title, body, severity, ref_type, ref_id,
       created_at, read_at, acted_at, payload_json
FROM notifications;

DROP TABLE notifications;
ALTER TABLE notifications_new RENAME TO notifications;

CREATE INDEX idx_notifications_created_at ON notifications(created_at DESC);
CREATE INDEX idx_notifications_read_at ON notifications(read_at);
CREATE INDEX idx_notifications_ref ON notifications(ref_type, ref_id);
