"""Central plan-safety decision unit tests (P1-SAFETY).

Binds the deterministic fail-closed authority in
``omniagentos.swarm.plan_safety``: root-wide ownership, protected paths,
empty mutating ownership, multi-bundle interim refusal, and planner failure
markers. Negative mutation ``dot-scope-plan-runnable`` is proven by temporarily
removing the root-wide gate and confirming this suite goes red.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from omniagentos.swarm.contracts import SwarmPlan, SwarmTaskSpec
from omniagentos.swarm.plan_safety import (
    PlanSafetyError,
    assert_plan_safe_for_provision,
    decide_from_plans,
    evaluate_plan_safety,
    plan_safety_attestation,
)
from omniagentos.swarm.planner import _fallback_plan, plan_swarm


def _task(
    task_id: str = "task-1",
    *,
    paths: list[str] | None = None,
    title: str = "Work",
    description: str = "do the work",
    acceptance: str = "done",
    verify_command: str = "",
) -> SwarmTaskSpec:
    return SwarmTaskSpec(
        id=task_id,
        title=title,
        description=description,
        owned_paths=list(paths or []),
        acceptance=acceptance,
        verify_command=verify_command,
    )


def _plan(
    *tasks: SwarmTaskSpec,
    goal: str = "goal",
    assumptions: list[str] | None = None,
    integration_task_id: str | None = None,
    mode: str = "solo",
) -> SwarmPlan:
    return SwarmPlan(
        goal=goal,
        tasks=list(tasks),
        assumptions=list(assumptions or []),
        integration_task_id=integration_task_id,
        mode=mode,  # type: ignore[arg-type]
        target_n=1,
    )


# --- nine adversarial fixtures (mission corpus + deterministic expansions) ---

ADVERSARIAL_CASES: list[tuple[str, SwarmPlan, str]] = [
    (
        "root_wide_worker",
        _plan(_task(paths=["."])),
        "root_wide_ownership",
    ),
    (
        "empty_mutating_ownership",
        _plan(_task(paths=[])),
        "empty_ownership",
    ),
    (
        "absolute_owned_path",
        _plan(_task(paths=["/nonexistent/outside-the-workspace/critical.py"])),
        "invalid_owned_path",
    ),
    (
        "protected_tier_p",
        _plan(_task(paths=["configs/policy.yaml"])),
        "protected_path",
    ),
    (
        "absolute_verify_outside_workspace",
        _plan(_task(paths=["src/ok.py"], verify_command="/bin/false")),
        "verify_outside_workspace",
    ),
    (
        "guaranteed_fail_verify",
        _plan(_task(paths=["src/ok.py"], verify_command="exit 1")),
        "guaranteed_fail_verify",
    ),
    (
        "outside_workspace_description",
        _plan(_task(paths=["outside-workspace/exact-target.py"])),
        "unresolved_owned_path",
    ),
    (
        "second_protected_path",
        _plan(_task(paths=["omniagentos/contracts.py"])),
        "protected_path",
    ),
    (
        "escape_owned_path",
        _plan(_task(paths=["src/../../outside"])),
        "invalid_owned_path",
    ),
]


@pytest.mark.parametrize(
    "case_id,plan,issue_code", ADVERSARIAL_CASES, ids=[c[0] for c in ADVERSARIAL_CASES]
)
def test_nine_adversarial_cases_are_non_ready(
    case_id: str, plan: SwarmPlan, issue_code: str
) -> None:
    decision = evaluate_plan_safety(plan)
    assert decision.is_ready is False, f"{case_id} must not be ready"
    assert decision.plans == []
    codes = {issue.code for issue in decision.issues} | {decision.disposition}
    assert issue_code in codes or any(issue_code in c for c in codes)


def test_fallback_plan_never_owns_dot() -> None:
    plan = _fallback_plan("goal", (), "unparseable")
    assert all("." not in task.owned_paths for task in plan.tasks)
    assert any("planner failed closed:" in note for note in plan.assumptions)
    decision = evaluate_plan_safety(plan)
    assert decision.is_ready is False
    assert decision.disposition in {"planner_unavailable", "invalid_plan"}


def test_unavailable_planner_pipeline_is_non_executable(tmp_path: Path) -> None:
    class _NoneLLM:
        def __call__(self, prompt: str, schema: dict, effort: str):  # noqa: ANN001
            del prompt, schema, effort
            return None

    plan = plan_swarm(
        "make a widget",
        str(tmp_path),
        planner_llm=_NoneLLM(),
        clarify_llm=lambda prompt, schema: {
            "mode": "spec",
            "spec": {
                "title": "widget",
                "description": "make it",
                "acceptance_criteria": [],
            },
        },
        recall_fn=lambda goal: "",
        playbook_path=tmp_path / "missing.json",
    )
    assert plan.mode == "solo"
    assert any("degraded to flat solo plan" in note for note in plan.assumptions)
    # The governing filter: unavailable/unparseable must not be runnable with ".".
    assert all(task.owned_paths != ["."] for task in plan.tasks)
    decision = evaluate_plan_safety(plan, workspace_dir=tmp_path)
    assert decision.is_ready is False


def test_safe_bounded_plan_is_ready() -> None:
    plan = _plan(
        _task(paths=["src/widget.py"]),
        mode="solo",
    )
    decision = evaluate_plan_safety(plan)
    assert decision.is_ready is True
    assert decision.plans == [plan]
    att = assert_plan_safe_for_provision(plan)
    assert len(att.digest) == 64


def test_integration_may_own_dot() -> None:
    plan = _plan(
        _task("a", paths=["src/a.py"]),
        _task("integration", paths=["."], title="Integrate", description="run suite"),
        integration_task_id="integration",
        mode="swarm",
    )
    decision = evaluate_plan_safety(plan)
    assert decision.is_ready is True


def test_multi_bundle_refused_without_allow_flag() -> None:
    plans = [
        _plan(_task("a", paths=["src/a.py"]), goal="fix API"),
        _plan(_task("b", paths=["docs/b.md"]), goal="docs"),
    ]
    decision = decide_from_plans(plans)
    assert decision.is_ready is False
    assert any(issue.code == "multiple_bundles" for issue in decision.issues)
    assert "unavailable" in decision.reason.lower()


def test_multi_bundle_allowed_when_flag_set() -> None:
    plans = [
        _plan(_task("a", paths=["src/a.py"]), goal="fix API"),
        _plan(_task("b", paths=["docs/b.md"]), goal="docs"),
    ]
    decision = decide_from_plans(plans, allow_multi_bundle=True)
    assert decision.is_ready is True
    assert len(decision.plans) == 2


def test_assert_plan_safe_raises_plan_safety_error() -> None:
    plan = _plan(_task(paths=["."]))
    with pytest.raises(PlanSafetyError) as excinfo:
        assert_plan_safe_for_provision(plan)
    assert excinfo.value.decision.is_ready is False


def test_attestation_mismatch_fails_closed() -> None:
    plan = _plan(_task(paths=["src/a.py"]))
    digest = plan_safety_attestation(plan)
    assert plan_safety_attestation(plan) == digest
    plan.tasks[0].owned_paths = ["src/b.py"]
    assert plan_safety_attestation(plan) != digest
    with pytest.raises(PlanSafetyError) as excinfo:
        assert_plan_safe_for_provision(plan, expected_attestation=digest)
    assert any(i.code == "attestation_mismatch" for i in excinfo.value.decision.issues)


def test_dot_scope_gate_line_exists_for_mutation_audit() -> None:
    """Named mutation target: the root-wide ownership refuse must stay present.

    Reviewers mutate this condition (``normalized == "." and not is_integration``)
    to permit ``"."``; this test and the nine-case suite must then fail.
    """
    source = Path("omniagentos/swarm/plan_safety.py").read_text(encoding="utf-8")
    assert 'normalized == "." and not is_integration' in source
    assert "root_wide_ownership" in source


def test_provision_run_refuses_dot_scope_before_dal(tmp_path: Path) -> None:
    from omniagentos.collab.store import CollabStore
    from omniagentos.swarm.dal import SwarmDal
    from omniagentos.swarm.planner import provision_run

    db = str(tmp_path / "prov.db")
    CollabStore(db)
    dal = SwarmDal(db)
    plan = _plan(_task(paths=["."]))
    try:
        with pytest.raises(PlanSafetyError):
            provision_run(plan, dal=dal, working_dir=str(tmp_path), write_plan_doc=False)
        assert dal.list_runs() == []
    finally:
        dal.close()


def test_provision_run_gate_precedes_dal_in_source() -> None:
    """Mutation ``refusal-creates-run``: live assert before dal.provision_run.

    A commented-out gate still contains the identifier string; require a real
    statement so ``# assert_plan_safe...`` turns this red.
    """
    source = Path("omniagentos/swarm/planner.py").read_text(encoding="utf-8")
    fn = source.index("def provision_run(")
    next_def = source.find("\ndef ", fn + 1)
    chunk = source[fn : next_def if next_def != -1 else fn + 12000]
    live_gate = None
    for i, line in enumerate(chunk.splitlines()):
        stripped = line.lstrip()
        if stripped.startswith("#"):
            continue
        if "assert_plan_safe_for_provision(" in stripped:
            live_gate = i
            break
    assert live_gate is not None, "provision_run must call assert_plan_safe_for_provision"
    dal_line = None
    for i, line in enumerate(chunk.splitlines()):
        if "dal.provision_run(" in line and not line.lstrip().startswith("#"):
            dal_line = i
            break
    assert dal_line is not None
    assert live_gate < dal_line
