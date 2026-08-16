"""Shared fixtures for the queue-core suite.

Every test opens its own DB under ``tmp_path``. Nothing here may touch
``var/`` — the live serving DB and the live queue DB are both off limits, and a
test that wrote to either would be indistinguishable from production traffic.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from omniagentos.workqueue.store import WorkQueueStore

BASE_SHA = "0" * 39 + "a"
T0 = "2026-08-11T12:00:00Z"


def at(offset_s: float, base: str = T0) -> str:
    """An ISO timestamp ``offset_s`` seconds after ``base`` — tests drive the clock."""
    started = datetime.strptime(base, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
    return (started + timedelta(seconds=offset_s)).strftime("%Y-%m-%dT%H:%M:%SZ")


def submit(key: str, **overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "idempotency_key": key,
        "repo_url": "https://github.com/Globex/OmniAgentOS.git",
        "repo_slug": "OmniAgentOS",
        "base_sha": BASE_SHA,
        "branch": f"wq/{key}",
        "owned_paths": ["omniagentos/workqueue/**"],
        "brief_inline": "do the thing",
        "agent_profile": "script",
        "acceptance_cmd": "python3 -c 'print(1)'",
        "risk_class": "standard",
    }
    payload.update(overrides)
    return payload


@pytest.fixture
def db_path(tmp_path):
    return tmp_path / "workqueue.sqlite3"


@pytest.fixture
def store(db_path):
    store = WorkQueueStore(db_path)
    try:
        yield store
    finally:
        store.close()
