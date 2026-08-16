-- M1 — put a routine on the project axis.
--
-- ORDINAL: 116, computed from the filesystem (highest prefix on disk was 115)
-- immediately before this file was written. Migrations are append-only and
-- byte-immutable: nothing above was renumbered or edited to make room.
--
-- WHY THIS EXISTS
-- Every other unit of work in this system can already say which project it
-- belongs to: `tasks.project_id` (014), `runs.project_id` (058), `chats` (073),
-- `board_tasks` (087), and — since 115 — the capability grants and requests
-- that authorize a call. A ROUTINE could not. It is the one work-producing
-- object with no project column at all, so a scheduled loop that fires every
-- hour produced runs whose project was, at best, whatever its task template
-- happened to carry, and the questions the project axis exists to answer —
-- "what is running in project P", "what has project P spent", "may this
-- routine's executor use project P's grants" — had no answer for routines that
-- was better than a guess.
--
-- NULLABLE, AND NULL IS A REAL ANSWER
-- Existing rows get `project_id = NULL` and there is no backfill. NULL means
-- "this routine is not scoped to a project", which is the truth about every
-- routine written before this column existed: nothing recorded a project for
-- them, so inventing one — from the name, from the working dir, from a
-- "default project" — would be manufacturing an authorization fact, exactly
-- the failure migration 115 refused on the grant side. A routine that should
-- be scoped is scoped by an explicit write, and until then it reads as
-- unscoped rather than as belonging to somewhere it does not.
--
-- COMPATIBILITY WITH 115 (grant binding)
-- The broker treats a project-bound grant and an unscoped call as a REFUSAL
-- (`call_project_unknown`), not as a wildcard. So this column is what lets a
-- routine's work name the project its grants were issued for; leaving it NULL
-- keeps the pre-M1 posture (an unscoped call, matched only by unbound grants)
-- and never widens anything. Adding the column cannot, on its own, authorize a
-- call that was previously denied.
--
-- REFERENCES projects(id) — the same declaration 115 used. It is enforced on
-- connections that run with PRAGMA foreign_keys=ON (SqliteStore does), which is
-- what the routines DAL uses.

ALTER TABLE routines ADD COLUMN project_id TEXT REFERENCES projects(id);

-- "What is scheduled to run in this project?" is the per-project operations
-- question, and the status filter is always part of it.
CREATE INDEX IF NOT EXISTS idx_routines_project
    ON routines(project_id, status);
