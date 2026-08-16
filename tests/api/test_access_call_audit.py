"""K5: the API broker door records each call ONCE, and names who made it.

Two defects lived in ``/api/access/call``:

* it wrote its own legacy ``broker_calls`` row on top of the two the U-A1
  spine already writes from inside ``broker.call``, so an API-originated call
  to a credentialed capability produced THREE rows and every count over
  ``broker_calls`` double-counted it; and
* it passed no ``audit_context``, so the two rows the broker DID own were
  attributed to the default holder ``"system"`` under a synthetic run id --
  the spine's "who did this" was wrong precisely where the acting agent was
  real and authenticated.

These tests are written against a REAL migrated store and count rows, because
the previous coverage asserted the response payload and never looked at what
was persisted.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from omniagentos.api.routes import access
from omniagentos.api.routes.access import API_BROKER_HOLDER, BrokerCallRequest
from omniagentos.api.services import ApiError
from omniagentos.connectors import Capability, HttpSpec, broker
from omniagentos.connectors.store import CapabilityStore
from omniagentos.contracts import ActionClass
from omniagentos.db.store import SqliteStore
from tests.support.db_template import make_store

AGENT_ID = "agt_fixture_actor"
RUN_ID = "run_fixture"


class _Response:
    status_code = 200
    is_success = True
    text = ""

    def json(self) -> dict[str, Any]:
        return {"ok": True}


def _cap(cap_id: str, *, credentialed: bool) -> Capability:
    return Capability(
        id=cap_id,
        connector="fixture",
        group="support",
        label=cap_id,
        action_class=ActionClass.READ_ONLY,
        http=HttpSpec(
            base_url="https://fixture.test",
            methods=["GET"],
            auth="bearer:FIXTURE_ENV" if credentialed else "",
        ),
    )


CREDENTIALED = _cap("fixture.read", credentialed=True)
FREE = _cap("fixture.echo", credentialed=False)


def _registry() -> SimpleNamespace:
    indexed = {c.id: c for c in (CREDENTIALED, FREE)}

    def capability(cap_id: str) -> Capability:
        return indexed[cap_id]

    return SimpleNamespace(
        capability=capability,
        connectors={"fixture": SimpleNamespace(env=["FIXTURE_ENV"])},
        groups={"support": SimpleNamespace(danger=False)},
    )


@pytest.fixture
def caps(tmp_path: Path) -> Any:
    path = str(tmp_path / "control.sqlite3")
    raw = make_store(SqliteStore, path)
    try:
        yield CapabilityStore(raw)
    finally:
        raw.close()


@pytest.fixture(autouse=True)
def _fixture_registry(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(broker, "load_registry", lambda *a, **k: _registry())
    monkeypatch.setenv("FIXTURE_ENV", "runtime-only-fixture")
    monkeypatch.setattr(httpx, "request", lambda *a, **k: _Response())


def _authorize(caps: CapabilityStore, cap_id: str) -> str:
    caps._conn.execute(
        "INSERT OR IGNORE INTO agents (id, name, model, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (AGENT_ID, "fixture actor", "test", "2026-01-01T00:00:00Z", "2026-01-01T00:00:00Z"),
    )
    caps._conn.execute(
        "INSERT OR REPLACE INTO agent_capabilities (agent_id, capability_id, granted_at) "
        "VALUES (?, ?, ?)",
        (AGENT_ID, cap_id, "2026-01-01T00:00:00Z"),
    )
    caps._conn.commit()
    return caps.issue_token(RUN_ID, AGENT_ID)


def _rows(caps: CapabilityStore) -> list[dict[str, Any]]:
    return list(reversed(caps.call_log()))


def test_a_credentialed_api_call_is_recorded_exactly_once_by_the_spine(
    caps: CapabilityStore,
) -> None:
    """Broker intent + terminal, and NOT a third row vouched for by the route."""
    token = _authorize(caps, CREDENTIALED.id)

    result = access.broker_call(
        BrokerCallRequest(capability=CREDENTIALED.id),
        caps,
        authorization=f"Bearer {token}",
    )

    assert result["ok"] is True
    rows = _rows(caps)
    assert [row["decision"] for row in rows] == ["intent", "allowed"], (
        f"expected exactly the broker's intent/terminal pair, got "
        f"{[row['decision'] for row in rows]}"
    )
    # One logical call, one call_id: a duplicate would break correlation too.
    assert len({row["call_id"] for row in rows}) == 1
    assert len(rows) == 2


def test_the_spine_names_the_acting_agent_and_the_real_run(
    caps: CapabilityStore,
) -> None:
    """``system`` under a synthetic run id is not an audit trail."""
    token = _authorize(caps, CREDENTIALED.id)

    access.broker_call(
        BrokerCallRequest(capability=CREDENTIALED.id),
        caps,
        authorization=f"Bearer {token}",
    )

    for row in _rows(caps):
        assert row["agent_id"] == AGENT_ID, (
            "the row must name the authenticated actor, not the lane it came through"
        )
        assert row["holder"] == API_BROKER_HOLDER
        assert row["holder"] != "system"
        assert row["run_id"] == RUN_ID, "the run must be the caller's, not a minted one"

    # ...and the rows are reachable from the operator's own view of that run.
    assert len(caps.call_log(RUN_ID)) == 2


def test_a_denied_credentialed_call_is_also_recorded_exactly_once(
    caps: CapabilityStore,
) -> None:
    """The dedup must hold on the refusal path or denials double-count instead."""
    # A token for an agent that holds nothing: the broker refuses ``not_granted``.
    caps._conn.execute(
        "INSERT OR IGNORE INTO agents (id, name, model, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (AGENT_ID, "fixture actor", "test", "2026-01-01T00:00:00Z", "2026-01-01T00:00:00Z"),
    )
    caps._conn.commit()
    token = caps.issue_token(RUN_ID, AGENT_ID)

    with pytest.raises(ApiError) as caught:
        access.broker_call(
            BrokerCallRequest(capability=CREDENTIALED.id),
            caps,
            authorization=f"Bearer {token}",
        )

    assert caught.value.code == "not_granted"
    rows = _rows(caps)
    assert [row["decision"] for row in rows] == ["intent", "denied"]
    assert len(rows) == 2
    assert {row["agent_id"] for row in rows} == {AGENT_ID}


def test_a_credential_free_call_keeps_exactly_one_route_written_row(
    caps: CapabilityStore,
) -> None:
    """The other side of the dedup: what the spine cannot see must not vanish.

    A credential-free capability takes ``broker._call_unlogged`` and writes no
    spine rows at all. Deleting the route's row outright would have erased those
    calls from "what did my agents do overnight" — so the route writes exactly
    one row, and only here.
    """
    token = _authorize(caps, FREE.id)

    access.broker_call(
        BrokerCallRequest(capability=FREE.id),
        caps,
        authorization=f"Bearer {token}",
    )

    rows = _rows(caps)
    assert len(rows) == 1, f"expected one route-written row, got {len(rows)}"
    assert rows[0]["agent_id"] == AGENT_ID
    assert rows[0]["holder"] == API_BROKER_HOLDER
    assert rows[0]["allowed"] == 1


def test_the_dedup_asks_the_broker_rather_than_guessing() -> None:
    """The predicate the route routes on, pinned to the audited/unaudited split."""
    import omniagentos.connectors.broker as broker_mod

    original = broker_mod.load_registry
    try:
        broker_mod.load_registry = lambda *a, **k: _registry()  # type: ignore[assignment]
        assert broker.audits_own_calls(CREDENTIALED.id) is True
        assert broker.audits_own_calls(FREE.id) is False
        # An unknown id also takes the unlogged path, so the route must keep it.
        assert broker.audits_own_calls("fixture.nonexistent") is False
    finally:
        broker_mod.load_registry = original  # type: ignore[assignment]


def test_the_api_holder_is_a_canonical_identity() -> None:
    """``AuditContext`` refuses non-canonical spellings; this one must pass it.

    If ``API_BROKER_HOLDER`` ever drifts to an ``agt_*`` spelling, every API
    broker call would raise ``invalid_holder`` instead of being audited.
    """
    normalized = broker.AuditContext(holder=API_BROKER_HOLDER, run_id=RUN_ID).normalized()
    assert normalized.holder == API_BROKER_HOLDER
    assert normalized.run_id == RUN_ID
