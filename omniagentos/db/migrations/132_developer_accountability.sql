-- Migration 132: developer accountability — daily commitments, a THIRD
-- completion state, and the automation-maturity axis.
--
-- The Team Work OS (123) can say a card is done and that somebody verified it.
-- It cannot say a verification was REFUSED, it cannot say what a person
-- promised to deliver today, and it cannot say how much of the work the system
-- did by itself. Those three gaps are what this migration closes, additively:
-- one new table, five nullable columns, and one CHECK widened on an existing
-- append-only table.
--
-- NUMBERING: the ratified workqueue plan (2026-08-13) reserved the names
-- 131-133 for its WP1. The governing rule is NEXT FREE AT MERGE TIME — whichever
-- change lands second renumbers its file. 130 was the highest prefix on disk
-- when this file was written.
--
-- Append-only (M-06): once applied, this file's checksum is frozen. Never edit
-- it — ship a forward migration instead.

-- --------------------------------------------------------------------------
-- team_commitments — what a person said they would finish, per LOCAL day
--
-- Generated deterministically at 06:55 from the queue (no LLM), resolved the
-- next morning against what the board actually recorded. A miss is a PRESERVED
-- ROW, never a deletion or a rewrite: accountability history that can be edited
-- away is not history. The carried follow-up is a NEW row pointing back at the
-- miss through `carried_from`, so "this slipped twice" is a query rather than a
-- memory.
--
-- `day` is a LOCAL YYYY-MM-DD date (the wall clock a person reads in the
-- morning DM), while every *_at timestamp in this schema stays UTC. Comparisons
-- convert UTC -> local; see omniagentos/team/commitments.py's docstring.
--
-- task_id ON DELETE SET NULL, for exactly the reason task_evidence carries the
-- same clause: purging an archived card (CollabStore.purge_archived_board_tasks)
-- must not destroy the record that somebody committed to it. carried_from has
-- NO cascade — commitment rows are never deleted, so there is nothing to
-- propagate.
-- --------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS team_commitments (
    id               TEXT PRIMARY KEY,               -- new_id('tcm')
    day              TEXT NOT NULL,                  -- YYYY-MM-DD (LOCAL date)
    employee_id      TEXT NOT NULL REFERENCES employees(id),
    -- NULL for an improvement commitment (it names no card up front) and for a
    -- task commitment whose card was later purged.
    task_id          TEXT REFERENCES board_tasks(id) ON DELETE SET NULL,
    kind             TEXT NOT NULL CHECK (kind IN ('task', 'improvement')),
    title            TEXT NOT NULL DEFAULT '',
    -- What "delivered" would look like, written down BEFORE the day starts, so
    -- the resolution is a check rather than a negotiation.
    expected_outcome TEXT NOT NULL DEFAULT '',
    status           TEXT NOT NULL DEFAULT 'committed'
                     CHECK (status IN ('committed', 'delivered', 'missed', 'carried')),
    -- Who put this row here: the 06:55 generator, the operator, or the person.
    source           TEXT NOT NULL DEFAULT 'auto'
                     CHECK (source IN ('auto', 'operator', 'self')),
    carried_from     TEXT REFERENCES team_commitments(id),
    resolved_at      TEXT,
    -- WHO resolved it: 'system' for the deterministic morning pass, an employee
    -- id when an operator ruled on it by hand. Without this, an auto-miss and an
    -- operator override are the same row, and only one of them is arguable.
    resolved_by      TEXT,
    resolution_note  TEXT NOT NULL DEFAULT '',
    created_at       TEXT NOT NULL,
    updated_at       TEXT NOT NULL
);

-- Idempotency for a generator that re-runs (and for the resolve/generate race
-- the carry-mint UPSERT resolves): one commitment per (day, person, card).
-- PARTIAL, because NULL task_ids must not collide — an improvement row and a
-- purged task row both carry NULL and both are legitimate.
CREATE UNIQUE INDEX IF NOT EXISTS idx_team_commitments_day_task
ON team_commitments(day, employee_id, task_id)
WHERE task_id IS NOT NULL;

-- Exactly ONE improvement slot per person per day. The slot is the spec's
-- governing principle ("every day makes the system more capable"), and two
-- slots would let a person satisfy it twice while another satisfies it never.
CREATE UNIQUE INDEX IF NOT EXISTS idx_team_commitments_day_improvement
ON team_commitments(day, employee_id, kind)
WHERE kind = 'improvement';

-- The read every surface makes: one person's commitments for a day (and their
-- recent history).
CREATE INDEX IF NOT EXISTS idx_team_commitments_employee_day
ON team_commitments(employee_id, day);

