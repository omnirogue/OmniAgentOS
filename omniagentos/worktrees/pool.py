"""Pre-warmed git worktree pool to avoid the overhead of git worktree creation.

WHY the force checkout and clean exist:
The slot retargeting sequence employs two distinct, individually load-bearing
cleanup steps (each required to pass the test suite) to prevent contamination:
- `git checkout --force <branch>` / `--force -B <branch> <base sha>`:
  discards modified tracked files and clears a wedged mid-merge state
  (`MERGE_HEAD` + conflicted index) left by a worker that died mid-merge.
  A plain `git checkout` silently carries local modifications across when the
  target commit does not need to touch those files, which is how a previous
  unit's edits leaked into the next one.
- `git clean -ffd -x` (plus `-e <dir>` per `dep_link_dirs`): the only step
  that removes untracked files and stale build artifacts. `-e` keeps the
  symlink-shared dependency dirs that `git.py` creates.

Each of the two is individually load-bearing and the test suite fails if
either is removed.

WHY an existing unit branch is attached at its tip rather than reset:
If a unit branch already exists, it means a predecessor attempt might have
salvaged and committed partial work. Attaching at its tip allows that salvaged
work to relay forward into the successor attempt, preventing data loss.
Using `checkout -B` would discard that work.

WHY base_ref is resolved in the main working dir before the slot is touched:
`git -C <slot> rev-parse HEAD` (or any base_ref resolution inside the slot)
would resolve to whatever the previous occupant left checked out, silently
basing the new unit's branch on the previous unit's commits. Resolve
base_ref to a sha in the main working dir once, then use that sha for
branch creation.

Invariant: every PooledWorktree returned by acquire() has .branch equal to
the branch actually checked out at .path. The pool never invents a worktree
outside SubprocessWorktrees.create, and never returns a detached or
mislabelled tree — that would silently drop the worker's commits when the
coordinator later merges pw.branch by name/sha.
"""

from __future__ import annotations

import logging
import os
import subprocess
import threading
from collections.abc import Sequence
from dataclasses import dataclass

from omniagentos.worktrees.git import SubprocessWorktrees

LOG = logging.getLogger(__name__)

__all__ = ["PooledWorktree", "WorktreePool"]


@dataclass(frozen=True)
class PooledWorktree:
    path: str
    branch: str
    base_sha: str
    pooled: bool  # True: came from the pool. False: on-demand fallback.
    reused: bool  # True: the unit branch already existed (relay)


