from __future__ import annotations

import json
import os
import plistlib
import re
import shutil
import signal
import socket
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from scripts.certification_evidence import (
    build_manifest,
    compare_inventories,
    read_passed_testcases,
)
from scripts.process_supervisor import (
    ProcessSpec,
    ProcessSupervisor,
    SupervisionError,
    acquire_runtime_lock,
    build_process_specs,
    port_available,
    release_runtime_lock,
)

REPO_ROOT = Path(__file__).resolve().parents[2]


class FakeProcess:
    def __init__(self, pid: int) -> None:
        self.pid = pid
        self.returncode: int | None = None
        self.wait_calls = 0

    def poll(self) -> int | None:
        return self.returncode

    def wait(self, timeout: float | None = None) -> int:
        self.wait_calls += 1
        del timeout
        if self.returncode is None:
            raise subprocess.TimeoutExpired("fake", 0)
        return self.returncode


def test_supervisor_starts_isolated_process_groups_and_cleans_every_group(
    tmp_path: Path,
) -> None:
    processes = [FakeProcess(101), FakeProcess(102)]
    popen_kwargs: list[dict[str, Any]] = []

    def popen(*_args: object, **kwargs: Any) -> FakeProcess:
        popen_kwargs.append(kwargs)
        return processes[len(popen_kwargs) - 1]

    signals: list[tuple[int, int]] = []

    def kill_group(pid: int, signum: int) -> None:
        signals.append((pid, signum))
        next(process for process in processes if process.pid == pid).returncode = -signum

    specs = [
        ProcessSpec("api", ("fake-api",), tmp_path, tmp_path / "api.log"),
        ProcessSpec("runner", ("fake-runner",), tmp_path, tmp_path / "runner.log"),
    ]
    supervisor = ProcessSupervisor(
        specs,
        ["http://health"],
        health_timeout=1,
        stop_timeout=1,
        popen=popen,
        kill_group=kill_group,
        group_alive=lambda pid: (
            next(process for process in processes if process.pid == pid).returncode is None
        ),
        health_probe=lambda _url: True,
    )

    supervisor.start()
    supervisor.wait_until_healthy()
    supervisor.stop_all()

    assert all(kwargs["start_new_session"] is True for kwargs in popen_kwargs)
    assert signals == [(102, signal.SIGTERM), (101, signal.SIGTERM)]


def test_supervisor_health_timeout_is_fail_closed(tmp_path: Path) -> None:
    process = FakeProcess(201)
    clock = [0.0]
    signals: list[int] = []

    def sleep(seconds: float) -> None:
        clock[0] += seconds

    def kill_group(_pid: int, signum: int) -> None:
        signals.append(signum)
        process.returncode = -signum

    supervisor = ProcessSupervisor(
        [ProcessSpec("api", ("fake",), tmp_path, tmp_path / "api.log")],
        ["http://never-healthy"],
        health_timeout=0.3,
        stop_timeout=0.1,
        poll_interval=0.1,
        popen=lambda *_args, **_kwargs: process,
        health_probe=lambda _url: False,
        kill_group=kill_group,
        group_alive=lambda _pid: process.returncode is None,
        monotonic=lambda: clock[0],
        sleep=sleep,
    )
    supervisor.start()

    with pytest.raises(SupervisionError, match="health checks did not pass"):
        supervisor.wait_until_healthy()
    supervisor.stop_all()

    assert signals == [signal.SIGTERM]


def test_supervisor_escalates_when_leader_exits_but_descendant_survives(
    tmp_path: Path,
) -> None:
    process = FakeProcess(301)
    clock = [0.0]
    descendant_alive = [True]
    signals: list[int] = []

    def sleep(seconds: float) -> None:
        clock[0] += seconds

    def kill_group(_pid: int, signum: int) -> None:
        signals.append(signum)
        if signum == signal.SIGTERM:
            # The group leader exits, but a descendant deliberately ignores
            # TERM and keeps the process group alive.
            process.returncode = -signum
        elif signum == signal.SIGKILL:
            descendant_alive[0] = False

    supervisor = ProcessSupervisor(
        [ProcessSpec("dashboard", ("fake",), tmp_path, tmp_path / "dashboard.log")],
        [],
        health_timeout=1,
        stop_timeout=0.2,
        poll_interval=0.1,
        kill_group=kill_group,
        group_alive=lambda _pid: descendant_alive[0],
        monotonic=lambda: clock[0],
        sleep=sleep,
    )
    supervisor.processes = [(supervisor.specs[0], process)]

    supervisor.stop_all()

    assert signals == [signal.SIGTERM, signal.SIGKILL]
    assert descendant_alive == [False]
    assert process.wait_calls == 1


def test_supervisor_cleanup_continues_after_one_group_signal_error(tmp_path: Path) -> None:
    processes = [FakeProcess(401), FakeProcess(402)]
    group_alive = {401: True, 402: True}
    clock = [0.0]
    signals: list[tuple[int, int]] = []

    def sleep(seconds: float) -> None:
        clock[0] += seconds

    def kill_group(pid: int, signum: int) -> None:
        signals.append((pid, signum))
        if pid == 402:
            raise PermissionError("simulated signal denial")
        group_alive[pid] = False
        processes[0].returncode = -signum

    supervisor = ProcessSupervisor(
        [
            ProcessSpec("api", ("fake",), tmp_path, tmp_path / "api.log"),
            ProcessSpec("dashboard", ("fake",), tmp_path, tmp_path / "dashboard.log"),
        ],
        [],
        health_timeout=1,
        stop_timeout=0.1,
        poll_interval=0.1,
        kill_group=kill_group,
        group_alive=lambda pid: group_alive[pid],
        monotonic=lambda: clock[0],
        sleep=sleep,
    )
    supervisor.processes = list(zip(supervisor.specs, processes, strict=True))

    with pytest.raises(SupervisionError, match="signal"):
        supervisor.stop_all()

    assert (401, signal.SIGTERM) in signals
    assert (402, signal.SIGTERM) in signals
    assert (402, signal.SIGKILL) in signals


