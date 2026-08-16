"""Hard, durable spend reservations for live simulation/lab calls."""

from __future__ import annotations

import os
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path

import yaml

from omniagentos.contracts import BudgetSpec, utc_now_iso
from omniagentos.lab.contracts import Budgets
from omniagentos.routing.test_profile import (
    PROFILE_CONFIG_ENV,
    PROFILE_ENV,
    profile_config_path,
    profile_enabled,
)

CAMPAIGN_ENV = "OMNIAGENTOS_SIMULATION_CAMPAIGN_ID"
RESERVATION_DB_ENV = "OMNIAGENTOS_SIMULATION_BUDGET_DB"


class SimulationBudgetError(RuntimeError):
    """Invalid simulation budget configuration or a refused reservation."""


@dataclass(frozen=True, slots=True)
class SimulationBudgetProfile:
    max_usd_per_run: float
    max_usd_per_campaign: float
    on_exceeded: str


@dataclass(frozen=True, slots=True)
class SimulationReservation:
    """A durable pre-call reservation with an explicit lifecycle."""

    run_id: str
    campaign_id: str
    reserved_usd: float
    database: Path
    state: str = "active"
    actual_usd: float | None = None
    accounting_unknown: bool = False
    expires_at: str | None = None

    def settle(self, actual_usd: float | None) -> SimulationReservation:
        """Settle once; ``None`` preserves unknown accounting and opens the circuit.

        Settlement removes the full reservation from active capacity and counts
        only actual spend, so its unused remainder is released atomically.
        """

        return _settle_reservation(self.database, self.run_id, actual_usd)

    def record_spend(self, actual_usd: float | None) -> None:
        """Compatibility alias for callers that historically recorded final spend."""

        self.settle(actual_usd)

    def release(self) -> SimulationReservation:
        """Release an active reservation that never became provider spend."""

        return _transition_active_reservation(self.database, self.run_id, "released")

    def expire(self) -> SimulationReservation:
        """Expire an active reservation immediately."""

        return _transition_active_reservation(self.database, self.run_id, "expired")

def load_profile() -> SimulationBudgetProfile:
    """Load budget caps from the shared test-profile config path."""
    path = profile_config_path()
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        budget = data["test_profile"]["budget"]
        max_run = float(budget["max_usd_per_run"])
        max_campaign = float(budget["max_usd_per_campaign"])
        on_exceeded = str(budget["on_exceeded"]).strip().lower()
    except (KeyError, OSError, TypeError, ValueError, yaml.YAMLError) as exc:
        raise SimulationBudgetError(f"invalid simulation budget profile {path}: {exc}") from exc
    if max_run <= 0 or max_campaign <= 0 or max_campaign < max_run:
        raise SimulationBudgetError("simulation budget limits must be positive and coherent")
    if on_exceeded != "refuse":
        raise SimulationBudgetError("simulation profile budget.on_exceeded must be 'refuse'")
    return SimulationBudgetProfile(max_run, max_campaign, on_exceeded)


