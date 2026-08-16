"""L-16: interaction consume / answer / expiry on auto-migrated schema."""

from __future__ import annotations

from omniagentos.db.store import SqliteStore
from omniagentos.interactions.consumer import (
    InteractionConsumer,
    expire_due_interactions,
)
from omniagentos.interactions.store import InteractionsStore, normalize_timestamp


def test_consume_delivers_pending(tmp_path) -> None:
    store = SqliteStore(str(tmp_path / "ixn.db"))  # auto-migrated schema
    ixn_store = InteractionsStore(store)
    consumer = InteractionConsumer(ixn_store)

    ixn = ixn_store.create(
        work_ref_type="task",
        work_ref_id="t1",
        direction="agent_to_user",
        kind="question",
        body="how are you?",
    )
    assert ixn["status"] == "active"

    delivered = consumer.consume_pending("task", "t1")
    assert len(delivered) == 1
    assert delivered[0]["id"] == ixn["id"]
    assert delivered[0]["status"] == "delivered"
    assert delivered[0]["delivered_at"]


def test_answer_threads_without_overwriting_question(tmp_path) -> None:
    store = SqliteStore(str(tmp_path / "ixn.db"))
    ixn_store = InteractionsStore(store)
    consumer = InteractionConsumer(ixn_store)

    question_body = "what is the risk class?"
    ixn = ixn_store.create(
        work_ref_type="task",
        work_ref_id="t1",
        direction="agent_to_user",
        kind="question",
        body=question_body,
    )
    consumer.consume_pending("task", "t1")

    result = consumer.answer(ixn["id"], "bounded_external", author="operator")
    assert result is not None
    parent = result["parent"]
    answer = result["answer"]

    assert parent["id"] == ixn["id"]
    assert parent["status"] == "answered"
    assert parent["body"] == question_body  # original preserved
    assert parent["answered_at"] is not None

    assert answer["kind"] == "answer"
    assert answer["parent_id"] == ixn["id"]
    assert answer["body"] == "bounded_external"
    assert answer["direction"] == "user_to_agent"


def test_expire_active_and_delivered_unanswered(tmp_path) -> None:
    store = SqliteStore(str(tmp_path / "ixn.db"))
    ixn_store = InteractionsStore(store)
    consumer = InteractionConsumer(ixn_store)

    # Active + past expiry
    a = ixn_store.create(
        work_ref_type="task",
        work_ref_id="t1",
        direction="agent_to_user",
        kind="question",
        body="q1",
        expires_at="2000-01-01T00:00:00Z",
    )
    # Delivered but unanswered + past expiry
    b = ixn_store.create(
        work_ref_type="task",
        work_ref_id="t1",
        direction="agent_to_user",
        kind="question",
        body="q2",
        expires_at="2000-01-02T00:00:00Z",
    )
    ixn_store.mark_delivered(b["id"])

    # Still active, not expired yet (far future)
    c = ixn_store.create(
        work_ref_type="task",
        work_ref_id="t1",
        direction="agent_to_user",
        kind="question",
        body="q3",
        expires_at="2099-01-01T00:00:00Z",
    )

    n = consumer.expire_due()
    assert n == 2
    assert ixn_store.get(a["id"])["status"] == "expired"
    assert ixn_store.get(b["id"])["status"] == "expired"
    assert ixn_store.get(c["id"])["status"] == "active"

    # Consume path expires first, so no deliveries for expired work.
    delivered = consumer.consume_pending("task", "t1")
    assert len(delivered) == 1
    assert delivered[0]["id"] == c["id"]


def test_normalize_timestamp_and_create_normalizes_expires_at(tmp_path) -> None:
    assert normalize_timestamp("2020-01-01 12:00:00") == "2020-01-01T12:00:00Z"
    assert normalize_timestamp("2020-01-01T12:00:00+00:00") == "2020-01-01T12:00:00Z"

    store = SqliteStore(str(tmp_path / "ixn.db"))
    ixn_store = InteractionsStore(store)
    row = ixn_store.create(
        work_ref_type="task",
        work_ref_id="t1",
        direction="agent_to_user",
        kind="question",
        body="q",
        expires_at="2020-06-01 15:30:00",
    )
    assert row["expires_at"] == "2020-06-01T15:30:00Z"


def test_expire_due_interactions_plain_data_handoff(tmp_path) -> None:
    """L10 handoff: plain dict, no scheduler import."""
    store = SqliteStore(str(tmp_path / "ixn.db"))
    ixn_store = InteractionsStore(store)
    ixn_store.create(
        work_ref_type="task",
        work_ref_id="t1",
        direction="agent_to_user",
        kind="question",
        body="stale",
        expires_at="1999-01-01T00:00:00Z",
    )
    payload = expire_due_interactions(store)
    assert payload["capability"] == "interactions.expire"
    assert payload["expired"] == 1
