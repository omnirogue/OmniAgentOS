"""Swarm Mode: parallel multi-agent execution over the task board (WP1 base).

Schema: migration 044 (swarm_runs / swarm_deps / swarm_attempts + additive
board_tasks/sessions/claude_accounts columns). Contracts: `contracts.py`.
DAL: `dal.SwarmDal`. Later WPs add planner, scheduler, router, provider_exec,
API, summary, and optimizer on top of these seams.
"""

from omniagentos.swarm.contracts import (
    ATTEMPT_END_REASONS,
    SWARM_EVENT_ACTIONS,
    SWARM_EVENT_KIND,
    TERMINAL_RUN_STATUSES,
    SwarmEmitter,
    SwarmPlan,
    SwarmRunStatus,
    SwarmTaskSpec,
)
from omniagentos.swarm.dal import SwarmDal
from omniagentos.swarm.provider_exec import (
    ProviderExecPolicyError,
    ProviderExecSpawnError,
    ProviderSessionRunner,
)

__all__ = [
    "ATTEMPT_END_REASONS",
    "SWARM_EVENT_ACTIONS",
    "SWARM_EVENT_KIND",
    "SwarmDal",
    "SwarmEmitter",
    "SwarmPlan",
    "SwarmRunStatus",
    "SwarmTaskSpec",
    "TERMINAL_RUN_STATUSES",
    "ProviderExecPolicyError",
    "ProviderExecSpawnError",
    "ProviderSessionRunner",
]
