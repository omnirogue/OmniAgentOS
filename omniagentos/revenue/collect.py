"""Multi-vertical, per-campaign revenue & ad-spend collection.

Populates the dimensional ``revenue_facts`` store for one *Eastern* calendar day
across every configured brand:

  * Stripe, per brand (AcmeUni, Initech) -> one brand-level revenue fact each.
  * Meta, per brand, per ad account, PER CAMPAIGN -> one fact per campaign.

The load-bearing rule is **per-source isolation**: each (brand, source) — and each
Meta ad account — is collected inside its own try/except. If one is denied,
errors, or is unconfigured, we record NOTHING for it, add a ``data_quality`` note,
and carry on. A single failing source never fails the whole run and never zeroes
another source out; because the store UPSERTs, a source we skip keeps whatever
figure it last had.

Every external call goes through the broker: GET for Stripe, Meta, and FanBasis;
the two NMI sources use their exact query-only form POST endpoint. Day math and
the ET window come from :mod:`omniagentos.goals.collect` so the timezone fix is
single-sourced.
"""

from __future__ import annotations

import argparse
import json
import logging
import math
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any
from xml.etree import ElementTree

from omniagentos.connectors import broker, load_registry
from omniagentos.contracts import default_db_path
from omniagentos.db.store import SqliteStore
from omniagentos.goals.collect import (
    _body,
    _day_bounds,
    _number,
    _skip_reason,
    eastern_yesterday,
)
from omniagentos.revenue.store import RevenueFact, RevenueStore
from omniagentos.steward.alerts.rules import revenue_data_quality
from omniagentos.steward.notify import send_slack

logger = logging.getLogger(__name__)

# Fail-loud contract: a source whose collection streak reaches this many
# consecutive ET calendar days without a genuine successful/empty outcome
# flips the whole run RED (Slack alert + a failing health-sentinel check). Both
# a broker/parse failure AND a missing credential count toward the streak —
# "not configured" must never be a silent, permanent, unalarmed skip.
_CONSECUTIVE_FAIL_THRESHOLD = 2
_ALERT_KEY = "revenue_data_quality"


def _credential(cap_id: str, env_name: str) -> str | None:
    """Resolve one connector-owned value through the broker audit boundary."""
    try:
        value = broker.resolve_one_for(load_registry().capability(cap_id), env_name)
    except broker.BrokerDenied:
        return None
    return value or None

# Safety bound on Stripe pagination: 100 pages * 100/page = 10k charges/day.
_MAX_PAGES = 100

# NMI's Query API accepts these pagination form fields and returns at most this
# many <transaction> records per page. The same safety bound as Stripe keeps a
# malformed gateway response from looping forever.
_NMI_PAGE_SIZE = 1000
_NMI_REVENUE_ACTIONS = frozenset({"sale", "settle", "capture"})
_NMI_REVERSAL_ACTIONS = frozenset({"credit", "refund", "void"})
_NMI_KNOWN_ACTIONS = _NMI_REVENUE_ACTIONS | _NMI_REVERSAL_ACTIONS | {"auth"}

# The two global Stripe credentials select distinct Stripe accounts. They are
# intentionally separate from BRANDS: revenue from these rails is not brand data.
GLOBAL_STRIPE_ACCOUNTS: tuple[tuple[str, str, str], ...] = (
    ("primary", "stripe_primary.read", "STRIPE_PRIMARY_SECRET_KEY"),
    ("secondary", "stripe_secondary.read", "STRIPE_SECONDARY_SECRET_KEY"),
)

# The public FanBasis documentation is access-controlled. Probe only these
# conventional collection endpoints, all through its GET-only broker capability.
_FANBASIS_ENDPOINTS: tuple[str, ...] = (
    "/v1/transactions",
    "/v1/orders",
    "/v1/payouts",
)


@dataclass(frozen=True)
class Brand:
    """One revenue vertical and the read capabilities that back it.

    ``*_env`` names are looked up only to decide whether a source is configured —
    the credentials themselves are never read here; the broker resolves them.
    """

    vertical: str
    stripe_cap: str
    stripe_key_env: str
    meta_cap: str
    meta_token_env: str
    meta_accounts_env: str


# The verticals. Add a brand here (with a reviewed http block in connectors.yaml)
# to bring it into the P&L.
BRANDS: tuple[Brand, ...] = (
    Brand(
        vertical="AcmeUni",
        stripe_cap="stripe_acmeuni.read",
        stripe_key_env="ACMEUNI_STRIPE_PRIMARY_SECRET_KEY",
        meta_cap="meta_acmeuni.read",
        meta_token_env="ACMEUNI_META_ACCESS_TOKEN",
        meta_accounts_env="ACMEUNI_META_AD_ACCOUNT_IDS",
    ),
    Brand(
        vertical="Initech",
        stripe_cap="stripe_initech.read",
        stripe_key_env="INITECH_STRIPE_PRIMARY_SECRET_KEY",
        meta_cap="meta_initech.read",
        meta_token_env="INITECH_META_ACCESS_TOKEN",
        meta_accounts_env="INITECH_META_AD_ACCOUNT_IDS",
    ),
)


@dataclass
class Note:
    """A data-quality note surfaced to the operator. ``level`` is 'warn' or 'info'."""

    level: str
    message: str

    def row(self) -> dict[str, str]:
        return {"level": self.level, "message": self.message}


