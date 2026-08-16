-- W3: Project hierarchy + scoped conversations.
--
-- Two additive changes that turn the flat W2 project table into a bounded tree
-- and give every project *and* task an ordered message log:
--
--   1. projects.parent_project_id -- NULL means a top-level project; a set value
--      makes the row a sub-project of that parent. Nullable + self-referential so
--      existing rows stay top-level and the DAL can enforce "parent must exist"
--      and "no cycles" on top.
--   2. conversations -- a generic, scope-keyed message log. scope_type/scope_id
--      point at either a project ('project') or a task ('task'); seq is a
--      per-scope monotonic counter (append assigns MAX(seq)+1). The UNIQUE
--      constraint makes a duplicated seq a hard error rather than silent reorder.

ALTER TABLE projects ADD COLUMN parent_project_id TEXT REFERENCES projects(id);
CREATE INDEX idx_projects_parent ON projects(parent_project_id);

CREATE TABLE conversations (
    id          TEXT PRIMARY KEY,               -- 'cnv_' + uuid
    scope_type  TEXT NOT NULL,                  -- 'project' | 'task'
    scope_id    TEXT NOT NULL,                  -- projects.id or tasks.id
    seq         INTEGER NOT NULL,               -- per-scope 1-based order
    role        TEXT NOT NULL,                  -- 'user' | 'agent' | 'system'
    content     TEXT NOT NULL,
    model       TEXT,                           -- NULL unless an agent turn names one
    created_at  TEXT NOT NULL,
    meta_json   TEXT NOT NULL DEFAULT '{}',
    UNIQUE (scope_type, scope_id, seq)
);
CREATE INDEX idx_conversations_scope ON conversations(scope_type, scope_id);
