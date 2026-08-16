"""workfs — create-only WORK-tree convention (D3 / BP-A16).

Maps scope ``(company → department → subfolder)`` onto paths under
``OMNIAGENTOS_WORK_ROOT`` (default ``~/Work``), reads the real directory tree
for the picker, and ensures mapped folders with mkdir-p semantics.

Security charter (binding on every current and future lane):

* logical create-only — never delete, overwrite, or move a pre-existing user entry
* the sole physical rename atomically installs a fresh reserved staging inode
  with Darwin exclusive/no-follow/beneath kernel enforcement
* every write path is realpath-contained under the WORK root
* scope components reject ``..``, absolute forms, and path separators
* owner-folder names are NOT hardcoded here; they flow from taxonomy + FS
"""

from __future__ import annotations

from omniagentos.workfs.containment import path_is_contained, require_contained
from omniagentos.workfs.ensure import EnsureResult, ensure
from omniagentos.workfs.errors import WorkfsError, WorkfsPathError
from omniagentos.workfs.root import WORK_ROOT_ENV, work_root
from omniagentos.workfs.scope import map_scope
from omniagentos.workfs.tree import DEFAULT_MAX_DEPTH, read_tree

__all__ = [
    "DEFAULT_MAX_DEPTH",
    "WORK_ROOT_ENV",
    "EnsureResult",
    "WorkfsError",
    "WorkfsPathError",
    "ensure",
    "map_scope",
    "path_is_contained",
    "read_tree",
    "require_contained",
    "work_root",
]
