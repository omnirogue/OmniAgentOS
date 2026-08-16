"""WORKFS-E1 — containment far side: traversal / absolute / symlink escapes."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from omniagentos.workfs import (
    WorkfsPathError,
    ensure,
    map_scope,
    path_is_contained,
    require_contained,
)
from omniagentos.workfs.containment import _resolve_final_path
from omniagentos.workfs.root import STAGE_PREFIX

pytestmark = pytest.mark.skipif(
    sys.platform != "darwin", reason="workfs requires Darwin kernel atomic operations (renameatx_np)"
)


def _walk_names(root: Path) -> set[str]:
    """Relative paths under *root* (files + dirs), stable witness set."""
    out: set[str] = set()
    for dirpath, dirnames, filenames in os.walk(root):
        rel_dir = os.path.relpath(dirpath, root)
        if rel_dir == ".":
            rel_dir = ""
        for name in dirnames:
            key = name if rel_dir == "" else f"{rel_dir}/{name}"
            out.add(key)
        for name in filenames:
            key = name if rel_dir == "" else f"{rel_dir}/{name}"
            out.add(key)
    # Also capture top-level entries that os.walk might miss if empty? it doesn't.
    for name in os.listdir(root):
        out.add(name)
    return out


def test_traversal_refused_and_nothing_created_outside_root(
    work_tmp: Path, work_parent: Path
) -> None:
    """GLA-1 decisive (Case B / WORKFS-E1): parent-walk before == after."""
    before = _walk_names(work_parent)

    with pytest.raises(WorkfsPathError):
        map_scope("..", root=work_tmp)
    with pytest.raises(WorkfsPathError):
        map_scope("Company", "..", root=work_tmp)
    with pytest.raises(WorkfsPathError):
        map_scope("Company", "Dept", "..", root=work_tmp)
    with pytest.raises(WorkfsPathError):
        ensure("..", root=work_tmp)
    with pytest.raises(WorkfsPathError):
        ensure("/etc", root=work_tmp)
    with pytest.raises(WorkfsPathError):
        ensure("Company", "/absolute", root=work_tmp)

    after = _walk_names(work_parent)
    assert after == before, (
        "os-level far-side witness: parent of WORK root must be unchanged "
        f"after refused traversal; delta={after ^ before}"
    )


def test_absolute_scope_components_refused(work_tmp: Path, work_parent: Path) -> None:
    before = _walk_names(work_parent)
    for company in ("/etc", "/tmp", "C:\\Windows" if os.name == "nt" else "/var"):
        with pytest.raises(WorkfsPathError):
            ensure(company, root=work_tmp)
    after = _walk_names(work_parent)
    assert after == before


def test_reserved_stage_prefix_refused_as_scope(work_tmp: Path) -> None:
    for value in (f"{STAGE_PREFIX}chosen", f"{STAGE_PREFIX.upper()}CASEFOLD"):
        with pytest.raises(WorkfsPathError) as exc:
            ensure(value, root=work_tmp)
        assert exc.value.code == "reserved_scope_component"


def test_symlink_escape_refused_with_far_side_witness(work_tmp: Path, work_parent: Path) -> None:
    outside = work_parent / "outside-secret"
    outside.mkdir()
    marker = outside / "untouched.txt"
    marker.write_bytes(b"far-side-marker-v1")

    escape = work_tmp / "escape-link"
    escape.symlink_to(outside, target_is_directory=True)

    before_parent = _walk_names(work_parent)
    before_marker = marker.read_bytes()

    # Lexical path under root, realpath outside — must refuse.
    assert path_is_contained(escape / "new-folder", work_tmp) is False
    with pytest.raises(WorkfsPathError) as ei:
        require_contained(escape / "new-folder", work_tmp)
    assert ei.value.code == "path_outside_root"

    # ensure via a component that is the symlink name would map under root
    # lexically then fail containment on the resolved final path.
    with pytest.raises(WorkfsPathError):
        ensure("escape-link", "new-folder", root=work_tmp)

    assert marker.read_bytes() == before_marker
    assert marker.is_file()
    # Nothing new outside the WORK root (marker dir may already be in before).
    after_parent = _walk_names(work_parent)
    # escape-link is inside Work; outside-secret was pre-existing.
    new_outside = {
        p for p in (after_parent - before_parent) if not p.startswith("Work/") and p != "Work"
    }
    assert new_outside == set(), f"far-side polluted: {new_outside}"


def test_sibling_prefix_escape_refused(work_tmp: Path) -> None:
    """``<root>-evil`` must not pass a realpath+boundary containment check.

    This is the concrete property that ``cf-workfs-startswith-containment``
    deletes — a naive ``startswith(root)`` admits the sibling.
    """
    evil = work_tmp.parent / f"{work_tmp.name}-evil"
    evil.mkdir()
    target = evil / "planted"
    assert path_is_contained(target, work_tmp) is False
    with pytest.raises(WorkfsPathError):
        require_contained(target, work_tmp)
    # The decisive counterfeit target: string-prefix alone would accept this.
    root_s = os.path.realpath(work_tmp)
    evil_s = os.path.realpath(target)
    assert evil_s.startswith(root_s)  # naive (no boundary) would pass
    assert not evil_s.startswith(root_s + os.sep)  # correct boundary fails
    assert path_is_contained(target, work_tmp) is False


def test_string_prefix_counterfeit_fails_loudly(work_tmp: Path) -> None:
    """must_fail node for cf-workfs-startswith-containment.

    When containment is correctly implemented, a sibling-prefix path is
    refused (returns False / raises).  The counterfeit patch replaces the
    check with bare ``startswith`` so this assertion fails loudly.
    """
    evil = work_tmp.parent / f"{work_tmp.name}-evil"
    evil.mkdir(exist_ok=True)
    planted = evil / "planted-dir"
    # Production: must be outside.
    assert path_is_contained(planted, work_tmp) is False, (
        "string-prefix containment counterfeit: sibling path must be refused"
    )
    with pytest.raises(WorkfsPathError):
        require_contained(planted, work_tmp)


def test_resolve_final_path_follows_symlink_then_joins_missing(work_tmp: Path) -> None:
    inner = work_tmp / "inner"
    inner.mkdir()
    # non-existent leaf under a real dir
    leaf = inner / "missing" / "leaf"
    resolved = _resolve_final_path(leaf)
    assert resolved.endswith(os.path.join("inner", "missing", "leaf"))
    assert path_is_contained(leaf, work_tmp) is True


def test_dangling_symlink_not_treated_as_missing_component(work_tmp: Path) -> None:
    """Defect 3: lexists — dangling link is realpath'd, not skipped as missing.

    With exists()-based walking, a dangling link was classified as a missing
    component and rejoined as 'inside root'.  lexists stops on the link so
    realpath of an out-of-root target fails containment.
    """
    outside_target = work_tmp.parent / "dangling-outside-target"
    # Do NOT create outside_target — dangling.
    link = work_tmp / "dangle-out"
    link.symlink_to(outside_target)
    # Path under the dangling link must not report as contained via missing-join.
    candidate = link / "child"
    # realpath of dangling goes to outside_target/child → outside root
    assert path_is_contained(candidate, work_tmp) is False
    with pytest.raises(WorkfsPathError) as ei:
        require_contained(candidate, work_tmp)
    assert ei.value.code == "path_outside_root"


def test_dangling_symlink_at_each_scope_level_not_inside(
    work_tmp: Path,
) -> None:
    """Defect 3: dangling at company / department / subfolder fails containment."""
    # company
    (work_tmp / "DangleCo").symlink_to(work_tmp.parent / "no-co")
    assert path_is_contained(work_tmp / "DangleCo" / "X", work_tmp) is False

    # department
    (work_tmp / "RealCo").mkdir()
    (work_tmp / "RealCo" / "DangleDept").symlink_to(work_tmp.parent / "no-dept")
    assert path_is_contained(work_tmp / "RealCo" / "DangleDept" / "Y", work_tmp) is False

    # subfolder
    (work_tmp / "RealCo" / "RealDept").mkdir()
    (work_tmp / "RealCo" / "RealDept" / "DangleSub").symlink_to(work_tmp.parent / "no-sub")
    assert path_is_contained(work_tmp / "RealCo" / "RealDept" / "DangleSub", work_tmp) is False
