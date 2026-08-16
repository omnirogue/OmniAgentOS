"""Tests for omniagentos.selfimprove.reflexion -- Reflexion-style failure
reflections for the routing cascade. LLM-mode tests use a stub adapter and
omniagentos.mock_adapter.MockAdapter (scripted via metadata['mock']) --
NEVER a live CLI call."""

from __future__ import annotations

import json
from pathlib import Path

from omniagentos.contracts import AgentInput, AgentResult, AgentUsage, ResultStatus
from omniagentos.mock_adapter import MockAdapter
from omniagentos.selfimprove.reflexion import build_reflection, persist_reflection


class _ScriptedMockAdapter:
    """Wraps the real, frozen MockAdapter, injecting `metadata['mock']`
    scripting onto whatever AgentInput build_reflection constructs (which
    itself carries no metadata) -- lets these tests exercise MockAdapter's
    actual `.run()` path deterministically, per the package brief."""

    def __init__(self, mock_spec: dict) -> None:
        self._adapter = MockAdapter()
        self._mock_spec = mock_spec

    def run(self, input: AgentInput) -> AgentResult:
        scripted = input.model_copy(update={"metadata": {"mock": self._mock_spec}})
        return self._adapter.run(scripted)


class _RaisingAdapter:
    def run(self, input: AgentInput) -> AgentResult:
        raise RuntimeError("adapter exploded")


class _StubAdapter:
    """Minimal hand-written stub (not MockAdapter) satisfying ReflectionAdapter."""

    def __init__(self, reply: str) -> None:
        self._reply = reply

    def run(self, input: AgentInput) -> AgentResult:
        return AgentResult(
            status=ResultStatus.OK,
            output_text=self._reply,
            usage=AgentUsage(wall_ms=1),
        )


def test_template_mode_is_deterministic_and_extracts_error_lines() -> None:
    evidence = "collecting tests\nAssertionError: expected 4 got 5\nFAILED test_math.py::test_add"
    first = build_reflection("fix the adder", evidence)
    second = build_reflection("fix the adder", evidence)

    assert first.source == "template"
    assert first == second
    assert "AssertionError: expected 4 got 5" in first.paragraph
    assert "FAILED test_math.py::test_add" in first.paragraph
    assert "fix the adder" in first.paragraph
    assert "next attempt must address" in first.paragraph.lower()


def test_template_mode_caps_at_max_chars() -> None:
    long_evidence = "AssertionError: " + ("x" * 5000)
    reflection = build_reflection("a very long failing task", long_evidence, max_chars=200)
    assert reflection.source == "template"
    assert len(reflection.paragraph) <= 200


def test_template_mode_falls_back_to_first_line_when_no_error_keywords() -> None:
    reflection = build_reflection("some task", "nothing looks wrong here\nsecond line")
    assert reflection.source == "template"
    assert "nothing looks wrong here" in reflection.paragraph


def test_llm_mode_via_stub_adapter_returns_paragraph() -> None:
    reflection = build_reflection(
        "fix the parser", "SyntaxError: bad token", adapter=_StubAdapter("the parser choked on X")
    )
    assert reflection.source == "llm"
    assert reflection.paragraph == "the parser choked on X"


def test_llm_mode_via_mock_adapter_returns_paragraph() -> None:
    scripted = _ScriptedMockAdapter({"reply": "root cause was a stale cache"})
    reflection = build_reflection("fix the cache bug", "KeyError: cache miss", adapter=scripted)
    assert reflection.source == "llm"
    assert reflection.paragraph == "root cause was a stale cache"


def test_stub_adapter_raising_falls_back_to_template() -> None:
    evidence = "AssertionError: boom"
    reflection = build_reflection("fix it", evidence, adapter=_RaisingAdapter())
    assert reflection.source == "template"
    assert "AssertionError: boom" in reflection.paragraph


def test_mock_adapter_fail_scripting_falls_back_to_template() -> None:
    # MockAdapter's own "fail" scripting produces a non-OK AgentResult;
    # build_reflection must treat that the same as an empty/failed reply.
    scripted = _ScriptedMockAdapter({"fail": True})
    evidence = "Traceback (most recent call last): boom"
    reflection = build_reflection("fix it", evidence, adapter=scripted)
    assert reflection.source == "template"
    assert "Traceback (most recent call last): boom" in reflection.paragraph


def test_empty_llm_reply_falls_back_to_template() -> None:
    reflection = build_reflection("fix it", "AssertionError: nope", adapter=_StubAdapter("   "))
    assert reflection.source == "template"


def test_evidence_truncation_keeps_head_and_tail() -> None:
    head_marker = "HEAD-MARKER-START"
    tail_marker = "TAIL-MARKER-END"
    evidence = head_marker + ("z" * 10_000) + tail_marker

    captured: dict[str, str] = {}

    class _CapturingAdapter:
        def run(self, input: AgentInput) -> AgentResult:
            captured["prompt"] = input.prompt
            return AgentResult(
                status=ResultStatus.OK, output_text="ok", usage=AgentUsage(wall_ms=1)
            )

    build_reflection("task", evidence, adapter=_CapturingAdapter())

    assert head_marker in captured["prompt"]
    assert tail_marker in captured["prompt"]
    # The full 10000+ char body must NOT have made it through unabridged.
    assert len(captured["prompt"]) < len(evidence)


def test_persist_reflection_writes_readable_jsonl(tmp_path: Path) -> None:
    store_dir = tmp_path / "reflexion"
    reflection = build_reflection("task-a", "AssertionError: x != y")

    path = persist_reflection(reflection, task_class="unit-test-class", store_dir=str(store_dir))

    assert Path(path).exists()
    lines = Path(path).read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    row = json.loads(lines[0])
    assert row["task_class"] == "unit-test-class"
    assert row["source"] == "template"
    assert row["paragraph"] == reflection.paragraph
    assert "ts" in row


def test_persist_reflection_appends_across_calls(tmp_path: Path) -> None:
    store_dir = tmp_path / "reflexion"
    first = build_reflection("task-a", "AssertionError: first failure")
    second = build_reflection("task-b", "AssertionError: second failure")

    path1 = persist_reflection(first, task_class="class-a", store_dir=str(store_dir))
    path2 = persist_reflection(second, task_class="class-b", store_dir=str(store_dir))

    assert path1 == path2
    lines = Path(path1).read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    task_classes = {json.loads(line)["task_class"] for line in lines}
    assert task_classes == {"class-a", "class-b"}
