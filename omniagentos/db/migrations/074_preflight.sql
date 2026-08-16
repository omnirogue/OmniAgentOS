-- 074_preflight.sql
-- Add preflight_json column to board_tasks to store deploy-time intelligence results (category, scopes, skills, formation, parallelism).
ALTER TABLE board_tasks ADD COLUMN preflight_json TEXT;
