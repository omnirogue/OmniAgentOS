"""Kernel-atomic logical mkdir-p beneath the held Darwin WORK root.

Every mutation is a direct child of the held root FD. Missing destination
components are first created as fresh reserved staging directories and then
installed with ``renameatx_np`` using exclusive, no-follow-any, and
resolve-beneath flags. No descendant directory descriptor is ever a mutator.

The physical rename is limited to a fresh inode created by this call. WorkFS
never deletes, overwrites, or relocates a pre-existing user entry. A failed or
attacked stage is deliberately retained under its reserved root name.
"""

from __future__ import annotations

import errno
import os
import secrets
import stat as statmod
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from omniagentos.workfs.containment import require_contained
from omniagentos.workfs.darwin import ATOMIC_RENAME_FLAGS, rename_stage_atomic
from omniagentos.workfs.errors import WorkfsError, WorkfsPathError
from omniagentos.workfs.root import STAGE_PREFIX, work_root
from omniagentos.workfs.scope import map_scope

__all__ = ["EnsureResult", "ensure"]

# Deterministic test hooks. Production leaves both unset.
_after_resolve_hook: Callable[[], None] | None = None
_before_atomic_install_hook: Callable[[int, str, str], None] | None = None
_after_stage_validation_hook: Callable[[int, str, str], None] | None = None


@dataclass(frozen=True)
class EnsureResult:
    """Outcome of a logical create-only ensure call."""

    path: str
    created: bool
    company: str
    department: str | None
    subfolder: str | None


def ensure(
    company: str | None,
    department: str | None = None,
    subfolder: str | None = None,
    *,
    root: str | os.PathLike[str] | None = None,
) -> EnsureResult:
    """Atomically install every missing scope component under the held root."""
    if company is None or (isinstance(company, str) and company.strip() == ""):
        raise WorkfsPathError(
            "scope_required",
            "company scope is required for ensure; absent scope performs zero writes",
            detail={"company": company},
        )

    base = Path(os.path.abspath(os.fspath(root))) if root is not None else work_root()
    _require_existing_work_root(base)

    company_n = company.strip()
    dept_n = _norm_opt(department)
    sub_n = _norm_opt(subfolder)
    parts: list[str] = [company_n]
    if dept_n is not None:
        parts.append(dept_n)
    if sub_n is not None:
        parts.append(sub_n)

    root_fd, root_stat = _open_held_root(base)
    try:
        target = map_scope(company, department, subfolder, root=base)
        require_contained(target, base)

        hook = _after_resolve_hook
        if hook is not None:
            hook()

        created = _ensure_prefixes_atomic(root_fd, parts)
        _require_same_root_inode(base, root_stat)
    except WorkfsError as exc:
        try:
            _require_same_root_inode(base, root_stat)
        except WorkfsError as root_exc:
            raise root_exc from exc
        raise
    except OSError as exc:
        try:
            _require_same_root_inode(base, root_stat)
        except WorkfsError as root_exc:
            raise root_exc from exc
        raise WorkfsError(
            "ensure_failed",
            f"kernel-atomic directory install failed: {exc}",
            detail={
                "path": str(base.joinpath(*parts)),
                "errno": exc.errno,
                "error": str(exc),
            },
        ) from exc
    finally:
        try:
            os.close(root_fd)
        except OSError:
            pass

    return EnsureResult(
        path=str(target),
        created=created,
        company=company_n,
        department=dept_n,
        subfolder=sub_n,
    )


def _require_existing_work_root(base: Path) -> None:
    """Require a pre-existing real root; ensure never creates the root path."""
    try:
        root_stat = os.lstat(base)
    except OSError as exc:
        raise WorkfsError(
            "root_unavailable",
            "WORK root must pre-exist as a real directory",
            detail={"root": str(base), "errno": exc.errno, "error": str(exc)},
        ) from exc
    if statmod.S_ISLNK(root_stat.st_mode) or not statmod.S_ISDIR(root_stat.st_mode):
        raise WorkfsError(
            "root_not_directory",
            "WORK root must be a real directory, never a symlink",
            detail={"root": str(base)},
        )


def _directory_open_flags() -> tuple[int, int]:
    if not hasattr(os, "O_DIRECTORY") or not hasattr(os, "O_NOFOLLOW"):
        raise WorkfsError(
            "workfs_unavailable",
            "platform lacks required no-follow directory open protections",
        )
    return os.O_RDONLY | os.O_DIRECTORY, os.O_NOFOLLOW


def _open_held_root(base: Path) -> tuple[int, os.stat_result]:
    dir_flags, nofollow = _directory_open_flags()
    try:
        root_fd = os.open(os.fspath(base), dir_flags | nofollow)
    except OSError as exc:
        raise WorkfsError(
            "root_unavailable",
            f"cannot open WORK root without following links: {exc}",
            detail={"root": str(base), "errno": exc.errno, "error": str(exc)},
        ) from exc
    try:
        root_stat = os.fstat(root_fd)
        if not statmod.S_ISDIR(root_stat.st_mode):
            raise WorkfsError(
                "root_not_directory",
                "WORK root is not a directory",
                detail={"root": str(base)},
            )
    except BaseException:
        os.close(root_fd)
        raise
    return root_fd, root_stat


def _ensure_prefixes_atomic(root_fd: int, parts: list[str]) -> bool:
    created_any = False
    for depth in range(1, len(parts) + 1):
        prefix = parts[:depth]
        existing_fd = _open_relative_directory(root_fd, prefix)
        if existing_fd is not None:
            os.close(existing_fd)
            continue
        if _install_missing_prefix(root_fd, prefix):
            created_any = True
    return created_any


