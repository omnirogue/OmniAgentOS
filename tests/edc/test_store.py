"""DecisionStore substrate: owner-scoped CRUD, dedupe, numbering, events, rules."""

from __future__ import annotations

import pytest

from omniagentos.db.store import SqliteStore
from omniagentos.edc.store import DecisionStore
from tests.edc.conftest import make_decision


def test_owner_scoped_round_trip(decisions: DecisionStore, employees: dict[str, str]) -> None:
    created, was_new = decisions.create_decision(make_decision())
    assert was_new is True
    assert created["number"] == 1
    assert created["owner_employee_id"] == "emp_owner"
    # decoded JSON fields come back as objects, not strings
    assert created["recommended"] == {"kind": "reply", "human_line": "update the card"}
    assert created["available_actions"] == []

    fetched = decisions.get_decision(created["id"], owner_employee_id="emp_owner")
    assert fetched is not None
    assert fetched["id"] == created["id"]

    listed = decisions.list_decisions(owner_employee_id="emp_owner")
    assert [d["id"] for d in listed] == [created["id"]]


def test_foreign_owner_reads_as_absent(decisions: DecisionStore, employees: dict[str, str]) -> None:
    created, _ = decisions.create_decision(make_decision(owner_employee_id="emp_owner"))
    # Bob cannot see the operator's decision — reads as absent (API turns this into 404).
    assert decisions.get_decision(created["id"], owner_employee_id="emp_bob") is None
    assert decisions.list_decisions(owner_employee_id="emp_bob") == []


def test_unknown_key_raises_value_error(
    decisions: DecisionStore, employees: dict[str, str]
) -> None:
    with pytest.raises(ValueError, match="unknown columns"):
        decisions.create_decision(make_decision(owner="emp_owner"))


def test_missing_required_field_raises(decisions: DecisionStore, employees: dict[str, str]) -> None:
    payload = make_decision()
    del payload["classification"]
    with pytest.raises(ValueError, match="classification"):
        decisions.create_decision(payload)


def test_dedupe_reinsert_is_noop(decisions: DecisionStore, employees: dict[str, str]) -> None:
    first, new_1 = decisions.create_decision(make_decision(source="email", source_ref="msg-42"))
    second, new_2 = decisions.create_decision(
        make_decision(source="email", source_ref="msg-42", title="different title")
    )
    assert new_1 is True
    assert new_2 is False
    # Same row returned; the re-scan neither duplicated nor overwrote it.
    assert second["id"] == first["id"]
    assert second["title"] == "AWS payment method expired"
    assert decisions.list_decisions(owner_employee_id="emp_owner") == [
        decisions.get_decision(first["id"], owner_employee_id="emp_owner")
    ]


def test_same_ref_different_owner_is_distinct(
    decisions: DecisionStore, employees: dict[str, str]
) -> None:
    # UNIQUE is (source, source_ref, owner) — the same source ref for two owners
    # is two independent decisions (per-user isolation).
    a, new_a = decisions.create_decision(
        make_decision(owner_employee_id="emp_owner", source_ref="shared")
    )
    b, new_b = decisions.create_decision(
        make_decision(owner_employee_id="emp_bob", source_ref="shared")
    )
    assert new_a and new_b
    assert a["id"] != b["id"]


def test_number_allocation_monotonic(decisions: DecisionStore, employees: dict[str, str]) -> None:
    numbers = [
        decisions.create_decision(make_decision(source_ref=f"m{i}"))[0]["number"] for i in range(5)
    ]
    assert numbers == [1, 2, 3, 4, 5]
    # Dedupe does not consume a number.
    decisions.create_decision(make_decision(source_ref="m0"))
    nxt = decisions.create_decision(make_decision(source_ref="m5"))[0]["number"]
    assert nxt == 6


def test_create_appends_create_event(decisions: DecisionStore, employees: dict[str, str]) -> None:
    created, _ = decisions.create_decision(make_decision(status="open"))
    events = decisions.list_events(created["id"], owner_employee_id="emp_owner")
    assert [e["event"] for e in events] == ["create"]
    assert events[0]["to_status"] == "open"


def test_append_event_owner_scoped(decisions: DecisionStore, employees: dict[str, str]) -> None:
    created, _ = decisions.create_decision(make_decision())
    decisions.append_event(
        created["id"], owner_employee_id="emp_owner", actor="emp_owner", event="surface"
    )
    events = decisions.list_events(created["id"], owner_employee_id="emp_owner")
    assert [e["event"] for e in events] == ["create", "surface"]

    # A non-owner can neither append to nor read the trail.
    with pytest.raises(KeyError):
        decisions.append_event(
            created["id"], owner_employee_id="emp_bob", actor="emp_bob", event="surface"
        )
    assert decisions.list_events(created["id"], owner_employee_id="emp_bob") == []


def test_recovery_status_accepted(decisions: DecisionStore, employees: dict[str, str]) -> None:
    # F03: the crash-safety recovery states must be storable.
    created, _ = decisions.create_decision(make_decision())
    for status in ("in_progress", "failed_retryable", "reconcile_required", "done_unverified"):
        row = decisions.update_decision(
            created["id"], owner_employee_id="emp_owner", fields={"status": status}
        )
        assert row is not None and row["status"] == status


