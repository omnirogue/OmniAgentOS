"""Daily AcmeUni PiedPiper pipeline-rollup collector — read-only, dark-safe.

WHY THIS IS DARK BY DEFAULT
----------------------------
No ``piedpiper_acmeuni.read`` grant exists for this collector's holder identity on the
day this lands, and the estate has no operator-clickable, project-bound path
to issue one yet (see the proposal's ``decision`` block). So the collector
PREFLIGHT-CHECKS ``CapabilityStore.get_grant(HOLDER)`` and returns before any
credential resolution or ``broker.call`` when the capability is absent — zero
broker calls, zero ``broker_calls`` audit rows, zero ``metric_snapshots``
rows. Landing this module executes nothing; a scheduled invocation with no
grant is a no-op every single day until an operator issues one.

WHY THE HOLDER PATTERN, NOT THE LEGACY SELF-GRANT LIST
--------------------------------------------------------
``omniagentos.connectors.broker.call``'s ``granted`` parameter is documented
legacy: production callers identify a ``grant_holder`` and thread the
database-backed ``grant_store``, leaving ``granted`` unset so the broker loads
the STANDING grant itself rather than trusting a caller-supplied list (the
self-grant privilege-escalation shape). This collector is new work, so it
uses the holder-backed path throughout — see
:mod:`omniagentos.scheduler.loop_effects` (``_broker_call``) for the same
shape already in production.

FOUR ROLLUPS, ALL-OR-NOTHING
------------------------------
Every read is collected into memory FIRST; the collector-owned
``sales-pipeline-visibility`` goal is ensured and all four snapshots are
persisted together only after all four reads succeed. A denied or malformed
response on any one of the four leaves ZERO ``source='piedpiper'`` rows for that
day — never a partial rollup.
"""

from __future__ import annotations

import argparse
import json
import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from omniagentos.connectors import broker
from omniagentos.connectors.store import CapabilityStore
from omniagentos.contracts import Events, default_db_path, utc_now_iso
from omniagentos.db.store import SqliteStore
from omniagentos.steward.config import load_steward_config
from omniagentos.steward.store import StewardStore

logger = logging.getLogger(__name__)

#: The single capability this collector ever asks the broker for. GET-only,
#: registered in configs/connectors.yaml under /contacts, /opportunities,
#: /pipelines, /conversations, /calendars.
CAPABILITY_ID = "piedpiper_acmeuni.read"

#: Collector-owned goal id, ensured in-collector (goals/collect.py:304-319's
#: seam), never written to the contended omniagentos/goals/seed.py.
GOAL_ID = "sales-pipeline-visibility"

#: This collector's grant-store identity. It need not be a registered `agents`
#: row for CapabilityStore.get_grant() to answer (a SELECT against an unknown
#: id simply returns ``[]``), which is exactly the dark-by-default behaviour
#: this module relies on before an operator provisions a real holder.
HOLDER = "piedpiper-pipeline-collector"

# A "day" of pipeline state is an Eastern calendar day, matching every other
# collector in this estate (see omniagentos/goals/collect.py).
EASTERN = ZoneInfo("America/New_York")

_DEFAULT_GOAL = {
    "id": GOAL_ID,
    "name": GOAL_ID,
    "description": "Track AcmeUni PiedPiper pipeline state (opportunities, contacts, conversations).",
    "discipline_id": "sales",
    "north_star": {"source": "piedpiper", "metric": "open_opportunities", "window": "day"},
    "keywords": ["piedpiper", "piedpiper", "pipeline", "opportunities", "sales", "crm"],
    "priority": 30,
    "status": "active",
}


@dataclass(frozen=True)
class Rollup:
    """One brokered GET this collector issues, and the metric it produces."""

    metric: str
    path: str
    query: dict[str, Any]


#: Four fixed, read-only rollups. Every path is a GET under a prefix
#: ``piedpiper_acmeuni.read`` already allows; nothing here is discovered at runtime.
ROLLUPS: tuple[Rollup, ...] = (
    Rollup("open_opportunities", "/opportunities/search", {"status": "open"}),
    Rollup("total_opportunities", "/opportunities/search", {}),
    Rollup("contacts_total", "/contacts", {}),
    Rollup("conversations_total", "/conversations/search", {}),
)


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


def eastern_yesterday() -> date:
    """The just-closed Eastern calendar day (default collection target)."""
    return datetime.now(EASTERN).date() - timedelta(days=1)