class WorktreePool:
    """Pre-warmed worktree slot pool.

    Invariant: acquire() never returns a PooledWorktree whose .branch is not
    the branch actually checked out at .path. Failures of
    SubprocessWorktrees.create propagate; the pool does not invent detached
    worktrees as a fallback.
    """

    _IDENTITY = (
        "-c",
        "user.email=4580856+omniagentos-bot[bot]@users.noreply.github.com",
        "-c",
        "user.name=OmniAgentOS Swarm",
    )
    _NO_HOOKS = ("-c", "core.hooksPath=")

    def __init__(
        self,
        worktrees: SubprocessWorktrees,
        working_dir: str,
        owner_id: str,
        *,
        size: int = 2,
        dep_link_dirs: Sequence[str] = (),
    ) -> None:
        self._worktrees = worktrees
        self._working_dir = working_dir
        self._owner_id = owner_id
        self._size = size
        self._dep_link_dirs = tuple(dep_link_dirs)

        self._lock = threading.Lock()
        self._prewarmed = False
        self._slot_paths: set[str] = set()
        self._slot_unit_keys: dict[str, str] = {}
        self._free: list[str] = []
        self._in_use: set[str] = set()

        # Stats counters
        self._cold_fallbacks = 0
        self._acquisitions = 0

    def _git(self, cwd: str, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        return subprocess.run(  # noqa: S603 -- fixed argv, never shell
            ("git", "-C", cwd, *self._IDENTITY, *self._NO_HOOKS, *args),
            capture_output=True,
            text=True,
            timeout=120,
            check=check,
        )

    def prewarm(self, base_ref: str = "HEAD") -> list[str]:
        """Create the N slots, return their paths."""
        with self._lock:
            if self._prewarmed:
                return sorted(self._slot_paths)

            for i in range(self._size):
                unit_key = f"__pool{i}"
                info = self._worktrees.create(
                    self._working_dir, self._owner_id, unit_key, base_ref
                )
                self._slot_paths.add(info.path)
                self._slot_unit_keys[info.path] = unit_key
                self._free.append(info.path)

            self._prewarmed = True
            return sorted(self._slot_paths)

    def acquire(self, unit_key: str, base_ref: str = "HEAD") -> PooledWorktree:
        """Acquire a worktree from the pool, or fall back to on-demand creation.

        The slot retargeting sequence employs two distinct, individually
        load-bearing cleanup steps in this exact order to prevent any class of
        leftover from leaking (the test suite fails if either is removed):
        1. `git checkout --force <branch>` / `--force -B <branch> <base sha>`:
           discards modified tracked files and clears a wedged mid-merge state
           (`MERGE_HEAD` + conflicted index) left by a worker that died mid-merge.
           A plain `git checkout` silently carries local modifications across when the
           target commit does not need to touch those files, which is how a previous
           unit's edits leaked into the next one.
        2. `git clean -ffd -x` (plus `-e <dir>` per `dep_link_dirs`): the only step
           that removes untracked files and stale build artifacts. `-e` keeps the
           symlink-shared dependency dirs that `git.py` creates.

        If SubprocessWorktrees.create fails (cold path or retarget fallback),
        the exception propagates. The pool never invents a detached worktree.
        """
        if not self._prewarmed:
            self.prewarm(base_ref)

        # Resolve base_ref to a commit sha in the main working dir, once,
        # before touching the slot (never resolve inside the slot).
        base = self._git(
            self._working_dir, "rev-parse", "--verify", f"{base_ref}^{{commit}}"
        ).stdout.strip()

        slot_path: str | None = None
        with self._lock:
            while self._free:
                candidate = self._free.pop(0)
                if os.path.isdir(candidate):
                    slot_path = candidate
                    self._in_use.add(slot_path)
                    break
                else:
                    LOG.warning(
                        "Pool slot directory %s has disappeared, skipping.",
                        candidate,
                    )

        if slot_path is None:
            with self._lock:
                self._cold_fallbacks += 1
            info = self._worktrees.create(
                self._working_dir, self._owner_id, unit_key, base_ref
            )
            return PooledWorktree(
                path=info.path,
                branch=info.branch,
                base_sha=info.base_sha,
                pooled=False,
                reused=info.reused,
            )

        # We have a slot! Retarget it outside the lock.
        branch = self._worktrees.branch_name(self._owner_id, unit_key)

        try:
            # Check if the branch already exists
            branch_exists = (
                self._git(
                    self._working_dir,
                    "rev-parse",
                    "--verify",
                    "--quiet",
                    f"refs/heads/{branch}",
                    check=False,
                ).returncode
                == 0
            )

            # Step 1: switch to the target branch, forcing the checkout — this
            # is what discards modified tracked files and clears a wedged
            # mid-merge state left behind by the slot's previous occupant.
            if branch_exists:
                # attach at its tip
                self._git(slot_path, "checkout", "--force", branch)
            else:
                # create branch from base
                self._git(slot_path, "checkout", "--force", "-B", branch, base)

            # Step 2: Clean up untracked files and stale build artifacts
            clean_args = ["clean", "-ffd", "-x"]
            for d in self._dep_link_dirs:
                clean_args.extend(["-e", d])
            self._git(slot_path, *clean_args)

            base_sha = self._git(slot_path, "rev-parse", "HEAD").stdout.strip()

        except Exception as e:
            LOG.warning(
                "Failed to retarget pool slot %s: %s. Falling back to cold creation.",
                slot_path,
                e,
                exc_info=True,
            )
            # return the slot to the FREE LIST (do not leak it)
            with self._lock:
                self._in_use.discard(slot_path)
                if slot_path not in self._free:
                    self._free.append(slot_path)
                self._cold_fallbacks += 1

            # Fall back to cold creation; let create() exceptions propagate
            info = self._worktrees.create(
                self._working_dir, self._owner_id, unit_key, base_ref
            )
            return PooledWorktree(
                path=info.path,
                branch=info.branch,
                base_sha=info.base_sha,
                pooled=False,
                reused=info.reused,
            )

        with self._lock:
            self._acquisitions += 1

        return PooledWorktree(
            path=slot_path,
            branch=branch,
            base_sha=base_sha,
            pooled=True,
            reused=branch_exists,
        )

    def release(self, path: str, *, salvage: bool = True) -> bool:
        """Release a slot back to the free list, optionally salvaging uncommitted work."""
        with self._lock:
            if path not in self._slot_paths:
                return False
            if path not in self._in_use:
                # Already free/released, so no-op and return True to be idempotent.
                return True
            self._in_use.remove(path)

        if salvage:
            try:
                if self._worktrees.dirty_paths(path):
                    self._worktrees.salvage_commit(
                        path,
                        "worktree pool: salvage partial work before slot recycle",
                    )
            except Exception:
                LOG.warning(
                    "Failed to salvage partial work for %s on release",
                    path,
                    exc_info=True,
                )

        try:
            self._git(path, "checkout", "--detach", check=False)
        except Exception:
            LOG.warning("Failed to detach slot %s on release", path, exc_info=True)

        with self._lock:
            if path not in self._free:
                self._free.append(path)
            return True

    def shutdown(self, *, salvage: bool = True) -> list[str]:
        """Remove every pooled slot + its temp branch, returning their paths."""
        with self._lock:
            paths = sorted(self._slot_paths)

        removed_paths: list[str] = []

        for path in paths:
            status = "failed"
            try:
                outcome = self._worktrees.remove(
                    self._working_dir,
                    path,
                    salvage=salvage,
                    message="worktree pool: salvage partial work before shutdown",
                )
                status = outcome.status
            except Exception:
                LOG.warning(
                    "Failed to remove worktree %s during shutdown",
                    path,
                    exc_info=True,
                )

            if status == "removed":
                removed_paths.append(path)
                unit_key = self._slot_unit_keys.get(path)
                if unit_key:
                    branch = self._worktrees.branch_name(self._owner_id, unit_key)
                    try:
                        self._git(
                            self._working_dir, "branch", "-D", branch, check=False
                        )
                    except Exception:
                        pass

        with self._lock:
            for path in removed_paths:
                self._slot_paths.discard(path)
                self._slot_unit_keys.pop(path, None)
                if path in self._free:
                    self._free.remove(path)
                self._in_use.discard(path)

            if not self._slot_paths:
                self._prewarmed = False

        return removed_paths

    def stats(self) -> dict[str, int]:
        """Return size / free / in_use / cold_fallbacks / acquisitions."""
        with self._lock:
            return {
                "size": self._size,
                "free": len(self._free),
                "in_use": len(self._in_use),
                "cold_fallbacks": self._cold_fallbacks,
                "acquisitions": self._acquisitions,
            }
