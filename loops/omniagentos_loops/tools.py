"""LoopTool: the one execution seam every loop effect must pass through.

P4's boundary result, restated: *LangGraph does not bypass the existing risk
controls — but only if every tool goes through one owned execution seam.* This
module is that seam. A node never calls a connector directly; it calls
:func:`execute_effect`, which

1. re-derives the policy verdict from :mod:`omniagentos.policy` (it does NOT
   trust the graph's own gate token),
2. for a T2+ effect, re-reads the ``approvals`` row and refuses unless a real
   human approved it and it has not expired, and
3. wraps the underlying callable in an idempotency receipt (P7 guarded variant)
   so a crash-resume can never duplicate an external side effect.

Steps 1 and 2 are duplicated with the ``policy_gate`` node deliberately. The
gate exists so a loop parks *early* and legibly; the seam exists so a template
that loses its gate — by refactor, by bug, or by counterfeit mutation — still
executes zero unauthorized effects.
"""

from __future__ import annotations

import hashlib
import json
import threading
from collections.abc import Callable, Mapping
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any

from omniagentos.contracts import ActionClass
from omniagentos_loops.contracts import (
    APPROVAL_FLOOR_TIER,
    TIER_ACTION_CLASS,
    EffectDenied,
    EffectNotApproved,
    LoopError,
    RiskTier,
    UnknownToolError,
)

#: A tool's idempotency-key function maps its *arguments* to a business key —
#: the stable identity of the effect ("message 42 answered", "api restarted at
#: 12:30"), never a random id. Two ticks that mean the same effect MUST produce
#: the same key or the receipt cannot protect anything.
IdempotencyKeyFn = Callable[[Mapping[str, Any]], str]


@dataclass(frozen=True)
class Verification:
    """The verdict of an effect's verification predicate. ``ok`` is the gate.

    ``detail`` is what an operator reads when the effect is filed as failed, so
    it should name the evidence ("launchctl print gui/501/com.omni.api: not
    running"), never restate the intent.
    """

    ok: bool
    detail: str = ""

    def __bool__(self) -> bool:
        return self.ok


#: An effect's verification predicate: ``(result, args) -> ok``.
#:
#: This is the "close the effect against external evidence" seam. It receives
#: what the tool RETURNED and the arguments the tool was CALLED WITH, and it is
#: expected to look somewhere the tool does not control:
#:
#: * ``launchctl kickstart``  -> ``launchctl print gui/<uid>/<label>`` says the
#:   service is running (NOT "the subprocess exited 0");
#: * a render/export        -> ``Path(args["out"]).stat().st_size > 0``;
#: * a row write            -> ``store.get(args["id"]) is not None``;
#: * an HTTP call           -> ``200 <= result["status"] < 300 and
#:   result["body"]["id"] == args["id"]``.
#:
#: It may return ``bool``, a :class:`Verification`, or a mapping carrying an
#: ``ok``/``verified``/``success`` key. It runs ONCE per executed attempt, after
#: the tool returns and outside the execution seam, and never on a replay — so
#: its cost is one probe per real effect, not one per tick.
#:
#: A predicate that RAISES is not a failure verdict: the effect ran and its
#: outcome could not be established, which is ``EffectStateUnknown`` — fail
#: closed, exactly as a crash between claim and completion does.
VerifyFn = Callable[[Any, Mapping[str, Any]], "Verification | bool | Mapping[str, Any]"]

#: Attempt budget for one business key when a tool does not set its own.
#: Below the approval floor (T0/T1: local, reversible, cheap to repeat) three
#: attempts; at T2+ (externally visible, human-approved) exactly ONE, because a
#: tool that reports failure on an outbound send has not necessarily failed to
#: send it. Raising it is an explicit, per-tool decision.
DEFAULT_MAX_ATTEMPTS = 3
APPROVAL_FLOOR_MAX_ATTEMPTS = 1
#: Hard ceiling on any tool's declared budget. "Retry until it works" is how an
#: automated loop turns one broken dependency into an outage of its own.
MAX_ATTEMPTS_CEILING = 10


def coerce_verification(value: Any) -> Verification:
    """Normalise whatever a :data:`VerifyFn` returned into a Verification.

    Fail-CLOSED on shapes that carry no verdict (``None``, an empty mapping):
    a predicate that answers nothing has not verified anything.
    """
    if isinstance(value, Verification):
        return value
    if isinstance(value, bool):
        return Verification(ok=value)
    if isinstance(value, Mapping):
        for flag in ("ok", "verified", "success"):
            if flag in value:
                return Verification(
                    ok=bool(value[flag]),
                    detail=str(value.get("detail") or value.get("error") or ""),
                )
        return Verification(ok=False, detail=f"verification returned no verdict: {value!r}")
    if value is None:
        return Verification(ok=False, detail="verification returned None")
    return Verification(ok=bool(value))


