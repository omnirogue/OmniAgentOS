"""Fan-out width: independent_units must reflect DAG antichain width, not root layer only."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from omniagentos.swarm.planner import _compute_disjoint_dag_width, build_plan


def _raw_task(
    task_id: str,
    deps: tuple[str, ...] = (),
    paths: tuple[str, ...] = (),
    agent: int = 10,
    manual: int | None = None,
    **overrides: Any,
) -> dict[str, Any]:
    if manual is None:
        manual = agent * 3
    base: dict[str, Any] = {
        "id": task_id,
        "title": task_id.upper(),
        "description": f"do {task_id}",
        "depends_on": list(deps),
        "owned_paths": list(paths),
        "est_agent_minutes": agent,
        "est_manual_minutes": manual,
        "acceptance": f"{task_id} done",
        "verify_command": f"pytest tests/{task_id}",
    }
    base.update(overrides)
    return base


def _lane4_tasks() -> list[dict[str, Any]]:
    """Paired A/B campaign runner shape: one root, five mutually independent tests."""
    return [
        _raw_task("contract", paths=("var/contract",), agent=12, manual=36),
        _raw_task(
            "runner-core",
            deps=("contract",),
            paths=("scripts/campaign.py",),
            agent=55,
            manual=165,
        ),
        _raw_task(
            "test-both-arms",
            deps=("contract", "runner-core"),
            paths=("tests/t1.py",),
            agent=25,
            manual=75,
        ),
        _raw_task(
            "test-counterbalance",
            deps=("contract", "runner-core"),
            paths=("tests/t2.py",),
            agent=30,
            manual=90,
        ),
        _raw_task(
            "test-paired-inputs",
            deps=("contract", "runner-core"),
            paths=("tests/t3.py",),
            agent=32,
            manual=96,
        ),
        _raw_task(
            "test-unpaired",
            deps=("contract", "runner-core"),
            paths=("tests/t4.py",),
            agent=30,
            manual=90,
        ),
        _raw_task(
            "test-flags",
            deps=("contract", "runner-core"),
            paths=("tests/t5.py",),
            agent=26,
            manual=78,
        ),
    ]


def _allocation_note(plan: Any) -> str:
    for note in plan.assumptions:
        if note.startswith("allocation:"):
            return note
    raise AssertionError(f"no allocation assumption in {plan.assumptions!r}")


def _parse_allocation_fields(note: str) -> dict[str, int]:
    """Pull workers=N and hard_cap=N from the allocation assumption line."""
    fields: dict[str, int] = {}
    for token in note.replace(",", " ").split():
        if token.startswith("workers=") or token.startswith("hard_cap="):
            key, _, raw = token.partition("=")
            # strip trailing punctuation attached to the value
            raw = raw.rstrip(".,;")
            fields[key] = int(raw)
    return fields


class TestPlannerFanoutWidth:
    def test_single_root_fanout_reaches_target_two(self) -> None:
        plan = build_plan("Paired A/B campaign runner", _lane4_tasks())
        assert plan.mode == "swarm"
        assert plan.parallelism_ratio == pytest.approx(2.121)
        assert plan.target_n == 2
        for note in plan.assumptions:
            assert not note.startswith("fanout_cap:"), note

    def test_single_root_fanout_allocation_sees_real_width(self) -> None:
        plan = build_plan("Paired A/B campaign runner", _lane4_tasks())
        note = _allocation_note(plan)
        fields = _parse_allocation_fields(note)
        assert fields.get("workers", 0) >= 2, note
        assert fields.get("hard_cap", 0) >= 2, note

    def test_strict_serial_chain_still_targets_one(self) -> None:
        tasks = [
            _raw_task("a", paths=("src/a",), agent=10),
            _raw_task("b", deps=("a",), paths=("src/b",), agent=10),
            _raw_task("c", deps=("b",), paths=("src/c",), agent=10),
            _raw_task("d", deps=("c",), paths=("src/d",), agent=10),
            _raw_task("e", deps=("d",), paths=("src/e",), agent=10),
        ]
        plan = build_plan("strict serial chain", tasks)
        assert plan.parallelism_ratio == pytest.approx(1.0)
        assert plan.mode == "solo"
        assert plan.target_n == 1

    def test_wide_serial_tail_not_widened(self) -> None:
        # Three roots, but almost all minutes sit on one long serial tail after
        # root a. Expected ratio 330/310 ≈ 1.065 — below SOLO_RATIO_THRESHOLD.
        tasks = [
            _raw_task("a", paths=("src/a",), agent=10),
            _raw_task("b", paths=("src/b",), agent=10),
            _raw_task("c", paths=("src/c",), agent=10),
            _raw_task("d", deps=("a",), paths=("src/d",), agent=100),
            _raw_task("e", deps=("d",), paths=("src/e",), agent=100),
            _raw_task("f", deps=("e",), paths=("src/f",), agent=100),
        ]
        plan = build_plan("wide roots serial tail", tasks)
        # plan stores round(ratio, 3); 330/310 → 1.065
        assert plan.parallelism_ratio == pytest.approx(1.065)
        assert plan.parallelism_ratio < 1.5
        assert plan.mode == "solo"
        assert plan.target_n == 1

    def test_dag_width_is_max_antichain_not_root_count(self) -> None:
        tasks = [
            SimpleNamespace(id="contract", depends_on=()),
            SimpleNamespace(id="runner-core", depends_on=("contract",)),
            SimpleNamespace(id="test-both-arms", depends_on=("contract", "runner-core")),
            SimpleNamespace(id="test-counterbalance", depends_on=("contract", "runner-core")),
            SimpleNamespace(id="test-paired-inputs", depends_on=("contract", "runner-core")),
            SimpleNamespace(id="test-unpaired", depends_on=("contract", "runner-core")),
            SimpleNamespace(id="test-flags", depends_on=("contract", "runner-core")),
        ]
        root_count = sum(1 for t in tasks if not t.depends_on)
        assert root_count == 1
        assert _compute_disjoint_dag_width(tasks) == 5

    def test_chain_width_is_one(self) -> None:
        """Strict chain a→b→c→d→e has antichain width 1; fan-out cannot widen it."""
        tasks = [
            SimpleNamespace(id="a", depends_on=()),
            SimpleNamespace(id="b", depends_on=("a",)),
            SimpleNamespace(id="c", depends_on=("b",)),
            SimpleNamespace(id="d", depends_on=("c",)),
            SimpleNamespace(id="e", depends_on=("d",)),
        ]
        assert _compute_disjoint_dag_width(tasks) == 1

    @pytest.mark.parametrize(
        "shape_id,raw_tasks,expected_width",
        [
            (
                "chain",
                [
                    _raw_task("a", paths=("src/a",), agent=10),
                    _raw_task("b", deps=("a",), paths=("src/b",), agent=10),
                    _raw_task("c", deps=("b",), paths=("src/c",), agent=10),
                    _raw_task("d", deps=("c",), paths=("src/d",), agent=10),
                    _raw_task("e", deps=("d",), paths=("src/e",), agent=10),
                ],
                1,
            ),
            (
                "lane4",
                _lane4_tasks(),
                5,
            ),
            (
                "two-parallel-chains",
                [
                    _raw_task("a", paths=("src/a",), agent=30),
                    _raw_task("b", deps=("a",), paths=("src/b",), agent=30),
                    _raw_task("c", deps=("b",), paths=("src/c",), agent=30),
                    _raw_task("d", paths=("src/d",), agent=30),
                    _raw_task("e", deps=("d",), paths=("src/e",), agent=30),
                    _raw_task("f", deps=("e",), paths=("src/f",), agent=30),
                ],
                2,
            ),
            (
                "diamond",
                [
                    _raw_task("a", paths=("src/a",), agent=10),
                    _raw_task("b", deps=("a",), paths=("src/b",), agent=10),
                    _raw_task("c", deps=("a",), paths=("src/c",), agent=10),
                    _raw_task("d", deps=("b", "c"), paths=("src/d",), agent=10),
                ],
                2,
            ),
        ],
        ids=["chain", "lane4", "two-parallel-chains", "diamond"],
    )
    def test_target_n_never_exceeds_dag_width(
        self,
        shape_id: str,
        raw_tasks: list[dict[str, Any]],
        expected_width: int,
    ) -> None:
        """target_n must not exceed DAG width computed from the raw worker specs."""
        width_specs = [
            SimpleNamespace(id=t["id"], depends_on=tuple(t.get("depends_on") or ()))
            for t in raw_tasks
        ]
        width = _compute_disjoint_dag_width(width_specs)
        assert width == expected_width, f"{shape_id}: width={width}"

        plan = build_plan(f"fanout width property: {shape_id}", raw_tasks)
        # Report measured values for the operator (pytest -s shows print).
        print(
            f"shape={shape_id} width={width} target_n={plan.target_n} "
            f"mode={plan.mode} ratio={plan.parallelism_ratio}"
        )
        assert plan.target_n >= 1
        assert plan.target_n <= width, (
            f"{shape_id}: target_n={plan.target_n} exceeds width={width} "
            f"(mode={plan.mode}, ratio={plan.parallelism_ratio})"
        )
