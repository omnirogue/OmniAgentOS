"""PRIORITY 4: /api/revenue report shape + honesty rules (pure builder)."""

from __future__ import annotations

from typing import Any

from omniagentos.revenue.report import build_revenue_report


def _facts() -> list[dict[str, Any]]:
    return [
        {
            "day": "2026-07-10",
            "vertical": "AcmeUni",
            "source": "stripe",
            "campaign_id": None,
            "campaign_name": None,
            "revenue_usd": 500.0,
            "ad_spend_usd": 0.0,
            "roas": None,
            "payment_failures": 3,
            "refunds_usd": 25.0,
            "meta": {"charges": 40},
        },
        {
            "day": "2026-07-10",
            "vertical": "AcmeUni",
            "source": "meta",
            "campaign_id": "cmp_1",
            "campaign_name": "Prospecting",
            "revenue_usd": 0.0,
            "ad_spend_usd": 100.0,
            "roas": 2.5,
            "payment_failures": 0,
            "refunds_usd": 0.0,
            "meta": {"revenue_attributed_usd": 250.0, "attribution": "derived:spend*purchase_roas"},
        },
        {
            "day": "2026-07-10",
            "vertical": "Initech",
            "source": "stripe",
            "campaign_id": None,
            "campaign_name": None,
            "revenue_usd": 200.0,
            "ad_spend_usd": 0.0,
            "roas": None,
            "payment_failures": 0,
            "refunds_usd": 0.0,
            "meta": {"charges": 10},
        },
        {
            "day": "2026-07-10",
            "vertical": "Initech",
            "source": "meta",
            "campaign_id": "cmp_2",
            "campaign_name": "Retargeting",
            "revenue_usd": 0.0,
            "ad_spend_usd": 50.0,
            "roas": 0.0,
            "payment_failures": 0,
            "refunds_usd": 0.0,
            "meta": {"revenue_attributed_usd": 0.0, "attribution": "none"},
        },
    ]


def test_report_has_exact_frozen_shape() -> None:
    report = build_revenue_report("2026-07-10", _facts(), generated_at="2026-07-11T06:00:00Z")

    assert set(report.keys()) == {
        "day",
        "timezone",
        "generated_at",
        "totals",
        "by_vertical",
        "by_source",
        "by_campaign",
        "data_quality",
        "coverage",
        "source_status",
    }
    assert report["day"] == "2026-07-10"
    assert report["timezone"] == "America/New_York"
    assert report["generated_at"] == "2026-07-11T06:00:00Z"

    assert set(report["totals"].keys()) == {
        "revenue_usd",
        "ad_spend_usd",
        "gross_contribution_usd",
        "blended_roas",
        "roi",
        "payment_failures",
        "note",
    }
    assert set(report["by_vertical"][0].keys()) == {
        "vertical",
        "revenue_usd",
        "ad_spend_usd",
        "gross_contribution_usd",
        "roas",
        "blended_roas",
        "roi",
        "payment_failures",
    }
    assert set(report["by_source"][0].keys()) == {
        "vertical",
        "source",
        "revenue_usd",
        "ad_spend_usd",
        "payment_failures",
    }
    assert set(report["by_campaign"][0].keys()) == {
        "vertical",
        "source",
        "campaign_id",
        "campaign_name",
        "ad_spend_usd",
        "roas",
        "revenue_attributed_usd",
    }
    assert set(report["coverage"].keys()) == {
        "revenue_sources_live",
        "spend_sources_live",
        "not_wired",
    }


def test_totals_are_honest_gross_contribution_not_profit() -> None:
    report = build_revenue_report("2026-07-10", _facts(), generated_at="x")
    totals = report["totals"]
    # Real revenue = 500 + 200 (Stripe only); Meta attribution is NOT counted.
    assert totals["revenue_usd"] == 700.0
    assert totals["ad_spend_usd"] == 150.0
    assert totals["gross_contribution_usd"] == 550.0
    # Blended metrics use REAL revenue / spend — distinct from attribution roas.
    assert totals["blended_roas"] == round(700.0 / 150.0, 2)
    assert totals["roi"] == round((700.0 - 150.0) / 150.0, 2)
    assert totals["payment_failures"] == 3
    assert "NOT net profit" in totals["note"]


def test_meta_attribution_never_leaks_into_revenue() -> None:
    report = build_revenue_report("2026-07-10", _facts(), generated_at="x")
    by_source = {(r["vertical"], r["source"]): r for r in report["by_source"]}
    assert by_source[("AcmeUni", "meta")]["revenue_usd"] == 0.0
    acmeuni = next(v for v in report["by_vertical"] if v["vertical"] == "AcmeUni")
    assert acmeuni["revenue_usd"] == 500.0
    # Attribution ROAS = attributed / spend = 250 / 100.
    assert acmeuni["roas"] == 2.5
    # Blended metrics use REAL revenue (500) / spend (100) — distinct from the
    # attribution roas above (which is Meta-attributed value / spend).
    assert acmeuni["ad_spend_usd"] == 100.0
    assert acmeuni["blended_roas"] == round(500.0 / 100.0, 2)
    assert acmeuni["roi"] == round((500.0 - 100.0) / 100.0, 2)
    camp = next(c for c in report["by_campaign"] if c["campaign_name"] == "Prospecting")
    assert camp["revenue_attributed_usd"] == 250.0


