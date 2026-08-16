"""Tests for run-start spend authorization → bounded grant minting."""

from __future__ import annotations

import base64
import json
from pathlib import Path

import pytest

from omniagentos.connectors import load_registry
from omniagentos.contracts import utc_now_iso
from omniagentos.db.store import SqliteStore
from omniagentos.grants import GrantsStore
from omniagentos.grants.run_authorization import (
    ABSOLUTE_MAX_CEILING_USD,
    OWNER_PRINCIPAL,
    CapabilityAsk,
    mint_run_grants,
    parse_spend_authorization,
)

# Freeze the mint clock to *now* — the same wall clock the grant store reads at
# consume/validate time. A hardcoded past instant made every minted grant expire
# mid-day: mint stamps expires_at = NOW + horizon_hours (6h) while try_consume /
# validate_approval_token read utc_now_iso(), so any run after (NOW + 6h) UTC saw
# "expired". Deriving NOW from utc_now_iso() keeps mint and validation on one clock.
NOW = utc_now_iso()

#: The real connector catalogue; ``create_grant`` resolves the authoritative
#: action class from ``$OMNIAGENTOS_VAR_DIR/connectors.yaml``.
_REGISTRY_SRC = Path(__file__).resolve().parents[2] / "configs" / "connectors.yaml"


@pytest.fixture
def store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> GrantsStore:
    # Per-test isolated var root so the append-only audit log (and the registry
    # read by create_grant) never touch the operator's live tree. The registry
    # lives at ``$VAR_DIR/connectors.yaml``, so seed the real catalogue there —
    # a partial VAR override with no registry is the documented breach in
    # tests/conftest._isolate_var_and_reflexion. Set BOTH VAR names (never one)
    # and clear the cached registry so the seeded copy is the one read.
    var = tmp_path / "var"
    var.mkdir(parents=True, exist_ok=True)
    (var / "connectors.yaml").write_bytes(_REGISTRY_SRC.read_bytes())
    monkeypatch.setenv("OMNIAGENTOS_VAR", str(var))
    monkeypatch.setenv("OMNIAGENTOS_VAR_DIR", str(var))
    monkeypatch.delenv("OMNIAGENTOS_RUN_SPEND_CEILING_USD", raising=False)
    load_registry.cache_clear()
    return GrantsStore(SqliteStore(str(tmp_path / "grants.db")))


# --- parse ---------------------------------------------------------------

def test_parse_owner_explicit_amount() -> None:
    auth = parse_spend_authorization(
        "Buy a domain and a server for the demo. I authorize you to spend up to $50.",
        authorized_by=OWNER_PRINCIPAL,
        now_iso=NOW,
    )
    assert auth is not None
    assert auth.cap_usd == 50.0
    assert auth.authorized_by == OWNER_PRINCIPAL
    assert len(auth.prompt_sha256) == 64


def test_parse_us_spelling_authorize_spending() -> None:
    # The demo's own example prompt: American "authorize" + "spending" (-ing).
    # The s-only "authoris?e" form silently failed this → no grant, all parks.
    for text, expected in [
        ("Buy a domain and a server. I authorize spending up to $50.", 50.0),
        ("I authorize you to spend up to $50", 50.0),
        ("I authorized spending up to $75 total", 75.0),
        ("I authorise spend up to $30", 30.0),  # UK spelling still works
    ]:
        auth = parse_spend_authorization(text, authorized_by=OWNER_PRINCIPAL, now_iso=NOW)
        assert auth is not None, text
        assert auth.cap_usd == expected, (text, auth.cap_usd)


def test_parse_non_owner_refused() -> None:
    assert parse_spend_authorization(
        "I authorize spend up to $50", authorized_by="emp_someone", now_iso=NOW
    ) is None


def test_parse_no_amount_is_none() -> None:
    # Intent but no figure → fail closed, no default cap.
    assert parse_spend_authorization(
        "spend up to whatever it takes", authorized_by=OWNER_PRINCIPAL, now_iso=NOW
    ) is None


def test_parse_offhand_price_without_intent_is_none() -> None:
    # A mentioned price with no authorization phrase must not read as consent.
    assert parse_spend_authorization(
        "domains usually cost about $12 these days", authorized_by=OWNER_PRINCIPAL, now_iso=NOW
    ) is None


