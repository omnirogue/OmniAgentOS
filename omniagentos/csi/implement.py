"""Human-approved CSI implementation with containment and reviewer retention.

CSI writes only to a dedicated git worktree and never merges.  Approval binds
the evidence, synthesis, conflict forecast and code SHA; all are revalidated
at the plan/apply boundary.  Existing worktrees and branches are retained
rather than force-deleted.
"""

from __future__ import annotations

import ctypes
import errno
import fcntl
import hashlib
import json
import logging
import os
import re
import select
import shutil
import sqlite3
import stat
import subprocess
import sys
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from omniagentos.csi.config import load_csi_config
from omniagentos.csi.conflict import ConflictForecastService
from omniagentos.csi.frozen import (
    assert_canonical_destination,
    repo_relative,
)
from omniagentos.csi.models import PlannerProposal
from omniagentos.csi.store import CsiStore, approval_binding
from omniagentos.path_containment import inode_paths_equal, inode_relative_parts_anchored

LOG = logging.getLogger(__name__)
_ROOT = Path(__file__).resolve().parents[2]
_SAFE_RUN_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_O_DIRECTORY = getattr(os, "O_DIRECTORY", 0)
_O_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_RENAME_NOREPLACE = 1
_RENAME_SWAP = 2
_DARWIN_RENAME_EXCL = 4
_ACTIVE_REVIEW_STATES = {
    "ANALYZING",
    "AWAITING_HUMAN",
    "DEFERRED",
    "IMPLEMENTING",
    "AWAITING_MERGE",
    "INCIDENT",
}
_CLEANABLE_TERMINAL_STATES = {
    "CANCELLED",
    "MERGED",
    "QUARANTINED",
    "REJECTED",
}


@dataclass
class ImplementResult:
    ok: bool
    run_id: str
    improvement_id: str = ""
    branch: str = ""
    worktree: str = ""
    written_paths: list[str] = field(default_factory=list)
    status: str = ""
    error: str = ""
    idempotent: bool = False


@dataclass
class CleanupResult:
    ok: bool
    run_id: str
    action: str
    branch: str = ""
    worktree: str = ""
    retained_reason: str = ""
    error: str = ""


@dataclass(frozen=True)
class _DestinationSnapshot:
    exists: bool
    device: int = 0
    inode: int = 0
    mode: int = 0
    size: int = 0
    modified_ns: int = 0
    digest: str = ""


@dataclass(frozen=True)
class _DirectoryIdentity:
    device: int
    inode: int


@dataclass(frozen=True)
class _BoundIndex:
    live_path: Path
    private_path: Path
    sha256: str
    size: int


@dataclass(frozen=True)
class _WorktreeRegistration:
    active_path: Path
    retired_path: Path
    identity: _DirectoryIdentity
    worktree: Path
    branch: str
    state: str = "active_bound"


@dataclass(frozen=True)
class _QuarantineTombstone:
    path: Path
    source_path: Path
    identity: _DirectoryIdentity
    state: str
    path_verification: str = "pending_after_bound_persist"
    registration: _WorktreeRegistration | None = None


class _CleanupClaimLost(RuntimeError):
    """The durable cleanup claim changed at a filesystem mutation boundary."""


def _git(cwd: str | Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(cwd), *args],
        capture_output=True,
        text=True,
        timeout=120,
        check=check,
    )


def _git_text(cwd: str | Path, *args: str) -> str:
    return (_git(cwd, *args).stdout or "").strip()


def _git_bytes(cwd: str | Path, *args: str) -> bytes:
    p = subprocess.run(
        ["git", "-C", str(cwd), *args],
        capture_output=True,
        timeout=120,
        check=False,
    )
    if p.returncode != 0:
        return b""
    return p.stdout


def _git_with_index(
    cwd: str | Path,
    index_path: Path,
    *args: str,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["GIT_INDEX_FILE"] = str(index_path)
    return subprocess.run(
        ["git", "-C", str(cwd), *args],
        capture_output=True,
        text=True,
        timeout=120,
        check=check,
        env=env,
    )


def _read_regular_nofollow(path: Path) -> bytes:
    fd = os.open(path, os.O_RDONLY | _O_NOFOLLOW)
    try:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode):
            raise PermissionError(f"non_regular_git_index_refused:{path}")
        chunks: list[bytes] = []
        while True:
            chunk = os.read(fd, 1024 * 1024)
            if not chunk:
                break
            chunks.append(chunk)
        after = os.fstat(fd)
        if (
            before.st_dev != after.st_dev
            or before.st_ino != after.st_ino
            or before.st_size != after.st_size
            or before.st_mtime_ns != after.st_mtime_ns
        ):
            raise RuntimeError("git_index_changed_while_reading")
        return b"".join(chunks)
    finally:
        os.close(fd)


def _worktree_index_path(wt_root: Path) -> Path:
    raw_git_dir = _git_text(wt_root, "rev-parse", "--absolute-git-dir")
    git_dir = Path(raw_git_dir)
    if not git_dir.is_absolute():
        raise RuntimeError(f"worktree_git_dir_not_absolute:{raw_git_dir}")
    git_dir = git_dir.resolve(strict=True)
    if not git_dir.is_dir():
        raise RuntimeError(f"worktree_git_dir_invalid:{git_dir}")
    index_path = git_dir / "index"
    try:
        info = os.stat(index_path, follow_symlinks=False)
    except FileNotFoundError as exc:
        raise RuntimeError("worktree_git_index_missing") from exc
    if not stat.S_ISREG(info.st_mode):
        raise PermissionError(f"non_regular_git_index_refused:{index_path}")
    return index_path


@contextmanager
def _bind_staged_index(wt_root: Path) -> Iterator[_BoundIndex]:
    """Reserve and copy the exact live index used to derive the commit tree.

    The standard ``index.lock`` excludes cooperating Git writers for the whole
    commit/finalization boundary.  Tree and status operations use a private
    copy, so CSI never asks Git to replace the reserved lock.
    """

    index_path = _worktree_index_path(wt_root)
    lock_path = index_path.with_name(f"{index_path.name}.lock")
    private_path = index_path.with_name(f"csi-bound-index-{uuid.uuid4().hex}.tmp")
    lock_fd = -1
    private_created = False
    try:
        try:
            lock_fd = os.open(
                lock_path,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | _O_NOFOLLOW,
                0o600,
            )
        except FileExistsError as exc:
            raise RuntimeError("reviewer_index_busy") from exc
        payload = _read_regular_nofollow(index_path)
        private_fd = os.open(
            private_path,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | _O_NOFOLLOW,
            0o600,
        )
        private_created = True
        try:
            offset = 0
            while offset < len(payload):
                written = os.write(private_fd, payload[offset:])
                if written <= 0:
                    raise OSError("bound index copy made no progress")
                offset += written
            os.fsync(private_fd)
        finally:
            os.close(private_fd)
        yield _BoundIndex(
            live_path=index_path,
            private_path=private_path,
            sha256=hashlib.sha256(payload).hexdigest(),
            size=len(payload),
        )
    finally:
        if private_created:
            try:
                os.unlink(private_path)
            except FileNotFoundError:
                pass
        if lock_fd >= 0:
            os.close(lock_fd)
            try:
                os.unlink(lock_path)
            except FileNotFoundError:
                pass


def _assert_live_index_unchanged(binding: _BoundIndex) -> None:
    payload = _read_regular_nofollow(binding.live_path)
    if len(payload) != binding.size or hashlib.sha256(payload).hexdigest() != binding.sha256:
        raise RuntimeError("reviewer_index_changed_at_commit_boundary")


def _lexical_absolute_path(raw: str | Path, *, label: str) -> Path:
    raw_text = os.fspath(raw)
    path = Path(raw_text)
    if not path.is_absolute():
        raise PermissionError(f"{label}_not_absolute:{path}")
    normalized = os.path.normpath(raw_text)
    if raw_text != normalized:
        raise PermissionError(f"{label}_not_lexically_canonical:{path}")
    return Path(normalized)


def _worktree_directory_identity(root: Path, worktree: Path) -> _DirectoryIdentity:
    """Open every component without following links and return the final inode."""

    if not _O_DIRECTORY or not _O_NOFOLLOW:
        raise RuntimeError("secure_nofollow_directory_operations_unavailable")
    canonical_root = root.resolve(strict=True)
    target = _lexical_absolute_path(worktree, label="worktree_path")
    relative_parts = inode_relative_parts_anchored(target, canonical_root)
    if relative_parts is None:
        raise PermissionError(f"worktree_path_outside_repository:{target}")
    if not relative_parts:
        raise PermissionError("repository_root_cannot_be_cleanup_worktree")

    flags = os.O_RDONLY | _O_DIRECTORY | _O_NOFOLLOW
    root_fd = os.open(canonical_root, flags)
    current_fd = root_fd
    opened: list[int] = []
    try:
        for part in relative_parts:
            try:
                child_fd = os.open(part, flags, dir_fd=current_fd)
            except OSError as exc:
                if exc.errno in {errno.ELOOP, errno.ENOTDIR}:
                    raise PermissionError(
                        f"worktree_symlink_or_non_directory_refused:{target}"
                    ) from exc
                raise
            opened.append(child_fd)
            current_fd = child_fd
        info = os.fstat(current_fd)
        return _DirectoryIdentity(device=info.st_dev, inode=info.st_ino)
    finally:
        for fd in reversed(opened):
            os.close(fd)
        os.close(root_fd)


def _directory_entry_identity(dir_fd: int, name: str) -> _DirectoryIdentity:
    info = os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
    if not stat.S_ISDIR(info.st_mode):
        raise PermissionError(f"worktree_symlink_or_non_directory_refused:{name}")
    return _DirectoryIdentity(device=info.st_dev, inode=info.st_ino)


def _git_common_directory(root: Path) -> Path:
    common_raw = _git_text(root, "rev-parse", "--git-common-dir")
    common = Path(common_raw)
    if not common.is_absolute():
        common = (root / common).resolve(strict=True)
    else:
        common = common.resolve(strict=True)
    if not common.is_dir():
        raise RuntimeError(f"git_common_dir_invalid:{common}")
    return common


def _read_regular_at(dir_fd: int, name: str, *, label: str) -> bytes:
    fd = os.open(name, os.O_RDONLY | _O_NOFOLLOW, dir_fd=dir_fd)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise PermissionError(f"{label}_not_regular")
        if info.st_size > 1024 * 1024:
            raise PermissionError(f"{label}_too_large")
        chunks: list[bytes] = []
        remaining = info.st_size
        while remaining:
            chunk = os.read(fd, min(remaining, 64 * 1024))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        payload = b"".join(chunks)
        if len(payload) != info.st_size:
            raise RuntimeError(f"{label}_changed_while_reading")
        return payload
    finally:
        os.close(fd)


def _registration_text(payload: bytes, *, label: str) -> str:
    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise PermissionError(f"{label}_invalid_utf8") from exc
    return text.rstrip("\n")


def _bind_worktree_registration(
    root: Path,
    *,
    worktree_fd: int,
    worktree: Path,
    branch: str,
    retirement_token: str,
) -> _WorktreeRegistration:
    """Bind the exact Git administrative record for one linked worktree."""

    gitfile = _registration_text(
        _read_regular_at(worktree_fd, ".git", label="worktree_gitfile"),
        label="worktree_gitfile",
    )
    prefix = "gitdir: "
    if not gitfile.startswith(prefix):
        raise PermissionError("worktree_gitfile_invalid")
    admin_path = _lexical_absolute_path(
        gitfile[len(prefix) :],
        label="worktree_admin_path",
    )
    common = _git_common_directory(root)
    active_parent = common / "worktrees"
    # Safety (`is not True`): reject unless the admin parent is positively equal.
    if inode_paths_equal(admin_path.parent, active_parent) is not True:
        raise PermissionError("worktree_admin_path_outside_common_dir")
    if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,255}", admin_path.name) is None:
        raise PermissionError("worktree_admin_name_invalid")
    if re.fullmatch(r"[0-9a-f]{32}", retirement_token) is None:
        raise PermissionError("worktree_retirement_token_invalid")

    flags = os.O_RDONLY | _O_DIRECTORY | _O_NOFOLLOW
    parent_fd = os.open(active_parent, flags)
    admin_fd = -1
    try:
        admin_fd = os.open(admin_path.name, flags, dir_fd=parent_fd)
        info = os.fstat(admin_fd)
        identity = _DirectoryIdentity(device=info.st_dev, inode=info.st_ino)
        registered_gitfile = _registration_text(
            _read_regular_at(admin_fd, "gitdir", label="registration_gitdir"),
            label="registration_gitdir",
        )
        # Safety (`is not True`): reject unless registration binds the exact worktree.
        if inode_paths_equal(Path(registered_gitfile), worktree / ".git") is not True:
            raise PermissionError("registration_worktree_path_mismatch")
        head = _registration_text(
            _read_regular_at(admin_fd, "HEAD", label="registration_head"),
            label="registration_head",
        )
        if head != f"ref: refs/heads/{branch}":
            raise PermissionError("registration_branch_mismatch")
    finally:
        if admin_fd >= 0:
            os.close(admin_fd)
        os.close(parent_fd)

    retired_parent = common / "csi-retired-worktrees"
    retired_name = f"{admin_path.name}-{retirement_token}.retired"
    return _WorktreeRegistration(
        active_path=admin_path,
        retired_path=retired_parent / retired_name,
        identity=identity,
        worktree=worktree,
        branch=branch,
    )


def _registration_from_journal(
    recorded: dict[str, Any],
    *,
    root: Path,
    expected_worktree: Path,
    expected_branch: str,
) -> _WorktreeRegistration | None:
    raw = recorded.get("registration")
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise PermissionError("cleanup_registration_journal_invalid")
    active_path = _lexical_absolute_path(
        str(raw.get("active_path") or ""),
        label="cleanup_registration_active_path",
    )
    retired_path = _lexical_absolute_path(
        str(raw.get("retired_path") or ""),
        label="cleanup_registration_retired_path",
    )
    common = _git_common_directory(root)
    # Safety (`is not True`): reject unless the active parent is positively equal.
    if inode_paths_equal(active_path.parent, common / "worktrees") is not True:
        raise PermissionError("cleanup_registration_active_path_invalid")
    if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,255}", active_path.name) is None:
        raise PermissionError("cleanup_registration_active_name_invalid")
    # Safety (`is not True`): reject unless the retired parent is positively equal.
    if inode_paths_equal(retired_path.parent, common / "csi-retired-worktrees") is not True:
        raise PermissionError("cleanup_registration_retired_path_invalid")
    if (
        re.fullmatch(
            r"[A-Za-z0-9][A-Za-z0-9._-]{0,255}-[0-9a-f]{32}\.retired",
            retired_path.name,
        )
        is None
    ):
        raise PermissionError("cleanup_registration_retired_name_invalid")
    device = raw.get("device")
    inode = raw.get("inode")
    branch = str(raw.get("branch") or "")
    state = str(raw.get("state") or "")
    worktree = _lexical_absolute_path(
        str(raw.get("worktree") or ""),
        label="cleanup_registration_worktree",
    )
    if (
        not isinstance(device, int)
        or not isinstance(inode, int)
        or branch != expected_branch
        # Safety (`is not True`): reject unless the journal worktree is positively equal.
        or inode_paths_equal(worktree, expected_worktree) is not True
        or state not in {"active_bound", "retired_bound"}
    ):
        raise PermissionError("cleanup_registration_journal_invalid")
    return _WorktreeRegistration(
        active_path=active_path,
        retired_path=retired_path,
        identity=_DirectoryIdentity(device=device, inode=inode),
        worktree=worktree,
        branch=branch,
        state=state,
    )


