-- W2: Projects + scoped permissions.
--
-- Introduces a first-class "project" concept so work, budgets, tool grants and
-- filesystem reach can be scoped, and extends the global action-class policy
-- (configs/policy.yaml) with optional per-project overrides. Existing rows are
-- untouched: a NULL task.project_id means "unscoped / global", the pre-W2
-- behaviour.

CREATE TABLE projects (
    id                TEXT PRIMARY KEY,          -- 'proj_' + uuid
    name              TEXT NOT NULL UNIQUE,
    root_dirs_json    TEXT NOT NULL DEFAULT '[]', -- absolute repo/workspace roots
    vault_subfolder   TEXT NOT NULL DEFAULT '',   -- vault/ subfolder for this project's notes
    budget_usd        REAL,                        -- soft spend ceiling; NULL = unset
    allowed_tools_json TEXT NOT NULL DEFAULT '[]', -- tool/capability allowlist for this project
    allowed_dirs_json TEXT NOT NULL DEFAULT '[]',  -- dirs the runner may touch (enforced later)
    created_at        TEXT NOT NULL
);

-- Per-project overlay on the global action-class policy. A row here overrides the
-- global requires_approval/always_human for that (project, action_class). A
-- project may only *tighten* always_human (a globally hard-human class stays
-- hard-human); requires_approval may be raised or, for non-hard classes, relaxed.
CREATE TABLE project_permission_grants (
    project_id        TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    action_class      TEXT NOT NULL,             -- contracts.ActionClass
    requires_approval INTEGER NOT NULL DEFAULT 1,
    always_human      INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (project_id, action_class)
);

-- Associate work with a project. Additive + nullable so existing tasks keep
-- working; the API/board filter by this column.
ALTER TABLE tasks ADD COLUMN project_id TEXT REFERENCES projects(id);
CREATE INDEX idx_tasks_project ON tasks(project_id);
