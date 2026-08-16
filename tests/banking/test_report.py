"""build_banking_report — folds stored facts into the /api/banking shape."""

from __future__ import annotations

from omniagentos.banking.report import MONEY_OUT_NOTE, build_banking_report


def _accounts() -> list[dict]:
    return [
        {
            "id": "slash:a1",
            "provider": "slash",
            "brand": "AcmeUni",
            "name": "Operating",
            "mask": "5555",
            "kind": "checking",
        },
        {
            "id": "slash:a2",
            "provider": "slash",
            "brand": "Initech",
            "name": "Card",
            "mask": "9999",
            "kind": "card",
        },
    ]


def _facts() -> list[dict]:
    return [
        {
            "account_id": "slash:a1",
            "provider": "slash",
            "balance_usd": 12000.0,
            "deposits_usd": 100.0,
            "expenses_usd": 250.0,
            "meta": {},
        },
        {
            "account_id": "slash:a2",
            "provider": "slash",
            "balance_usd": 500.0,
            "deposits_usd": 0.0,
            "expenses_usd": 75.0,
            "meta": {},
        },
    ]


def test_totals_and_by_account_sorting() -> None:
    report = build_banking_report(
        "2026-07-10",
        _accounts(),
        _facts(),
        {"money_in_usd": 300.0, "money_out_usd": 900.0, "net_flow_usd": -600.0},
        [],
        generated_at="2026-07-11T06:00:00Z",
    )
    assert report["day"] == "2026-07-10"
    assert report["timezone"] == "America/New_York"
    assert report["totals"]["cash_balance_usd"] == 12500.0
    assert report["totals"]["money_in_usd"] == 100.0
    assert report["totals"]["money_out_usd"] == 325.0
    assert report["totals"]["net_flow_usd"] == -225.0
    assert report["totals"]["note"] == MONEY_OUT_NOTE
    # by_account sorted by balance desc.
    assert [r["name"] for r in report["by_account"]] == ["Operating", "Card"]
    # month-to-date passthrough.
    assert report["month_to_date"]["net_flow_usd"] == -600.0


def test_card_balance_and_teller_notes() -> None:
    report = build_banking_report(
        "2026-07-10",
        _accounts(),
        _facts(),
        {"money_in_usd": 0, "money_out_usd": 0, "net_flow_usd": 0},
        [],
        generated_at="x",
    )
    messages = [n["message"] for n in report["data_quality"]]
    assert any("Teller" in m for m in messages), "Teller gap must always be surfaced"
    assert any("OUTSTANDING balance" in m for m in messages), "card balance caveat expected"


def test_coverage_and_largest_transactions() -> None:
    txns = [
        {
            "account_id": "slash:a1",
            "posted_at": "2026-07-10T15:00:00Z",
            "amount_usd": -250.0,
            "description": "AWS",
        },
        {
            "account_id": "slash:a1",
            "posted_at": "2026-07-10T10:00:00Z",
            "amount_usd": 100.0,
            "description": "Stripe",
        },
    ]
    report = build_banking_report(
        "2026-07-10",
        _accounts(),
        _facts(),
        {"money_in_usd": 0, "money_out_usd": 0, "net_flow_usd": 0},
        txns,
        generated_at="x",
    )
    assert report["coverage"]["banks_live"] == ["slash (AcmeUni)", "slash (Initech)"]
    assert any("Teller" in item for item in report["coverage"]["not_wired"])
    assert report["largest_transactions"][0]["account"] == "Operating ••5555"
    assert report["largest_transactions"][0]["amount_usd"] == -250.0


def test_no_facts_warns() -> None:
    report = build_banking_report(
        "2026-07-10",
        [],
        [],
        {"money_in_usd": 0, "money_out_usd": 0, "net_flow_usd": 0},
        [],
        generated_at="x",
    )
    assert report["totals"]["cash_balance_usd"] == 0.0
    assert any(
        n["level"] == "warn" and "No bank facts" in n["message"] for n in report["data_quality"]
    )