def test_parse_clamps_to_absolute_max() -> None:
    auth = parse_spend_authorization(
        "I authorize a budget of $999999", authorized_by=OWNER_PRINCIPAL, now_iso=NOW
    )
    assert auth is not None
    assert auth.cap_usd == ABSOLUTE_MAX_CEILING_USD


def test_parse_comma_and_dollar_forms() -> None:
    auth = parse_spend_authorization(
        "you may spend $1,000 on infrastructure", authorized_by=OWNER_PRINCIPAL, now_iso=NOW
    )
    assert auth is not None and auth.cap_usd == 1000.0


# --- mint ----------------------------------------------------------------

def _auth(cap: float = 50.0):
    return parse_spend_authorization(
        f"I authorize spend up to ${cap:.0f}", authorized_by=OWNER_PRINCIPAL, now_iso=NOW
    )


def test_mint_happy_path(store: GrantsStore) -> None:
    res = mint_run_grants(
        store,
        authorization=_auth(50),
        asks=[
            CapabilityAsk("stripe_acmeuni.charge", "consequential", 20.0, 1),
            CapabilityAsk("paypal.payout", "consequential", 20.0, 1),
            CapabilityAsk("meta_acmeuni.launch", "consequential", 5.0, 5),
        ],
        project_id="proj-demo",
        run_id="run-1",
        now_iso=NOW,
    )
    assert res.ok, res.refused_reason
    assert {g.capability for g in res.minted} == {
        "stripe_acmeuni.charge", "paypal.payout", "meta_acmeuni.launch"
    }
    for g in res.minted:
        assert g.grant_id
        assert g.expires_at > NOW
    # audit line written
    assert res.audit_ref and Path(res.audit_ref).exists()


def test_mint_no_authorization_refused(store: GrantsStore) -> None:
    res = mint_run_grants(
        store, authorization=None,
        asks=[CapabilityAsk("stripe_acmeuni.charge", "consequential", 10.0, 1)],
        project_id="p", run_id="r", now_iso=NOW,
    )
    assert not res.ok and res.refused_reason == "no_authorization"
    assert res.minted == []


def test_mint_over_ceiling_is_all_or_nothing(store: GrantsStore) -> None:
    res = mint_run_grants(
        store, authorization=_auth(30),
        asks=[
            CapabilityAsk("stripe_acmeuni.charge", "consequential", 20.0, 1),
            CapabilityAsk("paypal.payout", "consequential", 20.0, 1),
        ],
        project_id="p", run_id="r", now_iso=NOW,
    )
    assert not res.ok and res.refused_reason == "over_ceiling"
    assert res.minted == []
    # nothing minted in the store
    assert store.list_active_grants(project_id="p") == []


def test_mint_rejects_non_consequential_class(store: GrantsStore) -> None:
    res = mint_run_grants(
        store, authorization=_auth(50),
        asks=[CapabilityAsk("clerk.read", "read_only", 0.0, 1)],
        project_id="p", run_id="r", now_iso=NOW,
    )
    assert not res.ok and res.refused_reason == "ungrantable_class"
    assert res.minted == []


def test_mint_broadcast_without_audience_refused(store: GrantsStore) -> None:
    res = mint_run_grants(
        store, authorization=_auth(50),
        asks=[CapabilityAsk("gmail.send", "consequential", 1.0, 1, target_set=None)],
        project_id="p", run_id="r", now_iso=NOW,
    )
    assert not res.ok and res.refused_reason == "broadcast_needs_audience"


def test_mint_broadcast_with_audience_ok(store: GrantsStore) -> None:
    res = mint_run_grants(
        store, authorization=_auth(50),
        asks=[CapabilityAsk("gmail.send", "consequential", 1.0, 1, target_set=["owner@example.com"])],
        project_id="p", run_id="r", now_iso=NOW,
    )
    assert res.ok, res.refused_reason
    assert res.minted[0].capability == "gmail.send"


