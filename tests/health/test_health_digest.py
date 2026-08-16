from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from omniagentos.contracts import (
    ActionClass,
    AgentInput,
    BudgetDecision,
    HarnessType,
    PolicyDecision,
    RunState,
    SandboxSpec,
    TaskState,
    new_id,
    utc_now_iso,
)
from omniagentos.db.migrate import migrate
from omniagentos.db.store import SqliteStore
from omniagentos.health.digest import build_health_digest, read_health_snapshot
from omniagentos.mock_adapter import MockAdapter
from omniagentos.runner.core import Runner, RunnerDependencies
from omniagentos.swarm.scheduler import build_worker_brief

NOW = datetime(2026, 8, 3, 12, 0, tzinfo=UTC)


def _snapshot(ts: datetime = NOW, checks: list[dict[str, str]] | None = None) -> dict[str, Any]:
    return {
        "ts": ts.isoformat().replace("+00:00", "Z"),
        "checks": checks if checks is not None else [{"name": "api", "status": "ok"}],
    }


def _available_snapshot(checks: list[dict[str, str]] | None = None) -> dict[str, Any]:
    snapshot = _snapshot(checks=checks)
    return {"available": True, "checks": snapshot["checks"], "ts": snapshot["ts"]}


def _write_snapshot(path: Path, data: dict[str, Any]) -> None:
    path.write_text(json.dumps(data), encoding="utf-8")


def test_missing_snapshot_is_unavailable_and_omitted(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    snapshot = read_health_snapshot(tmp_path / "missing.json", now=NOW)

    assert snapshot["available"] is False
    assert build_health_digest(snapshot) == ""
    assert "Health digest omitted" in caplog.text


def test_stale_snapshot_is_unavailable_and_omitted(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    path = tmp_path / "latest.json"
    _write_snapshot(path, _snapshot(NOW - timedelta(seconds=3601)))

    snapshot = read_health_snapshot(path, now=NOW)

    assert snapshot["available"] is False
    assert build_health_digest(snapshot) == ""
    assert "Health digest omitted" in caplog.text


def test_future_snapshot_is_unavailable_and_omitted(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    path = tmp_path / "latest.json"
    _write_snapshot(path, _snapshot(NOW + timedelta(seconds=61)))

    snapshot = read_health_snapshot(path, now=NOW)

    assert snapshot["available"] is False
    assert build_health_digest(snapshot) == ""
    assert "Health digest omitted" in caplog.text


def test_fresh_degraded_snapshot_renders_subsystems(tmp_path: Path) -> None:
    path = tmp_path / "latest.json"
    _write_snapshot(
        path,
        _snapshot(NOW - timedelta(seconds=45), [{"name": "api", "status": "fail"}]),
    )

    snapshot = read_health_snapshot(path, now=NOW)
    digest = build_health_digest(snapshot)

    assert snapshot["available"] is True
    assert "## System Health Alert" in digest
    assert "- api: FAIL" in digest
    assert snapshot["ts"] in digest


def test_fresh_healthy_snapshot_has_no_digest(tmp_path: Path) -> None:
    path = tmp_path / "latest.json"
    _write_snapshot(path, _snapshot())

    snapshot = read_health_snapshot(path, now=NOW)

    assert snapshot["available"] is True
    assert build_health_digest(snapshot) == ""


def _worker_brief() -> str:
    return build_worker_brief(
        {},
        {"title": "health test", "description": "exercise prompt context"},
        {"owned_paths": ["omniagentos/health/"], "acceptance": "ok", "verify_command": "true"},
        {},
    )


def test_worker_brief_includes_health_alert(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "omniagentos.health.digest.read_health_snapshot",
        lambda: _available_snapshot([{"name": "runner", "status": "warn"}]),
    )

    brief = _worker_brief()

    assert "## System Health Alert" in brief
    assert brief.index("## Owned paths") < brief.index("## System Health Alert") < brief.index(
        "## Hard rules"
    )


def test_worker_brief_omits_healthy_health_section(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("omniagentos.health.digest.read_health_snapshot", _available_snapshot)

    assert "## System Health Alert" not in _worker_brief()


class _PromptCapturingAdapter(MockAdapter):
    def __init__(self) -> None:
        self.calls: list[AgentInput] = []

    def run(self, input: AgentInput) -> Any:
        self.calls.append(input)
        return super().run(input)


def _runner_prompt(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> str:
    monkeypatch.setenv("OMNIAGENTOS_MEMORY", "0")
    monkeypatch.setenv("OMNIAGENTOS_WORKSPACE_DIR", str(tmp_path / "workspaces"))
    store = SqliteStore(str(tmp_path / "runner.db"))
    migrate(str(tmp_path / "runner.db"))
    adapter = _PromptCapturingAdapter()
    dependencies = RunnerDependencies(
        evaluate_policy=lambda _action: PolicyDecision(requires_approval=False),
        sandbox_for_tools=lambda _harness, _tools: SandboxSpec(level="read_only"),
        check_budget=lambda *_args: BudgetDecision(allowed=True),
        resolve_adapter=lambda _harness: adapter,
        append_manifest=lambda root, _manifest: str(Path(root) / "runs.jsonl"),
        render_run_note=lambda run, _steps, _manifest, _receipts, **_kwargs: (
            f"runs/{run['id']}.md",
            "done",
        ),
        write_note=lambda root, relpath, _content: str(Path(root) / relpath),
    )
    now = utc_now_iso()
    task_id, run_id = new_id("tsk"), new_id("run")
    store.create_task(
        {
            "id": task_id,
            "discipline_id": "code-changes",
            "title": "runner health test",
            "input_json": json.dumps({"prompt": "final task prompt", "tools_allowed": []}),
            "acceptance_json": "{}",
            "state": TaskState.QUEUED.value,
            "risk": "low",
            "created_at": now,
            "updated_at": now,
        }
    )
    store.enqueue_run(
        {
            "id": run_id,
            "task_id": task_id,
            "discipline_id": "code-changes",
            "harness": HarnessType.MOCK.value,
            "state": RunState.QUEUED.value,
            "plan_json": json.dumps(
                [
                    {
                        "name": "agent",
                        "kind": "agent",
                        "action_class": ActionClass.SANDBOXED_CREATION.value,
                        "params": {"adapter": "mock"},
                    }
                ]
            ),
            "budget_json": "{}",
            "trace_id": f"trace-{run_id}",
            "queued_at": now,
            "created_at": now,
            "updated_at": now,
        }
    )
    try:
        assert Runner(store, "health-test-worker", dependencies=dependencies).tick()
        return adapter.calls[0].prompt
    finally:
        store.close()


def test_runner_prompt_includes_health_alert(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "omniagentos.health.digest.read_health_snapshot",
        lambda: _available_snapshot([{"name": "api", "status": "fail"}]),
    )

    prompt = _runner_prompt(tmp_path, monkeypatch)

    assert "## System Health Alert" in prompt
    assert prompt.index("## System Health Alert") < prompt.index("final task prompt")


def test_runner_prompt_omits_healthy_health_section(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("omniagentos.health.digest.read_health_snapshot", _available_snapshot)

    assert "## System Health Alert" not in _runner_prompt(tmp_path, monkeypatch)
