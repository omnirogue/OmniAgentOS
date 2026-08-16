from __future__ import annotations

from collections.abc import Mapping
from enum import StrEnum
from typing import Any


class JobRole(StrEnum):
    """What kind of work this is — the job axis.

    Grouped by plane (control / execution / evaluation / release / cross-cutting)
    per the OmniAgentOS engineering specification. Every value here MUST have a
    matching ``vault/prompts/roles/<value>.md``; ``tests/swarm/test_roles.py``
    asserts the two sets are equal, and ``promptshape.rolepack.JOB_ROLES`` is
    derived from this enum so there is exactly one vocabulary.
    """

    # Control plane — decide what happens and under what authority.
    ROUTER = "router"
    PLANNER = "planner"
    CONTEXT_BUILDER = "context_builder"
    CONTRACT_GENERATOR = "contract_generator"

    # Execution plane — produce the artifacts.
    TEAM_LEAD = "team_lead"
    IMPLEMENTER = "implementer"
    TESTER = "tester"
    DEBUGGER = "debugger"

    # Evaluation plane — judge the output, then judge the process.
    REVIEWER = "reviewer"
    TRACE_AUDITOR = "trace_auditor"

    # Release — combine, then dispose.
    INTEGRATOR = "integrator"
    ACCEPTANCE = "acceptance"

    # Cross-cutting.
    LEARNING = "learning"
    INCIDENT = "incident"

DEFAULT_JOB_ROLE = JobRole.IMPLEMENTER

def job_role_from_swarm_json(swarm_json: Mapping[str, Any] | None) -> JobRole:
    """
    Determine the job role based on swarm JSON complexity and integration fields.

    This axis is ORTHOGONAL to model_role/RUNGS (CBM capability routing)
    and the two must never be merged.
    """
    if not isinstance(swarm_json, Mapping):
        return DEFAULT_JOB_ROLE
    try:
        if swarm_json.get("integration"):
            return JobRole.INTEGRATOR
        complexity = swarm_json.get("complexity")
        if str(complexity or "").lower() in {"review", "verify"}:
            return JobRole.REVIEWER
    except Exception:
        pass
    return DEFAULT_JOB_ROLE
