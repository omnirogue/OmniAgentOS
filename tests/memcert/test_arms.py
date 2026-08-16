"""Decisive tests for scripts/memcert/arms.py (DESIGN §5/§12 arm context builders).

Hermetic: no network, no wall clock, deterministic, tmp_path only. Fixtures are
built by hand (gen.py may not exist yet in a parallel build) in the documented
session-jsonl shape arms.py reads (see arms.py's module docstring). The module
under test is loaded from its file path, matching
tests/scripts/test_prompt_ab_runner.py and tests/memcert/test_grade.py.
"""

from __future__ import annotations

import importlib.util
import json
import math
from pathlib import Path

import pytest


def _load(name: str, rel: str):
    path = Path(__file__).parents[2] / rel
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# Load arms.py first and read `core` off of it (rather than loading core.py
# independently via spec_from_file_location) so that on Python 3.12,
# core.py's frozen dataclasses resolve their annotations against a properly
# sys.modules-registered `core` -- arms.py's own import fallback does a real
# `import core` (registering it), which spec_from_file_location loading
# core.py directly here would not.
ARMS = _load("memcert_arms", "scripts/memcert/arms.py")
CORE = ARMS.core


# --------------------------------------------------------------------------
# Fixture world: 3 sessions, hand-written in the shape arms.py's docstring
# documents ("[<timestamp>] <role>: <text>" renderable stream-json entries).
# --------------------------------------------------------------------------

RUN_UUID = "test-run-0001"
CANARY_TEXT = CORE.canary_line(RUN_UUID)

# s2 line index 13 (the very last line) carries the one rare token the rag
# test searches for; every other line is generic filler. 14 lines means the
# 12-line/stride-6 chunker produces exactly two chunks for s2 (s2:0 = lines
# 0-11, s2:1 = lines 6-13) and only s2:1 contains the rare token.
_S2_LINES = [
    "Morning check-in: systems nominal.",
    "Acknowledged, systems nominal.",
    "Reviewing the deployment queue now.",
    "Deployment queue looks clear.",
    "Budget review scheduled for later.",
    "Noted, budget review later.",
    "Switching to the infra sync topic.",
    "Ready for infra sync.",
    "Checking disk usage on the pool.",
    "Disk usage looks fine.",
    "Discussing the new caching layer.",
    "Caching layer looks promising.",
    "One more item before we close out.",
    "The server codename is flibbertigibbet9000.",
]
RARE_TERM = "flibbertigibbet9000"


def _entry(i: int, role: str, text: str, *, day: str) -> dict:
    return {
        "type": role,
        "timestamp": f"2026-01-{day}T09:{i:02d}:00Z",
        "message": {"role": role, "content": [{"type": "text", "text": text}]},
    }


def _write_session(sessions_dir: Path, sid: str, day: str, lines: list[str]) -> None:
    entries = [{"type": "canary", "text": CANARY_TEXT}]
    for i, text in enumerate(lines):
        role = "user" if i % 2 == 0 else "assistant"
        entries.append(_entry(i, role, text, day=day))
    path = sessions_dir / f"{sid}.jsonl"
    path.write_text("\n".join(json.dumps(e) for e in entries) + "\n", encoding="utf-8")


@pytest.fixture
def world(tmp_path: Path) -> Path:
    world_dir = tmp_path / "world"
    sessions_dir = world_dir / "sessions"
    sessions_dir.mkdir(parents=True)
    _write_session(
        sessions_dir,
        "s1",
        "01",
        ["The office opens at nine each day.", "Noted, the office opens at nine."],
    )
    _write_session(sessions_dir, "s2", "02", _S2_LINES)
    _write_session(
        sessions_dir,
        "s3",
        "03",
        ["Friday standup moved to 10am this week.", "Got it, Friday standup at 10am."],
    )
    return world_dir


def _item(
    item_id: str,
    axis: str,
    question: str = "irrelevant question",
    arm_overrides: dict | None = None,
) -> CORE.Item:
    return CORE.Item(
        item_id=item_id,
        axis=axis,
        level=1,
        split="dev",
        question=question,
        answer_spec=CORE.AnswerSpec(kind="exact", value="n/a"),
        session_scope=(),
        cluster_id="w1",
        arm_overrides=arm_overrides or {},
    )


def _rng():
    return CORE.rng_for(1, "test_arms")


# --------------------------------------------------------------------------
# 1. none is empty
# --------------------------------------------------------------------------


