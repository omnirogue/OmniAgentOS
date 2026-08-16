"""Tests for the scorecards module (W5)."""

from __future__ import annotations

import pytest

from omniagentos.reliability.scorecards import (
    aggregate_metrics,
    compute_scorecard,
)
from omniagentos.reliability.store import SqliteReliabilityStore


@pytest.fixture
def store_fixture(tmp_path):
    """Create a test store with schema."""
    db_path = str(tmp_path / "test.db")
    store = SqliteReliabilityStore(db_path)
    return store


class TestAggregateMetrics:
    """Test metric aggregation."""

    def test_empty_rows(self):
        """Empty rows returns baseline metrics."""
        metrics = aggregate_metrics([])

        assert metrics["run_count"] == 0
        assert metrics["success_rate"] == 0.0
        assert metrics["failure_count"] == 0

    def test_success_rate_calculation(self):
        """Success rate calculated from run states."""
        rows = [
            {"state": "completed"},
            {"state": "completed"},
            {"state": "failed"},
        ]

        metrics = aggregate_metrics(rows)

        assert metrics["run_count"] == 3
        assert metrics["success_rate"] == pytest.approx(2.0 / 3.0)
        assert metrics["failure_count"] == 1

    def test_latency_median(self):
        """Median latency calculated from wall_ms."""
        rows = [
            {"wall_ms": 1000},
            {"wall_ms": 2000},
            {"wall_ms": 3000},
        ]

        metrics = aggregate_metrics(rows)

        assert metrics["median_latency_s"] == pytest.approx(2.0)

    def test_cost_aggregation(self):
        """Mean cost calculated from cost_usd."""
        rows = [
            {"cost_usd": 1.0},
            {"cost_usd": 2.0},
            {"cost_usd": 3.0},
        ]

        metrics = aggregate_metrics(rows)

        assert metrics["mean_cost_usd"] == pytest.approx(2.0)

    def test_judge_approval_rate(self):
        """Judge approval rate from verdicts."""
        rows = [
            {"verdict": "approve"},
            {"verdict": "approve_with_conditions"},
            {"verdict": "reject"},
        ]

        metrics = aggregate_metrics(rows)

        # 2 approvals (approve + approve_with_conditions) out of 3
        assert metrics["judge_approval_rate"] == pytest.approx(2.0 / 3.0)


class TestComputeScorecard:
    """Test scorecard computation and storage."""

    def test_compute_scorecard_creates_row(self, store_fixture):
        """compute_scorecard creates a scorecard row."""
        rows = [
            {"state": "completed"},
            {"state": "completed"},
        ]

        result = compute_scorecard(
            store_fixture,
            subject_type="agent",
            subject_id="agt_test123",
            window="day",
            period_start="2026-01-01T00:00:00Z",
            rows=rows,
        )

        assert "id" in result
        assert result["subject_type"] == "agent"
        assert result["subject_id"] == "agt_test123"
        assert "metrics_json" in result

    def test_scorecard_upsert_idempotency(self, store_fixture):
        """Scorecard upsert is idempotent (updates on duplicate key)."""
        rows1 = [{"state": "completed"}, {"state": "failed"}]
        rows2 = [{"state": "completed"}, {"state": "completed"}]

        # First upsert
        result1 = compute_scorecard(
            store_fixture,
            subject_type="model",
            subject_id="gpt-4",
            window="day",
            period_start="2026-01-01T00:00:00Z",
            rows=rows1,
        )

        # Second upsert with same key should update
        result2 = compute_scorecard(
            store_fixture,
            subject_type="model",
            subject_id="gpt-4",
            window="day",
            period_start="2026-01-01T00:00:00Z",
            rows=rows2,
        )

        # Verify second one had higher success rate
        assert result2["metrics_json"]["success_rate"] > result1["metrics_json"]["success_rate"]

    def test_scorecard_persistence(self, store_fixture):
        """Scorecard persists and is retrievable."""
        compute_scorecard(
            store_fixture,
            subject_type="harness",
            subject_id="cli-claude",
            window="week",
            period_start="2026-01-01T00:00:00Z",
            rows=[{"state": "completed"}],
        )

        # Retrieve
        scorecard = store_fixture.get_scorecard(
            subject_type="harness",
            subject_id="cli-claude",
            window="week",
            period_start="2026-01-01T00:00:00Z",
        )

        assert scorecard is not None
        assert scorecard.subject_id == "cli-claude"


class TestScorecardIntegration:
    """Integration tests for scorecard pipeline."""

    def test_multiple_scorecards(self, store_fixture):
        """Multiple scorecards for different subjects."""
        subjects = [
            ("agent", "agt_alice"),
            ("agent", "agt_bob"),
            ("model", "gpt-4"),
            ("model", "claude-3"),
        ]

        for subject_type, subject_id in subjects:
            compute_scorecard(
                store_fixture,
                subject_type=subject_type,
                subject_id=subject_id,
                window="day",
                period_start="2026-01-01T00:00:00Z",
                rows=[{"state": "completed"}],
            )

        # List all scorecards
        all_scorecards = store_fixture.list_scorecards(limit=100)
        assert len(all_scorecards) >= len(subjects)

    def test_daily_vs_weekly_windows(self, store_fixture):
        """Separate scorecards for daily and weekly windows."""
        rows = [{"state": "completed"}]

        compute_scorecard(
            store_fixture,
            subject_type="department",
            subject_id="eng",
            window="day",
            period_start="2026-01-01T00:00:00Z",
            rows=rows,
        )

        compute_scorecard(
            store_fixture,
            subject_type="department",
            subject_id="eng",
            window="week",
            period_start="2026-01-01T00:00:00Z",
            rows=rows,
        )

        # Both should exist as separate rows
        daily = store_fixture.get_scorecard(
            subject_type="department",
            subject_id="eng",
            window="day",
            period_start="2026-01-01T00:00:00Z",
        )
        weekly = store_fixture.get_scorecard(
            subject_type="department",
            subject_id="eng",
            window="week",
            period_start="2026-01-01T00:00:00Z",
        )

        assert daily is not None
        assert weekly is not None


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
