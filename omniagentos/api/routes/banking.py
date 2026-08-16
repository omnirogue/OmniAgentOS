"""Cash / banking dashboard HTTP surface.

GET /api/banking?day=YYYY-MM-DD reads the stored ``bank_facts`` for one Eastern
calendar day (default: yesterday ET) and returns the shape the Cash dashboard
builds against: bank balances, money in, TRUE cash expenses, net flow, a
month-to-date roll-up, per-account rows, the largest transactions, and the
honest data-quality / coverage surface. It is a pure read model — no external
calls, no writes.
"""

from __future__ import annotations

from datetime import date
from typing import Annotated, Any, cast

from fastapi import APIRouter, Query

from omniagentos.api.deps import StoreDep
from omniagentos.api.routes.control import fail
from omniagentos.banking.report import build_banking_report
from omniagentos.banking.store import BankingStore
from omniagentos.contracts import utc_now_iso
from omniagentos.db.store import SqliteStore
from omniagentos.goals.collect import eastern_yesterday

router = APIRouter(prefix="/api/banking", tags=["banking"])

_LARGEST_LIMIT = 10


def _resolve_day(day: str | None) -> str:
    if day is None:
        return eastern_yesterday().isoformat()
    try:
        return date.fromisoformat(day).isoformat()
    except ValueError:
        # fail() is NoReturn — it raises ApiError, so control never falls through.
        fail(422, "validation", "day must be YYYY-MM-DD", {"day": day})


@router.get("")
def get_banking(
    store: StoreDep,
    day: Annotated[str | None, Query(description="Eastern calendar day YYYY-MM-DD")] = None,
) -> dict[str, Any]:
    target_day = _resolve_day(day)
    month_start = date.fromisoformat(target_day).replace(day=1).isoformat()

    banking = BankingStore(cast(SqliteStore, store))
    accounts = banking.get_accounts()
    day_facts = banking.query_facts_day(target_day)
    month_to_date = banking.sum_facts_between(month_start, target_day)
    largest = banking.query_transactions_day(target_day, limit=_LARGEST_LIMIT)

    return build_banking_report(
        target_day,
        accounts,
        day_facts,
        month_to_date,
        largest,
        generated_at=utc_now_iso(),
    )
