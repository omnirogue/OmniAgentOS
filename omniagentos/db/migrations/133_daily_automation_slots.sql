-- Migration 133: the daily automation slots (the operator's ruling, 2026-08-14).
--
-- Q1 of the accountability spec was answered: every dev commits to THREE new
-- automations or skills a day, on top of the existing improvement slot. A
-- commitment table that can only say "task" or "improvement" cannot record
-- that, and three slots a day means the one-row-per-(day, person, kind) rule
-- has to become one row per SLOT.
--
-- Two changes, both to `team_commitments` (migration 132, four days old):
--   1. `kind` widens to ('task', 'improvement', 'automation');
--   2. a new `slot` INTEGER NOT NULL DEFAULT 1, and the single-improvement
--      partial unique index becomes UNIQUE(day, employee_id, kind, slot) over
--      BOTH slotted kinds — so a person has exactly one improvement slot and
--      exactly three automation slots per day, and a generator that re-runs
--      collides with itself instead of minting a fourth.
--
-- SQLite cannot widen a CHECK in place, so this is a copy-drop-rename rebuild.
-- ROWID PRESERVATION IS NOT LOAD-BEARING HERE, and that is a deliberate,
-- checked statement rather than an omission: nothing reads `team_commitments`
-- by rowid. `TeamStore.list_commitments` orders by (day, employee_id, kind,
-- created_at, rowid) — rowid only breaks ties between two rows written in the
-- same second for the same person and kind, where the rows are already
-- distinguished by their slot. Contrast `task_events` (migration 132), whose
-- first-verify lookup DOES depend on rowid order and whose rebuild therefore
-- copies rowid explicitly. Row CONTENT preservation is load-bearing, and the
-- guard below asserts it.
--
-- `slot` backfills to 1 for every existing row: every pre-133 commitment is
-- either a task (unslotted, and the task index does not read the column) or
-- the day's single improvement, which IS slot 1.
--
-- Append-only (M-06): once applied, this file's checksum is frozen. Never edit
-- it — ship a forward migration instead.

CREATE TABLE team_commitments_133 (
    id               TEXT PRIMARY KEY,               -- new_id('tcm')
    day              TEXT NOT NULL,                  -- YYYY-MM-DD (LOCAL date)
    employee_id      TEXT NOT NULL REFERENCES employees(id),
    -- NULL for a slotted commitment (it names no card up front) and for a task
    -- commitment whose card was later purged.
    task_id          TEXT REFERENCES board_tasks(id) ON DELETE SET NULL,
    -- 'automation' is the 2026-08-14 ruling: three a day, every day, per dev.
    kind             TEXT NOT NULL CHECK (kind IN ('task', 'improvement', 'automation')),
    -- Which of that kind's daily slots this row is. 1 for a task commitment
    -- (unslotted — its identity is its card) and for the single improvement;
    -- 1..3 for the automation slots. CHECKed rather than merely conventional:
    -- the unique index below only stops a DUPLICATE slot, so without this a
    -- generator bug could mint slot 4 (a fourth daily automation nobody
    -- promised) or slot 0/-1 (which Python's negative indexing would then
    -- resolve against the END of the qualifying list — a slot silently filled
    -- by the wrong card).
    slot             INTEGER NOT NULL DEFAULT 1
                     CHECK ((kind IN ('task', 'improvement') AND slot = 1)
                            OR (kind = 'automation' AND slot BETWEEN 1 AND 3)),
    title            TEXT NOT NULL DEFAULT '',
    -- What "delivered" would look like, written down BEFORE the day starts, so
    -- the resolution is a check rather than a negotiation.
    expected_outcome TEXT NOT NULL DEFAULT '',
    status           TEXT NOT NULL DEFAULT 'committed'
                     CHECK (status IN ('committed', 'delivered', 'missed', 'carried')),
    -- Who put this row here: the 06:55 generator, the operator, or the person.
    source           TEXT NOT NULL DEFAULT 'auto'
                     CHECK (source IN ('auto', 'operator', 'self')),
    -- SELF-reference, and it must be declared against the REBUILD's name.
    -- Pointing it at `team_commitments` would aim it at the OLD table, which
    -- this migration drops three statements below: with PRAGMA foreign_keys=ON
    -- (the migration runner's state), DROP TABLE performs an implicit DELETE of
    -- the parent's rows, and any pre-existing carry chain copied into the new
    -- table would fail the constraint and abort the whole migration. Declared
    -- against `team_commitments_133`, the reference stays inside the table
    -- being built, and SQLite REWRITES it to the final name during the RENAME
    -- below (verified: the stored SQL reads `REFERENCES "team_commitments"(id)`
    -- afterwards), so the shipped schema is self-referential either way — only
    -- one of the two spellings survives a database that has carried anything.
    carried_from     TEXT REFERENCES team_commitments_133(id),
    resolved_at      TEXT,
    -- WHO resolved it: 'system' for the deterministic morning pass, an employee
    -- id when an operator ruled on it by hand. Without this, an auto-miss and an
    -- operator override are the same row, and only one of them is arguable.
    resolved_by      TEXT,
    resolution_note  TEXT NOT NULL DEFAULT '',
    created_at       TEXT NOT NULL,
    updated_at       TEXT NOT NULL
);

INSERT INTO team_commitments_133 (
    id, day, employee_id, task_id, kind, slot, title, expected_outcome, status,
    source, carried_from, resolved_at, resolved_by, resolution_note, created_at, updated_at
)
SELECT
    id, day, employee_id, task_id, kind, 1, title, expected_outcome, status,
    source, carried_from, resolved_at, resolved_by, resolution_note, created_at, updated_at
FROM team_commitments;

-- Row-count equality, expressed the only way plain SQL can raise: a CHECK that
-- a 0 violates (SQLite has no bare RAISE outside a trigger). Commitments are
-- the accountability record — a rebuild that dropped one would erase a promise
-- somebody made, silently.
CREATE TEMP TABLE migration_133_copy_guard (
    ok INTEGER NOT NULL CHECK (ok = 1)
);

INSERT INTO migration_133_copy_guard (ok)
SELECT CASE
    WHEN (SELECT COUNT(*) FROM team_commitments_133) = (SELECT COUNT(*) FROM team_commitments)
    THEN 1 ELSE 0
END;

DROP TABLE migration_133_copy_guard;

DROP TABLE team_commitments;

ALTER TABLE team_commitments_133 RENAME TO team_commitments;

-- Recreated by hand: a DROP takes the old table's indexes with it.

-- One commitment per (day, person, card). PARTIAL, because NULL task_ids must
-- not collide — slotted rows all carry NULL and all are legitimate.
CREATE UNIQUE INDEX IF NOT EXISTS idx_team_commitments_day_task
ON team_commitments(day, employee_id, task_id)
WHERE task_id IS NOT NULL;

-- The SLOT rule, replacing 132's single-improvement index: one row per slot,
-- for both slotted kinds. This is what makes the generator idempotent (a
-- re-run collides with its own rows) and what caps the automation commitment
-- at three a day rather than three per run.
CREATE UNIQUE INDEX IF NOT EXISTS idx_team_commitments_day_slot
ON team_commitments(day, employee_id, kind, slot)
WHERE kind IN ('improvement', 'automation');

-- The read every surface makes: one person's commitments for a day (and their
-- recent history).
CREATE INDEX IF NOT EXISTS idx_team_commitments_employee_day
ON team_commitments(employee_id, day);
