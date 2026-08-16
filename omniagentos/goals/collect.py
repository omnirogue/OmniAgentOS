"""Daily Stripe and Meta goal metric collection through the credential broker."""

from __future__ import annotations

import argparse
import json
import logging
import math
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from omniagentos.connectors import broker, load_registry
from omniagentos.contracts import Events, default_db_path, utc_now_iso
from omniagentos.db.store import SqliteStore
from omniagentos.steward.config import load_steward_config
from omniagentos.steward.store import StewardStore

logger = logging.getLogger(__name__)


def _credential(cap_id: str, env_name: str) -> str | None:
    """Resolve one connector-owned value through the audited broker."""
    try:
        value = broker.resolve_one_for(load_registry().capability(cap_id), env_name)
    except broker.BrokerDenied:
        return None
    return value or None

# The business — and the operator reading this at 2 AM — lives in US/Eastern. A
# "day" of revenue is an *Eastern calendar day*, never a UTC one. Computing the
# window in UTC (the previous bug) smeared "yesterday" by 4-5 hours: an 11 PM ET
# charge (03:00 UTC next day) was pushed into the following day or dropped
# entirely. All day math anchors on this zone. ZoneInfo carries the DST rules, so
# spring-forward (23 h) and fall-back (25 h) days convert to the correct instants.
EASTERN = ZoneInfo("America/New_York")


def eastern_yesterday() -> date:
    """The just-closed Eastern calendar day (default collection target)."""
    return datetime.now(EASTERN).date() - timedelta(days=1)


# Default goal definitions for FK ensure (H3).
_DEFAULT_GOALS = {
    "increase-revenue": {
        "id": "increase-revenue",
        "name": "increase-revenue",
        "description": "Increase net revenue while monitoring payment failures.",
        "discipline_id": "revenue",
        "north_star": {"source": "stripe", "metric": "net_revenue_usd", "window": "day"},
        "keywords": ["revenue", "sales", "stripe", "order", "refund", "churn"],
        "priority": 10,
        "status": "active",
    },
    "improve-ad-roi": {
        "id": "improve-ad-roi",
        "name": "improve-ad-roi",
        "description": "Improve paid-media return on ad spend.",
        "discipline_id": "ad-roi",
        "north_star": {"source": "meta", "metric": "roas", "window": "day"},
        "keywords": ["roas", "ads", "ad spend", "meta", "facebook", "cpc", "cpm", "campaign"],
        "priority": 20,
        "status": "active",
    },
}


@dataclass(frozen=True)
class Snapshot:
    """A metric ready to be persisted for one target day."""

    goal_id: str
    source: str
    metric: str
    value: float
    unit: str
    captured_at: str
    meta: dict[str, Any]

    def row(self) -> dict[str, Any]:
        return {
            "goal_id": self.goal_id,
            "source": self.source,
            "metric": self.metric,
            "value": self.value,
            "unit": self.unit,
            "window": "day",
            "captured_at": self.captured_at,
            "meta": self.meta,
        }


def _day_bounds(target_day: date) -> tuple[int, int, str]:
    """Unix bounds of one *Eastern* calendar day, plus a day-anchored capture stamp.

    ``start``/``end`` are the ET-midnight..ET-23:59:59 instants converted to unix
    seconds — the form Stripe's ``created[gte]``/``created[lte]`` expects. Because
    the window is built in ET, an 11 PM ET charge on day D lands in day D (it did
    not under the old UTC window, where 11 PM ET = 03:00 UTC on D+1).

    The third element is an ISO stamp carrying the ET UTC-offset (e.g.
    ``2026-07-10T23:59:59-04:00``); its ``[:10]`` prefix is the ET calendar day,
    which is what ``insert_metric_snapshot`` uses to keep one row per day. It is a
    real instant with a real offset — never a UTC-labelled ("Z") Eastern time.
    """
    start = datetime.combine(target_day, time.min, tzinfo=EASTERN)
    end = start + timedelta(days=1) - timedelta(seconds=1)
    return int(start.timestamp()), int(end.timestamp()), end.isoformat()


def _body(result: dict[str, Any]) -> dict[str, Any] | None:
    if not result.get("ok") or int(result.get("status", 0)) >= 400:
        return None
    value = result.get("body")
    return value if isinstance(value, dict) else None


