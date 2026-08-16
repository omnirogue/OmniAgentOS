-- NT-notify: durable, inspectable notification feed.
--
-- Every notification the system raises (an approval request, a session parked
-- awaiting approval, a HIGH/critical Steward alert, a runner escalation, and
-- plain info/done/blocked signals) is PERSISTED as a row here BEFORE the
-- best-effort OS/push fires. A notification therefore always has a real,
-- inspectable target: ref_type + ref_id point at the approval / run / session /
-- alert it is about, so an "approval required" notification can never be a
-- phantom pointing at nothing. If the referenced approval is later resolved or
-- expired, the row still exists and the API reports the outcome instead of a
-- dead link -- so "notification but empty queue" is structurally impossible.
--
-- Additive and backward-compatible: no existing table is touched.

CREATE TABLE notifications (
    id           TEXT PRIMARY KEY,               -- contracts.new_id('ntf')
    kind         TEXT NOT NULL CHECK (
        kind IN ('approval', 'alert', 'escalation', 'done', 'blocked', 'info')
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

-- Newest-first listing is the hot path; unread filtering and per-target lookup
-- (idempotent "already notified about this approval?" checks) are next.
CREATE INDEX idx_notifications_created_at ON notifications(created_at DESC);
CREATE INDEX idx_notifications_read_at ON notifications(read_at);
CREATE INDEX idx_notifications_ref ON notifications(ref_type, ref_id);
