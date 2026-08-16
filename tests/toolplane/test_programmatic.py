"""Tests for programmatic-call router and declarative plan runner."""

from __future__ import annotations

import json
from pathlib import Path

from omniagentos.connectors import ResultSizeClass, SideEffectClass
from omniagentos.contracts import ActionClass
from omniagentos.toolplane.catalog import CatalogEntry
from omniagentos.toolplane.programmatic import (
    AggregateStep,
    DeclarativePlan,
    FilterStep,
    MapStep,
    RoutingContext,
    route_programmatic,
    run_declarative,
    validate_plan,
)
from omniagentos.toolplane.scheduler import ToolCall


def _make_catalog_entry(
    tool_id: str,
    *,
    read_only: bool = True,
    idempotent: bool = True,
    result_size_class: ResultSizeClass = ResultSizeClass.SMALL,
    classified: bool = True,
    resource_keys: tuple[str, ...] = (),
) -> CatalogEntry:
    """Create a mock CatalogEntry for testing."""
    return CatalogEntry(
        id=tool_id,
        namespace="test",
        label=tool_id.title(),
        compact_hint="hint",
        description="desc",
        source="builtin",
        action_class=ActionClass.READ_ONLY if read_only else ActionClass.INTERNAL_REVERSIBLE,
        read_only=read_only,
        side_effect_class=SideEffectClass.NONE if read_only else SideEffectClass.INTERNAL_WRITE,
        resource_keys=resource_keys,
        idempotent=idempotent,
        parallel_safe=True,
        cancellation_group="test-cg",
        credential_scope="",
        result_size_class=result_size_class,
        risk="low",
        requires_scope=False,
        input_examples=(),
        parameter_names=(),
        callable_now=True,
        classified=classified,
    )


