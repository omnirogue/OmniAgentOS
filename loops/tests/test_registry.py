"""The routine-row contract, validated against PRODUCTION's own validator.

If ``omniagentos.scheduler.routines.validate_routine`` rejects the row this
module generates, a loop can never fire — so the test calls the real validator
rather than asserting on a dict shape.

The same reasoning is why the argv tests at the bottom drive
``omniagentos.scheduler.loop_jobs.run_loop_job`` with a row this helper built,
instead of a dict written by hand: a hand-written payload can carry a field the
helper cannot produce, and for ``instance_module`` it did — every loop routine
seeded through the public helper fired and failed on ``required_tools``.
"""

from __future__ import annotations

import json

import pytest
from omniagentos_loops.registry import (
    LOOP_MODULE,
    LoopSpecError,
    loop_routine_row,
    loop_spec,
)
from omniagentos_loops.templates import TEMPLATE_NAMES

from omniagentos.scheduler.routines import RoutineValidationError, validate_routine

W2_MODULE = "omniagentos_loops.instances.w2_inbox_triage"
W3_MODULE = "omniagentos_loops.instances.health_monitor"

#: Every row below declares its own gate, because there is no default to fall
#: back on any more: the one this helper used to substitute graded the
#: scheduler→worker mechanism and therefore passed whatever the loop produced
#: (see ``_require_gate_command``). A gate naming the instance's own suite is
#: the shape the live rows use.
W2_GATE = "pytest loops/tests/instances/test_w2_inbox_triage.py"


@pytest.mark.parametrize("template", sorted(TEMPLATE_NAMES))
def test_generated_rows_pass_the_production_routine_validator(template):
    row = loop_routine_row(
        name=f"loop-{template}",
        template=template,
        instance_id="w2_inbox_triage",
        instance_module=W2_MODULE,
        cron="*/15 * * * *",
        params={"mailbox": "support"},
        gate_command=W2_GATE,
    )
    validate_routine(row)


def test_generated_rows_are_actually_INSERTABLE_by_the_routines_store(db_path):
    """The decisive registry test: ``validate_routine`` is not the only gate.

    ``RoutinesStore.create_routine`` runs a second, stricter belt (D5) that
    refuses an ACTIVE ``harness='mock'`` row without a non-vacuous verifier
    command. A row that only passes ``validate_routine`` can still be
    unregisterable, which is exactly the failure this test caught.
    """
    from omniagentos.db.store import SqliteStore
    from omniagentos.scheduler.store import RoutinesStore

    store = SqliteStore(db_path)
    try:
        routines = RoutinesStore(store)
        for name in sorted(TEMPLATE_NAMES):
            routines.create_routine(
                loop_routine_row(
                    name=f"loop-{name}",
                    template=name,
                    instance_id="w2_inbox_triage",
                    instance_module=W2_MODULE,
                    cron="*/15 * * * *",
                    gate_command=W2_GATE,
                )
            )
        rows = routines.list_routines()
        assert len(rows) == len(TEMPLATE_NAMES)
        assert all(row["status"] == "active" for row in rows)
    finally:
        store.close()


def test_the_seeded_gate_command_is_one_the_gate_runner_can_actually_execute():
    """A seeded gate is now EXECUTED every tick, so it must be runnable.

    Settlement runs ``gate_config.command`` in the gate workspace at its pinned
    commit. Two failure modes that used to be harmless are now expensive, and
    both are mechanical to refuse here:

    * a command the allowlist does not recognise never produces evidence;
    * a command naming a target that does not exist is a REFUSAL
      (``gate target does not exist``), which settles ``gate_passed=0`` — so a
      loop seeded with a stale test path would auto-pause itself within three
      ticks while behaving perfectly.

    Checked against the real repo tree with the runner's own primitives, not a
    string match.
    """
    from pathlib import Path

    from omniagentos.scheduler.gate_runner import parse_gate_command, resolve_targets

    repo_root = Path(__file__).resolve().parents[2]
    row = _w3_row()
    tool, targets = parse_gate_command(str(row["gate_config"]["command"]))
    assert tool == "pytest"
    assert resolve_targets(repo_root, targets) == targets


