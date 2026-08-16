"""Tests for LaneProfile registry (context/lanes.py).

Tests cover:
- Valid LaneProfile loading with seeded rows
- Load failures: wildcard rows, revocation-less rows
- Authorization logic: same-scope allowed, cross-scope denied
- Canonical identity validation: valid identities allowed, non-canonical rejected
- Receipt structure and audit trail
"""

import pytest

from omniagentos.context.lanes import (
    SEEDED_DESIGNATIONS,
    AccessDesignation,
    AccessReceipt,
    LaneProfile,
    authorize_memory_access,
    load_profile,
)


class TestAccessDesignationValidation:
    """Test AccessDesignation construction and validation."""

    def test_valid_designation_construction(self):
        """A valid designation with all required fields constructs successfully."""
        d = AccessDesignation(
            holder="lane:runner.step",
            surface="conversation",
            scope="task",
            mode="read-write",
            grant_ref="system capture grant",
            revocation="disable runner row in memory policy",
        )
        assert d.holder == "lane:runner.step"
        assert d.surface == "conversation"
        assert d.scope == "task"
        assert d.mode == "read-write"
        assert d.revocation == "disable runner row in memory policy"

    def test_wildcard_holder_rejected(self):
        """Wildcard holders like '*' or 'all-agents' are rejected at construction."""
        with pytest.raises(ValueError, match="Non-canonical holder"):
            AccessDesignation(
                holder="*",
                surface="conversation",
                scope="task",
                mode="read",
                grant_ref="system",
                revocation="remove row",
            )

        with pytest.raises(ValueError, match="Non-canonical holder"):
            AccessDesignation(
                holder="all-agents",
                surface="conversation",
                scope="task",
                mode="read",
                grant_ref="system",
                revocation="remove row",
            )

    def test_non_canonical_holder_rejected(self):
        """Non-canonical holder spellings like 'agent:bob' are rejected."""
        with pytest.raises(ValueError, match="Non-canonical holder"):
            AccessDesignation(
                holder="agent:bob",
                surface="conversation",
                scope="task",
                mode="read",
                grant_ref="system",
                revocation="remove row",
            )

        with pytest.raises(ValueError, match="Non-canonical holder"):
            AccessDesignation(
                holder="system:foo",
                surface="conversation",
                scope="task",
                mode="read",
                grant_ref="system",
                revocation="remove row",
            )

    def test_missing_revocation_rejected(self):
        """A designation with None revocation fails to construct."""
        with pytest.raises(ValueError, match="invalid revocation"):
            AccessDesignation(
                holder="lane:runner.step",
                surface="conversation",
                scope="task",
                mode="read",
                grant_ref="system",
                revocation=None,  # type: ignore
            )

    def test_empty_revocation_rejected(self):
        """A designation with empty-string revocation fails to construct."""
        with pytest.raises(ValueError, match="invalid revocation"):
            AccessDesignation(
                holder="lane:runner.step",
                surface="conversation",
                scope="task",
                mode="read",
                grant_ref="system",
                revocation="",
            )

    def test_weak_revocation_rejected(self):
        """Revocation of 'none' or '*' is too weak and rejected."""
        with pytest.raises(ValueError, match="weak revocation"):
            AccessDesignation(
                holder="lane:runner.step",
                surface="conversation",
                scope="task",
                mode="read",
                grant_ref="system",
                revocation="none",
            )

        with pytest.raises(ValueError, match="weak revocation"):
            AccessDesignation(
                holder="lane:runner.step",
                surface="conversation",
                scope="task",
                mode="read",
                grant_ref="system",
                revocation="*",
            )

    def test_invalid_mode_rejected(self):
        """Invalid access modes are rejected."""
        with pytest.raises(ValueError, match="Invalid mode"):
            AccessDesignation(
                holder="lane:runner.step",
                surface="conversation",
                scope="task",
                mode="execute",  # type: ignore
                grant_ref="system",
                revocation="remove row",
            )

    def test_canonical_identities_accepted(self):
        """All canonical identity forms are accepted."""
        # Fixed identities
        for holder in [
            "lane:runner.step",
            "lane:swarm.planner",
            "lane:intake.planner",
            "lane:sessions",
            "lane:chat",
        ]:
            d = AccessDesignation(
                holder=holder,
                surface="conversation",
                scope="task",
                mode="read",
                grant_ref="system",
                revocation="remove row",
            )
            assert d.holder == holder

        # Dynamic identities with prefixes
        for holder in [
            "lane:swarm.worker.batch-eval",
            "lane:swarm.worker.xyz",
            "loop:loop_abc123",
            "job:job_key_1",
            "human:owner",
        ]:
            d = AccessDesignation(
                holder=holder,
                surface="knowledge",
                scope="project",
                mode="read",
                grant_ref="system",
                revocation="remove row",
            )
            assert d.holder == holder