class TestProgrammaticRouter:
    """Test suite for route_programmatic decision logic."""

    def test_too_few_calls(self):
        """Routing check: 1 and 2 calls are too few for programmatic execution."""
        catalog = {
            "read_tool": _make_catalog_entry("read_tool", read_only=True),
        }
        ctx = RoutingContext()

        # 1 call
        res_1 = route_programmatic([ToolCall(index=0, tool="read_tool")], ctx, catalog)
        assert res_1.route == "conversational"
        assert res_1.reason == "too-few-calls"

        # 2 calls
        res_2 = route_programmatic(
            [ToolCall(index=0, tool="read_tool"), ToolCall(index=1, tool="read_tool")],
            ctx,
            catalog,
        )
        assert res_2.route == "conversational"
        assert res_2.reason == "too-few-calls"

    def test_reasoning_needed_after_each_result(self):
        """Routing check: reasoning needed after each result disqualifies programmatic execution."""
        catalog = {
            "read_tool": _make_catalog_entry("read_tool", read_only=True),
        }
        calls = [ToolCall(index=i, tool="read_tool") for i in range(10)]
        ctx = RoutingContext(reasoning_needed_after_each_result=True)

        res = route_programmatic(calls, ctx, catalog)
        assert res.route == "conversational"
        assert res.reason == "reasoning-between-calls"

    def test_user_feedback_between_calls(self):
        """Routing check: user feedback expected between calls disqualifies programmatic execution."""
        catalog = {
            "read_tool": _make_catalog_entry("read_tool", read_only=True),
        }
        calls = [ToolCall(index=i, tool="read_tool") for i in range(10)]
        ctx = RoutingContext(user_feedback_between_calls=True)

        res = route_programmatic(calls, ctx, catalog)
        assert res.route == "conversational"
        assert res.reason == "user-feedback-between-calls"

    def test_unknown_tool_among_calls(self):
        """Routing check: an unclassified or unknown tool among calls disqualifies programmatic execution."""
        catalog = {
            "read_tool": _make_catalog_entry("read_tool", read_only=True),
        }
        calls = [
            ToolCall(index=0, tool="read_tool"),
            ToolCall(index=1, tool="read_tool"),
            ToolCall(index=2, tool="unknown_tool"),
        ]
        ctx = RoutingContext()

        res = route_programmatic(calls, ctx, catalog)
        assert res.route == "conversational"
        assert res.reason == "unclassified-call"

    def test_unclassified_tool(self):
        """Routing check: a tool in catalog with classified=False is unclassified."""
        catalog = {
            "unclassified_tool": _make_catalog_entry("unclassified_tool", classified=False),
        }
        calls = [
            ToolCall(index=0, tool="unclassified_tool"),
            ToolCall(index=1, tool="unclassified_tool"),
            ToolCall(index=2, tool="unclassified_tool"),
        ]
        ctx = RoutingContext()

        res = route_programmatic(calls, ctx, catalog)
        assert res.route == "conversational"
        assert res.reason == "unclassified-call"

    def test_non_idempotent_write_scoped(self):
        """Routing check: non-idempotent write, even if scoped, cannot run programmatically."""
        catalog = {
            "write_tool": _make_catalog_entry("write_tool", read_only=False, idempotent=False),
        }
        calls = [ToolCall(index=i, tool="write_tool") for i in range(3)]
        ctx = RoutingContext(scoped=True)

        res = route_programmatic(calls, ctx, catalog)
        assert res.route == "conversational"
        assert res.reason == "unscoped-or-non-idempotent-effects"

    def test_idempotent_write_unscoped(self):
        """Routing check: write that is unscoped, even if idempotent, cannot run programmatically."""
        catalog = {
            "write_tool": _make_catalog_entry("write_tool", read_only=False, idempotent=True),
        }
        calls = [ToolCall(index=i, tool="write_tool") for i in range(3)]
        ctx = RoutingContext(scoped=False)

        res = route_programmatic(calls, ctx, catalog)
        assert res.route == "conversational"
        assert res.reason == "unscoped-or-non-idempotent-effects"

    def test_disqualifier_beats_qualifier(self):
        """Routing check: disqualifiers absolutely outvote qualifiers.

        20 identical unscoped writes (perfect fan-out shape) is disqualified.
        """
        catalog = {
            "write_tool": _make_catalog_entry("write_tool", read_only=False, idempotent=True),
        }
        calls = [ToolCall(index=i, tool="write_tool") for i in range(20)]
        ctx = RoutingContext(scoped=False)  # unscoped

        res = route_programmatic(calls, ctx, catalog)
        assert res.route == "conversational"
        assert res.reason == "unscoped-or-non-idempotent-effects"
        assert res.evidence == ()

    def test_six_identical_reads_fanout(self):
        """Routing check: fan-out qualifier matches for >= 4 identical calls."""
        catalog = {
            "read_tool": _make_catalog_entry("read_tool", read_only=True),
        }
        calls = [ToolCall(index=i, tool="read_tool") for i in range(6)]
        ctx = RoutingContext()

        res = route_programmatic(calls, ctx, catalog)
        assert res.route == "programmatic"
        assert "fan-out" in res.evidence
        assert res.reason == "fan-out"

    def test_large_intermediates(self):
        """Routing check: large-intermediates qualifier matches for large result size class."""
        catalog = {
            "large_tool": _make_catalog_entry(
                "large_tool", read_only=True, result_size_class=ResultSizeClass.LARGE
            ),
        }
        calls = [ToolCall(index=i, tool="large_tool") for i in range(3)]
        ctx = RoutingContext()

        res = route_programmatic(calls, ctx, catalog)
        assert res.route == "programmatic"
        assert "large-intermediates" in res.evidence
        assert res.reason == "large-intermediates"

    def test_deterministic_chain(self):
        """Routing check: deterministic-chain matches when all calls are read_only or idempotent."""
        catalog = {
            "read_tool": _make_catalog_entry("read_tool", read_only=True, idempotent=True),
        }
        calls = [ToolCall(index=i, tool="read_tool") for i in range(3)]
        ctx = RoutingContext()

        res = route_programmatic(calls, ctx, catalog)
        assert res.route == "programmatic"
        assert "deterministic-chain" in res.evidence
        assert res.reason == "deterministic-chain"

    def test_parallel_io_disjoint_resources(self):
        """Routing check: parallel-io matches when there are disjoint resource claims."""
        catalog = {
            "read_tool_1": _make_catalog_entry(
                "read_tool_1", read_only=True, resource_keys=("r1",)
            ),
            "read_tool_2": _make_catalog_entry(
                "read_tool_2", read_only=True, resource_keys=("r2",)
            ),
            "read_tool_3": _make_catalog_entry(
                "read_tool_3", read_only=True, resource_keys=("r1", "r2")
            ),
        }
        calls = [
            ToolCall(index=0, tool="read_tool_1"),
            ToolCall(index=1, tool="read_tool_2"),
            ToolCall(index=2, tool="read_tool_3"),
        ]
        ctx = RoutingContext()

        res = route_programmatic(calls, ctx, catalog)
        assert res.route == "programmatic"
        assert "parallel-io" in res.evidence

    def test_deterministic_decision(self):
        """Routing check: decision logic is purely deterministic."""
        catalog = {
            "read_tool": _make_catalog_entry("read_tool", read_only=True),
        }
        calls = [ToolCall(index=i, tool="read_tool") for i in range(5)]
        ctx = RoutingContext()

        res1 = route_programmatic(calls, ctx, catalog)
        res2 = route_programmatic(calls, ctx, catalog)
        assert res1 == res2


