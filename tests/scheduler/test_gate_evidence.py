"""Tests for gate evidence and trusted execution."""

from __future__ import annotations

import hashlib
import hmac
import json
import subprocess
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest

from omniagentos.scheduler import gate_evidence as gate_evidence_mod
from omniagentos.scheduler.gate_evidence import (
    MAX_EVIDENCE_AGE_SECONDS,
    MERGE_GATE_ROUTINE_ID,
    MERGE_GATE_TYPE,
    SCHEMA_V2,
    GateEvidence,
    GateEvidenceError,
    GateEvidenceExists,
    GateEvidenceRefusal,
    GateEvidenceStore,
    GateExecutionInfraError,
    GateStepReceipt,
    binding_digest,
    candidate_receipt_rejections,
    evidence_rejections,
    gate_evidence_dir,
    load_skip_allowlist,
    normalize_gate_command,
    record_step_receipt,
    verify_candidate_receipt,
    verify_step_receipt,
    workspace_digest_for,
)
from omniagentos.scheduler.gate_runner import (
    GateRunRequest,
    PytestGateRunner,
    default_gate_runner,
    default_gate_workspace,
    parse_gate_command,
    produce_gate_evidence,
    resolve_targets,
)

# Wire contract literals — do NOT derive these from production SCHEMA imports.
# A test that constructs with `from ... import SCHEMA` follows a revert to v2.
_SCHEMA_V3_WIRE = "omniagentos.gate-evidence.v3"
_SCHEMA_V2_WIRE = "omniagentos.gate-evidence.v2"

NOW = datetime(2026, 1, 1, 9, 0, tzinfo=UTC)

PASSING_SUITE = """
def test_one() -> None:
    assert True


def test_two() -> None:
    assert True
"""

FAILING_SUITE = """
def test_one() -> None:
    assert True


def test_two() -> None:
    assert False
"""

SKIPPING_SUITE = """
import pytest


def test_one() -> None:
    assert True


@pytest.mark.skip(reason="not today")
def test_two() -> None:
    assert False
"""

THREE_SKIPPING_SUITE = """
import pytest


def test_one() -> None:
    assert True


@pytest.mark.skip(reason="credential-dependent")
def test_two() -> None:
    assert False


@pytest.mark.skip(reason="credential-dependent")
def test_three() -> None:
    assert False


@pytest.mark.skip(reason="platform-dependent")
def test_four() -> None:
    assert False
"""

EMPTY_SUITE = """
def helper() -> None:
    return None
"""


@pytest.fixture
def store(tmp_path: Path) -> GateEvidenceStore:
    return GateEvidenceStore(tmp_path / "gate-evidence")


def _workspace(tmp_path: Path, suite: str) -> Path:
    root = tmp_path / "workspace"
    root.mkdir(parents=True, exist_ok=True)
    (root / "suite").mkdir(parents=True, exist_ok=True)
    (root / "suite" / "test_gate.py").write_text(suite, encoding="utf-8")
    (root / ".gitignore").write_text(".pytest_cache\n__pycache__\nvar\n", encoding="utf-8")

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
    subprocess.run(["git", "add", "."], cwd=str(root), check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=str(root), check=True, capture_output=True)
    return root


def _request(workspace: Path, command: str = "pytest suite", **overrides: object) -> GateRunRequest:
    fields: dict[str, object] = {
        "routine_id": "rt-1",
        "run_id": "run-1",
        "iteration": 1,
        "gate_type": "test_command",
        "gate_config": {"command": command, "expected_exit_code": 0},
        "workspace": workspace,
    }
    fields.update(overrides)
    return GateRunRequest(**fields)  # type: ignore[arg-type]


# --- command normalization and binding -------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("pytest  suite", "pytest suite"),
        ("pytest 'suite'", "pytest suite"),
        ("  pytest suite  ", "pytest suite"),
    ],
)
def test_normalize_gate_command_is_stable_across_formatting(raw: str, expected: str) -> None:
    assert normalize_gate_command(raw) == expected


@pytest.mark.parametrize("raw", ["", "   ", "pytest 'unterminated"])
def test_normalize_gate_command_refuses_unusable_input(raw: str) -> None:
    with pytest.raises(GateEvidenceRefusal):
        normalize_gate_command(raw)


def test_binding_digest_separates_identity_and_work() -> None:
    base = {
        "routine_id": "rt-1",
        "run_id": "run-1",
        "iteration": 1,
        "gate_type": "test_command",
        "command": "pytest suite",
        "targets": ("suite",),
        "workspace_digest": "ws",
        "candidate_sha": "a" * 40,
        "merge_base_sha": "b" * 40,
    }
    reference = binding_digest(**base)  # type: ignore[arg-type]

    assert binding_digest(**{**base, "run_id": "run-2"}) != reference  # type: ignore[arg-type]
    assert binding_digest(**{**base, "iteration": 2}) != reference  # type: ignore[arg-type]
    assert binding_digest(**{**base, "targets": ("other",)}) != reference  # type: ignore[arg-type]
    assert binding_digest(**{**base, "workspace_digest": "x"}) != reference  # type: ignore[arg-type]
    assert binding_digest(**{**base, "candidate_sha": "c" * 40}) != reference  # type: ignore[arg-type]
    assert binding_digest(**{**base, "merge_base_sha": "d" * 40}) != reference  # type: ignore[arg-type]
    assert binding_digest(**{**base, "command": "pytest  'suite'"}) == reference  # type: ignore[arg-type]


# --- schema wire contract ---------------------------------------------------


def test_schema_constant_is_v3_wire_contract() -> None:
    """SCHEMA must be the v3 wire string, asserted independently of production.

    Failing-on-revert: changing production to
    ``SCHEMA = "omniagentos.gate-evidence.v2"`` must fail this test.
    Importing SCHEMA into the expected side would follow the mutation.
    """
    assert gate_evidence_mod.SCHEMA == _SCHEMA_V3_WIRE
    assert gate_evidence_mod.SCHEMA != _SCHEMA_V2_WIRE
    assert gate_evidence_mod.SCHEMA_V2 == _SCHEMA_V2_WIRE
    assert gate_evidence_mod.SCHEMA_V1 == "omniagentos.gate-evidence.v1"