def _captured_at(target_day: date) -> str:
    """A day-anchored ET capture stamp, matching goals/collect.py:_day_bounds.

    ``insert_metric_snapshot`` keys its same-day UPSERT off ``captured_at[:10]``,
    so this must be a real ET instant (offset, never "Z") whose calendar-day
    prefix is ``target_day``.
    """
    end = datetime.combine(target_day, datetime.min.time(), tzinfo=EASTERN) + timedelta(
        days=1, seconds=-1
    )
    return end.isoformat()


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


def _count(body: dict[str, Any]) -> float:
    """Extract a rollup's scalar total from a PiedPiper search/list response body."""
    meta = body.get("meta")
    if isinstance(meta, dict):
        total = meta.get("total")
        if isinstance(total, (int, float)) and not isinstance(total, bool):
            return float(total)
    return 0.0


def _collect_snapshots(
    capability_store: CapabilityStore, target_day: date
) -> tuple[list[Snapshot], str | None]:
    """Preflight the grant, then issue all four GETs. Returns (snapshots, skip_reason).

    DARK-SAFE: the ``get_grant`` preflight below is the ONLY thing that runs
    when the capability is absent. No credential is resolved, ``broker.call``
    is never invoked, and no ``broker_calls`` audit row is written.
    """
    grant = capability_store.get_grant(HOLDER)
    if CAPABILITY_ID not in grant:
        return [], f"{CAPABILITY_ID} is not granted to {HOLDER!r}"

    captured_at = _captured_at(target_day)
    snapshots: list[Snapshot] = []
    for rollup in ROLLUPS:
        try:
            result = broker.call(
                CAPABILITY_ID,
                method="GET",
                path=rollup.path,
                query=dict(rollup.query),
                grant_store=capability_store,
                grant_holder=HOLDER,
                audit_store=capability_store,
            )
        except Exception as exc:  # noqa: BLE001 - broker denials collapse to one skip
            return [], f"broker denied or unavailable: {exc}"
        body = _body(result)
        if body is None:
            return [], _skip_reason(result)
        meta = {"date": target_day.isoformat(), "path": rollup.path}
        snapshots.append(
            Snapshot(GOAL_ID, "piedpiper", rollup.metric, _count(body), "count", captured_at, meta)
        )
    return snapshots, None


def collect_once(
    store: SqliteStore,
    *,
    target_day: date | None = None,
    dry_run: bool = False,
) -> list[dict[str, Any]]:
    """Run the collector once, returning the messages also printed by the CLI.

    All-or-nothing: the collector-owned goal is ensured and all four snapshots
    are persisted together, only after all four reads succeed. Absent grant,
    a denied response, or a malformed response each leave zero rows.
    """
    load_steward_config()
    day = target_day or eastern_yesterday()
    capability_store = CapabilityStore(store)
    steward = StewardStore(store)

    snapshots, reason = _collect_snapshots(capability_store, day)
    if reason is not None:
        message: dict[str, Any] = {"collector": "piedpiper", "skipped": reason}
        print(json.dumps(message, sort_keys=True))
        return [message]

    if dry_run:
        messages: list[dict[str, Any]] = [
            {"collector": "piedpiper", "snapshot": snapshot.row(), "dry_run": True}
            for snapshot in snapshots
        ]
        for message in messages:
            print(json.dumps(message, sort_keys=True, default=str))
        return messages

    # FK ensure (mirrors goals/collect.py:304-319): the collector owns this
    # goal's lifecycle rather than editing the contended goals/seed.py.
    if steward.get_goal(GOAL_ID) is None:
        steward.upsert_goal({**_DEFAULT_GOAL, "created_at": utc_now_iso()})
        logger.info(f"Created missing goal {GOAL_ID}")

    messages = []
    for snapshot in snapshots:
        steward.insert_metric_snapshot(snapshot.row())
        store.insert_event(
            Events.GOAL_METRIC,
            "goals",
            "snapshot",
            target_type="goal",
            target_id=snapshot.goal_id,
            payload={"metric": snapshot.metric, "value": snapshot.value},
        )
        message = {"collector": "piedpiper", "snapshot": snapshot.row(), "inserted": True}
        print(json.dumps(message, sort_keys=True, default=str))
        messages.append(message)
    return messages


def _parse_day(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("date must be YYYY-MM-DD") from exc


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Collect the daily AcmeUni PiedPiper pipeline rollup")
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
