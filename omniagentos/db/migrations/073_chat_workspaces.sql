-- 073_chat_workspaces.sql
-- M1: Chats as first-class objects with hidden companion board tasks + org-linked folder projects.

CREATE TABLE IF NOT EXISTS chats (
    id             TEXT PRIMARY KEY,              -- 'cht_' + uuid
    project_id     TEXT,                          -- optional, references projects(id)
    board_task_id  TEXT NOT NULL UNIQUE,          -- companion task, references board_tasks(id)
    title          TEXT NOT NULL,
    status         TEXT NOT NULL DEFAULT 'active',
    promoted_at    TEXT,                          -- ISO-8601 when promoted
    meta_json      TEXT NOT NULL DEFAULT '{}',
    created_at     TEXT NOT NULL,
    updated_at     TEXT NOT NULL,
    FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE SET NULL,
    FOREIGN KEY (board_task_id) REFERENCES board_tasks(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_chats_project ON chats(project_id);
CREATE INDEX IF NOT EXISTS idx_chats_board_task ON chats(board_task_id);

-- Alter board_tasks to add origin column: 'board' or 'chat'
ALTER TABLE board_tasks ADD COLUMN origin TEXT NOT NULL DEFAULT 'board' CHECK (origin IN ('board', 'chat'));
CREATE INDEX IF NOT EXISTS idx_board_tasks_origin ON board_tasks(origin);

-- Alter projects to add org_company_id and org_product_id columns
ALTER TABLE projects ADD COLUMN org_company_id TEXT REFERENCES org_companies(id);
ALTER TABLE projects ADD COLUMN org_product_id TEXT REFERENCES org_products(id);
CREATE INDEX IF NOT EXISTS idx_projects_org_co ON projects(org_company_id);
CREATE INDEX IF NOT EXISTS idx_projects_org_prod ON projects(org_product_id);
