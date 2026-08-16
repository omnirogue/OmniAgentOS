"""Module for assembling stable, cacheable prompt segments from universal base and role prompts.

The role pack consolidates universal-base.md and the role-specific prompts into a
single stable Segment to optimize provider prompt caching. It is designed to be
fail-soft, returning None and logging warnings rather than raising exceptions.
"""

from __future__ import annotations

import logging
import threading
from pathlib import Path, PurePath

from omniagentos.promptshape.segments import Segment, render
from omniagentos.roles import JobRole

LOG = logging.getLogger(__name__)

# Derived, never hand-maintained: omniagentos.roles.JobRole is the single job
# vocabulary. A second literal list here is how the two drift apart, which is the
# defect class the JobRole commit existed to close.
JOB_ROLES: tuple[str, ...] = tuple(role.value for role in JobRole)

_cache: dict[tuple[str, str], tuple[int, int, Segment]] = {}
_cache_lock = threading.Lock()


def _repository_root() -> Path:
    """Return the checkout root independently of the process working directory."""
    return Path(__file__).resolve().parents[2]


def clear_role_pack_cache() -> None:
    """Empty the module-level role pack cache."""
    with _cache_lock:
        _cache.clear()


def role_pack(job_role: str, *, root: Path | None = None) -> Segment | None:
    """universal-base.md + roles/<job_role>.md as one stable Segment."""
    try:
        if not isinstance(job_role, str):
            LOG.warning("job_role must be a string, got %s", type(job_role).__name__)
            return None

        component = PurePath(job_role)
        if (
            not job_role
            or "/" in job_role
            or "\\" in job_role
            or component.name != job_role
            or job_role in {".", ".."}
        ):
            LOG.warning("job_role must be a single path component, got %r", job_role)
            return None

        if job_role not in JOB_ROLES:
            LOG.warning(
                "job_role %r is not a known role; expected one of %s",
                job_role,
                JOB_ROLES,
            )
            return None

        resolved_root = (root or _repository_root()).resolve()

        base_path = resolved_root / "vault" / "prompts" / "universal-base.md"
        role_path = resolved_root / "vault" / "prompts" / "roles" / f"{job_role}.md"

        base_stat = base_path.stat()
        role_stat = role_path.stat()

        base_mtime = base_stat.st_mtime_ns
        role_mtime = role_stat.st_mtime_ns

        cache_key = (str(resolved_root), job_role)

        with _cache_lock:
            if cache_key in _cache:
                cached_base_mtime, cached_role_mtime, cached_segment = _cache[cache_key]
                if cached_base_mtime == base_mtime and cached_role_mtime == role_mtime:
                    return cached_segment

        base_text = base_path.read_text(encoding="utf-8")
        role_text = role_path.read_text(encoding="utf-8")

        base_text_stripped = base_text.strip()
        role_text_stripped = role_text.strip()

        text = render([
            Segment(kind="stable", label="universal-base", text=base_text_stripped),
            Segment(kind="stable", label=f"roles/{job_role}", text=role_text_stripped),
        ])

        segment = Segment(kind="stable", label=f"rolepack:{job_role}", text=text)

        with _cache_lock:
            _cache[cache_key] = (base_mtime, role_mtime, segment)

        return segment

    except (OSError, UnicodeDecodeError) as e:
        LOG.warning("Failed to load role pack for %r: %s", job_role, e)
        return None
