"""PRIORITY 3: multi-vertical + per-campaign collection, per-source isolation.

Fully offline — the broker is mocked. No test may touch the live network.
"""

from __future__ import annotations

from datetime import date
from typing import Any

import pytest

from omniagentos.goals.collect import _day_bounds
from omniagentos.revenue import collect
from omniagentos.revenue.collect import collect_day


def _stripe_body() -> dict[str, Any]:
    return {
        "ok": True,
        "status": 200,
        "body": {
            "data": [
                {"id": "ch1", "status": "succeeded", "amount": 10000, "amount_refunded": 0},
                {"id": "ch2", "status": "succeeded", "amount": 5000, "amount_refunded": 1000},
                {"id": "ch3", "status": "failed", "amount": 2000, "amount_refunded": 0},
            ],
            "has_more": False,
        },
    }


def _meta_body(account_id: str) -> dict[str, Any]:
    return {
        "ok": True,
        "status": 200,
        "body": {
            "data": [
                {
                    "campaign_id": f"cmp_{account_id}_a",
                    "campaign_name": "Campaign A",
                    "spend": "20.00",
                    "purchase_roas": [{"value": "3.0"}],
                    "action_values": [{"action_type": "omni_purchase", "value": "75.00"}],
                },
                {
                    "campaign_id": f"cmp_{account_id}_b",
                    "campaign_name": "Campaign B",
                    "spend": "10.00",
                    "purchase_roas": [{"value": "2.0"}],
                },
            ]
        },
    }


