"""Adversarial and mutation gates for Darwin kernel-atomic WorkFS installs."""

from __future__ import annotations

import ctypes
import errno
import importlib
import os
import sys
from pathlib import Path
from typing import Any

import pytest

from omniagentos.workfs import WorkfsError, ensure, map_scope, read_tree
from omniagentos.workfs import darwin as darwin_backend
from omniagentos.workfs.darwin import (
    ATOMIC_RENAME_FLAGS,
    RENAME_EXCL,
    RENAME_NOFOLLOW_ANY,
    RENAME_RESOLVE_BENEATH,
    rename_stage_atomic,
)
from omniagentos.workfs.root import STAGE_PREFIX, WORK_ROOT_ENV, work_root

pytestmark = pytest.mark.skipif(
    sys.platform != "darwin", reason="workfs requires Darwin kernel atomic operations (renameatx_np)"
)

ensure_mod = importlib.import_module("omniagentos.workfs.ensure")


def _tree_bytes(root: Path) -> dict[str, bytes | str]:
    snapshot: dict[str, bytes | str] = {}
    for path in sorted(root.rglob("*")):
        relative = str(path.relative_to(root))
        if path.is_symlink():
            snapshot[relative] = f"symlink:{os.readlink(path)}"
        elif path.is_file():
            snapshot[relative] = path.read_bytes()
        elif path.is_dir():
            snapshot[relative] = "directory"
    return snapshot


def _stages(root: Path) -> list[Path]:
    return sorted(path for path in root.iterdir() if path.name.startswith(STAGE_PREFIX))


def test_missing_root_fails_without_creating_it(tmp_path: Path) -> None:
    missing = tmp_path / "missing-Work"
    with pytest.raises(WorkfsError) as exc:
        ensure("Acme", root=missing)
    assert exc.value.code == "root_unavailable"
    assert not missing.exists()


@pytest.mark.parametrize("target_exists", [True, False], ids=["outside", "dangling"])
def test_env_configured_root_symlink_never_launders_authority(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    target_exists: bool,
) -> None:
    """Production ``root=None`` must let lstat/O_NOFOLLOW see the configured link."""
    configured = tmp_path / "Work"
    configured.mkdir()
    (configured / "original.marker").write_bytes(b"original-v1")
    original = tmp_path / "Work-original"
    os.rename(configured, original)

    target = tmp_path / ("outside" if target_exists else "missing-target")
    if target_exists:
        target.mkdir()
        (target / "outside.marker").write_bytes(b"outside-v1")
    witness = tmp_path / "separate-witness"
    witness.mkdir()
    (witness / "witness.marker").write_bytes(b"witness-v1")
    configured.symlink_to(target, target_is_directory=True)

    original_before = _tree_bytes(original)
    target_before = _tree_bytes(target) if target_exists else {}
    witness_before = _tree_bytes(witness)
    monkeypatch.setenv(WORK_ROOT_ENV, str(configured))

    assert work_root() == configured
    with pytest.raises(WorkfsError) as mutation:
        ensure("EscapedCo")
    assert mutation.value.code == "root_not_directory"
    with pytest.raises(WorkfsError) as mapping:
        map_scope("EscapedCo")
    assert mapping.value.code == "root_not_directory"
    with pytest.raises(WorkfsError) as listing:
        read_tree()
    assert listing.value.code == "root_not_directory"

    assert _tree_bytes(original) == original_before
    assert _tree_bytes(witness) == witness_before
    if target_exists:
        assert _tree_bytes(target) == target_before
        assert not (target / "EscapedCo").exists()
    else:
        assert not target.exists()