def test_none_arm_is_empty(world: Path) -> None:
    ctx = ARMS.build_context("none", world, _item("i1", "A"), 100, _rng())
    assert ctx.context_block == ""
    assert ctx.meta["chars"] == 0
    assert ctx.meta["truncated"] is False


# --------------------------------------------------------------------------
# 2. fullhistory contains text from every session when under budget
# --------------------------------------------------------------------------


def test_fullhistory_contains_every_session_under_budget(world: Path) -> None:
    ctx = ARMS.build_context("fullhistory", world, _item("i2", "A"), 5000, _rng())
    assert "office opens at nine" in ctx.context_block
    assert RARE_TERM in ctx.context_block
    assert "Friday standup" in ctx.context_block
    assert ctx.meta["truncated"] is False
    assert ctx.meta["sources"] == ["s1", "s2", "s3"]


# --------------------------------------------------------------------------
# 3. fullhistory truncates oldest-first over budget and sets truncated
# --------------------------------------------------------------------------


def test_fullhistory_truncates_oldest_first_over_budget(world: Path) -> None:
    ctx = ARMS.build_context("fullhistory", world, _item("i3", "A"), 8, _rng())
    assert ctx.meta["truncated"] is True
    assert "office opens" not in ctx.context_block
    assert "s1" not in ctx.meta["sources"]
    # Some tail of the newest session should have survived.
    assert ctx.context_block != ""


def test_fullhistory_drops_whole_oldest_session_before_partial(world: Path) -> None:
    # Budget that fits s3 alone (plus a hair) but not s2+s3: s1 must be fully
    # absent, s3 fully present.
    sessions = ARMS._rendered_sessions(world)
    s3_chars = ARMS._chars_len(dict(sessions)["s3"])
    budget_tokens = math.ceil((s3_chars + 2) / 4)
    ctx = ARMS.build_context("fullhistory", world, _item("i3b", "A"), budget_tokens, _rng())
    assert "Friday standup moved to 10am" in ctx.context_block
    assert "office opens" not in ctx.context_block
    assert "s1" not in ctx.meta["sources"]


# --------------------------------------------------------------------------
# 4. transcript selects most recent sessions
# --------------------------------------------------------------------------


def test_transcript_selects_most_recent_sessions(world: Path) -> None:
    sessions = ARMS._rendered_sessions(world)
    s3_chars = ARMS._chars_len(dict(sessions)["s3"])
    budget_tokens = math.ceil(s3_chars / 4)
    ctx = ARMS.build_context("transcript", world, _item("i4", "A"), budget_tokens, _rng())
    assert "s1" not in ctx.meta["sources"]
    assert "s3" in ctx.meta["sources"]
    assert "Friday standup" in ctx.context_block
    assert "office opens" not in ctx.context_block
    assert ctx.meta["truncated"] is True


def test_transcript_renders_selected_sessions_chronologically(world: Path) -> None:
    ctx = ARMS.build_context("transcript", world, _item("i4b", "A"), 5000, _rng())
    # Under a generous budget everything fits; s2's content must still precede
    # s3's content in the rendered block (chronological order preserved).
    assert ctx.context_block.index(RARE_TERM) < ctx.context_block.index("Friday standup")
    assert ctx.meta["sources"] == ["s1", "s2", "s3"]
    assert ctx.meta["truncated"] is False


# --------------------------------------------------------------------------
# 5. rag: a chunk containing the query's rare term ranks first; deterministic
# --------------------------------------------------------------------------


def test_rag_ranks_chunk_with_rare_term_first_and_is_deterministic(world: Path) -> None:
    item = _item("i5", "A", question=f"What is {RARE_TERM}?")
    ctx1 = ARMS.build_context("rag", world, item, 1000, _rng())
    ctx2 = ARMS.build_context("rag", world, item, 1000, _rng())

    assert ctx1.meta["num_chunks"] == 4  # s1:0, s2:0, s2:1, s3:0
    assert ctx1.meta["sources"][0] == "s2:1"
    assert RARE_TERM in ctx1.context_block

    assert ctx1.context_block == ctx2.context_block
    assert ctx1.meta == ctx2.meta


def test_rag_respects_budget_with_whole_chunk_granularity(world: Path) -> None:
    item = _item("i5b", "A", question=f"What is {RARE_TERM}?")
    ctx = ARMS.build_context("rag", world, item, 1, _rng())
    # Budget far too small for even the top chunk: nothing included, but the
    # ranking must still have been computed without error.
    assert ctx.context_block == "" or len(ctx.context_block) <= 4 * 1.05


