"""Randomised scheduler equivalence and race property tests.

WHY THREADS AND SLEEP ARE USED:
------------------------------
The scheduler's core claim is that a concurrent wave is indistinguishable from
running those calls one at a time. A concurrent wave execution must be shown to have
no overlapping conflicting operations in flight at the same instant.
To prove this under real parallel execution conditions, this test suite uses real
Python threads via `ThreadPoolExecutor` and introduces a tiny time delay (`time.sleep`)
inside the simulated operations. Without the thread pool and the sleep, operations would
run sequentially and extremely fast, preventing real thread interleaving and overlap.
With a sleep, overlapping executions are forced to occur when scheduled in parallel,
and the safety-critical tracker (`in_flight`) can detect and report any overlapping conflicts
(e.g., if the scheduler incorrectly parallelised conflicting writes or read/write pairs).
"""

from __future__ import annotations

import random
import threading
import time
from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

import pytest

from omniagentos.connectors import ResultSizeClass, SideEffectClass
from omniagentos.contracts import ActionClass
from omniagentos.toolplane.catalog import CatalogEntry
from omniagentos.toolplane.scheduler import (
    ExecutionPlan,
    ToolCall,
    plan_execution,
    resource_keys_for,
    wave_lock_claims,
)

SEEDS = tuple(range(40))
KEYS = ("k0", "k1", "k2", "k3")
TOOLS = ("read_a", "read_b", "read_c", "write_a", "write_b", "write_c")


@dataclass
class FakeStore:
    """A tiny key/value world plus an in-flight tracker that detects overlap."""

    state: dict[str, int]
    in_flight: dict[str, str]  # resource key -> owning op label
    violations: list[str]
    lock: threading.Lock


def make_catalog_entry(
    tool_id: str,
    *,
    read_only: bool = False,
    parallel_safe: bool = True,
    idempotent: bool = False,
    classified: bool = True,
    resource_keys: tuple[str, ...] = (),
    cancellation_group: str = "",
) -> CatalogEntry:
    """Create a mock CatalogEntry for testing with sensible defaults."""
    return CatalogEntry(
        id=tool_id,
        namespace="test",
        label=tool_id,
        compact_hint="test",
        description="test",
        source="builtin",
        action_class=ActionClass.READ_ONLY if read_only else ActionClass.INTERNAL_REVERSIBLE,
        read_only=read_only,
        side_effect_class=SideEffectClass.NONE if read_only else SideEffectClass.INTERNAL_WRITE,
        resource_keys=resource_keys,
        idempotent=idempotent,
        parallel_safe=parallel_safe,
        cancellation_group=cancellation_group,
        credential_scope="",
        result_size_class=ResultSizeClass.SMALL,
        risk="low" if read_only else "medium",
        requires_scope=False,
        input_examples=(),
        parameter_names=(),
        callable_now=True,
        classified=classified,
    )


def make_test_catalog() -> dict[str, CatalogEntry]:
    """Build the synthetic catalog for tests."""
    return {
        "read_a": make_catalog_entry("read_a", read_only=True, parallel_safe=True, idempotent=True),
        "read_b": make_catalog_entry("read_b", read_only=True, parallel_safe=True, idempotent=True),
        "read_c": make_catalog_entry("read_c", read_only=True, parallel_safe=True, idempotent=True),
        "write_a": make_catalog_entry(
            "write_a", read_only=False, parallel_safe=True, idempotent=False
        ),
        "write_b": make_catalog_entry(
            "write_b", read_only=False, parallel_safe=True, idempotent=False
        ),
        "write_c": make_catalog_entry(
            "write_c", read_only=False, parallel_safe=True, idempotent=False
        ),
        "opaque": make_catalog_entry(
            "opaque",
            read_only=False,
            parallel_safe=False,
            idempotent=False,
            classified=False,
        ),
    }


def generate_workload(seed: int) -> list[ToolCall]:
    """Generate a random workload of calls."""
    rng = random.Random(seed)
    num_calls = rng.randint(3, 12)
    calls = []
    for i in range(num_calls):
        tool = rng.choice(TOOLS)
        key = rng.choice(KEYS)
        calls.append(
            ToolCall(
                index=i,
                tool=tool,
                resource_keys=(f"path:{key}",),
            )
        )
    return calls


