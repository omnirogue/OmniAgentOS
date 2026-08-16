"""Pure policy loading and evaluation helpers."""

from __future__ import annotations

from enum import StrEnum
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from omniagentos.contracts import (
    ActionClass,
    ApprovalState,
    HarnessType,
    PolicyDecision,
    SandboxSpec,
)


class PolicyError(ValueError):
    """Raised when policy input cannot be evaluated safely."""


class PolicyMode(StrEnum):
    """Autonomy posture for the whole system (AC-policy).

    AUTO is the default. Its ``irreversible`` ActionClass is a fail-closed routing
    floor; the AD-15 resolver then parks only money/customer/production-delete,
    refuses bank writes, and may auto-run proven local-temp deletes. SUPERVISED
    restores configured floors for external_reversible + consequential too.
    """

    AUTO = "auto"
    SUPERVISED = "supervised"


class AutonomyTier(StrEnum):
    """Operator-facing autonomy tier (AUTO-APPROVE Phase 3).

    ``hands_off`` keeps the AUTO class-routing floor while allowing shell work
    already proven inside granted roots. AD-15 approval decisions remain
    finance-only: secret reads and unresolved/production deletes park, remote
    actions are not approval hard stops, and bank writes refuse.
    """

    SUPERVISED = "supervised"
    AUTO = "auto"
    HANDS_OFF = "hands_off"


# The one ActionClass routing floor in AUTO mode. Frozen: provisioning imports
# ``is_hard_stop`` to auto-grant every non-floor scope, so membership is a
# cross-package contract. It is deliberately broader than the effective AD-15
# approval gate; orchestrator.approvals resolves the actual finance-only policy.
HARD_STOP_CLASSES: frozenset[ActionClass] = frozenset({ActionClass.IRREVERSIBLE})


def is_hard_stop(action_class: ActionClass | str) -> bool:
    """Frozen predicate: True when ``action_class`` reaches the AUTO routing floor.

    This is the single source of truth other packages (e.g. provisioning) rely on
    to decide what scope may be auto-granted without a human. It fails CLOSED: an
    unknown / malformed class is treated as a hard-stop (do not auto-grant).
    """
    try:
        normalized = ActionClass(action_class)
    except (TypeError, ValueError):
        return True
    return normalized in HARD_STOP_CLASSES


class ActionClassPolicy(BaseModel):
    """Policy settings for one action class."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    requires_approval: bool
    always_human: bool = False


class ToolsPolicy(BaseModel):
    """Known tools and their sandbox mappings."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    known: list[str] = Field(default_factory=list)
    sandbox_mapping: dict[str, dict[str, str]] = Field(default_factory=dict)


class PolicyConfig(BaseModel):
    """Validated representation of ``configs/policy.yaml``."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    mode: PolicyMode = PolicyMode.AUTO
    autonomy: AutonomyTier = AutonomyTier.AUTO
    action_classes: dict[ActionClass, ActionClassPolicy]
    tools: ToolsPolicy
    approval_expiry_hours: int
    stale_worker_seconds: int


class ApprovalGateResult(BaseModel):
    """Result of approval_satisfies_gate, capturing both human-decision and expiry checks."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    human_ok: bool
    """True when the approval has a valid human decision (or always_human is not required)."""

    expired: bool
    """True when the approval is past its expiry (state=PENDING, now_iso >= expires_at)."""

    reason: str = ""
    """Optional explanatory message."""


# Non-human decider identities. An ``always_human`` approval decided by any of
# these can NEVER satisfy the human gate (SEC-002). The rejection lives here so
# it is the single source of truth for both the runner and the Session Bridge
# supervisor — no component may re-derive a weaker variant.
_AUTOMATION_DECIDER_PREFIXES = frozenset({"bot", "automation", "session-supervisor", "system"})


def _is_automation_identity(decided_by: str, actor: str) -> bool:
    """Return True when ``decided_by`` is an automated / non-human identity.

    Rejected identities: empty/whitespace, the deciding actor itself (self-approval),
    any ``runner:*`` worker signature, any ``*-bot`` name, and any identity whose
    scheme prefix is bot/automation/system/session-supervisor (e.g. ``bot:ci``).
    """
    identity = decided_by.strip().lower()
    if not identity:
        return True
    if identity == str(actor or "").strip().lower():
        return True
    if identity.startswith("runner:"):
        return True
    if identity.endswith("-bot"):
        return True
    return identity.split(":", 1)[0] in _AUTOMATION_DECIDER_PREFIXES