def _open_relative_directory(root_fd: int, parts: list[str]) -> int | None:
    """Read-only no-follow traversal; returned child FDs are never mutators."""
    dir_flags, nofollow = _directory_open_flags()
    parent_fd = os.dup(root_fd)
    try:
        for part in parts:
            try:
                child_fd = os.open(
                    part,
                    dir_flags | nofollow,
                    dir_fd=parent_fd,
                )
            except FileNotFoundError:
                return None
            except OSError as exc:
                raise _component_os_error(part, exc) from exc
            os.close(parent_fd)
            parent_fd = child_fd
        result_fd = parent_fd
        parent_fd = -1
        return result_fd
    finally:
        if parent_fd >= 0:
            os.close(parent_fd)


def _install_missing_prefix(root_fd: int, parts: list[str]) -> bool:
    """Create one direct-root stage and atomically install it at *parts*."""
    destination = "/".join(parts)
    stage = f"{STAGE_PREFIX}{secrets.token_hex(16)}"
    try:
        os.mkdir(stage, mode=0o755, dir_fd=root_fd)
        staged = os.stat(stage, dir_fd=root_fd, follow_symlinks=False)
    except OSError as exc:
        raise WorkfsError(
            "ensure_failed",
            "could not create reserved WorkFS stage",
            detail={
                "stage": stage,
                "errno": exc.errno,
                "error": str(exc),
            },
        ) from exc
    if not statmod.S_ISDIR(staged.st_mode):
        raise WorkfsError(
            "containment_lost",
            "reserved WorkFS stage is not a real directory",
            detail={"stage": stage},
        )

    hook = _before_atomic_install_hook
    if hook is not None:
        hook(root_fd, stage, destination)
    _require_same_stage_inode(root_fd, stage, staged)
    validated_hook = _after_stage_validation_hook
    if validated_hook is not None:
        validated_hook(root_fd, stage, destination)

    # This attempted protected install is also the runtime capability probe on
    # the configured filesystem. Any symbol/flag/filesystem failure occurs
    # before a user-visible destination exists and leaves only the hidden stage.
    try:
        rename_stage_atomic(
            root_fd,
            stage,
            root_fd,
            destination,
            ATOMIC_RENAME_FLAGS,
        )
    except FileExistsError as exc:
        winner_fd = _open_relative_directory(root_fd, parts)
        if winner_fd is None:
            raise WorkfsError(
                "atomic_install_conflict",
                "destination collision did not produce a real directory",
                detail={"destination": destination, "stage": stage},
            ) from exc
        os.close(winner_fd)
        return False
    except OSError as exc:
        raise WorkfsError(
            "atomic_install_failed",
            "Darwin protected staging install failed closed",
            detail={
                "destination": destination,
                "stage": stage,
                "errno": exc.errno,
                "error": str(exc),
            },
        ) from exc

    installed_fd = _open_relative_directory(root_fd, parts)
    if installed_fd is None:
        raise WorkfsError(
            "containment_lost",
            "installed directory vanished before verification",
            detail={"destination": destination},
        )
    try:
        installed = os.fstat(installed_fd)
    finally:
        os.close(installed_fd)
    if installed.st_dev != staged.st_dev or installed.st_ino != staged.st_ino:
        raise WorkfsError(
            "containment_lost",
            "installed directory inode does not match the reserved stage",
            detail={"destination": destination, "stage": stage},
        )
    return True


def _require_same_stage_inode(
    root_fd: int,
    stage: str,
    expected: os.stat_result,
) -> None:
    try:
        current = os.stat(stage, dir_fd=root_fd, follow_symlinks=False)
    except OSError as exc:
        raise WorkfsError(
            "containment_lost",
            "reserved stage disappeared before atomic install",
            detail={"stage": stage, "errno": exc.errno, "error": str(exc)},
        ) from exc
    if (
        not statmod.S_ISDIR(current.st_mode)
        or current.st_dev != expected.st_dev
        or current.st_ino != expected.st_ino
    ):
        raise WorkfsError(
            "containment_lost",
            "reserved stage identity changed before atomic install",
            detail={"stage": stage},
        )


def _require_same_root_inode(base: Path, root_stat: os.stat_result) -> None:
    try:
        current = os.lstat(base)
    except OSError as exc:
        raise WorkfsError(
            "containment_lost",
            "WORK root disappeared during ensure",
            detail={"root": str(base), "errno": exc.errno, "error": str(exc)},
        ) from exc
    if (
        not statmod.S_ISDIR(current.st_mode)
        or current.st_dev != root_stat.st_dev
        or current.st_ino != root_stat.st_ino
    ):
        raise WorkfsError(
            "containment_lost",
            "WORK root changed during ensure; result was not accepted",
            detail={"root": str(base)},
        )


def _component_os_error(name: str, exc: OSError) -> WorkfsError:
    error_number = exc.errno
    if error_number in (errno.ELOOP, errno.ENOTDIR):
        return WorkfsError(
            "target_not_directory",
            f"scope component is not a real directory: {name!r}",
            detail={"component": name, "errno": error_number, "error": str(exc)},
        )
    return WorkfsError(
        "ensure_failed",
        f"cannot read scope component {name!r}: {exc}",
        detail={"component": name, "errno": error_number, "error": str(exc)},
    )


def _norm_opt(value: str | None) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text if text else None
