"""M2 — per-ecosystem gate executors behind the counted-evidence seam.

Every toolchain here is a STUB on PATH that emits the real wire format
(``go test -json`` events, a vitest JUnit XML, libtest's stable summary block).
That is deliberate and is not a weakening: the thing under test is the parsing,
counting, cross-checking and refusal logic, and a stub is the only way to drive
a malformed, inflated, truncated, cached-looking or hanging toolchain
deterministically on a box that may not have Go installed at all (this one does
not). The stubs' outputs are copied from real toolchain output, so the parsers
are exercised against genuine formats.

The counterfeits are the point of the file:

* a fabricated JUnit XML committed into the candidate tree must not count;
* a toolchain that exits 0 while reporting zero tests must be refused;
* a toolchain that prints a success line but no machine-readable events must be
  refused;
* a summary line that claims more passes than the per-test lines above it must
  be refused;
* a toolchain that hangs must be terminated and reported INCONCLUSIVE.
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
import time
from datetime import UTC, datetime
from pathlib import Path

import pytest

from omniagentos.scheduler.gate_ecosystems import (
    ECOSYSTEM_PYTHON,
    SUPPORTED_ECOSYSTEMS,
    CargoTestExecutor,
    GoTestExecutor,
    NpmVitestExecutor,
    ecosystem_command_is_valid,
    ecosystem_of_gate_config,
    executor_for,
    expected_tool_for_gate_config,
    normalize_ecosystem,
    read_artifact_nofollow,
    resolve_program,
    sanitize_path_env,
)
from omniagentos.scheduler.gate_evidence import (
    MERGE_GATE_ROUTINE_ID,
    MERGE_GATE_TYPE,
    GateEvidence,
    GateEvidenceRefusal,
    GateEvidenceStore,
    GateExecutionInfraError,
    GateWorkspaceUnusable,
    binding_digest,
    candidate_receipt_rejections,
    evidence_rejections,
    workspace_digest_for,
)
from omniagentos.scheduler.gate_evidence import SCHEMA as GATE_EVIDENCE_SCHEMA
from omniagentos.scheduler.gate_runner import (
    EcosystemGateRunner,
    GateRunRequest,
    default_gate_runner,
    produce_gate_evidence,
)
from omniagentos.scheduler.routines import RoutineValidationError, validate_routine
from tests.routines.conftest import valid_routine_payload

# --- fixtures ---------------------------------------------------------------


@pytest.fixture
def store(tmp_path: Path) -> GateEvidenceStore:
    return GateEvidenceStore(tmp_path / "gate-evidence")


def _git_init(root: Path) -> None:
    subprocess.run(["git", "init"], cwd=str(root), check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.name", "Test"], cwd=str(root), check=True, capture_output=True
    )
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"],
        cwd=str(root),
        check=True,
        capture_output=True,
    )
    subprocess.run(["git", "add", "-A"], cwd=str(root), check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=str(root), check=True, capture_output=True)


def _fixture_workspace(tmp_path: Path, files: dict[str, str]) -> Path:
    """A committed, tiny project of whatever ecosystem *files* describes."""
    root = tmp_path / "workspace"
    root.mkdir(parents=True, exist_ok=True)
    (root / ".gitignore").write_text(
        "node_modules\ntarget\nvar\n.pytest_cache\n__pycache__\n", encoding="utf-8"
    )
    for relative, content in files.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    _git_init(root)
    return root


GO_PROJECT = {
    "go.mod": "module example.com/demo\n\ngo 1.22\n",
    "pkg/demo_test.go": 'package demo\n\nimport "testing"\n\nfunc TestOne(t *testing.T) {}\n',
}

CARGO_PROJECT = {
    "crate/Cargo.toml": '[package]\nname = "demo"\nversion = "0.1.0"\nedition = "2021"\n',
    "crate/src/lib.rs": "pub fn add(a: i32, b: i32) -> i32 { a + b }\n",
}

NPM_PROJECT = {
    "package.json": '{"name":"demo","devDependencies":{"vitest":"^2.1.0"}}\n',
    "app/tests/demo.test.ts": "import {test, expect} from 'vitest'\ntest('a', () => expect(1).toBe(1))\n",
}


def _install_stub(bin_dir: Path, name: str, script: str) -> Path:
    bin_dir.mkdir(parents=True, exist_ok=True)
    path = bin_dir / name
    path.write_text(script, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return path


@pytest.fixture
def stub_bin(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A PATH prefix the executors — and their children — resolve through."""
    bin_dir = tmp_path / "stub-bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    scratch_tmp = tmp_path / "child-tmp"
    scratch_tmp.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}")
    # TMPDIR is on the child environment allowlist, so it is the one channel a
    # stub can report its own argv back through. GATE_STUB_ARGV_FILE cannot be
    # used: the allowlist drops it, which is exactly the behaviour under test
    # elsewhere.
    monkeypatch.setenv("TMPDIR", f"{scratch_tmp}{os.sep}")
    for var in ("OMNIAGENTOS_GATE_GO", "OMNIAGENTOS_GATE_CARGO", "OMNIAGENTOS_GATE_VITEST"):
        monkeypatch.delenv(var, raising=False)
    return bin_dir


def _recorded_argv(scratch_tmp: Path) -> str:
    return (scratch_tmp / "gate-stub-argv.txt").read_text(encoding="utf-8")


def _go_stub(body: str, *, exit_code: int = 0) -> str:
    return (
        "#!/bin/sh\n"
        'if [ "$1" = "version" ]; then echo "go version go1.22.0 darwin/arm64"; exit 0; fi\n'
        'printf "%s" "$*" > "${TMPDIR:-/tmp}/gate-stub-argv.txt" 2>/dev/null || true\n'
        f"cat <<'STUBEOF'\n{body}\nSTUBEOF\n"
        f"exit {exit_code}\n"
    )


def _cargo_stub(body: str, *, exit_code: int = 0) -> str:
    return (
        "#!/bin/sh\n"
        'if [ "$1" = "--version" ]; then echo "cargo 1.87.0 (Homebrew)"; exit 0; fi\n'
        'printf "%s" "$*" > "${TMPDIR:-/tmp}/gate-stub-argv.txt" 2>/dev/null || true\n'
        f"cat <<'STUBEOF'\n{body}\nSTUBEOF\n"
        f"exit {exit_code}\n"
    )


def _vitest_stub(xml: str | None, *, exit_code: int = 0) -> str:
    write = ""
    if xml is not None:
        write = (
            'out=""\n'
            'for arg in "$@"; do\n'
            '  case "$arg" in --outputFile=*) out="${arg#--outputFile=}";; esac\n'
            "done\n"
            'if [ -n "$out" ]; then\n'
            f"cat > \"$out\" <<'STUBEOF'\n{xml}\nSTUBEOF\n"
            "fi\n"
        )
    return (
        "#!/bin/sh\n"
        'if [ "$1" = "--version" ]; then echo "vitest/2.1.0 darwin-arm64"; exit 0; fi\n'
        'printf "%s" "$*" > "${TMPDIR:-/tmp}/gate-stub-argv.txt" 2>/dev/null || true\n'
        f"{write}"
        f"exit {exit_code}\n"
    )


GO_PKG = "example.com/demo/pkg"


def _go_stream(*events: dict[str, str]) -> str:
    return "\n".join(json.dumps(event) for event in events)


#: A REAL `go test -json` stream brackets the package: `start` first, a terminal
#: package result last. Both are required by the parser, so the fixtures carry
#: them exactly as the toolchain emits them.
GO_TWO_PASSING = _go_stream(
    {"Action": "start", "Package": GO_PKG},
    {"Action": "run", "Package": GO_PKG, "Test": "TestOne"},
    {"Action": "pass", "Package": GO_PKG, "Test": "TestOne"},
    {"Action": "run", "Package": GO_PKG, "Test": "TestTwo"},
    {"Action": "pass", "Package": GO_PKG, "Test": "TestTwo"},
    {"Action": "pass", "Package": GO_PKG},
)

GO_ONE_FAILING = _go_stream(
    {"Action": "start", "Package": GO_PKG},
    {"Action": "run", "Package": GO_PKG, "Test": "TestOne"},
    {"Action": "pass", "Package": GO_PKG, "Test": "TestOne"},
    {"Action": "run", "Package": GO_PKG, "Test": "TestTwo"},
    {"Action": "fail", "Package": GO_PKG, "Test": "TestTwo"},
    {"Action": "fail", "Package": GO_PKG},
)

VITEST_TWO_PASSING = """<?xml version="1.0" encoding="UTF-8" ?>
<testsuites name="vitest tests" tests="2" failures="0" errors="0" time="0.02">
    <testsuite name="app/tests/demo.test.ts" timestamp="2026-08-04T12:00:00" hostname="host" tests="2" failures="0" errors="0" skipped="0" time="0.02">
        <testcase classname="app/tests/demo.test.ts" name="a" time="0.01" />
        <testcase classname="app/tests/demo.test.ts" name="b" time="0.01" />
    </testsuite>
</testsuites>"""

VITEST_ONE_FAILING = """<?xml version="1.0" encoding="UTF-8" ?>
<testsuites name="vitest tests" tests="2" failures="1" errors="0" time="0.02">
    <testsuite name="app/tests/demo.test.ts" timestamp="2026-08-04T12:00:00" hostname="host" tests="2" failures="1" errors="0" skipped="0" time="0.02">
        <testcase classname="app/tests/demo.test.ts" name="a" time="0.01" />
        <testcase classname="app/tests/demo.test.ts" name="b" time="0.01">
            <failure message="expected 1 to be 2">AssertionError</failure>
        </testcase>
    </testsuite>
</testsuites>"""

VITEST_EMPTY = """<?xml version="1.0" encoding="UTF-8" ?>
<testsuites name="vitest tests" tests="0" failures="0" errors="0" time="0">
    <testsuite name="app/tests/demo.test.ts" timestamp="2026-08-04T12:00:00" hostname="host" tests="0" failures="0" errors="0" skipped="0" time="0">
    </testsuite>
</testsuites>"""

CARGO_TWO_PASSING = """
running 2 tests
test tests::adds ... ok
test tests::adds_negative ... ok

test result: ok. 2 passed; 0 failed; 0 ignored; 0 measured; 0 filtered out; finished in 0.00s
"""

CARGO_ONE_FAILING = """
running 2 tests
test tests::adds ... ok
test tests::adds_negative ... FAILED

failures:
    tests::adds_negative

test result: FAILED. 1 passed; 1 failed; 0 ignored; 0 measured; 0 filtered out; finished in 0.00s
"""

CARGO_ZERO = """
running 0 tests

test result: ok. 0 passed; 0 failed; 0 ignored; 0 measured; 0 filtered out; finished in 0.00s
"""


def _vitest_pin(workspace: Path) -> str:
    """The operator's approved config digest for *workspace*.

    Setup, not assertion: an npm gate is required to carry a pin, so every
    npm test that is about something ELSE has to supply the correct one. The
    pin MECHANISM is pinned independently, by tests that change the tree after
    pinning and assert refusal without ever recomputing a digest — those cannot
    be satisfied by a constant.
    """
    return NpmVitestExecutor().config_digest(workspace)


def _request(workspace: Path, command: str, ecosystem: str, **overrides: object) -> GateRunRequest:
    config: dict[str, object] = {
        "command": command,
        "expected_exit_code": 0,
        "ecosystem": ecosystem,
    }
    if ecosystem == "npm":
        config["vitest_config_digest"] = _vitest_pin(workspace)
    config.update(overrides.pop("gate_config", {}))  # type: ignore[arg-type]
    fields: dict[str, object] = {
        "routine_id": "rt-1",
        "run_id": "run-1",
        "iteration": 1,
        "gate_type": "test_command",
        "gate_config": config,
        "workspace": workspace,
    }
    fields.update(overrides)
    return GateRunRequest(**fields)  # type: ignore[arg-type]


def _rejections(evidence: object, workspace: Path, command: str, ecosystem: str) -> list[str]:
    return evidence_rejections(
        evidence,
        routine_id="rt-1",
        run_id="run-1",
        iteration=1,
        gate_type="test_command",
        gate_config={"command": command, "expected_exit_code": 0, "ecosystem": ecosystem},
        workspace_digest=workspace_digest_for(workspace),
        now=datetime.now(UTC),
    )


# --- config selection: the trust boundary -----------------------------------


def test_absent_ecosystem_means_python_and_nothing_else() -> None:
    """Every routine written before M2 keeps its meaning exactly."""
    assert normalize_ecosystem(None) == ECOSYSTEM_PYTHON
    assert ecosystem_of_gate_config({"command": "pytest suite"}) == ECOSYSTEM_PYTHON
    assert ecosystem_of_gate_config({"command": "pytest suite", "ecosystem": None}) == (
        ECOSYSTEM_PYTHON
    )
    assert expected_tool_for_gate_config({"command": "pytest suite"}) == "pytest"


