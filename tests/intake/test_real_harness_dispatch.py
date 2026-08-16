"""The real-harness opt-in: a filed goal that actually executes -- only when armed.

The keystone gap this covers: intake plans a goal, creates a board card and a
control-plane task+run, and then the queued run is a text-only ``readonly``
generation -- it carries no tools and no scoped working dir, so nothing real ever
happens. ``OMNIAGENTOS_REAL_HARNESS`` is the operator's arming switch for that
last mile.

SAFETY IS THE POINT OF THIS FILE. Autonomous execution spends money, so the
switch is default-OFF and every test here is written to fail loudly if that ever
stops being true:

* flag UNSET -> byte-identical to today: readonly, no tools, no working dir, no
  new response key, and the adapter runs with an empty tool grant.
* flag SET -> the dispatch is upgraded to the tool-carrying ``tools`` posture
  (scoped working dir + ``_INTAKE_TOOLS``), whose single agent step is declared
  ``consequential`` -- so the runner's EXISTING policy gate still parks it for a
  human under SUPERVISED. Nothing here bypasses that gate.
* a run dispatched WHILE ARMED, executed later by a runner that is NOT armed,
  fails closed at the adapter boundary -- disarming stops queued work too.

No real agent, no network, no paid call: the harness boundary is a recording
fake adapter, exactly like ``tests/intake/test_exec.py``.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from omniagentos.collab.store import CollabStore
from omniagentos.contracts import (
    AgentInput,
    AgentResult,
    AgentUsage,
    BudgetDecision,
    HarnessType,
    HealthStatus,
    ResultStatus,
)
from omniagentos.intake.service import _INTAKE_TOOLS, dispatch_spec
from omniagentos.policy import PolicyMode, evaluate_action, load_policy, sandbox_for_tools
from omniagentos.runner.core import Runner, RunnerDependencies

REAL_HARNESS_ENV = "OMNIAGENTOS_REAL_HARNESS"

# A REAL harness name (not ``mock``): arming is deliberately inert on the mock
# harness, because a run recorded as harness='mock' executed nothing -- that is
# the exact defect this lane exists to close. The ADAPTER is still faked below,
# so these tests spend nothing and touch no network.
REAL_HARNESS = HarnessType.CLI_CLAUDE.value


class _RecordingAdapter:
    """Stands in for a live CLI harness; records every invocation, spends nothing."""

    name = "cli-claude"
    version = "1.0"

    def __init__(self) -> None:
        self.calls: list[AgentInput] = []

    def run(self, input: AgentInput) -> AgentResult:
        self.calls.append(input)
        return AgentResult(
            status=ResultStatus.OK,
            output_text="did-work",
            usage=AgentUsage(
                wall_ms=1,
                turns=1,
                input_tokens=1,
                output_tokens=1,
                cost_usd=0.0,
                estimated=True,
                source="estimator",
            ),
        )

    def cancel(self, session_ref: str) -> bool:  # pragma: no cover - unused
        return True

    def health(self) -> HealthStatus:  # pragma: no cover - unused
        return HealthStatus(healthy=True)


def _deps(adapter: _RecordingAdapter, cfg: Any) -> RunnerDependencies:
    return RunnerDependencies(
        evaluate_policy=lambda action: evaluate_action(action, cfg),
        sandbox_for_tools=lambda harness, tools: sandbox_for_tools(harness, tools, cfg),
        check_budget=lambda spec, w, t, c: BudgetDecision(allowed=True),
        resolve_adapter=lambda harness: adapter,
        append_manifest=lambda root, manifest: str(Path(root) / "runs.jsonl"),
        render_run_note=lambda run, steps, manifest_path, receipts, **kw: (
            f"runs/{run['id']}.md",
            "note",
        ),
        write_note=lambda root, relpath, content: str(Path(root) / relpath),
    )


def _make_runner(store: Any, adapter: _RecordingAdapter, cfg: Any, tmp_path: Path) -> Runner:
    return Runner(
        store,
        "w-real-harness",
        dependencies=_deps(adapter, cfg),
        ledger_dir=str(tmp_path / "ledger"),
        vault_dir=str(tmp_path / "vault"),
        workspace_base=str(tmp_path / "ws"),
    )


def _drain(runner: Runner) -> None:
    for _ in range(20):
        if not runner.tick():
            break


def _spec() -> Any:
    from omniagentos.intake.contracts import RefinedSpec

    return RefinedSpec(
        title="Write a hello file",
        description="Create hello.txt with the word hi.",
        acceptance_criteria=["hello.txt exists"],
    )


def _stores(tmp_path: Path, name: str) -> tuple[CollabStore, Any]:
    collab = CollabStore(str(tmp_path / name))
    return collab, collab._store


# --------------------------------------------------------------------------- #
# flag OFF -- today's behaviour, byte for byte
# --------------------------------------------------------------------------- #


def test_flag_unset_keeps_todays_readonly_dispatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(REAL_HARNESS_ENV, raising=False)
    monkeypatch.setenv("OMNIAGENTOS_VAR_DIR", str(tmp_path / "var"))
    collab, store = _stores(tmp_path, "off.db")
    cfg = load_policy()

    result = dispatch_spec(store, collab, cfg, _spec(), harness=HarnessType.MOCK.value)

    # Unchanged posture: readonly, no tool grant, no scoped working dir, and NO
    # new response key (an unarmed dispatch is indistinguishable from today's).
    assert result["execute"] == "readonly"
    assert result["working_dir"] is None
    assert "real_harness" not in result
    task = store.get_task(str(result["task_id"]))
    assert task is not None
    assert "tools_allowed" not in json.loads(task["input_json"])
    run = store.get_run(str(result["run_id"]))
    assert run is not None
    assert json.loads(run["plan_json"] or "[]") == [] or all(
        "real_harness" not in json.dumps(step) for step in json.loads(run["plan_json"] or "[]")
    )

    adapter = _RecordingAdapter()
    _drain(_make_runner(store, adapter, cfg, tmp_path))

    run = store.get_run(str(result["run_id"]))
    assert run is not None
    assert run["state"] == "completed"
    assert len(adapter.calls) == 1
    assert adapter.calls[0].tools_allowed == []
    assert adapter.calls[0].metadata["sandbox"]["level"] == "read_only"


@pytest.mark.parametrize("value", ["", "0", "false", "no", "off"])
def test_falsey_flag_values_stay_off(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    """Only an explicit affirmative arms it -- an empty/0/false env is still OFF."""
    monkeypatch.setenv(REAL_HARNESS_ENV, value)
    monkeypatch.setenv("OMNIAGENTOS_VAR_DIR", str(tmp_path / "var"))
    collab, store = _stores(tmp_path, f"off-{value or 'empty'}.db")
    cfg = load_policy()

    result = dispatch_spec(store, collab, cfg, _spec(), harness=HarnessType.MOCK.value)

    assert result["execute"] == "readonly"
    assert result["working_dir"] is None
    assert "real_harness" not in result


def test_flag_unset_never_upgrades_an_explicit_posture(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An explicit ``tools`` dispatch is untouched by the switch (both ways)."""
    monkeypatch.delenv(REAL_HARNESS_ENV, raising=False)
    monkeypatch.setenv("OMNIAGENTOS_VAR_DIR", str(tmp_path / "var"))
    collab, store = _stores(tmp_path, "explicit.db")
    cfg = load_policy()

    result = dispatch_spec(
        store, collab, cfg, _spec(), harness=HarnessType.MOCK.value, execute="tools"
    )

    assert result["execute"] == "tools"
    assert "real_harness" not in result
    run = store.get_run(str(result["run_id"]))
    assert run is not None
    plan = json.loads(run["plan_json"] or "[]")
    assert plan and "real_harness" not in plan[0]["params"]


