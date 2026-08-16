"""LOOPS Phase 1: the routines tick must actually LAUNCH a loop worker.

Decisive assertion
------------------
Drive ``routines_tick.tick`` — the production entry the launchd job runs — with
a real ``kind='loop'`` routine row and observe (a) that a worker subprocess was
launched with the row's template/instance/params on argv, and (b) that the
resulting ``routine_runs`` row records the worker's CLAIM in the right class —
never as a verdict, which only an executed gate can write (see
``tests/scheduler/test_loop_gate_execution.py``). Asserting that
``run_loop_job`` works in isolation would prove the function exists and say
nothing about whether the scheduler calls it.

Counterfeits this must make RED
-------------------------------
- registering the loop module but leaving ``builtin_for`` template-blind (the
  worker would be launched with no template and could not know what to run);
- scoring a ``parked`` tick as unfavourable (auto-pauses every human-gated loop
  after two ticks — the non-result-as-unfavourable defect class, four prior
  incidents);
- scoring a ``parked`` or ``idle`` tick as FAVOURABLE (the opposite defect: a
  loop that parks the same approval every tick and heals nothing reports 100%
  acceptance, so it can never trip the floor and a dead loop is
  indistinguishable from a working one);
- accepting an unvalidated instance/template name from the row into argv.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

import pytest

from omniagentos.db.store import SqliteStore
from omniagentos.policy import load_policy
from omniagentos.scheduler import loop_jobs
from omniagentos.scheduler.builtin_jobs import BUILTIN_JOBS, builtin_for
from omniagentos.scheduler.routines_tick import tick
from omniagentos.scheduler.store import RoutinesStore
from tests.support.db_template import make_store

DUE_NOW = datetime(2026, 8, 1, 12, 0, tzinfo=UTC)


def _loop_routine(**overrides: Any) -> dict[str, Any]:
    payload = {
        "name": "loop-inbox-triage",
        "description": "Inbox triage loop",
        "trigger_type": "cron",
        "trigger_config": {"cron": "*/5 * * * *"},
        "task_template": {
            "title": "Loop tick: w2_inbox_triage",
            "harness": "mock",
            "input": {
                "module": loop_jobs.LOOP_MODULE,
                "kind": "loop",
                "template": "poll_classify_act_verify",
                "instance_id": "w2_inbox_triage",
                # The module whose register(ctx) supplies this instance's tools.
                # The worker ships none, so a row without this cannot pass any
                # template's required_tools check (see the helper-built tests at
                # the bottom: this fixture's hand-written payload is exactly how
                # the missing field went unnoticed in production).
                "instance_module": "omniagentos_loops.instances.w2_inbox_triage",
                "params": {"mailbox": "support"},
            },
        },
        # A loop row must ship a non-vacuous verifier command: the D5 store belt
        # refuses an ACTIVE harness='mock' row without one. This command IS
        # executed now — settlement runs it in the gate workspace and writes the
        # verdict — which is why this file pins the workspace off: it is
        # asserting how a claim is recorded, not how a gate rules.
        #
        # It names the INSTANCE's own suite, not this file. Gating a loop on
        # `pytest tests/scheduler/test_loop_jobs.py` (what this fixture used to
        # do, and what the seeding helper used to substitute) is now refused at
        # creation: that suite grades the scheduler→worker mechanism shared by
        # every loop, so it passes whatever the instance produced. See
        # ``tests/scheduler/test_loop_gate_refusal.py``.
        "gate_type": "test_command",
        "gate_config": {
            "command": "pytest loops/tests/instances/test_w2_inbox_triage.py",
            "expected_exit_code": 0,
        },
        "hard_cap_type": "budget_usd",
        "hard_cap_value": 5.0,
        "notification_target": {"channel": "desktop"},
        "status": "active",
    }
    payload.update(overrides)
    return payload


@pytest.fixture(autouse=True)
def _no_gate_workspace(monkeypatch: pytest.MonkeyPatch) -> None:
    """This file is about the CLAIM half; the gate half has its own file.

    ``tick`` settles at the end of every call, and settlement executes the
    routine's declared gate when ``OMNIAGENTOS_GATE_WORKSPACE`` resolves. On an
    operator box that variable is exported by ``scripts/launch-env.sh``, so
    without this pin the same test would assert one thing on CI and another on
    the machine the loops actually run on — and the ambient one would really
    execute pytest inside a worktree. Executed-gate behaviour is asserted, with
    a real workspace, in ``tests/scheduler/test_loop_gate_execution.py``.
    """
    monkeypatch.delenv("OMNIAGENTOS_GATE_WORKSPACE", raising=False)


@pytest.fixture
def store(tmp_path):
    db_path = str(tmp_path / "control.sqlite3")
    handle = make_store(SqliteStore, db_path)
    yield handle
    handle.close()


class _FakeCompleted:
    def __init__(self, stdout: str, returncode: int = 0, stderr: str = "") -> None:
        self.stdout = stdout
        self.returncode = returncode
        self.stderr = stderr


def _worker_report(status: str, detail: str = "") -> str:
    return json.dumps(
        {
            "instance_id": "w2_inbox_triage",
            "template": "poll_classify_act_verify",
            "status": status,
            "detail": detail,
            "effects": [],
            "approval_id": None,
            "resumed": False,
            "accepted": True,
        }
    )


@pytest.fixture
def spy_worker(monkeypatch, tmp_path):
    """Intercept the subprocess and pretend the worker binary exists."""
    calls: list[list[str]] = []
    outcome = {"status": "parked", "rc": 0}

    fake_worker = tmp_path / "loop-worker"
    fake_worker.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    monkeypatch.setattr(loop_jobs, "_loop_worker_path", lambda: fake_worker)

    envs: list[dict] = []

    def fake_run(argv, **kwargs):
        calls.append(list(argv))
        envs.append(dict(kwargs.get("env") or {}))
        return _FakeCompleted(_worker_report(str(outcome["status"])), int(outcome["rc"]))

    monkeypatch.setattr(loop_jobs.subprocess, "run", fake_run)
    return calls, outcome, envs


def _tick(store):
    return tick(store, load_policy(), now=DUE_NOW)


# --------------------------------------------------------------------------
# decisive: the scheduler launches the worker with the ROW's instruction
# --------------------------------------------------------------------------


def test_a_due_loop_routine_launches_the_worker_with_its_template_and_params(store, spy_worker):
    calls, _, _envs = spy_worker
    routines = RoutinesStore(store)
    routines.create_routine(_loop_routine())

    result = _tick(store)

    assert len(calls) == 1, "the tick must launch exactly one loop worker"
    argv = calls[0]
    assert "--template" in argv and argv[argv.index("--template") + 1] == "poll_classify_act_verify"
    assert "--instance" in argv and argv[argv.index("--instance") + 1] == "w2_inbox_triage"
    assert json.loads(argv[argv.index("--params") + 1]) == {"mailbox": "support"}
    fired = [entry for entry in result["fired"] if entry.get("fired")]
    assert fired and fired[0]["builtin"] == loop_jobs.LOOP_MODULE


def test_a_parked_tick_is_recorded_as_neither_accepted_nor_rejected(store, spy_worker):
    """A loop waiting on a human is a NON-RESULT: neutral, not accepted.

    Both booleans are wrong here. ``0`` auto-pauses a loop for behaving
    correctly (four incidents on 2026-07-31); ``1`` is what let
    rtn_1e5567b9f3314a2c9d76 report acceptance 1.0 across two ticks that parked
    the same approval and healed nothing. NULL/NULL is the honest row, and it
    is the same shape the settlement layer already writes for evidence-absence.
    """
    routines = RoutinesStore(store)
    routines.create_routine(_loop_routine())

    _tick(store)

    rows = routines.list_runs(routines.list_routines()[0]["id"])
    assert len(rows) == 1
    assert rows[0]["accepted"] is None, "parking for a human is not an acceptance"
    assert rows[0]["gate_passed"] is None, "no gate ruled on a parked tick"
    assert rows[0]["outcome_class"] == "neutral"
    # WHICH non-result it was must be queryable, not buried in prose.
    assert rows[0]["stop_reason"] == "loop_parked_awaiting_human"
    assert "parked" in rows[0]["notes"]


@pytest.mark.parametrize(
    ("status", "stop_reason"),
    [
        ("parked", "loop_parked_awaiting_human"),
        ("idle", "loop_idle_no_work"),
    ],
)
def test_non_result_statuses_are_neutral_and_distinguishable(
    store, spy_worker, status, stop_reason
):
    """`parked` and `idle` are both neutral — and are told APART in the data.

    "Waiting on a human" and "nothing to do" call for different operator
    responses; collapsing them into one bucket is what made a stalled loop
    unreadable.
    """
    calls, outcome, _envs = spy_worker
    outcome["status"] = status
    routines = RoutinesStore(store)
    routines.create_routine(_loop_routine())
    _tick(store)
    rows = routines.list_runs(routines.list_routines()[0]["id"])
    assert rows[0]["accepted"] is None
    assert rows[0]["outcome_class"] == "neutral"
    assert rows[0]["stop_reason"] == stop_reason


def test_a_completed_tick_is_the_only_favourable_one(store, spy_worker):
    """`completed` is the only claim that can EVER become favourable...

    ...and on its own it still is not one. With no gate workspace configured
    there is no executed evidence, so a loop reporting ``completed`` settles
    UNCORROBORATED — NULL/NULL, neutral, out of the acceptance denominator —
    rather than accepted. That is the point of the whole lane: a self-report
    can lower a verdict and can never raise one, so acceptance requires
    evidence the loop did not author.

    ``tests/scheduler/test_loop_gate_execution.py`` is the other half: the same
    claim, with the routine's declared gate actually executed, settles
    favourable when the gate passes and ADVERSE when it fails.
    """
    calls, outcome, _envs = spy_worker
    outcome["status"] = "completed"
    routines = RoutinesStore(store)
    routines.create_routine(_loop_routine())
    _tick(store)
    rows = routines.list_runs(routines.list_routines()[0]["id"])
    assert rows[0]["accepted"] is None, "a claim with no executed gate is not an acceptance"
    assert rows[0]["gate_passed"] is None, "no gate ran, so no gate verdict may be recorded"
    assert rows[0]["outcome_class"] == "neutral"
    assert rows[0]["stop_reason"] == "gate_evidence_unavailable"
    # The claim itself is durable and queryable — it is simply not the verdict.
    assert rows[0]["self_reported_status"] == "completed"


@pytest.mark.parametrize(
    ("status", "stop_reason"),
    [
        ("aborted", "loop_aborted"),
        ("failed", "loop_failed"),
        # A system-owned inability to proceed (dead credential, revoked grant).
        # It does no work, exactly like `idle` — and must NOT be scored like it,
        # because this one the system can act on.
        ("blocked", "loop_blocked"),
    ],
)
def test_refusing_failing_and_blocked_ticks_count_against_the_floor(
    store, spy_worker, status, stop_reason
):
    calls, outcome, _envs = spy_worker
    outcome["status"] = status
    routines = RoutinesStore(store)
    routines.create_routine(_loop_routine())
    _tick(store)
    rows = routines.list_runs(routines.list_routines()[0]["id"])
    assert rows[0]["accepted"] == 0
    assert rows[0]["gate_passed"] == 0
    assert rows[0]["outcome_class"] == "adverse"
    assert rows[0]["stop_reason"] == stop_reason


def test_an_unrecognised_worker_status_is_adverse_not_neutral(store, spy_worker):
    """Fail closed on a status this scheduler has never heard of.

    Neutral would mean a worker that starts emitting an unknown status silently
    stops being judged — the same invisibility the taxonomy exists to close.
    """
    calls, outcome, _envs = spy_worker
    outcome["status"] = "quantum_superposition"
    routines = RoutinesStore(store)
    routines.create_routine(_loop_routine())
    _tick(store)
    rows = routines.list_runs(routines.list_routines()[0]["id"])
    assert rows[0]["accepted"] == 0
    assert rows[0]["outcome_class"] == "adverse"
    assert rows[0]["stop_reason"] == "loop_status_unrecognized"


# --------------------------------------------------------------------------
# the hook itself
# --------------------------------------------------------------------------


def test_builtin_for_binds_the_task_template_for_loop_rows():
    template = _loop_routine()["task_template"]
    job = builtin_for(template)
    assert job is not None
    assert getattr(job, "keywords", {}).get("task_template") == template


def test_builtin_for_leaves_other_jobs_unbound():
    job = builtin_for({"input": {"module": "omniagentos.memlife.dream"}})
    assert job is not None
    assert not hasattr(job, "keywords")


def test_the_registry_key_matches_loop_jobs_module_constant():
    assert loop_jobs.LOOP_MODULE in BUILTIN_JOBS


@pytest.mark.parametrize(
    "bad_input",
    [
        {"template": "../../etc/passwd", "instance_id": "x"},
        {"template": "poll_classify_act_verify", "instance_id": "--help"},
        {"template": "poll_classify_act_verify", "instance_id": "a; rm -rf /"},
        {"template": "", "instance_id": "x"},
    ],
)
def test_unsafe_names_never_reach_argv(spy_worker, bad_input):
    calls, _, _envs = spy_worker
    template = {
        "input": {
            "module": loop_jobs.LOOP_MODULE,
            "instance_module": "omniagentos_loops.instances.w2_inbox_triage",
            **bad_input,
        }
    }
    result = loop_jobs.run_loop_job(None, task_template=template)
    assert result.accepted is False
    assert "invalid loop routine" in result.notes
    assert calls == [], "a malformed row must never launch a process"


@pytest.mark.parametrize(
    "module",
    [
        "os",
        "omniagentos_loops.instances",
        "omniagentos_loops.instances.",
        "omniagentos_loops.instancesX.evil",
        "omniagentos_loops.instances.a b",
        "omniagentos_loops.instances.Evil",
    ],
)
def test_an_instance_module_outside_the_instances_package_is_refused(spy_worker, module):
    calls, _, _envs = spy_worker
    template = _loop_routine()["task_template"]
    template["input"]["instance_module"] = module
    result = loop_jobs.run_loop_job(None, task_template=template)
    assert result.accepted is False
    assert calls == []


# --------------------------------------------------------------------------
# instance_module: the seam between the seeding helper and the worker
# --------------------------------------------------------------------------


def _helper_built_loop_row(**overrides: Any) -> dict[str, Any]:
    """A row built by the PUBLIC seeding helper, not by hand.

    ``loops/`` is a separate package tree (it is never installed into the
    production venv, and its runtime needs LangGraph), but ``registry`` is
    import-light stdlib on purpose — "the row shape must be readable from any
    venv" — so the production suite can and MUST check what the helper actually
    produces. Every hand-written payload in this file is a fixture the helper
    could not have generated; that gap is what let a loop routine ship without
    an instance module and fail every ten-minute tick.
    """
    import sys
    from pathlib import Path

    loops_root = str(Path(__file__).resolve().parents[2] / "loops")
    if loops_root not in sys.path:
        sys.path.insert(0, loops_root)
    from omniagentos_loops.registry import loop_routine_row

    payload: dict[str, Any] = {
        "name": "w3-health-monitor",
        "template": "monitor_diagnose_repair_verify",
        "instance_id": "w3_health_monitor",
        "instance_module": "omniagentos_loops.instances.health_monitor",
        "cron": "*/10 * * * *",
        "gate_command": "pytest loops/tests/instances/test_health_monitor.py",
    }
    payload.update(overrides)
    return loop_routine_row(**payload)


def test_a_row_from_the_seeding_helper_carries_its_instance_module_to_argv(store, spy_worker):
    """The regression test for the 2026-08-01 production defect.

    Seed a row through the public helper, register it in the real store, and let
    the real ``routines_tick`` fire it: the flag the worker needs must be on
    argv. Asserting this against a hand-written ``task_template`` proves nothing
    about the path an operator actually uses.
    """
    calls, outcome, _envs = spy_worker
    outcome["status"] = "idle"
    routines = RoutinesStore(store)
    routines.create_routine(_helper_built_loop_row())

    _tick(store)

    assert len(calls) == 1
    argv = calls[0]
    assert "--instance-module" in argv, (
        "the loop worker registers no tools of its own; without this flag the "
        "tick fails on 'instance is missing required tools'"
    )
    assert argv[argv.index("--instance-module") + 1] == "omniagentos_loops.instances.health_monitor"
    rows = routines.list_runs(routines.list_routines()[0]["id"])
    # The worker was reached and reported for itself: an idle tick is a neutral
    # non-result, NOT the adverse 'builtin_failed' the old defect produced.
    assert rows[0]["outcome_class"] == "neutral", rows[0]["notes"]
    assert rows[0]["stop_reason"] == "loop_idle_no_work", rows[0]["notes"]


def test_a_loop_row_without_an_instance_module_is_refused_before_the_spawn(spy_worker):
    """Loud and early: refuse the row, do not spawn a worker that must fail."""
    calls, _, _envs = spy_worker
    template = _loop_routine()["task_template"]
    del template["input"]["instance_module"]

    result = loop_jobs.run_loop_job(None, task_template=template)

    assert result.accepted is False
    assert "instance_module" in result.notes
    assert calls == [], "a row that cannot possibly run must never launch a process"


def test_a_missing_loops_runtime_reports_instead_of_raising(monkeypatch, tmp_path):
    monkeypatch.setattr(loop_jobs, "_loop_worker_path", lambda: tmp_path / "absent")
    result = loop_jobs.run_loop_job(None, task_template=_loop_routine()["task_template"])
    assert result.accepted is False
    assert "not installed" in result.notes


def test_a_timeout_is_reported_not_raised(monkeypatch, tmp_path, spy_worker):
    import subprocess

    def boom(*args, **kwargs):
        raise subprocess.TimeoutExpired(cmd="loop-worker", timeout=1)

    monkeypatch.setattr(loop_jobs.subprocess, "run", boom)
    result = loop_jobs.run_loop_job(None, task_template=_loop_routine()["task_template"])
    assert result.accepted is False
    assert "exceeded" in result.notes


def test_unparseable_worker_output_is_reported_not_raised(monkeypatch, spy_worker):
    monkeypatch.setattr(
        loop_jobs.subprocess,
        "run",
        lambda *a, **k: _FakeCompleted("not json", 1, "traceback..."),
    )
    result = loop_jobs.run_loop_job(None, task_template=_loop_routine()["task_template"])
    assert result.accepted is False
    assert "rc=1" in result.notes


def test_a_non_loop_template_is_not_claimed():
    assert (
        loop_jobs.run_loop_job(None, task_template={"input": {"module": "other"}}).accepted is False
    )
    assert builtin_for({"input": {"module": "other"}}) is None


# --------------------------------------------------------------------------
# the process boundary is a SECURITY boundary
# --------------------------------------------------------------------------


def test_the_worker_environment_is_scrubbed_not_inherited(store, spy_worker, monkeypatch):
    """No credential may cross into the loop venv.

    The scheduler is launched by scripts/launch-env.sh and therefore holds all
    of connections.env. Inheriting that wholesale would make the separate venv a
    boundary in name only — the worker would be one os.environ read away from
    every payment rail and provider key in the system.
    """
    calls, _outcome, envs = spy_worker
    secrets = {
        "STRIPE_API_KEY": "sk_live_x",
        "PIEDPIPER_ACMEUNI_TOKEN": "tok",
        "SLACK_WEBHOOK_URL": "https://hooks.slack.com/services/secret",
        "OPERATOR_TOKEN": "op",
        "KNOWLEDGE_ADMIN_DSN": "postgres://u:p@h/db",
        "AWS_SECRET_ACCESS_KEY": "shh",
    }
    for name, value in secrets.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv("OMNIAGENTOS_LOOPS_ROOT", "/tmp/loops-root")

    routines = RoutinesStore(store)
    routines.create_routine(_loop_routine())
    _tick(store)

    assert len(envs) == 1
    passed = envs[0]
    for name, value in secrets.items():
        assert name not in passed, f"{name} leaked into the loop worker"
        assert value not in passed.values(), f"the VALUE of {name} leaked"
    # ...while the non-secret pointers the worker needs do survive.
    assert passed["OMNIAGENTOS_LOOPS_ROOT"] == "/tmp/loops-root"
    assert passed.get("PATH")
    assert passed.get("HOME")


def test_credential_shaped_names_admitted_by_a_prefix_rule_are_denied_here(
    store, spy_worker, monkeypatch
):
    """The shared scrub's PREFIX rules cannot see what follows the prefix.

    ``adapters.common._scrubbed_env`` keeps ``XDG_*`` (config-dir pointers) and
    ``OMNIAGENTOS_BRIDGE_SESSION*`` (the hook plumbing's session id) wholesale,
    and its force-deny list has no bare ``AUTH`` entry — only ``API_KEY`` /
    ``ACCESS_KEY`` / ``PRIVATE_KEY`` / ``SESSION_KEY``. Both names below were
    probe-verified reaching the loop worker. The post-filter is deliberately at
    THIS boundary and not in the shared adapter, which other spawn paths depend
    on unchanged.
    """
    calls, _outcome, envs = spy_worker
    smuggled = {
        "XDG_AUTH": "xdg-auth-value",
        "OMNIAGENTOS_BRIDGE_SESSION_AUTH": "bridge-auth-value",
    }
    for name, value in smuggled.items():
        monkeypatch.setenv(name, value)

    routines = RoutinesStore(store)
    routines.create_routine(_loop_routine())
    _tick(store)

    passed = envs[0]
    for name, value in smuggled.items():
        assert name not in passed, f"{name} reached the loop worker"
        assert value not in passed.values(), f"the VALUE of {name} reached the loop worker"
    assert passed.get("PATH") and passed.get("HOME"), "the post-filter must not gut hygiene vars"


def test_no_variable_the_worker_receives_is_credential_shaped(store, spy_worker, monkeypatch):
    """The invariant, stated over the whole env rather than over named probes."""
    calls, _outcome, envs = spy_worker
    monkeypatch.setenv("XDG_SESSION_TOKEN", "t")
    monkeypatch.setenv("LC_SECRET_THING", "s")
    routines = RoutinesStore(store)
    routines.create_routine(_loop_routine())
    _tick(store)

    offenders = [name for name in envs[0] if loop_jobs._is_credential_shaped(name)]
    assert not offenders, offenders


def test_the_loops_boundary_denies_what_a_regressed_shared_scrub_admits(monkeypatch):
    """The loops post-filter is pinned on its OWN, not through the shared scrub.

    ``_worker_env`` is defence in depth: ``adapters.common._scrubbed_env``
    allowlists, and THEN this module sweeps every remaining credential-shaped
    name. Once the shared scrub grew its own post-prefix shape closure, the two
    probes above stopped distinguishing the layers — ``XDG_AUTH`` and
    ``OMNIAGENTOS_BRIDGE_SESSION_AUTH`` are now dropped one level down, so
    deleting the loops sweep entirely left every loop test green (counterfeit
    ``loops-worker-credential-shaped-env``, which survived on exactly that).

    So this test neutralises the shared layer's contribution: it replaces
    ``_scrubbed_env`` with what that function ACTUALLY returned before the
    closure landed — prefix-admitted names, probe-verified reaching the worker —
    and asserts the loops boundary still denies them. That is the only way to
    observe the second layer, because the two share one shape predicate; it is
    also precisely the regression the sweep is retained for ("any future source
    of worker env values"). Delete the sweep and this goes red on its own.
    """
    from omniagentos.adapters import common

    smuggled = {
        "XDG_AUTH": "xdg-auth-value",
        "OMNIAGENTOS_BRIDGE_SESSION_AUTH": "bridge-auth-value",
    }
    hygiene = {"PATH": "/usr/bin:/bin", "HOME": "/home/loops", "LANG": "en_US.UTF-8"}

    def _pre_closure_scrub(base: dict[str, str] | None = None) -> dict[str, str]:
        # The allowlist WITHOUT the post-prefix shape check: XDG_* and
        # OMNIAGENTOS_BRIDGE_SESSION* kept wholesale, as they once were.
        return {**hygiene, **smuggled}

    # _worker_env imports the helper from the module at call time, so patching
    # the module attribute is what the production code will resolve.
    monkeypatch.setattr(common, "_scrubbed_env", _pre_closure_scrub)

    env = loop_jobs._worker_env()

    for name, value in smuggled.items():
        assert name not in env, f"{name} reached the loop worker: the loops-side filter is gone"
        assert value not in env.values(), f"the VALUE of {name} reached the loop worker"
    for name, value in hygiene.items():
        assert env[name] == value, "the loops-side filter must not gut the hygiene vars"


def test_the_enumerated_pointers_are_themselves_not_credential_shaped():
    """The exemption cannot become the smuggling route.

    ``_WORKER_ENV_PASSTHROUGH`` is the worker's entire window onto the parent
    environment. If a future edit adds a credential-shaped name to it, this
    fails rather than quietly reopening the hole.
    """
    offenders = [
        name for name in loop_jobs._WORKER_ENV_PASSTHROUGH if loop_jobs._is_credential_shaped(name)
    ]
    assert not offenders, offenders


def test_credential_shaped_passthrough_entries_are_not_added(monkeypatch):
    """A future passthrough exemption cannot bypass the shared shape closure."""
    monkeypatch.setattr(
        loop_jobs,
        "_WORKER_ENV_PASSTHROUGH",
        ("MY_AUTH_TOKEN", "OMNIAGENTOS_LOOPS_ROOT"),
    )
    monkeypatch.setenv("MY_AUTH_TOKEN", "dummy-auth-token")
    monkeypatch.setenv("OMNIAGENTOS_LOOPS_ROOT", "/tmp/loops-root")

    env = loop_jobs._worker_env()

    assert "MY_AUTH_TOKEN" not in env
    assert env["OMNIAGENTOS_LOOPS_ROOT"] == "/tmp/loops-root"


def test_the_worker_is_bound_to_the_store_it_was_handed(store, spy_worker):
    """Otherwise a programmatic tick writes approvals into a different DB."""
    calls, _outcome, _envs = spy_worker
    routines = RoutinesStore(store)
    routines.create_routine(_loop_routine())
    _tick(store)

    argv = calls[0]
    assert "--db" in argv
    passed_db = argv[argv.index("--db") + 1]
    bound = store._connection.execute("PRAGMA database_list").fetchone()[2]
    assert passed_db == bound


def test_a_parked_tick_pages_a_human_once(store, spy_worker, monkeypatch):
    """Delivery happens HERE, where the credential legitimately lives."""
    sent: list[str] = []
    monkeypatch.setattr(
        "omniagentos.steward.notify.send_slack",
        lambda text, **kw: sent.append(text) or type("R", (), {"ok": True, "detail": "sent"})(),
    )
    approval_id = "apr_" + "a" * 20
    store.create_approval(
        {
            "id": approval_id,
            "action_class": "consequential",
            "proposed_action": "w2/send",
            "params_json": json.dumps(
                {"loop_instance": "w2_inbox_triage", "node": "send", "tool": "send", "tier": "T2"}
            ),
            "risk": "loop_approval",
            "evidence": "loop floor: tier T2",
            "state": "pending",
            "expires_at": "2099-01-01T00:00:00Z",
            "created_at": "2026-08-01T00:00:00Z",
        }
    )

    assert loop_jobs._deliver_page(store, approval_id) is True
    assert loop_jobs._deliver_page(store, approval_id) is False, "must not re-page every tick"
    assert len(sent) == 1
    assert approval_id in sent[0]
    assert "/approvals?approval=" in sent[0]


def test_a_transient_paging_failure_is_retried_on_the_next_tick(store, monkeypatch):
    """A failed page must leave NO record, or the dedupe check makes it permanent.

    Recording ``loop.approval.delivered`` with ``delivered=False`` on a
    transient Slack failure was self-defeating: the already-delivered probe then
    matched on every later tick, so the retry never happened and the request
    expired unseen — the exact audit failure (15/16 approvals expiring
    undecided) that paging exists to prevent.
    """
    attempts: list[str] = []

    def flaky(text, **kw):
        attempts.append(text)
        ok = len(attempts) > 1  # first call fails, Slack recovers before the next tick
        return type("R", (), {"ok": ok, "detail": "sent" if ok else "HTTP 500"})()

    monkeypatch.setattr("omniagentos.steward.notify.send_slack", flaky)
    approval_id = "apr_" + "b" * 20
    store.create_approval(
        {
            "id": approval_id,
            "action_class": "consequential",
            "proposed_action": "w2/send",
            "params_json": json.dumps({"loop_instance": "w2", "node": "send", "tool": "send"}),
            "risk": "loop_approval",
            "evidence": "loop floor: tier T2",
            "state": "pending",
            "expires_at": "2099-01-01T00:00:00Z",
            "created_at": "2026-08-01T00:00:00Z",
        }
    )

    assert loop_jobs._deliver_page(store, approval_id) is False, "the transient failure"
    assert loop_jobs._deliver_page(store, approval_id) is True, "the retry must be allowed"
    assert loop_jobs._deliver_page(store, approval_id) is False, "and then dedupe again"

    assert len(attempts) == 2, f"expected fail-then-succeed, got {len(attempts)} sends"
    rows = store._connection.execute(
        "SELECT payload_json FROM events WHERE action = 'loop.approval.delivered'"
    ).fetchall()
    assert len(rows) == 1, "exactly one delivered event, written only on success"
    assert json.loads(dict(rows[0])["payload_json"])["delivered"] is True


def test_paging_a_decided_or_unknown_approval_is_a_no_op(store, monkeypatch):
    monkeypatch.setattr(
        "omniagentos.steward.notify.send_slack",
        lambda text, **kw: pytest.fail("must not page"),
    )
    assert loop_jobs._deliver_page(store, "apr_missing") is False
    assert loop_jobs._deliver_page(store, "") is False
    assert loop_jobs._deliver_page(None, "apr_x") is False


# --------------------------------------------------------------------------
# the credential seam: a per-tick socket in ARGV, and still no secret in the env
# --------------------------------------------------------------------------


def test_the_tick_opens_a_credential_seam_and_names_it_in_argv_not_the_environment(
    store, spy_worker, monkeypatch
):
    """The worker is TOLD where the seam is; it is never GIVEN what is behind it.

    The path travels in argv on purpose. Putting it in the environment would
    mean adding a name to ``_WORKER_ENV_PASSTHROUGH``, and that list exists to
    be five non-credential pointers that a reviewer can check in one glance —
    every addition to it is a new thing the credential-shape filter has to be
    trusted about.
    """
    calls, _outcome, envs = spy_worker
    monkeypatch.setenv("REPLICATE_API_TOKEN", "r8_live_secret")

    routines = RoutinesStore(store)
    routines.create_routine(_loop_routine())
    _tick(store)

    argv = calls[0]
    assert "--effect-socket" in argv, "a tick must open the parent-side effect seam"
    socket_path = argv[argv.index("--effect-socket") + 1]
    assert socket_path.endswith("effects.sock")

    passed = envs[0]
    assert "REPLICATE_API_TOKEN" not in passed
    assert "r8_live_secret" not in passed.values()
    assert not any("SOCKET" in name for name in passed), (
        "the seam path must not travel in the environment"
    )
    # And the passthrough list is exactly what it was: five non-credential
    # pointers, each of which still fails the credential-shape test.
    assert loop_jobs._WORKER_ENV_PASSTHROUGH == (
        "OMNIAGENTOS_LOOPS_ROOT",
        "OMNIAGENTOS_LOOPS_VENV",
        "OMNIAGENTOS_VAR_DIR",
        "OMNIAGENTOS_VAR",
        "OMNIAGENTOS_DASHBOARD_ORIGIN",
    )
    assert not any(loop_jobs._is_credential_shaped(n) for n in loop_jobs._WORKER_ENV_PASSTHROUGH)


def test_the_seam_socket_is_gone_once_the_tick_ends(store, spy_worker):
    from pathlib import Path

    calls, _outcome, _envs = spy_worker
    routines = RoutinesStore(store)
    routines.create_routine(_loop_routine())
    _tick(store)

    argv = calls[0]
    socket_path = Path(argv[argv.index("--effect-socket") + 1])
    assert not socket_path.exists(), "a tick's seam must not outlive the tick"
    assert not socket_path.parent.exists()
