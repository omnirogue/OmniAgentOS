"""WORKFS-E2/E3 — create-only ensure, idempotency, mapping, survival."""

from __future__ import annotations

import importlib
import os
import re
import stat
import sys
from pathlib import Path

import pytest

from omniagentos.workfs import (
    WorkfsError,
    WorkfsPathError,
    ensure,
    map_scope,
    read_tree,
    work_root,
)

pytestmark = pytest.mark.skipif(
    sys.platform != "darwin", reason="workfs requires Darwin kernel atomic operations (renameatx_np)"
)

# Package re-exports `ensure` as a function; load the submodule via importlib.
ensure_mod = importlib.import_module("omniagentos.workfs.ensure")


def assert_work_tree_dirs_only(root: Path) -> None:
    """WORKFS-E2 witness: every entry is a real directory; zero files; zero symlinks.

    Walks with followlinks=False.  Vacuous ``S_ISDIR or S_ISLNK`` is forbidden —
    symlinks must FAIL this witness (M7 mutant proof).
    """
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        for name in filenames:
            pytest.fail(f"workfs created or left a file: {Path(dirpath) / name}")
        for name in dirnames:
            p = Path(dirpath) / name
            assert not os.path.islink(p), f"symlink under WORK root (E2 forbid): {p}"
            st = os.lstat(p)
            assert stat.S_ISDIR(st.st_mode), f"non-directory under WORK root: {p}"
            assert not stat.S_ISLNK(st.st_mode), f"symlink mode under WORK root: {p}"


@pytest.mark.skipif(
    sys.platform != "darwin",
    reason="requires Darwin atomic rename via renameatx_np to complete the full atomic install sequence",
)
def test_mapping_and_mkdir_p_far_side(work_tmp: Path) -> None:
    """WORKFS-E3: scope maps to canonical path; ensure twice is idempotent."""
    mapped = map_scope("AcmeCo", "Operations", "FridayMeeting", root=work_tmp)
    assert mapped == work_tmp / "AcmeCo" / "Operations" / "FridayMeeting"

    first = ensure("AcmeCo", "Operations", "FridayMeeting", root=work_tmp)
    assert first.created is True
    assert Path(first.path).is_dir()
    st1 = os.stat(first.path)

    second = ensure("AcmeCo", "Operations", "FridayMeeting", root=work_tmp)
    assert second.created is False
    assert second.path == first.path
    st2 = os.stat(second.path)
    assert st1.st_ino == st2.st_ino
    assert Path(second.path).is_dir()


def test_absent_scope_zero_writes(work_tmp: Path, work_parent: Path) -> None:
    before = set(os.listdir(work_tmp))
    with pytest.raises(WorkfsPathError) as ei:
        ensure(None, root=work_tmp)
    assert ei.value.code == "scope_required"
    with pytest.raises(WorkfsPathError):
        ensure("", root=work_tmp)
    with pytest.raises(WorkfsPathError):
        ensure("   ", root=work_tmp)
    assert set(os.listdir(work_tmp)) == before


@pytest.mark.skipif(
    sys.platform != "darwin",
    reason="requires Darwin atomic rename via renameatx_np to complete the full atomic install sequence",
)
def test_idempotent_double_ensure(work_tmp: Path) -> None:
    a = ensure("Co", "Sales", root=work_tmp)
    b = ensure("Co", "Sales", root=work_tmp)
    assert a.created is True
    assert b.created is False
    assert a.path == b.path
    assert Path(a.path).is_dir()


@pytest.mark.skipif(
    sys.platform != "darwin",
    reason="requires Darwin atomic rename via renameatx_np to complete the full atomic install sequence",
)
def test_seeded_file_survives_every_public_function(work_tmp: Path) -> None:
    """WORKFS-E2: the narrow stage install never touches a user seed file."""
    seed_dir = work_tmp / "SeedCo" / "Sales"
    seed_dir.mkdir(parents=True)
    seed = seed_dir / "IMPORTANT.seed"
    payload = b"seed-bytes-must-survive-v1\n"
    seed.write_bytes(payload)

    # Exercise every public mutator/reader against the seed's parent scope.
    ensure("SeedCo", "Sales", root=work_tmp)
    ensure("SeedCo", "Sales", "NewSub", root=work_tmp)
    ensure("SeedCo", "Operations", root=work_tmp)
    read_tree(root=work_tmp)
    map_scope("SeedCo", "Sales", root=work_tmp)
    _ = work_root()

    assert seed.is_file(), "seeded file was deleted or moved"
    assert seed.read_bytes() == payload, "seeded file bytes changed"
    # Still a regular file at the original path (not replaced by a dir/symlink).
    st = os.lstat(seed)
    assert stat.S_ISREG(st.st_mode)


