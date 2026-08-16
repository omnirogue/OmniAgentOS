"""AC-policy: money-movement is READ-ONLY in AUTO mode; a WRITE hard-stops.

These tests prove the broker boundary: a payment/bank connector may only make
GET/read calls unattended. Any mutating call (a *.transfer, refund, charge,
payout, or any non-read method against a payments-group connector) is refused
before the outbound request is ever issued, and never silently executes.
"""

from __future__ import annotations

from typing import Any

import pytest

from omniagentos.connectors import Capability, HttpSpec, load_registry
from omniagentos.connectors.broker import (
    PAYMENT_GROUPS,
    BrokerDenied,
    authorize,
    call,
    is_money_write,
    is_read_only_query_post,
    validate_request,
)
from omniagentos.contracts import ActionClass

# Every declared money-movement / payment-processing WRITE capability. Each MUST
# be refused by the broker even when the caller holds the grant.
MONEY_WRITES = [
    "stripe_acmeuni.refund",
    "stripe_acmeuni.charge",
    "stripe_initech.refund",
    "stripe_initech.charge",
    "slash_bank.transfer",
    "paypal.refund",
    "paypal.payout",
    "nmi_fortpoint.refund",
    "nmi_glacier.refund",
    "chargeblast.refund",
    "vandelay.refund",
    "vandelay.rebill_edit",
]


def test_is_money_write_reads_are_auto_writes_are_not() -> None:
    """A payment/bank READ (GET/HEAD) is NOT a money write; any mutation IS."""
    read_cap = load_registry().capability("stripe_acmeuni.read")  # group=payments
    assert read_cap.group in PAYMENT_GROUPS
    # Reads stay auto.
    assert is_money_write(read_cap, "GET") is False
    assert is_money_write(read_cap, "HEAD") is False
    # Any mutating method against a payments-group connector is a money-move.
    for method in ("POST", "PUT", "PATCH", "DELETE", "post"):
        assert is_money_write(read_cap, method) is True


def test_is_money_write_ignores_non_payment_groups() -> None:
    """A write to a non-payment connector is not a money-move (handled elsewhere)."""
    crm_cap = load_registry().capability("piedpiper_acmeuni.read")  # group=crm
    assert crm_cap.group not in PAYMENT_GROUPS
    assert is_money_write(crm_cap, "POST") is False


def test_nmi_query_post_is_the_only_payment_post_read_exception() -> None:
    """NMI query.php is safe; its transact.php route remains a money-write."""
    nmi = load_registry().capability("nmi_glacier.read")
    assert is_read_only_query_post(nmi, "POST", "/api/query.php")
    assert not is_money_write(nmi, "POST", "/api/query.php")
    validate_request(nmi, "POST", "/api/query.php")
    assert not is_read_only_query_post(nmi, "POST", "/api/transact.php")
    assert is_money_write(nmi, "POST", "/api/transact.php")
    with pytest.raises(BrokerDenied, match="path_not_allowed"):
        validate_request(nmi, "POST", "/api/transact.php")


@pytest.mark.parametrize("cap_id", MONEY_WRITES)
def test_money_write_capability_is_refused_even_when_granted(cap_id: str) -> None:
    """Holding a money-write grant buys the right to ASK, never to move money."""
    with pytest.raises(BrokerDenied) as exc:
        authorize(cap_id, granted=[cap_id])
    # Refused at gate 2 (consequential) -- the money-move never authorizes.
    assert exc.value.reason == "requires_human_approval"


def _synthetic_transfer_cap() -> Capability:
    """A callable payments-group WRITE cap, softer than consequential on purpose.

    Simulates a future/misconfigured money write that slipped past the per-cap
    action_class gate, to prove the GROUP+method money-write gate still stops it.
    """
    return Capability(
        id="fake_bank.transfer",
        connector="fake_bank",
        group="payments",
        label="fake transfer",
        action_class=ActionClass.INTERNAL_REVERSIBLE,  # deliberately NOT consequential
        http=HttpSpec(base_url="https://example.test", methods=["POST"]),
    )


def test_broker_call_hard_stops_a_money_write_before_any_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """call() refuses a payment WRITE and never issues the outbound request."""
    cap = _synthetic_transfer_cap()
    monkeypatch.setattr("omniagentos.connectors.broker.authorize", lambda *a, **k: cap)
    monkeypatch.setattr("omniagentos.connectors.broker.validate_request", lambda *a, **k: None)

    sent: list[Any] = []

    def _boom(*args: Any, **kwargs: Any) -> Any:  # pragma: no cover - must never run
        sent.append((args, kwargs))
        raise AssertionError("a money-move must NEVER reach the network in auto mode")

    import httpx

    monkeypatch.setattr(httpx, "request", _boom)

    with pytest.raises(BrokerDenied) as exc:
        call("fake_bank.transfer", ["fake_bank.transfer"], method="POST", path="/transfers")
    assert exc.value.reason == "money_write_requires_approval"
    assert sent == []  # the request was refused, not silently sent


def test_broker_call_allows_a_payment_read_with_explicit_grant(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A payment/bank READ (GET) is auto: the money-write gate does not fire."""
    read_cap = load_registry().capability("stripe_acmeuni.read")
    monkeypatch.setattr("omniagentos.connectors.broker.authorize", lambda *a, **k: read_cap)
    monkeypatch.setattr("omniagentos.connectors.broker.validate_request", lambda *a, **k: None)
    monkeypatch.setattr(
        "omniagentos.connectors.broker._auth_headers",
        lambda *a, **k: ({}, {}, None),
    )

    class _Resp:
        status_code = 200
        is_success = True

        def json(self) -> dict[str, Any]:
            return {"ok": True}

    import httpx

    monkeypatch.setattr(httpx, "request", lambda *a, **k: _Resp())

    out = call("stripe_acmeuni.read", ["stripe_acmeuni.read"], method="GET", path="/v1/charges")
    assert out["ok"] is True  # the read went through: money reads stay auto
