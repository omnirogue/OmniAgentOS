"""P1-RECOVERY readiness smoke for future escalation_attempt_identity migration.

This root job is policy-only. Durable SQL/materialization belongs to
P1-RECOVERY-CURSOR. This module proves the **dict/record shape** a later
migration would persist — no SQL, no omniagentos.db imports, no schema edits.

If a future migration invents different field names, these tests fail first.
"""

from __future__ import annotations

from omniagentos.routing.escalation import (
    DecisionKind,
    build_ladder,
    decide_escalate,
    initial_route,
    is_terminal,
)
from omniagentos.routing.escalation_store import (
    AttemptIdentityRecord,
    InMemoryEscalationCursorStore,
    cursor_from_record,
    cursor_to_record,
)

# Field names a durable row must be able to reconstruct (contract for CURSOR lane).
CURSOR_ROW_KEYS = frozenset(
    {
        "policy_hash",
        "rung_index",
        "generation",
        "terminal",
        "visited",
        "skip_reasons",
    }
)

VISITED_KEYS = frozenset({"provider", "canonical_model"})

SKIP_REASON_KEYS = frozenset(
    {
        "rung_index",
        "provider",
        "display_model",
        "canonical_model",
        "reason",
    }
)

ATTEMPT_IDENTITY_KEYS = frozenset(
    {
        "scope_id",
        "generation",
        "rung_index",
        "provider",
        "canonical_model",
        "decision_kind",
        "terminal",
        "policy_hash",
    }
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
        ]
    )


class TestCursorMigrationShape:
    def test_cursor_record_has_migration_ready_keys(self) -> None:
        ladder = _ladder()
        start = initial_route(ladder)
        raw = cursor_to_record(start.cursor)
        assert CURSOR_ROW_KEYS.issubset(raw.keys())
        assert isinstance(raw["policy_hash"], str) and raw["policy_hash"]
        assert isinstance(raw["rung_index"], int)
        assert isinstance(raw["generation"], int)
        assert raw["terminal"] is None
        assert isinstance(raw["visited"], list)
        for item in raw["visited"]:
            assert isinstance(item, dict)
            assert VISITED_KEYS.issubset(item.keys())

    def test_terminal_cursor_serializes_typed_terminal_string(self) -> None:
        ladder = _ladder()
        start = initial_route(ladder)
        mid = decide_escalate(ladder, start.cursor)
        done = decide_escalate(ladder, mid.cursor)
        assert done.kind is DecisionKind.ESCALATION_EXHAUSTED
        raw = cursor_to_record(done.cursor)
        assert raw["terminal"] == "escalation_exhausted"
        restored = cursor_from_record(raw)
        assert is_terminal(restored)
        assert restored.terminal is DecisionKind.ESCALATION_EXHAUSTED

    def test_skip_reasons_shape_for_same_identity_walk(self) -> None:
        ladder = build_ladder(
            [
                {
                    "provider": "openai",
                    "display_model": "Sol High",
                    "canonical_model": "gpt-5.6-sol",
                },
                {
                    "provider": "openai",
                    "display_model": "Sol Ultra",
                    "canonical_model": "gpt-5.6-sol",
                },
                {
                    "provider": "anthropic",
                    "display_model": "opus",
                    "canonical_model": "claude-opus",
                },
            ]
        )
        start = initial_route(ladder)
        esc = decide_escalate(ladder, start.cursor)
        assert any(s.reason == "already_visited_identity" for s in esc.skips)
        raw = cursor_to_record(esc.cursor)
        assert isinstance(raw["skip_reasons"], list)
        assert raw["skip_reasons"]
        for skip in raw["skip_reasons"]:
            assert isinstance(skip, dict)
            assert SKIP_REASON_KEYS.issubset(skip.keys())
        assert raw["skip_reasons"][0]["reason"] == "already_visited_identity"


class TestStoreMigrationBoundary:
    def test_in_memory_store_ready_for_cas_consumers(self) -> None:
        store = InMemoryEscalationCursorStore()
        ladder = _ladder()
        start = initial_route(ladder)
        store.put_initial("scope-a", start.cursor)
        esc = decide_escalate(ladder, start.cursor)
        advanced = store.cas_advance(
            "scope-a",
            expected_generation=start.cursor.generation,
            new_cursor=esc.cursor,
        )
        assert advanced is not None
        got = store.get_cursor("scope-a")
        assert got is not None
        assert got.rung_index == esc.cursor.rung_index
        assert cursor_to_record(got)["policy_hash"] == ladder.policy_hash

    def test_attempt_record_fields_match_future_table(self) -> None:
        store = InMemoryEscalationCursorStore()
        ladder = _ladder()
        start = initial_route(ladder)
        store.put_initial("scope-a", start.cursor)
        esc = decide_escalate(ladder, start.cursor)
        store.cas_advance(
            "scope-a",
            expected_generation=start.cursor.generation,
            new_cursor=esc.cursor,
        )
        rows = store.attempt_transcript("scope-a")
        assert rows
        for row in rows:
            assert isinstance(row, AttemptIdentityRecord)
            as_dict = {
                "scope_id": row.scope_id,
                "generation": row.generation,
                "rung_index": row.rung_index,
                "provider": row.provider,
                "canonical_model": row.canonical_model,
                "decision_kind": row.decision_kind,
                "terminal": row.terminal,
                "policy_hash": row.policy_hash,
            }
            assert ATTEMPT_IDENTITY_KEYS == frozenset(as_dict.keys())
            assert as_dict["policy_hash"]
            assert as_dict["scope_id"] == "scope-a"

    def test_no_sql_surface_in_this_lane(self) -> None:
        """Guard: this readiness module must not pull durable DB stack."""
        import omniagentos.routing.escalation_store as store_mod

        source = open(store_mod.__file__, encoding="utf-8").read()
        assert "omniagentos.db" not in source
        assert "sqlite" not in source.lower()
        assert "CREATE TABLE" not in source
        assert "P1-RECOVERY-CURSOR" in source or "durable" in source.lower()
