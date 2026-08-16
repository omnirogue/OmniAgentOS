"""Scope tuple → path under the WORK root.

Scope components are opaque path segments supplied by callers (taxonomy,
picker, UI).  This module never hardcodes owner-folder or department names —
those flow from the taxonomy + the real filesystem only (WORKFS-E4).
"""

from __future__ import annotations

import os
import re
import stat as statmod
from pathlib import Path

from omniagentos.workfs.errors import WorkfsPathError
from omniagentos.workfs.root import STAGE_PREFIX, work_root

__all__ = [
    "map_scope",
]

# A single path segment: no separators, no traversal, no absolute form.
# Spaces and most punctuation used in real Work folders are allowed; the
# containment layer is the security boundary, not this charset.
_COMPONENT_RE = re.compile(r"^[^/\x00\\]+$")


def _validate_component(value: str, *, field: str) -> str:
    """Validate one scope segment; reject traversal and absolute forms."""
    if not isinstance(value, str):
        raise WorkfsPathError(
            "invalid_scope_component",
            f"{field} must be a non-empty string",
            detail={"field": field, "value": value},
        )
    name = value.strip()
    if not name:
        raise WorkfsPathError(
            "invalid_scope_component",
            f"{field} must be a non-empty string",
            detail={"field": field, "value": value},
        )
    if name in (".", ".."):
        raise WorkfsPathError(
            "invalid_scope_component",
            f"{field} rejects traversal segments",
            detail={"field": field, "value": value},
        )
    if os.path.isabs(name) or name.startswith("~"):
        raise WorkfsPathError(
            "invalid_scope_component",
            f"{field} rejects absolute paths",
            detail={"field": field, "value": value},
        )
    if "/" in name or "\\" in name or "\x00" in name:
        raise WorkfsPathError(
            "invalid_scope_component",
            f"{field} rejects path separators and NUL",
            detail={"field": field, "value": value},
        )
    if not _COMPONENT_RE.match(name):
        raise WorkfsPathError(
            "invalid_scope_component",
            f"{field} is not a valid path segment",
            detail={"field": field, "value": value},
        )
    if name.casefold().startswith(STAGE_PREFIX.casefold()):
        raise WorkfsPathError(
            "reserved_scope_component",
            f"{field} uses the reserved WorkFS staging namespace",
            detail={"field": field, "value": value},
        )
    return name


def map_scope(
    company: str,
    department: str | None = None,
    subfolder: str | None = None,
    *,
    root: str | os.PathLike[str] | None = None,
) -> Path:
    """Map ``(company → department → subfolder)`` onto a path under the WORK root.

    Only *company* is required.  Empty / None optional levels are omitted.
    The returned path is the lexical join under the root; callers that write
    must still pass it through :func:`require_contained` / :func:`ensure`.
    """
    base = Path(root) if root is not None else work_root()
    try:
        root_stat = os.lstat(base)
    except OSError as exc:
        raise WorkfsPathError(
            "root_unavailable",
            "WORK root must pre-exist as a real directory",
            detail={"root": str(base), "errno": exc.errno, "error": str(exc)},
        ) from exc
    if statmod.S_ISLNK(root_stat.st_mode) or not statmod.S_ISDIR(root_stat.st_mode):
        raise WorkfsPathError(
            "root_not_directory",
            "WORK root must be a real directory, never a symlink",
            detail={"root": str(base)},
        )
    parts: list[str] = [_validate_component(company, field="company")]
    if department is not None and str(department).strip() != "":
        parts.append(_validate_component(str(department), field="department"))
    if subfolder is not None and str(subfolder).strip() != "":
        parts.append(_validate_component(str(subfolder), field="subfolder"))

    # Refuse absolute or parent segments that somehow slipped past (defense).
    for part in parts:
        if part in (".", "..") or os.path.isabs(part) or "/" in part or "\\" in part:
            raise WorkfsPathError(
                "invalid_scope_component",
                "scope component failed secondary validation",
                detail={"component": part},
            )

    candidate = base.joinpath(*parts)
    # The join is lexical over validated single components. Mutators establish
    # filesystem containment from their held root descriptor.
    return candidate
