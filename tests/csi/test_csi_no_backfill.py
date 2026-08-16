"""Test CSI post-merge observation prevents post-window checkpoint backfill."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from omniagentos.csi.observe import PostMergeObserver, sweep_post_merge


def test_csi_post_merge_no_backfill_at_31d(tmp_path: Path) -> None:
    now = datetime.now(UTC)
    applied_at = (now - timedelta(days=31)).isoformat()

    record = {
        "improvement_id": "imp_late_31d",
        "applied_at": applied_at,
        "claimed_metric": "latency",
        "baseline": "100ms",
        "observed": "80ms",
        "materialised": True,
    }

    observer = PostMergeObserver(ledger_dir=str(tmp_path))

    # Perform sweep at 31d after applied_at
    summary = sweep_post_merge([record], now=now, observer=observer)

    # Exactly 1 observation is written (30d). 24h and 7d were missed and must NOT be backfilled.
    assert summary["observed"] == 1
    assert summary["deferred"] == 2  # 24h and 7d are closed/deferred

    # Check stored observation files
    obs_dir = tmp_path / "csi-observations"
    stored_files = list(obs_dir.glob("*.json"))
    assert len(stored_files) == 1

    obs_data = json.loads(stored_files[0].read_text(encoding="utf-8"))
    assert obs_data["improvement_id"] == "imp_late_31d"
    assert obs_data["window"] == "30d"

    # Confirm 24h and 7d observations were never created
    assert not observer.is_observed("imp_late_31d", "24h")
    assert not observer.is_observed("imp_late_31d", "7d")
    assert observer.is_observed("imp_late_31d", "30d")