def generate_biased_workload(seed: int) -> list[ToolCall]:
    """Generate a workload biased towards writes and a smaller key space."""
    rng = random.Random(seed)
    num_calls = rng.randint(3, 12)
    keys_subset = KEYS[:2]
    calls = []
    for i in range(num_calls):
        # 60% writes, 40% reads
        is_write = rng.random() < 0.60
        if is_write:
            tool = rng.choice(["write_a", "write_b", "write_c"])
        else:
            tool = rng.choice(["read_a", "read_b", "read_c"])
        key = rng.choice(keys_subset)
        calls.append(
            ToolCall(
                index=i,
                tool=tool,
                resource_keys=(f"path:{key}",),
            )
        )
    return calls


def generate_disjoint_reads_workload(seed: int) -> list[ToolCall]:
    """Generate a workload consisting solely of reads over disjoint keys."""
    rng = random.Random(seed)
    num_calls = rng.randint(2, min(len(KEYS), 4))
    keys_subset = list(KEYS[:num_calls])
    rng.shuffle(keys_subset)
    calls = []
    for i in range(num_calls):
        tool = rng.choice(["read_a", "read_b", "read_c"])
        key = keys_subset[i]
        calls.append(
            ToolCall(
                index=i,
                tool=tool,
                resource_keys=(f"path:{key}",),
            )
        )
    return calls


def get_target_key(call: ToolCall) -> str:
    """Extract targeted key K from the call's resource keys."""
    for rk in call.resource_keys:
        if rk.startswith("path:"):
            return rk[len("path:") :]
    raise ValueError(f"Call {call} has no target key prefixed with path:")


def apply_op(store: FakeStore, call: ToolCall) -> int:
    """Apply read or write semantics under lock."""
    key = get_target_key(call)
    is_write = "write" in call.tool or call.tool == "opaque"
    with store.lock:
        if is_write:
            val = store.state.get(key, 0) + 1
            store.state[key] = val
            return val
        else:
            return store.state.get(key, 0)


def guard(store: FakeStore, call: ToolCall, catalog: dict[str, CatalogEntry]) -> int:
    """Wrapper that tracks in-flight keys to detect concurrency violations."""
    keys = resource_keys_for(call, catalog)
    op_label = f"{call.tool}#{call.index}"

    with store.lock:
        for key in keys:
            if key in store.in_flight:
                other_op = store.in_flight[key]
                store.violations.append(
                    f"Violation: {op_label} overlapped with {other_op} on resource {key}"
                )
        for key in keys:
            store.in_flight[key] = op_label

    try:
        # sleep/jitter to allow interleavings
        time.sleep(0.001)
        return apply_op(store, call)
    finally:
        with store.lock:
            for key in keys:
                if store.in_flight.get(key) == op_label:
                    del store.in_flight[key]


def run_serial_reference(
    calls: Sequence[ToolCall], catalog: dict[str, CatalogEntry]
) -> tuple[dict[str, int], list[int]]:
    """Execute calls sequentially starting from a fresh store."""
    store = FakeStore(state={}, in_flight={}, violations=[], lock=threading.Lock())
    results = []
    for call in calls:
        results.append(apply_op(store, call))
    return store.state, results


def run_planned(
    plan: ExecutionPlan, catalog: dict[str, CatalogEntry]
) -> tuple[dict[str, int], list[int], list[str]]:
    """Execute plan waves in order, parallelising concurrent ones."""
    store = FakeStore(state={}, in_flight={}, violations=[], lock=threading.Lock())
    results_map: dict[int, int] = {}

    for wave in plan.waves:
        if wave.kind == "serial":
            for call in wave.calls:
                results_map[call.index] = guard(store, call, catalog)
        elif wave.kind == "concurrent":
            with ThreadPoolExecutor(max_workers=len(wave.calls)) as executor:
                futures = {
                    executor.submit(guard, store, call, catalog): call.index for call in wave.calls
                }
                for future in futures:
                    call_idx = futures[future]
                    results_map[call_idx] = future.result()

    ordered_results = [results_map[idx] for idx in plan.order]
    return store.state, ordered_results, store.violations


# --- TESTS ---


