"""Shared directory-grant validator (AC-policy fix7).

A single gate every filesystem-scope grant must pass BEFORE it is recorded as a
project's reachable directory. Two consumers author untrusted directory scope:

  * ``api.routes.projects.create_project`` — ``root_dirs`` / ``allowed_dirs`` on a
    freshly-created project row, and
  * ``provision.request_scope`` — a mid-task ``dir`` widen request.

Round-7 found both were open: ``create_project`` accepted ANY absolute path
(only ``..``/NUL were rejected) and provisioning auto-granted ANY dir under
``$HOME`` (``_dir_within_home``). An agent could therefore loopback-``POST
/api/projects`` to self-grant ``root_dirs:[~/.ssh]`` and then schedule a run
whose ``working_dir`` the runner would trust. This module closes that:

  * every path is realpath-resolved (``~``/``$VAR`` expanded, ``..``/symlinks
    collapsed) the SAME way for every layer, then
  * REQUIRED to be contained by a configured user-approved allow-root
    (default: ``~/OmniAgentOS``, the ``OMNIAGENTOS_VAR_DIR`` workspace root when
    set, the Drive roots from :mod:`omniagentos.drive`, and any explicitly
    configured ``OMNIAGENTOS_PROJECT_BASES``), and
  * HARD-REJECTED when the path IS, is INSIDE, or CONTAINS a secret-registry dir
    (:func:`omniagentos.policy.secrets.secret_dirs`) — so neither ``~/.ssh`` nor a
    broad grant like ``~`` that would engulf ``~/.ssh`` is ever grantable.

``references_secret_dir`` (hard-reject) and ``within_allowed_roots``
(containment) are exposed separately so the provisioning flow can keep its
distinction between a HARD-REJECT (a secret dir) and an ESCALATION (a normal dir
that merely sits outside the approved roots), while ``validate_grant_dir``
rejects both for the create-time path that has no escalation lane.
"""

from __future__ import annotations

import os
import sys

from omniagentos.drive import drive_roots
from omniagentos.mounts import grantable_mount_roots
from omniagentos.path_containment import inode_relative_parts_anchored
from omniagentos.policy.secrets import secret_dirs
from omniagentos.simgate import SimContext

__all__ = [
    "DirGrantError",
    "allowed_grant_roots",
    "references_secret_dir",
    "resolve_grant_dir",
    "validate_grant_dir",
    "within_allowed_roots",
]


class DirGrantError(ValueError):
    """A directory grant outside the approved roots, or that hits a secret dir."""


def _realpath(path: str) -> str:
    """Expand ``~``/``$VAR`` and realpath-resolve (follow symlinks, collapse ``..``)."""
    try:
        return os.path.realpath(os.path.expanduser(os.path.expandvars(str(path))))
    except (OSError, ValueError):
        return os.path.expanduser(str(path))


def _casefold(path: str) -> str:
    """Case-fold for containment on case-insensitive filesystems (mirrors secrets.py).

    macOS's default filesystem is case-insensitive but case-preserving and
    ``realpath`` does not canonicalize case, so a ``~/.SSH`` reference must not
    slip past a ``~/.ssh`` containment check. Linux stays case-sensitive.
    """
    normalized = os.path.normcase(path)
    if sys.platform == "darwin":
        normalized = normalized.lower()
    return normalized


def _contains(parent: str, child: str) -> bool:
    """True when ``child`` equals or is nested under ``parent`` by inode."""
    if not parent or not child:
        return False
    return inode_relative_parts_anchored(child, parent) is not None


def _configured_project_bases() -> list[str]:
    """Explicitly user-approved project base dirs from ``OMNIAGENTOS_PROJECT_BASES``.

    ``os.pathsep``-separated absolute paths. This is the escape hatch for an
    operator who keeps their repos somewhere other than ``~/OmniAgentOS`` — it is
    a deliberate, out-of-band configuration, never something an agent can set
    through the API.
    """
    raw = os.environ.get("OMNIAGENTOS_PROJECT_BASES", "")
    bases: list[str] = []
    for item in raw.split(os.pathsep):
        stripped = item.strip()
        if stripped:
            bases.append(_realpath(stripped))
    return bases