def test_env_ceiling_clamps_below_parsed_cap(store: GrantsStore, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OMNIAGENTOS_RUN_SPEND_CEILING_USD", "5")
    res = mint_run_grants(
        store, authorization=_auth(50),
        asks=[CapabilityAsk("stripe_acmeuni.charge", "consequential", 10.0, 1)],
        project_id="p", run_id="r", now_iso=NOW, ceiling_usd=None,
    )
    # env ceiling $5 < asked $10 → refused even though the prompt said $50
    assert not res.ok and res.refused_reason == "over_ceiling"


def test_minted_grant_satisfies_broker_validator(store: GrantsStore) -> None:
    """The whole point: a minted grant makes a consequential call pass the same
    validator the broker consumes at call time."""
    res = mint_run_grants(
        store, authorization=_auth(50),
        asks=[CapabilityAsk("stripe_acmeuni.charge", "consequential", 20.0, 1)],
        project_id="proj-demo", run_id="run-x", now_iso=NOW,
    )
    assert res.ok
    grant_id = res.minted[0].grant_id
    from omniagentos.grants.validation import validate_approval_token

    result = validate_approval_token(
        grant_id,
        grant_store=store,
        capability="stripe_acmeuni.charge",
        action_class="consequential",
        connector="stripe_acmeuni",
        tool="charge",
        target=None,
        generation=0,
        scoped_args=None,
        spend_usd=15.0,
    )
    assert result.ok, getattr(result, "reason", "")


# --- adversarial hardening ----------------------------------------------

def test_parse_phone_or_date_is_not_a_cap() -> None:
    """An authorization phrase whose only figure is a phone/date-like token —
    no currency marker — must NOT parse as a spend cap (fail closed)."""
    for goal in (
        "I authorize you to spend on the demo; call 5551234 if blocked",
        "you may spend as needed, ticket 2026-08-14, ref 4001",
        "spend up to a reasonable amount, my number is 555 000 1234",
    ):
        assert (
            parse_spend_authorization(goal, authorized_by=OWNER_PRINCIPAL, now_iso=NOW)
            is None
        ), goal


def test_parse_binds_amount_to_authorization_not_incidental_price() -> None:
    """Two figures in the prompt: the cap is the authorized amount ($30), never
    an incidental price — even when the incidental price is LARGER (the unsafe
    direction). Anchoring to the intent verb is the fail-closed choice."""
    auth = parse_spend_authorization(
        "spend up to $30, the domain is ~$12", authorized_by=OWNER_PRINCIPAL, now_iso=NOW
    )
    assert auth is not None and auth.cap_usd == 30.0

    # A larger incidental figure must not raise the cap above the authorization.
    auth2 = parse_spend_authorization(
        "the server runs $500/mo but I authorize spend up to $30 total",
        authorized_by=OWNER_PRINCIPAL,
        now_iso=NOW,
    )
    assert auth2 is not None and auth2.cap_usd == 30.0


def test_mint_refuses_when_asks_exceed_authorized_amount(store: GrantsStore) -> None:
    """The $30 authorization binds: an ask set totalling more is refused whole,
    nothing minted."""
    auth = parse_spend_authorization(
        "spend up to $30, the domain is ~$12", authorized_by=OWNER_PRINCIPAL, now_iso=NOW
    )
    res = mint_run_grants(
        store, authorization=auth,
        asks=[
            CapabilityAsk("stripe_acmeuni.charge", "consequential", 25.0, 1),
            CapabilityAsk("paypal.payout", "consequential", 10.0, 1),
        ],
        project_id="p", run_id="r", now_iso=NOW,
    )
    assert not res.ok and res.refused_reason == "over_ceiling"
    assert res.minted == []
    assert store.list_active_grants(project_id="p") == []


def test_minted_grant_is_consumed_once(store: GrantsStore) -> None:
    """max_actions=1 binds at call time: the first consume succeeds, a second
    consume beyond the cap fails. The grant is not an unlimited pass."""
    res = mint_run_grants(
        store, authorization=_auth(50),
        asks=[CapabilityAsk("stripe_acmeuni.charge", "consequential", 20.0, 1)],
        project_id="proj-demo", run_id="run-c", now_iso=NOW,
    )
    assert res.ok
    grant_id = res.minted[0].grant_id

    first = store.try_consume(
        grant_id, capability="stripe_acmeuni.charge", action_class="consequential",
        connector="stripe_acmeuni", tool="charge", generation=0, spend_usd=15.0,
    )
    assert first.ok, getattr(first, "reason", "")

    second = store.try_consume(
        grant_id, capability="stripe_acmeuni.charge", action_class="consequential",
        connector="stripe_acmeuni", tool="charge", generation=0, spend_usd=1.0,
    )
    assert not second.ok
    assert second.reason == "max_actions"


def test_minted_grant_metadata_and_expiry(store: GrantsStore) -> None:
    """Every minted grant pins the ask's action_class, marks its provenance as
    run_authorization, and expires in the future."""
    res = mint_run_grants(
        store, authorization=_auth(50),
        asks=[CapabilityAsk("paypal.payout", "consequential", 20.0, 1)],
        project_id="proj-demo", run_id="run-m", now_iso=NOW,
    )
    assert res.ok
    grant = store.get_grant(res.minted[0].grant_id)
    assert grant is not None
    meta = grant["metadata"]
    assert meta["action_class"] == "consequential"
    assert meta["source"] == "run_authorization"
    assert meta["run_id"] == "run-m"
    assert meta["prompt_sha256"] == _auth(50).prompt_sha256
    assert grant["expires_at"] > NOW


def test_audit_is_append_only_and_records_refusals(store: GrantsStore) -> None:
    """Two mints append two+ lines; a refusal also writes an audit line, so the
    "who authorized what, and what we refused" chain is reconstructable."""
    r1 = mint_run_grants(
        store, authorization=_auth(50),
        asks=[CapabilityAsk("stripe_acmeuni.charge", "consequential", 10.0, 1)],
        project_id="p", run_id="run-a1", now_iso=NOW,
    )
    r2 = mint_run_grants(
        store, authorization=_auth(50),
        asks=[CapabilityAsk("paypal.payout", "consequential", 10.0, 1)],
        project_id="p", run_id="run-a2", now_iso=NOW,
    )
    assert r1.ok and r2.ok
    audit = Path(r1.audit_ref)
    assert audit == Path(r2.audit_ref)  # one append-only log
    lines_after_two = audit.read_text(encoding="utf-8").splitlines()
    assert len(lines_after_two) >= 2

    # A refusal appends too — it does not silently vanish.
    r3 = mint_run_grants(
        store, authorization=None,
        asks=[CapabilityAsk("stripe_acmeuni.charge", "consequential", 10.0, 1)],
        project_id="p", run_id="run-a3", now_iso=NOW,
    )
    assert not r3.ok
    lines_after_refusal = audit.read_text(encoding="utf-8").splitlines()
    assert len(lines_after_refusal) == len(lines_after_two) + 1
    last = json.loads(lines_after_refusal[-1])
    assert last["decision"] == "refused" and last["reason"] == "no_authorization"


# --- review blocker regressions (PR #432) --------------------------------

def _gmail_raw_mime(recipients: list[str]) -> str:
    """A minimal base64url-encoded gmail MIME body naming ``recipients`` as To:.

    Mirrors the send-path body the audience-bound check parses at consume time
    (see tests/grants/test_grants.py and connectors/extractors._gmail_send_bounds).
    """
    message = f"To: {', '.join(recipients)}\r\nSubject: hi\r\n\r\nbody"
    return base64.urlsafe_b64encode(message.encode("utf-8")).decode("ascii").rstrip("=")


def test_parse_distant_incidental_price_is_not_a_cap() -> None:
    """F1: an intent phrase with NO figure adjacent, but a large incidental
    price in a later sentence, must fail closed (return None) — the distant
    $800 must never become the cap."""
    auth = parse_spend_authorization(
        "Spend up to the limit for the demo. By the way, the enterprise server costs $800.",
        authorized_by=OWNER_PRINCIPAL,
        now_iso=NOW,
    )
    assert auth is None


def test_parse_adjacent_amount_wins_over_larger_later_price() -> None:
    """F1: the authorized figure adjacent to the verb ($30) is the cap, even
    though a LARGER incidental price ($500) appears later in the prompt."""
    auth = parse_spend_authorization(
        "I authorize spend up to $30; the server is $500/mo",
        authorized_by=OWNER_PRINCIPAL,
        now_iso=NOW,
    )
    assert auth is not None and auth.cap_usd == 30.0


def test_parse_explicit_zero_cap_fails_closed_not_overgrant() -> None:
    """NEW1: an explicit $0 (owner denying spend) is authoritative and final —
    it must NOT be skipped so a later incidental figure ($500) becomes the cap."""
    assert parse_spend_authorization(
        "I authorize spend up to $0, but the server is $500/mo",
        authorized_by=OWNER_PRINCIPAL,
        now_iso=NOW,
    ) is None
    # A $0.00 first figure is likewise a deny, not "keep looking".
    assert parse_spend_authorization(
        "budget of $0.00 — actually the plan is $250",
        authorized_by=OWNER_PRINCIPAL,
        now_iso=NOW,
    ) is None


def test_parse_positive_first_figure_still_wins() -> None:
    """NEW1 guard: a legitimate positive first in-window figure is still used."""
    auth = parse_spend_authorization(
        "I authorize spend up to $40, the server is $500/mo",
        authorized_by=OWNER_PRINCIPAL,
        now_iso=NOW,
    )
    assert auth is not None and auth.cap_usd == 40.0


def test_mint_refuses_when_only_amount_is_a_distant_incidental(store: GrantsStore) -> None:
    """F1 end-to-end: the distant-$800 prompt parses to no authorization, so a
    mint attempt for it refuses and nothing is minted."""
    auth = parse_spend_authorization(
        "Spend up to the limit for the demo. By the way, the enterprise server costs $800.",
        authorized_by=OWNER_PRINCIPAL,
        now_iso=NOW,
    )
    assert auth is None
    res = mint_run_grants(
        store, authorization=auth,
        asks=[CapabilityAsk("stripe_acmeuni.charge", "consequential", 20.0, 1)],
        project_id="p", run_id="r", now_iso=NOW,
    )
    assert not res.ok and res.refused_reason == "no_authorization"
    assert res.minted == []


def test_mint_refuses_zero_spend_cap_ask(store: GrantsStore) -> None:
    """F2 root cause: a $0 spend cap is un-consumable at the store, so minting
    it is refused rather than handing back a dead grant."""
    res = mint_run_grants(
        store, authorization=_auth(50),
        asks=[CapabilityAsk("stripe_acmeuni.charge", "consequential", 0.0, 1)],
        project_id="p", run_id="r", now_iso=NOW,
    )
    assert not res.ok and res.refused_reason == "bad_bounds"
    assert res.minted == []
    assert store.list_active_grants(project_id="p") == []


def test_minted_broadcast_grant_is_consumable(store: GrantsStore) -> None:
    """F2: a broadcast grant minted by mint_run_grants must actually pass the
    broker validator at consume time — audience bound present, and consumable
    against the approved recipient with a real outbound body (NOT refused with
    no_audience_bound / max_spend)."""
    res = mint_run_grants(
        store, authorization=_auth(50),
        asks=[CapabilityAsk("gmail.send", "consequential", 1.0, 1, target_set=["owner@example.com"])],
        project_id="proj-demo", run_id="run-b", now_iso=NOW,
    )
    assert res.ok, res.refused_reason
    grant_id = res.minted[0].grant_id

    # The mint recorded the audience bound the send path re-checks.
    grant = store.get_grant(grant_id)
    assert grant is not None
    assert grant["metadata"]["max_recipients"] == 1
    assert grant["metadata"]["audience_snapshot_hash"]

    from omniagentos.grants.validation import validate_approval_token

    result = validate_approval_token(
        grant_id,
        grant_store=store,
        capability="gmail.send",
        action_class="consequential",
        connector="gmail",
        tool="send",
        target="owner@example.com",
        generation=0,
        scoped_args={"raw": _gmail_raw_mime(["owner@example.com"])},
        spend_usd=0.5,
    )
    assert result.ok, getattr(result, "reason", "")


def test_minted_broadcast_grant_consumed_once(store: GrantsStore) -> None:
    """F2 + bounds: the broadcast grant consumes once (max_actions=1); a second
    consume beyond the cap is refused."""
    res = mint_run_grants(
        store, authorization=_auth(50),
        asks=[CapabilityAsk("gmail.send", "consequential", 2.0, 1, target_set=["owner@example.com"])],
        project_id="proj-demo", run_id="run-b2", now_iso=NOW,
    )
    assert res.ok, res.refused_reason
    grant_id = res.minted[0].grant_id
    body = {"raw": _gmail_raw_mime(["owner@example.com"])}

    first = store.try_consume(
        grant_id, capability="gmail.send", action_class="consequential",
        connector="gmail", tool="send", target="owner@example.com",
        generation=0, scoped_args=body, spend_usd=0.5,
    )
    assert first.ok, getattr(first, "reason", "")

    second = store.try_consume(
        grant_id, capability="gmail.send", action_class="consequential",
        connector="gmail", tool="send", target="owner@example.com",
        generation=0, scoped_args=body, spend_usd=0.5,
    )
    assert not second.ok and second.reason == "max_actions"
