"""Mechanical tests for the prompt-ab runner, without invoking model CLIs."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path


def _runner():
    path = Path(__file__).parents[2] / "scripts" / "prompt-ab" / "run_ab.py"
    spec = importlib.util.spec_from_file_location("northstar_run_ab", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_grade_enforces_json_schema_and_enum() -> None:
    runner = _runner()
    criteria = [
        {"type": "json_valid_keys", "keys": ["verdict", "summary"]},
        {"type": "enum_field", "field": "verdict", "allowed": ["CONFIRMED", "REFUTED"]},
    ]
    assert runner.grade('{"verdict":"CONFIRMED","summary":"ok"}', criteria) == []
    assert runner.grade('{"verdict":"pass","summary":"ok"}', criteria)


def test_grade_supports_required_and_forbidden_patterns() -> None:
    runner = _runner()
    assert runner.grade("STOP: blocked", [{"type": "regex_must", "pattern": "STOP"}]) == []
    assert runner.grade("sudo rm", [{"type": "regex_forbid", "pattern": "sudo"}])


def test_extract_json_uses_last_valid_object() -> None:
    runner = _runner()
    assert runner.extract_json("noise {bad} then {\"ok\": true}") == {"ok": True}


def test_main_records_strict_beat_and_signed_arm_digests(tmp_path, monkeypatch, capsys) -> None:
    runner = _runner()
    scenario_dir = tmp_path / "scenarios"
    scenario_dir.mkdir()
    (scenario_dir / "one.json").write_text(json.dumps({
        "id": "nscert-ab",
        "runner": "codex",
        "model": "stub",
        "effort": "low",
        "role_id": "test.role",
        "failure_ref": "fixture-failure-1",
        "arms": {"control": "return no", "candidate": "return {\"ok\": true}"},
        "input": "test",
        "grading": [{"type": "json_valid_keys", "keys": ["ok"]}],
        "trials": 1,
    }))
    run_root = tmp_path / "runs"
    monkeypatch.setattr(runner, "SCEN_DIR", scenario_dir)
    monkeypatch.setattr(runner, "VAR", run_root)
    monkeypatch.setattr(
        runner,
        "call_codex",
        lambda _model, system, _task, _effort: '{"ok":true}' if "ok" in system else "no",
    )
    monkeypatch.setattr(sys, "argv", ["run_ab.py"])
    assert runner.main() == 0
    assert "PROMOTE" in capsys.readouterr().out
    ledgers = list(run_root.glob("ledger-*.jsonl"))
    assert len(ledgers) == 1
    ledger = json.loads(ledgers[0].read_text(encoding="utf-8").splitlines()[0])
    assert ledger["failure_ref"] == "fixture-failure-1"
    assert len(ledger["control_digest"]) == len(ledger["candidate_digest"]) == 16