def load_policy(path: str | None = None) -> PolicyConfig:
    """Load and validate a policy configuration from YAML.

    Default path resolves cwd-independently via contracts.default_policy_path()
    so the runner/API work when launched from any directory (integration fix)."""
    if path is None:
        from omniagentos.contracts import default_policy_path

        path = default_policy_path()
    try:
        with open(path, encoding="utf-8") as policy_file:
            raw: Any = yaml.safe_load(policy_file)
        return PolicyConfig.model_validate(raw)
    except (OSError, yaml.YAMLError, ValidationError) as exc:
        raise PolicyError(f"Unable to load policy from {path!r}: {exc}") from exc


def evaluate_action(
    action_class: ActionClass,
    cfg: PolicyConfig,
    *,
    in_granted_scope: bool = False,
) -> PolicyDecision:
    """Return the decision for an action class under the active mode, failing closed.

    AUTO mode (default): the irreversible class reaches the approval routing floor.
    For orchestrator-owned sessions, the downstream AD-15 resolver parks money
    writes, customer writes, secret reads, and production/unresolved deletes; refuses
    bank writes; and auto-approves proven local-temp deletes plus remote operations.

    Under ``autonomy=hands_off``, a hard-stop class that is *fully inside granted
    roots* (``in_granted_scope=True``) auto-executes. Callers must only set that
    flag when every path operand is proven in-scope. The finance-only resolver is
    still authoritative for bank/money/customer operations.

    SUPERVISED mode: the legacy behavior -- the configured ``action_classes``
    floors gate external_reversible + consequential (+ irreversible) as before.

    Fails closed on an unknown class in both modes.
    """
    try:
        normalized = ActionClass(action_class)
    except (TypeError, ValueError) as exc:
        raise PolicyError(f"Unknown action class {action_class!r}; action denied") from exc

    if cfg.mode == PolicyMode.AUTO:
        if is_hard_stop(normalized):
            if (
                cfg.autonomy == AutonomyTier.HANDS_OFF
                and in_granted_scope
                and normalized is ActionClass.IRREVERSIBLE
            ):
                return PolicyDecision(
                    requires_approval=False,
                    always_human=False,
                    reason=(
                        "HANDS_OFF: irreversible auto-execute — all path operands "
                        "proven inside granted roots"
                    ),
                )
            return PolicyDecision(
                requires_approval=True,
                always_human=True,
                reason=(
                    f"AUTO mode routing floor: {normalized.value} "
                    "(AD-15 finance-only resolution required)"
                ),
            )
        # Grok product stance (STATUS: full-auto with finance-only approvals):
        # CONSEQUENTIAL auto-executes under AUTO. Money/customer HARD_HUMAN capabilities
        # still refuse unattended via connectors.broker.HARD_HUMAN_CLASSES — that
        # gate is store-backed (grant_id + grant_store), never a caller-supplied row.
        # Reason string is greppable: "AUTO mode gate: consequential"
        if normalized is ActionClass.CONSEQUENTIAL:
            return PolicyDecision(
                requires_approval=False,
                always_human=False,
                reason=(
                    "AUTO mode gate: consequential auto-execute "
                    "(finance/HARD_HUMAN still broker-gated)"
                ),
            )
        # Remaining classes auto-execute under AUTO for max production speed.
        return PolicyDecision(
            requires_approval=False,
            always_human=False,
            reason=f"AUTO mode auto-execute for action class {normalized.value}",
        )

    action_policy = cfg.action_classes.get(normalized)
    if action_policy is None:
        raise PolicyError(f"Action class {normalized.value!r} is not configured; action denied")

    return PolicyDecision(
        requires_approval=action_policy.requires_approval,
        always_human=action_policy.always_human,
        reason=f"Policy for action class {normalized.value}",
    )