@pytest.mark.parametrize(
    "value",
    ["", "PYTHON", "Go", "nodejs", "node", "javascript", "rust", "npm ", "pytest", 5, True, []],
)
def test_unknown_ecosystem_is_refused_and_never_falls_back_to_python(value: object) -> None:
    """Failing open here would silently grade a Go project with pytest."""
    with pytest.raises(GateEvidenceRefusal):
        normalize_ecosystem(value)
    with pytest.raises(GateEvidenceRefusal):
        ecosystem_of_gate_config({"command": "go test pkg", "ecosystem": value})


def test_supported_ecosystems_is_the_complete_declared_set() -> None:
    assert SUPPORTED_ECOSYSTEMS == {"python", "npm", "go", "cargo"}
    with pytest.raises(GateEvidenceRefusal):
        executor_for(ECOSYSTEM_PYTHON)  # python is not executed by this seam


# --- per-ecosystem command grammar ------------------------------------------


@pytest.mark.parametrize(
    ("ecosystem", "command"),
    [
        ("go", "go test pkg"),
        ("go", "go test pkg ./other"),
        ("cargo", "cargo test crate"),
        ("npm", "vitest run app/tests"),
        ("npm", "vitest run app/tests/a.test.ts app/tests/b.test.ts"),
    ],
)
def test_ecosystem_verifier_shapes_are_accepted(ecosystem: str, command: str) -> None:
    assert ecosystem_command_is_valid(ecosystem, command)


@pytest.mark.parametrize(
    ("ecosystem", "command"),
    [
        ("go", "go build ./pkg"),
        ("go", "go test -run TestOnlyThisOne pkg"),
        ("go", "go test"),
        ("go", "go test /etc/passwd"),
        ("go", "go test ../outside"),
        ("go", "go test pkg && echo ok"),
        ("go", "cargo test crate"),
        ("cargo", "cargo build crate"),
        ("cargo", "cargo test crate extra_name_filter"),
        ("cargo", "cargo test --all-features crate"),
        ("cargo", "cargo test"),
        ("npm", "vitest app/tests"),
        ("npm", "npm test"),
        ("npm", "vitest run --coverage app/tests"),
        ("npm", "vitest run"),
    ],
)
def test_non_verifier_shapes_are_refused(ecosystem: str, command: str) -> None:
    """Flags, name filters, escapes and wrong programs never become a gate."""
    assert not ecosystem_command_is_valid(ecosystem, command)


def test_cargo_takes_exactly_one_manifest_target() -> None:
    """A second positional would be read by cargo as a TEST NAME FILTER."""
    assert not ecosystem_command_is_valid("cargo", "cargo test a b")
    assert CargoTestExecutor().build_argv("/bin/cargo", ("crate",), Path("/tmp")) == [
        "/bin/cargo",
        "test",
        "--locked",
        "--offline",
        "--no-fail-fast",
        "--manifest-path",
        "crate/Cargo.toml",
    ]


# --- the routine write path -------------------------------------------------


def _payload(**gate_config: object) -> dict[str, object]:
    return valid_routine_payload(
        gate_type="test_command",
        gate_config={"expected_exit_code": 0, **gate_config},
    )


def test_write_path_stores_an_ecosystem_gate() -> None:
    validate_routine(_payload(command="go test pkg", ecosystem="go"))
    validate_routine(_payload(command="cargo test crate", ecosystem="cargo"))
    # npm additionally carries the operator's approved config digest — see
    # test_write_path_requires_the_npm_config_pin for why it is mandatory.
    validate_routine(
        _payload(command="vitest run app", ecosystem="npm", vitest_config_digest="a" * 64)
    )


def test_write_path_refuses_an_unknown_ecosystem() -> None:
    with pytest.raises(RoutineValidationError) as excinfo:
        validate_routine(_payload(command="go test pkg", ecosystem="golang"))
    assert any("ecosystem" in error for error in excinfo.value.errors)


def test_write_path_refuses_a_command_from_the_wrong_ecosystem() -> None:
    with pytest.raises(RoutineValidationError):
        validate_routine(_payload(command="pytest suite", ecosystem="go"))
    with pytest.raises(RoutineValidationError):
        validate_routine(_payload(command="go test pkg", ecosystem="python"))


def test_write_path_python_rules_are_unchanged() -> None:
    validate_routine(_payload(command="pytest tests"))
    validate_routine(_payload(command="pytest tests", ecosystem="python"))
    with pytest.raises(RoutineValidationError):
        validate_routine(_payload(command="echo ok"))


# --- happy paths: counted evidence the seam accepts -------------------------


def test_go_gate_produces_counted_evidence_the_seam_accepts(
    store: GateEvidenceStore, tmp_path: Path, stub_bin: Path
) -> None:
    _install_stub(stub_bin, "go", _go_stub(GO_TWO_PASSING))
    workspace = _fixture_workspace(tmp_path, GO_PROJECT)

    evidence = EcosystemGateRunner(store).run(_request(workspace, "go test pkg", "go"))

    assert (evidence.checks_collected, evidence.checks_passed, evidence.checks_failed) == (2, 2, 0)
    assert evidence.tool == "go test"
    assert evidence.tool_version.startswith("go version")
    assert Path(evidence.interpreter).is_absolute()
    assert _rejections(evidence, workspace, "go test pkg", "go") == []


def test_npm_gate_produces_counted_evidence_the_seam_accepts(
    store: GateEvidenceStore, tmp_path: Path, stub_bin: Path
) -> None:
    _install_stub(stub_bin, "vitest", _vitest_stub(VITEST_TWO_PASSING))
    workspace = _fixture_workspace(tmp_path, NPM_PROJECT)

    evidence = EcosystemGateRunner(store).run(_request(workspace, "vitest run app/tests", "npm"))

    assert (evidence.checks_collected, evidence.checks_passed, evidence.checks_failed) == (2, 2, 0)
    assert evidence.tool == "vitest"
    assert _rejections(evidence, workspace, "vitest run app/tests", "npm") == []


def test_cargo_gate_produces_counted_evidence_the_seam_accepts(
    store: GateEvidenceStore, tmp_path: Path, stub_bin: Path
) -> None:
    _install_stub(stub_bin, "cargo", _cargo_stub(CARGO_TWO_PASSING))
    workspace = _fixture_workspace(tmp_path, CARGO_PROJECT)

    evidence = EcosystemGateRunner(store).run(_request(workspace, "cargo test crate", "cargo"))

    assert (evidence.checks_collected, evidence.checks_passed, evidence.checks_failed) == (2, 2, 0)
    assert evidence.tool == "cargo test"
    assert _rejections(evidence, workspace, "cargo test crate", "cargo") == []


# --- failing suites are COUNTED, then rejected ------------------------------


def test_go_failing_suite_is_counted_and_rejected(
    store: GateEvidenceStore, tmp_path: Path, stub_bin: Path
) -> None:
    _install_stub(stub_bin, "go", _go_stub(GO_ONE_FAILING, exit_code=1))
    workspace = _fixture_workspace(tmp_path, GO_PROJECT)

    evidence = EcosystemGateRunner(store).run(_request(workspace, "go test pkg", "go"))

    assert (evidence.checks_collected, evidence.checks_passed, evidence.checks_failed) == (2, 1, 1)
    rejections = _rejections(evidence, workspace, "go test pkg", "go")
    assert any("1 failed checks" in reason for reason in rejections), rejections


def test_npm_failing_suite_is_counted_and_rejected(
    store: GateEvidenceStore, tmp_path: Path, stub_bin: Path
) -> None:
    _install_stub(stub_bin, "vitest", _vitest_stub(VITEST_ONE_FAILING, exit_code=1))
    workspace = _fixture_workspace(tmp_path, NPM_PROJECT)

    evidence = EcosystemGateRunner(store).run(_request(workspace, "vitest run app/tests", "npm"))

    assert (evidence.checks_collected, evidence.checks_failed) == (2, 1)
    rejections = _rejections(evidence, workspace, "vitest run app/tests", "npm")
    assert any("1 failed checks" in reason for reason in rejections), rejections


def test_cargo_failing_suite_is_counted_and_rejected(
    store: GateEvidenceStore, tmp_path: Path, stub_bin: Path
) -> None:
    _install_stub(stub_bin, "cargo", _cargo_stub(CARGO_ONE_FAILING, exit_code=101))
    workspace = _fixture_workspace(tmp_path, CARGO_PROJECT)

    evidence = EcosystemGateRunner(store).run(_request(workspace, "cargo test crate", "cargo"))

    assert (evidence.checks_collected, evidence.checks_passed, evidence.checks_failed) == (2, 1, 1)
    rejections = _rejections(evidence, workspace, "cargo test crate", "cargo")
    assert any("1 failed checks" in reason for reason in rejections), rejections


# --- COUNTERFEIT (a): a fabricated report on disk is not evidence -----------


def test_fabricated_junit_xml_in_the_candidate_tree_is_not_counted(
    store: GateEvidenceStore, tmp_path: Path, stub_bin: Path
) -> None:
    """A committed ``junit.xml`` claiming 999 passes must never be read.

    The stub runs and exits 0 but writes NO report. Evidence may only come from
    the artifact directory this invocation minted, so the counterfeit on disk is
    invisible and the run produces no counted evidence at all.
    """
    _install_stub(stub_bin, "vitest", _vitest_stub(None))
    forged = (
        """<?xml version="1.0" encoding="UTF-8" ?>
<testsuites name="vitest tests" tests="999" failures="0" errors="0" time="0.1">
    <testsuite name="app/tests/demo.test.ts" tests="999" failures="0" errors="0" skipped="0" time="0.1">
"""
        + "\n".join(
            f'        <testcase classname="app/tests/demo.test.ts" name="forged{index}" time="0"/>'
            for index in range(999)
        )
        + """
    </testsuite>
</testsuites>"""
    )
    # Every plausible place a naive implementation might look, INCLUDING the
    # executor's own report filename at the tree root. A fallback of the shape
    # "if the scratch report is missing, try the run tree" would find one of
    # these and count 999 passes.
    workspace = _fixture_workspace(
        tmp_path,
        {
            **NPM_PROJECT,
            "junit.xml": forged,
            "test-results/junit.xml": forged,
            "vitest-junit.xml": forged,
            "app/tests/vitest-junit.xml": forged,
        },
    )

    with pytest.raises(GateExecutionInfraError) as excinfo:
        EcosystemGateRunner(store).run(_request(workspace, "vitest run app/tests", "npm"))

    assert "no JUnit report" in str(excinfo.value)
    assert store.load("rt-1", "run-1") is None


def test_a_symlink_planted_at_the_report_path_is_not_evidence(
    store: GateEvidenceStore, tmp_path: Path, stub_bin: Path
) -> None:
    """The child knows the report path — it is on its own command line.

    So the cheapest attack is not writing a report at all but pointing the name
    at one the candidate already committed. ``O_NOFOLLOW`` makes that fail at
    open() instead of silently delivering the forgery.
    """
    forged = VITEST_TWO_PASSING.replace('tests="2"', 'tests="900"')
    _install_stub(
        stub_bin,
        "vitest",
        "#!/bin/sh\n"
        'if [ "$1" = "--version" ]; then echo "vitest/2.1.0"; exit 0; fi\n'
        'out=""\n'
        'for arg in "$@"; do\n'
        '  case "$arg" in --outputFile=*) out="${arg#--outputFile=}";; esac\n'
        "done\n"
        'ln -s "$PWD/forged.xml" "$out"\n'
        "exit 0\n",
    )
    workspace = _fixture_workspace(tmp_path, {**NPM_PROJECT, "forged.xml": forged})

    with pytest.raises(GateExecutionInfraError) as excinfo:
        EcosystemGateRunner(store).run(_request(workspace, "vitest run app/tests", "npm"))

    assert "regular file" in str(excinfo.value)
    assert store.load("rt-1", "run-1") is None


def test_a_report_left_by_a_previous_run_cannot_be_reused(
    store: GateEvidenceStore, tmp_path: Path, stub_bin: Path
) -> None:
    """The artifact directory is minted per invocation and proved empty."""
    _install_stub(stub_bin, "vitest", _vitest_stub(VITEST_TWO_PASSING))
    workspace = _fixture_workspace(tmp_path, NPM_PROJECT)
    runner = EcosystemGateRunner(store)
    first = runner.run(_request(workspace, "vitest run app/tests", "npm"))
    assert first.checks_collected == 2

    _install_stub(stub_bin, "vitest", _vitest_stub(None))
    with pytest.raises(GateExecutionInfraError):
        runner.run(_request(workspace, "vitest run app/tests", "npm", run_id="run-2"))


# --- COUNTERFEIT (b): exit 0 with zero tests is not a pass ------------------


