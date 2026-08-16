-- Migration 100: durable plans (LANE A / t/cb-plans).
--
-- Backs the plan job store in omniagentos/api/routes/intake.py with a durable
-- table. The in-memory ``_PLAN_JOBS`` dict stays as a process-local cache, but
-- it is no longer the system of record: a plan that reached ``ready`` is
-- readable AND confirmable after the API restarts or after the cache evicts it.
--
-- The row therefore has to carry everything confirm needs, not just the plan
-- text: goal/harness/execute_mode/speed and the route decision were previously
-- process-local only, so a post-restart confirm had a plan and no route and
-- could not run.
--
-- Column notes:
--   plan_json          ProjectPlan.model_dump() as JSON (the planned hierarchy).
--   route_json         RouteDecision.model_dump() as JSON (new vs existing project).
--   route_target_name  Display name of the routed parent project, when routed.
--   goal/harness/execute_mode/speed  The dial as chosen when planning started;
--                      confirm re-reads them so an approval after a restart
--                      dispatches with the same posture the operator picked.
--   execution_metadata JSON captured at confirm: the preflight-derived facts that
--                      determine execution (owned paths, formation, confidence)
--                      plus the cards/runs the approval produced. It is a durable
--                      record of the approved execution parameters, read back by
--                      GET /api/intake/plan/{job_id}; it is NOT (yet) re-enforced
--                      by the runtime, so do not read it as a runtime guarantee.
--   decided_by/decided_at/reason  The approval audit: who decided, when, and why.
--
-- The status vocabulary (draft|ready|approved|rejected|superseded) is validated
-- app-side in omniagentos/plans/models.py and enforced by
-- omniagentos/plans/store.py on BOTH create and update, matching the repo's
-- stated convention (Migrations 098/099) so the vocabulary can grow without
-- rebuilding the table. The same seam refuses a transition out of a terminal
-- state (the one exception is approved -> superseded) and refuses a second
-- concurrently-approved plan for one source.
--
-- Binding contract: tests/db/test_migration_plans_durable.py.
--
-- Migration execution is serialized by omniagentos.db.migrate and every
-- application connection enables PRAGMA foreign_keys=ON. This migration is
-- idempotent and append-only: once applied, change schema with a new migration.

CREATE TABLE IF NOT EXISTS plans (
    id                 TEXT PRIMARY KEY,              -- planjob_... / pln_...
    chat_id            TEXT NULL REFERENCES chats(id) ON DELETE CASCADE,
    board_task_id      TEXT NULL REFERENCES board_tasks(id) ON DELETE CASCADE,
    version            INTEGER NOT NULL,              -- 1-indexed, per plan generation
    status             TEXT NOT NULL DEFAULT 'draft', -- draft|ready|approved|rejected|superseded (app-side)
    plan_json          TEXT NOT NULL,                 -- ProjectPlan.model_dump() as JSON
    content_md         TEXT NULL,                     -- rendered markdown for the preview/approval UI
    goal               TEXT NULL,                     -- the clarified ask this plan answers
    harness            TEXT NULL,                     -- harness pin chosen at kickoff
    execute_mode       TEXT NULL,                     -- readonly|tools|session|swarm|single|auto
    speed              TEXT NULL,                     -- fast|auto|ultra (D10 dial)
    route_json         TEXT NULL,                     -- RouteDecision.model_dump() as JSON
    route_target_name  TEXT NULL,                     -- routed parent project's display name
    decided_by         TEXT NULL,                     -- who approved/rejected/superseded
    decided_at         TEXT NULL,                     -- ISO-8601 when decided
    reason             TEXT NULL,                     -- reason for rejection/supersession
    org_company_id     TEXT NULL REFERENCES org_companies(id),
    work_folder        TEXT NULL,                     -- resolved ~/Work-relative path
    execution_metadata TEXT NULL,                     -- approved execution parameters (JSON)
    created_at         TEXT NOT NULL,
    updated_at         TEXT NOT NULL
);

-- Index for chat-based plan lookups and management.
CREATE INDEX IF NOT EXISTS idx_plans_chat
    ON plans(chat_id);

-- Index for task-based plan lookups (which plan produced this card).
CREATE INDEX IF NOT EXISTS idx_plans_task
    ON plans(board_task_id);

-- Index for status filtering (common query: list all ready|approved plans).
CREATE INDEX IF NOT EXISTS idx_plans_status
    ON plans(status);

-- Index for company filtering.
CREATE INDEX IF NOT EXISTS idx_plans_company
    ON plans(org_company_id);

-- UNIQUE(chat_id, version) for plans rooted on a chat: version is app-assigned
-- and incremented per generation, so a repeat is a double-write, not a v2.
CREATE UNIQUE INDEX IF NOT EXISTS idx_plans_chat_version
    ON plans(chat_id, version)
    WHERE chat_id IS NOT NULL;

-- UNIQUE(board_task_id, version) for plans rooted on a task.
CREATE UNIQUE INDEX IF NOT EXISTS idx_plans_task_version
    ON plans(board_task_id, version)
    WHERE board_task_id IS NOT NULL;

-- At most ONE non-terminal (draft|ready) plan per chat, and one per task: two
-- planners racing on the same source cannot both leave a pending plan behind,
-- so the operator is never shown two competing "approve this" cards for one
-- source. The loser takes sqlite3.IntegrityError at COMMIT.
--
-- SCOPE, precisely: these two indexes bind any writer that roots a plan on a
-- chat or a card. The /api/intake/plan route is NOT such a writer today — it
-- creates source-less rows (chat_id and board_task_id both NULL) while planning
-- and only sets board_task_id at confirm time, by which point the row is
-- already terminal ('approved'). They are exercised directly against the DAL in
-- tests/db/test_migration_plans_durable.py; no claim is made here that the
-- intake route's own concurrency is constrained by them.
CREATE UNIQUE INDEX IF NOT EXISTS idx_plans_chat_nonterminal
    ON plans(chat_id)
    WHERE chat_id IS NOT NULL AND status IN ('draft', 'ready');

CREATE UNIQUE INDEX IF NOT EXISTS idx_plans_task_nonterminal
    ON plans(board_task_id)
    WHERE board_task_id IS NOT NULL AND status IN ('draft', 'ready');
