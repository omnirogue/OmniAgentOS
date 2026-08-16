"""Tests for the deterministic resource-aware scheduler.

L17/Work Item W5 requirements tested:
1. three read-only calls with disjoint keys -> ONE concurrent wave, reason "read-only-disjoint"
2. three read-only calls SHARING a resource key -> three serial waves
3. a mutation between two reads -> reads never batch across it; the mutation is its own serial wave
4. two writes with parallel_safe=True and disjoint non-empty keys -> one concurrent wave,
   reason "proven-disjoint-writes"
5. two writes with parallel_safe=True but OVERLAPPING keys -> serial
6. a write with parallel_safe=True but EMPTY declared keys -> serial (no proven ownership)
7. an unknown tool -> serial, reason "serial-unclassified"
8. classified=False -> serial
9. budget: 5 disjoint reads with budget=2 -> waves of width <= 2
10. plan.order always equals the request order, for every above scenario
11. flattening plan.waves yields exactly the input calls, once each, in request order
12. reads and writes NEVER share a wave
13. idempotency_keys omits non-idempotent calls and includes read-only ones; the same call yields
    the same key across two plan_execution runs; a different args_digest yields a different key
14. cancellation_groups groups correctly; calls_in_cancellation_group("nope") is ()
15. propagate_cancellation calls the hook and returns indices; a hook that raises is swallowed
16. wave_lock_claims returns only path-prefixed keys, prefix stripped, sorted
17. an EMPTY call list -> a plan with no waves, empty order, and no crash
18. a single call -> one serial wave, reason "serial-single"
19. determinism: two plan_execution calls on the same input produce equal plans
"""

from __future__ import annotations

from omniagentos.connectors import ResultSizeClass, SideEffectClass
from omniagentos.contracts import ActionClass
from omniagentos.toolplane.catalog import CatalogEntry
from omniagentos.toolplane.scheduler import (
    ToolCall,
    plan_execution,
    propagate_cancellation,
    resource_keys_for,
    wave_lock_claims,
)