@pytest.mark.parametrize(
    ("ecosystem", "program", "script", "command"),
    [
        ("npm", "vitest", _vitest_stub(VITEST_EMPTY), "vitest run app/tests"),
        (
            "go",
            "go",
            _go_stub(json.dumps({"Action": "pass", "Package": "example.com/demo/pkg"})),
            "go test pkg",
        ),
        ("cargo", "cargo", _cargo_stub(CARGO_ZERO), "cargo test crate"),
    ],
)
def test_zero_tests_with_exit_zero_is_refused_not_passed(
    store: GateEvidenceStore,
    tmp_path: Path,
    stub_bin: Path,
    ecosystem: str,
    program: str,
    script: str,
    command: str,
) -> None:
    """A project with no tests is NOT gate-valid, however cleanly it exits."""
    _install_stub(stub_bin, program, script)
    workspace = _fixture_workspace(tmp_path, {**GO_PROJECT, **CARGO_PROJECT, **NPM_PROJECT})

    with pytest.raises(GateEvidenceRefusal) as excinfo:
        EcosystemGateRunner(store).run(_request(workspace, command, ecosystem))

    assert "zero checks" in str(excinfo.value)
    assert store.load("rt-1", "run-1") is None


def test_a_test_that_started_but_never_finished_is_a_partial_execution(
    store: GateEvidenceStore, tmp_path: Path, stub_bin: Path
) -> None:
    """A started-but-unterminated test counts as collected and not as passed.

    The seam then refuses it as a partial execution, which is the honest
    reading: the stream never said what happened to it.
    """
    truncated = _go_stream(
        {"Action": "start", "Package": GO_PKG},
        {"Action": "run", "Package": GO_PKG, "Test": "TestOne"},
        {"Action": "pass", "Package": GO_PKG, "Test": "TestOne"},
        {"Action": "run", "Package": GO_PKG, "Test": "TestTwo"},
        {"Action": "pass", "Package": GO_PKG},
    )
    _install_stub(stub_bin, "go", _go_stub(truncated))
    workspace = _fixture_workspace(tmp_path, GO_PROJECT)

    evidence = EcosystemGateRunner(store).run(_request(workspace, "go test pkg", "go"))

    assert (evidence.checks_collected, evidence.checks_passed) == (2, 1)
    rejections = _rejections(evidence, workspace, "go test pkg", "go")
    assert any("partial execution" in reason for reason in rejections), rejections


# --- COUNTERFEIT (c): a success line without machine-readable events --------


def test_go_stub_printing_a_success_line_without_json_events_is_refused(
    store: GateEvidenceStore, tmp_path: Path, stub_bin: Path
) -> None:
    """``ok example.com/demo/pkg 0.012s`` is a sentence, not evidence."""
    _install_stub(stub_bin, "go", _go_stub("ok  \texample.com/demo/pkg\t0.012s"))
    workspace = _fixture_workspace(tmp_path, GO_PROJECT)

    with pytest.raises(GateExecutionInfraError) as excinfo:
        EcosystemGateRunner(store).run(_request(workspace, "go test pkg", "go"))

    assert "not a JSON event" in str(excinfo.value)
    assert store.load("rt-1", "run-1") is None


def test_go_cached_results_are_disabled_by_the_executors_own_argv(
    store: GateEvidenceStore, tmp_path: Path, stub_bin: Path
) -> None:
    """``-count=1``: a cached ``ok`` describes some earlier tree, not this one."""
    _install_stub(stub_bin, "go", _go_stub(GO_TWO_PASSING))
    workspace = _fixture_workspace(tmp_path, GO_PROJECT)

    EcosystemGateRunner(store).run(_request(workspace, "go test pkg", "go"))

    # EXACT argv, not substrings: an implementation that drops `-count=1`, adds a
    # `-run` name filter, or renames the package must fail this, and a substring
    # assertion would let two of those three through.
    assert _recorded_argv(tmp_path / "child-tmp") == "test -json -count=1 ./pkg"


# --- COUNTERFEIT (d): a report that disagrees with itself -------------------


def test_cargo_summary_inflated_above_its_test_lines_is_refused(
    store: GateEvidenceStore, tmp_path: Path, stub_bin: Path
) -> None:
    """Two ``ok`` lines cannot support a summary claiming five passes."""
    inflated = """
running 5 tests
test tests::adds ... ok
test tests::adds_negative ... ok

test result: ok. 5 passed; 0 failed; 0 ignored; 0 measured; 0 filtered out; finished in 0.00s
"""
    _install_stub(stub_bin, "cargo", _cargo_stub(inflated))
    workspace = _fixture_workspace(tmp_path, CARGO_PROJECT)

    with pytest.raises(GateExecutionInfraError) as excinfo:
        EcosystemGateRunner(store).run(_request(workspace, "cargo test crate", "cargo"))

    assert "disagrees" in str(excinfo.value)
    assert store.load("rt-1", "run-1") is None


def test_cargo_output_without_a_summary_line_is_refused(
    store: GateEvidenceStore, tmp_path: Path, stub_bin: Path
) -> None:
    _install_stub(stub_bin, "cargo", _cargo_stub("running 2 tests\ntest a ... ok\ntest b ... ok"))
    workspace = _fixture_workspace(tmp_path, CARGO_PROJECT)

    with pytest.raises(GateExecutionInfraError) as excinfo:
        EcosystemGateRunner(store).run(_request(workspace, "cargo test crate", "cargo"))

    assert "no `test result:` summary" in str(excinfo.value)


def test_npm_junit_attributes_disagreeing_with_its_testcases_is_refused(
    store: GateEvidenceStore, tmp_path: Path, stub_bin: Path
) -> None:
    """A truncated or edited report is inconclusive, never a smaller green."""
    tampered = VITEST_TWO_PASSING.replace('tests="2"', 'tests="7"')
    _install_stub(stub_bin, "vitest", _vitest_stub(tampered))
    workspace = _fixture_workspace(tmp_path, NPM_PROJECT)

    with pytest.raises(GateExecutionInfraError) as excinfo:
        EcosystemGateRunner(store).run(_request(workspace, "vitest run app/tests", "npm"))

    assert "disagrees" in str(excinfo.value)


@pytest.mark.parametrize(
    ("label", "report"),
    [
        # Testcases removed but the header kept: the truncation a half-written
        # file actually produces. A check that only compared FAILURES would miss
        # this, and a check that only summed testcases would call it 1/1 green.
        (
            "testcases truncated away",
            """<?xml version="1.0" encoding="UTF-8" ?>
<testsuites name="vitest tests" tests="2" failures="0" errors="0" time="0.02">
    <testsuite name="app/tests/demo.test.ts" tests="2" failures="0" errors="0" skipped="0" time="0.02">
        <testcase classname="app/tests/demo.test.ts" name="a" time="0.01" />
    </testsuite>
</testsuites>""",
        ),
        # A failure element present while the attributes claim a clean run: the
        # shape a tampered report takes when someone edits the header only.
        (
            "failure understated in the attributes",
            """<?xml version="1.0" encoding="UTF-8" ?>
<testsuites name="vitest tests" tests="2" failures="0" errors="0" time="0.02">
    <testsuite name="app/tests/demo.test.ts" tests="2" failures="0" errors="0" skipped="0" time="0.02">
        <testcase classname="app/tests/demo.test.ts" name="a" time="0.01" />
        <testcase classname="app/tests/demo.test.ts" name="b" time="0.01">
            <failure message="boom">AssertionError</failure>
        </testcase>
    </testsuite>
</testsuites>""",
        ),
        # Errors counted separately from failures — a report whose `errors`
        # attribute is dropped is still inconsistent with its own elements.
        (
            "error element not reflected in the attributes",
            """<?xml version="1.0" encoding="UTF-8" ?>
<testsuites name="vitest tests" tests="1" failures="0" errors="0" time="0.02">
    <testsuite name="app/tests/demo.test.ts" tests="1" failures="0" errors="0" skipped="0" time="0.02">
        <testcase classname="app/tests/demo.test.ts" name="a" time="0.01">
            <error message="boom">TypeError</error>
        </testcase>
    </testsuite>
</testsuites>""",
        ),
    ],
)
def test_junit_reports_inconsistent_with_their_own_attributes_are_refused(
    store: GateEvidenceStore, tmp_path: Path, stub_bin: Path, label: str, report: str
) -> None:
    """Each shape a tampered or half-written JUnit file actually takes."""
    _install_stub(stub_bin, "vitest", _vitest_stub(report))
    workspace = _fixture_workspace(tmp_path, NPM_PROJECT)

    with pytest.raises(GateExecutionInfraError) as excinfo:
        EcosystemGateRunner(store).run(_request(workspace, "vitest run app/tests", "npm"))

    assert "disagrees" in str(excinfo.value), label
    assert store.load("rt-1", "run-1") is None


def test_an_errored_vitest_case_is_counted_as_failed_not_as_inconsistent(
    store: GateEvidenceStore, tmp_path: Path, stub_bin: Path
) -> None:
    """`<error>` is a failure, and a report that says so honestly is CONSISTENT.

    This is the other side of the attribute cross-check, and the side a
    weakening actually reaches: if the declared tally ignored the `errors`
    attribute, this truthful report would read as self-contradictory and settle
    INCONCLUSIVE — turning a real, attributable test failure into "no evidence".
    The report must be accepted as consistent and the verdict must be a failure.
    """
    honest = """<?xml version="1.0" encoding="UTF-8" ?>
<testsuites name="vitest tests" tests="2" failures="0" errors="1" time="0.02">
    <testsuite name="app/tests/demo.test.ts" tests="2" failures="0" errors="1" skipped="0" time="0.02">
        <testcase classname="app/tests/demo.test.ts" name="a" time="0.01" />
        <testcase classname="app/tests/demo.test.ts" name="b" time="0.01">
            <error message="boom">TypeError</error>
        </testcase>
    </testsuite>
</testsuites>"""
    _install_stub(stub_bin, "vitest", _vitest_stub(honest, exit_code=1))
    workspace = _fixture_workspace(tmp_path, NPM_PROJECT)

    evidence = EcosystemGateRunner(store).run(_request(workspace, "vitest run app/tests", "npm"))

    assert (evidence.checks_collected, evidence.checks_passed, evidence.checks_failed) == (2, 1, 1)
    rejections = _rejections(evidence, workspace, "vitest run app/tests", "npm")
    assert any("1 failed checks" in reason for reason in rejections), rejections


def test_go_package_failure_with_no_failing_test_settles_as_a_candidate_failure(
    store: GateEvidenceStore, tmp_path: Path, stub_bin: Path
) -> None:
    """A build error, panic or failing TestMain is the CANDIDATE's defect.

    Classifying it INCONCLUSIVE would park a genuinely broken repository in the
    excluded-from-the-acceptance-floor bucket, where it can never register an
    adverse outcome and can never auto-pause. It is a refusal, which settles.
    """
    stream = _go_stream(
        {"Action": "start", "Package": GO_PKG},
        {"Action": "run", "Package": GO_PKG, "Test": "TestOne"},
        {"Action": "pass", "Package": GO_PKG, "Test": "TestOne"},
        {"Action": "fail", "Package": GO_PKG},
    )
    _install_stub(stub_bin, "go", _go_stub(stream, exit_code=1))
    workspace = _fixture_workspace(tmp_path, GO_PROJECT)
    request = _request(workspace, "go test pkg", "go")

    with pytest.raises(GateEvidenceRefusal) as excinfo:
        EcosystemGateRunner(store).run(request)
    assert "outside the counted tests" in str(excinfo.value)
    assert not isinstance(excinfo.value, GateExecutionInfraError)

    outcome = produce_gate_evidence(EcosystemGateRunner(store), store, request)
    assert outcome.status == "refused"


def test_cargo_compile_failure_settles_as_a_candidate_failure(
    store: GateEvidenceStore, tmp_path: Path, stub_bin: Path
) -> None:
    """Code that does not compile fails its gate; it is not "inconclusive"."""
    _install_stub(
        stub_bin,
        "cargo",
        '#!/bin/sh\nif [ "$1" = "--version" ]; then echo "cargo 1.87.0"; exit 0; fi\n'
        'echo "error[E0308]: mismatched types" >&2\n'
        'echo "error: could not compile \\`demo\\` (lib test) due to 1 previous error" >&2\n'
        "exit 101\n",
    )
    workspace = _fixture_workspace(tmp_path, CARGO_PROJECT)
    request = _request(workspace, "cargo test crate", "cargo")

    with pytest.raises(GateEvidenceRefusal) as excinfo:
        EcosystemGateRunner(store).run(request)
    assert "could not compile" in str(excinfo.value)
    assert not isinstance(excinfo.value, GateExecutionInfraError)

    outcome = produce_gate_evidence(EcosystemGateRunner(store), store, request)
    assert outcome.status == "refused"


