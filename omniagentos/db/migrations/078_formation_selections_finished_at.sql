-- 078_formation_selections_finished_at.sql
-- Add finished_at column to formation_selections to record outcome-telemetry timestamps.

ALTER TABLE formation_selections ADD COLUMN finished_at TEXT;
