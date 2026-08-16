"""The executor: injected-runner only, stop on first failure, honest reporting."""

from __future__ import annotations

import inspect
import os
import subprocess
import sys

import pytest

from omniagentos.deploy import bootstrap as bootstrap_mod
from omniagentos.deploy import contracts as contracts_mod
from omniagentos.deploy import deploy as deploy_mod
from omniagentos.deploy import executor as executor_mod
from omniagentos.deploy.bootstrap import plan_server_bootstrap
from omniagentos.deploy.contracts import AppSpec, DeployPlan, DeployStep, RunResult, ServerSpec
from omniagentos.deploy.deploy import plan_app_deploy
from omniagentos.deploy.executor import dry_run_runner, execute_plan
from tests.deploy.fakes import FakeRunner, RaisingRunner


@pytest.fixture
def plan() -> DeployPlan:
    return DeployPlan(
        name="t",
        target_host="host",
        steps=(
            DeployStep("one", "first", "echo 1"),
            DeployStep("two", "second", "echo 2"),
            DeployStep("three", "third", "echo 3"),
        ),
    )


def test_every_command_goes_through_the_injected_runner(plan: DeployPlan) -> None:
    runner = FakeRunner()
    report = execute_plan(plan, runner)
    assert runner.commands == ["echo 1", "echo 2", "echo 3"]
    assert report.ok
    assert report.failed_step is None
    assert report.executed_step_ids == ("one", "two", "three")


def test_stops_on_first_failing_step_and_names_it(plan: DeployPlan) -> None:
    runner = FakeRunner(failures={"echo 2": RunResult(exit_code=7, stderr="boom")})
    report = execute_plan(plan, runner)

    assert not report.ok
    assert report.failed_step_id == "two"
    assert report.failed_step is not None
    assert report.failed_step.exit_code == 7
    assert report.failed_step.stderr == "boom"
    # The third step must never have been attempted.
    assert runner.commands == ["echo 1", "echo 2"]
    assert report.skipped_step_ids == ("three",)
    assert "two" in report.summary() and "FAILED" in report.summary()


def test_report_covers_every_step_in_plan_order(plan: DeployPlan) -> None:
    report = execute_plan(plan, FakeRunner(failures={"echo 1": RunResult(exit_code=1)}))
    assert [s.step_id for s in report.steps] == ["one", "two", "three"]
    assert [s.skipped for s in report.steps] == [False, True, True]


def test_runner_exception_is_reported_as_a_step_failure(plan: DeployPlan) -> None:
    runner = RaisingRunner(boom_on="echo 2")
    report = execute_plan(plan, runner)
    assert report.failed_step_id == "two"
    assert "ConnectionResetError" in report.steps[1].stderr
    assert report.steps[2].skipped


def test_runner_must_return_a_runresult(plan: DeployPlan) -> None:
    with pytest.raises(TypeError):
        execute_plan(plan, lambda _cmd: "done")  # type: ignore[arg-type,return-value]


def test_runner_must_be_callable(plan: DeployPlan) -> None:
    with pytest.raises(TypeError):
        execute_plan(plan, None)  # type: ignore[arg-type]


def test_dry_run_runner_records_without_executing(plan: DeployPlan) -> None:
    seen: list[str] = []
    report = execute_plan(plan, dry_run_runner(seen))
    assert report.ok
    assert seen == ["echo 1", "echo 2", "echo 3"]


def test_full_plans_execute_end_to_end_against_a_fake(
    app: AppSpec, server: ServerSpec
) -> None:
    runner = FakeRunner()
    boot = execute_plan(plan_server_bootstrap(server), runner)
    dep = execute_plan(plan_app_deploy(app), runner)
    assert boot.ok and dep.ok
    assert len(runner.commands) == len(boot.steps) + len(dep.steps)


# --- the "nothing else is called" guarantee ---------------------------------


_DEPLOY_MODULES = (bootstrap_mod, contracts_mod, deploy_mod, executor_mod)


def test_executor_never_touches_a_real_process(
    plan: DeployPlan, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _forbidden(*args: object, **kwargs: object) -> None:
        raise AssertionError(f"deploy library executed something for real: {args!r}")

    for name in ("run", "Popen", "call", "check_call", "check_output", "getoutput"):
        monkeypatch.setattr(subprocess, name, _forbidden, raising=False)
    for name in ("system", "popen", "execv", "execvp", "fork", "spawnv", "posix_spawn"):
        monkeypatch.setattr(os, name, _forbidden, raising=False)

    runner = FakeRunner()
    report = execute_plan(plan, runner)
    assert report.ok
    assert runner.commands  # the fake runner is the ONLY execution path


def test_deploy_modules_import_no_execution_or_network_library() -> None:
    banned = ("subprocess", "paramiko", "asyncssh", "fabric", "socket", "httpx",
              "requests", "urllib", "os.system", "pty", "telnetlib")
    for module in _DEPLOY_MODULES:
        source = inspect.getsource(module)
        for name in banned:
            assert f"import {name}" not in source, f"{module.__name__} imports {name}"


def test_no_ssh_library_is_loaded_by_importing_the_package() -> None:
    assert "paramiko" not in sys.modules
    assert "asyncssh" not in sys.modules
