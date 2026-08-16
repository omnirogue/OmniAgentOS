"""write_summary (3-line human summary) and append_improvement_log (JSONL,
improver='provider-sentinel') -- the curator handoff step."""

from __future__ import annotations

import json
from pathlib import Path
from types import ModuleType


def test_write_summary_is_exactly_three_lines(sentinel: ModuleType, tmp_path: Path) -> None:
    summary_path = tmp_path / "summary.txt"
    doctor_results = {
        "codex:default": {"ok": True},
        "grok:default": {"ok": False},
    }
    sentinel.write_summary(
        doctor_results=doctor_results,
        usages=[],
        alerts=[],
        ts="2026-07-24T22:30:00Z",
        summary_path=summary_path,
    )
    lines = summary_path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 3
    assert "1/2 ok" in lines[0]
    assert "grok:default" in lines[0]
    assert lines[2] == "Alerts tonight: 0"


def test_write_summary_reports_alert_count_and_issues(sentinel: ModuleType, tmp_path: Path) -> None:
    summary_path = tmp_path / "summary.txt"
    alert = sentinel.Alert(
        provider="grok",
        account_id=None,
        issue="auth_failure",
        title="Provider health: grok auth failure",
        body="...",
        mark_error=True,
    )
    sentinel.write_summary(
        doctor_results={"grok:default": {"ok": False}},
        usages=[],
        alerts=[alert],
        ts="2026-07-24T22:30:00Z",
        summary_path=summary_path,
    )
    lines = summary_path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 3
    assert "Alerts tonight: 1" in lines[2]
    assert "grok:auth_failure" in lines[2]


def test_append_improvement_log_writes_one_valid_jsonl_line(
    sentinel: ModuleType, tmp_path: Path
) -> None:
    log_path = tmp_path / "improvement-log.jsonl"
    sentinel.append_improvement_log(ts="2026-07-24T22:30:00Z", notes="test run", log_path=log_path)

    lines = log_path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    entry = json.loads(lines[0])
    assert entry["improver"] == "provider-sentinel"
    assert entry["ts"] == "2026-07-24T22:30:00Z"
    assert entry["notes"] == "test run"
    assert entry["changes"] == []


def test_append_improvement_log_appends_never_rewrites(
    sentinel: ModuleType, tmp_path: Path
) -> None:
    log_path = tmp_path / "improvement-log.jsonl"
    sentinel.append_improvement_log(ts="2026-07-23T22:30:00Z", notes="night 1", log_path=log_path)
    sentinel.append_improvement_log(ts="2026-07-24T22:30:00Z", notes="night 2", log_path=log_path)

    lines = log_path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    assert json.loads(lines[0])["notes"] == "night 1"
    assert json.loads(lines[1])["notes"] == "night 2"
