-- Project provisioning: the auto-grant-vs-escalate audit trail.
--
-- When an orchestrator provisions a project it scopes it to exactly the folders
-- and connector APIs a goal needs (projects.root_dirs_json / allowed_dirs_json /
-- allowed_tools_json, migration 014). Mid-task an agent/run may REQUEST more
-- scope: a normal dir or a read connector is auto-granted; a hard-stop scope (a
-- money-move/transfer connector, a delete-capable scope, or a dir outside the
-- user's home) becomes an escalation that a human must approve.
--
-- Every such request -- granted or escalated -- lands here so the UI can show
-- exactly what a project asked for, what was auto-granted, and what is parked
-- awaiting approval. The projects tables stay the source of truth for the
-- effective scope; this table is the decision log over them.
CREATE TABLE project_scope_requests (
    id            TEXT PRIMARY KEY,          -- 'psr_' + uuid
    project_id    TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    run_id        TEXT,                      -- requesting run, NULL if operator-initiated
    scope_kind    TEXT NOT NULL,             -- 'dir' | 'connector'
    scope_value   TEXT NOT NULL,             -- absolute dir path, or 'connector.action' capability id
    action_class  TEXT,                      -- contracts.ActionClass for connector requests; NULL for dirs
    decision      TEXT NOT NULL,             -- 'auto_granted' | 'escalated'
    status        TEXT NOT NULL,             -- 'granted' | 'pending' | 'denied'
    reason        TEXT NOT NULL DEFAULT '',
    created_at    TEXT NOT NULL
);

CREATE INDEX idx_scope_requests_project ON project_scope_requests(project_id);
