"""policy_gate — the bridge from a loop tool to ``omniagentos.policy``.

The control plane's classifier stays authoritative: this module *calls*
:func:`omniagentos.policy.evaluate_action` in-process and never re-implements a
decision. What it adds on top is the unattended-operation floor:

* a tool the instance was not granted is **denied** (P4 variant A: the callable
  is never reached);
* a tier >= T2 effect **always** parks for a human, even where AUTO mode would
  auto-execute the equivalent ActionClass for an interactive session — a loop
  has no operator watching it;
* anything the policy layer cannot classify **denies** (fails closed).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal

from omniagentos.contracts import ActionClass
from omniagentos.policy import PolicyError, evaluate_action
from omniagentos_loops.contracts import APPROVAL_FLOOR_TIER, RiskTier
from omniagentos_loops.tools import LoopTool

Decision = Literal["allow", "approve", "deny"]


@dataclass(frozen=True)
class GateVerdict:
    """What the gate decided about one (tool, args) pair."""

    decision: Decision
    tier: RiskTier
    action_class: ActionClass
    reason: str

    @property
    def parks(self) -> bool:
        return self.decision == "approve"


def evaluate_tool(ctx: Any, tool: LoopTool, args: Mapping[str, Any]) -> GateVerdict:
    """Classify one prospective effect. Pure w.r.t. the world: it writes nothing."""
    granted = ctx.tools.names()
    if tool.name not in granted:
        return GateVerdict(
            "deny",
            tool.tier,
            tool.resolved_action_class(),
            f"tool {tool.name!r} is not granted to loop instance {ctx.instance_id!r}",
        )

    denied = getattr(ctx, "denied_tools", frozenset())
    if tool.name in denied:
        return GateVerdict(
            "deny",
            tool.tier,
            tool.resolved_action_class(),
            f"tool {tool.name!r} is explicitly denied for this instance",
        )

    action_class = tool.resolved_action_class()
    try:
        decision = evaluate_action(action_class, ctx.policy_cfg)
    except PolicyError as exc:
        # Unknown/unclassifiable => deny. Never "assume read-only".
        return GateVerdict("deny", tool.tier, action_class, f"policy refused: {exc}")

    # The stricter of (what policy demands) and (what the loop floor demands).
    if decision.requires_approval or tool.tier >= APPROVAL_FLOOR_TIER:
        why = (
            f"policy: {decision.reason}"
            if decision.requires_approval
            else f"loop floor: tier {tool.tier.name} >= {APPROVAL_FLOOR_TIER.name}"
        )
        return GateVerdict("approve", tool.tier, action_class, why)

    return GateVerdict("allow", tool.tier, action_class, decision.reason)


def preview(ctx: Any, tool_name: str, args: Mapping[str, Any]) -> GateVerdict:
    """Verdict for a tool *name*, resolving an ungranted name to a denial."""
    from omniagentos_loops.contracts import UnknownToolError

    try:
        tool = ctx.tools.get(tool_name)
    except UnknownToolError as exc:
        return GateVerdict("deny", RiskTier.T3, ActionClass.IRREVERSIBLE, str(exc))
    return evaluate_tool(ctx, tool, args)


__all__ = ["Decision", "GateVerdict", "evaluate_tool", "preview"]
