"""Atomic write scoping for the cognitive-state stores (L18 / M-40).

All four L18 packages — ``metacog``, ``orgdims``, ``cbm``, ``graph_runtime`` —
open SQLite through ``omniagentos.db.store._connect``, which sets
``isolation_level=None``. That is full autocommit: every ``execute`` is its own
transaction and ``Connection.commit()`` is a no-op unless someone issued an
explicit ``BEGIN``. A multi-statement write (artifact + provenance edges, run +
nodes + edges, outcome + closure + stats + outbox) could therefore commit a
prefix and then fail, leaving durable half-state on disk that a retry cannot
repair — the dedupe/idempotency guards read the prefix back and skip the rest.

``atomic`` puts those writes in one real transaction:

- **outermost scope** → ``BEGIN IMMEDIATE`` … ``COMMIT`` / ``ROLLBACK``.
  Immediate rather than deferred so the write lock is taken up front; a
  deferred transaction that reads first and writes later can fail its
  read-to-write upgrade against a concurrent writer.
- **nested scope** (a transaction is already open on this connection) →
  ``SAVEPOINT`` … ``RELEASE`` / ``ROLLBACK TO``. Nesting is what lets a
  compound service call wrap store methods that are themselves atomic without
  an inner call committing the outer one.

``discarded`` is the same scoping for writes that must *never* persist — the
side-effect-free probe in ``CognitiveBudgetService.recommend_rung``.

This module sits beside the four packages rather than inside one of them only
because all four need it. It is L18-internal plumbing with no public surface.
"""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager

# Savepoint names are interpolated into SQL, so they are restricted to plain
# identifiers. Callers pass module-level literals; this is a guard against a
# future caller threading a value through.
_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _check(name: str) -> str:
    if not _NAME_RE.match(name):
        raise ValueError(f"invalid transaction scope name: {name!r}")
    return name


def _begin(connection: sqlite3.Connection, name: str) -> bool:
    """Open a scope; return True when it is nested inside an existing one."""
    if connection.in_transaction:
        connection.execute(f"SAVEPOINT {name}")
        return True
    connection.execute("BEGIN IMMEDIATE")
    return False


def _undo(connection: sqlite3.Connection, name: str, *, nested: bool) -> None:
    """Discard the scope's writes, leaving the connection usable.

    SQLite aborts the whole transaction by itself for some errors (I/O error,
    disk full), which makes the targeted statements fail. The connection-level
    rollback is the fallback so the connection is never left holding a
    half-applied transaction.
    """
    try:
        if nested:
            connection.execute(f"ROLLBACK TO SAVEPOINT {name}")
            connection.execute(f"RELEASE SAVEPOINT {name}")
        else:
            connection.rollback()
        return
    except sqlite3.Error:
        pass
    try:
        connection.rollback()
    except sqlite3.Error:
        pass


@contextmanager
def atomic(connection: sqlite3.Connection, name: str) -> Iterator[sqlite3.Connection]:
    """Commit every write in the block, or none of them.

    Any exception — including a failure of the final COMMIT/RELEASE — rolls the
    scope back and propagates. Nothing is swallowed: a caller never sees success
    for a write that did not become durable.
    """
    _check(name)
    nested = _begin(connection, name)
    try:
        yield connection
    except BaseException:
        _undo(connection, name, nested=nested)
        raise
    try:
        if nested:
            connection.execute(f"RELEASE SAVEPOINT {name}")
        else:
            connection.commit()
    except BaseException:
        _undo(connection, name, nested=nested)
        raise


@contextmanager
def discarded(connection: sqlite3.Connection, name: str) -> Iterator[sqlite3.Connection]:
    """Scope whose writes are always rolled back (side-effect-free probes)."""
    _check(name)
    nested = _begin(connection, name)
    try:
        yield connection
    finally:
        _undo(connection, name, nested=nested)
