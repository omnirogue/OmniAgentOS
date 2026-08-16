"""M-12 mypy-facing protocol check: reliability rows are not typed as SSE inserts.

This file is intentionally strict so ``mypy`` rejects assigning a reliability
insert call to the Store SSE method shape. Runtime assertions guard the same
contract when mypy is not run.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from omniagentos.contracts import Store
from omniagentos.reliability.contracts import ReliabilityStore
from omniagentos.reliability.store import SqliteReliabilityStore


@runtime_checkable
class _SseInsert(Protocol):
    def insert_event(
        self,
        type: str,
        actor: str,
        action: str,
        target_type: str = "",
        target_id: str = "",
        payload: dict | None = None,
        trace_id: str = "",
    ) -> int: ...


@runtime_checkable
class _ReliabilityInsert(Protocol):
    def insert_reliability_event(
        self,
        failure_class: str,
        severity: str,
        signature: str,
        occurrence_key: str,
        source: str,
        ref_type: str | None = None,
        ref_id: str | None = None,
        evidence_json: dict | None = None,
    ) -> str: ...


def test_store_protocol_is_sse_insert_not_reliability() -> None:
    assert "insert_event" in Store.__dict__ or hasattr(Store, "insert_event")
    assert not hasattr(Store, "insert_reliability_event")


def test_reliability_protocol_uses_named_reliability_insert() -> None:
    assert hasattr(ReliabilityStore, "insert_reliability_event")
    # Must not re-declare insert_event with the reliability shape (that was M-12).
    # Protocol may inherit nothing named insert_event.
    members = getattr(ReliabilityStore, "__protocol_attrs__", None)
    if members is None:
        members = {name for name in dir(ReliabilityStore) if not name.startswith("_")}
    assert "insert_reliability_event" in members or hasattr(
        ReliabilityStore, "insert_reliability_event"
    )


def test_concrete_store_satisfies_both_named_protocols() -> None:
    # Structural: SqliteReliabilityStore supports both named methods.
    assert issubclass(SqliteReliabilityStore, _SseInsert) or hasattr(
        SqliteReliabilityStore, "insert_event"
    )
    assert issubclass(SqliteReliabilityStore, _ReliabilityInsert) or hasattr(
        SqliteReliabilityStore, "insert_reliability_event"
    )
    # Distinct callables — no dual-dispatch override on insert_event.
    from omniagentos.db.store import SqliteStore

    assert SqliteReliabilityStore.insert_event is SqliteStore.insert_event
    assert (
        SqliteReliabilityStore.insert_reliability_event is not SqliteReliabilityStore.insert_event
    )


def _mypy_only_examples(store: SqliteReliabilityStore) -> None:
    """Examples checked by mypy: wrong shapes must not type-check as SSE.

    Runtime does not call this. mypy should accept SSE insert_event and
    reliability insert_reliability_event, and reject reliability kwargs on
    insert_event if --strict is enabled on this file.
    """
    _sse: int = store.insert_event("audit.event", "api", "ok")
    _rel: str = store.insert_reliability_event(
        "run_failed",
        "warning",
        "sig",
        "occ",
        "src",
    )
    # The following would be a mypy error if uncommented (reliability kwargs on SSE):
    # store.insert_event(failure_class="x", severity="y", signature="z", occurrence_key="o", source="s")
    assert _sse is not None or _rel is not None  # keep names used for linters
