"""MAJOR 4 — ``web.fetch`` is reachable from the runtime, not only importable.

The parent-side loop-effects seam is the surface a loop worker actually speaks
to. These tests drive it the way a worker does — a protocol envelope naming a
capability — and prove the whole chain: seam → grant floor → standing grant row
→ SSRF-guarded, IP-pinned fetch → U-A1 audit pair.

``INSTANCE_CAPABILITIES`` deliberately lists no web reader yet (see the comment
on that table), so the floor is monkeypatched here exactly as
``tests/scheduler/test_loop_effects.py`` monkeypatches ``CAPABILITIES``: what is
under test is that the wiring WORKS the moment an operator turns both keys, and
that it refuses when either key is missing.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

import httpx
import pytest

from omniagentos.connectors.store import CapabilityStore
from omniagentos.contracts import ActionClass
from omniagentos.db.migrate import migrate
from omniagentos.db.store import SqliteStore
from omniagentos.scheduler import loop_effects
from omniagentos.scheduler.loop_effects import (
    OUTCOME_OK,
    OUTCOME_REFUSED,
    SEAM_PROTOCOL_VERSION,
    execute,
)

INSTANCE = "research_probe"
HOLDER = f"loop:{INSTANCE}"


@pytest.fixture
def db_path(tmp_path: Path) -> str:
    path = str(tmp_path / "control.sqlite3")
    migrate(path)
    return path


def _envelope(url: str = "https://fixture.example/article") -> dict[str, Any]:
    return {
        "v": SEAM_PROTOCOL_VERSION,
        "instance": INSTANCE,
        "capability": "web.fetch",
        "args": {"url": url},
    }


def _grant_web_fetch(db_path: str) -> None:
    """The broker-side key: the standing grant row an operator/migration seeds."""
    # The same two rows migration 108 seeds for the replicate loops: the holder
    # must exist as an agent before it can hold a capability.
    connection = sqlite3.connect(db_path)
    try:
        connection.execute(
            "INSERT OR IGNORE INTO agents "
            "(id, name, lineage, model, expertise_json, trust_level, status, "
            "created_at, updated_at) "
            "VALUES (?, ?, 'scheduler.loop_effects', NULL, '[]', 'T1', 'idle', "
            "CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)",
            (HOLDER, HOLDER),
        )
        connection.commit()
    finally:
        connection.close()
    store = SqliteStore(db_path)
    try:
        CapabilityStore(store).set_grant(HOLDER, ["web.fetch"], actor="test")
    finally:
        store.close()


def _floor(monkeypatch: pytest.MonkeyPatch) -> None:
    """The source-side key: the in-source floor for this loop instance."""
    monkeypatch.setitem(
        loop_effects.INSTANCE_CAPABILITIES, INSTANCE, frozenset({"web.fetch"})
    )


def test_web_fetch_is_a_declared_read_only_parent_capability() -> None:
    capability = loop_effects.CAPABILITIES["web.fetch"]
    assert capability.action_class is ActionClass.READ_ONLY
    assert capability.broker_capability == "web.fetch"
    assert "web.fetch" not in loop_effects.PAID_CAPABILITIES


def test_a_granted_loop_worker_reaches_the_web_through_the_seam(
    db_path: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    _floor(monkeypatch)
    _grant_web_fetch(db_path)
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers["host"])
        return httpx.Response(200, content=b"public article body", request=request)

    real_fetch = loop_effects_fetch()
    monkeypatch.setattr(
        "omniagentos.connectors.web_read.fetch",
        lambda url, **kwargs: real_fetch(
            url,
            transport=httpx.MockTransport(handler),
            host_resolver=lambda _host: ("93.184.216.34",),
            **kwargs,
        ),
    )

    answer = execute(_envelope(), db_path=db_path)

    assert answer["outcome"] == OUTCOME_OK, answer
    assert answer["result"]["content"] == "public article body"
    assert answer["result"]["receipt"]["resolved_ip"] == "93.184.216.34"
    assert seen == ["fixture.example"]

    store = SqliteStore(db_path)
    try:
        rows = CapabilityStore(store).call_log(run_id=HOLDER)
    finally:
        store.close()
    assert sorted(row["decision"] for row in rows) == ["allowed", "intent"]
    assert all(row["target_host"] == "fixture.example" for row in rows)
    assert all(row["path"] == "/article" for row in rows)


def test_a_loop_on_the_floor_without_a_standing_grant_row_is_refused(
    db_path: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One key is not enough: the source floor alone cannot reach the network."""
    _floor(monkeypatch)
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover
        seen.append(str(request.url))
        return httpx.Response(200, content=b"leaked", request=request)

    real_fetch = loop_effects_fetch()
    monkeypatch.setattr(
        "omniagentos.connectors.web_read.fetch",
        lambda url, **kwargs: real_fetch(
            url,
            transport=httpx.MockTransport(handler),
            host_resolver=lambda _host: ("93.184.216.34",),
            **kwargs,
        ),
    )

    answer = execute(_envelope(), db_path=db_path)

    assert answer["outcome"] == OUTCOME_REFUSED
    assert answer["reason"] == "not_granted"
    assert seen == []


def test_a_loop_without_the_floor_is_refused_even_holding_the_grant_row(
    db_path: str,
) -> None:
    """The other key alone is not enough either; absence from the floor is denial."""
    _grant_web_fetch(db_path)
    answer = execute(_envelope(), db_path=db_path)
    assert answer["outcome"] == OUTCOME_REFUSED
    assert answer["reason"] == "not_granted"


def test_the_seam_cannot_be_used_to_reach_the_control_plane(
    db_path: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A fully authorized worker still cannot aim the capability at localhost."""
    _floor(monkeypatch)
    _grant_web_fetch(db_path)

    answer = execute(_envelope("http://169.254.169.254/latest/meta-data/"), db_path=db_path)

    assert answer["outcome"] == OUTCOME_REFUSED
    assert answer["reason"] == "ssrf_refused"

    store = SqliteStore(db_path)
    try:
        rows = CapabilityStore(store).call_log(run_id=HOLDER)
    finally:
        store.close()
    assert sorted(row["decision"] for row in rows) == ["denied", "intent"]
    assert all(row["target_host"] == "169.254.169.254" for row in rows)


def test_the_seam_refuses_a_url_that_is_not_a_string(
    db_path: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    _floor(monkeypatch)
    _grant_web_fetch(db_path)
    envelope = _envelope()
    envelope["args"] = {"url": 12}
    answer = execute(envelope, db_path=db_path)
    assert answer["outcome"] == OUTCOME_REFUSED
    assert answer["reason"] == "invalid_arguments"


def loop_effects_fetch() -> Any:
    """The real ``web_read.fetch``, captured before any monkeypatching."""
    from omniagentos.connectors import web_read

    return web_read.fetch
