"""Tests for the fail-closed deliverable checker (omniagentos.intake.deliverable_checks).

This checker is lane 1 of a two-lane plan and is invocation-agnostic and
inert on its own: nothing in the runtime calls it yet (that is lane 2, a
separate change to omniagentos/intake/service.py). These tests invoke it
exactly the way lane 2 will — as a subprocess run with cwd set to a
workspace directory and `--spec <path>` — so they exercise the real
contract, not an in-process shortcut.

FAIL-CLOSED IS THE CORE PROPERTY UNDER TEST: every absence path (missing
spec, empty checks list, unknown check kind, empty workspace, escaping
paths) must grade as FAILURE (non-zero exit), never as a vacuous pass.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path

import pytest

CHECKER = Path(__file__).resolve().parents[2] / "omniagentos" / "intake" / "deliverable_checks.py"


def run_checker(workspace: Path, spec_path: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(CHECKER), "--spec", str(spec_path)],
        cwd=str(workspace),
        capture_output=True,
        text=True,
        timeout=30,
    )


def write_spec(path: Path, checks: list[dict]) -> Path:
    path.write_text(json.dumps({"checks": checks}))
    return path


def parse_receipt(result: subprocess.CompletedProcess[str]) -> dict:
    assert result.stdout.strip(), f"expected JSON receipt on stdout, got empty. stderr={result.stderr}"
    return json.loads(result.stdout)


# ---------------------------------------------------------------------------
# Importability / CLI shape
# ---------------------------------------------------------------------------


def test_module_imports_cleanly_with_no_third_party_import() -> None:
    # DONE-criterion from the plan: the module imports cleanly with no
    # third-party import. Importing it must not raise, and stdlib-only.
    import omniagentos.intake.deliverable_checks as mod  # noqa: F401

    assert hasattr(mod, "main")


def test_help_exits_zero() -> None:
    result = subprocess.run(
        [sys.executable, str(CHECKER), "--help"],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr


def test_missing_spec_argument_exits_nonzero() -> None:
    result = subprocess.run(
        [sys.executable, str(CHECKER)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode != 0


# ---------------------------------------------------------------------------
# Fail-closed: spec-level absence must never read as a pass
# ---------------------------------------------------------------------------


def test_missing_spec_fails_closed(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    missing_spec = workspace / "nope.json"

    result = run_checker(workspace, missing_spec)

    assert result.returncode != 0
    receipt = parse_receipt(result)
    assert receipt["ok"] is False
    assert receipt["reason"]
    assert "not found" in receipt["reason"].lower()


def test_unparseable_spec_fails_closed(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    spec_path = workspace / "spec.json"
    spec_path.write_text("{ this is not valid json ]")

    result = run_checker(workspace, spec_path)

    assert result.returncode != 0
    receipt = parse_receipt(result)
    assert receipt["ok"] is False
    assert receipt["reason"]


def test_empty_checks_list_fails_closed(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    spec_path = write_spec(workspace / "spec.json", [])

    result = run_checker(workspace, spec_path)

    assert result.returncode != 0
    receipt = parse_receipt(result)
    assert receipt["ok"] is False
    assert receipt["checks"] == []


def test_unknown_check_kind_fails_closed(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    spec_path = write_spec(
        workspace / "spec.json", [{"kind": "not_a_real_kind", "path": "x.txt"}]
    )

    result = run_checker(workspace, spec_path)

    assert result.returncode != 0
    receipt = parse_receipt(result)
    assert receipt["ok"] is False
    assert len(receipt["checks"]) == 1
    assert receipt["checks"][0]["ok"] is False
    assert receipt["checks"][0]["kind"] == "not_a_real_kind"


def test_non_object_spec_fails_closed(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    spec_path = workspace / "spec.json"
    spec_path.write_text(json.dumps([1, 2, 3]))

    result = run_checker(workspace, spec_path)

    assert result.returncode != 0
    receipt = parse_receipt(result)
    assert receipt["ok"] is False


def test_non_object_check_entry_fails_closed(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    spec_path = write_spec(workspace / "spec.json", ["not-a-dict"])

    result = run_checker(workspace, spec_path)

    assert result.returncode != 0
    receipt = parse_receipt(result)
    assert receipt["ok"] is False
    assert receipt["checks"][0]["ok"] is False


# ---------------------------------------------------------------------------
# produced_output: the floor check
# ---------------------------------------------------------------------------


def test_produced_output_fails_when_workspace_empty_and_passes_with_a_file(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    spec_path = write_spec(workspace / "spec.json", [{"kind": "produced_output"}])

    # Empty workspace (aside from the spec itself) -> must fail closed, not
    # vacuous-pass just because the process ran without error.
    result = run_checker(workspace, spec_path)
    assert result.returncode != 0
    receipt = parse_receipt(result)
    assert receipt["ok"] is False
    assert receipt["checks"][0]["kind"] == "produced_output"
    assert receipt["checks"][0]["ok"] is False

    # Now write a non-empty output file newer than the spec -> must pass.
    output = workspace / "report.txt"
    output.write_text("the deliverable")

    result2 = run_checker(workspace, spec_path)
    assert result2.returncode == 0, result2.stdout + result2.stderr
    receipt2 = parse_receipt(result2)
    assert receipt2["ok"] is True
    assert receipt2["checks"][0]["ok"] is True


def test_produced_output_ignores_control_dir(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    spec_path = write_spec(workspace / "spec.json", [{"kind": "produced_output"}])

    # A file written only under .omniagentos/ must NOT count as produced
    # output — that dir holds the checker + spec, not the deliverable.
    control_dir = workspace / ".omniagentos"
    control_dir.mkdir()
    (control_dir / "decoy.txt").write_text("not the deliverable")

    result = run_checker(workspace, spec_path)
    assert result.returncode != 0
    receipt = parse_receipt(result)
    assert receipt["ok"] is False


def test_produced_output_ignores_empty_file(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    spec_path = write_spec(workspace / "spec.json", [{"kind": "produced_output"}])
    (workspace / "empty.txt").write_text("")

    result = run_checker(workspace, spec_path)
    assert result.returncode != 0
    receipt = parse_receipt(result)
    assert receipt["ok"] is False


# ---------------------------------------------------------------------------
# file_exists / must_include / must_not_include
# ---------------------------------------------------------------------------


def test_file_exists_passes_and_fails(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()

    spec_path = write_spec(
        workspace / "spec.json", [{"kind": "file_exists", "path": "report.md"}]
    )
    result = run_checker(workspace, spec_path)
    assert result.returncode != 0

    (workspace / "report.md").write_text("hello")
    result2 = run_checker(workspace, spec_path)
    assert result2.returncode == 0, result2.stdout + result2.stderr


def test_file_exists_fails_on_empty_file(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    (workspace / "report.md").write_text("")
    spec_path = write_spec(
        workspace / "spec.json", [{"kind": "file_exists", "path": "report.md"}]
    )

    result = run_checker(workspace, spec_path)
    assert result.returncode != 0
    receipt = parse_receipt(result)
    assert receipt["checks"][0]["ok"] is False


def test_must_include_passes_and_fails(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    (workspace / "report.md").write_text("Summary: all good")

    spec_pass = write_spec(
        workspace / "spec_pass.json",
        [{"kind": "must_include", "path": "report.md", "text": "Summary"}],
    )
    result = run_checker(workspace, spec_pass)
    assert result.returncode == 0, result.stdout + result.stderr

    spec_fail = write_spec(
        workspace / "spec_fail.json",
        [{"kind": "must_include", "path": "report.md", "text": "Nowhere"}],
    )
    result2 = run_checker(workspace, spec_fail)
    assert result2.returncode != 0


def test_must_include_is_literal_not_regex(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    (workspace / "report.md").write_text("cost is $5.00 exactly")

    # '.' would match any char as a regex; as a literal it must only match
    # a literal '.'.
    spec_path = write_spec(
        workspace / "spec.json",
        [{"kind": "must_include", "path": "report.md", "text": "$5.00"}],
    )
    result = run_checker(workspace, spec_path)
    assert result.returncode == 0, result.stdout + result.stderr


def test_must_include_fails_when_file_missing(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    spec_path = write_spec(
        workspace / "spec.json",
        [{"kind": "must_include", "path": "nope.md", "text": "anything"}],
    )
    result = run_checker(workspace, spec_path)
    assert result.returncode != 0


def test_must_not_include_passes_and_fails(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    (workspace / "report.md").write_text("Summary: all good")

    spec_pass = write_spec(
        workspace / "spec_pass.json",
        [{"kind": "must_not_include", "path": "report.md", "text": "TODO"}],
    )
    result = run_checker(workspace, spec_pass)
    assert result.returncode == 0, result.stdout + result.stderr

    spec_fail = write_spec(
        workspace / "spec_fail.json",
        [{"kind": "must_not_include", "path": "report.md", "text": "Summary"}],
    )
    result2 = run_checker(workspace, spec_fail)
    assert result2.returncode != 0


# ---------------------------------------------------------------------------
# Path confinement
# ---------------------------------------------------------------------------


def test_path_escape_is_refused(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("secret")

    escaping_paths = ["/etc/passwd", "../outside.txt"]

    for bad_path in escaping_paths:
        spec_path = write_spec(
            workspace / "spec.json", [{"kind": "file_exists", "path": bad_path}]
        )
        result = run_checker(workspace, spec_path)
        assert result.returncode != 0, f"expected refusal for {bad_path!r}"
        receipt = parse_receipt(result)
        assert receipt["checks"][0]["ok"] is False
        assert "escape" in receipt["checks"][0]["detail"].lower() or (
            "path" in receipt["checks"][0]["detail"].lower()
        )

    # Symlink escape: a symlink inside the workspace pointing outside it.
    link = workspace / "escape_link.txt"
    try:
        link.symlink_to(outside)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks not supported on this platform")

    spec_path = write_spec(
        workspace / "spec_symlink.json",
        [{"kind": "file_exists", "path": "escape_link.txt"}],
    )
    result = run_checker(workspace, spec_path)
    assert result.returncode != 0
    receipt = parse_receipt(result)
    assert receipt["checks"][0]["ok"] is False


def test_symlinked_directory_escape_is_refused(tmp_path: Path) -> None:
    """A dir symlink inside the workspace cannot be traversed out of it.

    Containment is proved by inode ancestry, so neither the plain spelling
    through the link nor a ``..`` spelling whose kernel meaning depends on
    following the link first can reach the outside file.
    """
    workspace = tmp_path / "ws"
    workspace.mkdir()
    outside_dir = tmp_path / "outside"
    outside_dir.mkdir()
    (outside_dir / "secret.txt").write_text("secret")

    link = workspace / "link"
    try:
        link.symlink_to(outside_dir, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks not supported on this platform")

    for bad_path in ("link/secret.txt", "link/../outside/secret.txt"):
        spec_path = write_spec(
            workspace / "spec.json",
            [{"kind": "must_include", "path": bad_path, "text": "secret"}],
        )
        result = run_checker(workspace, spec_path)
        assert result.returncode != 0, f"expected refusal for {bad_path!r}"
        receipt = parse_receipt(result)
        assert receipt["checks"][0]["ok"] is False
        # The outside file was never read: the refusal is a containment
        # verdict, not a "substring not found" content answer.
        assert "found required substring" not in receipt["checks"][0]["detail"]


def test_filesystem_error_during_containment_proof_refuses_with_a_receipt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A filesystem error while proving containment refuses, never crashes.

    This module's contract is that callers ALWAYS get a parseable JSON
    receipt, so the ancestry proof is guarded here rather than relying on the
    primitive's own internal error discipline. Run in-process (not as a
    subprocess) because the fault has to be injected into the primitive.
    """
    import omniagentos.intake.deliverable_checks as mod

    workspace = tmp_path / "ws"
    workspace.mkdir()
    spec_path = write_spec(
        workspace / "spec.json",
        [{"kind": "file_exists", "path": "report.md"}, {"kind": "produced_output"}],
    )
    (workspace / "report.md").write_text("the deliverable")

    def boom(*_args: object, **_kwargs: object) -> object:
        raise OSError(5, "Input/output error")

    monkeypatch.setattr(mod, "inode_relative_parts", boom)
    monkeypatch.setattr(mod, "inode_paths_equal", boom)
    monkeypatch.chdir(workspace)

    exit_code = mod.main(["--spec", str(spec_path)])

    receipt = json.loads(capsys.readouterr().out)
    assert exit_code == 1
    assert receipt["ok"] is False
    # Both checks graded, both refused: an unprovable path is a refusal, and
    # a file that cannot be proved contained is never credited as output.
    assert [check["ok"] for check in receipt["checks"]] == [False, False]


