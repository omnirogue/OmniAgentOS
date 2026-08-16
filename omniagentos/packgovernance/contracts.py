"""Closed typed vocabulary for automation-pack governance.

Nothing in this module reads a file, runs a command, or reaches a network. It
defines the only words the rest of the subsystem is allowed to use:

* an automation pack is UNTRUSTED DATA — every span of its prose is carried in
  :class:`UntrustedText` so it can never be mistaken for an instruction;
* an operator decision is AUTHORITY — dispositions and constraints are closed
  enums, so an unrecognised directive is visible rather than silently dropped;
* capabilities and side effects are DERIVED FACTS with per-item evidence, never
  adjectives copied out of the pack ("read-only", "advisory only");
* a governance tier is a FUNCTION of those derived facts plus the authority
  record, so a tier can always be explained by naming the checks that held.

The dataclasses are frozen and slotted: once intake has produced a record, no
later phase can edit it into something the checksum no longer covers.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from types import MappingProxyType

__all__ = [
    "ArtifactKind",
    "AuthorityConflict",
    "BoundArtifact",
    "Capability",
    "CapabilityEvidence",
    "CapabilityProfile",
    "ConstraintKind",
    "DecisionConstraint",
    "DiagnosisClaim",
    "Disposition",
    "DispositionKind",
    "GovernanceTier",
    "IntakeOutcome",
    "InjectionSignal",
    "ItemVerdict",
    "LineageRelation",
    "OperatorDecision",
    "PackItem",
    "PackSection",
    "PlacementRequirement",
    "PromptPack",
    "ProseClaim",
    "RefusalCode",
    "RefusalReason",
    "RepositoryBinding",
    "RepositoryLineage",
    "SideEffect",
    "TierDecision",
    "TierReason",
    "TriggerKind",
    "UntrustedText",
    "Verdict",
]


# --------------------------------------------------------------------------
# Untrusted prose
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class UntrustedText:
    """A span of pack prose, quarantined from instruction positions.

    This is deliberately NOT a ``str`` subclass. Prose lifted out of a pack must
    never flow into a prompt, a shell command or a policy decision by accident;
    a caller that wants the characters has to ask for them through
    :meth:`excerpt`, which truncates and single-lines them first.
    """

    raw: str
    field_name: str
    line: int

    def excerpt(self, limit: int = 160) -> str:
        """Return a single-line, length-bounded quotation suitable for a report."""
        flattened = " ".join(self.raw.split())
        if len(flattened) <= limit:
            return flattened
        return flattened[: limit - 1] + "…"

    def contains(self, needle: str) -> bool:
        """Case-insensitive substring probe used only to produce EVIDENCE."""
        return needle.casefold() in self.raw.casefold()

    def __len__(self) -> int:
        return len(self.raw)


@dataclass(frozen=True, slots=True)
class InjectionSignal:
    """A known prompt-injection or permission-escalation marker seen in the pack.

    A signal is evidence, not a verdict: this corpus legitimately contains
    ``--dangerously-skip-permissions`` inside a prohibition. Signals therefore
    force the full pipeline instead of silently rejecting an item.
    """

    marker: str
    line: int
    excerpt: str


@dataclass(frozen=True, slots=True)
class ProseClaim:
    """A permission-shaped assertion the pack makes about itself.

    Recorded so it can be CONTRADICTED by derived capability, per the operator
    rule that side effects are "derived and gated, never inferred from prose".
    """

    claim: str
    field_name: str
    line: int
    excerpt: str


# --------------------------------------------------------------------------
# Checksum binding
# --------------------------------------------------------------------------


class ArtifactKind(StrEnum):
    UNTRUSTED_PACK_FIXTURE = "untrusted-pack-fixture"
    OPERATOR_AUTHORITY = "operator-authority"


@dataclass(frozen=True, slots=True)
class BoundArtifact:
    """Bytes that have been proven to match a caller-declared checksum."""

    path: str
    kind: ArtifactKind
    sha256: str
    byte_length: int
    text: str
    normalized_line_endings: bool = False

    @property
    def is_authority(self) -> bool:
        return self.kind is ArtifactKind.OPERATOR_AUTHORITY


# --------------------------------------------------------------------------
# Pack structure
# --------------------------------------------------------------------------


class TriggerKind(StrEnum):
    """How an automation item is set running. Derived, never declared."""

    SCHEDULE = "schedule"
    PULL_REQUEST = "pull_request"
    PUSH = "push"
    MANUAL_DISPATCH = "workflow_dispatch"
    CRON_HOST = "cron_host"
    INTERACTIVE_SESSION = "interactive_session"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class PackSection:
    """A non-item heading kept so nothing in the pack is silently discarded."""

    heading: str
    level: int
    line: int
    body: UntrustedText


@dataclass(frozen=True, slots=True)
class PackItem:
    """One automation loop as the pack states it — all fields untrusted."""

    item_id: str
    title: UntrustedText
    line: int
    metadata: Mapping[str, UntrustedText] = field(default_factory=dict)
    prose: Mapping[str, UntrustedText] = field(default_factory=dict)
    code_blocks: tuple[tuple[str, UntrustedText], ...] = ()
    consolidated_into: str | None = None
    injection_signals: tuple[InjectionSignal, ...] = ()
    prose_claims: tuple[ProseClaim, ...] = ()

    def __post_init__(self) -> None:
        # A frozen dataclass only freezes the BINDINGS; a plain dict value would
        # let a later phase edit parsed untrusted text while the artifact
        # checksum still claims to cover it. Copy-then-proxy so even a caller
        # holding the constructor's dict cannot mutate the record.
        object.__setattr__(self, "metadata", MappingProxyType(dict(self.metadata)))
        object.__setattr__(self, "prose", MappingProxyType(dict(self.prose)))

    @property
    def pack_letter(self) -> str:
        return self.item_id[0]

    def metadata_text(self, key: str) -> str:
        entry = self.metadata.get(key.casefold())
        return entry.raw if entry is not None else ""

    def all_text(self) -> str:
        """Concatenate every untrusted span. Used ONLY for evidence extraction."""
        parts = [self.title.raw]
        parts.extend(value.raw for value in self.metadata.values())
        parts.extend(value.raw for value in self.prose.values())
        parts.extend(body.raw for _language, body in self.code_blocks)
        return "\n".join(parts)


@dataclass(frozen=True, slots=True)
class PromptPack:
    """A checksum-bound automation pack parsed into closed records."""

    artifact: BoundArtifact
    title: UntrustedText
    items: tuple[PackItem, ...]
    sections: tuple[PackSection, ...] = ()
    declared_repository: str | None = None
    injection_signals: tuple[InjectionSignal, ...] = ()

    @property
    def sha256(self) -> str:
        return self.artifact.sha256

    def item(self, item_id: str) -> PackItem | None:
        for candidate in self.items:
            if candidate.item_id == item_id:
                return candidate
        return None

    @property
    def item_ids(self) -> tuple[str, ...]:
        return tuple(candidate.item_id for candidate in self.items)


# --------------------------------------------------------------------------
# Operator authority
# --------------------------------------------------------------------------


class DispositionKind(StrEnum):
    """The closed set of stances an operator may take on a pack item."""

    APPROVED = "approved"
    REJECTED = "rejected"
    DEFERRED = "deferred"
    BLOCKED = "blocked"
    CANDIDATE = "candidate"
    UNSPECIFIED = "unspecified"

    @property
    def permits_execution(self) -> bool:
        return self is DispositionKind.APPROVED


@dataclass(frozen=True, slots=True)
class Disposition:
    """An operator stance on one item, with the sentence that produced it."""

    item_id: str
    kind: DispositionKind
    evidence: UntrustedText
    scope_note: str | None = None
    conditions: tuple[str, ...] = ()
    blockers: tuple[str, ...] = ()
    partial_scope: bool = False


@dataclass(frozen=True, slots=True)
class AuthorityConflict:
    """Two incompatible dispositions for the same item. Fails that item closed."""

    item_id: str
    kinds: tuple[DispositionKind, ...]
    evidence: tuple[str, ...]


class ConstraintKind(StrEnum):
    """Closed set of non-disposition operator directives."""

    GLOBAL_PROHIBITION = "global_prohibition"
    ITEM_PROHIBITION = "item_prohibition"
    SCOPE_EXCLUSION = "scope_exclusion"
    PROTECTED_SCOPE = "protected_scope"
    DERIVATION_RULE = "derivation_rule"
    EVIDENCE_TRUST_RULE = "evidence_trust_rule"
    CONTRADICTION_SURFACING = "contradiction_surfacing"
    FAST_TIER_RULE = "fast_tier_rule"
    ENGINE_REQUIREMENT = "engine_requirement"
    UNCLASSIFIED = "unclassified"


@dataclass(frozen=True, slots=True)
class DecisionConstraint:
    """One operator directive that is not itself a per-item disposition.

    ``item_ids`` names pack items the directive binds; ``scopes`` names paths or
    repositories it protects; ``forbidden_side_effects`` is the machine-readable
    translation of a prohibition, so the authorizer never re-reads the prose.
    """

    kind: ConstraintKind
    text: str
    line: int
    item_ids: tuple[str, ...] = ()
    scopes: tuple[str, ...] = ()
    forbidden_side_effects: frozenset[SideEffect] = frozenset()


@dataclass(frozen=True, slots=True)
class DiagnosisClaim:
    """A pack assertion about what went wrong, held separate from proposals.

    The operator ruled that a diagnosis "may be trusted only where independently
    reproduced", so ``reproduced`` starts False and only an evidence-bearing
    later phase may set it.
    """

    item_id: str
    text: UntrustedText
    reproduced: bool = False

    @property
    def trusted(self) -> bool:
        return self.reproduced


@dataclass(frozen=True, slots=True)
class OperatorDecision:
    """The checksum-bound authority record. The only source of permission."""

    artifact: BoundArtifact
    target_repository: str | None
    dispositions: tuple[Disposition, ...]
    constraints: tuple[DecisionConstraint, ...]
    conflicts: tuple[AuthorityConflict, ...] = ()
    approval_is_exclusive: bool = False
    unclassified_directives: tuple[str, ...] = ()

    @property
    def sha256(self) -> str:
        return self.artifact.sha256

    def disposition_for(self, item_id: str) -> Disposition | None:
        found: Disposition | None = None
        for candidate in self.dispositions:
            if candidate.item_id != item_id:
                continue
            # A conflict is reported separately; here the most restrictive wins.
            if found is None or _RESTRICTIVENESS[candidate.kind] > _RESTRICTIVENESS[found.kind]:
                found = candidate
        return found

    def constraints_of(self, kind: ConstraintKind) -> tuple[DecisionConstraint, ...]:
        return tuple(entry for entry in self.constraints if entry.kind is kind)

    @property
    def protected_scopes(self) -> tuple[str, ...]:
        scopes: list[str] = []
        for entry in self.constraints_of(ConstraintKind.PROTECTED_SCOPE):
            scopes.extend(entry.scopes)
        return tuple(dict.fromkeys(scopes))

    @property
    def globally_forbidden_side_effects(self) -> frozenset[SideEffect]:
        forbidden: set[SideEffect] = set()
        for entry in self.constraints_of(ConstraintKind.GLOBAL_PROHIBITION):
            forbidden |= entry.forbidden_side_effects
        return frozenset(forbidden)

    def forbidden_side_effects_for(self, item_id: str) -> frozenset[SideEffect]:
        forbidden = set(self.globally_forbidden_side_effects)
        for entry in self.constraints:
            if entry.kind is ConstraintKind.ITEM_PROHIBITION and item_id in entry.item_ids:
                forbidden |= entry.forbidden_side_effects
        return frozenset(forbidden)

    def exclusions_for(self, item_id: str) -> tuple[DecisionConstraint, ...]:
        return tuple(
            entry
            for entry in self.constraints
            if entry.kind is ConstraintKind.SCOPE_EXCLUSION and item_id in entry.item_ids
        )

    def has_conflict(self, item_id: str) -> bool:
        return any(entry.item_id == item_id for entry in self.conflicts)


# Higher wins when one item carries several stances. UNSPECIFIED is the floor,
# REJECTED the ceiling: an operator who both approved and rejected an item gets
# the refusal, never the approval.
_RESTRICTIVENESS: Mapping[DispositionKind, int] = {
    DispositionKind.UNSPECIFIED: 0,
    DispositionKind.APPROVED: 1,
    DispositionKind.CANDIDATE: 2,
    DispositionKind.DEFERRED: 3,
    DispositionKind.BLOCKED: 4,
    DispositionKind.REJECTED: 5,
}


# --------------------------------------------------------------------------
# Capabilities and side effects
# --------------------------------------------------------------------------


class Capability(StrEnum):
    """A permission an item must actually hold to run. Derived from mechanics."""

    CONTENTS_READ = "contents:read"
    CONTENTS_WRITE = "contents:write"
    ISSUES_WRITE = "issues:write"
    PULL_REQUESTS_WRITE = "pull-requests:write"
    ACTIONS_READ = "actions:read"
    ACTIONS_WRITE = "actions:write"
    REPOSITORY_ADMIN = "repository:admin"
    SECRET_READ = "secret:read"
    SCHEDULED_TRIGGER = "schedule:trigger"
    SHELL_EXEC = "shell:exec"
    NETWORK_EGRESS = "network:egress"
    SSH_REMOTE = "ssh:remote"
    FILESYSTEM_WRITE = "filesystem:write"
    PACKAGE_INSTALL = "package:install"
    CONTAINER_BUILD = "container:build"
    AGENT_RUNTIME = "agent:runtime"
    MODEL_CREDENTIAL = "credential:model-api"
    THIRD_PARTY_CREDENTIAL = "credential:third-party"


class SideEffect(StrEnum):
    """A change an item makes outside its own process."""

    REPO_ISSUE_WRITE = "repo.issue.write"
    REPO_PR_OPEN = "repo.pr.open"
    REPO_PR_MERGE = "repo.pr.merge"
    REPO_BRANCH_PUSH = "repo.branch.push"
    REPO_BRANCH_DELETE = "repo.branch.delete"
    REPO_SETTINGS_CHANGE = "repo.settings.change"
    PRODUCT_CODE_MUTATION = "product.code.mutation"
    DEPLOYMENT = "deployment"
    MONEY_MOVEMENT = "money.movement"
    CUSTOMER_COMMUNICATION = "customer.communication"
    EXTERNAL_SERVICE_WRITE = "external.service.write"
    SCHEDULE_ACTIVATION = "schedule.activation"
    CREDENTIAL_USE = "credential.use"


@dataclass(frozen=True, slots=True)
class CapabilityEvidence:
    """Why a capability or side effect was attributed to an item."""

    subject: str
    source_field: str
    line: int
    excerpt: str


@dataclass(frozen=True, slots=True)
class CapabilityProfile:
    """The derived permission and side-effect shape of one pack item."""

    item_id: str
    capabilities: frozenset[Capability]
    side_effects: frozenset[SideEffect]
    triggers: frozenset[TriggerKind]
    written_paths: tuple[str, ...]
    evidence: tuple[CapabilityEvidence, ...]
    prose_claims: tuple[ProseClaim, ...] = ()
    contradictions: tuple[str, ...] = ()
    declared_deterministic: bool = False

    @property
    def is_read_only(self) -> bool:
        return not self.side_effects and self.capabilities <= {Capability.CONTENTS_READ}

    @property
    def file_count(self) -> int:
        return len(self.written_paths)

    def evidence_for(self, subject: str) -> tuple[CapabilityEvidence, ...]:
        return tuple(entry for entry in self.evidence if entry.subject == subject)


# --------------------------------------------------------------------------
# Repository identity and lineage
# --------------------------------------------------------------------------


class LineageRelation(StrEnum):
    """How a pack item's repository relates to the bound target repository."""

    SAME_REPOSITORY = "same_repository"
    UNRELATED_LINEAGE = "unrelated_lineage"
    UNRESOLVED = "unresolved"


