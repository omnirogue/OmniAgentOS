-- 059_scope_locks.sql — T3.2: the cross-lane path-ownership kernel.
--
-- Phase 3 exists to make "two workers editing the same file at the same time"
-- structurally impossible rather than statistically unlikely. This migration
-- lands the three pieces of durable state that makes that possible; the store
-- (omniagentos/scope/locks.py) and the algebra (omniagentos/scope/{paths,model,
-- conflict}.py) are the code side.
--
-- Contains:
--   (a) runner fenced lease columns on runs   — mirrors 052 for swarm_runs
--   (b) resource_locks                        — ONE generic cross-lane lock table
--   (c) scope_waiters                         — durable FIFO park queue
--   (d) runs.scope_json / sessions.scope_json — the declared claim set
--
-- Enum CHECK lists below duplicate the Literals in omniagentos/scope/* by hand,
-- the post-011 migration idiom (see 058_execution_contract.sql).

-- ---------------------------------------------------------------------------
-- (a) Runner fenced lease.
--
-- Migration 052 gave swarm_runs a lease_generation so a merely-WEDGED (not dead)
-- coordinator could not interleave git mutations with its adopter: adoption
-- BUMPS the generation, and the displaced coordinator's next conditional
-- heartbeat matches zero rows and it stops. runs had no equivalent, so a runner
-- worker that stalled past its stale threshold and then woke up would happily
-- keep writing next to whoever adopted its run.
--
-- Same shape, same reason:
--   * lease_generation — bumped by reclaim/adopt. Every fenced write (heartbeat,
--     lock renew, lock release) carries the generation the worker believes it
--     holds; a mismatch means "you were adopted, stop".
--   * lease_expires_at — when the lease lapses if nobody renews. NULL means "no
--     lease held" (queued/terminal runs), which is why the sweep index below is
--     partial: only an ACTIVE run can have a lease worth reclaiming, and a
--     partial index keeps the scan proportional to the live fleet rather than to
--     the whole (append-only, unbounded) runs table.
--
-- The state list mirrors contracts.RunState's non-terminal, worker-owned states.
-- 'queued' is excluded on purpose: an unclaimed run has no worker and therefore
-- no lease (the same reasoning as SwarmDal.mark_stale_failed excluding queued).
-- ---------------------------------------------------------------------------
ALTER TABLE runs ADD COLUMN lease_generation INTEGER NOT NULL DEFAULT 0;
ALTER TABLE runs ADD COLUMN lease_expires_at TEXT;

CREATE INDEX idx_runs_lease_expiry ON runs(lease_expires_at)
  WHERE state IN ('running', 'validating', 'awaiting_approval');

-- ---------------------------------------------------------------------------
-- (b) resource_locks — ONE generic table, deliberately NOT one per lane.
--
-- WHY GENERIC. The conflict this table prevents is inherently cross-lane: a
-- runner run and a swarm worker writing the same file is the exact case, and it
-- is also the common one (both are pointed at the same repo). Per-lane tables
-- (runner_locks / swarm_locks / session_locks) would force the acquire path to
-- UNION three tables inside BEGIN IMMEDIATE just to answer "is anything holding
-- this path" — three tables' worth of read, three chances to forget one when a
-- fifth lane appears, and a read that is no longer a single index range. That
-- multi-table read-then-decide is the exact bug class this phase exists to
-- remove, so the schema must not reintroduce it.
--
-- Precedent: account_reservations (migration 046) is one generic slot table
-- serving three lanes (fast-lane, longhaul, swarm) for exactly this reason.
-- `lane` is a column, not a table name.
--
-- NO 'mode' COLUMN, and no shared/read locks in v1. Concurrent WRITERS are the
-- entire problem — nothing in this system is harmed by two readers — and a mode
-- column would force the partial unique index below to become
-- (realm, rel_path, kind, purpose, mode), which no longer expresses the
-- invariant "at most one live exclusive claim on this exact path". A backstop
-- that does not express the invariant is not a backstop.
--
-- Column notes:
--   realm      — the conflict universe, a case-folded realpath from
--                scope.paths.realm_of(). Claims in different realms can NEVER
--                conflict; every query here is realm-first for that reason.
--   rel_path   — realm-relative posix text; '.' is the whole realm (the wire
--                spelling of the empty component tuple).
--   depth      — len(components); 0 == whole realm. Stored, not derived, so the
--                broadest-first ordering the waiter queue depends on is an
--                index range rather than a scan + parse.
--   kind       — 'file' (this exact path) or 'root' (this path and its subtree).
--   holder_*   — WHO holds it. holder_generation is the fence: release and renew
--                are both conditional on it, so a stale (adopted-away) worker
--                cannot free or extend its adopter's locks.
--   purpose    — 'work' (ordinary editing) or 'commit'. A commit touches the
--                index/worktree as a whole and is never relaxed by co-ownership.
--   expires_at — TTL. The reaper is LAZY (head of every acquire) and BOUNDED;
--                see the store. Correctness does not depend on the reap having
--                run: every read of live locks filters on expires_at too, so an
--                unreaped expired row never blocks anyone.
--   released_at— soft delete. Keeping released rows makes contention
--                measurable (shadow mode's whole purpose) and makes the partial
--                indexes below cover exactly the live set.
-- ---------------------------------------------------------------------------
CREATE TABLE resource_locks (
    id                TEXT PRIMARY KEY,          -- new_id('lck')
    realm             TEXT NOT NULL,
    rel_path          TEXT NOT NULL,             -- '.' == whole realm
    depth             INTEGER NOT NULL DEFAULT 0,
    kind              TEXT NOT NULL CHECK (kind IN ('file', 'root')),
    holder_kind       TEXT NOT NULL CHECK (
        holder_kind IN ('run', 'session', 'attempt', 'swarm_task', 'coordinator')
    ),
    holder_id         TEXT NOT NULL,
    holder_generation INTEGER NOT NULL DEFAULT 0,
    lane              TEXT NOT NULL CHECK (lane IN ('runner', 'session', 'longhaul', 'swarm')),
    owner_group       TEXT NOT NULL DEFAULT '',
    purpose           TEXT NOT NULL DEFAULT 'work' CHECK (purpose IN ('work', 'commit')),
    acquired_at       TEXT NOT NULL,
    expires_at        TEXT NOT NULL,
    released_at       TEXT
);

-- BACKSTOP FOR THE EXACT-DUPLICATE CASE ONLY. Two live claims on the SAME
-- (realm, rel_path, kind, purpose) are impossible at the SQL layer, which is a
-- last line of defense against a caller that bypasses the store.
--
-- It cannot do more than that, and pretending otherwise would be the dangerous
-- reading: ancestor/descendant exclusion ("src/**" excludes "src/a/b.py") is a
-- CONTAINMENT predicate over two rows, and no B-tree index can express it. That
-- exclusion is enforced by the SELECT-then-decide inside the store's single
-- BEGIN IMMEDIATE (scope.conflict.first_conflict over the realm's live set),
-- which is exactly why the acquire transaction must be IMMEDIATE: a deferred
-- transaction's read would not lock out a concurrent writer and the check would
-- be advisory.
CREATE UNIQUE INDEX idx_resource_locks_live
    ON resource_locks(realm, rel_path, kind, purpose)
    WHERE released_at IS NULL;

-- The acquire path's one read: every live lock in a realm, broadest-first.
CREATE INDEX idx_resource_locks_realm
    ON resource_locks(realm, depth)
    WHERE released_at IS NULL;

-- Renew / release / crash-resume: "what does this holder still hold".
CREATE INDEX idx_resource_locks_holder
    ON resource_locks(holder_kind, holder_id)
    WHERE released_at IS NULL;

-- The lazy reaper's range scan.
CREATE INDEX idx_resource_locks_expiry
    ON resource_locks(expires_at)
    WHERE released_at IS NULL;

-- ---------------------------------------------------------------------------
-- (c) scope_waiters — the durable FIFO park queue.
--
-- Mirrors longhaul/store.py:296 next_waiting_in_category: when a claim cannot be
-- granted the claimant does NOT spin and does NOT hold what it already has — it
-- parks a row here and returns. A woken waiter re-runs the whole all-or-nothing
-- acquire from scratch.
--
-- NO CYCLE DETECTION AND NO CROSS-OWNER HANDOFF, deliberately. Both exist to
-- make hold-and-wait survivable, and hold-and-wait is precisely what this design
-- does not have: try_acquire_scope takes the WHOLE claim set or none of it
-- inside one transaction, so a holder never waits while holding. With
-- hold-and-wait absent, one of Coffman's four necessary conditions is absent,
-- and deadlock is impossible BY CONSTRUCTION — there is no cycle for a detector
-- to find. Adding handoff ("give me your lock when you're done") would
-- reintroduce hold-and-wait and then require a detector to survive it: strictly
-- more machinery for strictly less safety.
--
-- blocked_on is DIAGNOSTIC ONLY — the observed blocker's lock id at park time.
-- It is not a dependency edge and nothing may schedule off it; by the time the
-- waiter wakes, that lock may be long gone and a different one may block.
-- Its job is to answer "what was I waiting for" in an event payload.
--
-- id is INTEGER AUTOINCREMENT rather than a new_id() TEXT because FIFO order IS
-- insertion order and must be totally ordered. created_at is second-granularity
-- (contracts.utc_now_iso), so simultaneous parks tie, and a random uuid tiebreak
-- would silently shuffle the queue. Ordering by the autoincrement id is also
-- immune to a wall-clock step backwards, which ordering by created_at is not.
-- ---------------------------------------------------------------------------
CREATE TABLE scope_waiters (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    realm        TEXT NOT NULL,
    lane         TEXT NOT NULL CHECK (lane IN ('runner', 'session', 'longhaul', 'swarm')),
    holder_kind  TEXT NOT NULL CHECK (
        holder_kind IN ('run', 'session', 'attempt', 'swarm_task', 'coordinator')
    ),
    holder_id    TEXT NOT NULL,
    blocked_on   TEXT NOT NULL DEFAULT '',   -- observed resource_locks.id; DIAGNOSTIC
    blocked_path TEXT NOT NULL DEFAULT '',   -- realm-relative text of the refused claim
    created_at   TEXT NOT NULL,
    cleared_at   TEXT
);

-- One live park per holder. Re-parking an already-parked holder must UPDATE this
-- row rather than insert a second one — otherwise a holder that retries every
-- few seconds keeps appending fresh rows at the BACK of the queue and starves
-- itself forever. The unique index is what makes that mistake impossible.
CREATE UNIQUE INDEX idx_scope_waiters_holder
    ON scope_waiters(holder_kind, holder_id)
    WHERE cleared_at IS NULL;

-- The FIFO wake scan, realm-scoped.
CREATE INDEX idx_scope_waiters_fifo
    ON scope_waiters(realm, created_at)
    WHERE cleared_at IS NULL;

-- ---------------------------------------------------------------------------
-- (d) The declared claim set, persisted next to the work that declared it.
--
-- scope_json is the serialized ScopeClaim list a run/session declared up front.
-- It is what crash-resume replays through PathLockStore.reacquire_scope (a
-- resumed run must re-take the same paths, not guess), and what the declared-vs-
-- actual verification in Phase 4 diffs against.
--
-- EASY TO MISS: 'scope_json' must ALSO be added to _RUN_COLUMNS in
-- omniagentos/db/store.py, or every update_run() carrying it raises
-- "unknown columns" from _checked_fields.
-- ---------------------------------------------------------------------------
ALTER TABLE runs ADD COLUMN scope_json TEXT;
ALTER TABLE sessions ADD COLUMN scope_json TEXT;
