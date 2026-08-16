"""THE execution seam: the one place in this package a tool is ever invoked.

A node never calls a connector directly. It calls :func:`execute_effect`, which

1. **re-derives the policy verdict** from :mod:`selfloop.policy` — it does not
   trust the gate token the graph left in the state;
2. for a parked (T2+) effect, **re-reads the approval row itself** and refuses
   unless a human approved *this exact action* and the decision has not expired;
   and
3. wraps the underlying callable in an **idempotency receipt**, so a
   crash-resume can never duplicate an external side effect.

Steps 1 and 2 duplicate the gate node deliberately. The gate exists so a loop
parks *early* and legibly; the seam exists so a template that has lost its gate —
by refactor, by bug, or by a counterfeit mutation — still executes zero
unauthorised effects.

How the seal works
------------------

:meth:`~selfloop.contracts.ToolRegistry.register` runs every tool through the
sealer this module installs at import. The sealer replaces the tool's callable
with a closure whose ONLY handle to the implementation is a cell: no attribute,
no ``__dict__`` entry, no dataclass field, no property, and no ``__wrapped__``.
``functools.wraps`` is deliberately not used, because it would re-add one. The
predecessor of this design exposed three separate routes to the raw callable and
a reviewer executed an irreversible effect through each of them.

The live ticket is ``(tool_name, nonce)`` and both halves are load-bearing. The
NAME stops one tool's open seam from running a different tool's callable. The
NONCE exists because **a ContextVar is copied, not shared, into an asyncio
task**: a task created inside the seam inherits a snapshot that still says "the
seam is open for me", so a name-only check let leftover async work execute an
effect long after :func:`execute_effect` had returned — with no policy verdict,
no approval and no receipt. The nonce is revoked in the seam's ``finally``. The
copy survives; its authority does not.

What this does NOT promise
--------------------------

Stated plainly, because an overclaimed boundary is worse than an honest one.
Within this package, every tool registered through a ``ToolRegistry`` is
invocable only through :func:`_invoke_in_seam`, and that is enforced by a
mechanical review suite over ``selfloop/`` and by the counterfeit corpus. It is
a strong convention with a machine checking it.

It is **not** an in-process memory boundary, and Python offers no wrapper that
would be one. ``guarded.__closure__[0].cell_contents`` still reaches the
implementation, as do ``gc.get_referrers`` and ``ctypes``, and a module that kept
its own name for the callable never lost it — wrapping cannot take away a
reference somebody already has. Code that walks closure cells is hostile code
already running in your worker; the defence against that is a process boundary,
not a decorator. **For untrusted tool code, run effects in a separate process.**
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Mapping
from contextvars import ContextVar
from dataclasses import replace
from typing import Any

from selfloop import approvals, policy, receipts
from selfloop.context import LoopContext
from selfloop.contracts import (
    EffectDenied,
    EffectNotApproved,
    LoopTool,
    RiskTier,
    SeamBypass,
    args_digest,
    install_sealer,
)

#: The seam ticket. Present only while :func:`execute_effect` is invoking a tool.
#: See the module docstring for why it carries a nonce as well as a name.
_IN_SEAM: ContextVar[tuple[str, object] | None] = ContextVar("selfloop_in_seam", default=None)

#: Nonces of the seams executing RIGHT NOW, in this process. A context snapshot
#: that outlives its seam still holds its ticket, but the ticket is no longer in
#: here — which is precisely what makes the snapshot worthless.
_OPEN_SEAMS: set[object] = set()

#: Guards :data:`_OPEN_SEAMS`. The set is read from every thread that invokes a
#: tool and written on entry and exit of every seam, so it is genuinely shared
#: mutable state and not merely nominally so.
_SEAM_LOCK = threading.Lock()

#: The modes :func:`execute_effect` accepts.
EFFECT_MODE = "effect"
READ_MODE = "read"


def _seam_is_open_for(tool_name: str) -> bool:
    """True only inside the LIVE seam that was opened for *tool_name*.

    Two conditions, and dropping either one opens a real hole. The ticket must
    name this tool (otherwise one tool's open seam runs another's callable), and
    the ticket's nonce must still be live (otherwise a context copy that outlived
    its seam is still a valid ticket forever).
    """
    ticket = _IN_SEAM.get()
    if ticket is None:
        return False
    name, nonce = ticket
    if name != tool_name:
        return False
    with _SEAM_LOCK:
        return nonce in _OPEN_SEAMS


def _sealed(fn: Callable[..., Any], tool_name: str) -> Callable[..., Any]:
    """Return the ONLY handle to *fn* this runtime keeps.

    *fn* is captured in this closure's cell and stored nowhere else. There is
    deliberately no attribute, no ``__dict__`` entry, no dataclass field, no
    property and no ``__wrapped__`` through which it can be recovered by name,
    and ``functools.wraps`` is not used for exactly that reason. The three
    metadata assignments below are strings, not references.

    A tool re-sealed under a *different* name fails closed rather than silently
    executing: the outer guard checks the new name, the inner guard checks the
    old one, and the inner check refuses. That is the correct answer to "somebody
    handed tool B the sealed callable of tool A" — legibly nothing happens,
    rather than B quietly running A's implementation.

    See the module docstring for what this does not close.
    """

    def guarded(*args: Any, **kwargs: Any) -> Any:
        if not _seam_is_open_for(tool_name):
            raise SeamBypass(
                f"tool {tool_name!r} was called outside execute_effect "
                f"(seam ticket {_IN_SEAM.get()!r}) — every tool call must derive a "
                "policy verdict first"
            )
        return fn(*args, **kwargs)

    guarded.__name__ = f"guarded__{tool_name}"
    guarded.__qualname__ = f"guarded__{tool_name}"
    guarded.__doc__ = f"Sealed handle for tool {tool_name!r}: runs only inside execute_effect."
    return guarded


def _seal_tool(tool: LoopTool) -> LoopTool:
    """The sealer :meth:`ToolRegistry.register` applies to every granted tool.

    Installed into :mod:`selfloop.contracts` at import (see the call below).
    Splitting it this way is what lets ``contracts`` stay dependency-free while
    the seam still owns the seal: ``contracts`` defines the hook and the tool
    record, this module supplies the behaviour.
    """
    return replace(tool, call=_sealed(tool.call, tool.name))


#: Installing the sealer is the one thing this module does at import, and it is
#: the reason importing it matters: a ``ToolRegistry`` built in a process where
#: ``selfloop.tools`` was never imported registers tools UNSEALED, because
#: ``contracts``' default sealer is the identity function. Anything that builds a
#: registry should therefore reach this module first — which every path through
#: :func:`selfloop.runtime.run_once` does.
install_sealer(_seal_tool)


def _invoke_in_seam(tool: LoopTool, args: Mapping[str, Any]) -> Any:
    """Call *tool* with the seam open. The ONLY place a tool is ever invoked.

    Module-private on purpose. As a public ``invoke(tool, args)`` this function
    WAS the bypass: anything holding a :class:`~selfloop.contracts.LoopTool`
    could run an irreversible effect through it with no policy verdict, no
    approval and no receipt — the seam's own front door standing open next to
    the locked one. It is not exported, and the review suite over ``selfloop/``
    checks that nothing outside this module calls it.
    """
    nonce = object()
    with _SEAM_LOCK:
        _OPEN_SEAMS.add(nonce)
    token = _IN_SEAM.set((tool.name, nonce))
    try:
        return tool.call(**dict(args))
    finally:
        _IN_SEAM.reset(token)
        # Revoking the nonce expires every CONTEXT COPY of this ticket — an
        # asyncio task spawned inside the seam, a snapshot taken with
        # contextvars.copy_context(), anything at all that outlives this frame.
        with _SEAM_LOCK:
            _OPEN_SEAMS.discard(nonce)


def effect_binding(
    ctx: LoopContext, node: str, tool: LoopTool, args: Mapping[str, Any]
) -> dict[str, Any]:
    """The identity an approval is bound to.

    Scoped by template AND instance AND node AND tool, so two loop instances that
    happen to share a name — or one instance running two templates — cannot
    authorise each other's effects. That is the confused-deputy case, and it is
    not hypothetical: an approval row is just a row, and without the scope in the
    binding, whichever loop derives a matching id first gets to spend the
    human's decision.

    ``args_digest`` is what makes the approval about an ACTION rather than a
    slot. It is computed from the real arguments, never from the redacted copy
    shown on the approvals page, so the thing a human agreed to and the thing
    that runs are digested from the same bytes.
    """
    return {
        "template": ctx.template,
        "instance": ctx.instance_id,
        "node": node,
        "tool": tool.name,
        "action_class": tool.resolved_action_class().value,
        "args_digest": args_digest(args),
    }


def execute_effect(
    ctx: LoopContext,
    *,
    node: str,
    tool: LoopTool,
    args: Mapping[str, Any],
    business_key: str,
    gate_token: Mapping[str, Any] | None = None,
    mode: str = EFFECT_MODE,
) -> dict[str, Any]:
    """Run one tool call under policy, plus approval and receipt guards for effects.

    Returns ``{"result", "replayed", "succeeded", "detail", "receipt", "attempt",
    "verified"}``.

    ``succeeded=False`` means **the effect ran and did not take effect**. It is
    not an exception, because deciding what that means for the tick is the
    template's job — its verify node is the thing that knows what "it did not
    work" means for this domain. A caller that renders a tick as completed
    without reading this field is reporting a success it has evidence against.

    Raises :class:`~selfloop.contracts.EffectDenied`,
    :class:`~selfloop.contracts.EffectNotApproved` and
    :class:`~selfloop.contracts.EffectStateUnknown` strictly *before* the
    callable is reached, never after — so an operator reading one of those in a
    ledger knows the external system was not touched.

    ``mode="read"`` is for a T0 read: the registry is still consulted and the
    policy verdict is still derived, but no approval and no receipt apply,
    because a poll MUST re-read every tick and a receipt would make the second
    tick a no-op. **It is routed through here anyway**, and that is the point:
    the predecessor's read helper called the tool directly, which was a
    policy-free door into the tool plane. The read path keeps its own guard —
    tier exactly T0 *and* an ``allow`` verdict — so widening it requires editing
    this function rather than passing a different string.

    A read returns three keys and not seven. Inventing ``succeeded=True`` for an
    unreceipted call would let a caller treat a read as a receipted effect, and
    the absence of the key is a better error message than a lie.
    """
    if mode not in (EFFECT_MODE, READ_MODE):
        raise ValueError(
            f"unknown execute_effect mode {mode!r}; expected {EFFECT_MODE!r} or {READ_MODE!r}"
        )

    verdict = policy.evaluate_tool(ctx, tool, args)
    if verdict.decision == "deny":
        raise EffectDenied(f"{node}/{tool.name}: {verdict.reason}")

    if mode == READ_MODE:
        if tool.tier != RiskTier.T0 or not verdict.allows:
            raise EffectDenied(
                f"{node}/{tool.name}: read mode requires a T0 tool with an 'allow' verdict, "
                f"got tier {tool.tier.name} / {verdict.decision}. A read is exempt from the "
                "receipt, not from the policy."
            )
        return {"result": _invoke_in_seam(tool, args), "replayed": False, "receipt": None}

    binding = effect_binding(ctx, node, tool, args)

    if verdict.parks:
        expected = approvals.approval_id(
            ctx.instance_id,
            ctx.template,
            node,
            tool.name,
            business_key,
            args_digest(args),
        )
        # The gate token is a HINT that a park was minted, never authority. It is
        # checked first only because a missing token means the gate node did not
        # run for this effect, and a template that reaches an effect without
        # having parked must not execute it even if an approval row happens to
        # exist — the loop is supposed to stop and be seen stopping.
        token = dict(gate_token or {})
        if token.get("approval_id") != expected:
            raise EffectNotApproved(
                f"{node}/{tool.name}: no gate token for approval {expected} "
                f"(tier {tool.tier.name}); got {token.get('approval_id')!r} — refusing to execute"
            )
        outcome = approvals.read_outcome(ctx, expected, binding=binding)
        if not outcome.approved:
            raise EffectNotApproved(
                f"{node}/{tool.name}: approval {expected} is {outcome.state} "
                f"({outcome.reason}) — refusing to execute"
            )

    key = receipts.receipt_key(ctx.instance_id, ctx.template, node, tool.name, business_key)

    def _run() -> Any:
        return _invoke_in_seam(tool, args)

    receipt = receipts.guarded(
        ctx,
        key=key,
        node=node,
        tool=tool,
        execute=_run,
        args=args,
        business_key=business_key,
    )
    return {
        "result": receipt.result,
        "replayed": receipt.replayed,
        "succeeded": receipt.succeeded,
        "detail": receipt.detail,
        # The ATTEMPT key, not the base key: the audit trail names a row that
        # exists, and attempt 2 of an incident is legible as such.
        "receipt": receipt.key,
        "attempt": receipt.attempt,
        "verified": receipt.verified,
    }


#: ``invoke`` is deliberately absent. The seam's invoker is ``_invoke_in_seam``
#: and it is module-private; exporting it would restore the exact bypass this
#: module exists to close.
__all__ = [
    "EFFECT_MODE",
    "READ_MODE",
    "effect_binding",
    "execute_effect",
]