def test_produced_output_does_not_credit_a_symlink_out_of_the_workspace(
    tmp_path: Path,
) -> None:
    """A symlinked file pointing outside must not satisfy the floor check.

    os.walk does not follow symlinked *directories*, but a symlinked file is
    listed and ``stat()`` follows it — without a containment proof, content
    the run never produced would read as its deliverable.
    """
    workspace = tmp_path / "ws"
    workspace.mkdir()
    spec_path = write_spec(workspace / "spec.json", [{"kind": "produced_output"}])

    outside = tmp_path / "outside.txt"
    outside.write_text("content this run did not produce")

    link = workspace / "report.txt"
    try:
        link.symlink_to(outside)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks not supported on this platform")

    result = run_checker(workspace, spec_path)
    assert result.returncode != 0, result.stdout
    receipt = parse_receipt(result)
    assert receipt["ok"] is False
    assert receipt["checks"][0]["ok"] is False

    # A real file in the same workspace still passes: the refusal above is
    # about the escape, not a blanket failure of the floor check.
    (workspace / "real.txt").write_text("the deliverable")
    result2 = run_checker(workspace, spec_path)
    assert result2.returncode == 0, result2.stdout + result2.stderr
    assert parse_receipt(result2)["checks"][0]["detail"].endswith("real.txt")


