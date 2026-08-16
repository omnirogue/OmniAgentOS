from __future__ import annotations

from pathlib import Path

import pytest

from omniagentos.db.store import SqliteStore
from omniagentos.steward.config import AlertsConfig, StewardConfig
from omniagentos.steward.store import StewardStore
from tests.support.db_template import make_store


@pytest.fixture
def database(tmp_path: Path) -> SqliteStore:
    return make_store(SqliteStore, tmp_path / "alerts.db")


@pytest.fixture
def steward(database: SqliteStore) -> StewardStore:
    return StewardStore(database)


@pytest.fixture
def alerts_config() -> AlertsConfig:
    return AlertsConfig(
        roas_floor=1.0,
        spend_spike_pct=50.0,
        payment_failure_burst=3,
        vip_senders=["vip@example.com"],
        cooldown_minutes=60,
        urgent_patterns=["urgent"],
    )


@pytest.fixture
def steward_config(alerts_config: AlertsConfig) -> StewardConfig:
    return StewardConfig(alerts=alerts_config)


@pytest.fixture
def triaged_marker_path(tmp_path: Path) -> Path:
    """Isolated per-test path for monitor.py's "already triaged" marker file.

    Without this, monitor_once would fall back to its production default
    (next to contracts.default_db_path()'s directory), which is the SAME real
    var/ dir for every test process regardless of each test's own tmp_path
    sqlite db -- silently leaking triaged-message state across tests whose
    autoincrement comms-message ids can coincidentally collide (e.g. both
    using row id 1 in their own fresh per-test db).
    """
    return tmp_path / "steward-triaged.json"
