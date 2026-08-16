"""T-DESIGN-002 / AC-15: session.updated is SSE-synthesized, never persisted."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any

import pytest
from starlette.requests import Request

from omniagentos.api.routes import control
from omniagentos.api.routes.control import events
from omniagentos.contracts import Events
from tests.api.fake_store import FakeStore


class FakeReader:
    def list_sessions(self, limit: int = 200) -> list[dict[str, Any]]:
        del limit
        return [
            {
                "id": "ses_x",
                "source": "bridge",
                "state": "awaiting_approval",
                "project_dir": "/p",
                "title": "t",
                "model": "haiku",
                "cost_usd": 0.0,
                "last_activity_at": None,
                "updated_at": "2026-01-01T00:00:00Z",
            }
        ]

    def close(self) -> None:  # pragma: no cover - trivial
        pass


def _request() -> Request:
    async def receive() -> dict[str, str]:
        return {"type": "http.request"}

    return Request({"type": "http", "method": "GET", "path": "/api/events", "headers": []}, receive)


async def _first_frame(store: FakeStore, **query: object) -> str:
    response = await events(_request(), store, **query)
    iterator = response.body_iterator
    assert isinstance(iterator, AsyncIterator)
    try:
        # The stream opens with an immediate flush (`retry:` + `: connected`) so
        # EventSource.onopen fires at once; skip it to reach the first event.
        async for raw in iterator:
            frame = raw.decode() if isinstance(raw, bytes) else raw
            stripped = frame.strip()
            if stripped.startswith("retry:") or stripped.startswith(":"):
                continue
            return frame
        raise AssertionError("stream produced no event frame")
    finally:
        await iterator.aclose()


def test_session_updated_is_synthesized_without_durable_event(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(control, "_open_sessions_reader", lambda: FakeReader())
    store = FakeStore()

    frame = asyncio.run(
        _first_frame(store, after_id=0, types=Events.SESSION_UPDATED, last_event_id=None)
    )
    assert "event: session.updated" in frame
    assert '"session_id":"ses_x"' in frame
    assert '"state":"awaiting_approval"' in frame
    # SSE-only: nothing persisted to the events table.
    assert store.events == []
