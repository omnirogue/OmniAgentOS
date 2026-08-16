"""Decisive tests for the hybrid assembler upgrades (OMNIAGENTOS_MEMORY_HYBRID).

Four mechanisms, each pre-registered as a memcert hypothesis
(devtasks/memcert/DESIGN-v2.md §2) and each pinned here:

1. history leg — "## RELEVANT HISTORY" from behind the recency window (H1);
2. temporal stamps on rendered turns (axis C);
3. abstention guard line in the guidance (H2);
4. budget-reserved packing — a rich recency window cannot starve the
   lessons/knowledge/history sections (the measured axis-G mechanism).

Flag OFF must render a byte-identical v1 BLOCK (telemetry keeps a zero
history_hits field — the claim is prompt-block scope) — asserted directly.
"""

from __future__ import annotations

import pytest

from omniagentos.memory.assemble import assemble_context
from omniagentos.memory.contracts import ConversationTurn


def _turn(
    seq: int, content: str, role: str = "user", ts: str | None = None, created_at: str | None = None
) -> ConversationTurn:
    meta = {"ts": ts} if ts else {}
    return ConversationTurn(  # type: ignore[arg-type]
        seq=seq, role=role, content=content, created_at=created_at, meta=meta
    )


class _Reader:
    def __init__(self, turns: list[ConversationTurn], summary: str | None = None) -> None:
        self.turns = turns
        self.summary = summary

    def recent_turns(self, scope_type: str, scope_id: str, limit: int) -> list[ConversationTurn]:
        return self.turns[-limit:] if limit < len(self.turns) else list(self.turns)

    def resolve_ancestors(self, scope_type: str, scope_id: str):
        return []

    def rolling_summary(self, scope_type: str, scope_id: str):
        return self.summary


def _corpus(n: int = 40) -> list[ConversationTurn]:
    return [
        _turn(
            i,
            f"Routine filler chatter with nothing of note, slot {i}.",
            ts=f"2027-03-{(i % 27) + 1:02d}T09:00:00Z",
        )
        for i in range(n)
    ]


_QUESTION = "Which machine carries the workloads of the project that Bevora leads?"


def _join_corpus() -> list[ConversationTurn]:
    turns = _corpus(40)
    turns[3] = _turn(3, "Bevora leads the Kilmot effort.", ts="2027-03-04T09:00:00Z")
    turns[22] = _turn(22, "Kilmot runs its workloads on Tazvor.", ts="2027-03-23T09:00:00Z")
    return turns


