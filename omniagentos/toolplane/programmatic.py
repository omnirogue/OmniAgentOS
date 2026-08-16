"""Programmatic-call router and declarative plan runner.

The Boundary
============
Programmatic execution is worth it when the model's judgement adds nothing between
calls, and harmful when it adds everything. Routing to ``programmatic``:

**YES** — fan-out over many independent items; large intermediates that want
aggregating or filtering before they reach the context; deterministic chains of
three or more calls; parallel I/O.

**NO** — one or two calls (the machinery costs more than it saves); reasoning
genuinely needed after each result; user feedback expected between calls; effects
that are unscoped or non-idempotent (a declarative runner retries and re-runs,
and neither is safe on an effect nobody has bounded).


The Hard Rule
=============
**THE HARD RULE: this runner executes DATA, never code.** There is no ``eval``,
no ``exec``, no import by name, no callable loaded from a plan, and no operator
outside the closed sets below. A plan is a tuple of typed steps; anything a
plan cannot express is something the caller must do itself. That is the entire
security argument for letting a model author one.


Raw Intermediates on Disk
=========================
Raw intermediates go to disk; only condensed results come back. This ensures
that large collections of data processed in mapping or filtering steps do not
bloat the agent's LLM context window.
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from omniagentos.connectors import ResultSizeClass
from omniagentos.toolplane.catalog import CatalogEntry, default_catalog
from omniagentos.toolplane.scheduler import ToolCall, resource_keys_for

__all__ = [
    "AggregateOp",
    "AggregateStep",
    "DEFAULT_ARTIFACT_ROOT",
    "DeclarativePlan",
    "DeclarativeStep",
    "FANOUT_THRESHOLD",
    "FilterOp",
    "FilterStep",
    "MIN_PROGRAMMATIC_CALLS",
    "MapStep",
    "ProgrammaticResult",
    "RoutingContext",
    "RoutingDecision",
    "ToolInvoker",
    "route_programmatic",
    "run_declarative",
    "validate_plan",
]

MIN_PROGRAMMATIC_CALLS = 3
FANOUT_THRESHOLD = 4
DEFAULT_ARTIFACT_ROOT = "var/toolplane/programmatic"
MAX_INLINE_ITEMS = 20


@dataclass(frozen=True, slots=True)
class RoutingContext:
    """What the caller knows that the call list alone cannot say."""

    reasoning_needed_after_each_result: bool = False
    user_feedback_between_calls: bool = False
    scoped: bool = True  # the caller has proven scope for any effect these calls have
    run_id: str = ""


@dataclass(frozen=True, slots=True)
class RoutingDecision:
    """The decision of whether to route programmatic or conversational."""

    route: Literal["programmatic", "conversational"]
    reason: str
    evidence: tuple[str, ...]


def route_programmatic(
    calls: Sequence[ToolCall],
    ctx: RoutingContext,
    catalog: Mapping[str, CatalogEntry] | None = None,
) -> RoutingDecision:
    """Decide whether a list of calls should be executed programmatically or conversationally.

    First, checks all disqualifiers. If any disqualifier matches, the route is
    instantly conversational.
    Next, checks all qualifiers to gather evidence. If any qualifier matches,
    the route is programmatic.
    Otherwise, falls back to conversational with a reason of 'no-qualifying-pattern'.
    """
    resolved = catalog if catalog is not None else default_catalog()

    # --- DISQUALIFIERS FIRST ---
    # 1. Too few calls
    if len(calls) < MIN_PROGRAMMATIC_CALLS:
        return RoutingDecision(route="conversational", reason="too-few-calls", evidence=())

    # 2. Reasoning needed after each result
    if ctx.reasoning_needed_after_each_result:
        return RoutingDecision(
            route="conversational", reason="reasoning-between-calls", evidence=()
        )

    # 3. User feedback needed between calls
    if ctx.user_feedback_between_calls:
        return RoutingDecision(
            route="conversational", reason="user-feedback-between-calls", evidence=()
        )

    # 4. Unclassified calls
    for call in calls:
        if call.tool not in resolved:
            return RoutingDecision(route="conversational", reason="unclassified-call", evidence=())
        entry = resolved[call.tool]
        if not entry.classified:
            return RoutingDecision(route="conversational", reason="unclassified-call", evidence=())

    # 5. Unscoped or non-idempotent effects
    for call in calls:
        entry = resolved[call.tool]
        if not entry.read_only:
            if (not ctx.scoped) or (not entry.idempotent):
                return RoutingDecision(
                    route="conversational",
                    reason="unscoped-or-non-idempotent-effects",
                    evidence=(),
                )

    # --- QUALIFIERS ---
    evidence_list: list[str] = []

    # Qualifier: fan-out
    tool_counts: dict[str, int] = {}
    for call in calls:
        tool_counts[call.tool] = tool_counts.get(call.tool, 0) + 1
    if any(count >= FANOUT_THRESHOLD for count in tool_counts.values()):
        evidence_list.append("fan-out")

    # Qualifier: large-intermediates
    has_large = False
    for call in calls:
        entry = resolved[call.tool]
        if entry.result_size_class in (ResultSizeClass.LARGE, ResultSizeClass.STREAMING):
            has_large = True
            break
    if has_large:
        evidence_list.append("large-intermediates")

    # Qualifier: deterministic-chain
    all_read_or_idempotent = True
    for call in calls:
        entry = resolved[call.tool]
        if not (entry.read_only or entry.idempotent):
            all_read_or_idempotent = False
            break
    if all_read_or_idempotent:
        evidence_list.append("deterministic-chain")

    # Qualifier: parallel-io
    # At least two read_only calls whose resource_keys_for(...) are disjoint
    read_only_calls = [call for call in calls if resolved[call.tool].read_only]
    has_parallel_io = False
    if len(read_only_calls) >= 2:
        resource_sets = [set(resource_keys_for(call, resolved)) for call in read_only_calls]
        # Check all pairs
        for i in range(len(resource_sets)):
            for j in range(i + 1, len(resource_sets)):
                if resource_sets[i].isdisjoint(resource_sets[j]):
                    has_parallel_io = True
                    break
            if has_parallel_io:
                break
    if has_parallel_io:
        evidence_list.append("parallel-io")

    if evidence_list:
        return RoutingDecision(
            route="programmatic",
            reason=evidence_list[0],
            evidence=tuple(evidence_list),
        )

    return RoutingDecision(route="conversational", reason="no-qualifying-pattern", evidence=())


# --- PART B ---

FilterOp = Literal["eq", "ne", "lt", "lte", "gt", "gte", "contains", "exists", "truthy"]
AggregateOp = Literal["count", "sum", "min", "max", "mean", "collect", "group_by"]


@dataclass(frozen=True, slots=True)
class MapStep:
    """One tool call per item.

    The item is bound to `arg_key` in the call arguments.
    """

    name: str
    tool: str
    items: tuple[Any, ...]
    arg_key: str = "item"
    extra_args: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class FilterStep:
    """Filter records from a source step based on a comparison operator."""

    name: str
    source: str
    field: str  # dotted path, dicts only
    op: FilterOp
    value: Any = None


@dataclass(frozen=True, slots=True)
class AggregateStep:
    """Aggregate a source list step into a single value or mapped structure."""

    name: str
    source: str
    op: AggregateOp
    field: str = ""


DeclarativeStep = MapStep | FilterStep | AggregateStep


@dataclass(frozen=True, slots=True)
class DeclarativePlan:
    """A series of non-looping steps culminating in a condensed output step."""

    steps: tuple[DeclarativeStep, ...]
    output: str  # name of the step whose CONDENSED result is returned


@dataclass(frozen=True, slots=True)
class ProgrammaticResult:
    """The result of executing a DeclarativePlan."""

    ok: bool
    output: Any
    provenance: Mapping[str, str]  # step name -> absolute artifact path
    steps_run: tuple[str, ...] = ()
    error: str = ""


ToolInvoker = Callable[[str, dict[str, Any]], Any]


def validate_plan(plan: DeclarativePlan) -> str:
    """Validate a DeclarativePlan before execution.

    Returns an empty string ("") if the plan is valid, or a descriptive reason
    explaining why it is invalid.
    """
    if not plan.steps:
        return "Plan must contain at least one step."

    seen_names = set()
    for step in plan.steps:
        if not isinstance(step, (MapStep, FilterStep, AggregateStep)):
            return f"Unknown step type: {type(step).__name__}"

        if not step.name:
            return "Step name cannot be empty."

        if step.name in seen_names:
            return f"Duplicate step name: {step.name}"

        if isinstance(step, MapStep):
            if not step.tool:
                return f"MapStep '{step.name}' tool cannot be empty."
            if not step.items:
                return f"MapStep '{step.name}' items cannot be empty."

        elif isinstance(step, (FilterStep, AggregateStep)):
            if step.source not in seen_names:
                return f"Source '{step.source}' not defined before step '{step.name}'."

            if isinstance(step, FilterStep):
                valid_filter_ops = {
                    "eq",
                    "ne",
                    "lt",
                    "lte",
                    "gt",
                    "gte",
                    "contains",
                    "exists",
                    "truthy",
                }
                if step.op not in valid_filter_ops:
                    return f"Invalid filter operator '{step.op}' in step '{step.name}'."

            elif isinstance(step, AggregateStep):
                valid_aggregate_ops = {
                    "count",
                    "sum",
                    "min",
                    "max",
                    "mean",
                    "collect",
                    "group_by",
                }
                if step.op not in valid_aggregate_ops:
                    return f"Invalid aggregate operator '{step.op}' in step '{step.name}'."
                if step.op in {"sum", "min", "max", "mean", "group_by"}:
                    if not step.field:
                        return f"Aggregate operator '{step.op}' in step '{step.name}' requires a non-empty field."

        seen_names.add(step.name)

    if plan.output not in seen_names:
        return f"Output step '{plan.output}' is not defined in the plan."

    return ""


def _field(record: Any, path: str) -> Any:
    """Traverse a dotted path over plain dicts only.

    No getattr is used here, to prevent any traversal or access of python
    object attributes, ensuring strict security boundaries.
    """
    # SECURITY WARNING: Do NOT use getattr or walk object attributes here.
    # Walking object attributes is a massive escape hatch that allows arbitrary code execution or leakage.
    # Dotted traversal MUST be restricted to plain dict keys only.
    if not path:
        return record

    parts = path.split(".")
    curr = record
    for part in parts:
        if not isinstance(curr, dict):
            return None
        if part not in curr:
            return None
        curr = curr[part]
    return curr


def _compare(val: Any, op: FilterOp, expected: Any) -> bool:
    """Compare a field value against an expected value using a FilterOp."""
    try:
        if op == "eq":
            return val == expected
        elif op == "ne":
            return val != expected
        elif op == "lt":
            if val is None:
                return False
            return val < expected
        elif op == "lte":
            if val is None:
                return False
            return val <= expected
        elif op == "gt":
            if val is None:
                return False
            return val > expected
        elif op == "gte":
            if val is None:
                return False
            return val >= expected
        elif op == "contains":
            if val is None:
                return False
            return expected in val
        elif op == "exists":
            return val is not None
        elif op == "truthy":
            return bool(val)
    except TypeError:
        return False
    return False


def _condense(value: Any) -> Any:
    """Condense a result value for inline return.

    A scalar, dict, or short list (<= 20 items) passes through as-is.
    A list longer than 20 items is truncated and returned as a summary dict
    containing its count, truncated flag, and a sample of the first 5 items,
    ensuring large intermediates do not overwhelm the caller's context.
    """
    if isinstance(value, list) and len(value) > MAX_INLINE_ITEMS:
        return {
            "count": len(value),
            "truncated": True,
            "sample": value[:5],
        }
    return value


def _sanitize_step_name(name: str) -> str:
    """Sanitize the step name for safe filesystem usage."""
    return re.sub(r"[^A-Za-z0-9_.-]", "-", name)


def _atomic_write(filepath: Path, payload: Any) -> None:
    """Write any JSON-serializable payload atomically via temp file + fsync + replace."""
    temp_path = filepath.with_name(f"{filepath.name}.{os.getpid()}.tmp")
    try:
        with open(temp_path, "w", encoding="utf-8") as f:
            json.dump(payload, f, separators=(",", ":"), sort_keys=True)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(temp_path, filepath)
    except Exception:
        try:
            temp_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def run_declarative(
    plan: DeclarativePlan,
    invoke: ToolInvoker,
    *,
    artifact_dir: str | None = None,
    run_id: str = "",
) -> ProgrammaticResult:
    """Execute a DeclarativePlan.

    Validates the plan, prepares directories, executes steps sequentially,
    atomically writes raw intermediates to disk, and keeps results in memory
    only as long as dependent steps require them.

    Never raises; all exceptions are caught and returned as a failed ProgrammaticResult.
    """
    reason = validate_plan(plan)
    if reason:
        return ProgrammaticResult(ok=False, output=None, provenance={}, error=reason)

    artifact_base = Path(artifact_dir or DEFAULT_ARTIFACT_ROOT)
    run_dir_name = run_id if run_id else "run"
    artifact_path = artifact_base / run_dir_name

    try:
        artifact_path.mkdir(parents=True, exist_ok=True)
    except Exception as exc:
        return ProgrammaticResult(
            ok=False,
            output=None,
            provenance={},
            steps_run=(),
            error=f"{type(exc).__name__}: {exc}",
        )

    # Track dependencies to clear memory as soon as intermediates are no longer needed
    dependents: dict[str, set[str]] = {step.name: set() for step in plan.steps}
    for step in plan.steps:
        if isinstance(step, (FilterStep, AggregateStep)):
            if step.source in dependents:
                dependents[step.source].add(step.name)

    # Ensure output step's raw intermediate stays in memory until we exit
    if plan.output in dependents:
        dependents[plan.output].add("__output__")

    pending_dependents = {k: set(v) for k, v in dependents.items()}

    step_results: dict[str, Any] = {}
    completed_steps: list[str] = []
    provenance: dict[str, str] = {}

    try:
        for step in plan.steps:
            # A step's raw result is a list for map/filter but a scalar or a mapping for
            # the aggregate operators, so it is deliberately untyped here rather than
            # narrowed per branch. What it must satisfy is that it is JSON-serialisable,
            # which _atomic_write enforces at the point it matters.
            step_result: Any
            if isinstance(step, MapStep):
                step_result = []
                for item in step.items:
                    args = dict(step.extra_args)
                    args[step.arg_key] = item
                    res = invoke(step.tool, args)
                    step_result.append(res)

            elif isinstance(step, FilterStep):
                source_res = step_results.get(step.source)
                if source_res is None:
                    raise ValueError(f"Source '{step.source}' result is missing from memory.")
                if not isinstance(source_res, list):
                    raise TypeError(
                        f"Source '{step.source}' result must be a list for FilterStep, got {type(source_res).__name__}"
                    )

                step_result = []
                for record in source_res:
                    field_val = _field(record, step.field)
                    if _compare(field_val, step.op, step.value):
                        step_result.append(record)

            elif isinstance(step, AggregateStep):
                source_res = step_results.get(step.source)
                if source_res is None:
                    raise ValueError(f"Source '{step.source}' result is missing from memory.")
                if not isinstance(source_res, list):
                    raise TypeError(
                        f"Source '{step.source}' result must be a list for AggregateStep, got {type(source_res).__name__}"
                    )

                if step.op == "count":
                    step_result = len(source_res)
                elif step.op == "collect":
                    if not step.field:
                        step_result = list(source_res)
                    else:
                        step_result = [_field(record, step.field) for record in source_res]
                elif step.op in ("sum", "min", "max", "mean"):
                    numeric_values = []
                    for record in source_res:
                        val = _field(record, step.field)
                        if (
                            val is not None
                            and isinstance(val, (int, float))
                            and not isinstance(val, bool)
                        ):
                            numeric_values.append(val)

                    if step.op == "sum":
                        step_result = sum(numeric_values) if numeric_values else 0
                    elif step.op == "mean":
                        step_result = (
                            sum(numeric_values) / len(numeric_values) if numeric_values else None
                        )
                    elif step.op == "min":
                        step_result = min(numeric_values) if numeric_values else None
                    elif step.op == "max":
                        step_result = max(numeric_values) if numeric_values else None
                elif step.op == "group_by":
                    counts: dict[str, int] = {}
                    for record in source_res:
                        val = _field(record, step.field)
                        val_str = str(val)
                        counts[val_str] = counts.get(val_str, 0) + 1
                    step_result = {k: counts[k] for k in sorted(counts.keys())}
                else:
                    raise ValueError(f"Unsupported aggregate operator: {step.op}")

            # Write RAW results atomically
            sanitized_name = _sanitize_step_name(step.name)
            filepath = (artifact_path / f"{sanitized_name}.json").resolve()
            _atomic_write(filepath, step_result)

            # Store result in memory, record provenance and run history
            step_results[step.name] = step_result
            provenance[step.name] = str(filepath)
            completed_steps.append(step.name)

            # Clean up memory immediately if all dependent steps have finished consuming it
            if isinstance(step, (FilterStep, AggregateStep)):
                src = step.source
                if src in pending_dependents and step.name in pending_dependents[src]:
                    pending_dependents[src].remove(step.name)
                    if not pending_dependents[src]:
                        step_results.pop(src, None)

        # Retrieve the output step's raw result, condense it and return
        output_val = step_results.get(plan.output)
        condensed_output = _condense(output_val)

        return ProgrammaticResult(
            ok=True,
            output=condensed_output,
            provenance=provenance,
            steps_run=tuple(completed_steps),
            error="",
        )

    except Exception as exc:
        return ProgrammaticResult(
            ok=False,
            output=None,
            provenance=provenance,
            steps_run=tuple(completed_steps),
            error=f"{type(exc).__name__}: {exc}",
        )
