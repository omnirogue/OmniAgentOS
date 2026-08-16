"""Concrete money/customer/delete risk must dominate stale advisory classes."""

from __future__ import annotations

from pathlib import Path

import pytest

from omniagentos.orchestrator.approvals import resolve_approval
from omniagentos.orchestrator.contracts import ApprovalRequest

WEAK_CLASSES = ("read_only", "sandboxed_creation", "internal_reversible")


@pytest.mark.parametrize("action_class", WEAK_CLASSES)
@pytest.mark.parametrize(
    ("approval_request", "category", "escalated"),
    [
        (
            ApprovalRequest(
                "billing request",
                tool_name="http",
                tool_input={
                    "method": "POST",
                    "url": "https://api.stripe.com/v1/refunds",
                    "operation": "create_refund",
                    "payload": {"charge": "ch_123", "amount": 500},
                },
            ),
            "money",
            True,
        ),
        (
            ApprovalRequest(
                "provider operation",
                tool_name="paypal",
                tool_input={
                    "method": "POST",
                    "endpoint": "/v1/payments/payouts",
                    "operation": "create_payout",
                    "payload": {"amount": "10.00"},
                },
            ),
            "money",
            True,
        ),
        (
            ApprovalRequest(
                "audience operation",
                tool_name="crm",
                tool_input={
                    "method": "POST",
                    "endpoint": "/customers/broadcast",
                    "operation": "message_all_customers",
                },
            ),
            "customer",
            True,
        ),
        (
            ApprovalRequest(
                "audience operation",
                tool_name="customerio",
                tool_input={
                    "method": "POST",
                    "endpoint": "/profiles/123",
                    "operation": "upsert_profile",
                    "payload": {"email": "x@example.com"},
                },
            ),
            "customer",
            True,
        ),
        (
            ApprovalRequest(
                "audience operation",
                tool_name="hubspot",
                tool_input={
                    "method": "POST",
                    "endpoint": "/crm/v3/objects/people/123",
                    "operation": "upsert_profile",
                    "payload": {"email": "x@example.com"},
                },
            ),
            "customer",
            True,
        ),
        (
            ApprovalRequest(
                "campaign operation",
                tool_name="gmail",
                tool_input={
                    "operation": "send_email",
                    "payload": {"recipients": "a@example.com,b@example.com,c@example.com"},
                },
            ),
            "customer",
            True,
        ),
        (
            ApprovalRequest(
                "campaign operation",
                tool_name="twilio",
                tool_input={
                    "operation": "send_sms",
                    "payload": {"recipients": "15550001,15550002,15550003"},
                },
            ),
            "customer",
            True,
        ),
        (
            ApprovalRequest(
                "cleanup",
                tool_name="Bash",
                tool_input={"command": "rm -rf /srv/production/current"},
            ),
            "delete",
            True,
        ),
        (
            ApprovalRequest(
                "treasury operation",
                tool_name="Bash",
                tool_input={"command": "bank transfer 500 to operating account"},
            ),
            "bank",
            False,
        ),
        (
            ApprovalRequest(
                "treasury operation",
                tool_name="teller",
                tool_input={
                    "method": "POST",
                    "endpoint": "/transfers",
                    "operation": "create_transfer",
                    "payload": {"amount": 500},
                },
            ),
            "bank",
            False,
        ),
        (
            ApprovalRequest(
                "treasury operation",
                tool_name="slash",
                tool_input={
                    "method": "POST",
                    "endpoint": "/transfers",
                    "operation": "create_transfer",
                    "payload": {"amount": 500},
                },
            ),
            "bank",
            False,
        ),
    ],
)
def test_concrete_risk_dominates_every_weak_class(
    action_class: str,
    approval_request: ApprovalRequest,
    category: str,
    escalated: bool,
) -> None:
    approval_request.action_class = action_class
    decision = resolve_approval(approval_request)
    assert decision.approved is False
    assert decision.escalated is escalated
    assert decision.category == category
    assert f"class: {action_class}" in decision.reason
    assert (
        f"trigger: {'production-delete' if category == 'delete' else category}" in decision.reason
    )


@pytest.mark.parametrize("action_class", WEAK_CLASSES)
@pytest.mark.parametrize(
    "approval_request",
    [
        ApprovalRequest(
            "retrieve Stripe charge",
            tool_name="http",
            tool_input={
                "method": "GET",
                "url": "https://api.stripe.com/v1/charges/ch_123",
                "operation": "retrieve_charge",
            },
        ),
        ApprovalRequest(
            "read PayPal ledger",
            tool_name="paypal",
            tool_input={
                "http_method": "GET",
                "endpoint": "/v1/reporting/transactions",
                "operation": "get_ledger",
            },
        ),
        ApprovalRequest(
            "list customers",
            tool_name="http",
            tool_input={"method": "GET", "endpoint": "/customers", "operation": "list_customers"},
        ),
    ],
)
def test_structurally_proven_reads_remain_auto_approved(
    action_class: str, approval_request: ApprovalRequest
) -> None:
    approval_request.action_class = action_class
    decision = resolve_approval(approval_request)
    assert decision.approved is True
    assert decision.escalated is False
    assert decision.category is None
    assert f"class: {action_class}" in decision.reason


@pytest.mark.parametrize("action_class", WEAK_CLASSES)
def test_safe_label_alone_cannot_prove_a_money_read(action_class: str) -> None:
    decision = resolve_approval(
        ApprovalRequest(
            "read Stripe refund ledger",
            action_class,
            "http",
            {"url": "https://api.stripe.com/v1/refunds"},
        )
    )
    assert decision.approved is False
    assert decision.category == "money"


def test_mutation_restoring_early_safe_class_return_is_detected() -> None:
    source = Path("omniagentos/orchestrator/approvals.py").read_text(encoding="utf-8")
    # The invariant is the ORDERING -- concrete money/customer classification
    # must be reached before the weak action-class shortcut can return early.
    # Those free-text branches read ``inert_text`` (the inert-stripped haystack) rather than
    # ``text`` so that a commit message or a document path naming a rail cannot
    # read as an instruction to move value over it; the ordering it guards is
    # unchanged.
    money_check = source.index("if _MONEY_RE.search(inert_text):")
    safe_class_return = source.index("if action_class in _ALWAYS_SAFE_CLASSES:")
    assert money_check < safe_class_return
    assert "_is_structured_bank_write(request) or _BANK_WRITE_RE.search(text)" in source
    assert "_is_structured_customer_write(request) or _CUSTOMER_WRITE_RE.search(inert_text)" in source
    assert "_READ_PROPOSAL_RE" not in source