def test_flag_off_is_byte_identical_v1(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OMNIAGENTOS_MEMORY_HYBRID", raising=False)
    ctx = assemble_context("task", "t1", 1200, reader=_Reader(_join_corpus()), task_text=_QUESTION)
    assert "## RELEVANT HISTORY" not in ctx.block
    assert "UNKNOWN" not in ctx.block  # no abstention guard line
    assert "[2027-03-" not in ctx.block  # no temporal stamps
    assert ctx.history_hits == 0
    # v1 turn rendering shape preserved exactly.
    assert "[user] Routine filler chatter" in ctx.block


def test_hybrid_retrieves_join_facts_with_stamps(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OMNIAGENTOS_MEMORY_HYBRID", "1")
    ctx = assemble_context("task", "t1", 1200, reader=_Reader(_join_corpus()), task_text=_QUESTION)
    assert ctx.history_hits == 2
    assert "## RELEVANT HISTORY" in ctx.block
    assert "[2027-03-04] [user] Bevora leads the Kilmot effort." in ctx.block
    assert "[2027-03-23] [user] Kilmot runs its workloads on Tazvor." in ctx.block
    # Abstention guard (H2) present in the guidance line — worded to forbid
    # fabrication-from-memory while leaving tool-based discovery open.
    assert "verify it first, or treat it as UNKNOWN" in ctx.block
    # History renders BEFORE the recent window: block reads old -> new so the
    # freshest statement of an updated fact lands last (axis-D protection).
    assert ctx.block.index("## RELEVANT HISTORY") < ctx.block.index("## CONVERSATION SO FAR")


def test_hybrid_stamps_recent_turns_and_prefers_meta_ts(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OMNIAGENTOS_MEMORY_HYBRID", "1")
    turns = [
        _turn(0, "Alpha note.", ts="2027-03-05T09:00:00Z", created_at="2026-08-13T00:00:00Z"),
        _turn(1, "Beta note.", created_at="2026-08-13T01:00:00Z"),
    ]
    ctx = assemble_context("task", "t1", 800, reader=_Reader(turns), task_text="notes")
    # meta.ts (virtual timeline) outranks created_at (ingestion wall clock).
    assert "[2027-03-05] [user] Alpha note." in ctx.block
    # created_at is the fallback stamp.
    assert "[2026-08-13] [user] Beta note." in ctx.block


def test_reserved_packing_keeps_lessons_under_conversation_pressure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # 12 long recent turns exceed the budget on their own, and the lesson's
    # marginal cost is bigger than the leftover gap v1's greedy packer leaves —
    # so v1 starves the lessons section (the measured axis-G mechanism). Hybrid
    # must keep the lesson via its 15% reserve. Sizes are deliberate: turns
    # ~590 chars (~150 tokens), lesson ~390 chars (~100 tokens), budget 1150,
    # reserve 172 tokens >= one lesson — the reserve-granularity precondition.
    turn_text = ("deploy pipeline detail " * 26)[:590]
    turns = [_turn(i, f"{turn_text} slot {i}") for i in range(12)]
    lesson = ["Lesson: " + ("the darvo approach fails when draining queues; use pelmu " * 7)[:380]]
    budget = 1150

    monkeypatch.delenv("OMNIAGENTOS_MEMORY_HYBRID", raising=False)
    v1 = assemble_context(
        "task", "t1", budget, reader=_Reader(turns),
        memory_recaller=lambda q, k: lesson, task_text="deploy pipeline",
    )
    assert "## LEARNED LESSONS" not in v1.block  # the starvation this fixes

    monkeypatch.setenv("OMNIAGENTOS_MEMORY_HYBRID", "1")
    v2 = assemble_context(
        "task", "t1", budget, reader=_Reader(turns),
        memory_recaller=lambda q, k: lesson, task_text="deploy pipeline",
    )
    assert "## LEARNED LESSONS" in v2.block
    assert "darvo approach fails" in v2.block
    assert v2.estimated_tokens <= budget
    # Conversation still present — reserves shrink it, never erase it.
    assert "## CONVERSATION SO FAR" in v2.block


def test_explicit_history_retriever_is_used_and_faults_degrade(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OMNIAGENTOS_MEMORY_HYBRID", "1")
    supplied = [_turn(2, "Handpicked older fact.", ts="2027-01-02T08:00:00Z")]
    ctx = assemble_context(
        "task", "t1", 800, reader=_Reader(_corpus(20)),
        history_retriever=lambda q, k: supplied, task_text="anything relevant",
    )
    assert "[2027-01-02] [user] Handpicked older fact." in ctx.block
    assert ctx.history_hits == 1

    def _boom(q: str, k: int) -> list[ConversationTurn]:
        raise RuntimeError("retriever fault")

    ctx2 = assemble_context(
        "task", "t1", 800, reader=_Reader(_corpus(20)),
        history_retriever=_boom, task_text="anything relevant",
    )
    assert ctx2.history_hits == 0
    assert "## RELEVANT HISTORY" not in ctx2.block

    # Malformed objects from an injected retriever are filtered INSIDE the
    # guarded seam — they never escape into the offer loop (CR-004-R2).
    junk = [object(), {"seq": 1}, _turn(3, "Real older fact.", ts="2027-01-03T08:00:00Z")]
    ctx3 = assemble_context(
        "task", "t1", 800, reader=_Reader(_corpus(20)),
        history_retriever=lambda q, k: junk,  # type: ignore[arg-type,return-value]
        task_text="anything relevant",
    )
    assert ctx3.history_hits == 1
    assert "Real older fact." in ctx3.block


def test_default_history_call_site_is_guarded_like_its_siblings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The DEFAULT retrieve_history call (no history_retriever supplied) is the
    # path every production caller uses. gemini review, PR#407: unlike the
    # sibling recaller/memory_recaller calls, it had no call-site guard.
    # retrieve_history itself now never raises (see tests/memory/test_history.py
    # ::test_none_returning_reader_degrades_to_empty_not_typeerror), but this
    # pins the call-site guard as defense in depth against any future fault
    # inside it, the same contract the sibling calls already hold.
    monkeypatch.setenv("OMNIAGENTOS_MEMORY_HYBRID", "1")

    def _boom(*args: object, **kwargs: object):
        raise RuntimeError("retrieve_history fault")

    monkeypatch.setattr("omniagentos.memory.history.retrieve_history", _boom)
    ctx = assemble_context(
        "task", "t1", 800,
        reader=_Reader(_corpus(20)),
        task_text="anything relevant",
    )
    assert ctx.history_hits == 0
    assert "## RELEVANT HISTORY" not in ctx.block


def test_history_never_duplicates_recency_window_turns(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OMNIAGENTOS_MEMORY_HYBRID", "1")
    turns = _corpus(20)
    turns[19] = _turn(19, "Kilmot runs its workloads on Tazvor.", ts="2027-03-20T09:00:00Z")
    # A retriever that (wrongly) returns a turn already inside the window.
    ctx = assemble_context(
        "task", "t1", 1200, reader=_Reader(turns),
        history_retriever=lambda q, k: [turns[19]], task_text="Kilmot workloads",
    )
    assert ctx.history_hits == 0
    assert ctx.block.count("Kilmot runs its workloads on Tazvor.") == 1


def test_budget_ceiling_always_holds_under_hybrid(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OMNIAGENTOS_MEMORY_HYBRID", "1")
    turns = _join_corpus()
    for budget in (60, 120, 300, 900):
        ctx = assemble_context("task", "t1", budget, reader=_Reader(turns), task_text=_QUESTION)
        assert ctx.estimated_tokens <= budget


def test_rescue_pass_seats_a_reserved_item_bigger_than_its_floor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # codex-critic CR-002: at small budgets a reserved section's ONE feasible
    # item can cost more than its fractional floor; the main pass skips it and
    # v2 must rescue it by evicting oldest-rendered conversation turns rather
    # than shipping an empty reserved section. Budget 450: lessons floor is 67
    # tokens but the lesson costs ~100 — only the rescue pass can seat it.
    monkeypatch.setenv("OMNIAGENTOS_MEMORY_HYBRID", "1")
    turn_text = ("deploy pipeline detail " * 26)[:590]
    turns = [_turn(i, f"{turn_text} slot {i}") for i in range(12)]
    lesson = ["Lesson: " + ("the darvo approach fails when draining queues; use pelmu " * 7)[:380]]
    ctx = assemble_context(
        "task", "t1", 450, reader=_Reader(turns),
        memory_recaller=lambda q, k: lesson, task_text="deploy pipeline",
    )
    assert "## LEARNED LESSONS" in ctx.block
    assert "darvo approach fails" in ctx.block
    assert ctx.estimated_tokens <= 450


def test_explicit_hybrid_param_overrides_env_both_ways(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The thread-safe pin for concurrent A/B harnesses: an explicit bool wins
    # over the env flag in BOTH directions; None consults the flag.
    turns = _join_corpus()
    monkeypatch.setenv("OMNIAGENTOS_MEMORY_HYBRID", "0")
    forced_on = assemble_context(
        "task", "t1", 1200, reader=_Reader(turns), task_text=_QUESTION, hybrid=True
    )
    assert forced_on.history_hits > 0 and "## RELEVANT HISTORY" in forced_on.block

    monkeypatch.setenv("OMNIAGENTOS_MEMORY_HYBRID", "1")
    forced_off = assemble_context(
        "task", "t1", 1200, reader=_Reader(turns), task_text=_QUESTION, hybrid=False
    )
    assert forced_off.history_hits == 0 and "## RELEVANT HISTORY" not in forced_off.block
    assert "[2027-03-" not in forced_off.block  # no stamps either — full v1 shape
