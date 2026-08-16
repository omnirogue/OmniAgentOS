"""Unit tests for the Fable approval gate over low-risk reflection proposals."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from omniagentos.contracts import utc_now_iso
from omniagentos.db.store import SqliteStore
from omniagentos.reflection.fable_gate import (
    VERDICT_SCHEMA,
    resolve_mode,
    run_gate,
)


def _insert_proposal(
    db_path: str,
    proposal_id: str,
    *,
    kind: str = "model_config",
    target: dict[str, Any] | str | None = None,
    risk_class: str = "low",
    status: str = "pending",
    proposed: str = "true",
) -> None:
    if target is None:
        target = {"file": "configs/modelintel.yaml", "key": "models.gemini.available"}
    store = SqliteStore(db_path)
    now = utc_now_iso()
    with store._lock:
        store._connection.execute(
            """
            INSERT INTO reflection_proposals (
                id, kind, target, current, proposed, rationale, evidence_refs_json,
                predicted_impact, risk_class, status, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                proposal_id,
                kind,
                json.dumps(target) if isinstance(target, dict) else target,
                "false",
                proposed,
                "test rationale",
                "[]",
                "test impact",
                risk_class,
                status,
                now,
                now,
            ),
        )


def _status_of(db_path: str, proposal_id: str) -> str:
    store = SqliteStore(db_path)
    with store._lock:
        row = store._connection.execute(
            "SELECT status FROM reflection_proposals WHERE id = ?", (proposal_id,)
        ).fetchone()
    return str(row["status"])


class _FakeRunner:
    """Records the review call and returns a canned verdict payload."""

    def __init__(self, payload: dict[str, Any] | Exception) -> None:
        self.payload = payload
        self.calls: list[dict[str, Any]] = []

    def __call__(
        self,
        stage: Any,
        prompt: str,
        schema: dict[str, Any],
        working_dir: Path,
        wall_ms: int,
    ) -> dict[str, Any]:
        self.calls.append({"stage": stage, "prompt": prompt, "schema": schema, "wall_ms": wall_ms})
        if isinstance(self.payload, Exception):
            raise self.payload
        return self.payload


@pytest.fixture
def gate_env(tmp_path, monkeypatch):
    """Hermetic repo root + db + artifact root for gate runs."""
    db_path = str(tmp_path / "state.sqlite3")
    SqliteStore(db_path)  # trigger migrations
    monkeypatch.setenv("OMNIAGENTOS_HOME", str(tmp_path))
    monkeypatch.delenv("OMNIAGENTOS_FABLE_GATE_MODE", raising=False)
    configs_dir = tmp_path / "configs"
    configs_dir.mkdir()
    (configs_dir / "modelintel.yaml").write_text(
        "models:\n  gemini:\n    available: false\n", encoding="utf-8"
    )
    return {"db": db_path, "root": tmp_path, "artifacts": tmp_path / "gate-artifacts"}


def test_resolve_mode_fail_closed(monkeypatch):
    monkeypatch.delenv("OMNIAGENTOS_FABLE_GATE_MODE", raising=False)
    assert resolve_mode() == "shadow"  # documented default
    monkeypatch.setenv("OMNIAGENTOS_FABLE_GATE_MODE", "on")
    assert resolve_mode() == "on"
    assert resolve_mode("shadow") == "shadow"  # explicit beats env
    monkeypatch.setenv("OMNIAGENTOS_FABLE_GATE_MODE", "banana")
    assert resolve_mode() == "off"  # invalid env disables the gate


def test_mode_off_touches_nothing(gate_env):
    _insert_proposal(gate_env["db"], "p1")
    runner = _FakeRunner({"verdicts": [{"id": "p1", "verdict": "approve", "reason": "ok"}]})
    result = run_gate(
        gate_env["db"], mode="off", artifact_root=gate_env["artifacts"], json_runner=runner
    )
    assert result.mode == "off"
    assert runner.calls == []
    assert _status_of(gate_env["db"], "p1") == "pending"


