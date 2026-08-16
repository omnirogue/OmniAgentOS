"""Fan-in adjudication: parallel candidates → one verified outcome.

Separate product (OmniAgentOS): never merge into OmniAgentOS.
"""

from __future__ import annotations

from omniagentos.fanin.adjudicate import adjudicate, rank_key
from omniagentos.fanin.normalize import content_hash, normalize_candidates
from omniagentos.fanin.pipeline import (
    FanInPipelineResult,
    VerifyOutcome,
    run_fanin_pipeline,
)
from omniagentos.fanin.types import (
    Candidate,
    FanInMode,
    FanInResult,
    NormalizedCandidate,
)

__all__ = [
    "Candidate",
    "FanInMode",
    "FanInPipelineResult",
    "FanInResult",
    "NormalizedCandidate",
    "VerifyOutcome",
    "adjudicate",
    "content_hash",
    "normalize_candidates",
    "rank_key",
    "run_fanin_pipeline",
]
