"""Durable storage on stdlib ``sqlite3``: five ports, five tables, one file.

This is the adapter a real loop runs on. It implements
:class:`~selfloop.ports.ReceiptStore`, :class:`~selfloop.ports.ApprovalStore`,
:class:`~selfloop.ports.RecordStore`, :class:`~selfloop.ports.EventLog` and
:class:`~selfloop.ports.CheckpointStore` over one database, with no ORM, no
migration framework and no DDL versioning — a single
``CREATE TABLE IF NOT EXISTS`` block that runs on every open.

Why no migration framework: this schema is five tables of ``(id, json)``, and
adding a record kind changes no DDL at all because
:class:`~selfloop.ports.RecordStore` is kind-generic. A migration framework here
would be a dependency, a state machine and an operational hazard bought to
manage a schema that does not change.

**Three semantics live in the SQL and not in convention**, because a caller must
not be able to talk the store out of them — including a caller that is confused,
buggy, or a future refactor of this package:

* ``claim`` is ``INSERT OR IGNORE`` and reports whether THIS caller won the race.
  Not "does a row exist" — a loser must fail closed rather than proceed on the
  theory that the winner will probably succeed.
* ``release`` carries ``AND result_json IS NULL`` **in the WHERE clause**, so a
  receipt that already has a terminal outcome can never be deleted, whatever the
  caller believes. That is the crash-window guarantee.
* ``decide`` is a compare-and-set on ``state = 'pending'``, and ``transition``
  is a compare-and-set on the caller-supplied ``expect`` mapping. Without them,
  two writers both "succeed" and the second silently overwrites the first, so an
  approval can overwrite a rejection and a retired lesson can be resurrected to
  promoted.

``events.id`` is ``INTEGER PRIMARY KEY AUTOINCREMENT`` and IS the replay cursor.
``AUTOINCREMENT`` rather than plain ``INTEGER PRIMARY KEY`` is deliberate: the
plain form reuses the largest deleted rowid, so a log that was ever trimmed can
hand the same integer out twice, and the learning pass would silently re-mine or
skip a stretch of history. The extra ``sqlite_sequence`` row buys a cursor that
is monotonic for the life of the database.

Durability: opened WAL with ``synchronous = FULL``. WAL alone survives a process
being killed — the data is already in the OS page cache — but the port's
obligation is that ``save()`` and ``complete()`` are durable *before they
return*, unqualified, and only ``FULL`` fsyncs the WAL on each commit. The kill
drill tests the process-kill case; ``FULL`` is what makes the same promise
honest for a power cut.
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from typing import Any, Self

from selfloop.adapters.memory import approval_row_id, canonical_value, fields_match
from selfloop.contracts import LoopError

#: The whole schema. Five tables, run on every open, idempotent by construction.
SCHEMA = """
CREATE TABLE IF NOT EXISTS receipts (
    key          TEXT PRIMARY KEY,
    instance_id  TEXT NOT NULL,
    node         TEXT NOT NULL,
    claimed_at   TEXT NOT NULL,
    result_json  TEXT,
    completed_at TEXT
);
CREATE TABLE IF NOT EXISTS approvals (
    approval_id TEXT PRIMARY KEY,
    row_json    TEXT NOT NULL,
    state       TEXT NOT NULL,
    decided_by  TEXT,
    decided_at  TEXT,
    note        TEXT
);
CREATE TABLE IF NOT EXISTS records (
    kind         TEXT NOT NULL,
    record_id    TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    PRIMARY KEY (kind, record_id)
);
CREATE TABLE IF NOT EXISTS events (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    event_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS checkpoints (
    thread_id       TEXT PRIMARY KEY,
    checkpoint_json TEXT NOT NULL,
    saved_at        TEXT
);
"""


def _dumps(payload: Mapping[str, Any]) -> str:
    """Serialise exactly as the in-memory adapter does, so the two never diverge.

    ``sort_keys`` is not cosmetic here: :meth:`SqliteRecordStore.transition`
    compares the stored string against the string it just read, and that
    optimistic compare-and-set is only sound if the same payload always produces
    the same bytes.
    """
    return json.dumps(dict(payload), sort_keys=True, default=str)


class SqliteBackend:
    """One connection, one file, five port façades.

    The façades exist because the ports collide on method names — three of them
    define ``get`` with different signatures — so one object cannot structurally
    satisfy all five. Each façade is a few lines over the shared connection, and
    the connection is the thing that owns durability, locking and the schema.

    **Connection per process, guarded by one lock.** The connection is opened
    with ``check_same_thread=False`` and every statement runs under
    ``self._lock``, because a ``sqlite3`` connection is not thread-safe on its
    own and this package's own in-memory adapters are. Sharing this object across
    processes is not supported — open one per process, which is the shape the
    runtime uses anyway (one tick, one invocation).
    """

    def __init__(self, path: str | os.PathLike[str]) -> None:
        self.path = os.fspath(path)
        parent = os.path.dirname(os.path.abspath(self.path))
        if self.path != ":memory:" and parent:
            os.makedirs(parent, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(
            self.path,
            timeout=30.0,
            # Autocommit. Every statement below either stands alone (and is then
            # durable the moment it returns, which is what the ports require) or
            # opens an explicit BEGIN IMMEDIATE. Python's implicit transaction
            # management would open transactions we did not ask for and hold
            # write locks across calls we did not intend to group.
            isolation_level=None,
            check_same_thread=False,
        )
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.execute("PRAGMA journal_mode = WAL")
            self._conn.execute("PRAGMA synchronous = FULL")
            self._conn.execute("PRAGMA busy_timeout = 30000")
            self._conn.executescript(SCHEMA)

    # -- lifecycle ---------------------------------------------------------

    def close(self) -> None:
        """Close the connection. Safe to call twice."""
        with self._lock:
            try:
                self._conn.close()
            except sqlite3.Error:  # pragma: no cover - already closed
                pass

    def __enter__(self) -> Self:
        """Usable as a context manager so a test or a script closes the file."""
        return self

    def __exit__(self, *exc: object) -> None:
        """Close on the way out; exceptions inside the block are never swallowed."""
        self.close()

    # -- port façades ------------------------------------------------------

    @property
    def receipts(self) -> SqliteReceiptStore:
        return SqliteReceiptStore(self)

    @property
    def approvals(self) -> SqliteApprovalStore:
        return SqliteApprovalStore(self)

    @property
    def records(self) -> SqliteRecordStore:
        return SqliteRecordStore(self)

    @property
    def events(self) -> SqliteEventLog:
        return SqliteEventLog(self)

    @property
    def checkpoints(self) -> SqliteCheckpointStore:
        return SqliteCheckpointStore(self)

    def as_context_overrides(self) -> dict[str, Any]:
        """The five keyword arguments that point a ``LoopContext`` at this file.

        Written so a fixture can run an entire suite against durable storage
        without restating the wiring::

            ctx = build_memory_context(**backend.as_context_overrides())

        The remaining ports (clock, policy, model, gate, notifier, lease) are
        unaffected by the choice of storage backend, which is exactly why they
        are not in here.
        """
        return {
            "receipts": self.receipts,
            "approvals": self.approvals,
            "records": self.records,
            "events": self.events,
            "checkpoints": self.checkpoints,
        }

    # -- statement helpers -------------------------------------------------

    @contextmanager
    def immediate(self) -> Iterator[None]:
        """Hold the database's write lock across several statements.

        The one place this package needs more than a single statement is
        :meth:`SqliteRecordStore.transition`, whose read and conditional write
        must not be separable by another writer. ``BEGIN IMMEDIATE`` takes the
        write lock at the start rather than on first write, which is what stops
        two upgraders from deadlocking against each other.

        The lock is an ``RLock`` so the ``execute`` calls inside the block can
        re-enter it.
        """
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                yield
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise
            self._conn.execute("COMMIT")

    def execute(self, sql: str, params: tuple[Any, ...] = ()) -> sqlite3.Cursor:
        """Run one statement under the connection lock, returning its cursor.

        Every façade below goes through this rather than touching ``_conn``, so
        there is exactly one place that has to hold the lock and exactly one
        place a future maintainer has to look at when they wonder whether this
        adapter is thread-safe.
        """
        with self._lock:
            return self._conn.execute(sql, params)

    def fetchone(self, sql: str, params: tuple[Any, ...] = ()) -> sqlite3.Row | None:
        """One row or None. The cursor is consumed inside the lock, not after it."""
        with self._lock:
            return self._conn.execute(sql, params).fetchone()

    def fetchall(self, sql: str, params: tuple[Any, ...] = ()) -> list[sqlite3.Row]:
        """Every row, materialised inside the lock so no cursor escapes it."""
        with self._lock:
            return list(self._conn.execute(sql, params).fetchall())


class SqliteReceiptStore:
    """Exactly-once bookkeeping. See :class:`~selfloop.ports.ReceiptStore`."""

    def __init__(self, backend: SqliteBackend) -> None:
        self._db = backend

    def claim(self, key: str, *, instance_id: str, node: str, at: str) -> bool:
        """``INSERT OR IGNORE``, reporting whether THIS caller created the row.

        The race is arbitrated by the primary key inside SQLite, so there is no
        check-then-insert window for two workers to interleave in. A caller that
        gets False has lost and must fail closed.
        """
        cur = self._db.execute(
            "INSERT OR IGNORE INTO receipts (key, instance_id, node, claimed_at) "
            "VALUES (?, ?, ?, ?)",
            (key, instance_id, node, at),
        )
        return cur.rowcount == 1

    def get(self, key: str) -> Mapping[str, Any] | None:
        row = self._db.fetchone(
            "SELECT key, instance_id, node, claimed_at, result_json, completed_at "
            "FROM receipts WHERE key = ?",
            (key,),
        )
        return dict(row) if row is not None else None

    def complete(self, key: str, *, envelope_json: str, at: str) -> None:
        """Record this attempt's terminal outcome. Durable before returning.

        Raises when the row is absent. A completion for a key that was never
        claimed means the claim/act/complete sequence has come apart upstream,
        and quietly inserting the row would hide it: the caller would believe an
        exactly-once protocol had arbitrated an effect that nothing arbitrated.

        Deliberately NOT guarded on ``result_json IS NULL``. Attempt keys are
        already attempt-scoped by the caller (``<key>``, ``<key>#a2``), so a
        second completion of the same attempt is a replay writing the same
        envelope, and the port states its obligation for ``release`` and not for
        this. Adding an unstated guard here would silently drop a legitimate
        write.
        """
        cur = self._db.execute(
            "UPDATE receipts SET result_json = ?, completed_at = ? WHERE key = ?",
            (envelope_json, at, key),
        )
        if cur.rowcount == 0:
            raise LoopError(
                f"cannot complete receipt {key!r}: no claim row exists. A completion "
                "without a claim means nothing arbitrated the race for this effect."
            )

    def release(self, key: str) -> bool:
        """Drop a claim that provably produced no effect. No-op once a result exists.

        ``AND result_json IS NULL`` is in the WHERE clause and not in a Python
        pre-check, and that placement is the whole guarantee: a caller cannot
        talk the database out of it, and there is no window between the check and
        the delete in which a concurrent ``complete`` could land.
        """
        cur = self._db.execute(
            "DELETE FROM receipts WHERE key = ? AND result_json IS NULL",
            (key,),
        )
        return cur.rowcount == 1


class SqliteApprovalStore:
    """Park/approve rows. See :class:`~selfloop.ports.ApprovalStore`.

    The row a caller creates is stored whole as JSON, and the four fields the
    decision protocol needs — ``state``, ``decided_by``, ``decided_at``,
    ``note`` — are also real columns. ``state`` has to be a column because
    :meth:`decide` compare-and-sets on it, and a CAS against a value buried in a
    JSON blob is a read-modify-write with a race in the middle.
    """

    def __init__(self, backend: SqliteBackend) -> None:
        self._db = backend

    def get(self, approval_id: str) -> Mapping[str, Any] | None:
        """The row, or None. Writes nothing — a read must never mint a row."""
        row = self._db.fetchone(
            "SELECT row_json, state, decided_by, decided_at, note FROM approvals "
            "WHERE approval_id = ?",
            (approval_id,),
        )
        if row is None:
            return None
        stored = dict(json.loads(row["row_json"]))
        # The columns win over whatever the creation payload said, because the
        # columns are what decide() moves. A row created as "pending" and since
        # approved must never read back as pending.
        stored["state"] = row["state"]
        stored["decided_by"] = row["decided_by"]
        stored["decided_at"] = row["decided_at"]
        stored["note"] = row["note"]
        return stored

    def create(self, row: Mapping[str, Any]) -> bool:
        """Insert if absent. False means it already existed, which is not an error."""
        approval_id = approval_row_id(row)
        payload = dict(row)
        state = str(payload.get("state") or "pending")
        cur = self._db.execute(
            "INSERT OR IGNORE INTO approvals (approval_id, row_json, state, decided_by, "
            "decided_at, note) VALUES (?, ?, ?, NULL, NULL, NULL)",
            (approval_id, _dumps(payload), state),
        )
        return cur.rowcount == 1

    def decide(self, approval_id: str, *, state: str, by: str, note: str, at: str) -> bool:
        """Compare-and-set from ``pending``. False when another decider won.

        One statement, so there is no interval between reading ``pending`` and
        writing the decision. Returns False rather than raising because losing is
        a normal outcome — a worker and an operator at a CLI can decide the same
        row at the same instant.
        """
        cur = self._db.execute(
            "UPDATE approvals SET state = ?, decided_by = ?, decided_at = ?, note = ? "
            "WHERE approval_id = ? AND state = 'pending'",
            (state, by, at, note, approval_id),
        )
        return cur.rowcount == 1


class SqliteRecordStore:
    """The kind-generic record store. See :class:`~selfloop.ports.RecordStore`."""

    def __init__(self, backend: SqliteBackend) -> None:
        self._db = backend

    def put_once(self, kind: str, record_id: str, payload: Mapping[str, Any]) -> bool:
        """HISTORY. ``INSERT OR IGNORE``; False when a record already exists.

        A run must not be able to overwrite its own report card, and the refusal
        is the primary key rather than a caller's discipline.
        """
        cur = self._db.execute(
            "INSERT OR IGNORE INTO records (kind, record_id, payload_json) VALUES (?, ?, ?)",
            (str(kind), record_id, _dumps(payload)),
        )
        return cur.rowcount == 1

    def put_latest(self, kind: str, record_id: str, payload: Mapping[str, Any]) -> None:
        """CACHE. Last write wins, so a fresher green supersedes a stale one."""
        self._db.execute(
            "INSERT INTO records (kind, record_id, payload_json) VALUES (?, ?, ?) "
            "ON CONFLICT (kind, record_id) DO UPDATE SET payload_json = excluded.payload_json",
            (str(kind), record_id, _dumps(payload)),
        )

    def get(self, kind: str, record_id: str) -> Mapping[str, Any] | None:
        row = self._db.fetchone(
            "SELECT payload_json FROM records WHERE kind = ? AND record_id = ?",
            (str(kind), record_id),
        )
        return dict(json.loads(row["payload_json"])) if row is not None else None

    def query(self, kind: str, /, **equals: Any) -> list[Mapping[str, Any]]:
        """Every payload of *kind* whose fields equal all the given values.

        Over-fetches by ``kind`` and filters in Python, exactly as the port
        describes. That is honest at prototype scale and wrong at any real one; a
        production backend should promote ``scope`` and ``cursor`` to indexed
        columns. It is written this way so the two shipped adapters return
        results in the same order for the same data, which is what lets one test
        suite run against both.
        """
        rows = self._db.fetchall(
            "SELECT payload_json FROM records WHERE kind = ? ORDER BY record_id",
            (str(kind),),
        )
        payloads = [dict(json.loads(row["payload_json"])) for row in rows]
        return [payload for payload in payloads if fields_match(payload, equals)]

    def transition(
        self,
        kind: str,
        record_id: str,
        *,
        expect: Mapping[str, Any],
        set: Mapping[str, Any],  # noqa: A002 - the name IS the contract; callers read it as SQL
    ) -> bool:
        """Compare-and-set. False means another writer moved the row first.

        Belt and braces, on purpose. ``BEGIN IMMEDIATE`` serialises this against
        every other writer of the database, and the ``UPDATE`` additionally
        carries the exact ``payload_json`` string it read in its WHERE clause —
        so the statement is still a correct compare-and-set if a future refactor
        drops the transaction. That is only sound because :func:`_dumps` sorts
        keys, which makes one payload produce one byte string.

        This is the method that keeps lesson state safe: promote, retire,
        append-evidence and counter updates all race, because the runtime's
        learning pass runs unattended while an operator runs the CLI.
        """
        with self._db.immediate():
            row = self._db.fetchone(
                "SELECT payload_json FROM records WHERE kind = ? AND record_id = ?",
                (str(kind), record_id),
            )
            if row is None:
                return False
            current_json = row["payload_json"]
            payload = dict(json.loads(current_json))
            if not fields_match(payload, expect):
                return False
            payload.update(canonical_value(dict(set)))
            cur = self._db.execute(
                "UPDATE records SET payload_json = ? WHERE kind = ? AND record_id = ? "
                "AND payload_json = ?",
                (_dumps(payload), str(kind), record_id, current_json),
            )
            return cur.rowcount == 1


class SqliteEventLog:
    """The ordered replay cursor. See :class:`~selfloop.ports.EventLog`.

    ``append`` returns ``lastrowid`` from an ``AUTOINCREMENT`` column, which is
    strictly increasing for the life of the database — across processes and
    across restarts — and never reuses an id that a delete freed. That is the
    property "extract signals since cursor N" rests on, and it is why the cursor
    may not be a timestamp: two events written in the same millisecond are
    unordered, so a time window either re-mines them or drops them, and a clock
    that steps backwards silently re-mines an arbitrary stretch of history as if
    it were new evidence.
    """

    def __init__(self, backend: SqliteBackend) -> None:
        self._db = backend

    def append(self, event: Mapping[str, Any]) -> int:
        cur = self._db.execute("INSERT INTO events (event_json) VALUES (?)", (_dumps(event),))
        cursor = cur.lastrowid
        if cursor is None:  # pragma: no cover - sqlite always sets it for this table
            raise LoopError("sqlite did not report a rowid for the appended event")
        return int(cursor)

    def read(self, *, after: int = 0, limit: int = 500) -> list[Mapping[str, Any]]:
        """Events with a cursor strictly greater than *after*, in cursor order.

        Each event comes back with a ``cursor`` key injected by the log, which
        overwrites any key of that name the writer supplied. The cursor is the
        log's statement about ordering and not the writer's: a writer that could
        set it could make the learning pass re-mine or skip whatever it liked.
        """
        if limit <= 0:
            return []
        rows = self._db.fetchall(
            "SELECT id, event_json FROM events WHERE id > ? ORDER BY id LIMIT ?",
            (int(after), int(limit)),
        )
        events: list[Mapping[str, Any]] = []
        for row in rows:
            event = dict(json.loads(row["event_json"]))
            event["cursor"] = int(row["id"])
            events.append(event)
        return events


class SqliteCheckpointStore:
    """The durability seam. See :class:`~selfloop.ports.CheckpointStore`.

    ``save`` is one autocommitted statement against a connection opened
    ``synchronous = FULL``, so it is on disk before it returns. That single
    obligation is the entire content of the kill drill: a process SIGKILLed at
    any instant must resume from the last node whose checkpoint returned.
    """

    def __init__(self, backend: SqliteBackend) -> None:
        self._db = backend

    def load(self, thread_id: str) -> Mapping[str, Any] | None:
        row = self._db.fetchone(
            "SELECT checkpoint_json FROM checkpoints WHERE thread_id = ?",
            (thread_id,),
        )
        return dict(json.loads(row["checkpoint_json"])) if row is not None else None

    def save(self, thread_id: str, checkpoint: Mapping[str, Any]) -> None:
        self._db.execute(
            "INSERT INTO checkpoints (thread_id, checkpoint_json, saved_at) "
            "VALUES (?, ?, datetime('now')) "
            "ON CONFLICT (thread_id) DO UPDATE SET "
            "checkpoint_json = excluded.checkpoint_json, saved_at = excluded.saved_at",
            (thread_id, _dumps(checkpoint)),
        )

    def drop(self, thread_id: str) -> None:
        """Forget this thread. For operator use; the runtime never calls it."""
        self._db.execute("DELETE FROM checkpoints WHERE thread_id = ?", (thread_id,))


__all__ = [
    "SCHEMA",
    "SqliteApprovalStore",
    "SqliteBackend",
    "SqliteCheckpointStore",
    "SqliteEventLog",
    "SqliteReceiptStore",
    "SqliteRecordStore",
]