# ---------------------------------------------------------------------------
# Receipt shape
# ---------------------------------------------------------------------------


def test_receipt_enumerates_every_check(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()

    checks = [
        {"kind": "produced_output"},
        {"kind": "file_exists", "path": "report.md"},
        {"kind": "must_include", "path": "report.md", "text": "Summary"},
        {"kind": "must_not_include", "path": "report.md", "text": "TODO"},
    ]
    # Write the spec BEFORE the deliverable file so the produced_output
    # check's mtime-ordering requirement (file mtime >= spec mtime) holds,
    # matching real usage: the spec is materialized before the agent runs.
    spec_path = write_spec(workspace / "spec.json", checks)
    (workspace / "report.md").write_text("Summary: everything is finished")

    result = run_checker(workspace, spec_path)
    assert result.returncode == 0, result.stdout + result.stderr

    receipt = parse_receipt(result)
    assert receipt["ok"] is True
    assert isinstance(receipt["checks"], list)
    assert len(receipt["checks"]) == len(checks)
    for expected, actual in zip(checks, receipt["checks"], strict=True):
        assert actual["kind"] == expected["kind"]
        assert actual["ok"] is True
        assert "detail" in actual and actual["detail"]

    expected_digest = hashlib.sha256(CHECKER.read_bytes()).hexdigest()
    assert receipt["checker_sha256"] == expected_digest


def test_receipt_is_emitted_even_on_spec_level_failure(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    missing_spec = workspace / "nope.json"

    result = run_checker(workspace, missing_spec)
    receipt = parse_receipt(result)

    expected_digest = hashlib.sha256(CHECKER.read_bytes()).hexdigest()
    assert receipt["checker_sha256"] == expected_digest
    assert receipt["checks"] == []
