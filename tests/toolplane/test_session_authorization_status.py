"""Test session tool call observation records status='allowed' for gate authorization."""

from __future__ import annotations

import pytest

from omniagentos.toolplane.session import denial_code, observe_session_tool_call


def test_session_authorization_status_is_allowed() -> None:
    obs = observe_session_tool_call(
        tool_name="Read",
        session_id="ses_auth_test",
        decision="allow",
        duration_ms=15,
        reason=None,
    )

    assert obs["status"] == "allowed"
    assert obs["status"] != "success"
    assert obs["ok"] is True
    assert obs["decision"] == "allow"
    assert obs["error"] is None


def test_session_authorization_status_deny() -> None:
    obs = observe_session_tool_call(
        tool_name="Write",
        session_id="ses_auth_test",
        decision="deny",
        duration_ms=10,
        reason="parked: approval required",
    )

    assert obs["status"] == "denied"
    assert obs["ok"] is False
    assert obs["decision"] == "deny"
    assert obs["error"] == "approval_required"


@pytest.mark.parametrize(
    ("reason", "expected"),
    [
        (
            "parked per finance-only policy (class: irreversible; trigger: production-delete)",
            "approval_required",
        ),
        (
            "refused per finance-only policy (class: irreversible; trigger: bank)",
            "orchestrator_refused",
        ),
        (
            "auto-approved per finance-only policy "
            "(class: irreversible; hard_stop: delete; scope: local_temp)",
            "orchestrator_auto_approved",
        ),
        ("orchestrator escalated (money)", "orchestrator_escalated"),
    ],
)
def test_finance_only_reason_prefixes_are_reduced_without_leaks(reason: str, expected: str) -> None:
    assert denial_code(reason) == expected
