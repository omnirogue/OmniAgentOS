"""Test-asserted inventory of every FastAPI GET route's authentication posture."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from omniagentos.api.main import app
from tests.api.fixtures_posture import (
    UNCLASSIFIED,
    get_posture_registry,
    write_posture_report,
)

REPORT_PATH = Path("/Users/youruser/.claude/fimi/20260803-omniagentos-harvest/get-posture-report.md")
BASELINE_PATH = Path(__file__).parent / "unclassified_get_routes_baseline.json"

# The classifier now ASKS the app whether a route is gated (see
# fixtures_posture.observed_session_token_gate), which only answers truthfully
# with the real app-level gate active. Without this marker the suite-wide
# bypass in tests/conftest.py makes every route look ungated and the registry
# silently reverts to path-shape-only classification.
pytestmark = pytest.mark.real_auth


def test_every_openapi_get_has_declared_auth_posture() -> None:
    """Every mounted GET must be gated or deliberately named public.

    This test enforces a shrinking allowlist (ratchet): new routes may not
    become unclassified, and the baseline count must not grow. Classifying
    unclassified routes reduces the count; no regression is allowed.
    """
    paths = [path for path, operations in app.openapi()["paths"].items() if "get" in operations]
    registry = get_posture_registry(paths)
    try:
        write_posture_report(registry, REPORT_PATH)
    except PermissionError:
        # The report lives outside the repository. Sandboxed test runners can
        # not update that operator-owned directory. Do not let that environmental
        # restriction obscure the actual posture assertion below.
        pass

    unclassified = sorted(
        path for path, entry in registry.items() if entry.classification == UNCLASSIFIED
    )

    # Load the baseline shrinking allowlist.
    with open(BASELINE_PATH) as f:
        baseline_data = json.load(f)
    baseline_routes = set(baseline_data["baseline_routes"])
    baseline_count = baseline_data["baseline_count"]

    # Ratchet 1: No new routes may become unclassified.
    new_unclassified = set(unclassified) - baseline_routes
    assert not new_unclassified, (
        f"NEW unclassified GET routes detected: {sorted(new_unclassified)}. "
        "Gate them, or explicitly review and add only provably safe routes to PUBLIC_GETS."
    )

    # Ratchet 2: The baseline count must never grow.
    current_count = len(unclassified)
    assert current_count <= baseline_count, (
        f"Unclassified route count increased: was {baseline_count}, now {current_count}. "
        "Some route became unclassified unexpectedly."
    )

    # Progress check: if count decreased, that's allowed and encouraged.
    if current_count < baseline_count:
        pass  # Silent success; this is the intended progress path.
