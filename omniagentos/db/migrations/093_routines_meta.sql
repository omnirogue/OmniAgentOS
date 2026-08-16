-- Migration 093: routines.scope + routines.purpose (Migration F / LOOPS-1)
-- Numbered by P0-MIGRATION-AUTH at integration gate completion 2026-07-30.
-- Source: migrations-staging/routines_meta.sql

ALTER TABLE routines ADD COLUMN scope TEXT;
ALTER TABLE routines ADD COLUMN purpose TEXT;

UPDATE routines
SET scope = 'system'
WHERE name IN (
    'improve-lane-dispatcher',
    'lab-jobs-drain',
    'memlife-dream-cycle'
)
  AND scope IS NULL;

UPDATE routines
SET scope = 'company',
    purpose = 'goal_review'
WHERE name = 'goal-review'
  AND scope IS NULL;