def digest_key(*parts: Any) -> str:
    """Stable short digest for composing business keys."""
    raw = json.dumps(parts, sort_keys=True, default=str, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]


def args_digest(args: Mapping[str, Any]) -> str:
    """Digest of the EXACT arguments an effect will be invoked with.

    Stored on the approvals row and re-checked at execution time, so an approval
    authorises the payload a human actually saw and nothing else.
    """
    return digest_key(dict(args))


#: The seam ticket. Present only while :func:`execute_effect` is invoking a tool.
#:
#: It carries two things, and BOTH are load-bearing:
#:
#: * the tool NAME, so one tool's open seam cannot run a different tool's
#:   callable (the piggybacking case); and
#: * an opaque NONCE, because a ContextVar is *copied* — not shared — into an
#:   ``asyncio`` task. A task created inside the seam inherits a snapshot that
#:   still says "the seam is open for me", so a name-only check let leftover
#:   async work execute an effect long after ``execute_effect`` had returned,
#:   with no policy verdict, no approval and no receipt. The nonce is revoked in
#:   the seam's ``finally``: the copy survives, its authority does not.
_IN_SEAM: ContextVar[tuple[str, object] | None] = ContextVar(
    "omniagentos_loops_in_seam", default=None
)

#: Nonces of the seams that are executing RIGHT NOW, in this process. A context
#: snapshot that outlives its seam still holds its ticket, but the ticket is no
#: longer in here, which is what makes the snapshot worthless.
_OPEN_SEAMS: set[object] = set()
_SEAM_LOCK = threading.Lock()


class SeamBypass(LoopError):
    """A tool callable was invoked outside :func:`execute_effect`."""