-- --------------------------------------------------------------------------
-- board_tasks: the failure half of verification, and the automation axis
--
-- Nullable, no backfill: an existing card has no failed verification and no
-- measured automation maturity, and inventing either would be a claim the
-- board cannot support. Vocabulary for automation_maturity is validated
-- APP-SIDE (CollabStore.update_board_task, like `priority`) rather than by a
-- CHECK, because 123 deliberately left the mutable board columns CHECK-free —
-- a vocabulary that widens should not require a table rebuild.
-- --------------------------------------------------------------------------

ALTER TABLE board_tasks ADD COLUMN automation_maturity TEXT;

-- "What could the system do itself next time" — free text, the human-reported
-- half of a measurement the estate cannot take automatically.
ALTER TABLE board_tasks ADD COLUMN automation_note TEXT;

ALTER TABLE board_tasks ADD COLUMN verification_failed_at TEXT;
ALTER TABLE board_tasks ADD COLUMN verification_failed_by TEXT;
ALTER TABLE board_tasks ADD COLUMN verification_failed_reason TEXT;

-- --------------------------------------------------------------------------
-- task_events: widen the event CHECK with 'verify_failed'
--
-- The card columns above are the CURRENT state; this event is the durable
-- RECORD. A later successful verify clears verification_failed_*, so without
-- an append-only event carrying the reason, the refusal — and why — would
-- vanish the moment it was fixed.
--
-- ROWID PRESERVATION IS LOAD-BEARING. TeamStore.verify_task finds the FIRST
-- verify event with `ORDER BY created_at ASC, rowid ASC` and re-stamps the
-- original timestamp, so a re-verify cannot move a card into a later scoring
-- week. created_at is second-resolution: rowid is what breaks the tie. A naive
-- copy would renumber rowids by scan order and silently reorder same-second
-- events, so the INSERT names `rowid` explicitly and reads `ORDER BY rowid`.
--
-- task_events is a CHILD table (FK -> board_tasks). Dropping a child cascades
-- nothing. The whole rebuild runs inside the single BEGIN IMMEDIATE that
-- migrate_connection opens (PRAGMA foreign_keys cannot be toggled inside a
-- transaction; the connection has it ON), and migrate.py runs
-- PRAGMA foreign_key_check once before commit.
-- --------------------------------------------------------------------------

CREATE TABLE task_events_131 (
    id          TEXT PRIMARY KEY,               -- new_id('tve')
    task_id     TEXT NOT NULL REFERENCES board_tasks(id) ON DELETE CASCADE,
    actor       TEXT NOT NULL,                  -- employee id | agent id | 'system'
    event       TEXT NOT NULL CHECK (event IN (
                    'status_change', 'assign', 'verify', 'unverify',
                    'verify_failed', 'block', 'comment', 'evidence', 'create')),
    from_status TEXT,
    to_status   TEXT,
    note        TEXT NOT NULL DEFAULT '',
    created_at  TEXT NOT NULL
);

INSERT INTO task_events_131 (
    rowid, id, task_id, actor, event, from_status, to_status, note, created_at
)
SELECT
    rowid, id, task_id, actor, event, from_status, to_status, note, created_at
FROM task_events
ORDER BY rowid;

-- The audit-trail equality assert, expressed the only way plain SQL can raise:
-- a CHECK that a 0 violates. Row count AND max rowid must both survive the
-- copy, or this migration aborts and the transaction rolls back with an
-- IntegrityError naming this guard. (SQLite has no bare RAISE outside a
-- trigger, and no other migration in this tree needed one yet.)
CREATE TEMP TABLE migration_131_copy_guard (
    ok INTEGER NOT NULL CHECK (ok = 1)
);

INSERT INTO migration_131_copy_guard (ok)
SELECT CASE
    WHEN (SELECT COUNT(*) FROM task_events_131) = (SELECT COUNT(*) FROM task_events)
     AND (SELECT COALESCE(MAX(rowid), 0) FROM task_events_131)
       = (SELECT COALESCE(MAX(rowid), 0) FROM task_events)
    THEN 1 ELSE 0
END;

DROP TABLE migration_131_copy_guard;

DROP TABLE task_events;

ALTER TABLE task_events_131 RENAME TO task_events;

-- Recreated by hand: a DROP takes the old table's indexes with it, and the
-- team queue/report reads that scan a card's trail depend on this one.
CREATE INDEX IF NOT EXISTS idx_task_events_task ON task_events(task_id, created_at);
