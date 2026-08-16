"""Standing granted roots (HANDOFF AUTO-APPROVE Phase 1).

Loaded from ``configs/roots.yaml`` and merged into every session's
``granted_roots`` at spawn so ordinary work outside a project's own workspace
(Desktop deliverables, managed ``var/``, etc.) does not park for approval.

``never`` entries are excluded even if they nest under a standing root.
``read_only_roots`` widen *read* scope only — callers that need write grants
must not pass them as write roots.
"""

from __future__ import annotations

import logging
import os
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

from omniagentos.path_containment import inode_path_is_within_anchored, inode_paths_equal
from omniagentos.policy.dir_grants import resolve_grant_dir
from omniagentos.policy.secrets import secret_dirs

LOG = logging.getLogger(__name__)


def _default_roots_path() -> Path:
    return Path(__file__).resolve().parents[2] / "configs" / "roots.yaml"


def _expand(raw: str) -> str:
    return os.path.expanduser(str(raw).strip())


def _is_or_under_secret(path: str) -> bool:
    """True when *path* IS or sits under a secret dir.

    Unlike ``references_secret_dir``, a managed ``var/`` root that merely
    *contains* ``var/secrets`` is still grantable — agent scratch lives there.
    Writes into the secret subdir remain blocked by the shell secret classifier.

    Safety polarity for this *exclusion* filter: treat unknown (``None``) as
    secret so an unstatable candidate is never admitted as a standing root.
    Only a positive ``False`` (proved outside every secret dir) is grantable.
    """
    try:
        resolved = os.path.realpath(os.path.expanduser(path))
    except (OSError, ValueError):
        resolved = os.path.expanduser(path)
    for secret in secret_dirs():
        # Exclusion (`is not False`): drop on True *or* unknown.
        if inode_path_is_within_anchored(resolved, secret) is not False:
            return True
    return False


def _load_raw(path: Path | None = None) -> dict[str, Any]:
    cfg_path = path or _default_roots_path()
    try:
        with open(cfg_path, encoding="utf-8") as fh:
            data = yaml.safe_load(fh) or {}
        if not isinstance(data, dict):
            return {}
        return data
    except FileNotFoundError:
        return {}
    except (OSError, yaml.YAMLError) as exc:
        LOG.warning("could not load standing roots from %s: %s", cfg_path, exc)
        return {}


def _resolve_list(raw_items: list[Any] | None) -> list[str]:
    out: list[str] = []
    for item in raw_items or []:
        text = _expand(str(item))
        if not text:
            continue
        if _is_or_under_secret(text):
            LOG.debug("standing roots dropped secret path %s", text)
            continue
        resolved = resolve_grant_dir(text) or text
        if resolved and resolved not in out:
            out.append(resolved)
    return out


@lru_cache(maxsize=4)
def _cached_config(path_str: str) -> tuple[tuple[str, ...], tuple[str, ...], tuple[str, ...]]:
    raw = _load_raw(Path(path_str) if path_str else None)
    standing = tuple(_resolve_list(raw.get("standing_roots")))
    read_only = tuple(_resolve_list(raw.get("read_only_roots")))
    never = tuple(_resolve_list(raw.get("never")))
    return standing, read_only, never


def clear_roots_cache() -> None:
    """Test helper: drop the cached roots config."""
    _cached_config.cache_clear()


def _active_path() -> str:
    env = os.environ.get("OMNIAGENTOS_ROOTS_CONFIG")
    if env:
        return env
    return str(_default_roots_path())


def standing_write_roots(*, exclude: set[str] | None = None) -> list[str]:
    """Write-capable standing roots, minus ``never`` and optional excludes."""
    standing, _ro, never = _cached_config(_active_path())
    skip = set(exclude or ())
    never_set = set(never)
    out: list[str] = []
    for root in standing:
        if root in skip or root in never_set:
            continue
        if any(_is_under(root, blocked) for blocked in never_set):
            continue
        if _is_blocked_by_never(root, never_set):
            continue
        out.append(root)
    return out


def standing_read_only_roots() -> list[str]:
    """Read-only standing roots.

    Explicit ``read_only_roots`` are intentional exceptions to ``never`` (e.g.
    ``~/Library/Caches`` under a ``~/Library`` never-write rule). Do not apply
    the write-side never filter here.
    """
    _standing, read_only, _never = _cached_config(_active_path())
    return list(read_only)


def never_roots() -> list[str]:
    _s, _r, never = _cached_config(_active_path())
    return list(never)


def _is_under(path: str, root: str) -> bool:
    """True when *path* is under *root* for exclusion purposes.

    Callers use this to *refuse* standing write roots past ``never`` / secret
    filters.  Safety polarity: unknown (``None``) counts as under so it cannot
    silently admit a root.  Only a positive ``False`` (proved outside) allows
    the root through.
    """
    # Exclusion (`is not False`): block on True *or* unknown.
    return inode_path_is_within_anchored(path, root) is not False


def _is_blocked_by_never(path: str, never_set: set[str]) -> bool:
    """True when *path* is or sits under a never root (product source, secrets)."""
    for blocked in never_set:
        # Identity and containment both go through the shared inode primitive.
        # A path is under itself (relative parts ``()``), so no string ``==``.
        if _is_under(path, blocked):
            return True
    return False


def merge_standing_roots(
    existing: list[str] | None,
    *,
    working_dir: str | None = None,
) -> list[str]:
    """Merge standing write roots into a session's granted_roots list."""
    out: list[str] = []
    seen: set[str] = set()
    working_real = resolve_grant_dir(working_dir) if working_dir else None
    for raw in list(existing or []) + standing_write_roots():
        if not raw:
            continue
        resolved = resolve_grant_dir(raw) or raw
        if not resolved or resolved in seen:
            continue
        # Safety (`is True`): skip only when positively the working dir by inode.
        if working_real and inode_paths_equal(resolved, working_real) is True:
            continue
        if _is_blocked_by_never(resolved, set(never_roots())):
            continue
        seen.add(resolved)
        out.append(resolved)
    return out


__all__ = [
    "clear_roots_cache",
    "merge_standing_roots",
    "never_roots",
    "standing_read_only_roots",
    "standing_write_roots",
]
