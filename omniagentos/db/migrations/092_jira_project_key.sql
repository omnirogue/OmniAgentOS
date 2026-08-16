-- Migration 092: projects.jira_project_key (Migration A / JG1-BE)
-- Numbered by P0-MIGRATION-AUTH at integration gate completion 2026-07-30.
-- Source: migrations-staging/jira_project_key.sql

ALTER TABLE projects ADD COLUMN jira_project_key TEXT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS idx_projects_jira_project_key
  ON projects(jira_project_key) WHERE jira_project_key IS NOT NULL;
