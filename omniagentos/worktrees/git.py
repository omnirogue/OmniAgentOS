"""Lane-agnostic ``git worktree`` machinery — the isolation mechanism every
parallel lane shares.

This is the swarm Phase-2 worktree implementation lifted VERBATIM out of
``omniagentos/swarm/worktrees.py`` and parameterized on two things only:

- ``namespace`` — the branch namespace (``<namespace>/<owner_id>/<unit_key>``);
- ``var_root``  — the lane's var root (worktrees land at
  ``<var_root>/worktrees/<owner_id>/<unit_key>``).

``owner_id`` is whatever groups a batch of parallel units for one lane (a swarm
``run_id``, a longhaul task, a fusion build) and ``unit_key`` is the individual
unit of work. ``omniagentos.swarm.worktrees.SubprocessSwarmWorktrees`` is the
swarm binding (``namespace="swarm"``, ``var_root=default_swarm_var_root()``),
which keeps its historical branch/path layout byte-identical.

Model: each parallel unit gets a PRIVATE ``git worktree`` on its own branch
under the repo's ``var`` tree (D3: inside ``<repo>/var`` so the F-015 workspace
floor approves it — the ``~/OmniAgentOS-worktrees`` convention is DEV-only and
would 403 in board-files/API surfaces). Workers commit freely inside their
worktree; the coordinator merges each branch ``--no-ff`` into the main
workspace when the unit is confirmed.

Everything here mirrors the ``SwarmGitProto`` idiom: an injectable seam
(``WorktreesProto``), one real subprocess implementation with a pinned commit
identity, and the rule that EVERY call is made by the coordinator (or its
worker threads) under the lane's git lock — workers never run ``git worktree``
themselves (their brief forbids it and their Seatbelt write roots only cover
their own worktree + the git common dir).

Ref-tampering surface (M3): workers get the git COMMON dir as a Seatbelt
write root, with ``refs/heads`` + ``packed-refs`` DENIED and only the owner's
own ``refs/heads/<namespace>/<owner_id>/`` namespace re-allowed (they must
still commit their own branch; ref lockfiles live beside the refs). ``main``
and every other owner's refs are therefore unwritable from a worker sandbox.
ACCEPTED RESIDUAL: same-owner sibling refs inside the namespace stay mutually
writable — neutralized by SHA-merge: the quality gate captures the exact
worktree HEAD it diffed/verified and CONFIRM merges that sha, never the ref,
so a tampered sibling ref cannot change what lands in main.

Hooks are disabled (``-c core.hooksPath=``) on EVERY git invocation (m9):
a repo's checkout/commit hooks (e.g. a migration guard) firing inside the
coordinator's mechanical git plumbing would make creation/merge/salvage
nondeterministic — verified live: a failing ``post-checkout`` hook fails
``git worktree add`` outright, and a failing ``pre-commit`` hook would have
silently skipped salvage commits. Salvage commits are identity-pinned and
bypass user hooks by design, same as ``SubprocessSwarmGit`` (which carries
the same blanket disable). In worktree mode this is also defense in depth:
a worker with the git common dir writable must never get a planted hook
executed by coordinator plumbing (the Seatbelt ``.git/hooks`` write deny is
the first layer).
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import time
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Protocol

from omniagentos.path_containment import inode_paths_equal
from omniagentos.scope.paths import safe_component

LOG = logging.getLogger(__name__)


# --------------------------------------------------------------------------
# Read-only allowlist — Phase-A inventory tooling's ONLY way to invoke git.
#
# Nothing below this block is used by ``SubprocessWorktrees``'s own mutation
# methods (those keep their unrestricted ``_git`` for the same reason this
# module exists: creating/removing/merging worktrees IS mutation). This is
# the choke point ``scripts/hygiene/worktree_inventory.py`` routes every git
# call through, so "Phase A never mutates" is enforced mechanically rather
# than by convention — a reviewer (or a test) can assert the allowlist
# rejects each mutating verb directly, no need to audit every call site.
# --------------------------------------------------------------------------

READONLY_GIT_SUBCOMMANDS = frozenset(
    {"status", "rev-list", "rev-parse", "for-each-ref", "merge-base", "ls-files", "log", "show"}
)


def assert_readonly_git_args(args: Sequence[str]) -> None:
    """Raise ``ValueError`` unless ``args`` is one of a small, explicit set of
    read-only git invocations.

    ``worktree`` is special-cased: ONLY ``worktree list`` is permitted —
    ``worktree add``/``remove``/``prune`` all mutate and are refused. Every
    other top-level word (``branch`` included) is refused outright by simply
    not being in :data:`READONLY_GIT_SUBCOMMANDS`: ``branch -d``/``-D``
    mutate under the same top-level word as harmless listing forms, so rather
    than trying to enumerate every safe vs. unsafe ``branch`` flag
    combination, callers that need branch listing use ``for-each-ref``
    instead (which has no mutating form at all, `--merged`/`--no-merged`
    included)."""
    if not args:
        raise ValueError("empty git argv is not a valid read-only invocation")
    head = args[0]
    if head == "worktree":
        if len(args) < 2 or args[1] != "list":
            sub = args[1] if len(args) > 1 else "<none>"
            raise ValueError(
                f"git worktree {sub!r} is not read-only; only 'git worktree list' is allowlisted"
            )
        return
    if head not in READONLY_GIT_SUBCOMMANDS:
        raise ValueError(
            f"git {head!r} is not in the read-only allowlist "
            f"({sorted(READONLY_GIT_SUBCOMMANDS)} plus 'worktree list' only)"
        )


def run_readonly_git(
    args: Sequence[str], cwd: str, *, timeout: float = 30.0
) -> subprocess.CompletedProcess[str]:
    """Run one git command after :func:`assert_readonly_git_args` clears it.

    Raises ``ValueError`` (never runs the subprocess) for anything not on the
    allowlist. This is the ONLY git entry point Phase-A inventory tooling is
    allowed to call."""
    assert_readonly_git_args(args)
    return subprocess.run(  # noqa: S603 -- fixed argv, never shell, allowlist-checked above
        ["git", "-C", cwd, *args],
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


def parse_worktree_porcelain(text: str) -> list[dict[str, str]]:
    """Parse ``git worktree list --porcelain`` stdout into one dict per
    worktree entry (blank-line-delimited ``key value`` records).

    Shared by :meth:`SubprocessWorktrees._porcelain` (mutation machinery
    below) and the read-only Phase-A inventory
    (``scripts/hygiene/worktree_inventory.py``) so this parsing exists in
    exactly one place instead of two copies drifting apart."""
    entries: list[dict[str, str]] = []
    current: dict[str, str] = {}
    for line in text.splitlines():
        if not line.strip():
            if current:
                entries.append(current)
                current = {}
            continue
        key, _, value = line.partition(" ")
        current[key] = value
    if current:
        entries.append(current)
    return entries


@dataclass(frozen=True)
class WorktreeInfo:
    """One created/reused unit worktree."""

    path: str
    branch: str
    base_sha: str
    reused: bool = False


MergeStatus = Literal["merged", "conflict", "noop"]


@dataclass(frozen=True)
class MergeOutcome:
    """Result of merging one unit branch into the main workspace."""

    status: MergeStatus
    sha: str | None = None
    conflict_files: tuple[str, ...] = ()
    detail: str = ""


RemoveStatus = Literal["removed", "salvage_failed"]


@dataclass(frozen=True)
class RemoveOutcome:
    """Result of removing one unit worktree (R2b).

    ``salvage_failed`` means a salvage was requested, the tree is STILL dirty,
    and the salvage commit could not be made — the worktree was LEFT IN PLACE
    (never rmtree'd over unpersisted work); callers log and skip it."""

    status: RemoveStatus
    salvage_sha: str | None = None


class WorktreesProto(Protocol):
    """Injectable worktree seam (fake in tests, subprocess in production).

    Callers hold the lane's git lock for every mutation."""

    def supported(self, working_dir: str) -> bool: ...

    def git_common_dir(self, working_dir: str) -> str | None: ...

    def worktree_git_dir(self, path: str) -> str | None: ...

    def create(
        self, working_dir: str, owner_id: str, unit_key: str, base_ref: str
    ) -> WorktreeInfo: ...

    def remove(
        self, working_dir: str, path: str, *, salvage: bool, message: str = ""
    ) -> RemoveOutcome: ...

    def head_sha(self, path: str) -> str | None: ...

    def merge_branch(
        self, working_dir: str, branch: str, message: str, *, sha: str | None = None
    ) -> MergeOutcome: ...

    def has_pending_merge(self, working_dir: str) -> bool: ...

    def abort_merge(self, working_dir: str) -> bool: ...

    def changed_paths_since(self, path: str, base_sha: str) -> list[str]: ...

    def dirty_paths(self, path: str) -> list[str]: ...

    def salvage_commit(self, path: str, message: str) -> str | None: ...

    def list_run_worktrees(self, working_dir: str, owner_id: str) -> list[tuple[str, str]]: ...

    def prune_orphans(
        self, working_dir: str, owner_id: str, live_task_keys: Iterable[str]
    ) -> list[str]: ...

    def delete_run_branches(self, working_dir: str, owner_id: str) -> list[str]: ...


class SubprocessWorktrees:
    """Real worktree ops via subprocess; identity pinned like
    ``SubprocessSwarmGit`` so salvage/merge commits never depend on ambient
    git config, hooks disabled for determinism.

    m9: ``-c core.hooksPath=`` is baked into EVERY invocation (``_git``), not
    just ``worktree add``/``merge`` — the module docstring's claim that
    salvage commits bypass user hooks is only true if the salvage ``add``/
    ``commit`` themselves carry the disable, and a worker-planted hook in a
    writable common dir must never fire inside coordinator plumbing (the
    Seatbelt ``.git/hooks`` write deny is the first layer; this is depth)."""

    _IDENTITY = (
        "-c",
        "user.email=4580856+omniagentos-bot[bot]@users.noreply.github.com",
        "-c",
        "user.name=OmniAgentOS Swarm",
    )
    _NO_HOOKS = ("-c", "core.hooksPath=")

    def __init__(
        self,
        *,
        namespace: str,
        var_root: Path,
        dep_link_dirs: Sequence[str] | None = None,
        lock_retry_attempts: int = 3,
        lock_retry_sleep: float = 0.5,
    ) -> None:
        self._namespace = safe_component(namespace)
        self._var_root = Path(var_root)
        self._dep_link_dirs = tuple(dep_link_dirs) if dep_link_dirs is not None else ()
        self._lock_retry_attempts = max(1, int(lock_retry_attempts))
        self._lock_retry_sleep = max(0.0, float(lock_retry_sleep))

    # -- plumbing ------------------------------------------------------------

    def _git(self, cwd: str, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        return subprocess.run(  # noqa: S603 -- fixed argv, never shell
            ("git", "-C", cwd, *self._IDENTITY, *self._NO_HOOKS, *args),
            capture_output=True,
            text=True,
            timeout=120,
            check=check,
        )

    def worktree_path(self, owner_id: str, unit_key: str) -> Path:
        """D3 placement: ``<var_root>/worktrees/<owner_id>/<unit_key>`` — under
        the repo var tree so the F-015 workspace floor approves it."""
        return self._var_root / "worktrees" / safe_component(owner_id) / safe_component(unit_key)

    def branch_name(self, owner_id: str, unit_key: str) -> str:
        return f"{self._namespace}/{safe_component(owner_id)}/{safe_component(unit_key)}"

    def branch_prefix(self, owner_id: str) -> str:
        return f"{self._namespace}/{safe_component(owner_id)}/"

    # -- probes --------------------------------------------------------------

    def supported(self, working_dir: str) -> bool:
        """``git worktree list`` probe — False falls the lane back to its
        same-directory path."""
        try:
            return self._git(working_dir, "worktree", "list", check=False).returncode == 0
        except (OSError, subprocess.SubprocessError):
            return False

    def git_common_dir(self, working_dir: str) -> str | None:
        """Absolute main ``.git`` dir — the extra Seatbelt write root a worker
        needs before ``git commit`` inside a worktree can write objects/refs."""
        try:
            proc = self._git(working_dir, "rev-parse", "--git-common-dir", check=False)
        except (OSError, subprocess.SubprocessError):
            return None
        raw = proc.stdout.strip()
        if proc.returncode != 0 or not raw:
            return None
        if not os.path.isabs(raw):
            raw = os.path.join(working_dir, raw)
        return os.path.realpath(raw)

    def worktree_git_dir(self, path: str) -> str | None:
        """Absolute PRIVATE gitdir of one linked worktree
        (``<common>/worktrees/<id>`` — ``git rev-parse --absolute-git-dir``
        run IN the worktree). R1: the scheduler passes it as an extra Seatbelt
        write root at spawn, so the profile can deny the whole
        ``<common>/worktrees`` subtree (sibling-gitdir HEAD hijack) and
        re-open ONLY the worker's own."""
        try:
            proc = self._git(path, "rev-parse", "--absolute-git-dir", check=False)
        except (OSError, subprocess.SubprocessError):
            return None
        raw = proc.stdout.strip()
        if proc.returncode != 0 or not raw:
            return None
        return os.path.realpath(raw)

    # -- create / remove -----------------------------------------------------

    def create(self, working_dir: str, owner_id: str, unit_key: str, base_ref: str) -> WorktreeInfo:
        """Create (or reuse) the unit worktree.

        A still-registered worktree at the canonical path is reused AS-IS so a
        successor attempt inherits uncommitted partial work; a leftover
        directory that git no longer registers is cleared first. When the
        unit branch already exists the worktree is attached AT ITS TIP
        (``worktree add <path> <branch>``) so salvage-committed partial work
        relays forward; otherwise the branch is created from ``base_ref``.
        """
        path = self.worktree_path(owner_id, unit_key)
        branch = self.branch_name(owner_id, unit_key)
        if path.is_dir() and self._is_registered(working_dir, path):
            # M2a: a reaped predecessor can die holding index.lock — clear it
            # (bounded) or every git op of the successor attempt fails.
            self._clear_stale_index_lock(str(path))
            sha = self._git(str(path), "rev-parse", "HEAD").stdout.strip()
            self._link_dep_dirs(working_dir, path)
            return WorktreeInfo(path=str(path), branch=branch, base_sha=sha, reused=True)
        # Crash leftovers: unregister + clear the path before a fresh add.
        self._git(working_dir, "worktree", "remove", "--force", str(path), check=False)
        self._git(working_dir, "worktree", "prune", check=False)
        if path.exists() or path.is_symlink():
            shutil.rmtree(path, ignore_errors=True)
        path.parent.mkdir(parents=True, exist_ok=True)
        branch_exists = (
            self._git(
                working_dir,
                "rev-parse",
                "--verify",
                "--quiet",
                f"refs/heads/{branch}",
                check=False,
            ).returncode
            == 0
        )
        if branch_exists:
            self._git(working_dir, "worktree", "add", str(path), branch)
        else:
            self._git(working_dir, "worktree", "add", "-b", branch, str(path), base_ref)
        sha = self._git(str(path), "rev-parse", "HEAD").stdout.strip()
        self._link_dep_dirs(working_dir, path)
        return WorktreeInfo(path=str(path), branch=branch, base_sha=sha, reused=branch_exists)

    def _is_registered(self, working_dir: str, path: Path) -> bool:
        resolved = os.path.realpath(str(path))
        return any(
            # Safety (`is True`): reuse only a positively equal registered worktree.
            inode_paths_equal(os.path.realpath(entry.get("worktree", "")), resolved) is True
            for entry in self._porcelain(working_dir)
            if entry.get("worktree")
        )

    def _link_dep_dirs(self, working_dir: str, path: Path) -> None:
        """Symlink-share gitignored dependency dirs (item 11): cheap, generic,
        read-mostly. Concurrent writers through a shared link can race — the
        shared-file/lockfile-to-integration ownership rule bounds that; the
        ``install_per_worktree`` config is the safe-but-slow escape hatch
        (it empties this list, see ``swarm.worktrees.config_dep_link_dirs``)."""
        for name in self._dep_link_dirs:
            source = Path(working_dir) / name
            target = path / name
            try:
                if source.is_dir() and not target.exists() and not target.is_symlink():
                    os.symlink(source, target)
            except OSError:
                LOG.debug("could not symlink %s into %s", name, path, exc_info=True)

    def _clear_stale_index_lock(self, path: str) -> None:
        """M2a: clear a stale ``index.lock`` before a worktree is REUSED.

        A reaped/crashed predecessor can die mid-write leaving the lock in
        the worktree's private git dir; the successor attempt would then fail
        every git operation. Bounded wait (the same retry/sleep budget as
        salvage) gives a still-live writer time to finish; a lock that
        survives the wait has no living owner — ``create`` runs only after
        the prior attempt's session is terminal — so it is removed."""
        try:
            proc = self._git(path, "rev-parse", "--absolute-git-dir", check=False)
        except (OSError, subprocess.SubprocessError):
            return
        git_dir = proc.stdout.strip()
        if proc.returncode != 0 or not git_dir:
            return
        lock = Path(git_dir) / "index.lock"
        for attempt in range(self._lock_retry_attempts):
            if not lock.exists():
                return
            if attempt < self._lock_retry_attempts - 1:
                time.sleep(self._lock_retry_sleep)
        try:
            lock.unlink()
            LOG.warning("cleared stale index.lock in %s before worktree reuse", git_dir)
        except OSError:
            LOG.warning("could not clear stale index.lock in %s", git_dir, exc_info=True)

    def remove(
        self, working_dir: str, path: str, *, salvage: bool, message: str = ""
    ) -> RemoveOutcome:
        """Remove one worktree; with ``salvage`` the dirty state is committed
        to the branch first (partial work survives for relay/forensics).

        R2b: when salvage was requested and the tree is STILL dirty after the
        salvage attempt (persistent lock, commit failure), the worktree is
        LEFT IN PLACE — an rmtree over unpersisted work is the exact data
        loss M2c exists to prevent. Callers log and skip on
        ``salvage_failed``; the kept path is recorded durably by the
        scheduler so crash-resume GC skips it too (R2a)."""
        salvage_sha: str | None = None
        if salvage and os.path.isdir(path):
            salvage_sha = self.salvage_commit(
                path, message or f"{self._namespace}: salvage partial work"
            )
            if salvage_sha is None:
                try:
                    still_dirty = bool(self.dirty_paths(path))
                except (OSError, subprocess.SubprocessError):
                    still_dirty = True  # cannot prove clean — never rmtree blind
                if still_dirty:
                    return RemoveOutcome(status="salvage_failed")
        self._git(working_dir, "worktree", "remove", "--force", path, check=False)
        if os.path.exists(path):
            shutil.rmtree(path, ignore_errors=True)
        self._git(working_dir, "worktree", "prune", check=False)
        return RemoveOutcome(status="removed", salvage_sha=salvage_sha)

    def salvage_commit(self, path: str, message: str) -> str | None:
        """Commit dirty worktree state to its branch, tolerating a stale
        ``index.lock`` (an A2-reaped worker can die mid-write) with a bounded
        retry; after the retries salvage is SKIPPED — it never blocks removal."""
        last_error = ""
        for attempt in range(self._lock_retry_attempts):
            if attempt:
                time.sleep(self._lock_retry_sleep)
            try:
                add = self._git(path, "add", "-A", check=False)
                if add.returncode != 0:
                    last_error = add.stderr.strip()
                    if "index.lock" in add.stderr:
                        continue
                    return None
                if self._git(path, "diff", "--cached", "--quiet", check=False).returncode == 0:
                    return None  # clean tree: nothing to salvage
                commit = self._git(path, "commit", "-m", message, check=False)
                if commit.returncode != 0:
                    last_error = (commit.stderr + commit.stdout).strip()
                    if "index.lock" in commit.stderr + commit.stdout:
                        continue
                    return None
                return self._git(path, "rev-parse", "HEAD").stdout.strip()
            except (OSError, subprocess.SubprocessError):
                return None
        # R2c: the bounded retry only WAITS on the lock; a lock whose owner
        # died (A2-reaped worker) never releases. Every salvage caller runs
        # only after the attempt's session is terminal — same precondition as
        # create()'s reuse path — so clear the stale lock and try once more.
        if "index.lock" in last_error:
            self._clear_stale_index_lock(path)
            try:
                add = self._git(path, "add", "-A", check=False)
                if add.returncode == 0:
                    if self._git(path, "diff", "--cached", "--quiet", check=False).returncode == 0:
                        return None  # clean tree: nothing to salvage
                    commit = self._git(path, "commit", "-m", message, check=False)
                    if commit.returncode == 0:
                        return self._git(path, "rev-parse", "HEAD").stdout.strip()
                    last_error = (commit.stderr + commit.stdout).strip()
                else:
                    last_error = add.stderr.strip()
            except (OSError, subprocess.SubprocessError):
                pass
        LOG.warning(
            "salvage commit skipped for %s after %d attempts (stale index.lock?): %s",
            path,
            self._lock_retry_attempts,
            last_error[:200],
        )
        return None

    # -- merge ---------------------------------------------------------------

    def head_sha(self, path: str) -> str | None:
        """The worktree's current HEAD commit — captured by the quality gate
        as the exact sha it diffed/verified, and later merged AS a sha (M3)."""
        try:
            proc = self._git(path, "rev-parse", "HEAD", check=False)
        except (OSError, subprocess.SubprocessError):
            return None
        sha = proc.stdout.strip()
        return sha if proc.returncode == 0 and sha else None

    def merge_branch(
        self, working_dir: str, branch: str, message: str, *, sha: str | None = None
    ) -> MergeOutcome:
        """``merge --no-ff`` one unit branch into the main workspace.

        M3 (ref-tampering closure): when ``sha`` is given, the merge target
        is EXACTLY that commit — the sha the quality gate captured after it
        diffed/verified the worktree — not the branch ref. A branch ref
        tampered by a sandboxed sibling (same-owner refs stay writable inside
        the Seatbelt namespace allow) can then no longer change what lands
        in main. ``branch`` still names the merge for messages/routing.

        Conflicts capture the unmerged file list, then ``merge --abort`` so
        the workspace stays pristine — the branch stays alive for the
        integration unit to merge manually (D5)."""
        head_before = self._git(working_dir, "rev-parse", "HEAD").stdout.strip()
        try:
            proc = self._git(
                working_dir,
                "merge",
                "--no-ff",
                "-m",
                message,
                sha or branch,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            # M4a: a merge that dies mid-flight (TimeoutExpired/OSError, not
            # just returncode!=0) can leave MERGE_HEAD + a half-staged index
            # wedging the MAIN workspace — abort best-effort, then propagate.
            self.abort_merge(working_dir)
            raise
        if proc.returncode == 0:
            head_after = self._git(working_dir, "rev-parse", "HEAD").stdout.strip()
            if head_after == head_before:
                return MergeOutcome(status="noop", sha=head_after)
            return MergeOutcome(status="merged", sha=head_after)
        conflict = self._git(working_dir, "diff", "--name-only", "--diff-filter=U", check=False)
        files = tuple(
            sorted({line.strip() for line in conflict.stdout.splitlines() if line.strip()})
        )
        self.abort_merge(working_dir)
        detail = (proc.stderr + proc.stdout).strip()[:500]
        return MergeOutcome(status="conflict", conflict_files=files, detail=detail)

    def has_pending_merge(self, working_dir: str) -> bool:
        """True when the workspace sits mid-merge (``MERGE_HEAD`` present) —
        the wedged state a crashed coordinator can leave behind (M4c)."""
        try:
            proc = self._git(working_dir, "rev-parse", "-q", "--verify", "MERGE_HEAD", check=False)
        except (OSError, subprocess.SubprocessError):
            return False
        return proc.returncode == 0

    def abort_merge(self, working_dir: str) -> bool:
        """Best-effort ``git merge --abort``; True when it succeeded."""
        try:
            return self._git(working_dir, "merge", "--abort", check=False).returncode == 0
        except (OSError, subprocess.SubprocessError):
            return False

    # -- diff ----------------------------------------------------------------

    def changed_paths_since(self, path: str, base_sha: str) -> list[str]:
        """The unit's cumulative delta: committed range (``base..HEAD``) +
        uncommitted working-tree changes + untracked files. Workers commit
        freely in their worktree, so a HEAD-only delta would under-report.
        Symlink-shared dependency dirs are excluded (they are workspace
        plumbing, not unit output)."""
        committed = self._git(
            path, "diff", "--name-only", base_sha, "HEAD", check=False
        ).stdout.splitlines()
        return self._filter_paths((*committed, *self._dirty_lines(path)))

    def dirty_paths(self, path: str) -> list[str]:
        """Uncommitted working-tree changes + untracked files only (symlink-
        shared dependency dirs excluded) — the still-UNPERSISTED slice of a
        worktree. The scheduler's post-CONFIRM removal clean-check (M2) reads
        this: any owned path here would be destroyed by a plain remove."""
        return self._filter_paths(self._dirty_lines(path))

    def _dirty_lines(self, path: str) -> tuple[str, ...]:
        uncommitted = self._git(
            path, "diff", "--name-only", "HEAD", check=False
        ).stdout.splitlines()
        untracked = self._git(
            path, "ls-files", "--others", "--exclude-standard", check=False
        ).stdout.splitlines()
        return (*uncommitted, *untracked)

    def _filter_paths(self, lines: Iterable[str]) -> list[str]:
        linked = set(self._dep_link_dirs)
        return sorted(
            {
                line.strip()
                for line in lines
                if line.strip() and line.strip().split("/", 1)[0] not in linked
            }
        )

    # -- listing / GC --------------------------------------------------------

    def _porcelain(self, working_dir: str) -> list[dict[str, str]]:
        try:
            proc = self._git(working_dir, "worktree", "list", "--porcelain", check=False)
        except (OSError, subprocess.SubprocessError):
            return []
        return parse_worktree_porcelain(proc.stdout)

    def list_run_worktrees(self, working_dir: str, owner_id: str) -> list[tuple[str, str]]:
        """``(path, unit_key)`` for every registered worktree on one of this
        owner's branches."""
        prefix = f"refs/heads/{self.branch_prefix(owner_id)}"
        results: list[tuple[str, str]] = []
        for entry in self._porcelain(working_dir):
            branch = entry.get("branch") or ""
            path = entry.get("worktree") or ""
            if path and branch.startswith(prefix):
                results.append((path, branch[len(prefix) :]))
        return results

    def prune_orphans(
        self, working_dir: str, owner_id: str, live_task_keys: Iterable[str]
    ) -> list[str]:
        """Remove (salvaging) owner worktrees whose unit is no longer live —
        crash-resume GC. Returns the removed paths.

        ``live_task_keys`` keeps its historical name: existing callers
        (``scripts/hygiene/hygiene.py``, the swarm suite) pass it by keyword."""
        live: set[str] = set()
        for key in live_task_keys:
            try:
                live.add(safe_component(str(key)))
            except ValueError:
                continue
        removed: list[str] = []
        for path, unit_key in self.list_run_worktrees(working_dir, owner_id):
            if unit_key in live:
                continue
            outcome = self.remove(
                working_dir,
                path,
                salvage=True,
                message=f"{self._namespace} {owner_id}: salvage orphaned worktree {unit_key}",
            )
            if outcome.status == "salvage_failed":
                # R2: unpersisted work with no salvage — leave it on disk;
                # the scheduler's kept-marker path owns surfacing it.
                LOG.error(
                    "orphan prune kept worktree %s (%s): salvage failed on a dirty tree",
                    path,
                    unit_key,
                )
                continue
            removed.append(path)
        return removed

    def delete_run_branches(self, working_dir: str, owner_id: str) -> list[str]:
        """Delete every MERGED ``<namespace>/<owner_id>/*`` branch (terminal
        cleanup on COMPLETED runs only — failed runs keep branches for
        forensics).

        ``branch -d`` (merged-only), NEVER ``-D``: a branch still carrying
        unmerged work — a conflict routed to integration, or a salvage commit
        of confirmed/partial work (M2) — must survive terminal cleanup, or
        the salvage that preserved it was theater."""
        prefix = self.branch_prefix(owner_id)
        proc = self._git(
            working_dir,
            "for-each-ref",
            "--format=%(refname:short)",
            f"refs/heads/{prefix}",
            check=False,
        )
        deleted: list[str] = []
        for line in proc.stdout.splitlines():
            branch = line.strip()
            if not branch:
                continue
            if self._git(working_dir, "branch", "-d", branch, check=False).returncode == 0:
                deleted.append(branch)
        return deleted


__all__ = [
    "READONLY_GIT_SUBCOMMANDS",
    "MergeOutcome",
    "MergeStatus",
    "RemoveOutcome",
    "RemoveStatus",
    "SubprocessWorktrees",
    "WorktreeInfo",
    "WorktreesProto",
    "assert_readonly_git_args",
    "parse_worktree_porcelain",
    "run_readonly_git",
]
