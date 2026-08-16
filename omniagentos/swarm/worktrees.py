"""Swarm worktree isolation (merge-model Phase 2) — the coordinator's worktree
surface.

The MECHANISM now lives in ``omniagentos/worktrees/git.py``
(``SubprocessWorktrees``), parameterized on a branch namespace + a var root so
every parallel lane can use it. This module is swarm's BINDING of it plus the
swarm-only rollout config; the layout it produces is unchanged:
``swarm/<run_id>/<task_key>`` branches under
``default_swarm_var_root()/'worktrees'/<run_id>/<task_key>`` (D3: inside
``<repo>/var`` so the F-015 workspace floor approves it — the
``~/OmniAgentOS-worktrees`` convention is DEV-only and would 403 in
board-files/API surfaces).

Phase 1 ran every worker in ONE shared git checkout with coordinator-owned
snapshot/pathspec commits. Phase 2 gives each non-integration/bootstrap task a
PRIVATE ``git worktree`` on a per-task branch. Workers commit freely inside
their worktree; the coordinator merges each branch ``--no-ff`` into the main
workspace at CONFIRM time, which is automatically topological (task eligibility
already requires all deps done, and done ⇒ merged-at-confirm).

Everything mirrors the ``SwarmGitProto`` idiom: an injectable seam
(``SwarmWorktreesProto``), one real subprocess implementation with a pinned
commit identity, and the rule that EVERY call is made by the coordinator (or
its worker threads) under the run's ``git_lock`` — workers never run
``git worktree`` themselves (their brief forbids it and their Seatbelt write
roots only cover their own worktree + the git common dir).

Ref-tampering surface (M3), hook disabling (m9) and the salvage/merge
rationales are documented on ``omniagentos.worktrees.git`` — they are
properties of the mechanism, not of swarm.

Rollout is opt-in (D4) and stays HERE because it reads swarm's own config:
``configs/swarm.yaml`` ``worktrees.enabled`` (default false) with the
``OMNIAGENTOS_SWARM_WORKTREES`` env override winning either way. With the flag
off the scheduler's Phase-1 same-directory path runs byte-identical (pinned by
test).
"""

from __future__ import annotations

import logging
import os
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, cast

from omniagentos.path_containment import inode_relative_parts_anchored
from omniagentos.scope.paths import safe_component
from omniagentos.swarm.spawn import default_swarm_var_root
from omniagentos.worktrees.git import (
    MergeOutcome,
    MergeStatus,
    RemoveOutcome,
    RemoveStatus,
    SubprocessWorktrees,
    WorktreeInfo,
    WorktreesProto,
)

LOG = logging.getLogger(__name__)

# Env override for drills: beats configs/swarm.yaml worktrees.enabled in BOTH
# directions (a truthy value enables, a falsy value force-disables).
WORKTREES_ENV = "OMNIAGENTOS_SWARM_WORKTREES"

_TRUTHY = frozenset({"1", "true", "yes", "on"})
_FALSY = frozenset({"0", "false", "no", "off"})

# B2 / CORAL worker context. This is intentionally independent of the
# worktree-isolation switch: an integration caller can provision the hub into
# any already-created worker directory, while SubprocessSwarmWorktrees wires it
# into the private-worktree lifecycle.
CORAL_CONTEXT_ENV = "OMNIAGENTOS_CORAL_CONTEXT_MODE"
# Keep worktree plumbing below the repository's already-gitignored ``var/``
# root. Workers and salvage both use ``git add -A``; a top-level ``.coral``
# directory would otherwise stage shared-context symlinks into task commits.
CORAL_HUB_DIR = "var/coral"
CoralContextMode = Literal["off", "shadow", "enforce"]
CORAL_CONTEXT_MODES: tuple[CoralContextMode, ...] = ("off", "shadow", "enforce")
DEFAULT_CORAL_CONTEXT_MODE: CoralContextMode = "off"

# A worker never receives an unbounded view of the shared root. The coordinator
# selects at most twelve skill bodies plus four excerpts/notes of each other
# kind, and no linked file may exceed 64 KiB. The prompt integration applies
# its smaller byte cap when it reads a fallback excerpt.
CORAL_KIND_LIMITS: dict[str, int] = {"skills": 12, "playbooks": 4, "runs": 4}
CORAL_MAX_FILE_BYTES = 64 * 1024
CORAL_MAX_TOTAL_BYTES = 256 * 1024
_CORAL_GITIGNORE = "*\n"

