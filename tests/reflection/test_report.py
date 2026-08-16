"""Unit tests for Stage E (report)."""

from __future__ import annotations

from pathlib import Path

from omniagentos.db.store import SqliteStore
from omniagentos.reflection.report import generate_reflection_report


def test_generate_reflection_report(tmp_path):
    """Verify generating a morning report compiles proposals/outcomes into the vault."""
    db_path = str(tmp_path / "test.db")
    store = SqliteStore(db_path)

    # 1. Seed some dummy rows in database.
    # The report filters promoted proposals and outcomes by created_at/updated_at
    # matching the requested date_str, so seed timestamps on that same (fixed)
    # date instead of utc_now_iso() — otherwise the test only passes on the day
    # it was written.
    date_str = "2026-07-26"
    now = f"{date_str}T07:30:00Z"
    with store._lock:
        # pending proposal
        store._connection.execute(
            """
            INSERT INTO reflection_proposals (
                id, kind, target, current, proposed, rationale, evidence_refs_json, predicted_impact, risk_class, status, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "p_report_pending_01",
                "model_config",
                "configs/swarm.yaml",
                "{}",
                "{}",
                "Observed high latency.",
                "[]",
                "Success rate up",
                "low",
                "pending",
                now,
                now,
            ),
        )
        # promoted proposal
        store._connection.execute(
            """
            INSERT INTO reflection_proposals (
                id, kind, target, current, proposed, rationale, evidence_refs_json, predicted_impact, risk_class, status, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "p_report_promoted_01",
                "brief_template",
                "AGENTS.md",
                "{}",
                "{}",
                "Improve sub-agent brief patterns.",
                "[]",
                "Better accuracy",
                "low",
                "promoted",
                now,
                now,
            ),
        )
        # outcome row
        store._connection.execute(
            """
            INSERT INTO reflection_outcomes (
                id, proposal_id, kind, target, applied_at, outcome_metrics_json, status, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "out_report_01",
                "p_report_promoted_01",
                "brief_template",
                "AGENTS.md",
                now,
                "{}",
                "monitored",
                now,
                now,
            ),
        )

    # Resolve vault_dir to tmp_path / "vault"
    vault_dir = str(tmp_path / "vault")

    # Generate report
    report_path_str = generate_reflection_report(
        date_str=date_str,
        db_path=db_path,
        vault_dir=vault_dir,
    )

    report_path = Path(report_path_str)
    assert report_path.is_file()

    # Assert content sections are present
    content = report_path.read_text(encoding="utf-8")
    assert "## 1. What was learned" in content
    assert "## 2. What changed automatically" in content
    assert "## 3. Needs your decision" in content
    assert "## 4. Yesterday's changes scored" in content
    assert "## 5. Loop health" in content

    # Assert seeded IDs are formatted inside
    assert "p_report_pending_01" in content
    assert "p_report_promoted_01" in content
    assert "out_report_01" in content