def test_port_ownership_preflight_rejects_existing_listener() -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        port = listener.getsockname()[1]
        assert port_available(port) is False


def test_runtime_lock_is_atomic_and_logs_stay_in_selected_runtime(tmp_path: Path) -> None:
    lock_path = tmp_path / "runtime" / ".supervisor.json.lock"
    assert acquire_runtime_lock(lock_path) is True
    assert acquire_runtime_lock(lock_path) is False

    specs = build_process_specs(
        tmp_path / "repo",
        tmp_path / "repo" / "scripts" / "launcher.sh",
        tmp_path / "runtime",
    )
    assert all(spec.log_path.parent == tmp_path / "runtime" / "logs" for spec in specs)

    release_runtime_lock(lock_path)
    assert not lock_path.exists()


def test_malformed_runtime_lock_fails_closed(tmp_path: Path) -> None:
    lock_path = tmp_path / "runtime" / ".supervisor.json.lock"
    lock_path.parent.mkdir()
    lock_path.write_text("", encoding="utf-8")

    assert acquire_runtime_lock(lock_path) is False
    assert lock_path.exists()


def test_certification_manifest_requires_executed_behavioral_tests(tmp_path: Path) -> None:
    test_dir = tmp_path / "tests" / "toolplane"
    test_dir.mkdir(parents=True)
    (test_dir / "test_toolplane.py").write_text("def test_ok(): pass\n", encoding="utf-8")

    manifest = build_manifest(
        repo_root=tmp_path,
        repository_sha="a" * 40,
        pytest_exit_code=0,
        clean_tree=True,
        executed_paths=["tests/toolplane"],
        passed_testcases={"tests.toolplane.test_toolplane"},
        test_counts={"tests": 1, "passed": 1, "skipped": 0, "failed": 0},
        suite_complete=True,
        expected_test_counts={"tests/toolplane": 1},
        actual_test_counts={"tests/toolplane": 1},
    )
    claims = manifest["claims"]
    assert isinstance(claims, dict)
    assert manifest["certified"] is True
    assert claims["Toolplane"]["status"] == "passed"
    assert claims["TaskContract"]["status"] == "not_certified"


def test_certification_manifest_rejects_mismatched_surface_counts(tmp_path: Path) -> None:
    test_dir = tmp_path / "tests" / "toolplane"
    test_dir.mkdir(parents=True)
    (test_dir / "test_toolplane.py").write_text("def test_ok(): pass\n", encoding="utf-8")

    manifest = build_manifest(
        repo_root=tmp_path,
        repository_sha="a" * 40,
        pytest_exit_code=0,
        clean_tree=True,
        executed_paths=["tests/toolplane"],
        passed_testcases={"tests.toolplane.test_toolplane"},
        test_counts={"tests": 1, "passed": 1, "skipped": 0, "failed": 0},
        suite_complete=True,
        expected_test_counts={"tests/toolplane": 2},
        actual_test_counts={"tests/toolplane": 1},
    )

    claims = manifest["claims"]
    assert isinstance(claims, dict)
    assert manifest["certified"] is False
    assert claims["Toolplane"]["status"] == "not_certified"


def test_skipped_junit_case_cannot_certify_a_claim(tmp_path: Path) -> None:
    test_dir = tmp_path / "tests" / "toolplane"
    test_dir.mkdir(parents=True)
    (test_dir / "test_toolplane.py").write_text("def test_ok(): pass\n", encoding="utf-8")
    junit = tmp_path / "pytest.xml"
    junit.write_text(
        '<testsuites><testsuite tests="1" skipped="1">'
        '<testcase classname="tests.toolplane.test_toolplane" name="test_ok">'
        "<skipped /></testcase></testsuite></testsuites>",
        encoding="utf-8",
    )
    passed_testcases, counts = read_passed_testcases(junit)

    manifest = build_manifest(
        repo_root=tmp_path,
        repository_sha="a" * 40,
        pytest_exit_code=0,
        clean_tree=True,
        executed_paths=["tests/toolplane"],
        passed_testcases=passed_testcases,
        test_counts=counts,
        suite_complete=True,
        expected_test_counts={"tests/toolplane": 1},
        actual_test_counts={"tests/toolplane": 0},
    )

    claims = manifest["claims"]
    assert isinstance(claims, dict)
    assert manifest["certified"] is False
    assert claims["Toolplane"]["status"] == "not_certified"
    assert manifest["test_counts"] == {"tests": 1, "passed": 0, "skipped": 1, "failed": 0}


