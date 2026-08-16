"""Formation Engine — select a reusable team structure per task class.

Phase 5 foundation (HANDOFF/formation) + Phase B binding. Does not rebuild
CBM, assess, or routing — those already exist. This package:

* names the six formations (including prediction)
* classifies a task into a formation (with confidence)
* maps formation → topology
* reorders routing candidates by implementer preference
* returns a role map the swarm/CBM layers can consume
"""

from omniagentos.formation.etar import (
    EtarBreakdown,
    compare_arms,
    compute_etar,
    latency_from_speed,
)
from omniagentos.formation.lineage import (
    LINEAGE_MAP,
    LineageAssignmentError,
    ReviewerAssignmentError,
    UnknownModelLineageError,
    assign_reviewer,
    assign_verifier,
    lineage_for_model,
)
from omniagentos.formation.selector import (
    CONFIDENCE_THRESHOLD,
    FORMATION_TOPOLOGY,
    FORMATIONS,
    Formation,
    FormationRole,
    FormationSelection,
    clear_formation_cache,
    is_low_confidence,
    prefer_implementers,
    select_formation,
    select_formation_with_confidence,
    topology_for_formation,
)
from omniagentos.formation.telemetry import list_selections, record_selection

__all__ = [
    "CONFIDENCE_THRESHOLD",
    "FORMATIONS",
    "FORMATION_TOPOLOGY",
    "LINEAGE_MAP",
    "EtarBreakdown",
    "Formation",
    "FormationRole",
    "FormationSelection",
    "LineageAssignmentError",
    "ReviewerAssignmentError",
    "UnknownModelLineageError",
    "assign_reviewer",
    "assign_verifier",
    "clear_formation_cache",
    "compare_arms",
    "compute_etar",
    "is_low_confidence",
    "latency_from_speed",
    "list_selections",
    "lineage_for_model",
    "prefer_implementers",
    "record_selection",
    "select_formation",
    "select_formation_with_confidence",
    "topology_for_formation",
]