@dataclass(frozen=True, slots=True)
class RepositoryLineage:
    """One branch line. Lines that share no history need their own landings."""

    name: str
    is_default_branch: bool = False
    accepts_pull_requests: bool = False
    accepts_direct_pushes: bool = False
    shares_history_with: frozenset[str] = frozenset()

    def shares_history(self, other: str) -> bool:
        return other == self.name or other in self.shares_history_with


@dataclass(frozen=True, slots=True)
class RepositoryBinding:
    """Immutable identity of the repository this run may touch.

    Sourced from the operator decision and the loop contract, never from the
    pack: a pack that names its own repository is describing a DIFFERENT estate.
    """

    repository: str
    base_sha: str
    review_branch: str
    worktree: str
    source_branch: str | None = None
    push_disabled: bool = True
    lineages: tuple[RepositoryLineage, ...] = ()
    protected_scopes: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.repository:
            raise ValueError("repository binding requires a repository name")
        if len(self.base_sha) != 40 or not all(c in "0123456789abcdef" for c in self.base_sha):
            raise ValueError("repository binding requires a full lowercase 40-hex base SHA")
        if not self.review_branch:
            raise ValueError("repository binding requires a review branch")

    def lineage(self, name: str) -> RepositoryLineage | None:
        for candidate in self.lineages:
            if candidate.name == name:
                return candidate
        return None

    @property
    def default_branch(self) -> str | None:
        for candidate in self.lineages:
            if candidate.is_default_branch:
                return candidate.name
        return None