def _skip_reason(result: dict[str, Any] | None) -> str:
    if result is None:
        return "invalid broker response"
    status = result.get("status")
    return f"broker HTTP failure ({status})" if status is not None else "broker request failed"


class StripeCollector:
    """Collect successful-charge net revenue and failed payment count."""

    name = "stripe"
    goal_id = "increase-revenue"

    def collect(self, target_day: date) -> tuple[list[Snapshot], str | None]:
        """Collect Stripe charges with full pagination (up to safety limit of 100 pages).

        H14: Replaces hardcoded range(10) with has_more/starting_after loop.
        When pagination limit is hit, sets meta["truncated"]=True and logs.
        """
        if not _credential("stripe_acmeuni.read", "ACMEUNI_STRIPE_PRIMARY_SECRET_KEY"):
            return [], "ACMEUNI_STRIPE_PRIMARY_SECRET_KEY is not configured"

        start, end, captured_at = _day_bounds(target_day)
        payload: dict[str, Any] | None = None
        charges: list[dict[str, Any]] = []
        starting_after: str | None = None
        page_count = 0
        MAX_PAGES = 100  # Safety limit: 100 pages * 100 per page = 10k charges
        truncated = False

        while page_count < MAX_PAGES:
            query: dict[str, Any] = {
                "created[gte]": start,
                "created[lte]": end,
                "limit": 100,
            }
            if starting_after:
                query["starting_after"] = starting_after
            try:
                result = broker.call(
                    "stripe_acmeuni.read",
                    ["stripe_acmeuni.read"],
                    method="GET",
                    path="/v1/charges",
                    query=query,
                )
            except Exception as exc:
                return [], f"broker denied or unavailable: {exc}"
            payload = _body(result)
            if payload is None:
                return [], _skip_reason(result)
            page_data = payload.get("data", [])
            if not isinstance(page_data, list):
                return [], "invalid Stripe response"
            page = [charge for charge in page_data if isinstance(charge, dict)]
            charges.extend(page)
            page_count += 1

            if not payload.get("has_more") or not page:
                break
            last_id = page[-1].get("id")
            if not isinstance(last_id, str) or not last_id:
                return [], "Stripe pagination response has no charge id"
            starting_after = last_id

        # Check if we hit the pagination limit.
        if page_count >= MAX_PAGES and payload is not None and payload.get("has_more"):
            truncated = True
            logger.warning(
                f"Stripe pagination truncated at {MAX_PAGES} pages ({len(charges)} charges) for {target_day}"
            )

        net_revenue = sum(
            (float(charge.get("amount", 0)) - float(charge.get("amount_refunded", 0))) / 100
            for charge in charges
            if charge.get("status") == "succeeded"
        )
        failures = float(sum(1 for charge in charges if charge.get("status") == "failed"))
        meta = {"date": target_day.isoformat(), "charges": len(charges)}
        if truncated:
            meta["truncated"] = True
        return [
            Snapshot(
                self.goal_id, self.name, "net_revenue_usd", net_revenue, "usd", captured_at, meta
            ),
            Snapshot(
                self.goal_id, self.name, "payment_failures", failures, "count", captured_at, meta
            ),
        ], None