@pytest.mark.skipif(
    sys.platform != "darwin",
    reason="requires Darwin atomic rename via renameatx_np to complete the full atomic install sequence",
)
def test_post_run_walk_only_directories(work_tmp: Path) -> None:
    """WORKFS-E2 post-run witness: every entry is a real directory (no files/symlinks)."""
    ensure("OnlyDirs", "DeptA", "Sub1", root=work_tmp)
    ensure("OnlyDirs", "DeptB", root=work_tmp)
    read_tree(root=work_tmp)
    assert_work_tree_dirs_only(work_tmp)


@pytest.mark.skipif(
    sys.platform != "darwin",
    reason="requires Darwin atomic rename via renameatx_np to complete the full atomic install sequence",
)
def test_e2_witness_fails_on_planted_symlink(work_tmp: Path) -> None:
    """M7-equivalent: witness must FAIL when a symlink is planted under WORK.

    The prior decoration (``S_ISDIR or S_ISLNK``) stayed green under this
    plant; the rewritten witness must raise AssertionError.
    """
    ensure("OnlyDirs", "DeptA", root=work_tmp)
    # grep-evading plant equivalent: getattr(os, "sym"+"link")
    plant = getattr(os, "sym" + "link")
    plant("/etc", work_tmp / "OnlyDirs" / "evil-link")
    with pytest.raises(AssertionError, match="symlink"):
        assert_work_tree_dirs_only(work_tmp)


def test_create_only_grep_gate() -> None:
    """WORKFS-E2 [MECH]: no destructive/file-write API and one honest rename rail.

    ``os.open`` (dirfd / O_NOFOLLOW) is allowed; bare ``open(`` for file I/O is not.
    ``renameatx_np`` is permitted only in the Darwin wrapper, whose exact mask
    and argument constraints have dedicated mutation tests.
    """
    root = Path(__file__).resolve().parents[2] / "omniagentos" / "workfs"
    assert root.is_dir()
    forbidden = re.compile(
        r"os\.remove|os\.rename|os\.unlink|os\.rmdir|os\.symlink|os\.link|"
        r"shutil\.rmtree|shutil\.move|shutil\.copy|"
        r"write_text|write_bytes|(?<!os\.)\bopen\("
    )
    offenders: list[str] = []
    for path in sorted(root.rglob("*.py")):
        text = path.read_text(encoding="utf-8")
        for i, line in enumerate(text.splitlines(), 1):
            if line.lstrip().startswith("#"):
                continue
            if forbidden.search(line):
                offenders.append(f"{path.relative_to(root.parent.parent)}:{i}:{line.strip()}")
    assert offenders == [], "create-only charter violated:\n" + "\n".join(offenders)

    rename_sources = {
        path.name
        for path in sorted(root.rglob("*.py"))
        if "renameatx_np" in path.read_text(encoding="utf-8")
    }
    assert rename_sources == {"darwin.py", "ensure.py"}, (
        "physical rename must remain limited to the Darwin wrapper and its "
        f"single ensure caller, got {rename_sources}"
    )
    backend = (root / "darwin.py").read_text(encoding="utf-8")
    assert (
        "ATOMIC_RENAME_FLAGS = RENAME_EXCL | RENAME_NOFOLLOW_ANY | RENAME_RESOLVE_BENEATH"
    ) in backend


def test_delete_added_counterfeit_target(work_tmp: Path) -> None:
    """must_fail node for cf-workfs-delete-added (grep + survival).

    Production stays green: no remove path, seed survives.  The counterfeit
    introduces a remove so either the grep gate or survival fails.
    """
    test_create_only_grep_gate()
    seed_dir = work_tmp / "CfCo"
    seed_dir.mkdir()
    seed = seed_dir / "keep-me.bin"
    seed.write_bytes(b"alive")
    ensure("CfCo", "X", root=work_tmp)
    assert seed.is_file() and seed.read_bytes() == b"alive"


@pytest.mark.skipif(
    sys.platform != "darwin",
    reason="requires Darwin atomic rename via renameatx_np to complete the full atomic install sequence",
)
def test_dangling_symlink_refused_at_company_level(work_tmp: Path) -> None:
    """Defect 3: dangling symlink at company must raise WorkfsError (not raw OSError)."""
    (work_tmp / "DangleCo").symlink_to(work_tmp / "no-such-company-target")
    with pytest.raises(WorkfsError) as ei:
        ensure("DangleCo", "Dept", root=work_tmp)
    assert not isinstance(ei.value, OSError) or isinstance(ei.value, WorkfsError)
    assert ei.value.code in {
        "target_not_directory",
        "ensure_failed",
        "path_outside_root",
        "containment_lost",
    }
    assert not (work_tmp / "no-such-company-target").exists()


