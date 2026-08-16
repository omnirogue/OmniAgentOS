"""Overnight reserve-and-run: shared numbering, reservations, launch containment."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from omniagentos.team import decisions, overnight


@pytest.fixture()
def repo_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    (tmp_path / "var").mkdir()
    monkeypatch.setenv("OMNIAGENTOS_VAR", str(tmp_path / "var"))
    return tmp_path


class _Notifier:
    def __init__(self) -> None:
        self.posts: list[str] = []

    def post_channel(self, text: str, **kwargs: Any) -> bool:
        self.posts.append(text)
        return True


def _suggest(repo_root: Path) -> list[dict[str, Any]]:
    return overnight.register_suggestions(
        repo_root,
        [{"employee_id": "emp_owner", "card_id": "btk_1", "ref": "U7", "title": "Shared queue spec"}],
    )


def test_shared_allocator_with_repairs_and_daily_dedup(repo_root: Path) -> None:
    from omniagentos.team.session_tracker import Overall

    decisions.register_repair_proposals(
        repo_root, Overall(bottleneck="merge gate red (x)", failed_merges_last_hour=1)
    )
    pending = _suggest(repo_root)
    assert [d["number"] for d in pending] == [2]  # repairs took 1; one number space
    assert _suggest(repo_root)[0]["number"] == 2  # same-day dedup, not renumbered


def test_confirmed_overnight_reserves_never_files_findings(
    repo_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _suggest(repo_root)
    monkeypatch.setattr(
        decisions, "_file_repair_finding",
        lambda root, d: (_ for _ in ()).throw(AssertionError("finding filer must not run")),
    )
    monkeypatch.setattr(
        decisions, "_slack_history",
        lambda token, channel, oldest: [{"user": "UOWNER", "text": "1 yes"}],
    )
    monkeypatch.setattr(decisions, "load_slack_map", lambda: {"UOWNER": "emp_owner"})
    outbound = _Notifier()
    stats = decisions.process_replies(
        repo_root, notifier=outbound, token="t", channel="C"
    )
    assert stats["approved"] == 1 and stats["executed"] == 0
    assert "reserved for" in outbound.posts[0]
    assert overnight.reserve_approved(repo_root) == 1
    queue = overnight.load_reservations(repo_root)
    assert queue[0]["ref"] == "U7" and queue[0]["launched"] is False
    assert overnight.reserve_approved(repo_root) == 0  # idempotent


def test_launch_uses_worktree_gtimeout_and_marks_launched(
    repo_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    overnight.save_reservations(
        repo_root,
        [{"number": 5, "employee_id": "emp_owner", "card_id": "btk_1", "ref": "U7",
          "title": "Shared queue spec", "launched": False}],
    )
    runs: list[list[str]] = []
    popens: list[str] = []
    monkeypatch.setattr(
        overnight.subprocess, "run",
        lambda cmd, **k: runs.append(cmd) or type("R", (), {"returncode": 0})(),
    )
    monkeypatch.setattr(
        overnight.subprocess, "Popen",
        lambda cmd, **k: popens.append(cmd[-1]) or type("P", (), {"pid": 1})(),
    )
    # Isolate the real worktree root (a hardcoded personal-Mac path) to this
    # test's tmp dir — same as test_worktree_gets_mechanical_deny_settings
    # below; without it this mkdir()s a real absolute path that does not
    # exist off the operator's own machine (fatal on CI/Linux).
    monkeypatch.setattr(overnight, "WORKTREE_ROOT", repo_root / "wt")
    outbound = _Notifier()
    count = overnight.launch_reservations(repo_root, notifier=outbound)
    assert count == 1
    assert any("worktree" in " ".join(map(str, cmd)) for cmd in runs)
    assert "gtimeout" in popens[0] and "acceptEdits" in popens[0]
    assert "overnight/u7-" in popens[0]
    assert overnight.load_reservations(repo_root)[0]["launched"] is True
    assert "launched for U7" in outbound.posts[0]
    # Second pass launches nothing (marked launched).
    assert overnight.launch_reservations(repo_root, notifier=outbound) == 0


def test_session_brief_contains_containment_rules(repo_root: Path) -> None:
    brief = overnight._session_brief(
        {"number": 5, "ref": "U7", "title": "Spec", "card_id": "x"}, "overnight/u7-0811"
    )
    for required in ("NEVER merge", "refs U7", "OVERNIGHT-SUMMARY.md", "BLOCKED.md"):
        assert required in brief


def test_launcher_merge_on_save_preserves_midpass_reservation(
    repo_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A reservation approved WHILE the runner is mid-pass must survive its save."""
    overnight.save_reservations(
        repo_root,
        [{"number": 5, "employee_id": "emp_owner", "card_id": "b1", "ref": "U7",
          "title": "T", "launched": False}],
    )
    def fake_popen(cmd: Any, **k: Any) -> Any:
        # Simulate the 300s processor landing a NEW reservation mid-pass.
        current = overnight.load_reservations(repo_root)
        current.append({"number": 9, "employee_id": "emp_alice", "card_id": "b2",
                        "ref": "U9", "title": "New", "launched": False})
        overnight.save_reservations(repo_root, current)
        return type("P", (), {"pid": 1})()
    monkeypatch.setattr(
        overnight.subprocess, "run",
        lambda cmd, **k: type("R", (), {"returncode": 0})(),
    )
    monkeypatch.setattr(overnight.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(overnight, "WORKTREE_ROOT", repo_root / "wt")  # isolate, see above
    overnight.launch_reservations(repo_root, notifier=_Notifier())
    final = {r["number"]: r for r in overnight.load_reservations(repo_root)}
    assert final[5]["launched"] is True   # our launch recorded
    assert 9 in final and final[9]["launched"] is False  # mid-pass arrival survives


def test_same_day_reapproval_gets_unique_branch(repo_root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    overnight.save_reservations(
        repo_root,
        [{"number": 5, "ref": "U7", "card_id": "b", "title": "T", "employee_id": "e", "launched": False},
         {"number": 8, "ref": "U7", "card_id": "b", "title": "T", "employee_id": "e", "launched": False}],
    )
    branches: list[str] = []
    monkeypatch.setattr(
        overnight.subprocess, "run",
        lambda cmd, **k: branches.append(cmd[cmd.index("-b") + 1]) or type("R", (), {"returncode": 0})(),
    )
    monkeypatch.setattr(
        overnight.subprocess, "Popen", lambda cmd, **k: type("P", (), {"pid": 1})()
    )
    monkeypatch.setattr(overnight, "WORKTREE_ROOT", repo_root / "wt")  # isolate, see above
    overnight.launch_reservations(repo_root, notifier=_Notifier())
    assert len(set(branches)) == 2  # decision number keeps them collide-free


def test_worktree_gets_mechanical_deny_settings(
    repo_root: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    overnight.save_reservations(
        repo_root,
        [{"number": 5, "ref": "U7", "card_id": "b", "title": "T", "employee_id": "e", "launched": False}],
    )
    monkeypatch.setattr(overnight, "WORKTREE_ROOT", tmp_path / "wt")
    monkeypatch.setattr(
        overnight.subprocess, "run",
        lambda cmd, **k: type("R", (), {"returncode": 0})(),
    )
    monkeypatch.setattr(
        overnight.subprocess, "Popen", lambda cmd, **k: type("P", (), {"pid": 1})()
    )
    overnight.launch_reservations(repo_root, notifier=_Notifier())
    settings_files = list((tmp_path / "wt").rglob(".claude/settings.json"))
    assert len(settings_files) == 1
    written = json.loads(settings_files[0].read_text())
    denied = written["permissions"]["deny"]
    assert "Bash(gh pr merge:*)" in denied  # documented prefix grammar
    hook = written["hooks"]["PreToolUse"][0]["hooks"][0]["command"]
    assert "push" in hook and "main|master" in hook  # main-push guard hook