@dataclass
class CollectionResult:
    day: str
    facts: list[RevenueFact] = field(default_factory=list)
    notes: list[Note] = field(default_factory=list)
    # Fail-loud contract outcome: 'ok' (default) or 'red'. 'red' means either a
    # tracked source hit its consecutive-failure streak or this run wrote zero
    # facts from at least one attempted source (an immediate floor breach, no
    # streak required). Only ever computed for a real (non-dry-run) run --
    # dry-run is a preview and never touches the persisted streak.
    outcome: str = "ok"
    alert: dict[str, Any] | None = None


class SourceError(Exception):
    """A controlled per-source failure. Caught by the isolation wrapper."""


# --------------------------------------------------------------------------- #
# Stripe (brand-level real revenue)                                           #
# --------------------------------------------------------------------------- #
def _stripe_charges(cap: str, start: int, end: int) -> tuple[list[dict[str, Any]], bool]:
    """Fetch a brand's charges for the ET day window, following pagination.

    Bounded at ``_MAX_PAGES``; returns ``(charges, truncated)``. ``truncated`` is
    True only when the page budget was exhausted with more still available, so an
    undercount is flagged rather than silently summed.
    """
    charges: list[dict[str, Any]] = []
    starting_after: str | None = None
    truncated = True
    for _ in range(_MAX_PAGES):
        query: dict[str, Any] = {"created[gte]": start, "created[lte]": end, "limit": 100}
        if starting_after:
            query["starting_after"] = starting_after
        try:
            result = broker.call(cap, [cap], method="GET", path="/v1/charges", query=query)
        except Exception as exc:  # broker denial / transport error
            raise SourceError(f"broker denied or unavailable: {exc}") from exc
        payload = _body(result)
        if payload is None:
            raise SourceError(_skip_reason(result))
        # Absent `data` is unreadable, not a genuine empty list. Defaulting to []
        # would UPSERT a fabricated zero-charge day over prior revenue.
        if "data" not in payload:
            raise SourceError("invalid Stripe response: missing data")
        page_data = payload["data"]
        if not isinstance(page_data, list):
            raise SourceError("invalid Stripe response")
        # Non-object entries are unreadable, not skippable. Filtering them would
        # make a page of garbage look like a genuine empty charge list.
        page: list[dict[str, Any]] = []
        for charge in page_data:
            if not isinstance(charge, dict):
                raise SourceError("invalid Stripe response: non-object charge entry")
            page.append(charge)
        charges.extend(page)
        if not payload.get("has_more") or not page:
            truncated = False
            break
        last_id = page[-1].get("id")
        if not isinstance(last_id, str) or not last_id:
            raise SourceError("Stripe pagination response has no charge id")
        starting_after = last_id
    return charges, truncated


def _stripe_cents(charge: dict[str, Any], key: str) -> float:
    """Parse a Stripe money field in cents, or raise if unreadable.

    Missing, null, non-numeric, and non-finite values are unknown — never zero.
    Absent refund money must not render as an explicit measured $0 refund.
    """
    if key not in charge:
        raise SourceError(
            f"unreadable Stripe money on charge (missing {key}) — aggregate unknowable"
        )
    raw = charge[key]
    if raw is None:
        raise SourceError(f"unreadable Stripe money on charge (null {key}) — aggregate unknowable")
    try:
        value = float(raw)
    except (TypeError, ValueError) as exc:
        raise SourceError(
            f"unreadable Stripe money on charge ({key}) — aggregate unknowable"
        ) from exc
    if not math.isfinite(value):
        raise SourceError(f"unreadable Stripe money on charge ({key}) — aggregate unknowable")
    return value


def _stripe_totals(charges: list[dict[str, Any]]) -> tuple[float, float, int]:
    """Net revenue, refunds, and failure count from charge rows.

    Every charge requires present, finite ``amount_refunded``. Succeeded charges
    also require present, finite ``amount``. Defaulting missing or unparseable
    money to 0.0 would UPSERT a fabricated measured $0 day / $0 refund.
    """
    revenue = 0.0
    refunds = 0.0
    failures = 0
    for charge in charges:
        refunded = _stripe_cents(charge, "amount_refunded")
        refunds += refunded / 100
        if charge.get("status") == "failed":
            failures += 1
        if charge.get("status") == "succeeded":
            amount = _stripe_cents(charge, "amount")
            revenue += (amount - refunded) / 100
    return revenue, refunds, failures


def _stripe_failed_charge_ids(charges: list[dict[str, Any]]) -> list[str]:
    """Ids (in charge order) of every failed charge, or raise if any is unusable.

    A failed charge with a missing or non-string ``id`` makes the whole day's
    failed-id list unusable: silently dropping it would return a SHORTER list
    that still renders as complete — the favourable-absence defect class. Raise
    instead so the isolation wrapper records NOTHING for this day (never a
    partial ``failed_charge_ids`` claimed as whole).
    """
    ids: list[str] = []
    for charge in charges:
        if charge.get("status") != "failed":
            continue
        charge_id = charge.get("id")
        if not isinstance(charge_id, str) or not charge_id:
            raise SourceError(
                "failed Stripe charge has missing or non-string id — "
                "failed_charge_ids would be incomplete"
            )
        ids.append(charge_id)
    return ids