def _retire_worktree_registration(
    root: Path,
    registration: _WorktreeRegistration,
) -> _WorktreeRegistration:
    """Atomically move one exact registration out of Git's active namespace."""

    common = _git_common_directory(root)
    # Safety (`is not True`): reject unless the active namespace is positively equal.
    if inode_paths_equal(registration.active_path.parent, common / "worktrees") is not True:
        raise PermissionError("worktree_registration_active_path_invalid")
    if (
        # Safety (`is not True`): reject unless the retired namespace is positively equal.
        inode_paths_equal(
            registration.retired_path.parent,
            common / "csi-retired-worktrees",
        )
        is not True
    ):
        raise PermissionError("worktree_registration_retired_path_invalid")
    _secure_ensure_dir(common, ("csi-retired-worktrees",))

    flags = os.O_RDONLY | _O_DIRECTORY | _O_NOFOLLOW
    active_parent_fd = os.open(registration.active_path.parent, flags)
    retired_parent_fd = os.open(registration.retired_path.parent, flags)
    try:
        try:
            retired_identity = _directory_entry_identity(
                retired_parent_fd,
                registration.retired_path.name,
            )
        except FileNotFoundError:
            retired_identity = None
        if retired_identity is not None:
            if retired_identity != registration.identity:
                raise RuntimeError("retired_worktree_registration_identity_mismatch")
            try:
                _directory_entry_identity(
                    active_parent_fd,
                    registration.active_path.name,
                )
            except FileNotFoundError:
                return _WorktreeRegistration(
                    active_path=registration.active_path,
                    retired_path=registration.retired_path,
                    identity=registration.identity,
                    worktree=registration.worktree,
                    branch=registration.branch,
                    state="retired_bound",
                )
            raise RuntimeError("worktree_registration_exists_in_both_namespaces")

        active_identity = _directory_entry_identity(
            active_parent_fd,
            registration.active_path.name,
        )
        if active_identity != registration.identity:
            raise RuntimeError("worktree_registration_identity_mismatch")
        admin_fd = os.open(
            registration.active_path.name,
            flags,
            dir_fd=active_parent_fd,
        )
        try:
            gitdir_text = _registration_text(
                _read_regular_at(admin_fd, "gitdir", label="registration_gitdir"),
                label="registration_gitdir",
            )
            head = _registration_text(
                _read_regular_at(admin_fd, "HEAD", label="registration_head"),
                label="registration_head",
            )
            # Safety (`is not True`): reject unless registration binds the exact worktree.
            if inode_paths_equal(Path(gitdir_text), registration.worktree / ".git") is not True:
                raise RuntimeError("registration_worktree_path_mismatch")
            if head != f"ref: refs/heads/{registration.branch}":
                raise RuntimeError("registration_branch_mismatch")
        finally:
            os.close(admin_fd)

        _atomic_rename_at(
            active_parent_fd,
            registration.active_path.name,
            registration.retired_path.name,
            exchange=False,
            destination_dir_fd=retired_parent_fd,
        )
        moved_identity = _directory_entry_identity(
            retired_parent_fd,
            registration.retired_path.name,
        )
        if moved_identity != registration.identity:
            try:
                _atomic_rename_at(
                    retired_parent_fd,
                    registration.retired_path.name,
                    registration.active_path.name,
                    exchange=False,
                    destination_dir_fd=active_parent_fd,
                )
            except OSError as rollback_exc:
                raise RuntimeError(
                    "retired_worktree_registration_identity_mismatch:substitute_rollback_failed"
                ) from rollback_exc
            raise RuntimeError("retired_worktree_registration_identity_mismatch")
        try:
            _directory_entry_identity(
                active_parent_fd,
                registration.active_path.name,
            )
        except FileNotFoundError:
            pass
        else:
            raise RuntimeError("worktree_registration_retirement_incomplete")
    finally:
        os.close(retired_parent_fd)
        os.close(active_parent_fd)

    if _branch_worktree(root, registration.branch) is not None:
        raise RuntimeError("retired_worktree_registration_still_active")
    return _WorktreeRegistration(
        active_path=registration.active_path,
        retired_path=registration.retired_path,
        identity=registration.identity,
        worktree=registration.worktree,
        branch=registration.branch,
        state="retired_bound",
    )


@contextmanager
def _clear_bound_directory_tree(
    parent_fd: int,
    name: str,
    *,
    expected_identity: _DirectoryIdentity,
) -> Iterator[int]:
    """Clear and keep open the exact directory selected from ``parent_fd``.

    POSIX does not provide a portable directory-fd equivalent of ``rmdir``.
    The now-empty directory therefore remains as a quarantine tombstone rather
    than reintroducing an inode-check-to-name-unlink race.  The descriptor is
    kept open across durable journal persistence so the journal's device/inode
    always names the exact emptied object even if its pathname is substituted.
    """

    flags = os.O_RDONLY | _O_DIRECTORY | _O_NOFOLLOW
    try:
        directory_fd = os.open(name, flags, dir_fd=parent_fd)
    except OSError as exc:
        if exc.errno in {errno.ELOOP, errno.ENOTDIR}:
            raise PermissionError(f"worktree_symlink_or_non_directory_refused:{name}") from exc
        raise
    try:
        opened = os.fstat(directory_fd)
        opened_identity = _DirectoryIdentity(
            device=opened.st_dev,
            inode=opened.st_ino,
        )
        if opened_identity != expected_identity:
            raise RuntimeError("cleanup_worktree_quarantine_identity_mismatch")
        if _directory_entry_identity(parent_fd, name) != expected_identity:
            raise RuntimeError("cleanup_worktree_quarantine_identity_mismatch")

        with os.scandir(directory_fd) as iterator:
            entries = list(iterator)
        for entry in entries:
            if entry.is_dir(follow_symlinks=False):
                shutil.rmtree(entry.name, dir_fd=directory_fd)
            else:
                os.unlink(entry.name, dir_fd=directory_fd)

        if _directory_entry_identity(parent_fd, name) != expected_identity:
            raise RuntimeError("cleanup_worktree_quarantine_identity_mismatch")
        yield directory_fd
    finally:
        os.close(directory_fd)


def _retain_bound_directory_tombstone(
    parent_fd: int,
    name: str,
    *,
    path: Path,
    source_path: Path,
    expected_identity: _DirectoryIdentity,
    registration: _WorktreeRegistration | None,
) -> _QuarantineTombstone:
    """Record, but never unlink or clear again, the retained quarantine."""

    if _directory_entry_identity(parent_fd, name) != expected_identity:
        raise RuntimeError("cleanup_worktree_quarantine_identity_mismatch")
    return _QuarantineTombstone(
        path=path,
        source_path=source_path,
        identity=expected_identity,
        state="bound_retained_at_persist",
        registration=registration,
    )


def _quarantine_journal(
    tombstone: _QuarantineTombstone,
    *,
    restore_status: str,
) -> dict[str, Any]:
    journal: dict[str, Any] = {
        "path": str(tombstone.path),
        "source_path": str(tombstone.source_path),
        "device": tombstone.identity.device,
        "inode": tombstone.identity.inode,
        "state": tombstone.state,
        "path_verification": tombstone.path_verification,
        "unlink_pending": True,
        "restore_status": restore_status,
    }
    if tombstone.registration is not None:
        journal["registration"] = _registration_journal(tombstone.registration)
    return journal


def _registration_journal(
    registration: _WorktreeRegistration,
) -> dict[str, Any]:
    return {
        "active_path": str(registration.active_path),
        "retired_path": str(registration.retired_path),
        "device": registration.identity.device,
        "inode": registration.identity.inode,
        "worktree": str(registration.worktree),
        "branch": registration.branch,
        "state": registration.state,
    }


def _observe_quarantine_path(
    recorded: dict[str, Any],
    *,
    expected_worktree: Path,
) -> str:
    """Observe a journaled pathname without treating it as the bound inode."""

    raw_path = recorded.get("path")
    raw_source_path = recorded.get("source_path")
    device = recorded.get("device")
    inode = recorded.get("inode")
    journal_state = recorded.get("state")
    if raw_source_path is None and journal_state == "bound_empty_at_persist":
        return _legacy_observe_quarantine_path(
            recorded,
            expected_worktree=expected_worktree,
        )
    if (
        journal_state
        not in {
            "rename_intent_bound",
            "quarantined_bound",
            "bound_empty_at_persist",
            "bound_retained_at_persist",
        }
        or recorded.get("unlink_pending") is not True
        or not isinstance(raw_path, str)
        or not isinstance(raw_source_path, str)
        or not isinstance(device, int)
        or not isinstance(inode, int)
    ):
        return "invalid_quarantine_journal"
    try:
        path = _lexical_absolute_path(raw_path, label="cleanup_quarantine_path")
        source_path = _lexical_absolute_path(
            raw_source_path,
            label="cleanup_quarantine_source_path",
        )
    except PermissionError:
        return "invalid_quarantine_journal"
    if (
        # Safety (`is not True`): reject unless the source is the expected worktree.
        inode_paths_equal(source_path, expected_worktree) is not True
        # Safety (`is not True`): reject unless quarantine keeps the expected parent.
        or inode_paths_equal(path.parent, expected_worktree.parent) is not True
        or re.fullmatch(r"\.csi-cleanup-[0-9a-f]{32}\.quarantine", path.name) is None
    ):
        return "invalid_quarantine_journal"

    def _observe_one(candidate: Path) -> str:
        flags = os.O_RDONLY | _O_DIRECTORY | _O_NOFOLLOW
        try:
            directory_fd = os.open(candidate, flags)
        except FileNotFoundError:
            return "missing"
        except OSError:
            return "non_directory_or_symlink"
        try:
            info = os.fstat(directory_fd)
            if info.st_dev != device or info.st_ino != inode:
                return "identity_mismatch"
            with os.scandir(directory_fd) as iterator:
                try:
                    next(iterator)
                except StopIteration:
                    return "matched_bound_identity_empty"
            return "matched_bound_identity_nonempty"
        finally:
            os.close(directory_fd)

    quarantine_observation = _observe_one(path)
    if journal_state in {
        "bound_empty_at_persist",
        "bound_retained_at_persist",
    }:
        return {
            "missing": "missing_at_current_observation",
            "non_directory_or_symlink": ("non_directory_or_symlink_at_current_observation"),
            "identity_mismatch": "identity_mismatch_at_current_observation",
            "matched_bound_identity_empty": ("matched_bound_identity_empty_at_current_observation"),
            "matched_bound_identity_nonempty": (
                "matched_bound_identity_nonempty_at_current_observation"
            ),
        }[quarantine_observation]

    source_observation = _observe_one(source_path)
    if quarantine_observation.startswith("matched_bound_identity_"):
        return f"{journal_state}:quarantine_{quarantine_observation}_at_current_observation"
    if source_observation.startswith("matched_bound_identity_"):
        return f"{journal_state}:source_{source_observation}_at_current_observation"
    return (
        f"{journal_state}:quarantine_{quarantine_observation}:"
        f"source_{source_observation}_at_current_observation"
    )


def _legacy_observe_quarantine_path(
    recorded: dict[str, Any],
    *,
    expected_worktree: Path,
) -> str:
    """Compatibility observation for journals created before phase metadata."""

    raw_path = recorded.get("path")
    device = recorded.get("device")
    inode = recorded.get("inode")
    if (
        recorded.get("state") != "bound_empty_at_persist"
        or recorded.get("unlink_pending") is not True
        or not isinstance(raw_path, str)
        or not isinstance(device, int)
        or not isinstance(inode, int)
    ):
        return "invalid_quarantine_journal"
    try:
        path = _lexical_absolute_path(raw_path, label="cleanup_quarantine_path")
    except PermissionError:
        return "invalid_quarantine_journal"
    if (
        # Safety (`is not True`): reject unless quarantine keeps the expected parent.
        inode_paths_equal(path.parent, expected_worktree.parent) is not True
        or re.fullmatch(r"\.csi-cleanup-[0-9a-f]{32}\.quarantine", path.name) is None
    ):
        return "invalid_quarantine_journal"

    flags = os.O_RDONLY | _O_DIRECTORY | _O_NOFOLLOW
    try:
        directory_fd = os.open(path, flags)
    except FileNotFoundError:
        return "missing_at_current_observation"
    except OSError:
        return "non_directory_or_symlink_at_current_observation"
    try:
        info = os.fstat(directory_fd)
        if info.st_dev != device or info.st_ino != inode:
            return "identity_mismatch_at_current_observation"
        with os.scandir(directory_fd) as iterator:
            try:
                next(iterator)
            except StopIteration:
                return "matched_bound_identity_empty_at_current_observation"
        return "matched_bound_identity_nonempty_at_current_observation"
    finally:
        os.close(directory_fd)


def _reconcile_journaled_registration(
    recorded: dict[str, Any],
    *,
    root: Path,
    expected_worktree: Path,
    expected_branch: str,
    quarantine_observation: str,
) -> tuple[dict[str, Any], str]:
    """Retire a stale exact registration without touching worktree bytes."""

    registration = _registration_from_journal(
        recorded,
        root=root,
        expected_worktree=expected_worktree,
        expected_branch=expected_branch,
    )
    if registration is None:
        return recorded, "registration_journal_unavailable"
    if "source_matched_bound_identity_" in quarantine_observation:
        return recorded, "registration_active_source_still_bound"
    previous_state = registration.state
    retired = _retire_worktree_registration(root, registration)
    updated = dict(recorded)
    updated["registration"] = _registration_journal(retired)
    return updated, (
        "worktree_registration_retired"
        if previous_state == "active_bound"
        else "worktree_registration_already_retired"
    )


