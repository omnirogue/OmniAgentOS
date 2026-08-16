from __future__ import annotations

import io
from contextlib import redirect_stderr, redirect_stdout

import pytest

from omniagentos.knowledge import brief_recall


def test_recall_lessons_renders_and_does_not_credit(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: dict = {}

    def fake_recall(store, **kwargs):
        calls["store"] = store
        calls.update(kwargs)
        return "result"

    monkeypatch.setattr("omniagentos.knowledge.recall._get_store", lambda: "store")
    monkeypatch.setattr("omniagentos.knowledge.recall.recall", fake_recall)
    monkeypatch.setattr(
        "omniagentos.knowledge.recall.render_recall_block",
        lambda result, **kwargs: "<recalled-knowledge>lesson</recalled-knowledge>",
    )

    block = brief_recall.recall_lessons(
        "Build the bridge", paths=("pipeline/bridge.py",), role="implementer", budget_tokens=42
    )

    assert block.startswith("<recalled-knowledge>")
    assert "Build the bridge" in calls["prompt"]
    assert "pipeline/bridge.py" in calls["prompt"]
    assert "implementer" in calls["prompt"]
    assert calls["run_id"] is None
    # `role` is prompt-text only -- it is not a knowledge discipline.
    assert calls["discipline"] is None


def test_cli_empty_recall_has_empty_stdout(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(brief_recall, "knowledge_enabled", lambda: True)
    monkeypatch.setattr(brief_recall, "recall_lessons", lambda **kwargs: "")
    stdout = io.StringIO()
    with redirect_stdout(stdout):
        code = brief_recall.main(["--task-summary", "nothing to recall"])
    assert code == 0
    assert stdout.getvalue() == ""


def test_cli_store_failure_is_exit_2_and_stderr_only(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(brief_recall, "knowledge_enabled", lambda: True)

    def unavailable(**kwargs):
        raise RuntimeError("store unavailable")

    monkeypatch.setattr(brief_recall, "recall_lessons", unavailable)
    stdout, stderr = io.StringIO(), io.StringIO()
    with redirect_stdout(stdout), redirect_stderr(stderr):
        code = brief_recall.main(["--task-summary", "needs recall"])
    assert code == 2
    assert stdout.getvalue() == ""
    assert "store unavailable" in stderr.getvalue()


def test_cli_disabled_is_exit_3_and_never_calls_recall(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(brief_recall, "knowledge_enabled", lambda: False)
    called = {"hit": False}

    def must_not_run(**kwargs):
        called["hit"] = True
        return "should not run"

    monkeypatch.setattr(brief_recall, "recall_lessons", must_not_run)
    stdout, stderr = io.StringIO(), io.StringIO()
    with redirect_stdout(stdout), redirect_stderr(stderr):
        code = brief_recall.main(["--task-summary", "needs recall"])
    assert code == 3
    assert stdout.getvalue() == ""
    assert "knowledge subsystem is disabled" in stderr.getvalue()
    assert called["hit"] is False


def test_cli_paths_flag_is_repeatable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(brief_recall, "knowledge_enabled", lambda: True)
    captured: dict = {}

    def fake_recall_lessons(*, task_summary, paths, role, budget_tokens):
        captured["paths"] = paths
        return ""

    monkeypatch.setattr(brief_recall, "recall_lessons", fake_recall_lessons)
    stdout = io.StringIO()
    with redirect_stdout(stdout):
        code = brief_recall.main(
            [
                "--task-summary",
                "multi path",
                "--paths",
                "a/one.py",
                "--paths",
                "b/two.py,b/three.py",
            ]
        )
    assert code == 0
    assert captured["paths"] == ("a/one.py", "b/two.py", "b/three.py")