def test_zero_meta_spend_yields_warn_and_null_roas() -> None:
    facts = [
        {
            "day": "2026-07-10",
            "vertical": "AcmeUni",
            "source": "meta",
            "campaign_id": "c",
            "campaign_name": "n",
            "revenue_usd": 0.0,
            "ad_spend_usd": 0.0,
            "roas": None,
            "payment_failures": 0,
            "refunds_usd": 0.0,
            "meta": {"revenue_attributed_usd": 0.0},
        }
    ]
    report = build_revenue_report(
        "2026-07-10",
        facts,
        generated_at="x",
        source_status=[
            {
                "status_key": "AcmeUni:meta",
                "vertical": "AcmeUni",
                "source": "meta",
                "last_day": "2026-07-10",
                "last_status": "ok",
                "last_message": "collected",
                "consecutive_failures": 0,
                "updated_at": "2026-07-11T00:00:00Z",
            }
        ],
    )
    acmeuni = next(v for v in report["by_vertical"] if v["vertical"] == "AcmeUni")
    assert acmeuni["roas"] is None  # no fabricated 0 when there is no spend
    assert acmeuni["blended_roas"] is None  # blended metrics: same honesty rule
    assert acmeuni["roi"] is None
    assert report["totals"]["blended_roas"] is None
    assert report["totals"]["roi"] is None
    assert any(
        n["level"] == "warn" and "Meta AcmeUni spend returned 0" in n["message"]
        for n in report["data_quality"]
    )


def test_missing_sources_surface_as_data_quality_warnings() -> None:
    report = build_revenue_report("2026-07-10", [], generated_at="x")
    messages = [n["message"] for n in report["data_quality"]]
    # Every expected (brand, source) pair absent -> a warn each.
    assert any("No AcmeUni stripe facts" in m for m in messages)
    assert any("No Initech meta facts" in m for m in messages)
    assert report["coverage"]["revenue_sources_live"] == []
    assert "COGS" in report["coverage"]["not_wired"]


def test_source_status_replaces_ambiguous_missing_source_notes() -> None:
    report = build_revenue_report(
        "2026-07-10",
        [],
        generated_at="x",
        source_status=[
            {
                "status_key": "AcmeUni:meta",
                "vertical": "AcmeUni",
                "source": "meta",
                "last_day": "2026-07-10",
                "last_status": "failed",
                "last_message": "Meta token expired",
                "consecutive_failures": 2,
                "updated_at": "2026-07-11T00:00:00Z",
            },
            {
                "status_key": "AcmeUni:stripe",
                "vertical": "AcmeUni",
                "source": "stripe",
                "last_day": "2026-07-10",
                "last_status": "unconfigured",
                "last_message": "ACMEUNI_STRIPE_PRIMARY_SECRET_KEY not configured",
                "consecutive_failures": 1,
                "updated_at": "2026-07-11T00:00:00Z",
            },
        ],
    )

    notes = report["data_quality"]
    assert any(
        n["level"] == "info" and "ACMEUNI_STRIPE_PRIMARY_SECRET_KEY" in n["message"] for n in notes
    )
    assert any(
        n["level"] == "warn" and "Meta token expired" in n["message"] and "2" in n["message"]
        for n in notes
    )
    assert not any("No AcmeUni meta facts" in n["message"] for n in notes)
    assert any("No Initech meta facts" in n["message"] for n in notes)
    assert report["source_status"] == [
        {
            "vertical": "AcmeUni",
            "source": "meta",
            "status": "failed",
            "message": "Meta token expired",
            "consecutive_failures": 2,
        },
        {
            "vertical": "AcmeUni",
            "source": "stripe",
            "status": "unconfigured",
            "message": "ACMEUNI_STRIPE_PRIMARY_SECRET_KEY not configured",
            "consecutive_failures": 1,
        },
    ]


def test_omitted_source_status_keeps_ambiguous_missing_source_note() -> None:
    report = build_revenue_report("2026-07-10", [], generated_at="x")

    assert any(
        "No AcmeUni meta facts for 2026-07-10" in note["message"]
        and "not distinguishable from stored facts alone" in note["message"]
        for note in report["data_quality"]
    )
    assert report["source_status"] == []


def test_stripe_failures_are_surfaced() -> None:
    report = build_revenue_report("2026-07-10", _facts(), generated_at="x")
    assert any("Stripe: 3 failed payment(s)" in n["message"] for n in report["data_quality"])
