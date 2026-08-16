"""The tier floor — the one rule a caller's policy adapter cannot argue with.

Thirty lines of real logic, and they are the reason an unattended loop is safe to
leave running. Everything else in this module is documentation of *why* those
thirty lines are shaped the way they are.

The rule is the **stricter-of** rule::

    if decision.requires_approval or tool.tier >= APPROVAL_FLOOR_TIER:
        park

Two independent authorities get a veto and neither gets a waiver. The caller's
:class:`~selfloop.ports.PolicyPort` classifies an action class and may demand a
human; this package demands a human for anything at or above
:data:`~selfloop.contracts.APPROVAL_FLOOR_TIER` no matter what the adapter said.

**Why the floor lives here and not in the adapter.** A policy tuned for an
interactive session can reasonably auto-execute a CONSEQUENTIAL action — there
is a person watching the terminal, and asking them to click twice is friction
without safety. The system this package was extracted from had exactly that
policy, returning ``requires_approval=False`` for CONSEQUENTIAL in its default
mode. An unattended loop inherits the policy and does *not* inherit the person,
so without the floor a T2 outbound send would have auto-executed at 3am with
nobody in the room. The floor is applied *after* the adapter's answer is read
and is not reachable from inside it: an adapter can make T0 and T1 stricter, and
there is no value it can return that lowers T2.

**Why the verdict is spelled ``park`` and not ``approve``.** The consequent of
the rule above used to read ``approve``, which parses as *grant authority* when
it means the exact opposite — *stop, and require a human*. A reviewer reading
the gate for the first time read it as an auto-approve and flagged it as a
literal misimplementation risk. No reading of ``park`` can be mistaken for
granting authority. See :data:`~selfloop.contracts.Decision`.

**Purity.** Evaluating a verdict writes nothing, mints no approval row and
reaches no external system. That is what lets the execution seam re-derive the
verdict at the moment of the call without any of the cost or the side effects of
having done so once already at the gate node — which is the whole mechanism by
which a template that has lost its gate still executes zero unauthorised
effects.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any

from selfloop.context import LoopContext
from selfloop.contracts import (
    APPROVAL_FLOOR_TIER,
    ActionClass,
    GateVerdict,
    LoopTool,
    PolicyDecision,
    PolicyError,
    RiskTier,
    UnknownToolError,
)


def evaluate_tool(ctx: LoopContext, tool: LoopTool, args: Mapping[str, Any]) -> GateVerdict:
    """Classify one prospective effect. Pure with respect to the world.

    The order of the four checks is the specification, not an implementation
    detail. Each one is strictly earlier than anything that could talk it out of
    its answer:

    1. **A tool this instance was not granted is denied.** Absence is denial.
       The check is against the instance's registry rather than against a global
       one, so a template that outgrew its grant fails closed instead of
       reaching whatever happens to be importable.
    2. **A tool in :attr:`~selfloop.context.LoopContext.denied_tools` is
       denied**, and it is checked *before* the policy adapter is consulted, so
       an explicit denial cannot be argued out of by an adapter that classifies
       the action leniently.
    3. **An adapter that cannot classify denies.** A
       :class:`~selfloop.contracts.PolicyError` becomes ``deny``, never "assume
       read-only" — that reasoning makes an unrecognised action class the
       cheapest possible way out of the gate, and the way you find out is that
       something with a typo in its action class ran unattended. An adapter that
       raises anything *else* also denies: a port that fails in a way its own
       contract does not describe has told us nothing, and nothing is not
       permission.
    4. **Then the stricter of the two authorities**, per the module docstring.

    *args* is accepted and deliberately not read. A verdict is about the ACT —
    which tool, at which tier, in which class — and not about the payload. The
    payload is bound separately and at a different layer: it is digested into
    the approval row (see :func:`selfloop.tools.effect_binding`), where changing
    one argument after a human decided must invalidate that human's decision
    rather than silently re-classify the act. Keeping the parameter means a
    caller-supplied replacement for this function *can* look at arguments
    without a signature change, and means the gate node and the seam call the
    identical shape.
    """
    granted = ctx.tools.names()
    action_class = tool.resolved_action_class()

    if tool.name not in granted:
        return GateVerdict(
            "deny",
            tool.tier,
            action_class,
            f"tool {tool.name!r} is not granted to loop instance {ctx.instance_id!r} "
            f"(granted: {sorted(granted)})",
        )

    if tool.name in ctx.denied_tools:
        return GateVerdict(
            "deny",
            tool.tier,
            action_class,
            f"tool {tool.name!r} is explicitly denied for loop instance {ctx.instance_id!r}",
        )

    try:
        decision = ctx.policy.evaluate(action_class)
    except PolicyError as exc:
        return GateVerdict("deny", tool.tier, action_class, f"policy refused: {exc}")
    except Exception as exc:  # noqa: BLE001 - an undescribed failure is not permission
        return GateVerdict(
            "deny",
            tool.tier,
            action_class,
            f"policy adapter raised {type(exc).__name__}: {exc} — "
            "an adapter that fails outside its own contract has classified nothing",
        )

    if decision.requires_approval or tool.tier >= APPROVAL_FLOOR_TIER:
        why = (
            f"policy: {decision.reason}"
            if decision.requires_approval
            else f"loop floor: tier {tool.tier.name} >= {APPROVAL_FLOOR_TIER.name}"
        )
        return GateVerdict("park", tool.tier, action_class, why)

    return GateVerdict("allow", tool.tier, action_class, decision.reason)


def preview(ctx: LoopContext, tool_name: str, args: Mapping[str, Any]) -> GateVerdict:
    """The verdict for a tool *name*, for a CLI or a dry run. Writes nothing.

    An ungranted name resolves to ``deny`` at ``T3`` / ``IRREVERSIBLE`` rather
    than at some neutral tier. The reason is that a preview of a name nobody
    granted has no idea what the act would have been, and a preview that
    *under*-reports risk is worse than useless — an operator reads "T0,
    read-only, denied", fixes the grant, and now believes they have enabled
    something harmless. Naming the worst class the package knows keeps the
    denial legible as "this could be anything".
    """
    try:
        tool = ctx.tools.get(tool_name)
    except UnknownToolError as exc:
        return GateVerdict("deny", RiskTier.T3, ActionClass.IRREVERSIBLE, str(exc))
    return evaluate_tool(ctx, tool, args)


#: The default classification table: what each action class costs before the
#: tier floor is applied. Frozen, because a mutable module-level policy table is
#: a global that any imported module can widen, and the widening is invisible at
#: the call site that is supposed to be making the decision.
#:
#: Note how little work this table is doing at T2+. ``CONSEQUENTIAL`` and
#: ``IRREVERSIBLE`` both require approval here, but even if they did not, the
#: floor in :func:`evaluate_tool` would park them anyway. That redundancy is the
#: point: this table is a *default*, and defaults get replaced.
TIER_POLICY_TABLE: Mapping[ActionClass, PolicyDecision] = MappingProxyType(
    {
        ActionClass.READ_ONLY: PolicyDecision(
            requires_approval=False,
            reason="read-only: no state leaves this process",
            action_class=ActionClass.READ_ONLY,
        ),
        ActionClass.INTERNAL_REVERSIBLE: PolicyDecision(
            requires_approval=False,
            reason="internal and reversible: a local write this loop can undo",
            action_class=ActionClass.INTERNAL_REVERSIBLE,
        ),
        ActionClass.CONSEQUENTIAL: PolicyDecision(
            requires_approval=True,
            reason="consequential: the effect is visible outside this process",
            action_class=ActionClass.CONSEQUENTIAL,
        ),
        ActionClass.IRREVERSIBLE: PolicyDecision(
            requires_approval=True,
            reason="irreversible: nothing this loop can run afterwards undoes it",
            action_class=ActionClass.IRREVERSIBLE,
        ),
        ActionClass.ALWAYS_HUMAN: PolicyDecision(
            requires_approval=True,
            reason="always_human: the act is declared undelegatable by the tool itself",
            action_class=ActionClass.ALWAYS_HUMAN,
        ),
    }
)


@dataclass(frozen=True)
class TierPolicy:
    """The shipped default :class:`~selfloop.ports.PolicyPort`. Twenty-five lines.

    It exists so that a caller who has no policy layer of their own still gets a
    real one rather than reaching for the nearest permissive stub. The whole
    behaviour is a lookup in :data:`TIER_POLICY_TABLE` plus a refusal.

    **What a replacement of this can and cannot do.** An operator's own adapter
    can make ``READ_ONLY`` or ``INTERNAL_REVERSIBLE`` require approval — a shop
    that wants a human on every local write is free to say so. It can never make
    ``CONSEQUENTIAL`` or ``IRREVERSIBLE`` auto-execute, because
    :func:`evaluate_tool` applies the tier floor *after* reading this port's
    answer and there is no return value that reaches past it. That asymmetry is
    deliberate: the direction an integrator might get wrong under pressure is
    the permissive one, and this is the direction that is closed structurally.

    The refusal is the other half. An action class with no row raises
    :class:`~selfloop.contracts.PolicyError`, which :func:`evaluate_tool` turns
    into a denial. A table lookup that "helpfully" fell back to ``READ_ONLY``
    would make a misspelled action class the cheapest route past the gate in the
    entire system.
    """

    #: Hours a human's decision remains authority. Eight, because an approval is
    #: a statement about a moment and not a standing permission — see
    #: :func:`selfloop.approvals.read_outcome`, where expiry binds even over a
    #: row that reads ``approved``.
    approval_expiry_hours: int = 8
    #: Overrides merged over :data:`TIER_POLICY_TABLE`. Supplying one for an
    #: action class the default already covers is how you make T0/T1 stricter.
    table: Mapping[ActionClass, PolicyDecision] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.approval_expiry_hours < 1:
            raise ValueError(
                f"approval_expiry_hours must be at least 1 (got {self.approval_expiry_hours}); "
                "a zero-hour TTL expires every approval before a human can read the page"
            )
        merged = dict(TIER_POLICY_TABLE)
        merged.update(dict(self.table))
        object.__setattr__(self, "table", MappingProxyType(merged))

    def evaluate(self, action_class: ActionClass) -> PolicyDecision:
        """Classify one action class, or refuse. Never guesses."""
        decision = self.table.get(action_class)
        if decision is None:
            raise PolicyError(
                f"no policy row for action class {action_class!r}; refusing to guess. "
                f"Known classes: {sorted(cls.value for cls in self.table)}"
            )
        return decision


__all__ = [
    "TIER_POLICY_TABLE",
    "TierPolicy",
    "evaluate_tool",
    "preview",
]