class TestDeclarativeValidator:
    """Test suite for declarative plan validator."""

    def test_empty_steps(self):
        """Validator check: plan must contain at least one step."""
        plan = DeclarativePlan(steps=(), output="any")
        reason = validate_plan(plan)
        assert "at least one step" in reason

        calls = 0

        def invoker(tool, args):
            nonlocal calls
            calls += 1
            return {}

        res = run_declarative(plan, invoker)
        assert res.ok is False
        assert res.error == reason
        assert calls == 0

    def test_duplicate_names(self):
        """Validator check: step names must be unique."""
        plan = DeclarativePlan(
            steps=(
                MapStep(name="step1", tool="t1", items=(1,)),
                MapStep(name="step1", tool="t2", items=(2,)),
            ),
            output="step1",
        )
        reason = validate_plan(plan)
        assert "Duplicate step name" in reason

        calls = 0

        def invoker(tool, args):
            nonlocal calls
            calls += 1
            return {}

        res = run_declarative(plan, invoker)
        assert res.ok is False
        assert calls == 0

    def test_source_naming_later_step(self):
        """Validator check: source must be defined strictly earlier (DAG check)."""
        plan = DeclarativePlan(
            steps=(
                FilterStep(name="step1", source="step2", field="x", op="eq", value=1),
                MapStep(name="step2", tool="t1", items=(1,)),
            ),
            output="step1",
        )
        reason = validate_plan(plan)
        assert "not defined before step" in reason

        calls = 0

        def invoker(tool, args):
            nonlocal calls
            calls += 1
            return {}

        res = run_declarative(plan, invoker)
        assert res.ok is False
        assert calls == 0

    def test_source_naming_unknown_step(self):
        """Validator check: source must refer to a defined step."""
        plan = DeclarativePlan(
            steps=(FilterStep(name="step1", source="unknown", field="x", op="eq", value=1),),
            output="step1",
        )
        reason = validate_plan(plan)
        assert "not defined before step" in reason

        calls = 0

        def invoker(tool, args):
            nonlocal calls
            calls += 1
            return {}

        res = run_declarative(plan, invoker)
        assert res.ok is False
        assert calls == 0

    def test_output_naming_unknown_step(self):
        """Validator check: output step must be defined."""
        plan = DeclarativePlan(
            steps=(MapStep(name="step1", tool="t1", items=(1,)),),
            output="unknown",
        )
        reason = validate_plan(plan)
        assert "not defined in the plan" in reason

        calls = 0

        def invoker(tool, args):
            nonlocal calls
            calls += 1
            return {}

        res = run_declarative(plan, invoker)
        assert res.ok is False
        assert calls == 0

    def test_empty_map_items(self):
        """Validator check: MapStep items must not be empty."""
        plan = DeclarativePlan(
            steps=(MapStep(name="step1", tool="t1", items=()),),
            output="step1",
        )
        reason = validate_plan(plan)
        assert "items cannot be empty" in reason

        calls = 0

        def invoker(tool, args):
            nonlocal calls
            calls += 1
            return {}

        res = run_declarative(plan, invoker)
        assert res.ok is False
        assert calls == 0

    def test_empty_map_tool(self):
        """Validator check: MapStep tool must not be empty."""
        plan = DeclarativePlan(
            steps=(MapStep(name="step1", tool="", items=(1,)),),
            output="step1",
        )
        reason = validate_plan(plan)
        assert "tool cannot be empty" in reason

    def test_aggregate_needing_field_but_given_none(self):
        """Validator check: sum/min/max/mean/group_by AggregateStep require a non-empty field."""
        plan = DeclarativePlan(
            steps=(
                MapStep(name="step1", tool="t1", items=(1,)),
                AggregateStep(name="step2", source="step1", op="sum", field=""),
            ),
            output="step2",
        )
        reason = validate_plan(plan)
        assert "requires a non-empty field" in reason

        calls = 0

        def invoker(tool, args):
            nonlocal calls
            calls += 1
            return {}

        res = run_declarative(plan, invoker)
        assert res.ok is False
        assert calls == 0