def test_the_helper_has_no_default_gate_command_to_substitute():
    """The refusal, at the seam an author actually touches.

    A loop's gate is the ONLY thing that can contradict the loop's own report,
    so a helper that fills it in when the author does not is a helper that
    invents the verdict. The one it used to fill in — ``pytest
    tests/scheduler/test_loop_jobs.py`` — grades the scheduler→worker mechanism
    and passes on every tick of every loop, so a routine seeded without a gate
    settled favourable over garbage forever.

    Two shapes, both refused: the omitted argument (``TypeError``, the same
    contract ``instance_module`` has) and the blank one, which is what a caller
    threading an empty config value would produce.
    """
    import omniagentos_loops.registry as registry

    assert not hasattr(registry, "DEFAULT_GATE_COMMAND"), (
        "a default gate command is the defect: it is substituted silently and "
        "passes whatever the loop produced"
    )
    with pytest.raises(TypeError):
        loop_routine_row(  # type: ignore[call-arg]
            name="loop-x",
            template="draft_approve_send",
            instance_id="i",
            instance_module=W2_MODULE,
            cron="0 * * * *",
        )
    for blank in ("", "   ", None):
        with pytest.raises(LoopSpecError) as excinfo:
            _w3_row(gate_command=blank)
        assert "gate_command" in str(excinfo.value)


def test_event_triggered_rows_are_valid_too():
    row = loop_routine_row(
        name="loop-comms-inbound",
        template="poll_classify_act_verify",
        instance_id="cs_intake",
        instance_module=W2_MODULE,
        event="comms.message",
        gate_command=W2_GATE,
    )
    validate_routine(row)
    assert row["trigger_type"] == "event"


def test_a_row_without_a_trigger_is_refused():
    with pytest.raises(LoopSpecError):
        loop_routine_row(
            name="x",
            template="draft_approve_send",
            instance_id="y",
            instance_module=W2_MODULE,
            gate_command=W2_GATE,
        )


def test_generated_rows_round_trip_through_loop_spec():
    row = loop_routine_row(
        name="loop-x",
        template="draft_approve_send",
        instance_id="cs_replies",
        instance_module="omniagentos_loops.instances.cs_replies",
        cron="0 * * * *",
        params={"subject": "hi"},
        timeout_s=120,
        gate_command="pytest loops/tests/instances/test_cs_replies.py",
    )
    spec = loop_spec(row["task_template"])
    assert spec is not None
    assert spec.template == "draft_approve_send"
    assert spec.instance_id == "cs_replies"
    assert spec.instance_module == "omniagentos_loops.instances.cs_replies"
    assert spec.params == {"subject": "hi"}
    assert spec.timeout_s == 120


def test_non_loop_templates_are_ignored():
    assert loop_spec({"input": {"module": "omniagentos.memlife.dream"}}) is None
    assert loop_spec({}) is None


@pytest.mark.parametrize(
    "bad",
    [
        {"module": LOOP_MODULE, "template": "../etc/passwd", "instance_id": "x"},
        {"module": LOOP_MODULE, "template": "draft_approve_send", "instance_id": "a b"},
        {"module": LOOP_MODULE, "template": "draft_approve_send", "instance_id": ""},
        {"module": LOOP_MODULE, "template": "Draft_Approve_Send", "instance_id": "x"},
    ],
)
def test_unsafe_names_are_refused(bad):
    with pytest.raises(LoopSpecError):
        loop_spec({"input": {**bad, "instance_module": W2_MODULE}})


def test_timeout_is_clamped_to_a_sane_window():
    row = {
        "input": {
            "module": LOOP_MODULE,
            "template": "x_y",
            "instance_id": "i",
            "instance_module": W2_MODULE,
            "timeout_s": 99999,
        }
    }
    assert loop_spec(row).timeout_s == 3600
    row["input"]["timeout_s"] = 1
    assert loop_spec(row).timeout_s == 30


def test_a_generated_row_is_disable_able_and_carries_a_revision_free_shape():
    row = loop_routine_row(
        name="loop-x",
        template="draft_approve_send",
        instance_id="i",
        instance_module=W2_MODULE,
        cron="0 * * * *",
        gate_command=W2_GATE,
    )
    assert row["status"] == "active"
    assert "revision" not in row, "revision is owned by the store's CAS, never by a seed payload"
    with pytest.raises(RoutineValidationError):
        validate_routine({**row, "gate_type": "nonsense"})


# --------------------------------------------------------------------------
# instance_module: the row must name the module that supplies its tools
# --------------------------------------------------------------------------


class _FakeCompleted:
    def __init__(self, stdout: str, returncode: int = 0, stderr: str = "") -> None:
        self.stdout = stdout
        self.returncode = returncode
        self.stderr = stderr


