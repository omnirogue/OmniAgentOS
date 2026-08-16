-- Migration A (JG-1) — UNNUMBERED staging copy.
-- Number is allocated at merge by P0-MIGRATION-AUTH (GMP-6). Never commit a
-- numbered file under omniagentos/db/migrations/ from this lane.
--
-- Maps OmniAgentOS projects to live Jira project keys (ACM·CA·INI·HOO·OAOS).
-- Partial unique index: multiple NULLs allowed; non-NULL keys are unique.

ALTER TABLE projects ADD COLUMN jira_project_key TEXT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS idx_projects_jira_project_key
  ON projects(jira_project_key) WHERE jira_project_key IS NOT NULL;
