"""Pure quality-first allocation policy."""

from __future__ import annotations

from dataclasses import dataclass

from omniagentos.allocation.characterize import TaskCharacterization
from omniagentos.allocation.topologies import Topology


@dataclass(frozen=True, slots=True)
class FanoutDecision:
    topology: Topology
    worker_count: int
    verifier_count: int
    hard_capacity: int
    rationale: str


TOPOLOGY_CAPS: dict[str, int] = {
    "sequential": 1,
    "generator_critic": 2,
    "specialist_panel": 3,
    "parallel_sections": 8,
}
PARALLEL_SECTIONS_FLOOR = 2
RESEARCH_SHAPED_TARGET = 5


def decide_fanout(
    char: TaskCharacterization,
    *,
    free_slots: int,
    writer_slots: int,
    verifier_capacity: int,
    budget_workers: int | None = None,
    independent_units: int | None = None,
    preferred_topology: str | None = None,
    disjoint_dag_width: int | None = None,
) -> FanoutDecision:
    """Choose a bounded topology; this function never reserves or mutates state.

    ``preferred_topology`` (formation-owned when confidence is high) is honoured
    when capacity allows and dependencies are not sequential (F4 / R2-F3).
    """
    units = independent_units if independent_units is not None else int(max(1.0, char.I * 4.0))
    limits = [max(0, int(units)), max(0, int(free_slots)), max(0, int(writer_slots))]
    if budget_workers is not None:
        limits.append(max(0, int(budget_workers)))
    hard_capacity = min(limits)

    high_risk = char.R >= 0.7
    verifier_count = min(
        max(0, int(verifier_capacity)),
        2 if high_risk else 1 if char.V >= 0.7 and char.C >= 0.5 else 0,
    )
    if verifier_count:
        hard_capacity = min(hard_capacity, max(0, int(verifier_capacity)))

    # Dependency chains are quality-sensitive: concurrent writers create merge
    # work rather than productive work, so they remain one-worker sequential.
    if char.S >= 0.6:
        return FanoutDecision(
            "sequential",
            min(1, hard_capacity),
            verifier_count,
            hard_capacity,
            "Sequential dependencies prohibit multi-writer fan-out.",
        )

    pref = str(preferred_topology or "").strip().lower()
    if pref == "parallel_sections":
        # Generator diversity has a topology-specific floor. Independent units
        # size the preferred width but do not lower that floor; real slots and
        # an explicit budget remain hard bounds.
        parallel_limits = [max(0, int(free_slots)), max(0, int(writer_slots))]
        if budget_workers is not None:
            parallel_limits.append(max(0, int(budget_workers)))
        parallel_capacity = min(parallel_limits)
        dag_width = disjoint_dag_width if disjoint_dag_width is not None else units
        generator_count = min(
            max(int(dag_width), PARALLEL_SECTIONS_FLOOR),
            TOPOLOGY_CAPS["parallel_sections"],
        )
        workers = min(parallel_capacity, generator_count)
        return FanoutDecision(
            "parallel_sections",
            workers,
            1,
            parallel_capacity,
            f"Formation preferred topology=parallel_sections: generators={workers}, critic=1",
        )

    if hard_capacity <= 1:
        return FanoutDecision(
            "sequential",
            hard_capacity,
            verifier_count,
            hard_capacity,
            "Capacity or independent work is insufficient for useful fan-out.",
        )

    if pref in {
        "hierarchical",
        "map_reduce",
        "generator_critic",
        "specialist_panel",
        "sequential",
    }:
        if pref == "sequential":
            return FanoutDecision(
                "sequential",
                1,
                verifier_count,
                hard_capacity,
                f"Formation preferred topology={pref}",
            )
        workers = hard_capacity
        if pref == "generator_critic":
            workers = min(hard_capacity, TOPOLOGY_CAPS["generator_critic"])
        elif pref == "specialist_panel":
            workers = min(hard_capacity, TOPOLOGY_CAPS["specialist_panel"])
        elif pref == "hierarchical":
            workers = hard_capacity
        return FanoutDecision(
            pref,  # type: ignore[arg-type]
            workers,
            verifier_count,
            hard_capacity,
            f"Formation preferred topology={pref}",
        )

    if char.D >= 0.6 and char.I >= 0.5:
        return FanoutDecision(
            "map_reduce",
            hard_capacity,
            verifier_count,
            hard_capacity,
            "Independent partitions justify bounded map-reduce workers.",
        )
    if char.M >= 0.7:
        return FanoutDecision(
            "specialist_panel",
            min(hard_capacity, 3),
            verifier_count,
            hard_capacity,
            "Multiple specialties benefit from a bounded specialist panel.",
        )
    if char.U >= 0.7 and char.V >= 0.5:
        return FanoutDecision(
            "generator_critic",
            min(hard_capacity, 2),
            verifier_count,
            hard_capacity,
            "Uncertain but checkable work benefits from generation and critique.",
        )
    return FanoutDecision(
        "sequential",
        1,
        verifier_count,
        hard_capacity,
        "No collaboration shape clears the quality-first fan-out threshold.",
    )
