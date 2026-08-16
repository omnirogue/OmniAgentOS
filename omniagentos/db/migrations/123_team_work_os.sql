-- Migration 123: Team Work OS core data layer.
--
-- Turns the agent-only board (004) into a board humans and agents share: a
-- one-level parent/child hierarchy, an owning EMPLOYEE (098's roster) beside
-- the existing agent `claimed_by`, a ladder up to a company goal, and the two
-- append-only spines the productivity view is computed from — EVIDENCE (what
-- actually happened, deterministically attributed) and EVENTS (who moved the
-- card, and from where to where).
--
-- Every statement is additive. `board_tasks` and `company_goals` gain nullable
-- or defaulted columns only, so every pre-123 row stays valid and every
-- existing writer keeps working untouched.
--
-- Append-only (M-06): once applied, this file's checksum is frozen. Never edit
-- it — ship a forward migration instead.

-- --------------------------------------------------------------------------
-- board_tasks: hierarchy, human ownership, and the verification spine
-- --------------------------------------------------------------------------

-- Depth is capped at ONE (a parent has no parent) app-side in
-- omniagentos/collab/store.py; SQLite cannot express that constraint, and a
-- CHECK that could would still not see the other row.
ALTER TABLE board_tasks ADD COLUMN parent_task_id TEXT REFERENCES board_tasks(id);

-- The ladder from work to why. NULL = the card is not yet tied to a goal;
-- never a guessed default (same discipline as 087's project scope).
ALTER TABLE board_tasks ADD COLUMN goal_id TEXT REFERENCES company_goals(id);

-- The PERSON accountable. Deliberately a second axis beside `claimed_by`
-- (agents) rather than a reuse of it: an agent card and a human card must be
-- distinguishable, because the done-gate below only binds owned human work.
ALTER TABLE board_tasks ADD COLUMN owner_employee_id TEXT REFERENCES employees(id);

-- External handle for the card (PR/issue/doc key). Unique when present — see
-- the partial index below.
ALTER TABLE board_tasks ADD COLUMN ref TEXT;

ALTER TABLE board_tasks ADD COLUMN size TEXT NOT NULL DEFAULT 'M'
CHECK (size IN ('S', 'M', 'L'));

-- Non-empty acceptance criteria is what makes a card VERIFIABLE, so it is half
-- the predicate that arms the evidence-backed done-gate.
ALTER TABLE board_tasks ADD COLUMN acceptance_criteria TEXT NOT NULL DEFAULT '';

-- A blocked card must say what it is blocked ON; enforced app-side on the
-- transition, defaulted empty here so existing rows stay valid.
ALTER TABLE board_tasks ADD COLUMN blocked_reason TEXT NOT NULL DEFAULT '';

ALTER TABLE board_tasks ADD COLUMN verified_at TEXT;
ALTER TABLE board_tasks ADD COLUMN verified_by TEXT;
ALTER TABLE board_tasks ADD COLUMN due_date TEXT;

-- Where the card came from (standup, transcript, customer reply, ...).
ALTER TABLE board_tasks ADD COLUMN source TEXT NOT NULL DEFAULT '';

CREATE INDEX IF NOT EXISTS idx_board_tasks_parent
ON board_tasks(parent_task_id);

-- The team-queue read: one employee's cards, bucketed by status.
CREATE INDEX IF NOT EXISTS idx_board_tasks_owner_status
ON board_tasks(owner_employee_id, status);

CREATE INDEX IF NOT EXISTS idx_board_tasks_goal
ON board_tasks(goal_id);

-- PARTIAL unique index, not UNIQUE(ref): under a plain UNIQUE constraint NULLs
-- never collide, but the column would then also be an index every unreffed card
-- pays for. Uniqueness applies exactly where a ref exists (098's idiom).
CREATE UNIQUE INDEX IF NOT EXISTS idx_board_tasks_ref
ON board_tasks(ref)
WHERE ref IS NOT NULL;

-- --------------------------------------------------------------------------
-- company_goals: the person accountable for the goal
-- --------------------------------------------------------------------------

ALTER TABLE company_goals ADD COLUMN owner_employee_id TEXT REFERENCES employees(id);

-- --------------------------------------------------------------------------
-- Roster roles. 098 seeds the four people with role NULL on purpose ("this
-- lane does not invent titles"); the Team Work OS needs three of them named
-- because its verification rule reads them. Guarded on NULL/'' so an operator
-- who already set a title is never overwritten, and a no-op on a database
-- seeded after this migration (the seed keeps its own NULL role — those rows
-- are titled through the API, not here).
-- --------------------------------------------------------------------------

UPDATE employees SET role = 'operator'
WHERE id = 'emp_owner' AND (role IS NULL OR role = '');

UPDATE employees SET role = 'reviewer-merger'
WHERE id = 'emp_alice' AND (role IS NULL OR role = '');

UPDATE employees SET role = 'candidate-author'
WHERE id = 'emp_bob' AND (role IS NULL OR role = '');

-- --------------------------------------------------------------------------
-- task_evidence — what actually happened, attributed to a card
--
-- Rows arrive from deterministic collectors (a commit trailer, a merged PR, a
-- test run). A collector that cannot name the card writes task_id NULL: an
-- UNATTRIBUTED row an operator reattributes later, which is strictly better
-- than guessing an owner and inflating someone's numbers.
--
-- ON DELETE SET NULL, not CASCADE: purging an archived card (see
-- CollabStore.purge_archived_board_tasks) must not destroy the record that the
-- work happened — it un-attributes it, which is a state this table already
-- models.
-- --------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS task_evidence (
    id           TEXT PRIMARY KEY,              -- new_id('tev')
    task_id      TEXT REFERENCES board_tasks(id) ON DELETE SET NULL,  -- NULL = unattributed
    kind         TEXT NOT NULL CHECK (kind IN (
                     'commit', 'pr', 'review', 'session', 'test_run',
                     'deploy', 'doc', 'customer_reply', 'research', 'note')),
    ref          TEXT NOT NULL,                 -- sha | PR number | session id | url
    repo         TEXT NOT NULL DEFAULT '',
    actor        TEXT NOT NULL DEFAULT '',      -- employee id | agent id | login
    title        TEXT NOT NULL DEFAULT '',
    -- 'manual' marks a human correction, which no later deterministic sweep is
    -- allowed to overwrite (TeamStore.is_manual guards that).
    attribution  TEXT NOT NULL DEFAULT 'deterministic'
                 CHECK (attribution IN ('deterministic', 'manual')),
    confidence   REAL NOT NULL DEFAULT 1.0,
    -- Not every artifact is CREDIT: a rejected review, a reverted commit, or a
    -- card that took excessive attempts is evidence that must not count as
    -- verified output.
    quality_gate TEXT NOT NULL DEFAULT 'pass'
                 CHECK (quality_gate IN ('pass', 'rejected', 'reverted', 'excessive_attempts')),
    meta_json    TEXT NOT NULL DEFAULT '{}',
    created_at   TEXT NOT NULL,
    -- Idempotency key for re-runnable collectors: the same artifact re-collected
    -- is the SAME row, never a second one inflating the count.
    UNIQUE (kind, repo, ref)
);

CREATE INDEX IF NOT EXISTS idx_evidence_task ON task_evidence(task_id);

-- The operator's reattribution inbox, oldest first. Partial so it stays the
-- size of the backlog rather than the size of the table.
CREATE INDEX IF NOT EXISTS idx_evidence_unattr
ON task_evidence(created_at)
WHERE task_id IS NULL;

-- --------------------------------------------------------------------------
-- task_events — the card's audit trail (append-only, one row per mutation)
--
-- ON DELETE CASCADE: an event has no meaning without its card, and purging an
-- archived card must not fail on a dangling reference.
-- --------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS task_events (
    id          TEXT PRIMARY KEY,               -- new_id('tve')
    task_id     TEXT NOT NULL REFERENCES board_tasks(id) ON DELETE CASCADE,
    actor       TEXT NOT NULL,                  -- employee id | agent id | 'system'
    event       TEXT NOT NULL CHECK (event IN (
                    'status_change', 'assign', 'verify', 'unverify',
                    'block', 'comment', 'evidence', 'create')),
    from_status TEXT,
    to_status   TEXT,
    note        TEXT NOT NULL DEFAULT '',
    created_at  TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_task_events_task ON task_events(task_id, created_at);

-- --------------------------------------------------------------------------
-- prod_snapshots — one immutable row per (day, employee)
--
-- A snapshot, not a live query: the productivity numbers must be reproducible
-- for a past day even after cards move. Nullable columns are the ones that are
-- genuinely UNMEASURABLE on a day with no sessions — NULL, never a favourable 0.
-- --------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS prod_snapshots (
    day                 TEXT NOT NULL,          -- YYYY-MM-DD (UTC)
    employee_id         TEXT NOT NULL,
    verified_points     INTEGER NOT NULL DEFAULT 0,
    verified_outcomes   INTEGER NOT NULL DEFAULT 0,
    avg_active_sessions REAL,
    peak_sessions       INTEGER,
    merged_prs          INTEGER,
    first_pass_rate     REAL,
    production_x        REAL,
    breakdown_json      TEXT NOT NULL DEFAULT '{}',
    PRIMARY KEY (day, employee_id)
);
