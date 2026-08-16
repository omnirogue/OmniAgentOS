"""Test observability — read-only views over the estate's test evidence.

Four categories, four sources, all under ``var/``: North Star cert results
(its own sqlite file, opened read-only), memcert JUnit, the feature-health
JSONL ledger, and merge-gate receipts. Nothing here writes, migrates, or
touches the control-plane database.

Absence is rendered as absence: a source that is missing, locked or corrupt
yields ``{"available": false, "reason": ...}``, never a coerced zero and never
a 500.
"""

from omniagentos.testobs.readers import (
    CATEGORIES,
    NSC_METRICS,
    OVERVIEW_DAYS,
    WEAKSPOT_STATUSES,
    read_diagnostics,
    read_memory,
    read_northstar,
    read_suite,
    snapshot,
    testobs_var_root,
    weakspot_rank,
)

__all__ = [
    "CATEGORIES",
    "NSC_METRICS",
    "OVERVIEW_DAYS",
    "WEAKSPOT_STATUSES",
    "read_diagnostics",
    "read_memory",
    "read_northstar",
    "read_suite",
    "snapshot",
    "testobs_var_root",
    "weakspot_rank",
]
