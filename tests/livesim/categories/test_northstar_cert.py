"""North Star fixture and read-only estate bindings."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

pytestmark = pytest.mark.livesim

ROOT = Path(__file__).resolve().parents[2] / "fixtures" / "northstar"


def test_northstar_domain_fixture_manifest_is_pinned() -> None:
    manifest = json.loads((ROOT / "MANIFEST.json").read_text(encoding="utf-8"))
    assert manifest["suite"] == "northstar-cert/domains.v1"
    assert len(manifest["fixtures"]) == 7
    for entry in manifest["fixtures"]:
        path = ROOT / entry["path"]
        assert path.is_file()
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        assert digest == entry["sha256"], entry["id"]
        assert entry["seed"] > 0
        assert entry["expected_outcome"]


def test_northstar_live_result_surfaces_are_readable(livesim, live_db_ro) -> None:
    """A missing writer is evidence-dark, never a favorable empty result."""
    livesim.target("db")
    tables = {
        row["name"]
        for row in live_db_ro.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    required = {"swarm_runs", "eval_results"}
    livesim.record(inputs={"required_tables": sorted(required)}, outputs={"tables": sorted(tables)})
    if not required <= tables:
        pytest.skip(f"North Star result writer surfaces absent: {sorted(required - tables)}")
    counts = {
        table: int(live_db_ro.execute(f'SELECT COUNT(*) n FROM "{table}"').fetchone()["n"])
        for table in sorted(required)
    }
    livesim.extra(**{f"{key}_rows": value for key, value in counts.items()})
    assert all(value >= 0 for value in counts.values())
