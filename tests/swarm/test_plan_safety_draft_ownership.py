"""Bounded NON-FILE ownership is expressible, and it is DRAFT-ONLY.

Operator ruling 2026-08-10: a mission plan whose task owns a campaign/segment/
slug rather than a file path must be *representable* (it used to be refused
with ``empty_ownership``, so no non-code plan could ever be written down), but
it must never execute — the conflict freedom the coordinator enforces at run
time (pre-attempt snapshot, ownership revert, path-scoped commit) is defined
over file paths only.

This module pins both halves:

* a well-formed ``resource:<kind>:<name>`` entry satisfies the ownership
  requirement, and a malformed one refuses;
* every plan carrying one decides ``draft`` — a non-ready disposition, so the
  decision envelope refuses to carry the plan, ``assert_plan_safe_for_provision``
  raises, and ``provision_run`` writes no run row (the seam the scheduler reads
  from: no run row, no dispatch).

Named mutations this suite must catch: deleting the draft classification (a
resource plan would become ``ready``), widening the resource grammar to admit a
path separator, and letting a token file path alongside a resource entry buy
back an executable plan.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from omniagentos.swarm.contracts import (
    DRAFT_PLAN_DISPOSITION,
    SwarmPlan,
    SwarmPlanDecision,
    SwarmTaskSpec,
)
from omniagentos.swarm.plan_safety import (
    DRAFT_ONLY_OWNERSHIP_CODE,
    DRAFT_ONLY_OWNERSHIP_REASON,
    PlanSafetyError,
    assert_plan_safe_for_provision,
    decide_from_plans,
    evaluate_plan_safety,
    is_owned_resource_entry,
    parse_owned_resource,
    plan_safety_attestation,
)


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
    integration_task_id: str | None = None,
    mode: str = "solo",
) -> SwarmPlan:
    return SwarmPlan(
        goal=goal,
        tasks=list(tasks),
        integration_task_id=integration_task_id,
        mode=mode,  # type: ignore[arg-type]
        target_n=1,
    )


def _codes(decision: SwarmPlanDecision) -> set[str]:
    return {issue.code for issue in decision.issues}


# --------------------------------------------------------------------------
# 1. A bounded resource entry satisfies ownership (the refusal that blocked
#    every non-code mission plan).
# --------------------------------------------------------------------------


def test_resource_entry_satisfies_ownership_without_empty_ownership() -> None:
    plan = _plan(
        _task(
            paths=["resource:campaign:spring-launch"],
            title="Write the five-email sales sequence",
            description="produce a publish-ready sequence; do not send",
        )
    )
    decision = evaluate_plan_safety(plan)
    assert "empty_ownership" not in _codes(decision)
    assert decision.disposition == DRAFT_PLAN_DISPOSITION
    assert _codes(decision) == {DRAFT_ONLY_OWNERSHIP_CODE}
    assert decision.reason == DRAFT_ONLY_OWNERSHIP_REASON


def test_draft_issue_names_the_resources_it_saw() -> None:
    plan = _plan(_task(paths=["resource:campaign:spring-launch", "resource:segment:warm_leads"]))
    decision = evaluate_plan_safety(plan)
    issue = next(i for i in decision.issues if i.code == DRAFT_ONLY_OWNERSHIP_CODE)
    assert issue.detail["resources"] == [
        "resource:campaign:spring-launch",
        "resource:segment:warm_leads",
    ]
    assert issue.detail["task_id"] == "task-1"


@pytest.mark.parametrize(
    "entry,expected",
    [
        ("resource:campaign:spring-launch", ("campaign", "spring-launch")),
        ("resource:segment:warm_leads", ("segment", "warm_leads")),
        ("resource:slug:2026-08-10.launch-note", ("slug", "2026-08-10.launch-note")),
        ("  resource:campaign:x  ", ("campaign", "x")),
    ],
)
def test_parse_owned_resource_accepts_bounded_forms(entry: str, expected: tuple[str, str]) -> None:
    assert parse_owned_resource(entry) == expected


# --------------------------------------------------------------------------
# 2. Malformed resource forms refuse — and never satisfy ownership.
# --------------------------------------------------------------------------


MALFORMED_RESOURCES: list[str] = [
    "resource:",
    "resource::spring",
    "resource:campaign:",
    "resource:campaign",
    "resource:campaign:a/b",  # path separator
    "resource:campaign:a\\b",  # windows path separator
    "resource:campaign:a:b",  # extra segment
    "resource:camp aign:spring",  # whitespace
    "resource:campaign:sp ring",
    "resource:campaign:*",  # glob
    "resource:campaign:..",  # traversal-shaped name
    "resource:.:spring",
    "resource:-campaign:spring",  # must start alphanumeric
    "resource:CAMPAIGN:spring",  # kind is canonical lowercase
    "Resource:campaign:spring",  # near-miss prefix must not read as a file
    "RESOURCE:campaign:spring",
    "resource:" + "k" * 40 + ":spring",  # unbounded kind
    "resource:campaign:" + "n" * 200,  # unbounded name
]


@pytest.mark.parametrize("entry", MALFORMED_RESOURCES)
def test_malformed_resource_entry_refuses(entry: str) -> None:
    decision = evaluate_plan_safety(_plan(_task(paths=[entry])))
    assert decision.is_ready is False
    assert decision.plans == []
    assert decision.disposition == "invalid_plan"
    assert "invalid_owned_resource" in _codes(decision)
    # Fail closed: a malformed resource must not buy draft status either.
    assert DRAFT_ONLY_OWNERSHIP_CODE not in _codes(decision)


@pytest.mark.parametrize("entry", MALFORMED_RESOURCES)
def test_malformed_resource_entry_is_never_parsed(entry: str) -> None:
    assert parse_owned_resource(entry) is None
    assert is_owned_resource_entry(entry) is True


def test_malformed_resource_alongside_a_file_path_still_refuses() -> None:
    decision = evaluate_plan_safety(_plan(_task(paths=["src/widget.py", "resource:campaign:a/b"])))
    assert decision.disposition == "invalid_plan"
    assert "invalid_owned_resource" in _codes(decision)


# --------------------------------------------------------------------------
# 3. Draft never executes: envelope, provision boundary, and the DAL seam.
# --------------------------------------------------------------------------


def test_draft_decision_carries_no_executable_plan() -> None:
    decision = evaluate_plan_safety(_plan(_task(paths=["resource:campaign:spring"])))
    assert decision.is_ready is False
    assert decision.plans == []


def test_draft_disposition_cannot_carry_plans_at_all() -> None:
    plan = _plan(_task(paths=["resource:campaign:spring"]))
    with pytest.raises(ValidationError, match="only disposition 'ready'"):
        SwarmPlanDecision(disposition=DRAFT_PLAN_DISPOSITION, plans=[plan])


def test_assert_plan_safe_refuses_a_draft_with_the_named_reason() -> None:
    plan = _plan(_task(paths=["resource:campaign:spring"]))
    with pytest.raises(PlanSafetyError) as excinfo:
        assert_plan_safe_for_provision(plan)
    assert str(excinfo.value) == DRAFT_ONLY_OWNERSHIP_REASON
    assert excinfo.value.decision.disposition == DRAFT_PLAN_DISPOSITION
    assert excinfo.value.decision.is_ready is False


def test_provision_run_refuses_a_draft_before_the_dal(tmp_path: Path) -> None:
    """The execution seam: no run row means the scheduler has nothing to start."""
    from omniagentos.collab.store import CollabStore
    from omniagentos.swarm.dal import SwarmDal
    from omniagentos.swarm.planner import provision_run

    db = str(tmp_path / "prov.db")
    CollabStore(db)
    dal = SwarmDal(db)
    plan = _plan(
        _task(
            paths=["resource:campaign:spring-launch"],
            title="Draft the launch sequence",
            description="publish-ready artifact only",
        )
    )
    try:
        with pytest.raises(PlanSafetyError) as excinfo:
            provision_run(plan, dal=dal, working_dir=str(tmp_path), write_plan_doc=False)
        assert excinfo.value.decision.disposition == DRAFT_PLAN_DISPOSITION
        assert dal.list_runs() == []
    finally:
        dal.close()


def test_mixed_file_and_resource_ownership_is_still_draft() -> None:
    """A token file path must not buy an executable plan for a resource task."""
    decision = evaluate_plan_safety(
        _plan(_task(paths=["docs/launch-notes.md", "resource:campaign:spring"]))
    )
    assert decision.disposition == DRAFT_PLAN_DISPOSITION
    assert decision.plans == []


def test_one_resource_task_makes_the_whole_plan_draft() -> None:
    decision = evaluate_plan_safety(
        _plan(
            _task("a", paths=["src/widget.py"]),
            _task("b", paths=["resource:segment:warm_leads"]),
            mode="swarm",
        )
    )
    assert decision.disposition == DRAFT_PLAN_DISPOSITION


def test_real_defects_outrank_draft_status() -> None:
    protected = evaluate_plan_safety(
        _plan(
            _task("a", paths=["resource:campaign:spring"]),
            _task("b", paths=["configs/policy.yaml"]),
            mode="swarm",
        )
    )
    assert protected.disposition == "policy_denied"
    assert DRAFT_ONLY_OWNERSHIP_CODE in _codes(protected)

    impossible = evaluate_plan_safety(
        _plan(
            _task("a", paths=["resource:campaign:spring"]),
            _task("b", paths=["src/ok.py"], verify_command="/bin/false"),
            mode="swarm",
        )
    )
    assert impossible.disposition == "impossible"

    invalid = evaluate_plan_safety(
        _plan(
            _task("a", paths=["resource:campaign:spring"]),
            _task("b", paths=["."]),
            mode="swarm",
        )
    )
    assert invalid.disposition == "invalid_plan"
    assert invalid.reason == "plan safety rejected 1 issue(s)"


def test_resource_ownership_is_covered_by_the_attestation() -> None:
    plan = _plan(_task(paths=["resource:campaign:spring"]))
    digest = plan_safety_attestation(plan)
    plan.tasks[0].owned_paths = ["resource:campaign:autumn"]
    assert plan_safety_attestation(plan) != digest


# --------------------------------------------------------------------------
# 4. verify_command: relaxed for drafts only.
# --------------------------------------------------------------------------


def test_draft_may_omit_verify_command_even_when_acceptance_promises_one() -> None:
    decision = evaluate_plan_safety(
        _plan(
            _task(
                paths=["resource:campaign:spring"],
                acceptance="artifact exists and the suite passes",
                verify_command="",
            )
        )
    )
    assert decision.disposition == DRAFT_PLAN_DISPOSITION
    assert "missing_verify_command" not in _codes(decision)
    assert "invalid_verify_command" not in _codes(decision)


def test_executing_plan_keeps_the_verify_command_requirement() -> None:
    decision = evaluate_plan_safety(
        _plan(
            _task(
                paths=["src/widget.py"],
                acceptance="artifact exists and the suite passes",
                verify_command="",
            )
        )
    )
    assert decision.disposition == "invalid_plan"
    assert "missing_verify_command" in _codes(decision)


def test_draft_with_an_unsafe_verify_command_still_refuses() -> None:
    """The relaxation is *omission* only — a declared verifier stays strict."""
    unparsable = evaluate_plan_safety(
        _plan(_task(paths=["resource:campaign:spring"], verify_command="curl example.com"))
    )
    assert unparsable.disposition == "invalid_plan"
    assert "invalid_verify_command" in _codes(unparsable)

    escaping = evaluate_plan_safety(
        _plan(_task(paths=["resource:campaign:spring"], verify_command="/bin/false"))
    )
    assert escaping.disposition == "impossible"
    assert "verify_outside_workspace" in _codes(escaping)


def test_draft_may_declare_an_allowed_verifier() -> None:
    decision = evaluate_plan_safety(
        _plan(_task(paths=["resource:campaign:spring"], verify_command="ruff check ."))
    )
    assert decision.disposition == DRAFT_PLAN_DISPOSITION
    assert _codes(decision) == {DRAFT_ONLY_OWNERSHIP_CODE}


# --------------------------------------------------------------------------
# 5. Zero drift for file-path plans (the whole existing estate).
# --------------------------------------------------------------------------


FILE_PATH_CASES: list[tuple[str, SwarmPlan, str, str]] = [
    (
        "ready",
        _plan(_task(paths=["src/widget.py"])),
        "ready",
        "plan passed deterministic safety checks",
    ),
    (
        "empty_ownership",
        _plan(_task(paths=[])),
        "invalid_plan",
        "plan safety rejected 1 issue(s)",
    ),
    (
        "root_wide",
        _plan(_task(paths=["."])),
        "invalid_plan",
        "plan safety rejected 1 issue(s)",
    ),
    (
        "protected",
        _plan(_task(paths=["configs/policy.yaml"])),
        "policy_denied",
        "plan safety rejected 1 issue(s)",
    ),
    (
        "guaranteed_fail_verify",
        _plan(_task(paths=["src/ok.py"], verify_command="exit 1")),
        "impossible",
        "plan safety rejected 1 issue(s)",
    ),
    (
        "absolute_owned_path",
        _plan(_task(paths=["/etc/passwd"])),
        "invalid_plan",
        "plan safety rejected 1 issue(s)",
    ),
]


@pytest.mark.parametrize(
    "case_id,plan,disposition,reason",
    FILE_PATH_CASES,
    ids=[c[0] for c in FILE_PATH_CASES],
)
def test_file_path_plans_keep_identical_decisions(
    case_id: str, plan: SwarmPlan, disposition: str, reason: str
) -> None:
    decision = evaluate_plan_safety(plan)
    assert decision.disposition == disposition, case_id
    assert decision.reason == reason, case_id
    assert DRAFT_ONLY_OWNERSHIP_CODE not in _codes(decision)


def test_file_path_bundle_decisions_are_unchanged() -> None:
    plans = [
        _plan(_task("a", paths=["src/a.py"]), goal="fix API"),
        _plan(_task("b", paths=["docs/b.md"]), goal="docs"),
    ]
    assert decide_from_plans(plans).disposition == "needs_clarification"
    assert decide_from_plans(plans, allow_multi_bundle=True).is_ready is True