# Gitignored dependency dirs shared into each worktree by symlink when present
# in the main workspace (a worktree checkout carries tracked files only).
# ``.venv``/``venv`` are deliberately ABSENT: python venvs embed absolute paths
# (script shebangs + pyvenv.cfg) that do not survive symlinking under another
# root — venv-needing worktrees are the install_per_worktree case instead.
DEFAULT_DEP_LINK_DIRS: tuple[str, ...] = ("node_modules", "vendor", "target")

# The seam type is the generic one; the alias keeps swarm's vocabulary (and
# every ``SwarmWorktreesProto`` annotation in the scheduler) working.
SwarmWorktreesProto = WorktreesProto


@dataclass(frozen=True)
class CoralHubReference:
    """One validated shared document exposed inside a worker directory."""

    kind: str
    source: Path
    worker_path: str
    size_bytes: int


def coral_context_mode(value: str | None = None) -> CoralContextMode:
    """Resolve the CORAL rollout gate, defaulting invalid or absent values off."""

    raw = os.environ.get(CORAL_CONTEXT_ENV, "") if value is None else value
    normalized = str(raw).strip().lower()
    if normalized in CORAL_CONTEXT_MODES:
        return cast(CoralContextMode, normalized)
    return DEFAULT_CORAL_CONTEXT_MODE


def default_coral_shared_root(var_root: Path | None = None) -> Path:
    """Canonical approved root populated by the CORAL context curator."""

    root = var_root if var_root is not None else default_swarm_var_root()
    return Path(root) / "coral"


def _ensure_coral_root(coral_shared_root: Path) -> None:
    """Create the empty CORAL root and its bounded category directories.

    This is deliberately explicit and idempotent. In particular,
    :class:`SubprocessSwarmWorktrees` construction does not call it, so merely
    resolving a worktree seam never writes operator or campaign state.

    ``OSError`` is allowed to escape, but carries the exact mkdir target and a
    stable ``mkdir failed`` marker so callers can surface an actionable error.
    """
    root = Path(coral_shared_root).expanduser()
    for path in (root, *(root / kind for kind in CORAL_KIND_LIMITS)):
        try:
            path.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise OSError(
                exc.errno,
                f"CORAL mkdir failed for {path}: {exc.strerror or exc}",
                str(path),
            ) from exc


def _path_within(path: Path, root: Path) -> bool:
    return inode_relative_parts_anchored(path, root) is not None


def _coral_source(
    approved_root: Path,
    kind: str,
    candidate: str | Path,
) -> tuple[Path, str, int]:
    """Validate one relative source and return its resolved path and link name."""

    relative = Path(candidate)
    if relative.is_absolute() or ".." in relative.parts:
        raise ValueError(f"CORAL source must be relative and traversal-free: {candidate!s}")
    if len(relative.parts) != 2 or relative.parts[0] != kind:
        raise ValueError(f"CORAL {kind} source must have form {kind}/<file>: {candidate!s}")
    name = relative.parts[1]
    if safe_component(name) != name or name in {".", ".."}:
        raise ValueError(f"unsafe CORAL link name: {name!r}")
    try:
        resolved = (approved_root / relative).resolve(strict=True)
    except OSError as exc:
        raise ValueError(f"CORAL source does not resolve: {candidate!s}") from exc
    if not _path_within(resolved, approved_root):
        raise ValueError(f"CORAL source escapes approved shared root: {candidate!s}")
    if not resolved.is_file():
        raise ValueError(f"CORAL source is not a regular file: {candidate!s}")
    size = resolved.stat().st_size
    if size > CORAL_MAX_FILE_BYTES:
        raise ValueError(f"CORAL source exceeds {CORAL_MAX_FILE_BYTES} byte limit: {candidate!s}")
    return resolved, name, size


def discover_coral_sources(
    approved_shared_root: str | Path,
) -> dict[str, tuple[str, ...]]:
    """Select a deterministic bounded view of a canonical shared CORAL root.

    Only direct files under the three approved category directories are
    candidates. Nested trees and extra top-level content are never exposed.
    """

    try:
        root = Path(approved_shared_root).resolve(strict=True)
    except OSError as exc:
        raise ValueError(f"CORAL shared root does not resolve: {approved_shared_root!s}") from exc
    if not root.is_dir():
        raise ValueError(f"CORAL shared root is not a directory: {approved_shared_root!s}")

    selected: dict[str, tuple[str, ...]] = {}
    for kind, limit in CORAL_KIND_LIMITS.items():
        category = root / kind
        if not category.is_dir():
            selected[kind] = ()
            continue
        names = sorted(
            (path.name for path in category.iterdir() if path.is_file() or path.is_symlink()),
            key=str.casefold,
        )
        selected[kind] = tuple(f"{kind}/{name}" for name in names[:limit])
    return selected