def _seam_is_open_for(tool_name: str) -> bool:
    """True only inside the live seam that was opened for *tool_name*."""
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

    *fn* is captured in this closure's cell and is stored nowhere else. There is
    deliberately no attribute, no ``__dict__`` entry, no dataclass field, no
    property and no ``__wrapped__`` that yields it — the previous revision
    exposed all three of ``_GuardedCall._fn``, ``_GuardedCall.unwrapped`` and a
    public ``invoke()``, and a reviewer executed a T3 effect through each of
    them. ``functools.wraps`` is not used here for exactly that reason: it would
    re-add ``__wrapped__``.

    What this does NOT close, stated plainly rather than implied:

    * ``guarded.__closure__[i].cell_contents`` still reaches *fn*, as does
      ``gc.get_referrers``/``ctypes``. Python has no in-process memory boundary,
      so no wrapper can. Code that walks closure cells is hostile code already
      running in the worker; the defences for that are the process/venv boundary
      and review of ``instances/``, not this function.
    * a module that keeps its OWN name for *fn* can still call it directly —
      wrapping never took away a reference the module already had. That route is
      closed mechanically instead, by
      ``tests/test_security_seam.py::test_no_module_invokes_a_callable_it_registers_as_a_tool``.
    """

    def guarded(*args: Any, **kwargs: Any) -> Any:
        if not _seam_is_open_for(tool_name):
            raise SeamBypass(
                f"tool {tool_name!r} was called outside execute_effect "
                f"(seam ticket {_IN_SEAM.get()!r}) — every tool call must derive policy first"
            )
        return fn(*args, **kwargs)

    guarded.__name__ = f"guarded__{tool_name}"
    guarded.__qualname__ = f"guarded__{tool_name}"
    guarded.__doc__ = f"Sealed handle for tool {tool_name!r}: runs only inside execute_effect."
    #: Marker so re-constructing a LoopTool from an already-sealed callable does
    #: not double-wrap. It names the tool; it is not a route to the callable.
    guarded.__loop_seam__ = tool_name  # type: ignore[attr-defined]
    return guarded


@dataclass(frozen=True)
class LoopTool:
    """name + risk tier + idempotency-key fn + underlying callable.

    ``call`` is the *existing* implementation — a broker library-path call, a
    ``steward.notify`` sender, or plain stdlib. The loop runtime never
    reimplements a connector and never handles a credential itself.

    The callable supplied to the constructor is SEALED (see :func:`_sealed`)
    before it is stored, so ``tool.call`` is a guard, never the implementation:
    it raises :class:`SeamBypass` anywhere except inside :func:`execute_effect`.
    That makes the "one execution seam" property enforced rather than merely
    reviewed — including during ``register(ctx)``, where an instance module must
    only register. ``call`` remains the constructor's public parameter name, so
    instance modules are unchanged.
    """

    name: str
    tier: RiskTier
    idempotency_key: IdempotencyKeyFn
    call: Callable[..., Any]
    action_class: ActionClass | None = None
    description: str = ""
    #: Opt-in only. True means "re-running this effect is harmless", which lets
    #: the receipt guard retry an UNKNOWN-state effect instead of failing
    #: closed. Never set it on an outbound send, a payment, or a merge.
    replay_on_unknown: bool = False
    #: External evidence that the effect took effect (see :data:`VerifyFn`).
    #: When declared it is checked BEFORE the receipt may be marked succeeded,
    #: and it can only make success harder: the tool's own report and this
    #: predicate must BOTH be non-adverse. A weak verifier therefore cannot
    #: launder a self-declared failure into a completed receipt.
    verify: VerifyFn | None = None
    #: Attempts allowed per business key before the loop escalates instead of
    #: retrying. ``None`` takes the tier default (:data:`DEFAULT_MAX_ATTEMPTS`,
    #: or :data:`APPROVAL_FLOOR_MAX_ATTEMPTS` at T2+).
    max_attempts: int | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.tier, RiskTier):
            raise ValueError(f"tool {self.name!r}: tier must be a RiskTier")
        if self.max_attempts is not None and not (
            1 <= int(self.max_attempts) <= MAX_ATTEMPTS_CEILING
        ):
            raise ValueError(
                f"tool {self.name!r}: max_attempts must be between 1 and "
                f"{MAX_ATTEMPTS_CEILING} (got {self.max_attempts!r}) — an unbounded "
                "retry is an outage generator, not a repair"
            )
        if self.verify is not None and not callable(self.verify):
            raise ValueError(f"tool {self.name!r}: verify must be callable")
        if self.replay_on_unknown and self.tier >= APPROVAL_FLOOR_TIER:
            raise ValueError(
                f"tool {self.name!r}: replay_on_unknown is forbidden at tier "
                f"{self.tier.name} — a T2+ effect must never be blind-retried"
            )
        # Seal unless it is ALREADY this tool's seal. Handing tool B the sealed
        # callable of tool A therefore double-seals: B's seam opens, A's inner
        # guard refuses. Fail-closed and legible, rather than silently letting B
        # execute A's implementation.
        if getattr(self.call, "__loop_seam__", None) != self.name:
            object.__setattr__(self, "call", _sealed(self.call, self.name))

    def resolved_action_class(self) -> ActionClass:
        return self.action_class or TIER_ACTION_CLASS[self.tier]

    def resolved_max_attempts(self) -> int:
        """How many times ONE business key may be attempted, ever."""
        if self.max_attempts is not None:
            return int(self.max_attempts)
        if self.tier >= APPROVAL_FLOOR_TIER:
            return APPROVAL_FLOOR_MAX_ATTEMPTS
        return DEFAULT_MAX_ATTEMPTS


@dataclass
class ToolRegistry:
    """The tools ONE loop instance is granted. Absence is denial."""

    tools: dict[str, LoopTool] = field(default_factory=dict)

    def register(self, tool: LoopTool) -> LoopTool:
        if tool.name in self.tools:
            raise ValueError(f"tool {tool.name!r} already registered")
        self.tools[tool.name] = tool
        return tool

    def get(self, name: str) -> LoopTool:
        """Return the granted tool, or fail closed.

        A node asking for an ungranted tool is the P4 "denied tool" case: the
        underlying callable is never reached, so its execution count stays 0.
        """
        tool = self.tools.get(name)
        if tool is None:
            raise UnknownToolError(
                f"tool {name!r} is not granted to this loop instance "
                f"(granted: {sorted(self.tools)})"
            )
        return tool

    def names(self) -> frozenset[str]:
        return frozenset(self.tools)


def _invoke_in_seam(tool: LoopTool, args: Mapping[str, Any]) -> Any:
    """Call *tool* with the seam open. The ONLY place a tool is ever invoked.

    Module-private on purpose. As a public ``invoke(tool, args)`` this function
    WAS the bypass: anything holding a ``LoopTool`` could run a T3 effect
    through it with no policy verdict, no approval and no receipt — the seam's
    own front door standing open next to the locked one.
    """
    nonce = object()
    with _SEAM_LOCK:
        _OPEN_SEAMS.add(nonce)
    token = _IN_SEAM.set((tool.name, nonce))
    try:
        return tool.call(**dict(args))
    finally:
        _IN_SEAM.reset(token)
        # Revoking the nonce is what expires every CONTEXT COPY of this ticket —
        # an asyncio task spawned inside the seam, a snapshot taken with
        # contextvars.copy_context(), anything that outlives this frame.
        with _SEAM_LOCK:
            _OPEN_SEAMS.discard(nonce)


def effect_binding(
    ctx: Any, *, node: str, tool: LoopTool, args: Mapping[str, Any]
) -> dict[str, Any]:
    """The identity an approval is bound to.

    Scoped by template AND instance AND node AND tool: two loop instances that
    happen to share a name (or one instance running two templates) can no longer
    authorise each other's effects — the confused-deputy case.
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
    ctx: Any,
    *,
    node: str,
    tool: LoopTool,
    args: Mapping[str, Any],
    business_key: str,
    gate_token: Mapping[str, Any] | None,
    mode: str = "effect",
) -> dict[str, Any]:
    """Run one tool call under policy (+ approval + receipt guards for effects).

    ``mode="read"`` is for a T0 read: policy is still derived and the registry
    is still consulted, but no approval and no receipt apply — a poll MUST
    re-read every tick, and a receipt would make the second tick a no-op. It is
    routed through here anyway so that *every* tool invocation in the system has
    exactly one code path (blocker: ``add_read`` used to call ``tool.call``
    directly, which was a policy-free door into the tool plane).

    Returns ``{"result", "replayed", "succeeded", "detail", "receipt",
    "attempt", "verified"}``. Raises
    :class:`EffectDenied` / :class:`EffectNotApproved` / ``EffectStateUnknown``
    *before* the callable is reached, never after. An effect that RAN and did
    not take effect is not an exception: it returns with ``succeeded=False`` and
    a ``failed`` receipt, so the template's verify node — not this seam — decides
    what the tick's status is (see :mod:`omniagentos_loops.receipts`).
    """
    # Imported here (not at module import time) so contracts/tools stay usable
    # without the approvals + receipts stack in narrow unit tests.
    from omniagentos_loops import approvals, receipts
    from omniagentos_loops.policy_gate import evaluate_tool

    if mode not in ("effect", "read"):
        raise ValueError(f"unknown execute_effect mode {mode!r}")

    verdict = evaluate_tool(ctx, tool, args)
    if verdict.decision == "deny":
        raise EffectDenied(f"{node}/{tool.name}: {verdict.reason}")

    if mode == "read":
        if tool.tier > RiskTier.T0 or verdict.decision != "allow":
            raise EffectDenied(
                f"{node}/{tool.name}: read mode requires a T0 auto-allowed tool, "
                f"got tier {tool.tier.name} / {verdict.decision}"
            )
        return {"result": _invoke_in_seam(tool, args), "replayed": False, "receipt": None}

    binding = effect_binding(ctx, node=node, tool=tool, args=args)
    if verdict.decision == "approve":
        token = dict(gate_token or {})
        expected = approvals.approval_id(
            ctx.instance_id, ctx.template, node, tool.name, business_key
        )
        if token.get("approval_id") != expected:
            raise EffectNotApproved(
                f"{node}/{tool.name}: no gate token for approval {expected} "
                f"(tier {tool.tier.name}) — refusing to execute"
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

    receipt = receipts.guarded(ctx, key=key, node=node, tool=tool, execute=_run, args=args)
    return {
        "result": receipt.result,
        "replayed": receipt.replayed,
        # FALSE means the effect ran and did not take effect. A template that
        # renders COMPLETED without reading this is reporting a success it has
        # evidence against.
        "succeeded": receipt.succeeded,
        "detail": receipt.detail,
        # The ATTEMPT key, not the base key: the audit trail names a row that
        # exists, and attempt 2 of an incident is legible as such.
        "receipt": receipt.key,
        "attempt": receipt.attempt,
        "verified": receipt.verified,
    }


#: ``invoke`` is deliberately absent: the seam's invoker is ``_invoke_in_seam``
#: and it is module-private. Exporting it would restore the exact bypass this
#: module exists to close.
__all__ = [
    "APPROVAL_FLOOR_MAX_ATTEMPTS",
    "DEFAULT_MAX_ATTEMPTS",
    "MAX_ATTEMPTS_CEILING",
    "IdempotencyKeyFn",
    "LoopTool",
    "SeamBypass",
    "ToolRegistry",
    "Verification",
    "VerifyFn",
    "args_digest",
    "coerce_verification",
    "digest_key",
    "effect_binding",
    "execute_effect",
]
