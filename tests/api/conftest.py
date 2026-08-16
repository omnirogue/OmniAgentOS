"""Fixtures for the control-plane test suite."""

from __future__ import annotations

import sys
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest

from omniagentos.api.deps import get_store
from omniagentos.api.main import app
from omniagentos.sessions import token
from tests.api.fake_store import FakeStore
from tests.api.fixtures_content_type import (  # noqa: F401
    html_artifact,
    javascript_artifact,
    svg_artifact,
    text_artifact,
)


@pytest.fixture(autouse=True)
def _stop_chat_turn_bridge() -> Iterator[None]:
    """Kill the leaked ``chat-turn-bridge`` daemon after every api test.

    ``POST /api/chats/{id}/messages`` warms the process-global ChatTurnBridge
    singleton (``omniagentos/chats/bridge.py``), whose ``register`` starts a
    daemon tailer thread that only idle-exits 60s after its registry empties.
    Nothing in api teardown stops it, so exactly one ``chat-turn-bridge``
    thread survives ``tests/api`` into the rest of the suite — a scheduling
    amplifier behind flaky swarm/sessions ladder tests.

    Stop it after each test through the bridge's own idempotent ``stop()``.
    The ``sys.modules`` guard keeps this free for tests that never imported
    the bridge: it never imports (and so never constructs) the singleton
    itself, only stops one that already exists.
    """
    yield
    bridge_mod = sys.modules.get("omniagentos.chats.bridge")
    if bridge_mod is None:
        return
    try:
        bridge_mod.reset_chat_turn_bridge()
    except Exception:  # pragma: no cover - teardown must never fail a green test
        pass


@pytest.fixture
def store() -> FakeStore:
    return FakeStore()


@pytest.fixture
def auth_headers(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> dict[str, str]:
    """The X-Session-Token header the fix6-gated mutating routes require.

    Points the token file at a tmp path (0600, agent-unreadable in production)
    and mints it, mirroring how the dashboard's server-side proxy attaches it.
    """
    monkeypatch.setattr(token, "TOKEN_PATH", tmp_path / "sessions-token")
    return {"X-Session-Token": token.load_or_create_token()}


@pytest.fixture
def asgi_client(store: FakeStore) -> httpx.AsyncClient:
    """An ASGI client with no filesystem-backed database dependency."""
    app.dependency_overrides[get_store] = lambda: store
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver")
    try:
        yield client
    finally:
        app.dependency_overrides.clear()