def _collect_stripe(brand: Brand, day: str, start: int, end: int) -> RevenueFact:
    charges, truncated = _stripe_charges(brand.stripe_cap, start, end)
    revenue, refunds, failures = _stripe_totals(charges)
    failed_charge_ids = _stripe_failed_charge_ids(charges)
    meta: dict[str, Any] = {"charges": len(charges), "failed_charge_ids": failed_charge_ids}
    if truncated:
        meta["truncated"] = True
        logger.warning(
            "Stripe pagination truncated at %s pages for %s %s", _MAX_PAGES, brand.vertical, day
        )
    return RevenueFact(
        day=day,
        vertical=brand.vertical,
        source="stripe",
        campaign_id=None,
        revenue_usd=round(revenue, 2),
        refunds_usd=round(refunds, 2),
        payment_failures=failures,
        meta=meta,
    )


def _stripe_summary(cap: str, start: int, end: int) -> dict[str, Any]:
    """Return serialisable daily charge metrics for one Stripe account."""
    charges, truncated = _stripe_charges(cap, start, end)
    revenue, refunds, failures = _stripe_totals(charges)
    failed_charge_ids = _stripe_failed_charge_ids(charges)
    return {
        "charges": len(charges),
        "revenue_usd": round(revenue, 2),
        "refunds_usd": round(refunds, 2),
        "payment_failures": failures,
        "failed_charge_ids": failed_charge_ids,
        "truncated": truncated,
    }


def _collect_global_stripe_account(
    account_id: str, cap: str, start: int, end: int
) -> tuple[str, dict[str, Any]]:
    """Collect one global Stripe account; caller owns per-account isolation."""
    return account_id, _stripe_summary(cap, start, end)


def _global_stripe_fact(
    day: str, account_summaries: list[tuple[str, dict[str, Any]]]
) -> RevenueFact:
    """Combine the two account totals into one global, store-safe Stripe row."""
    account_meta = {account_id: summary for account_id, summary in account_summaries}
    return RevenueFact(
        day=day,
        vertical="global",
        source="stripe",
        campaign_id=None,
        revenue_usd=round(
            sum(_number(summary["revenue_usd"]) for summary in account_meta.values()), 2
        ),
        refunds_usd=round(
            sum(_number(summary["refunds_usd"]) for summary in account_meta.values()), 2
        ),
        payment_failures=sum(int(summary["payment_failures"]) for summary in account_meta.values()),
        meta={
            "account_ids": sorted(account_meta),
            "accounts": account_meta,
            "charges": sum(int(summary["charges"]) for summary in account_meta.values()),
            "failed_charge_ids": [
                charge_id
                for account_id in sorted(account_meta)
                for charge_id in account_meta[account_id]["failed_charge_ids"]
            ],
            "truncated": any(bool(summary["truncated"]) for summary in account_meta.values()),
        },
    )


# --------------------------------------------------------------------------- #
# NMI (Glacier and Fortpoint settlement query)                                #
# --------------------------------------------------------------------------- #
def _nmi_date(value: int) -> str:
    """Format a Unix instant as NMI's ET ``YYYYMMDDhhmmss`` query timestamp."""
    # EASTERN is the project's shared IANA timezone implementation and carries
    # the DST rules required for NMI's local-day query window.
    from omniagentos.goals.collect import EASTERN

    return datetime.fromtimestamp(value, EASTERN).strftime("%Y%m%d%H%M%S")


def _xml_text(element: ElementTree.Element, name: str) -> str:
    """Read a direct XML field while accepting NMI namespace-qualified tags."""
    for child in element:
        if child.tag.rsplit("}", 1)[-1] == name:
            return (child.text or "").strip()
    return ""


@dataclass(frozen=True)
class _NmiPage:
    """One parsed NMI page, expressed in cents until the final conversion."""

    net_cents: Decimal = Decimal(0)
    reversal_cents: Decimal = Decimal(0)
    settlement_count: int = 0
    reversal_count: int = 0
    transaction_count: int = 0
    issues: tuple[str, ...] = ()


