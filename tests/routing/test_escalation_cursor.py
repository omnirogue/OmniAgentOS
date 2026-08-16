"""P1-RECOVERY: in-memory escalation cursor store (CAS + terminal closed).

Policy is pure; this store holds the settled cursor per scope until the durable
``escalation_attempt_identity`` migration lands under P1-RECOVERY-CURSOR.

Callers of three-valued ``get_cursor`` / ``cas_advance`` must handle ``None``
explicitly — bare truthiness collapsing miss with a falsey cursor is a defect.
"""

from __future__ import annotations

import pytest

from omniagentos.routing.escalation import (
    DecisionKind,
    EscalationCursor,
    build_ladder,
    decide_escalate,
    decide_retry,
    initial_route,
    is_terminal,
)
from omniagentos.routing.escalation_store import (
    CursorAlreadyExists,
    InMemoryEscalationCursorStore,
    InvalidCursorAdvance,
    cursor_from_record,
    cursor_to_record,
)


def _ladder():
    return build_ladder(
        [
            {
                "provider": "openai",
                "display_model": "sol",
                "canonical_model": "gpt-5.6-sol",
            },
            {
                "provider": "anthropic",
                "display_model": "opus",
                "canonical_model": "claude-opus",
            },
            {
                "provider": "moonshot",
                "display_model": "kimi",
                "canonical_model": "kimi-k2",
            },
        ]
    )


class TestGetCursorThreeValued:
    def test_missing_scope_returns_none_not_false(self) -> None:
        store = InMemoryEscalationCursorStore()
        got = store.get_cursor("missing-scope")
        assert got is None
        if got is None:
            handled = "absent"
        else:
            handled = "present"
        assert handled == "absent"

    def test_empty_scope_id_raises(self) -> None:
        store = InMemoryEscalationCursorStore()
        with pytest.raises(ValueError):
            store.get_cursor("")
        with pytest.raises(ValueError):
            store.get_cursor("   ")


class TestPutInitialAndCas:
    def test_put_initial_then_get(self) -> None:
        store = InMemoryEscalationCursorStore()
        ladder = _ladder()
        start = initial_route(ladder)
        stored = store.put_initial("job-1", start.cursor)
        assert stored.rung_index == 0
        got = store.get_cursor("job-1")
        assert got is not None
        assert got.policy_hash == start.cursor.policy_hash
        assert got.rung_index == 0

    def test_put_initial_duplicate_raises(self) -> None:
        store = InMemoryEscalationCursorStore()
        ladder = _ladder()
        start = initial_route(ladder)
        store.put_initial("job-1", start.cursor)
        with pytest.raises(CursorAlreadyExists):
            store.put_initial("job-1", start.cursor)

    def test_cas_advance_success_on_monotonic_generation(self) -> None:
        store = InMemoryEscalationCursorStore()
        ladder = _ladder()
        start = initial_route(ladder)
        store.put_initial("job-1", start.cursor)
        esc = decide_escalate(ladder, start.cursor)
        assert esc.kind is DecisionKind.ESCALATE
        advanced = store.cas_advance(
            "job-1",
            expected_generation=start.cursor.generation,
            new_cursor=esc.cursor,
        )
        assert advanced is not None
        assert advanced.rung_index == 1
        assert advanced.generation == start.cursor.generation + 1
        got = store.get_cursor("job-1")
        assert got is not None
        assert got.rung_index == 1

    def test_cas_conflict_returns_none_explicitly(self) -> None:
        store = InMemoryEscalationCursorStore()
        ladder = _ladder()
        start = initial_route(ladder)
        store.put_initial("job-1", start.cursor)
        esc = decide_escalate(ladder, start.cursor)
        conflict = store.cas_advance(
            "job-1",
            expected_generation=start.cursor.generation + 99,
            new_cursor=esc.cursor,
        )
        assert conflict is None
        got = store.get_cursor("job-1")
        assert got is not None
        assert got.generation == start.cursor.generation

    def test_cas_missing_scope_returns_none(self) -> None:
        store = InMemoryEscalationCursorStore()
        ladder = _ladder()
        start = initial_route(ladder)
        esc = decide_escalate(ladder, start.cursor)
        assert (
            store.cas_advance(
                "nope",
                expected_generation=0,
                new_cursor=esc.cursor,
            )
            is None
        )

    def test_retry_does_not_cas_as_escalation_generation(self) -> None:
        """Retry keeps generation; CAS that requires +1 rejects same-generation."""
        store = InMemoryEscalationCursorStore()
        ladder = _ladder()
        start = initial_route(ladder)
        store.put_initial("job-1", start.cursor)
        retry = decide_retry(ladder, start.cursor)
        assert retry.cursor.generation == start.cursor.generation
        with pytest.raises(InvalidCursorAdvance, match="generation"):
            store.cas_advance(
                "job-1",
                expected_generation=start.cursor.generation,
                new_cursor=retry.cursor,
            )


