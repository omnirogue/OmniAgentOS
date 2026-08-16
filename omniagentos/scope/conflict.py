"""The conflict matrix: does a candidate claim collide with the held locks?

Four cases, decided on path COMPONENTS (never string prefixes), after both sides
are confirmed to be in the same realm:

===================  ================  =========================================
candidate.kind       held.kind         collides when
===================  ================  =========================================
``file``             ``file``          the components are the SAME path
``file``             ``root``          candidate is under held
``root``             ``file``          held is under candidate
``root``             ``root``          the two subtrees overlap
===================  ================  =========================================

``()`` — the whole realm — is an ancestor of everything INCLUDING ITSELF, so a
whole-realm root claim collides with every other claim in its realm, and two
whole-realm claims collide with each other.

Ordering is part of the contract. :func:`find_conflicts` and
:func:`first_conflict` sort by (depth ascending, lock id, path, kind) so the
BROADEST blocker is reported first and two racing claimants naming the same
candidate always get the same answer — which is what makes a waiter queue built
on this stable instead of thrashing between equally-valid blockers.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Literal

from omniagentos.path_containment import inode_paths_equal
from omniagentos.scope.model import COMMIT_PURPOSE, ScopeClaim
from omniagentos.scope.paths import overlap, under

__all__ = [
    "CONFLICT_REASONS",
    "ConflictReason",
    "ScopeConflict",
    "conflicts_with",
    "find_conflicts",
    "first_conflict",
]

ConflictReason = Literal[
    "same_file",
    "candidate_under_held",
    "held_under_candidate",
    "root_overlap",
]

CONFLICT_REASONS: tuple[ConflictReason, ...] = (
    "same_file",
    "candidate_under_held",
    "held_under_candidate",
    "root_overlap",
)


@dataclass(frozen=True, slots=True)
class ScopeConflict:
    """One blocking pair: the claim that was refused and the lock that refused it."""

    candidate: ScopeClaim
    held: ScopeClaim
    reason: ConflictReason

    def describe(self) -> str:
        """One line for an event payload / denial message."""
        holder = self.held.lock_id or self.held.owner_group or "held"
        return f"{self.candidate} blocked by {self.held} ({holder}): {self.reason}"


def _same_realm(a: ScopeClaim, b: ScopeClaim) -> bool:
    """Realm equality under the platform's path case rules.

    Realms produced by :func:`~omniagentos.scope.paths.realm_of` are already
    case-folded, so this is normally a plain string compare; the fold is kept as
    a backstop against a hand-built claim whose realm was not canonicalized —
    silently "no conflict" because of a case difference is exactly the failure
    this module exists to prevent.
    """
    # Safety (`is not False`): unknown identity fails closed to one conflict realm.
    return inode_paths_equal(a.realm, b.realm) is not False


def _matrix_reason(candidate: ScopeClaim, held: ScopeClaim) -> ConflictReason | None:
    """The four-case matrix, ignoring ownership. ``None`` == disjoint."""
    if candidate.is_root and held.is_root:
        return "root_overlap" if overlap(candidate.components, held.components) else None
    if candidate.is_root:  # held is a file
        return "held_under_candidate" if under(held.components, candidate.components) else None
    if held.is_root:  # candidate is a file
        return "candidate_under_held" if under(candidate.components, held.components) else None
    return "same_file" if candidate.components == held.components else None


def _same_owner_relaxed(candidate: ScopeClaim, held: ScopeClaim) -> bool:
    """The same-owner relaxation: two co-owned FILES are allowed to coexist.

    Conditions, all required: the same non-empty ``owner_group``; BOTH sides
    name exact files (a root claim on either side is NEVER relaxed — a subtree
    owner cannot be partially bypassed); the two files are not the same file;
    and the held lock is not a commit (a commit is realm-wide in effect, so it
    is never relaxed either).

    Deliberately near-dead code. Under today's matrix, two distinct files can
    only reach here from the ``same_file`` branch, which already requires equal
    components — so this can never flip a decision, and a test asserts that. It
    is implemented anyway because the moment file/file semantics widen (case
    folding, glob claims, directory-implying-file), the relaxation becomes
    reachable and the invariant "a root claim is never relaxed" needs to already
    be written down rather than rediscovered.
    """
    if not candidate.owner_group or candidate.owner_group != held.owner_group:
        return False
    if candidate.is_root or held.is_root:
        return False
    if candidate.components == held.components:
        return False
    return held.purpose != COMMIT_PURPOSE


def conflicts_with(candidate: ScopeClaim, held: ScopeClaim) -> ConflictReason | None:
    """Why ``candidate`` collides with ``held``, or ``None`` if it does not.

    Cross-realm pairs never collide — that is what a realm IS — so the realm
    gate runs before the matrix.
    """
    if not _same_realm(candidate, held):
        return None
    reason = _matrix_reason(candidate, held)
    if reason is None:
        return None
    if _same_owner_relaxed(candidate, held):
        return None
    return reason


def _order_key(conflict: ScopeConflict) -> tuple[int, str, str, str]:
    """Broadest blocker first, then a total tiebreak so the order is deterministic."""
    held = conflict.held
    return (held.depth, held.lock_id, held.path_text, held.kind)


def find_conflicts(candidate: ScopeClaim, held_locks: Iterable[ScopeClaim]) -> list[ScopeConflict]:
    """Every lock blocking ``candidate``, broadest-first, in deterministic order."""
    found = [
        ScopeConflict(candidate=candidate, held=held, reason=reason)
        for held in held_locks
        if (reason := conflicts_with(candidate, held)) is not None
    ]
    found.sort(key=_order_key)
    return found


def first_conflict(candidate: ScopeClaim, held_locks: Iterable[ScopeClaim]) -> ScopeConflict | None:
    """The single blocker to report, or ``None`` when the claim is grantable.

    Deterministic by construction: the minimum under :func:`_order_key`, so two
    claimants racing for the same path name the same blocker and queue behind
    the same lock instead of each waiting on a different one.
    """
    best: ScopeConflict | None = None
    best_key: tuple[int, str, str, str] | None = None
    for held in held_locks:
        reason = conflicts_with(candidate, held)
        if reason is None:
            continue
        conflict = ScopeConflict(candidate=candidate, held=held, reason=reason)
        key = _order_key(conflict)
        if best_key is None or key < best_key:
            best, best_key = conflict, key
    return best