class TestLaneProfile:
    """Test LaneProfile registry operations."""

    def test_add_designation(self):
        """Designations can be added to a profile."""
        profile = LaneProfile()
        d = AccessDesignation(
            holder="lane:runner.step",
            surface="conversation",
            scope="task",
            mode="read-write",
            grant_ref="system",
            revocation="remove row",
        )
        profile.add_designation(d)
        assert ("lane:runner.step", "conversation") in profile.designations

    def test_add_duplicate_designation_same_params(self):
        """Adding the same designation twice is idempotent."""
        profile = LaneProfile()
        d = AccessDesignation(
            holder="lane:runner.step",
            surface="conversation",
            scope="task",
            mode="read-write",
            grant_ref="system",
            revocation="remove row",
        )
        profile.add_designation(d)
        profile.add_designation(d)  # Should not raise
        assert len(profile.designations) == 1

    def test_add_conflicting_designation(self):
        """Adding a designation with same key but different params raises."""
        profile = LaneProfile()
        d1 = AccessDesignation(
            holder="lane:runner.step",
            surface="conversation",
            scope="task",
            mode="read-write",
            grant_ref="system",
            revocation="remove row",
        )
        d2 = AccessDesignation(
            holder="lane:runner.step",
            surface="conversation",
            scope="project",  # Different scope
            mode="read",
            grant_ref="system",
            revocation="remove row",
        )
        profile.add_designation(d1)
        with pytest.raises(ValueError, match="Duplicate designation"):
            profile.add_designation(d2)