def test_recorded_evidence_carries_literal_v3_schema(
    store: GateEvidenceStore, tmp_path: Path
) -> None:
    """On-disk and in-memory evidence must stamp the v3 schema string literally."""
    workspace = _workspace(tmp_path, PASSING_SUITE)
    evidence = PytestGateRunner(store).run(_request(workspace))

    assert evidence.schema == _SCHEMA_V3_WIRE
    path = store._record_path("rt-1", "run-1")
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["schema"] == _SCHEMA_V3_WIRE


# --- the store --------------------------------------------------------------


def test_recording_twice_for_one_run_is_refused(store: GateEvidenceStore, tmp_path: Path) -> None:
    """Idempotency: a second execution cannot overwrite the first answer."""
    workspace = _workspace(tmp_path, PASSING_SUITE)
    evidence = PytestGateRunner(store).run(_request(workspace))

    with pytest.raises(GateEvidenceExists):
        store.record(evidence)

    reloaded = store.load("rt-1", "run-1")
    assert reloaded is not None
    assert reloaded.nonce == evidence.nonce


def test_stored_evidence_survives_a_round_trip(store: GateEvidenceStore, tmp_path: Path) -> None:
    workspace = _workspace(tmp_path, PASSING_SUITE)
    evidence = PytestGateRunner(store).run(_request(workspace))

    assert store.load("rt-1", "run-1") == evidence
    assert store.verify(evidence)


def test_v2_record_is_quarantined_before_candidate_bound_reexecution(
    store: GateEvidenceStore,
    tmp_path: Path,
) -> None:
    workspace = _workspace(tmp_path, PASSING_SUITE)
    PytestGateRunner(store).run(_request(workspace))
    path = store._record_path("rt-1", "run-1")
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["schema"] = SCHEMA_V2
    payload.pop("candidate_sha")
    payload.pop("merge_base_sha")
    unsigned = {key: value for key, value in payload.items() if key != "signature"}
    canonical = json.dumps(unsigned, sort_keys=True, separators=(",", ":")).encode("utf-8")
    payload["signature"] = hmac.new(store._key, canonical, "sha256").hexdigest()
    path.write_text(json.dumps(payload), encoding="utf-8")

    assert store.load("rt-1", "run-1") is None
    assert not path.exists()
    assert path.with_name("run-1.json.superseded-v2").exists()


@pytest.mark.parametrize(
    "mutation",
    [
        {"exit_code": 0, "checks_failed": 0, "checks_passed": 2},
        {"checks_collected": 99},
        {"command": "pytest other"},
        {"signature": "0" * 64},
    ],
)
def test_tampered_record_on_disk_is_not_loadable(
    store: GateEvidenceStore, tmp_path: Path, mutation: dict[str, object]
) -> None:
    """Editing the JSON invalidates the HMAC, raising GateExecutionInfraError."""
    workspace = _workspace(tmp_path, FAILING_SUITE)
    PytestGateRunner(store).run(_request(workspace))
    path = next((store.root / "records").rglob("*.json"))
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload.update(mutation)
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(GateExecutionInfraError):
        store.load("rt-1", "run-1")


def test_record_relocated_to_another_run_is_not_loadable(
    store: GateEvidenceStore, tmp_path: Path
) -> None:
    """On-disk replay: copying a passing record onto another run proves nothing."""
    workspace = _workspace(tmp_path, PASSING_SUITE)
    PytestGateRunner(store).run(_request(workspace))
    source = store.root / "records" / "rt-1" / "run-1.json"
    (source.parent / "run-2.json").write_bytes(source.read_bytes())

    assert store.load("rt-1", "run-1") is not None
    with pytest.raises(GateExecutionInfraError):
        store.load("rt-1", "run-2")


@pytest.mark.parametrize(
    ("routine_id", "run_id"),
    [("../escape", "run-1"), ("rt-1", "../escape"), ("", "run-1"), ("rt-1", "")],
)
def test_unsafe_identity_never_reads_outside_the_store(
    store: GateEvidenceStore, routine_id: str, run_id: str
) -> None:
    assert store.load(routine_id, run_id) is None


def test_evidence_directory_follows_the_runtime_var_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OMNIAGENTOS_VAR_DIR", str(tmp_path / "var"))
    assert gate_evidence_dir() == tmp_path / "var" / "gate-evidence"


# --- command and target preflight -------------------------------------------


@pytest.mark.parametrize(
    "command",
    [
        "pytest --collect-only suite",
        "pytest -k smoke suite",
        "pytest",
        "pytest ../outside",
        "pytest /etc",
        "pytest suite && rm -rf /",
        "echo pass",
        "true",
    ],
)
def test_parse_gate_command_refuses_anything_the_allowlist_would(command: str) -> None:
    with pytest.raises(GateEvidenceError):
        parse_gate_command(command)


@pytest.mark.parametrize(
    ("command", "targets"),
    [
        ("pytest suite", ("suite",)),
        ("python -m pytest suite tests", ("suite", "tests")),
        ("pytest suite/test_gate.py::test_one", ("suite/test_gate.py::test_one",)),
    ],
)
def test_parse_gate_command_accepts_executable_pytest_invocations(
    command: str, targets: tuple[str, ...]
) -> None:
    assert parse_gate_command(command) == ("pytest", targets)


@pytest.mark.parametrize("command", ["mypy suite", "ruff check .", "git diff --check"])
def test_allowlisted_verifiers_without_a_trusted_executor_fail_closed(command: str) -> None:
    with pytest.raises(GateEvidenceError, match="no trusted executor"):
        parse_gate_command(command)


def test_resolve_targets_accepts_a_target_that_exists_in_the_workspace(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path, PASSING_SUITE)

    assert resolve_targets(workspace, ("suite",)) == ("suite",)


