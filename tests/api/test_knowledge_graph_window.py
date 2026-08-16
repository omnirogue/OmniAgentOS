"""GET /api/knowledge/graph must be served from a window of the rows it keeps.

``routes/knowledge.py:get_graph`` drops every fact that is not ``status='active'`` after
``KnowledgeStore.graph_snapshot`` has already applied its ``LIMIT``. Quarantined is the
default and dominant status (migration 001 defaults to it; the 002 trigger forces it for
agent writes; promotion needs >=2 non-agent source classes), so an unfiltered window
fills with rows the route discards and the endpoint returns an empty graph. The same
window feeds ``briefing/gather.py:_promoted_facts``, which reports the empty result as
healthy.

The end-to-end proof lives in ``tests/knowledge/test_graph_snapshot_window.py`` and needs
PostgreSQL. These assertions pin the query shape that makes it hold, and run anywhere.
"""

from __future__ import annotations

from threading import RLock
from typing import Any

import pytest

from omniagentos.knowledge.store import KnowledgeStore


class _RecordingCursor:
    def __init__(self, sink: list[str]) -> None:
        self._sink = sink
        self.description: list[Any] = []

    def __enter__(self) -> _RecordingCursor:
        return self

    def __exit__(self, *_args: Any) -> bool:
        return False

    def execute(self, sql: str, params: Any = None) -> None:
        self._sink.append(" ".join(str(sql).split()))

    def fetchall(self) -> list[tuple[Any, ...]]:
        return []


class _RecordingConnection:
    closed = False

    def __init__(self) -> None:
        self.statements: list[str] = []

    def cursor(self) -> _RecordingCursor:
        return _RecordingCursor(self.statements)

    def commit(self) -> None:
        return None


def _facts_query(*, include_quarantined: bool) -> str:
    """Capture the facts SELECT graph_snapshot issues, without needing PostgreSQL."""
    store = object.__new__(KnowledgeStore)
    store._lock = RLock()
    store._role = "knowledge_admin"
    store._connection = _RecordingConnection()
    store.graph_snapshot(limit_nodes=10, include_quarantined=include_quarantined)
    return next(sql for sql in store._connection.statements if "FROM facts" in sql)


def _where_clause(sql: str) -> str:
    return sql.split("WHERE", 1)[1].split("ORDER BY")[0]


@pytest.mark.parametrize("include_quarantined", [False, True])
def test_graph_snapshot_facts_query_is_deterministically_ordered(
    include_quarantined: bool,
) -> None:
    """An unordered LIMIT leaves which rows come back undefined between identical calls."""
    assert "ORDER BY" in _facts_query(include_quarantined=include_quarantined)


def test_graph_snapshot_facts_query_constrains_status_before_the_limit() -> None:
    """The status predicate belongs in the WHERE clause, not in the caller."""
    assert "status" in _where_clause(_facts_query(include_quarantined=False))


def test_graph_snapshot_can_still_request_quarantined_rows() -> None:
    """The active-only window is a default, not a capability removal."""
    assert "status" not in _where_clause(_facts_query(include_quarantined=True))


@pytest.mark.parametrize("include_quarantined", [False, True])
def test_graph_snapshot_excludes_capabilities_before_the_limit(
    include_quarantined: bool,
) -> None:
    """The general graph never admits tenant-scoped capability rows into its window."""
    where = _where_clause(_facts_query(include_quarantined=include_quarantined))
    assert "capability_scope IS NULL" in where
