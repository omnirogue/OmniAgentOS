"""``LoopContext`` — everything one loop instance is allowed to reach.

This is the object that makes ``selfloop`` a library rather than a framework.
You do not subclass anything, register anything globally, or put a file in a
magic directory: you build one of these, name a template, and call ``run_once``.
Every host-specific thing the runtime can touch is a field here, which means the
answer to "what does this loop have access to?" is one ``repr`` away and the
answer to "what does it need from me?" is this class's signature.

Two properties of the shape are load-bearing rather than stylistic:

* **Every port is a required field.** There is no default in-memory store that
  quietly appears when you forget one. A context missing its gate is a loop that
  cannot corroborate anything, and that must be a decision somebody typed
  (``gate=None``) rather than a default somebody inherited.
* **It is frozen.** A node cannot swap ``ctx.policy`` mid-tick, and a template
  cannot lower its own instance's tier map. Derive a variant with
  ``dataclasses.replace(ctx, ...)``. The one deliberately mutable member is
  ``signal_sources``, because appending a source is the documented way to extend
  the learning loop.

This module imports no adapter. An earlier revision had a ``for_testing()``
helper here that built a context from the in-memory adapters, which made the
foundation depend on a layer above it and produced a circular import the first
time somebody imported an adapter from a contract. Test fixtures build contexts;
contexts do not build themselves.
"""

from __future__ import annotations

from collections.abc import Mapping, MutableSequence
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any

from selfloop.contracts import APPROVAL_FLOOR_TIER, RiskTier, ToolRegistry
from selfloop.ports import (
    ApprovalStore,
    CheckpointStore,
    Clock,
    EventLog,
    GateRunner,
    LeasePort,
    ModelPort,
    Notifier,
    PolicyPort,
    ReceiptStore,
    RecordStore,
    SignalSource,
)


