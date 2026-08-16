"""Grant a project read/write to a specific Drive subfolder.

Thin, safety-checked wrapper over :meth:`omniagentos.provision.store.ProvisionStore.grant_dir`
that is specific to cloud-drive paths (see :mod:`omniagentos.drive`): it validates
the requested path is a REAL, EXISTING subfolder under a known Drive root before
ever widening a project's scope, and always refuses to grant an entire Drive root.
"""

from __future__ import annotations

import os

from omniagentos.drive import drive_roots, resolve_drive_path
from omniagentos.provision.store import ProvisionError, ProvisionStore


def grant_drive_dir(store: ProvisionStore, project_id: str, path: str) -> list[str]:
    """Grant ``project_id`` read/write access to the Drive subfolder ``path``.

    Validates (in order, failing closed on the first violation):

    1. ``path`` resolves under a known Drive root (:func:`~omniagentos.drive.resolve_drive_path`
       -- rejects anything outside iCloud Drive / Google Drive, and any ``..`` escape);
    2. the resolved path is not itself a whole Drive root (granting an entire
       drive is never allowed -- only real subfolders may be granted);
    3. the resolved path is an existing directory (a typo'd or not-yet-created
       path is rejected rather than silently granted).

    On success, delegates to the existing, idempotent
    :meth:`~omniagentos.provision.store.ProvisionStore.grant_dir` -- the SAME
    mechanism used for any other project directory grant, so a granted Drive
    folder is indistinguishable from a granted repo folder everywhere
    downstream (runner dir-threading, adapter ``--add-dir``, the future
    sandbox/policy guardrail). Returns the project's updated ``root_dirs``.

    Raises :class:`~omniagentos.provision.store.ProvisionError` for every
    rejected path (including an unknown project, raised by the underlying
    ``grant_dir``) so a bad grant can never silently no-op.
    """
    resolved = resolve_drive_path(path)
    if resolved is None:
        raise ProvisionError(
            f"{path!r} is not a real path under a known Drive root (known roots: {drive_roots()!r})"
        )
    if resolved in drive_roots():
        raise ProvisionError(f"refusing to grant an entire Drive root: {resolved!r}")
    if not os.path.isdir(resolved):
        raise ProvisionError(f"{resolved!r} is not an existing folder")
    return store.grant_dir(project_id, resolved)


__all__ = ["grant_drive_dir"]
