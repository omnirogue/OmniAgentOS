"""Coverage-based test-impact analysis (TESTING_SPEED_PLAN Phase 5), shadow-only.

Nothing in this package skips, deselects, or gates a test. It answers one question —
"which test files could a change to these paths possibly affect?" — and records the
answer so `scripts/tia_shadow.py` can audit it against reality before anyone considers
letting it choose what runs.

Two independent impact analyses now exist in this repo and they are complements, not
rivals:

* ``scripts/testlanes/impacted.py`` (Phase 4) walks the **static import graph**. It sees
  edges that no test happened to execute, and it works on files coverage never measured.
* this package (Phase 5) reads **runtime coverage contexts**. It sees dynamic edges the
  AST cannot — importlib dispatch, subprocess entry points, plugin hooks — but only for
  code some test actually executed.

Both hold the same safety rule, which is the entire contract: an input the analysis
cannot explain resolves to FULL. "Run nothing" is never an answer.

It lives under ``scripts/`` and not in ``omniagentos/`` on purpose. This is developer
tooling about the test suite, not product code: it makes no filesystem containment
decision, opens nothing it classifies, and grants no access. Its sibling analysis
(``scripts/testlanes/``) sits here for the same reason, and the product package's
path-security registry (``tests/acceptance/test_19_path_security_registry.py``) stays
about code that actually resolves paths for the product.
"""

from __future__ import annotations

from scripts.tia.changes import changed_files, changed_files_for_commit
from scripts.tia.coverage_map import (
    CoverageMap,
    CoverageMapError,
    build_map_from_context_pairs,
    build_map_from_coverage_json,
    resolve_context,
)
from scripts.tia.selector import (
    ALWAYS_RUN_PATTERNS,
    CriticalPatternError,
    Selection,
    critical_pattern_matches,
    select_tests,
)

__all__ = [
    "ALWAYS_RUN_PATTERNS",
    "CoverageMap",
    "CoverageMapError",
    "CriticalPatternError",
    "Selection",
    "build_map_from_context_pairs",
    "build_map_from_coverage_json",
    "changed_files",
    "changed_files_for_commit",
    "critical_pattern_matches",
    "resolve_context",
    "select_tests",
]
