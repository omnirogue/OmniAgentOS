from omniagentos.allocation.characterize import characterize
from omniagentos.allocation.fanout import decide_fanout


def test_trivial_task_keeps_one_worker() -> None:
    decision = decide_fanout(
        characterize({"work_volume": 1}), free_slots=4, writer_slots=4, verifier_capacity=2
    )
    assert decision.topology == "sequential"
    assert decision.worker_count == 1


def test_partitionable_work_maps_to_multiple_workers() -> None:
    char = characterize({"has_partitions": True, "partition_count": 8, "independent_units": 8})
    decision = decide_fanout(
        char, free_slots=4, writer_slots=3, verifier_capacity=2, independent_units=8
    )
    assert decision.topology == "map_reduce"
    assert decision.worker_count == 3 == decision.hard_capacity


def test_sequential_work_never_gets_multiple_writers() -> None:
    char = characterize({"sequential": True, "has_partitions": True, "partition_count": 8})
    decision = decide_fanout(
        char, free_slots=8, writer_slots=8, verifier_capacity=2, independent_units=8
    )
    assert decision.worker_count == 1


def test_high_risk_gets_extra_verifiers() -> None:
    char = characterize({"risk": 1, "critical": True, "verifiable": True})
    decision = decide_fanout(char, free_slots=4, writer_slots=4, verifier_capacity=3)
    assert decision.verifier_count == 2