def _write_inventory(
    path: Path,
    *,
    mode: str,
    repo_root: Path,
    nodeids: list[str],
    outcomes: dict[str, str],
    selected: list[str] | None = None,
    deselected: dict[str, list[str]] | None = None,
    selection: dict[str, object] | None = None,
    environment: dict[str, object] | None = None,
    producer: dict[str, object] | None = None,
) -> None:
    """Write an otherwise-certifiable v2 inventory so negatives isolate one defect."""
    root = repo_root.resolve()
    payload: dict[str, object] = {
        "schema": "omniagentos.pytest-inventory.v2",
        "mode": mode,
        "pytest_exit_code": 0,
        "collected_nodeids": nodeids,
        "selected_nodeids": list(nodeids) if selected is None else selected,
        "deselected_nodeids": deselected or {},
        "outcomes": outcomes,
        "selection": {
            "keyword": "",
            "markexpr": "not (live_cli or perf or live_ollama or live or counterfeit_gate or feature_health or e2e)",
            "deselect": [],
            "ignore": [],
            "ignore_glob": [],
            "maxfail": 0,
            "last_failed": False,
            "failed_first": False,
            "collect_only": mode == "expected",
            **(selection or {}),
        },
        "environment": {
            "plugin_autoload_disabled": True,
            "pytest_addopts": "",
            "pytest_plugins": "",
            "plugin_dists": [],
            "pytest_version": "9.1.1",
            **(environment or {}),
        },
        "producer": {
            "plugin_file": str(root / "scripts" / "certification_pytest_plugin.py"),
            "invocation_args": [],
            "invocation_dir": str(root),
            **(producer or {}),
        },
    }
    path.write_text(json.dumps(payload), encoding="utf-8")


def _inventory_errors(
    tmp_path: Path,
    *,
    expected_kwargs: dict[str, object],
    execution_kwargs: dict[str, object],
    executed_paths: list[str] | None = None,
) -> tuple[bool, list[str]]:
    expected = tmp_path / "expected.json"
    execution = tmp_path / "execution.json"
    _write_inventory(expected, mode="expected", repo_root=tmp_path, **expected_kwargs)  # type: ignore[arg-type]
    _write_inventory(execution, mode="execution", repo_root=tmp_path, **execution_kwargs)  # type: ignore[arg-type]
    complete, _expected, _actual, _counts, _passed, errors = compare_inventories(
        expected_path=expected,
        execution_path=execution,
        executed_paths=executed_paths or ["tests/toolplane"],
        repo_root=tmp_path,
    )
    return complete, errors


def test_partial_or_deselected_inventory_cannot_certify(tmp_path: Path) -> None:
    expected = tmp_path / "expected.json"
    execution = tmp_path / "execution.json"
    first = "tests/toolplane/test_contract.py::test_allow"
    second = "tests/toolplane/test_contract.py::test_deny"
    _write_inventory(
        expected, mode="expected", repo_root=tmp_path, nodeids=[first, second], outcomes={}
    )
    _write_inventory(
        execution,
        mode="execution",
        repo_root=tmp_path,
        nodeids=[first],
        outcomes={first: "passed"},
    )

    complete, expected_counts, actual_counts, counts, passed, errors = compare_inventories(
        expected_path=expected,
        execution_path=execution,
        executed_paths=["tests/toolplane"],
        repo_root=tmp_path,
    )

    assert complete is False
    assert expected_counts == {"tests/toolplane": 2}
    assert actual_counts == {"tests/toolplane": 1}
    assert counts == {"tests": 1, "passed": 1, "skipped": 0, "failed": 0}
    assert passed == {"tests.toolplane.test_contract"}
    assert any("partial/deselected" in error for error in errors)


def test_mixed_skipped_inventory_cannot_certify(tmp_path: Path) -> None:
    expected = tmp_path / "expected.json"
    execution = tmp_path / "execution.json"
    first = "tests/toolplane/test_contract.py::test_allow"
    second = "tests/toolplane/test_contract.py::test_optional"
    _write_inventory(
        expected, mode="expected", repo_root=tmp_path, nodeids=[first, second], outcomes={}
    )
    _write_inventory(
        execution,
        mode="execution",
        repo_root=tmp_path,
        nodeids=[first, second],
        outcomes={first: "passed", second: "skipped"},
    )

    complete, expected_counts, actual_counts, counts, _passed, errors = compare_inventories(
        expected_path=expected,
        execution_path=execution,
        executed_paths=["tests/toolplane"],
        repo_root=tmp_path,
    )

    assert complete is False
    assert expected_counts == {"tests/toolplane": 2}
    assert actual_counts == {"tests/toolplane": 1}
    assert counts == {"tests": 2, "passed": 1, "skipped": 1, "failed": 0}
    assert any("skipped" in error for error in errors)


_ALLOW = "tests/toolplane/test_contract.py::test_allow"
_DENY = "tests/toolplane/test_contract.py::test_deny"


def test_v1_schema_inventories_cannot_certify(tmp_path: Path) -> None:
    """v1 recorded no pre-deselection surface, so a v1 artifact must never certify."""
    expected = tmp_path / "expected.json"
    execution = tmp_path / "execution.json"
    for path, mode, outcomes in (
        (expected, "expected", {}),
        (execution, "execution", {_ALLOW: "passed"}),
    ):
        path.write_text(
            json.dumps(
                {
                    "schema": "omniagentos.pytest-inventory.v1",
                    "mode": mode,
                    "pytest_exit_code": 0,
                    "collected_nodeids": [_ALLOW],
                    "outcomes": outcomes,
                }
            ),
            encoding="utf-8",
        )

    complete, _expected, _actual, _counts, _passed, errors = compare_inventories(
        expected_path=expected,
        execution_path=execution,
        executed_paths=["tests/toolplane"],
        repo_root=tmp_path,
    )

    assert complete is False
    assert errors == [
        "expected inventory schema is invalid",
        "execution inventory schema is invalid",
    ]