def test_update_rejects_immutable_and_unknown(
    decisions: DecisionStore, employees: dict[str, str]
) -> None:
    created, _ = decisions.create_decision(make_decision())
    with pytest.raises(ValueError, match="immutable"):
        decisions.update_decision(
            created["id"], owner_employee_id="emp_owner", fields={"source": "slack"}
        )
    with pytest.raises(ValueError, match="unknown columns"):
        decisions.update_decision(created["id"], owner_employee_id="emp_owner", fields={"bogus": 1})


def test_reclassify_maybe_not_frozen(decisions: DecisionStore, employees: dict[str, str]) -> None:
    # F06: a MAYBE from an LLM outage must be re-evaluable in place — the source
    # uniqueness constraint is the adapter cursor, not a permanent verdict.
    created, _ = decisions.create_decision(
        make_decision(
            classification="maybe",
            classifier="llm_unavailable",
            confidence=0.0,
        )
    )
    updated = decisions.reclassify_decision(
        created["id"],
        owner_employee_id="emp_owner",
        fields={
            "classification": "needs_owner",
            "classifier": "deterministic",
            "confidence": 0.9,
            "recommended": {"kind": "reply", "human_line": "call the vendor"},
        },
    )
    assert updated is not None
    assert updated["classification"] == "needs_owner"
    assert updated["classifier"] == "deterministic"
    assert updated["recommended"]["human_line"] == "call the vendor"

    events = [e["event"] for e in decisions.list_events(created["id"], owner_employee_id="emp_owner")]
    assert events == ["create", "classify"]


def test_reclassify_owner_scoped_and_terminal_guarded(
    decisions: DecisionStore, employees: dict[str, str]
) -> None:
    created, _ = decisions.create_decision(make_decision(classification="maybe"))
    # Not the owner → no-op.
    assert (
        decisions.reclassify_decision(
            created["id"],
            owner_employee_id="emp_bob",
            fields={"classification": "needs_owner"},
        )
        is None
    )
    # Terminal decision → reclassify refused (would rewrite history).
    decisions.update_decision(
        created["id"], owner_employee_id="emp_owner", fields={"status": "done_verified"}
    )
    assert (
        decisions.reclassify_decision(
            created["id"],
            owner_employee_id="emp_owner",
            fields={"classification": "urgent"},
        )
        is None
    )


def test_reclassify_requires_classification(
    decisions: DecisionStore, employees: dict[str, str]
) -> None:
    created, _ = decisions.create_decision(make_decision(classification="maybe"))
    with pytest.raises(ValueError, match="classification"):
        decisions.reclassify_decision(
            created["id"], owner_employee_id="emp_owner", fields={"confidence": 0.5}
        )


def test_rules_owner_scoped(decisions: DecisionStore, employees: dict[str, str]) -> None:
    rule = decisions.create_rule(
        {
            "owner_employee_id": "emp_owner",
            "kind": "suppress",
            "matcher": {"sender_domain": "newsletter.example"},
        }
    )
    assert rule["state"] == "proposed"
    assert rule["matcher"] == {"sender_domain": "newsletter.example"}
    assert decisions.get_rule(rule["id"], owner_employee_id="emp_owner") is not None
    # Owner isolation on rules too.
    assert decisions.get_rule(rule["id"], owner_employee_id="emp_bob") is None
    assert decisions.list_rules(owner_employee_id="emp_bob") == []
    assert [
        r["id"] for r in decisions.list_rules(owner_employee_id="emp_owner", kind="suppress")
    ] == [rule["id"]]


def test_rule_unknown_key_raises(decisions: DecisionStore, employees: dict[str, str]) -> None:
    with pytest.raises(ValueError, match="unknown columns"):
        decisions.create_rule(
            {"owner_employee_id": "emp_owner", "kind": "suppress", "matcher": {}, "oops": 1}
        )


def test_source_cursor_watermark(decisions: DecisionStore, employees: dict[str, str]) -> None:
    # F1: durable per-(source, owner) triage watermark.
    assert decisions.get_source_cursor("gmail_ownera", "emp_owner") is None
    cur = decisions.advance_source_cursor("gmail_ownera", "emp_owner", last_message_id="17")
    assert cur["last_message_id"] == "17"
    cur = decisions.advance_source_cursor("gmail_ownera", "emp_owner", last_message_id="42")
    assert cur["last_message_id"] == "42"
    # Distinct owners keep distinct cursors.
    assert decisions.get_source_cursor("gmail_ownera", "emp_bob") is None


def test_migration_applies_cleanly(store: SqliteStore) -> None:
    # Building the store ran every migration; assert 130's objects exist.
    conn = store._connection
    tables = {
        row["name"]
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    }
    assert {"decisions", "decision_events", "decision_rules", "edc_source_cursor"} <= tables
    cols = {row["name"] for row in conn.execute("PRAGMA table_info(comms_messages)").fetchall()}
    assert "owner_employee_id" in cols
    assert {row["name"] for row in conn.execute("PRAGMA table_info(decisions)").fetchall()} >= {
        "escalated_for_deadline",
        "number",
    }
