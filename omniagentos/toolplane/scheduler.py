"""The Phase-3 deterministic resource-aware tool scheduler.

Unknown classification means SERIAL
====================================
A tool absent from the catalog, or present but with ``classified=False``, is never
admitted to a concurrent wave. The failure mode of guessing "probably parallel-safe"
is two writers racing on the same resource; the failure mode of guessing "serial" is
that it runs slower. Those are not comparable, so the default is serial and it is
not configurable.

Pure, Clock-Free, I/O-Free and Global State-Free Determinism
============================================================
The batching algorithm in ``plan_execution`` is entirely pure and deterministic.
Given the same sequence of intended tool calls and catalog configuration, it will
produce the exact same execution plan, waves, reasons, and idempotency keys, with no
dependencies on system clocks, random number generators, or database/file I/O.

No Dependency on ``omniagentos.scope.locks``
=============================================
This module does not import or depend on ``omniagentos.scope.locks``. It stays pure
and dependency-light, returning plain strings as lock claims. The integrator or runner
is responsible for wiring these claims to the lock store.

Deadlock-freedom there is STRUCTURAL: ``try_acquire_scope`` is all-or-nothing inside
one ``BEGIN IMMEDIATE`` transaction, so a claimant never holds one lock while waiting
for another. You must not break that. This module NEVER acquires a lock; it emits the
COMPLETE claim set for a wave so the caller can make exactly one all-or-nothing acquisition.
Incremental per-call acquisition is strictly forbidden: it reintroduces hold-and-wait,
which is the one Coffman condition that ``omniagentos.scope.locks`` removes structurally
to achieve deadlock-freedom without a runtime cycle detector.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Literal, Protocol

from omniagentos.toolplane.catalog import CatalogEntry, default_catalog
from omniagentos.toolplane.config import concurrency_budget

__all__ = [
    "CancellationHook",
    "ExecutionPlan",
    "ToolCall",
    "Wave",
    "WaveKind",
    "idempotency_key",
    "plan_execution",
    "propagate_cancellation",
    "resource_keys_for",
    "wave_lock_claims",
]

WaveKind = Literal["concurrent", "serial"]


@dataclass(frozen=True, slots=True)
class ToolCall:
    """One intended call.

    ``index`` is its position in the CALLER'S request order.
    """

    index: int
    tool: str
    resource_keys: tuple[str, ...] = ()  # call-level keys, UNIONED with the catalog's
    args_digest: str = ""  # caller-supplied stable digest of the arguments


@dataclass(frozen=True, slots=True)
class Wave:
    """A single execution wave of tool calls."""

    kind: WaveKind
    calls: tuple[ToolCall, ...]
    resource_keys: tuple[str, ...]  # sorted union — the ALL-OR-NOTHING claim set
    reason: str  # why this wave is shaped this way


@dataclass(frozen=True, slots=True)
class ExecutionPlan:
    """The completed ordered plan of execution waves."""

    waves: tuple[Wave, ...]
    order: tuple[int, ...]  # request-order indices, for result reassembly
    idempotency_keys: Mapping[int, str]  # call index -> key, ONLY for retryable calls
    cancellation_groups: Mapping[str, tuple[int, ...]]

    @property
    def concurrent_waves(self) -> int:
        """The total number of concurrent waves in this plan."""
        return sum(1 for w in self.waves if w.kind == "concurrent")

    @property
    def max_wave_width(self) -> int:
        """The maximum number of calls in any single wave of this plan."""
        return max((len(w.calls) for w in self.waves), default=0)

    def calls_in_cancellation_group(self, group: str) -> tuple[int, ...]:
        """Return all call indices registered under the specified cancellation group."""
        return self.cancellation_groups.get(group, ())


def resource_keys_for(
    call: ToolCall, catalog: Mapping[str, CatalogEntry] | None = None
) -> tuple[str, ...]:
    """Retrieve the sorted resource keys for a given tool call.

    Sorted union of ``call.resource_keys`` and the catalog entry's ``resource_keys``.
    If the tool is UNKNOWN, return ``(f"unknown:{call.tool}",)`` — an unknown tool
    contends with itself, which combined with the serial rule keeps it from ever
    pairing up.

    ``catalog=None`` resolves the process-wide
    :func:`~omniagentos.toolplane.catalog.default_catalog`; only a tool genuinely
    absent from that catalog counts as unclassified.
    """
    resolved = catalog if catalog is not None else default_catalog()
    if call.tool not in resolved:
        return (f"unknown:{call.tool}",)

    entry = resolved[call.tool]
    if not entry.classified:
        return (f"unknown:{call.tool}",)

    union = set(call.resource_keys) | set(entry.resource_keys)
    return tuple(sorted(union))


def idempotency_key(call: ToolCall, entry: CatalogEntry | None, *, run_key: str = "") -> str:
    """Generate a deterministic, 32-character SHA256 idempotency key.

    Conceptually mirrors the receipt idiom in ``omniagentos/runner/core.py`` (without
    importing from runner to avoid circular cycles). Identical inputs produce
    identical keys.
    """
    payload = f"{run_key}|{call.tool}|{call.index}|{call.args_digest}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32]


def plan_execution(
    calls: Sequence[ToolCall],
    catalog: Mapping[str, CatalogEntry] | None = None,
    *,
    budget: int | None = None,
) -> ExecutionPlan:
    """Greedily partition a list of tool calls into waves of execution.

    The batching algorithm is greedy, left-to-right, and order-preserving.

    Results are reassembled in request order regardless of wave shape.

    The budget is clamped to >= 1.

    ``catalog=None`` resolves the process-wide
    :func:`~omniagentos.toolplane.catalog.default_catalog`; only a tool genuinely
    absent from that catalog counts as unclassified.
    """
    resolved: Mapping[str, CatalogEntry] = catalog if catalog is not None else default_catalog()

    if not calls:
        return ExecutionPlan(
            waves=(),
            order=(),
            idempotency_keys={},
            cancellation_groups={},
        )

    effective_budget = max(1, budget if budget is not None else concurrency_budget())

    waves_list: list[Wave] = []
    current_group: list[ToolCall] = []
    start_split_reason: str | None = None

    def emit_current_group() -> None:
        nonlocal start_split_reason
        if not current_group:
            return

        kind: WaveKind = "concurrent" if len(current_group) >= 2 else "serial"

        union_keys: set[str] = set()
        for call in current_group:
            union_keys.update(resource_keys_for(call, resolved))
        sorted_keys = tuple(sorted(union_keys))

        if kind == "concurrent":
            # Concurrent waves must be pure read-only or pure parallel-safe writes
            all_read_only = True
            for call in current_group:
                entry = resolved.get(call.tool)
                if entry is None or not entry.read_only:
                    all_read_only = False
                    break
            reason = "read-only-disjoint" if all_read_only else "proven-disjoint-writes"
        else:
            single_call = current_group[0]
            entry = resolved.get(single_call.tool)
            if entry is None or not entry.classified:
                reason = "serial-unclassified"
            elif start_split_reason is not None:
                reason = start_split_reason
            else:
                reason = "serial-single"

        waves_list.append(
            Wave(
                kind=kind,
                calls=tuple(current_group),
                resource_keys=sorted_keys,
                reason=reason,
            )
        )
        current_group.clear()

    def has_declared_ownership(keys: tuple[str, ...]) -> bool:
        if not keys:
            return False
        return not any(k.startswith("unknown:") for k in keys)

    def check_write_safe(call_obj: ToolCall, entry_obj: CatalogEntry) -> bool:
        if not entry_obj.parallel_safe:
            return False
        keys = resource_keys_for(call_obj, resolved)
        return has_declared_ownership(keys)

    for call in calls:
        if not current_group:
            current_group.append(call)
            continue

        # Check if we can admit `call` to the open `current_group`
        # 1. Budget check
        if len(current_group) >= effective_budget:
            emit_current_group()
            start_split_reason = "serial-budget"
            current_group.append(call)
            continue

        # 2. Classified check for C
        entry_C = resolved.get(call.tool)
        if entry_C is None or not entry_C.classified:
            emit_current_group()
            start_split_reason = "serial-unclassified"
            current_group.append(call)
            continue

        # 3. Classified check for calls in G
        group_has_unclassified = False
        for g in current_group:
            entry_g = resolved.get(g.tool)
            if entry_g is None or not entry_g.classified:
                group_has_unclassified = True
                break

        if group_has_unclassified:
            emit_current_group()
            start_split_reason = "serial-unclassified"
            current_group.append(call)
            continue

        # 4. Resource keys disjointness check
        keys_C = set(resource_keys_for(call, resolved))
        union_G_keys: set[str] = set()
        for g in current_group:
            union_G_keys.update(resource_keys_for(g, resolved))

        if not keys_C.isdisjoint(union_G_keys):
            emit_current_group()
            start_split_reason = "serial-conflict"
            current_group.append(call)
            continue

        # 5. Read-only or Parallel-safe writes consistency check
        all_read_only = entry_C.read_only
        if all_read_only:
            for g in current_group:
                entry_g = resolved[g.tool]
                if not entry_g.read_only:
                    all_read_only = False
                    break

        all_write_safe = check_write_safe(call, entry_C)
        if all_write_safe:
            for g in current_group:
                entry_g = resolved[g.tool]
                if not check_write_safe(g, entry_g):
                    all_write_safe = False
                    break

        if all_read_only or all_write_safe:
            current_group.append(call)
        else:
            emit_current_group()
            start_split_reason = "serial-conflict"
            current_group.append(call)

    # Emit any remaining calls
    emit_current_group()

    # Order (request-order indices)
    order = tuple(c.index for c in calls)

    # Idempotency keys (only when entry exists and entry.idempotent or entry.read_only is True)
    idempotency_keys: dict[int, str] = {}
    for call in calls:
        if call.tool in resolved:
            entry = resolved[call.tool]
            if entry.idempotent or entry.read_only:
                idempotency_keys[call.index] = idempotency_key(call, entry)

    # Cancellation groups
    cancellation_groups: dict[str, list[int]] = {}
    for call in calls:
        group_name = "unknown"
        if call.tool in resolved:
            entry = resolved[call.tool]
            if entry.cancellation_group:
                group_name = entry.cancellation_group
        cancellation_groups.setdefault(group_name, []).append(call.index)

    cancellation_groups_final = {
        group: tuple(sorted(indices)) for group, indices in cancellation_groups.items()
    }

    return ExecutionPlan(
        waves=tuple(waves_list),
        order=order,
        idempotency_keys=idempotency_keys,
        cancellation_groups=cancellation_groups_final,
    )


def wave_lock_claims(wave: Wave, *, prefix: str = "path:") -> tuple[str, ...]:
    """The COMPLETE set of path-shaped resource keys a wave needs, for ONE acquisition.

    Hand this whole tuple to a single all-or-nothing acquire (the
    ``omniagentos.scope.locks`` contract). NEVER acquire these one at a time as the wave
    executes: partial acquisition is hold-and-wait, and hold-and-wait is the single Coffman
    condition that package removes structurally to get deadlock-freedom without a cycle
    detector. Acquiring incrementally would put that machinery back on the table.
    """
    claims: list[str] = []
    for key in wave.resource_keys:
        if key.startswith(prefix):
            claims.append(key[len(prefix) :])
    return tuple(sorted(claims))


class CancellationHook(Protocol):
    """Protocol for cancellation notification propagation."""

    def __call__(self, group: str, indices: tuple[int, ...]) -> None:
        """Call when propagate_cancellation is called."""
        ...


def propagate_cancellation(
    plan: ExecutionPlan, group: str, hook: CancellationHook
) -> tuple[int, ...]:
    """Notify `hook` of every call index in `group` and return them.

    Never raises; a hook that raises is swallowed so a cancellation cannot
    itself fail the plan.
    """
    indices = plan.calls_in_cancellation_group(group)
    try:
        hook(group, indices)
    except Exception:
        pass
    return indices