def _configure(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ACMEUNI_STRIPE_PRIMARY_SECRET_KEY", "sk_test_acmeuni")
    monkeypatch.setenv("INITECH_STRIPE_PRIMARY_SECRET_KEY", "sk_test_omni")
    monkeypatch.setenv("ACMEUNI_META_ACCESS_TOKEN", "tok_acmeuni")
    monkeypatch.setenv("ACMEUNI_META_AD_ACCOUNT_IDS", "111,222")
    monkeypatch.setenv("INITECH_META_ACCESS_TOKEN", "tok_omni")
    monkeypatch.setenv("INITECH_META_AD_ACCOUNT_IDS", "333,444")


def test_collects_all_brands_all_accounts_per_campaign(
    store: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    _configure(monkeypatch)
    calls: list[dict[str, Any]] = []

    def fake_call(cap: str, _granted: list[str], **kwargs: Any) -> dict[str, Any]:
        calls.append({"cap": cap, **kwargs})
        if kwargs["path"] == "/v1/charges":
            return _stripe_body()
        account_id = kwargs["path"].split("/")[1].removeprefix("act_")
        assert kwargs["query"]["level"] == "campaign"
        return _meta_body(account_id)

    monkeypatch.setattr(collect.broker, "call", fake_call)
    result = collect_day(store, target_day=date(2026, 7, 10))

    # Two Stripe brand rows + (2 brands * 2 accounts * 2 campaigns) = 8 Meta rows.
    stripe = [f for f in result.facts if f.source == "stripe"]
    meta = [f for f in result.facts if f.source == "meta"]
    assert {f.vertical for f in stripe} == {"AcmeUni", "Initech"}
    assert len(meta) == 8

    # Stripe net revenue = (10000 + (5000-1000))/100 = 140.0; refunds 10.0; 1 failure.
    acmeuni_stripe = next(f for f in stripe if f.vertical == "AcmeUni")
    assert acmeuni_stripe.revenue_usd == 140.0
    assert acmeuni_stripe.refunds_usd == 10.0
    assert acmeuni_stripe.payment_failures == 1
    assert acmeuni_stripe.campaign_id is None
    assert acmeuni_stripe.meta["failed_charge_ids"] == ["ch3"]
    assert len(acmeuni_stripe.meta["failed_charge_ids"]) == acmeuni_stripe.payment_failures

    # Meta attribution never counts as real revenue.
    assert all(f.revenue_usd == 0.0 for f in meta)
    camp_a = next(f for f in meta if f.campaign_name == "Campaign A")
    assert camp_a.meta["revenue_attributed_usd"] == 75.0  # from action_values
    assert camp_a.meta["attribution"] == "meta_action_value:omni_purchase"
    assert camp_a.roas == 3.0
    camp_b = next(f for f in meta if f.campaign_name == "Campaign B")
    assert camp_b.meta["revenue_attributed_usd"] == 20.0  # derived spend*roas = 10*2
    assert camp_b.meta["attribution"] == "derived:spend*purchase_roas"

    # All four ad accounts were looped (not just the first per brand).
    insight_accounts = {c["path"].split("/")[1] for c in calls if c["path"].endswith("/insights")}
    assert insight_accounts == {"act_111", "act_222", "act_333", "act_444"}

    # Everything was persisted.
    from omniagentos.revenue.store import RevenueStore

    rows = RevenueStore(store).query_day("2026-07-10")
    assert len(rows) == len(result.facts)
    acmeuni_row = next(r for r in rows if r["source"] == "stripe" and r["vertical"] == "AcmeUni")
    assert acmeuni_row["meta"]["failed_charge_ids"] == ["ch3"]
    assert len(acmeuni_row["meta"]["failed_charge_ids"]) == acmeuni_row["payment_failures"]


def test_per_source_isolation_one_failure_does_not_sink_the_run(
    store: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    _configure(monkeypatch)

    def fake_call(cap: str, _granted: list[str], **kwargs: Any) -> dict[str, Any]:
        if cap == "stripe_initech.read":
            raise RuntimeError("broker denied: Initech key revoked")
        if kwargs["path"] == "/v1/charges":
            return _stripe_body()
        account_id = kwargs["path"].split("/")[1].removeprefix("act_")
        return _meta_body(account_id)

    monkeypatch.setattr(collect.broker, "call", fake_call)
    result = collect_day(store, target_day=date(2026, 7, 10))

    # AcmeUni Stripe still collected; Initech Stripe recorded NOTHING (not zeroed).
    stripe_verticals = {f.vertical for f in result.facts if f.source == "stripe"}
    assert stripe_verticals == {"AcmeUni"}
    warn = [n for n in result.notes if n.level == "warn"]
    assert any("Initech Stripe" in n.message for n in warn)
    # Meta for both brands unaffected.
    assert {f.vertical for f in result.facts if f.source == "meta"} == {"AcmeUni", "Initech"}


def test_unconfigured_source_is_info_not_error(store: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ACMEUNI_STRIPE_PRIMARY_SECRET_KEY", "sk_test_acmeuni")
    monkeypatch.delenv("INITECH_STRIPE_PRIMARY_SECRET_KEY", raising=False)
    monkeypatch.delenv("ACMEUNI_META_ACCESS_TOKEN", raising=False)
    monkeypatch.delenv("INITECH_META_ACCESS_TOKEN", raising=False)

    def fake_call(cap: str, _granted: list[str], **kwargs: Any) -> dict[str, Any]:
        return _stripe_body()

    monkeypatch.setattr(collect.broker, "call", fake_call)
    result = collect_day(store, target_day=date(2026, 7, 10))

    assert {f.vertical for f in result.facts if f.source == "stripe"} == {"AcmeUni"}
    assert any(
        n.level == "info" and "INITECH_STRIPE_PRIMARY_SECRET_KEY" in n.message
        for n in result.notes
    )


def test_dry_run_reports_without_writing(store: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    _configure(monkeypatch)

    def fake_call(cap: str, _granted: list[str], **kwargs: Any) -> dict[str, Any]:
        if kwargs["path"] == "/v1/charges":
            return _stripe_body()
        return _meta_body(kwargs["path"].split("/")[1].removeprefix("act_"))

    monkeypatch.setattr(collect.broker, "call", fake_call)
    result = collect_day(store, target_day=date(2026, 7, 10), dry_run=True)
    assert result.facts  # facts computed
    from omniagentos.revenue.store import RevenueStore

    assert RevenueStore(store).query_day("2026-07-10") == []  # but nothing written


def _disable_brand_sources(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        "ACMEUNI_STRIPE_PRIMARY_SECRET_KEY",
        "INITECH_STRIPE_PRIMARY_SECRET_KEY",
        "ACMEUNI_META_ACCESS_TOKEN",
        "ACMEUNI_META_AD_ACCOUNT_IDS",
        "INITECH_META_ACCESS_TOKEN",
        "INITECH_META_AD_ACCOUNT_IDS",
    ):
        monkeypatch.delenv(name, raising=False)


def _configure_global_sources(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("STRIPE_PRIMARY_SECRET_KEY", "sk_test_global_primary")
    monkeypatch.setenv("STRIPE_SECONDARY_SECRET_KEY", "sk_test_global_secondary")
    monkeypatch.setenv("GLACIER_NMI_SECURITY_KEY", "nmi_test_glacier")
    monkeypatch.setenv("FORTPOINT_NMI_SECURITY_KEY", "nmi_test_fortpoint")
    monkeypatch.setenv("FANBASIS_API_KEY", "fanbasis_test_key")


def _nmi_xml(amount_cents: str, kind: str = "sale", success: str = "1") -> str:
    return (
        "<nm_response><total_count>1</total_count><transaction><action>"
        f"<action_type>{kind}</action_type><amount>{amount_cents}</amount>"
        f"<date>20260710120000</date><success>{success}</success>"
        "</action></transaction></nm_response>"
    )


def test_collects_all_global_sources_and_sums_combined_revenue(
    store: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    _disable_brand_sources(monkeypatch)
    _configure_global_sources(monkeypatch)
    calls: list[dict[str, Any]] = []

    def fake_call(cap: str, _granted: list[str], **kwargs: Any) -> dict[str, Any]:
        calls.append({"cap": cap, **kwargs})
        if cap == "stripe_primary.read":
            return {
                "ok": True,
                "status": 200,
                "body": {
                    "data": [
                        {
                            "status": "succeeded",
                            "amount": 10000,
                            "amount_refunded": 0,
                        }
                    ],
                    "has_more": False,
                },
            }
        if cap == "stripe_secondary.read":
            return {
                "ok": True,
                "status": 200,
                "body": {
                    "data": [
                        {"status": "succeeded", "amount": 6000, "amount_refunded": 1000},
                        {"id": "ch_sec_failed", "status": "failed", "amount": 999, "amount_refunded": 0},
                    ],
                    "has_more": False,
                },
            }
        if cap == "nmi_glacier.read":
            return {"ok": True, "status": 200, "body": {"raw": _nmi_xml("2000")}}
        if cap == "nmi_fortpoint.read":
            return {"ok": True, "status": 200, "body": {"raw": _nmi_xml("3000")}}
        assert cap == "fanbasis.read"
        return {
            "ok": True,
            "status": 200,
            "body": {"data": [{"status": "paid", "amount": "40.00"}]},
        }

    monkeypatch.setattr(collect.broker, "call", fake_call)
    result = collect_day(store, target_day=date(2026, 7, 10))

    facts = {fact.source: fact for fact in result.facts}
    assert set(facts) == {"stripe", "glacier", "fortpoint", "fanbasis"}
    assert all(fact.vertical == "global" and fact.campaign_id is None for fact in facts.values())
    assert facts["stripe"].revenue_usd == 150.0
    assert facts["stripe"].refunds_usd == 10.0
    assert facts["stripe"].payment_failures == 1
    assert facts["stripe"].meta["failed_charge_ids"] == ["ch_sec_failed"]
    assert facts["stripe"].meta["account_ids"] == ["primary", "secondary"]
    assert facts["glacier"].revenue_usd == 20.0
    assert facts["fortpoint"].revenue_usd == 30.0
    assert facts["fanbasis"].revenue_usd == 40.0
    assert sum(fact.revenue_usd for fact in facts.values()) == 240.0

    nmi_calls = [call for call in calls if call["cap"].startswith("nmi_")]
    assert all(call["method"] == "POST" and call["path"] == "/api/query.php" for call in nmi_calls)
    assert all("security_key" not in call["form"] for call in nmi_calls)
    assert nmi_calls[0]["form"]["start_date"] == "20260710000000"
    assert nmi_calls[0]["form"]["end_date"] == "20260710235959"
    assert nmi_calls[0]["form"]["page_number"] == 1
    assert nmi_calls[0]["form"]["result_limit"] == collect._NMI_PAGE_SIZE
    assert all(call["method"] == "GET" for call in calls if call["cap"] == "fanbasis.read")
    assert all("transact.php" not in call["path"] for call in calls)


def test_global_stripe_fact_retains_failed_charge_ids(
    store: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The global/per-account Stripe path must propagate failed_charge_ids too.

    STEP 4 propagation: :func:`_global_stripe_fact` combines two account
    summaries. Both the per-account entries under ``meta['accounts']`` and the
    combined top-level ``meta['failed_charge_ids']`` must retain ids, and the
    top-level list length must equal the fact's total ``payment_failures``.
    """
    _disable_brand_sources(monkeypatch)
    _configure_global_sources(monkeypatch)

    def fake_call(cap: str, _granted: list[str], **kwargs: Any) -> dict[str, Any]:
        if cap == "stripe_primary.read":
            return {
                "ok": True,
                "status": 200,
                "body": {
                    "data": [
                        {"id": "chp1", "status": "succeeded", "amount": 10000, "amount_refunded": 0},
                        {"id": "chp2", "status": "failed", "amount": 500, "amount_refunded": 0},
                    ],
                    "has_more": False,
                },
            }
        if cap == "stripe_secondary.read":
            return {
                "ok": True,
                "status": 200,
                "body": {
                    "data": [
                        {"id": "chs1", "status": "succeeded", "amount": 6000, "amount_refunded": 1000},
                        {"id": "chs2", "status": "failed", "amount": 999, "amount_refunded": 0},
                    ],
                    "has_more": False,
                },
            }
        if cap == "nmi_glacier.read":
            return {"ok": True, "status": 200, "body": {"raw": _nmi_xml("2000")}}
        if cap == "nmi_fortpoint.read":
            return {"ok": True, "status": 200, "body": {"raw": _nmi_xml("3000")}}
        assert cap == "fanbasis.read"
        return {
            "ok": True,
            "status": 200,
            "body": {"data": [{"status": "paid", "amount": "40.00"}]},
        }

    monkeypatch.setattr(collect.broker, "call", fake_call)
    result = collect_day(store, target_day=date(2026, 7, 10))

    stripe = next(f for f in result.facts if f.source == "stripe")
    assert stripe.payment_failures == 2
    assert stripe.meta["failed_charge_ids"] == ["chp2", "chs2"]
    assert len(stripe.meta["failed_charge_ids"]) == stripe.payment_failures
    assert stripe.meta["accounts"]["primary"]["failed_charge_ids"] == ["chp2"]
    assert stripe.meta["accounts"]["secondary"]["failed_charge_ids"] == ["chs2"]

    # Read back through the store too.
    from omniagentos.revenue.store import RevenueStore

    row = next(r for r in RevenueStore(store).query_day("2026-07-10") if r["source"] == "stripe")
    assert row["meta"]["failed_charge_ids"] == ["chp2", "chs2"]


def test_nmi_xml_parsing_keeps_settled_sales_only() -> None:
    xml = """
    <nm_response>
      <transaction>
        <action><action_type>sale</action_type><amount>1250</amount><date>20260710100000</date><success>1</success></action>
        <action><action_type>auth</action_type><amount>1250</amount><date>20260710095900</date><success>1</success></action>
      </transaction>
      <transaction><action><action_type>settle</action_type><amount>725</amount><date>20260710110000</date><success>1</success></action></transaction>
      <transaction><action><action_type>capture</action_type><amount>500</amount><date>20260710120000</date><success>0</success></action></transaction>
      <transaction><action><action_type>refund</action_type><amount>300</amount><date>20260710130000</date><success>1</success></action></transaction>
      <transaction><action><action_type>credit</action_type><amount>100</amount><date>20260710140000</date><success>1</success></action></transaction>
      <transaction><action><action_type>void</action_type><amount>50</amount><date>20260710150000</date><success>1</success></action></transaction>
    </nm_response>
    """
    assert collect._nmi_settled_sales(xml) == (15.25, 2)
    page = collect._nmi_page(xml)
    assert float(page.reversal_cents) == 450
    assert page.reversal_count == 3
    malformed = collect._nmi_page("<response>")
    assert malformed.net_cents == 0
    assert malformed.issues == ("malformed NMI XML response",)


def test_nmi_error_response_is_reported_as_api_error() -> None:
    """NMI answers HTTP 200 with an ``error_response`` envelope for auth/API
    errors. That field must be surfaced as the issue, not discarded in favour
    of the generic no-transaction-elements shape complaint."""
    page = collect._nmi_page(
        "<nm_response><error_response>Invalid Security Key</error_response>"
        "<total_count>0</total_count></nm_response>"
    )
    assert page.issues == ("NMI API error: Invalid Security Key",)
    assert page.net_cents == 0
    assert page.settlement_count == 0

    # Genuine-empty day must still be distinguishable from an error: no
    # issues, zero money.
    empty = collect._nmi_page("<nm_response><total_count>0</total_count></nm_response>")
    assert empty.issues == ()
    assert empty.net_cents == 0

    # Truly malformed shape (no error_response, no total_count) keeps the
    # existing generic complaint.
    shapeless = collect._nmi_page("<nm_response></nm_response>")
    assert shapeless.issues == ("invalid NMI response shape (no transaction elements)",)


def test_nmi_error_response_is_surfaced_even_when_transactions_are_present() -> None:
    """An error alongside a PARTIAL transaction list must still fail loud.

    NMI can answer HTTP 200 with an ``error_response`` and a truncated
    transaction list. Checking the error only when the page had zero
    transactions meant this shape accumulated its few transactions and returned
    no issues at all, so a failed call was indistinguishable from a successful
    sync and the day was persisted permanently under-reported. The page is
    refused whole: an error is never trusted in part on a money path.
    """
    page = collect._nmi_page(
        "<nm_response>"
        "<error_response>Rate limit exceeded, results truncated</error_response>"
        "<transaction><action>"
        "<action_type>sale</action_type><amount>10.00</amount>"
        "<date>20260813120000</date><success>1</success>"
        "</action></transaction>"
        "</nm_response>"
    )
    assert page.issues == ("NMI API error: Rate limit exceeded, results truncated",)
    # The partial money is discarded rather than reported as the day's revenue.
    assert page.net_cents == 0
    assert page.settlement_count == 0


def test_nmi_paginates_until_short_page(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(collect, "_NMI_PAGE_SIZE", 2)
    calls: list[dict[str, Any]] = []
    page_one = """
    <nm_response><total_count>3</total_count>
      <transaction><action><action_type>sale</action_type><amount>1000</amount><date>20260710100000</date><success>1</success></action></transaction>
      <transaction><action><action_type>capture</action_type><amount>500</amount><date>20260710110000</date><success>1</success></action></transaction>
    </nm_response>
    """
    page_two = _nmi_xml("250", "refund")

    def fake_call(cap: str, _granted: list[str], **kwargs: Any) -> dict[str, Any]:
        calls.append(kwargs)
        raw = page_one if kwargs["form"]["page_number"] == 1 else page_two
        return {"ok": True, "status": 200, "body": {"raw": raw}}

    monkeypatch.setattr(collect.broker, "call", fake_call)
    start, end, _ = _day_bounds(date(2026, 7, 10))
    fact = collect._collect_nmi("glacier", "nmi_glacier.read", "2026-07-10", start, end)

    assert fact.revenue_usd == 12.5
    assert fact.refunds_usd == 2.5
    assert fact.meta["pages"] == 2
    assert fact.meta["source_status"] == "ok"
    assert [call["form"]["page_number"] for call in calls] == [1, 2]


def test_nmi_shape_mismatch_records_nothing_with_quality_note(
    store: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Unusable NMI shape is unknown — must not invent a $0 revenue fact."""
    _disable_brand_sources(monkeypatch)
    monkeypatch.setenv("GLACIER_NMI_SECURITY_KEY", "nmi_test_glacier")
    monkeypatch.delenv("FORTPOINT_NMI_SECURITY_KEY", raising=False)
    for name in ("STRIPE_PRIMARY_SECRET_KEY", "STRIPE_SECONDARY_SECRET_KEY", "FANBASIS_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(
        collect.broker,
        "call",
        lambda *_args, **_kwargs: {
            "ok": True,
            "status": 200,
            "body": {
                "raw": "<nm_response><transaction><settled_amount>9999</settled_amount></transaction></nm_response>"
            },
        },
    )

    result = collect_day(store, target_day=date(2026, 7, 10))
    assert not any(fact.source == "glacier" for fact in result.facts)
    assert any(
        note.level == "warn"
        and "glacier collection failed" in note.message
        and "recorded nothing" in note.message
        for note in result.notes
    )


def test_fanbasis_probes_get_paths_and_uses_first_available_endpoint(
    store: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    _disable_brand_sources(monkeypatch)
    monkeypatch.setenv("FANBASIS_API_KEY", "fanbasis_test_key")
    calls: list[dict[str, Any]] = []

    def fake_call(cap: str, _granted: list[str], **kwargs: Any) -> dict[str, Any]:
        calls.append(kwargs)
        if kwargs["path"] == "/v1/transactions":
            return {"ok": False, "status": 404, "body": {}}
        return {
            "ok": True,
            "status": 200,
            "body": {"orders": [{"payment_status": "completed", "amount_cents": 1234}]},
        }

    monkeypatch.setattr(collect.broker, "call", fake_call)
    result = collect_day(store, target_day=date(2026, 7, 10))
    fanbasis = next(fact for fact in result.facts if fact.source == "fanbasis")
    assert fanbasis.revenue_usd == 12.34
    assert fanbasis.meta["endpoint"] == "/v1/orders"
    assert [call["path"] for call in calls] == ["/v1/transactions", "/v1/orders"]
    assert all(call["method"] == "GET" for call in calls)


def test_missing_global_keys_and_absent_fanbasis_endpoint_skip_cleanly(
    store: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    _disable_brand_sources(monkeypatch)
    for name in (
        "STRIPE_PRIMARY_SECRET_KEY",
        "STRIPE_SECONDARY_SECRET_KEY",
        "GLACIER_NMI_SECURITY_KEY",
        "FORTPOINT_NMI_SECURITY_KEY",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("FANBASIS_API_KEY", "fanbasis_test_key")

    monkeypatch.setattr(
        collect.broker,
        "call",
        lambda *_args, **_kwargs: {"ok": False, "status": 404, "body": {}},
    )
    result = collect_day(store, target_day=date(2026, 7, 10))

    assert result.facts == []
    # Every *per-source* skip is informational -- an unconfigured source is not an
    # error. The run as a WHOLE is not clean, though: it attempted 9 sources and
    # wrote zero facts, which the data_quality floor breach (collect.py, "zero
    # effective facts written this run") must report at warn. That alarm landed
    # after this test was written and this assertion was never updated, which left
    # main red here; assert the real contract instead of re-muting the alarm.
    assert all(note.level != "error" for note in result.notes)
    assert all(note.level == "info" for note in result.notes if "not configured" in note.message)
    assert any(
        note.level == "warn" and "data_quality floor breach" in note.message
        for note in result.notes
    )
    assert any("STRIPE_PRIMARY_SECRET_KEY not configured" in note.message for note in result.notes)
    assert any("GLACIER_NMI_SECURITY_KEY not configured" in note.message for note in result.notes)
    assert any(
        "no supported GET revenue endpoint found — skipped" in note.message for note in result.notes
    )


def test_global_source_errors_are_isolated_and_do_not_create_zero_facts(
    store: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    _disable_brand_sources(monkeypatch)
    _configure_global_sources(monkeypatch)

    def fake_call(cap: str, _granted: list[str], **kwargs: Any) -> dict[str, Any]:
        if cap == "stripe_primary.read":
            raise TimeoutError("timeout")
        if cap == "stripe_secondary.read":
            return {
                "ok": True,
                "status": 200,
                "body": {
                    "data": [
                        {
                            "status": "succeeded",
                            "amount": 1000,
                            "amount_refunded": 0,
                        }
                    ],
                    "has_more": False,
                },
            }
        if cap == "nmi_glacier.read":
            return {"ok": True, "status": 200, "body": {"raw": "not xml"}}
        if cap == "nmi_fortpoint.read":
            raise RuntimeError("broker denied")
        return {"ok": False, "status": 503, "body": {}}

    monkeypatch.setattr(collect.broker, "call", fake_call)
    result = collect_day(store, target_day=date(2026, 7, 10))

    # Unusable/failed global sources must record NOTHING (not a flattering 0.0 fact).
    assert result.facts == []
    warnings = [note.message for note in result.notes if note.level == "warn"]
    assert any("stripe primary collection failed" in message for message in warnings)
    assert any("glacier collection failed" in message for message in warnings)
    assert any("fortpoint collection failed" in message for message in warnings)
    assert any("fanbasis collection failed" in message for message in warnings)


def test_nmi_invalid_shape_does_not_upsert_zero_over_prior_revenue(
    store: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Unknown NMI total must not be written as $0 — that clobbers a real prior figure.

    Defect class: non-result presented as favourable result (unknown → 0.0 UPSERT).
    Counterfeit that must still fail: still UPSERT a zero-revenue fact but attach
    source_status=invalid_shape / a warn note — the store would still read $0.
    """
    from omniagentos.revenue.store import RevenueFact, RevenueStore

    _disable_brand_sources(monkeypatch)
    monkeypatch.setenv("GLACIER_NMI_SECURITY_KEY", "nmi_test_glacier")
    for name in (
        "FORTPOINT_NMI_SECURITY_KEY",
        "STRIPE_PRIMARY_SECRET_KEY",
        "STRIPE_SECONDARY_SECRET_KEY",
        "FANBASIS_API_KEY",
    ):
        monkeypatch.delenv(name, raising=False)

    rs = RevenueStore(store)
    rs.upsert_revenue_fact(
        RevenueFact(
            day="2026-07-10",
            vertical="global",
            source="glacier",
            revenue_usd=633.0,
            meta={"source_status": "ok", "settlement_count": 4},
        )
    )

    monkeypatch.setattr(
        collect.broker,
        "call",
        lambda *_args, **_kwargs: {
            "ok": True,
            "status": 200,
            "body": {
                "raw": (
                    "<nm_response><transaction>"
                    "<settled_amount>9999</settled_amount>"
                    "</transaction></nm_response>"
                )
            },
        },
    )

    result = collect_day(store, target_day=date(2026, 7, 10))

    assert not any(f.source == "glacier" for f in result.facts)
    assert any(
        n.level == "warn" and "glacier" in n.message.lower() and "recorded nothing" in n.message
        for n in result.notes
    ), result.notes

    rows = rs.query_day("2026-07-10")
    glacier_rows = [r for r in rows if r["source"] == "glacier"]
    assert len(glacier_rows) == 1
    assert glacier_rows[0]["revenue_usd"] == 633.0, (
        "invalid NMI response must leave prior revenue intact; "
        f"got {glacier_rows[0]['revenue_usd']!r} (unknown-as-zero clobber)"
    )


def test_fanbasis_missing_status_is_not_treated_as_success() -> None:
    """Unrecorded payment status must not map to settled revenue.

    Defect class: unrecorded outcome mapped to success.
    Missing or blank status is unknown, not paid — raise to preserve prior figures.
    """
    # Rows with missing status must raise
    rows_missing = [
        {"amount": "50.00"},  # no status / payment_status at all
    ]
    with pytest.raises(collect.SourceError, match="missing or blank FanBasis status"):
        collect._fanbasis_fact("2026-07-10", "/v1/orders", rows_missing)

    # Rows with empty status must raise
    rows_empty = [
        {"status": "", "amount": "25.00"},  # empty status
    ]
    with pytest.raises(collect.SourceError, match="missing or blank FanBasis status"):
        collect._fanbasis_fact("2026-07-10", "/v1/orders", rows_empty)

    # Valid rows still process correctly
    rows_valid = [
        {"payment_status": "pending", "amount": "10.00"},  # skip, not success
        {"status": "paid", "amount": "40.00"},  # count
        {"payment_status": "completed", "amount_cents": 600},  # count
    ]
    fact = collect._fanbasis_fact("2026-07-10", "/v1/orders", rows_valid)
    assert fact.revenue_usd == 46.0  # 40.00 + 6.00 only
    assert fact.meta["settlement_count"] == 2


def test_stripe_absent_data_is_not_genuine_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    """HTTP-OK Stripe body without a `data` field is unreadable, not empty.

    Defect class: missing/unreadable source reported identically to genuine empty.
    Counterfeit that must still fail: default missing `data` to [] and still
    return a zero-charge fact (payload.get("data", [])).
    """
    calls = {"n": 0}

    def fake_call(cap: str, _granted: list[str], **kwargs: Any) -> dict[str, Any]:
        calls["n"] += 1
        # Present and empty is a real empty day; absent is not.
        if calls["n"] == 1:
            return {"ok": True, "status": 200, "body": {"object": "list", "has_more": False}}
        return {"ok": True, "status": 200, "body": {"data": [], "has_more": False}}

    monkeypatch.setattr(collect.broker, "call", fake_call)

    with pytest.raises(collect.SourceError, match="(?i)data|invalid|stripe"):
        collect._stripe_charges("stripe_acmeuni.read", 1, 2)

    charges, truncated = collect._stripe_charges("stripe_acmeuni.read", 1, 2)
    assert charges == []
    assert truncated is False


def test_stripe_absent_data_does_not_upsert_zero_over_prior_revenue(
    store: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Missing Stripe `data` must record nothing — not UPSERT $0 over prior revenue.

    Defect class: non-result presented as favourable result (unknown → 0.0 UPSERT).
    Counterfeit that must still fail: still write revenue_usd=0 with charges=0.
    """
    from omniagentos.revenue.store import RevenueFact, RevenueStore

    _disable_brand_sources(monkeypatch)
    monkeypatch.setenv("STRIPE_PRIMARY_SECRET_KEY", "sk_test_primary")
    monkeypatch.setenv("STRIPE_SECONDARY_SECRET_KEY", "sk_test_secondary")
    for name in (
        "GLACIER_NMI_SECURITY_KEY",
        "FORTPOINT_NMI_SECURITY_KEY",
        "FANBASIS_API_KEY",
    ):
        monkeypatch.delenv(name, raising=False)

    rs = RevenueStore(store)
    rs.upsert_revenue_fact(
        RevenueFact(
            day="2026-07-10",
            vertical="global",
            source="stripe",
            revenue_usd=250.0,
            meta={"charges": 3},
        )
    )

    monkeypatch.setattr(
        collect.broker,
        "call",
        lambda *_args, **_kwargs: {
            "ok": True,
            "status": 200,
            # HTTP OK but no `data` key — unreadable, not a genuine empty list.
            "body": {"object": "list", "has_more": False},
        },
    )

    result = collect_day(store, target_day=date(2026, 7, 10))

    assert not any(f.source == "stripe" for f in result.facts)
    assert any(
        n.level == "warn" and "stripe" in n.message.lower() and "recorded nothing" in n.message
        for n in result.notes
    ), result.notes

    rows = rs.query_day("2026-07-10")
    stripe_rows = [r for r in rows if r["source"] == "stripe"]
    assert len(stripe_rows) == 1
    assert stripe_rows[0]["revenue_usd"] == 250.0, (
        "absent Stripe data must leave prior revenue intact; "
        f"got {stripe_rows[0]['revenue_usd']!r} (unknown-as-zero clobber)"
    )


def test_fanbasis_unparseable_amount_is_not_settled_zero() -> None:
    """Unparseable money on an explicit success row makes the aggregate unknowable.

    Defect class: unparseable must never render as good (unknown → settled $0 or
    partial total that silently omitted the paid row).
    Counterfeit that must still fail: skip unreadable rows and still return a
    normal RevenueFact (zero or partial) that collect_day would UPSERT.
    """
    with pytest.raises(collect.SourceError, match="(?i)unreadable|unknowable|money"):
        collect._fanbasis_fact(
            "2026-07-10",
            "/v1/orders",
            [{"status": "paid", "amount": "not-a-number"}],
        )

    # Mixed rows with any unreadable success money are also unknowable (not partial).
    with pytest.raises(collect.SourceError, match="(?i)unreadable|unknowable|money"):
        collect._fanbasis_fact(
            "2026-07-10",
            "/v1/orders",
            [
                {"status": "paid", "amount": "not-a-number"},
                {"status": "paid", "amount_cents": "oops"},
                {"status": "paid", "amount": "40.00"},
                {"payment_status": "completed", "amount_in_cents": 600},
                {"status": "paid", "amount": 0},
            ],
        )

    # All-readable success money still settles normally, including genuine $0.
    ok = collect._fanbasis_fact(
        "2026-07-10",
        "/v1/orders",
        [
            {"status": "paid", "amount": "40.00"},
            {"payment_status": "completed", "amount_in_cents": 600},
            {"status": "paid", "amount": 0},
        ],
    )
    assert ok.revenue_usd == 46.0
    assert ok.meta["settlement_count"] == 3


def test_fanbasis_absent_amount_is_not_settled_zero() -> None:
    """Missing money fields on a success row make the aggregate unknowable.

    Defect class: unknown/absent must never render as good (absent → settled $0
    or partial total).
    Counterfeit that must still fail: default missing amount keys to 0 / skip
    the row and still return a normal RevenueFact for UPSERT.
    Explicit ``amount: 0`` remains a real zero settlement when present.
    """
    with pytest.raises(collect.SourceError, match="(?i)unreadable|unknowable|money"):
        collect._fanbasis_fact(
            "2026-07-10",
            "/v1/orders",
            [{"status": "paid"}],
        )

    with pytest.raises(collect.SourceError, match="(?i)unreadable|unknowable|money"):
        collect._fanbasis_fact(
            "2026-07-10",
            "/v1/orders",
            [
                {"status": "paid"},  # no amount keys
                {"status": "paid", "amount": "40.00"},
                {"payment_status": "completed", "amount_cents": 600},
                {"status": "paid", "amount": 0},
                {"status": "paid", "net_amount": None},
            ],
        )

    # Readable money only — including genuine zero — is a real measured day.
    ok = collect._fanbasis_fact(
        "2026-07-10",
        "/v1/orders",
        [
            {"status": "paid", "amount": "40.00"},
            {"payment_status": "completed", "amount_cents": 600},
            {"status": "paid", "amount": 0},
        ],
    )
    assert ok.revenue_usd == 46.0
    assert ok.meta["settlement_count"] == 3


def test_fanbasis_unreadable_amount_does_not_upsert_zero_over_prior_revenue(
    store: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Unknown FanBasis money must record nothing — not UPSERT $0 over prior revenue.

    Defect class: non-result presented as favourable result (unknown → 0.0 UPSERT).
    Counterfeit that must still fail: skip unreadable paid rows and still write
    revenue_usd=0 / settlement_count=0 (or a partial total) over a real prior figure.
    """
    from omniagentos.revenue.store import RevenueFact, RevenueStore

    _disable_brand_sources(monkeypatch)
    monkeypatch.setenv("FANBASIS_API_KEY", "fanbasis_test_key")
    for name in (
        "GLACIER_NMI_SECURITY_KEY",
        "FORTPOINT_NMI_SECURITY_KEY",
        "STRIPE_PRIMARY_SECRET_KEY",
        "STRIPE_SECONDARY_SECRET_KEY",
    ):
        monkeypatch.delenv(name, raising=False)

    rs = RevenueStore(store)
    rs.upsert_revenue_fact(
        RevenueFact(
            day="2026-07-10",
            vertical="global",
            source="fanbasis",
            revenue_usd=633.0,
            meta={"endpoint": "/v1/transactions", "settlement_count": 4},
        )
    )

    # Explicit-success row with no money field — unreadable, not a measured $0 day.
    monkeypatch.setattr(
        collect.broker,
        "call",
        lambda *_args, **_kwargs: {
            "ok": True,
            "status": 200,
            "body": {"data": [{"status": "paid"}]},
        },
    )

    result = collect_day(store, target_day=date(2026, 7, 10))

    assert not any(f.source == "fanbasis" for f in result.facts)
    assert any(
        n.level == "warn" and "fanbasis" in n.message.lower() and "recorded nothing" in n.message
        for n in result.notes
    ), result.notes

    rows = rs.query_day("2026-07-10")
    fanbasis_rows = [r for r in rows if r["source"] == "fanbasis"]
    assert len(fanbasis_rows) == 1
    assert fanbasis_rows[0]["revenue_usd"] == 633.0, (
        "unreadable FanBasis money must leave prior revenue intact; "
        f"got {fanbasis_rows[0]['revenue_usd']!r} (unknown-as-zero clobber)"
    )


def test_meta_absent_data_is_not_genuine_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    """HTTP-OK Meta body without a `data` field is unreadable, not empty campaigns.

    Defect class: missing/unreadable source reported identically to genuine empty.
    Counterfeit that must still fail: payload.get("data", []) and return [] so
    collect_day emits the normal "no campaigns returned" info note.
    """
    brand = collect.BRANDS[0]
    calls = {"n": 0}

    def fake_call(cap: str, _granted: list[str], **kwargs: Any) -> dict[str, Any]:
        calls["n"] += 1
        if calls["n"] == 1:
            # HTTP OK but no `data` key — unreadable, not a measured empty list.
            return {"ok": True, "status": 200, "body": {"paging": {}}}
        return {"ok": True, "status": 200, "body": {"data": []}}

    monkeypatch.setattr(collect.broker, "call", fake_call)

    with pytest.raises(collect.SourceError, match="(?i)data|invalid|meta"):
        collect._collect_meta_account(brand, "2026-07-10", "111")

    # Present empty data remains a genuine empty campaign day.
    assert collect._collect_meta_account(brand, "2026-07-10", "111") == []


def test_meta_absent_data_does_not_emit_genuine_empty_note(
    store: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Missing Meta `data` must warn and record nothing — not the empty-campaign note.

    Defect class: missing source reported identically to genuine empty.
    Counterfeit that must still fail: return [] and append "no campaigns returned".
    """
    monkeypatch.setenv("ACMEUNI_META_ACCESS_TOKEN", "tok_acmeuni")
    monkeypatch.setenv("ACMEUNI_META_AD_ACCOUNT_IDS", "111")
    for name in (
        "ACMEUNI_STRIPE_PRIMARY_SECRET_KEY",
        "INITECH_STRIPE_PRIMARY_SECRET_KEY",
        "INITECH_META_ACCESS_TOKEN",
        "INITECH_META_AD_ACCOUNT_IDS",
        "STRIPE_PRIMARY_SECRET_KEY",
        "STRIPE_SECONDARY_SECRET_KEY",
        "GLACIER_NMI_SECURITY_KEY",
        "FORTPOINT_NMI_SECURITY_KEY",
        "FANBASIS_API_KEY",
    ):
        monkeypatch.delenv(name, raising=False)

    monkeypatch.setattr(
        collect.broker,
        "call",
        lambda *_args, **_kwargs: {
            "ok": True,
            "status": 200,
            "body": {"paging": {}},  # no `data`
        },
    )

    result = collect_day(store, target_day=date(2026, 7, 10))

    assert not any(f.source == "meta" for f in result.facts)
    assert any(
        n.level == "warn" and "meta" in n.message.lower() and "recorded nothing" in n.message
        for n in result.notes
    ), result.notes
    assert not any("no campaigns returned" in n.message for n in result.notes), result.notes


def test_stripe_unreadable_succeeded_amount_is_not_measured_zero(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Succeeded Stripe charge with absent/unparseable money is not a measured $0.

    Defect class: unknown/unparseable must never render as good (unknown → $0.0).
    Counterfeit that must still fail: default amount to 0 via .get("amount", 0) /
    _number(...) and still emit revenue_usd=0 with charges=1.
    """
    brand = collect.BRANDS[0]

    # Succeeded charge with no amount key (refund present so amount is the defect).
    monkeypatch.setattr(
        collect,
        "_stripe_charges",
        lambda cap, start, end: (
            [{"id": "ch_missing", "status": "succeeded", "amount_refunded": 0}],
            False,
        ),
    )
    with pytest.raises(collect.SourceError, match="(?i)unreadable|money|amount|unknowable"):
        collect._collect_stripe(brand, "2026-07-10", 1, 2)

    # Succeeded charge with unparseable amount.
    monkeypatch.setattr(
        collect,
        "_stripe_charges",
        lambda cap, start, end: (
            [
                {
                    "id": "ch_bad",
                    "status": "succeeded",
                    "amount": "not-a-number",
                    "amount_refunded": 0,
                }
            ],
            False,
        ),
    )
    with pytest.raises(collect.SourceError, match="(?i)unreadable|money|amount|unknowable"):
        collect._collect_stripe(brand, "2026-07-10", 1, 2)

    # Readable money including genuine $0 still measures normally.
    monkeypatch.setattr(
        collect,
        "_stripe_charges",
        lambda cap, start, end: (
            [
                {"id": "ch_zero", "status": "succeeded", "amount": 0, "amount_refunded": 0},
                {"id": "ch_ok", "status": "succeeded", "amount": 2500, "amount_refunded": 500},
            ],
            False,
        ),
    )
    fact = collect._collect_stripe(brand, "2026-07-10", 1, 2)
    assert fact.revenue_usd == 20.0  # (0 + 2500-500)/100
    assert fact.meta["charges"] == 2


def test_stripe_unreadable_amount_does_not_upsert_zero_over_prior_revenue(
    store: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Unreadable succeeded-charge money must record nothing — not UPSERT $0.

    Defect class: non-result presented as favourable (unknown → 0.0 UPSERT).
    Counterfeit that must still fail: still write revenue_usd=0 with charges=1.
    """
    from omniagentos.revenue.store import RevenueFact, RevenueStore

    monkeypatch.setenv("ACMEUNI_STRIPE_PRIMARY_SECRET_KEY", "sk_test_acmeuni")
    for name in (
        "INITECH_STRIPE_PRIMARY_SECRET_KEY",
        "ACMEUNI_META_ACCESS_TOKEN",
        "INITECH_META_ACCESS_TOKEN",
        "STRIPE_PRIMARY_SECRET_KEY",
        "STRIPE_SECONDARY_SECRET_KEY",
        "GLACIER_NMI_SECURITY_KEY",
        "FORTPOINT_NMI_SECURITY_KEY",
        "FANBASIS_API_KEY",
    ):
        monkeypatch.delenv(name, raising=False)

    rs = RevenueStore(store)
    rs.upsert_revenue_fact(
        RevenueFact(
            day="2026-07-10",
            vertical="AcmeUni",
            source="stripe",
            revenue_usd=99.0,
            meta={"charges": 2},
        )
    )

    monkeypatch.setattr(
        collect.broker,
        "call",
        lambda *_args, **_kwargs: {
            "ok": True,
            "status": 200,
            "body": {
                "data": [
                    {
                        "id": "ch_missing",
                        "status": "succeeded",
                        "amount_refunded": 0,
                    }
                ],
                "has_more": False,
            },
        },
    )

    result = collect_day(store, target_day=date(2026, 7, 10))

    assert not any(f.source == "stripe" and f.vertical == "AcmeUni" for f in result.facts)
    assert any(
        n.level == "warn"
        and "acmeuni" in n.message.lower()
        and "stripe" in n.message.lower()
        and "recorded nothing" in n.message
        for n in result.notes
    ), result.notes

    rows = rs.query_day("2026-07-10")
    acmeuni_stripe = [r for r in rows if r["source"] == "stripe" and r["vertical"] == "AcmeUni"]
    assert len(acmeuni_stripe) == 1
    assert acmeuni_stripe[0]["revenue_usd"] == 99.0, (
        "unreadable Stripe money must leave prior revenue intact; "
        f"got {acmeuni_stripe[0]['revenue_usd']!r} (unknown-as-zero clobber)"
    )


def test_stripe_non_dict_data_entry_is_not_genuine_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Non-object entries in a present Stripe `data` array are unreadable, not empty.

    Defect class: missing/unreadable source reported identically to genuine empty.
    Counterfeit that must still fail: filter non-dict elements and return [] so a
    page of garbage looks like a measured zero-charge day.
    """

    def fake_call(cap: str, _granted: list[str], **kwargs: Any) -> dict[str, Any]:
        return {
            "ok": True,
            "status": 200,
            "body": {"data": ["not-a-charge", 42, None], "has_more": False},
        }

    monkeypatch.setattr(collect.broker, "call", fake_call)

    with pytest.raises(collect.SourceError, match="(?i)invalid|stripe|object|charge|entry"):
        collect._stripe_charges("stripe_acmeuni.read", 1, 2)


def test_fanbasis_nonfinite_amount_is_not_settled() -> None:
    """Non-finite money on a success row makes the aggregate unknowable.

    Defect class: unparseable must never render as good (NaN/Inf as ordinary settlement).
    Counterfeit that must still fail: float('NaN')/float('Infinity') accepted and
    counted as a normal settlement; float('-Infinity') silently skipped as negative.
    """
    for raw in ("NaN", "Infinity", "-Infinity", float("nan"), float("inf"), float("-inf")):
        with pytest.raises(collect.SourceError, match="(?i)unreadable|unknowable|money"):
            collect._fanbasis_fact(
                "2026-07-10",
                "/v1/orders",
                [{"status": "paid", "amount": raw}],
            )

    # Finite readable money still settles normally.
    ok = collect._fanbasis_fact(
        "2026-07-10",
        "/v1/orders",
        [{"status": "paid", "amount": "40.00"}, {"status": "paid", "amount": 0}],
    )
    assert ok.revenue_usd == 40.0
    assert ok.meta["settlement_count"] == 2


def test_stripe_nonfinite_amount_is_not_measured_zero(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Non-finite Stripe amount / amount_refunded is unreadable money, not measured zero.

    Defect class: unparseable must never render as good (NaN/Inf as ordinary money).
    Counterfeit that must still fail: accept float('NaN')/float('Infinity') after
    float() and still emit a normal RevenueFact (including -Infinity as a
    negative-looking total or silent skip). Removing only the math.isfinite guard
    must make this test fail.
    """
    brand = collect.BRANDS[0]
    for key, raw in (
        ("amount", "NaN"),
        ("amount", "Infinity"),
        ("amount", "-Infinity"),
        ("amount", float("nan")),
        ("amount", float("inf")),
        ("amount", float("-inf")),
        ("amount_refunded", "NaN"),
        ("amount_refunded", "Infinity"),
        ("amount_refunded", float("nan")),
        ("amount_refunded", float("inf")),
    ):
        charge: dict[str, Any] = {
            "id": "ch_nf",
            "status": "succeeded",
            "amount": 1000,
            "amount_refunded": 0,
        }
        charge[key] = raw
        monkeypatch.setattr(
            collect,
            "_stripe_charges",
            lambda cap, start, end, c=charge: ([c], False),
        )
        with pytest.raises(collect.SourceError, match="(?i)unreadable|money|unknowable"):
            collect._collect_stripe(brand, "2026-07-10", 1, 2)

    # Finite money still measures normally.
    monkeypatch.setattr(
        collect,
        "_stripe_charges",
        lambda cap, start, end: (
            [{"id": "ch_ok", "status": "succeeded", "amount": 2500, "amount_refunded": 500}],
            False,
        ),
    )
    fact = collect._collect_stripe(brand, "2026-07-10", 1, 2)
    assert fact.revenue_usd == 20.0
    assert fact.refunds_usd == 5.0


def test_stripe_absent_refund_is_not_explicit_zero(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Missing / null amount_refunded is unreadable, not a measured $0 refund.

    Defect class: unknown/absent must never render as good (absent refund → $0
    refunds_usd, inflating net revenue when refunds were simply not reported).
    Counterfeit that must still fail: default missing amount_refunded to 0 and
    treat the day as a normal measured total identical to explicit zero refunds.
    """
    brand = collect.BRANDS[0]

    # Key entirely absent.
    monkeypatch.setattr(
        collect,
        "_stripe_charges",
        lambda cap, start, end: (
            [{"id": "ch1", "status": "succeeded", "amount": 1000}],
            False,
        ),
    )
    with pytest.raises(collect.SourceError, match="(?i)unreadable|money|refund|unknowable"):
        collect._collect_stripe(brand, "2026-07-10", 1, 2)

    # Explicit null is also unreadable.
    monkeypatch.setattr(
        collect,
        "_stripe_charges",
        lambda cap, start, end: (
            [{"id": "ch1", "status": "succeeded", "amount": 1000, "amount_refunded": None}],
            False,
        ),
    )
    with pytest.raises(collect.SourceError, match="(?i)unreadable|money|refund|unknowable"):
        collect._collect_stripe(brand, "2026-07-10", 1, 2)

    # Explicit 0 is a real measured zero refund.
    monkeypatch.setattr(
        collect,
        "_stripe_charges",
        lambda cap, start, end: (
            [{"id": "ch1", "status": "succeeded", "amount": 1000, "amount_refunded": 0}],
            False,
        ),
    )
    fact = collect._collect_stripe(brand, "2026-07-10", 1, 2)
    assert fact.revenue_usd == 10.0
    assert fact.refunds_usd == 0.0


def test_stripe_failed_charge_missing_id_makes_aggregate_unusable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed charge with no readable id must never render as a complete list.

    Defect class: favourable absence. Silently dropping the unusable failed
    charge would return a SHORTER ``failed_charge_ids`` that still claims to be
    the whole day's list — the most-refused shape in this codebase's money code.
    Raise instead so the isolation wrapper records NOTHING for the day.
    """
    brand = collect.BRANDS[0]

    for bad_charge in (
        {"status": "failed", "amount": 500, "amount_refunded": 0},  # no id key
        {"id": None, "status": "failed", "amount": 500, "amount_refunded": 0},
        {"id": "", "status": "failed", "amount": 500, "amount_refunded": 0},
        {"id": 12345, "status": "failed", "amount": 500, "amount_refunded": 0},
    ):
        monkeypatch.setattr(
            collect,
            "_stripe_charges",
            lambda cap, start, end, c=bad_charge: ([c], False),
        )
        with pytest.raises(collect.SourceError, match="(?i)failed.*id|incomplete"):
            collect._collect_stripe(brand, "2026-07-10", 1, 2)

    # A failure with a valid string id still measures normally.
    monkeypatch.setattr(
        collect,
        "_stripe_charges",
        lambda cap, start, end: (
            [{"id": "ch_bad_only", "status": "failed", "amount": 500, "amount_refunded": 0}],
            False,
        ),
    )
    fact = collect._collect_stripe(brand, "2026-07-10", 1, 2)
    assert fact.meta["failed_charge_ids"] == ["ch_bad_only"]
    assert len(fact.meta["failed_charge_ids"]) == fact.payment_failures


def test_fanbasis_non_object_rows_are_not_genuine_empty() -> None:
    """Non-object entries in a present FanBasis list are unreadable, not empty.

    Defect class: missing/unreadable source reported identically to genuine empty.
    Counterfeit that must still fail: filter non-dict elements and return [] so a
    page of garbage looks like a measured zero-settlement day.
    """
    with pytest.raises(collect.SourceError, match="(?i)invalid|fanbasis|object|row"):
        collect._fanbasis_rows(
            {"data": ["not-a-row", 42, None]},
            "/v1/orders",
        )

    # Present empty list remains genuine empty.
    assert collect._fanbasis_rows({"data": []}, "/v1/orders") == []

    # All-object rows still extract.
    rows = collect._fanbasis_rows(
        {"data": [{"status": "paid", "amount": 10}]},
        "/v1/orders",
    )
    assert len(rows) == 1


def test_meta_non_object_rows_are_not_genuine_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Non-object entries in a present Meta `data` array are unreadable, not empty.

    Defect class: missing/unreadable source reported identically to genuine empty.
    Counterfeit that must still fail: skip non-dict rows and return [] so a page
    of garbage looks like a measured empty campaign day ("no campaigns returned").
    """
    brand = collect.BRANDS[0]

    def garbage_call(cap: str, _granted: list[str], **kwargs: Any) -> dict[str, Any]:
        return {
            "ok": True,
            "status": 200,
            "body": {"data": ["not-a-campaign", 42, None]},
        }

    monkeypatch.setattr(collect.broker, "call", garbage_call)

    with pytest.raises(collect.SourceError, match="(?i)invalid|meta|object|campaign|entry"):
        collect._collect_meta_account(brand, "2026-07-10", "111")

    # Present empty data remains genuine empty.
    monkeypatch.setattr(
        collect.broker,
        "call",
        lambda *_a, **_k: {"ok": True, "status": 200, "body": {"data": []}},
    )
    assert collect._collect_meta_account(brand, "2026-07-10", "111") == []


def test_meta_unreadable_spend_is_not_measured_zero(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Missing / unparseable Meta spend is unknown, not measured ad_spend_usd=0.0.

    Defect class: unknown cost recorded as 0.0 (understates spend against a cap).
    Counterfeit that must still fail: row.get("spend", 0.0) + _number(...) and
    still emit a normal fact with ad_spend_usd=0.0.
    """
    brand = collect.BRANDS[0]

    def _body_with(row: dict[str, Any]) -> dict[str, Any]:
        return {"ok": True, "status": 200, "body": {"data": [row]}}

    # Missing spend key.
    monkeypatch.setattr(
        collect.broker,
        "call",
        lambda *_a, **_k: _body_with(
            {"campaign_id": "c1", "campaign_name": "C1", "purchase_roas": [{"value": "2.0"}]}
        ),
    )
    with pytest.raises(collect.SourceError, match="(?i)unreadable|spend|unknowable"):
        collect._collect_meta_account(brand, "2026-07-10", "111")

    # Unparseable spend.
    monkeypatch.setattr(
        collect.broker,
        "call",
        lambda *_a, **_k: _body_with(
            {
                "campaign_id": "c1",
                "campaign_name": "C1",
                "spend": "not-a-number",
            }
        ),
    )
    with pytest.raises(collect.SourceError, match="(?i)unreadable|spend|unknowable"):
        collect._collect_meta_account(brand, "2026-07-10", "111")

    # Non-finite spend.
    for raw in ("NaN", "Infinity", float("nan"), float("inf")):

        def _nf_call(*_a: Any, _r: Any = raw, **_k: Any) -> dict[str, Any]:
            return _body_with({"campaign_id": "c1", "campaign_name": "C1", "spend": _r})

        monkeypatch.setattr(collect.broker, "call", _nf_call)
        with pytest.raises(collect.SourceError, match="(?i)unreadable|spend|unknowable"):
            collect._collect_meta_account(brand, "2026-07-10", "111")

    # Explicit present 0 is a real measured zero spend.
    monkeypatch.setattr(
        collect.broker,
        "call",
        lambda *_a, **_k: _body_with(
            {
                "campaign_id": "c_zero",
                "campaign_name": "Zero",
                "spend": "0.00",
            }
        ),
    )
    facts = collect._collect_meta_account(brand, "2026-07-10", "111")
    assert len(facts) == 1
    assert facts[0].ad_spend_usd == 0.0
    assert facts[0].campaign_id == "c_zero"