def _clean_coral_hub(hub: Path) -> None:
    """Clear only plumbing links from a hub previously created by this module."""

    if hub.is_symlink() or (hub.exists() and not hub.is_dir()):
        raise ValueError(f"CORAL hub path is not a managed directory: {hub}")
    hub.mkdir(parents=True, exist_ok=True)
    allowed = {*CORAL_KIND_LIMITS, ".gitignore"}
    unexpected = [child for child in hub.iterdir() if child.name not in allowed]
    if unexpected:
        raise ValueError(f"refusing to replace unmanaged CORAL hub entry: {unexpected[0]}")
    ignore = hub / ".gitignore"
    if ignore.is_symlink() or (ignore.exists() and not ignore.is_file()):
        raise ValueError(f"CORAL ignore path is not a managed file: {ignore}")
    ignore.write_text(_CORAL_GITIGNORE, encoding="utf-8")
    for kind in CORAL_KIND_LIMITS:
        category = hub / kind
        if category.is_symlink() or (category.exists() and not category.is_dir()):
            raise ValueError(f"CORAL category path is not a managed directory: {category}")
        category.mkdir(exist_ok=True)
        for child in category.iterdir():
            if not child.is_symlink():
                raise ValueError(f"refusing to replace unmanaged CORAL hub entry: {child}")
            child.unlink()


def _coral_hub_path(worker: Path) -> Path:
    """Resolve the hub parent without allowing a worker-side symlink escape."""

    relative = Path(CORAL_HUB_DIR)
    parent = worker / relative.parent
    if parent.is_symlink() or parent.exists():
        try:
            resolved_parent = parent.resolve(strict=True)
        except OSError as exc:
            raise ValueError(f"CORAL hub parent does not resolve: {parent}") from exc
        if not resolved_parent.is_dir() or not _path_within(resolved_parent, worker):
            raise ValueError(f"CORAL hub parent escapes worker directory: {parent}")
    else:
        parent.mkdir(parents=True)
    return worker / relative


def provision_coral_hub(
    worktree_path: str | Path,
    approved_shared_root: str | Path,
    *,
    skill_bodies: Sequence[str | Path] = (),
    playbook_excerpts: Sequence[str | Path] = (),
    recent_run_notes: Sequence[str | Path] = (),
    mode: CoralContextMode | str | None = None,
) -> tuple[CoralHubReference, ...]:
    """Expose a bounded, validated CORAL context set inside one worker tree.

    Sources are paths relative to ``approved_shared_root`` and must live
    directly below ``skills/``, ``playbooks/`` or ``runs/`` respectively.
    Validation resolves every source (including symlinks) before any worktree
    mutation, so traversal and symlink escapes fail atomically. ``off`` is a
    strict no-op and does not even require either path to exist.
    """

    resolved_mode = coral_context_mode() if mode is None else coral_context_mode(str(mode))
    if resolved_mode == "off":
        return ()

    try:
        root = Path(approved_shared_root).resolve(strict=True)
    except OSError as exc:
        raise ValueError(f"CORAL shared root does not resolve: {approved_shared_root!s}") from exc
    if not root.is_dir():
        raise ValueError(f"CORAL shared root is not a directory: {approved_shared_root!s}")
    try:
        worker = Path(worktree_path).resolve(strict=True)
    except OSError as exc:
        raise ValueError(f"worker directory does not resolve: {worktree_path!s}") from exc
    if not worker.is_dir():
        raise ValueError(f"worker path is not a directory: {worktree_path!s}")

    requested: dict[str, Sequence[str | Path]] = {
        "skills": skill_bodies,
        "playbooks": playbook_excerpts,
        "runs": recent_run_notes,
    }
    validated: list[tuple[str, Path, str, int]] = []
    total_bytes = 0
    for kind, candidates in requested.items():
        limit = CORAL_KIND_LIMITS[kind]
        if len(candidates) > limit:
            raise ValueError(f"CORAL {kind} exceeds {limit} item limit")
        seen_names: set[str] = set()
        for candidate in candidates:
            source, name, size = _coral_source(root, kind, candidate)
            if name in seen_names:
                raise ValueError(f"duplicate CORAL {kind} link name: {name}")
            seen_names.add(name)
            total_bytes += size
            if total_bytes > CORAL_MAX_TOTAL_BYTES:
                raise ValueError(f"CORAL hub exceeds {CORAL_MAX_TOTAL_BYTES} total byte limit")
            validated.append((kind, source, name, size))

    references: list[CoralHubReference] = []
    if resolved_mode == "shadow":
        for kind, source, name, size in validated:
            reference = CoralHubReference(
                kind=kind,
                source=source,
                worker_path=(Path(CORAL_HUB_DIR) / kind / name).as_posix(),
                size_bytes=size,
            )
            references.append(reference)
            LOG.info(
                "CORAL shadow mode: would provision %s from %s",
                reference.worker_path,
                source,
            )
        return tuple(references)

    hub = _coral_hub_path(worker)
    _clean_coral_hub(hub)
    for kind, source, name, size in validated:
        target = hub / kind / name
        os.symlink(source, target)
        resolved_target = target.resolve(strict=True)
        if not _path_within(resolved_target, root):
            # Defensive postcondition: even the newly provisioned link must
            # still resolve under the approved root.
            target.unlink(missing_ok=True)
            raise ValueError(f"provisioned CORAL link escapes approved root: {target}")
        references.append(
            CoralHubReference(
                kind=kind,
                source=source,
                worker_path=(Path(CORAL_HUB_DIR) / kind / name).as_posix(),
                size_bytes=size,
            )
        )
    return tuple(references)