def _quarantine_and_clear_worktree(
    root: Path,
    worktree: Path,
    *,
    branch: str,
    expected_identity: _DirectoryIdentity,
    claim_current: Callable[[], bool],
    persist_tombstone: Callable[[_QuarantineTombstone], bool],
) -> _QuarantineTombstone:
    """Clear the validated worktree and retain its exact directory tombstone.

    The worktree entry is atomically renamed through a held parent descriptor
    before recursive removal.  The random quarantine directory is opened and
    compared with the expected inode; its contents are then removed through
    that bound descriptor.  Because portable POSIX ``rmdir`` is name-based,
    the emptied directory remains as a retained tombstone.  Git metadata and
    the branch must remain until an operator can reconcile that tombstone.
    If either the source or quarantine entry was substituted, no substituted
    contents are removed and the moved entry is restored when possible.
    """

    if not getattr(shutil.rmtree, "avoids_symlink_attacks", False):
        raise RuntimeError("symlink_safe_recursive_removal_unavailable")
    if not _O_DIRECTORY or not _O_NOFOLLOW:
        raise RuntimeError("secure_nofollow_directory_operations_unavailable")

    canonical_root = root.resolve(strict=True)
    target = _lexical_absolute_path(worktree, label="worktree_path")
    relative_parts = inode_relative_parts_anchored(target, canonical_root)
    if relative_parts is None:
        raise PermissionError(f"worktree_path_outside_repository:{target}")
    if len(relative_parts) < 2:
        raise PermissionError("unsafe_cleanup_worktree_location")

    flags = os.O_RDONLY | _O_DIRECTORY | _O_NOFOLLOW
    root_fd = os.open(canonical_root, flags)
    parent_fd = root_fd
    opened: list[int] = []
    retirement_token = uuid.uuid4().hex
    quarantine_name = f".csi-cleanup-{retirement_token}.quarantine"
    quarantine_path = target.with_name(quarantine_name)
    leaf = relative_parts[-1]
    bound_worktree_fd = -1
    registration: _WorktreeRegistration | None = None

    try:
        for part in relative_parts[:-1]:
            try:
                child_fd = os.open(part, flags, dir_fd=parent_fd)
            except OSError as exc:
                if exc.errno in {errno.ELOOP, errno.ENOTDIR}:
                    raise PermissionError(
                        f"worktree_symlink_or_non_directory_refused:{target}"
                    ) from exc
                raise
            opened.append(child_fd)
            parent_fd = child_fd

        if _directory_entry_identity(parent_fd, leaf) != expected_identity:
            raise RuntimeError("cleanup_worktree_identity_changed")
        bound_worktree_fd = os.open(leaf, flags, dir_fd=parent_fd)
        bound_info = os.fstat(bound_worktree_fd)
        if (
            bound_info.st_dev != expected_identity.device
            or bound_info.st_ino != expected_identity.inode
        ):
            raise RuntimeError("cleanup_worktree_identity_changed")
        registration = _bind_worktree_registration(
            root,
            worktree_fd=bound_worktree_fd,
            worktree=target,
            branch=branch,
            retirement_token=retirement_token,
        )

        # Persist the exact source/quarantine names and the descriptor-bound
        # identity before the rename.  A crash after the namespace mutation is
        # therefore always recoverable from durable metadata.
        intent = _QuarantineTombstone(
            path=quarantine_path,
            source_path=target,
            identity=expected_identity,
            state="rename_intent_bound",
            path_verification="source_bound_before_rename",
            registration=registration,
        )
        if not persist_tombstone(intent):
            raise _CleanupClaimLost("cleanup_claim_lost_before_rename_intent")
        if not claim_current():
            raise _CleanupClaimLost("cleanup_claim_lost_after_rename_intent")
        if _directory_entry_identity(parent_fd, leaf) != expected_identity:
            raise RuntimeError("cleanup_worktree_identity_changed_before_rename")
        rebound_info = os.fstat(bound_worktree_fd)
        if (
            rebound_info.st_dev != expected_identity.device
            or rebound_info.st_ino != expected_identity.inode
        ):
            raise RuntimeError("cleanup_worktree_identity_changed_before_rename")

        _atomic_rename_at(
            parent_fd,
            leaf,
            quarantine_name,
            exchange=False,
        )
        try:
            moved_identity = _directory_entry_identity(parent_fd, quarantine_name)
        except (OSError, PermissionError) as exc:
            raise RuntimeError(
                f"cleanup_worktree_quarantine_identity_mismatch:{quarantine_path}"
            ) from exc
        if moved_identity != expected_identity:
            raise RuntimeError(f"cleanup_worktree_quarantine_identity_mismatch:{quarantine_path}")
        moved_bound_info = os.fstat(bound_worktree_fd)
        if (
            moved_bound_info.st_dev != expected_identity.device
            or moved_bound_info.st_ino != expected_identity.inode
        ):
            raise RuntimeError("cleanup_worktree_quarantine_identity_mismatch")

        if not claim_current():
            raise _CleanupClaimLost("cleanup_claim_lost_before_bound_clear")

        try:
            with _clear_bound_directory_tree(
                parent_fd,
                quarantine_name,
                expected_identity=expected_identity,
            ):
                # The initial CSI worktree contents are gone before publishing
                # the quarantine-bound point.  A cooperating reviewer can
                # still hold a descriptor opened before the rename.  From the
                # moment this durable journal lands, no code in this cleanup
                # path may delete anything added through that descriptor.
                quarantined_record = _QuarantineTombstone(
                    path=quarantine_path,
                    source_path=target,
                    identity=expected_identity,
                    state="quarantined_bound",
                    path_verification="quarantine_bound_after_initial_clear",
                    registration=registration,
                )
                if not persist_tombstone(quarantined_record):
                    raise _CleanupClaimLost("cleanup_claim_lost_before_quarantined_journal")
                # Registration retirement moves only the descriptor-bound Git
                # administrative directory out of Git's active worktree
                # namespace. It never traverses either worktree pathname.
                registration = _retire_worktree_registration(
                    root,
                    registration,
                )
                registration_record = _QuarantineTombstone(
                    path=quarantine_path,
                    source_path=target,
                    identity=expected_identity,
                    state="quarantined_bound",
                    path_verification="quarantine_bound_registration_retired",
                    registration=registration,
                )
                if not persist_tombstone(registration_record):
                    raise _CleanupClaimLost("cleanup_claim_lost_after_registration_retirement")
                tombstone = _retain_bound_directory_tombstone(
                    parent_fd,
                    quarantine_name,
                    path=quarantine_path,
                    source_path=target,
                    expected_identity=expected_identity,
                    registration=registration,
                )
                if not persist_tombstone(tombstone):
                    raise _CleanupClaimLost("cleanup_claim_lost_before_tombstone_journal")
                # The durable journal identifies a descriptor-bound retained
                # inode, not an assertion that it will remain empty.  Never
                # restore, unlink, prune, or delete from here.
                try:
                    current_identity = _directory_entry_identity(
                        parent_fd,
                        quarantine_name,
                    )
                except (OSError, PermissionError):
                    path_verification = "missing_or_non_directory_after_persist"
                else:
                    path_verification = (
                        "matched_bound_identity_after_persist"
                        if current_identity == expected_identity
                        else "identity_mismatch_after_persist"
                    )
        except (OSError, RuntimeError):
            raise
        return _QuarantineTombstone(
            path=quarantine_path,
            source_path=target,
            identity=expected_identity,
            state="bound_retained_at_persist",
            path_verification=path_verification,
            registration=registration,
        )
    finally:
        if bound_worktree_fd >= 0:
            os.close(bound_worktree_fd)
        for fd in reversed(opened):
            os.close(fd)
        os.close(root_fd)


def _ref_transaction_command(
    process: subprocess.Popen[str],
    commands: str,
    *,
    expected_response: str,
) -> None:
    if process.stdin is None or process.stdout is None:
        raise RuntimeError("git_ref_guard_pipes_unavailable")
    process.stdin.write(commands)
    if not commands.endswith("\n"):
        process.stdin.write("\n")
    process.stdin.flush()
    ready, _, _ = select.select([process.stdout], [], [], 120)
    if not ready:
        raise RuntimeError("git_ref_guard_response_timeout")
    response = process.stdout.readline().strip()
    if response != expected_response:
        detail = ""
        if process.poll() is not None and process.stderr is not None:
            detail = process.stderr.read().strip()
        raise RuntimeError(
            f"git_ref_guard_failed:{expected_response}:{response or detail or 'unknown'}"
        )


@contextmanager
def _hold_expected_branch_ref(
    root: Path,
    *,
    branch: str,
    expected_commit: str,
    expected_head: str | None = None,
) -> Iterator[None]:
    """Hold Git's ref lock across the durable implementation finalization."""

    ref = f"refs/heads/{branch}"
    process: subprocess.Popen[str]
    with subprocess.Popen(  # noqa: S603 - fixed local git command
        ["git", "-C", str(root), "update-ref", "--stdin"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
    ) as process:
        prepared = False
        try:
            _ref_transaction_command(
                process,
                "start",
                expected_response="start: ok",
            )
            verify_commands = f"verify {ref} {expected_commit}"
            if expected_head is not None:
                verify_commands += f"\nverify HEAD {expected_head}"
            _ref_transaction_command(
                process,
                f"{verify_commands}\nprepare",
                expected_response="prepare: ok",
            )
            prepared = True
            try:
                yield
            except BaseException:
                if process.poll() is None:
                    try:
                        _ref_transaction_command(
                            process,
                            "abort",
                            expected_response="abort: ok",
                        )
                        prepared = False
                    except (BrokenPipeError, OSError, RuntimeError):
                        pass
                raise
            else:
                _ref_transaction_command(
                    process,
                    "commit",
                    expected_response="commit: ok",
                )
                prepared = False
        finally:
            if process.stdin is not None and not process.stdin.closed:
                process.stdin.close()
            if prepared and sys.exc_info()[0] is None:
                raise RuntimeError("git_ref_guard_released_without_commit")
    if process.returncode != 0:
        raise RuntimeError(f"git_ref_guard_exit_failed:{process.returncode}")


@contextmanager
def _mutation_fence(root: Path) -> Iterator[None]:
    """Serialize CSI git/worktree mutation through the repository common dir."""

    common = _git_common_directory(root)
    lock_path = common / "csi-mutation.lock"
    flags = os.O_RDWR | os.O_CREAT | _O_NOFOLLOW
    fd = os.open(lock_path, flags, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


def _atomic_rename_at(
    dir_fd: int,
    source: str,
    destination: str,
    *,
    exchange: bool,
    destination_dir_fd: int | None = None,
) -> None:
    """Rename without overwrite or atomically exchange two directory entries."""

    target_dir_fd = dir_fd if destination_dir_fd is None else destination_dir_fd
    libc = ctypes.CDLL(None, use_errno=True)
    source_b = os.fsencode(source)
    destination_b = os.fsencode(destination)
    if sys.platform == "darwin":
        try:
            renameatx = libc.renameatx_np
        except AttributeError as exc:
            raise RuntimeError("atomic_rename_primitives_unavailable") from exc
        renameatx.argtypes = [
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_uint,
        ]
        renameatx.restype = ctypes.c_int
        flag = _RENAME_SWAP if exchange else _DARWIN_RENAME_EXCL
        rc = renameatx(dir_fd, source_b, target_dir_fd, destination_b, flag)
    else:
        try:
            renameat2 = libc.renameat2
        except AttributeError as exc:
            raise RuntimeError("atomic_rename_primitives_unavailable") from exc
        renameat2.argtypes = [
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_uint,
        ]
        renameat2.restype = ctypes.c_int
        flag = _RENAME_SWAP if exchange else _RENAME_NOREPLACE
        rc = renameat2(dir_fd, source_b, target_dir_fd, destination_b, flag)
    if rc != 0:
        error_number = ctypes.get_errno()
        raise OSError(error_number, os.strerror(error_number), destination)


def _snapshot_entry(dir_fd: int, name: str) -> _DestinationSnapshot:
    try:
        info = os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
    except FileNotFoundError:
        return _DestinationSnapshot(exists=False)
    if not stat.S_ISREG(info.st_mode):
        raise PermissionError(f"non_regular_destination_refused:{name}")
    read_fd = os.open(name, os.O_RDONLY | _O_NOFOLLOW, dir_fd=dir_fd)
    digest = hashlib.sha256()
    try:
        while True:
            chunk = os.read(read_fd, 1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    finally:
        os.close(read_fd)
    return _DestinationSnapshot(
        exists=True,
        device=info.st_dev,
        inode=info.st_ino,
        mode=info.st_mode,
        size=info.st_size,
        modified_ns=info.st_mtime_ns,
        digest=digest.hexdigest(),
    )


def _directory_matches_path(root: Path, parent_parts: tuple[str, ...], held_fd: int) -> bool:
    """Prove a held parent descriptor is still the canonical in-root directory."""

    flags = os.O_RDONLY | _O_DIRECTORY | _O_NOFOLLOW
    root_fd = os.open(root.resolve(strict=True), flags)
    current_fd = root_fd
    opened: list[int] = []
    try:
        for part in parent_parts:
            child_fd = os.open(part, flags, dir_fd=current_fd)
            opened.append(child_fd)
            current_fd = child_fd
        held = os.fstat(held_fd)
        current = os.fstat(current_fd)
        return held.st_dev == current.st_dev and held.st_ino == current.st_ino
    except OSError:
        return False
    finally:
        for fd in reversed(opened):
            os.close(fd)
        os.close(root_fd)


def _remove_and_verify(dir_fd: int, name: str) -> None:
    os.unlink(name, dir_fd=dir_fd)
    try:
        os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
    except FileNotFoundError:
        return
    raise RuntimeError(f"containment_rollback_failed:{name}")


def _expected_locations(root: Path, run_id: str) -> tuple[str, Path, Path]:
    if not _SAFE_RUN_ID.fullmatch(run_id) or run_id in {".", ".."} or ".." in run_id:
        raise PermissionError(f"unsafe_run_id:{run_id!r}")
    branch = f"csi/{run_id[:16]}"
    wt_base = root / "var" / "csi" / "worktrees"
    wt_root = wt_base / run_id
    return branch, wt_base, wt_root


def _assert_git_root(root: Path) -> None:
    try:
        top = Path(_git_text(root, "rev-parse", "--show-toplevel")).resolve()
    except (OSError, subprocess.SubprocessError) as exc:
        raise RuntimeError(f"repo_root_not_git_repository:{exc}") from exc
    # Safety (`is not True`): reject unless Git positively identifies this root.
    if inode_paths_equal(top, root) is not True:
        raise RuntimeError(f"repo_root_not_git_toplevel:{top}")


def _current_sha(root: Path) -> str:
    try:
        sha = _git_text(root, "rev-parse", "HEAD")
    except (OSError, subprocess.SubprocessError) as exc:
        raise RuntimeError(f"codebase_sha_unavailable:{exc}") from exc
    if not re.fullmatch(r"[0-9a-fA-F]{40}", sha):
        raise RuntimeError(f"codebase_sha_invalid:{sha!r}")
    return sha.lower()


def _parse_json_dict(raw: object, field_name: str) -> dict[str, Any]:
    try:
        value = json.loads(str(raw or "{}"))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"invalid_{field_name}") from exc
    if not isinstance(value, dict):
        raise ValueError(f"invalid_{field_name}")
    return value


def _implementation_meta(run: dict[str, Any]) -> dict[str, Any]:
    return _parse_json_dict(run.get("implement_json"), "implement_json")


def _proposals(run: dict[str, Any]) -> list[PlannerProposal]:
    synthesis = _parse_json_dict(run.get("synthesis_json"), "synthesis_json")
    raw_proposals = synthesis.get("accepted_proposals")
    if not isinstance(raw_proposals, list) or not raw_proposals:
        raise ValueError("empty_proposals")
    proposals: list[PlannerProposal] = []
    for raw in raw_proposals:
        try:
            proposal = PlannerProposal.model_validate(raw)
        except Exception as exc:  # noqa: BLE001
            raise ValueError(f"invalid_proposal:{exc}") from exc
        if not proposal.affected_paths:
            raise ValueError("empty_affected_paths")
        proposals.append(proposal)
    return proposals


def _validate_approval_binding(run: dict[str, Any]) -> str | None:
    try:
        meta = _implementation_meta(run)
        recorded = meta.get("approval_binding")
        if not isinstance(recorded, dict):
            return "approval_binding_missing"
        current = approval_binding(run)
    except ValueError as exc:
        return str(exc)
    labels = {
        "codebase_sha": "approval_codebase_sha_changed",
        "evidence_sha256": "approval_evidence_changed",
        "synthesis_sha256": "approval_synthesis_changed",
        "conflict_sha256": "approval_conflict_changed",
    }
    for key, error in labels.items():
        if str(recorded.get(key) or "") != current[key]:
            return error
    return None


def _secure_ensure_dir(root: Path, relative_parts: tuple[str, ...]) -> Path:
    """Create directories below ``root`` without following symlink components."""

    if not _O_DIRECTORY or not _O_NOFOLLOW:
        raise RuntimeError("secure_nofollow_directory_operations_unavailable")
    root = root.resolve(strict=True)
    flags = os.O_RDONLY | _O_DIRECTORY
    root_fd = os.open(root, flags | _O_NOFOLLOW)
    current_fd = root_fd
    opened: list[int] = []
    try:
        built: list[str] = []
        for part in relative_parts:
            if not part or part in {".", ".."} or "/" in part or "\\" in part:
                raise PermissionError(f"unsafe_directory_component:{part!r}")
            built.append(part)
            candidate = root.joinpath(*built)
            candidate_real = candidate.resolve(strict=False)
            if inode_relative_parts_anchored(candidate_real, root) is None:
                raise PermissionError(f"directory containment escape:{candidate}")
            try:
                os.mkdir(part, mode=0o755, dir_fd=current_fd)
            except FileExistsError:
                pass
            child_fd = os.open(
                part,
                flags | _O_NOFOLLOW,
                dir_fd=current_fd,
            )
            opened.append(child_fd)
            current_fd = child_fd
    finally:
        for fd in reversed(opened):
            os.close(fd)
        os.close(root_fd)
    return root.joinpath(*relative_parts)


def _secure_write_text(
    root: Path,
    relative_path: str,
    body: str,
    *,
    expected_snapshot: _DestinationSnapshot | None = None,
    pre_publish: Callable[[], None] | None = None,
) -> _DestinationSnapshot:
    """Publish below ``root/vault`` with no-follow walking and atomic CAS.

    Existing files are exchanged with the staged file so the exact displaced
    inode can be compared with the reviewer snapshot.  A mismatch is swapped
    back before returning an error.  New files use no-replace rename semantics.
    The held parent descriptor is also compared with the canonical path after
    publication; a renamed parent is rolled back through that descriptor.
    """

    if not _O_DIRECTORY or not _O_NOFOLLOW:
        raise RuntimeError("secure_nofollow_directory_operations_unavailable")
    root = root.resolve(strict=True)
    rel, _ = assert_canonical_destination(relative_path, root=root)
    parts = tuple(rel.split("/"))
    flags = os.O_RDONLY | _O_DIRECTORY
    root_fd = os.open(root, flags | _O_NOFOLLOW)
    current_fd = root_fd
    opened: list[int] = []
    temp_name = f".csi-{uuid.uuid4().hex}.tmp"
    temp_created = False
    try:
        built: list[str] = []
        for part in parts[:-1]:
            built.append(part)
            assert_canonical_destination(
                "/".join((*built, parts[-1])),
                root=root,
            )
            try:
                os.mkdir(part, mode=0o755, dir_fd=current_fd)
            except FileExistsError:
                pass
            try:
                child_fd = os.open(
                    part,
                    flags | _O_NOFOLLOW,
                    dir_fd=current_fd,
                )
            except OSError as exc:
                if exc.errno in {errno.ELOOP, errno.ENOTDIR}:
                    raise PermissionError(f"symlink component refused:{'/'.join(built)}") from exc
                raise
            opened.append(child_fd)
            current_fd = child_fd

        observed = _snapshot_entry(current_fd, parts[-1])
        expected = expected_snapshot if expected_snapshot is not None else observed
        if observed != expected:
            raise RuntimeError(f"reviewer_edits_present:{rel}")

        write_flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | _O_NOFOLLOW
        temp_fd = os.open(
            temp_name,
            write_flags,
            0o644,
            dir_fd=current_fd,
        )
        temp_created = True
        try:
            payload = body.encode("utf-8")
            offset = 0
            while offset < len(payload):
                written_now = os.write(temp_fd, payload[offset:])
                if written_now <= 0:
                    raise OSError("secure write made no progress")
                offset += written_now
            os.fsync(temp_fd)
        finally:
            os.close(temp_fd)

        if pre_publish is not None:
            pre_publish()
        assert_canonical_destination(rel, root=root)
        if not _directory_matches_path(root, parts[:-1], current_fd):
            raise PermissionError(f"symlink or renamed directory component refused:{rel}")
        if _snapshot_entry(current_fd, parts[-1]) != expected:
            raise RuntimeError(f"reviewer_edits_present:{rel}")

        exchanged = expected.exists
        try:
            _atomic_rename_at(
                current_fd,
                temp_name,
                parts[-1],
                exchange=exchanged,
            )
        except OSError as exc:
            if not exchanged and exc.errno in {errno.EEXIST, errno.ENOTEMPTY}:
                raise RuntimeError(f"reviewer_edits_present:{rel}") from exc
            raise
        if not exchanged:
            temp_created = False
        os.fsync(current_fd)

        containment_ok = _directory_matches_path(root, parts[:-1], current_fd)
        try:
            assert_canonical_destination(rel, root=root)
        except (OSError, PermissionError):
            containment_ok = False
        if not containment_ok:
            if exchanged:
                displaced = _snapshot_entry(current_fd, temp_name)
                _atomic_rename_at(
                    current_fd,
                    temp_name,
                    parts[-1],
                    exchange=True,
                )
                if _snapshot_entry(current_fd, parts[-1]) != displaced:
                    raise RuntimeError(f"containment_rollback_failed:{rel}")
                _remove_and_verify(current_fd, temp_name)
                temp_created = False
            else:
                _remove_and_verify(current_fd, parts[-1])
            os.fsync(current_fd)
            raise PermissionError(f"symlink or renamed directory component refused:{rel}")

        if exchanged:
            displaced = _snapshot_entry(current_fd, temp_name)
            if displaced != expected:
                _atomic_rename_at(
                    current_fd,
                    temp_name,
                    parts[-1],
                    exchange=True,
                )
                if _snapshot_entry(current_fd, parts[-1]) != displaced:
                    raise RuntimeError(f"reviewer_rollback_failed:{rel}")
                _remove_and_verify(current_fd, temp_name)
                temp_created = False
                os.fsync(current_fd)
                raise RuntimeError(f"reviewer_edits_present:{rel}")
            _remove_and_verify(current_fd, temp_name)
            temp_created = False
            os.fsync(current_fd)
        return _snapshot_entry(current_fd, parts[-1])
    finally:
        if temp_created:
            try:
                os.unlink(temp_name, dir_fd=current_fd)
            except FileNotFoundError:
                pass
        for fd in reversed(opened):
            os.close(fd)
        os.close(root_fd)


def _snapshot_destination(root: Path, relative_path: str) -> _DestinationSnapshot:
    rel, _ = assert_canonical_destination(relative_path, root=root)
    parts = tuple(rel.split("/"))
    flags = os.O_RDONLY | _O_DIRECTORY | _O_NOFOLLOW
    root_fd = os.open(root.resolve(strict=True), flags)
    current_fd = root_fd
    opened: list[int] = []
    try:
        for part in parts[:-1]:
            try:
                child_fd = os.open(part, flags, dir_fd=current_fd)
            except FileNotFoundError:
                return _DestinationSnapshot(exists=False)
            opened.append(child_fd)
            current_fd = child_fd
        return _snapshot_entry(current_fd, parts[-1])
    finally:
        for fd in reversed(opened):
            os.close(fd)
        os.close(root_fd)


def _worktree_entries(root: Path) -> list[dict[str, str]]:
    proc = _git(root, "worktree", "list", "--porcelain", check=False)
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "unknown")[:240]
        raise RuntimeError(f"worktree_observation_failed:{detail}")
    entries: list[dict[str, str]] = []
    current: dict[str, str] = {}
    for line in proc.stdout.splitlines():
        if not line:
            if current:
                entries.append(current)
                current = {}
            continue
        key, _, value = line.partition(" ")
        current[key] = value
    if current:
        entries.append(current)
    return entries


def _branch_worktree(root: Path, branch: str) -> Path | None:
    target = f"refs/heads/{branch}"
    for entry in _worktree_entries(root):
        if entry.get("branch") == target and entry.get("worktree"):
            return _lexical_absolute_path(
                entry["worktree"],
                label="registered_worktree_path",
            )
    return None


def _branch_exists(root: Path, branch: str) -> bool:
    proc = _git(
        root,
        "show-ref",
        "--verify",
        "--quiet",
        f"refs/heads/{branch}",
        check=False,
    )
    return proc.returncode == 0


def _worktree_clean_at(wt_root: Path, *, branch: str, sha: str) -> tuple[bool, str]:
    try:
        actual_branch = _git_text(wt_root, "symbolic-ref", "--short", "HEAD")
        actual_sha = _git_text(wt_root, "rev-parse", "HEAD").lower()
        status = _git_text(
            wt_root,
            "status",
            "--porcelain=v1",
            "--untracked-files=all",
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return False, f"reviewer_state_observation_failed:{exc}"
    if actual_branch != branch:
        return False, f"unexpected_worktree_branch:{actual_branch}"
    if actual_sha != sha.lower():
        return False, f"reviewer_branch_sha_changed:{actual_sha}"
    if status:
        return False, "reviewer_edits_present"
    return True, ""


def _target_has_reviewer_edits(wt_root: Path, relative_path: str) -> bool:
    proc = _git(
        wt_root,
        "status",
        "--porcelain=v1",
        "-z",
        "--untracked-files=all",
        "--",
        relative_path,
        check=False,
    )
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "unknown")[:200]
        raise RuntimeError(f"reviewer_target_observation_failed:{detail}")
    return bool(proc.stdout)


def _render_note(
    *,
    run_id: str,
    approved_by: str,
    proposal: PlannerProposal,
) -> str:
    return (
        f"# CSI Self-Learning note\n\n"
        f"Generated for run `{run_id}` (approved by {approved_by}).\n\n"
        f"## Change\n\n{proposal.change}\n\n"
        f"## Baseline\n\n{proposal.baseline}\n\n"
        f"## Target\n\n{proposal.target}\n\n"
        f"## Measured by\n\n{proposal.measured_by}\n\n"
        f"## Rollback\n\n{proposal.rollback}\n\n"
        f"_Do not merge without human review._\n"
    )


def _validate_fresh_run(
    store: CsiStore,
    *,
    run_id: str,
    expected_status: str,
    observed: dict[str, Any],
) -> tuple[dict[str, Any] | None, str | None]:
    fresh = store.get_run(run_id)
    if not fresh:
        return None, "run_disappeared"
    if str(fresh.get("status") or "") != expected_status:
        return fresh, "run_state_changed"
    if str(fresh.get("approval_status") or "") != "approved":
        return fresh, "approval_revoked"
    for field_name in (
        "approved_by",
        "approved_at",
        "codebase_sha",
        "evidence_json",
        "synthesis_json",
        "conflict_json",
        "implement_json",
    ):
        if fresh.get(field_name) != observed.get(field_name):
            return fresh, f"{field_name}_changed"
    binding_error = _validate_approval_binding(fresh)
    if binding_error:
        return fresh, binding_error
    return fresh, None


def _revalidate_claimed_boundary(
    store: CsiStore,
    *,
    run_id: str,
    observed: dict[str, Any],
    root: Path,
    base_sha: str,
    proposals: list[PlannerProposal],
    own_worktree: Path | None = None,
) -> dict[str, Any]:
    current = store.get_run(run_id)
    if not current or str(current.get("status") or "") != "IMPLEMENTING":
        raise RuntimeError("run_state_changed_before_mutation")
    if str(current.get("approval_status") or "") != "approved":
        raise RuntimeError("approval_revoked_before_mutation")
    binding_error = _validate_approval_binding(current)
    if binding_error:
        raise RuntimeError(binding_error)
    for field_name in (
        "approved_by",
        "approved_at",
        "codebase_sha",
        "evidence_json",
        "synthesis_json",
        "conflict_json",
        "implement_json",
    ):
        if current.get(field_name) != observed.get(field_name):
            raise RuntimeError(f"{field_name}_changed_before_mutation")
    if _current_sha(root) != base_sha:
        raise RuntimeError("codebase_sha_changed_before_mutation")
    ignored = (str(own_worktree),) if own_worktree is not None else ()
    conflict = ConflictForecastService(repo_root=root).forecast(
        proposals,
        base_sha=base_sha,
        ignore_paths=ignored,
    )
    if not conflict.safe_to_implement:
        raise RuntimeError("conflict_changed_before_write:" + ";".join(conflict.reasons))
    return current


def _nul_paths(proc: subprocess.CompletedProcess[str], *, error_prefix: str) -> set[str]:
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "unknown")[:300]
        raise RuntimeError(f"{error_prefix}:{detail}")
    return {item for item in proc.stdout.split("\0") if item}