@pytest.mark.skipif(
    sys.platform != "darwin",
    reason="requires Darwin atomic rename via renameatx_np to complete the full atomic install sequence",
)
def test_old_inner_rename_race_has_zero_far_side_delta(
    work_tmp: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    dept = work_tmp / "Acme" / "Dept"
    dept.mkdir(parents=True)
    outside = work_tmp.parent / "outside-old-race"
    outside.mkdir()
    (outside / "marker").write_bytes(b"far-side-v1")
    moved = outside / "Dept-moved"
    after_adversary: dict[str, bytes | str] = {}

    def race(_root_fd: int, _stage: str, destination: str) -> None:
        assert destination == "Acme/Dept/Child"
        os.rename(dept, moved)
        after_adversary.update(_tree_bytes(outside))

    monkeypatch.setattr(ensure_mod, "_before_atomic_install_hook", race)
    with pytest.raises(WorkfsError) as exc:
        ensure("Acme", "Dept", "Child", root=work_tmp)

    assert exc.value.code == "atomic_install_failed"
    assert _tree_bytes(outside) == after_adversary
    assert not (moved / "Child").exists()
    assert len(_stages(work_tmp)) == 1


@pytest.mark.skipif(
    sys.platform != "darwin",
    reason="requires Darwin atomic rename via renameatx_np to complete the full atomic install sequence",
)
def test_move_plus_symlink_substitution_fails_closed(
    work_tmp: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    dept = work_tmp / "Acme" / "Dept"
    dept.mkdir(parents=True)
    outside = work_tmp.parent / "outside-symlink-substitution"
    outside.mkdir()
    moved = outside / "Dept-moved"
    after_adversary: dict[str, bytes | str] = {}

    def race(_root_fd: int, _stage: str, _destination: str) -> None:
        os.rename(dept, moved)
        dept.symlink_to(outside, target_is_directory=True)
        after_adversary.update(_tree_bytes(outside))

    monkeypatch.setattr(ensure_mod, "_before_atomic_install_hook", race)
    with pytest.raises(WorkfsError) as exc:
        ensure("Acme", "Dept", "Child", root=work_tmp)

    assert exc.value.code == "atomic_install_failed"
    assert _tree_bytes(outside) == after_adversary
    assert not (outside / "Child").exists()
    assert len(_stages(work_tmp)) == 1


@pytest.mark.skipif(
    sys.platform != "darwin",
    reason="requires Darwin atomic rename via renameatx_np to complete the full atomic install sequence",
)
def test_source_stage_theft_rejects_replacement_inode(
    work_tmp: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    outside = work_tmp.parent / "outside-stage-theft"
    outside.mkdir()
    after_adversary: dict[str, bytes | str] = {}

    def steal(_root_fd: int, stage: str, _destination: str) -> None:
        os.rename(work_tmp / stage, outside / "stolen-stage")
        (work_tmp / stage).symlink_to(outside, target_is_directory=True)
        after_adversary.update(_tree_bytes(outside))

    monkeypatch.setattr(ensure_mod, "_before_atomic_install_hook", steal)
    with pytest.raises(WorkfsError) as exc:
        ensure("Acme", root=work_tmp)

    assert exc.value.code == "containment_lost"
    assert _tree_bytes(outside) == after_adversary
    assert not (outside / "Acme").exists()


@pytest.mark.skipif(
    sys.platform != "darwin",
    reason="requires Darwin atomic rename via renameatx_np to complete the full atomic install sequence",
)
def test_stage_replacement_after_validation_never_writes_outside_or_succeeds(
    work_tmp: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    outside = work_tmp.parent / "outside-stage-post-validation"
    outside.mkdir()
    (outside / "far-side.marker").write_bytes(b"far-side-v1")
    after_adversary: dict[str, bytes | str] = {}

    def replace(_root_fd: int, stage: str, destination: str) -> None:
        assert destination == "Acme"
        os.rename(work_tmp / stage, outside / "stolen-original-stage")
        replacement = work_tmp / stage
        replacement.mkdir()
        (replacement / "attacker.marker").write_bytes(b"replacement-v1")
        after_adversary.update(_tree_bytes(outside))

    monkeypatch.setattr(ensure_mod, "_after_stage_validation_hook", replace)
    with pytest.raises(WorkfsError) as exc:
        ensure("Acme", root=work_tmp)

    assert exc.value.code == "containment_lost"
    assert _tree_bytes(outside) == after_adversary
    assert (work_tmp / "Acme" / "attacker.marker").read_bytes() == b"replacement-v1"


@pytest.mark.skipif(
    sys.platform != "darwin",
    reason="requires Darwin atomic rename via renameatx_np to complete the full atomic install sequence",
)
def test_every_mutating_mkdir_is_directly_root_anchored(
    work_tmp: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    expected = os.stat(work_tmp)
    real_mkdir = ensure_mod.os.mkdir
    observed: list[tuple[int, int, str]] = []

    def checked_mkdir(
        path: str | bytes | os.PathLike[str],
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> None:
        assert dir_fd is not None
        parent = os.fstat(dir_fd)
        observed.append((parent.st_dev, parent.st_ino, os.fspath(path)))
        assert (parent.st_dev, parent.st_ino) == (expected.st_dev, expected.st_ino)
        real_mkdir(path, mode, dir_fd=dir_fd)

    monkeypatch.setattr(ensure_mod.os, "mkdir", checked_mkdir)
    result = ensure("Acme", "Dept", "Child", root=work_tmp)
    assert result.created is True
    assert len(observed) == 3
    assert all(name.startswith(STAGE_PREFIX) for _, _, name in observed)


@pytest.mark.skipif(
    sys.platform != "darwin",
    reason="requires Darwin-specific renameatx_np wrapper and ATOMIC_RENAME_FLAGS to exercise kernel enforcement",
)
def test_wrapper_requires_exact_flags_same_root_and_safe_paths(
    work_tmp: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[tuple[Any, ...]] = []
    assert RENAME_EXCL == 0x04
    assert RENAME_NOFOLLOW_ANY == 0x10
    assert RENAME_RESOLVE_BENEATH == 0x20
    assert ATOMIC_RENAME_FLAGS == 0x34

    def fake(*args: Any) -> int:
        calls.append(args)
        return 0

    monkeypatch.setattr(darwin_backend, "_load_renameatx_np", lambda: fake)
    root_fd = os.open(work_tmp, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    other_fd = os.dup(root_fd)
    try:
        stage = f"{STAGE_PREFIX}abc"
        rename_stage_atomic(root_fd, stage, root_fd, "Acme/Dept", ATOMIC_RENAME_FLAGS)
        assert calls and calls[0][-1] == ATOMIC_RENAME_FLAGS

        for dropped in (
            RENAME_EXCL,
            RENAME_NOFOLLOW_ANY,
            RENAME_RESOLVE_BENEATH,
        ):
            with pytest.raises(WorkfsError):
                rename_stage_atomic(
                    root_fd,
                    stage,
                    root_fd,
                    "Acme/Dept",
                    ATOMIC_RENAME_FLAGS & ~dropped,
                )
        with pytest.raises(WorkfsError):
            rename_stage_atomic(root_fd, stage, other_fd, "Acme", ATOMIC_RENAME_FLAGS)
        for source in ("user-dir", f"{STAGE_PREFIX}bad/name", "/absolute"):
            with pytest.raises(WorkfsError):
                rename_stage_atomic(root_fd, source, root_fd, "Acme", ATOMIC_RENAME_FLAGS)
        for destination in ("/absolute", "../outside", "Acme/../outside", "Acme//Dept"):
            with pytest.raises(WorkfsError):
                rename_stage_atomic(
                    root_fd,
                    stage,
                    root_fd,
                    destination,
                    ATOMIC_RENAME_FLAGS,
                )
    finally:
        os.close(other_fd)
        os.close(root_fd)


@pytest.mark.skipif(
    sys.platform != "darwin",
    reason="requires Darwin atomic rename via renameatx_np to complete the full atomic install sequence",
)
def test_rename_excl_never_overwrites_concurrent_winner(
    work_tmp: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    winner: dict[str, int] = {}

    def create_winner(_root_fd: int, _stage: str, destination: str) -> None:
        assert destination == "Acme"
        target = work_tmp / destination
        target.mkdir()
        (target / "marker").write_bytes(b"winner-v1")
        winner["inode"] = target.stat().st_ino

    monkeypatch.setattr(ensure_mod, "_before_atomic_install_hook", create_winner)
    result = ensure("Acme", root=work_tmp)
    assert result.created is False
    assert (work_tmp / "Acme").stat().st_ino == winner["inode"]
    assert (work_tmp / "Acme" / "marker").read_bytes() == b"winner-v1"
    assert len(_stages(work_tmp)) == 1


@pytest.mark.skipif(
    sys.platform != "darwin",
    reason="requires Darwin atomic rename via renameatx_np to complete the full atomic install sequence",
)
@pytest.mark.parametrize("error_number", [errno.EINVAL, errno.ENOTSUP, errno.EXDEV, 107])
def test_capability_errors_fail_before_destination_write(
    work_tmp: Path,
    monkeypatch: pytest.MonkeyPatch,
    error_number: int,
) -> None:
    def unavailable(*_args: Any, **_kwargs: Any) -> None:
        raise OSError(error_number, os.strerror(error_number))

    monkeypatch.setattr(ensure_mod, "rename_stage_atomic", unavailable)
    with pytest.raises(WorkfsError) as exc:
        ensure("Acme", root=work_tmp)
    assert exc.value.code == "atomic_install_failed"
    assert not (work_tmp / "Acme").exists()
    assert len(_stages(work_tmp)) == 1
    assert _stages(work_tmp)[0].is_dir()


def test_absent_symbol_and_non_darwin_fail_closed(
    work_tmp: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(darwin_backend, "_bind_attempted", False)
    monkeypatch.setattr(darwin_backend, "_renameatx_np", None)
    monkeypatch.setattr(darwin_backend.ctypes, "CDLL", lambda *_a, **_k: object())
    with pytest.raises(WorkfsError) as absent:
        ensure("MissingSymbol", root=work_tmp)
    assert absent.value.code == "workfs_unavailable"
    assert not (work_tmp / "MissingSymbol").exists()

    monkeypatch.setattr(darwin_backend, "_bind_attempted", False)
    monkeypatch.setattr(darwin_backend, "_renameatx_np", None)
    monkeypatch.setattr(darwin_backend.sys, "platform", "linux")
    with pytest.raises(WorkfsError) as platform:
        ensure("WrongPlatform", root=work_tmp)
    assert platform.value.code == "workfs_unavailable"
    assert not (work_tmp / "WrongPlatform").exists()


@pytest.mark.skipif(
    sys.platform != "darwin",
    reason="requires Darwin atomic rename via renameatx_np to test errno handling in kernel-enforced install",
)
def test_wrapper_copies_errno_immediately(work_tmp: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def fail(*_args: Any) -> int:
        ctypes.set_errno(errno.ENOTSUP)
        return -1

    monkeypatch.setattr(darwin_backend, "_load_renameatx_np", lambda: fail)
    root_fd = os.open(work_tmp, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        with pytest.raises(OSError) as exc:
            rename_stage_atomic(
                root_fd,
                f"{STAGE_PREFIX}errno",
                root_fd,
                "Acme",
                ATOMIC_RENAME_FLAGS,
            )
        assert exc.value.errno == errno.ENOTSUP
    finally:
        os.close(root_fd)