class TestTerminalClosedInStore:
    def test_cannot_reopen_terminal_to_non_terminal(self) -> None:
        store = InMemoryEscalationCursorStore()
        ladder = _ladder()
        start = initial_route(ladder)
        store.put_initial("job-1", start.cursor)

        cursor = start.cursor
        while True:
            d = decide_escalate(ladder, cursor)
            advanced = store.cas_advance(
                "job-1",
                expected_generation=cursor.generation,
                new_cursor=d.cursor,
            )
            assert advanced is not None
            cursor = advanced
            if is_terminal(cursor):
                break

        assert cursor.terminal is DecisionKind.ESCALATION_EXHAUSTED

        reopen = EscalationCursor(
            policy_hash=cursor.policy_hash,
            rung_index=cursor.rung_index,
            generation=cursor.generation + 1,
            terminal=None,
            visited=cursor.visited,
        )
        with pytest.raises(InvalidCursorAdvance, match="terminal"):
            store.cas_advance(
                "job-1",
                expected_generation=cursor.generation,
                new_cursor=reopen,
            )
        still = store.get_cursor("job-1")
        assert still is not None
        assert is_terminal(still)
        assert still.terminal is DecisionKind.ESCALATION_EXHAUSTED

    def test_idempotent_same_terminal_reclose_allowed(self) -> None:
        store = InMemoryEscalationCursorStore()
        ladder = _ladder()
        start = initial_route(ladder)
        store.put_initial("job-1", start.cursor)
        cursor = start.cursor
        while True:
            d = decide_escalate(ladder, cursor)
            advanced = store.cas_advance(
                "job-1",
                expected_generation=cursor.generation,
                new_cursor=d.cursor,
            )
            assert advanced is not None
            cursor = advanced
            if is_terminal(cursor):
                break

        same_terminal = EscalationCursor(
            policy_hash=cursor.policy_hash,
            rung_index=cursor.rung_index,
            generation=cursor.generation + 1,
            terminal=cursor.terminal,
            visited=cursor.visited,
            skip_reasons=cursor.skip_reasons,
        )
        reclosed = store.cas_advance(
            "job-1",
            expected_generation=cursor.generation,
            new_cursor=same_terminal,
        )
        assert reclosed is not None
        assert reclosed.terminal == cursor.terminal


class TestCursorSerialization:
    def test_round_trip_record(self) -> None:
        ladder = _ladder()
        start = initial_route(ladder)
        esc = decide_escalate(ladder, start.cursor)
        raw = cursor_to_record(esc.cursor)
        restored = cursor_from_record(raw)
        assert restored.policy_hash == esc.cursor.policy_hash
        assert restored.rung_index == esc.cursor.rung_index
        assert restored.generation == esc.cursor.generation
        assert restored.terminal == esc.cursor.terminal
        assert restored.visited == esc.cursor.visited

    def test_terminal_round_trip(self) -> None:
        ladder = _ladder()
        cursor = initial_route(ladder).cursor
        while True:
            d = decide_escalate(ladder, cursor)
            cursor = d.cursor
            if is_terminal(cursor):
                break
        raw = cursor_to_record(cursor)
        assert raw["terminal"] == "escalation_exhausted"
        restored = cursor_from_record(raw)
        assert is_terminal(restored)
        assert restored.terminal is DecisionKind.ESCALATION_EXHAUSTED

    @pytest.mark.parametrize("field", ["rung_index", "generation"])
    @pytest.mark.parametrize("value", [True, 1.5, object(), "not-an-integer"])
    def test_integer_fields_reject_non_integer_values(self, field: str, value: object) -> None:
        raw = cursor_to_record(initial_route(_ladder()).cursor)
        raw[field] = value
        with pytest.raises(ValueError, match=rf"{field} must be an integer"):
            cursor_from_record(raw)

    def test_attempt_transcript_records_advances(self) -> None:
        store = InMemoryEscalationCursorStore()
        ladder = _ladder()
        start = initial_route(ladder)
        store.put_initial("job-1", start.cursor)
        esc = decide_escalate(ladder, start.cursor)
        store.cas_advance(
            "job-1",
            expected_generation=start.cursor.generation,
            new_cursor=esc.cursor,
        )
        rows = store.attempt_transcript("job-1")
        assert len(rows) == 2
        assert rows[0].scope_id == "job-1"
        assert rows[0].generation == start.cursor.generation
        assert rows[1].generation == esc.cursor.generation
        assert rows[1].canonical_model == "claude-opus"
