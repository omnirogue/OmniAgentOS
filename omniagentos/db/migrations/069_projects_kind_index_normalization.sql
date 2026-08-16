-- 069_projects_kind_index_normalization.sql
-- M-06: migration 064 existed in two released forms. Older databases created
-- idx_projects_kind_parent(kind, parent_project_id), while fresh databases
-- created idx_projects_kind(kind). Normalize both histories forward without
-- rewriting the already-applied 064 file.

DROP INDEX IF EXISTS idx_projects_kind_parent;
DROP INDEX IF EXISTS idx_projects_kind;
CREATE INDEX idx_projects_kind ON projects(kind);