def test_identical_pre_deselection_in_both_runs_cannot_certify(tmp_path: Path) -> None:
    """The core Blocker-2 attack: deselect the same test in both runs.

    Post-filter node-id sets match exactly and every surviving test passes, so
    every comparison-only check is satisfied.  Only the recorded pre-deselection
    inventory and the explicit deselection record reject it.
    """
    complete, errors = _inventory_errors(
        tmp_path,
        expected_kwargs={
            "nodeids": [_ALLOW, _DENY],
            "selected": [_ALLOW],
            "deselected": {_DENY: ["live"]},
            "outcomes": {},
            "selection": {"markexpr": ""},
        },
        execution_kwargs={
            "nodeids": [_ALLOW, _DENY],
            "selected": [_ALLOW],
            "deselected": {_DENY: ["live"]},
            "outcomes": {_ALLOW: "passed"},
            "selection": {"markexpr": ""},
        },
    )

    assert complete is False
    assert any("deselected 1 tests" in error for error in errors)
    assert any("selected fewer tests than it collected" in error for error in errors)


def test_unmarked_deselection_cannot_certify(tmp_path: Path) -> None:
    complete, errors = _inventory_errors(
        tmp_path,
        expected_kwargs={
            "nodeids": [_ALLOW, _DENY],
            "selected": [_ALLOW],
            "deselected": {_DENY: []},
            "outcomes": {},
        },
        execution_kwargs={
            "nodeids": [_ALLOW, _DENY],
            "selected": [_ALLOW],
            "deselected": {_DENY: []},
            "outcomes": {_ALLOW: "passed"},
        },
    )

    assert complete is False
    assert any("expected inventory node IDs are invalid" in error for error in errors) or any(
        "execution inventory node IDs are invalid" in error for error in errors
    )


def test_fabricated_deselection_reason_cannot_certify(tmp_path: Path) -> None:
    complete, errors = _inventory_errors(
        tmp_path,
        expected_kwargs={
            "nodeids": [_ALLOW, _DENY],
            "selected": [_ALLOW],
            "deselected": {_DENY: ["bogus"]},
            "outcomes": {},
        },
        execution_kwargs={
            "nodeids": [_ALLOW, _DENY],
            "selected": [_ALLOW],
            "deselected": {_DENY: ["bogus"]},
            "outcomes": {_ALLOW: "passed"},
        },
    )

    assert complete is False
    assert any("expected inventory node IDs are invalid" in error for error in errors) or any(
        "execution inventory node IDs are invalid" in error for error in errors
    )


def test_silent_item_dropping_without_deselect_hook_cannot_certify(tmp_path: Path) -> None:
    """A plugin that mutates ``items`` without calling ``pytest_deselected``."""
    complete, errors = _inventory_errors(
        tmp_path,
        expected_kwargs={
            "nodeids": [_ALLOW, _DENY],
            "selected": [_ALLOW],
            "outcomes": {},
            "selection": {"markexpr": ""},
        },
        execution_kwargs={
            "nodeids": [_ALLOW, _DENY],
            "selected": [_ALLOW],
            "outcomes": {_ALLOW: "passed", _DENY: "passed"},
            "selection": {"markexpr": ""},
        },
    )

    assert complete is False
    assert any("selected fewer tests than it collected" in error for error in errors)


@pytest.mark.parametrize(
    ("selection", "fragment"),
    [
        ({"keyword": "not slow"}, "keyword selector"),
        ({"markexpr": "not integration"}, "invalid markexpr"),
        ({"deselect": ["tests/toolplane/test_contract.py::test_deny"]}, "deselect selector"),
        ({"ignore": ["tests/toolplane"]}, "ignore selector"),
        ({"ignore_glob": ["tests/**"]}, "ignore_glob selector"),
        ({"maxfail": 1}, "maxfail (partial execution)"),
        ({"last_failed": True}, "cached-failure selector"),
        ({"failed_first": True}, "cached-failure selector"),
    ],
)
def test_selection_narrowing_options_cannot_certify(
    tmp_path: Path, selection: dict[str, object], fragment: str
) -> None:
    complete, errors = _inventory_errors(
        tmp_path,
        expected_kwargs={"nodeids": [_ALLOW], "outcomes": {}},
        execution_kwargs={
            "nodeids": [_ALLOW],
            "outcomes": {_ALLOW: "passed"},
            "selection": selection,
        },
    )

    assert complete is False
    assert any(fragment in error for error in errors), errors


def test_collect_only_execution_run_cannot_certify(tmp_path: Path) -> None:
    """A ``--collect-only`` execution run never runs a test, so it cannot certify."""
    complete, errors = _inventory_errors(
        tmp_path,
        expected_kwargs={"nodeids": [_ALLOW], "outcomes": {}},
        execution_kwargs={
            "nodeids": [_ALLOW],
            "outcomes": {_ALLOW: "passed"},
            "selection": {"collect_only": True},
        },
    )

    assert complete is False
    assert any("wrong collect-only mode" in error for error in errors)


