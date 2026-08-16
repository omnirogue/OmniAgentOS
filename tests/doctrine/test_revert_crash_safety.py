"""Crash-safety proofs for the bounded doctrine runtime copy."""

from __future__ import annotations

import hashlib
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from tests.doctrine import DoctrineError, TextReplace, revert_test
from tests.doctrine import revert as revert_module

_REPO = Path(__file__).resolve().parents[2]
_SUBJECT = _REPO / "tests" / "doctrine" / "_fixtures" / "subject.py"
_NODE = "tests/doctrine/_fixtures/test_subject.py::test_empty_denominator_returns_none"
_ORIGINAL = "    if denominator == 0:\n        return None"
_MUTATED = "    if denominator == 0:\n        return 0.0"


def _tracked_status() -> bytes:
    return subprocess.run(
        ["git", "status", "--porcelain", "--untracked-files=no"],
        cwd=_REPO,
        check=True,
        capture_output=True,
    ).stdout


def _tracked_snapshot() -> dict[bytes, tuple[int, bytes]]:
    """Hash every tracked entry, preserving mode/link identity in the comparison."""
    names = subprocess.run(
        ["git", "ls-files", "-z"],
        cwd=_REPO,
        check=True,
        capture_output=True,
    ).stdout.split(b"\0")
    snapshot: dict[bytes, tuple[int, bytes]] = {}
    for raw_name in names:
        if not raw_name:
            continue
        path = _REPO / os.fsdecode(raw_name)
        mode = path.lstat().st_mode
        payload = os.fsencode(os.readlink(path)) if path.is_symlink() else path.read_bytes()
        snapshot[raw_name] = (mode, hashlib.sha256(payload).digest())
    return snapshot


def _wait_for(path: Path, process: subprocess.Popen[bytes], timeout: float = 15.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.is_file():
            return
        if process.poll() is not None:
            stdout, stderr = process.communicate()
            pytest.fail(
                "doctrine child exited before mutation-ready signal\n"
                f"stdout={stdout!r}\nstderr={stderr!r}"
            )
        time.sleep(0.02)
    process.kill()
    stdout, stderr = process.communicate()
    pytest.fail(
        "timed out waiting for doctrine mutation-ready signal\n"
        f"stdout={stdout!r}\nstderr={stderr!r}"
    )


def _residue_shape(root: Path) -> tuple[int, int, int]:
    """Return entry count, regular bytes, and link count without following links."""
    entries = 1  # root
    regular_bytes = 0
    symlinks = 0
    for directory, dirnames, filenames in os.walk(root, followlinks=False):
        base = Path(directory)
        for name in [*dirnames, *filenames]:
            candidate = base / name
            entries += 1
            if candidate.is_symlink():
                symlinks += 1
            elif candidate.is_file():
                regular_bytes += candidate.lstat().st_size
    return entries, regular_bytes, symlinks


def test_success_removes_runtime_copy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scratch_parent = tmp_path / "scratch"
    scratch_parent.mkdir()
    monkeypatch.setattr(revert_module, "_scratch_parent", lambda _root: scratch_parent)

    report = revert_test(
        target=_SUBJECT,
        mutation=TextReplace(old=_ORIGINAL, new=_MUTATED),
        nodeid=_NODE,
    )

    assert report.mutated_failure.failed
    assert report.restored_pass.passed
    assert list(scratch_parent.iterdir()) == []


def test_killed_child_leaves_only_bounded_disposable_state(tmp_path: Path) -> None:
    """Kill after scratch mutation; producer bytes and tracked status stay identical."""
    scratch_parent = tmp_path / "scratch"
    scratch_parent.mkdir()
    ready = tmp_path / "mutation-ready"
    before_bytes = _SUBJECT.read_bytes()
    before_status = _tracked_status()
    before_snapshot = _tracked_snapshot()

    script = f"""
import time
from pathlib import Path
from tests.doctrine import revert_test

ready = Path({str(ready)!r})

def mutate_target(path):
    path.write_text({(_SUBJECT.read_text(encoding='utf-8').replace(_ORIGINAL, _MUTATED))!r}, encoding="utf-8")
    ready.write_text(str(path), encoding="utf-8")
    while True:
        time.sleep(1)

revert_test(
    target=Path({str(_SUBJECT)!r}),
    mutation=mutate_target,
    nodeid={_NODE!r},
)
"""
    env = os.environ.copy()
    env["TMPDIR"] = str(scratch_parent)
    env["PYTHONPATH"] = str(_REPO)
    process = subprocess.Popen(
        [sys.executable, "-c", script],
        cwd=_REPO,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        _wait_for(ready, process)
        isolated_target = Path(ready.read_text(encoding="utf-8"))
        assert _SUBJECT.read_bytes() == before_bytes
        assert _tracked_status() == before_status
        assert _tracked_snapshot() == before_snapshot
        process.kill()
        process.communicate(timeout=10)

        assert process.returncode is not None and process.returncode < 0
        assert _SUBJECT.read_bytes() == before_bytes
        assert _tracked_status() == before_status
        assert _tracked_snapshot() == before_snapshot

        roots = list(scratch_parent.glob(f"{revert_module._SCRATCH_PREFIX}*"))
        assert len(roots) == 1
        root = roots[0]
        assert not root.is_relative_to(_REPO)
        assert isolated_target.is_relative_to(root)
        assert isolated_target != _SUBJECT
        assert isolated_target.stat().st_ino != _SUBJECT.stat().st_ino
        assert isolated_target.read_text(encoding="utf-8").find("return 0.0") >= 0

        entry_count, regular_bytes, symlink_count = _residue_shape(root)
        assert entry_count <= revert_module._MAX_SCRATCH_ENTRIES
        assert regular_bytes <= revert_module._MAX_SCRATCH_BYTES
        assert symlink_count == 0
    finally:
        if process.poll() is None:
            process.kill()
            process.communicate(timeout=10)


def test_in_tree_temp_parent_is_refused_before_materialisation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    before = _SUBJECT.read_bytes()
    monkeypatch.setattr(revert_module.tempfile, "gettempdir", lambda: str(_REPO))

    with pytest.raises(DoctrineError, match="temporary directory inside the producing tree"):
        revert_test(
            target=_SUBJECT,
            mutation=TextReplace(old=_ORIGINAL, new=_MUTATED),
            nodeid=_NODE,
        )

    assert _SUBJECT.read_bytes() == before