# --------------------------------------------------------------------------- #
# flag ON -- the goal actually reaches a real harness
# --------------------------------------------------------------------------- #


def test_flag_on_dispatches_to_the_real_harness(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(REAL_HARNESS_ENV, "1")
    monkeypatch.setenv("OMNIAGENTOS_VAR_DIR", str(tmp_path / "var"))
    collab, store = _stores(tmp_path, "on.db")
    cfg = load_policy()

    result = dispatch_spec(store, collab, cfg, _spec(), harness=REAL_HARNESS)
    task_id = str(result["task_id"])

    # Upgraded to the tool-carrying posture, and it says so.
    assert result["execute"] == "tools"
    assert result["real_harness"] is True
    working_dir = result["working_dir"]
    assert working_dir is not None
    assert Path(working_dir) == tmp_path / "var" / "intake-workspace" / task_id
    assert Path(working_dir).is_dir()

    task = store.get_task(task_id)
    assert task is not None
    assert json.loads(task["input_json"])["tools_allowed"] == _INTAKE_TOOLS

    run = store.get_run(str(result["run_id"]))
    assert run is not None
    plan = json.loads(run["plan_json"] or "[]")
    assert len(plan) == 1
    # Still declared consequential: the policy gate is NOT bypassed by arming.
    assert plan[0]["action_class"] == "consequential"
    # The run carries the arming marker, so the runner can re-check it at execution.
    assert plan[0]["params"]["real_harness"] is True

    adapter = _RecordingAdapter()
    _drain(_make_runner(store, adapter, cfg, tmp_path))

    # The real harness boundary was reached, with the tool grant and the scoped dir.
    assert len(adapter.calls) == 1
    assert adapter.calls[0].tools_allowed == _INTAKE_TOOLS
    assert adapter.calls[0].working_dir == working_dir
    run = store.get_run(str(result["run_id"]))
    assert run is not None
    assert run["state"] == "completed"


def test_flag_on_still_parks_for_approval_under_supervised(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Arming does not weaken the human gate: SUPERVISED still parks before work."""
    monkeypatch.setenv(REAL_HARNESS_ENV, "1")
    monkeypatch.setenv("OMNIAGENTOS_VAR_DIR", str(tmp_path / "var"))
    collab, store = _stores(tmp_path, "supervised.db")
    cfg = load_policy().model_copy(update={"mode": PolicyMode.SUPERVISED})

    result = dispatch_spec(store, collab, cfg, _spec(), harness=REAL_HARNESS)

    adapter = _RecordingAdapter()
    _drain(_make_runner(store, adapter, cfg, tmp_path))

    run = store.get_run(str(result["run_id"]))
    assert run is not None
    assert run["state"] == "awaiting_approval"
    assert adapter.calls == []
    pending = [a for a in store.list_approvals(state="pending") if a["run_id"] == run["id"]]
    assert len(pending) == 1
    assert pending[0]["action_class"] == "consequential"


# --------------------------------------------------------------------------- #
# arming is never a licence to dress a mock run up as real execution
# --------------------------------------------------------------------------- #


def test_armed_dispatch_on_the_mock_harness_is_blocked_and_says_so(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The headline defect: runs recorded as harness='mock' that executed nothing.

    Arming must not upgrade a mock dispatch -- that would queue one more mock
    run while the response claimed real execution. The upgrade is withheld AND
    the response says it was withheld, so "armed but nothing happens" is
    mechanically observable instead of silent.
    """
    monkeypatch.setenv(REAL_HARNESS_ENV, "1")
    monkeypatch.setenv("OMNIAGENTOS_VAR_DIR", str(tmp_path / "var"))
    collab, store = _stores(tmp_path, "armed-mock.db")
    cfg = load_policy()

    result = dispatch_spec(store, collab, cfg, _spec(), harness=HarnessType.MOCK.value)

    assert result["execute"] == "readonly"
    assert result["working_dir"] is None
    assert result["real_harness"] is False
    assert result["real_harness_blocked"] == "harness=mock"

    run = store.get_run(str(result["run_id"]))
    assert run is not None
    assert run["harness"] == HarnessType.MOCK.value
    plan = json.loads(run["plan_json"] or "[]")
    # No arming marker reached the queue, so the runner cannot execute it as real.
    assert all("real_harness" not in json.dumps(step) for step in plan)


def test_armed_dispatch_is_blocked_when_the_mock_harness_comes_from_the_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Same block via the DEFAULT carrier: ``OMNIAGENTOS_INTAKE_HARNESS=mock``.

    This is the production shape of the defect -- no caller ever typed "mock";
    the deployment's env default did. ``DEFAULT_INTAKE_HARNESS`` is read at
    import, so it is patched on the module here rather than via the env.
    """
    from omniagentos.intake import service as intake_service

    monkeypatch.setenv(REAL_HARNESS_ENV, "1")
    monkeypatch.setenv("OMNIAGENTOS_VAR_DIR", str(tmp_path / "var"))
    monkeypatch.setattr(intake_service, "DEFAULT_INTAKE_HARNESS", HarnessType.MOCK.value)
    collab, store = _stores(tmp_path, "armed-env-mock.db")
    cfg = load_policy()

    result = dispatch_spec(store, collab, cfg, _spec())  # no explicit harness

    assert result["execute"] == "readonly"
    assert result["real_harness"] is False
    assert result["real_harness_blocked"] == "harness=mock"

    adapter = _RecordingAdapter()
    _drain(_make_runner(store, adapter, cfg, tmp_path))

    # It still runs as today's harmless text-only run -- blocking the upgrade
    # must not break the existing dispatch.
    run = store.get_run(str(result["run_id"]))
    assert run is not None
    assert run["state"] == "completed"
    assert len(adapter.calls) == 1
    assert adapter.calls[0].tools_allowed == []


# --------------------------------------------------------------------------- #
# disarming stops queued work too (fail-closed at the runner)
# --------------------------------------------------------------------------- #


def test_runner_refuses_an_armed_run_when_disarmed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(REAL_HARNESS_ENV, "1")
    monkeypatch.setenv("OMNIAGENTOS_VAR_DIR", str(tmp_path / "var"))
    collab, store = _stores(tmp_path, "disarm.db")
    cfg = load_policy()

    result = dispatch_spec(store, collab, cfg, _spec(), harness=REAL_HARNESS)
    assert result["real_harness"] is True

    # The operator turns it back off before the runner gets to the queued run.
    monkeypatch.delenv(REAL_HARNESS_ENV, raising=False)

    adapter = _RecordingAdapter()
    _drain(_make_runner(store, adapter, cfg, tmp_path))

    run = store.get_run(str(result["run_id"]))
    assert run is not None
    assert run["state"] == "failed"
    assert adapter.calls == []  # nothing was spent
    assert "OMNIAGENTOS_REAL_HARNESS" in str(run["error"])


# --------------------------------------------------------------------------- #
# the operator's concurrency cap
# --------------------------------------------------------------------------- #


def test_concurrency_is_capped_only_when_armed(monkeypatch: pytest.MonkeyPatch) -> None:
    from omniagentos.runner.__main__ import (
        DEFAULT_REAL_HARNESS_MAX_CONCURRENCY,
        resolve_concurrency,
    )

    assert DEFAULT_REAL_HARNESS_MAX_CONCURRENCY == 3

    # Disarmed: whatever the operator asked for, untouched.
    monkeypatch.delenv(REAL_HARNESS_ENV, raising=False)
    monkeypatch.delenv("OMNIAGENTOS_REAL_HARNESS_MAX_CONCURRENCY", raising=False)
    assert resolve_concurrency(1) == 1
    assert resolve_concurrency(8) == 8

    # Armed: clamped to the operator's 3-concurrent-worker cap, never raised.
    monkeypatch.setenv(REAL_HARNESS_ENV, "1")
    assert resolve_concurrency(8) == 3
    assert resolve_concurrency(2) == 2
    assert resolve_concurrency(0) == 1

    # The cap itself is configurable, and a garbage value falls back to 3.
    monkeypatch.setenv("OMNIAGENTOS_REAL_HARNESS_MAX_CONCURRENCY", "2")
    assert resolve_concurrency(8) == 2
    monkeypatch.setenv("OMNIAGENTOS_REAL_HARNESS_MAX_CONCURRENCY", "banana")
    assert resolve_concurrency(8) == 3
