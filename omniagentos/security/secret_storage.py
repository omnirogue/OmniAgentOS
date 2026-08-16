"""Permission containment for local secret-store files.

This module deliberately examines only filesystem metadata.  It never opens a
store file, so a refusal cannot expose credential contents.
"""

from __future__ import annotations

import os
import stat

from omniagentos.path_containment import inode_relative_parts_anchored


class PermissionViolation(Exception):
    """Raised when a store violates permission policy (NO value echoed)."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class StoragePermissionGuard:
    """Check secret store permission mode before resolution."""

    def check_store_access(self, store_path: str, env_name: str) -> None:
        """Verify a store file and its containing directory without opening it.

        ``env_name`` is accepted so callers can associate a typed denial with a
        credential identifier.  It is intentionally excluded from this guard's
        exception messages, which contain a reason code only.
        """
        del env_name

        # Keep the lexical parent as the protected store directory, while using
        # real paths for containment so both normal and broken links are handled.
        try:
            requested_path = os.path.abspath(os.path.expanduser(store_path))
            store_root = os.path.dirname(requested_path)
            resolved_root = os.path.realpath(store_root)
            resolved_path = os.path.realpath(requested_path)
        except (OSError, TypeError, ValueError):
            # Suppress the OS exception chain: it may contain the store path.
            raise PermissionViolation("symlink_escape") from None

        # Check containment before existence through the shared inode primitive:
        # a broken link aimed outside the store remains an escape, while a missing
        # in-root leaf can still reach the typed file_missing check below.
        relative_parts = inode_relative_parts_anchored(resolved_path, resolved_root)
        if relative_parts in (None, ()):
            raise PermissionViolation("symlink_escape")

        try:
            # os.stat on the RESOLVED path already covers every absence this
            # check can see: a missing file, and a dangling symlink whose target
            # is gone. The preceding lstat's result was discarded and could not
            # fail where this one succeeds, so it only implied a distinction the
            # code did not make.
            file_stat = os.stat(resolved_path)
        except OSError:
            # Suppress the OS exception chain: it may contain the store path.
            raise PermissionViolation("file_missing") from None

        try:
            directory_stat = os.stat(resolved_root)
        except OSError:
            # Suppress the OS exception chain: it may contain the store path.
            raise PermissionViolation("file_missing") from None

        if stat.S_IMODE(directory_stat.st_mode) & 0o077:
            raise PermissionViolation("dir_too_permissive")
        if stat.S_IMODE(file_stat.st_mode) & 0o177:
            raise PermissionViolation("file_too_permissive")

        # Mode alone is not containment: a 0600 file owned by somebody else is a
        # store this process cannot trust, and 0700 on a directory owned by
        # another account still lets that account replace what is inside it.
        # root-owned paths are accepted because root can read everything anyway,
        # so requiring otherwise would refuse legitimate system installs.
        euid = os.geteuid()
        for entry_stat in (directory_stat, file_stat):
            if entry_stat.st_uid not in (euid, 0):
                raise PermissionViolation("wrong_owner")


_permission_guard: StoragePermissionGuard | None = None


def register_permission_guard(guard: StoragePermissionGuard | None) -> None:
    """Set the global permission guard instance (inject during broker init)."""
    global _permission_guard
    _permission_guard = guard


def get_permission_guard() -> StoragePermissionGuard | None:
    """Retrieve the guard registered for credential resolution, if any."""
    return _permission_guard