def _verify_tree_payloads(
    wt_root: Path,
    *,
    tree: str,
    base_sha: str,
    written: set[str],
    generated_bodies: dict[str, str],
    label: str,
) -> dict[str, str]:
    changed = _nul_paths(
        _git(
            wt_root,
            "diff",
            "--name-only",
            "-z",
            base_sha,
            tree,
            check=False,
        ),
        error_prefix=f"{label}_diff_observation_failed",
    )
    if changed != written:
        raise RuntimeError(
            f"{label}_scope_mismatch:" + ",".join(sorted(changed.symmetric_difference(written)))
        )

    payload_hashes: dict[str, str] = {}
    for rel, expected_body in generated_bodies.items():
        blob = _git(wt_root, "show", f"{tree}:{rel}", check=False)
        if blob.returncode != 0 or blob.stdout != expected_body:
            raise RuntimeError(f"{label}_content_mismatch:{rel}")
        payload_hashes[rel] = hashlib.sha256(expected_body.encode("utf-8")).hexdigest()
    return payload_hashes


def _verify_commit_provenance(
    wt_root: Path,
    *,
    commit: str,
    expected_tree: str,
    base_sha: str,
    written: set[str],
    generated_bodies: dict[str, str],
) -> dict[str, str]:
    commit_tree = _git_text(wt_root, "rev-parse", f"{commit}^{{tree}}").lower()
    if commit_tree != expected_tree.lower():
        raise RuntimeError(f"committed_tree_mismatch:expected={expected_tree}:actual={commit_tree}")
    lineage = _git_text(wt_root, "rev-list", "--parents", "-n", "1", commit).split()
    if lineage != [commit, base_sha]:
        raise RuntimeError("implementation_commit_lineage_mismatch")
    return _verify_tree_payloads(
        wt_root,
        tree=commit,
        base_sha=base_sha,
        written=written,
        generated_bodies=generated_bodies,
        label="committed",
    )


def _verify_stored_implementation_provenance(
    root: Path,
    metadata: dict[str, Any],
) -> None:
    """Verify every immutable Git object and payload recorded as merge-ready."""

    commit = str(metadata.get("implementation_commit") or "").lower()
    tree = str(metadata.get("implementation_tree") or "").lower()
    parent = str(metadata.get("implementation_parent") or "").lower()
    base_sha = str(metadata.get("base_sha") or "").lower()
    if not all(re.fullmatch(r"[0-9a-f]{40}", value) for value in (commit, tree, parent, base_sha)):
        raise RuntimeError("implementation_provenance_sha_invalid")
    if parent != base_sha:
        raise RuntimeError("implementation_parent_base_mismatch")

    raw_written = metadata.get("written_paths")
    raw_hashes = metadata.get("committed_payload_sha256")
    if not isinstance(raw_written, list) or not isinstance(raw_hashes, dict):
        raise RuntimeError("implementation_payload_metadata_invalid")
    written = [repo_relative(str(path), root) for path in raw_written]
    if len(written) != len(set(written)):
        raise RuntimeError("implementation_written_paths_duplicated")
    hashes = {str(path): str(digest).lower() for path, digest in raw_hashes.items()}
    if set(written) != set(hashes):
        raise RuntimeError("implementation_payload_hash_scope_mismatch")
    if not all(re.fullmatch(r"[0-9a-f]{64}", digest) for digest in hashes.values()):
        raise RuntimeError("implementation_payload_hash_invalid")

    commit_tree = _git_text(root, "rev-parse", f"{commit}^{{tree}}").lower()
    if commit_tree != tree:
        raise RuntimeError("implementation_stored_tree_mismatch")
    lineage = _git_text(root, "rev-list", "--parents", "-n", "1", commit).split()
    if lineage != [commit, parent]:
        raise RuntimeError("implementation_stored_lineage_mismatch")
    changed = _nul_paths(
        _git(
            root,
            "diff",
            "--name-only",
            "-z",
            parent,
            commit,
            check=False,
        ),
        error_prefix="implementation_stored_scope_observation_failed",
    )
    if changed != set(written):
        raise RuntimeError("implementation_stored_scope_mismatch")
    for rel in written:
        blob = _git(root, "show", f"{commit}:{rel}", check=False)
        if blob.returncode != 0:
            raise RuntimeError(f"implementation_stored_payload_missing:{rel}")
        digest = hashlib.sha256(blob.stdout.encode("utf-8")).hexdigest()
        if digest != hashes[rel]:
            raise RuntimeError(f"implementation_stored_payload_mismatch:{rel}")


def _force_non_merge_ready(
    store: CsiStore,
    *,
    run_id: str,
    observed_implement_json: str,
    error: str,
) -> None:
    """CAS a failed readiness proof against the exact observed provenance."""

    try:
        store.invalidate_finalized_implementation(
            run_id,
            observed_implement_json=observed_implement_json,
            error=error,
        )
    except sqlite3.Error:
        LOG.exception("primary CSI merge-readiness invalidation failed")
        try:
            store.rollback()
        except sqlite3.Error:
            pass
    current = store.get_run(run_id)
    if current and str(current.get("status") or "") == "AWAITING_MERGE":
        store.force_implementation_incident(
            run_id,
            observed_implement_json=observed_implement_json,
            error=error,
        )
        current = store.get_run(run_id)
    if current and str(current.get("status") or "") == "AWAITING_MERGE":
        raise RuntimeError("implementation_non_merge_ready_transition_failed")


def _validate_merge_readiness(
    *,
    store: CsiStore,
    run_id: str,
    root: Path,
    expected_branch: str,
) -> tuple[dict[str, Any] | None, str | None]:
    """CAS-lock the branch while proving the exact durable readiness record."""

    row = store.get_run(run_id)
    if not row:
        return None, "unknown_run"
    if str(row.get("status") or "") != "AWAITING_MERGE":
        return row, f"merge_readiness_state_changed:{row.get('status') or ''}"
    observed_json = str(row.get("implement_json") or "{}")
    metadata: dict[str, Any] = {}
    try:
        metadata = _implementation_meta(row)
        if metadata.get("approval_binding") != approval_binding(row):
            raise RuntimeError("implementation_approval_binding_changed")
        branch = str(metadata.get("branch") or "")
        if branch != expected_branch:
            raise RuntimeError("implementation_branch_metadata_mismatch")
        _, _, expected_worktree = _expected_locations(root, run_id)
        metadata_worktree = _lexical_absolute_path(
            str(metadata.get("worktree") or ""),
            label="implementation_worktree_metadata",
        )
        # Safety (`is not True`): reject unless metadata names the expected worktree inode.
        if inode_paths_equal(metadata_worktree, expected_worktree) is not True:
            raise RuntimeError("implementation_worktree_metadata_mismatch")
        expected_commit = str(metadata.get("implementation_commit") or "").lower()
        if re.fullmatch(r"[0-9a-f]{40}", expected_commit) is None:
            raise RuntimeError("implementation_commit_metadata_invalid")
        with _hold_expected_branch_ref(
            root,
            branch=expected_branch,
            expected_commit=expected_commit,
        ):
            _verify_stored_implementation_provenance(root, metadata)
            if not store.merge_readiness_is_current(
                run_id,
                observed_implement_json=observed_json,
            ):
                raise RuntimeError("implementation_readiness_metadata_changed")
    except (
        OSError,
        PermissionError,
        RuntimeError,
        sqlite3.Error,
        subprocess.SubprocessError,
    ) as exc:
        error = f"implementation_merge_readiness_invalid:{exc}"
        try:
            _force_non_merge_ready(
                store,
                run_id=run_id,
                observed_implement_json=observed_json,
                error=error,
            )
        except RuntimeError as transition_exc:
            error = f"{error}:{transition_exc}"
        return store.get_run(run_id), error
    return row, None