def approval_satisfies_gate(
    approval: dict[str, Any],
    decision: PolicyDecision,
    *,
    actor: str,
    now_iso: str,
) -> ApprovalGateResult:
    """Pure function to evaluate approval against both human-decision and expiry gates.

    This is the centralized gate logic used by both the runner and (later) the Session Bridge
    supervisor to enforce the invariant: "an always_human action may only proceed on a real
    HUMAN decision, and a pending approval past its expiry is not valid" (DR-002).

    Args:
        approval: The approval dict from the store, with fields like decided_by, expires_at, state.
        decision: The PolicyDecision result from evaluate_action, with always_human flag.
        actor: The automated actor identity (e.g., runner's self.actor), for rejecting runner-signed decisions.
        now_iso: Current timestamp in ISO format (caller supplies, keeps function pure/testable).

    Returns:
        ApprovalGateResult with human_ok (human-decision check) and expired (expiry check).
    """
    # Human-decision gate: check if the approval satisfies the always_human requirement.
    # - If always_human is False, any approval is OK (human_ok = True).
    # - If always_human is True, require a real HUMAN decision: decided_by must be
    #   present AND must not be an automation/non-human identity (SEC-002). The
    #   full non-human rule lives in _is_automation_identity so runner and
    #   supervisor share one definition (no weaker copy anywhere).
    human_ok = True
    if decision.always_human:
        decided_by = str(approval.get("decided_by") or "")
        human_ok = bool(decided_by.strip()) and not _is_automation_identity(decided_by, actor)

    # Expiry gate: an approval past its expires_at is not usable as resume authority.
    # Checked for PENDING *and* APPROVED (T-CODE-002): a late human approval on an
    # already-expired row must NOT become resumable — expiry binds regardless of a
    # subsequent approve. Terminal decisions (rejected/expired) keep their own
    # handling and are never re-reported as "expired" here.
    expired = False
    state = approval.get("state")
    expires_at = approval.get("expires_at")
    if (
        expires_at
        and str(expires_at) <= now_iso
        and state in {ApprovalState.PENDING.value, ApprovalState.APPROVED.value}
    ):
        expired = True

    return ApprovalGateResult(human_ok=human_ok, expired=expired)


def validate_tools(tools_allowed: list[str], cfg: PolicyConfig) -> None:
    """Reject the first tool that is neither a known primitive nor a capability.

    A tools_allowed list mixes two namespaces:

      * bare primitives -- 'shell', 'file_write' -- declared in policy.yaml, which
        describe what the harness may do to its own workspace; and
      * namespaced connector capabilities -- 'stripe_acmeuni.read' -- declared in
        configs/connectors.yaml, which describe what it may reach in the outside
        world through the broker.

    Both fail closed. The dot is what distinguishes them, which is why the registry
    enforces that every capability id is 'connector.action'.
    """
    known = cfg.tools.known
    registry_caps: set[str] | None = None
    for tool in tools_allowed:
        if tool in known:
            continue
        if "." in tool:
            if registry_caps is None:
                # Lazy import: keeps the policy module free of a hard dependency on
                # the connector package, which imports contracts.
                from omniagentos.connectors import load_registry

                registry_caps = set(load_registry().capabilities)
            if tool in registry_caps:
                continue
            raise PolicyError(f"Unknown capability {tool!r}; not in the connector registry")
        raise PolicyError(f"Unknown tool {tool!r}; known tools: {known}")


def sandbox_for_tools(
    harness: HarnessType, tools_allowed: list[str], cfg: PolicyConfig
) -> SandboxSpec:
    """Select the least-privileged configured sandbox for a tool allowlist."""
    validate_tools(tools_allowed, cfg)
    level = (
        "workspace_write"
        if "file_write" in tools_allowed or "shell" in tools_allowed
        else "read_only"
    )
    harness_name = harness.value if isinstance(harness, HarnessType) else str(harness)
    detail = cfg.tools.sandbox_mapping.get(level, {}).get(harness_name, "")
    return SandboxSpec(level=level, detail=detail)


__all__ = [
    "HARD_STOP_CLASSES",
    "ActionClassPolicy",
    "ApprovalGateResult",
    "AutonomyTier",
    "PolicyConfig",
    "PolicyError",
    "PolicyMode",
    "ToolsPolicy",
    "approval_satisfies_gate",
    "evaluate_action",
    "is_hard_stop",
    "load_policy",
    "sandbox_for_tools",
    "validate_tools",
]
