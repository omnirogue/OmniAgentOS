"""Synthetic DAG policy simulator — compare fan-out choices without live agents."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from omniagentos.allocation.audit import audit_decomposition
from omniagentos.allocation.capacity import CapacitySnapshot, capacity_from_mapping
from omniagentos.allocation.characterize import characterize
from omniagentos.allocation.fanout import decide_fanout
from omniagentos.swarm.planner import _compute_disjoint_dag_width


@dataclass(frozen=True, slots=True)
class SimResult:
    task_count: int
    audit_ok: bool
    topology: str
    worker_count: int
    verifier_count: int
    hard_capacity: int
    rationale: str
    idle_spawn: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_count": self.task_count,
            "audit_ok": self.audit_ok,
            "topology": self.topology,
            "worker_count": self.worker_count,
            "verifier_count": self.verifier_count,
            "hard_capacity": self.hard_capacity,
            "rationale": self.rationale,
            "idle_spawn": self.idle_spawn,
        }


def simulate_fanout(
    tasks: Sequence[Mapping[str, Any]],
    capacity: CapacitySnapshot | Mapping[str, Any] | None = None,
) -> SimResult:
    """Run audit + characterize + decide_fanout on a synthetic task list.

    Parallel capacity is the DAG's maximum antichain width (Dilworth), not the
    root-layer size. Root count alone misclassifies single-root/wide-body DAGs
    (one contract then N independent siblings) as sequential.
    """
    audit = audit_decomposition(tasks)
    cap = (
        capacity
        if isinstance(capacity, CapacitySnapshot)
        else capacity_from_mapping(capacity or {})
    )
    n = max(1, len(tasks))
    # Reuse planner measure — do not invent a second width algorithm.
    dag_width = max(1, int(_compute_disjoint_dag_width(tasks)))
    has_partitions = dag_width > 1
    char = characterize(
        {
            "has_partitions": has_partitions,
            "sequential": not has_partitions and n <= 2,
            "uncertainty": 0.3,
            "risk": 0.4,
            "verifiable": 0.8,
            "independent_units": dag_width,
            "work_volume": float(n),
        }
    )
    decision = decide_fanout(
        char,
        free_slots=cap.global_free_slots,
        writer_slots=cap.repository_writer_slots,
        verifier_capacity=cap.verifier_absorption,
        independent_units=dag_width,
        budget_workers=cap.budget_workers,
        disjoint_dag_width=dag_width,
    )
    idle = decision.worker_count > dag_width and not has_partitions
    return SimResult(
        task_count=n,
        audit_ok=audit.ok,
        topology=str(decision.topology),
        worker_count=int(decision.worker_count),
        verifier_count=int(decision.verifier_count),
        hard_capacity=int(decision.hard_capacity),
        rationale=str(decision.rationale),
        idle_spawn=idle,
    )


def fixture_suite() -> list[dict[str, Any]]:
    """Built-in Volume-III style fixtures for regression."""
    return [
        {
            "name": "trivial",
            "tasks": [{"id": "t1", "title": "one step", "acceptance": "done"}],
            "expect_max_workers": 1,
        },
        {
            "name": "partitionable",
            "tasks": [
                {"id": "a", "title": "mod a", "acceptance": "tests", "owned_paths": ["a/"]},
                {"id": "b", "title": "mod b", "acceptance": "tests", "owned_paths": ["b/"]},
                {"id": "c", "title": "mod c", "acceptance": "tests", "owned_paths": ["c/"]},
            ],
            "capacity": {
                "global_free_slots": 10,
                "repository_writer_slots": 4,
                "verifier_absorption": 2,
            },
            "expect_min_workers": 2,
        },
        {
            "name": "sequential_chain",
            "tasks": [
                {"id": "a", "title": "first", "acceptance": "ok"},
                {"id": "b", "title": "second", "depends_on": ["a"], "acceptance": "ok"},
                {"id": "c", "title": "third", "depends_on": ["b"], "acceptance": "ok"},
            ],
            "expect_max_workers": 1,
        },
        {
            # Single root then wide body: root-layer size is 1, antichain width is 5.
            # Root-count misreading collapses this to sequential/worker_count=1.
            "name": "single_root_wide_body",
            "tasks": [
                {"id": "contract", "title": "contract", "acceptance": "ok"},
                {
                    "id": "s1",
                    "title": "sibling 1",
                    "depends_on": ["contract"],
                    "acceptance": "ok",
                    "owned_paths": ["s1/"],
                },
                {
                    "id": "s2",
                    "title": "sibling 2",
                    "depends_on": ["contract"],
                    "acceptance": "ok",
                    "owned_paths": ["s2/"],
                },
                {
                    "id": "s3",
                    "title": "sibling 3",
                    "depends_on": ["contract"],
                    "acceptance": "ok",
                    "owned_paths": ["s3/"],
                },
                {
                    "id": "s4",
                    "title": "sibling 4",
                    "depends_on": ["contract"],
                    "acceptance": "ok",
                    "owned_paths": ["s4/"],
                },
                {
                    "id": "s5",
                    "title": "sibling 5",
                    "depends_on": ["contract"],
                    "acceptance": "ok",
                    "owned_paths": ["s5/"],
                },
            ],
            "capacity": {
                "global_free_slots": 10,
                "repository_writer_slots": 5,
                "verifier_absorption": 2,
            },
            "expect_min_workers": 2,
            "expect_parallel": True,
        },
        {
            # Strict chain must stay sequential even with headroom capacity.
            "name": "strict_chain",
            "tasks": [
                {"id": "a", "title": "a", "acceptance": "ok"},
                {"id": "b", "title": "b", "depends_on": ["a"], "acceptance": "ok"},
                {"id": "c", "title": "c", "depends_on": ["b"], "acceptance": "ok"},
                {"id": "d", "title": "d", "depends_on": ["c"], "acceptance": "ok"},
                {"id": "e", "title": "e", "depends_on": ["d"], "acceptance": "ok"},
            ],
            "capacity": {
                "global_free_slots": 10,
                "repository_writer_slots": 5,
                "verifier_absorption": 2,
            },
            "expect_max_workers": 1,
            "expect_topology": "sequential",
        },
    ]
