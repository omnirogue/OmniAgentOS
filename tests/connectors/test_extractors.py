"""Request-body extractors (TN.6): bounded facts out of send/write bodies.

Extractors OBSERVE every brokered call and never block one -- except the strict
meta field boundary, which is fail-closed BY DESIGN: meta_*.launch and
meta_*.budget_change are the same POST /{object-id} on the wire, and only the
allowed-field sets here keep "flip a campaign live" and "set its budget" apart.

The end-to-end tests at the bottom mock httpx; no test ever reaches a real API.
"""

from __future__ import annotations

import base64
from pathlib import Path
from typing import Any

import httpx
import pytest

from omniagentos.connectors.broker import BrokerDenied, call
from omniagentos.connectors.extractors import CallBounds, extract_call_bounds


def _b64url_mime(mime: str) -> str:
    """Encode exactly like a gmail.send caller would: base64url, no padding."""
    return base64.urlsafe_b64encode(mime.encode()).decode().rstrip("=")


# --- gmail.send: recipients out of the raw MIME -------------------------------


def test_gmail_send_extracts_to_and_cc_from_a_real_mime() -> None:
    """A small real MIME round-trips into recipients + recipient_count."""
    mime = (
        "From: sender@example.com\r\n"
        "To: alice@example.com, Bob <bob@example.com>\r\n"
        "Cc: carol@example.com\r\n"
        "Subject: weekend numbers\r\n"
        "\r\n"
        "See the attached sheet.\r\n"
    )
    bounds = extract_call_bounds(
        "gmail.send",
        "POST",
        "/gmail/v1/users/me/messages/send",
        {"raw": _b64url_mime(mime), "threadId": "t-1"},  # threadId is ignored
    )
    assert bounds.recipients == ("alice@example.com", "bob@example.com", "carol@example.com")
    assert bounds.recipient_count == 3


def test_gmail_account_variants_dispatch_the_same_way() -> None:
    """gmail_ownera.send (and any gmail_* send) shares the extractor."""
    mime = "To: owner@example.com\r\n\r\nhi\r\n"
    for cap_id in ("gmail.send", "gmail_ownera.send"):
        bounds = extract_call_bounds(cap_id, "POST", "/x", {"raw": _b64url_mime(mime)})
        assert bounds.recipients == ("owner@example.com",)
        assert bounds.recipient_count == 1


@pytest.mark.parametrize(
    "body",
    [
        None,  # no body at all
        "raw-string-body",  # not a mapping
        {},  # no raw field
        {"raw": 123},  # raw is not a string
        {"raw": "!!!"},  # undecodable / decodes to nothing MIME-ish
        {"raw": _b64url_mime("garbage with no headers")},
    ],
)
def test_gmail_send_unparseable_bodies_yield_empty_bounds_and_never_raise(body: Any) -> None:
    """Unknown recipients is the truthful answer; the grant layer treats unknown
    as outside any allowed target set. Extraction must NEVER raise here."""
    bounds = extract_call_bounds("gmail.send", "POST", "/x", body)
    assert bounds == CallBounds()


# --- piedpiper conversation_send: contactId + mutated field names -------------------


def test_piedpiper_conversation_send_bounds() -> None:
    bounds = extract_call_bounds(
        "piedpiper_acmeuni.conversation_send",
        "POST",
        "/conversations/messages",
        {"type": "SMS", "contactId": "cnt_123", "message": "appointment reminder text"},
    )
    assert bounds.recipients == ("cnt_123",)
    assert bounds.recipient_count == 1
    assert bounds.mutates_fields == ("contactId", "message", "type")
    # Bounds carry field NAMES, never values: the message text stays out of them.
    assert "appointment reminder text" not in str(bounds.as_dict())


def test_piedpiper_conversation_send_without_a_contact_id() -> None:
    """No contactId means unknown recipient; the field names are still recorded."""
    bounds = extract_call_bounds(
        "piedpiper_initech.conversation_send",
        "POST",
        "/conversations/messages",
        {"type": "Email", "message": "hi"},
    )
    assert bounds.recipients == ()
    assert bounds.recipient_count == 0
    assert bounds.mutates_fields == ("message", "type")