@pytest.mark.parametrize(
    ("targets", "expected"),
    [
        pytest.param((), "declares no targets", id="empty"),
        pytest.param(("missing",), "does not exist", id="nonexistent"),
        pytest.param(("suite/absent.py",), "does not exist", id="nonexistent-file"),
        pytest.param(("../escape",), "escapes the workspace", id="parent"),
        pytest.param(("/etc",), "escapes the workspace", id="absolute"),
        pytest.param(("suite/../../escape",), "escapes the workspace", id="embedded-parent"),
    ],
)
def test_resolve_targets_refuses_absent_or_escaping_targets(
    tmp_path: Path, targets: tuple[str, ...], expected: str
) -> None:
    workspace = _workspace(tmp_path, PASSING_SUITE)

    with pytest.raises(GateEvidenceError, match=expected):
        resolve_targets(workspace, targets)


def test_symlinked_target_pointing_outside_the_workspace_is_refused(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path, PASSING_SUITE)
    outside = tmp_path / "outside"
    outside.mkdir()
    (workspace / "linked").symlink_to(outside, target_is_directory=True)

    with pytest.raises(GateEvidenceError):
        resolve_targets(workspace, ("linked",))


# --- the executor -----------------------------------------------------------


def test_executor_records_exact_counts_for_a_passing_suite(
    store: GateEvidenceStore, tmp_path: Path
) -> None:
    workspace = _workspace(tmp_path, PASSING_SUITE)
    workspace_sha = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=workspace,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()

    evidence = PytestGateRunner(store).run(
        _request(
            workspace,
            candidate_sha=workspace_sha,
            merge_base_sha=workspace_sha,
        )
    )

    assert evidence.exit_code == 0
    assert (evidence.checks_collected, evidence.checks_passed) == (2, 2)
    assert (evidence.checks_skipped, evidence.checks_failed) == (0, 0)
    assert evidence.targets == ("suite",)
    assert evidence.tool == "pytest"
    assert evidence.tool_version
    assert evidence.workspace_digest == workspace_digest_for(workspace)
    assert evidence.workspace_sha == workspace_sha
    assert evidence.candidate_sha == workspace_sha
    assert evidence.merge_base_sha == workspace_sha
    assert (
        evidence_rejections(
            evidence,
            routine_id="rt-1",
            run_id="run-1",
            iteration=1,
            gate_type="test_command",
            gate_config={"command": "pytest suite", "expected_exit_code": 0},
            workspace_digest=workspace_digest_for(workspace),
            now=datetime.now(UTC),
            verifier=store.verify,
        )
        == []
    )


def test_executor_refuses_candidate_sha_that_is_not_the_workspace_tip(
    store: GateEvidenceStore, tmp_path: Path
) -> None:
    workspace = _workspace(tmp_path, PASSING_SUITE)

    with pytest.raises(GateEvidenceRefusal, match="candidate SHA"):
        PytestGateRunner(store).run(
            _request(
                workspace,
                candidate_sha="f" * 40,
                merge_base_sha="e" * 40,
            )
        )


def test_executor_refuses_merge_base_that_is_not_an_ancestor_of_candidate(
    store: GateEvidenceStore, tmp_path: Path
) -> None:
    """merge_base_sha must be a real ancestor of the candidate tip.

    Failing-on-revert: disabling the ``merge-base --is-ancestor`` refusal
    (``if False and ancestor.returncode != 0``) lets this non-ancestor pair
    mint signed evidence.
    """
    workspace = _workspace(tmp_path, PASSING_SUITE)
    candidate_sha = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=workspace,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    root_branch = subprocess.run(
        ["git", "rev-parse", "--abbrev-ref", "HEAD"],
        cwd=workspace,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()

    subprocess.run(
        ["git", "checkout", "-b", "side-non-ancestor"],
        cwd=workspace,
        check=True,
        capture_output=True,
    )
    (workspace / "side.txt").write_text("side\n", encoding="utf-8")
    subprocess.run(["git", "add", "side.txt"], cwd=workspace, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "side commit"],
        cwd=workspace,
        check=True,
        capture_output=True,
    )
    non_ancestor_sha = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=workspace,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    subprocess.run(
        ["git", "checkout", root_branch],
        cwd=workspace,
        check=True,
        capture_output=True,
    )

    assert non_ancestor_sha != candidate_sha
    ancestor_check = subprocess.run(
        ["git", "merge-base", "--is-ancestor", non_ancestor_sha, candidate_sha],
        cwd=workspace,
        capture_output=True,
        text=True,
    )
    assert ancestor_check.returncode != 0

    with pytest.raises(GateEvidenceRefusal, match="not an ancestor"):
        PytestGateRunner(store).run(
            _request(
                workspace,
                candidate_sha=candidate_sha,
                merge_base_sha=non_ancestor_sha,
            )
        )


def test_existing_runner_mints_a_verifiable_merge_candidate_receipt(
    store: GateEvidenceStore,
    tmp_path: Path,
) -> None:
    workspace = _workspace(tmp_path, PASSING_SUITE)
    candidate_sha = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=workspace,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    request = _request(
        workspace,
        routine_id=MERGE_GATE_ROUTINE_ID,
        run_id=candidate_sha,
        gate_type=MERGE_GATE_TYPE,
        candidate_sha=candidate_sha,
        merge_base_sha=candidate_sha,
    )

    evidence = PytestGateRunner(store).run(request)
    receipt_path = store._record_path(MERGE_GATE_ROUTINE_ID, candidate_sha)

    assert (
        verify_candidate_receipt(
            receipt_path,
            evidence_root=store.root,
            candidate_sha=candidate_sha,
            merge_base_sha=candidate_sha,
        )
        == evidence
    )


@pytest.mark.parametrize(
    ("suite", "expected_counts", "expected_rejection"),
    [
        pytest.param(FAILING_SUITE, (2, 1, 0, 1), "1 failed checks", id="failing"),
        pytest.param(EMPTY_SUITE, (0, 0, 0, 0), "zero checks (vacuous pass)", id="empty"),
    ],
)
def test_executor_reports_incomplete_runs_as_they_happened(
    store: GateEvidenceStore,
    tmp_path: Path,
    suite: str,
    expected_counts: tuple[int, int, int, int],
    expected_rejection: str,
) -> None:
    workspace = _workspace(tmp_path, suite)

    evidence = PytestGateRunner(store).run(_request(workspace))

    assert (
        evidence.checks_collected,
        evidence.checks_passed,
        evidence.checks_skipped,
        evidence.checks_failed,
    ) == expected_counts
    rejections = evidence_rejections(
        evidence,
        routine_id="rt-1",
        run_id="run-1",
        iteration=1,
        gate_type="test_command",
        gate_config={"command": "pytest suite", "expected_exit_code": 0},
        workspace_digest=workspace_digest_for(workspace),
        now=datetime.now(UTC),
        verifier=store.verify,
    )
    assert any(expected_rejection in rejection for rejection in rejections), rejections


