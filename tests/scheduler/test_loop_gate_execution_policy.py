"""The loop-gate target policy, checked where the gate actually EXECUTES.

The gap this closes
-------------------
``routines.loop_gate_errors`` classifies a loop's gate targets against the
checkout the validating process happens to be running in. The gate runs later,
in a SEPARATELY configured workspace, pinned at a DIFFERENT commit — and nothing
binds those two states. The reviewer's concrete bypass:

* commit A (the API's checkout) has no ``aliases/loop_gate.py``, so validation
  cannot resolve it, falls back to its spelling, and admits the row;
* commit B (the gate workspace, clean and committed) carries that same tracked
  path as a symlink to ``tests/scheduler/test_loop_jobs.py``;
* the executor resolves inode-anchored, so it runs the machinery — the suite
  that passes on every tick of every loop whatever the instance produced.

"Clean and committed" never meant "the same commit as validation", and a target
that is honest today can be turned into that symlink by any later commit.
Making validation environment-dependent was the wrong end to close it at (it
would refuse honest rows whose target only exists at the workspace's pin), so
the binding check lives at the OTHER boundary: in the run tree, at the pinned
SHA, using the SAME verdict function.

The class it settles as
-----------------------
``GateEvidenceRefusal`` — CONDEMNING (``gate_passed=0``,
``stop_reason='gate_refused'``), per ``routines_settle``'s taxonomy: "refused"
is *a fact about the CANDIDATE*, alongside an unrecognised verifier and a target
that does not exist. Nothing here is absent and nothing is wrong with the
workspace; the workspace answered perfectly and the answer condemns the gate.
Settling it NULL would take the alias out of the acceptance-floor denominator
and let it tick forever, uncounted — the self-grading loop again, wearing an
infrastructure problem's mask.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from omniagentos.scheduler.gate_evidence import (
    GateEvidenceRefusal,
    GateEvidenceStore,
    GateWorkspaceUnusable,
)
from omniagentos.scheduler.gate_runner import (
    GateRunRequest,
    PytestGateRunner,
    refuse_loop_gate_on_the_machinery,
)
from omniagentos.scheduler.routines import LOOP_TASK_MODULE
from omniagentos.scheduler.store import RoutinesStore

ALIAS = "aliases/loop_gate.py"
MACHINERY = "tests/scheduler/test_loop_jobs.py"
HONEST = "loops/tests/instances/test_health_monitor.py"


@pytest.fixture
def store_db(tmp_path: Path):
    """A migrated scratch DB — never the live one."""
    from omniagentos.db.store import SqliteStore
    from tests.support.db_template import make_store

    db_path = str(tmp_path / "control.sqlite3")
    handle = make_store(SqliteStore, db_path)
    yield handle
    handle.close()


def _loop_row(*, command: str) -> dict:
    return {
        "name": "w3-health-monitor",
        "description": "W3 health monitor loop",
        "trigger_type": "cron",
        "trigger_config": {"cron": "*/10 * * * *"},
        "task_template": {
            "title": "Loop tick: w3_health_monitor",
            "harness": "codex",
            "input": {
                "module": LOOP_TASK_MODULE,
                "kind": "loop",
                "template": "monitor_diagnose_repair_verify",
                "instance_id": "w3_health_monitor",
                "instance_module": "omniagentos_loops.instances.health_monitor",
                "params": {},
            },
        },
        "gate_type": "test_command",
        "gate_config": {"command": command, "expected_exit_code": 0},
        "hard_cap_type": "budget_usd",
        "hard_cap_value": 5.0,
        "notification_target": {"channel": "desktop"},
        "status": "active",
    }


def _git(tree: Path, *args: str) -> None:
    done = subprocess.run(
        ["git", "-C", str(tree), *args], capture_output=True, text=True, check=False
    )
    assert done.returncode == 0, f"git {args}: {done.stderr or done.stdout}"


@pytest.fixture
def diverged_workspace(tmp_path: Path) -> Path:
    """A clean, pinned workspace whose COMMITTED tree contains the alias.

    A real git checkout, because the executor pins and verifies one: the point
    of the fixture is that the symlink is tracked and the tree is clean, which
    is exactly the state the "clean and committed" argument said was safe.
    """
    tree = tmp_path / "gate-workspace"
    (tree / "tests" / "scheduler").mkdir(parents=True)
    (tree / "loops" / "tests" / "instances").mkdir(parents=True)
    (tree / "aliases").mkdir()
    (tree / "tests" / "scheduler" / "test_loop_jobs.py").write_text(
        "def test_mechanism():\n    assert True\n", encoding="utf-8"
    )
    (tree / "loops" / "tests" / "instances" / "test_health_monitor.py").write_text(
        "def test_instance():\n    assert True\n", encoding="utf-8"
    )
    (tree / "aliases" / "loop_gate.py").symlink_to(
        Path("..") / "tests" / "scheduler" / "test_loop_jobs.py"
    )
    _git(tree, "init", "--quiet")
    _git(tree, "config", "user.email", "gate@test")
    _git(tree, "config", "user.name", "gate")
    _git(tree, "add", "-A")
    _git(tree, "commit", "--quiet", "-m", "workspace with a tracked alias")
    return tree


def _request(
    workspace: Path, command: str, *, task_module: str = LOOP_TASK_MODULE
) -> GateRunRequest:
    return GateRunRequest(
        routine_id="rtn_loop",
        run_id="btrun_1",
        iteration=1,
        gate_type="test_command",
        gate_config={"command": command, "expected_exit_code": 0},
        workspace=workspace,
        task_module=task_module,
    )


# ---------------------------------------------------------------------------
# 1. the divergence, end to end
# ---------------------------------------------------------------------------


def test_the_validating_checkout_admits_the_alias_it_cannot_see(
    store_db, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Half one of the bypass, asserted rather than assumed.

    Creation is ADMITTED — deliberately, and documented as best-effort: refusing
    every target this checkout cannot resolve would refuse honest gates whose
    file exists only at the workspace's pin. This test exists so that admission
    is a recorded decision instead of an accident, and so the execution-time
    refusal below is provably the thing doing the work.
    """
    blind = tmp_path / "checkout-without-the-alias"
    blind.mkdir()
    monkeypatch.setattr("omniagentos.contracts._repo_root", lambda: str(blind))

    created = RoutinesStore(store_db).create_routine(_loop_row(command=f"pytest {ALIAS}"))

    assert created["gate_config"]["command"] == f"pytest {ALIAS}"