def reserve_live_simulation(
    budgets: Budgets,
    budget_spec: BudgetSpec,
    run_id: str,
    *,
    dry_run: bool,
    expires_at: str | None = None,
) -> SimulationReservation | None:
    """Reserve the full per-run cost ceiling before a live provider call."""

    if dry_run or not profile_enabled():
        return None
    errors = budgets.live_ceiling_errors()
    if errors:
        raise SimulationBudgetError(
            "live simulation requires complete ceilings: " + "; ".join(errors)
        )
    requested = budget_spec.cost_usd_max
    if requested is None or requested <= 0:
        raise SimulationBudgetError("live simulation requires a positive cost_usd ceiling")

    campaign_id = (os.environ.get(CAMPAIGN_ENV) or "").strip()
    database_raw = (os.environ.get(RESERVATION_DB_ENV) or "").strip()
    if not campaign_id:
        raise SimulationBudgetError(f"{CAMPAIGN_ENV} is required for live simulation")
    if not database_raw:
        raise SimulationBudgetError(f"{RESERVATION_DB_ENV} is required for live simulation")
    database = Path(database_raw).expanduser().resolve()
    database.parent.mkdir(parents=True, exist_ok=True)
    profile = load_profile()
    if requested > profile.max_usd_per_run:
        raise SimulationBudgetError(
            f"simulation run reservation ${requested:.2f} exceeds "
            f"${profile.max_usd_per_run:.2f} cap"
        )

    with closing(_connect(database)) as connection, connection:
        _ensure_schema(connection)
        connection.execute("BEGIN IMMEDIATE")
        existing = connection.execute(
            "SELECT * FROM simulation_reservations WHERE run_id = ?",
            (run_id,),
        ).fetchone()
        if existing is not None:
            if (
                str(existing["campaign_id"]) != campaign_id
                or float(existing["reserved_usd"]) != float(requested)
                or existing["expires_at"] != expires_at
            ):
                raise SimulationBudgetError(
                    f"simulation reservation id {run_id!r} was reused with different terms"
                )
            connection.commit()
            return _reservation_from_row(existing, database)

        campaign = connection.execute(
            "SELECT circuit_open FROM simulation_campaigns WHERE campaign_id = ?",
            (campaign_id,),
        ).fetchone()
        if campaign is None:
            connection.execute(
                "INSERT INTO simulation_campaigns(campaign_id, circuit_open) VALUES (?, 0)",
                (campaign_id,),
            )
        elif int(campaign[0]):
            raise SimulationBudgetError(f"simulation campaign {campaign_id!r} circuit is open")

        committed = float(
            connection.execute(
                "SELECT COALESCE(SUM(CASE "
                "  WHEN state = 'active' THEN reserved_usd "
                "  WHEN state = 'settled' THEN COALESCE(actual_usd, spent_usd) "
                "  ELSE 0 END), 0) "
                "FROM simulation_reservations WHERE campaign_id = ?",
                (campaign_id,),
            ).fetchone()[0]
        )
        if committed + requested > profile.max_usd_per_campaign:
            raise SimulationBudgetError(
                f"simulation campaign reservation would reach "
                f"${committed + requested:.2f}, above "
                f"${profile.max_usd_per_campaign:.2f} cap"
            )
        now = utc_now_iso()
        connection.execute(
            "INSERT INTO simulation_reservations"
            "(run_id, campaign_id, reserved_usd, spent_usd, actual_usd, state, "
            " accounting_unknown, created_at, expires_at) "
            "VALUES (?, ?, ?, 0, NULL, 'active', 0, ?, ?)",
            (run_id, campaign_id, float(requested), now, expires_at),
        )
        stored = connection.execute(
            "SELECT * FROM simulation_reservations WHERE run_id = ?",
            (run_id,),
        ).fetchone()
        assert stored is not None
        connection.commit()
    return _reservation_from_row(stored, database)


def expire_stale_reservations(
    database: str | Path,
    *,
    now: str | None = None,
) -> int:
    """Expire active reservations whose explicit deadline has elapsed."""

    path = Path(database).expanduser().resolve()
    with closing(_connect(path)) as connection, connection:
        connection.execute("BEGIN IMMEDIATE")
        cutoff = now or utc_now_iso()
        cursor = connection.execute(
            "UPDATE simulation_reservations "
            "SET state = 'expired', finished_at = ? "
            "WHERE state = 'active' AND expires_at IS NOT NULL AND expires_at <= ?",
            (cutoff, cutoff),
        )
        connection.commit()
        return int(cursor.rowcount)


def _reservation_from_row(
    row: sqlite3.Row,
    database: Path,
) -> SimulationReservation:
    return SimulationReservation(
        run_id=str(row["run_id"]),
        campaign_id=str(row["campaign_id"]),
        reserved_usd=float(row["reserved_usd"]),
        database=database,
        state=str(row["state"]),
        actual_usd=None if row["actual_usd"] is None else float(row["actual_usd"]),
        accounting_unknown=bool(row["accounting_unknown"]),
        expires_at=None if row["expires_at"] is None else str(row["expires_at"]),
    )


def _get_reservation(
    connection: sqlite3.Connection,
    run_id: str,
) -> sqlite3.Row:
    row = connection.execute(
        "SELECT * FROM simulation_reservations WHERE run_id = ?",
        (run_id,),
    ).fetchone()
    if row is None:
        raise SimulationBudgetError(f"unknown simulation reservation {run_id!r}")
    return row