def _transition_validated_merge_ready_status(
    *,
    store: CsiStore,
    run_id: str,
    root: Path,
    observed_implement_json: str,
    status: str,
    error: str | None,
) -> bool:
    """Exit merge readiness while its exact approved branch ref is locked."""

    if status not in {"MERGED", "REJECTED", "CANCELLED", "QUARANTINED", "INCIDENT"}:
        return False
    row = store._approved_merge_ready_observation(  # noqa: SLF001
        run_id,
        observed_implement_json=observed_implement_json,
    )
    if row is None:
        return False
    try:
        _assert_git_root(root)
        metadata = _implementation_meta(row)
        if metadata.get("approval_binding") != approval_binding(row):
            raise RuntimeError("implementation_approval_binding_changed")
        expected_branch, _, expected_worktree = _expected_locations(root, run_id)
        if str(metadata.get("branch") or "") != expected_branch:
            raise RuntimeError("implementation_branch_metadata_mismatch")
        metadata_worktree = _lexical_absolute_path(
            str(metadata.get("worktree") or ""),
            label="implementation_worktree_metadata",
        )
        # Safety (`is not True`): reject unless metadata names the expected worktree inode.
        if inode_paths_equal(metadata_worktree, expected_worktree) is not True:
            raise RuntimeError("implementation_worktree_metadata_mismatch")
        expected_commit = str(metadata.get("implementation_commit") or "").lower()
        if re.fullmatch(r"[0-9a-f]{40}", expected_commit) is None:
            raise RuntimeError("implementation_commit_metadata_invalid")
        expected_head = _current_sha(root) if status == "MERGED" else None
        with _hold_expected_branch_ref(
            root,
            branch=expected_branch,
            expected_commit=expected_commit,
            expected_head=expected_head,
        ):
            _verify_stored_implementation_provenance(root, metadata)
            if status == "MERGED":
                merged = _git(
                    root,
                    "merge-base",
                    "--is-ancestor",
                    expected_commit,
                    "HEAD",
                    check=False,
                )
                if merged.returncode != 0:
                    return False
            fresh = store._approved_merge_ready_observation(  # noqa: SLF001
                run_id,
                observed_implement_json=observed_implement_json,
            )
            if fresh is None:
                return False
            return store._cas_approved_merge_ready_status(  # noqa: SLF001
                fresh,
                status=status,
                error=error,
            )
    except (
        OSError,
        PermissionError,
        RuntimeError,
        sqlite3.Error,
        subprocess.SubprocessError,
    ) as exc:
        try:
            _force_non_merge_ready(
                store,
                run_id=run_id,
                observed_implement_json=observed_implement_json,
                error=f"implementation_merge_transition_invalid:{exc}",
            )
        except RuntimeError:
            LOG.exception("failed to fence invalid CSI merge transition")
        return False


def _cleanup_finalization_matches_repository(
    *,
    root: Path,
    run_id: str,
    metadata: dict[str, Any],
) -> bool:
    """Prove a cleanup result agrees with the exact current Git/filesystem state."""

    try:
        _assert_git_root(root)
        expected_branch, _, expected_wt = _expected_locations(root, run_id)
        if str(metadata.get("branch") or "") != expected_branch:
            return False
        metadata_wt = _lexical_absolute_path(
            str(metadata.get("worktree") or ""),
            label="implementation_worktree_metadata",
        )
        # Safety (`is not True`): metadata worktree must match expected by inode.
        if inode_paths_equal(metadata_wt, expected_wt) is not True:
            return False
        # A typed cleanup may finalize only after the linked registration and
        # canonical worktree pathname are both absent. This turns the filesystem
        # mutation into an independently re-observed precondition of the DB CAS.
        if _branch_worktree(root, expected_branch) is not None:
            return False
        if expected_wt.exists() or expected_wt.is_symlink():
            return False

        action = str(metadata.get("cleanup_action") or "")
        action_parts = set(action.split(","))
        retained_reason = str(metadata.get("cleanup_retained_reason") or "")
        journal = metadata.get("cleanup_quarantine")
        if journal is not None:
            if not isinstance(journal, dict):
                return False
            if not {
                "worktree_quarantined",
                "worktree_registration_retired",
            }.issubset(action_parts):
                return False
            observation = _observe_quarantine_path(
                journal,
                expected_worktree=expected_wt,
            )
            if "matched_bound_identity_" not in observation:
                return False
            registration = _registration_from_journal(
                journal,
                root=root,
                expected_worktree=expected_wt,
                expected_branch=expected_branch,
            )
            if registration is None or registration.state != "retired_bound":
                return False
            flags = os.O_RDONLY | _O_DIRECTORY | _O_NOFOLLOW
            retired_fd = os.open(registration.retired_path, flags)
            try:
                retired_info = os.fstat(retired_fd)
            finally:
                os.close(retired_fd)
            if (
                retired_info.st_dev != registration.identity.device
                or retired_info.st_ino != registration.identity.inode
            ):
                return False
            if retained_reason != "worktree_quarantine_retained_for_safe_unlink":
                return False
        elif "worktree_quarantined" in action_parts:
            return False

        branch_exists = _branch_exists(root, expected_branch)
        if journal is not None:
            if not branch_exists or "merged_branch_deleted" in action_parts:
                return False
        elif branch_exists:
            if (
                retained_reason
                not in {
                    "safe_branch_delete_failed",
                    "unmerged_branch_retained_for_recovery",
                }
                or "merged_branch_deleted" in action_parts
            ):
                return False
        elif retained_reason or action_parts not in (
            {"nothing_to_clean"},
            {"merged_branch_deleted"},
        ):
            return False
    except (
        FileNotFoundError,
        OSError,
        PermissionError,
        RuntimeError,
        subprocess.SubprocessError,
    ):
        return False
    return True


def _idempotent_result(
    *,
    store: CsiStore,
    run: dict[str, Any],
    root: Path,
    run_id: str,
) -> ImplementResult:
    try:
        expected_branch, _, expected_wt = _expected_locations(root, run_id)
    except PermissionError as exc:
        return ImplementResult(
            ok=False,
            run_id=run_id,
            status=str(run.get("status") or ""),
            error=str(exc),
        )
    validated_row, readiness_error = _validate_merge_readiness(
        store=store,
        run_id=run_id,
        root=root,
        expected_branch=expected_branch,
    )
    if readiness_error:
        return ImplementResult(
            ok=False,
            run_id=run_id,
            status=str((validated_row or {}).get("status") or "INCIDENT"),
            error=readiness_error,
        )
    run = validated_row or run
    try:
        meta = _implementation_meta(run)
    except ValueError as exc:
        _force_non_merge_ready(
            store,
            run_id=run_id,
            observed_implement_json=str(run.get("implement_json") or "{}"),
            error=f"implementation_metadata_invalid:{exc}",
        )
        return ImplementResult(
            ok=False,
            run_id=run_id,
            status=str((store.get_run(run_id) or {}).get("status") or "INCIDENT"),
            error=str(exc),
        )
    branch = str(meta.get("branch") or "")
    worktree = str(meta.get("worktree") or "")
    written = [str(p) for p in (meta.get("written_paths") or [])]
    try:
        metadata_wt = _lexical_absolute_path(
            worktree,
            label="implementation_worktree_metadata",
        )
    except PermissionError as exc:
        return ImplementResult(
            ok=False,
            run_id=run_id,
            status=str(run.get("status") or ""),
            error=str(exc),
        )
    # Safety (`is not True`): branch name is exact; worktree is inode identity.
    if branch != expected_branch or inode_paths_equal(metadata_wt, expected_wt) is not True:
        return ImplementResult(
            ok=False,
            run_id=run_id,
            status=str(run.get("status") or ""),
            error="implementation_metadata_mismatch",
        )
    if not _branch_exists(root, branch):
        return ImplementResult(
            ok=False,
            run_id=run_id,
            branch=branch,
            worktree=worktree,
            written_paths=written,
            status=str(run.get("status") or ""),
            error="implemented_branch_missing",
        )
    try:
        actual_wt = _branch_worktree(root, branch)
    except RuntimeError as exc:
        return ImplementResult(
            ok=False,
            run_id=run_id,
            branch=branch,
            worktree=worktree,
            written_paths=written,
            status=str(run.get("status") or ""),
            error=str(exc),
        )
    # Safety (`is not True`): registered worktree must match expected by inode.
    if actual_wt is not None and inode_paths_equal(actual_wt, expected_wt) is not True:
        return ImplementResult(
            ok=False,
            run_id=run_id,
            branch=branch,
            worktree=worktree,
            written_paths=written,
            status=str(run.get("status") or ""),
            error="registered_worktree_path_mismatch",
        )
    if actual_wt is not None:
        try:
            _worktree_directory_identity(root, actual_wt)
        except FileNotFoundError:
            actual_wt = None
        except (OSError, PermissionError, RuntimeError) as exc:
            return ImplementResult(
                ok=False,
                run_id=run_id,
                branch=branch,
                worktree=worktree,
                written_paths=written,
                status=str(run.get("status") or ""),
                error=str(exc),
            )
    if actual_wt is None:
        recovered = recover_implementation(
            run_id=run_id,
            db_path=store.db_path,
            repo_root=root,
        )
        if not recovered.ok:
            return recovered
        actual_wt = Path(recovered.worktree)
    validated_row, readiness_error = _validate_merge_readiness(
        store=store,
        run_id=run_id,
        root=root,
        expected_branch=expected_branch,
    )
    if readiness_error:
        return ImplementResult(
            ok=False,
            run_id=run_id,
            improvement_id=str(run.get("improvement_id") or ""),
            branch=branch,
            worktree=str(actual_wt),
            written_paths=written,
            status=str((validated_row or {}).get("status") or "INCIDENT"),
            error=readiness_error,
        )
    return ImplementResult(
        ok=True,
        run_id=run_id,
        improvement_id=str(run.get("improvement_id") or ""),
        branch=branch,
        worktree=str(actual_wt),
        written_paths=written,
        status=str(run.get("status") or "AWAITING_MERGE"),
        idempotent=True,
    )


