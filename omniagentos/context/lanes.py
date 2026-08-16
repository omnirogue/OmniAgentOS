"""LaneProfile registry: per-lane read/write designations over memory/context surfaces.

Per PLAN.md §1 binding invariant 2, this module implements the ONE prompt-side
read-designation store for memory/context designations. Connector grants remain
in agent_capabilities; learn.* ACLs remain the digest plane's. One policy SHAPE,
three stores, three enforcement points.

LaneProfile
-----------
A holder (canonical lane identity) combined with a surface (memory family name),
scope (task/project/user/channel/none), and mode (read/write/read-write) defines
what the holder may access. Default is DENY: omitted (holder × surface × scope)
tuples are forbidden.

Every row MUST name its revocation: a meaningful identifier that explains how to
disable that row's grant. Wildcard holders like "*" or "all-agents" MUST fail
load. A row missing or bearing None as revocation MUST fail load.

Canonical lane identities (enforced by a CHECK constraint in the capability_requests
table, U-E1's migration) are:
  - lane:runner.step
  - lane:swarm.planner
  - lane:swarm.worker.<formation>  (e.g., lane:swarm.worker.batch-eval)
  - lane:intake.planner
  - lane:sessions
  - lane:chat
  - loop:<instance_id>
  - job:<catalog-key>
  - human:<operator>

Non-canonical spellings (agent:bob, system:foo) are rejected.

authorize_memory_access
-----------------------
Returns (allow: bool, receipt: dict) where receipt always carries:
  - holder (the requesting identity)
  - surface (memory surface being queried)
  - scope (task/project/user/channel/None)
  - mode (read/write/read-write)
  - grant_ref (the revocation reference if allowed; "none" if denied)

When a holder seeks access to a surface and the registry has no matching row,
the decision is DENY and grant_ref is set to "none" (no row to revoke).

Two rules the word "deny-by-default" is easy to write and easy to lose:

* The designation's ``mode`` is a CEILING, not a label. ``read`` authorizes
  ``read`` and nothing else; only ``read-write`` authorizes ``read-write``.
* A designation that NAMES a scope is only satisfied by a request that names
  the SAME scope type. An unscoped request against a scoped row is a request
  to read across every scope at once, which is wider than the row, so it is
  denied. Omission is never the way past a check.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

__all__ = [
    "LaneProfile",
    "AccessReceipt",
    "authorize_memory_access",
    "is_machine_identity",
    "load_profile",
    "SEEDED_DESIGNATIONS",
]


# Canonical identities for the capability system (U-E7 binding invariant).
CANONICAL_LANE_PREFIXES = {
    "lane:runner.step",
    "lane:swarm.planner",
    "lane:swarm.worker.",  # Prefix; full form is lane:swarm.worker.<formation>
    "lane:intake.planner",
    "lane:sessions",
    "lane:chat",
    "loop:",  # Prefix; full form is loop:<instance_id>
    "job:",   # Prefix; full form is job:<catalog-key>
    "human:", # Prefix; full form is human:<operator>
}

AccessMode = Literal["read", "write", "read-write"]
ScopeType = Literal["task", "project", "user", "channel", "none"]

#: What a designation of each mode may authorize. The designation is a CEILING,
#: so ``read-write`` is the only one that satisfies a ``read-write`` request:
#: a holder with a read-only row asking for read-write is asking to escalate,
#: and the answer is no.
_PERMITTED_REQUESTS: dict[str, frozenset[str]] = {
    "read": frozenset({"read"}),
    "write": frozenset({"write"}),
    "read-write": frozenset({"read", "write", "read-write"}),
}


#: Prefixes of the canonical U-E7 identities that are NOT people or org agents.
#: Since migration 108 these are seeded into the ``agents`` table, because that
#: table is also the identity table broker grants foreign-key to.
MACHINE_IDENTITY_PREFIXES: tuple[str, ...] = ("lane:", "loop:", "job:", "human:")


def is_machine_identity(identity: str | None) -> bool:
    """Whether ``identity`` is a canonical grant HOLDER rather than an org agent.

    ``agents`` does double duty: it is the company roster AND the identity table
    that ``agent_capabilities`` references, so ``loop:render_probe`` sits in it
    beside real agents. Anything asking a headcount question ("which agents are
    unnecessary", "who reports to whom") must filter these out — they are not
    staff, they have no harness and no model, and counting them pollutes exactly
    the analysis that proposes retiring agents.

    Anything asking a GRANT question must NOT filter them out: they are the
    holders the whole capability system is addressed to.
    """
    return bool(identity) and str(identity).startswith(MACHINE_IDENTITY_PREFIXES)


def _is_canonical_holder(holder: str) -> bool:
    """Validate that holder follows canonical identity spelling (K2-8, U-E7)."""
    if not holder or not isinstance(holder, str):
        return False
    if holder == "*" or holder == "all-agents":
        return False
    # Exact match for fixed identities
    if holder in ("lane:runner.step", "lane:swarm.planner", "lane:intake.planner",
                   "lane:sessions", "lane:chat"):
        return True
    # Prefix match for dynamic identities
    for prefix in CANONICAL_LANE_PREFIXES:
        if prefix.endswith("."):
            # lane:swarm.worker.<formation>, lane:swarm.worker.xyz
            if holder.startswith(prefix):
                return len(holder) > len(prefix)
        elif prefix.endswith(":"):
            # loop:<id>, job:<key>, human:<op>
            if holder.startswith(prefix):
                return len(holder) > len(prefix)
    return False


@dataclass(frozen=True)
class AccessDesignation:
    """One row in the LaneProfile: a grant over a surface to a holder."""

    holder: str  # Canonical identity: lane:*, loop:*, job:*, human:*
    surface: str  # Memory surface name: conversation, knowledge, metacog, memlife, etc.
    scope: ScopeType | None  # task, project, user, channel, or none (no scope check)
    mode: AccessMode  # read, write, or read-write
    grant_ref: str  # External authority: "system capture grant", "knowledge service", etc.
    revocation: str  # How to disable: "revoke runner row", "demote record", "remove project grant"

    def __post_init__(self) -> None:
        """Validate the designation at construction."""
        if not _is_canonical_holder(self.holder):
            raise ValueError(
                f"Non-canonical holder '{self.holder}': must be lane:*, loop:*, job:*, human:*, "
                "or a fixed lane identity (lane:runner.step, lane:swarm.planner, etc.)"
            )
        if not self.surface or not isinstance(self.surface, str):
            raise ValueError(f"Invalid surface '{self.surface}': must be a non-empty string")
        if self.mode not in ("read", "write", "read-write"):
            raise ValueError(f"Invalid mode '{self.mode}': must be read, write, or read-write")
        if not self.revocation or not isinstance(self.revocation, str):
            raise ValueError(
                f"Row for {self.holder}/{self.surface} has invalid revocation '{self.revocation}': "
                "revocation must be a non-empty string"
            )
        if "*" in self.revocation or self.revocation.lower() in ("none", ""):
            raise ValueError(
                f"Row for {self.holder}/{self.surface} has weak revocation '{self.revocation}': "
                "revocation must be specific (not '*', 'none', or empty)"
            )


@dataclass(frozen=True)
class AccessReceipt:
    """Machine-readable decision and metadata for a memory access request."""

    allowed: bool
    holder: str
    surface: str
    scope: tuple[str, str] | None  # (scope_type, scope_id)
    mode: AccessMode
    grant_ref: str  # Revocation reference if allowed; "none" if denied

    @property
    def scope_display(self) -> str:
        """Human-readable scope representation."""
        if self.scope is None:
            return "none"
        scope_type, scope_id = self.scope
        return f"{scope_type}:{scope_id}"


@dataclass
class LaneProfile:
    """Registry of per-lane memory/context read/write designations.

    Default is DENY: an omitted (holder × surface × scope) entry is forbidden.
    Every row must name a meaningful revocation; wildcards and revocation-less
    rows fail load.
    """

    designations: dict[tuple[str, str], AccessDesignation] = field(default_factory=dict)

    def add_designation(self, designation: AccessDesignation) -> None:
        """Add a designation to the registry (idempotent for the same key)."""
        key = (designation.holder, designation.surface)
        if key in self.designations:
            existing = self.designations[key]
            if existing != designation:
                raise ValueError(
                    f"Duplicate designation for {designation.holder}/{designation.surface} "
                    "with different parameters"
                )
        else:
            self.designations[key] = designation

    def authorize(
        self,
        holder: str,
        surface: str,
        scope: tuple[str, str] | None,
        mode: AccessMode,
    ) -> AccessReceipt:
        """Authorize a memory access request.

        Parameters
        ----------
        holder : str
            Canonical lane identity (lane:runner.step, loop:xyz, etc.)
        surface : str
            Memory surface name (conversation, knowledge, metacog, etc.)
        scope : (scope_type, scope_id) tuple or None
            Task/project/user/channel context; None means no scope check.
        mode : 'read' | 'write' | 'read-write'
            The requested access mode.

        Returns
        -------
        AccessReceipt
            Decision (allowed/denied) and metadata for audit/telemetry.
        """
        # Validate holder canonicity first (K2-8 requirement).
        if not _is_canonical_holder(holder):
            return AccessReceipt(
                allowed=False,
                holder=holder,
                surface=surface,
                scope=scope,
                mode=mode,
                grant_ref="invalid_holder_identity",
            )

        # Check registry for a matching designation.
        key = (holder, surface)
        if key not in self.designations:
            # No row for this holder+surface → DENY (default-none).
            return AccessReceipt(
                allowed=False,
                holder=holder,
                surface=surface,
                scope=scope,
                mode=mode,
                grant_ref="none",
            )

        designation = self.designations[key]

        # Scope. A designation that NAMES a scope is a scoped grant, and an
        # unscoped query is not a narrower request — it is a request to read
        # across every scope at once. The original ``and scope is not None``
        # made omission the way past the check: ``recall(query,
        # holder="lane:runner.step", scope=None)`` authorized a task-scoped row
        # for an unscoped conversation read. Absence of a scope is now a DENY
        # against a scoped row, so the only way to satisfy a scoped designation
        # is to say which scope you are in.
        if designation.scope is not None:
            if scope is None or scope[0] != designation.scope:
                return AccessReceipt(
                    allowed=False,
                    holder=holder,
                    surface=surface,
                    scope=scope,
                    mode=mode,
                    grant_ref=designation.revocation,
                )

        # Mode. The designation is the CEILING; the request may not exceed it.
        # ``read-write`` is strictly more than either half, so a ``read`` row
        # authorizing a ``read-write`` request (and a ``write`` row doing the
        # same) was an escalation available through the public
        # ``authorize_memory_access`` API: ask for read-write, hold read.
        allowed = mode in _PERMITTED_REQUESTS.get(designation.mode, frozenset())

        return AccessReceipt(
            allowed=allowed,
            holder=holder,
            surface=surface,
            scope=scope,
            mode=mode,
            grant_ref=designation.revocation,
        )


# Global singleton instance (loaded once at module import).
_GLOBAL_PROFILE: LaneProfile | None = None


def load_profile(designations: list[AccessDesignation]) -> LaneProfile:
    """Load and validate a LaneProfile from a list of designations.

    Raises ValueError if any row is invalid (wildcard holder, missing revocation,
    non-canonical identity, etc.).

    Parameters
    ----------
    designations : list[AccessDesignation]
        Rows to load. Each is validated at construction (see AccessDesignation).

    Returns
    -------
    LaneProfile
        Validated registry ready for authorization queries.
    """
    profile = LaneProfile()
    for designation in designations:
        # AccessDesignation.__post_init__() already validated; we just add it.
        profile.add_designation(designation)
    return profile


def authorize_memory_access(
    holder: str,
    surface: str,
    scope: tuple[str, str] | None = None,
    mode: AccessMode = "read",
) -> AccessReceipt:
    """Authorize a memory access request using the global LaneProfile.

    This is the public API for recall.py and other memory consumers.

    Parameters
    ----------
    holder : str
        Canonical lane identity (lane:runner.step, loop:xyz, etc.)
    surface : str
        Memory surface name (conversation, knowledge, metacog, etc.)
    scope : (scope_type, scope_id) tuple or None
        Task/project/user/channel context; None means no scope check.
    mode : 'read' | 'write' | 'read-write'
        The requested access mode. Default is 'read'.

    Returns
    -------
    AccessReceipt
        Decision (allowed/denied) and metadata for audit/telemetry.
    """
    global _GLOBAL_PROFILE

    # Lazy-load the global profile on first call.
    if _GLOBAL_PROFILE is None:
        _GLOBAL_PROFILE = load_profile(SEEDED_DESIGNATIONS)

    return _GLOBAL_PROFILE.authorize(holder, surface, scope, mode)


# ============================================================================
# Seeded designations matching today's live behavior (SECTION-1 access matrix)
# ============================================================================

SEEDED_DESIGNATIONS: list[AccessDesignation] = [
    # Runner step (lane:runner.step)
    AccessDesignation(
        holder="lane:runner.step",
        surface="conversation",
        scope="task",
        mode="read-write",
        grant_ref="system capture grant",
        revocation="disable runner conversation row in memory policy manifest",
    ),
    AccessDesignation(
        holder="lane:runner.step",
        surface="knowledge",
        scope="project",
        mode="read",
        grant_ref="knowledge service grant",
        revocation="revoke lane reader role + manifest row",
    ),
    AccessDesignation(
        holder="lane:runner.step",
        surface="metacog",
        scope=None,  # Promoted-only: scope is checked by the leg, not the registry
        mode="read",
        grant_ref="steward promotion + register row",
        revocation="demote record or remove lane row",
    ),
    AccessDesignation(
        holder="lane:runner.step",
        surface="memlife",
        scope="project",
        mode="read",
        grant_ref="steward acceptance + project grant",
        revocation="reject lesson or remove project/lane row",
    ),

    # Swarm planner (lane:swarm.planner)
    AccessDesignation(
        holder="lane:swarm.planner",
        surface="knowledge",
        scope="project",
        mode="read",
        grant_ref="system planner profile",
        revocation="revoke planner profile row",
    ),

    # Sessions bridge (lane:sessions)
    AccessDesignation(
        holder="lane:sessions",
        surface="conversation",
        scope="user",  # session-channel scoped as user:channel
        mode="read-write",
        grant_ref="system session grant",
        revocation="terminate session or revoke session row",
    ),

    # Chat lane (lane:chat)
    AccessDesignation(
        holder="lane:chat",
        surface="conversation",
        scope="user",  # current chat/channel scoped
        mode="read-write",
        grant_ref="user-channel grant",
        revocation="close channel or revoke chat row",
    ),
]