def _nmi_page(xml: str) -> _NmiPage:
    """Parse the documented ``nm_response/transaction/action`` Query API shape.

    Invalid or unfamiliar XML is represented as a zero-valued page with issues;
    callers can surface those issues as data quality without crashing or silently
    treating an old/flat response shape as genuine zero revenue.
    """
    try:
        root = ElementTree.fromstring(xml)
    except ElementTree.ParseError:
        return _NmiPage(issues=("malformed NMI XML response",))

    if root.tag.rsplit("}", 1)[-1] != "nm_response":
        return _NmiPage(issues=("invalid NMI response shape (expected nm_response root)",))

    # An ``error_response`` envelope is authoritative and is checked BEFORE the
    # transaction elements, never nested under "the page happened to be empty".
    # NMI can answer HTTP 200 with an error alongside a partial transaction list
    # (a warning, a truncated/paginated result); scoping this check to the
    # zero-transaction case let exactly that shape accumulate its few
    # transactions and return no issues, so a failed call reported as a
    # successful sync and the day was persisted permanently under-reported.
    # A page that carries an error is refused whole rather than trusted in part:
    # the caller raises on issues and records nothing, which is the fail-closed
    # side to be on for money.
    if error := _xml_text(root, "error_response"):
        return _NmiPage(issues=(f"NMI API error: {error}",))

    transactions = [
        element for element in root.iter() if element.tag.rsplit("}", 1)[-1] == "transaction"
    ]
    if not transactions:
        if _xml_text(root, "total_count") == "0":
            return _NmiPage()
        return _NmiPage(issues=("invalid NMI response shape (no transaction elements)",))

    revenue_cents = Decimal(0)
    reversal_cents = Decimal(0)
    settlement_count = 0
    reversal_count = 0
    issues: list[str] = []
    for transaction_number, transaction in enumerate(transactions, start=1):
        actions = [
            element
            for element in transaction.iter()
            if element is not transaction and element.tag.rsplit("}", 1)[-1] == "action"
        ]
        if not actions:
            issues.append(f"transaction {transaction_number} has no action records")
            continue
        for action_number, action in enumerate(actions, start=1):
            kind = _xml_text(action, "action_type").lower()
            amount_text = _xml_text(action, "amount")
            action_date = _xml_text(action, "date")
            success = _xml_text(action, "success")
            label = f"transaction {transaction_number} action {action_number}"
            if (
                not kind
                or not amount_text
                or len(action_date) != 14
                or not action_date.isdigit()
                or success not in {"0", "1"}
            ):
                issues.append(
                    f"{label} is missing action_type, integer-cent amount, "
                    "YYYYMMDDhhmmss date, or valid success"
                )
                continue
            if kind not in _NMI_KNOWN_ACTIONS:
                issues.append(f"{label} has unknown action_type {kind!r}")
                continue
            try:
                amount_cents = Decimal(amount_text)
            except InvalidOperation:
                issues.append(f"{label} has invalid amount")
                continue
            if (
                not amount_cents.is_finite()
                or amount_cents < 0
                or amount_cents != amount_cents.to_integral_value()
            ):
                issues.append(f"{label} has invalid amount")
                continue
            if success != "1":
                continue
            if kind in _NMI_REVENUE_ACTIONS:
                revenue_cents += amount_cents
                settlement_count += 1
            elif kind in _NMI_REVERSAL_ACTIONS:
                reversal_cents += amount_cents
                reversal_count += 1

    return _NmiPage(
        net_cents=revenue_cents - reversal_cents,
        reversal_cents=reversal_cents,
        settlement_count=settlement_count,
        reversal_count=reversal_count,
        transaction_count=len(transactions),
        issues=tuple(issues),
    )


def _nmi_settled_sales(xml: str) -> tuple[float, int]:
    """Compatibility helper returning net settled dollars and positive-action count."""
    page = _nmi_page(xml)
    return round(float(page.net_cents / 100), 2), page.settlement_count


def _collect_nmi(source: str, cap: str, day: str, start: int, end: int) -> RevenueFact:
    """Read completed NMI actions, paging the query-only endpoint to exhaustion."""
    net_cents = Decimal(0)
    reversal_cents = Decimal(0)
    settlement_count = 0
    reversal_count = 0
    transaction_count = 0
    issues: list[str] = []
    pages = 0
    truncated = True
    for page_number in range(1, _MAX_PAGES + 1):
        try:
            response = broker.call(
                cap,
                [cap],
                method="POST",
                path="/api/query.php",
                form={
                    "condition": "complete",
                    "start_date": _nmi_date(start),
                    "end_date": _nmi_date(end),
                    "result_limit": _NMI_PAGE_SIZE,
                    "page_number": page_number,
                },
            )
        except Exception as exc:
            raise SourceError(f"broker denied or unavailable: {exc}") from exc
        payload = _body(response)
        if payload is None:
            raise SourceError(_skip_reason(response))
        raw = payload.get("raw")
        parsed = (
            _nmi_page(raw)
            if isinstance(raw, str)
            else _NmiPage(issues=("invalid NMI response (expected XML)",))
        )
        pages += 1
        net_cents += parsed.net_cents
        reversal_cents += parsed.reversal_cents
        settlement_count += parsed.settlement_count
        reversal_count += parsed.reversal_count
        transaction_count += parsed.transaction_count
        issues.extend(f"page {page_number}: {issue}" for issue in parsed.issues)
        if parsed.issues or parsed.transaction_count < _NMI_PAGE_SIZE:
            truncated = False
            break

    if truncated:
        issues.append(f"pagination exceeded {_MAX_PAGES} pages")

    # A malformed / truncated response makes the aggregate unknowable. Raise so the
    # isolation wrapper records NOTHING and leaves any prior figure intact —
    # never UPSERT revenue_usd=0.0 for "unknown" (that clobbers real money).
    if issues:
        raise SourceError(f"unusable NMI response: {'; '.join(issues)}")

    return RevenueFact(
        day=day,
        vertical="global",
        source=source,
        campaign_id=None,
        revenue_usd=round(float(net_cents / 100), 2),
        refunds_usd=round(float(reversal_cents / 100), 2),
        meta={
            "settlement_count": settlement_count,
            "reversal_count": reversal_count,
            "transaction_count": transaction_count,
            "condition": "complete",
            "query_start": _nmi_date(start),
            "query_end": _nmi_date(end),
            "page_size": _NMI_PAGE_SIZE,
            "pages": pages,
            "source_status": "ok",
            "data_quality": [],
        },
    )


# --------------------------------------------------------------------------- #
# FanBasis (best-effort GET-only endpoint probe)                              #
# --------------------------------------------------------------------------- #
class FanbasisEndpointUnavailable(SourceError):
    """No documented GET revenue endpoint responded for the current account."""


# FanBasis has moved/renamed collection routes before without documentation
# catching up (observed: a stale path 301/302/307/308-redirects rather than
# 404ing). Treated exactly like 404/405 -- "this path is not a live GET route
# right now" -- so a redirecting endpoint no longer sinks the whole probe with
# a raw "broker HTTP failure (301)"; the probe just tries the next candidate.
_FANBASIS_UNAVAILABLE_STATUSES = frozenset({301, 302, 307, 308, 404, 405})