# --- customerio trigger_broadcast: segment ids and explicit emails ------------


def test_customerio_broadcast_bounds_from_segments_and_emails() -> None:
    bounds = extract_call_bounds(
        "customerio_acmeuni.trigger_broadcast",
        "POST",
        "/v1/broadcasts/12/triggers",
        {"recipients": {"segment": {"ids": [4, 9]}}, "emails": ["vip@example.com"]},
    )
    assert bounds.recipients == ("4", "9", "vip@example.com")
    assert bounds.recipient_count == 3


def test_customerio_broadcast_bounds_from_emails_only() -> None:
    bounds = extract_call_bounds(
        "customerio_initech.trigger_broadcast",
        "POST",
        "/v1/broadcasts/7/triggers",
        {"emails": ["a@example.com", "b@example.com"]},
    )
    assert bounds.recipients == ("a@example.com", "b@example.com")
    assert bounds.recipient_count == 2


def test_customerio_broadcast_garbage_body_yields_empty_bounds() -> None:
    assert (
        extract_call_bounds("customerio_acmeuni.trigger_broadcast", "POST", "/x", "not-a-mapping")
        == CallBounds()
    )
    assert (
        extract_call_bounds(
            "customerio_acmeuni.trigger_broadcast",
            "POST",
            "/x",
            {"recipients": {"segment": "oops"}},  # malformed nesting is skipped
        )
        == CallBounds()
    )


# --- meta: the strict field boundary ------------------------------------------


def test_meta_launch_allows_only_status_json_or_form() -> None:
    """launch may flip an object live via JSON or form fields -- nothing else."""
    for body, form in (({"status": "ACTIVE"}, None), (None, {"status": "PAUSED"})):
        bounds = extract_call_bounds("meta_acmeuni.launch", "POST", "/238123", body, form)
        assert bounds.mutates_fields == ("status",)
        assert bounds.resource_ids == ("238123",)
        assert bounds.spend_usd is None
        assert bounds.account_id is None


def test_meta_launch_refuses_budget_fields() -> None:
    """The cross-boundary case: a launch body that tries to set a budget."""
    with pytest.raises(BrokerDenied) as exc:
        extract_call_bounds(
            "meta_acmeuni.launch",
            "POST",
            "/238123",
            {"status": "ACTIVE", "daily_budget": 5000},
        )
    assert exc.value.reason == "body_field_not_allowed"
    assert exc.value.cap_id == "meta_acmeuni.launch"


def test_meta_budget_change_priced_in_usd_from_minor_units() -> None:
    """Meta budgets are cents: 5000 means $50.00."""
    bounds = extract_call_bounds(
        "meta_acmeuni.budget_change",
        "POST",
        "/238123",
        None,
        {"daily_budget": "5000"},
    )
    assert bounds.spend_usd == 50.0
    assert bounds.mutates_fields == ("daily_budget",)
    bounds = extract_call_bounds(
        "meta_initech.budget_change",
        "POST",
        "/238123",
        {"lifetime_budget": 100000},
    )
    assert bounds.spend_usd == 1000.0


def test_meta_budget_change_bid_amount_is_not_a_budget() -> None:
    """A bid ceiling is allowed but does not count as spend."""
    bounds = extract_call_bounds(
        "meta_globex.budget_change",
        "POST",
        "/238123",
        {"bid_amount": 500},
    )
    assert bounds.spend_usd is None
    assert bounds.mutates_fields == ("bid_amount",)


def test_meta_budget_change_refuses_status_and_foreign_keys() -> None:
    """budget_change can never flip a status, nor carry unreviewed fields."""
    for body in ({"status": "ACTIVE"}, {"daily_budget": 5000, "campaign_id": "x"}):
        with pytest.raises(BrokerDenied) as exc:
            extract_call_bounds("meta_acmeuni.budget_change", "POST", "/238123", body)
        assert exc.value.reason == "body_field_not_allowed"


def test_meta_strict_refuses_an_uninspectable_body() -> None:
    """Fail-closed both ways: a body whose fields cannot be seen cannot be allowed."""
    with pytest.raises(BrokerDenied) as exc:
        extract_call_bounds("meta_acmeuni.launch", "POST", "/238123", "status=ACTIVE")
    assert exc.value.reason == "body_field_not_allowed"