def implement_approved(
    *,
    run_id: str,
    db_path: str | Path,
    improvement_id: str | None = None,
    repo_root: str | Path | None = None,
    approved_by: str = "operator",
) -> ImplementResult:
    """Apply one CSI-native approved run into a worktree. Never merges."""

    db = str(db_path)
    root = Path(repo_root or _ROOT).resolve()
    cfg = load_csi_config()
    store = CsiStore(db)

    if cfg.global_halt:
        return ImplementResult(ok=False, run_id=run_id, error="global_halt", status="CANCELLED")
    try:
        branch, wt_base, wt_root = _expected_locations(root, run_id)
    except PermissionError as exc:
        return ImplementResult(ok=False, run_id=run_id, error=str(exc))

    run_d = store.get_run(run_id)
    if not run_d:
        return ImplementResult(ok=False, run_id=run_id, error="unknown_run")
    run_imp = str(run_d.get("improvement_id") or "")
    effective_approved_by = str(run_d.get("approved_by") or approved_by)
    if improvement_id and run_imp and improvement_id != run_imp:
        return ImplementResult(
            ok=False,
            run_id=run_id,
            improvement_id=improvement_id,
            error="improvement_id_mismatch",
        )

    run_status = str(run_d.get("status") or "")
    if run_status == "AWAITING_MERGE":
        return _idempotent_result(
            store=store,
            run=run_d,
            root=root,
            run_id=run_id,
        )
    if run_status not in {"AWAITING_HUMAN", "DEFERRED"}:
        return ImplementResult(
            ok=False,
            run_id=run_id,
            improvement_id=run_imp,
            error=f"invalid_run_state:{run_status}",
            status=run_status,
        )
    if str(run_d.get("approval_status") or "none") != "approved":
        return ImplementResult(
            ok=False,
            run_id=run_id,
            improvement_id=run_imp,
            error="run_not_approved",
            status=run_status,
        )

    try:
        applies = store.count_applies_today()
    except sqlite3.Error as exc:
        return ImplementResult(
            ok=False,
            run_id=run_id,
            error=f"rate_limit_count_failed:{exc}",
            status="DEFERRED",
        )
    if applies >= int(cfg.max_merges_per_day):
        return ImplementResult(
            ok=False,
            run_id=run_id,
            error=f"max_merges_per_day={cfg.max_merges_per_day} reached",
            status="DEFERRED",
        )

    binding_error = _validate_approval_binding(run_d)
    if binding_error:
        return ImplementResult(
            ok=False,
            run_id=run_id,
            improvement_id=run_imp,
            error=binding_error,
            status=run_status,
        )
    try:
        evidence = _parse_json_dict(run_d.get("evidence_json"), "evidence_json")
        proposals = _proposals(run_d)
        for proposal in proposals:
            for raw in proposal.affected_paths:
                assert_canonical_destination(raw, root=root)
        base_sha = str(run_d.get("codebase_sha") or "").lower()
        if evidence.get("codebase_sha") not in {None, "", base_sha}:
            raise ValueError("evidence_codebase_sha_mismatch")
        _assert_git_root(root)
        current_sha = _current_sha(root)
    except (OSError, PermissionError, RuntimeError, ValueError) as exc:
        return ImplementResult(
            ok=False,
            run_id=run_id,
            improvement_id=run_imp,
            error=str(exc),
            status="REJECTED",
        )
    if current_sha != base_sha:
        return ImplementResult(
            ok=False,
            run_id=run_id,
            improvement_id=run_imp,
            error=f"codebase_sha_changed:planned={base_sha}:current={current_sha}",
            status="DEFERRED",
        )

    try:
        existing_wt = _branch_worktree(root, branch)
    except RuntimeError as exc:
        return ImplementResult(ok=False, run_id=run_id, error=str(exc), status="DEFERRED")
    if existing_wt is not None or _branch_exists(root, branch) or wt_root.exists():
        return ImplementResult(
            ok=False,
            run_id=run_id,
            branch=branch,
            worktree=str(existing_wt or wt_root),
            error="existing_reviewer_state_retained",
            status=run_status,
        )

    conflict = ConflictForecastService(repo_root=root).forecast(
        proposals,
        base_sha=base_sha,
    )
    if not conflict.safe_to_implement:
        return ImplementResult(
            ok=False,
            run_id=run_id,
            error="conflict_revalidation_failed:" + ";".join(conflict.reasons),
            status="DEFERRED",
        )

    try:
        with _mutation_fence(root):
            fresh, fresh_error = _validate_fresh_run(
                store,
                run_id=run_id,
                expected_status=run_status,
                observed=run_d,
            )
            if fresh_error:
                return ImplementResult(
                    ok=False,
                    run_id=run_id,
                    error=fresh_error,
                    status=str((fresh or {}).get("status") or "DEFERRED"),
                )
            if _current_sha(root) != base_sha:
                return ImplementResult(
                    ok=False,
                    run_id=run_id,
                    error="codebase_sha_changed_before_claim",
                    status="DEFERRED",
                )
            conflict = ConflictForecastService(repo_root=root).forecast(
                proposals,
                base_sha=base_sha,
            )
            if not conflict.safe_to_implement:
                return ImplementResult(
                    ok=False,
                    run_id=run_id,
                    error="conflict_changed_before_claim:" + ";".join(conflict.reasons),
                    status="DEFERRED",
                )
            existing_wt = _branch_worktree(root, branch)
            if existing_wt is not None or _branch_exists(root, branch) or wt_root.exists():
                return ImplementResult(
                    ok=False,
                    run_id=run_id,
                    branch=branch,
                    worktree=str(existing_wt or wt_root),
                    error="existing_reviewer_state_retained",
                    status=run_status,
                )
            claimed = store.claim_implementation(
                run_id,
                expected_status=run_status,
                observed=run_d,
            )
    except (OSError, RuntimeError, sqlite3.Error, subprocess.SubprocessError) as exc:
        return ImplementResult(
            ok=False,
            run_id=run_id,
            error=f"implementation_claim_failed:{exc}",
            status="DEFERRED",
        )
    if not claimed:
        return ImplementResult(
            ok=False,
            run_id=run_id,
            error="implementation_claim_lost",
            status="DEFERRED",
        )

    written: list[str] = []
    generated_bodies: dict[str, str] = {}
    generated_snapshots: dict[str, _DestinationSnapshot] = {}
    implementation_commit = ""
    implementation_tree = ""
    staged_index_sha256 = ""
    committed_payload_sha256: dict[str, str] = {}
    worktree_created = False
    try:
        with _mutation_fence(root):
            _revalidate_claimed_boundary(
                store,
                run_id=run_id,
                observed=run_d,
                root=root,
                base_sha=base_sha,
                proposals=proposals,
            )
            existing_wt = _branch_worktree(root, branch)
            if existing_wt is not None or _branch_exists(root, branch) or wt_root.exists():
                raise RuntimeError("existing_reviewer_state_retained")
            _secure_ensure_dir(root, ("var", "csi", "worktrees"))
            wt_parent_real = wt_base.resolve(strict=True)
            if inode_relative_parts_anchored(wt_parent_real, root) is None:
                raise PermissionError(f"worktree_parent_outside_repository:{wt_parent_real}")
            proc = _git(
                root,
                "worktree",
                "add",
                "-b",
                branch,
                str(wt_root),
                base_sha,
                check=False,
            )
            if proc.returncode != 0:
                detail = (proc.stderr or proc.stdout or "unknown")[:400]
                raise RuntimeError(f"worktree_add_failed:{detail}")
            worktree_created = True
            _revalidate_claimed_boundary(
                store,
                run_id=run_id,
                observed=run_d,
                root=root,
                base_sha=base_sha,
                proposals=proposals,
                own_worktree=wt_root,
            )
            clean, state_error = _worktree_clean_at(
                wt_root,
                branch=branch,
                sha=base_sha,
            )
            if not clean:
                raise RuntimeError(state_error)

        for proposal in proposals:
            for raw in proposal.affected_paths:
                rel = repo_relative(raw, root)
                with _mutation_fence(root):
                    _revalidate_claimed_boundary(
                        store,
                        run_id=run_id,
                        observed=run_d,
                        root=root,
                        base_sha=base_sha,
                        proposals=proposals,
                        own_worktree=wt_root,
                    )
                    assert_canonical_destination(rel, root=wt_root)
                    expected_snapshot = _snapshot_destination(wt_root, rel)
                    previous_snapshot = generated_snapshots.get(rel)
                    if previous_snapshot is None:
                        if _target_has_reviewer_edits(wt_root, rel):
                            raise RuntimeError(f"reviewer_edits_present:{rel}")
                    elif expected_snapshot != previous_snapshot:
                        raise RuntimeError(f"reviewer_edits_present:{rel}")
                    body = _render_note(
                        run_id=run_id,
                        approved_by=effective_approved_by,
                        proposal=proposal,
                    )

                    def _verify_publication_boundary() -> None:
                        _revalidate_claimed_boundary(
                            store,
                            run_id=run_id,
                            observed=run_d,
                            root=root,
                            base_sha=base_sha,
                            proposals=proposals,
                            own_worktree=wt_root,
                        )

                    generated_snapshot = _secure_write_text(
                        wt_root,
                        rel,
                        body,
                        expected_snapshot=expected_snapshot,
                        pre_publish=_verify_publication_boundary,
                    )
                    generated_bodies[rel] = body
                    generated_snapshots[rel] = generated_snapshot
                    written.append(rel)

        with _mutation_fence(root):
            current_row = _revalidate_claimed_boundary(
                store,
                run_id=run_id,
                observed=run_d,
                root=root,
                base_sha=base_sha,
                proposals=proposals,
                own_worktree=wt_root,
            )
            for rel, expected_snapshot in generated_snapshots.items():
                if _snapshot_destination(wt_root, rel) != expected_snapshot:
                    raise RuntimeError(f"reviewer_edits_present_before_add:{rel}")
            add = _git(wt_root, "add", "--", *sorted(set(written)), check=False)
            if add.returncode != 0:
                detail = (add.stderr or add.stdout or "unknown")[:300]
                raise RuntimeError(f"git_add_failed:{detail}")
            current_row = _revalidate_claimed_boundary(
                store,
                run_id=run_id,
                observed=run_d,
                root=root,
                base_sha=base_sha,
                proposals=proposals,
                own_worktree=wt_root,
            )
            for rel, expected_snapshot in generated_snapshots.items():
                if _snapshot_destination(wt_root, rel) != expected_snapshot:
                    raise RuntimeError(f"reviewer_edits_present_before_commit:{rel}")
            expected_written = set(written)
            with _bind_staged_index(wt_root) as index_binding:
                staged_index_sha256 = index_binding.sha256
                current_row = _revalidate_claimed_boundary(
                    store,
                    run_id=run_id,
                    observed=run_d,
                    root=root,
                    base_sha=base_sha,
                    proposals=proposals,
                    own_worktree=wt_root,
                )
                for rel, expected_snapshot in generated_snapshots.items():
                    if _snapshot_destination(wt_root, rel) != expected_snapshot:
                        raise RuntimeError(f"reviewer_edits_present_at_commit_boundary:{rel}")

                staged = _nul_paths(
                    _git_with_index(
                        wt_root,
                        index_binding.private_path,
                        "diff",
                        "--cached",
                        "--name-only",
                        "-z",
                        check=False,
                    ),
                    error_prefix="staged_scope_observation_failed",
                )
                if staged != expected_written:
                    raise RuntimeError(
                        "staged_scope_mismatch:"
                        + ",".join(sorted(staged.symmetric_difference(expected_written)))
                    )
                for rel, expected_body in generated_bodies.items():
                    staged_body = _git_with_index(
                        wt_root,
                        index_binding.private_path,
                        "show",
                        f":{rel}",
                        check=False,
                    )
                    if staged_body.returncode != 0 or staged_body.stdout != expected_body:
                        raise RuntimeError(f"staged_content_mismatch:{rel}")

                tree_proc = _git_with_index(
                    wt_root,
                    index_binding.private_path,
                    "write-tree",
                    check=False,
                )
                staged_tree = (tree_proc.stdout or "").strip().lower()
                if tree_proc.returncode != 0 or not re.fullmatch(
                    r"[0-9a-f]{40}",
                    staged_tree,
                ):
                    detail = (tree_proc.stderr or tree_proc.stdout or "unknown")[:300]
                    raise RuntimeError(f"git_write_tree_failed:{detail}")
                implementation_tree = staged_tree
                payload_hashes = _verify_tree_payloads(
                    wt_root,
                    tree=staged_tree,
                    base_sha=base_sha,
                    written=expected_written,
                    generated_bodies=generated_bodies,
                    label="staged_tree",
                )

                _assert_live_index_unchanged(index_binding)
                for rel, expected_snapshot in generated_snapshots.items():
                    if _snapshot_destination(wt_root, rel) != expected_snapshot:
                        raise RuntimeError(f"reviewer_edits_present_before_commit:{rel}")
                current_row = _revalidate_claimed_boundary(
                    store,
                    run_id=run_id,
                    observed=run_d,
                    root=root,
                    base_sha=base_sha,
                    proposals=proposals,
                    own_worktree=wt_root,
                )

                commit = _git(
                    wt_root,
                    "-c",
                    "user.email=csi@local",
                    "-c",
                    "user.name=csi",
                    "commit-tree",
                    staged_tree,
                    "-p",
                    base_sha,
                    "-m",
                    f"csi: implement {run_id} (awaiting human merge)",
                    check=False,
                )
                implementation_commit = (commit.stdout or "").strip().lower()
                if commit.returncode != 0 or not re.fullmatch(
                    r"[0-9a-f]{40}",
                    implementation_commit,
                ):
                    detail = (commit.stderr or commit.stdout or "unknown")[:300]
                    raise RuntimeError(f"git_commit_tree_failed:{detail}")

                payload_hashes = _verify_commit_provenance(
                    wt_root,
                    commit=implementation_commit,
                    expected_tree=staged_tree,
                    base_sha=base_sha,
                    written=expected_written,
                    generated_bodies=generated_bodies,
                )
                committed_payload_sha256 = payload_hashes
                _assert_live_index_unchanged(index_binding)
                for rel, expected_snapshot in generated_snapshots.items():
                    if _snapshot_destination(wt_root, rel) != expected_snapshot:
                        raise RuntimeError(f"reviewer_edits_present_before_ref_update:{rel}")
                current_row = _revalidate_claimed_boundary(
                    store,
                    run_id=run_id,
                    observed=run_d,
                    root=root,
                    base_sha=base_sha,
                    proposals=proposals,
                    own_worktree=wt_root,
                )

                update_ref = _git(
                    wt_root,
                    "update-ref",
                    f"refs/heads/{branch}",
                    implementation_commit,
                    base_sha,
                    check=False,
                )
                if update_ref.returncode != 0:
                    detail = (update_ref.stderr or update_ref.stdout or "unknown")[:300]
                    raise RuntimeError(f"implementation_ref_update_failed:{detail}")

                branch_commit = _git_text(
                    wt_root,
                    "rev-parse",
                    f"refs/heads/{branch}",
                ).lower()
                if branch_commit != implementation_commit:
                    raise RuntimeError("implementation_branch_commit_mismatch")
                payload_hashes = _verify_commit_provenance(
                    wt_root,
                    commit=branch_commit,
                    expected_tree=staged_tree,
                    base_sha=base_sha,
                    written=expected_written,
                    generated_bodies=generated_bodies,
                )
                committed_payload_sha256 = payload_hashes
                post_status = _git_with_index(
                    wt_root,
                    index_binding.private_path,
                    "status",
                    "--porcelain=v1",
                    "--untracked-files=all",
                    check=False,
                )
                if post_status.returncode != 0:
                    detail = (post_status.stderr or post_status.stdout or "unknown")[:300]
                    raise RuntimeError(f"post_commit_reviewer_state_observation_failed:{detail}")
                if post_status.stdout:
                    raise RuntimeError("reviewer_edits_present_after_commit")
                _assert_live_index_unchanged(index_binding)
                for rel, expected_snapshot in generated_snapshots.items():
                    if _snapshot_destination(wt_root, rel) != expected_snapshot:
                        raise RuntimeError(f"reviewer_edits_present_after_commit:{rel}")
                current_row = _revalidate_claimed_boundary(
                    store,
                    run_id=run_id,
                    observed=run_d,
                    root=root,
                    base_sha=base_sha,
                    proposals=proposals,
                    own_worktree=wt_root,
                )

                impl_meta = {
                    "approval_binding": approval_binding(current_row),
                    "base_sha": base_sha,
                    "branch": branch,
                    "implementation_commit": implementation_commit,
                    "implementation_parent": base_sha,
                    "implementation_tree": implementation_tree,
                    "staged_index_sha256": staged_index_sha256,
                    "committed_payload_sha256": committed_payload_sha256,
                    "worktree": str(wt_root),
                    "written_paths": written,
                    "approved_by": effective_approved_by,
                }
                finalized = False
                try:
                    with _hold_expected_branch_ref(
                        root,
                        branch=branch,
                        expected_commit=implementation_commit,
                    ):
                        finalized = store.finalize_implementation_claim(
                            run_id,
                            observed=run_d,
                            implement_json=impl_meta,
                            repo_root=root,
                        )
                        if not finalized:
                            raise RuntimeError("implementation_finalization_claim_lost")
                except Exception as exc:
                    if finalized:
                        _force_non_merge_ready(
                            store,
                            run_id=run_id,
                            observed_implement_json=json.dumps(
                                impl_meta,
                                sort_keys=True,
                            ),
                            error=f"implementation_ref_guard_failed:{exc}",
                        )
                    raise
    except Exception as exc:  # noqa: BLE001
        error = f"implementation_failed:{exc}"
        try:
            if worktree_created:
                incident_observed = store.get_run(run_id)
                try:
                    incident_meta = _implementation_meta(incident_observed or {})
                    incident_meta["recovery_required"] = True
                except ValueError:
                    incident_meta = None
                store.mark_implementation_incident(
                    run_id,
                    observed=incident_observed,
                    implement_json=incident_meta,
                    error=error,
                )
            else:
                released = store.release_implementation_claim(
                    run_id,
                    restore_status=run_status,
                    error=error,
                    observed=run_d,
                )
                if not released:
                    incident_observed = store.get_run(run_id)
                    if str((incident_observed or {}).get("status") or "") == "IMPLEMENTING":
                        try:
                            incident_meta = _implementation_meta(incident_observed or {})
                            incident_meta["recovery_required"] = False
                        except ValueError:
                            incident_meta = None
                        store.mark_implementation_incident(
                            run_id,
                            observed=incident_observed,
                            implement_json=incident_meta,
                            error=error,
                        )
        except sqlite3.Error:
            LOG.exception("failed to persist CSI implementation incident")
        current_status = str(
            (store.get_run(run_id) or {}).get("status")
            or ("INCIDENT" if worktree_created else run_status)
        )
        return ImplementResult(
            ok=False,
            run_id=run_id,
            improvement_id=run_imp,
            branch=branch,
            worktree=str(wt_root),
            written_paths=written,
            error=error,
            status=current_status,
        )

    validated_row, readiness_error = _validate_merge_readiness(
        store=store,
        run_id=run_id,
        root=root,
        expected_branch=branch,
    )
    if readiness_error:
        return ImplementResult(
            ok=False,
            run_id=run_id,
            improvement_id=run_imp,
            branch=branch,
            worktree=str(wt_root),
            written_paths=written,
            status=str((validated_row or {}).get("status") or "INCIDENT"),
            error=readiness_error,
        )
    return ImplementResult(
        ok=True,
        run_id=run_id,
        improvement_id=run_imp,
        branch=branch,
        worktree=str(wt_root),
        written_paths=written,
        status="AWAITING_MERGE",
    )


