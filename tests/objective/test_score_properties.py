"""W4-01/W4-03: ordering and null-discipline for the swarm objective."""

from __future__ import annotations

import ast
import inspect
import json
from dataclasses import dataclass
from pathlib import Path

import pytest

from omniagentos.collab.store import CollabStore
from omniagentos.swarm import optimize, summary
from omniagentos.swarm.dal import SwarmDal
from tests.objective.corpus import (
    SynthRun,
    completed_run,
    independent_tasks,
    synth_run,
    zero_work_run,
)


@pytest.fixture
def dal(tmp_path: Path) -> SwarmDal:
    db_path = str(tmp_path / "objective.db")
    CollabStore(db_path)
    opened = SwarmDal(db_path)
    try:
        yield opened
    finally:
        opened.close()


@dataclass(frozen=True)
class DominancePair:
    less: SynthRun
    more: SynthRun


DOMINANCE_CORPUS = (
    pytest.param(
        DominancePair(
            less=zero_work_run(wall_minutes=1),
            more=completed_run(1, wall_minutes=120, target_concurrency=1),
        ),
        id="zero-work-vs-slow-real-work",
    ),
    pytest.param(
        DominancePair(
            less=completed_run(1, wall_minutes=10, target_concurrency=1),
            more=completed_run(2, wall_minutes=10, target_concurrency=4),
        ),
        id="one-task-vs-two-tasks",
    ),
    pytest.param(
        DominancePair(
            less=completed_run(1, wall_minutes=10, target_concurrency=1),
            more=completed_run(4, wall_minutes=10, target_concurrency=8),
        ),
        id="one-task-vs-four-tasks",
    ),
    pytest.param(
        DominancePair(
            less=completed_run(2, wall_minutes=10, target_concurrency=2),
            more=completed_run(4, wall_minutes=10, target_concurrency=8),
        ),
        id="two-tasks-vs-four-tasks",
    ),
)


@pytest.mark.parametrize("pair", DOMINANCE_CORPUS)
def test_more_work_never_scores_lower(dal: SwarmDal, pair: DominancePair) -> None:
    less_axes = pair.less.work_axes
    more_axes = pair.more.work_axes
    assert all(more > less for less, more in zip(less_axes, more_axes, strict=True)), (
        f"bad dominance fixture: less={less_axes}, more={more_axes}"
    )

    less_metrics = summary.compute_metrics(synth_run(dal, pair.less), dal)
    more_metrics = summary.compute_metrics(synth_run(dal, pair.more), dal)

    assert more_metrics["score"] >= less_metrics["score"], (
        "objective prefers the run that did strictly less work: "
        f"less={less_metrics}, more={more_metrics}"
    )


def _emitted_ratio_fields() -> set[str]:
    """Derive rate fields from the function's data flow and emitted dictionary."""
    tree = ast.parse(inspect.getsource(summary.compute_metrics))
    function = next(node for node in tree.body if isinstance(node, ast.FunctionDef))

    ratio_accumulators: set[str] = set()
    for node in ast.walk(function):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "append"
            and isinstance(node.func.value, ast.Name)
            and node.args
            and any(isinstance(child, ast.Div) for child in ast.walk(node.args[0]))
        ):
            ratio_accumulators.add(node.func.value.id)

    def contains_ratio(expression: ast.AST) -> bool:
        for child in ast.walk(expression):
            if isinstance(child, ast.Div):
                return True
            if (
                isinstance(child, ast.Call)
                and isinstance(child.func, ast.Name)
                and child.func.id in {"_ratio_or_none", "_rate_or_none"}
            ):
                return True
        referenced = {
            child.id for child in ast.walk(expression) if isinstance(child, ast.Name)
        }
        return bool(referenced & ratio_accumulators)

    ratio_locals: set[str] = set()
    metrics_dict: ast.Dict | None = None
    for node in ast.walk(function):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and contains_ratio(node.value):
                    ratio_locals.add(target.id)
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            if node.value is not None and contains_ratio(node.value):
                ratio_locals.add(node.target.id)
            if node.target.id == "metrics" and isinstance(node.value, ast.Dict):
                metrics_dict = node.value

    assert metrics_dict is not None, "compute_metrics no longer emits a literal metrics mapping"
    emitted: set[str] = set()
    for key_node, value_node in zip(metrics_dict.keys, metrics_dict.values, strict=True):
        if not isinstance(key_node, ast.Constant) or not isinstance(key_node.value, str):
            continue
        referenced = {
            child.id for child in ast.walk(value_node) if isinstance(child, ast.Name)
        }
        if referenced & ratio_locals:
            emitted.add(key_node.value)
    assert emitted, "module walk found no emitted rates; the null-discipline test is blind"
    return emitted


def test_empty_denominator_rates_are_null_and_unrewarded(dal: SwarmDal) -> None:
    metrics = summary.compute_metrics(synth_run(dal, zero_work_run()), dal)
    ratio_fields = _emitted_ratio_fields()

    for field in sorted(ratio_fields):
        assert metrics[field] is None, (
            f"{field} defaulted to {metrics[field]!r} on an empty denominator"
        )
    assert metrics["score"] <= 5.0

    round_tripped = json.loads(json.dumps(metrics))
    for field in ratio_fields:
        assert round_tripped[field] is None

    sizing = optimize._aggregate_sizing([{"metrics_json": json.dumps(metrics)}])
    bucket = sizing["small (1-3 tasks)"]
    assert bucket["runs"] == 1
    assert bucket["median_utilization"] is None


def test_utilization_measures_realized_parallelism_not_requested_width(
    dal: SwarmDal,
) -> None:
    tasks = tuple(independent_tasks(8))
    serial = SynthRun(tasks=tasks, wall_seconds=240 * 60, target_concurrency=1)
    parallel = SynthRun(tasks=tasks, wall_seconds=60 * 60, target_concurrency=4)
    oversized = SynthRun(tasks=tasks, wall_seconds=240 * 60, target_concurrency=8)

    serial_metrics = summary.compute_metrics(synth_run(dal, serial), dal)
    parallel_metrics = summary.compute_metrics(synth_run(dal, parallel), dal)
    oversized_metrics = summary.compute_metrics(synth_run(dal, oversized), dal)

    assert parallel_metrics["utilization"] > serial_metrics["utilization"]
    assert oversized_metrics["utilization"] == pytest.approx(
        serial_metrics["utilization"]
    )


def test_zero_work_is_the_degenerate_dominance_case(dal: SwarmDal) -> None:
    empty = summary.compute_metrics(synth_run(dal, zero_work_run()), dal)
    for task_count in (1, 2, 4):
        worked = summary.compute_metrics(
            synth_run(
                dal,
                completed_run(
                    task_count,
                    wall_minutes=120,
                    target_concurrency=min(task_count, 4),
                ),
            ),
            dal,
        )
        assert worked["score"] >= empty["score"]