def test_shadow_records_verdicts_but_changes_no_rows(gate_env):
    _insert_proposal(gate_env["db"], "p1")
    runner = _FakeRunner({"verdicts": [{"id": "p1", "verdict": "approve", "reason": "ok"}]})
    result = run_gate(
        gate_env["db"], mode="shadow", artifact_root=gate_env["artifacts"], json_runner=runner
    )
    assert len(runner.calls) == 1
    assert runner.calls[0]["schema"] == VERDICT_SCHEMA
    assert result.verdicts["p1"]["verdict"] == "approve"
    assert result.applied == []
    assert result.needs_human == ["p1"]  # shadow never applies
    assert _status_of(gate_env["db"], "p1") == "pending"
    # Verdict artifact + improvement-log entry exist.
    artifact = Path(result.artifact_dir) / "verdicts.json"
    assert artifact.is_file()
    log_lines = (
        (gate_env["root"] / "var" / "improvement-log.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    )
    assert json.loads(log_lines[-1])["improver"] == "fable-gate"


def test_on_mode_applies_approvals_and_rejects_rejections(gate_env):
    _insert_proposal(gate_env["db"], "p_ok")
    _insert_proposal(gate_env["db"], "p_bad")
    runner = _FakeRunner(
        {
            "verdicts": [
                {"id": "p_ok", "verdict": "approve", "reason": "supported"},
                {"id": "p_bad", "verdict": "reject", "reason": "not supported"},
            ]
        }
    )
    result = run_gate(
        gate_env["db"], mode="on", artifact_root=gate_env["artifacts"], json_runner=runner
    )
    assert result.applied == ["p_ok"]
    assert result.rejected == ["p_bad"]
    assert _status_of(gate_env["db"], "p_ok") == "promoted"
    assert _status_of(gate_env["db"], "p_bad") == "rejected"
    # The approved single-key edit actually landed in the hermetic repo root.
    import yaml

    data = yaml.safe_load(
        (gate_env["root"] / "configs" / "modelintel.yaml").read_text(encoding="utf-8")
    )
    assert data["models"]["gemini"]["available"] is True


@pytest.mark.parametrize(
    ("kind", "target", "risk"),
    [
        ("formation", {"file": "configs/swarm.yaml", "key": "router.x"}, "low"),
        ("model_config", {"file": "configs/policy.yaml", "key": "mode"}, "low"),
        ("model_config", {"file": "configs/loop_models.yaml", "key": "primary.model"}, "low"),
        ("model_config", {"file": "docs/notes.yaml", "key": "a"}, "low"),
        ("model_config", {"file": "../outside.yaml", "key": "a"}, "low"),
        ("effort_override", {"file": "configs/swarm.yaml", "key": "router.concurrency.max"}, "low"),
        ("model_config", {"file": "configs/modelintel.yaml", "key": "a"}, "medium"),
        ("brief_template", None, "low"),  # no doc -> AGENTS.md default (protected)
        ("lesson", {"doc": "AGENTS.md"}, "low"),
    ],
)
def test_ineligible_rows_never_reach_the_reviewer(gate_env, kind, target, risk):
    if kind == "brief_template" and target is None:
        _insert_proposal(gate_env["db"], "p_x", kind=kind, target="", risk_class=risk)
    else:
        _insert_proposal(gate_env["db"], "p_x", kind=kind, target=target, risk_class=risk)
    runner = _FakeRunner({"verdicts": []})
    result = run_gate(
        gate_env["db"], mode="on", artifact_root=gate_env["artifacts"], json_runner=runner
    )
    assert runner.calls == []  # nothing eligible -> reviewer never invoked
    assert "p_x" in result.ineligible
    assert _status_of(gate_env["db"], "p_x") == "pending"


def test_eligible_lesson_target_passes_filter(gate_env):
    _insert_proposal(
        gate_env["db"],
        "p_lesson",
        kind="lesson",
        target={"doc": "docs/lessons/2026-07-29-reflection.md"},
        proposed="- 2026-07-29: verify before trusting green suites.",
    )
    runner = _FakeRunner(
        {"verdicts": [{"id": "p_lesson", "verdict": "approve", "reason": "additive"}]}
    )
    result = run_gate(
        gate_env["db"], mode="on", artifact_root=gate_env["artifacts"], json_runner=runner
    )
    assert result.applied == ["p_lesson"]
    lesson = gate_env["root"] / "docs" / "lessons" / "2026-07-29-reflection.md"
    assert lesson.is_file()


def test_reviewer_failure_degrades_to_needs_human(gate_env):
    _insert_proposal(gate_env["db"], "p1")
    runner = _FakeRunner(RuntimeError("adapter down"))
    result = run_gate(
        gate_env["db"], mode="on", artifact_root=gate_env["artifacts"], json_runner=runner
    )
    assert result.applied == []
    assert result.needs_human == ["p1"]
    assert any("reviewer stage failed" in err for err in result.errors)
    assert _status_of(gate_env["db"], "p1") == "pending"


def test_unknown_ids_and_invalid_verdicts_are_ignored(gate_env):
    _insert_proposal(gate_env["db"], "p1")
    runner = _FakeRunner(
        {
            "verdicts": [
                {"id": "p_unsubmitted", "verdict": "approve", "reason": "smuggled"},
                {"id": "p1", "verdict": "ship-it", "reason": "not a verdict"},
            ]
        }
    )
    result = run_gate(
        gate_env["db"], mode="on", artifact_root=gate_env["artifacts"], json_runner=runner
    )
    assert result.applied == []
    assert result.verdicts["p1"]["verdict"] == "needs_human"
    assert "p_unsubmitted" not in result.verdicts
    assert _status_of(gate_env["db"], "p1") == "pending"