@pytest.mark.parametrize(
    ("environment", "fragment"),
    [
        ({"plugin_autoload_disabled": False}, "allowed pytest plugin autoloading"),
        ({"pytest_addopts": "-k smoke"}, "inherited PYTEST_ADDOPTS"),
        ({"pytest_plugins": "evil_selector"}, "inherited PYTEST_PLUGINS"),
        ({"plugin_dists": ["pytest-randomly"]}, "external pytest plugin distributions"),
    ],
)
def test_external_plugin_environment_cannot_certify(
    tmp_path: Path, environment: dict[str, object], fragment: str
) -> None:
    complete, errors = _inventory_errors(
        tmp_path,
        expected_kwargs={"nodeids": [_ALLOW], "outcomes": {}},
        execution_kwargs={
            "nodeids": [_ALLOW],
            "outcomes": {_ALLOW: "passed"},
            "environment": environment,
        },
    )

    assert complete is False
    assert any(fragment in error for error in errors), errors


@pytest.mark.parametrize(
    ("producer", "fragment"),
    [
        ({"plugin_file": "/opt/attacker/certification_pytest_plugin.py"}, "outside the certified"),
        ({"plugin_file": ""}, "does not name its producing plugin"),
        ({"invocation_dir": "/opt/attacker"}, "invoked outside the certified tree"),
    ],
)
def test_foreign_producer_inventory_cannot_certify(
    tmp_path: Path, producer: dict[str, object], fragment: str
) -> None:
    """A shadowed or foreign plugin copy cannot vouch for the certified tree."""
    complete, errors = _inventory_errors(
        tmp_path,
        expected_kwargs={"nodeids": [_ALLOW], "outcomes": {}},
        execution_kwargs={
            "nodeids": [_ALLOW],
            "outcomes": {_ALLOW: "passed"},
            "producer": producer,
        },
    )

    assert complete is False
    assert any(fragment in error for error in errors), errors


def test_missing_v2_blocks_cannot_certify(tmp_path: Path) -> None:
    """A v1-shaped payload relabelled as v2 has no selection/environment/producer."""
    expected = tmp_path / "expected.json"
    execution = tmp_path / "execution.json"
    for path, mode, outcomes in (
        (expected, "expected", {}),
        (execution, "execution", {_ALLOW: "passed"}),
    ):
        path.write_text(
            json.dumps(
                {
                    "schema": "omniagentos.pytest-inventory.v2",
                    "mode": mode,
                    "pytest_exit_code": 0,
                    "collected_nodeids": [_ALLOW],
                    "selected_nodeids": [_ALLOW],
                    "deselected_nodeids": {},
                    "outcomes": outcomes,
                }
            ),
            encoding="utf-8",
        )

    complete, _expected, _actual, _counts, _passed, errors = compare_inventories(
        expected_path=expected,
        execution_path=execution,
        executed_paths=["tests/toolplane"],
        repo_root=tmp_path,
    )

    assert complete is False
    assert any("no selection block" in error for error in errors)
    assert any("no environment block" in error for error in errors)
    assert any("no producer block" in error for error in errors)


def certified_test_paths() -> list[str]:
    """The certified surface, read from the script that defines it.

    Duplicating this list in a test would let the two drift, and a fixture that
    silently describes a smaller surface than the one actually certified is the
    same class of defect as a run that silently deselects tests.
    """
    script = (REPO_ROOT / "scripts" / "certify-omniagentos.sh").read_text(encoding="utf-8")
    match = re.search(r"^TEST_PATHS=\(\n(.*?)^\)$", script, re.DOTALL | re.MULTILINE)
    assert match is not None, "certify-omniagentos.sh no longer declares a TEST_PATHS array"
    paths = [line.strip() for line in match.group(1).splitlines() if line.strip()]
    assert paths, "certify-omniagentos.sh declares an empty TEST_PATHS array"
    return paths


def test_certification_shell_forces_plugin_autoload_off() -> None:
    script = (REPO_ROOT / "scripts" / "certify-omniagentos.sh").read_text(encoding="utf-8")
    assert "export PYTEST_DISABLE_PLUGIN_AUTOLOAD=1" in script
    assert script.count("-p scripts.certification_pytest_plugin") == 2


def test_routine_gate_chain_is_inside_the_certified_surface() -> None:
    """The gate chain is the operations-truth claim, so it must be certified.

    A routine settles itself unattended on the strength of its objective gate.
    If the evidence chain that decides that gate is outside the certified
    surface, certification is silent about the one thing it most needs to cover.
    """
    paths = certified_test_paths()

    assert "tests/scheduler" in paths
    for module in ("test_gate_evidence.py", "test_routines_settle.py"):
        assert (REPO_ROOT / "tests" / "scheduler" / module).is_file()


