"""Tests for the analyzer module (W5)."""

from __future__ import annotations

import pytest

from omniagentos.reliability.analyzer import validate_proposal
from omniagentos.reliability.store import SqliteReliabilityStore
from omniagentos.reliability.taxonomy import ImprovementStatus


@pytest.fixture
def store_fixture(tmp_path):
    """Create a test store with schema."""
    db_path = str(tmp_path / "test.db")
    store = SqliteReliabilityStore(db_path)
    return store


class TestValidateProposal:
    """Test proposal validation against contract."""

    def test_valid_proposal(self):
        """Valid proposal passes validation."""
        proposal = {
            "change_type": "config",
            "files": [],
            "config_edits": [],
            "plan": ["Step 1"],
            "restart_required": False,
        }
        result = validate_proposal(proposal)
        assert result == proposal

    def test_missing_required_field(self):
        """Missing required field raises ValueError."""
        proposal = {
            "change_type": "config",
            "files": [],
            "config_edits": [],
            "plan": ["Step 1"],
        }
        with pytest.raises(ValueError, match="missing fields"):
            validate_proposal(proposal)

    def test_invalid_change_type(self):
        """Empty change_type raises ValueError."""
        proposal = {
            "change_type": "",
            "files": [],
            "config_edits": [],
            "plan": ["Step 1"],
            "restart_required": False,
        }
        with pytest.raises(ValueError, match="change_type"):
            validate_proposal(proposal)


class TestProposalContract:
    """Test proposal contract enforcement."""

    def test_create_improvement_stores_proposal(self, store_fixture):
        """Improvements store proposal_json correctly."""
        proposal = {
            "change_type": "files",
            "files": ["test.py"],
            "config_edits": [],
            "plan": ["Update test.py"],
            "restart_required": False,
        }

        imp_id = store_fixture.create_improvement(
            origin="realtime",
            kind="fix",
            title="Test fix",
            proposal_json=proposal,
        )

        improvement = store_fixture.get_improvement(imp_id)
        assert improvement.proposal_json == proposal
        assert improvement.status == ImprovementStatus.PROPOSED.value


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