def test_execution_refuses_the_alias_by_its_location_in_the_pinned_run_tree(
    diverged_workspace: Path, tmp_path: Path
) -> None:
    """Half two: the same row, judged where it actually runs.

    Drives the real ``PytestGateRunner`` — it pins the workspace, materialises
    the ephemeral run tree, and refuses there — so this asserts the production
    path and not a helper in isolation.
    """
    runner = PytestGateRunner(GateEvidenceStore(tmp_path / "evidence"))

    with pytest.raises(GateEvidenceRefusal) as excinfo:
        runner.run(_request(diverged_workspace, f"pytest {ALIAS}"))

    message = str(excinfo.value)
    assert "resolves to" in message
    assert "tests/scheduler/test_loop_jobs.py" in message
    assert "machinery" in message


def test_the_refusal_is_condemning_and_not_a_workspace_problem(
    diverged_workspace: Path, tmp_path: Path
) -> None:
    """The class matters more than the message.

    ``GateWorkspaceUnusable`` is a SUBCLASS of ``GateEvidenceRefusal`` and
    ``produce_gate_evidence`` catches it first, mapping it to ``unavailable`` →
    ``gate_passed=NULL`` → out of the acceptance-floor denominator. Raising that
    class here would let the alias tick forever, uncounted. The refusal must
    therefore be the base class exactly: a fact about the candidate.
    """
    runner = PytestGateRunner(GateEvidenceStore(tmp_path / "evidence"))

    with pytest.raises(GateEvidenceRefusal) as excinfo:
        runner.run(_request(diverged_workspace, f"pytest {ALIAS}"))

    assert not isinstance(excinfo.value, GateWorkspaceUnusable)


