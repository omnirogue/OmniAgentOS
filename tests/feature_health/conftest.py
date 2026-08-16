"""Feature-health lane conftest.

Every test collected under tests/feature_health/ is force-marked ``feature_health``
here so a forgotten decorator can never leak a lane test into the default or fast
lanes (pyproject addopts and FAST_LANE_MARKERS both exclude the marker).

``fh_known_issue(id=...)`` marks a red-by-design test documenting a known product
defect (registry: docs/testing/KNOWN-ISSUES.yaml). At collection time the
nodeid -> issue-id map is written to a sidecar JSON so the ledger writer
(scripts/feature_health/fh.py) can tag those failures ``expected=true`` and the
``issues`` report can separate regressions from documented defects.
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterator
from pathlib import Path

import pytest

_LANE_ROOT = Path(__file__).resolve().parent
_REPO_ROOT = _LANE_ROOT.parent.parent


def _sidecar_path() -> Path:
    override = os.environ.get("FH_KNOWN_ISSUES_SIDECAR")
    if override:
        return Path(override)
    return _REPO_ROOT / "var" / "feature-health" / "known-issues-collected.json"


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    known: dict[str, str] = {}
    for item in items:
        try:
            in_lane = Path(str(item.fspath)).resolve().is_relative_to(_LANE_ROOT)
        except (OSError, ValueError):
            in_lane = False
        if not in_lane:
            continue
        item.add_marker(pytest.mark.feature_health)
        marker = item.get_closest_marker("fh_known_issue")
        if marker is not None:
            issue_id = marker.kwargs.get("id") or (marker.args[0] if marker.args else "")
            if not issue_id:
                raise pytest.UsageError(
                    f"{item.nodeid}: fh_known_issue requires id=<KNOWN-ISSUES.yaml key>"
                )
            known[item.nodeid] = str(issue_id)
    if known:
        sidecar = _sidecar_path()
        sidecar.parent.mkdir(parents=True, exist_ok=True)
        merged: dict[str, str] = {}
        if sidecar.exists():
            try:
                merged = json.loads(sidecar.read_text())
            except (OSError, json.JSONDecodeError):
                merged = {}
        merged.update(known)
        sidecar.write_text(json.dumps(merged, indent=1, sort_keys=True))


@pytest.fixture()
def fh_subprocess_env() -> dict[str, str]:
    """Env for lane subprocesses (runner --once, routines_tick, uvicorn, ...).

    Copies the pytest-pinned environment so the session-wide isolation from
    tests/conftest.py (tmp OMNIAGENTOS_DB, tmp OMNIAGENTOS_VAR/VAR_DIR, startup
    seeding off) is inherited, then fails closed if any pin is missing or points
    into the product runtime. Never launch a lane subprocess through a login
    shell (`sh -l`), which would re-source launch-env.sh and repoint the DB.
    """
    env = os.environ.copy()
    db = env.get("OMNIAGENTOS_DB", "")
    var_dir = env.get("OMNIAGENTOS_VAR_DIR", "")
    assert db, "OMNIAGENTOS_DB pin missing — root conftest isolation did not apply"
    assert var_dir, "OMNIAGENTOS_VAR_DIR pin missing — root conftest isolation did not apply"
    forbidden = str(_REPO_ROOT / "var")
    for name, value in (("OMNIAGENTOS_DB", db), ("OMNIAGENTOS_VAR_DIR", var_dir)):
        assert not value.startswith(forbidden), (
            f"{name}={value} points into the product var/ — refusing to spawn"
        )
    env.setdefault("OMNIAGENTOS_LEDGER_DIR", str(Path(var_dir) / "ledger"))
    env.setdefault("OMNIAGENTOS_VAULT_DIR", str(Path(var_dir) / "vault"))
    return env


class Tier2Budget:
    """Spend accounting for tier2. Exact costs accumulate against FH_TIER2_MAX_USD;
    CLI-harness calls (no cost reporting) are bounded by count instead."""

    def __init__(self) -> None:
        self.max_usd = float(os.environ.get("FH_TIER2_MAX_USD", "0.25"))
        self.max_cli_calls = int(os.environ.get("FH_TIER2_MAX_CLI_CALLS", "6"))
        self.spent_usd = 0.0
        self.cli_calls = 0

    def record_cost(self, usd: float) -> None:
        self.spent_usd += max(0.0, float(usd))

    def record_cli_call(self) -> None:
        self.cli_calls += 1

    def require_headroom(self, *, cli: bool = False) -> None:
        if self.spent_usd >= self.max_usd:
            pytest.skip(
                f"fh-budget-exhausted: spent ${self.spent_usd:.4f} >= cap ${self.max_usd:.2f}"
            )
        if cli and self.cli_calls >= self.max_cli_calls:
            pytest.skip(
                f"fh-budget-exhausted: {self.cli_calls} CLI calls >= cap {self.max_cli_calls}"
            )


@pytest.fixture(scope="session")
def fh_budget() -> Iterator[Tier2Budget]:
    budget = Tier2Budget()
    yield budget
    summary = {
        "spent_usd": round(budget.spent_usd, 6),
        "cli_calls": budget.cli_calls,
        "max_usd": budget.max_usd,
    }
    out = os.environ.get("FH_BUDGET_SUMMARY_PATH")
    if out:
        try:
            Path(out).parent.mkdir(parents=True, exist_ok=True)
            Path(out).write_text(json.dumps(summary))
        except OSError:
            pass