def _fanbasis_rows(payload: dict[str, Any], endpoint: str) -> list[dict[str, Any]]:
    """Extract common list shapes from a revenue endpoint response.

    Non-object entries in a present list are unreadable, not skippable. Filtering
    them would make a page of garbage look like a genuine empty settlement list.
    """
    collection = endpoint.rsplit("/", 1)[-1]
    candidates = (payload.get("data"), payload.get(collection), payload.get("results"))
    for value in candidates:
        if isinstance(value, list):
            rows: list[dict[str, Any]] = []
            for row in value:
                if not isinstance(row, dict):
                    raise SourceError("invalid FanBasis response: non-object row")
                rows.append(row)
            return rows
    raise SourceError("invalid FanBasis response")


def _fanbasis_amount_usd(row: dict[str, Any]) -> float | None:
    """Parse settled dollars from a FanBasis row, or None if money is unreadable.

    Absent money keys and parse failures both return None. Defaulting missing
    fields to 0.0 would count as a measured $0 settlement; callers must treat
    None as unknown and make the day aggregate unusable (raise), never skip the
    paid row and UPSERT a counterfeit zero or partial total. An explicit present
    0 remains a real zero settlement.
    """
    if "amount_cents" in row:
        raw, cents = row["amount_cents"], True
    elif "amount_in_cents" in row:
        raw, cents = row["amount_in_cents"], True
    elif "net_amount" in row:
        raw, cents = row["net_amount"], False
    elif "settled_amount" in row:
        raw, cents = row["settled_amount"], False
    elif "amount" in row:
        raw, cents = row["amount"], False
    elif "total" in row:
        raw, cents = row["total"], False
    else:
        return None
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    # float() accepts "NaN"/"Infinity"; those are unreadable money, not settlements.
    if not math.isfinite(value):
        return None
    return value / 100 if cents else value


def _fanbasis_fact(day: str, endpoint: str, rows: list[dict[str, Any]]) -> RevenueFact:
    """Turn settled/successful FanBasis rows into one global revenue fact.

    Status must be an explicit success value. Missing or empty status is
    *unknown*, not paid — counting it as settled would map an unrecorded
    outcome to success. Unreadable money on a success row makes the day
    aggregate unknowable: raise so the isolation wrapper records NOTHING
    and leaves any prior figure intact — never UPSERT revenue_usd=0.0 or a
    partial total that silently omitted paid rows.
    """
    revenue = 0.0
    count = 0
    _SUCCESS = frozenset({"paid", "succeeded", "complete", "completed", "settled"})
    for row in rows:
        status_raw = row.get("status", row.get("payment_status"))
        # Missing or empty status is unknown, not paid — raising preserves prior figures.
        if status_raw is None or (isinstance(status_raw, str) and not status_raw.strip()):
            raise SourceError("missing or blank FanBasis status on row — aggregate unknowable")
        status = str(status_raw).lower()
        if status not in _SUCCESS:
            continue
        amount = _fanbasis_amount_usd(row)
        if amount is None:
            # Paid/settled but money unreadable → whole day total is unknowable.
            # Skipping would invent $0 (sole unreadable row) or a partial sum.
            raise SourceError("unreadable FanBasis money on settled row — aggregate unknowable")
        if amount < 0:
            continue
        revenue += amount
        count += 1
    return RevenueFact(
        day=day,
        vertical="global",
        source="fanbasis",
        campaign_id=None,
        revenue_usd=round(revenue, 2),
        meta={"endpoint": endpoint, "settlement_count": count},
    )


def _collect_fanbasis(day: str, start: int, end: int) -> RevenueFact:
    """Probe only reviewed FanBasis GET collection paths, never a write route."""
    for endpoint in _FANBASIS_ENDPOINTS:
        try:
            response = broker.call(
                "fanbasis.read",
                ["fanbasis.read"],
                method="GET",
                path=endpoint,
                query={"start": start, "end": end},
            )
        except Exception as exc:
            raise SourceError(f"broker denied or unavailable: {exc}") from exc
        if response.get("status") in _FANBASIS_UNAVAILABLE_STATUSES:
            continue
        payload = _body(response)
        if payload is None:
            raise SourceError(_skip_reason(response))
        return _fanbasis_fact(day, endpoint, _fanbasis_rows(payload, endpoint))
    raise FanbasisEndpointUnavailable("no supported GET revenue endpoint found")


# --------------------------------------------------------------------------- #
# Meta (per-campaign spend + platform-attributed conversion value)            #
# --------------------------------------------------------------------------- #
def _account_ids(raw: str) -> list[str]:
    """Split the comma env into bare account ids (single ``act_`` prefix later)."""
    ids: list[str] = []
    for item in raw.split(","):
        item = item.strip()
        if not item:
            continue
        if item.startswith("act_"):
            item = item[len("act_") :]
        ids.append(item)
    return ids


def _meta_spend_usd(row: dict[str, Any]) -> float:
    """Parse campaign spend dollars, or raise if unreadable.

    Missing, null, non-numeric, and non-finite spend are unknown — never a
    measured $0.0 ad_spend that would understate cost against a spend cap.
    Explicit present 0 remains a real measured zero.
    """
    if "spend" not in row:
        raise SourceError("unreadable Meta spend (missing) — aggregate unknowable")
    raw = row["spend"]
    if raw is None:
        raise SourceError("unreadable Meta spend (null) — aggregate unknowable")
    try:
        value = float(raw)
    except (TypeError, ValueError) as exc:
        raise SourceError("unreadable Meta spend — aggregate unknowable") from exc
    if not math.isfinite(value):
        raise SourceError("unreadable Meta spend (non-finite) — aggregate unknowable")
    return value


