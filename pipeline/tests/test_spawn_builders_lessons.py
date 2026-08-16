from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bridge import spawn_builders as sb  # noqa: E402
from test_spawn_builders import _proposal, _write_proposal  # noqa: E402

pytest_plugins = ("test_spawn_builders",)


def _brief_for(repo: Path, item: dict) -> str:
    result = sb._run_iteration(repo / "var" / "loopqueue", repo)
    return Path(result["worklist"][0]["brief"]).read_text()


def test_builder_brief_includes_recalled_lessons(loops_root: Path, repo: Path, monkeypatch) -> None:
    item = _proposal("lessons-present")
    _write_proposal(loops_root, item)
    monkeypatch.setattr(
        sb,
        "_recall_lessons",
        lambda summary, *, paths, role, budget_tokens=None: (
            "<recalled-knowledge>known lesson</recalled-knowledge>"
        ),
    )
    monkeypatch.setattr(sb, "_knowledge_enabled", lambda: True)
    brief = _brief_for(repo, item)
    assert "## Lessons from previous runs" in brief
    assert "known lesson" in brief


def test_builder_brief_mints_without_lessons(loops_root: Path, repo: Path, monkeypatch) -> None:
    item = _proposal("lessons-absent")
    _write_proposal(loops_root, item)
    # Sentinel return: if the stub is never actually called (e.g. the call
    # site raises TypeError against a mismatched signature and that gets
    # swallowed), the brief would still mint without a lessons section for
    # the WRONG reason. Asserting the marker below closes that hole.
    monkeypatch.setattr(
        sb,
        "_recall_lessons",
        lambda summary, *, paths, role, budget_tokens=None: "",
    )
    monkeypatch.setattr(sb, "_knowledge_enabled", lambda: True)
    brief = _brief_for(repo, item)
    assert "## Lessons from previous runs" not in brief


def test_builder_brief_ignores_recall_failure(loops_root: Path, repo: Path, monkeypatch) -> None:
    item = _proposal("lessons-raising")
    _write_proposal(loops_root, item)
    reached = {"raised": False}

    def explode(summary, *, paths, role, budget_tokens=None):
        reached["raised"] = True
        raise RuntimeError("knowledge store unavailable")

    monkeypatch.setattr(sb, "_recall_lessons", explode)
    monkeypatch.setattr(sb, "_knowledge_enabled", lambda: True)
    brief = _brief_for(repo, item)
    assert "## Lessons from previous runs" not in brief
    assert reached["raised"] is True


def test_lessons_block_returns_sentinel_from_stub(monkeypatch) -> None:
    """Sentinel test (finding C3): a call-site/stub signature mismatch must
    surface as a test failure, not as a silently-swallowed TypeError that
    also happens to satisfy the "no lessons" assertion."""
    monkeypatch.setattr(
        sb,
        "_recall_lessons",
        lambda summary, *, paths, role, budget_tokens=None: "SENTINEL-MARKER",
    )
    monkeypatch.setattr(sb, "_knowledge_enabled", lambda: True)
    assert sb._lessons_block({"title": "t", "paths": ["a.py"]}) == "SENTINEL-MARKER"


def test_lessons_block_disabled_never_calls_recall(monkeypatch) -> None:
    called = {"hit": False}

    def boom(*_a, **_k):
        called["hit"] = True
        return "should not run"

    monkeypatch.setattr(sb, "_recall_lessons", boom)
    monkeypatch.setattr(sb, "_knowledge_enabled", lambda: False)
    assert sb._lessons_block({"title": "t", "paths": ["a.py"]}) == ""
    assert called["hit"] is False


def test_lessons_block_propagates_keyboard_interrupt(monkeypatch) -> None:
    def boom(*_a, **_k):
        raise KeyboardInterrupt("operator pressed Ctrl-C")

    monkeypatch.setattr(sb, "_recall_lessons", boom)
    monkeypatch.setattr(sb, "_knowledge_enabled", lambda: True)
    with pytest.raises(KeyboardInterrupt):
        sb._lessons_block({"title": "t", "paths": ["a.py"]})
