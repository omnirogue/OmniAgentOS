"""Idempotently install the default business goals."""

from __future__ import annotations

from typing import Any

from omniagentos.contracts import default_db_path, utc_now_iso
from omniagentos.db.store import SqliteStore
from omniagentos.steward.config import load_steward_config
from omniagentos.steward.store import StewardStore

_DISCIPLINES = (
    {"id": "revenue", "name": "Revenue", "metric_contract": "{}", "status": "active"},
    {"id": "ad-roi", "name": "Ad ROI", "metric_contract": "{}", "status": "active"},
)

_GOALS = (
    {
        "id": "increase-revenue",
        "name": "increase-revenue",
        "description": "Increase net revenue while monitoring payment failures.",
        "discipline_id": "revenue",
        "north_star": {"source": "stripe", "metric": "net_revenue_usd", "window": "day"},
        "keywords": ["revenue", "sales", "stripe", "order", "refund", "churn"],
        "priority": 10,
        "status": "active",
    },
    {
        "id": "improve-ad-roi",
        "name": "improve-ad-roi",
        "description": "Improve paid-media return on ad spend.",
        "discipline_id": "ad-roi",
        "north_star": {"source": "meta", "metric": "roas", "window": "day"},
        "keywords": ["roas", "ads", "ad spend", "meta", "facebook", "cpc", "cpm", "campaign"],
        "priority": 20,
        "status": "active",
    },
)


def seed(store: SqliteStore | None = None) -> list[dict[str, Any]]:
    """Create the default disciplines and goals without duplicating either."""
    database = store or SqliteStore(default_db_path())
    # Loading the shared configuration keeps this entrypoint aligned with the rest
    # of the Steward surface, even though these defaults need no configuration.
    load_steward_config()
    for discipline in _DISCIPLINES:
        database.create_discipline({**discipline, "created_at": utc_now_iso()})
    steward = StewardStore(database)
    return [steward.upsert_goal(goal) for goal in _GOALS]


def main() -> int:
    seed()
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised through the module command
    raise SystemExit(main())
