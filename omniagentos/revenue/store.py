"""DAL for the dimensional ``revenue_facts`` store.

A thin, composed layer over an already-configured :class:`SqliteStore` — the same
pattern as :class:`omniagentos.steward.store.StewardStore`. It owns exactly two
operations the rest of the system needs: an idempotent per-day/vertical/source/
campaign UPSERT, and a whole-day read for the dashboard endpoint.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from typing import Any

from omniagentos.contracts import utc_now_iso
from omniagentos.db.store import SqliteStore


@dataclass
class RevenueFact:
    """One dimensional revenue/ad-spend fact for one ET calendar day.

    ``revenue_usd`` is real *collected* money (Stripe, net of refunds). Meta rows
    carry ``revenue_usd = 0`` and stash their platform-attributed conversion value
    under ``meta['revenue_attributed_usd']`` — attribution is an estimate and is
    never summed into real revenue.
    """

    day: str
    vertical: str
    source: str
    campaign_id: str | None = None
    campaign_name: str | None = None
    revenue_usd: float = 0.0
    ad_spend_usd: float = 0.0
    roas: float | None = None
    payment_failures: int = 0
    refunds_usd: float = 0.0
    currency: str = "usd"
    meta: dict[str, Any] = field(default_factory=dict)


_COLUMNS = (
    "day",
    "vertical",
    "source",
    "campaign_id",
    "campaign_name",
    "revenue_usd",
    "ad_spend_usd",
    "roas",
    "payment_failures",
    "refunds_usd",
    "currency",
    "captured_at",
    "meta_json",
)


def _row_to_fact(row: dict[str, Any]) -> dict[str, Any]:
    out = dict(row)
    raw = out.pop("meta_json", "{}") or "{}"
    try:
        out["meta"] = json.loads(raw)
    except (ValueError, TypeError):
        out["meta"] = {}
    return out


# Self-contained tables (CREATE IF NOT EXISTS), same pattern as
# omniagentos/taskcontract/store.py: this DAL owns its own schema rather than
# depending on a migration file, so the fail-loud data-quality contract does
# not require a schema migration to land.
#
# revenue_source_status tracks one row per (vertical, source) — the durable
# "has this source been quietly failing" streak the collector reads to decide
# whether a run is RED. Both 'failed' (broker/parse error) and 'unconfigured'
# (missing credential) count toward the streak: an operator who never set
# STRIPE_*_SECRET_KEY must see that as a growing failure, never a silent skip.
#
# revenue_alert_state is a once-per-day claim so an hourly re-collect that
# stays RED all day fires exactly one alert, not one per run.
_STATUS_SCHEMA = """
CREATE TABLE IF NOT EXISTS revenue_source_status (
    status_key           TEXT PRIMARY KEY,   -- "<vertical>:<source>"
    vertical              TEXT NOT NULL,
    source                TEXT NOT NULL,
    last_day              TEXT NOT NULL,      -- ET calendar day, YYYY-MM-DD
    last_status           TEXT NOT NULL,      -- 'ok' | 'failed' | 'unconfigured'
    last_message          TEXT NOT NULL DEFAULT '',
    consecutive_failures  INTEGER NOT NULL DEFAULT 0,
    updated_at            TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS revenue_alert_state (
    alert_key       TEXT PRIMARY KEY,
    last_alert_day  TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);
"""

_SOURCE_STATUSES = frozenset({"ok", "failed", "unconfigured"})


class RevenueStore:
    """Upsert and read dimensional revenue facts, plus the source data-quality streak."""

    def __init__(self, store: SqliteStore) -> None:
        self._store = store
        self._ensure_status_schema()

    @property
    def _connection(self) -> sqlite3.Connection:
        """The CALLING thread's connection, resolved live from the composed store.

        Never cache this on the instance. ``SqliteStore`` hands out one
        connection per thread and opens them with ``check_same_thread=False``,
        so a handle captured at construction time would bind this DAL to
        whichever thread built it and silently interleave its statements into
        another thread's transaction rather than raising.
        """
        return self._store._connection

    def upsert_revenue_fact(self, fact: RevenueFact) -> None:
        """Insert or refresh one fact, keyed on (day, vertical, source, campaign_id).

        Uses delete-then-insert inside one transaction rather than ``ON CONFLICT``:
        the key column ``campaign_id`` is nullable and SQLite treats each NULL as
        distinct, so an ON CONFLICT target would silently fail to match the
        brand-level (Stripe) rows. Matching with ``COALESCE(campaign_id,'')`` makes
        the hourly re-collect idempotent for every row, NULL campaign or not.
        """
        captured_at = utc_now_iso()
        values = {
            "day": fact.day,
            "vertical": fact.vertical,
            "source": fact.source,
            "campaign_id": fact.campaign_id,
            "campaign_name": fact.campaign_name,
            "revenue_usd": float(fact.revenue_usd),
            "ad_spend_usd": float(fact.ad_spend_usd),
            "roas": None if fact.roas is None else float(fact.roas),
            "payment_failures": int(fact.payment_failures),
            "refunds_usd": float(fact.refunds_usd),
            "currency": fact.currency,
            "captured_at": captured_at,
            "meta_json": json.dumps(fact.meta, separators=(",", ":"), sort_keys=True),
        }
        placeholders = ", ".join("?" for _ in _COLUMNS)
        columns = ", ".join(_COLUMNS)
        with self._store._lock:
            self._store._begin()
            try:
                self._connection.execute(
                    "DELETE FROM revenue_facts WHERE day = ? AND vertical = ? AND source = ? "
                    "AND COALESCE(campaign_id, '') = COALESCE(?, '')",
                    (fact.day, fact.vertical, fact.source, fact.campaign_id),
                )
                self._connection.execute(
                    f"INSERT INTO revenue_facts ({columns}) VALUES ({placeholders})",
                    tuple(values[column] for column in _COLUMNS),
                )
                self._store._commit()
            except BaseException:
                self._store._rollback()
                raise

    def query_day(self, day: str) -> list[dict[str, Any]]:
        """Return every fact for one ET calendar day, meta_json decoded to ``meta``."""
        with self._store._lock:
            rows = self._connection.execute(
                "SELECT * FROM revenue_facts WHERE day = ? "
                "ORDER BY vertical ASC, source ASC, COALESCE(campaign_id, '') ASC",
                (day,),
            ).fetchall()
        return [_row_to_fact(dict(row)) for row in rows]

    def aggregate_days(self, start_day: str, end_day: str) -> list[dict[str, Any]]:
        """Aggregate a day range by vertical and day in one indexed query.

        ``revenue_usd`` is already collected revenue net of refunds; callers must
        not subtract ``refunds_usd`` from it again.
        """
        with self._store._lock:
            rows = self._connection.execute(
                "SELECT day, vertical, SUM(revenue_usd) AS revenue_usd, "
                "SUM(ad_spend_usd) AS ad_spend_usd, SUM(refunds_usd) AS refunds_usd "
                "FROM revenue_facts WHERE day BETWEEN ? AND ? "
                "GROUP BY vertical, day ORDER BY vertical ASC, day ASC",
                (start_day, end_day),
            ).fetchall()
        return [dict(row) for row in rows]

    def _ensure_status_schema(self) -> None:
        with self._store._lock:
            self._connection.executescript(_STATUS_SCHEMA)
            self._connection.commit()

    def record_source_outcome(
        self, *, day: str, vertical: str, source: str, status: str, message: str = ""
    ) -> dict[str, Any]:
        """Persist today's outcome for one (vertical, source) and return the updated streak.

        Idempotent within one calendar day: re-running the collector multiple times
        for the same ``day`` (the hourly re-collect) refreshes the row in place and
        does not inflate the streak. The streak only advances when ``day`` moves
        forward from the previously recorded day, and only a non-'ok' status
        advances it; a fresh 'ok' resets it to 0 immediately, same-day or not.
        """
        if status not in _SOURCE_STATUSES:
            raise ValueError(
                f"invalid source status: {status!r} (expected one of {_SOURCE_STATUSES})"
            )
        key = f"{vertical}:{source}"
        now = utc_now_iso()
        with self._store._lock:
            self._store._begin()
            try:
                row = self._connection.execute(
                    "SELECT last_day, last_status, consecutive_failures "
                    "FROM revenue_source_status WHERE status_key = ?",
                    (key,),
                ).fetchone()
                if row is None:
                    consecutive = 0 if status == "ok" else 1
                elif status == "ok":
                    consecutive = 0
                elif row["last_day"] == day:
                    # Same-day re-run: a failing run that was already failing today
                    # keeps its streak; a first failure TODAY (prior run this day
                    # was ok) starts at 1 rather than double-incrementing.
                    consecutive = (
                        1 if row["last_status"] == "ok" else int(row["consecutive_failures"])
                    )
                else:
                    consecutive = int(row["consecutive_failures"]) + 1
                self._connection.execute(
                    "INSERT INTO revenue_source_status "
                    "(status_key, vertical, source, last_day, last_status, last_message, "
                    "consecutive_failures, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
                    "ON CONFLICT(status_key) DO UPDATE SET "
                    "last_day=excluded.last_day, last_status=excluded.last_status, "
                    "last_message=excluded.last_message, "
                    "consecutive_failures=excluded.consecutive_failures, "
                    "updated_at=excluded.updated_at",
                    (key, vertical, source, day, status, message, consecutive, now),
                )
                self._store._commit()
            except BaseException:
                self._store._rollback()
                raise
        return {"status_key": key, "status": status, "consecutive_failures": consecutive}

    def source_status_snapshot(self) -> list[dict[str, Any]]:
        """Every tracked (vertical, source) row, for the health-sentinel + alert path."""
        with self._store._lock:
            rows = self._connection.execute(
                "SELECT status_key, vertical, source, last_day, last_status, last_message, "
                "consecutive_failures, updated_at FROM revenue_source_status "
                "ORDER BY status_key ASC"
            ).fetchall()
        return [dict(row) for row in rows]

    def claim_daily_alert(self, *, alert_key: str, day: str) -> bool:
        """Best-effort once-per-day claim. True only the FIRST call for ``day``.

        Backs the "exactly one RED alert per bad day" contract: an hourly
        re-collect that stays RED all day must not fire the same alert on every
        pass.
        """
        now = utc_now_iso()
        with self._store._lock:
            self._store._begin()
            try:
                row = self._connection.execute(
                    "SELECT last_alert_day FROM revenue_alert_state WHERE alert_key = ?",
                    (alert_key,),
                ).fetchone()
                if row is not None and row["last_alert_day"] == day:
                    self._store._commit()
                    return False
                self._connection.execute(
                    "INSERT INTO revenue_alert_state (alert_key, last_alert_day, updated_at) "
                    "VALUES (?, ?, ?) ON CONFLICT(alert_key) DO UPDATE SET "
                    "last_alert_day=excluded.last_alert_day, updated_at=excluded.updated_at",
                    (alert_key, day, now),
                )
                self._store._commit()
            except BaseException:
                self._store._rollback()
                raise
        return True


__all__ = ["RevenueFact", "RevenueStore"]
