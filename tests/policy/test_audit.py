from __future__ import annotations

from typing import Any, cast

from omniagentos.audit import audit
from omniagentos.contracts import Events, Store


class FakeStore:
    def __init__(self) -> None:
        self.event: dict[str, Any] | None = None

    def insert_event(self, **event: Any) -> int:
        self.event = event
        return 73


def test_audit_inserts_event_and_returns_row_id() -> None:
    store = FakeStore()
    payload = {"approved": True}

    event_id = audit(
        cast(Store, store),
        "operator",
        "approval.granted",
        target_type="run",
        target_id="run_123",
        payload=payload,
        trace_id="trace_456",
    )

    assert event_id == 73
    assert store.event == {
        "type": Events.AUDIT,
        "actor": "operator",
        "action": "approval.granted",
        "target_type": "run",
        "target_id": "run_123",
        "payload": payload,
        "trace_id": "trace_456",
    }