def recover_implementation(
    *,
    run_id: str,
    db_path: str | Path,
    repo_root: str | Path | None = None,
) -> ImplementResult:
    """Restore a missing worktree from its retained CSI branch."""

    root = Path(repo_root or _ROOT).resolve()
    store = CsiStore(str(db_path))
    run = store.get_run(run_id)
    if not run:
        return ImplementResult(ok=False, run_id=run_id, error="unknown_run")
    status = str(run.get("status") or "")
    if status not in {
        "AWAITING_MERGE",
        "IMPLEMENTING",
        "INCIDENT",
        "REJECTED",
        "CANCELLED",
        "QUARANTINED",
        "MERGED",
    }:
        return ImplementResult(
            ok=False,
            run_id=run_id,
            status=status,
            error=f"recovery_not_allowed_for_state:{status}",
        )
    try:
        _assert_git_root(root)
        expected_branch, _, expected_wt = _expected_locations(root, run_id)
        meta = _implementation_meta(run)
        cleanup_quarantine = meta.get("cleanup_quarantine")
        if (
            isinstance(cleanup_quarantine, dict)
            and cleanup_quarantine.get("unlink_pending") is True
        ):
            raise RuntimeError("cleanup_quarantine_retained")
        if str(meta.get("branch") or "") != expected_branch:
            raise PermissionError("implementation_branch_metadata_mismatch")
        metadata_wt = _lexical_absolute_path(
            str(meta.get("worktree") or ""),
            label="implementation_worktree_metadata",
        )
        # Safety (`is not True`): metadata worktree must match expected by inode.
        if inode_paths_equal(metadata_wt, expected_wt) is not True:
            raise PermissionError("implementation_worktree_metadata_mismatch")
        if status == "AWAITING_MERGE":
            validated_row, readiness_error = _validate_merge_readiness(
                store=store,
                run_id=run_id,
                root=root,
                expected_branch=expected_branch,
            )
            if readiness_error:
                status = str((validated_row or {}).get("status") or "INCIDENT")
                raise RuntimeError(readiness_error)
        else:
            if meta.get("approval_binding") != approval_binding(run):
                raise RuntimeError("implementation_approval_binding_changed")
            if not _branch_exists(root, expected_branch):
                raise RuntimeError("recovery_branch_missing")
            # Terminal/recovery state may contain a human follow-up commit.
            # Validate that the approved commit remains an intact immutable
            # object, but do not require the reviewer-owned branch to remain
            # pinned to it merely to preserve or recreate the workspace.
            _verify_stored_implementation_provenance(root, meta)
        current = _branch_worktree(root, expected_branch)
        if current is not None:
            # Safety (`is not True`): treat unknown/different registration as stale.
            if inode_paths_equal(current, expected_wt) is not True:
                raise RuntimeError("stale_worktree_registration_retained")
            try:
                _worktree_directory_identity(root, current)
            except FileNotFoundError:
                # The directory may have been moved and may contain reviewer
                # edits. Keep the registration for explicit operator repair.
                raise RuntimeError("stale_worktree_registration_retained") from None
            else:
                if status == "AWAITING_MERGE":
                    _, readiness_error = _validate_merge_readiness(
                        store=store,
                        run_id=run_id,
                        root=root,
                        expected_branch=expected_branch,
                    )
                    if readiness_error:
                        raise RuntimeError(readiness_error)
                return ImplementResult(
                    ok=True,
                    run_id=run_id,
                    improvement_id=str(run.get("improvement_id") or ""),
                    branch=expected_branch,
                    worktree=str(current),
                    written_paths=[str(p) for p in meta.get("written_paths") or []],
                    status=status,
                    idempotent=True,
                )
        if expected_wt.exists() or expected_wt.is_symlink():
            raise RuntimeError("recovery_path_occupied_state_retained")
        observed_implement_json = str(run.get("implement_json") or "{}")
        with _mutation_fence(root):
            fresh = store.get_run(run_id)
            if (
                not fresh
                or str(fresh.get("status") or "") != status
                or str(fresh.get("implement_json") or "") != observed_implement_json
            ):
                raise RuntimeError("recovery_state_changed_before_mutation")
            if not _branch_exists(root, expected_branch):
                raise RuntimeError("recovery_branch_missing")
            current = _branch_worktree(root, expected_branch)
            if current is not None:
                raise RuntimeError("recovery_worktree_changed_before_mutation")
            if expected_wt.exists() or expected_wt.is_symlink():
                raise RuntimeError("recovery_path_occupied_state_retained")
            _secure_ensure_dir(root, ("var", "csi", "worktrees"))
            proc = _git(
                root,
                "worktree",
                "add",
                str(expected_wt),
                expected_branch,
                check=False,
            )
            if proc.returncode != 0:
                detail = (proc.stderr or proc.stdout or "unknown")[:300]
                raise RuntimeError(f"worktree_recovery_failed:{detail}")
            if status == "AWAITING_MERGE":
                _, readiness_error = _validate_merge_readiness(
                    store=store,
                    run_id=run_id,
                    root=root,
                    expected_branch=expected_branch,
                )
                if readiness_error:
                    raise RuntimeError(readiness_error)
            else:
                _verify_stored_implementation_provenance(root, meta)
    except (OSError, PermissionError, RuntimeError, subprocess.SubprocessError) as exc:
        return ImplementResult(
            ok=False,
            run_id=run_id,
            status=status,
            error=str(exc),
        )
    return ImplementResult(
        ok=True,
        run_id=run_id,
        improvement_id=str(run.get("improvement_id") or ""),
        branch=expected_branch,
        worktree=str(expected_wt),
        written_paths=[str(p) for p in meta.get("written_paths") or []],
        status=status,
        idempotent=True,
    )


def _mark_current_cleanup_incident(
    store: CsiStore,
    *,
    run_id: str,
    error: str,
) -> bool:
    """Fail a current cleanup claim closed while preserving its exact bytes."""

    current = store.get_run(run_id)
    if current is None or str(current.get("status") or "") != "CLEANING":
        return False
    return store.mark_cleanup_incident(
        run_id,
        observed_implement_json=str(current.get("implement_json") or "{}"),
        error=error,
    )


def _mark_invalid_cleanup_incident(
    store: CsiStore,
    *,
    run_id: str,
    error: str,
) -> bool:
    """Fence only a cleanup whose durable source binding is no longer valid."""

    current = store.get_run(run_id)
    if current is None or str(current.get("status") or "") != "CLEANING":
        return False
    current_json = str(current.get("implement_json") or "{}")
    if store.cleanup_claim_is_current(
        run_id,
        observed_implement_json=current_json,
    ):
        return False
    return store.mark_cleanup_incident(
        run_id,
        observed_implement_json=current_json,
        error=error,
    )


def _cleanup_terminal_locked(
    *,
    store: CsiStore,
    run_id: str,
    terminal_status: str,
    root: Path,
    expected_branch: str,
    expected_wt: Path,
    meta: dict[str, Any],
    observed_implement_json: str,
) -> CleanupResult:
    def _claim_lost() -> CleanupResult:
        current = store.get_run(run_id)
        current_status = str((current or {}).get("status") or "")
        if current_status == "CLEANING":
            current_json = str((current or {}).get("implement_json") or "{}")
            if not store.cleanup_claim_is_current(
                run_id,
                observed_implement_json=current_json,
            ) and _mark_current_cleanup_incident(
                store,
                run_id=run_id,
                error="cleanup_claim_source_changed",
            ):
                current_status = "INCIDENT"
        return CleanupResult(
            ok=False,
            run_id=run_id,
            action="retained",
            branch=expected_branch,
            worktree=str(expected_wt),
            retained_reason=f"cleanup_claim_lost:{current_status}",
        )

    def _release(result: CleanupResult, *, error: str | None = None) -> CleanupResult:
        released = store.release_cleanup_claim(
            run_id,
            restore_status=terminal_status,
            observed_implement_json=observed_implement_json,
            error=error,
        )
        if released:
            return result
        if _mark_invalid_cleanup_incident(
            store,
            run_id=run_id,
            error=error or "cleanup_release_source_changed",
        ):
            return CleanupResult(
                ok=False,
                run_id=run_id,
                action="retained",
                branch=expected_branch,
                worktree=str(expected_wt),
                retained_reason=error or "cleanup_release_source_changed",
                error=error or "cleanup_release_source_changed",
            )
        return _claim_lost()

    def _persist_quarantine_journal(tombstone: _QuarantineTombstone) -> bool:
        nonlocal observed_implement_json
        updated = store.journal_cleanup_quarantine(
            run_id,
            observed_implement_json=observed_implement_json,
            cleanup_quarantine=_quarantine_journal(
                tombstone,
                restore_status=terminal_status,
            ),
        )
        if updated is None:
            return False
        observed_implement_json = updated
        return True

    fresh = store.get_run(run_id)
    if (
        not fresh
        or str(fresh.get("status") or "") != terminal_status
        or str(fresh.get("implement_json") or "") != observed_implement_json
    ):
        return _claim_lost()
    try:
        if str(fresh.get("approval_status") or "") != "approved" or meta.get(
            "approval_binding"
        ) != approval_binding(fresh):
            raise RuntimeError("implementation_approval_binding_changed")
        _verify_stored_implementation_provenance(root, meta)
    except (
        OSError,
        PermissionError,
        RuntimeError,
        subprocess.SubprocessError,
    ) as exc:
        error = f"cleanup_provenance_invalid:{exc}"
        return CleanupResult(
            ok=False,
            run_id=run_id,
            action="retained",
            branch=expected_branch,
            worktree=str(expected_wt),
            retained_reason=error,
            error=error,
        )
    claimed_implement_json = store.claim_cleanup(
        run_id,
        expected_status=terminal_status,
        observed_implement_json=observed_implement_json,
    )
    if claimed_implement_json is None:
        return _claim_lost()
    observed_implement_json = claimed_implement_json

    try:
        actual_wt = _branch_worktree(root, expected_branch)
    except RuntimeError as exc:
        return _release(
            CleanupResult(
                ok=False,
                run_id=run_id,
                action="retained",
                branch=expected_branch,
                error=str(exc),
            ),
            error=str(exc),
        )
    if not store.cleanup_claim_is_current(
        run_id,
        observed_implement_json=observed_implement_json,
    ):
        return _claim_lost()

    recorded_quarantine = meta.get("cleanup_quarantine")
    if isinstance(recorded_quarantine, dict) and recorded_quarantine.get("unlink_pending") is True:
        current_verification = _observe_quarantine_path(
            recorded_quarantine,
            expected_worktree=expected_wt,
        )
        observed_tombstone = dict(recorded_quarantine)
        observed_tombstone["path_verification"] = current_verification
        try:
            observed_tombstone, _registration_action = _reconcile_journaled_registration(
                observed_tombstone,
                root=root,
                expected_worktree=expected_wt,
                expected_branch=expected_branch,
                quarantine_observation=current_verification,
            )
        except (OSError, PermissionError, RuntimeError) as exc:
            error = f"cleanup_registration_retirement_failed:{exc}"
            return _release(
                CleanupResult(
                    ok=False,
                    run_id=run_id,
                    action="retained",
                    branch=expected_branch,
                    worktree=str(expected_wt),
                    retained_reason=error,
                    error=error,
                ),
                error=error,
            )
        updated = store.journal_cleanup_quarantine(
            run_id,
            observed_implement_json=observed_implement_json,
            cleanup_quarantine=observed_tombstone,
        )
        if updated is None:
            return _claim_lost()
        observed_implement_json = updated
        path_matches = current_verification in {
            "matched_bound_identity_empty_at_current_observation",
            "matched_bound_identity_nonempty_at_current_observation",
        }
        if current_verification == ("matched_bound_identity_nonempty_at_current_observation"):
            retained_reason = "worktree_quarantine_reviewer_content_retained"
        elif path_matches:
            retained_reason = "worktree_quarantine_retained_for_safe_unlink"
        else:
            retained_reason = f"worktree_quarantine_path_mismatch:{current_verification}"
        return _release(
            CleanupResult(
                ok=path_matches,
                run_id=run_id,
                action="retained",
                branch=expected_branch,
                worktree=str(expected_wt),
                retained_reason=retained_reason,
                error="" if path_matches else retained_reason,
            ),
            error=None if path_matches else retained_reason,
        )

    action_parts: list[str] = []
    quarantine_tombstone: _QuarantineTombstone | None = None
    if actual_wt is not None:
        # Safety (`is not True`): treat unknown/different as a moved worktree.
        if inode_paths_equal(actual_wt, expected_wt) is not True:
            return _release(
                CleanupResult(
                    ok=True,
                    run_id=run_id,
                    action="retained",
                    branch=expected_branch,
                    worktree=str(actual_wt),
                    retained_reason="reviewer_moved_worktree",
                )
            )
        try:
            original_identity = _worktree_directory_identity(root, actual_wt)
        except FileNotFoundError:
            return _release(
                CleanupResult(
                    ok=True,
                    run_id=run_id,
                    action="retained",
                    branch=expected_branch,
                    worktree=str(actual_wt),
                    retained_reason="stale_registration_retained_for_manual_repair",
                )
            )
        except (OSError, PermissionError, RuntimeError) as exc:
            error = f"cleanup_worktree_identity_refused:{exc}"
            return _release(
                CleanupResult(
                    ok=False,
                    run_id=run_id,
                    action="retained",
                    branch=expected_branch,
                    worktree=str(actual_wt),
                    retained_reason="worktree_symlink_or_identity_substitution",
                    error=error,
                ),
                error=error,
            )
        branch_proc = _git(
            actual_wt,
            "symbolic-ref",
            "--short",
            "HEAD",
            check=False,
        )
        if branch_proc.returncode != 0 or branch_proc.stdout.strip() != expected_branch:
            return _release(
                CleanupResult(
                    ok=False,
                    run_id=run_id,
                    action="retained",
                    branch=expected_branch,
                    worktree=str(actual_wt),
                    retained_reason="unexpected_worktree_branch",
                    error="reviewer_state_observation_failed",
                ),
                error="reviewer_state_observation_failed",
            )
        status_proc = _git(
            actual_wt,
            "status",
            "--porcelain=v1",
            "--untracked-files=all",
            check=False,
        )
        if not store.cleanup_claim_is_current(
            run_id,
            observed_implement_json=observed_implement_json,
        ):
            return _claim_lost()
        if status_proc.returncode != 0:
            return _release(
                CleanupResult(
                    ok=False,
                    run_id=run_id,
                    action="retained",
                    branch=expected_branch,
                    worktree=str(actual_wt),
                    error="reviewer_state_observation_failed",
                ),
                error="reviewer_state_observation_failed",
            )
        if status_proc.stdout.strip():
            return _release(
                CleanupResult(
                    ok=True,
                    run_id=run_id,
                    action="retained",
                    branch=expected_branch,
                    worktree=str(actual_wt),
                    retained_reason="reviewer_edits_present",
                )
            )
        try:
            after_status_identity = _worktree_directory_identity(root, actual_wt)
        except (OSError, PermissionError, RuntimeError) as exc:
            error = f"cleanup_worktree_identity_changed:{exc}"
            return _release(
                CleanupResult(
                    ok=False,
                    run_id=run_id,
                    action="retained",
                    branch=expected_branch,
                    worktree=str(actual_wt),
                    retained_reason="worktree_symlink_or_identity_substitution",
                    error=error,
                ),
                error=error,
            )
        if after_status_identity != original_identity:
            error = "cleanup_worktree_identity_changed"
            return _release(
                CleanupResult(
                    ok=False,
                    run_id=run_id,
                    action="retained",
                    branch=expected_branch,
                    worktree=str(actual_wt),
                    retained_reason="worktree_symlink_or_identity_substitution",
                    error=error,
                ),
                error=error,
            )
        if not store.cleanup_claim_is_current(
            run_id,
            observed_implement_json=observed_implement_json,
        ):
            return _claim_lost()
        try:
            registered_now = _branch_worktree(root, expected_branch)
        except RuntimeError as exc:
            error = str(exc)
            return _release(
                CleanupResult(
                    ok=False,
                    run_id=run_id,
                    action="retained",
                    branch=expected_branch,
                    worktree=str(actual_wt),
                    retained_reason="worktree_registration_changed",
                    error=error,
                ),
                error=error,
            )
        # Safety (`is not True`): refuse cleanup if registration is missing or
        # no longer matches (fail-closed path identity).
        if registered_now is None or inode_paths_equal(registered_now, expected_wt) is not True:
            return _release(
                CleanupResult(
                    ok=False,
                    run_id=run_id,
                    action="retained",
                    branch=expected_branch,
                    worktree=str(actual_wt),
                    retained_reason="worktree_registration_changed",
                    error="cleanup_worktree_registration_changed",
                ),
                error="cleanup_worktree_registration_changed",
            )
        try:
            final_identity = _worktree_directory_identity(root, expected_wt)
        except (OSError, PermissionError, RuntimeError) as exc:
            error = f"cleanup_worktree_identity_changed:{exc}"
            return _release(
                CleanupResult(
                    ok=False,
                    run_id=run_id,
                    action="retained",
                    branch=expected_branch,
                    worktree=str(expected_wt),
                    retained_reason="worktree_symlink_or_identity_substitution",
                    error=error,
                ),
                error=error,
            )
        if final_identity != original_identity:
            error = "cleanup_worktree_identity_changed"
            return _release(
                CleanupResult(
                    ok=False,
                    run_id=run_id,
                    action="retained",
                    branch=expected_branch,
                    worktree=str(expected_wt),
                    retained_reason="worktree_symlink_or_identity_substitution",
                    error=error,
                ),
                error=error,
            )
        final_status = _git(
            expected_wt,
            "status",
            "--porcelain=v1",
            "--untracked-files=all",
            check=False,
        )
        if final_status.returncode != 0:
            return _release(
                CleanupResult(
                    ok=False,
                    run_id=run_id,
                    action="retained",
                    branch=expected_branch,
                    worktree=str(expected_wt),
                    retained_reason="reviewer_state_observation_failed",
                    error="reviewer_state_observation_failed",
                ),
                error="reviewer_state_observation_failed",
            )
        if final_status.stdout.strip():
            return _release(
                CleanupResult(
                    ok=True,
                    run_id=run_id,
                    action="retained",
                    branch=expected_branch,
                    worktree=str(expected_wt),
                    retained_reason="reviewer_edits_present",
                )
            )
        try:
            final_status_identity = _worktree_directory_identity(root, expected_wt)
        except (OSError, PermissionError, RuntimeError) as exc:
            error = f"cleanup_worktree_identity_changed:{exc}"
            return _release(
                CleanupResult(
                    ok=False,
                    run_id=run_id,
                    action="retained",
                    branch=expected_branch,
                    worktree=str(expected_wt),
                    retained_reason="worktree_symlink_or_identity_substitution",
                    error=error,
                ),
                error=error,
            )
        if final_status_identity != original_identity:
            error = "cleanup_worktree_identity_changed"
            return _release(
                CleanupResult(
                    ok=False,
                    run_id=run_id,
                    action="retained",
                    branch=expected_branch,
                    worktree=str(expected_wt),
                    retained_reason="worktree_symlink_or_identity_substitution",
                    error=error,
                ),
                error=error,
            )
        if not store.cleanup_claim_is_current(
            run_id,
            observed_implement_json=observed_implement_json,
        ):
            return _claim_lost()
        expected_commit = str(meta.get("implementation_commit") or "").lower()
        if re.fullmatch(r"[0-9a-f]{40}", expected_commit) is None:
            error = "cleanup_provenance_invalid:implementation_commit_metadata_invalid"
            return _release(
                CleanupResult(
                    ok=False,
                    run_id=run_id,
                    action="retained",
                    branch=expected_branch,
                    worktree=str(expected_wt),
                    retained_reason=error,
                    error=error,
                ),
                error=error,
            )
        clean_at_approved_commit, final_state_error = _worktree_clean_at(
            expected_wt,
            branch=expected_branch,
            sha=expected_commit,
        )
        if not clean_at_approved_commit:
            return _release(
                CleanupResult(
                    ok=True,
                    run_id=run_id,
                    action="retained",
                    branch=expected_branch,
                    worktree=str(expected_wt),
                    retained_reason=final_state_error,
                )
            )
        try:
            # The earlier clean observations are advisory. Hold the approved
            # branch ref across the final clean/SHA/identity checks and the
            # descriptor-bound quarantine plus registration retirement. A
            # clean human follow-up commit is reviewer state, not disposable
            # CSI output.
            with _hold_expected_branch_ref(
                root,
                branch=expected_branch,
                expected_commit=expected_commit,
            ):
                fresh = store.get_run(run_id)
                if (
                    not fresh
                    or str(fresh.get("status") or "") != "CLEANING"
                    or str(fresh.get("approval_status") or "") != "approved"
                    or str(fresh.get("implement_json") or "") != observed_implement_json
                ):
                    raise _CleanupClaimLost("cleanup_claim_lost_at_provenance_boundary")
                fresh_meta = _implementation_meta(fresh)
                if fresh_meta.get("approval_binding") != approval_binding(fresh):
                    raise RuntimeError("implementation_approval_binding_changed")
                _verify_stored_implementation_provenance(root, fresh_meta)
                clean_at_approved_commit, final_state_error = _worktree_clean_at(
                    expected_wt,
                    branch=expected_branch,
                    sha=expected_commit,
                )
                if not clean_at_approved_commit:
                    return _release(
                        CleanupResult(
                            ok=True,
                            run_id=run_id,
                            action="retained",
                            branch=expected_branch,
                            worktree=str(expected_wt),
                            retained_reason=final_state_error,
                        )
                    )
                guarded_identity = _worktree_directory_identity(
                    root,
                    expected_wt,
                )
                if guarded_identity != original_identity:
                    raise RuntimeError("cleanup_worktree_identity_changed")
                quarantine_tombstone = _quarantine_and_clear_worktree(
                    root,
                    expected_wt,
                    branch=expected_branch,
                    expected_identity=original_identity,
                    claim_current=lambda: store.cleanup_claim_is_current(
                        run_id,
                        observed_implement_json=observed_implement_json,
                    ),
                    persist_tombstone=_persist_quarantine_journal,
                )
        except _CleanupClaimLost:
            return _claim_lost()
        except (OSError, PermissionError, RuntimeError) as exc:
            error = f"cleanup_provenance_or_remove_failed:{exc}"
            return _release(
                CleanupResult(
                    ok=False,
                    run_id=run_id,
                    action="retained",
                    branch=expected_branch,
                    worktree=str(actual_wt),
                    retained_reason="worktree_symlink_or_identity_substitution",
                    error=error,
                ),
                error=error,
            )
        action_parts.append("worktree_quarantined")
        if (
            quarantine_tombstone.registration is not None
            and quarantine_tombstone.registration.state == "retired_bound"
        ):
            action_parts.append("worktree_registration_retired")
        if not _persist_quarantine_journal(quarantine_tombstone):
            return _claim_lost()
        if quarantine_tombstone.path_verification != "matched_bound_identity_after_persist":
            retained_reason = (
                f"worktree_quarantine_path_mismatch:{quarantine_tombstone.path_verification}"
            )
            return _release(
                CleanupResult(
                    ok=False,
                    run_id=run_id,
                    action="retained",
                    branch=expected_branch,
                    worktree=str(expected_wt),
                    retained_reason=retained_reason,
                    error=retained_reason,
                ),
                error=retained_reason,
            )
        if not store.cleanup_claim_is_current(
            run_id,
            observed_implement_json=observed_implement_json,
        ):
            return _claim_lost()

    if not store.cleanup_claim_is_current(
        run_id,
        observed_implement_json=observed_implement_json,
    ):
        return _claim_lost()
    retained_reason = ""
    if quarantine_tombstone is not None:
        retained_reason = "worktree_quarantine_retained_for_safe_unlink"
    elif _branch_exists(root, expected_branch):
        merged = _git(
            root,
            "merge-base",
            "--is-ancestor",
            expected_branch,
            "HEAD",
            check=False,
        )
        if not store.cleanup_claim_is_current(
            run_id,
            observed_implement_json=observed_implement_json,
        ):
            return _claim_lost()
        if merged.returncode == 0:
            delete = _git(
                root,
                "branch",
                "-d",
                expected_branch,
                check=False,
            )
            if delete.returncode == 0:
                action_parts.append("merged_branch_deleted")
            else:
                retained_reason = "safe_branch_delete_failed"
        else:
            retained_reason = "unmerged_branch_retained_for_recovery"

    finalized_meta = dict(meta)
    finalized_meta.update(
        {
            "cleanup_action": ",".join(action_parts) or "nothing_to_clean",
            "cleanup_retained_reason": retained_reason,
        }
    )
    if quarantine_tombstone is not None:
        finalized_meta["cleanup_quarantine"] = _quarantine_journal(
            quarantine_tombstone,
            restore_status=terminal_status,
        )
    if not store.cleanup_claim_is_current(
        run_id,
        observed_implement_json=observed_implement_json,
    ):
        return _claim_lost()
    if not store.finalize_cleanup_claim(
        run_id,
        restore_status=terminal_status,
        observed_implement_json=observed_implement_json,
        implement_json=finalized_meta,
        repo_root=root,
    ):
        return _claim_lost()
    return CleanupResult(
        ok=True,
        run_id=run_id,
        action=",".join(action_parts) or "nothing_to_clean",
        branch=expected_branch,
        worktree=str(expected_wt),
        retained_reason=retained_reason,
    )