# --- an unread balance must never read as a measured $0.00 (issue #141) ----- #
#
# `_slash_cash_balance` records `(0.0, "unknown")` when the balance sub-resource
# could not be read, and its own docstring says the point of the marker is that
# "the fact is flagged rather than a false 0 shown". The flagging half was never
# wired: the report summed the 0.0 and read no marker, so a 503 on one account's
# balance endpoint was byte-identical to that account genuinely holding nothing.


def _unread(account_id: str) -> dict:
    """A fact the collector recorded with an UNREAD balance — 0.0 + the marker."""
    return {
        "account_id": account_id,
        "provider": "slash",
        "balance_usd": 0.0,
        "deposits_usd": 0.0,
        "expenses_usd": 0.0,
        "meta": {"balance_kind": "unknown"},
    }


def test_an_unread_balance_is_flagged_not_silently_summed_as_zero() -> None:
    report = build_banking_report(
        "2026-07-10",
        _accounts(),
        [_facts()[0], _unread("slash:a2")],
        {"money_in_usd": 0, "money_out_usd": 0, "net_flow_usd": 0},
        [],
        generated_at="x",
    )
    warns = [n["message"] for n in report["data_quality"] if n["level"] == "warn"]
    assert any("could not be read" in m for m in warns), (
        "an unread balance was summed as a measured $0.00 with no data-quality "
        f"note — the operator cannot tell it from an empty account. warns={warns}"
    )
    # The account must be NAMED. "some balance is unknown" does not tell an
    # operator which figure to distrust.
    assert any("Card" in m or "slash:a2" in m for m in warns), f"warns={warns}"


def test_every_unread_account_is_named_not_just_the_first() -> None:
    """Globs the fact rows rather than naming accounts.

    A hand-written list of subjects has the same failure mode as the marker it
    is testing, so this asserts over whatever the day produced.
    """
    facts = [_unread("slash:a1"), _unread("slash:a2")]
    report = build_banking_report(
        "2026-07-10",
        _accounts(),
        facts,
        {"money_in_usd": 0, "money_out_usd": 0, "net_flow_usd": 0},
        [],
        generated_at="x",
    )
    warns = " ".join(n["message"] for n in report["data_quality"] if n["level"] == "warn")
    accounts_by_id = {a["id"]: a for a in _accounts()}
    for fact in facts:
        label = accounts_by_id[fact["account_id"]]["name"]
        assert label in warns, f"{label} has an unread balance but is not named: {warns}"


def test_a_read_balance_of_zero_is_not_flagged() -> None:
    """The control: a genuinely empty account must NOT raise the note.

    Without this the guard could 'pass' by warning on every zero balance, which
    would be decoration rather than a check.
    """
    empty = {
        "account_id": "slash:a2",
        "provider": "slash",
        "balance_usd": 0.0,
        "deposits_usd": 0.0,
        "expenses_usd": 0.0,
        "meta": {"balance_kind": "cash_available"},
    }
    report = build_banking_report(
        "2026-07-10",
        _accounts(),
        [_facts()[0], empty],
        {"money_in_usd": 0, "money_out_usd": 0, "net_flow_usd": 0},
        [],
        generated_at="x",
    )
    warns = [n["message"] for n in report["data_quality"] if n["level"] == "warn"]
    assert not any("could not be read" in m for m in warns), f"warns={warns}"


def test_the_cash_sweep_explanation_is_withheld_when_the_cash_figure_is_unread() -> None:
    """CASH_SWEEP_NOTE explains a real ~$0 cash balance. It must not be offered
    as the explanation for a balance nobody could read — that converts a hole
    into an authoritative one."""
    fact = dict(_unread("slash:a1"), available_credit_usd=12500.0, card_balance_usd=300.0)
    report = build_banking_report(
        "2026-07-10",
        _accounts(),
        [fact],
        {"money_in_usd": 0, "money_out_usd": 0, "net_flow_usd": 0},
        [],
        generated_at="x",
    )
    assert report["totals"]["cash_sweep_note"] is None, (
        "the report volunteered a causal story for a balance it never read"
    )