def _parse_roas(row: dict[str, Any]) -> float | None:
    purchase_roas = row.get("purchase_roas", [])
    if isinstance(purchase_roas, list) and purchase_roas and isinstance(purchase_roas[0], dict):
        return _number(purchase_roas[0].get("value", 0.0))
    return None


def _attributed_revenue(row: dict[str, Any], spend: float, roas: float | None) -> tuple[float, str]:
    """Meta-attributed purchase VALUE for a campaign, and how it was derived.

    Prefers Meta's reported conversion *value* (``action_values``, real dollars);
    falls back to ``spend * purchase_roas`` (the definition of ROAS) when no
    purchase action value is reported. Either way this is platform ATTRIBUTION, an
    estimate — it is returned so callers can label it honestly and is never summed
    into real (Stripe) revenue.
    """
    action_values = row.get("action_values")
    if isinstance(action_values, list):
        for preferred in ("omni_purchase", "purchase"):
            for av in action_values:
                if isinstance(av, dict) and av.get("action_type") == preferred:
                    return _number(av.get("value")), f"meta_action_value:{preferred}"
        for av in action_values:
            if isinstance(av, dict) and "purchase" in str(av.get("action_type", "")):
                return _number(av.get("value")), "meta_action_value:purchase*"
    if roas is not None and spend:
        return round(spend * roas, 2), "derived:spend*purchase_roas"
    return 0.0, "none"


def _meta_account_probe(brand: Brand, account_id: str) -> bool:
    """Distinguish an invalid/expired Meta token from a genuinely empty campaign day.

    An empty ``/insights`` response is ambiguous on its own: "no active
    campaigns spent money today" and "the token/account can no longer be read
    at all" render identically. This is a cheap secondary GET on the account's
    own status field; a token that cannot even read that is not usable, so the
    empty insights result it produced is unusable too, not a genuine zero.

    Deliberately called only by the orchestrator (:func:`collect_day`), never
    by :func:`_collect_meta_account` itself -- that function's return value is
    a frozen, directly-tested contract ("present empty data is genuine empty"),
    and this probe is an extra network call the orchestrator is free to make
    but a pure per-account collector must not.
    """
    try:
        result = broker.call(
            brand.meta_cap,
            [brand.meta_cap],
            method="GET",
            path=f"/act_{account_id}",
            query={"fields": "account_status"},
        )
    except Exception:
        return False
    payload = _body(result)
    return isinstance(payload, dict) and "account_status" in payload


def _collect_meta_account(brand: Brand, day: str, account_id: str) -> list[RevenueFact]:
    try:
        result = broker.call(
            brand.meta_cap,
            [brand.meta_cap],
            method="GET",
            path=f"/act_{account_id}/insights",
            query={
                "level": "campaign",
                # Plain YYYY-MM-DD -> Meta uses the AD ACCOUNT's own tz (recorded below).
                "time_range": json.dumps({"since": day, "until": day}),
                "fields": "campaign_id,campaign_name,spend,purchase_roas,actions,action_values",
            },
        )
    except Exception as exc:
        raise SourceError(f"broker denied or unavailable: {exc}") from exc
    payload = _body(result)
    if payload is None:
        raise SourceError(_skip_reason(result))
    # Absent `data` is unreadable, not a genuine empty campaign list. Defaulting
    # to [] would emit the normal "no campaigns returned" note for a bad shape.
    if "data" not in payload:
        raise SourceError("invalid Meta response: missing data")
    data = payload["data"]
    if not isinstance(data, list):
        raise SourceError("invalid Meta response")

    facts: list[RevenueFact] = []
    for row in data:
        # Non-object entries are unreadable, not skippable. Skipping them would
        # make a page of garbage look like a genuine empty campaign list.
        if not isinstance(row, dict):
            raise SourceError("invalid Meta response: non-object campaign entry")
        spend = _meta_spend_usd(row)
        roas = _parse_roas(row)
        attributed, method = _attributed_revenue(row, spend, roas)
        campaign_id = row.get("campaign_id") or None
        campaign_name = row.get("campaign_name")
        facts.append(
            RevenueFact(
                day=day,
                vertical=brand.vertical,
                source="meta",
                campaign_id=campaign_id,
                campaign_name=campaign_name,
                revenue_usd=0.0,  # attribution is NOT real revenue — see meta below
                ad_spend_usd=round(spend, 2),
                roas=roas,
                meta={
                    "ad_account_id": account_id,
                    "time_range_tz": "ad_account",
                    "revenue_attributed_usd": round(attributed, 2),
                    "attribution": method,
                },
            )
        )
    return facts