@pytest.mark.skipif(
    sys.platform != "darwin",
    reason="requires Darwin atomic rename via renameatx_np to complete the full atomic install sequence",
)
def test_dangling_symlink_refused_at_department_level(work_tmp: Path) -> None:
    """Defect 3: dangling symlink at department level."""
    (work_tmp / "RealCo").mkdir()
    (work_tmp / "RealCo" / "DangleDept").symlink_to(work_tmp / "no-such-dept-target")
    with pytest.raises(WorkfsError) as ei:
        ensure("RealCo", "DangleDept", "Sub", root=work_tmp)
    assert ei.value.code in {
        "target_not_directory",
        "ensure_failed",
        "path_outside_root",
        "containment_lost",
    }


@pytest.mark.skipif(
    sys.platform != "darwin",
    reason="requires Darwin atomic rename via renameatx_np to complete the full atomic install sequence",
)
def test_dangling_symlink_refused_at_subfolder_level(work_tmp: Path) -> None:
    """Defect 3: dangling symlink at subfolder level."""
    dept = work_tmp / "RealCo" / "RealDept"
    dept.mkdir(parents=True)
    (dept / "DangleSub").symlink_to(work_tmp / "no-such-sub-target")
    with pytest.raises(WorkfsError) as ei:
        ensure("RealCo", "RealDept", "DangleSub", root=work_tmp)
    assert ei.value.code in {
        "target_not_directory",
        "ensure_failed",
        "path_outside_root",
        "containment_lost",
    }


