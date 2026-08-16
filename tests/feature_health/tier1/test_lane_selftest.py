"""Lane self-test: marker hygiene the background lane re-verifies on every tier1 run."""

from __future__ import annotations

import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]


def test_addopts_excludes_feature_health() -> None:
    text = (REPO / "pyproject.toml").read_text()
    match = re.search(r'addopts = "(.*)"', text)
    assert match, "pyproject addopts line missing"
    assert "feature_health" in match.group(1), (
        "default addopts no longer excludes feature_health — lane tests would leak "
        "into make test / certification"
    )


def test_fast_lane_markers_exclude_feature_health() -> None:
    text = (REPO / "Makefile").read_text()
    match = re.search(r"FAST_LANE_MARKERS := (.+)", text)
    assert match, "FAST_LANE_MARKERS missing from Makefile"
    expr = match.group(1)
    assert "feature_health" in expr, (
        "FAST_LANE_MARKERS no longer excludes feature_health — lane tests would leak "
        "into make test-fast"
    )
    assert expr.count("not") == 1 and expr.strip().startswith("not ("), (
        "FAST_LANE_MARKERS must stay in single-negation superset form (see Makefile comment)"
    )


def test_matrix_never_includes_quarantined_dirs() -> None:
    text = (REPO / "configs" / "feature-health.yaml").read_text()
    for banned in ("tests/doctrine", "tests/counterfeits", "tests/simharness", "tests/longhaul"):
        for line in text.splitlines():
            stripped = line.strip()
            if stripped.startswith("- ") and banned in stripped:
                raise AssertionError(f"feature-health matrix must never run {banned}")
