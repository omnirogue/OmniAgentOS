"""The employee kill switch: `~/.ai-telemetry-off` stops both feeds, honestly.

Contract under test (docs/operations/team-telemetry-privacy.md):
- marker present → the collector reports ONLY an opted-out marker (no
  sessions, no usage, no transcript ever opened) and the uploader scans and
  uploads NOTHING, exiting 0;
- marker absent → both behave exactly as before;
- telemetry_ctl off/on/status manage the marker and are idempotent;
- the tracker renders an opted-out drop-file as a choice, not as absence.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from omniagentos.team import session_collector as collector
from omniagentos.team import telemetry_ctl as ctl
from omniagentos.team import transcript_uploader as uploader


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "home"
    root.mkdir()
    monkeypatch.setenv("HOME", str(root))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: root))
    return root


def _write_transcript(home: Path) -> Path:
    path = home / ".claude" / "projects" / "-Users-dev-proj" / "abc123.jsonl"
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps({"message": {"role": "user", "content": "fix the login bug"}}) + "\n",
        encoding="utf-8",
    )
    return path


def test_collector_reports_only_opt_out_marker(home: Path) -> None:
    _write_transcript(home)
    (home / ".ai-telemetry-off").write_text(
        json.dumps({"schema": 1, "off_since": "2026-08-14T00:00:00Z"}), encoding="utf-8"
    )
    report = collector.collect("emp_alice", 75, 15)
    assert report["opted_out"] is True
    assert report["opted_out_since"] == "2026-08-14T00:00:00Z"
    assert report["sessions"] == []
    assert report["active_count"] == 0 and report["recent_count"] == 0
    assert report["claude_usage"]["accounts"] == []
    # Nothing session-derived leaks into the payload.
    assert "fix the login bug" not in json.dumps(report)


def test_collector_normal_when_marker_absent(home: Path) -> None:
    _write_transcript(home)
    report = collector.collect("emp_alice", 75, 15)
    assert "opted_out" not in report
    assert report["recent_count"] == 1
    assert report["sessions"][0]["description"] == "fix the login bug"


def test_empty_handmade_marker_counts_as_off(home: Path) -> None:
    _write_transcript(home)
    (home / ".ai-telemetry-off").touch()
    report = collector.collect("emp_alice", 75, 15)
    assert report["opted_out"] is True
    assert report["opted_out_since"]  # mtime fallback, never empty


def test_uploader_scans_nothing_while_opted_out(
    home: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_transcript(home)
    (home / ".ai-telemetry-off").touch()

    def _boom(*args: object, **kwargs: object) -> None:
        raise AssertionError("harvest must not run while opted out")

    monkeypatch.setattr(uploader, "harvest", _boom)
    assert uploader.main(["--employee", "emp_alice"]) == 0
    out = capsys.readouterr().out
    assert "OFF" in out and "nothing uploaded" in out
    state = home / ".transcript-uploader-state.json"
    assert not state.exists()


def test_uploader_print_reports_opt_out(home: Path, capsys: pytest.CaptureFixture[str]) -> None:
    (home / ".ai-telemetry-off").touch()
    assert uploader.main(["--employee", "emp_alice", "--print"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["opted_out"] is True
    assert payload["would_upload"] == []


def test_ctl_off_on_status_lifecycle(home: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert ctl.main(["status"]) == 0
    assert "ON" in capsys.readouterr().out

    assert ctl.main(["off", "--note", "vacation"]) == 0
    capsys.readouterr()
    marker = json.loads((home / ".ai-telemetry-off").read_text(encoding="utf-8"))
    assert marker["off_since"] and marker["note"] == "vacation"

    # Idempotent: a second off keeps the original timestamp.
    assert ctl.main(["off"]) == 0
    assert "already OFF" in capsys.readouterr().out
    assert json.loads((home / ".ai-telemetry-off").read_text(encoding="utf-8")) == marker

    assert ctl.main(["status"]) == 0
    assert "OFF" in capsys.readouterr().out

    assert ctl.main(["on"]) == 0
    capsys.readouterr()
    assert not (home / ".ai-telemetry-off").exists()
    assert ctl.main(["on"]) == 0
    assert "already ON" in capsys.readouterr().out


def test_tracker_renders_opt_out_as_choice(home: Path) -> None:
    from omniagentos.team import session_tracker as tracker

    reports = {
        "emp_alice": {
            "_age_seconds": 60,
            "opted_out": True,
            "opted_out_since": "2026-08-14T00:00:00Z",
            "host": "alices-laptop",
            "sessions": [],
        }
    }
    overall = tracker.Overall()
    text = tracker.render(overall, [("emp_alice", "Alice")], reports)
    assert "telemetry off (opted out since 2026-08-14T00:00:00Z)" in text
    assert "no session report received" not in text


# --- Fixes from the 2026-08-14 cross-lineage review (findings 1-6) ---


@pytest.mark.skipif(__import__("os").geteuid() == 0, reason="root ignores file modes")
def test_unreadable_marker_fails_closed(home: Path) -> None:
    """Finding 1: an EXISTING marker that cannot be read still means OFF."""
    _write_transcript(home)
    marker = home / ".ai-telemetry-off"
    marker.touch()
    marker.chmod(0o000)
    try:
        report = collector.collect("emp_alice", 75, 15)
        assert report["opted_out"] is True
        assert report["sessions"] == []
        assert uploader.main(["--employee", "emp_alice"]) == 0
        assert ctl._read_marker(home) is not None
    finally:
        marker.chmod(0o600)


def test_collector_midrun_flip_discards_collected_data(
    home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Finding 2: opting out after collection started delivers only the marker."""

    def _collect_then_flip(employee_id: str, *args: object, **kwargs: object) -> dict:
        (home / ".ai-telemetry-off").write_text(
            json.dumps({"schema": 1, "off_since": "2026-08-14T01:02:03Z"}), encoding="utf-8"
        )
        return {
            "schema": 1,
            "employee_id": employee_id,
            "host": "h",
            "generated_at": "2026-08-14T01:00:00Z",
            "active_count": 1,
            "recent_count": 1,
            "sessions": [{"description": "secret work in flight"}],
            "claude_usage": {},
        }

    monkeypatch.setattr(collector, "collect", _collect_then_flip)
    out = tmp_path / "drop.json"
    assert collector.main(["--employee", "emp_alice", "--out", str(out)]) == 0
    delivered = json.loads(out.read_text(encoding="utf-8"))
    assert delivered["opted_out"] is True
    assert delivered["sessions"] == []
    assert "secret work in flight" not in out.read_text(encoding="utf-8")