@pytest.fixture
def spy_worker(monkeypatch, tmp_path):
    """Intercept the loop-worker subprocess the PRODUCTION hook launches."""
    from omniagentos.scheduler import loop_jobs

    calls: list[list[str]] = []
    fake_worker = tmp_path / "loop-worker"
    fake_worker.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    monkeypatch.setattr(loop_jobs, "_loop_worker_path", lambda: fake_worker)

    report = json.dumps(
        {
            "instance_id": "w3_health_monitor",
            "template": "monitor_diagnose_repair_verify",
            "status": "idle",
            "detail": "",
            "effects": [],
            "approval_id": None,
            "resumed": False,
            "accepted": True,
        }
    )

    def fake_run(argv, **kwargs):
        calls.append(list(argv))
        return _FakeCompleted(report)

    monkeypatch.setattr(loop_jobs.subprocess, "run", fake_run)
    return calls


def _w3_row(**overrides):
    payload = {
        "name": "w3-health-monitor",
        "template": "monitor_diagnose_repair_verify",
        "instance_id": "w3_health_monitor",
        "instance_module": W3_MODULE,
        "cron": "*/10 * * * *",
        "gate_command": "pytest loops/tests/instances/test_health_monitor.py",
    }
    payload.update(overrides)
    return loop_routine_row(**payload)


def test_a_helper_built_row_reaches_the_worker_with_its_instance_module(spy_worker):
    """The regression test for the 2026-08-01 production defect.

    The row is built by the PUBLIC helper and then driven through the exact
    production path a due tick uses (``loop_jobs.run_loop_job``). The bridge's
    original tests hand-wrote the payload dict, so they could assert on a field
    the helper was incapable of producing — and did, for a whole day, while
    every live tick settled ``builtin_failed`` on missing tools.
    """
    from omniagentos.scheduler import loop_jobs

    result = loop_jobs.run_loop_job(None, task_template=_w3_row()["task_template"])

    # The spy worker reports `idle`, so the decisive evidence that the row
    # REACHED the worker is a neutral non-result — not the adverse
    # `builtin_failed` a row missing --instance-module produces.
    assert result.outcome_class == "neutral", result.notes
    assert result.stop_reason == "loop_idle_no_work", result.notes
    assert len(spy_worker) == 1
    argv = spy_worker[0]
    assert "--instance-module" in argv, (
        "the worker registers NO tools of its own: without --instance-module "
        "every tick fails on required_tools"
    )
    assert argv[argv.index("--instance-module") + 1] == W3_MODULE


def test_a_row_whose_payload_lost_its_instance_module_never_launches_a_worker(spy_worker):
    """The refusal path: an unrunnable row is refused BY NAME, not at tick time."""
    from omniagentos.scheduler import loop_jobs

    template = _w3_row()["task_template"]
    del template["input"]["instance_module"]

    result = loop_jobs.run_loop_job(None, task_template=template)

    assert result.accepted is False
    assert "instance_module" in result.notes
    assert spy_worker == [], "a row that cannot run must never spawn a process"


@pytest.mark.parametrize(
    "module",
    [
        "",
        "   ",
        "os",
        "omniagentos_loops.instances",
        "omniagentos_loops.instances.",
        "omniagentos_loops.instancesX.evil",
        "omniagentos_loops.instances..evil",
        "omniagentos_loops.instances.evil; rm -rf /",
        "omniagentos_loops.instances.Evil",
        "../../etc/passwd",
    ],
)
def test_the_helper_refuses_a_row_that_names_no_importable_instance_module(module):
    with pytest.raises(LoopSpecError):
        _w3_row(instance_module=module)


def test_instance_module_is_required_and_has_no_convention_default():
    """Deriving it from ``instance_id`` would have produced two wrong imports.

    Live rows: instance ``w3_health_monitor`` registers from
    ``...instances.health_monitor``, and instance ``w2_inbox`` from
    ``...instances.w2_inbox_triage``. A convention default silently rebuilds the
    late failure it was meant to prevent, so the parameter is REQUIRED.
    """
    with pytest.raises(TypeError):
        loop_routine_row(  # type: ignore[call-arg]
            name="loop-x",
            template="draft_approve_send",
            instance_id="cs_replies",
            cron="0 * * * *",
        )


def test_the_parsed_spec_refuses_a_module_outside_the_instances_package():
    row = _w3_row()
    row["task_template"]["input"]["instance_module"] = "os"
    with pytest.raises(LoopSpecError):
        loop_spec(row["task_template"])
