"""P3 -- the shell classifier's ``extra_roots`` (session granted-scope) widening.

``classify_shell(..., extra_roots=[...])`` relaxes ONLY the in-project WRITE
boundary: a write proven inside a granted root classifies INTERNAL_REVERSIBLE
instead of IRREVERSIBLE. Every OTHER guard -- deletes, secret reads, money,
out-of-scope writes -- must be unchanged, because they are evaluated against
``project_dir`` BEFORE the write-scope test.
"""

from __future__ import annotations

from pathlib import Path

from omniagentos.contracts import ActionClass
from omniagentos.policy.shell import classify_shell


def _dirs(tmp_path: Path) -> tuple[str, str, str]:
    project = tmp_path / "project"
    granted = tmp_path / "granted"
    outside = tmp_path / "outside"
    for directory in (project, granted, outside):
        directory.mkdir()
    return str(project), str(granted), str(outside)


def test_write_into_granted_root_is_in_scope(tmp_path: Path) -> None:
    project, granted, _ = _dirs(tmp_path)
    target = f"{granted}/out.txt"
    # Without the grant it is an out-of-scope write -> hard stop.
    assert classify_shell(f"echo hi > {target}", project) == ActionClass.IRREVERSIBLE
    # With the grant it is an in-scope, reversible write -> auto.
    assert (
        classify_shell(f"echo hi > {target}", project, extra_roots=[granted])
        == ActionClass.INTERNAL_REVERSIBLE
    )


def test_heredoc_write_into_granted_root_is_in_scope(tmp_path: Path) -> None:
    """Heredoc bodies must not be classified as unknown commands (Desktop bug)."""
    project, granted, _ = _dirs(tmp_path)
    target = f"{granted}/bird-clock.html"
    cmd = f"cat > {target} << 'EOF'\n<!DOCTYPE html>\nEOF"
    assert classify_shell(cmd, project) == ActionClass.IRREVERSIBLE
    assert classify_shell(cmd, project, extra_roots=[granted]) == ActionClass.INTERNAL_REVERSIBLE


def test_interpreter_in_workspace_auto_runs(tmp_path: Path) -> None:
    """In-scope *script* path auto-runs; inline ``-c`` payloads are always IRREVERSIBLE.

    Reversal of the prior ``python3 -c`` auto-run claim (HANDOFF Task 1.2): no
    denylist over Turing-complete payloads can be made sound, so the whole
    inline shape parks for a human. A normal ``python3 ok.py`` inside the
    workspace remains INTERNAL_REVERSIBLE.
    """
    project, _, _ = _dirs(tmp_path)
    script = Path(project) / "ok.py"
    script.write_text("print(1)\n", encoding="utf-8")
    assert classify_shell(f"python3 {script}", project) == ActionClass.INTERNAL_REVERSIBLE
    assert classify_shell("python3 -c 'print(1)'", project) == ActionClass.IRREVERSIBLE
    assert classify_shell("node --check x.js", project) == ActionClass.INTERNAL_REVERSIBLE


def test_cp_into_granted_root_is_in_scope(tmp_path: Path) -> None:
    project, granted, _ = _dirs(tmp_path)
    assert (
        classify_shell(f"cp {project}/a.txt {granted}/b.txt", project, extra_roots=[granted])
        == ActionClass.INTERNAL_REVERSIBLE
    )


def test_write_outside_every_root_still_hard_stops(tmp_path: Path) -> None:
    project, granted, outside = _dirs(tmp_path)
    assert (
        classify_shell(f"echo hi > {outside}/x.txt", project, extra_roots=[granted])
        == ActionClass.IRREVERSIBLE
    )


def test_delete_inside_write_grant_still_requires_approval(tmp_path: Path) -> None:
    """A root grant widens write proof, not deletion authority."""
    project, granted, _ = _dirs(tmp_path)
    assert (
        classify_shell(f"rm -rf {granted}/build", project, extra_roots=[granted])
        == ActionClass.IRREVERSIBLE
    )


def test_delete_outside_every_root_still_hard_stops(tmp_path: Path) -> None:
    project, granted, outside = _dirs(tmp_path)
    assert (
        classify_shell(f"rm -rf {outside}/build", project, extra_roots=[granted])
        == ActionClass.IRREVERSIBLE
    )


def test_delete_of_a_scope_root_itself_still_hard_stops(tmp_path: Path) -> None:
    """Deleting the workspace is destroying the scope that made it provable."""
    project, granted, _ = _dirs(tmp_path)
    assert classify_shell(f"rm -rf {project}", project, extra_roots=[granted]) == (
        ActionClass.IRREVERSIBLE
    )
    assert classify_shell(f"rm -rf {granted}", project, extra_roots=[granted]) == (
        ActionClass.IRREVERSIBLE
    )


def test_delete_without_any_scope_still_hard_stops(tmp_path: Path) -> None:
    project, _granted, _ = _dirs(tmp_path)
    assert classify_shell(f"rm -rf {project}/build", None) == ActionClass.IRREVERSIBLE


def test_secret_read_inside_granted_root_stays_hard_stop(tmp_path: Path) -> None:
    """Invariant (1): a credential store inside a granted root is still refused."""
    project, granted, _ = _dirs(tmp_path)
    assert (
        classify_shell(f"cat {granted}/id_rsa", project, extra_roots=[granted])
        == ActionClass.IRREVERSIBLE
    )


def test_no_extra_roots_is_byte_identical(tmp_path: Path) -> None:
    """Invariant (3): omitting extra_roots reproduces the pre-P3 classification."""
    project, granted, _ = _dirs(tmp_path)
    command = f"echo hi > {granted}/out.txt"
    assert classify_shell(command, project) == classify_shell(command, project, extra_roots=None)
    assert classify_shell(command, project) == ActionClass.IRREVERSIBLE