def test_produce_gate_evidence_maps_the_refusal_to_a_condemning_settlement(
    diverged_workspace: Path, tmp_path: Path
) -> None:
    """…and the mapping to gate_passed=0 is asserted, not assumed."""
    from omniagentos.scheduler.gate_runner import produce_gate_evidence

    evidence_store = GateEvidenceStore(tmp_path / "evidence")
    runner = PytestGateRunner(evidence_store)

    outcome = produce_gate_evidence(
        runner, evidence_store, _request(diverged_workspace, f"pytest {ALIAS}")
    )

    # routines_settle: outcome "refused" → gate_passed=False, stop_reason
    # 'gate_refused'; "unavailable" → NULL and excluded from the floor.
    assert outcome.status == "refused", outcome.detail
    assert "machinery" in outcome.detail


# ---------------------------------------------------------------------------
# 2. the policy does not over-refuse
# ---------------------------------------------------------------------------


def test_an_honest_instance_gate_is_not_refused(diverged_workspace: Path) -> None:
    """The sanctioned shape must survive the new check, in the same workspace."""
    refuse_loop_gate_on_the_machinery(
        diverged_workspace, (HONEST,), _request(diverged_workspace, f"pytest {HONEST}")
    )


def test_a_non_loop_routine_may_still_be_gated_by_the_scheduler_suite(
    diverged_workspace: Path,
) -> None:
    """The policy is keyed on the routine's work, never on the path alone.

    A routine whose work IS the scheduler is properly graded by the scheduler's
    own suite; refusing that would be a rule about paths rather than about
    self-grading.
    """
    refuse_loop_gate_on_the_machinery(
        diverged_workspace,
        (MACHINERY,),
        _request(diverged_workspace, f"pytest {MACHINERY}", task_module="omniagentos.lab.jobs"),
    )


@pytest.mark.parametrize("target", [MACHINERY, ".", "tests", ALIAS])
def test_every_machinery_or_blanket_target_is_refused_in_the_run_tree(
    diverged_workspace: Path, target: str
) -> None:
    """Direct spellings, the alias, and blanket gates: one rule, one function."""
    with pytest.raises(GateEvidenceRefusal):
        refuse_loop_gate_on_the_machinery(
            diverged_workspace, (target,), _request(diverged_workspace, f"pytest {target}")
        )


def test_settlement_records_the_refusal_as_gate_passed_0_and_gate_refused(
    store_db, diverged_workspace: Path, tmp_path: Path
) -> None:
    """The whole claim, through the production settler, in one assertion.

    Same shape as the pinned "a gate naming a missing target is still a failure"
    doctrine test — the other member of the fact-about-the-CANDIDATE class. A
    NULL here would be the interesting failure: it would keep this loop out of
    the acceptance-floor denominator and let the alias tick forever.
    """
    from omniagentos.contracts import RunState
    from omniagentos.scheduler.routines_settle import settle_routine_run

    routines = RoutinesStore(store_db)
    routine = routines.create_routine(_loop_row(command=f"pytest {HONEST}"))
    # The row is honest at validation time and the alias is what EXECUTES: the
    # divergence this whole file is about, written the short way.
    routines.update_routine(
        routine["id"], {"gate_config": {"command": f"pytest {ALIAS}", "expected_exit_code": 0}}
    )
    routine = routines.get_routine(routine["id"])
    run_id = "btrun_alias"
    routine_run = routines.record_run(
        routine["id"],
        {"run_id": run_id, "iteration": 1, "started_at": "2026-01-01T09:00:00Z"},
    )

    settled = settle_routine_run(
        store_db,
        routine_run,
        routine,
        {"id": run_id, "state": RunState.COMPLETED.value, "finished_at": "2026-01-01T09:00:00Z"},
        evidence_store=GateEvidenceStore(tmp_path / "evidence"),
        gate_runner=PytestGateRunner(GateEvidenceStore(tmp_path / "evidence")),
        workspace=diverged_workspace,
    )

    assert settled["stop_reason"] == "gate_refused"
    # …refused by THIS policy, not by a target that happens to be missing.
    assert "machinery" in str(settled.get("notes") or "")
    assert settled["gate_passed"] == 0, (
        "a loop gate that resolves onto the machinery must CONDEMN the run, not "
        "settle NULL — NULL is excluded from the acceptance floor, which is how "
        "an alias would tick forever uncounted"
    )