@dataclass(frozen=True, slots=True)
class PlacementRequirement:
    """Where an item's file must live for its triggers to fire at all.

    ``contradictions`` records the cases the operator asked to surface: a single
    copy on one line cannot guard lines that share no history, a scheduled
    workflow only fires from the default branch, and a PR gate must exist on the
    PR's base branch.
    """

    item_id: str
    required_branches: tuple[str, ...]
    triggers: frozenset[TriggerKind]
    rationale: tuple[str, ...]
    contradictions: tuple[str, ...] = ()
    relation: LineageRelation = LineageRelation.UNRESOLVED

    @property
    def is_executable_here(self) -> bool:
        return self.relation is LineageRelation.SAME_REPOSITORY and not self.contradictions


# --------------------------------------------------------------------------
# Governance tiers and verdicts
# --------------------------------------------------------------------------


class GovernanceTier(StrEnum):
    FAST = "fast"
    FULL = "full"


@dataclass(frozen=True, slots=True)
class TierReason:
    """One named eligibility check with its outcome and the evidence behind it."""

    code: str
    holds: bool
    detail: str


@dataclass(frozen=True, slots=True)
class TierDecision:
    """Tier plus the complete check list that produced it."""

    item_id: str
    tier: GovernanceTier
    reasons: tuple[TierReason, ...]

    @property
    def failed_checks(self) -> tuple[str, ...]:
        return tuple(reason.code for reason in self.reasons if not reason.holds)

    def explain(self) -> str:
        if self.tier is GovernanceTier.FAST:
            return "fast tier: every eligibility check held"
        return "full tier: " + ", ".join(self.failed_checks)