def test_certification_script_reaches_filter_rejection_with_contained_runtime(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    scripts = repo / "scripts"
    scripts.mkdir(parents=True)
    # certify-omniagentos.sh sources launch-env.sh for canonical OMNIAGENTOS_DB resolution.
    for name in ("certify-omniagentos.sh", "launch-env.sh"):
        shutil.copy2(REPO_ROOT / "scripts" / name, scripts / name)
    python_dir = repo / ".venv" / "bin"
    python_dir.mkdir(parents=True)
    (python_dir / "python").symlink_to(sys.executable)
    (repo / ".gitignore").write_text(".venv/\nvar/\n", encoding="utf-8")
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.invalid"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, check=True)
    subprocess.run(["git", "add", "--all"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "seed"], cwd=repo, check=True)

    result = subprocess.run(
        ["/bin/bash", "scripts/certify-omniagentos.sh", "-k", "only_one_test"],
        cwd=repo,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 2
    assert "does not accept pytest filters" in result.stderr
    assert "runtime path escapes" not in result.stderr


def test_certification_sanitizes_inherited_pytest_selection_environment(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    scripts = repo / "scripts"
    scripts.mkdir(parents=True)
    for name in (
        "certify-omniagentos.sh",
        "launch-env.sh",
        "certification_evidence.py",
        "certification_pytest_plugin.py",
    ):
        shutil.copy2(REPO_ROOT / "scripts" / name, scripts / name)
    # Hermeticity: this venv installs the real repository root on sys.path, where
    # ``scripts`` IS a regular package.  A namespace-package copy under the temp
    # root loses that resolution race, so without this file
    # ``-p scripts.certification_pytest_plugin`` would import the shared plugin
    # and the assertions below would describe the wrong code.
    (scripts / "__init__.py").write_text("", encoding="utf-8")

    test_paths = certified_test_paths()
    for index, relative in enumerate(test_paths):
        path = repo / relative
        if path.suffix != ".py":
            path = path / f"test_surface_{index}.py"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"def test_surface_{index}():\n    assert True\n", encoding="utf-8")
    # Two certified paths name the same filename in different directories, so
    # every test directory is a package to keep module names unique.
    for directory in (repo / "tests").rglob("*"):
        if directory.is_dir():
            (directory / "__init__.py").write_text("", encoding="utf-8")
    (repo / "tests" / "__init__.py").write_text("", encoding="utf-8")

    (repo / ".gitignore").write_text(
        ".pytest_cache/\n__pycache__/\n*.pyc\nvar/\n",
        encoding="utf-8",
    )
    # Anchor pytest rootdir inside the hermetic repo. When the host sandbox
    # forces basetemp under the real worktree, walking up would otherwise pick
    # the outer pyproject.toml and emit non-relative nodeids that fail path
    # ownership checks in certification_evidence.py.
    (repo / "pytest.ini").write_text("[pytest]\n", encoding="utf-8")
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.invalid"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, check=True)
    subprocess.run(["git", "add", "--all"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "seed"], cwd=repo, check=True)

    result = subprocess.run(
        ["/bin/bash", "scripts/certify-omniagentos.sh"],
        cwd=repo,
        env={
            **os.environ,
            "OMNIAGENTOS_PYTHON": sys.executable,
            "OMNIAGENTOS_VAR_DIR": str(repo / "var"),
            "PYTEST_ADDOPTS": "-k definitely_not_all",
            "PYTEST_PLUGINS": "definitely_missing_plugin",
            "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
        },
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )

    assert result.returncode == 0, f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    evidence = json.loads(
        (repo / "var" / "certification" / "evidence.json").read_text(encoding="utf-8")
    )
    assert evidence["certified"] is True
    assert evidence["suite_complete"] is True
    assert evidence["test_counts"] == {
        "tests": len(test_paths),
        "passed": len(test_paths),
        "skipped": 0,
        "failed": 0,
    }
    assert evidence["expected_test_counts"] == dict.fromkeys(test_paths, 1)
    assert evidence["actual_test_counts"] == dict.fromkeys(test_paths, 1)

    # Prove the fixture exercised the copy under test rather than the shared
    # editable ``scripts`` package, and that the hostile selection environment
    # was actually scrubbed rather than merely tolerated.
    certification = repo / "var" / "certification"
    for name in ("pytest-expected.json", "pytest-execution.json"):
        inventory = json.loads((certification / name).read_text(encoding="utf-8"))
        producer = inventory["producer"]
        assert Path(producer["plugin_file"]).is_relative_to(repo.resolve()), producer
        assert Path(producer["invocation_dir"]) == repo.resolve()
        assert inventory["deselected_nodeids"] == {}
        assert inventory["selection"]["keyword"] == ""
        assert inventory["environment"]["pytest_addopts"] == ""
        assert inventory["environment"]["pytest_plugins"] == ""
        assert inventory["environment"]["plugin_autoload_disabled"] is True
        assert inventory["environment"]["plugin_dists"] == []


def _fake_tool_path(tmp_path: Path) -> tuple[Path, Path]:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    for name in ("python3", "python3.12"):
        (fake_bin / name).symlink_to(sys.executable)
    marker = tmp_path / "launchctl-called"
    launchctl = fake_bin / "launchctl"
    launchctl.write_text(
        f"#!/bin/sh\nprintf called > {marker}\nexit 99\n",
        encoding="utf-8",
    )
    launchctl.chmod(0o755)
    return fake_bin, marker


def test_product_installers_render_to_fake_destination_without_launchctl(
    tmp_path: Path,
) -> None:
    fake_bin, launchctl_marker = _fake_tool_path(tmp_path)
    target_dir = tmp_path / "LaunchAgents"
    connections = tmp_path / ".config" / "omni" / "connections.env"
    connections.parent.mkdir(parents=True)
    connections.write_text("", encoding="utf-8")
    (tmp_path / "runtime").mkdir()
    env = {
        **os.environ,
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
        "HOME": str(tmp_path),
        "OMNIAGENTOS_LAUNCHD_TARGET_DIR": str(target_dir),
        "OMNIAGENTOS_DB": str(tmp_path / "runtime" / "state.sqlite3"),
        "OMNIAGENTOS_VAR_DIR": str(tmp_path / "runtime"),
    }
    installers = [
        "scripts/reliability/install.sh",
        "scripts/hygiene/install-hygiene.sh",
        "scripts/curator/run.sh",
        "scripts/golden-suite/install-golden-suite.sh",
        "scripts/provider-sentinel/install.sh",
        "scripts/backlog-executor/install.sh",
        "scripts/archi-morning/install-archi-morning.sh",
        "scripts/scheduler/install-routines.sh",
        "scripts/scheduler/install.sh",
        "scripts/scheduler/install-steward.sh",
    ]

    for relative in installers:
        result = subprocess.run(
            ["/bin/sh", relative],
            cwd=REPO_ROOT,
            env=env,
            check=False,
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, (
            f"{relative} failed\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        )

    assert not launchctl_marker.exists()
    plists = list(target_dir.glob("*.plist"))
    assert {path.stem for path in plists} == {
        "com.omniagentos.archi-morning",
        "com.omniagentos.backlog-executor",
        "com.omniagentos.curator",
        "com.omniagentos.golden-suite",
        "com.omniagentos.hygiene",
        "com.omniagentos.morning",
        "com.omniagentos.provider-sentinel",
        "com.omniagentos.reliability-audit",
        "com.omniagentos.reliability-daily",
        "com.omniagentos.reliability-watch",
        "com.omniagentos.reliability-weekly",
        "com.omniagentos.routines",
        "com.omniagentos.steward.alerts",
        "com.omniagentos.steward.briefing",
        "com.omniagentos.steward.comms",
        "com.omniagentos.steward.metrics",
    }
    for path in plists:
        with path.open("rb") as handle:
            payload = plistlib.load(handle)
        assert payload["Label"] == path.stem
    assert "omniagentos.goals.seed" not in (
        REPO_ROOT / "scripts" / "scheduler" / "install-steward.sh"
    ).read_text(encoding="utf-8")


@pytest.mark.parametrize(
    "label",
    [
        # A well-shaped but stale/legacy label value (distinct from the
        # wrong-subsystem case below): the product rename collapsed the
        # original mismatch this case exercised (a pre-rename product-prefix
        # variant), so this checks the same exact-pin refusal with a
        # different kind of mismatched-but-valid-shape label.
        "com.omniagentos.hygiene-legacy",
        "com.omniagentos.reliability-audit",
    ],
)
def test_installer_rejects_noncanonical_label(tmp_path: Path, label: str) -> None:
    fake_bin, launchctl_marker = _fake_tool_path(tmp_path)
    target_dir = tmp_path / "LaunchAgents"
    result = subprocess.run(
        ["/bin/sh", "scripts/hygiene/install-hygiene.sh"],
        cwd=REPO_ROOT,
        env={
            **os.environ,
            "PATH": f"{fake_bin}:{os.environ['PATH']}",
            "HOME": str(tmp_path),
            "OMNIAGENTOS_LAUNCHD_TARGET_DIR": str(target_dir),
            "OMNIAGENTOS_HYGIENE_LAUNCHD_LABEL": label,
        },
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode != 0
    # scripts/lib/launchd-label.sh:55 is the shared shape+pin check every
    # installer now calls through require_safe_launchd_label(); it refuses
    # correctly here (this assertion was written against the pre-refactor
    # per-installer message and never updated to the centralized one).
    assert (
        f"error: refusing unexpected launchd label: {label} "
        "(expected com.omniagentos.hygiene)" in result.stderr
    )
    assert not launchctl_marker.exists()
    assert not target_dir.exists()


def test_installer_default_destination_is_project_runtime_not_live_launchd(
    tmp_path: Path,
) -> None:
    fake_bin, launchctl_marker = _fake_tool_path(tmp_path)
    var_root = tmp_path / "project-runtime"
    home = tmp_path / "home"
    result = subprocess.run(
        ["/bin/sh", "scripts/scheduler/install.sh"],
        cwd=REPO_ROOT,
        env={
            **os.environ,
            "PATH": f"{fake_bin}:{os.environ['PATH']}",
            "HOME": str(home),
            "OMNIAGENTOS_VAR_DIR": str(var_root),
        },
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    rendered = var_root / "launchd" / "rendered" / "com.omniagentos.morning.plist"
    assert rendered.is_file()
    assert str(rendered) in result.stdout
    assert not (home / "Library" / "LaunchAgents").exists()
    assert not launchctl_marker.exists()


def test_launcher_rejects_runtime_path_that_resolves_into_tracked_state() -> None:
    result = subprocess.run(
        ["scripts/launch-supervised.sh", "status"],
        cwd=REPO_ROOT,
        env={
            **os.environ,
            "OMNIAGENTOS_PYTHON": sys.executable,
            "OMNIAGENTOS_VAR_DIR": str(REPO_ROOT / "var" / ".." / "ledger"),
        },
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode != 0
    assert "runtime state path is source-controlled" in result.stderr


def test_launcher_rejects_pid_file_inside_source_tree(tmp_path: Path) -> None:
    result = subprocess.run(
        ["scripts/launch-supervised.sh", "status"],
        cwd=REPO_ROOT,
        env={
            **os.environ,
            "OMNIAGENTOS_PYTHON": sys.executable,
            "OMNIAGENTOS_VAR_DIR": str(tmp_path / "runtime"),
            "OMNIAGENTOS_SUPERVISOR_PID_FILE": str(REPO_ROOT / "STATUS.md"),
        },
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode != 0
    assert "runtime state path is source-controlled" in result.stderr


def test_live_marked_tests_cannot_execute_in_certification(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    scripts = repo / "scripts"
    scripts.mkdir(parents=True)
    for name in (
        "certify-omniagentos.sh",
        "launch-env.sh",
        "certification_evidence.py",
        "certification_pytest_plugin.py",
    ):
        shutil.copy2(REPO_ROOT / "scripts" / name, scripts / name)
    (scripts / "__init__.py").write_text("", encoding="utf-8")

    test_paths = certified_test_paths()
    for index, relative in enumerate(test_paths):
        path = repo / relative
        if path.suffix != ".py":
            path = path / f"test_surface_{index}.py"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"def test_surface_{index}():\n    assert True\n", encoding="utf-8")

        # Add a live-marked test in one of the files
        if index == 0:
            path.write_text(
                "import pytest\n"
                "@pytest.mark.live\n"
                "def test_live_surface():\n"
                "    assert False, 'Live test should not execute'\n\n"
                f"def test_surface_{index}():\n"
                "    assert True\n",
                encoding="utf-8",
            )

    for directory in (repo / "tests").rglob("*"):
        if directory.is_dir():
            (directory / "__init__.py").write_text("", encoding="utf-8")
    (repo / "tests" / "__init__.py").write_text("", encoding="utf-8")

    (repo / ".gitignore").write_text(
        ".pytest_cache/\n__pycache__/\n*.pyc\nvar/\n",
        encoding="utf-8",
    )
    (repo / "pytest.ini").write_text(
        "[pytest]\n"
        "markers =\n"
        "    live_cli\n"
        "    perf\n"
        "    live_ollama\n"
        "    live\n"
        "    counterfeit_gate\n"
        "    feature_health\n"
        "    e2e\n",
        encoding="utf-8",
    )
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.invalid"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, check=True)
    subprocess.run(["git", "add", "--all"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "seed"], cwd=repo, check=True)

    result = subprocess.run(
        ["/bin/bash", "scripts/certify-omniagentos.sh"],
        cwd=repo,
        env={
            **os.environ,
            "OMNIAGENTOS_PYTHON": sys.executable,
            "OMNIAGENTOS_VAR_DIR": str(repo / "var"),
            "JIRA_BASE_URL": "https://fake.jira",
            "JIRA_EMAIL": "fake@example.com",
            "JIRA_API_TOKEN": "fake_token",
        },
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )

    assert result.returncode == 0, f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"

    cert_dir = repo / "var" / "certification"
    execution_inventory = json.loads(
        (cert_dir / "pytest-execution.json").read_text(encoding="utf-8")
    )
    expected_inventory = json.loads((cert_dir / "pytest-expected.json").read_text(encoding="utf-8"))

    if test_paths[0].endswith(".py"):
        prefix = test_paths[0]
    else:
        prefix = f"{test_paths[0]}/test_surface_0.py"
    safe_node_id = f"{prefix}::test_surface_0"
    live_node_id = f"{prefix}::test_live_surface"

    for inventory in (execution_inventory, expected_inventory):
        assert live_node_id in inventory["collected_nodeids"]
        assert safe_node_id in inventory["collected_nodeids"]

        assert safe_node_id in inventory["selected_nodeids"]
        assert live_node_id not in inventory["selected_nodeids"]

        assert live_node_id in inventory["deselected_nodeids"]
        assert safe_node_id not in inventory["deselected_nodeids"]

        assert inventory["deselected_nodeids"][live_node_id] == ["live"]

    assert execution_inventory["outcomes"][safe_node_id] == "passed"
    assert live_node_id not in execution_inventory["outcomes"]


def test_entire_path_live_marked_vacuous_refusal(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    scripts = repo / "scripts"
    scripts.mkdir(parents=True)
    for name in (
        "certify-omniagentos.sh",
        "launch-env.sh",
        "certification_evidence.py",
        "certification_pytest_plugin.py",
    ):
        shutil.copy2(REPO_ROOT / "scripts" / name, scripts / name)
    (scripts / "__init__.py").write_text("", encoding="utf-8")

    test_paths = certified_test_paths()
    for index, relative in enumerate(test_paths):
        path = repo / relative
        if path.suffix != ".py":
            path = path / f"test_surface_{index}.py"
        path.parent.mkdir(parents=True, exist_ok=True)
        if index == 0:
            path.write_text(
                "import pytest\n@pytest.mark.live\ndef test_vacuous():\n    pass\n",
                encoding="utf-8",
            )
        else:
            path.write_text(f"def test_surface_{index}():\n    assert True\n", encoding="utf-8")

    for directory in (repo / "tests").rglob("*"):
        if directory.is_dir():
            (directory / "__init__.py").write_text("", encoding="utf-8")
    (repo / "tests" / "__init__.py").write_text("", encoding="utf-8")

    (repo / ".gitignore").write_text(
        ".pytest_cache/\n__pycache__/\n*.pyc\nvar/\n", encoding="utf-8"
    )
    (repo / "pytest.ini").write_text(
        "[pytest]\n"
        "markers =\n"
        "    live_cli\n"
        "    perf\n"
        "    live_ollama\n"
        "    live\n"
        "    counterfeit_gate\n"
        "    feature_health\n"
        "    e2e\n",
        encoding="utf-8",
    )
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.invalid"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, check=True)
    subprocess.run(["git", "add", "--all"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "seed"], cwd=repo, check=True)

    result = subprocess.run(
        ["/bin/bash", "scripts/certify-omniagentos.sh"],
        cwd=repo,
        env={
            **os.environ,
            "OMNIAGENTOS_PYTHON": sys.executable,
            "OMNIAGENTOS_VAR_DIR": str(repo / "var"),
        },
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    assert result.returncode != 0
    assert "requested test path selected zero tests" in result.stderr