def test_cargo_silence_without_a_compile_error_stays_inconclusive(
    store: GateEvidenceStore, tmp_path: Path, stub_bin: Path
) -> None:
    """The reclassification must not swallow genuine instrument silence."""
    _install_stub(stub_bin, "cargo", _cargo_stub("some unrelated chatter"))
    workspace = _fixture_workspace(tmp_path, CARGO_PROJECT)

    with pytest.raises(GateExecutionInfraError):
        EcosystemGateRunner(store).run(_request(workspace, "cargo test crate", "cargo"))


def test_cargo_filtered_out_tests_reach_the_seam_as_deselected(
    store: GateEvidenceStore, tmp_path: Path, stub_bin: Path
) -> None:
    """A filtered run is a partial run, and the seam already refuses those."""
    filtered = """
running 1 test
test tests::adds ... ok

test result: ok. 1 passed; 0 failed; 0 ignored; 0 measured; 4 filtered out; finished in 0.00s
"""
    _install_stub(stub_bin, "cargo", _cargo_stub(filtered))
    workspace = _fixture_workspace(tmp_path, CARGO_PROJECT)

    evidence = EcosystemGateRunner(store).run(_request(workspace, "cargo test crate", "cargo"))

    assert evidence.deselected_count == 4
    rejections = _rejections(evidence, workspace, "cargo test crate", "cargo")
    assert any("4 deselected checks" in reason for reason in rejections), rejections


# --- host facts: absent toolchain is UNAVAILABLE, never a pass --------------