class TestAuthorization:
    """Test authorization logic."""

    def test_same_scope_allowed(self):
        """A holder can read a surface within their scope."""
        profile = LaneProfile()
        profile.add_designation(
            AccessDesignation(
                holder="lane:runner.step",
                surface="conversation",
                scope="task",
                mode="read",
                grant_ref="system",
                revocation="remove row",
            )
        )

        receipt = profile.authorize(
            "lane:runner.step",
            "conversation",
            scope=("task", "tsk_abc"),
            mode="read",
        )
        assert receipt.allowed is True
        assert receipt.grant_ref == "remove row"

    def test_cross_scope_denied(self):
        """A holder is denied access when scope type doesn't match."""
        profile = LaneProfile()
        profile.add_designation(
            AccessDesignation(
                holder="lane:runner.step",
                surface="conversation",
                scope="task",
                mode="read",
                grant_ref="system",
                revocation="remove row",
            )
        )

        # Request scope is 'project' but designation requires 'task'
        receipt = profile.authorize(
            "lane:runner.step",
            "conversation",
            scope=("project", "proj_123"),
            mode="read",
        )
        assert receipt.allowed is False
        assert receipt.grant_ref == "remove row"  # Still named for audit

    def test_no_scope_designation_allows_any_scope(self):
        """A designation with scope=None allows any scope."""
        profile = LaneProfile()
        profile.add_designation(
            AccessDesignation(
                holder="lane:runner.step",
                surface="metacog",
                scope=None,  # No scope check
                mode="read",
                grant_ref="system",
                revocation="remove row",
            )
        )

        # Any scope should be allowed
        receipt = profile.authorize(
            "lane:runner.step",
            "metacog",
            scope=("task", "tsk_abc"),
            mode="read",
        )
        assert receipt.allowed is True

    def test_no_matching_row_denied(self):
        """A holder without a matching designation row is denied."""
        profile = LaneProfile()
        # No designation for lane:chat + knowledge

        receipt = profile.authorize(
            "lane:chat",
            "knowledge",
            scope=("task", "tsk_abc"),
            mode="read",
        )
        assert receipt.allowed is False
        assert receipt.grant_ref == "none"  # No row to revoke

    def test_read_mode_allows_read_request(self):
        """A 'read' mode row allows a 'read' request."""
        profile = LaneProfile()
        profile.add_designation(
            AccessDesignation(
                holder="lane:runner.step",
                surface="knowledge",
                scope="project",
                mode="read",  # Read only
                grant_ref="system",
                revocation="remove row",
            )
        )

        receipt = profile.authorize(
            "lane:runner.step",
            "knowledge",
            scope=("project", "proj_123"),
            mode="read",
        )
        assert receipt.allowed is True

    def test_read_mode_denies_write_request(self):
        """A 'read' mode row denies a 'write' request."""
        profile = LaneProfile()
        profile.add_designation(
            AccessDesignation(
                holder="lane:runner.step",
                surface="knowledge",
                scope="project",
                mode="read",  # Read only
                grant_ref="system",
                revocation="remove row",
            )
        )

        receipt = profile.authorize(
            "lane:runner.step",
            "knowledge",
            scope=("project", "proj_123"),
            mode="write",
        )
        assert receipt.allowed is False

    def test_write_mode_denies_read_request(self):
        """A 'write' mode row denies a 'read' request."""
        profile = LaneProfile()
        profile.add_designation(
            AccessDesignation(
                holder="lane:sessions",
                surface="conversation",
                scope="user",
                mode="write",  # Write only
                grant_ref="system",
                revocation="remove row",
            )
        )

        receipt = profile.authorize(
            "lane:sessions",
            "conversation",
            scope=("user", "user_123"),
            mode="read",
        )
        assert receipt.allowed is False

    def test_read_write_mode_allows_both(self):
        """A 'read-write' mode row allows both read and write."""
        profile = LaneProfile()
        profile.add_designation(
            AccessDesignation(
                holder="lane:sessions",
                surface="conversation",
                scope="user",
                mode="read-write",
                grant_ref="system",
                revocation="remove row",
            )
        )

        receipt = profile.authorize(
            "lane:sessions",
            "conversation",
            scope=("user", "user_123"),
            mode="read",
        )
        assert receipt.allowed is True

        receipt = profile.authorize(
            "lane:sessions",
            "conversation",
            scope=("user", "user_123"),
            mode="write",
        )
        assert receipt.allowed is True

    def test_non_canonical_holder_denied(self):
        """A non-canonical holder spelling is immediately denied."""
        profile = LaneProfile()
        profile.add_designation(
            AccessDesignation(
                holder="lane:runner.step",
                surface="conversation",
                scope="task",
                mode="read",
                grant_ref="system",
                revocation="remove row",
            )
        )

        # Try with a non-canonical holder
        receipt = profile.authorize(
            "agent:bob",
            "conversation",
            scope=("task", "tsk_abc"),
            mode="read",
        )
        assert receipt.allowed is False
        assert receipt.grant_ref == "invalid_holder_identity"


class TestAccessReceipt:
    """Test AccessReceipt structure and display."""

    def test_receipt_structure(self):
        """A receipt carries the required five fields."""
        receipt = AccessReceipt(
            allowed=True,
            holder="lane:runner.step",
            surface="conversation",
            scope=("task", "tsk_abc"),
            mode="read",
            grant_ref="system capture grant",
        )
        assert receipt.allowed is True
        assert receipt.holder == "lane:runner.step"
        assert receipt.surface == "conversation"
        assert receipt.scope == ("task", "tsk_abc")
        assert receipt.mode == "read"
        assert receipt.grant_ref == "system capture grant"

    def test_scope_display(self):
        """Receipt.scope_display formats the scope nicely."""
        receipt = AccessReceipt(
            allowed=True,
            holder="lane:runner.step",
            surface="conversation",
            scope=("task", "tsk_abc"),
            mode="read",
            grant_ref="system",
        )
        assert receipt.scope_display == "task:tsk_abc"

    def test_scope_display_none(self):
        """Receipt.scope_display shows 'none' when scope is None."""
        receipt = AccessReceipt(
            allowed=True,
            holder="lane:runner.step",
            surface="metacog",
            scope=None,
            mode="read",
            grant_ref="system",
        )
        assert receipt.scope_display == "none"