class RefusalCode(StrEnum):
    """Closed set of reasons the engine declines to act on an item."""

    ACTIVATION_DISABLED = "activation_disabled"
    CHECKSUM_UNBOUND = "checksum_unbound"
    NO_OPERATOR_APPROVAL = "no_operator_approval"
    OPERATOR_REJECTED = "operator_rejected"
    OPERATOR_DEFERRED = "operator_deferred"
    OPERATOR_BLOCKED = "operator_blocked"
    OPERATOR_CANDIDATE_ONLY = "operator_candidate_only"
    AUTHORITY_CONFLICT = "authority_conflict"
    OUTSIDE_EXCLUSIVE_APPROVAL = "outside_exclusive_approval"
    FOREIGN_REPOSITORY_LINEAGE = "foreign_repository_lineage"
    BRANCH_PLACEMENT_CONTRADICTION = "branch_placement_contradiction"
    PROTECTED_SCOPE = "protected_scope"
    FORBIDDEN_ACTION_REQUIRED = "forbidden_action_required"
    ITEM_PROHIBITION_VIOLATED = "item_prohibition_violated"
    SCOPE_EXCLUDED = "scope_excluded"
    UNKNOWN_ITEM = "unknown_item"


@dataclass(frozen=True, slots=True)
class RefusalReason:
    """A refusal that carries its own proof."""

    code: RefusalCode
    message: str
    evidence: tuple[str, ...] = ()

    def render(self) -> str:
        if not self.evidence:
            return f"{self.code.value}: {self.message}"
        return f"{self.code.value}: {self.message} [{'; '.join(self.evidence)}]"