def coral_hub_references(
    worktree_path: str | Path,
    approved_shared_root: str | Path,
    *,
    mode: CoralContextMode | str | None = None,
) -> tuple[CoralHubReference, ...]:
    """Discover the bounded CORAL selection and provision it only in enforce mode."""

    if (coral_context_mode() if mode is None else coral_context_mode(str(mode))) == "off":
        return ()
    sources = discover_coral_sources(approved_shared_root)
    return provision_coral_hub(
        worktree_path,
        approved_shared_root,
        skill_bodies=sources["skills"],
        playbook_excerpts=sources["playbooks"],
        recent_run_notes=sources["runs"],
        mode=mode,
    )


def worktrees_config() -> dict[str, Any]:
    """The ``worktrees:`` block of configs/swarm.yaml (``{}`` when absent)."""
    try:
        from omniagentos.routing.limit_state import load_swarm_config

        raw = load_swarm_config().get("worktrees")
    except Exception:  # noqa: BLE001 -- a broken config never breaks Phase-1 fallback.
        LOG.debug("could not load worktrees config", exc_info=True)
        return {}
    return dict(raw) if isinstance(raw, dict) else {}


def swarm_worktrees_enabled() -> bool:
    """D4 opt-in flag: env override wins, else configs/swarm.yaml (default off).

    H-35: production collision safety keeps worktrees off by default. A
    drill-time env force-on still works (Phase-2 tests). When config sets
    ``enabled: true`` without the env override, enforce-grade isolation also
    requires the L11 fencing gate (``collision_safety.swarm_worktrees_enforceable``)
    so a premature config flip cannot ship dark isolation without fencing.
    """
    env = os.environ.get(WORKTREES_ENV, "").strip().lower()
    if env in _TRUTHY:
        return True
    if env in _FALSY:
        return False
    if not bool(worktrees_config().get("enabled")):
        return False
    # Config asked for on: allow only when collision-safety enforcement is
    # permitted (L11 fencing ready + evidence), otherwise stay dark.
    try:
        from omniagentos.swarm.collision_safety import swarm_worktrees_enforceable

        if not swarm_worktrees_enforceable():
            LOG.warning(
                "swarm worktrees.enabled=true blocked until L11 fencing is ready "
                "(set OMNIAGENTOS_SWARM_WORKTREES=1 for drill-time force-on)"
            )
            return False
    except Exception:  # noqa: BLE001 -- a gate import failure must not enable.
        LOG.warning("collision safety gate unavailable; keeping worktrees off", exc_info=True)
        return False
    return True


def config_dep_link_dirs() -> tuple[str, ...]:
    """Names symlink-shared into each worktree; empty when the config opts for
    per-worktree installs instead (``install_per_worktree: true``)."""
    config = worktrees_config()
    if config.get("install_per_worktree"):
        return ()
    raw = config.get("dep_link_dirs")
    if isinstance(raw, list):
        return tuple(str(name).strip() for name in raw if str(name).strip())
    return DEFAULT_DEP_LINK_DIRS


