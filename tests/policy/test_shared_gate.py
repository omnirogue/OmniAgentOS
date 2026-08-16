"""Tests for approval_satisfies_gate, the shared approval gate logic (DR-002)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from omniagentos.contracts import ApprovalState, PolicyDecision, utc_now_iso
from omniagentos.policy import ApprovalGateResult, approval_satisfies_gate


class TestApprovalGateHumanOkLogic:
    """Test the human_ok gate (always_human check)."""

    def test_not_always_human_returns_ok_regardless_of_decided_by(self) -> None:
        """When always_human is False, any decided_by is OK."""
        decision = PolicyDecision(requires_approval=True, always_human=False)
        now = utc_now_iso()

        # Empty decided_by
        approval = {"state": ApprovalState.PENDING.value, "decided_by": ""}
        result = approval_satisfies_gate(approval, decision, actor="runner:w1", now_iso=now)
        assert result.human_ok is True

        # None decided_by
        approval = {"state": ApprovalState.PENDING.value, "decided_by": None}
        result = approval_satisfies_gate(approval, decision, actor="runner:w1", now_iso=now)
        assert result.human_ok is True

        # Missing decided_by
        approval = {"state": ApprovalState.PENDING.value}
        result = approval_satisfies_gate(approval, decision, actor="runner:w1", now_iso=now)
        assert result.human_ok is True

    def test_always_human_requires_non_empty_decided_by(self) -> None:
        """When always_human is True, decided_by must be present and non-empty after strip."""
        decision = PolicyDecision(requires_approval=True, always_human=True)
        now = utc_now_iso()

        # Empty string → not ok
        approval = {"state": ApprovalState.PENDING.value, "decided_by": ""}
        result = approval_satisfies_gate(approval, decision, actor="human", now_iso=now)
        assert result.human_ok is False

        # Whitespace only → not ok (stripped is empty)
        approval = {"state": ApprovalState.PENDING.value, "decided_by": "   "}
        result = approval_satisfies_gate(approval, decision, actor="human", now_iso=now)
        assert result.human_ok is False

        # None → not ok
        approval = {"state": ApprovalState.PENDING.value, "decided_by": None}
        result = approval_satisfies_gate(approval, decision, actor="human", now_iso=now)
        assert result.human_ok is False

        # Missing → not ok
        approval = {"state": ApprovalState.PENDING.value}
        result = approval_satisfies_gate(approval, decision, actor="human", now_iso=now)
        assert result.human_ok is False

    def test_always_human_rejects_runner_signed_decisions(self) -> None:
        """When always_human is True, decided_by starting with 'runner:' is rejected."""
        decision = PolicyDecision(requires_approval=True, always_human=True)
        now = utc_now_iso()

        # "runner:x" → not ok
        approval = {"state": ApprovalState.PENDING.value, "decided_by": "runner:w1"}
        result = approval_satisfies_gate(approval, decision, actor="w1", now_iso=now)
        assert result.human_ok is False

        # "Runner:x" (uppercase) → not ok (case-insensitive check)
        approval = {"state": ApprovalState.PENDING.value, "decided_by": "Runner:w1"}
        result = approval_satisfies_gate(approval, decision, actor="w1", now_iso=now)
        assert result.human_ok is False

        # "RUNNER:x" (all caps) → not ok
        approval = {"state": ApprovalState.PENDING.value, "decided_by": "RUNNER:w1"}
        result = approval_satisfies_gate(approval, decision, actor="w1", now_iso=now)
        assert result.human_ok is False

    def test_always_human_rejects_actor_self_approval(self) -> None:
        """When always_human is True, decided_by == actor is rejected."""
        decision = PolicyDecision(requires_approval=True, always_human=True)
        now = utc_now_iso()

        # decided_by == actor → not ok
        approval = {"state": ApprovalState.PENDING.value, "decided_by": "alice"}
        result = approval_satisfies_gate(approval, decision, actor="alice", now_iso=now)
        assert result.human_ok is False

    @pytest.mark.parametrize(
        "decided_by",
        ["bot", "automation", "system", "session-supervisor", "ci-bot", "bot:ci", "system:cron"],
    )
    def test_always_human_rejects_automation_identities(self, decided_by: str) -> None:
        """SEC-002: bot/automation/system/*-bot deciders never satisfy always_human."""
        decision = PolicyDecision(requires_approval=True, always_human=True)
        approval = {"state": ApprovalState.APPROVED.value, "decided_by": decided_by}
        result = approval_satisfies_gate(
            approval, decision, actor="session-supervisor", now_iso=utc_now_iso()
        )
        assert result.human_ok is False

    def test_always_human_accepts_real_human_decision(self) -> None:
        """When always_human is True and decided_by is a real human, return ok."""
        decision = PolicyDecision(requires_approval=True, always_human=True)
        now = utc_now_iso()

        # Real human name → ok
        approval = {"state": ApprovalState.PENDING.value, "decided_by": "alice"}
        result = approval_satisfies_gate(approval, decision, actor="runner:w1", now_iso=now)
        assert result.human_ok is True

        # With whitespace (stripped) → ok
        approval = {"state": ApprovalState.PENDING.value, "decided_by": "  alice  "}
        result = approval_satisfies_gate(approval, decision, actor="runner:w1", now_iso=now)
        assert result.human_ok is True

        # Different from actor → ok
        approval = {"state": ApprovalState.PENDING.value, "decided_by": "alice"}
        result = approval_satisfies_gate(approval, decision, actor="bob", now_iso=now)
        assert result.human_ok is True


class TestApprovalGateExpiryLogic:
    """Test the expired gate (expiry check)."""

    def test_approved_past_expiry_is_expired(self) -> None:
        """T-CODE-002: an APPROVED approval past its expiry is not usable as resume authority."""
        decision = PolicyDecision(requires_approval=True, always_human=False)
        past = (datetime.now(UTC) - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
        approval = {
            "state": ApprovalState.APPROVED.value,
            "expires_at": past,
            "decided_by": "alice",
        }
        result = approval_satisfies_gate(
            approval, decision, actor="runner:w1", now_iso=utc_now_iso()
        )
        assert result.expired is True

    def test_terminal_decisions_never_expire(self) -> None:
        """REJECTED/EXPIRED keep their own terminal handling and are not re-reported expired."""
        decision = PolicyDecision(requires_approval=True, always_human=False)
        past = (datetime.now(UTC) - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")

        approval = {"state": ApprovalState.REJECTED.value, "expires_at": past}
        result = approval_satisfies_gate(
            approval, decision, actor="runner:w1", now_iso=utc_now_iso()
        )
        assert result.expired is False

        approval = {"state": ApprovalState.EXPIRED.value, "expires_at": past}
        result = approval_satisfies_gate(
            approval, decision, actor="runner:w1", now_iso=utc_now_iso()
        )
        assert result.expired is False

    def test_pending_without_expiry_never_expires(self) -> None:
        """PENDING approvals without expires_at are never marked as expired."""
        decision = PolicyDecision(requires_approval=True, always_human=False)
        now = utc_now_iso()

        # PENDING, no expires_at → not expired
        approval = {"state": ApprovalState.PENDING.value}
        result = approval_satisfies_gate(approval, decision, actor="runner:w1", now_iso=now)
        assert result.expired is False

        # PENDING, expires_at is None → not expired
        approval = {"state": ApprovalState.PENDING.value, "expires_at": None}
        result = approval_satisfies_gate(approval, decision, actor="runner:w1", now_iso=now)
        assert result.expired is False

    def test_pending_future_expiry_not_expired(self) -> None:
        """PENDING approvals with future expiry are not marked as expired."""
        decision = PolicyDecision(requires_approval=True, always_human=False)

        # Expiry in 1 hour → not expired
        future = (datetime.now(UTC) + timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
        now = utc_now_iso()
        approval = {"state": ApprovalState.PENDING.value, "expires_at": future}
        result = approval_satisfies_gate(approval, decision, actor="runner:w1", now_iso=now)
        assert result.expired is False

    def test_pending_past_expiry_is_expired(self) -> None:
        """PENDING approvals with past expiry are marked as expired."""
        decision = PolicyDecision(requires_approval=True, always_human=False)

        # Expiry 1 hour ago → expired
        past = (datetime.now(UTC) - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
        now = utc_now_iso()
        approval = {"state": ApprovalState.PENDING.value, "expires_at": past}
        result = approval_satisfies_gate(approval, decision, actor="runner:w1", now_iso=now)
        assert result.expired is True

    def test_expiry_uses_string_comparison(self) -> None:
        """Expiry check uses string comparison (ISO timestamps sort correctly)."""
        decision = PolicyDecision(requires_approval=True, always_human=False)

        # Test exact boundary: now_iso == expires_at → expired (using <=)
        now_iso = "2026-07-20T12:30:45Z"
        approval = {"state": ApprovalState.PENDING.value, "expires_at": "2026-07-20T12:30:45Z"}
        result = approval_satisfies_gate(approval, decision, actor="runner:w1", now_iso=now_iso)
        assert result.expired is True

        # One second before expiry → not expired
        approval = {"state": ApprovalState.PENDING.value, "expires_at": "2026-07-20T12:30:46Z"}
        result = approval_satisfies_gate(approval, decision, actor="runner:w1", now_iso=now_iso)
        assert result.expired is False

        # One second after expiry → expired
        approval = {"state": ApprovalState.PENDING.value, "expires_at": "2026-07-20T12:30:44Z"}
        result = approval_satisfies_gate(approval, decision, actor="runner:w1", now_iso=now_iso)
        assert result.expired is True


class TestApprovalGateCombined:
    """Test combinations of human_ok and expired."""

    def test_result_captures_both_gates_independently(self) -> None:
        """Both gates are checked independently; both can be True or False."""
        decision = PolicyDecision(requires_approval=True, always_human=True)

        # Both OK
        approval = {
            "state": ApprovalState.PENDING.value,
            "decided_by": "alice",
            "expires_at": (datetime.now(UTC) + timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        }
        result = approval_satisfies_gate(
            approval, decision, actor="runner:w1", now_iso=utc_now_iso()
        )
        assert result.human_ok is True
        assert result.expired is False

        # human_ok but expired
        approval = {
            "state": ApprovalState.PENDING.value,
            "decided_by": "alice",
            "expires_at": (datetime.now(UTC) - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        }
        result = approval_satisfies_gate(
            approval, decision, actor="runner:w1", now_iso=utc_now_iso()
        )
        assert result.human_ok is True
        assert result.expired is True

        # human_ok is False, but not expired
        approval = {
            "state": ApprovalState.PENDING.value,
            "decided_by": "runner:w1",
            "expires_at": (datetime.now(UTC) + timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        }
        result = approval_satisfies_gate(
            approval, decision, actor="runner:w1", now_iso=utc_now_iso()
        )
        assert result.human_ok is False
        assert result.expired is False

    def test_result_is_frozen_pydantic_model(self) -> None:
        """ApprovalGateResult is a frozen pydantic model."""
        decision = PolicyDecision(requires_approval=True, always_human=False)
        approval = {"state": ApprovalState.PENDING.value}
        result = approval_satisfies_gate(
            approval, decision, actor="runner:w1", now_iso=utc_now_iso()
        )

        assert isinstance(result, ApprovalGateResult)
        # Frozen model: cannot modify
        with pytest.raises((TypeError, ValueError)):
            result.human_ok = False  # type: ignore[misc]
