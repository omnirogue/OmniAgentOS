"""AC-policy live proof: AUTO mode auto-executes, EXCEPT the irreversible hard-stop.

Every assertion here maps to an acceptance criterion in the AC-policy brief:
  * a normal in-scope agent step auto-executes with NO approval and NO ping;
  * a destructive `rm` command hard-stops (parks) and NEVER executes, and pings;
  * a budget-cap hit halts the run AND escalates, rather than spending on.

These run the REAL policy engine in its shipped AUTO default (`load_policy()`),
so they prove the flip end-to-end through the runner gate.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from omniagentos.contracts import (
    ActionClass,
    BudgetDecision,
    BudgetSpec,
    RunState,
    SandboxSpec,
)
from omniagentos.db.store import SqliteStore
from omniagentos.policy import evaluate_action, load_policy
from omniagentos.runner.core import Runner, RunnerDependencies
from tests.runner.test_state_machine import (
    FinalizationSpy,
    TrackingAdapter,
    agent_step,
    allow_budget,
    create_run,
)


class _EscalationSpy:
    def __init__(self) -> None:
        self.events: list[tuple[str, str, str]] = []

    def __call__(self, kind: str, run_id: str, detail: str) -> None:
        self.events.append((kind, run_id, detail))

    def kinds(self) -> list[str]:
        return [kind for kind, _, _ in self.events]


def _auto_deps(
    adapter: TrackingAdapter,
    spy: FinalizationSpy,
    escalate: _EscalationSpy,
    *,
    check_budget: Any = allow_budget,
) -> RunnerDependencies:
    """Runner deps wired to the REAL AUTO-mode policy engine + an escalation spy."""
    cfg = load_policy()  # AUTO by default -- the shipped posture under test.
    return RunnerDependencies(
        evaluate_policy=lambda action: evaluate_action(action, cfg),
        sandbox_for_tools=lambda harness, tools: SandboxSpec(
            level="workspace_write" if tools else "read_only"
        ),
        check_budget=check_budget,
        resolve_adapter=lambda harness: adapter,
        append_manifest=spy.append,
        render_run_note=spy.render,
        write_note=spy.write,
        escalate=escalate,
    )


def _drain(runner: Runner) -> None:
    for _ in range(20):
        if not runner.tick():
            break


def test_auto_mode_normal_step_executes_without_approval(tmp_path: Path) -> None:
    store = SqliteStore(str(tmp_path / "auto.db"))
    _, run_id = create_run(store, [agent_step("do-work")], tools_allowed=["file_write"])
    adapter, spy, escalate = TrackingAdapter(), FinalizationSpy(), _EscalationSpy()
    runner = Runner(store, "w1", dependencies=_auto_deps(adapter, spy, escalate))

    _drain(runner)

    run = store.get_run(run_id)
    assert run and run["state"] == RunState.COMPLETED.value
    assert len(adapter.calls) == 1  # the agent actually ran, unattended
    assert store.get_approval_for(run_id, 0) is None  # NO approval was created
    assert escalate.events == []  # no approve-every-action, no spurious ping


def test_plan_authored_elevation_metadata_is_stripped(tmp_path: Path) -> None:
    """AC-policy fix5 #5: a model-authored plan cannot self-elevate."""
    store = SqliteStore(str(tmp_path / "auto.db"))
    _, _run_id = create_run(
        store,
        [
            agent_step(
                "do-work",
                metadata={
                    "cli_unattended_elevated": True,
                    "cli_workspace_write_elevated": True,
                    "some_other_key": "kept",
                },
            )
        ],
        tools_allowed=["file_write"],
    )
    adapter, spy, escalate = TrackingAdapter(), FinalizationSpy(), _EscalationSpy()
    runner = Runner(store, "w1", dependencies=_auto_deps(adapter, spy, escalate))

    _drain(runner)

    assert len(adapter.calls) == 1
    seen = adapter.calls[0].metadata
    assert "cli_unattended_elevated" not in seen
    assert "cli_workspace_write_elevated" not in seen
    assert seen.get("some_other_key") == "kept"