class Verdict(StrEnum):
    ALLOW = "allow"
    REFUSE = "refuse"


@dataclass(frozen=True, slots=True)
class ItemVerdict:
    """The phase-1 answer for one pack item: act at a tier, or refuse with proof."""

    item_id: str
    verdict: Verdict
    tier: TierDecision
    disposition: Disposition | None
    profile: CapabilityProfile
    placement: PlacementRequirement
    refusals: tuple[RefusalReason, ...] = ()

    @property
    def allowed(self) -> bool:
        return self.verdict is Verdict.ALLOW and not self.refusals


@dataclass(frozen=True, slots=True)
class IntakeOutcome:
    """Everything phase 1 produces: bound inputs, derived facts, and verdicts."""

    pack: PromptPack
    decision: OperatorDecision
    binding: RepositoryBinding
    verdicts: tuple[ItemVerdict, ...]
    diagnosis_claims: tuple[DiagnosisClaim, ...] = ()
    activation_enabled: bool = False
    intake_refusals: tuple[RefusalReason, ...] = ()

    def verdict_for(self, item_id: str) -> ItemVerdict | None:
        for candidate in self.verdicts:
            if candidate.item_id == item_id:
                return candidate
        return None

    @property
    def allowed_items(self) -> tuple[str, ...]:
        return tuple(entry.item_id for entry in self.verdicts if entry.allowed)

    def items_at(self, tier: GovernanceTier) -> tuple[str, ...]:
        return tuple(
            entry.item_id for entry in self.verdicts if entry.allowed and entry.tier.tier is tier
        )

    def refusals_by_code(self, code: RefusalCode) -> tuple[str, ...]:
        return tuple(
            entry.item_id
            for entry in self.verdicts
            if any(reason.code is code for reason in entry.refusals)
        )


def freeze_capabilities(values: Iterable[Capability]) -> frozenset[Capability]:
    """Small helper so callers never build a mutable capability set by mistake."""
    return frozenset(values)