@pytest.mark.skipif(
    sys.platform != "darwin",
    reason="requires Darwin atomic rename via renameatx_np to complete the full atomic install sequence",
)
def test_oserror_file_exists_wrapped_as_workfs_error(
    work_tmp: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Defect 2: FileExistsError from create path is WorkfsError (not 500)."""

    def boom(*_a: object, **_k: object) -> None:
        raise FileExistsError(17, "File exists")

    monkeypatch.setattr(ensure_mod.os, "mkdir", boom)
    with pytest.raises(WorkfsError) as ei:
        ensure("WrapCo", "Dept", root=work_tmp)
    assert ei.value.code in {
        "containment_lost",
        "ensure_failed",
        "target_not_directory",
    }
    assert "File exists" in ei.value.message or ei.value.detail is not None


@pytest.mark.skipif(
    sys.platform != "darwin",
    reason="requires Darwin atomic rename via renameatx_np to complete the full atomic install sequence",
)
def test_oserror_file_not_found_wrapped_as_workfs_error(
    work_tmp: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Defect 2: FileNotFoundError from create path is WorkfsError (not 500)."""
    real_open = ensure_mod.os.open
    calls = {"n": 0}

    def flaky_open(*a: object, **k: object) -> int:
        calls["n"] += 1
        # First open is the WORK root — allow it; subsequent component opens fail.
        if calls["n"] == 1:
            return real_open(*a, **k)  # type: ignore[arg-type]
        raise FileNotFoundError(2, "No such file or directory")

    monkeypatch.setattr(ensure_mod.os, "open", flaky_open)
    with pytest.raises(WorkfsError) as ei:
        ensure("MissingRaceCo", root=work_tmp)
    assert ei.value.code in {
        "containment_lost",
        "ensure_failed",
        "target_not_directory",
    }


@pytest.mark.skipif(
    sys.platform != "darwin",
    reason="requires Darwin atomic rename via renameatx_np to complete the full atomic install sequence",
)
def test_toctou_ancestor_swap_to_symlink_refused(
    work_tmp: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Defect 4: concurrent rename swap after resolve must not create outside root.

    Deterministic: monkeypatch hook between resolve and create (no real race).
    """
    co = work_tmp / "RaceCo"
    co.mkdir()
    outside = work_tmp.parent / "outside-race"
    outside.mkdir()
    before_outside = set(os.listdir(outside))

    def swap() -> None:
        # Concurrent rename: replace resolved ancestor with symlink out of root.
        os.rename(co, work_tmp / "RaceCo-moved")
        os.symlink(outside, co)

    monkeypatch.setattr(ensure_mod, "_after_resolve_hook", swap)

    with pytest.raises(WorkfsError) as ei:
        ensure("RaceCo", "Planted", root=work_tmp)

    assert ei.value.code in {
        "target_not_directory",
        "ensure_failed",
        "containment_lost",
        "path_outside_root",
    }
    assert set(os.listdir(outside)) == before_outside
    assert not (outside / "Planted").exists()
    # Nothing planted via the swapped symlink.
    assert not any(Path(outside).rglob("Planted"))


def _tree_bytes(root: Path) -> dict[str, bytes | str]:
    """Byte-for-byte far-side witness, including symlink targets."""
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


@pytest.mark.skipif(
    sys.platform != "darwin",
    reason="requires Darwin atomic rename via renameatx_np to complete the full atomic install sequence",
)
def test_post_open_child_rename_out_refused_before_descendant_write(
    work_tmp: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """F1 regression: an opened child moved outside cannot become mkdirat parent.

    The race fires after the kernel returns the real ``Work/Acme/Dept``
    descriptor, then moves its still-open ``Acme`` ancestor outside.  The
    immediate Dept link remains valid, so only full-chain validation before
    the Child write catches it.  The snapshot captured immediately after the
    adversarial rename must remain byte-for-byte equal after ensure refuses.
    """
    acme = work_tmp / "Acme"
    dept = acme / "Dept"
    dept.mkdir(parents=True)
    (dept / "inside.marker").write_bytes(b"inside-v1")
    outside = work_tmp.parent / "outside-post-open"
    outside.mkdir()
    (outside / "far-side.marker").write_bytes(b"far-side-v1")
    moved = outside / "Acme-moved"
    after_adversary: dict[str, bytes | str] = {}
    real_open = ensure_mod.os.open
    raced = False

    def racing_open(
        path: str | bytes | os.PathLike[str],
        flags: int,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> int:
        nonlocal raced
        if dir_fd is None:
            fd = real_open(path, flags, mode)
        else:
            fd = real_open(path, flags, mode, dir_fd=dir_fd)
        if not raced and path == "Dept" and dir_fd is not None:
            raced = True
            os.rename(acme, moved)
            after_adversary.update(_tree_bytes(outside))
        return fd

    monkeypatch.setattr(ensure_mod.os, "open", racing_open)
    with pytest.raises(WorkfsError) as exc:
        ensure("Acme", "Dept", "Child", root=work_tmp)

    assert raced is True
    assert exc.value.code in {"atomic_install_failed", "containment_lost"}
    assert after_adversary
    assert _tree_bytes(outside) == after_adversary
    assert not (moved / "Dept" / "Child").exists()


@pytest.mark.skipif(
    sys.platform != "darwin",
    reason="requires Darwin atomic rename via renameatx_np to complete the full atomic install sequence",
)
def test_root_rename_symlink_swap_stays_on_original_root_inode(
    work_tmp: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A swapped root pathname cannot redirect create through a new symlink.

    This is the root-FD regression: reopening ``realpath(base)`` after the
    hook would create ``RootSwap/Child`` in ``outside``.  The held descriptor
    instead creates only under the renamed original inode and reports the
    changed configured root as a containment failure.
    """
    outside = work_tmp.parent / "outside-root-symlink"
    outside.mkdir()
    marker = outside / "do-not-touch.bin"
    marker.write_bytes(b"far-side-v1")
    before_outside = _tree_bytes(outside)
    original_root = work_tmp.parent / "Work-original-inode"

    def swap_root() -> None:
        os.rename(work_tmp, original_root)
        os.symlink(outside, work_tmp, target_is_directory=True)

    monkeypatch.setattr(ensure_mod, "_after_resolve_hook", swap_root)
    with pytest.raises(WorkfsError) as exc:
        ensure("RootSwap", "Child", root=work_tmp)

    assert exc.value.code == "containment_lost"
    assert _tree_bytes(outside) == before_outside
    assert (original_root / "RootSwap" / "Child").is_dir()
    assert not (outside / "RootSwap").exists()


@pytest.mark.skipif(
    sys.platform != "darwin",
    reason="requires Darwin atomic rename via renameatx_np to complete the full atomic install sequence",
)
def test_root_recreate_swap_stays_on_original_root_inode(
    work_tmp: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Replacing root with a new directory cannot receive the ensure write."""
    outside = work_tmp.parent / "outside-root-recreate"
    outside.mkdir()
    marker = outside / "do-not-touch.bin"
    marker.write_bytes(b"far-side-v1")
    before_outside = _tree_bytes(outside)

    def replace_root() -> None:
        os.rmdir(work_tmp)
        work_tmp.mkdir()

    monkeypatch.setattr(ensure_mod, "_after_resolve_hook", replace_root)
    with pytest.raises(WorkfsError) as exc:
        ensure("Recreated", "Child", root=work_tmp)

    assert exc.value.code == "containment_lost"
    assert _tree_bytes(outside) == before_outside
    assert list(work_tmp.iterdir()) == []
