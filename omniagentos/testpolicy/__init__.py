"""Release-gate test policy: coverage, skips, scale, and classification.

Owned by L19 / S19B (coverage-scale). These modules are pure analysis and
in-process gates — no live services, no operator DB, no network.
"""

from __future__ import annotations

from omniagentos.testpolicy.coverage_policy import (
    CoveragePolicy,
    CoverageReportSummary,
    evaluate_coverage_report,
    load_coverage_policy,
)
from omniagentos.testpolicy.deadcode import (
    DeadCodeClassification,
    classify_stub,
    classify_tree_deadcode,
)
from omniagentos.testpolicy.handlers import (
    HandlerClassification,
    classify_broad_handlers,
    classify_tree_handlers,
)
from omniagentos.testpolicy.scale_gates import (
    BackpressureDecision,
    ScaleGateResult,
    run_bounded_scale_gate,
)
from omniagentos.testpolicy.skip_policy import (
    RequiredSuite,
    SkipSurfaceReport,
    surface_required_skips,
)

__all__ = [
    "BackpressureDecision",
    "CoveragePolicy",
    "CoverageReportSummary",
    "DeadCodeClassification",
    "HandlerClassification",
    "RequiredSuite",
    "ScaleGateResult",
    "SkipSurfaceReport",
    "classify_broad_handlers",
    "classify_stub",
    "classify_tree_deadcode",
    "classify_tree_handlers",
    "evaluate_coverage_report",
    "load_coverage_policy",
    "run_bounded_scale_gate",
    "surface_required_skips",
]