def _recover_interrupted_cleanup_locked(
    *,
    store: CsiStore,
    run_id: str,
    root: Path,
    expected_branch: str,
    expected_wt: Path,
    observed_implement_json: str,
) -> CleanupResult:
    """Reconcile a crashed CLEANING claim using only durable observations."""

    fresh = store.get_run(run_id)
    if (
        not fresh
        or str(fresh.get("status") or "") != "CLEANING"
        or str(fresh.get("implement_json") or "") != observed_implement_json
    ):
        current_status = str((fresh or {}).get("status") or "")
        return CleanupResult(
            ok=False,
            run_id=run_id,
            action="retained",
            branch=expected_branch,
            worktree=str(expected_wt),
            retained_reason=f"cleanup_claim_lost:{current_status}",
        )
    if not store.cleanup_claim_is_current(
        run_id,
        observed_implement_json=observed_implement_json,
    ):
        _mark_current_cleanup_incident(
            store,
            run_id=run_id,
            error="cleanup_recovery_source_changed",
        )
        return CleanupResult(
            ok=False,
            run_id=run_id,
            action="retained",
            branch=expected_branch,
            worktree=str(expected_wt),
            retained_reason="cleanup_recovery_source_changed",
            error="cleanup_recovery_source_changed",
        )

    try:
        metadata = json.loads(observed_implement_json)
    except (TypeError, ValueError):
        metadata = {}
    journal = metadata.get("cleanup_quarantine") if isinstance(metadata, dict) else None
    if not isinstance(journal, dict):
        error = "interrupted_cleanup_journal_missing"
        transitioned = store.mark_cleanup_incident(
            run_id,
            observed_implement_json=observed_implement_json,
            error=error,
        )
        return CleanupResult(
            ok=False,
            run_id=run_id,
            action="retained",
            branch=expected_branch,
            worktree=str(expected_wt),
            retained_reason=error,
            error="" if transitioned else "cleanup_recovery_claim_lost",
        )

    restore_status = str(journal.get("restore_status") or "")
    if restore_status not in _CLEANABLE_TERMINAL_STATES:
        error = "interrupted_cleanup_restore_status_invalid"
        transitioned = store.mark_cleanup_incident(
            run_id,
            observed_implement_json=observed_implement_json,
            error=error,
        )
        return CleanupResult(
            ok=False,
            run_id=run_id,
            action="retained",
            branch=expected_branch,
            worktree=str(expected_wt),
            retained_reason=error,
            error="" if transitioned else "cleanup_recovery_claim_lost",
        )

    observation = _observe_quarantine_path(
        journal,
        expected_worktree=expected_wt,
    )
    observed_journal = dict(journal)
    observed_journal["path_verification"] = observation
    try:
        observed_journal, _registration_action = _reconcile_journaled_registration(
            observed_journal,
            root=root,
            expected_worktree=expected_wt,
            expected_branch=expected_branch,
            quarantine_observation=observation,
        )
    except (OSError, PermissionError, RuntimeError) as exc:
        error = f"cleanup_registration_retirement_failed:{exc}"
        released = store.release_cleanup_claim(
            run_id,
            restore_status=restore_status,
            observed_implement_json=observed_implement_json,
            error=error,
        )
        if not released:
            released = _mark_invalid_cleanup_incident(
                store,
                run_id=run_id,
                error=error,
            )
        return CleanupResult(
            ok=False,
            run_id=run_id,
            action="retained",
            branch=expected_branch,
            worktree=str(expected_wt),
            retained_reason=error,
            error="" if released else "cleanup_recovery_claim_lost",
        )
    updated_json = store.journal_cleanup_quarantine(
        run_id,
        observed_implement_json=observed_implement_json,
        cleanup_quarantine=observed_journal,
    )
    if updated_json is None:
        _mark_invalid_cleanup_incident(
            store,
            run_id=run_id,
            error="cleanup_recovery_source_changed",
        )
        return CleanupResult(
            ok=False,
            run_id=run_id,
            action="retained",
            branch=expected_branch,
            worktree=str(expected_wt),
            retained_reason="cleanup_recovery_claim_lost",
        )

    identity_located = "matched_bound_identity" in observation
    reason = (
        f"interrupted_cleanup_reconciled:{observation}"
        if identity_located
        else f"interrupted_cleanup_path_mismatch:{observation}"
    )
    if not store.release_cleanup_claim(
        run_id,
        restore_status=restore_status,
        observed_implement_json=updated_json,
        error=None if identity_located else reason,
    ):
        _mark_invalid_cleanup_incident(
            store,
            run_id=run_id,
            error="cleanup_recovery_release_source_changed",
        )
        return CleanupResult(
            ok=False,
            run_id=run_id,
            action="retained",
            branch=expected_branch,
            worktree=str(expected_wt),
            retained_reason="cleanup_recovery_claim_lost",
        )
    return CleanupResult(
        ok=identity_located,
        run_id=run_id,
        action="retained",
        branch=expected_branch,
        worktree=str(expected_wt),
        retained_reason=reason,
        error="" if identity_located else reason,
    )


def cleanup_implementation(
    *,
    run_id: str,
    db_path: str | Path,
    repo_root: str | Path | None = None,
) -> CleanupResult:
    """Remove only clean terminal worktrees and only fully merged branches."""

    root = Path(repo_root or _ROOT).resolve()
    store = CsiStore(str(db_path))
    run = store.get_run(run_id)
    if not run:
        return CleanupResult(ok=False, run_id=run_id, action="none", error="unknown_run")
    status = str(run.get("status") or "")
    try:
        _assert_git_root(root)
        expected_branch, _, expected_wt = _expected_locations(root, run_id)
        meta = _implementation_meta(run)
    except (PermissionError, RuntimeError, ValueError) as exc:
        return CleanupResult(ok=False, run_id=run_id, action="none", error=str(exc))
    if str(meta.get("branch") or "") != expected_branch:
        return CleanupResult(
            ok=True,
            run_id=run_id,
            action="nothing_to_clean",
            retained_reason="no_valid_implementation_metadata",
        )
    try:
        metadata_wt = _lexical_absolute_path(
            str(meta.get("worktree") or ""),
            label="implementation_worktree_metadata",
        )
    except PermissionError as exc:
        return CleanupResult(
            ok=False,
            run_id=run_id,
            action="retained",
            branch=expected_branch,
            retained_reason="implementation_worktree_metadata_mismatch",
            error=str(exc),
        )
    # Safety (`is not True`): metadata worktree must match expected by inode.
    if inode_paths_equal(metadata_wt, expected_wt) is not True:
        return CleanupResult(
            ok=False,
            run_id=run_id,
            action="retained",
            branch=expected_branch,
            retained_reason="implementation_worktree_metadata_mismatch",
        )
    if status == "CLEANING":
        observed_implement_json = str(run.get("implement_json") or "{}")
        try:
            with _mutation_fence(root):
                return _recover_interrupted_cleanup_locked(
                    store=store,
                    run_id=run_id,
                    root=root,
                    expected_branch=expected_branch,
                    expected_wt=expected_wt,
                    observed_implement_json=observed_implement_json,
                )
        except (OSError, RuntimeError, sqlite3.Error, subprocess.SubprocessError) as exc:
            return CleanupResult(
                ok=False,
                run_id=run_id,
                action="retained",
                branch=expected_branch,
                worktree=str(expected_wt),
                error=f"cleanup_recovery_fence_failed:{exc}",
            )
    if status in _ACTIVE_REVIEW_STATES:
        return CleanupResult(
            ok=True,
            run_id=run_id,
            action="retained",
            branch=expected_branch,
            worktree=str(expected_wt),
            retained_reason=f"active_or_recoverable_state:{status}",
        )
    if status not in _CLEANABLE_TERMINAL_STATES:
        return CleanupResult(
            ok=True,
            run_id=run_id,
            action="retained",
            branch=expected_branch,
            worktree=str(expected_wt),
            retained_reason=f"unknown_or_nonterminal_state:{status}",
        )

    observed_implement_json = str(run.get("implement_json") or "{}")
    try:
        with _mutation_fence(root):
            return _cleanup_terminal_locked(
                store=store,
                run_id=run_id,
                terminal_status=status,
                root=root,
                expected_branch=expected_branch,
                expected_wt=expected_wt,
                meta=meta,
                observed_implement_json=observed_implement_json,
            )
    except (OSError, RuntimeError, sqlite3.Error, subprocess.SubprocessError) as exc:
        return CleanupResult(
            ok=False,
            run_id=run_id,
            action="retained",
            branch=expected_branch,
            worktree=str(expected_wt),
            error=f"cleanup_fence_failed:{exc}",
        )


def approve_run(
    *,
    run_id: str,
    db_path: str | Path,
    approved_by: str = "operator",
) -> bool:
    """Approve the current CSI evidence/decision bundle."""

    store = CsiStore(str(db_path))
    run = store.get_run(run_id)
    if not run:
        return False
    if str(run.get("status") or "") not in {"AWAITING_HUMAN", "DEFERRED"}:
        return False
    return store.set_approval(run_id, status="approved", approved_by=approved_by)


__all__ = [
    "CleanupResult",
    "ImplementResult",
    "approve_run",
    "cleanup_implementation",
    "implement_approved",
    "recover_implementation",
]
