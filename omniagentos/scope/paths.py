"""Pure path algebra for scope ownership: normalization, realms, containment.

This module is the ONE implementation of workspace-path reasoning. It replaces
five divergent copies, each of which had a hole the others fixed:

- ``swarm.planner.normalize_owned_path`` — rejected absolute / ``~`` /
  drive-letter / ``..``-escape paths (kept: see :func:`normalize_rel`).
- ``swarm.planner.paths_overlap`` — the only copy that handled ``"."`` (whole
  workspace) correctly (kept, and generalized: ``"."`` is now the EMPTY
  component tuple, so the special case disappears entirely).
- ``swarm.scheduler._contained_in`` / ``._paths_overlap`` — the only copies that
  compared by path COMPONENT rather than string prefix, so ``github/x`` is not
  "inside" ``.github`` (kept: see :func:`under`). They were ALSO the copies with
  the live bug — ``_paths_overlap(".", "src/a")`` returned ``False`` because
  ``".".split("/") == ["."]`` compares against ``["src"]``. That code was saved
  only because its caller stripped ``"."`` into a separate ``parent_all`` flag
  before calling it. Normalizing ``"."`` to ``()`` deletes ``parent_all`` as a
  concept.
- ``runner.core._resolve_realpath`` / ``._path_within_granted`` — realpath
  handling for realm discovery (kept), with filesystem containment now
  delegated to the repository-wide inode-ancestry primitive (see
  :func:`resolve_into_realm`).
- ``swarm.spawn._safe_component`` — one safe path component (kept verbatim as
  :func:`safe_component`; ``swarm.spawn`` now re-exports it, which also fixes a
  layering inversion where the generic ``swarm.worktrees`` imported from a
  sibling spawner module).

Layering: this imports only the stdlib-only bottom-layer
``omniagentos.path_containment`` module.  Everything in Phase 3 keys off this
algebra, so upward imports remain forbidden.  The only other I/O is
``os.path.realpath`` and, in :func:`realm_of`, one memoized ``git rev-parse``.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from collections.abc import Iterable, Sequence
from functools import lru_cache
from pathlib import Path

from omniagentos.path_containment import inode_relative_parts

__all__ = [
    "WHOLE_REALM",
    "ScopePathError",
    "clear_realm_cache",
    "normalize_rel",
    "overlap",
    "private_workspace_bases",
    "realm_of",
    "register_private_base",
    "rel_text",
    "resolve_into_realm",
    "safe_component",
    "under",
]


class ScopePathError(ValueError):
    """A path cannot be expressed as a realm-relative scope.

    Subclasses ``ValueError`` so the call sites this consolidates (which caught
    ``ValueError`` or a ``SwarmPlanError``) keep behaving as before.
    """


#: The component tuple that means "the whole realm". ``normalize_rel(".")`` and
#: ``normalize_rel("")``-equivalents land here, and it is an ancestor of every
#: path INCLUDING ITSELF (see :func:`under`).
WHOLE_REALM: tuple[str, ...] = ()

_SAFE_COMPONENT_RE = re.compile(r"[^A-Za-z0-9._-]")
_DRIVE_RE = re.compile(r"^[A-Za-z]:")

#: Components below a private base that form one private realm root. ``var/runs``
#: and ``var/intake-workspace`` are keyed by a single id; a swarm worktree is
#: ``<worktrees base>/<run_id>/<task_key>``, i.e. two.
_RUN_WORKSPACE_DEPTH = 1
_WORKTREE_DEPTH = 2
# var/projects/<project_id>/workspace -- the private unit is the workspace INSIDE
# the per-project dir, so the shielded depth is 2, not 1.
_PROJECT_WORKSPACE_DEPTH = 2


# ---------------------------------------------------------------------------
# Relative path algebra (pure; no filesystem)
# ---------------------------------------------------------------------------


def normalize_rel(path: str | Sequence[str]) -> tuple[str, ...]:
    """Normalize one realm-relative path to a component tuple.

    ``"./a//b/"`` -> ``("a", "b")``; ``"a/x/../b"`` -> ``("a", "b")``; ``"."``
    (and anything that collapses to the realm root) -> ``()`` == the WHOLE
    realm. Backslashes are folded to ``/`` first, so a Windows-style path
    normalizes rather than being treated as one component.

    Raises :class:`ScopePathError` for anything that is not realm-relative: an
    empty/blank path, an absolute path, a ``~`` home path, a drive-letter path,
    an embedded NUL, or a path whose ``..`` segments climb out of the realm.

    A sequence of components is accepted and validated the same way, so callers
    can round-trip a stored tuple without re-joining it.
    """
    if not isinstance(path, str):
        try:
            parts_in = [str(part) for part in path]
        except TypeError as exc:  # not iterable
            raise ScopePathError(f"owned path is not a path: {path!r}") from exc
        if not parts_in:
            # An empty component sequence is the whole realm -- the tuple
            # spelling of "."; only an empty STRING is a malformed path.
            return WHOLE_REALM
        raw = "/".join(parts_in)
    else:
        raw = path
    text = raw.strip().replace("\\", "/")
    if not text:
        raise ScopePathError("owned path is empty")
    if "\x00" in text:
        raise ScopePathError(f"owned path contains NUL: {path!r}")
    if text.startswith(("/", "~")) or _DRIVE_RE.match(text):
        raise ScopePathError(f"owned path must be realm-relative: {path!r}")
    parts: list[str] = []
    for segment in text.split("/"):
        if segment in ("", "."):
            continue
        if segment == "..":
            if not parts:
                raise ScopePathError(f"owned path escapes the realm: {path!r}")
            parts.pop()
        else:
            parts.append(segment)
    return tuple(parts)


def rel_text(components: Iterable[str]) -> str:
    """Render a component tuple back to posix text; ``()`` renders as ``"."``.

    Inverse of :func:`normalize_rel` for already-normalized input. ``"."`` is
    the wire/display spelling of the whole realm — the tuple is the truth.
    """
    parts = tuple(components)
    return "/".join(parts) if parts else "."


def safe_component(value: str) -> str:
    """One path component: reject separators/traversal, sanitize the rest.

    Lifted verbatim from ``swarm.spawn._safe_component`` (which now re-exports
    this). Used to build filesystem paths out of ids (run ids, task keys), so it
    must reject anything that could escape its parent — ``Path(value).name !=
    value`` catches every separator and traversal form the regex would happily
    rewrite into a plausible-looking name.
    """
    cleaned = _SAFE_COMPONENT_RE.sub("_", value.strip())
    if not cleaned or cleaned in (".", "..") or Path(value).name != value:
        raise ValueError(f"unsafe path component: {value!r}")
    return cleaned


def under(child: str | Sequence[str], parent: str | Sequence[str]) -> bool:
    """True iff ``child`` is at or below ``parent``, compared COMPONENT-wise.

    Never string prefixes: ``under("github/x", ".github")`` is ``False``.
    ``()`` (the whole realm) is an ancestor of everything INCLUDING ITSELF, so
    ``under(anything, ())`` and ``under((), ())`` are both ``True``, and
    ``under(("a",), ())`` is ``True`` while ``under((), ("a",))`` is ``False``.

    Both arguments accept either a component sequence or a raw string (which is
    run through :func:`normalize_rel`, and may therefore raise).
    """
    child_parts = _components(child)
    parent_parts = _components(parent)
    return child_parts[: len(parent_parts)] == parent_parts


def overlap(a: str | Sequence[str], b: str | Sequence[str]) -> bool:
    """True iff ``a`` and ``b`` are NOT disjoint (equal, or one contains the other).

    This is the fix for ``SwarmScheduler._paths_overlap(".", "src/a")``, which
    returned ``False``: with ``"."`` normalized to ``()``, ``overlap((),
    ("src", "a"))`` is ``True`` with no special case at all.
    """
    a_parts = _components(a)
    b_parts = _components(b)
    shorter = min(len(a_parts), len(b_parts))
    return a_parts[:shorter] == b_parts[:shorter]


def _components(value: str | Sequence[str]) -> tuple[str, ...]:
    """Coerce either spelling of a scope path to a component tuple.

    ``str`` is checked FIRST because ``str`` is itself a ``Sequence[str]`` —
    without that order ``"abc"`` would decompose into ``("a", "b", "c")``.
    """
    if isinstance(value, str):
        return normalize_rel(value)
    return tuple(str(part) for part in value)


# ---------------------------------------------------------------------------
# Realms (the only filesystem-touching part)
# ---------------------------------------------------------------------------


def _casefold_path(path: str) -> str:
    """Case-fold a path for containment on a case-insensitive filesystem.

    Mirrors ``runner.core._casefold_path`` / ``policy.secrets._casefold_path``:
    macOS's default volume is case-insensitive, so ``/Repo/Src`` and
    ``/repo/src`` are the SAME directory and must fold to one realm key. Linux
    stays case-sensitive (``normcase`` is identity on posix).
    """
    normalized = os.path.normcase(path)
    if sys.platform == "darwin":
        normalized = normalized.lower()
    return normalized


def _resolve_realpath(path: str) -> str | None:
    """``realpath`` with ``~`` expansion and quote-stripping; ``None`` if unusable.

    Lifted from ``runner.core._resolve_realpath``. ``realpath`` resolves both
    ``..`` and symlinks, so an escape attempt lands OUT of scope rather than
    sneaking through a lexical check, and two symlinked-together roots collapse
    to the same string.
    """
    text = path.strip().strip("\"'") if isinstance(path, str) else ""
    if not text:
        return None
    try:
        return os.path.realpath(os.path.expanduser(text))
    except (OSError, ValueError):
        return None


def _repo_root() -> str:
    """This checkout's root, derived from the package location (cwd-independent)."""
    return os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _var_root() -> str:
    """``<repo>/var``, honoring ``OMNIAGENTOS_VAR_DIR`` (the knob every other
    var-anchored helper reads)."""
    return os.environ.get("OMNIAGENTOS_VAR_DIR") or os.path.join(_repo_root(), "var")