def allowed_grant_roots(sim_ctx: SimContext | None = None) -> list[str]:
    """Realpath-resolved roots a directory grant may live under (deny by default).

    A validated simulation context collapses this floor to exactly its
    canonical campaign root. Production keeps the configured workspace, Drive,
    mount, and project-base roots below.

    Default set: ``~/OmniAgentOS``; the ``OMNIAGENTOS_VAR_DIR`` workspace root
    when configured (per-project + intake workspaces live there); every Drive
    mount root from :func:`omniagentos.drive.drive_roots`; every grantable
    machine root that exists from :func:`omniagentos.mounts.grantable_mount_roots`
    (``configs/mounts.yaml`` -- iCloud, Google Drive, Dropbox, ~/Work, ~/Desktop,
    ...); and any explicitly configured ``OMNIAGENTOS_PROJECT_BASES``. Nothing
    else is grantable without a deliberate operator configuration change.

    NOTE: widening the grantable ROOTS never weakens the secret hard-reject --
    ``references_secret_dir`` still rejects any grant that IS or ENGULFS a
    credential store, and ``home`` is intentionally ``grantable: false`` in the
    mounts registry, so the whole home directory is browse-only, never a grant.
    """
    if sim_ctx is not None and sim_ctx.sim_mode:
        if sim_ctx.campaign_root is None:
            raise DirGrantError("broken invariant: sim context has no campaign root")
        return [os.path.realpath(str(sim_ctx.campaign_root))]

    roots: list[str] = []

    def add(candidate: str) -> None:
        resolved = _realpath(candidate)
        if resolved and resolved not in roots:
            roots.append(resolved)

    add(os.path.join(os.path.expanduser("~"), "OmniAgentOS"))
    var_dir = os.environ.get("OMNIAGENTOS_VAR_DIR")
    if var_dir:
        add(var_dir)
    for root in drive_roots():
        add(root)
    for root in grantable_mount_roots():
        add(root)
    for base in _configured_project_bases():
        if base not in roots:
            roots.append(base)
    return roots


def references_secret_dir(path: str) -> bool:
    """True when ``path`` IS, is INSIDE, or CONTAINS a secret-registry directory.

    Both containment directions are rejected: a grant of ``~/.ssh`` (the path IS a
    secret dir) AND a grant of ``~`` (which CONTAINS ``~/.ssh``) are both secret
    hits, so no grant can hand an agent a credential store directly or by
    engulfing one.
    """
    resolved = _realpath(path)
    for secret in secret_dirs():
        if _contains(secret, resolved) or _contains(resolved, secret):
            return True
    return False


def within_allowed_roots(
    path: str,
    allow_roots: list[str] | None = None,
    *,
    sim_ctx: SimContext | None = None,
) -> bool:
    """True when ``path`` resolves to or under one of the approved allow-roots."""
    resolved = _realpath(path)
    roots = allow_roots if allow_roots is not None else allowed_grant_roots(sim_ctx)
    return any(_contains(_realpath(root), resolved) for root in roots)


def resolve_grant_dir(path: str) -> str:
    """The realpath a grant would be recorded as (single source of truth)."""
    return _realpath(path)


def validate_grant_dir(
    path: str,
    *,
    allow_roots: list[str] | None = None,
    sim_ctx: SimContext | None = None,
) -> str:
    """Validate a create-time directory grant; return its realpath or raise.

    Rejects (``DirGrantError``) a path that hits a secret-registry dir OR that
    falls outside every approved allow-root. Used by ``create_project``, which
    has no escalation lane — an invalid grant is a clean rejection, never a
    silently-accepted scope.
    """
    value = str(path or "").strip()
    if not value:
        raise DirGrantError("directory grant must not be empty")
    resolved = _realpath(value)
    if references_secret_dir(value):
        raise DirGrantError(f"{path!r} resolves to or contains a secret/credential directory")
    if not within_allowed_roots(value, allow_roots, sim_ctx=sim_ctx):
        raise DirGrantError(
            f"{path!r} is outside the approved grant roots "
            f"(configure OMNIAGENTOS_PROJECT_BASES to widen)"
        )
    return resolved


def _ensure_str_list(values: object) -> list[str]:
    if not isinstance(values, (list, tuple)):
        return []
    return [str(v) for v in values if str(v).strip()]


def validate_grant_dirs(
    paths: object,
    *,
    allow_roots: list[str] | None = None,
    sim_ctx: SimContext | None = None,
) -> list[str]:
    """Validate a list of directory grants; return their realpaths or raise."""
    return [
        validate_grant_dir(p, allow_roots=allow_roots, sim_ctx=sim_ctx)
        for p in _ensure_str_list(paths)
    ]
