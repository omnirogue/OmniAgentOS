"""Agentless: localize -> sample N candidate patches -> select by tests.

Implements the Agentless pipeline (Xia et al., arXiv 2407.01489): a single cheap
localization pass replaces agentic multi-turn exploration, and a project's own
test suite — not a model or a human — selects the winning patch among N samples.
Compute-optimal batching/early-stopping (Snell et al. 2408.03314) means the loop
spends only as much sampling budget as it takes to find one verified fix.

Public API re-exported here; see :mod:`omniagentos.agentless.pipeline` for the
orchestration entry point (``run_agentless``) and :mod:`omniagentos.agentless.contracts`
for the data shapes every stage passes between it and the next.
"""

from __future__ import annotations

from omniagentos.agentless.contracts import (
    AgentlessResult,
    CandidatePatch,
    LocalizationResult,
    SampleSpec,
    SymbolRef,
    VerifiedCandidate,
)
from omniagentos.agentless.localize import localize
from omniagentos.agentless.patch import apply_candidate, extract_diff
from omniagentos.agentless.pipeline import run_agentless
from omniagentos.agentless.select import normalize_diff, select_candidate
from omniagentos.agentless.verify import run_tests

__all__ = [
    "AgentlessResult",
    "CandidatePatch",
    "LocalizationResult",
    "SampleSpec",
    "SymbolRef",
    "VerifiedCandidate",
    "localize",
    "apply_candidate",
    "extract_diff",
    "run_agentless",
    "normalize_diff",
    "select_candidate",
    "run_tests",
]