# --------------------------------------------------------------------------- #
# Orchestration                                                               #
# --------------------------------------------------------------------------- #
def collect_day(
    store: SqliteStore,
    *,
    target_day: date | None = None,
    dry_run: bool = False,
) -> CollectionResult:
    """Collect every brand/source for one ET day and UPSERT the facts.

    Per-source isolation: a failure in one brand/source/account is recorded as a
    data_quality note and skipped; the run always completes.
    """
    day = target_day or eastern_yesterday()
    day_str = day.isoformat()
    start, end, _ = _day_bounds(day)
    result = CollectionResult(day=day_str)

    # Fail-loud bookkeeping: one (vertical, source_key, status, message) entry
    # per source this run actually attempted to reason about. 'unconfigured'
    # (missing credential) counts exactly like 'failed' -- see the
    # _CONSECUTIVE_FAIL_THRESHOLD docstring above for why absence must stay
    # loud rather than reading as a benign, permanent skip.
    statuses: list[tuple[str, str, str, str]] = []

    def _mark(vertical: str, source_key: str, status: str, message: str) -> None:
        statuses.append((vertical, source_key, status, message))

    # --- Global Stripe (both accounts, one combined fact) ---
    # The store key intentionally has no account dimension for global sources.
    # Collect each account in isolation, but only UPSERT a combined total when all
    # configured accounts succeeded; otherwise the prior complete total remains.
    global_stripe: list[tuple[str, dict[str, Any]]] = []
    global_stripe_complete = True
    for account_id, cap, key_env in GLOBAL_STRIPE_ACCOUNTS:
        if not _credential(cap, key_env):
            global_stripe_complete = False
            result.notes.append(
                Note("info", f"stripe {account_id}: {key_env} not configured — skipped")
            )
            _mark("global", f"stripe_{account_id}", "unconfigured", f"{key_env} not configured")
            continue
        try:
            global_stripe.append(_collect_global_stripe_account(account_id, cap, start, end))
        except Exception as exc:
            global_stripe_complete = False
            result.notes.append(
                Note(
                    "warn",
                    f"stripe {account_id} collection failed: {exc} — recorded nothing, "
                    "prior figure left intact",
                )
            )
            _mark("global", f"stripe_{account_id}", "failed", str(exc))
        else:
            _mark("global", f"stripe_{account_id}", "ok", "collected")
    if global_stripe_complete:
        result.facts.append(_global_stripe_fact(day_str, global_stripe))
    elif global_stripe:
        result.notes.append(
            Note(
                "warn",
                "stripe global total incomplete — skipped UPSERT so prior figure remains intact",
            )
        )

    # --- Global NMI settlement sources ---
    for source, cap, key_env in (
        ("glacier", "nmi_glacier.read", "GLACIER_NMI_SECURITY_KEY"),
        ("fortpoint", "nmi_fortpoint.read", "FORTPOINT_NMI_SECURITY_KEY"),
    ):
        if not _credential(cap, key_env):
            result.notes.append(Note("info", f"{source}: {key_env} not configured — skipped"))
            _mark("global", source, "unconfigured", f"{key_env} not configured")
            continue
        try:
            result.facts.append(_collect_nmi(source, cap, day_str, start, end))
        except Exception as exc:
            result.notes.append(
                Note(
                    "warn",
                    f"{source} collection failed: {exc} — recorded nothing, prior figure left intact",
                )
            )
            _mark("global", source, "failed", str(exc))
        else:
            _mark("global", source, "ok", "collected")

    # --- FanBasis (GET-only probe; docs currently do not publish a stable route) ---
    if not _credential("fanbasis.read", "FANBASIS_API_KEY"):
        result.notes.append(Note("info", "fanbasis: FANBASIS_API_KEY not configured — skipped"))
        _mark("global", "fanbasis", "unconfigured", "FANBASIS_API_KEY not configured")
    else:
        try:
            result.facts.append(_collect_fanbasis(day_str, start, end))
        except FanbasisEndpointUnavailable as exc:
            result.notes.append(Note("info", f"fanbasis: {exc} — skipped"))
            _mark("global", "fanbasis", "failed", str(exc))
        except Exception as exc:
            result.notes.append(
                Note(
                    "warn",
                    f"fanbasis collection failed: {exc} — recorded nothing, prior figure left intact",
                )
            )
            _mark("global", "fanbasis", "failed", str(exc))
        else:
            _mark("global", "fanbasis", "ok", "collected")

    for brand in BRANDS:
        # --- Stripe (brand-level) ---
        if not _credential(brand.stripe_cap, brand.stripe_key_env):
            result.notes.append(
                Note(
                    "info",
                    f"{brand.vertical} Stripe: {brand.stripe_key_env} not configured — skipped",
                )
            )
            _mark(
                brand.vertical,
                "stripe",
                "unconfigured",
                f"{brand.stripe_key_env} not configured",
            )
        else:
            try:
                result.facts.append(_collect_stripe(brand, day_str, start, end))
            except Exception as exc:
                result.notes.append(
                    Note(
                        "warn",
                        f"{brand.vertical} Stripe collection failed: {exc} — recorded nothing, "
                        "prior figure left intact",
                    )
                )
                _mark(brand.vertical, "stripe", "failed", str(exc))
            else:
                _mark(brand.vertical, "stripe", "ok", "collected")

        # --- Meta (per ad account, per campaign) ---
        token = _credential(brand.meta_cap, brand.meta_token_env)
        accounts = _account_ids(_credential(brand.meta_cap, brand.meta_accounts_env) or "")
        if not token:
            result.notes.append(
                Note(
                    "info",
                    f"{brand.vertical} Meta: {brand.meta_token_env} not configured — skipped",
                )
            )
            _mark(
                brand.vertical, "meta", "unconfigured", f"{brand.meta_token_env} not configured"
            )
        elif not accounts:
            result.notes.append(
                Note(
                    "info",
                    f"{brand.vertical} Meta: {brand.meta_accounts_env} not configured — skipped",
                )
            )
            _mark(
                brand.vertical,
                "meta",
                "unconfigured",
                f"{brand.meta_accounts_env} not configured",
            )
        else:
            for account_id in accounts:
                source_key = f"meta:{account_id}"
                try:
                    account_facts = _collect_meta_account(brand, day_str, account_id)
                except Exception as exc:
                    result.notes.append(
                        Note(
                            "warn",
                            f"{brand.vertical} Meta act_{account_id} collection failed: {exc} "
                            "— recorded nothing",
                        )
                    )
                    _mark(brand.vertical, source_key, "failed", str(exc))
                    continue
                if not account_facts:
                    # Empty insights is ambiguous (see _meta_account_probe): only
                    # count it as a genuine "no active campaigns" day when the
                    # account itself is still readable. A probe failure means the
                    # token/account is unusable, not that spend was genuinely zero.
                    if _meta_account_probe(brand, account_id):
                        result.notes.append(
                            Note(
                                "info",
                                f"{brand.vertical} Meta act_{account_id}: no campaigns "
                                f"returned for {day_str}",
                            )
                        )
                        _mark(brand.vertical, source_key, "ok", "no active campaigns")
                    else:
                        result.notes.append(
                            Note(
                                "warn",
                                f"{brand.vertical} Meta act_{account_id}: no campaigns "
                                f"returned for {day_str} AND the account status probe "
                                "failed — treating as a failure, not a genuine empty day "
                                "(token/account likely invalid)",
                            )
                        )
                        _mark(
                            brand.vertical,
                            source_key,
                            "failed",
                            "empty insights + failed account probe (invalid token/account)",
                        )
                else:
                    _mark(brand.vertical, source_key, "ok", f"{len(account_facts)} campaign(s)")
                result.facts.extend(account_facts)

    if dry_run:
        return result

    revenue_store = RevenueStore(store)
    for fact in result.facts:
        revenue_store.upsert_revenue_fact(fact)

    # --- Fail-loud contract: persist the streak, then decide RED/ok ---
    for vertical, source_key, status, message in statuses:
        revenue_store.record_source_outcome(
            day=day_str, vertical=vertical, source=source_key, status=status, message=message
        )

    # Two independent RED triggers, matching the acceptance contract:
    # (1) a source's consecutive-failure streak reached the threshold -- pure
    #     rule logic lives in steward.alerts.rules so it is tested and shaped
    #     the same way every other data-quality alert in this system is;
    # (2) an immediate floor breach -- THIS run wrote zero effective facts
    #     despite attempting at least one source, which must never wait for a
    #     multi-day streak to become visible.
    streak_candidates = revenue_data_quality(
        revenue_store.source_status_snapshot(), threshold=_CONSECUTIVE_FAIL_THRESHOLD, day=day_str
    )
    streak_red = set(streak_candidates[0].evidence["red_sources"]) if streak_candidates else set()

    floor_breach = bool(statuses) and not result.facts
    floor_red: set[str] = set()
    if floor_breach:
        result.notes.append(
            Note(
                "warn",
                "zero effective facts written this run despite attempting "
                f"{len(statuses)} source(s) — data_quality floor breach",
            )
        )
        floor_red = {
            f"{vertical}:{source_key}"
            for vertical, source_key, status, _ in statuses
            if status != "ok"
        }

    red_sources = sorted(streak_red | floor_red)

    if red_sources:
        result.outcome = "red"
        if revenue_store.claim_daily_alert(alert_key=_ALERT_KEY, day=day_str):
            text = (
                "[CRITICAL] Revenue data quality: RED\n"
                f"Day {day_str} — {len(red_sources)} source(s) failing "
                f"{_CONSECUTIVE_FAIL_THRESHOLD}+ consecutive days or a zero-fact floor "
                f"breach: {', '.join(red_sources)}"
            )
            notify_result = send_slack(text)
            result.alert = {
                "sent": notify_result.ok,
                "channel": notify_result.channel,
                "detail": notify_result.detail,
                "red_sources": red_sources,
            }
            result.notes.append(
                Note(
                    "warn" if notify_result.ok else "info",
                    f"revenue data_quality RED alert {'sent' if notify_result.ok else 'NOT sent'} "
                    f"({notify_result.channel}: {notify_result.detail}) — {', '.join(red_sources)}",
                )
            )
        else:
            result.alert = {"sent": False, "detail": "already alerted today", "red_sources": red_sources}

    return result


def _parse_day(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("date must be YYYY-MM-DD") from exc


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Collect dimensional revenue facts (P&L)")
    parser.add_argument("--once", action="store_true", help="run one collection pass")
    parser.add_argument("--dry-run", action="store_true", help="report facts without writing")
    parser.add_argument(
        "--date", type=_parse_day, help="Eastern (America/New_York) target date (YYYY-MM-DD)"
    )
    args = parser.parse_args(argv)
    if not args.once:
        parser.error("--once is required")
    result = collect_day(SqliteStore(default_db_path()), target_day=args.date, dry_run=args.dry_run)
    summary = {
        "day": result.day,
        "facts_written": 0 if args.dry_run else len(result.facts),
        "facts": len(result.facts),
        "data_quality": [note.row() for note in result.notes],
        "dry_run": args.dry_run,
        "outcome": result.outcome,
        "alert": result.alert,
    }
    print(json.dumps(summary, sort_keys=True, default=str))
    # Fail-loud: a RED outcome on a real (non-dry-run) run must not exit 0 --
    # that was the original defect (every source failing, exit code always 0).
    if not args.dry_run and result.outcome == "red":
        return 1
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised through the module command
    raise SystemExit(main())
