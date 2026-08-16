"""Deterministic task characterization and worker-allocation policy."""

from omniagentos.allocation.audit import DecompositionAudit, audit_decomposition
from omniagentos.allocation.capacity import (
    DEFAULT_REPO_WRITER_SLOTS,
    CapacitySnapshot,
    capacity_from_mapping,
)
from omniagentos.allocation.characterize import TaskCharacterization, characterize
from omniagentos.allocation.fanout import FanoutDecision, decide_fanout
from omniagentos.allocation.simulator import SimResult, fixture_suite, simulate_fanout

__all__ = [
    "CapacitySnapshot",
    "DEFAULT_REPO_WRITER_SLOTS",
    "DecompositionAudit",
    "FanoutDecision",
    "SimResult",
    "TaskCharacterization",
    "audit_decomposition",
    "capacity_from_mapping",
    "characterize",
    "decide_fanout",
    "fixture_suite",
    "simulate_fanout",
]
