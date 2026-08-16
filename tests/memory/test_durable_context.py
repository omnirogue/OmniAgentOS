"""Durable delegation and skill context survives ordinary conversation pruning."""

from __future__ import annotations

from omniagentos.memory.assemble import _extract_durable_ledger, assemble_context
from omniagentos.memory.contracts import ConversationTurn, ScopeRef


class StubReader:
    def __init__(self, turns: list[ConversationTurn]) -> None:
        self.turns = turns

    def recent_turns(self, _scope_type: str, _scope_id: str, limit: int) -> list[ConversationTurn]:
        return self.turns[-limit:] if limit > 0 else []

    def resolve_ancestors(self, _scope_type: str, _scope_id: str) -> list[ScopeRef]:
        return []

    def rolling_summary(self, _scope_type: str, _scope_id: str) -> str | None:
        return None


def _turn(seq: int, *, meta: dict[str, object] | None = None) -> ConversationTurn:
    return ConversationTurn(seq=seq, role="agent", content="recorded event", meta=meta or {})


def _delegation(seq: int, *, summary: str = "ready") -> ConversationTurn:
    return _turn(
        seq,
        meta={
            "kind": "delegation",
            "delegation_id": f"delegate-{seq}",
            "outcome": "success",
            "artifact_pointer": f"/artifacts/delegation-{seq}.md",
            "summary": summary,
        },
    )


def test_durable_ledger_captures_delegations() -> None:
    ctx = assemble_context("task", "tsk_1", 2000, reader=StubReader([_delegation(1)]))

    assert "## DURABLE LEDGER" in ctx.block
    assert "[delegation-delega] success" in ctx.block
    assert "/artifacts/delegation-1.md" in ctx.block
    assert ctx.durable_ledger_entries == 1


def test_durable_ledger_captures_loaded_skills() -> None:
    reader = StubReader(
        [
            _turn(
                1,
                meta={
                    "kind": "loaded_skill",
                    "skill_name": "durable-objects",
                    "artifact_pointer": "/skills/durable-objects/SKILL.md",
                },
            )
        ]
    )
    ctx = assemble_context("task", "tsk_1", 2000, reader=reader)

    assert "LOADED: durable-objects from /skills/durable-objects/SKILL.md" in ctx.block
    assert ctx.durable_ledger_entries == 1


def test_durable_ledger_byte_cap() -> None:
    turns = [_delegation(seq, summary="x" * 180) for seq in range(12)]
    ledger, entry_count = _extract_durable_ledger(turns)

    assert len(ledger.encode("utf-8")) <= 800
    assert "earlier entries elided" in ledger
    assert entry_count < len(turns)


def test_durable_ledger_survives_truncation() -> None:
    reader = StubReader([_delegation(1)])
    ctx = assemble_context("task", "tsk_1", 100, reader=reader, max_node_turns=0)

    assert "## DURABLE LEDGER" in ctx.block
    assert ctx.durable_ledger_entries > 0
    assert ctx.node_turns == 0


def test_empty_ledger_no_section() -> None:
    ctx = assemble_context("task", "tsk_1", 2000, reader=StubReader([_turn(1)]))

    assert "## DURABLE LEDGER" not in ctx.block
    assert ctx.durable_ledger_entries == 0


def test_mixed_metadata_kinds() -> None:
    reader = StubReader(
        [
            _delegation(1),
            _turn(
                2,
                meta={
                    "kind": "loaded_skill",
                    "skill_name": "scraper",
                    "path": "/skills/scraper/SKILL.md",
                },
            ),
            _turn(3, meta={"kind": "summary", "artifact_pointer": "/ignore/me"}),
        ]
    )
    ctx = assemble_context("task", "tsk_1", 2000, reader=reader)

    ledger = ctx.block.split("## DURABLE LEDGER\n", 1)[1].split("\n## ", 1)[0]
    assert "delegation-1.md" in ledger
    assert "LOADED: scraper" in ledger
    assert "/ignore/me" not in ledger
    assert ctx.durable_ledger_entries == 2


def test_ledger_ordering_newest_first() -> None:
    turns = [_delegation(seq, summary="x" * 180) for seq in range(12)]
    ledger, _entry_count = _extract_durable_ledger(turns)

    assert "/artifacts/delegation-11.md" in ledger
    assert "/artifacts/delegation-0.md" not in ledger
    assert "earlier entries elided" in ledger
    assert ledger.index("delegation-11.md") < ledger.index("delegation-10.md")