# Extra private bases registered at runtime, e.g. a swarm worktrees root that was
# configured away from the default. (base, depth) pairs; see register_private_base.
_EXTRA_PRIVATE_BASES: list[tuple[str, int]] = []


def register_private_base(base: str, depth: int = 1) -> None:
    """Declare an additional private-workspace base.

    ``depth`` is how many components BELOW ``base`` form one private realm root
    (1 for ``<base>/<id>``, 2 for ``<base>/<run_id>/<task_key>``). Needed when a
    lane is configured with a non-default var root: :func:`realm_of` cannot
    import the lane's module to ask, and a private base that is not declared
    folds into the enclosing repo realm — the catastrophic case this ordering
    exists to prevent.
    """
    resolved = _resolve_realpath(base)
    if resolved is None:
        raise ScopePathError(f"private base is not a usable path: {base!r}")
    entry = (_casefold_path(resolved), max(0, int(depth)))
    if entry not in _EXTRA_PRIVATE_BASES:
        _EXTRA_PRIVATE_BASES.append(entry)


def private_workspace_bases() -> tuple[tuple[str, int], ...]:
    """Every ``(folded_base, depth)`` whose CHILDREN are each their own realm.

    These are the dirs that live INSIDE this git checkout but are emphatically
    not part of it:

    - ``runner._default_workspace_base()`` — ``<repo>/var/runs`` (or
      ``OMNIAGENTOS_WORKSPACE_DIR``); ``var/runs/<run_id>`` is one run's private
      workspace.
    - the intake scratch base — ``<var>/intake-workspace/<task_id>``.
    - the swarm worktrees base — ``<var>/swarm/worktrees/<run_id>/<task_key>``,
      each a linked git worktree.
    - the per-project workspace base — ``<var>/projects/<project_id>/workspace``
      (documented at ``intake/service.py:829``). These exist on disk today and
      are the SAME hazard class: without shielding, every project workspace
      folded into the single OmniAgentOS realm, so two agents working on two
      different projects would have conflicted on each other's scratch space.
      Depth 2, because the private unit is ``<project_id>/workspace`` — the
      per-project dir itself is not the realm, the workspace inside it is.

    Every one of these resolves to a path under the OmniAgentOS repo, so if
    :func:`realm_of` asked git for a toplevel FIRST, all of them would collapse
    into the single OmniAgentOS realm and every concurrent run would mutually
    conflict on its own private scratch space. Hence: this check runs first.
    """
    var_root = _var_root()
    raw: list[tuple[str, int]] = [
        (
            os.environ.get("OMNIAGENTOS_WORKSPACE_DIR")
            or os.path.join(_repo_root(), "var", "runs"),
            _RUN_WORKSPACE_DEPTH,
        ),
        (os.path.join(var_root, "intake-workspace"), _RUN_WORKSPACE_DEPTH),
        (os.path.join(var_root, "swarm", "worktrees"), _WORKTREE_DEPTH),
        (os.path.join(var_root, "projects"), _PROJECT_WORKSPACE_DEPTH),
        # The repo-anchored defaults too, so an OMNIAGENTOS_VAR_DIR /
        # OMNIAGENTOS_WORKSPACE_DIR override does not un-shield the in-repo dirs a
        # previously-started process still owns workspaces in.
        (os.path.join(_repo_root(), "var", "runs"), _RUN_WORKSPACE_DEPTH),
        (os.path.join(_repo_root(), "var", "intake-workspace"), _RUN_WORKSPACE_DEPTH),
        (os.path.join(_repo_root(), "var", "swarm", "worktrees"), _WORKTREE_DEPTH),
        (os.path.join(_repo_root(), "var", "projects"), _PROJECT_WORKSPACE_DEPTH),
    ]
    bases: list[tuple[str, int]] = []
    for base, depth in raw:
        resolved = _resolve_realpath(base)
        if resolved is None:
            continue
        entry = (_casefold_path(resolved), depth)
        if entry not in bases:
            bases.append(entry)
    for entry in _EXTRA_PRIVATE_BASES:
        if entry not in bases:
            bases.append(entry)
    return tuple(bases)


