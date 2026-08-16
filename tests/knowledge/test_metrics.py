"""Knowledge dashboard metric contracts."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from omniagentos.knowledge.contracts import KnowledgeUnavailable
from omniagentos.knowledge.metrics import empty_stats, knowledge_stats, recall_health


class _Cursor:
    def execute(self, *_args: Any) -> None:
        return None

    def fetchone(self) -> tuple[datetime] | None:
        return (datetime(2026, 1, 1, tzinfo=UTC),)

    def __enter__(self) -> _Cursor:
        return self

    def __exit__(self, *_args: Any) -> None:
        return None


class _Store:
    class _Lock:
        def __enter__(self) -> _Store._Lock:
            return self

        def __exit__(self, *_args: Any) -> None:
            return None

    _lock = _Lock()

    class _Connection:
        def cursor(self) -> _Cursor:
            return _Cursor()

    def _conn(self) -> _Connection:
        return self._Connection()

    def stats(self) -> dict[str, Any]:
        return {
            "facts": {"total": 2},
            "entities": 1,
            "edges": 1,
            "episodes": 1,
            "recalls": {"count": 1, "avg_latency_ms": 4.0, "avg_tokens": 3.0},
        }


class _Audit:
    def get_events_after(self, *_args: Any, **_kwargs: Any) -> list[dict[str, Any]]:
        return [{"ts": datetime.now(UTC).isoformat(), "payload": {"error": "timeout"}}]


class _DeadStore(_Store):
    def stats(self) -> dict[str, Any]:
        raise KnowledgeUnavailable("offline")


def test_recall_health_includes_recall_log_and_audit_errors() -> None:
    health = recall_health(_Store(), _Audit())
    assert health["last_successful_recall_at"] == "2026-01-01T00:00:00+00:00"
    assert health["recall_errors_24h"] == 1
    assert health["last_error"] == "timeout"


def test_knowledge_stats_degrades_when_pg_is_down() -> None:
    assert knowledge_stats(_DeadStore()) == empty_stats()