class TestDeclarativeRunner:
    """Test suite for declarative plan runner execution."""

    def test_map_step_execution(self, tmp_path):
        """Runner check: map step invokes tool plane per item in order with bound argument."""
        plan = DeclarativePlan(
            steps=(MapStep(name="step1", tool="my_tool", items=("a", "b", "c")),),
            output="step1",
        )
        calls = []

        def invoker(tool, args):
            calls.append((tool, args))
            return {"val": args["item"]}

        res = run_declarative(plan, invoker, artifact_dir=str(tmp_path))
        assert res.ok is True
        assert res.output == [{"val": "a"}, {"val": "b"}, {"val": "c"}]
        assert calls == [
            ("my_tool", {"item": "a"}),
            ("my_tool", {"item": "b"}),
            ("my_tool", {"item": "c"}),
        ]

    def test_map_filter_aggregate_count(self, tmp_path):
        """Runner check: map -> filter -> aggregate (count) pipeline works end-to-end."""
        plan = DeclarativePlan(
            steps=(
                MapStep(name="map1", tool="t1", items=(1, 2, 3, 4)),
                FilterStep(name="filter1", source="map1", field="val", op="gt", value=2),
                AggregateStep(name="agg1", source="filter1", op="count"),
            ),
            output="agg1",
        )

        def invoker(tool, args):
            return {"val": args["item"]}

        res = run_declarative(plan, invoker, artifact_dir=str(tmp_path))
        assert res.ok is True
        assert res.output == 2

    def test_aggregate_numeric_operations(self, tmp_path):
        """Runner check: sum, mean, min, max correctly skip non-numeric and None values."""
        plan = DeclarativePlan(
            steps=(
                MapStep(
                    name="map1",
                    tool="t1",
                    items=(
                        {"v": 10},
                        {"v": "not_numeric"},
                        {"v": None},
                        {"v": True},  # boolean should be skipped
                        {"v": 20},
                    ),
                ),
                AggregateStep(name="sum_step", source="map1", op="sum", field="v"),
                AggregateStep(name="mean_step", source="map1", op="mean", field="v"),
                AggregateStep(name="min_step", source="map1", op="min", field="v"),
                AggregateStep(name="max_step", source="map1", op="max", field="v"),
            ),
            output="sum_step",
        )

        def invoker(tool, args):
            return args["item"]

        # Sum step
        res = run_declarative(plan, invoker, artifact_dir=str(tmp_path))
        assert res.ok is True
        assert res.output == 30

        # Mean step
        plan_mean = DeclarativePlan(steps=plan.steps, output="mean_step")
        res_mean = run_declarative(plan_mean, invoker, artifact_dir=str(tmp_path))
        assert res_mean.output == 15.0

        # Min step
        plan_min = DeclarativePlan(steps=plan.steps, output="min_step")
        res_min = run_declarative(plan_min, invoker, artifact_dir=str(tmp_path))
        assert res_min.output == 10

        # Max step
        plan_max = DeclarativePlan(steps=plan.steps, output="max_step")
        res_max = run_declarative(plan_max, invoker, artifact_dir=str(tmp_path))
        assert res_max.output == 20

        # Sum of nothing is 0, Mean of nothing is None
        plan_empty = DeclarativePlan(
            steps=(
                MapStep(name="map_empty", tool="t1", items=({"v": "abc"},)),
                AggregateStep(name="sum_empty", source="map_empty", op="sum", field="v"),
                AggregateStep(name="mean_empty", source="map_empty", op="mean", field="v"),
            ),
            output="sum_empty",
        )
        res_sum_empty = run_declarative(plan_empty, invoker, artifact_dir=str(tmp_path))
        assert res_sum_empty.output == 0

        plan_mean_empty = DeclarativePlan(steps=plan_empty.steps, output="mean_empty")
        res_mean_empty = run_declarative(plan_mean_empty, invoker, artifact_dir=str(tmp_path))
        assert res_mean_empty.output is None

    def test_group_by_sorted(self, tmp_path):
        """Runner check: group_by returns key-sorted counts mapping."""
        plan = DeclarativePlan(
            steps=(
                MapStep(
                    name="map1",
                    tool="t1",
                    items=(
                        {"category": "banana"},
                        {"category": "apple"},
                        {"category": "banana"},
                        {"category": "cherry"},
                    ),
                ),
                AggregateStep(name="group1", source="map1", op="group_by", field="category"),
            ),
            output="group1",
        )

        def invoker(tool, args):
            return args["item"]

        res = run_declarative(plan, invoker, artifact_dir=str(tmp_path))
        assert res.ok is True
        assert res.output == {
            "apple": 1,
            "banana": 2,
            "cherry": 1,
        }
        assert list(res.output.keys()) == ["apple", "banana", "cherry"]

    def test_provenance_and_artifacts_on_disk(self, tmp_path):
        """Runner check: raw step intermediates are stored on disk, while outputs are condensed."""
        items = [{"id": i} for i in range(25)]
        plan = DeclarativePlan(
            steps=(MapStep(name="map1", tool="t1", items=tuple(items)),),
            output="map1",
        )

        def invoker(tool, args):
            return args["item"]

        res = run_declarative(plan, invoker, artifact_dir=str(tmp_path), run_id="my_run")
        assert res.ok is True

        # Returned output should be condensed due to length > 20
        assert isinstance(res.output, dict)
        assert res.output["count"] == 25
        assert res.output["truncated"] is True
        assert len(res.output["sample"]) == 5

        # Raw file should exist on disk and hold ALL elements
        assert "map1" in res.provenance
        artifact_path = Path(res.provenance["map1"])
        assert artifact_path.exists()
        assert artifact_path.is_absolute()

        with open(artifact_path, encoding="utf-8") as f:
            loaded_raw = json.load(f)
        assert len(loaded_raw) == 25
        assert loaded_raw == items

    def test_condense_helper(self):
        """Runner check: lists > 20 elements get condensed, smaller lists pass through."""
        from omniagentos.toolplane.programmatic import _condense

        short_list = [1, 2, 3]
        assert _condense(short_list) is short_list

        long_list = list(range(100))
        condensed = _condense(long_list)
        assert isinstance(condensed, dict)
        assert condensed["count"] == 100
        assert condensed["truncated"] is True
        assert condensed["sample"] == [0, 1, 2, 3, 4]

    def test_invoker_raises_exception(self, tmp_path):
        """Runner check: exception during execution is gracefully caught and returned."""
        plan = DeclarativePlan(
            steps=(MapStep(name="map1", tool="t1", items=("item0", "item1", "item2")),),
            output="map1",
        )
        calls = 0

        def invoker(tool, args):
            nonlocal calls
            calls += 1
            if args["item"] == "item1":
                raise ValueError("Failure on item 1")
            return {"val": args["item"]}

        res = run_declarative(plan, invoker, artifact_dir=str(tmp_path))
        assert res.ok is False
        assert "ValueError" in res.error
        assert "Failure on item 1" in res.error
        assert res.steps_run == ()
        assert calls == 2

    def test_invoker_raises_in_second_step(self, tmp_path):
        """Earlier steps' provenance survives a later step's failure.

        A partial run must still hand back where the completed work landed. Losing
        `map1`'s artifact because `map2` failed would mean paying for that work twice,
        and would hide the evidence needed to see WHY the second step failed.
        """
        plan = DeclarativePlan(
            steps=(
                MapStep(name="map1", tool="t1", items=(1, 2)),
                MapStep(name="map2", tool="t2", items=(3, 4)),
                AggregateStep(name="total", source="map2", op="count"),
            ),
            output="total",
        )

        def invoker(tool, args):
            if tool == "t2":
                raise ValueError("t2 is down")
            return {"x": args["item"]}

        res = run_declarative(plan, invoker, artifact_dir=str(tmp_path))
        assert res.ok is False
        assert "ValueError" in res.error
        assert "t2 is down" in res.error
        # The first step completed, so its artifact is on disk and named in provenance.
        assert res.steps_run == ("map1",)
        assert "map1" in res.provenance
        assert Path(res.provenance["map1"]).exists()
        assert "map2" not in res.steps_run

    def test_filtering_non_dict_records_yields_no_matches_rather_than_an_error(self, tmp_path):
        """Pins a decision that is easy to reach by accident: non-dict records filter OUT.

        `_field` traverses dicts only, so a record that is not a dict resolves to None
        and fails every comparison. The result is an empty list, NOT an error.

        This is deliberate but it is a sharp edge: a plan whose tool returns scalars will
        report "nothing matched" rather than "your filter does not fit your data". The
        raw artifact on disk is what distinguishes the two, which is one more reason the
        runner writes every step's raw output rather than only its condensed result.
        """
        plan = DeclarativePlan(
            steps=(
                MapStep(name="map1", tool="t1", items=(1, 2)),
                FilterStep(name="filter1", source="map1", field="x", op="eq", value=1),
            ),
            output="filter1",
        )

        res = run_declarative(plan, lambda tool, args: "not-a-dict", artifact_dir=str(tmp_path))
        assert res.ok is True
        assert res.output == []
        # The raw map output IS preserved, so an operator can see what really came back.
        raw = json.loads(Path(res.provenance["map1"]).read_text(encoding="utf-8"))
        assert raw == ["not-a-dict", "not-a-dict"]

    def test_field_traversal_security(self):
        """Runner check: dotted path traversal must NOT walk python object attributes."""
        from omniagentos.toolplane.programmatic import _field

        # Missing key -> None
        assert _field({"a": 1}, "b") is None

        # Non-dict midway -> None
        assert _field({"a": 1}, "a.b") is None

        # None midway -> None
        assert _field(None, "a.b") is None

        # Class instance with attribute -> None
        class SecretObject:
            def __init__(self):
                self.secret = "my_secret_token"

        obj = SecretObject()
        assert _field(obj, "secret") is None

    def test_plan_output_is_deterministic(self, tmp_path):
        """Runner check: plan execution output is completely deterministic."""
        plan = DeclarativePlan(
            steps=(
                MapStep(name="map1", tool="t1", items=(1, 2, 3)),
                FilterStep(name="filter1", source="map1", field="val", op="ne", value=2),
                AggregateStep(name="agg1", source="filter1", op="collect", field="val"),
            ),
            output="agg1",
        )

        def invoker(tool, args):
            return {"val": args["item"]}

        res1 = run_declarative(plan, invoker, artifact_dir=str(tmp_path), run_id="run1")
        res2 = run_declarative(plan, invoker, artifact_dir=str(tmp_path), run_id="run2")
        assert res1.ok is True
        assert res2.ok is True
        assert res1.output == res2.output