def test_uploader_midrun_flip_writes_nothing(
    home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Finding 2 (uploader): a mid-run flip stops everything before the clone."""
    dest = tmp_path / "clone"
    (dest / ".git").mkdir(parents=True)

    real_harvest = uploader.harvest

    def _harvest_then_flip(*args: object, **kwargs: object) -> list:
        (home / ".ai-telemetry-off").touch()
        return real_harvest(*args, **kwargs)

    _write_transcript(home)
    monkeypatch.setattr(uploader, "harvest", _harvest_then_flip)
    assert uploader.main(["--employee", "emp_alice", "--dest", str(dest)]) == 0
    assert "mid-run" in capsys.readouterr().out
    assert not (dest / "transcripts").exists()


def test_api_body_preserves_opt_out_marker() -> None:
    """Finding 3: the --post transport must not strip the opt-out fields."""
    from omniagentos.api.routes.team import SessionReportBody

    payload = {
        "schema": 1,
        "employee_id": "emp_alice",
        "host": "laptop",
        "generated_at": "2026-08-14T00:00:00Z",
        "opted_out": True,
        "opted_out_since": "2026-08-14T00:00:00Z",
        "sessions": [],
        "active_count": 0,
        "recent_count": 0,
        "claude_usage": {"accounts": []},
    }
    dumped = SessionReportBody(**payload).model_dump(by_alias=True)
    assert dumped["opted_out"] is True
    assert dumped["opted_out_since"] == "2026-08-14T00:00:00Z"


def test_balance_alerts_opt_out_is_unknown_not_recovery(home: Path) -> None:
    """Finding 4: opted-out zeros must never read as no_claude/ok."""
    import time as _time

    from omniagentos.team import balance_alerts as ba
    from omniagentos.team.session_collector import _iso as sc_iso

    report = {
        "employee_id": "emp_alice",
        "host": "laptop",
        "generated_at": sc_iso(_time.time()),
        "opted_out": True,
        "opted_out_since": "2026-08-14T00:00:00Z",
        "claude_usage": {
            "accounts": [],
            "distinct_accounts": 0,
            "authed_accounts": 0,
            "authed_no_snapshot": 0,
            "best_remaining_percent": None,
            "best_dir": None,
        },
    }
    verdict = ba.assess_machine(report)
    assert verdict.status == "unknown"
    assert "opted out" in verdict.reason
    # A standing breach survives the opt-out: unknown neither pages nor clears.
    key = ba._state_key(verdict)
    state = {key: {"state": "breached", "last_alert_ts": 0.0}}
    alerts, recoveries, next_state = ba.decide_notifications([verdict], state)
    assert alerts == [] and recoveries == []
    assert next_state[key]["state"] == "breached"


def test_uploader_print_text_shows_redacted_body(
    home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Finding 5: the self-audit must show the exact redacted bytes."""
    _write_transcript(home)
    assert uploader.main(["--employee", "emp_alice", "--print", "--print-text"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["would_upload"], "expected the fixture transcript in the plan"
    assert "fix the login bug" in payload["would_upload"][0]["text"]


def test_show_payloads_missing_feed_is_a_failure(
    home: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Finding 5: an audit that cannot show a feed must not exit clean."""
    monkeypatch.setattr(ctl, "_find_feed_script", lambda name: None)
    assert ctl.main(["show-payloads", "--employee", "emp_alice"]) == 1
    assert "not installed" in capsys.readouterr().out


def test_slack_blocks_render_opt_out_as_choice() -> None:
    """Finding 6: the Slack surface (blocks, not fallback text) says why."""
    from omniagentos.team import slack_blocks
    from omniagentos.team.session_tracker import Overall

    reports = {
        "emp_alice": {
            "_age_seconds": 60,
            "opted_out": True,
            "opted_out_since": "2026-08-14T00:00:00Z",
            "host": "alices-laptop",
            "sessions": [],
            "active_count": 0,
            "recent_count": 0,
        }
    }
    _color, blocks = slack_blocks.tracker_blocks(
        Overall(), [("emp_alice", "Alice")], reports, stamp="now", fresh_seconds=4000
    )
    rendered = json.dumps(blocks)
    assert "opted out since 2026-08-14T00:00:00Z" in rendered
    assert "0 active" not in rendered
