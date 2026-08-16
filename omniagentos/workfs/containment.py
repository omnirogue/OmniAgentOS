"""Inode-backed containment for the WorkFS WORK root."""

from __future__ import annotations

import os
from pathlib import Path

from omniagentos.path_containment import inode_relative_parts
from omniagentos.workfs.errors import WorkfsPathError

__all__ = [
    "path_is_contained",
    "require_contained",
]


def _resolve_final_path(candidate: str | os.PathLike[str]) -> str:
    """Return the realpath-resolved form of *candidate*, allowing a missing suffix."""
    try:
        return os.path.realpath(os.path.abspath(os.path.expanduser(os.fspath(candidate))))
    except (OSError, TypeError, ValueError) as exc:
        raise WorkfsPathError(
            "path_unresolvable",
            f"cannot resolve path: {candidate!r}",
            detail={"candidate": str(candidate), "error": str(exc)},
        ) from exc


def path_is_contained(
    candidate: str | os.PathLike[str],
    root: str | os.PathLike[str],
) -> bool:
    """Return True iff *candidate* is inside *root* by inode ancestry."""
    return inode_relative_parts(candidate, root) is not None


def require_contained(
    candidate: str | os.PathLike[str],
    root: str | os.PathLike[str],
) -> Path:
    """Resolve *candidate* and refuse unless it is contained under *root*."""
    if not path_is_contained(candidate, root):
        raise WorkfsPathError(
            "path_outside_root",
            "resolved path escapes the WORK root",
            detail={
                "candidate": str(candidate),
                "root": str(root),
            },
        )
    return Path(_resolve_final_path(candidate))
