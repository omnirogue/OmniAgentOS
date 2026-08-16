"""The scope claim value objects: WHAT a lane owns and WHO owns it.

Pure data + validation, no persistence. A later work package stores these rows;
the algebra in :mod:`omniagentos.scope.conflict` decides on them. Keeping the
model here means the conflict rules can be unit-tested with no database at all.

A claim is a ``(realm, kind, components)`` triple plus ownership metadata:

- ``realm`` — the conflict universe, from :func:`omniagentos.scope.paths.realm_of`.
  Claims in DIFFERENT realms can never conflict, so the realm must be a
  canonical key (case-folded realpath), never a caller's raw spelling.
- ``kind`` — ``"file"`` (this exact path) or ``"root"`` (this path and its whole
  subtree). ``components == ()`` is the WHOLE realm and is therefore always a
  root; a file claim on ``()`` is nonsense and is rejected.
- ``components`` — the normalized relative path tuple; ``()`` == whole realm.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

from omniagentos.scope.paths import ScopePathError, normalize_rel, rel_text

__all__ = [
    "COMMIT_PURPOSE",
    "DEFAULT_PURPOSE",
    "SCOPE_KINDS",
    "ScopeClaim",
    "ScopeKind",
]

ScopeKind = Literal["file", "root"]

#: The two shapes a claim can take. ``file`` collides only with the exact path
#: (or a root containing it); ``root`` collides with anything in its subtree.
SCOPE_KINDS: tuple[ScopeKind, ...] = ("file", "root")

#: Ordinary editing intent. ``purpose`` is deliberately an open string — the
#: conflict rules only special-case ``COMMIT_PURPOSE`` — so later work packages
#: can add intents without a migration to this module.
DEFAULT_PURPOSE = "edit"

#: The one purpose that is never relaxed: a commit touches the index/worktree as
#: a whole, so a co-owner may not slip a second write past it.
COMMIT_PURPOSE = "commit"


@dataclass(frozen=True, slots=True)
class ScopeClaim:
    """One claim on a path (or subtree) inside one realm.

    Frozen and hashable so claims can go in sets and be compared directly. Use
    :meth:`for_path` when starting from raw text — the constructor validates but
    does not normalize, because a stored tuple must round-trip byte-identically.

    ``lock_id`` is empty for a candidate that has not been granted yet, and
    carries the persisted lock's id once it is held. It participates in the
    deterministic conflict ordering (see :mod:`omniagentos.scope.conflict`) so
    two racing claimants report the SAME blocker.
    """

    realm: str
    components: tuple[str, ...] = ()
    kind: ScopeKind = "root"
    owner_group: str = ""
    purpose: str = DEFAULT_PURPOSE
    lock_id: str = ""

    def __post_init__(self) -> None:
        if not self.realm or not self.realm.strip():
            raise ScopePathError("scope claim requires a realm")
        if self.kind not in SCOPE_KINDS:
            raise ScopePathError(f"unknown scope kind: {self.kind!r}")
        if not isinstance(self.components, tuple):
            raise ScopePathError(f"components must be a tuple: {self.components!r}")
        for part in self.components:
            if not isinstance(part, str) or not part or part in (".", "..") or "/" in part:
                raise ScopePathError(f"bad scope component: {part!r}")
        if self.kind == "file" and not self.components:
            # () is the realm root -- a directory, and always the whole realm.
            raise ScopePathError("the whole realm cannot be claimed as a file")
        if not self.purpose:
            raise ScopePathError("scope claim requires a purpose")

    @classmethod
    def for_path(
        cls,
        realm: str,
        path: str | Sequence[str],
        *,
        kind: ScopeKind = "file",
        owner_group: str = "",
        purpose: str = DEFAULT_PURPOSE,
        lock_id: str = "",
    ) -> ScopeClaim:
        """Build a claim from a raw realm-relative path.

        The path goes through :func:`~omniagentos.scope.paths.normalize_rel`, so
        ``"."``/``"./"``/``"a/.."`` all become the whole realm and an escape
        raises :class:`~omniagentos.scope.paths.ScopePathError`. A path that
        normalizes to the whole realm is forced to ``kind="root"`` rather than
        rejected — ``"."`` with a file-ish default is the common planner
        spelling for "this task owns everything".
        """
        components = normalize_rel(path)
        effective: ScopeKind = "root" if not components else kind
        return cls(
            realm=realm,
            components=components,
            kind=effective,
            owner_group=owner_group,
            purpose=purpose,
            lock_id=lock_id,
        )

    @property
    def is_root(self) -> bool:
        """True when this claim covers a whole subtree, not one exact file."""
        return self.kind == "root"

    @property
    def is_whole_realm(self) -> bool:
        """True when this claim covers the entire realm (``components == ()``)."""
        return not self.components

    @property
    def path_text(self) -> str:
        """Display/wire spelling of the path; ``"."`` for the whole realm."""
        return rel_text(self.components)

    @property
    def depth(self) -> int:
        """Component depth — the whole realm is 0. Broadest-first ordering key."""
        return len(self.components)

    def __str__(self) -> str:
        suffix = "/**" if self.is_root else ""
        return f"{self.realm}:{self.path_text}{suffix}"
