"""A loop routine seeded without an explicit gate is REFUSED at creation.

The defect, live on 2026-08-02
------------------------------
``omniagentos_loops.registry.loop_routine_row`` substituted
``DEFAULT_GATE_COMMAND = "pytest tests/scheduler/test_loop_jobs.py"`` whenever an
author omitted ``gate_command``. That suite proves the scheduler→worker
MECHANISM — the same code for every loop instance — so it passes on every tick
no matter what the loop produced. A loop tick reports its own status and the one
thing authorised to contradict that report is ``gate_config.command``, executed
by ``routines_settle`` in the gate workspace; a gate that cannot fail on the
loop's work is therefore a loop that grades itself, and
``rtn_1e5567b9f3314a2c9d76`` did exactly that: 10 runs, 10 "accepted",
acceptance 1.0, zero heals.

Where the refusal lives, and why it is not at settlement
-------------------------------------------------------
Fail-closed at SEED time, in two places that cannot disagree:

* the helper refuses to build a row without a gate (no default to substitute);
* :func:`omniagentos.scheduler.routines.validate_routine` refuses to persist
  one, so a hand-written payload, the HTTP route and a future importer all hit
  the same rule.

Settlement is deliberately untouched. A rule applied at settlement would
re-judge rows already in the database and auto-pause loops that are behaving
correctly — the failure this repo hit four times on 2026-07-31 — so the tests
below also pin that a row seeded BEFORE the refusal keeps reading, disabling and
parsing exactly as it did.

Counterfeits this must make RED
-------------------------------
- restoring the helper's silent default (any default at all);
- dropping the validator's refusal, so a machinery-gated loop row persists;
- narrowing the rule to ``input.kind == 'loop'``, which ``loop_jobs._spec``
  does not read — a row with any other ``kind`` still executes as a loop.
"""

from __future__ import annotations

import inspect
import json
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from omniagentos.db.store import SqliteStore
from omniagentos.scheduler import loop_jobs
from omniagentos.scheduler.routines import (
    LOOP_TASK_MODULE,
    RoutineValidationError,
    validate_routine,
)
from omniagentos.scheduler.store import RoutinesStore

REPO_ROOT = Path(__file__).resolve().parents[2]

#: The gate the live W3 row carries: it names the INSTANCE's own suite, so it
#: goes red when that instance's work is missing. This is the sanctioned shape.
REAL_GATE = "pytest loops/tests/instances/test_health_monitor.py"

#: The command the helper used to substitute. Spelled out rather than imported,
#: because the point of this file is that no module exports it any more.
MACHINERY_GATE = "pytest tests/scheduler/test_loop_jobs.py"


def _loops_registry() -> ModuleType:
    """The PUBLIC seeding helper, imported the way the production suite may.

    ``loops/`` is a separate package tree that is never installed into the
    production venv, but ``registry`` is import-light stdlib on purpose — "the
    row shape must be readable from any venv" — so this suite can check what the
    helper actually produces. Asserting against a hand-written payload would
    prove nothing about the path an operator uses, which is precisely how the
    missing ``instance_module`` shipped for a whole day.
    """
    loops_root = str(REPO_ROOT / "loops")
    if loops_root not in sys.path:
        sys.path.insert(0, loops_root)
    import omniagentos_loops.registry as registry

    return registry


@pytest.fixture
def store(tmp_path: Path):
    from tests.support.db_template import make_store

    db_path = str(tmp_path / "control.sqlite3")
    handle = make_store(SqliteStore, db_path)
    yield handle
    handle.close()


