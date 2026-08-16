"""Execution vocabulary and the deterministic execution policy (T4.4).

Two layers, split on purpose:

* :mod:`omniagentos.execution.vocab` — the one tier/effort vocabulary
  (re-exported from ``contracts``) and the one parser for it.
* :mod:`omniagentos.execution.policy` — :func:`decide_execution`, which turns
  signals into an :class:`~omniagentos.contracts.ExecutionEnvelope`.

The *floors* the policy stands on live in :mod:`omniagentos.policy.execution`,
not here. That is not a layering accident: floors are a safety control and belong
beside ``evaluate_action`` under the same protection, whereas this package is
allowed to grow lane heuristics on top of them.
"""

from __future__ import annotations

from omniagentos.execution.assess import (
    AssessmentContext,
    AssessmentResult,
    Assessor,
    assess,
    fix_scope_after_fail,
)
from omniagentos.execution.policy import (
    POLICY_VERSION,
    BreadthReading,
    ExecutionSignals,
    ScopeBreadth,
    decide_execution,
    measure_breadth,
)
from omniagentos.execution.verify import (
    execution_level_from_risk,
    observe_working_tree,
    scope_verdict,
    verify_working_tree,
)
from omniagentos.execution.violations import (
    ViolationActionPlan,
    ViolationContext,
    longhaul_waiting_review,
    plan_violation_response,
    runner_quarantine_park,
    swarm_revert_flag,
)
from omniagentos.execution.vocab import (
    DEFAULT_EFFORT_BY_TIER,
    ModelTier,
    ReasoningEffort,
    default_effort,
    normalize_effort,
    normalize_tier,
)

__all__ = [
    "DEFAULT_EFFORT_BY_TIER",
    "POLICY_VERSION",
    "BreadthReading",
    "Assessor",
    "AssessmentContext",
    "AssessmentResult",
    "ExecutionSignals",
    "ModelTier",
    "ReasoningEffort",
    "ScopeBreadth",
    "decide_execution",
    "assess",
    "default_effort",
    "measure_breadth",
    "execution_level_from_risk",
    "fix_scope_after_fail",
    "normalize_effort",
    "normalize_tier",
    "observe_working_tree",
    "scope_verdict",
    "verify_working_tree",
    "ViolationActionPlan",
    "ViolationContext",
    "longhaul_waiting_review",
    "plan_violation_response",
    "runner_quarantine_park",
    "swarm_revert_flag",
]
