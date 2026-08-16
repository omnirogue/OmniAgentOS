"""gather_balances: roster parsing, dedupe, fleet lines, and render wiring."""

from __future__ import annotations

from typing import Any

import pytest

import omniagentos.team.session_tracker as tracker
from omniagentos.team import slack_blocks

_CLAUDE_JSON = {
    "accounts": [
        {
            "config_dir": "/u/.claude-account-1",
            "authed": True,
            "remaining_percent": 60.0,
            "duplicate_of": None,
        },
        {
            "config_dir": "/u/.claude-account-2",
            "authed": True,
            "remaining_percent": 5.0,
            "duplicate_of": None,
        },
        {  # duplicate login: must not inflate counts
            "config_dir": "/u/.claude-account-3",
            "authed": True,
            "remaining_percent": 60.0,
            "duplicate_of": "/u/.claude-account-1",
        },
        {  # unauthed: never counted as measured
            "config_dir": "/u/.claude-account-4",
            "authed": False,
            "remaining_percent": 99.0,
            "duplicate_of": None,
        },
    ]
}

_CODEX_JSON = [
    {
        "home": "/u/.codex",
        "short": "~/.codex",
        "login": True,
        "used_percent": 100.0,
        "duplicate_of": None,
    },
    {
        "home": "/u/.codex-second",
        "short": "~/.codex-second",
        "login": True,
        "used_percent": 100.0,
        "duplicate_of": "/u/.codex",
    },
    {
        "home": "/u/.codex-third",
        "short": "~/.codex-third",
        "login": True,
        "used_percent": 6.0,
        "duplicate_of": None,
    },
    {
        "home": "/u/.codex-fourth",
        "short": "~/.codex-fourth",
        "login": False,
        "used_percent": None,
        "duplicate_of": None,
    },
]