@pytest.mark.parametrize("seed", SEEDS)
def test_planned_execution_matches_the_serial_reference(seed: int) -> None:
    """1. Equivalence under standard randomized workloads."""
    catalog = make_test_catalog()
    calls = generate_workload(seed)

    plan = plan_execution(calls, catalog)

    ref_state, ref_results = run_serial_reference(calls, catalog)
    plan_state, plan_results, violations = run_planned(plan, catalog)

    assert plan_state == ref_state, f"State mismatch for seed {seed}: {plan_state} != {ref_state}"
    assert plan_results == ref_results, (
        f"Results mismatch for seed {seed}: {plan_results} != {ref_results}"
    )
    assert not violations, (
        f"Overlapping execution violations detected for seed {seed}: {violations}"
    )


def test_conflicting_operations_never_overlap() -> None:
    """2. Conflicting operations never overlap under biased workloads."""
    catalog = make_test_catalog()
    has_concurrent_wave = False

    for seed in SEEDS:
        calls = generate_biased_workload(seed)
        plan = plan_execution(calls, catalog)

        if any(w.kind == "concurrent" and len(w.calls) >= 2 for w in plan.waves):
            has_concurrent_wave = True

        ref_state, ref_results = run_serial_reference(calls, catalog)
        plan_state, plan_results, violations = run_planned(plan, catalog)

        assert plan_state == ref_state, (
            f"State mismatch for seed {seed}: {plan_state} != {ref_state}"
        )
        assert plan_results == ref_results, (
            f"Results mismatch for seed {seed}: {plan_results} != {ref_results}"
        )
        assert not violations, (
            f"Overlapping execution violations detected for seed {seed}: {violations}"
        )

    assert has_concurrent_wave, (
        "No seed produced a concurrent wave with width >= 2 under biased workload"
    )


@pytest.mark.parametrize("seed", SEEDS)
def test_reads_only_workload_actually_parallelises(seed: int) -> None:
    """3. All-reads workloads over disjoint keys must actually parallelise."""
    catalog = make_test_catalog()
    calls = generate_disjoint_reads_workload(seed)

    if len(calls) < 2:
        return

    plan = plan_execution(calls, catalog)

    assert plan.max_wave_width >= 2, (
        f"Seed {seed} failed to parallelise: width {plan.max_wave_width} < 2"
    )

    ref_state, ref_results = run_serial_reference(calls, catalog)
    plan_state, plan_results, violations = run_planned(plan, catalog)

    assert plan_state == ref_state, f"State mismatch for seed {seed}: {plan_state} != {ref_state}"
    assert plan_results == ref_results, (
        f"Results mismatch for seed {seed}: {plan_results} != {ref_results}"
    )
    assert not violations, (
        f"Overlapping execution violations detected for seed {seed}: {violations}"
    )


@pytest.mark.parametrize("seed", SEEDS)
def test_all_writes_on_one_key_is_fully_serial(seed: int) -> None:
    """4. Sequential updates on the same key must be executed fully serially."""
    catalog = make_test_catalog()
    rng = random.Random(seed)
    num_calls = rng.randint(3, 12)
    key = "k0"
    calls = []
    for i in range(num_calls):
        tool = rng.choice(["write_a", "write_b", "write_c"])
        calls.append(
            ToolCall(
                index=i,
                tool=tool,
                resource_keys=(f"path:{key}",),
            )
        )

    plan = plan_execution(calls, catalog)

    for wave in plan.waves:
        assert wave.kind == "serial", f"Seed {seed} has non-serial wave for sequential writes"

    assert plan.max_wave_width == 1, f"Seed {seed} has max wave width {plan.max_wave_width} > 1"

    ref_state, ref_results = run_serial_reference(calls, catalog)
    plan_state, plan_results, violations = run_planned(plan, catalog)

    assert plan_state == ref_state, f"State mismatch for seed {seed}: {plan_state} != {ref_state}"
    assert plan_results == ref_results, (
        f"Results mismatch for seed {seed}: {plan_results} != {ref_results}"
    )
    assert not violations, (
        f"Overlapping execution violations detected for seed {seed}: {violations}"
    )
    assert plan_state.get(key, 0) == num_calls, (
        f"Seed {seed} lost updates: {plan_state.get(key, 0)} != {num_calls}"
    )