def _loop_row(**overrides: Any) -> dict[str, Any]:
    """A hand-written loop row — the payload shape the store must judge."""
    payload: dict[str, Any] = {
        "name": "w3-health-monitor",
        "description": "W3 health monitor loop",
        "trigger_type": "cron",
        "trigger_config": {"cron": "*/10 * * * *"},
        "task_template": {
            "title": "Loop tick: w3_health_monitor",
            "harness": "mock",
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
        "gate_config": {"command": REAL_GATE, "expected_exit_code": 0},
        "hard_cap_type": "budget_usd",
        "hard_cap_value": 5.0,
        "notification_target": {"channel": "desktop"},
        "status": "active",
    }
    payload.update(overrides)
    return payload


def _real_harness_loop_row(**overrides: Any) -> dict[str, Any]:
    """A loop row the D5 mock-harness belt has nothing to say about.

    Every loop row the helper builds is ``harness='mock'``, and the belt refuses
    an active mock row that ships no real verifier — so on a mock row the belt
    and this lane's rule both fire and the belt's message ("active routine
    cannot have harness='mock'") could be mistaken for the gate refusal. A row
    with a real harness silences the belt, which is what makes the assertions
    below attribute the refusal to the loop-gate rule and to nothing else.
    """
    row = _loop_row(**overrides)
    row["task_template"] = {**row["task_template"], "harness": "codex"}
    return row


def _insert_legacy_row(
    store: SqliteStore, routine_id: str, row: dict[str, Any], status: str = "active"
) -> None:
    """Write a row STRAIGHT to the table, the way the old helper's rows exist.

    Raw SQL on purpose: every sanctioned write path now refuses these payloads,
    so a row that predates the rule cannot be produced through one — and "the
    rows already in the database keep working" is the claim under test.
    """
    from omniagentos.contracts import utc_now_iso

    now = utc_now_iso()
    store._write(
        "INSERT INTO routines (id, name, description, trigger_type, trigger_config_json, "
        "task_template_json, gate_type, gate_config_json, hard_cap_type, hard_cap_value, "
        "notification_target_json, status, auto_pause_reason, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [
            routine_id,
            row["name"],
            row["description"],
            row["trigger_type"],
            '{"cron": "*/10 * * * *"}',
            json.dumps(row["task_template"]),
            row["gate_type"],
            json.dumps(row["gate_config"]),
            row["hard_cap_type"],
            row["hard_cap_value"],
            '{"channel": "desktop"}',
            status,
            "",
            now,
            now,
        ],
    )


# ---------------------------------------------------------------------------
# 1. the helper: nothing to substitute
# ---------------------------------------------------------------------------


def test_the_seeding_helper_has_no_default_gate_command() -> None:
    """The decisive helper assertion: an omitted gate is an ERROR, not a default.

    Signature-level, because that is the property: a parameter with a default is
    a parameter the helper can fill in silently, and a silent gate is a verdict
    the system invented. ``instance_module`` already has this contract for the
    same reason (a row it could not express failed every tick for a day).
    """
    registry = _loops_registry()

    assert not hasattr(registry, "DEFAULT_GATE_COMMAND"), (
        "a default gate command is the defect itself — it is substituted "
        "whenever an author omits one and it passes whatever the loop produced"
    )
    parameter = inspect.signature(registry.loop_routine_row).parameters["gate_command"]
    assert parameter.default is inspect.Parameter.empty, (
        f"gate_command must have no default; found {parameter.default!r}"
    )

    with pytest.raises(TypeError):
        registry.loop_routine_row(  # type: ignore[call-arg]
            name="w3-health-monitor",
            template="monitor_diagnose_repair_verify",
            instance_id="w3_health_monitor",
            instance_module="omniagentos_loops.instances.health_monitor",
            cron="*/10 * * * *",
        )


@pytest.mark.parametrize("blank", ["", "   ", None])
def test_the_helper_refuses_a_blank_gate_command_with_the_remedy(blank: Any) -> None:
    """A threaded-through empty config value must not become a gateless row."""
    registry = _loops_registry()

    with pytest.raises(registry.LoopSpecError) as excinfo:
        registry.loop_routine_row(
            name="w3-health-monitor",
            template="monitor_diagnose_repair_verify",
            instance_id="w3_health_monitor",
            instance_module="omniagentos_loops.instances.health_monitor",
            cron="*/10 * * * *",
            gate_command=blank,
        )

    message = str(excinfo.value)
    assert "gate_command" in message
    # The author is told what to declare and why, not merely that it is missing.
    assert "loops/tests/instances/test_" in message
    assert "no default" in message


# ---------------------------------------------------------------------------
# 2. the store: a machinery gate never reaches the database
# ---------------------------------------------------------------------------


def test_a_loop_row_gated_on_the_loop_machinery_is_refused_at_creation(
    store: SqliteStore,
) -> None:
    """The backstop: even hand-written, the old default cannot be persisted.

    This is what makes the helper's missing default irreversible. Restoring it
    would not restore the behaviour, because the row it builds is refused by the
    store — the refusal moves, the loop still cannot grade itself.
    """
    routines = RoutinesStore(store)

    with pytest.raises(RoutineValidationError) as excinfo:
        routines.create_routine(
            _loop_row(gate_config={"command": MACHINERY_GATE, "expected_exit_code": 0})
        )

    message = str(excinfo.value)
    assert "grades the loop machinery" in message
    assert "tests/scheduler/test_loop_jobs.py" in message
    assert "loops/tests/instances/test_" in message, "the refusal must name the remedy"
    assert routines.list_routines() == [], "a refused row must not be half-written"