def test_meta_account_id_only_from_act_paths() -> None:
    """An /act_<id> path names its (env-backed) ad account; an object-id path
    carries no account signal, so None is the truthful answer."""
    bounds = extract_call_bounds(
        "meta_acmeuni.budget_change",
        "POST",
        "/act_123456789",
        {"daily_budget": 100},
    )
    assert bounds.account_id == "123456789"
    assert bounds.resource_ids == ("act_123456789",)
    assert (
        extract_call_bounds(
            "meta_acmeuni.budget_change", "POST", "/238123", {"daily_budget": 100}
        ).account_id
        is None
    )


def test_meta_launch_and_budget_change_share_path_shape_but_not_fields() -> None:
    """Same wire shape, opposite field sets: each refuses the other's keys."""
    path = "/999"
    extract_call_bounds("meta_acmeuni.launch", "POST", path, {"status": "ACTIVE"})
    extract_call_bounds("meta_acmeuni.budget_change", "POST", path, {"daily_budget": 100})
    with pytest.raises(BrokerDenied):
        extract_call_bounds("meta_acmeuni.launch", "POST", path, {"daily_budget": 100})
    with pytest.raises(BrokerDenied):
        extract_call_bounds("meta_acmeuni.budget_change", "POST", path, {"status": "ACTIVE"})


def test_unknown_capability_yields_empty_bounds_and_never_blocks() -> None:
    assert extract_call_bounds("stripe_acmeuni.read", "GET", "/v1/charges", None) == CallBounds()
    assert extract_call_bounds("no_such_cap.anything", "POST", "/x", {"a": 1}) == CallBounds()


# --- broker wiring: bounds in the result dict ---------------------------------


class _Resp:
    status_code = 200
    is_success = True

    def json(self) -> dict[str, Any]:
        return {"ok": True}


def _stub_auth_and_network(monkeypatch: pytest.MonkeyPatch) -> list[Any]:
    """Credentials stay out of the test: stub auth headers, capture the request."""
    monkeypatch.setattr(
        "omniagentos.connectors.broker._auth_headers",
        lambda *a, **k: ({}, {}, None),
    )
    sent: list[Any] = []

    def _fake_request(*args: Any, **kwargs: Any) -> _Resp:
        sent.append((args, kwargs))
        return _Resp()

    monkeypatch.setattr(httpx, "request", _fake_request)
    return sent