class MetaCollector:
    """Collect a Meta account's daily spend and purchase ROAS."""

    name = "meta"
    goal_id = "improve-ad-roi"

    def collect(self, target_day: date) -> tuple[list[Snapshot], str | None]:
        account_ids = _credential("meta_acmeuni.read", "ACMEUNI_META_AD_ACCOUNT_IDS") or ""
        account_id = next((item.strip() for item in account_ids.split(",") if item.strip()), "")
        # The env value may be stored either bare (1642…) or already act_-prefixed
        # (act_1642…). Normalize to the bare id so the path is a single act_ prefix
        # — otherwise the Graph call becomes /act_act_1642…/insights and 400s. (This
        # is a live-only bug the mocked tests, which use a bare id, never surfaced.)
        if account_id.startswith("act_"):
            account_id = account_id[len("act_") :]
        if not _credential("meta_acmeuni.read", "ACMEUNI_META_ACCESS_TOKEN"):
            return [], "ACMEUNI_META_ACCESS_TOKEN is not configured"
        if not account_id:
            return [], "ACMEUNI_META_AD_ACCOUNT_IDS is not configured"

        _, _, captured_at = _day_bounds(target_day)
        try:
            result = broker.call(
                "meta_acmeuni.read",
                ["meta_acmeuni.read"],
                method="GET",
                path=f"/act_{account_id}/insights",
                query={
                    "time_range": json.dumps(
                        {"since": target_day.isoformat(), "until": target_day.isoformat()}
                    ),
                    "fields": "spend,purchase_roas,actions",
                },
            )
        except Exception as exc:
            return [], f"broker denied or unavailable: {exc}"
        payload = _body(result)
        if payload is None:
            return [], _skip_reason(result)
        if "data" not in payload:
            return [], "invalid Meta response: missing data"
        data = payload["data"]
        if not isinstance(data, list):
            return [], "invalid Meta response"
        row = next((item for item in data if isinstance(item, dict)), {})
        if data and (
            "spend" not in row
            or row["spend"] is None
            or not _is_number(row["spend"])
        ):
            return [], "unreadable Meta spend on campaign row"
        spend = _number(row.get("spend", 0.0))
        purchase_roas = row.get("purchase_roas", [])
        roas = 0.0
        if isinstance(purchase_roas, list) and purchase_roas and isinstance(purchase_roas[0], dict):
            roas = _number(purchase_roas[0].get("value", 0.0))
        # Meta interprets a plain YYYY-MM-DD time_range in the AD ACCOUNT's own
        # timezone, NOT ET/UTC. We pass since/until as plain dates and record that
        # the boundary was the account's tz so the figure can be reconciled honestly.
        meta = {
            "date": target_day.isoformat(),
            "ad_account_id": account_id,
            "time_range_tz": "ad_account",
        }
        return [
            Snapshot(self.goal_id, self.name, "spend_usd", spend, "usd", captured_at, meta),
            Snapshot(self.goal_id, self.name, "roas", roas, "ratio", captured_at, meta),
        ], None


def _number(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _is_number(value: Any) -> bool:
    try:
        return math.isfinite(float(value))
    except (TypeError, ValueError):
        return False


def collect_once(
    store: SqliteStore,
    *,
    target_day: date | None = None,
    dry_run: bool = False,
    collectors: Iterable[StripeCollector | MetaCollector] | None = None,
) -> list[dict[str, Any]]:
    """Run all collectors, returning the messages also printed by the CLI.

    H3: Ensures goal row exists before inserting snapshot (FK ensure).
    """
    load_steward_config()
    day = target_day or eastern_yesterday()
    steward = StewardStore(store)
    messages: list[dict[str, Any]] = []
    message: dict[str, Any]
    for collector in collectors or (StripeCollector(), MetaCollector()):
        snapshots, reason = collector.collect(day)
        if reason is not None:
            message = {"collector": collector.name, "skipped": reason}
            print(json.dumps(message, sort_keys=True))
            messages.append(message)
            continue

        # H3: Ensure the goal exists before inserting snapshots (FK ensure).
        goal_id = collector.goal_id
        existing_goal = steward.get_goal(goal_id)
        if existing_goal is None:
            default_goal = _DEFAULT_GOALS.get(goal_id)
            if default_goal is not None:
                created_at = default_goal.get("created_at", utc_now_iso())
                steward.upsert_goal({**default_goal, "created_at": created_at})
                logger.info(f"Created missing goal {goal_id}")

        for snapshot in snapshots:
            message = {"collector": collector.name, "snapshot": snapshot.row()}
            if dry_run:
                message["dry_run"] = True
            else:
                steward.insert_metric_snapshot(snapshot.row())
                store.insert_event(
                    Events.GOAL_METRIC,
                    "goals",
                    "snapshot",
                    target_type="goal",
                    target_id=snapshot.goal_id,
                    payload={"metric": snapshot.metric, "value": snapshot.value},
                )
                message["inserted"] = True
            print(json.dumps(message, sort_keys=True, default=str))
            messages.append(message)
    return messages


def _parse_day(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("date must be YYYY-MM-DD") from exc


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Collect daily business-goal metrics")
    parser.add_argument("--once", action="store_true", help="run one collection pass")
    parser.add_argument("--dry-run", action="store_true", help="report snapshots without writing")
    parser.add_argument(
        "--date", type=_parse_day, help="Eastern (America/New_York) target date (YYYY-MM-DD)"
    )
    args = parser.parse_args(argv)
    if not args.once:
        parser.error("--once is required")
    collect_once(SqliteStore(default_db_path()), target_day=args.date, dry_run=args.dry_run)
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised through the module command
    raise SystemExit(main())