def _fake_rosters(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake(command: list[str]) -> Any:
        return _CLAUDE_JSON if "claude-roster" in command[-2] else _CODEX_JSON

    monkeypatch.setattr(tracker, "_roster_json", fake)
    monkeypatch.setattr(tracker, "_pool_capacity", lambda: None)


def test_balances_dedupe_and_best_lines(monkeypatch: pytest.MonkeyPatch) -> None:
    _fake_rosters(monkeypatch)
    text = tracker.gather_balances({})
    assert "Claude (this Mac): best 60% left (.claude-account-1)" in text
    assert "2 measured / 3 distinct" in text  # dup + unauthed excluded from measured
    assert "Codex: best 94% left (~/.codex-third)" in text
    assert "1 out" in text
    assert "no quota meter" in text  # xAI/Gemini never fabricated
    assert "CLAUDE_CONFIG_DIR=/u/.claude-account-1" in text
    assert "CODEX_HOME=/u/.codex-third" in text


def test_remote_fleet_lines_render_with_low_marker(monkeypatch: pytest.MonkeyPatch) -> None:
    _fake_rosters(monkeypatch)
    reports = {
        "emp_alice": {
            "host": "alice-mbp",
            "claude_usage": {
                "best_remaining_percent": 4.0,
                "best_dir": ".claude",
                "authed_accounts": 1,
            },
        },
        "emp_bob": {
            "host": "bob-mbp",
            "claude_usage": {
                "best_remaining_percent": None,
                "best_dir": None,
                "authed_accounts": 1,
            },
        },
    }
    text = tracker.gather_balances(reports)
    assert "Claude (alice-mbp): best 4% left (.claude) · 1 authed ⚠️ LOW" in text
    assert "Claude (bob-mbp): balance unknown" in text


def test_probe_failures_are_loud_lines_never_a_silent_vanish(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(tracker, "_roster_json", lambda command: None)
    monkeypatch.setattr(tracker, "_pool_capacity", lambda: None)
    text = tracker.gather_balances({})
    assert "Claude (this Mac): roster probe failed" in text
    assert "Codex: roster probe failed" in text


def test_codex_dedupe_prefers_account_level_window_percent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    codex = [
        # Primary home's own rollout is stale-low, but the ACCOUNT window
        # (deduped across homes) is exhausted — the account number must win.
        {
            "home": "/u/.codex",
            "short": "~/.codex",
            "login": True,
            "used_percent": 5.0,
            "window_used_percent": 98.0,
            "duplicate_of": None,
        },
        {
            "home": "/u/.codex-b",
            "short": "~/.codex-b",
            "login": True,
            "used_percent": 40.0,
            "window_used_percent": 40.0,
            "duplicate_of": None,
        },
    ]

    def fake(command: list[str]) -> object:
        return _CLAUDE_JSON if "claude-roster" in command[-2] else codex

    monkeypatch.setattr(tracker, "_roster_json", fake)
    monkeypatch.setattr(tracker, "_pool_capacity", lambda: None)
    text = tracker.gather_balances({})
    assert "Codex: best 60% left (~/.codex-b)" in text
    assert "1 out" in text


def test_stale_fleet_report_is_marked_never_presented_as_live(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _fake_rosters(monkeypatch)
    reports = {
        "machine_x": {
            "host": "old-box",
            "_age_seconds": 4 * 3600.0,
            "claude_usage": {
                "best_remaining_percent": 55.0,
                "best_dir": ".claude",
                "authed_accounts": 1,
            },
        },
    }
    text = tracker.gather_balances(reports)
    # Strict staleness (review TRK-02): the last-known percent must NOT render
    # as a live favorable balance — unknown, with the age named.
    assert "Claude (old-box): balance unknown" in text
    assert "4.0h old" in text
    assert "best 55%" not in text


def test_malformed_remote_usage_fields_never_raise(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _fake_rosters(monkeypatch)
    reports = {
        "machine_bad": {
            "host": "bad-box",
            "claude_usage": {
                "best_remaining_percent": float("inf"),
                "best_dir": None,
                "authed_accounts": "broken",
            },
        },
    }
    text = tracker.gather_balances(reports)
    assert "Claude (bad-box): balance unknown" in text


def test_append_balances_is_one_section(monkeypatch: pytest.MonkeyPatch) -> None:
    _fake_rosters(monkeypatch)
    text = tracker.gather_balances({})
    blocks = slack_blocks.append_balances([], text)
    sections = [b for b in blocks if b.get("type") == "section"]
    assert len(sections) == 1
    assert "AI BALANCES" in sections[0]["text"]["text"]


def _drop(tmp_path, name, content):
    directory = tmp_path / "team-sessions"
    directory.mkdir(exist_ok=True)
    (directory / name).write_text(content, encoding="utf-8")
    return directory


def test_corrupt_and_non_report_drop_files_become_unreadable_stubs(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import json as _json

    directory = _drop(tmp_path, "emp-a.json", "not json at all {")
    _drop(tmp_path, "emp-b.json", "[]")
    # JSON bomb: deep nesting raises RecursionError on decode — one stub,
    # never a run-wide abort (review API-02-R4 / TRK-05).
    _drop(tmp_path, "emp-c.json", "[" * 20000 + "]" * 20000)
    _drop(
        tmp_path,
        "emp-d.json",
        _json.dumps({"employee_id": "emp-d", "host": "d-box", "sessions": 1}),
    )
    monkeypatch.setattr(tracker, "_drop_dir", lambda repo_root: directory)

    reports = tracker.read_person_reports(tmp_path)
    for stem in ("emp-a", "emp-b", "emp-c", "emp-d"):
        assert reports[stem].get("_unreadable") is True, stem

    # render() and the Slack blocks must both name the unknown state, never
    # fabricate "0 active / 0 recent" (reviews TRK-05-R5 / TRK-06-R5).
    text = tracker.render(tracker.Overall(), [("emp-d", "emp-d")], reports)
    assert "drop-file unreadable — state unknown" in text
    _, blocks = slack_blocks.tracker_blocks(
        tracker.Overall(), [("emp-d", "emp-d")], reports, stamp="t", fresh_seconds=7200.0
    )
    rendered = " ".join(
        b.get("text", {}).get("text", "") for b in blocks if b.get("type") == "section"
    )
    assert "drop-file unreadable" in rendered
    assert "0 active" not in rendered


def test_real_report_always_beats_an_unreadable_stub(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import json as _json

    # Stub file sorts FIRST; the real (but stale) report shares the employee id
    # and must still win (review TRK-07-R5).
    directory = _drop(tmp_path, "a-corrupt.json", "{")
    (directory / "z-real.json").write_text(
        _json.dumps(
            {
                "employee_id": "a-corrupt",
                "host": "real-box",
                "generated_at": "2020-01-01T00:00:00Z",
                "sessions": [],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(tracker, "_drop_dir", lambda repo_root: directory)
    reports = tracker.read_person_reports(tmp_path)
    assert reports["a-corrupt"].get("_unreadable") is None
    assert reports["a-corrupt"]["host"] == "real-box"


def test_wrong_typed_counts_stub_the_report(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    import json as _json

    directory = _drop(
        tmp_path,
        "emp-x.json",
        _json.dumps(
            {
                "employee_id": "emp-x",
                "host": "x-box",
                "active_count": "three",
                "sessions": [],
            }
        ),
    )
    monkeypatch.setattr(tracker, "_drop_dir", lambda repo_root: directory)
    reports = tracker.read_person_reports(tmp_path)
    assert reports["emp-x"].get("_unreadable") is True
