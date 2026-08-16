"""TN.10 agent interactions DAL."""

from __future__ import annotations

from omniagentos.db.store import SqliteStore
from omniagentos.interactions.store import InteractionsStore


def test_create_and_delivery_once(tmp_path) -> None:
    db = tmp_path / "t.db"
    store = SqliteStore(str(db))
    ix = InteractionsStore(store)
    row = ix.create(
        work_ref_type="session",
        work_ref_id="sess_1",
        direction="user_to_agent",
        kind="nudge",
        body="tighten the intro",
        blocking_policy="checkpoint",
        session_id="sess_1",
    )
    assert row["status"] == "active"
    pending = ix.list_pending(session_id="sess_1", blocking_only=True)
    assert len(pending) == 1
    d1 = ix.mark_delivered(row["id"])
    assert d1 is not None
    assert d1["status"] == "delivered"
    assert d1["delivered_at"]
    d2 = ix.mark_delivered(row["id"])
    assert d2 is not None
    assert d2["delivered_at"] == d1["delivered_at"]