@lru_cache(maxsize=256)
def _git_toplevel(start_dir: str) -> str | None:
    """``git -C <start_dir> rev-parse --show-toplevel``, memoized.

    Memoized because this is the only shell-out in the module and
    :func:`realm_of` is called per claim. Inside a linked worktree git reports
    the WORKTREE's own root, which is the right answer; the private-base check
    runs first anyway, so a worktree path that is not yet an initialized
    worktree never reaches here.
    """
    try:
        completed = subprocess.run(  # noqa: S603 -- fixed argv, never shell
            ("git", "-C", start_dir, "rev-parse", "--show-toplevel"),
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if completed.returncode != 0:
        return None
    top = completed.stdout.strip()
    return top or None


def clear_realm_cache() -> None:
    """Drop the memoized ``git rev-parse`` results.

    Only needed when a directory becomes (or stops being) a git repo inside one
    process — i.e. tests. Production layout is fixed for a process's lifetime.
    """
    _git_toplevel.cache_clear()


def _nearest_existing_dir(resolved: str) -> str | None:
    """The deepest existing directory at or above ``resolved`` (``git -C`` needs one)."""
    current = resolved
    while True:
        if os.path.isdir(current):
            return current
        parent = os.path.dirname(current)
        if parent == current:
            return None
        current = parent


def realm_of(path: str) -> str | None:
    """The realm key a path belongs to, or ``None`` if the path is unusable.

    A "realm" is the conflict universe: two claims can only conflict when their
    realms are equal. The key is a case-folded realpath, so symlinked-together
    roots and macOS case variants fold to one key.

    Resolution order is load-bearing and must not be reordered:

    1. **Private-workspace exclusion FIRST.** Every per-run workspace, intake
       scratch dir and swarm worktree lives inside this repo (see
       :func:`private_workspace_bases`). Resolving git first would fold all of
       them into the OmniAgentOS realm and make every concurrent run conflict
       with every other one on its own scratch space.
    2. **Then git top-level.** Migration ``031_project_hierarchy`` gives projects
       a ``parent_project_id``, so a subproject's root dir can live physically
       inside its parent's root. Two realm strings for one tree is a MISSED
       conflict; collapsing both to the git toplevel makes parent and subproject
       share one realm, which is what they physically are.
    3. **Then realpath.** A plain directory outside any repo is its own realm.
    """
    resolved = _resolve_realpath(path)
    if resolved is None:
        return None
    folded = _casefold_path(resolved)

    # 1. private workspaces -- each child of a private base is its OWN realm.
    private = _private_realm(folded)
    if private is not None:
        return private

    # 2. git top-level (parent/subproject collapse).
    start = _nearest_existing_dir(resolved)
    if start is not None:
        top = _git_toplevel(start)
        if top is not None:
            top_resolved = _resolve_realpath(top)
            if top_resolved is not None:
                return _casefold_path(top_resolved)

    # 3. realpath.
    return folded


def _private_realm(folded: str) -> str | None:
    """The private realm key for ``folded``, or ``None`` if it is not in one.

    Matches COMPONENT-wise (never ``startswith``), so ``var/runs-archive`` is not
    treated as living under ``var/runs``. The DEEPEST matching base wins, which
    keeps nested bases unambiguous. A path that IS a base — or that is shallower
    than the base's ``depth`` (e.g. the ``<run_id>`` level of a two-deep
    worktrees base) — returns as much of itself as exists: it is a private
    container and still must not fold into the enclosing repo.
    """
    target = _split_abs(folded)
    best: tuple[int, str] | None = None
    for base, depth in private_workspace_bases():
        base_parts = _split_abs(base)
        if target[: len(base_parts)] != base_parts:
            continue
        realm_parts = target[: len(base_parts) + depth]
        candidate = (len(base_parts), _join_abs(realm_parts, folded))
        if best is None or candidate[0] > best[0]:
            best = candidate
    return best[1] if best is not None else None


def _split_abs(path: str) -> tuple[str, ...]:
    """Split an absolute path into components, dropping the empty root segment."""
    return tuple(part for part in path.split(os.sep) if part)


def _join_abs(parts: Sequence[str], reference: str) -> str:
    """Rebuild an absolute path from components, preserving the reference's root."""
    root = reference[: len(reference) - len(reference.lstrip(os.sep))] or os.sep
    return root + os.sep.join(parts)


def resolve_into_realm(candidate: str, realm: str) -> tuple[str, ...] | None:
    """Components of ``candidate`` relative to ``realm``, or ``None`` if outside.

    Fail-closed in every ambiguous case.  The shared inode-ancestry primitive
    re-canonicalizes the candidate itself, so a symlink or ``..`` chain that
    escapes the realm returns ``None`` rather than a plausible-looking
    relative path.

    A relative ``candidate`` is resolved against the realm root. The returned
    components preserve the canonical candidate spelling while inode identity
    decides containment, so a lock's stored path stays readable and openable.
    ``()`` means the candidate IS the realm root.
    """
    realm_real = _resolve_realpath(realm)
    if realm_real is None:
        return None
    text = candidate.strip().strip("\"'") if isinstance(candidate, str) else ""
    if not text:
        return None
    expanded = os.path.expanduser(text)
    if not os.path.isabs(expanded):
        expanded = os.path.join(realm_real, expanded)
    return inode_relative_parts(expanded, realm_real)