def test_broker_call_returns_bounds_for_a_send(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """End to end: real authorize (store-backed grant) + real allowlist, mocked network."""
    from omniagentos.db.store import SqliteStore
    from omniagentos.grants import GrantsStore

    _stub_auth_and_network(monkeypatch)
    grants = GrantsStore(SqliteStore(str(tmp_path / "extractors-grants.db")))
    grant = grants.create_grant(
        "piedpiper_acmeuni.conversation_send",
        approval_id="apr_extractor_1",
        max_actions=1,
        max_spend_usd=1.0,
        expires_at="2099-01-01T00:00:00+00:00",
        target_set=["cnt_9"],
        metadata={"generation": 0, "action_class": "consequential"},
    )
    out = call(
        "piedpiper_acmeuni.conversation_send",
        ["piedpiper_acmeuni.conversation_send"],
        method="POST",
        path="/conversations/messages",
        body={"type": "SMS", "contactId": "cnt_9", "message": "hi"},
        approval_token=grant["id"],
        grant_store=grants,
        generation=0,
    )
    assert out["ok"] is True
    assert out["bounds"]["recipients"] == ["cnt_9"]
    assert out["bounds"]["recipient_count"] == 1
    assert out["bounds"]["mutates_fields"] == ["contactId", "message", "type"]
    assert out["bounds"]["spend_usd"] is None


def test_broker_call_bounds_are_empty_for_a_plain_read(monkeypatch: pytest.MonkeyPatch) -> None:
    """The new return field is additive: reads are untouched, bounds truthful-empty."""
    _stub_auth_and_network(monkeypatch)
    out = call("stripe_acmeuni.read", ["stripe_acmeuni.read"], method="GET", path="/v1/charges")
    assert out["ok"] is True
    assert out["bounds"] == {
        "recipients": [],
        "recipient_count": 0,
        "spend_usd": None,
        "account_id": None,
        "resource_ids": [],
        "mutates_fields": [],
    }


def test_meta_launch_bounds_unit_and_strict_field_refusal() -> None:
    """Meta field boundary is unit-tested here when registry has no call path.

    Kimi's e2e used a callable meta_acmeuni.launch; this tree's registry may leave
    that capability as no_call_path. Extractors themselves still enforce the
    launch-vs-budget field sets fail-closed.
    """
    from omniagentos.connectors.extractors import extract_call_bounds

    ok = extract_call_bounds("meta_acmeuni.launch", "POST", "/238123", {"status": "ACTIVE"})
    assert ok.mutates_fields == ("status",)
    assert ok.resource_ids == ("238123",)

    with pytest.raises(BrokerDenied) as exc:
        extract_call_bounds(
            "meta_acmeuni.launch",
            "POST",
            "/238123",
            {"status": "ACTIVE", "daily_budget": 5000},
        )
    assert exc.value.reason == "body_field_not_allowed"


def test_duplicate_recipient_headers_are_all_captured() -> None:
    """Two To: headers -- Gmail delivers to both, so both must be bounded."""
    import base64

    from omniagentos.connectors.extractors import _gmail_send_bounds

    mime = (
        b"To: approved@corp.com\r\nTo: victim@evil.com\r\n"
        b"Cc: c1@x.com\r\nCc: c2@x.com\r\nSubject: t\r\n\r\nbody"
    )
    raw = base64.urlsafe_b64encode(mime).decode().rstrip("=")
    bounds = _gmail_send_bounds({"raw": raw})
    assert set(bounds.recipients) == {
        "approved@corp.com",
        "victim@evil.com",
        "c1@x.com",
        "c2@x.com",
    }
    assert bounds.recipient_count == 4


# --- unknown spend must not be recorded as $0 against a grant cap -------------


def _callable_budget_change_registry(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin a reviewed HTTP path on meta_acmeuni.budget_change for broker e2e tests.

    Production registry leaves budget_change uncallable until a route is reviewed;
    these tests need a real durable-consume path without network.
    """
    from omniagentos.connectors import HttpSpec, load_registry

    real = load_registry()
    cap = real.capability("meta_acmeuni.budget_change").model_copy(
        update={
            "http": HttpSpec(
                base_url="https://graph.example.test/v21.0",
                methods=["POST"],
                path_prefixes=["/"],
            )
        }
    )

    class _Reg:
        def capability(self, cap_id: str) -> Any:
            if cap_id == "meta_acmeuni.budget_change":
                return cap
            return real.capability(cap_id)

        @property
        def groups(self) -> Any:
            return real.groups

        @property
        def connectors(self) -> Any:
            return real.connectors

    monkeypatch.setattr("omniagentos.connectors.broker.load_registry", lambda: _Reg())


@pytest.mark.parametrize(
    "body",
    [
        {"daily_budget": "not-a-number"},  # present but unparseable
        {"daily_budget": None},  # present but null
        {"lifetime_budget": -5000},  # nonsense budget
        {"daily_budget": {"nested": True}},  # non-numeric mapping
    ],
)
def test_unknown_budget_spend_is_refused_not_recorded_as_zero(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, body: dict[str, Any]
) -> None:
    """Extractor contract: unknown spend is outside any spend bound.

    The defect class is non-result-as-favourable: ``bounds.spend_usd is None``
    (could not measure) coerced to ``0.0`` via ``float(bounds.spend_usd or 0)``
    so a budget write against a $4 cap is consumed as free $0 spend. That is
    exactly how real spend can blow a cap while the grant ledger reports success
    under budget.

    Counterfeit that would fake this fix: still mapping None→0.0, or only
    refusing when the whole body is missing (the cases above all claim a budget
    key). The grant must remain unconsumed after refusal.
    """
    from omniagentos.db.store import SqliteStore
    from omniagentos.grants import GrantsStore

    _callable_budget_change_registry(monkeypatch)
    sent = _stub_auth_and_network(monkeypatch)
    grants = GrantsStore(SqliteStore(str(tmp_path / "unknown-spend.db")))
    grant = grants.create_grant(
        "meta_acmeuni.budget_change",
        approval_id="apr_unknown_spend",
        max_actions=1,
        max_spend_usd=4.0,  # tight cap: free $0 consume would always pass
        expires_at="2099-01-01T00:00:00+00:00",
        target_set=[],
        metadata={"generation": 0, "action_class": "consequential"},
    )

    with pytest.raises(BrokerDenied) as exc:
        call(
            "meta_acmeuni.budget_change",
            ["meta_acmeuni.budget_change"],
            method="POST",
            path="/238123456",
            body=body,
            approval_token=grant["id"],
            grant_store=grants,
            generation=0,
        )

    assert exc.value.reason in {
        "spend_unknown",
        "grant_broke_out",
        "grant_refused",
    }, f"unexpected reason {exc.value.reason!r}: {exc.value.detail!r}"
    assert "spend" in (exc.value.detail or exc.value.reason).lower() or exc.value.reason == (
        "spend_unknown"
    )
    # Network must never see the call, and the one-shot grant must stay live.
    assert sent == []
    live = grants.get_grant(grant["id"])
    assert live is not None
    assert int(live.get("actions_used") or 0) == 0
    # Remaining spend must still be the full $4 — not reduced by a fake $0 consume.
    assert float(live.get("max_spend_usd") or 0) == 4.0


def test_known_budget_spend_still_consumes_under_cap(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Control: a parseable budget under the cap still authorizes and consumes.

    Prevents a counterfeit fix that refuses every durable budget_change.
    Meta minor units: 300 cents = $3.00 under a $4.00 cap.
    """
    from omniagentos.db.store import SqliteStore
    from omniagentos.grants import GrantsStore

    _callable_budget_change_registry(monkeypatch)
    sent = _stub_auth_and_network(monkeypatch)
    grants = GrantsStore(SqliteStore(str(tmp_path / "known-spend.db")))
    grant = grants.create_grant(
        "meta_acmeuni.budget_change",
        approval_id="apr_known_spend",
        max_actions=1,
        max_spend_usd=4.0,
        expires_at="2099-01-01T00:00:00+00:00",
        target_set=[],
        metadata={"generation": 0, "action_class": "consequential"},
    )

    out = call(
        "meta_acmeuni.budget_change",
        ["meta_acmeuni.budget_change"],
        method="POST",
        path="/238123456",
        body={"daily_budget": 300},  # $3.00
        approval_token=grant["id"],
        grant_store=grants,
        generation=0,
    )
    assert out["ok"] is True
    assert out["bounds"]["spend_usd"] == 3.0
    assert len(sent) == 1
    live = grants.get_grant(grant["id"])
    assert live is not None
    assert int(live.get("actions_used") or 0) == 1


def test_bid_only_budget_change_is_zero_spend_not_unknown(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """bid_amount is allowed and is not a budget — spend stays legitimately 0.

    Counterfeit that refuses every ``spend_usd is None`` would break bid-only
    updates and non-spend durable sends (gmail/piedpiper).
    """
    from omniagentos.db.store import SqliteStore
    from omniagentos.grants import GrantsStore

    _callable_budget_change_registry(monkeypatch)
    sent = _stub_auth_and_network(monkeypatch)
    grants = GrantsStore(SqliteStore(str(tmp_path / "bid-only.db")))
    grant = grants.create_grant(
        "meta_acmeuni.budget_change",
        approval_id="apr_bid_only",
        max_actions=1,
        max_spend_usd=4.0,
        expires_at="2099-01-01T00:00:00+00:00",
        target_set=[],
        metadata={"generation": 0, "action_class": "consequential"},
    )

    out = call(
        "meta_acmeuni.budget_change",
        ["meta_acmeuni.budget_change"],
        method="POST",
        path="/238123456",
        body={"bid_amount": 500},
        approval_token=grant["id"],
        grant_store=grants,
        generation=0,
    )
    assert out["ok"] is True
    assert out["bounds"]["spend_usd"] is None
    assert len(sent) == 1