def _settle_reservation(
    database: Path,
    run_id: str,
    actual_usd: float | None,
) -> SimulationReservation:
    if actual_usd is not None and actual_usd < 0:
        raise SimulationBudgetError("simulation spend cannot be negative")
    with closing(_connect(database)) as connection, connection:
        connection.execute("BEGIN IMMEDIATE")
        row = _get_reservation(connection, run_id)
        state = str(row["state"])
        stored_actual = None if row["actual_usd"] is None else float(row["actual_usd"])
        stored_unknown = bool(row["accounting_unknown"])
        requested_unknown = actual_usd is None
        if state == "settled":
            if stored_actual == actual_usd and stored_unknown == requested_unknown:
                connection.commit()
                return _reservation_from_row(row, database)
            raise SimulationBudgetError(
                f"simulation reservation {run_id!r} has a conflicting settlement"
            )
        if state != "active":
            raise SimulationBudgetError(f"simulation reservation {run_id!r} is already {state}")

        settled_at = utc_now_iso()
        spent = 0.0 if actual_usd is None else float(actual_usd)
        connection.execute(
            "UPDATE simulation_reservations "
            "SET spent_usd = ?, actual_usd = ?, accounting_unknown = ?, "
            "    state = 'settled', finished_at = ? "
            "WHERE run_id = ?",
            (spent, actual_usd, int(requested_unknown), settled_at, run_id),
        )
        overspent = actual_usd is not None and actual_usd > float(row["reserved_usd"])
        if requested_unknown or overspent:
            connection.execute(
                "UPDATE simulation_campaigns SET circuit_open = 1 WHERE campaign_id = ?",
                (str(row["campaign_id"]),),
            )
        stored = _get_reservation(connection, run_id)
        connection.commit()
    if overspent:
        raise SimulationBudgetError(
            f"simulation run spent ${actual_usd:.2f} beyond its "
            f"${float(row['reserved_usd']):.2f} reservation; campaign circuit opened"
        )
    return _reservation_from_row(stored, database)


def _transition_active_reservation(
    database: Path,
    run_id: str,
    target: str,
) -> SimulationReservation:
    with closing(_connect(database)) as connection, connection:
        connection.execute("BEGIN IMMEDIATE")
        row = _get_reservation(connection, run_id)
        state = str(row["state"])
        if state == target:
            connection.commit()
            return _reservation_from_row(row, database)
        if state != "active":
            raise SimulationBudgetError(f"simulation reservation {run_id!r} is already {state}")
        connection.execute(
            "UPDATE simulation_reservations SET state = ?, finished_at = ? WHERE run_id = ?",
            (target, utc_now_iso(), run_id),
        )
        stored = _get_reservation(connection, run_id)
        connection.commit()
        return _reservation_from_row(stored, database)


def _connect(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(path, timeout=30.0, isolation_level=None)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA busy_timeout = 30000")
    _ensure_schema(connection)
    return connection


def _ensure_schema(connection: sqlite3.Connection) -> None:
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS simulation_campaigns (
            campaign_id TEXT PRIMARY KEY,
            circuit_open INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS simulation_reservations (
            run_id TEXT PRIMARY KEY,
            campaign_id TEXT NOT NULL REFERENCES simulation_campaigns(campaign_id),
            reserved_usd REAL NOT NULL CHECK (reserved_usd > 0),
            spent_usd REAL NOT NULL DEFAULT 0 CHECK (spent_usd >= 0),
            actual_usd REAL CHECK (actual_usd IS NULL OR actual_usd >= 0),
            state TEXT NOT NULL DEFAULT 'active'
                CHECK (state IN ('active', 'settled', 'released', 'expired')),
            accounting_unknown INTEGER NOT NULL DEFAULT 0
                CHECK (accounting_unknown IN (0, 1)),
            created_at TEXT,
            expires_at TEXT,
            finished_at TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_simulation_reservations_campaign
            ON simulation_reservations(campaign_id);
        """
    )
    columns = {
        str(row["name"]) for row in connection.execute("PRAGMA table_info(simulation_reservations)")
    }
    additions = {
        "actual_usd": "REAL CHECK (actual_usd IS NULL OR actual_usd >= 0)",
        "state": (
            "TEXT NOT NULL DEFAULT 'active' "
            "CHECK (state IN ('active', 'settled', 'released', 'expired'))"
        ),
        "accounting_unknown": ("INTEGER NOT NULL DEFAULT 0 CHECK (accounting_unknown IN (0, 1))"),
        "created_at": "TEXT",
        "expires_at": "TEXT",
        "finished_at": "TEXT",
    }
    for name, definition in additions.items():
        if name not in columns:
            connection.execute(
                f"ALTER TABLE simulation_reservations ADD COLUMN {name} {definition}"
            )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_simulation_reservations_campaign_state "
        "ON simulation_reservations(campaign_id, state)"
    )


__all__ = [
    "CAMPAIGN_ENV",
    "PROFILE_CONFIG_ENV",
    "PROFILE_ENV",
    "RESERVATION_DB_ENV",
    "SimulationBudgetError",
    "SimulationBudgetProfile",
    "SimulationReservation",
    "expire_stale_reservations",
    "load_profile",
    "profile_enabled",
    "reserve_live_simulation",
]
