"""Deterministic, lane-local plans for responding to governance violations."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

__all__ = [
    "Lane",
    "ViolationActionPlan",
    "ViolationContext",
    "ViolationKind",
    "longhaul_waiting_review",
    "plan_violation_response",
    "runner_quarantine_park",
    "swarm_revert_flag",
]

Lane = Literal["swarm", "runner", "longhaul", "session"]
ViolationKind = Literal["scope", "tier_p", "undeclared_write", "policy", "unknown"]


@dataclass(frozen=True, slots=True)
class ViolationContext:
    """The minimum fact set a lane needs to choose its safe next state."""

    lane: Lane
    kind: ViolationKind
    holder_id: str
    paths: tuple[str, ...] = ()
    tier_p: bool = False
    message: str = ""


@dataclass(frozen=True, slots=True)
class ViolationActionPlan:
    """Ordered, machine-readable response actions with operator-facing context."""

    lane: Lane
    actions: tuple[str, ...]
    park_state: str | None
    flag: str | None
    reason: str


def _reason(ctx: ViolationContext) -> str:
    detail = ctx.message.strip() or ctx.kind
    return f"{ctx.lane} violation for {ctx.holder_id}: {detail}"


def swarm_revert_flag(ctx: ViolationContext) -> ViolationActionPlan:
    """Plan swarm rollback and flagging; Tier-P receives an explicit hard block."""
    if ctx.tier_p or ctx.kind == "tier_p":
        return ViolationActionPlan(
            lane="swarm",
            actions=("revert_worktree", "block_tier_p", "notify_operator"),
            park_state=None,
            flag="tier_p_violation",
            reason=_reason(ctx),
        )
    return ViolationActionPlan(
        lane="swarm",
        actions=("revert_worktree", "flag_scope_violation", "notify_operator"),
        park_state=None,
        flag="scope_violation",
        reason=_reason(ctx),
    )


def runner_quarantine_park(ctx: ViolationContext) -> ViolationActionPlan:
    """Plan commit quarantine and a human-review park for runner work."""
    return ViolationActionPlan(
        lane="runner",
        actions=("quarantine_commit", "park_run", "notify_operator"),
        park_state="waiting_review",
        flag="violation_quarantined",
        reason=_reason(ctx),
    )


def longhaul_waiting_review(ctx: ViolationContext) -> ViolationActionPlan:
    """Plan a durable longhaul review wait without mutating the active worktree."""
    return ViolationActionPlan(
        lane="longhaul",
        actions=("set_park_state:waiting_review", "notify_operator"),
        park_state="waiting_review",
        flag="violation_waiting_review",
        reason=_reason(ctx),
    )


def plan_violation_response(ctx: ViolationContext) -> ViolationActionPlan:
    """Dispatch to a lane handler; unsupported lanes fail closed."""
    if ctx.lane == "swarm":
        return swarm_revert_flag(ctx)
    if ctx.lane == "runner":
        return runner_quarantine_park(ctx)
    if ctx.lane == "longhaul":
        return longhaul_waiting_review(ctx)
    raise ValueError(f"no violation response is defined for lane {ctx.lane!r}")
