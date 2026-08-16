"""Filesystem-true path containment shared by every security consumer.

Containment is decided by inode ancestry, never by string prefixes.  Both
inputs must be absolute; the candidate is re-canonicalized internally so a
caller cannot bypass the check with ``..`` or a symlink spelling.  A suffix
that does not exist yet is supported by resolving its nearest lexically
existing ancestor and carrying the missing components through the result.

This module is deliberately bottom-layer and stdlib-only so API routes, scope
code, and process launchers can all consult the exact same primitive.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Sequence

__all__ = [
    "inode_path_is_within_anchored",
    "inode_paths_equal",
    "inode_relative_parts",
    "inode_relative_parts_anchored",
]





def _nearest_existing(path: str | os.PathLike[str]) -> str | None:
    try:
        node = os.path.abspath(os.path.expanduser(os.fspath(path)))
    except (OSError, TypeError, ValueError):
        return None
    while True:
        try:
            os.lstat(node)
            return node
        except (FileNotFoundError, NotADirectoryError):
            pass
        except (OSError, ValueError):
            return None
        parent = os.path.dirname(node)
        if parent == node:
            return None
        node = parent


def inode_relative_parts(
    candidate: str | os.PathLike[str],
    root: str | os.PathLike[str],
) -> tuple[str, ...] | None:
    """Return canonical ``candidate`` parts below ``root`` by inode.

    ``()`` means both paths identify the same inode.  ``None`` means
    containment could not be proved.  Relative paths, an unstatable root, a
    candidate whose nearest existing ancestor cannot be found, and any
    filesystem error all fail closed.

    The candidate is always expanded and canonicalized here.  Callers may
    still impose a stricter policy that *refuses* non-canonical spellings, but
    forgetting to pre-resolve can never turn an outside path into an admitted
    one.
    """
    decision, parts = _inode_relative_parts_decision(candidate, root)
    return parts if decision is True else None


def _inode_relative_parts_decision(
    candidate: str | os.PathLike[str],
    root: str | os.PathLike[str],
) -> tuple[bool | None, tuple[str, ...] | None]:
    """Return positive containment, positive exclusion, or unknown."""
    try:
        candidate_text = os.path.expanduser(os.fspath(candidate))
        root_text = os.path.expanduser(os.fspath(root))
    except (OSError, TypeError, ValueError):
        return None, None
    if not os.path.isabs(candidate_text) or not os.path.isabs(root_text):
        return None, None

    try:
        root_stat = os.stat(root_text)
    except (OSError, ValueError):
        return None, None

    node = os.path.abspath(candidate_text)
    missing_parts: list[str] = []
    while True:
        try:
            os.lstat(node)
            break
        except (FileNotFoundError, NotADirectoryError):
            pass
        except (OSError, ValueError):
            return None, None
        parent = os.path.dirname(node)
        if parent == node:
            return None, None
        missing_parts.append(os.path.basename(node))
        node = parent

    try:
        node = os.path.realpath(node)
    except (OSError, ValueError):
        return None, None

    canonical_parts: list[str] = []
    while True:
        try:
            node_stat = os.stat(node)
        except (OSError, ValueError):
            return None, None
        if os.path.samestat(node_stat, root_stat):
            return (
                True,
                tuple(reversed(canonical_parts)) + tuple(reversed(missing_parts)),
            )
        parent = os.path.dirname(node)
        if parent == node:
            return False, None
        canonical_parts.append(os.path.basename(node))
        node = parent


def inode_paths_equal(
    candidate: str | os.PathLike[str],
    reference: str | os.PathLike[str],
) -> bool | None:
    """Compare path identity, including an absent leaf, without string paths.

    The nearest existing ancestor of ``reference`` is identified by inode via
    :func:`inode_relative_parts`; only the **exact** remaining components are
    then compared (case-sensitive, no ``normcase`` / lower-fold).  Existing
    path identity still collapses through ``os.path.samestat``; only the
    unstatable suffix is compared literally.  This is the general form of the
    session-token safety pattern.  ``None`` means either path could not be
    anchored and identity is unknown.
    """
    anchor = _nearest_existing(reference)
    if anchor is None:
        return None
    reference_decision, reference_parts = _inode_relative_parts_decision(reference, anchor)
    if reference_decision is not True or reference_parts is None:
        return None
    candidate_decision, candidate_parts = _inode_relative_parts_decision(candidate, anchor)
    if candidate_decision is None:
        return None
    if candidate_decision is False or candidate_parts is None:
        return False
    return candidate_parts == reference_parts


def _inode_relative_parts_anchored_decision(
    candidate: str | os.PathLike[str],
    root: str | os.PathLike[str],
) -> tuple[bool | None, tuple[str, ...] | None]:
    anchor = _nearest_existing(root)
    if anchor is None:
        return None, None
    root_decision, root_parts = _inode_relative_parts_decision(root, anchor)
    candidate_decision, candidate_parts = _inode_relative_parts_decision(candidate, anchor)
    if root_decision is not True or root_parts is None or candidate_decision is None:
        return None, None
    if candidate_decision is False or candidate_parts is None:
        return False, None
    # Exact remaining components only — never case-fold absent suffixes.
    if candidate_parts[: len(root_parts)] != root_parts:
        return False, None
    return True, candidate_parts[len(root_parts) :]


def inode_path_is_within_anchored(
    candidate: str | os.PathLike[str],
    root: str | os.PathLike[str],
) -> bool | None:
    """Decide containment under a root that may not exist yet.

    ``True`` means containment was positively proved, ``False`` means the
    candidate was positively proved outside, and ``None`` means filesystem
    identity could not be determined.
    """
    decision, _parts = _inode_relative_parts_anchored_decision(candidate, root)
    return decision


def inode_relative_parts_anchored(
    candidate: str | os.PathLike[str],
    root: str | os.PathLike[str],
) -> tuple[str, ...] | None:
    """Return candidate parts below a root that may not exist yet.

    Both paths are first expressed relative to ``root``'s nearest existing
    ancestor, whose identity is proved with :func:`inode_relative_parts`.
    Only then are the root's exact missing components removed.
    """
    decision, parts = _inode_relative_parts_anchored_decision(candidate, root)
    return parts if decision is True else None


def _main(argv: Sequence[str]) -> int:
    """Shell-facing adapter used by the launcher; emit nothing, return truth."""
    if len(argv) != 2:
        return 2
    return 0 if inode_relative_parts(argv[0], argv[1]) is not None else 1


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv[1:]))