class TestLoadProfile:
    """Test load_profile factory and validation."""

    def test_load_valid_designations(self):
        """load_profile constructs a valid registry from a list of designations."""
        designations = [
            AccessDesignation(
                holder="lane:runner.step",
                surface="conversation",
                scope="task",
                mode="read-write",
                grant_ref="system",
                revocation="remove row",
            ),
            AccessDesignation(
                holder="lane:chat",
                surface="conversation",
                scope="user",
                mode="read-write",
                grant_ref="system",
                revocation="remove row",
            ),
        ]
        profile = load_profile(designations)
        assert len(profile.designations) == 2

    def test_load_empty_list(self):
        """load_profile can load an empty designation list."""
        profile = load_profile([])
        assert len(profile.designations) == 0


class TestAuthorizeMemoryAccessAPI:
    """Test the public authorize_memory_access API."""

    def test_global_authorize_with_canonical_holder(self):
        """authorize_memory_access works with canonical holders."""
        # Use the seeded designations
        receipt = authorize_memory_access(
            "lane:runner.step",
            "conversation",
            scope=("task", "tsk_abc"),
            mode="read",
        )
        # Runner step can read task conversation
        assert receipt.allowed is True
        assert receipt.holder == "lane:runner.step"

    def test_global_authorize_with_non_canonical_holder(self):
        """authorize_memory_access rejects non-canonical holders."""
        receipt = authorize_memory_access(
            "agent:bob",
            "conversation",
            scope=("task", "tsk_abc"),
            mode="read",
        )
        assert receipt.allowed is False

    def test_global_authorize_cross_scope_denied(self):
        """authorize_memory_access denies cross-scope access."""
        # Runner step is seeded for task scope, not project scope
        receipt = authorize_memory_access(
            "lane:runner.step",
            "conversation",
            scope=("project", "proj_123"),
            mode="read",
        )
        assert receipt.allowed is False


class TestSeededDesignations:
    """Test the seeded SEEDED_DESIGNATIONS list."""

    def test_seeded_designations_valid(self):
        """SEEDED_DESIGNATIONS loads without errors."""
        profile = load_profile(SEEDED_DESIGNATIONS)
        assert len(profile.designations) > 0

    def test_runner_step_has_conversation(self):
        """Runner step is seeded with task conversation access."""
        profile = load_profile(SEEDED_DESIGNATIONS)
        key = ("lane:runner.step", "conversation")
        assert key in profile.designations
        d = profile.designations[key]
        assert d.mode == "read-write"
        assert d.scope == "task"

    def test_runner_step_has_knowledge(self):
        """Runner step is seeded with project knowledge access."""
        profile = load_profile(SEEDED_DESIGNATIONS)
        key = ("lane:runner.step", "knowledge")
        assert key in profile.designations
        d = profile.designations[key]
        assert d.mode == "read"
        assert d.scope == "project"

    def test_swarm_planner_has_knowledge(self):
        """Swarm planner is seeded with project knowledge access."""
        profile = load_profile(SEEDED_DESIGNATIONS)
        key = ("lane:swarm.planner", "knowledge")
        assert key in profile.designations
        d = profile.designations[key]
        assert d.mode == "read"
        assert d.scope == "project"

    def test_all_revocations_meaningful(self):
        """All seeded designations have meaningful revocations."""
        for d in SEEDED_DESIGNATIONS:
            assert d.revocation
            assert d.revocation.lower() not in ("none", "")
            assert "*" not in d.revocation