@pytest.mark.parametrize(
    "command",
    [
        "pytest tests/scheduler/test_loop_jobs.py::test_one",
        "pytest ./tests/scheduler/test_loop_gate_execution.py",
        "pytest tests/scheduler",
        "python -m pytest tests/scheduler/test_loop_jobs.py",
        "pytest loops/tests/instances/test_health_monitor.py tests/scheduler/test_loop_jobs.py",
    ],
)
def test_the_machinery_refusal_is_about_the_target_not_the_spelling(
    store: SqliteStore, command: str
) -> None:
    """Node ids, ``./`` prefixes, the directory itself, ``python -m`` and a
    machinery target smuggled in beside an honest one are all the same defect."""
    routines = RoutinesStore(store)

    with pytest.raises(RoutineValidationError) as excinfo:
        routines.create_routine(
            _loop_row(gate_config={"command": command, "expected_exit_code": 0})
        )

    assert "grades the loop machinery" in str(excinfo.value)


def test_a_symlink_alias_to_the_machinery_is_refused_by_its_REAL_location(
    store: SqliteStore, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The blocker Sol found: spelling is not location.

    ``gate_runner`` resolves every target inode-anchored and symlink-following
    before executing it, so a gate spelled ``aliases/scheduler_suite.py`` that
    is a symlink into ``tests/scheduler/`` really RUNS the machinery. A
    validator that string-matched the spelling would admit exactly the gate the
    executor then treats as the machinery — two definitions of one path, which
    is this repo's settled-definition-divergence class.
    """
    checkout = tmp_path / "checkout"
    (checkout / "tests" / "scheduler").mkdir(parents=True)
    (checkout / "tests" / "scheduler" / "test_loop_jobs.py").write_text("", encoding="utf-8")
    (checkout / "aliases").mkdir()
    (checkout / "aliases" / "scheduler_suite.py").symlink_to(
        checkout / "tests" / "scheduler" / "test_loop_jobs.py"
    )
    monkeypatch.setattr("omniagentos.contracts._repo_root", lambda: str(checkout))

    with pytest.raises(RoutineValidationError) as excinfo:
        RoutinesStore(store).create_routine(
            _loop_row(
                gate_config={
                    "command": "pytest aliases/scheduler_suite.py",
                    "expected_exit_code": 0,
                }
            )
        )

    assert "grades the loop machinery" in str(excinfo.value)


@pytest.mark.parametrize(
    "command",
    [
        "pytest .",
        "pytest tests",
        "pytest ./tests",
        "pytest tests/",
        "python -m pytest .",
        "pytest loops/tests/instances/test_health_monitor.py tests",
    ],
)
def test_a_blanket_gate_over_the_whole_tree_is_refused(store: SqliteStore, command: str) -> None:
    """A gate that sweeps everything rules on nothing in particular.

    ``pytest tests`` and ``pytest .`` both CONTAIN the machinery, and both are
    green whenever the tree is green and red whenever anything anywhere is red —
    a verdict uncorrelated with what the instance did, which is the same
    property that made the seeded default a lie. Sol's judgment: the
    deliberately-scoped hole (denylist of the machinery only) is a blocker,
    because `pytest tests` is a one-word edit away from the refused spelling.
    """
    with pytest.raises(RoutineValidationError) as excinfo:
        RoutinesStore(store).create_routine(
            _loop_row(gate_config={"command": command, "expected_exit_code": 0})
        )

    assert "blanket gate" in str(excinfo.value)


def test_a_targetless_verifier_is_refused_for_a_loop(store: SqliteStore) -> None:
    """``git diff --check`` is a recognized verifier that says nothing about a loop.

    It is the one grammar-legal command with no targets at all, so it cannot be
    classified by target — and the executor refuses a targetless command on
    every tick (``gate command declares no targets``), which for a loop means an
    auto-pause after three ticks instead of a refusal at seed time.
    """
    with pytest.raises(RoutineValidationError) as excinfo:
        RoutinesStore(store).create_routine(
            _loop_row(gate_config={"command": "git diff --check", "expected_exit_code": 0})
        )

    assert "declares no targets" in str(excinfo.value)


def test_a_target_absent_from_this_checkout_is_judged_on_its_spelling(
    store: SqliteStore, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The explicit decision for a target that cannot be resolved here.

    Refusing every unresolvable target would make validation depend on which
    checkout the API happens to run from and would refuse a gate whose file
    exists only at the gate workspace's pinned commit. Accepting it unclassified
    would be fail-open. So: fall back to the SPELLED parts — every direct
    spelling is still refused, and an honest instance gate that this checkout
    has not got is still creatable.
    """
    empty = tmp_path / "empty-checkout"
    empty.mkdir()
    monkeypatch.setattr("omniagentos.contracts._repo_root", lambda: str(empty))
    routines = RoutinesStore(store)

    with pytest.raises(RoutineValidationError) as excinfo:
        routines.create_routine(
            _loop_row(gate_config={"command": MACHINERY_GATE, "expected_exit_code": 0})
        )
    assert "grades the loop machinery" in str(excinfo.value)

    created = routines.create_routine(
        _loop_row(
            gate_config={
                "command": "pytest loops/tests/instances/test_not_here_yet.py",
                "expected_exit_code": 0,
            }
        )
    )
    assert created["status"] == "active"


def test_a_loop_row_with_no_gate_command_is_refused_with_the_loop_specific_remedy(
    store: SqliteStore,
) -> None:
    """An executed gate type with nothing to execute.

    Without this rule the row is refused two layers later by the D5 mock-harness
    belt, whose message ("active routine cannot have harness='mock'") names
    neither the gate nor the fix.
    """
    routines = RoutinesStore(store)

    with pytest.raises(RoutineValidationError) as excinfo:
        routines.create_routine(_loop_row(gate_type="test_command", gate_config={}))

    message = str(excinfo.value)
    assert "must declare gate_config.command itself" in message
    assert "loops/tests/instances/test_" in message
    assert "settles every tick" in message


# ---------------------------------------------------------------------------
# 2b. the DECOY gate: a command nothing ever executes
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("harness_row", [_loop_row, _real_harness_loop_row])
def test_a_metric_threshold_loop_with_a_DECOY_command_is_refused_at_creation(
    store: SqliteStore, harness_row: Any
) -> None:
    """The original incident wearing a disguise.

    ``routines_settle`` only EXECUTES the command of a gate type in
    ``EXECUTED_GATE_TYPES``; a ``metric_threshold`` row settles with
    ``gate_passed=NULL`` no matter what string sits in ``gate_config.command``.
    So a loop carrying a perfectly honest-looking instance-test command beside a
    metric gate reads as gated in every list, every dashboard and every review —
    and grades itself forever, which is exactly the shipped defect.

    Checked on both harnesses: on a mock row the D5 belt would eventually refuse
    for an unrelated reason, and a rule that only works where another belt
    already fires is decoration.
    """
    with pytest.raises(RoutineValidationError) as excinfo:
        RoutinesStore(store).create_routine(
            harness_row(
                gate_type="metric_threshold",
                gate_config={
                    "metric": "heals",
                    "operator": ">=",
                    "threshold": 1,
                    # The decoy: valid, honest-looking, never executed.
                    "command": REAL_GATE,
                },
            )
        )

    message = str(excinfo.value)
    assert "may not use gate_type='metric_threshold'" in message
    assert "never EXECUTED at settlement" in message
    assert "loops/tests/instances/test_" in message, "the refusal must name the remedy"


def test_the_decoy_command_escape_is_closed_on_the_DAL_activation_path_too(
    store: SqliteStore,
) -> None:
    """Sol's exact reproduction: seed it disabled, then enable through the DAL.

    ``validate_routine``'s draft exemption used to be the front door and the DAL
    the back one. Both now ask the same question, so a decoy-gated loop cannot
    reach ACTIVE by either.
    """
    decoy = _real_harness_loop_row(
        gate_type="metric_threshold",
        gate_config={"metric": "heals", "operator": ">=", "threshold": 1, "command": REAL_GATE},
    )
    with pytest.raises(RoutineValidationError):
        RoutinesStore(store).create_routine({**decoy, "status": "disabled"})

    # …and even for a row that predates the rule and is already in the table.
    _insert_legacy_row(store, "rtn_decoy", decoy, status="disabled")
    routines = RoutinesStore(store)
    revision = routines.get_routine("rtn_decoy")["revision"]

    for flip in (
        lambda: routines.set_status("rtn_decoy", "active"),
        lambda: routines.set_status_cas("rtn_decoy", "active", expected_revision=revision),
    ):
        with pytest.raises(RoutineValidationError) as excinfo:
            flip()
        assert "never EXECUTED at settlement" in str(excinfo.value)

    assert routines.get_routine("rtn_decoy")["status"] == "disabled"


@pytest.mark.parametrize("command", ["echo ok", "pytest --collect-only tests", "ls -la"])
def test_a_command_no_verifier_grammar_recognizes_is_refused_for_a_loop(
    store: SqliteStore, command: str
) -> None:
    """``echo ok`` used to reach ACTIVE through disabled-then-enable.

    ``loop_gate_errors`` used to leave unrecognized commands to
    ``_validate_gate`` — which the draft exemption returns before, and which
    neither DAL activation belt calls. An unrecognised verifier is a fact about
    the candidate that the EXECUTOR also refuses, so the write side says it too.
    """
    row = _real_harness_loop_row(gate_config={"command": command, "expected_exit_code": 0})
    routines = RoutinesStore(store)

    with pytest.raises(RoutineValidationError) as excinfo:
        routines.create_routine({**row, "status": "disabled"})
    assert "not a recognized objective verifier" in str(excinfo.value)

    _insert_legacy_row(store, "rtn_echo", row, status="disabled")
    with pytest.raises(RoutineValidationError) as excinfo:
        routines.set_status("rtn_echo", "active")
    assert "not a recognized objective verifier" in str(excinfo.value)


def test_the_executed_gate_type_set_is_the_settlers_own(store: SqliteStore) -> None:
    """One definition, asserted as one — not two sets that happen to agree.

    If a gate type is ever added to the settler's executed set, this rule admits
    it the same day; a hand-copied frozenset here would have gone stale silently,
    which is the drift class the whole lane is about.
    """
    from omniagentos.scheduler import routines as routines_module
    from omniagentos.scheduler.routines_settle import EXECUTED_GATE_TYPES

    assert routines_module._executed_gate_types() is EXECUTED_GATE_TYPES
    for gate_type in EXECUTED_GATE_TYPES - {"merge_candidate"}:
        assert (
            routines_module.loop_gate_errors(
                _loop_row()["task_template"], gate_type, {"command": REAL_GATE}
            )
            == []
        )


def test_the_refusal_is_keyed_on_the_loop_MODULE_not_on_input_kind(
    store: SqliteStore,
) -> None:
    """``loop_jobs._spec`` dispatches on ``input.module`` and never reads
    ``kind``, so a rule keyed on ``kind`` would refuse nothing: ``kind='task'``
    would sail past the gate check and still launch a loop worker."""
    routines = RoutinesStore(store)
    row = _loop_row(gate_config={"command": MACHINERY_GATE, "expected_exit_code": 0})
    row["task_template"]["input"]["kind"] = "definitely-not-a-loop"

    with pytest.raises(RoutineValidationError) as excinfo:
        routines.create_routine(row)

    assert "grades the loop machinery" in str(excinfo.value)
    assert loop_jobs._spec(row["task_template"]) is not None, (
        "the row this test refuses must really be one the loop runtime would run"
    )


def test_the_machinery_gate_is_refused_by_the_loop_rule_not_by_the_D5_belt(
    store: SqliteStore,
) -> None:
    """Attribution matters: a test that a belt happens to satisfy is decoration."""
    with pytest.raises(RoutineValidationError) as excinfo:
        RoutinesStore(store).create_routine(
            _real_harness_loop_row(gate_config={"command": MACHINERY_GATE, "expected_exit_code": 0})
        )

    message = str(excinfo.value)
    assert "grades the loop machinery" in message
    assert "harness" not in message, "the D5 belt must not be what refused this row"


def test_an_update_cannot_swap_an_honest_loop_gate_for_the_machinery_suite(
    store: SqliteStore,
) -> None:
    """Creation is not the only write path; ``update_routine`` re-validates the
    MERGED row against its MERGED status, so the seed-time rule cannot be undone
    by an edit."""
    routines = RoutinesStore(store)
    created = routines.create_routine(_real_harness_loop_row())

    with pytest.raises(RoutineValidationError) as excinfo:
        routines.update_routine(
            created["id"],
            {"gate_config": {"command": MACHINERY_GATE, "expected_exit_code": 0}},
        )

    assert "grades the loop machinery" in str(excinfo.value)
    assert routines.get_routine(created["id"])["gate_config"]["command"] == REAL_GATE


# ---------------------------------------------------------------------------
# 3. the sanctioned path still works
# ---------------------------------------------------------------------------


def test_the_sanctioned_helper_built_loop_row_still_creates(store: SqliteStore) -> None:
    """The regression half: refusing gateless loops must not refuse loops.

    The documented seeding idiom is ``harness='mock'`` plus a ``test_command``
    gate, admitted by ``create_routine``'s ``allow_mock_with_real_gate``
    exemption (the D5 belt refuses an active mock row without a real verifier).
    A row built by the real helper, with an explicit gate, must still land
    ACTIVE — otherwise this lane has broken every loop instead of the bad ones.
    """
    registry = _loops_registry()
    row = registry.loop_routine_row(
        name="w3-health-monitor",
        template="monitor_diagnose_repair_verify",
        instance_id="w3_health_monitor",
        instance_module="omniagentos_loops.instances.health_monitor",
        cron="*/10 * * * *",
        gate_command=REAL_GATE,
    )

    validate_routine(row)
    created = RoutinesStore(store).create_routine(row)

    assert created["status"] == "active"
    assert created["task_template"]["harness"] == "mock"
    assert created["gate_config"]["command"] == REAL_GATE
    assert created["purpose"] == "loop"


def test_the_repos_own_w3_seed_payload_is_still_creatable(store: SqliteStore) -> None:
    """The one loop this repository seeds on API startup, through its own path."""
    _loops_registry()
    from omniagentos.scheduler.routines import w3_health_monitor_routine

    created = RoutinesStore(store).create_routine(w3_health_monitor_routine())

    assert created["status"] == "active"
    assert created["gate_config"]["command"] == REAL_GATE


def test_a_non_loop_routine_may_still_gate_on_the_scheduler_suite(
    store: SqliteStore,
) -> None:
    """The rule is about loops, not about ``tests/scheduler/``.

    A routine whose WORK is the scheduler (the lab-job drain, the dispatcher)
    is properly gated by the scheduler's own suite: that gate really can go red
    on what the routine does. Refusing it here would be a rule about paths
    rather than about self-grading.
    """
    row = _loop_row(
        name="lab-jobs-drain",
        gate_config={"command": MACHINERY_GATE, "expected_exit_code": 0},
    )
    row["task_template"] = {
        "title": "Lab job drain tick",
        "harness": "mock",
        "input": {"module": "omniagentos.lab.jobs"},
    }

    created = RoutinesStore(store).create_routine(row)

    assert created["gate_config"]["command"] == MACHINERY_GATE


# ---------------------------------------------------------------------------
# 4. live rows are not re-judged
# ---------------------------------------------------------------------------


def test_a_loop_row_seeded_before_the_refusal_still_reads_disables_and_parses(
    store: SqliteStore,
) -> None:
    """Refusal is a WRITE-side rule. Existing rows must not brick.

    Applying it to rows already in the database would settle or block loops that
    were seeded legitimately under the old contract — and a loop that cannot be
    read or disabled cannot even be retired by an operator. So a legacy row
    (inserted here the way the old helper would have) must still load, still
    hand ``loop_jobs`` a runnable spec, and still be disable-able; only a new
    WRITE of its shape is refused.
    """
    _insert_legacy_row(
        store,
        "rtn_legacy_gateless",
        _loop_row(gate_config={"command": MACHINERY_GATE, "expected_exit_code": 0}),
    )
    routines = RoutinesStore(store)

    loaded = routines.get_routine("rtn_legacy_gateless")
    assert loaded is not None
    assert loaded["gate_config"]["command"] == MACHINERY_GATE
    # The settlement/execution side never consults the new rule.
    assert loop_jobs._spec(loaded["task_template"])["instance_id"] == "w3_health_monitor"
    # And an operator can still retire it.
    assert routines.set_status("rtn_legacy_gateless", "disabled")["status"] == "disabled"


# ---------------------------------------------------------------------------
# 4b. the disabled-then-enable escape (Sol finding 3)
# ---------------------------------------------------------------------------


def test_a_gateless_loop_row_is_refused_even_as_a_DISABLED_draft(store: SqliteStore) -> None:
    """The rule is about the row's shape, not about its lifecycle.

    The draft exemption exists so a COMPOSER draft can be saved before it has
    chosen a trigger or a gate. It is not a licence to declare "my work is one
    tick of the loop runtime" and stay unjudgeable: a row that has chosen THAT
    much has chosen enough to say how the tick would be graded, and keying the
    loop rule on status meant the whole rule could be skipped by seeding
    disabled and enabling afterwards.
    """
    routines = RoutinesStore(store)
    row = _loop_row(status="disabled")
    row.pop("gate_config")
    row["gate_type"] = "exit_code"

    with pytest.raises(RoutineValidationError) as excinfo:
        routines.create_routine(row)

    assert "must declare gate_config.command itself" in str(excinfo.value)
    assert routines.list_routines() == []


def test_a_composer_draft_that_has_chosen_no_work_is_still_exempt(store: SqliteStore) -> None:
    """The draft exemption survives: no task_template means no loop, means no rule."""
    created = RoutinesStore(store).create_routine({"name": "draft", "status": "disabled"})

    assert created["status"] == "disabled"


@pytest.mark.parametrize("cas", [False, True])
def test_a_legacy_gateless_loop_row_cannot_be_flipped_ACTIVE_through_the_DAL(
    store: SqliteStore, cas: bool
) -> None:
    """``set_status`` does not re-validate shape — and that was the escape.

    A row seeded before the rule (or straight into the table) keeps every
    read-side behaviour, but it may not be made ACTIVE again until it declares a
    gate that can judge it. Both DAL flips are guarded: the plain one used by
    the disable route and by auto-pause recovery, and the CAS one the enable
    path uses.
    """
    gateless = _real_harness_loop_row(gate_type="exit_code", gate_config={"expected_exit_code": 0})
    _insert_legacy_row(store, "rtn_legacy_draft", gateless, status="disabled")
    routines = RoutinesStore(store)
    revision = routines.get_routine("rtn_legacy_draft")["revision"]

    with pytest.raises(RoutineValidationError) as excinfo:
        if cas:
            routines.set_status_cas("rtn_legacy_draft", "active", expected_revision=revision)
        else:
            routines.set_status("rtn_legacy_draft", "active")

    assert "must declare gate_config.command itself" in str(excinfo.value)
    assert routines.get_routine("rtn_legacy_draft")["status"] == "disabled"


def test_the_mock_harness_row_is_refused_by_the_older_belt_on_the_CAS_path(
    store: SqliteStore,
) -> None:
    """Ordering note, pinned so it cannot rot into a silent gap.

    Every helper-built loop row is ``harness='mock'``, and on the CAS path the
    D5 belt speaks first with its own message. The row is refused either way;
    the test above uses a real harness so the LOOP belt is provably the thing
    doing the refusing rather than a belt that would have refused anyway.
    """
    _insert_legacy_row(
        store,
        "rtn_legacy_mock",
        _loop_row(gate_type="exit_code", gate_config={"expected_exit_code": 0}),
        status="disabled",
    )
    routines = RoutinesStore(store)
    revision = routines.get_routine("rtn_legacy_mock")["revision"]

    with pytest.raises(RoutineValidationError):
        routines.set_status_cas("rtn_legacy_mock", "active", expected_revision=revision)

    assert routines.get_routine("rtn_legacy_mock")["status"] == "disabled"


def test_a_gated_loop_row_can_still_be_enabled_through_the_DAL(store: SqliteStore) -> None:
    """The belt refuses gateless loops, never gated ones (and never non-loops)."""
    _insert_legacy_row(store, "rtn_gated", _loop_row(), status="disabled")
    routines = RoutinesStore(store)

    assert routines.set_status("rtn_gated", "active")["status"] == "active"


# ---------------------------------------------------------------------------
# 5. the three copies of the loop module string
# ---------------------------------------------------------------------------


def test_the_loop_module_constant_agrees_across_every_copy() -> None:
    """Three modules in two venvs name the loop runtime; a drift is a bypass.

    ``routines`` decides which rows the gate rule applies to, ``loop_jobs``
    decides which rows launch a worker, and ``registry`` decides which rows the
    helper builds. If the strings ever diverge, a row runs as a loop while being
    validated as something else — the refusal would simply stop firing, quietly.
    """
    registry = _loops_registry()

    assert LOOP_TASK_MODULE == loop_jobs.LOOP_MODULE == registry.LOOP_MODULE