def test_absent_toolchain_is_unavailable_not_a_pass_and_not_a_failure(
    store: GateEvidenceStore, tmp_path: Path, stub_bin: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No Go on this host must not auto-pause every Go routine."""
    workspace = _fixture_workspace(tmp_path, GO_PROJECT)
    request = _request(workspace, "go test pkg", "go")
    # Narrowed only after the fixture is committed: the empty stub dir is the
    # whole PATH, so nothing — including `go` — resolves.
    monkeypatch.setenv("PATH", str(stub_bin))

    with pytest.raises(GateWorkspaceUnusable) as excinfo:
        EcosystemGateRunner(store).run(request)
    assert "toolchain is absent" in str(excinfo.value)

    outcome = produce_gate_evidence(EcosystemGateRunner(store), store, request)
    assert outcome.status == "unavailable"
    assert outcome.evidence is None


def test_a_toolchain_that_cannot_state_its_version_is_unusable(
    store: GateEvidenceStore, tmp_path: Path, stub_bin: Path
) -> None:
    """An unidentifiable binary is not trusted to grade anything."""
    _install_stub(stub_bin, "go", "#!/bin/sh\nexit 3\n")
    workspace = _fixture_workspace(tmp_path, GO_PROJECT)

    with pytest.raises(GateWorkspaceUnusable) as excinfo:
        EcosystemGateRunner(store).run(_request(workspace, "go test pkg", "go"))

    assert "printed no version" in str(excinfo.value)


# --- hangs: INCONCLUSIVE, terminated, never silent --------------------------


def test_a_hanging_toolchain_is_terminated_and_reported_inconclusive(
    store: GateEvidenceStore, tmp_path: Path, stub_bin: Path
) -> None:
    """This repo has been burned by an uncaught gate timeout. Never again."""
    _install_stub(
        stub_bin,
        "go",
        '#!/bin/sh\nif [ "$1" = "version" ]; then echo "go version go1.22.0"; exit 0; fi\n'
        "sleep 120\n",
    )
    workspace = _fixture_workspace(tmp_path, GO_PROJECT)
    request = _request(workspace, "go test pkg", "go")

    runner = EcosystemGateRunner(store, timeout_seconds=1)
    with pytest.raises(GateExecutionInfraError) as excinfo:
        runner.run(request)
    assert "exceeded 1s" in str(excinfo.value)
    assert store.load("rt-1", "run-1") is None

    outcome = produce_gate_evidence(EcosystemGateRunner(store, timeout_seconds=1), store, request)
    assert outcome.status == "unavailable"
    assert outcome.evidence is None


# --- the trust boundary at the decision point -------------------------------


def test_evidence_from_one_ecosystem_is_rejected_against_another_config(
    store: GateEvidenceStore, tmp_path: Path, stub_bin: Path
) -> None:
    """``tool`` is bound to the ecosystem the adjudicated config declares."""
    _install_stub(stub_bin, "go", _go_stub(GO_TWO_PASSING))
    workspace = _fixture_workspace(tmp_path, GO_PROJECT)
    evidence = EcosystemGateRunner(store).run(_request(workspace, "go test pkg", "go"))

    rejections = evidence_rejections(
        evidence,
        routine_id="rt-1",
        run_id="run-1",
        iteration=1,
        gate_type="test_command",
        gate_config={"command": "go test pkg", "expected_exit_code": 0, "ecosystem": "cargo"},
        workspace_digest=workspace_digest_for(workspace),
        now=datetime.now(UTC),
    )
    assert any("declares the 'cargo test' verifier" in reason for reason in rejections), rejections


def test_an_unknown_ecosystem_at_the_decision_point_rejects_the_evidence(
    store: GateEvidenceStore, tmp_path: Path, stub_bin: Path
) -> None:
    _install_stub(stub_bin, "go", _go_stub(GO_TWO_PASSING))
    workspace = _fixture_workspace(tmp_path, GO_PROJECT)
    evidence = EcosystemGateRunner(store).run(_request(workspace, "go test pkg", "go"))

    rejections = evidence_rejections(
        evidence,
        routine_id="rt-1",
        run_id="run-1",
        iteration=1,
        gate_type="test_command",
        gate_config={"command": "go test pkg", "expected_exit_code": 0, "ecosystem": "golang"},
        workspace_digest=workspace_digest_for(workspace),
        now=datetime.now(UTC),
    )
    assert any("usable ecosystem" in reason for reason in rejections), rejections


def test_python_gates_still_execute_on_the_pytest_runner(
    store: GateEvidenceStore, tmp_path: Path
) -> None:
    """The dispatcher delegates; it does not re-implement the Python path."""
    workspace = _fixture_workspace(
        tmp_path, {"suite/test_gate.py": "def test_one() -> None:\n    assert True\n"}
    )

    evidence = EcosystemGateRunner(store).run(
        GateRunRequest(
            routine_id="rt-1",
            run_id="run-1",
            iteration=1,
            gate_type="test_command",
            gate_config={"command": "pytest suite", "expected_exit_code": 0},
            workspace=workspace,
        )
    )

    assert evidence.tool == "pytest"
    assert evidence.checks_collected == 1
    assert _rejections(evidence, workspace, "pytest suite", "python") == []


def test_default_gate_runner_is_the_dispatcher(
    store: GateEvidenceStore, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = tmp_path / "present"
    workspace.mkdir()
    monkeypatch.setenv("OMNIAGENTOS_GATE_WORKSPACE", str(workspace))
    assert isinstance(default_gate_runner(store), EcosystemGateRunner)


# --- FINDING 1: the merge gate is pytest-only, at BOTH ends -----------------
#
# Merge-candidate evidence is written under a FIXED identity (routine_id
# `merge-gate`, run_id = the candidate SHA) and read by merge-gate.sh as
# authorization to merge THIS repository. A stored merge_candidate routine that
# named another ecosystem would settle through the dispatcher and land a signed,
# structurally valid receipt at exactly that path — two passing Go tests in a
# subdirectory authorizing a merge of the whole Python tree.


def test_merge_candidate_routine_cannot_declare_a_non_python_ecosystem() -> None:
    """The admission end: such a routine may not be STORED."""
    payload = valid_routine_payload(
        gate_type="merge_candidate",
        gate_config={
            "command": "go test pkg",
            "expected_exit_code": 0,
            "ecosystem": "go",
            "candidate_sha": "a" * 40,
            "merge_base_sha": "b" * 40,
        },
    )
    with pytest.raises(RoutineValidationError) as excinfo:
        validate_routine(payload)
    assert any("merge_candidate" in error for error in excinfo.value.errors), excinfo.value.errors


def test_merge_candidate_execution_refuses_a_non_python_ecosystem(
    store: GateEvidenceStore, tmp_path: Path, stub_bin: Path
) -> None:
    """The execution end, which also covers rows stored before that validation.

    Stored routine rows are never re-validated, so the write-path check alone
    would leave every pre-existing row as a live path to a forged receipt. This
    refuses where the receipt would actually be minted, and mints nothing.
    """
    _install_stub(stub_bin, "go", _go_stub(GO_TWO_PASSING))
    workspace = _fixture_workspace(tmp_path, GO_PROJECT)
    head = subprocess.run(
        ["git", "-C", str(workspace), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()

    request = GateRunRequest(
        routine_id=MERGE_GATE_ROUTINE_ID,
        run_id=head,
        iteration=1,
        gate_type=MERGE_GATE_TYPE,
        gate_config={"command": "go test pkg", "expected_exit_code": 0, "ecosystem": "go"},
        workspace=workspace,
        candidate_sha=head,
        merge_base_sha=head,
    )

    with pytest.raises(GateEvidenceRefusal) as excinfo:
        EcosystemGateRunner(store).run(request)
    assert "authorizes merging" in str(excinfo.value)
    assert store.load(MERGE_GATE_ROUTINE_ID, head) is None


def test_a_merge_receipt_from_another_ecosystem_is_not_believed(
    store: GateEvidenceStore, tmp_path: Path
) -> None:
    """The adjudication end: even a validly SIGNED non-pytest receipt is refused.

    Constructed and signed with the store's own key, so signature verification
    passes and only the tool check stands between it and a merge.
    """
    candidate = "c" * 40
    merge_base = "d" * 40
    workspace_digest = workspace_digest_for(tmp_path)
    command = "go test pkg"
    targets = ("pkg",)
    unsigned = GateEvidence(
        schema=GATE_EVIDENCE_SCHEMA,
        routine_id=MERGE_GATE_ROUTINE_ID,
        run_id=candidate,
        iteration=1,
        gate_type=MERGE_GATE_TYPE,
        command=command,
        targets=targets,
        workspace_digest=workspace_digest,
        binding_digest=binding_digest(
            routine_id=MERGE_GATE_ROUTINE_ID,
            run_id=candidate,
            iteration=1,
            gate_type=MERGE_GATE_TYPE,
            command=command,
            targets=targets,
            workspace_digest=workspace_digest,
            candidate_sha=candidate,
            merge_base_sha=merge_base,
        ),
        tool="go test",
        tool_version="go version go1.22.0",
        exit_code=0,
        checks_collected=2,
        checks_passed=2,
        checks_skipped=0,
        checks_failed=0,
        started_at="2026-08-04T08:59:00Z",
        finished_at="2026-08-04T09:00:00Z",
        nonce="0123456789abcdef0123456789abcdef",
        workspace_sha=candidate,
        workspace_tree_clean=True,
        interpreter="/usr/local/bin/go",
        interpreter_version="go version go1.22.0",
        node_inventory_digest="0" * 64,
        deselected_count=0,
        candidate_sha=candidate,
        merge_base_sha=merge_base,
    )
    signed = store.sign(unsigned)
    assert store.verify(signed), "the forged receipt must be genuinely well-signed"

    rejections = candidate_receipt_rejections(
        signed,
        candidate_sha=candidate,
        merge_base_sha=merge_base,
        now=datetime(2026, 8, 4, 9, 5, tzinfo=UTC),
    )
    assert any("accepts only 'pytest'" in reason for reason in rejections), rejections


# --- FINDING 2: cargo harness = false is a candidate-supplied grader --------


CARGO_FORGED_HARNESS_OUTPUT = """
running 2 tests
test tests::everything_is_fine ... ok
test tests::still_fine ... ok

test result: ok. 2 passed; 0 failed; 0 ignored; 0 measured; 0 filtered out; finished in 0.00s
"""


def test_cargo_target_with_harness_false_is_refused(
    store: GateEvidenceStore, tmp_path: Path, stub_bin: Path
) -> None:
    """The forged output is PERFECT — and that is exactly why the manifest decides.

    With ``harness = false`` the candidate's own ``main()`` writes the entire
    "libtest" block, so the per-test lines, the summary and the cross-check
    between them all agree while nothing was asserted. No parser can tell this
    apart from a real run; only the manifest can.
    """
    _install_stub(stub_bin, "cargo", _cargo_stub(CARGO_FORGED_HARNESS_OUTPUT))
    workspace = _fixture_workspace(
        tmp_path,
        {
            "crate/Cargo.toml": (
                '[package]\nname = "demo"\nversion = "0.1.0"\nedition = "2021"\n\n'
                '[[test]]\nname = "forged"\npath = "tests/forged.rs"\nharness = false\n'
            ),
            "crate/src/lib.rs": "pub fn add(a: i32, b: i32) -> i32 { a + b }\n",
            "crate/tests/forged.rs": (
                'fn main() {\n    println!("running 2 tests");\n'
                '    println!("test tests::everything_is_fine ... ok");\n'
                '    println!("test result: ok. 2 passed; 0 failed; 0 ignored; '
                '0 measured; 0 filtered out; finished in 0.00s");\n}\n'
            ),
        },
    )

    with pytest.raises(GateEvidenceRefusal) as excinfo:
        EcosystemGateRunner(store).run(_request(workspace, "cargo test crate", "cargo"))

    assert "harness = false" in str(excinfo.value)
    assert store.load("rt-1", "run-1") is None


def test_cargo_harness_false_in_a_workspace_member_is_refused(
    store: GateEvidenceStore, tmp_path: Path, stub_bin: Path
) -> None:
    """`--manifest-path` on a workspace root runs its members too, so they count.

    Checking only the named manifest would let the opt-out hide one directory
    down, which is where a real workspace puts its crates anyway.
    """
    _install_stub(stub_bin, "cargo", _cargo_stub(CARGO_FORGED_HARNESS_OUTPUT))
    workspace = _fixture_workspace(
        tmp_path,
        {
            "ws/Cargo.toml": '[workspace]\nmembers = ["crates/*"]\n',
            "ws/crates/honest/Cargo.toml": (
                '[package]\nname = "honest"\nversion = "0.1.0"\nedition = "2021"\n'
            ),
            "ws/crates/honest/src/lib.rs": "pub fn ok() {}\n",
            "ws/crates/sneaky/Cargo.toml": (
                '[package]\nname = "sneaky"\nversion = "0.1.0"\nedition = "2021"\n\n'
                '[[bench]]\nname = "b"\nharness = false\n'
            ),
            "ws/crates/sneaky/src/lib.rs": "pub fn ok() {}\n",
        },
    )

    with pytest.raises(GateEvidenceRefusal) as excinfo:
        EcosystemGateRunner(store).run(_request(workspace, "cargo test ws", "cargo"))

    assert "harness = false" in str(excinfo.value)
    assert "sneaky" in str(excinfo.value) or "'b'" in str(excinfo.value)


def test_cargo_harness_false_in_a_path_dependency_is_refused(
    store: GateEvidenceStore, tmp_path: Path, stub_bin: Path
) -> None:
    """MANDATORY counterfeit: `members` is only half the membership rule.

    Cargo auto-joins every path dependency residing in the workspace directory
    as an IMPLICIT member — it never appears in `workspace.members`, yet
    `cargo test` runs its targets. A `harness = false` crate hidden one
    `path = "../sneaky"` away therefore gets to write its own "libtest" output,
    and the forged block below is internally perfect: per-test lines, summary,
    and the cross-check between them all agree while nothing is asserted.
    """
    _install_stub(stub_bin, "cargo", _cargo_stub(CARGO_FORGED_HARNESS_OUTPUT))
    workspace = _fixture_workspace(
        tmp_path,
        {
            # `members` names ONLY the honest crate.
            "ws/Cargo.toml": '[workspace]\nmembers = ["crates/honest"]\n',
            "ws/crates/honest/Cargo.toml": (
                '[package]\nname = "honest"\nversion = "0.1.0"\nedition = "2021"\n\n'
                '[dependencies]\nsneaky = { path = "../sneaky" }\n'
            ),
            "ws/crates/honest/src/lib.rs": "pub fn ok() {}\n",
            # Never mentioned as a member — an implicit one, via the path dep.
            "ws/crates/sneaky/Cargo.toml": (
                '[package]\nname = "sneaky"\nversion = "0.1.0"\nedition = "2021"\n\n'
                '[[test]]\nname = "forged"\npath = "tests/forged.rs"\nharness = false\n'
            ),
            "ws/crates/sneaky/src/lib.rs": "pub fn ok() {}\n",
            "ws/crates/sneaky/tests/forged.rs": (
                'fn main() {\n    println!("test result: ok. 2 passed; 0 failed; 0 ignored; '
                '0 measured; 0 filtered out; finished in 0.00s");\n}\n'
            ),
        },
    )

    with pytest.raises(GateEvidenceRefusal) as excinfo:
        EcosystemGateRunner(store).run(_request(workspace, "cargo test ws", "cargo"))

    assert "harness = false" in str(excinfo.value)
    assert store.load("rt-1", "run-1") is None


@pytest.mark.parametrize(
    "table",
    ["dependencies", "dev-dependencies", "build-dependencies"],
)
def test_a_path_dependency_is_followed_from_every_dependency_table(
    tmp_path: Path, table: str
) -> None:
    """A path can hide in any dependency table, so all of them are scanned."""
    tree = tmp_path / f"tree-{table}"
    (tree / "crate").mkdir(parents=True)
    (tree / "crate" / "Cargo.toml").write_text(
        '[package]\nname = "demo"\nversion = "0.1.0"\n\n'
        f'[{table}]\nsneaky = {{ path = "../sneaky" }}\n',
        encoding="utf-8",
    )
    (tree / "sneaky").mkdir(parents=True)
    (tree / "sneaky" / "Cargo.toml").write_text(
        '[package]\nname = "sneaky"\nversion = "0.1.0"\n\n[lib]\nharness = false\n',
        encoding="utf-8",
    )

    with pytest.raises(GateEvidenceRefusal) as excinfo:
        CargoTestExecutor().preflight(tree, ("crate",))
    assert "harness = false" in str(excinfo.value)


def test_a_path_dependency_is_followed_from_a_platform_specific_table(
    tmp_path: Path,
) -> None:
    """`[target.'cfg(unix)'.dependencies]` is still a dependency table."""
    tree = tmp_path / "tree"
    (tree / "crate").mkdir(parents=True)
    (tree / "crate" / "Cargo.toml").write_text(
        '[package]\nname = "demo"\nversion = "0.1.0"\n\n'
        "[target.'cfg(unix)'.dev-dependencies]\n"
        'sneaky = { path = "../sneaky" }\n',
        encoding="utf-8",
    )
    (tree / "sneaky").mkdir(parents=True)
    (tree / "sneaky" / "Cargo.toml").write_text(
        '[package]\nname = "sneaky"\nversion = "0.1.0"\n\n[[bench]]\nname = "b"\nharness = false\n',
        encoding="utf-8",
    )

    with pytest.raises(GateEvidenceRefusal) as excinfo:
        CargoTestExecutor().preflight(tree, ("crate",))
    assert "harness = false" in str(excinfo.value)


def _crate(tree: Path, name: str, body: str) -> None:
    (tree / name).mkdir(parents=True, exist_ok=True)
    (tree / name / "Cargo.toml").write_text(body, encoding="utf-8")


def test_path_dependencies_are_followed_transitively(tmp_path: Path) -> None:
    """Depth 2 is not a hiding place either."""
    tree = tmp_path / "tree"
    _crate(
        tree,
        "crate",
        '[package]\nname = "demo"\nversion = "0.1.0"\n\n[dependencies]\nmid = { path = "../mid" }\n',
    )
    _crate(
        tree,
        "mid",
        '[package]\nname = "mid"\nversion = "0.1.0"\n\n[dependencies]\ndeep = { path = "../deep" }\n',
    )
    _crate(tree, "deep", '[package]\nname = "deep"\nversion = "0.1.0"\n\n[lib]\nharness = false\n')

    with pytest.raises(GateEvidenceRefusal) as excinfo:
        CargoTestExecutor().preflight(tree, ("crate",))
    assert "harness = false" in str(excinfo.value)


def test_a_dependency_cycle_terminates(tmp_path: Path) -> None:
    """The cycle guard survives the new traversal."""
    tree = tmp_path / "tree"
    for name, other in (("a", "b"), ("b", "a")):
        _crate(
            tree,
            name,
            f'[package]\nname = "{name}"\nversion = "0.1.0"\n\n'
            f'[dependencies]\n{other} = {{ path = "../{other}" }}\n',
        )
    CargoTestExecutor().preflight(tree, ("a",))


def test_a_path_dependency_outside_the_run_tree_is_refused(tmp_path: Path) -> None:
    """Unverifiable must not buy a green gate."""
    tree = tmp_path / "deep" / "tree"
    _crate(
        tree,
        "crate",
        '[package]\nname = "demo"\nversion = "0.1.0"\n\n'
        '[dependencies]\noutside = { path = "../../outside" }\n',
    )
    (tmp_path / "outside").mkdir(parents=True, exist_ok=True)
    (tmp_path / "outside" / "Cargo.toml").write_text(
        '[package]\nname = "outside"\nversion = "0.1.0"\n', encoding="utf-8"
    )

    with pytest.raises(GateEvidenceRefusal) as excinfo:
        CargoTestExecutor().preflight(tree, ("crate",))
    assert "escapes the workspace" in str(excinfo.value)


def test_a_path_dependency_with_no_manifest_is_refused(tmp_path: Path) -> None:
    """cargo could not build this either; silence is not a pass."""
    tree = tmp_path / "tree"
    _crate(
        tree,
        "crate",
        '[package]\nname = "demo"\nversion = "0.1.0"\n\n'
        '[dependencies]\nghost = { path = "../ghost" }\n',
    )

    with pytest.raises(GateEvidenceRefusal) as excinfo:
        CargoTestExecutor().preflight(tree, ("crate",))
    assert "no manifest" in str(excinfo.value)


def test_an_excluded_directory_is_not_a_member_and_is_not_refused(tmp_path: Path) -> None:
    """Over-refusing is its own defect: `workspace.exclude` is honoured per spec."""
    tree = tmp_path / "tree"
    _crate(tree, "ws", '[workspace]\nmembers = ["crates/*"]\nexclude = ["crates/legacy"]\n')
    _crate(tree, "ws/crates/honest", '[package]\nname = "honest"\nversion = "0.1.0"\n')
    _crate(
        tree,
        "ws/crates/legacy",
        '[package]\nname = "legacy"\nversion = "0.1.0"\n\n[lib]\nharness = false\n',
    )

    CargoTestExecutor().preflight(tree, ("ws",))


def test_a_path_dependency_with_its_own_workspace_table_is_a_separate_workspace(
    tmp_path: Path,
) -> None:
    """Per spec it is NOT auto-joined, so `cargo test` here never runs its targets."""
    tree = tmp_path / "tree"
    _crate(
        tree,
        "crate",
        '[package]\nname = "demo"\nversion = "0.1.0"\n\n'
        '[dependencies]\nseparate = { path = "../separate" }\n',
    )
    _crate(
        tree,
        "separate",
        '[workspace]\n\n[package]\nname = "separate"\nversion = "0.1.0"\n\n'
        "[lib]\nharness = false\n",
    )

    CargoTestExecutor().preflight(tree, ("crate",))


def test_cargo_lib_harness_false_is_refused(tmp_path: Path) -> None:
    """`[lib] harness = false` is a table, not an array — both shapes are checked."""
    tree = tmp_path / "tree"
    (tree / "crate").mkdir(parents=True)
    (tree / "crate" / "Cargo.toml").write_text(
        '[package]\nname = "demo"\nversion = "0.1.0"\n\n[lib]\nharness = false\n',
        encoding="utf-8",
    )
    with pytest.raises(GateEvidenceRefusal) as excinfo:
        CargoTestExecutor().preflight(tree, ("crate",))
    assert "harness = false" in str(excinfo.value)


def test_cargo_manifest_without_harness_opt_out_passes_preflight(tmp_path: Path) -> None:
    """The check must not refuse ordinary crates."""
    tree = tmp_path / "tree"
    (tree / "crate").mkdir(parents=True)
    (tree / "crate" / "Cargo.toml").write_text(
        '[package]\nname = "demo"\nversion = "0.1.0"\n\n[[test]]\nname = "t"\n',
        encoding="utf-8",
    )
    CargoTestExecutor().preflight(tree, ("crate",))


# --- FINDING 3: the vitest config is operator-pinned ------------------------


def test_an_npm_gate_without_a_config_pin_is_refused(
    store: GateEvidenceStore, tmp_path: Path, stub_bin: Path
) -> None:
    """No pin, no gate: the JUnit alone cannot show an excluded test file."""
    _install_stub(stub_bin, "vitest", _vitest_stub(VITEST_TWO_PASSING))
    workspace = _fixture_workspace(tmp_path, NPM_PROJECT)

    request = _request(
        workspace, "vitest run app/tests", "npm", gate_config={"vitest_config_digest": None}
    )
    with pytest.raises(GateEvidenceRefusal) as excinfo:
        EcosystemGateRunner(store).run(request)

    assert "vitest_config_digest" in str(excinfo.value)
    assert store.load("rt-1", "run-1") is None


def test_a_config_added_after_pinning_stops_the_gate(
    store: GateEvidenceStore, tmp_path: Path, stub_bin: Path
) -> None:
    """THE decisive test for the pin, and it never recomputes a digest.

    A candidate that commits a ``vitest.config.ts`` excluding its failing tests
    would otherwise produce a flawless, internally consistent, entirely green
    report with ``deselected = 0``. The pin was taken against a tree that had no
    config; the tree now has one; the gate stops until a human looks. Because
    the expected value here is a digest of a DIFFERENT tree, an implementation
    that returned a constant — or that ignored absent files — fails this.
    """
    _install_stub(stub_bin, "vitest", _vitest_stub(VITEST_TWO_PASSING))
    pinned_tree = _fixture_workspace(tmp_path / "pinned", NPM_PROJECT)
    approved_digest = _vitest_pin(pinned_tree)

    hostile = _fixture_workspace(
        tmp_path / "hostile",
        {
            **NPM_PROJECT,
            "vitest.config.ts": ("export default { test: { exclude: ['**/failing.test.ts'] } }\n"),
        },
    )

    request = _request(
        hostile,
        "vitest run app/tests",
        "npm",
        gate_config={"vitest_config_digest": approved_digest},
    )
    with pytest.raises(GateEvidenceRefusal) as excinfo:
        EcosystemGateRunner(store).run(request)

    assert "not the one this gate pinned" in str(excinfo.value)
    assert store.load("rt-1", "run-1") is None


@pytest.mark.parametrize(
    "filename",
    [
        "vitest.config.ts",
        # Added in round 3 — every one of these is resolvable by Vitest v2 and so
        # every one of them can redirect a run. A filename missing from the
        # pinned list is a file the candidate may add for free.
        "vitest.projects.ts",
        "vitest.projects.json",
        "vitest.workspace.mts",
        "vitest.config.cjs",
    ],
)
def test_a_config_added_at_any_resolvable_filename_stops_the_gate(
    store: GateEvidenceStore, tmp_path: Path, stub_bin: Path, filename: str
) -> None:
    """The pin must cover the WHOLE resolution order, not the popular part of it."""
    _install_stub(stub_bin, "vitest", _vitest_stub(VITEST_TWO_PASSING))
    pinned_tree = _fixture_workspace(tmp_path / "pinned", NPM_PROJECT)
    approved_digest = _vitest_pin(pinned_tree)

    body = (
        '{"projects": []}\n'
        if filename.endswith(".json")
        else "export default { test: { exclude: ['**/*'] } }\n"
    )
    hostile = _fixture_workspace(tmp_path / f"hostile-{filename}", {**NPM_PROJECT, filename: body})

    with pytest.raises(GateEvidenceRefusal) as excinfo:
        EcosystemGateRunner(store).run(
            _request(
                hostile,
                "vitest run app/tests",
                "npm",
                gate_config={"vitest_config_digest": approved_digest},
            )
        )
    assert "not the one this gate pinned" in str(excinfo.value)


def test_the_pinned_filename_list_covers_the_vitest_v2_resolution_order() -> None:
    """Enumerated independently of production, so a shrunk list fails here.

    Deriving the expected set from ``_CONFIG_FILENAMES`` would follow any
    deletion, which is the one mutation that matters for this list.
    """
    expected = {
        f"{base}.{ext}"
        for base in ("vitest.config", "vite.config")
        for ext in ("ts", "mts", "cts", "js", "mjs", "cjs")
    } | {
        f"{base}.{ext}"
        for base in ("vitest.workspace", "vitest.projects")
        for ext in ("ts", "mts", "cts", "js", "mjs", "cjs", "json")
    }
    assert set(NpmVitestExecutor._CONFIG_FILENAMES) == expected
    assert len(NpmVitestExecutor._CONFIG_FILENAMES) == len(expected) == 26


def test_editing_a_pinned_config_stops_the_gate(
    store: GateEvidenceStore, tmp_path: Path, stub_bin: Path
) -> None:
    """A pinned config that is then EDITED is a different config."""
    _install_stub(stub_bin, "vitest", _vitest_stub(VITEST_TWO_PASSING))
    original = _fixture_workspace(
        tmp_path / "original",
        {**NPM_PROJECT, "vitest.config.ts": "export default { test: {} }\n"},
    )
    approved_digest = _vitest_pin(original)

    edited = _fixture_workspace(
        tmp_path / "edited",
        {
            **NPM_PROJECT,
            "vitest.config.ts": "export default { test: { exclude: ['**/*'] } }\n",
        },
    )

    with pytest.raises(GateEvidenceRefusal):
        EcosystemGateRunner(store).run(
            _request(
                edited,
                "vitest run app/tests",
                "npm",
                gate_config={"vitest_config_digest": approved_digest},
            )
        )


def test_the_config_digest_covers_absence_and_every_resolvable_filename(
    tmp_path: Path,
) -> None:
    """Absence is hashed material, or adding a config would not move the digest."""
    executor = NpmVitestExecutor()
    empty = tmp_path / "empty"
    empty.mkdir()
    baseline = executor.config_digest(empty)

    # Identical CONTENT under different filenames must still digest differently:
    # the material is the (filename, content) pair for every filename vitest can
    # resolve, so moving a config from vite.config.js to vitest.config.ts — which
    # changes which file actually wins — cannot be invisible.
    digests = {"<none>": baseline}
    for name in (
        "vitest.config.ts",
        "vitest.config.mts",
        "vite.config.js",
        "vitest.workspace.json",
    ):
        tree = tmp_path / f"with-{name}"
        tree.mkdir()
        (tree / name).write_text("export default {}\n", encoding="utf-8")
        digests[name] = executor.config_digest(tree)
    assert len(set(digests.values())) == len(digests), digests


def test_a_symlinked_vitest_config_is_refused_outright(tmp_path: Path) -> None:
    """A symlinked config is refused BEFORE hashing, whatever the pin says."""
    executor = NpmVitestExecutor()
    tree = tmp_path / "tree"
    tree.mkdir()
    outside = tmp_path / "outside.ts"
    outside.write_text("export default { test: { exclude: ['**/*'] } }\n", encoding="utf-8")
    (tree / "vitest.config.ts").symlink_to(outside)

    with pytest.raises(GateEvidenceRefusal) as excinfo:
        executor.config_digest(tree)
    assert "symlink" in str(excinfo.value)


def test_a_symlink_planted_where_the_pin_says_absent_is_refused_not_matched(
    store: GateEvidenceStore, tmp_path: Path, stub_bin: Path
) -> None:
    """THE hole encoding a symlink as "absent" left, and why it had to close.

    Pin a tree that genuinely has no ``vitest.config.ts``. Then plant a SYMLINK
    at that name pointing at candidate-controlled content. Under the old
    encoding both trees hashed identically — the pin MATCHED — while vitest
    happily followed the link: the operator approved "no config" and the run got
    whatever the link pointed at. The refusal is what makes "absent" mean absent.
    """
    _install_stub(stub_bin, "vitest", _vitest_stub(VITEST_TWO_PASSING))
    pinned_tree = _fixture_workspace(tmp_path / "pinned", NPM_PROJECT)
    approved_digest = _vitest_pin(pinned_tree)

    hostile = _fixture_workspace(
        tmp_path / "hostile",
        {
            **NPM_PROJECT,
            "elsewhere/sneaky.ts": "export default { test: { exclude: ['**/*'] } }\n",
        },
    )
    (hostile / "vitest.config.ts").symlink_to(hostile / "elsewhere" / "sneaky.ts")

    with pytest.raises(GateEvidenceRefusal) as excinfo:
        EcosystemGateRunner(store).run(
            _request(
                hostile,
                "vitest run app/tests",
                "npm",
                gate_config={"vitest_config_digest": approved_digest},
            )
        )

    assert "symlink" in str(excinfo.value)
    assert store.load("rt-1", "run-1") is None


def test_write_path_requires_the_npm_config_pin() -> None:
    """Rejected at creation, not silently refused on every tick forever."""
    with pytest.raises(RoutineValidationError) as excinfo:
        validate_routine(_payload(command="vitest run app", ecosystem="npm"))
    assert any("vitest_config_digest" in error for error in excinfo.value.errors)


# --- FINDING 4: go targets are packages, never files ------------------------


@pytest.mark.parametrize(
    "command",
    [
        "go test ./pkg/passing_test.go",
        "go test pkg/passing_test.go",
        "go test pkg/a_test.go pkg/b_test.go",
        "go test ./.../pkg",
    ],
)
def test_go_file_targets_are_refused_at_grammar_time(command: str) -> None:
    """Naming files runs a partial suite that nothing in the stream reports."""
    assert not ecosystem_command_is_valid("go", command)


@pytest.mark.parametrize("command", ["go test ./...", "go test ./pkg/...", "go test pkg"])
def test_go_package_patterns_are_accepted(command: str) -> None:
    assert ecosystem_command_is_valid("go", command)


def test_go_recursion_patterns_are_still_containment_checked(tmp_path: Path) -> None:
    """A pattern must not be a hole in containment: its prefix still must exist."""
    executor = GoTestExecutor()
    assert executor.containment_paths(("./pkg/...",)) == ("pkg",)
    assert executor.containment_paths(("./...",)) == (".",)
    assert executor.containment_paths(("pkg",)) == ("pkg",)


def test_a_go_target_that_is_a_file_on_disk_is_refused_at_preflight(tmp_path: Path) -> None:
    """Belt and braces: the grammar refuses `.go`, preflight refuses any file."""
    tree = tmp_path / "tree"
    (tree / "pkg").mkdir(parents=True)
    (tree / "pkg" / "thing").write_text("not a directory\n", encoding="utf-8")

    with pytest.raises(GateEvidenceRefusal) as excinfo:
        GoTestExecutor().preflight(tree, ("pkg/thing",))
    assert "package directory" in str(excinfo.value)


# --- FINDING 5: process-group liveness on the NORMAL exit path --------------


def test_no_descendant_survives_the_leader_on_the_normal_exit_path(
    store: GateEvidenceStore, tmp_path: Path, stub_bin: Path
) -> None:
    """A backgrounded descendant could rewrite the report after the leader exits.

    `start_new_session=True` means the child leads its own session, so anything
    it spawns outlives it by default.

    TWO THINGS MAKE THIS TEST HONEST, and both were learned the hard way:

    1. The sleeper's stdio is REDIRECTED. A descendant that keeps the inherited
       stdout pipe holds `communicate()` open with it, so the run cannot finish
       while it lives and the existing timeout already covers it. The dangerous
       descendant DETACHES: it releases the pipe, the leader exits, counting
       proceeds, and it is still running with write access to this run's
       artifacts. An earlier version used a pipe-holding sleeper and passed
       against an implementation that reaped nothing at all — it sat for 124 s
       and then observed a process that had simply timed out on its own.
    2. The assertion is that the descendant never DID ANYTHING, not that some
       pid is gone. Probing a recorded pid with signal 0 is a race against pid
       reuse, and in a file that spawns this many subprocesses it duly flaked.
       The descendant here tries to write a file well after the run should
       have ended; the reap must stop it before it can, which is precisely the
       capability that matters — writing to the artifact after the read.
       The 6 s delay comfortably exceeds the ~1-2 s a real run takes, so the
       marker can only appear if the descendant genuinely survived.

    A THIRD lesson, from a load-sensitive flake this test used to produce: the
    property under test is "no descendant survives", not "no descendant
    survives, checked at this one exact wall-clock instant". A fixed
    `sleep(N)` followed by a single existence check turns ordinary CPU
    contention on the box — which slows down the *test's own* scheduling, not
    necessarily the reap it is trying to observe — into a false failure. The
    check below polls with a generous deadline instead: a descendant that
    truly never survives can never produce the marker no matter how long the
    poll runs, so nothing is weakened — a descendant that genuinely survives
    forever still fails, it just gets the same slow host the leader got.
    Separately, `killpg` inside the production reap can legitimately raise
    `PermissionError` — reproduced locally under exactly the kind of
    concurrent-fleet contention this test guards against, on the SIGKILL
    probe itself, almost certainly a pgid handed to a brand-new, unrelated
    process group by fast pid recycling under heavy fork/exec churn, not a
    sandbox artifact specific to any one CI box. `_reap_process_group` (in
    gate_runner.py) treats that as "cannot confirm this is dead" — never
    "gone" (that would fail open exactly like the counterfeits this file is
    about) and never "still alive" (that would fail a run whose descendant we
    simply lost the ability to signal, which is not evidence about the
    descendant). It raises `GateExecutionInfraError`, which settles the run
    INCONCLUSIVE at the seam, the same bucket a hang already settles in. This
    test honors that: it treats exactly that refusal as an acceptable
    outcome, not a bug, but a descendant confirmed to have SURVIVED SIGKILL
    (a different, unambiguous raise from the same function) still fails
    below, and either way the marker-file poll still runs — the empirical
    proof that nothing actually happened is never skipped just because the
    reap could not vouch for itself.
    """
    evidence_of_life = tmp_path / "descendant-did-work.txt"
    _install_stub(
        stub_bin,
        "go",
        "#!/bin/sh\n"
        'if [ "$1" = "version" ]; then echo "go version go1.22.0"; exit 0; fi\n'
        f"( sleep 6; echo alive > {evidence_of_life} ) >/dev/null 2>&1 </dev/null &\n"
        f"cat <<'STUBEOF'\n{GO_TWO_PASSING}\nSTUBEOF\n"
        "exit 0\n",
    )
    workspace = _fixture_workspace(tmp_path, GO_PROJECT)

    started = time.monotonic()
    try:
        evidence: object | None = EcosystemGateRunner(store).run(
            _request(workspace, "go test pkg", "go")
        )
    except GateExecutionInfraError as exc:
        # "survived SIGKILL" is a CONFIRMED survivor and must still fail this
        # test; only the honest "could not tell" shape is tolerated here.
        if "could not be signalled" not in str(exc):
            raise
        evidence = None
    elapsed = time.monotonic() - started

    if evidence is not None:
        assert evidence.checks_collected == 2
    # A generous ceiling, not a tight one: this only has to prove the run went
    # through the fast normal-exit reap path rather than the ~900 s overall
    # gate timeout (see lesson 1) — it is not itself a timing assertion about
    # the reap's correctness, so it must not be tight enough for ordinary host
    # contention (killpg polling needs the CPU too) to trip it.
    assert elapsed < 30, (
        "the run must not have waited the descendant out via the overall timeout; if "
        f"it did, this test proves nothing about reaping (took {elapsed:.1f}s)"
    )

    # Poll for a generous deadline rather than asserting at one fixed instant
    # (see the THIRD lesson above). 10 s comfortably exceeds both the
    # descendant's 6 s delay and any slop already spent in `elapsed`, and
    # every 50 ms is frequent enough that a genuine survivor is caught almost
    # as soon as it acts.
    deadline = time.monotonic() + 10.0
    survived = False
    while time.monotonic() < deadline:
        if evidence_of_life.exists():
            survived = True
            break
        time.sleep(0.05)
    assert not survived, (
        "a descendant outlived the leader and kept running long enough to write a "
        "file — it could equally have rewritten the report this run was counted from"
    )


# --- FINDING 6: PATH hygiene ------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("/usr/bin:/bin", "/usr/bin:/bin"),
        # "." and "" both mean the current working directory, which during a
        # gate IS the candidate's own tree.
        (".:/usr/bin", "/usr/bin"),
        ("/usr/bin:.", "/usr/bin"),
        (":/usr/bin", "/usr/bin"),
        ("/usr/bin::/bin", "/usr/bin:/bin"),
        ("relative/dir:/usr/bin", "/usr/bin"),
        ("", ""),
        (None, ""),
        (".", ""),
    ],
)
def test_path_sanitization_drops_cwd_and_relative_entries(raw: str | None, expected: str) -> None:
    assert sanitize_path_env(raw) == expected


def test_a_toolchain_named_by_a_dot_path_entry_is_not_resolved(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A `./go` committed by the candidate must never become the grader."""
    tree = tmp_path / "tree"
    tree.mkdir()
    _install_stub(tree, "go", "#!/bin/sh\necho hacked\n")
    monkeypatch.chdir(tree)
    monkeypatch.setenv("PATH", ".")
    monkeypatch.delenv("OMNIAGENTOS_GATE_GO", raising=False)

    with pytest.raises(GateWorkspaceUnusable) as excinfo:
        resolve_program(GoTestExecutor(), path=os.environ.get("PATH"))
    assert "toolchain is absent" in str(excinfo.value)


# --- FINDING 7: the go stream must show a package lifecycle ----------------


def test_bare_run_pass_pairs_without_a_package_lifecycle_are_refused(
    store: GateEvidenceStore, tmp_path: Path, stub_bin: Path
) -> None:
    """The cheapest forgery is a handful of run/pass objects. It is not a run."""
    forged = _go_stream(
        {"Action": "run", "Package": GO_PKG, "Test": "TestOne"},
        {"Action": "pass", "Package": GO_PKG, "Test": "TestOne"},
        {"Action": "run", "Package": GO_PKG, "Test": "TestTwo"},
        {"Action": "pass", "Package": GO_PKG, "Test": "TestTwo"},
    )
    _install_stub(stub_bin, "go", _go_stub(forged))
    workspace = _fixture_workspace(tmp_path, GO_PROJECT)

    with pytest.raises(GateExecutionInfraError) as excinfo:
        EcosystemGateRunner(store).run(_request(workspace, "go test pkg", "go"))

    assert "no package start event" in str(excinfo.value)
    assert store.load("rt-1", "run-1") is None


def test_a_package_that_never_reports_a_terminal_result_is_refused(
    store: GateEvidenceStore, tmp_path: Path, stub_bin: Path
) -> None:
    """Without the closing event nothing says the package finished."""
    truncated = _go_stream(
        {"Action": "start", "Package": GO_PKG},
        {"Action": "run", "Package": GO_PKG, "Test": "TestOne"},
        {"Action": "pass", "Package": GO_PKG, "Test": "TestOne"},
    )
    _install_stub(stub_bin, "go", _go_stub(truncated))
    workspace = _fixture_workspace(tmp_path, GO_PROJECT)

    with pytest.raises(GateExecutionInfraError) as excinfo:
        EcosystemGateRunner(store).run(_request(workspace, "go test pkg", "go"))

    assert "truncated" in str(excinfo.value)


# --- N3: the pin covers referenced sub-configs, not just the root -----------


NPM_WORKSPACE_PROJECT = {
    "package.json": '{"name":"demo","devDependencies":{"vitest":"^2.1.0"}}\n',
    "vitest.workspace.json": '["packages/*"]\n',
    "packages/alpha/vitest.config.ts": "export default { test: {} }\n",
    "packages/alpha/tests/demo.test.ts": "import {test} from 'vitest'\ntest('a', () => {})\n",
    "app/tests/demo.test.ts": "import {test} from 'vitest'\ntest('a', () => {})\n",
}


def test_editing_a_referenced_sub_config_stops_the_gate(
    store: GateEvidenceStore, tmp_path: Path, stub_bin: Path
) -> None:
    """A pinned workspace file REFERENCES its configuration; it does not contain it.

    ``vitest.workspace.json`` says ``["packages/*"]`` and each project then loads
    its own ``vitest.config.ts``. Pinning only root-level names left every one of
    those sub-configs unpinned and freely editable — the same exclusion hole the
    pin exists to close, one directory further down. The workspace file here is
    byte-identical in both trees; only the referenced sub-config differs.
    """
    _install_stub(stub_bin, "vitest", _vitest_stub(VITEST_TWO_PASSING))
    pinned_tree = _fixture_workspace(tmp_path / "pinned", NPM_WORKSPACE_PROJECT)
    approved_digest = _vitest_pin(pinned_tree)

    hostile = _fixture_workspace(
        tmp_path / "hostile",
        {
            **NPM_WORKSPACE_PROJECT,
            "packages/alpha/vitest.config.ts": (
                "export default { test: { exclude: ['**/failing.test.ts'] } }\n"
            ),
        },
    )
    assert (hostile / "vitest.workspace.json").read_text(encoding="utf-8") == (
        pinned_tree / "vitest.workspace.json"
    ).read_text(encoding="utf-8"), "only the SUB-config may differ, or this proves nothing"

    with pytest.raises(GateEvidenceRefusal) as excinfo:
        EcosystemGateRunner(store).run(
            _request(
                hostile,
                "vitest run app/tests",
                "npm",
                gate_config={"vitest_config_digest": approved_digest},
            )
        )
    assert "not the one this gate pinned" in str(excinfo.value)
    assert store.load("rt-1", "run-1") is None


def test_a_sub_config_added_after_pinning_stops_the_gate(tmp_path: Path) -> None:
    """Adding a config in a subdirectory is a change to what runs."""
    executor = NpmVitestExecutor()
    pinned = _fixture_workspace(tmp_path / "pinned", NPM_WORKSPACE_PROJECT)
    baseline = executor.config_digest(pinned)

    hostile = _fixture_workspace(
        tmp_path / "hostile",
        {**NPM_WORKSPACE_PROJECT, "packages/beta/vitest.config.mts": "export default {}\n"},
    )
    assert executor.config_digest(hostile) != baseline


def test_a_symlinked_sub_config_is_refused_too(tmp_path: Path) -> None:
    """The symlink rule follows the coverage down, or it is trivially bypassed."""
    executor = NpmVitestExecutor()
    tree = _fixture_workspace(tmp_path / "tree", NPM_WORKSPACE_PROJECT)
    outside = tmp_path / "outside.ts"
    outside.write_text("export default { test: { exclude: ['**/*'] } }\n", encoding="utf-8")
    (tree / "packages" / "beta").mkdir(parents=True)
    (tree / "packages" / "beta" / "vitest.config.ts").symlink_to(outside)

    with pytest.raises(GateEvidenceRefusal) as excinfo:
        executor.config_digest(tree)
    assert "symlink" in str(excinfo.value)


def test_a_committed_directory_symlink_loop_does_not_break_the_walk(
    tmp_path: Path,
) -> None:
    """A candidate can commit a directory symlink pointing at its own ancestor.

    There is no bypass here — a config reached through a link is refused by the
    symlink check, and one outside the tree by containment — but a walk that
    descended into the loop would recurse until it ran out of stack, and a gate
    that raises RecursionError is a gate that has stopped working. The walk must
    terminate with an ordinary result or a classified refusal, never that.
    """
    executor = NpmVitestExecutor()
    tree = tmp_path / "tree"
    (tree / "packages" / "alpha").mkdir(parents=True)
    (tree / "packages" / "alpha" / "vitest.config.ts").write_text(
        "export default {}\n", encoding="utf-8"
    )
    (tree / "packages" / "alpha" / "loop").symlink_to(tree / "packages")
    (tree / "up").symlink_to(tree)

    try:
        result = executor.config_digest(tree)
    except GateEvidenceRefusal:
        return  # a classified refusal is an acceptable outcome
    except RecursionError as exc:  # pragma: no cover - the defect under test
        pytest.fail(f"a committed symlink loop broke the config walk: {exc}")
    assert len(result) == 64

    # And the real config below the loop is still covered, so declining to
    # follow links did not create a blind spot.
    (tree / "packages" / "alpha" / "vitest.config.ts").write_text(
        "export default { test: { exclude: ['**/*'] } }\n", encoding="utf-8"
    )
    assert executor.config_digest(tree) != result


def test_a_symlinked_directory_in_the_tree_is_refused_not_skipped(tmp_path: Path) -> None:
    """MAJOR: skipping a symlinked directory is a hole, not a safeguard.

    `followlinks=False` stops the WALK descending, but vitest's own globbing
    follows symlinked directories (fast-glob defaults `followSymbolicLinks:
    true`). So a committed `packages/proj -> elsewhere` under a pinned workspace
    glob loads a config the digest never hashed: editing the file behind the
    link left the digest byte-identical. Refusing is the directory-level
    analogue of the rule already applied to config FILES.
    """
    executor = NpmVitestExecutor()
    tree = tmp_path / "tree"
    (tree / "elsewhere").mkdir(parents=True)
    (tree / "elsewhere" / "vitest.config.ts").write_text(
        "export default { test: { exclude: ['**/*'] } }\n", encoding="utf-8"
    )
    (tree / "packages").mkdir(parents=True)
    (tree / "packages" / "proj").symlink_to(tree / "elsewhere")

    with pytest.raises(GateEvidenceRefusal) as excinfo:
        executor.config_digest(tree)
    assert "symlinked directory" in str(excinfo.value)


def test_the_content_behind_a_symlinked_directory_was_invisible_to_the_digest(
    tmp_path: Path,
) -> None:
    """The defect, demonstrated: without the refusal the digest cannot see it.

    Walking with `followlinks=False` and NOT refusing means the file behind the
    link never enters the material, so editing it does not move the digest —
    which is exactly what a pinned gate must never allow. Proven here by
    digesting the linked-away subtree directly: its content is real, and it is
    absent from what the walk would have hashed.
    """
    executor = NpmVitestExecutor()
    tree = tmp_path / "tree"
    (tree / "elsewhere").mkdir(parents=True)
    hidden = tree / "elsewhere" / "vitest.config.ts"
    hidden.write_text("export default {}\n", encoding="utf-8")
    (tree / "packages").mkdir(parents=True)

    before = executor.config_digest(tree)
    hidden.write_text("export default { test: { exclude: ['**/*'] } }\n", encoding="utf-8")
    after = executor.config_digest(tree)
    assert before != after, "a config in a real directory must be covered"

    # Now hide the same directory behind a link: the walk cannot see it, so the
    # refusal is the only thing standing between the pin and a silent edit.
    (tree / "packages" / "proj").symlink_to(tree / "elsewhere")
    with pytest.raises(GateEvidenceRefusal):
        executor.config_digest(tree)


def test_a_committed_node_modules_is_refused(tmp_path: Path) -> None:
    """MINOR (a): pruning it assumed it was generated; nothing enforced that.

    This runner never installs dependencies and the run tree is a fresh git
    worktree, so a `node_modules` can only be there because the candidate
    committed it — which makes it candidate-authored content holding a config
    the pin would not hash.
    """
    executor = NpmVitestExecutor()
    tree = tmp_path / "tree"
    (tree / "node_modules" / "evil").mkdir(parents=True)
    (tree / "node_modules" / "evil" / "vitest.config.ts").write_text(
        "export default { test: { exclude: ['**/*'] } }\n", encoding="utf-8"
    )

    with pytest.raises(GateEvidenceRefusal) as excinfo:
        executor.config_digest(tree)
    assert "node_modules" in str(excinfo.value)


def test_a_nested_committed_node_modules_is_refused(tmp_path: Path) -> None:
    """One directory down is not a hiding place either."""
    executor = NpmVitestExecutor()
    tree = tmp_path / "tree"
    (tree / "packages" / "alpha" / "node_modules").mkdir(parents=True)

    with pytest.raises(GateEvidenceRefusal):
        executor.config_digest(tree)


def test_git_is_still_pruned(tmp_path: Path) -> None:
    """`.git` is the one directory that cannot hold candidate source."""
    executor = NpmVitestExecutor()
    tree = tmp_path / "tree"
    (tree / "app").mkdir(parents=True)
    baseline = executor.config_digest(tree)

    (tree / ".git" / "pkg").mkdir(parents=True)
    (tree / ".git" / "pkg" / "vitest.config.ts").write_text("export default {}\n", encoding="utf-8")
    assert executor.config_digest(tree) == baseline


def test_a_crate_beneath_an_excluded_directory_is_not_refused(tmp_path: Path) -> None:
    """MINOR (b): `workspace.exclude` removes a SUBTREE, not one directory.

    Cargo treats everything under an excluded path as a non-member, so a crate
    two levels down — reached here only via a path dependency — is equally not
    run. Exact-parent equality over-refused exactly those.
    """
    tree = tmp_path / "tree"
    _crate(
        tree,
        "ws",
        '[workspace]\nmembers = ["crates/live"]\nexclude = ["legacy"]\n',
    )
    _crate(
        tree,
        "ws/crates/live",
        '[package]\nname = "live"\nversion = "0.1.0"\n\n'
        '[dependencies]\nold = { path = "../../legacy/nested/old" }\n',
    )
    _crate(
        tree,
        "ws/legacy/nested/old",
        '[package]\nname = "old"\nversion = "0.1.0"\n\n[lib]\nharness = false\n',
    )

    CargoTestExecutor().preflight(tree, ("ws",))


def test_a_path_dependency_in_patch_is_scanned(tmp_path: Path) -> None:
    """MINOR (c): `[patch.<source>]` redirects a dependency to a local path."""
    tree = tmp_path / "tree"
    _crate(
        tree,
        "crate",
        '[package]\nname = "demo"\nversion = "0.1.0"\n\n'
        '[patch.crates-io]\nserde = { path = "../vendored" }\n',
    )
    _crate(
        tree,
        "vendored",
        '[package]\nname = "serde"\nversion = "0.1.0"\n\n[lib]\nharness = false\n',
    )

    with pytest.raises(GateEvidenceRefusal) as excinfo:
        CargoTestExecutor().preflight(tree, ("crate",))
    assert "harness = false" in str(excinfo.value)


def test_a_path_dependency_in_replace_is_scanned(tmp_path: Path) -> None:
    """MINOR (c): `[replace]` is the older spelling of the same redirect."""
    tree = tmp_path / "tree"
    _crate(
        tree,
        "crate",
        '[package]\nname = "demo"\nversion = "0.1.0"\n\n'
        '[replace]\n"serde:1.0.0" = { path = "../vendored" }\n',
    )
    _crate(
        tree,
        "vendored",
        '[package]\nname = "serde"\nversion = "0.1.0"\n\n[[test]]\nname = "t"\nharness = false\n',
    )

    with pytest.raises(GateEvidenceRefusal) as excinfo:
        CargoTestExecutor().preflight(tree, ("crate",))
    assert "harness = false" in str(excinfo.value)


# --- N4: a descendant that escaped the process group -----------------------


def test_a_detached_rewriter_cannot_change_the_counted_evidence(
    store: GateEvidenceStore, tmp_path: Path, stub_bin: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A setsid descendant leaves the process group and survives the reap.

    It can read the report path from its parent's argv, so it knows exactly what
    to overwrite. The counted evidence must reflect what the measured run wrote,
    not what something else wrote afterwards.

    The rewriter used to wait on a flat `sleep 3` and bet that the production
    read would land first. Under gate-concurrent load that bet loses: the read
    can be delayed past the 3s window (and, worse, past the mtime-tolerance
    clock the tamper check itself samples), so the rewriter wins the race the
    test meant to lose, and a CORRECT tamper refusal gets misread as a test
    failure. The rewriter now blocks on a sentinel file that is written only
    once ``read_artifact_nofollow`` has actually returned — a real detached
    process, held on a deterministic barrier instead of a wall-clock guess —
    so the "read commits the tally before any rewrite can land" ordering this
    test exercises is guaranteed, not merely probable.
    """
    forged = VITEST_TWO_PASSING.replace('tests="2"', 'tests="999"')
    (tmp_path / "forged.xml").write_text(forged, encoding="utf-8")
    sentinel = tmp_path / "read-done.sentinel"

    real_read_artifact_nofollow = read_artifact_nofollow

    def _signal_after_read(*args: object, **kwargs: object) -> bytes:
        payload = real_read_artifact_nofollow(*args, **kwargs)
        sentinel.write_bytes(b"1")
        return payload

    monkeypatch.setattr(
        "omniagentos.scheduler.gate_ecosystems.read_artifact_nofollow", _signal_after_read
    )

    _install_stub(
        stub_bin,
        "vitest",
        "#!/bin/sh\n"
        'if [ "$1" = "--version" ]; then echo "vitest/2.1.0"; exit 0; fi\n'
        'out=""\n'
        'for arg in "$@"; do\n'
        '  case "$arg" in --outputFile=*) out="${arg#--outputFile=}";; esac\n'
        "done\n"
        f"cat > \"$out\" <<'STUBEOF'\n{VITEST_TWO_PASSING}\nSTUBEOF\n"
        # Detached: its own session, so killpg on the leader's group never
        # reaches it. It blocks on the sentinel above — which appears only
        # once the production read has demonstrably returned — instead of a
        # wall-clock sleep, so it fires deterministically AFTER the read
        # instead of probably after 3 real seconds. Bounded at 10s so a run
        # that never reaches the read (some earlier, unrelated refusal)
        # still lets this descendant exit rather than poll forever.
        f'(i=0; while [ ! -f "{sentinel}" ] && [ "$i" -lt 500 ]; do '
        "i=$((i+1)); sleep 0.02; done; "
        f'cat "{tmp_path / "forged.xml"}" > "$out") >/dev/null 2>&1 </dev/null &\n'
        "exit 0\n",
    )
    workspace = _fixture_workspace(tmp_path, NPM_PROJECT)

    try:
        evidence = EcosystemGateRunner(store).run(
            _request(workspace, "vitest run app/tests", "npm")
        )
    finally:
        # Release the descendant promptly even if the run never reached the
        # read (e.g. an earlier, unrelated refusal), instead of leaving it to
        # poll out its whole 10s budget after the test has already finished.
        sentinel.write_bytes(b"1")

    assert evidence.checks_collected == 2, "the count must come from the measured run"
    assert evidence.checks_passed == 2


def test_a_report_modified_after_the_run_finished_is_refused(tmp_path: Path) -> None:
    """An honest report is written BEFORE the process that wrote it exits.

    So a report whose mtime postdates that exit was written by something this
    seam was not measuring — the escaped descendant, arriving late.
    """
    report = tmp_path / "report.xml"
    report.write_text("<testsuites/>", encoding="utf-8")
    long_ago = time.time_ns() - 600 * 1_000_000_000

    with pytest.raises(GateExecutionInfraError) as excinfo:
        read_artifact_nofollow(report, missing_message="absent", not_modified_after_ns=long_ago)
    assert "after the run" in str(excinfo.value)


def test_a_report_written_before_the_run_finished_is_accepted(tmp_path: Path) -> None:
    """The mtime rule must not refuse ordinary runs."""
    report = tmp_path / "report.xml"
    report.write_text("<testsuites/>", encoding="utf-8")

    payload = read_artifact_nofollow(
        report, missing_message="absent", not_modified_after_ns=time.time_ns()
    )
    assert payload == b"<testsuites/>"


def test_a_report_stamped_after_the_run_is_refused_end_to_end(
    store: GateEvidenceStore, tmp_path: Path, stub_bin: Path
) -> None:
    """The mtime rule must be WIRED, not merely implemented.

    The unit test above proves ``read_artifact_nofollow`` enforces it; this
    proves the runner actually hands it the run's finish time. Without that
    plumbing the check is dead code and a late rewrite counts. The stub stamps
    the report in the year 2030 — the shape a rewrite that lost the race with
    its own clock would take — and the run must refuse rather than count it.
    """
    _install_stub(
        stub_bin,
        "vitest",
        "#!/bin/sh\n"
        'if [ "$1" = "--version" ]; then echo "vitest/2.1.0"; exit 0; fi\n'
        'out=""\n'
        'for arg in "$@"; do\n'
        '  case "$arg" in --outputFile=*) out="${arg#--outputFile=}";; esac\n'
        "done\n"
        f"cat > \"$out\" <<'STUBEOF'\n{VITEST_TWO_PASSING}\nSTUBEOF\n"
        'touch -t 203001010000 "$out"\n'
        "exit 0\n",
    )
    workspace = _fixture_workspace(tmp_path, NPM_PROJECT)

    with pytest.raises(GateExecutionInfraError) as excinfo:
        EcosystemGateRunner(store).run(_request(workspace, "vitest run app/tests", "npm"))

    assert "after the run" in str(excinfo.value)
    assert store.load("rt-1", "run-1") is None


def test_a_report_rewritten_mid_read_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The escaped descendant's best shot: rewrite while we are reading.

    Forced deterministically by rewriting the file from inside ``os.read``,
    which is exactly the interleaving a surviving descendant would have to hit
    by luck. The descriptor's identity is re-checked after the read, so the
    spliced content is refused rather than counted.
    """
    report = tmp_path / "report.xml"
    report.write_text("<testsuites tests='2'/>", encoding="utf-8")

    real_read = os.read
    state = {"raced": False}

    def racing_read(fd: int, length: int) -> bytes:
        data = real_read(fd, length)
        if data and not state["raced"]:
            state["raced"] = True
            report.write_text(
                "<testsuites tests='999'/> <!-- rewritten by a survivor -->", encoding="utf-8"
            )
        return data

    monkeypatch.setattr(os, "read", racing_read)

    with pytest.raises(GateExecutionInfraError) as excinfo:
        read_artifact_nofollow(report, missing_message="absent")

    assert state["raced"], "the race must actually have been staged"
    assert "changed while it was being read" in str(excinfo.value)


def test_a_hard_linked_report_is_refused(tmp_path: Path) -> None:
    """A second name for the same inode is a second writer for the same content."""
    report = tmp_path / "report.xml"
    report.write_text("<testsuites/>", encoding="utf-8")
    os.link(report, tmp_path / "elsewhere.xml")

    with pytest.raises(GateExecutionInfraError) as excinfo:
        read_artifact_nofollow(report, missing_message="absent")
    assert "hard links" in str(excinfo.value)


# --- FINDING 9: the counterfeit claims stay reproducible --------------------


def test_every_counterfeit_mutation_anchor_still_exists() -> None:
    """A mutation whose anchor has drifted is a no-op that still reports KILLED.

    ``counterfeit_mutations_m2`` is the runnable proof that the counterfeits in
    this file are decisive; this is the cheap guard that keeps that proof
    honest, because a refactor that renames a line silently turns the
    corresponding mutation into a nothing-burger.
    """
    from tests.scheduler.counterfeit_mutations_m2 import MUTATIONS, anchors_present

    assert anchors_present() == []
    # Every mutation must name a test that exists in THIS module.
    module_tests = {name for name in globals() if name.startswith("test_")}
    for mutation in MUTATIONS:
        node = mutation.test.split("::", 1)[1]
        assert node in module_tests, f"{mutation.label} names a missing test: {node}"