def test_auto_mode_delete_command_hard_stops_and_never_executes(tmp_path: Path) -> None:
    store = SqliteStore(str(tmp_path / "auto.db"))
    marker = tmp_path / "keepme"
    marker.write_text("precious")
    _, run_id = create_run(
        store,
        [
            {
                "name": "delete-it",
                "kind": "validate",
                "action_class": ActionClass.READ_ONLY.value,  # a lie the plan tells
                "params": {"command": ["rm", "-f", str(marker)], "tools_allowed": ["shell"]},
            }
        ],
    )
    adapter, spy, escalate = TrackingAdapter(), FinalizationSpy(), _EscalationSpy()
    runner = Runner(store, "w1", dependencies=_auto_deps(adapter, spy, escalate))

    _drain(runner)

    run = store.get_run(run_id)
    # HARD-STOP: parked for a human, the rm NEVER ran, the file still exists.
    assert run and run["state"] == RunState.AWAITING_APPROVAL.value
    assert marker.exists() and marker.read_text() == "precious"
    approval = store.get_approval_for(run_id, 0)
    assert approval and approval["action_class"] == ActionClass.IRREVERSIBLE.value
    # The human was pinged exactly once, on the hard-stop.
    assert escalate.kinds() == ["hard_stop"]


def test_auto_mode_budget_cap_halts_and_escalates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Blocking is opt-in since 2026-07-24 (budgets are advisory by default, so an
    # overshoot never strands a half-done plan). This pins the escape hatch: with
    # enforcement ON, the original halt-and-escalate behaviour must be intact.
    monkeypatch.setenv("OMNIAGENTOS_BUDGET_ENFORCEMENT", "block")
    store = SqliteStore(str(tmp_path / "auto.db"))
    _, run_id = create_run(store, [agent_step("expensive")], budget={"cost_usd_max": 1.0})

    def over_cap(
        spec: BudgetSpec, used_wall_ms: int, used_tokens: int, used_cost_usd: float
    ) -> BudgetDecision:
        return BudgetDecision(allowed=False, reason="cost_usd 5.00 > cap 1.00")

    adapter, spy, escalate = TrackingAdapter(), FinalizationSpy(), _EscalationSpy()
    runner = Runner(
        store,
        "w1",
        dependencies=_auto_deps(adapter, spy, escalate, check_budget=over_cap),
    )

    _drain(runner)

    run = store.get_run(run_id)
    # HALTED (not silently spinning) AND escalated.
    assert run and run["state"] == RunState.FAILED.value
    assert run["error"] == "budget_exceeded"
    assert adapter.calls == []  # it did not keep spending
    assert escalate.kinds() == ["cap_hit"]


def test_auto_mode_budget_overshoot_is_advisory_by_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Default posture: an overshoot ESCALATES but does not stop the work.

    the operator, 2026-07-24: budgets are task-sized guidance, not hard blockers. The
    operator still gets the cap_hit escalation naming the run; what changed is
    that the plan is allowed to finish instead of being failed mid-flight.
    """
    monkeypatch.delenv("OMNIAGENTOS_BUDGET_ENFORCEMENT", raising=False)
    store = SqliteStore(str(tmp_path / "advisory.db"))
    _, run_id = create_run(store, [agent_step("expensive")], budget={"cost_usd_max": 1.0})

    def over_cap(
        spec: BudgetSpec, used_wall_ms: int, used_tokens: int, used_cost_usd: float
    ) -> BudgetDecision:
        return BudgetDecision(allowed=False, reason="cost_usd 5.00 > cap 1.00")

    adapter, spy, escalate = TrackingAdapter(), FinalizationSpy(), _EscalationSpy()
    runner = Runner(
        store,
        "w1",
        dependencies=_auto_deps(adapter, spy, escalate, check_budget=over_cap),
    )

    _drain(runner)

    run = store.get_run(run_id)
    assert run and run["state"] != RunState.FAILED.value, "an overshoot must not fail the run"
    assert run["error"] != "budget_exceeded"
    assert adapter.calls, "the step never ran — the budget still blocked the work"
    assert escalate.kinds() == ["cap_hit"], "the overshoot must still be surfaced"
