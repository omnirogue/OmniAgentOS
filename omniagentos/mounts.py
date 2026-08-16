"""Machine-roots registry (P1 mounts core): the top-level filesystem roots on
THIS machine that a directory grant may live under.

Loads and validates ``configs/mounts.yaml`` -- the single source of truth for
"which drives/folders exist on this machine, and which may host a grant". This
module is pure: it never reads a credential and never performs a write. It
answers three questions:

    * what mounts are configured, and what is each one's grant metadata?
    * which mount does an id name?
    * which mount roots may a directory grant actually live under RIGHT NOW?

Style-matches :mod:`omniagentos.drive` (every path expanded and
``os.path.realpath``-resolved so a symlink or ``..`` trick can never make an
out-of-mount path look like a known root) and the connector registry
(:mod:`omniagentos.connectors`): the YAML is read once and cached with
``functools.lru_cache``, and each mount's path is realpath'd a SINGLE time at
load. A running process therefore keeps the mounts it loaded at startup --
editing ``configs/mounts.yaml`` requires a process restart to take effect.

A mount whose path does not exist locally (iCloud not enabled, Google Drive for
desktop not installed, a folder not created yet) is retained in the loaded set
with ``exists=False`` so the API/UI can still display it, but it is EXCLUDED from
:func:`grantable_mount_roots` -- a grant can only ever be recorded under a root
that is actually present on disk.
"""

from __future__ import annotations

import functools
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

__all__ = [
    "Mount",
    "MountsError",
    "grantable_mount_roots",
    "load_mounts",
    "mount_by_id",
]


class MountsError(ValueError):
    """Raised when the mounts registry is malformed or declares duplicate ids."""


@dataclass(frozen=True)
class Mount:
    """One configured machine root.

    ``path`` is the raw, unexpanded value from the YAML (e.g. ``~/Work``);
    ``resolved`` is its ``expanduser`` + ``realpath`` form, computed exactly once
    at load. ``exists`` reflects whether ``resolved`` is a directory on disk at
    load time (retained for API display even when False).
    """

    id: str
    label: str
    path: str
    kind: str
    cloud: bool
    grantable: bool
    read_only: bool
    notes: str
    resolved: str
    exists: bool


def _default_mounts_path() -> Path:
    """Resolve ``configs/mounts.yaml`` independently of the working directory."""
    return Path(__file__).resolve().parents[1] / "configs" / "mounts.yaml"


def _coerce_bool(value: Any, default: bool) -> bool:
    return default if value is None else bool(value)


@functools.lru_cache(maxsize=4)
def load_mounts(path: str | None = None) -> tuple[Mount, ...]:
    """Load, validate, and cache the machine-roots registry.

    Every mount's path is ``expanduser``-ed and ``realpath``-resolved ONCE here.
    Ids must be unique (a duplicate is a :class:`MountsError`, never a silent
    last-wins). Non-existent paths are kept with ``exists=False`` for display.

    Cached with ``lru_cache`` (like the connector registry) -- a process restart
    picks up edits to the YAML. ``path`` overrides the default location, and is
    the cache key, so a test fixture can load its own YAML without disturbing the
    default.
    """
    target = Path(path) if path else _default_mounts_path()
    try:
        raw: Any = yaml.safe_load(target.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise MountsError(f"Unable to read mounts registry {str(target)!r}: {exc}") from exc

    if not isinstance(raw, dict):
        raise MountsError("Mounts registry must be a mapping.")

    entries = raw.get("mounts")
    if entries is None:
        entries = []
    if not isinstance(entries, list):
        raise MountsError("'mounts' must be a list.")

    mounts: list[Mount] = []
    seen: set[str] = set()
    for entry in entries:
        if not isinstance(entry, dict):
            raise MountsError("Each mount entry must be a mapping.")
        mount_id = str(entry.get("id") or "").strip()
        if not mount_id:
            raise MountsError("A mount entry is missing its 'id'.")
        if mount_id in seen:
            raise MountsError(f"Duplicate mount id {mount_id!r}.")
        seen.add(mount_id)

        raw_path = str(entry.get("path") or "").strip()
        if not raw_path:
            raise MountsError(f"Mount {mount_id!r} is missing its 'path'.")

        kind = str(entry.get("kind") or "local").strip() or "local"
        resolved = os.path.realpath(os.path.expanduser(raw_path))
        mounts.append(
            Mount(
                id=mount_id,
                label=str(entry.get("label") or mount_id),
                path=raw_path,
                kind=kind,
                cloud=_coerce_bool(entry.get("cloud"), kind != "local"),
                grantable=_coerce_bool(entry.get("grantable"), True),
                read_only=_coerce_bool(entry.get("read_only"), False),
                notes=str(entry.get("notes") or "").strip(),
                resolved=resolved,
                exists=os.path.isdir(resolved),
            )
        )
    return tuple(mounts)


def mount_by_id(mount_id: str, path: str | None = None) -> Mount | None:
    """Return the mount with ``mount_id`` from the registry, or ``None``."""
    for mount in load_mounts(path):
        if mount.id == mount_id:
            return mount
    return None


def grantable_mount_roots(path: str | None = None) -> list[str]:
    """Realpath'd roots a directory WRITE grant may live under, right now.

    A root is offered only when it is ALL of: ``grantable``, NOT ``read_only``,
    and present on disk. ``read_only`` mounts (e.g. ``downloads`` -- financial
    statements/tax files, "grant reads here, not writes") are intentionally
    excluded: they remain browsable via the mounts API but can never become a
    project write-grant root. ``home`` is ``grantable: false`` by design and is
    therefore never in this list -- the whole home directory is browse-only,
    never a write grant. Order follows the YAML; duplicate realpaths are
    collapsed.
    """
    roots: list[str] = []
    for mount in load_mounts(path):
        if mount.grantable and not mount.read_only and mount.exists and mount.resolved not in roots:
            roots.append(mount.resolved)
    return roots