# --------------------------------------------------------------------------
# 6. lessons routing for MEM-G items: real/placebo/shuffled pick the right
#    override lists; placebo length parity within +/-10% of real.
# --------------------------------------------------------------------------


def _g_item() -> CORE.Item:
    lessons_real = [
        "2026-01-01: office opens at nine, confirmed twice.",
        "2026-01-02: server codename is flibbertigibbet9000.",
    ]
    # Reversing each line's characters guarantees exact length parity (a
    # stronger, non-flaky version of the +/-10% token-matched-irrelevant
    # requirement) while being obviously unrelated content.
    lessons_placebo = [line[::-1] for line in lessons_real]
    lessons_shuffled = list(reversed(lessons_real))
    return _item(
        "g1",
        "G",
        arm_overrides={
            "lessons_real": lessons_real,
            "lessons_placebo": lessons_placebo,
            "lessons_shuffled": lessons_shuffled,
        },
    )


def test_lessons_routes_g_items_to_matching_override_lists(world: Path) -> None:
    item = _g_item()
    real = ARMS.build_context("lessons", world, item, 1000, _rng())
    placebo = ARMS.build_context("lessons_placebo", world, item, 1000, _rng())
    shuffled = ARMS.build_context("lessons_shuffled", world, item, 1000, _rng())

    assert real.context_block == "\n".join(item.arm_overrides["lessons_real"])
    assert placebo.context_block == "\n".join(item.arm_overrides["lessons_placebo"])
    assert shuffled.context_block == "\n".join(item.arm_overrides["lessons_shuffled"])
    assert "fallback" not in real.meta
    assert "fallback" not in placebo.meta
    assert "fallback" not in shuffled.meta


def test_lessons_placebo_length_parity_within_10_percent(world: Path) -> None:
    item = _g_item()
    real = ARMS.build_context("lessons", world, item, 1000, _rng())
    placebo = ARMS.build_context("lessons_placebo", world, item, 1000, _rng())
    real_len = len(real.context_block)
    placebo_len = len(placebo.context_block)
    assert real_len > 0
    assert abs(placebo_len - real_len) <= 0.10 * real_len


def test_lessons_non_g_items_fall_back_to_transcript(world: Path) -> None:
    non_g = _item("a1", "A")
    for arm in ("lessons", "lessons_placebo", "lessons_shuffled"):
        got = ARMS.build_context(arm, world, non_g, 5000, _rng())
        transcript = ARMS.build_context("transcript", world, non_g, 5000, _rng())
        assert got.context_block == transcript.context_block
        assert got.meta["fallback"] == "transcript"
        assert got.arm == arm


# --------------------------------------------------------------------------
# 7. canary lines never appear in any context_block
# --------------------------------------------------------------------------


def test_canary_lines_never_appear_in_any_context_block(world: Path) -> None:
    items = [_g_item(), _item("a1", "A", question=f"What is {RARE_TERM}?")]
    for item in items:
        for arm in ARMS.ARM_NAMES:
            for budget_tokens in (1, 5, 50, 5000):
                ctx = ARMS.build_context(arm, world, item, budget_tokens, _rng())
                assert CANARY_TEXT not in ctx.context_block
                assert "MEMCERT-CANARY" not in ctx.context_block


# --------------------------------------------------------------------------
# 8. budget respected: len(context_block)/4 <= budget_tokens * 1.05 for every
#    arm.
# --------------------------------------------------------------------------


def test_budget_respected_for_every_arm(world: Path) -> None:
    items = [_g_item(), _item("a1", "A", question=f"What is {RARE_TERM}?")]
    for item in items:
        for arm in ARMS.ARM_NAMES:
            for budget_tokens in (1, 5, 20, 100, 1000):
                ctx = ARMS.build_context(arm, world, item, budget_tokens, _rng())
                assert len(ctx.context_block) / 4 <= budget_tokens * 1.05, (
                    arm,
                    budget_tokens,
                    len(ctx.context_block),
                )


def test_system_arm_dispatches_to_system_module(world: Path) -> None:
    # The system arm is wired (2026-08-12); it needs the omniagentos runtime,
    # so here we only pin that dispatch reaches it (real coverage lives in
    # test_system_arm.py against a full gen.py world).
    ctx = ARMS.build_context("system", world, _item("i9", "A"), 100, _rng())
    assert ctx.arm == "system"


def test_unknown_arm_raises(world: Path) -> None:
    with pytest.raises(ValueError):
        ARMS.build_context("not-a-real-arm", world, _item("i10", "A"), 100, _rng())
