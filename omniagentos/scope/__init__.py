"""Scope algebra: realms, realm-relative paths, and the ownership conflict matrix.

The foundation every Phase-3 lane keys off. Three layers, all pure except for
``realpath`` and one memoized ``git rev-parse``:

- :mod:`omniagentos.scope.paths` — normalize a path to a component tuple, decide
  which REALM a directory belongs to, and answer containment/overlap.
- :mod:`omniagentos.scope.model` — :class:`ScopeClaim`, the value object a lane
  claims and a lock row persists.
- :mod:`omniagentos.scope.conflict` — the four-case matrix plus a deterministic
  blocker ordering.

The one invariant worth memorizing: ``()`` is the whole realm. It is an ancestor
of every path including itself, which is why ``"."`` needs no special case
anywhere in this package (and why the pre-existing
``SwarmScheduler._paths_overlap(".", "src/a") == False`` bug cannot recur here).

This package must never import from ``omniagentos.swarm`` /
``omniagentos.runner`` / ``omniagentos.db`` — it sits UNDER them.
"""

from omniagentos.scope.conflict import (
    CONFLICT_REASONS,
    ConflictReason,
    ScopeConflict,
    conflicts_with,
    find_conflicts,
    first_conflict,
)
from omniagentos.scope.model import (
    COMMIT_PURPOSE,
    DEFAULT_PURPOSE,
    SCOPE_KINDS,
    ScopeClaim,
    ScopeKind,
)
from omniagentos.scope.paths import (
    WHOLE_REALM,
    ScopePathError,
    clear_realm_cache,
    normalize_rel,
    overlap,
    private_workspace_bases,
    realm_of,
    register_private_base,
    rel_text,
    resolve_into_realm,
    safe_component,
    under,
)

__all__ = [
    "COMMIT_PURPOSE",
    "CONFLICT_REASONS",
    "DEFAULT_PURPOSE",
    "SCOPE_KINDS",
    "WHOLE_REALM",
    "ConflictReason",
    "ScopeClaim",
    "ScopeConflict",
    "ScopeKind",
    "ScopePathError",
    "clear_realm_cache",
    "conflicts_with",
    "find_conflicts",
    "first_conflict",
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
