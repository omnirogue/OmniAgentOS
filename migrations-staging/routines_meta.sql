-- Migration F (LOOPS-1) — routines.scope + routines.purpose
-- UNNUMBERED. P0-MIGRATION-AUTH assigns the version at merge from the
-- filesystem head (never hard-code a number here or in tests).
--
-- Two nullable TEXT columns. App-side validation owns the scope vocabulary
-- (personal|company|project|system); purpose is free text. NULL means
-- honest "untagged legacy" — no CHECK, no forced default.
--
-- In-SQL name-keyed backfill so no ml-wireB seeder file is touched:
--   improve-lane-dispatcher / lab-jobs-drain / memlife-dream-cycle → system
--   goal-review → company + purpose=goal_review (order-safe with JG-6)

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