def _skip_rejections(
    evidence: GateEvidence,
    *,
    store: GateEvidenceStore,
    workspace: Path,
) -> list[str]:
    return evidence_rejections(
        evidence,
        routine_id="rt-1",
        run_id="run-1",
        iteration=1,
        gate_type="test_command",
        gate_config={"command": "pytest suite", "expected_exit_code": 0},
        workspace_digest=workspace_digest_for(workspace),
        now=datetime.now(UTC),
        verifier=store.verify,
    )


def _write_skip_allowlist(root: Path, budget: int) -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / ".gate-skip-allowlist.yaml").write_text(
        "\n".join(
            [
                f"budget: {budget}",
                "declared:",
                '  - test: "test_conditional"',
                '    reason: "requires unavailable credentials"',
                "",
            ]
        ),
        encoding="utf-8",
    )


def test_declared_skip_within_budget_is_accepted(
    store: GateEvidenceStore,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = _workspace(tmp_path, THREE_SKIPPING_SUITE)
    verdict_root = tmp_path / "verdict-root"
    _write_skip_allowlist(verdict_root, budget=5)
    monkeypatch.setattr(gate_evidence_mod, "_repo_root", lambda: str(verdict_root))

    evidence = PytestGateRunner(store).run(_request(workspace))

    assert evidence.checks_skipped == 3
    assert _skip_rejections(evidence, store=store, workspace=workspace) == []


def test_undeclared_skip_is_refused(
    store: GateEvidenceStore,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = _workspace(tmp_path, SKIPPING_SUITE)
    verdict_root = tmp_path / "verdict-root"
    verdict_root.mkdir()
    monkeypatch.setattr(gate_evidence_mod, "_repo_root", lambda: str(verdict_root))

    evidence = PytestGateRunner(store).run(_request(workspace))

    assert _skip_rejections(evidence, store=store, workspace=workspace) == [
        "gate had 1 skipped checks; "
        "skipped checks require a declared allowlist at .gate-skip-allowlist.yaml"
    ]


def test_skip_budget_exceeded_is_refused(
    store: GateEvidenceStore,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = _workspace(tmp_path, SKIPPING_SUITE)
    verdict_root = tmp_path / "verdict-root"
    _write_skip_allowlist(verdict_root, budget=2)
    monkeypatch.setattr(gate_evidence_mod, "_repo_root", lambda: str(verdict_root))
    recorded = PytestGateRunner(store).run(_request(workspace))
    evidence = store.sign(replace(recorded, checks_collected=6, checks_passed=1, checks_skipped=5))

    assert _skip_rejections(evidence, store=store, workspace=workspace) == [
        "gate had 5 skipped checks (exceeds budget of 2)"
    ]


@pytest.mark.parametrize("with_allowlist", [False, True])
def test_no_skips_pass_regardless_of_allowlist(
    store: GateEvidenceStore,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    with_allowlist: bool,
) -> None:
    workspace = _workspace(tmp_path, PASSING_SUITE)
    verdict_root = tmp_path / "verdict-root"
    verdict_root.mkdir()
    if with_allowlist:
        _write_skip_allowlist(verdict_root, budget=5)
    monkeypatch.setattr(gate_evidence_mod, "_repo_root", lambda: str(verdict_root))

    evidence = PytestGateRunner(store).run(_request(workspace))

    assert evidence.checks_skipped == 0
    assert _skip_rejections(evidence, store=store, workspace=workspace) == []


def test_load_skip_allowlist_normalizes_declared_entries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_skip_allowlist(tmp_path, budget=5)
    monkeypatch.setattr(gate_evidence_mod, "_repo_root", lambda: str(tmp_path))

    assert load_skip_allowlist() == {
        "budget": 5,
        "declared": [{"test": "test_conditional", "reason": "requires unavailable credentials"}],
    }


def test_executor_refuses_a_gate_naming_a_target_that_does_not_exist(
    store: GateEvidenceStore, tmp_path: Path
) -> None:
    workspace = _workspace(tmp_path, PASSING_SUITE)

    with pytest.raises(GateEvidenceError):
        PytestGateRunner(store).run(_request(workspace, command="pytest tests/does-not-exist"))

    assert store.load("rt-1", "run-1") is None


def test_executor_ignores_inherited_pytest_selection_environment(
    store: GateEvidenceStore, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = _workspace(tmp_path, PASSING_SUITE)
    monkeypatch.setenv("PYTEST_ADDOPTS", "-k test_one")
    monkeypatch.setenv("PYTEST_PLUGINS", "nonexistent_plugin")

    evidence = PytestGateRunner(store).run(_request(workspace))

    assert (evidence.checks_collected, evidence.checks_passed) == (2, 2)


# --- fail-closed orchestration ----------------------------------------------


class _RaisingRunner:
    def run(self, request: GateRunRequest) -> GateEvidence:
        raise RuntimeError("executor is broken")


class _CountingRunner:
    def __init__(self, store: GateEvidenceStore) -> None:
        self.inner = PytestGateRunner(store)
        self.calls = 0

    def run(self, request: GateRunRequest) -> GateEvidence:
        self.calls += 1
        return self.inner.run(request)


def test_no_runner_yields_no_evidence(store: GateEvidenceStore, tmp_path: Path) -> None:
    res = produce_gate_evidence(None, store, _request(_workspace(tmp_path, PASSING_SUITE)))
    assert res.status == "unavailable"
    assert res.evidence is None


def test_a_broken_executor_fails_the_gate_rather_than_the_tick(
    store: GateEvidenceStore, tmp_path: Path
) -> None:
    workspace = _workspace(tmp_path, PASSING_SUITE)

    res = produce_gate_evidence(_RaisingRunner(), store, _request(workspace))
    assert res.status == "unavailable"
    assert res.evidence is None


def test_existing_evidence_is_reused_instead_of_reexecuted(
    store: GateEvidenceStore, tmp_path: Path
) -> None:
    workspace = _workspace(tmp_path, PASSING_SUITE)
    runner = _CountingRunner(store)

    first = produce_gate_evidence(runner, store, _request(workspace))
    second = produce_gate_evidence(runner, store, _request(workspace))

    assert first.status == "evidence"
    assert first.evidence is not None
    assert second.status == "evidence"
    assert first.evidence == second.evidence
    assert runner.calls == 1


def test_gate_workspace_requires_explicit_existing_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Trusted gates never default to the shared, potentially moving checkout."""
    store = GateEvidenceStore(tmp_path / "gate-evidence")
    monkeypatch.delenv("OMNIAGENTOS_GATE_WORKSPACE", raising=False)
    assert default_gate_workspace() is None
    assert default_gate_runner(store) is None

    monkeypatch.setenv("OMNIAGENTOS_GATE_WORKSPACE", str(tmp_path / "absent"))
    assert default_gate_runner(store) is None
    assert default_gate_workspace() is None

    workspace = tmp_path / "present"
    workspace.mkdir()
    monkeypatch.setenv("OMNIAGENTOS_GATE_WORKSPACE", str(workspace))
    assert default_gate_runner(store) is not None
    assert default_gate_workspace() == workspace


# --- candidate receipt caller-binding (merge-gate security property) --------
#
# candidate_receipt_rejections must bind the CALLER's expected candidate /
# merge-base SHAs to the receipt — never trust the values embedded in the
# receipt itself as the expected identity. The named counterfeit
# `candidate_sha = evidence.candidate_sha` as the first body statement makes
# every receipt→candidate comparison self-referential and must fail the
# decisive test below.


def _merge_receipt(
    store: GateEvidenceStore,
    *,
    candidate_sha: str,
    merge_base_sha: str,
    started_at: str = "2026-01-01T08:59:00Z",
    finished_at: str = "2026-01-01T09:00:00Z",
) -> GateEvidence:
    """Build a validly signed, internally consistent merge-candidate receipt."""
    # A REAL merge receipt is minted by PytestGateRunner, so a fixture that
    # stands in for one must be pytest evidence: `candidate_receipt_rejections`
    # pins the merge gate to MERGE_GATE_TOOL, and a receipt produced by any
    # other verifier is refused (see the M2 ecosystem work).
    command = "pytest tests/scheduler/test_gate_evidence.py"
    targets = ("candidate",)
    workspace_digest = "w" * 64
    unsigned = GateEvidence(
        schema=_SCHEMA_V3_WIRE,
        routine_id=MERGE_GATE_ROUTINE_ID,
        run_id=candidate_sha,
        iteration=1,
        gate_type=MERGE_GATE_TYPE,
        command=command,
        targets=targets,
        workspace_digest=workspace_digest,
        binding_digest=binding_digest(
            routine_id=MERGE_GATE_ROUTINE_ID,
            run_id=candidate_sha,
            iteration=1,
            gate_type=MERGE_GATE_TYPE,
            command=command,
            targets=targets,
            workspace_digest=workspace_digest,
            candidate_sha=candidate_sha,
            merge_base_sha=merge_base_sha,
        ),
        tool="pytest",
        tool_version="8.3.2",
        exit_code=0,
        checks_collected=1,
        checks_passed=1,
        checks_skipped=0,
        checks_failed=0,
        started_at=started_at,
        finished_at=finished_at,
        nonce="0123456789abcdef0123456789abcdef",
        workspace_sha=candidate_sha,
        workspace_tree_clean=True,
        interpreter="/usr/bin/python3",
        interpreter_version="3.12",
        node_inventory_digest="0" * 64,
        deselected_count=0,
        candidate_sha=candidate_sha,
        merge_base_sha=merge_base_sha,
    )
    signed = store.sign(unsigned)
    assert store.verify(signed)
    return signed


def test_candidate_receipt_rejects_valid_receipt_for_a_different_candidate_sha(
    store: GateEvidenceStore,
) -> None:
    """Decisive binding: caller SHA must not be overwritten by the receipt's SHA.

    Construct two distinct SHAs. A receipt issued for the first must be rejected
    when the caller asks about the second. Failing-on-revert: inserting
    ``candidate_sha = evidence.candidate_sha`` as the first statement of
    ``candidate_receipt_rejections`` makes this comparison self-referential and
    this assertion fails (empty rejection list).
    """
    receipt_candidate_sha = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    caller_candidate_sha = "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
    merge_base_sha = "cccccccccccccccccccccccccccccccccccccccc"
    assert receipt_candidate_sha != caller_candidate_sha

    evidence = _merge_receipt(
        store,
        candidate_sha=receipt_candidate_sha,
        merge_base_sha=merge_base_sha,
    )

    rejections = candidate_receipt_rejections(
        evidence,
        candidate_sha=caller_candidate_sha,
        merge_base_sha=merge_base_sha,
        now=NOW,
    )

    assert rejections, (
        "receipt for a different candidate SHA must be rejected; "
        f"got empty rejections (receipt={receipt_candidate_sha} caller={caller_candidate_sha})"
    )
    joined = " ".join(rejections).lower()
    assert "candidate" in joined, rejections
    assert any(
        "different candidate" in r.lower() or "different candidate run" in r.lower()
        for r in rejections
    ), rejections


def test_candidate_receipt_accepts_receipt_for_the_same_candidate_sha(
    store: GateEvidenceStore,
) -> None:
    """Control: a correctly bound receipt is accepted (empty rejection list)."""
    candidate_sha = "dddddddddddddddddddddddddddddddddddddddd"
    merge_base_sha = "eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee"

    evidence = _merge_receipt(
        store,
        candidate_sha=candidate_sha,
        merge_base_sha=merge_base_sha,
    )

    rejections = candidate_receipt_rejections(
        evidence,
        candidate_sha=candidate_sha,
        merge_base_sha=merge_base_sha,
        now=NOW,
    )

    assert rejections == [], rejections


def test_candidate_receipt_accepts_budgeted_skips_at_verdict_time(
    store: GateEvidenceStore,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    candidate_sha = "dededededededededededededededededededede"
    merge_base_sha = "efefefefefefefefefefefefefefefefefefefef"
    verdict_root = tmp_path / "verdict-root"
    _write_skip_allowlist(verdict_root, budget=1)
    monkeypatch.setattr(gate_evidence_mod, "_repo_root", lambda: str(verdict_root))
    evidence = store.sign(
        replace(
            _merge_receipt(
                store,
                candidate_sha=candidate_sha,
                merge_base_sha=merge_base_sha,
            ),
            checks_collected=2,
            checks_passed=1,
            checks_skipped=1,
        )
    )

    assert (
        candidate_receipt_rejections(
            evidence,
            candidate_sha=candidate_sha,
            merge_base_sha=merge_base_sha,
            now=NOW,
        )
        == []
    )


def test_candidate_receipt_rejects_valid_receipt_for_a_different_merge_base_sha(
    store: GateEvidenceStore,
) -> None:
    """Sibling binding: merge-base SHA from the caller must match the receipt."""
    candidate_sha = "ffffffffffffffffffffffffffffffffffffffff"
    receipt_merge_base_sha = "1111111111111111111111111111111111111111"
    caller_merge_base_sha = "2222222222222222222222222222222222222222"
    assert receipt_merge_base_sha != caller_merge_base_sha

    evidence = _merge_receipt(
        store,
        candidate_sha=candidate_sha,
        merge_base_sha=receipt_merge_base_sha,
    )

    rejections = candidate_receipt_rejections(
        evidence,
        candidate_sha=candidate_sha,
        merge_base_sha=caller_merge_base_sha,
        now=NOW,
    )

    assert rejections, rejections
    assert any("merge-base" in r.lower() for r in rejections), rejections


def test_candidate_receipt_rejects_stale_receipt_via_now(
    store: GateEvidenceStore,
) -> None:
    """Sibling binding: ``now`` must enforce the evidence age window."""
    candidate_sha = "3333333333333333333333333333333333333333"
    merge_base_sha = "4444444444444444444444444444444444444444"
    evidence = _merge_receipt(
        store,
        candidate_sha=candidate_sha,
        merge_base_sha=merge_base_sha,
        started_at="2026-01-01T08:59:00Z",
        finished_at="2026-01-01T09:00:00Z",
    )
    stale_now = datetime(2026, 1, 3, 9, 0, tzinfo=UTC)
    assert (stale_now - NOW).total_seconds() > MAX_EVIDENCE_AGE_SECONDS

    rejections = candidate_receipt_rejections(
        evidence,
        candidate_sha=candidate_sha,
        merge_base_sha=merge_base_sha,
        now=stale_now,
    )

    assert rejections, rejections
    assert any("stale" in r.lower() for r in rejections), rejections


def test_candidate_receipt_rejects_future_dated_receipt_via_now(
    store: GateEvidenceStore,
) -> None:
    """Sibling binding: a receipt finished far in the future relative to ``now`` is refused."""
    candidate_sha = "5555555555555555555555555555555555555555"
    merge_base_sha = "6666666666666666666666666666666666666666"
    evidence = _merge_receipt(
        store,
        candidate_sha=candidate_sha,
        merge_base_sha=merge_base_sha,
        started_at="2099-01-01T09:00:00Z",
        finished_at="2099-01-01T09:01:00Z",
    )

    rejections = candidate_receipt_rejections(
        evidence,
        candidate_sha=candidate_sha,
        merge_base_sha=merge_base_sha,
        now=NOW,
    )

    assert rejections, rejections
    assert any("future" in r.lower() for r in rejections), rejections


# --- per-step merge-gate receipts (re-run skip cache) ------------------------
#
# A step receipt lets merge-gate.sh SKIP re-running one expensive suite for a
# candidate SHA that already ran it green on the exact trial-merge tree with
# the exact command. Zero self-certification: every mismatch — SHA, tree,
# command, signature, freshness, verdict — must refuse, and a refusal always
# means "run the step for real".

_STEP_SCHEMA_WIRE = "omniagentos.gate-step-receipt.v1"
_STEP_CANDIDATE_SHA = "7" * 40
_STEP_MERGE_BASE_SHA = "8" * 40
_STEP_MERGE_TREE_SHA = "9" * 40
_STEP_COMMAND = "python -m pytest -q tests/api/ tests/swarm/ tests/sessions/"
_STEP_SUMMARY = "394 passed in 361.42s"


def _record_step(
    tmp_path: Path,
    *,
    output_text: str | None = None,
    **overrides: object,
) -> Path:
    """Record one step receipt with a real (fake-step) output artifact."""
    workspace = tmp_path / "step-workspace"
    workspace.mkdir(exist_ok=True)
    output = tmp_path / "step-output.txt"
    output.write_text(
        output_text if output_text is not None else f"...\n{_STEP_SUMMARY}\n",
        encoding="utf-8",
    )
    fields: dict[str, object] = {
        "step": "ladder",
        "candidate_sha": _STEP_CANDIDATE_SHA,
        "merge_base_sha": _STEP_MERGE_BASE_SHA,
        "merge_tree_sha": _STEP_MERGE_TREE_SHA,
        "command": _STEP_COMMAND,
        "workspace": workspace,
        "output_path": output,
        "exit_code": 0,
        "summary": _STEP_SUMMARY,
        "evidence_root": tmp_path / "gate-evidence",
        "started_at": "2026-01-01T08:59:00Z",
        "finished_at": "2026-01-01T09:00:00Z",
    }
    fields.update(overrides)
    return record_step_receipt(**fields)  # type: ignore[arg-type]


def _verify_step(tmp_path: Path, **overrides: object) -> GateStepReceipt:
    fields: dict[str, object] = {
        "step": "ladder",
        "candidate_sha": _STEP_CANDIDATE_SHA,
        "merge_base_sha": _STEP_MERGE_BASE_SHA,
        "merge_tree_sha": _STEP_MERGE_TREE_SHA,
        "command": _STEP_COMMAND,
        "evidence_root": tmp_path / "gate-evidence",
        "now": NOW,
    }
    fields.update(overrides)
    return verify_step_receipt(**fields)  # type: ignore[arg-type]


def test_step_receipt_schema_is_wire_literal() -> None:
    """STEP_SCHEMA is asserted as a literal, independent of production imports."""
    assert gate_evidence_mod.STEP_SCHEMA == _STEP_SCHEMA_WIRE


def test_step_receipt_round_trip_binds_the_exact_step_and_candidate(tmp_path: Path) -> None:
    path = _record_step(tmp_path)

    assert path.name == f"{_STEP_CANDIDATE_SHA}.json"
    assert path.parent.name == "ladder"
    assert path.parent.parent.name == "merge-gate-steps"

    receipt = _verify_step(tmp_path)
    assert receipt.schema == _STEP_SCHEMA_WIRE
    assert receipt.step == "ladder"
    assert receipt.candidate_sha == _STEP_CANDIDATE_SHA
    assert receipt.merge_base_sha == _STEP_MERGE_BASE_SHA
    assert receipt.merge_tree_sha == _STEP_MERGE_TREE_SHA
    assert receipt.command == _STEP_COMMAND
    assert receipt.summary == _STEP_SUMMARY
    assert receipt.exit_code == 0
    # Content-addressed: the digest is computed from the artifact bytes, never
    # accepted as a caller-supplied string.
    expected_digest = hashlib.sha256((tmp_path / "step-output.txt").read_bytes()).hexdigest()
    assert receipt.output_digest == expected_digest


def test_step_receipt_command_binding_ignores_formatting_only_differences(
    tmp_path: Path,
) -> None:
    _record_step(tmp_path)

    receipt = _verify_step(
        tmp_path,
        command="python  -m pytest -q 'tests/api/' tests/swarm/ tests/sessions/",
    )

    assert receipt.command == _STEP_COMMAND


@pytest.mark.parametrize(
    ("overrides", "expected"),
    [
        pytest.param({"candidate_sha": "f" * 40}, "no step receipt", id="candidate"),
        pytest.param({"step": "doctrine"}, "no step receipt", id="step"),
        pytest.param({"merge_base_sha": "f" * 40}, "different merge-base", id="merge-base"),
        pytest.param({"merge_tree_sha": "f" * 40}, "different trial-merge tree", id="merge-tree"),
        pytest.param(
            {"command": "python -m pytest -q tests/api/"},
            "does not match this gate step",
            id="command",
        ),
    ],
)
def test_step_receipt_refuses_reuse_for_any_identity_drift(
    tmp_path: Path, overrides: dict[str, object], expected: str
) -> None:
    """Decisive skip-binding: reuse is only for the exact recorded work.

    A different candidate or step resolves to an empty slot; a different
    merge-base, trial-merge tree, or command resolves to the recorded receipt
    and must still refuse. Failing-on-revert: dropping any comparison in
    ``_step_receipt_rejections`` lets a receipt skip work it never did.
    """
    _record_step(tmp_path)

    with pytest.raises(GateEvidenceRefusal, match=expected):
        _verify_step(tmp_path, **overrides)


def test_step_receipt_relocated_to_another_candidate_is_not_loadable(
    tmp_path: Path,
) -> None:
    """On-disk replay: copying a green receipt onto another candidate proves nothing."""
    path = _record_step(tmp_path)
    other_candidate = "6" * 40
    (path.parent / f"{other_candidate}.json").write_bytes(path.read_bytes())
    store = GateEvidenceStore(tmp_path / "gate-evidence")

    assert store.load_step("ladder", _STEP_CANDIDATE_SHA) is not None
    with pytest.raises(GateExecutionInfraError, match="identity mismatch"):
        store.load_step("ladder", other_candidate)


@pytest.mark.parametrize(
    ("record_overrides", "now", "expected"),
    [
        pytest.param({}, datetime(2026, 1, 3, 9, 0, tzinfo=UTC), "stale", id="stale"),
        pytest.param(
            {
                "started_at": "2099-01-01T09:00:00Z",
                "finished_at": "2099-01-01T09:01:00Z",
            },
            NOW,
            "future",
            id="future",
        ),
    ],
)
def test_step_receipt_reuse_is_refused_when_stale_or_future(
    tmp_path: Path,
    record_overrides: dict[str, object],
    now: datetime,
    expected: str,
) -> None:
    _record_step(tmp_path, **record_overrides)

    with pytest.raises(GateEvidenceRefusal, match=expected):
        _verify_step(tmp_path, now=now)


@pytest.mark.parametrize(
    ("overrides", "expected"),
    [
        pytest.param({"exit_code": 1}, "non-green", id="red-exit"),
        pytest.param({"summary": ""}, "verdict summary", id="empty-summary"),
        pytest.param(
            {"summary": "999 passed in 1.00s"},
            "does not appear in the step output",
            id="summary-not-in-output",
        ),
        pytest.param({"step": "not-a-step"}, "unknown merge-gate step", id="unknown-step"),
        pytest.param(
            {
                "summary": "total=210 caught=208 survived=2 other=0",
                "output_text": "total=210 caught=208 survived=2 other=0\n",
            },
            "SURVIVED",
            id="counterfeit-survivors",
        ),
        pytest.param(
            {
                "summary": "total=210 caught=209 survived=0 other=1",
                "output_text": "total=210 caught=209 survived=0 other=1\n",
            },
            "dead coverage",
            id="counterfeit-errored-entries",
        ),
        pytest.param(
            # The report format the corpus emitted BEFORE it counted errored
            # entries. It cannot certify that `other` was 0, so it certifies
            # nothing — fail closed rather than silently skip the check.
            {
                "summary": "total=210 caught=208 survived=0",
                "output_text": "total=210 caught=208 survived=0\n",
            },
            "NO verdict line",
            id="counterfeit-superseded-report-format",
        ),
    ],
)
def test_record_step_receipt_refuses_non_green_or_unverifiable_input(
    tmp_path: Path, overrides: dict[str, object], expected: str
) -> None:
    """Recording never accepts a pre-baked skip: only a green run with its
    real output artifact in hand can produce a receipt."""
    with pytest.raises(GateEvidenceRefusal, match=expected):
        _record_step(tmp_path, **overrides)


def test_record_step_receipt_refuses_a_missing_output_artifact(tmp_path: Path) -> None:
    with pytest.raises(GateEvidenceRefusal, match="unreadable"):
        _record_step(tmp_path, output_path=tmp_path / "absent-output.txt")


def test_step_receipt_tampered_on_disk_is_not_verifiable(tmp_path: Path) -> None:
    """Editing the stored JSON invalidates the HMAC — mismatch means re-run."""
    path = _record_step(tmp_path)
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["summary"] = "9999 passed in 0.01s"
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(GateExecutionInfraError, match="signature"):
        _verify_step(tmp_path)


def test_step_receipt_refresh_replaces_prior_receipt_for_the_same_slot(
    tmp_path: Path,
) -> None:
    """A step receipt is a skip cache, not run history: unlike run evidence
    (exclusive create, ``GateEvidenceExists``), a newer green receipt for the
    same ``(step, candidate)`` slot supersedes the old one."""
    _record_step(tmp_path)
    _record_step(
        tmp_path,
        summary="395 passed in 300.00s",
        output_text="...\n395 passed in 300.00s\n",
    )

    receipt = _verify_step(tmp_path)
    assert receipt.summary == "395 passed in 300.00s"


def test_step_receipt_absent_is_refused_not_passed(tmp_path: Path) -> None:
    GateEvidenceStore(tmp_path / "gate-evidence")  # trust root exists, slot empty

    with pytest.raises(GateEvidenceRefusal, match="no step receipt"):
        _verify_step(tmp_path)


def test_verify_step_receipt_creates_no_trust_state_when_key_absent(
    tmp_path: Path,
) -> None:
    """Verification must use only an existing key; it must not mint trust state."""
    empty_root = tmp_path / "absent-trust"

    with pytest.raises(GateExecutionInfraError, match="signing key"):
        verify_step_receipt(
            step="ladder",
            candidate_sha=_STEP_CANDIDATE_SHA,
            merge_base_sha=_STEP_MERGE_BASE_SHA,
            merge_tree_sha=_STEP_MERGE_TREE_SHA,
            command=_STEP_COMMAND,
            evidence_root=empty_root,
        )

    assert not (empty_root / "signing.key").exists()


def test_step_receipt_with_an_unlisted_step_name_never_verifies(tmp_path: Path) -> None:
    """Fail-closed on unknown steps even for a validly signed hand-crafted receipt."""
    store = GateEvidenceStore(tmp_path / "gate-evidence")
    store.record_step(
        GateStepReceipt(
            schema=_STEP_SCHEMA_WIRE,
            step="not-a-step",
            candidate_sha=_STEP_CANDIDATE_SHA,
            merge_base_sha=_STEP_MERGE_BASE_SHA,
            merge_tree_sha=_STEP_MERGE_TREE_SHA,
            command=_STEP_COMMAND,
            workspace_digest="w" * 64,
            output_digest="0" * 64,
            exit_code=0,
            summary=_STEP_SUMMARY,
            started_at="2026-01-01T08:59:00Z",
            finished_at="2026-01-01T09:00:00Z",
            nonce="0123456789abcdef0123456789abcdef",
        )
    )

    with pytest.raises(GateEvidenceRefusal, match="not a known merge-gate step"):
        _verify_step(tmp_path, step="not-a-step")


def test_converged_pipeline_step_names_are_receipt_allowlisted() -> None:
    # 2026-08-12: "contracts-scripts" was split into a PARALLEL "contracts" leg
    # and a SERIAL "scripts" step, and "scheduler" was lifted out of the xdist
    # ladder into its own SERIAL step (the os.killpg reap false-refuses trains
    # under xdist). Each serial step earns its own skip-on-reuse receipt, so all
    # three ids must be allowlisted or record-step/verify-step refuse them as
    # "unknown merge-gate step" and the gate spams a record failure every run.
    assert "scheduler" in gate_evidence_mod.MERGE_GATE_STEP_NAMES
    assert "contracts" in gate_evidence_mod.MERGE_GATE_STEP_NAMES
    assert "scripts" in gate_evidence_mod.MERGE_GATE_STEP_NAMES
    assert "pipeline-tests" in gate_evidence_mod.MERGE_GATE_STEP_NAMES
    # The retired id must be gone: its both-dirs-under-`-n` command string no
    # longer exists, so nothing must be able to reuse a stale receipt filed
    # under it.
    assert "contracts-scripts" not in gate_evidence_mod.MERGE_GATE_STEP_NAMES


def test_step_receipt_cli_records_verifies_and_refuses_mismatch(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """merge-gate.sh consumes exactly this CLI: record-step then verify-step."""
    workspace = tmp_path / "step-workspace"
    workspace.mkdir()
    output = tmp_path / "step-output.txt"
    output.write_text(f"...\n{_STEP_SUMMARY}\n", encoding="utf-8")
    root = tmp_path / "gate-evidence"
    base_argv = [
        "--evidence-root",
        str(root),
        "--step",
        "ladder",
        "--candidate-sha",
        _STEP_CANDIDATE_SHA,
        "--merge-base-sha",
        _STEP_MERGE_BASE_SHA,
        "--merge-tree-sha",
        _STEP_MERGE_TREE_SHA,
        "--command",
        _STEP_COMMAND,
    ]

    recorded = gate_evidence_mod._main(
        [
            "record-step",
            *base_argv,
            "--workspace",
            str(workspace),
            "--output",
            str(output),
            "--exit-code",
            "0",
            "--summary",
            _STEP_SUMMARY,
        ]
    )
    assert recorded == 0
    assert "recorded" in capsys.readouterr().out

    verified = gate_evidence_mod._main(["verify-step", *base_argv])
    captured = capsys.readouterr()
    assert verified == 0
    assert "reused" in captured.out
    assert _STEP_SUMMARY in captured.out

    mismatch_argv = [arg if arg != _STEP_MERGE_TREE_SHA else "f" * 40 for arg in base_argv]
    refused = gate_evidence_mod._main(["verify-step", *mismatch_argv])
    captured = capsys.readouterr()
    assert refused == 1
    assert "REFUSED" in captured.err