def make_catalog_entry(
    tool_id: str,
    *,
    read_only: bool = False,
    resource_keys: tuple[str, ...] = (),
    idempotent: bool = False,
    parallel_safe: bool = False,
    cancellation_group: str = "",
    classified: bool = True,
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


class TestSchedulerScenario:
    """Class grouping all the W5 specification test cases."""

    def test_three_disjoint_reads_batches_concurrently(self) -> None:
        """1. Three read-only calls with disjoint keys -> ONE concurrent wave."""
        catalog = {
            "read1": make_catalog_entry("read1", read_only=True, resource_keys=("path:a",)),
            "read2": make_catalog_entry("read2", read_only=True, resource_keys=("path:b",)),
            "read3": make_catalog_entry("read3", read_only=True, resource_keys=("path:c",)),
        }
        calls = [
            ToolCall(0, "read1"),
            ToolCall(1, "read2"),
            ToolCall(2, "read3"),
        ]
        plan = plan_execution(calls, catalog)
        assert len(plan.waves) == 1
        wave = plan.waves[0]
        assert wave.kind == "concurrent"
        assert wave.reason == "read-only-disjoint"
        assert wave.resource_keys == ("path:a", "path:b", "path:c")
        assert len(wave.calls) == 3
        assert plan.order == (0, 1, 2)

    def test_three_sharing_reads_serial(self) -> None:
        """2. Three read-only calls SHARING a resource key -> three serial waves."""
        catalog = {
            "read1": make_catalog_entry("read1", read_only=True, resource_keys=("path:shared",)),
            "read2": make_catalog_entry("read2", read_only=True, resource_keys=("path:shared",)),
            "read3": make_catalog_entry("read3", read_only=True, resource_keys=("path:shared",)),
        }
        calls = [
            ToolCall(0, "read1"),
            ToolCall(1, "read2"),
            ToolCall(2, "read3"),
        ]
        plan = plan_execution(calls, catalog)
        assert len(plan.waves) == 3
        assert all(w.kind == "serial" for w in plan.waves)
        assert plan.waves[0].reason == "serial-single"
        assert plan.waves[1].reason == "serial-conflict"
        assert plan.waves[2].reason == "serial-conflict"
        assert plan.order == (0, 1, 2)

    def test_mutation_between_reads_breaks_batch(self) -> None:
        """3. A mutation between two reads -> mutation is its own serial wave."""
        catalog = {
            "read1": make_catalog_entry("read1", read_only=True, resource_keys=("path:a",)),
            "write": make_catalog_entry("write", read_only=False, resource_keys=("path:b",)),
            "read2": make_catalog_entry("read2", read_only=True, resource_keys=("path:c",)),
        }
        calls = [
            ToolCall(0, "read1"),
            ToolCall(1, "write"),
            ToolCall(2, "read2"),
        ]
        plan = plan_execution(calls, catalog)
        assert len(plan.waves) == 3
        assert plan.waves[0].calls[0].tool == "read1"
        assert plan.waves[1].calls[0].tool == "write"
        assert plan.waves[2].calls[0].tool == "read2"
        assert all(w.kind == "serial" for w in plan.waves)
        assert plan.waves[0].reason == "serial-single"
        assert plan.waves[1].reason == "serial-conflict"
        assert plan.waves[2].reason == "serial-conflict"

    def test_two_proven_disjoint_writes_batches(self) -> None:
        """4. Two parallel-safe writes with disjoint keys -> concurrent wave."""
        catalog = {
            "write1": make_catalog_entry(
                "write1", read_only=False, parallel_safe=True, resource_keys=("path:a",)
            ),
            "write2": make_catalog_entry(
                "write2", read_only=False, parallel_safe=True, resource_keys=("path:b",)
            ),
        }
        calls = [
            ToolCall(0, "write1"),
            ToolCall(1, "write2"),
        ]
        plan = plan_execution(calls, catalog)
        assert len(plan.waves) == 1
        wave = plan.waves[0]
        assert wave.kind == "concurrent"
        assert wave.reason == "proven-disjoint-writes"
        assert wave.resource_keys == ("path:a", "path:b")

    def test_two_parallel_safe_writes_overlapping_keys_serial(self) -> None:
        """5. Two writes with parallel_safe=True but OVERLAPPING keys -> serial."""
        catalog = {
            "write1": make_catalog_entry(
                "write1", read_only=False, parallel_safe=True, resource_keys=("path:shared",)
            ),
            "write2": make_catalog_entry(
                "write2", read_only=False, parallel_safe=True, resource_keys=("path:shared",)
            ),
        }
        calls = [
            ToolCall(0, "write1"),
            ToolCall(1, "write2"),
        ]
        plan = plan_execution(calls, catalog)
        assert len(plan.waves) == 2
        assert all(w.kind == "serial" for w in plan.waves)
        assert plan.waves[1].reason == "serial-conflict"

    def test_parallel_safe_write_empty_keys_serial(self) -> None:
        """6. A write with parallel_safe=True but EMPTY declared keys -> serial."""
        catalog = {
            "write1": make_catalog_entry(
                "write1", read_only=False, parallel_safe=True, resource_keys=()
            ),
            "write2": make_catalog_entry(
                "write2", read_only=False, parallel_safe=True, resource_keys=("path:b",)
            ),
        }
        calls = [
            ToolCall(0, "write1"),
            ToolCall(1, "write2"),
        ]
        plan = plan_execution(calls, catalog)
        assert len(plan.waves) == 2
        assert all(w.kind == "serial" for w in plan.waves)
        assert plan.waves[1].reason == "serial-conflict"

    def test_unknown_tool_serial(self) -> None:
        """7. An unknown tool -> serial, reason "serial-unclassified"."""
        catalog = {}
        calls = [ToolCall(0, "unknown_tool")]
        plan = plan_execution(calls, catalog)
        assert len(plan.waves) == 1
        assert plan.waves[0].kind == "serial"
        assert plan.waves[0].reason == "serial-unclassified"

    def test_classified_false_serial(self) -> None:
        """8. classified=False -> serial."""
        catalog = {
            "tool": make_catalog_entry("tool", read_only=True, classified=False),
        }
        calls = [ToolCall(0, "tool")]
        plan = plan_execution(calls, catalog)
        assert len(plan.waves) == 1
        assert plan.waves[0].kind == "serial"
        assert plan.waves[0].reason == "serial-unclassified"

    def test_budget_constraints(self) -> None:
        """9. budget: 5 disjoint reads with budget=2 -> waves of width <= 2."""
        catalog = {
            f"read{i}": make_catalog_entry(f"read{i}", read_only=True, resource_keys=(f"path:{i}",))
            for i in range(1, 6)
        }
        calls = [ToolCall(i, f"read{i + 1}") for i in range(5)]
        plan = plan_execution(calls, catalog, budget=2)
        assert len(plan.waves) == 3
        assert plan.waves[0].kind == "concurrent"
        assert len(plan.waves[0].calls) == 2
        assert plan.waves[1].kind == "concurrent"
        assert len(plan.waves[1].calls) == 2
        assert plan.waves[2].kind == "serial"
        assert len(plan.waves[2].calls) == 1
        assert plan.waves[2].reason == "serial-budget"

    def test_order_and_flattening_properties(self) -> None:
        """10 & 11. order equals request order and flattening yields exact inputs."""
        catalog = {
            "read1": make_catalog_entry("read1", read_only=True, resource_keys=("path:a",)),
            "write": make_catalog_entry("write", read_only=False, resource_keys=("path:b",)),
            "read2": make_catalog_entry("read2", read_only=True, resource_keys=("path:c",)),
        }
        calls = [
            ToolCall(0, "read1"),
            ToolCall(1, "write"),
            ToolCall(2, "read2"),
        ]
        plan = plan_execution(calls, catalog)
        assert plan.order == (0, 1, 2)

        flattened = [call for wave in plan.waves for call in wave.calls]
        assert flattened == calls

    def test_reads_and_writes_never_mix(self) -> None:
        """12. Reads and writes NEVER share a wave."""
        catalog = {
            "read": make_catalog_entry("read", read_only=True, resource_keys=("path:a",)),
            "write": make_catalog_entry(
                "write", read_only=False, parallel_safe=True, resource_keys=("path:b",)
            ),
        }
        calls = [
            ToolCall(0, "read"),
            ToolCall(1, "write"),
        ]
        plan = plan_execution(calls, catalog)
        for wave in plan.waves:
            has_read = any(catalog[c.tool].read_only for c in wave.calls)
            has_write = any(not catalog[c.tool].read_only for c in wave.calls)
            assert not (has_read and has_write)

    def test_idempotency_keys_rules(self) -> None:
        """13. idempotency_keys validation and key uniqueness."""
        catalog = {
            "read": make_catalog_entry("read", read_only=True),
            "write_idempotent": make_catalog_entry(
                "write_idempotent", read_only=False, idempotent=True
            ),
            "write_unsafe": make_catalog_entry("write_unsafe", read_only=False, idempotent=False),
        }
        calls = [
            ToolCall(0, "read", args_digest="abc"),
            ToolCall(1, "write_idempotent", args_digest="xyz"),
            ToolCall(2, "write_unsafe", args_digest="123"),
        ]
        plan = plan_execution(calls, catalog)
        assert 0 in plan.idempotency_keys
        assert 1 in plan.idempotency_keys
        assert 2 not in plan.idempotency_keys

        # Same call -> same key
        plan2 = plan_execution(calls, catalog)
        assert plan.idempotency_keys[0] == plan2.idempotency_keys[0]

        # Different digest -> different key
        calls_diff = [
            ToolCall(0, "read", args_digest="diff"),
        ]
        plan_diff = plan_execution(calls_diff, catalog)
        assert plan.idempotency_keys[0] != plan_diff.idempotency_keys[0]

    def test_cancellation_groups_handling(self) -> None:
        """14. cancellation_groups mapping and query."""
        catalog = {
            "tool1": make_catalog_entry("tool1", cancellation_group="groupA"),
            "tool2": make_catalog_entry("tool2", cancellation_group="groupA"),
            "tool3": make_catalog_entry("tool3", cancellation_group="groupB"),
        }
        calls = [
            ToolCall(0, "tool1"),
            ToolCall(1, "tool2"),
            ToolCall(2, "tool3"),
        ]
        plan = plan_execution(calls, catalog)
        assert plan.calls_in_cancellation_group("groupA") == (0, 1)
        assert plan.calls_in_cancellation_group("groupB") == (2,)
        assert plan.calls_in_cancellation_group("nope") == ()

    def test_propagate_cancellation_hook(self) -> None:
        """15. propagate_cancellation hook invocation and error swallowing."""
        catalog = {
            "tool1": make_catalog_entry("tool1", cancellation_group="groupA"),
        }
        calls = [ToolCall(0, "tool1")]
        plan = plan_execution(calls, catalog)

        called_args = []

        def my_hook(group: str, indices: tuple[int, ...]) -> None:
            called_args.append((group, indices))

        res = propagate_cancellation(plan, "groupA", my_hook)
        assert res == (0,)
        assert called_args == [("groupA", (0,))]

        def raising_hook(group: str, indices: tuple[int, ...]) -> None:
            raise ValueError("Swallowed exception")

        res2 = propagate_cancellation(plan, "groupA", raising_hook)
        assert res2 == (0,)

    def test_wave_lock_claims_processing(self) -> None:
        """16. wave_lock_claims returns sorted prefix-stripped path keys."""
        catalog = {
            "read": make_catalog_entry(
                "read", read_only=True, resource_keys=("path:foo/bar", "other:val", "path:abc")
            ),
        }
        calls = [ToolCall(0, "read")]
        plan = plan_execution(calls, catalog)
        wave = plan.waves[0]
        claims = wave_lock_claims(wave, prefix="path:")
        assert claims == ("abc", "foo/bar")

    def test_empty_call_list_safety(self) -> None:
        """17. empty call list works safely."""
        plan = plan_execution([])
        assert len(plan.waves) == 0
        assert plan.order == ()
        assert plan.idempotency_keys == {}
        assert plan.cancellation_groups == {}

    def test_single_call_scenario(self) -> None:
        """18. single call -> serial wave with serial-single reason."""
        catalog = {
            "read": make_catalog_entry("read", read_only=True),
        }
        calls = [ToolCall(0, "read")]
        plan = plan_execution(calls, catalog)
        assert len(plan.waves) == 1
        assert plan.waves[0].kind == "serial"
        assert plan.waves[0].reason == "serial-single"

    def test_pure_determinism(self) -> None:
        """19. determinism of plan execution."""
        catalog = {
            "read1": make_catalog_entry("read1", read_only=True, resource_keys=("path:a",)),
            "write": make_catalog_entry("write", read_only=False, resource_keys=("path:b",)),
            "read2": make_catalog_entry("read2", read_only=True, resource_keys=("path:c",)),
        }
        calls = [
            ToolCall(0, "read1"),
            ToolCall(1, "write"),
            ToolCall(2, "read2"),
        ]
        plan1 = plan_execution(calls, catalog)
        plan2 = plan_execution(calls, catalog)
        assert plan1 == plan2

    def test_resource_keys_for_uses_default_catalog_when_none(self) -> None:
        """A known built-in resolves its catalog resource keys with catalog=None."""
        # read_file is a real built-in whose catalog resource_keys are ("fs:read",)
        assert resource_keys_for(ToolCall(index=0, tool="read_file"), None) == ("fs:read",)

    def test_plan_execution_defaults_to_real_catalog(self) -> None:
        """A real built-in is classified; only a genuinely absent tool is unclassified."""
        calls = [ToolCall(0, "read_file"), ToolCall(1, "definitely_not_a_tool")]
        plan = plan_execution(calls)
        assert len(plan.waves) == 2
        assert plan.waves[0].calls[0].tool == "read_file"
        assert plan.waves[0].reason == "serial-single"
        assert plan.waves[1].calls[0].tool == "definitely_not_a_tool"
        assert plan.waves[1].reason == "serial-unclassified"
        assert plan.order == (0, 1)

        # An explicitly passed catalog still wins over the default (pass a synthetic catalog that
        # does NOT contain read_file and assert that call is now "serial-unclassified").
        synthetic_catalog = {}
        plan_synthetic = plan_execution(calls, synthetic_catalog)
        assert len(plan_synthetic.waves) == 2
        assert plan_synthetic.waves[0].calls[0].tool == "read_file"
        assert plan_synthetic.waves[0].reason == "serial-unclassified"
        assert plan_synthetic.waves[1].calls[0].tool == "definitely_not_a_tool"
        assert plan_synthetic.waves[1].reason == "serial-unclassified"