@dataclass(frozen=True, kw_only=True)
class LoopContext:
    """One loop instance's tools, ports and knobs. Keyword-only by construction.

    Keyword-only because a positional constructor with two dozen parameters is a
    trap: two ports of the same structural type transpose silently, and a
    transposed ``receipts``/``records`` pair produces a loop that appears to work
    until the first crash-resume.
    """

    # --- identity -----------------------------------------------------------
    instance_id: str
    template: str

    # --- authority ----------------------------------------------------------
    #: The tools this instance is GRANTED. Absence is denial: a template can ask
    #: for anything it likes and reach only what is in here.
    tools: ToolRegistry

    # --- ports (all required; pass an explicit null implementation, not None,
    #     unless the field's type says None is meaningful) --------------------
    clock: Clock
    receipts: ReceiptStore
    approvals: ApprovalStore
    records: RecordStore
    events: EventLog
    checkpoints: CheckpointStore
    lease: LeasePort
    policy: PolicyPort
    #: Optional in behaviour, required in the constructor. Both shipped templates
    #: and the quickstart complete with a null model that raises if called, so
    #: passing one is a statement that this loop does not think, not an oversight.
    model: ModelPort | None
    #: The independent verifier. ``None`` is legal and is the only honest way to
    #: say "this loop has no corroboration": every tick then settles as
    #: neutral/uncorroborated, which is visible, rather than as accepted, which
    #: is not. It also means the learning loop has no non-neutral evidence and
    #: will never promote, so a loop that is meant to learn needs a real gate —
    #: ``selfloop.gates.ArtifactGate`` is the trivial-but-real default.
    gate: GateRunner | None
    notifier: Notifier

    # --- denial -------------------------------------------------------------
    #: Tools that are granted but refused for this instance. Checked before the
    #: policy adapter is consulted, so a denial cannot be argued out of by a
    #: policy that classifies the action leniently.
    denied_tools: frozenset[str] = frozenset()

    # --- per-instance configuration ----------------------------------------
    params: Mapping[str, Any] = field(default_factory=dict)

    # --- learning knobs -----------------------------------------------------
    #: Functions of shape ``(ctx, *, since_cursor) -> Iterable[LearningSignal]``.
    #: The one deliberately mutable member: appending to this list is the whole
    #: procedure for teaching the loop to notice a new kind of failure.
    signal_sources: MutableSequence[SignalSource] = field(default_factory=list)
    #: Distinct RUNS that must contribute evidence before a candidate may be
    #: admitted to promotion. Two, not three, and the number is arithmetic rather
    #: than taste: the minimum ticks to a first promotion is ``min_support + 1``
    #: (tick 1 and tick 2 produce evidence, tick 3 promotes and injects), so a
    #: liveness test that claims to prove the cycle in three ticks is only honest
    #: at 2. At 3 the advertised test can only pass by secretly lowering this,
    #: which greens a liveness test while production starves.
    min_support: int = 2
    #: Minimum share of a candidate's evidence that must agree on ONE failure
    #: tag. Clustering already partitions by ``(scope, failure_tag)``, so under
    #: the shipped clusterer this is satisfied by construction — it is a knob for
    #: a caller who supplies a looser clusterer of their own.
    min_evidence_consistency: float = 1.0
    #: Wilson lower-bound floor. Used ONLY for recall ranking and for regression
    #: retirement. It is NOT an admission test for promotion and must never
    #: become one: the counters it reads are written by attribution, which only
    #: runs after a lesson has been injected, so at first promotion the bound is
    #: 0.0 and any positive threshold is unsatisfiable. That exact wiring left
    #: the source system with 207 staged candidates and zero promotions.
    promote_threshold: float = 0.40
    #: Decay weight below which an unused promoted lesson retires. With the
    #: default 7/14-day decay curve, 0.2 retires a lesson that has gone unused
    #: for about 12.6 days.
    retire_floor: float = 0.2
    #: How many lessons one recall may inject. Bounded because the injected block
    #: competes for the same context the task needs, and an unbounded memory
    #: block is how a "memory" feature becomes a quality regression.
    recall_k: int = 3
    #: Risk tier of each learning SCOPE, which is what decides whether a lesson
    #: for that scope auto-promotes or parks for a human.
    scope_tiers: Mapping[str, RiskTier] = field(default_factory=dict)
    #: Tier for a scope that is not in :attr:`scope_tiers`. Defaults to the
    #: approval floor, so an undeclared scope PARKS. Fail closed: the failure
    #: mode of guessing low is a machine promoting rules about a part of the
    #: system nobody classified, and the failure mode of guessing high is an
    #: email.
    default_scope_tier: RiskTier = APPROVAL_FLOOR_TIER
    #: ``failure_tag -> remedy sentence``. The source of every promoted lesson's
    #: guidance text, and the reason guidance generation is pure and model-free:
    #: a lesson's guidance is injected into the next run's prompt, so letting a
    #: model author it reintroduces exactly the agent-prose promotion this design
    #: refuses. A tag with no remedy here yields no template-derived guidance,
    #: and a lesson without template-derived guidance is forced to T2 — human
    #: text, human approval.
    remedy_table: Mapping[str, str] = field(default_factory=dict)

    # --- operator ergonomics -----------------------------------------------
    #: Base URL for approval deep links, e.g. ``https://ops.example.com``. Empty
    #: by default: a link to a dashboard that does not exist is worse than no
    #: link, because an operator will click it before they read the id.
    dashboard_origin: str = ""
    #: What to tell a human when an effect's state is unknowable. Injected rather
    #: than hardcoded, because the predecessor's text named a module in its own
    #: host repo, which is meaningless advice to anyone who installed the library.
    reconcile_hint: str = (
        "establish what actually happened at the external system, then record it with "
        "`selfloop reconcile <key> --outcome succeeded|failed --by <human>`"
    )
    #: Hard ceiling on nodes executed in ONE tick. A cyclic template that never
    #: converges must terminate with a legible ``RecursionExceeded`` naming the
    #: path it took, not with an opaque limit error from somewhere inside a graph
    #: library.
    max_steps: int = 50

    def __post_init__(self) -> None:
        if not self.instance_id:
            raise ValueError(
                "a LoopContext needs an instance_id; it names the lease and the thread"
            )
        if not self.template:
            raise ValueError("a LoopContext needs a template name; it is half of the thread id")
        if self.min_support < 1:
            raise ValueError(
                f"min_support must be at least 1 (got {self.min_support}); "
                "zero support means promoting on no evidence at all"
            )
        if not 0.0 <= self.promote_threshold <= 1.0:
            raise ValueError(f"promote_threshold must be in [0, 1] (got {self.promote_threshold})")
        if not 0.0 <= self.min_evidence_consistency <= 1.0:
            raise ValueError(
                f"min_evidence_consistency must be in [0, 1] (got {self.min_evidence_consistency})"
            )
        if not 0.0 <= self.retire_floor <= 1.0:
            raise ValueError(f"retire_floor must be in [0, 1] (got {self.retire_floor})")
        if self.recall_k < 0:
            raise ValueError(f"recall_k must not be negative (got {self.recall_k})")
        if self.max_steps < 1:
            raise ValueError(f"max_steps must be at least 1 (got {self.max_steps})")
        # Read-only views, so a node that is handed the context cannot edit the
        # instance's configuration on its way past. Frozen fields stop rebinding;
        # these stop mutation of what the fields point at.
        object.__setattr__(self, "params", MappingProxyType(dict(self.params)))
        object.__setattr__(self, "denied_tools", frozenset(self.denied_tools))
        object.__setattr__(self, "scope_tiers", MappingProxyType(dict(self.scope_tiers)))
        object.__setattr__(self, "remedy_table", MappingProxyType(dict(self.remedy_table)))

    @property
    def actor(self) -> str:
        """This loop's identity when it writes to an audit trail: ``loop:<instance>``.

        Prefixed on purpose. Approval reads refuse a decision made by an
        automation identity, and because the loop's own actor can only ever be
        this string, self-approval is structurally impossible rather than
        merely forbidden — there is no value the loop could put in the ``by``
        field that would satisfy its own check.
        """
        return f"loop:{self.instance_id}"

    @property
    def thread_id(self) -> str:
        """Checkpoint key: ``<template>:<instance>``.

        Both halves are needed. Keyed on the instance alone, two templates
        driving the same instance resume each other's half-finished state, and
        because node names differ between templates the result is a silent
        no-op rather than a crash.
        """
        return f"{self.template}:{self.instance_id}"

    def scope_tier(self, scope: str) -> RiskTier:
        """Risk tier governing lessons for *scope*. Undeclared scopes park."""
        return self.scope_tiers.get(scope, self.default_scope_tier)

    def remedy_for(self, failure_tag: str) -> str:
        """Caller-supplied remedy for *failure_tag*, or ``""`` if there is none.

        An empty answer is not a defect to be papered over with a generic
        sentence. It means nobody has written down what to do about this failure,
        and the honest consequence is that any lesson built on it needs a human.
        """
        return self.remedy_table.get(failure_tag, "")


__all__ = ["LoopContext"]
