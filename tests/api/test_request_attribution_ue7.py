"""Tests for truthful request attribution (U-E7).

Verifies:
1. Worker-emitted requests name the worker canonically, never the operator.
2. Non-canonical spellings like agent:bob are rejected at write path.
3. Missing auth defaults to 'system' + trace logging.
4. Canonical spellings: lane:*, loop:*, job:*, human:*, system.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from omniagentos.api.deps import get_store
from omniagentos.api.main import app
from omniagentos.reliability.store import (
    SqliteReliabilityStore,
    _is_canonical_identity,
    _validate_from_agent_id,
)
from tests.api.fake_store import FakeStore
from tests.support.db_template import make_store


class RequestAttributionFakeStore(FakeStore):
    """FakeStore whose agent-request surface is the REAL store, not a re-implementation.

    The previous version re-implemented create/get/list over a dict. That fake
    was the reason a production defect (``sqlite3.Row.get``, which does not
    exist) stayed invisible: the API tests exercised the fake's happy path while
    every real read raised ``AttributeError``. Delegating to a migrated
    ``SqliteReliabilityStore`` means these routes are asserted against the code
    that actually runs; the fake still covers the rest of the API surface.
    """

    def __init__(self, db_path: Path) -> None:
        super().__init__()
        self._requests = make_store(SqliteReliabilityStore, db_path)

    def create_agent_request(self, *args: object, **kwargs: object) -> str:
        return self._requests.create_agent_request(*args, **kwargs)  # type: ignore[arg-type]

    def get_agent_request(self, *args: object, **kwargs: object):
        return self._requests.get_agent_request(*args, **kwargs)  # type: ignore[arg-type]

    def list_agent_requests(self, *args: object, **kwargs: object):
        return self._requests.list_agent_requests(*args, **kwargs)  # type: ignore[arg-type]


@pytest.fixture
def fake_store(tmp_path: Path) -> RequestAttributionFakeStore:
    """Request attribution store: real SQLite for agent requests."""
    return RequestAttributionFakeStore(tmp_path / "agent_requests.db")


@pytest.fixture
def auth_headers() -> dict[str, str]:
    """Auth headers for test requests."""
    return {"X-Session-Token": "test-token-123"}


@pytest.fixture(autouse=True)
def mock_verify_token(monkeypatch: pytest.MonkeyPatch) -> None:
    """Mock token verification to always succeed."""
    monkeypatch.setattr("omniagentos.sessions.token.verify_token", lambda _: True)


# ===== Validation tests (unit)


class TestCanonicalIdentityValidation:
    """Unit tests for canonical identity validation (§7, WI-6)."""

    def test_is_canonical_identity_accepts_system(self) -> None:
        """system is a valid canonical spelling."""
        assert _is_canonical_identity("system") is True

    def test_is_canonical_identity_accepts_lane_spellings(self) -> None:
        """lane:* spellings are canonical."""
        assert _is_canonical_identity("lane:runner.step") is True
        assert _is_canonical_identity("lane:swarm.planner") is True
        assert _is_canonical_identity("lane:swarm.worker.coding") is True
        assert _is_canonical_identity("lane:swarm.worker.research") is True

    def test_is_canonical_identity_rejects_agent_spelling(self) -> None:
        """agent:* spelling is NOT canonical (explicit rejection)."""
        assert _is_canonical_identity("agent:bob") is False

    def test_validate_from_agent_id_accepts_canonical(self) -> None:
        """Canonical spellings pass validation."""
        _validate_from_agent_id("system")
        _validate_from_agent_id("lane:swarm.worker.coding")

    def test_validate_from_agent_id_rejects_non_canonical(self) -> None:
        """Non-canonical spellings are rejected with clear error."""
        with pytest.raises(ValueError, match="Invalid from_agent_id"):
            _validate_from_agent_id("agent:bob")


class TestRequestAttributionIntegration:
    """Integration tests for request attribution (API + store)."""

    def test_decisive_worker_named_canonically(
        self, fake_store: RequestAttributionFakeStore, auth_headers: dict[str, str]
    ) -> None:
        """Decisive test: worker-emitted request names worker canonically."""
        app.dependency_overrides[get_store] = lambda: fake_store
        try:
            client = TestClient(app)
            worker_identity = "lane:swarm.worker.coding"
            response = client.post(
                "/api/org/agent-requests",
                json={"description": "Add a research agent"},
                headers={**auth_headers, "X-Agent-ID": worker_identity},
            )
            assert response.status_code == 200
            req_id = response.json()["id"]
            req = fake_store.get_agent_request(req_id)
            assert req.from_agent_id == worker_identity
            assert req.requested_by != "owner"
        finally:
            app.dependency_overrides.clear()

    def test_missing_auth_defaults_to_system(
        self, fake_store: RequestAttributionFakeStore, auth_headers: dict[str, str]
    ) -> None:
        """Missing authenticated identity → system."""
        app.dependency_overrides[get_store] = lambda: fake_store
        try:
            client = TestClient(app)
            response = client.post(
                "/api/org/agent-requests",
                json={"description": "Add an agent"},
                headers=auth_headers,
            )
            assert response.status_code == 200
            req_id = response.json()["id"]
            req = fake_store.get_agent_request(req_id)
            assert req.from_agent_id == "system"
        finally:
            app.dependency_overrides.clear()

    def test_counterfeit_non_canonical_spelling_rejected(
        self, fake_store: RequestAttributionFakeStore, auth_headers: dict[str, str]
    ) -> None:
        """Non-canonical spelling agent:bob is rejected."""
        app.dependency_overrides[get_store] = lambda: fake_store
        try:
            client = TestClient(app)
            response = client.post(
                "/api/org/agent-requests",
                json={"description": "Add an agent"},
                headers={**auth_headers, "X-Agent-ID": "agent:bob"},
            )
            assert response.status_code == 400
        finally:
            app.dependency_overrides.clear()