class SubprocessSwarmWorktrees(SubprocessWorktrees):
    """Swarm's binding of the shared worktree mechanism.

    Construction performs zero filesystem writes. CORAL's root and category
    directories are created lazily by ``create()`` through
    :func:`_ensure_coral_root`.

    Fixes the branch namespace to ``swarm`` (branches stay
    ``swarm/<run_id>/<task_key>``) and defaults the var root to
    ``default_swarm_var_root()`` (worktrees stay at
    ``var/swarm/worktrees/<run_id>/<task_key>``). ``dep_link_dirs`` defaults to
    the swarm config's answer — resolved HERE, at construction, so
    ``configs/swarm.yaml`` remains swarm's business and the generic mechanism
    stays config-free.
    """

    def __init__(
        self,
        *,
        var_root: Path | None = None,
        dep_link_dirs: Sequence[str] | None = None,
        coral_shared_root: Path | None = None,
        coral_mode: CoralContextMode | str | None = None,
        lock_retry_attempts: int = 3,
        lock_retry_sleep: float = 0.5,
    ) -> None:
        resolved_var_root = var_root if var_root is not None else default_swarm_var_root()
        self._coral_mode = (
            coral_context_mode() if coral_mode is None else coral_context_mode(str(coral_mode))
        )
        configured_coral_root = (
            coral_shared_root
            if coral_shared_root is not None
            else default_coral_shared_root(resolved_var_root)
        )
        # Construction is intentionally filesystem-write-free. CORAL creation
        # is deferred to create(), where _ensure_coral_root() is the one uniform
        # first-use guard for every caller.
        self._coral_shared_root = Path(os.path.realpath(os.path.expanduser(configured_coral_root)))
        super().__init__(
            namespace="swarm",
            var_root=resolved_var_root,
            dep_link_dirs=(dep_link_dirs if dep_link_dirs is not None else config_dep_link_dirs()),
            lock_retry_attempts=lock_retry_attempts,
            lock_retry_sleep=lock_retry_sleep,
        )

    def create(
        self,
        working_dir: str,
        owner_id: str,
        unit_key: str,
        base_ref: str,
    ) -> WorktreeInfo:
        if self._coral_mode != "off":
            try:
                _ensure_coral_root(self._coral_shared_root)
            except OSError:
                if self._coral_mode == "enforce":
                    raise
                LOG.warning(
                    "Could not initialize CORAL shared root %s; "
                    "worktree creation will apply the configured mode's backstop",
                    self._coral_shared_root,
                    exc_info=True,
                )
            if self._coral_mode == "enforce":
                coral_root_empty = all(
                    not any((self._coral_shared_root / kind).iterdir())
                    for kind in CORAL_KIND_LIMITS
                )
                if coral_root_empty:
                    LOG.warning(
                        "CORAL enforce shared root %s has no usable content; "
                        "workers will receive an empty CORAL context until the root is populated",
                        self._coral_shared_root,
                    )
        info = super().create(working_dir, owner_id, unit_key, base_ref)
        if self._coral_mode == "off":
            return info
        if not self._coral_shared_root.is_dir():
            if self._coral_mode == "enforce":
                raise ValueError(
                    f"CORAL shared root is unavailable: {self._coral_shared_root}. "
                    "Create it as a directory and populate its skills/, playbooks/, "
                    "and runs/ categories before using enforce mode."
                )
            LOG.info(
                "CORAL shadow mode: shared root %s is absent or not a directory; "
                "worktree left unchanged",
                self._coral_shared_root,
            )
            return info
        coral_hub_references(
            info.path,
            self._coral_shared_root,
            mode=self._coral_mode,
        )
        return info

    def _filter_paths(self, lines: Iterable[str]) -> list[str]:
        filtered = super()._filter_paths(lines)
        if self._coral_mode != "enforce":
            return filtered
        prefix = f"{CORAL_HUB_DIR}/"
        return [line for line in filtered if line != CORAL_HUB_DIR and not line.startswith(prefix)]


__all__ = [
    "CORAL_CONTEXT_ENV",
    "CORAL_CONTEXT_MODES",
    "CORAL_HUB_DIR",
    "CORAL_KIND_LIMITS",
    "CORAL_MAX_FILE_BYTES",
    "CORAL_MAX_TOTAL_BYTES",
    "CoralContextMode",
    "CoralHubReference",
    "DEFAULT_CORAL_CONTEXT_MODE",
    "DEFAULT_DEP_LINK_DIRS",
    "WORKTREES_ENV",
    "MergeOutcome",
    "MergeStatus",
    "RemoveOutcome",
    "RemoveStatus",
    "SubprocessSwarmWorktrees",
    "SwarmWorktreesProto",
    "WorktreeInfo",
    "coral_context_mode",
    "coral_hub_references",
    "config_dep_link_dirs",
    "default_coral_shared_root",
    "discover_coral_sources",
    "provision_coral_hub",
    "swarm_worktrees_enabled",
    "worktrees_config",
]
