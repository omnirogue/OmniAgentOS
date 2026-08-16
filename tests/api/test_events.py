from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

from starlette.requests import Request

from omniagentos.api.routes.control import events
from omniagentos.contracts import Events
from tests.api.fake_store import FakeStore


def _request() -> Request:
    async def receive() -> dict[str, str]:
        return {"type": "http.request"}

    return Request({"type": "http", "method": "GET", "path": "/api/events", "headers": []}, receive)


def _is_preamble(frame: str) -> bool:
    """The stream opens with an immediate flush (`retry:` + `: connected`) so the
    browser's EventSource.onopen fires at once; skip it to reach the first event."""
    stripped = frame.strip()
    return stripped.startswith("retry:") or stripped.startswith(":")


async def _first_frame(store: FakeStore, **query: object) -> str:
    response = await events(_request(), store, **query)
    iterator = response.body_iterator
    assert isinstance(iterator, AsyncIterator)
    try:
        async for raw in iterator:
            frame = raw.decode() if isinstance(raw, bytes) else raw
            if not _is_preamble(frame):
                return frame
        raise AssertionError("stream produced no event frame")
    finally:
        await iterator.aclose()


def test_events_flushes_connected_preamble_immediately() -> None:
    store = FakeStore()

    async def _preamble() -> str:
        response = await events(_request(), store, after_id=0, types=None, last_event_id=None)
        iterator = response.body_iterator
        assert isinstance(iterator, AsyncIterator)
        try:
            first = await anext(iterator)
            return first.decode() if isinstance(first, bytes) else first
        finally:
            await iterator.aclose()

    frame = asyncio.run(_preamble())
    assert "retry:" in frame or frame.strip().startswith(":")


def test_events_replay_and_type_filter() -> None:
    store = FakeStore()
    store.insert_event(Events.AUDIT, "api", "one", payload={"n": 1})
    store.insert_event(Events.PAUSE_CHANGED, "api", "two", payload={"paused": True, "reason": "x"})

    frame = asyncio.run(
        _first_frame(store, after_id=0, types=Events.PAUSE_CHANGED, last_event_id=None)
    )
    assert "id: 2" in frame
    assert "event: pause.changed" in frame


def test_events_resync_when_cursor_is_outside_replay_window() -> None:
    store = FakeStore()
    for number in range(501):
        store.insert_event(Events.AUDIT, "api", "seed", payload={"number": number})

    frame = asyncio.run(_first_frame(store, after_id=0, types=None, last_event_id=None))
    assert "event: resync" in frame
    assert '"latest_id":501' in frame


def test_events_synthesizes_heartbeats_without_durable_event() -> None:
    store = FakeStore()
    store.upsert_heartbeat("worker-1", 1, "run_1")

    frame = asyncio.run(
        _first_frame(store, after_id=0, types=Events.WORKER_HEARTBEAT, last_event_id=None)
    )
    assert "event: worker.heartbeat" in frame
    assert '"worker_id":"worker-1"' in frame
    assert store.events == []
