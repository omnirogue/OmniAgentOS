"""Read the actual WORK directory tree from a held no-follow root descriptor."""

from __future__ import annotations

import os
import stat as statmod
from pathlib import Path
from typing import Any

from omniagentos.workfs.errors import WorkfsError
from omniagentos.workfs.root import STAGE_PREFIX, work_root

__all__ = ["DEFAULT_MAX_DEPTH", "read_tree"]

DEFAULT_MAX_DEPTH = 3


def read_tree(
    *,
    root: str | os.PathLike[str] | None = None,
    max_depth: int = DEFAULT_MAX_DEPTH,
) -> dict[str, Any]:
    """Return a no-follow directory listing bounded to the held root inode."""
    if max_depth < 0 or max_depth > DEFAULT_MAX_DEPTH:
        raise WorkfsError(
            "invalid_depth",
            f"max_depth must be in 0..{DEFAULT_MAX_DEPTH}",
            detail={"max_depth": max_depth},
        )
    base = Path(root) if root is not None else work_root()
    dir_flags, nofollow = _directory_open_flags()
    try:
        root_fd = os.open(os.fspath(base), dir_flags | nofollow)
    except OSError as exc:
        raise WorkfsError(
            "root_not_directory",
            "WORK root must be a pre-existing real directory",
            detail={"root": str(base), "errno": exc.errno, "error": str(exc)},
        ) from exc
    try:
        root_stat = os.fstat(root_fd)
        entries = _scan_fd(
            root_fd,
            relative="",
            depth=0,
            max_depth=max_depth,
            dir_flags=dir_flags,
            nofollow=nofollow,
        )
        _require_same_root_path(base, root_stat)
    finally:
        os.close(root_fd)
    return {
        "root": str(base),
        "max_depth": max_depth,
        "entries": entries,
    }


def _scan_fd(
    directory_fd: int,
    *,
    relative: str,
    depth: int,
    max_depth: int,
    dir_flags: int,
    nofollow: int,
) -> list[dict[str, Any]]:
    if depth >= max_depth:
        return []
    try:
        names = sorted(os.listdir(directory_fd), key=lambda name: name.lower())
    except OSError:
        return []

    out: list[dict[str, Any]] = []
    for name in names:
        if name in {".", ".."} or name.casefold().startswith(STAGE_PREFIX.casefold()):
            continue
        try:
            entry_stat = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        except OSError:
            continue
        if statmod.S_ISLNK(entry_stat.st_mode) or not statmod.S_ISDIR(entry_stat.st_mode):
            continue
        try:
            child_fd = os.open(
                name,
                dir_flags | nofollow,
                dir_fd=directory_fd,
            )
        except OSError:
            continue
        try:
            held_stat = os.fstat(child_fd)
            if held_stat.st_dev != entry_stat.st_dev or held_stat.st_ino != entry_stat.st_ino:
                continue
            rel = name if relative == "" else f"{relative}/{name}"
            children: list[dict[str, Any]] = []
            if depth + 1 < max_depth:
                children = _scan_fd(
                    child_fd,
                    relative=rel,
                    depth=depth + 1,
                    max_depth=max_depth,
                    dir_flags=dir_flags,
                    nofollow=nofollow,
                )
            out.append({"name": name, "path": rel, "children": children})
        finally:
            os.close(child_fd)
    return out


def _directory_open_flags() -> tuple[int, int]:
    if not hasattr(os, "O_DIRECTORY") or not hasattr(os, "O_NOFOLLOW"):
        raise WorkfsError(
            "workfs_unavailable",
            "platform lacks required no-follow directory open protections",
        )
    return os.O_RDONLY | os.O_DIRECTORY, os.O_NOFOLLOW


def _require_same_root_path(base: Path, expected: os.stat_result) -> None:
    try:
        current = os.lstat(base)
    except OSError as exc:
        raise WorkfsError(
            "containment_lost",
            "WORK root disappeared while reading its tree",
            detail={"root": str(base), "errno": exc.errno, "error": str(exc)},
        ) from exc
    if (
        not statmod.S_ISDIR(current.st_mode)
        or current.st_dev != expected.st_dev
        or current.st_ino != expected.st_ino
    ):
        raise WorkfsError(
            "containment_lost",
            "WORK root changed while reading its tree",
            detail={"root": str(base)},
        )