def test_one_and_two_call_workloads_bypass_to_the_ordinary_path() -> None:
    """5. One- and two-call workloads bypass batching to the ordinary sequential path."""
    catalog = make_test_catalog()

    # 1. One call workload
    calls_1 = [ToolCall(index=0, tool="read_a", resource_keys=("path:k0",))]
    plan_1 = plan_execution(calls_1, catalog)
    assert len(plan_1.waves) == 1
    assert plan_1.waves[0].kind == "serial"
    assert plan_1.concurrent_waves == 0

    ref_state_1, ref_results_1 = run_serial_reference(calls_1, catalog)
    plan_state_1, plan_results_1, violations_1 = run_planned(plan_1, catalog)
    assert plan_state_1 == ref_state_1
    assert plan_results_1 == ref_results_1
    assert not violations_1

    # 2. Two calls workload (both reads, disjoint keys)
    calls_2 = [
        ToolCall(index=0, tool="read_a", resource_keys=("path:k0",)),
        ToolCall(index=1, tool="read_b", resource_keys=("path:k1",)),
    ]
    # Batching one or two calls is machinery that cannot pay for itself.
    # Forcing budget=1 bypasses to the ordinary sequential path where every wave is serial.
    plan_2 = plan_execution(calls_2, catalog, budget=1)
    assert len(plan_2.waves) == 2
    assert all(w.kind == "serial" for w in plan_2.waves)
    assert plan_2.concurrent_waves == 0

    ref_state_2, ref_results_2 = run_serial_reference(calls_2, catalog)
    plan_state_2, plan_results_2, violations_2 = run_planned(plan_2, catalog)
    assert plan_state_2 == ref_state_2
    assert plan_results_2 == ref_results_2
    assert not violations_2


@pytest.mark.parametrize("seed", SEEDS)
def test_unclassified_calls_never_join_a_concurrent_wave(seed: int) -> None:
    """6. Unclassified tools must always be serialised and never join concurrent waves."""
    catalog = make_test_catalog()
    rng = random.Random(seed)
    num_calls = rng.randint(3, 12)
    calls = []
    for i in range(num_calls):
        if rng.random() < 0.3:
            tool = "opaque"
        else:
            tool = rng.choice(TOOLS)
        key = rng.choice(KEYS)
        calls.append(
            ToolCall(
                index=i,
                tool=tool,
                resource_keys=(f"path:{key}",),
            )
        )

    plan = plan_execution(calls, catalog)

    for wave in plan.waves:
        if wave.kind == "concurrent":
            for call in wave.calls:
                assert call.tool != "opaque", (
                    f"Seed {seed} contains unclassified 'opaque' tool in concurrent wave"
                )

    ref_state, ref_results = run_serial_reference(calls, catalog)
    plan_state, plan_results, violations = run_planned(plan, catalog)

    assert plan_state == ref_state, f"State mismatch for seed {seed}: {plan_state} != {ref_state}"
    assert plan_results == ref_results, (
        f"Results mismatch for seed {seed}: {plan_results} != {ref_results}"
    )
    assert not violations, (
        f"Overlapping execution violations detected for seed {seed}: {violations}"
    )


@pytest.mark.parametrize("seed", SEEDS)
def test_results_preserve_request_order_under_every_schedule(seed: int) -> None:
    """7. Results must be assembled and returned in request order."""
    catalog = make_test_catalog()
    calls = generate_workload(seed)

    plan = plan_execution(calls, catalog)

    assert plan.order == tuple(range(len(calls))), (
        f"Seed {seed} has plan.order mismatch: {plan.order}"
    )

    ref_state, ref_results = run_serial_reference(calls, catalog)
    plan_state, plan_results, violations = run_planned(plan, catalog)

    assert plan_results == ref_results, (
        f"Seed {seed} results mismatch: {plan_results} != {ref_results}"
    )


@pytest.mark.parametrize("seed", SEEDS)
def test_wave_lock_claims_cover_every_key_the_wave_touches(seed: int) -> None:
    """8. The wave lock claims set must exactly cover all keys touched by wave calls."""
    catalog = make_test_catalog()
    calls = generate_workload(seed)

    plan = plan_execution(calls, catalog)

    for wave in plan.waves:
        expected_claims = set()
        for call in wave.calls:
            for rk in call.resource_keys:
                if rk.startswith("path:"):
                    expected_claims.add(rk[len("path:") :])

        assert set(wave_lock_claims(wave)) == expected_claims, (
            f"Seed {seed} lock claims mismatch for wave {wave}: {set(wave_lock_claims(wave))} != {expected_claims}"
        )
