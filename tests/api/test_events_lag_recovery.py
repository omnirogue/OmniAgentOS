"""A lagged SSE connection must recover the frames the cursor cannot replay.

``Subscription._deliver`` drops the remainder of a tick on the first
``asyncio.QueueFull``, and a tick's frames are ordered ``status, heartbeat,
session, event`` — so the frames at the front of the drop are exactly the
``Events.SSE_ONLY`` kinds that are never written to the events table and
therefore cannot be refetched by cursor. The hub's session dedupe stamp is
hub-wide, so a dropped ``session.updated`` is never published again.

The route already knows the connection lagged. These tests pin that it also
recovers the non-replayable kinds from the hub's cached snapshot, which is the
same source a fresh connect is caught up from.

Refs #197.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from starlette.requests import Request

from omniagentos.api.eventbus import FRAME_HEARTBEAT, FRAME_SESSION
from omniagentos.api.routes.control import events

Frame = tuple[str, dict[str, Any]]


class _Store:
    """Just enough store for the connect-time replay: no durable events at all."""

    def get_events_after(
        self, after_id: int, types: list[str] | None = None, limit: int = 500
    ) -> list[dict[str, Any]]:
        return []

    def latest_event_id(self) -> int:
        return 0

    def get_heartbeats(self) -> list[dict[str, Any]]:
        return []


class _LaggedSub:
    """A subscription whose queue overflowed while a session reached its end.

    First ``drain`` reports ``lagged=True`` with nothing buffered — the overflow
    dropped this tick's ``session.updated`` before it was ever enqueued, which is
    what ``_deliver`` does on ``QueueFull``. Every later drain is quiet: the hub
    burned the ``t2`` stamp on the dropped tick and will not publish it again.
    """

    def __init__(self, *, snapshot_rows: list[dict[str, Any]], beats: list[dict[str, Any]]):
        self._drains = 0
        self._snapshot_rows = snapshot_rows
        self._beats = beats
        self.snapshot_calls = 0

    async def drain(self, timeout: float) -> tuple[list[Frame], bool]:
        self._drains += 1
        if self._drains == 1:
            return [], True
        # Quiet stream. Sleep rather than spin so the bounded collector below
        # reaches its deadline without pinning a core.
        await asyncio.sleep(0.01)
        return [], False

    def snapshot(self) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        self.snapshot_calls += 1
        # Connect-time snapshot is the RUNNING row; by the time the connection
        # has lagged the hub's cache already holds the terminal row.
        if self.snapshot_calls == 1:
            return list(self._beats), [
                {**row, "state": "running", "updated_at": "t1"} for row in self._snapshot_rows
            ]
        return list(self._beats), list(self._snapshot_rows)

    def ring_replay(self, after_id: int) -> tuple[list[dict[str, Any]], bool]:
        # The ring covers the cursor and holds no durable events: the event half
        # of the resync is complete immediately, so nothing but the snapshot
        # recovery can produce the missing session frame.
        return [], True

    def close(self) -> None:
        return None


class _Hub:
    def __init__(self, sub: _LaggedSub) -> None:
        self._sub = sub

    def subscribe(self, *, wants_heartbeats: bool = True, wants_sessions: bool = True):
        return self._sub


async def _request() -> Request:
    async def receive() -> dict[str, str]:
        return {"type": "http.request"}

    return Request(
        {"type": "http", "method": "GET", "path": "/api/events", "headers": []},
        receive,
    )


async def _collect(sub: _LaggedSub, *, until: str, max_chunks: int = 60) -> str:
    """Read the SSE generator until ``until`` appears, or the stream goes quiet.

    A healthy quiet stream emits nothing between keepalives, so the iteration is
    bounded by a deadline as well as by ``until`` — otherwise the negative tests
    (which assert an absence) would hang instead of failing.
    """
    import omniagentos.api.eventbus as eventbus_module

    original = eventbus_module.get_event_hub
    eventbus_module.get_event_hub = lambda store: _Hub(sub)  # type: ignore[assignment]
    chunks: list[str] = []
    try:
        response = await events(
            await _request(), _Store(), after_id=0, types=None, last_event_id=None
        )
        iterator = response.body_iterator

        async def _pump() -> None:
            async for raw in iterator:
                chunks.append(raw.decode() if isinstance(raw, bytes) else raw)
                if until in "".join(chunks) or len(chunks) >= max_chunks:
                    return

        try:
            await asyncio.wait_for(_pump(), timeout=5.0)
        except TimeoutError:
            pass
        await iterator.aclose()
        return "".join(chunks)
    finally:
        eventbus_module.get_event_hub = original  # type: ignore[assignment]


def test_lagged_connection_recovers_the_terminal_session_update() -> None:
    """The defect: `s1` finishes during the overflow and the client never learns.

    `session.updated` is SSE-only (never in the events table), so the cursor
    resync at control.py cannot reach it, and the hub's hub-wide dedupe stamp
    means it is published exactly once — on the tick that was dropped.
    """
    sub = _LaggedSub(
        snapshot_rows=[{"id": "s1", "state": "done", "updated_at": "t2", "source": "claude"}],
        beats=[],
    )
    # `_format_sse` emits compact JSON (no space after the colon).
    body = asyncio.run(_collect(sub, until='"state":"done"'))

    assert "event: session.updated" in body, body
    assert '"session_id":"s1"' in body, body
    # The terminal state must appear, not just the connect-time running row.
    assert '"state":"done"' in body, body
    assert '"updated_at":"t2"' in body, body


def test_lag_recovery_does_not_replay_rows_this_connection_already_saw() -> None:
    """Recovery must be deduped per connection, not a blind snapshot re-dump.

    The route keeps `session_updated_seen` per connection; a row whose stamp has
    not moved since the connect-time catch-up must not be re-emitted, or every
    overflow would cost one frame per live session.
    """
    sub = _LaggedSub(
        snapshot_rows=[{"id": "s1", "state": "running", "updated_at": "t1", "source": "claude"}],
        beats=[],
    )
    body = asyncio.run(_collect(sub, until="__never__", max_chunks=12))

    # Exactly one session.updated: the connect-time one. The lag recovery saw
    # the same stamp `t1` and stayed silent.
    assert body.count("event: session.updated") == 1, body


def test_lag_recovery_re_announces_heartbeats() -> None:
    """`worker.heartbeat` is SSE-only too and is dropped by the same line."""
    sub = _LaggedSub(
        snapshot_rows=[],
        beats=[{"worker_id": "w-1", "current_run_id": "run-9"}],
    )
    body = asyncio.run(_collect(sub, until="__never__", max_chunks=12))

    assert body.count("event: worker.heartbeat") >= 2, body


@pytest.mark.parametrize("kind", [FRAME_SESSION, FRAME_HEARTBEAT])
def test_the_non_replayable_frame_kinds_are_the_ones_recovery_must_cover(kind: str) -> None:
    """Guard the RULE, not today's list of kinds.

    A frame kind that the cursor resync cannot refetch must be recoverable from
    the hub snapshot. If a new SSE-only kind is added, this is the assertion
    that should be extended alongside it.
    """
    from omniagentos.contracts import Events

    sse_only = set(Events.SSE_ONLY)
    assert {Events.SESSION_UPDATED, Events.WORKER_HEARTBEAT} <= sse_only, (
        "SSE_ONLY changed; the lag-recovery path in control.py must cover every member"
    )
    assert kind in {FRAME_SESSION, FRAME_HEARTBEAT}
