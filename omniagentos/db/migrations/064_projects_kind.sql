-- 064_projects_kind.sql
-- Portfolio redesign Phase A: durable projects vs orchestration scratch.
-- 'project'  durable, appears in the portfolio
-- 'scratch'  auto-created by orchestration dispatch, collapsed and swept

ALTER TABLE projects ADD COLUMN kind TEXT NOT NULL DEFAULT 'project';

-- Index on kind alone. Do NOT include parent_project_id here: tests that
-- simulate pre-031 schemas drop that column and would break if the index
-- referenced it (SQLite rewrites indexes on DROP COLUMN and errors).
CREATE INDEX IF NOT EXISTS idx_projects_kind ON projects(kind);

-- Backfill: stop depending on the name prefix for portfolio filtering.
UPDATE projects SET kind = 'scratch'
 WHERE name LIKE 'Orchestration:%' OR name LIKE 'Dispatch drill%';
