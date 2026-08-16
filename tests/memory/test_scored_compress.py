"""Compress-before-cap (LLMLingua-2) + scored packing (Generative Agents) in assembly.

Both are OFF by default: ``OMNIAGENTOS_COMPRESS`` unset and ``OMNIAGENTOS_MEMORY_SCORED``
unset leave ``assemble_context`` byte-identical (proven by the existing test_assemble
suite plus the task_text=None case here). Compress on shrinks repeated log noise BEFORE
the char cap so more distinct items fit the same budget; scored on ranks offered items
by recency x importance x relevance so a task-relevant older turn beats an irrelevant
recent one.
"""

from __future__ import annotations

import pytest

from omniagentos.memory.assemble import assemble_context
from omniagentos.memory.contracts import ConversationTurn, ScopeRef


class StubReader:
    def __init__(
        self,
        turns: dict[tuple[str, str], list[ConversationTurn]] | None = None,
        summaries: dict[tuple[str, str], str] | None = None,
        ancestors: dict[tuple[str, str], list[ScopeRef]] | None = None,
    ) -> None:
        self._turns = turns or {}
        self._summaries = summaries or {}
        self._ancestors = ancestors or {}

    def recent_turns(self, scope_type: str, scope_id: str, limit: int) -> list[ConversationTurn]:
        return self._turns.get((scope_type, scope_id), [])[-limit:]

    def resolve_ancestors(self, scope_type: str, scope_id: str) -> list[ScopeRef]:
        return self._ancestors.get((scope_type, scope_id), [])

    def rolling_summary(self, scope_type: str, scope_id: str) -> str | None:
        return self._summaries.get((scope_type, scope_id))


def _turn(seq: int, content: str) -> ConversationTurn:
    return ConversationTurn(seq=seq, role="user", content=content)  # type: ignore[arg-type]


def test_compress_packs_more_distinct_items(monkeypatch: pytest.MonkeyPatch) -> None:
    # Newest turn is 50 identical log lines; the rest are small distinct turns.
    repeated = "\n".join(["duplicate log line here"] * 50)
    distinct = [_turn(i, f"distinctword{i} alpha bravo charlie delta echo") for i in range(4)]
    reader = StubReader(turns={("task", "t1"): [*distinct, _turn(9, repeated)]})

    monkeypatch.delenv("OMNIAGENTOS_COMPRESS", raising=False)
    off = assemble_context("task", "t1", 120, reader=reader)

    monkeypatch.setenv("OMNIAGENTOS_COMPRESS", "basic")
    on = assemble_context("task", "t1", 120, reader=reader)

    # Uncompressed, the 50-line repeat is too big to fit and is dropped entirely; all 4
    # small distinct turns fit but the repeated turn does not.
    assert "[repeated 50 times]" not in off.block
    assert all(f"distinctword{i}" in off.block for i in range(4))
    # Compressed, the repeat collapses to a marker and fits ALONGSIDE all 4 distinct
    # turns within the same budget -> strictly more items packed.
    assert on.node_turns > off.node_turns
    assert "[repeated 50 times]" in on.block
    assert all(f"distinctword{i}" in on.block for i in range(4))


def test_scored_relevant_old_turn_beats_irrelevant_recent(monkeypatch: pytest.MonkeyPatch) -> None:
    reader = StubReader(
        turns={
            ("task", "t1"): [
                _turn(0, "billing invoice export pdf reportlab generation"),  # OLD, relevant
                _turn(1, "weather lunch chit chat unrelated banter today"),  # NEW, irrelevant
            ]
        }
    )
    task = "add a billing invoice export"

    # Baseline (scored off): newest-first offering keeps the irrelevant recent turn.
    monkeypatch.delenv("OMNIAGENTOS_MEMORY_SCORED", raising=False)
    base = assemble_context("task", "t1", 70, reader=reader, task_text=task)
    assert base.node_turns == 1
    assert "weather" in base.block and "billing" not in base.block

    # Scored on: the task-relevant older turn outranks the irrelevant recent one.
    monkeypatch.setenv("OMNIAGENTOS_MEMORY_SCORED", "1")
    scored = assemble_context("task", "t1", 70, reader=reader, task_text=task)
    assert scored.node_turns == 1
    assert "billing" in scored.block and "weather" not in scored.block


def test_task_text_none_preserves_fixed_priority_bit_for_bit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reader = StubReader(
        turns={
            ("task", "t1"): [
                _turn(0, "billing invoice export pdf reportlab"),
                _turn(1, "weather lunch chit chat unrelated"),
            ]
        },
        summaries={("task", "t1"): "Finance node summary", ("project", "p"): "Parent project"},
        ancestors={("task", "t1"): [ScopeRef("project", "p")]},
    )

    # Even with scored ENABLED, task_text=None keeps the fixed-priority order untouched.
    monkeypatch.setenv("OMNIAGENTOS_MEMORY_SCORED", "1")
    scored_none = assemble_context("task", "t1", 4000, reader=reader, task_text=None)
    monkeypatch.delenv("OMNIAGENTOS_MEMORY_SCORED", raising=False)
    disabled = assemble_context("task", "t1", 4000, reader=reader)

    assert scored_none.block == disabled.block
