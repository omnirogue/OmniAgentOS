"""L12 — Backend realtime and route truth (C-08, H-41, H-42, L-01, L-05).

Behavioral tests (not source-text greps) for CORS origin policy, event-hub
degradation/recovery, design-agent spawn failure, projects/tree uniqueness,
and revenue undercount warnings.
"""

from __future__ import annotations

import asyncio
import json
import threading
import time
from pathlib import Path
from typing import Any
from unittest.mock import patch

import httpx
import pytest
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from starlette.requests import Request

from omniagentos.api.eventbus import (
    DEGRADED_AFTER_FAILURES,
    FRAME_DEGRADED,
    EventHub,
    _TickOutcome,
)
from omniagentos.api.main import (
    allowed_cors_origins,
    app,
    cors_middleware_kwargs,
    validate_cors_origin,
)
from omniagentos.api.routes.control import events
from omniagentos.reliability.taxonomy import AgentRequestStatus
from omniagentos.revenue.report import build_revenue_report
from tests.api.fake_store import FakeStore

FIXTURE_PATH = (
    Path(__file__).resolve().parents[2] / "contracts" / "fixtures" / "backend-realtime-v1.json"
)


def _load_fixture() -> dict[str, Any]:
    return json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# C-08 — CORS / product origins
# ---------------------------------------------------------------------------


def test_canonical_dashboard_origin_is_allowed_by_default() -> None:
    origins = allowed_cors_origins(env={})
    assert "http://127.0.0.1:3003" in origins
    assert "http://localhost:3003" in origins
    assert "*" not in origins
    assert cors_middleware_kwargs(origins)["allow_credentials"] is False


def test_allowed_and_denied_origins_match_contract_fixture() -> None:
    fixture = _load_fixture()
    for origin in fixture["origins"]["allowed_examples"]:
        assert validate_cors_origin(origin) is not None, origin
    for origin in fixture["origins"]["denied_examples"]:
        assert validate_cors_origin(origin) is None, origin


def test_explicit_cors_env_overrides_dashboard_port() -> None:
    origins = allowed_cors_origins(
        env={"OMNIAGENTOS_CORS_ORIGINS": "http://127.0.0.1:19112, http://evil.example"},
        dashboard_port=3001,
    )
    assert origins == ["http://127.0.0.1:19112"]


def test_extra_ports_append_loopback_origins() -> None:
    origins = allowed_cors_origins(
        env={"OMNIAGENTOS_CORS_EXTRA_PORTS": "3011, not-a-port"},
        dashboard_port=3001,
    )
    assert "http://127.0.0.1:3001" in origins
    assert "http://127.0.0.1:3011" in origins
    assert "http://localhost:3011" in origins


def test_explicit_origins_and_extra_ports_are_additive() -> None:
    origins = allowed_cors_origins(
        env={
            "OMNIAGENTOS_CORS_ORIGINS": "http://127.0.0.1:19112",
            "OMNIAGENTOS_CORS_EXTRA_PORTS": "3011",
        },
        dashboard_port=3001,
    )
    assert origins == [
        "http://127.0.0.1:19112",
        "http://127.0.0.1:3011",
        "http://localhost:3011",
    ]


def test_injected_environment_controls_dashboard_port() -> None:
    origins = allowed_cors_origins(env={"OMNIAGENTOS_DASH_PORT": "3011"})
    assert origins == [
        "http://127.0.0.1:3011",
        "http://localhost:3011",
    ]


def test_out_of_range_cors_ports_are_dropped_without_raising() -> None:
    """urlparse raises ValueError on ports like 99999; validation must not.

    Module import calls allowed_cors_origins() for CORSMiddleware — invalid env
    origins must be dropped, never crash startup.
    """
    assert validate_cors_origin("http://127.0.0.1:99999") is None
    assert validate_cors_origin("http://localhost:65536") is None
    assert validate_cors_origin("http://127.0.0.1:0") is None
    assert validate_cors_origin("http://127.0.0.1:65535") == "http://127.0.0.1:65535"

    # Mixed list: invalid dropped, valid retained (no exception).
    origins = allowed_cors_origins(
        env={
            "OMNIAGENTOS_CORS_ORIGINS": (
                "http://127.0.0.1:99999,http://127.0.0.1:3011,http://localhost:70000"
            )
        },
        dashboard_port=3001,
    )
    assert origins == ["http://127.0.0.1:3011"]

    # All-invalid explicit list falls back to dashboard port (no crash).
    fallback = allowed_cors_origins(
        env={"OMNIAGENTOS_CORS_ORIGINS": "http://127.0.0.1:99999"},
        dashboard_port=3001,
    )
    assert "http://127.0.0.1:3001" in fallback
    assert "http://localhost:3001" in fallback


def test_http_cors_allows_canonical_and_denies_foreign_origin() -> None:
    """Real middleware path: allowed origin gets ACAO; denied origin does not."""
    mini = FastAPI()
    origins = allowed_cors_origins(env={}, dashboard_port=3001)
    mini.add_middleware(CORSMiddleware, **cors_middleware_kwargs(origins))

    @mini.get("/api/health")
    def _health() -> dict[str, str]:
        return {"status": "ok"}

    async def _check() -> None:
        transport = httpx.ASGITransport(app=mini)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            allowed = await client.options(
                "/api/health",
                headers={
                    "Origin": "http://127.0.0.1:3001",
                    "Access-Control-Request-Method": "GET",
                },
            )
            assert allowed.status_code == 200
            assert allowed.headers.get("access-control-allow-origin") == "http://127.0.0.1:3001"

            denied = await client.options(
                "/api/health",
                headers={
                    "Origin": "http://evil.example",
                    "Access-Control-Request-Method": "GET",
                },
            )
            assert denied.headers.get("access-control-allow-origin") is None

    asyncio.run(_check())


# ---------------------------------------------------------------------------
# H-41 — Event hub degraded state + bounded recovery
# ---------------------------------------------------------------------------


class _FlakyStore:
    """Store that fails get_events_after until `failures_remaining` hits 0."""

    def __init__(self, failures_remaining: int = 100) -> None:
        self.failures_remaining = failures_remaining
        self.calls = 0
        self._events: list[dict[str, Any]] = []
        self._next_id = 1

    def get_events_after(
        self, after_id: int, types: list[str] | None = None, limit: int = 500
    ) -> list[dict[str, Any]]:
        del types, limit
        self.calls += 1
        if self.failures_remaining > 0:
            self.failures_remaining -= 1
            raise RuntimeError("store down")
        return [row for row in self._events if int(row["id"]) > after_id]

    def latest_event_id(self) -> int:
        return self._next_id - 1

    def get_heartbeats(self) -> list[dict[str, Any]]:
        return []

    def insert_event(self, **kwargs: Any) -> int:
        row = {"id": self._next_id, **kwargs}
        self._next_id += 1
        self._events.append(row)
        return int(row["id"])


class _ObservedStore:
    """Count every EventHub query and optionally block a successful event sample."""

    def __init__(self, *, block_event_sample: bool = False) -> None:
        self.latest_calls = 0
        self.heartbeat_calls = 0
        self.event_calls = 0
        self.event_sample_started = threading.Event()
        self.release_event_sample = threading.Event()
        if not block_event_sample:
            self.release_event_sample.set()

    def get_events_after(
        self, after_id: int, types: list[str] | None = None, limit: int = 500
    ) -> list[dict[str, Any]]:
        del after_id, types, limit
        self.event_calls += 1
        self.event_sample_started.set()
        if not self.release_event_sample.wait(2.0):
            raise TimeoutError("test did not release event sample")
        return []

    def latest_event_id(self) -> int:
        self.latest_calls += 1
        return 0

    def get_heartbeats(self) -> list[dict[str, Any]]:
        self.heartbeat_calls += 1
        return []


def _degrade_hub(hub: EventHub) -> None:
    hub._note_tick_failure(RuntimeError("previous store outage"), where="event_sample")
    assert hub.is_degraded()


def _pending_status_reasons(hub: EventHub) -> list[str]:
    with hub._lock:
        return [
            str(payload.get("reason"))
            for kind, payload in hub._pending_status_frames
            if kind == FRAME_DEGRADED
        ]


def test_degraded_zero_subscriber_start_and_tick_are_idle_not_recovery() -> None:
    """An idle tailer proves nothing about store recovery and costs zero queries."""
    store = _ObservedStore()
    hub = EventHub(
        store,
        sessions_reader_factory=lambda: None,
        poll_interval_s=0.01,
        degraded_after_failures=1,
        restart_after_failures=50,
        max_tailer_restarts=0,
    )
    _degrade_hub(hub)
    tick_seen = threading.Event()
    original_tick = hub._tick

    def observed_tick(stop: threading.Event, generation: int) -> Any:
        try:
            return original_tick(stop, generation)
        finally:
            tick_seen.set()

    hub._tick = observed_tick  # type: ignore[method-assign]
    hub.start()
    try:
        assert tick_seen.wait(1.0), "idle tailer did not execute a tick"
    finally:
        hub.stop(timeout=1.0)

    status = hub.status()
    assert status["degraded"] is True
    assert status["last_success_at"] is None
    assert "tailer_recovered" not in _pending_status_reasons(hub)
    assert (store.latest_calls, store.heartbeat_calls, store.event_calls) == (0, 0, 0)


def test_last_subscriber_close_cannot_turn_inflight_sample_into_recovery() -> None:
    """A sample that finishes after the last close is not recovery evidence."""
    store = _ObservedStore(block_event_sample=True)
    hub = EventHub(
        store,
        sessions_reader_factory=lambda: None,
        poll_interval_s=1.0,
        degraded_after_failures=1,
        restart_after_failures=50,
        max_tailer_restarts=0,
    )

    async def _run() -> tuple[dict[str, Any], bool]:
        sub = hub.subscribe(wants_heartbeats=False, wants_sessions=False)
        assert await asyncio.to_thread(store.event_sample_started.wait, 1.0)
        _degrade_hub(hub)
        with hub._lock:
            tailer = hub._thread
        assert tailer is not None

        sub.close()
        store.release_event_sample.set()
        await asyncio.to_thread(tailer.join, 1.0)
        return hub.status(), tailer.is_alive()

    status, tailer_alive = asyncio.run(_run())
    assert tailer_alive is False
    assert status["subscriber_count"] == 0
    assert status["degraded"] is True
    assert status["last_success_at"] is None
    assert "tailer_recovered" not in _pending_status_reasons(hub)
    assert store.event_calls == 1


def test_stale_generation_cannot_erase_or_recover_current_sample_failure() -> None:
    """An overlapping stale success cannot overwrite current-generation health."""

    class _OverlapStore(_ObservedStore):
        def __init__(self) -> None:
            super().__init__()
            self.stale_sample_started = threading.Event()
            self.release_stale_sample = threading.Event()

        def get_events_after(
            self,
            after_id: int,
            types: list[str] | None = None,
            limit: int = 500,
        ) -> list[dict[str, Any]]:
            del after_id, types, limit
            self.event_calls += 1
            if threading.current_thread().name == "stale-eventhub-generation":
                self.stale_sample_started.set()
                if not self.release_stale_sample.wait(2.0):
                    raise TimeoutError("test did not release stale event sample")
                return []
            raise RuntimeError("current generation sample failed")

    class _ProbeSubscription:
        wants_heartbeats = False
        wants_sessions = False

        def _publish(self, frames: list[tuple[str, dict[str, Any]]]) -> None:
            del frames

    store = _OverlapStore()
    hub = EventHub(
        store,
        sessions_reader_factory=lambda: None,
        degraded_after_failures=1,
        restart_after_failures=50,
        max_tailer_restarts=0,
    )
    subscriber = _ProbeSubscription()
    stale_stop = threading.Event()
    current_stop = threading.Event()
    outcomes: dict[str, Any] = {}

    with hub._lock:
        hub._subs.add(subscriber)  # type: ignore[arg-type]
        hub._stop = stale_stop
        hub._generation = 1

    def _stale_tick() -> None:
        outcomes["stale"] = hub._tick(stale_stop, 1)

    stale_thread = threading.Thread(
        target=_stale_tick,
        name="stale-eventhub-generation",
    )
    stale_thread.start()
    assert store.stale_sample_started.wait(1.0)

    # The old tick is already inside its store query. Rotate ownership exactly
    # as a last-close/reconnect or bounded restart does.
    with hub._lock:
        stale_stop.set()
        hub._stop = current_stop
        hub._generation = 2

    outcomes["current"] = hub._tick(current_stop, 2)
    failed_status = hub.status()
    calls_before_stale_entry = store.event_calls

    store.release_stale_sample.set()
    stale_thread.join(1.0)
    assert not stale_thread.is_alive()

    # A stale tailer paused after _run's preliminary generation check must also
    # be rejected at _tick entry without querying or certifying recovery.
    assert hub._tick(stale_stop, 1) is _TickOutcome.IDLE
    assert store.event_calls == calls_before_stale_entry
    assert hub._note_tick_ok(stale_stop, 1) is False

    status = hub.status()
    assert outcomes["current"] is _TickOutcome.SAMPLED_FAILED
    assert outcomes["stale"] is _TickOutcome.IDLE
    assert failed_status["degraded"] is True
    assert "current generation sample failed" in str(failed_status["last_error"])
    assert status["degraded"] is True
    assert status["consecutive_failures"] == 1
    assert "current generation sample failed" in str(status["last_error"])
    assert status["last_success_at"] is None
    assert "tailer_recovered" not in _pending_status_reasons(hub)


@pytest.mark.parametrize(
    ("blocked_phase", "expected_stale_calls"),
    [
        ("heartbeat", ["heartbeat"]),
        ("factory", ["heartbeat", "factory"]),
        ("session", ["heartbeat", "factory", "session"]),
        ("event", ["heartbeat", "factory", "session", "event"]),
    ],
)
def test_stale_generation_stops_queries_and_discards_late_sample_state(
    blocked_phase: str,
    expected_stale_calls: list[str],
) -> None:
    """Every blocking sample boundary discards stale, non-empty results."""

    stale_thread_name = f"stale-{blocked_phase}-sample"
    blocked_call_started = threading.Event()
    release_blocked_call = threading.Event()
    calls: list[tuple[str, str]] = []

    def _is_stale_thread() -> bool:
        return threading.current_thread().name == stale_thread_name

    def _maybe_block(phase: str) -> None:
        if blocked_phase != phase or not _is_stale_thread():
            return
        blocked_call_started.set()
        if not release_blocked_call.wait(2.0):
            raise TimeoutError(f"test did not release stale {phase} sample")

    class _OverlapStore:
        def latest_event_id(self) -> int:
            return 0

        def get_heartbeats(self) -> list[dict[str, Any]]:
            calls.append((threading.current_thread().name, "heartbeat"))
            _maybe_block("heartbeat")
            worker_id = "STALE" if _is_stale_thread() else "CURRENT"
            return [{"worker_id": worker_id}]

        def get_events_after(
            self,
            after_id: int,
            types: list[str] | None = None,
            limit: int = 500,
        ) -> list[dict[str, Any]]:
            del after_id, types, limit
            calls.append((threading.current_thread().name, "event"))
            if _is_stale_thread():
                _maybe_block("event")
                return [{"id": 777, "type": "malicious.stale.event"}]
            raise RuntimeError("current generation sample failed")

    class _OverlapSessionsReader:
        def list_sessions(self, *, limit: int) -> list[dict[str, Any]]:
            del limit
            calls.append((threading.current_thread().name, "session"))
            _maybe_block("session")
            session_id = "STALE" if _is_stale_thread() else "CURRENT"
            return [{"id": session_id, "updated_at": "2026-07-25T00:00:00Z"}]

    class _ProbeSubscription:
        wants_heartbeats = True
        wants_sessions = True

        def _publish(self, frames: list[tuple[str, dict[str, Any]]]) -> None:
            del frames

    store = _OverlapStore()
    reader = _OverlapSessionsReader()

    def _sessions_reader_factory() -> _OverlapSessionsReader:
        calls.append((threading.current_thread().name, "factory"))
        _maybe_block("factory")
        return reader

    hub = EventHub(
        store,
        sessions_reader_factory=_sessions_reader_factory,
        degraded_after_failures=1,
        restart_after_failures=50,
        max_tailer_restarts=0,
    )
    subscriber = _ProbeSubscription()
    stale_stop = threading.Event()
    current_stop = threading.Event()
    outcomes: dict[str, _TickOutcome] = {}

    with hub._lock:
        hub._subs.add(subscriber)  # type: ignore[arg-type]
        hub._stop = stale_stop
        hub._generation = 1

    def _stale_tick() -> None:
        outcomes["stale"] = hub._tick(stale_stop, 1)

    stale_thread = threading.Thread(target=_stale_tick, name=stale_thread_name)
    stale_thread.start()
    assert blocked_call_started.wait(1.0)

    # Rotate ownership while the selected stale query is in flight. The current
    # generation then samples CURRENT cache data but fails its event sample.
    with hub._lock:
        stale_stop.set()
        hub._stop = current_stop
        hub._generation = 2
    outcomes["current"] = hub._tick(current_stop, 2)
    status_before_release = hub.status()
    heartbeats_before_release, sessions_before_release = hub.snapshot()

    release_blocked_call.set()
    stale_thread.join(1.0)
    assert not stale_thread.is_alive()

    stale_calls = [phase for thread, phase in calls if thread == stale_thread_name]
    heartbeats_after_release, sessions_after_release = hub.snapshot()
    with hub._lock:
        cursor_after_release = hub._cursor
        ring_after_release = list(hub._ring)

    assert outcomes["current"] is _TickOutcome.SAMPLED_FAILED
    assert outcomes["stale"] is _TickOutcome.IDLE
    assert stale_calls == expected_stale_calls
    assert heartbeats_before_release == [{"worker_id": "CURRENT"}]
    assert sessions_before_release == [{"id": "CURRENT", "updated_at": "2026-07-25T00:00:00Z"}]
    assert heartbeats_after_release == heartbeats_before_release
    assert sessions_after_release == sessions_before_release
    assert cursor_after_release == 0
    assert ring_after_release == []

    status = hub.status()
    assert status_before_release["degraded"] is True
    assert "current generation sample failed" in str(status_before_release["last_error"])
    assert status["degraded"] is True
    assert status["consecutive_failures"] == 1
    assert "current generation sample failed" in str(status["last_error"])
    assert status["last_success_at"] is None
    assert "tailer_recovered" not in _pending_status_reasons(hub)


def test_requested_session_reader_retries_exception_none_then_recovers() -> None:
    """Unavailable session sources are failures and retry without reconnect."""

    class _Store:
        def latest_event_id(self) -> int:
            return 0

        def get_heartbeats(self) -> list[dict[str, Any]]:
            return []

        def get_events_after(
            self,
            after_id: int,
            types: list[str] | None = None,
            limit: int = 500,
        ) -> list[dict[str, Any]]:
            del after_id, types, limit
            return []

    class _Reader:
        def list_sessions(self, *, limit: int) -> list[dict[str, Any]]:
            del limit
            return [{"id": "RECOVERED", "updated_at": "2026-07-25T00:00:00Z"}]

    class _ProbeSubscription:
        wants_heartbeats = False
        wants_sessions = True

        def _publish(self, frames: list[tuple[str, dict[str, Any]]]) -> None:
            del frames

    factory_calls = 0

    def _factory() -> Any | None:
        nonlocal factory_calls
        factory_calls += 1
        if factory_calls == 1:
            raise RuntimeError("sessions database unavailable")
        if factory_calls == 2:
            return None
        return _Reader()

    hub = EventHub(
        _Store(),
        sessions_reader_factory=_factory,
        degraded_after_failures=1,
        restart_after_failures=50,
        max_tailer_restarts=0,
    )
    stop = threading.Event()
    with hub._lock:
        hub._subs.add(_ProbeSubscription())  # type: ignore[arg-type]
        hub._stop = stop
        hub._generation = 1

    first = hub._tick(stop, 1)
    first_status = hub.status()
    with hub._lock:
        assert hub._sessions_reader is None
        assert hub._sessions_reader_resolved is False

    second = hub._tick(stop, 1)
    second_status = hub.status()
    with hub._lock:
        assert hub._sessions_reader is None
        assert hub._sessions_reader_resolved is False

    third = hub._tick(stop, 1)
    assert hub._note_tick_ok(stop, 1) is True
    recovered_status = hub.status()
    _, sessions = hub.snapshot()
    with hub._lock:
        recovered_frames = [
            payload
            for kind, payload in hub._pending_status_frames
            if kind == FRAME_DEGRADED and payload.get("reason") == "tailer_recovered"
        ]

    assert factory_calls == 3
    assert first is _TickOutcome.SAMPLED_FAILED
    assert second is _TickOutcome.SAMPLED_FAILED
    assert third is _TickOutcome.SAMPLED_OK
    assert first_status["degraded"] is True
    assert "sessions database unavailable" in str(first_status["last_error"])
    assert first_status["last_success_at"] is None
    assert second_status["degraded"] is True
    assert "sessions reader unavailable" in str(second_status["last_error"])
    assert second_status["last_success_at"] is None
    assert recovered_status["degraded"] is False
    assert recovered_status["last_error"] is None
    assert recovered_status["last_success_at"] is not None
    assert sessions == [{"id": "RECOVERED", "updated_at": "2026-07-25T00:00:00Z"}]
    assert recovered_frames
    assert recovered_frames[-1]["state"] == "ok"
    assert recovered_frames[-1]["last_error"] is None


def test_bounded_restart_re_resolves_session_reader() -> None:
    """A restart must not carry a persistently failing DAL handle forward."""

    class _Store:
        def latest_event_id(self) -> int:
            return 0

        def get_heartbeats(self) -> list[dict[str, Any]]:
            return []

        def get_events_after(
            self,
            after_id: int,
            types: list[str] | None = None,
            limit: int = 500,
        ) -> list[dict[str, Any]]:
            del after_id, types, limit
            return []

    class _ProbeSubscription:
        wants_heartbeats = False
        wants_sessions = True

        def _publish(self, frames: list[tuple[str, dict[str, Any]]]) -> None:
            del frames

    hub = EventHub(
        _Store(),
        restart_after_failures=2,
        max_tailer_restarts=1,
        restart_backoff_s=0,
    )
    stop = threading.Event()
    with hub._lock:
        hub._subs.add(_ProbeSubscription())  # type: ignore[arg-type]
        hub._stop = stop
        hub._generation = 1
        hub._sessions_reader = object()
        hub._sessions_reader_resolved = True
        hub._consecutive_failures = 2
        hub._degraded = True

    with patch.object(hub, "_start_locked") as start_replacement:
        hub._restart_tailer_async(stop, 1)

    with hub._lock:
        assert hub._sessions_reader is None
        assert hub._sessions_reader_resolved is False
        assert hub._tailer_restarts == 1
    assert stop.is_set()
    start_replacement.assert_called_once_with()


def test_degraded_hub_recovers_only_after_successful_sample_with_subscriber() -> None:
    store = _ObservedStore()
    hub = EventHub(
        store,
        sessions_reader_factory=lambda: None,
        poll_interval_s=0.01,
        degraded_after_failures=1,
        restart_after_failures=50,
        max_tailer_restarts=0,
    )
    _degrade_hub(hub)

    async def _run() -> tuple[dict[str, Any], list[str]]:
        sub = hub.subscribe(wants_heartbeats=False, wants_sessions=False)
        try:
            assert await asyncio.to_thread(store.event_sample_started.wait, 1.0)
            deadline = time.monotonic() + 1.0
            frames: list[tuple[str, dict[str, Any]]] = []
            while time.monotonic() < deadline:
                drained, _ = await sub.drain(0.05)
                frames.extend(drained)
                if not hub.is_degraded() and any(
                    kind == FRAME_DEGRADED and payload.get("reason") == "tailer_recovered"
                    for kind, payload in frames
                ):
                    break
            return (
                hub.status(),
                [str(payload.get("reason")) for kind, payload in frames if kind == FRAME_DEGRADED],
            )
        finally:
            sub.close()
            hub.stop(timeout=1.0)

    status, reasons = asyncio.run(_run())
    assert store.event_calls >= 1
    assert status["degraded"] is False
    assert status["last_success_at"] is not None
    assert "tailer_recovered" in reasons


def test_repeated_tail_failure_becomes_operator_visible_degraded() -> None:
    store = _FlakyStore(failures_remaining=100)
    hub = EventHub(
        store,
        sessions_reader_factory=lambda: None,
        poll_interval_s=0.01,
        degraded_after_failures=3,
        restart_after_failures=50,
        max_tailer_restarts=0,
    )

    async def _run() -> dict[str, Any]:
        sub = hub.subscribe(wants_heartbeats=False, wants_sessions=False)
        try:
            deadline = time.monotonic() + 3.0
            while time.monotonic() < deadline:
                if hub.is_degraded():
                    break
                await asyncio.sleep(0.02)
            status = hub.status()
            frames, _ = await sub.drain(0.2)
            degraded_frames = [f for f in frames if f[0] == FRAME_DEGRADED]
            return {"status": status, "degraded_frames": degraded_frames}
        finally:
            sub.close()
            hub.stop(timeout=1.0)

    result = asyncio.run(_run())
    status = result["status"]
    assert status["contract_version"] == 1
    assert status["degraded"] is True
    assert status["state"] == "degraded"
    assert status["consecutive_failures"] >= DEGRADED_AFTER_FAILURES
    assert status["last_error"]
    assert "store down" in str(status["last_error"])
    assert result["degraded_frames"], "expected FRAME_DEGRADED notice for operators"


def test_event_hub_recovers_after_store_returns() -> None:
    store = _FlakyStore(failures_remaining=4)
    hub = EventHub(
        store,
        sessions_reader_factory=lambda: None,
        poll_interval_s=0.01,
        degraded_after_failures=2,
        restart_after_failures=100,
        max_tailer_restarts=0,
    )

    async def _run() -> dict[str, Any]:
        sub = hub.subscribe(wants_heartbeats=False, wants_sessions=False)
        try:
            deadline = time.monotonic() + 3.0
            saw_degraded = False
            while time.monotonic() < deadline:
                if hub.is_degraded():
                    saw_degraded = True
                if saw_degraded and not hub.is_degraded():
                    break
                await asyncio.sleep(0.02)
            return hub.status()
        finally:
            sub.close()
            hub.stop(timeout=1.0)

    status = asyncio.run(_run())
    assert status["degraded"] is False
    assert status["state"] == "ok"
    assert status["consecutive_failures"] == 0
    assert status["last_success_at"] is not None


def test_bounded_tailer_restart_on_persistent_failure() -> None:
    store = _FlakyStore(failures_remaining=1000)
    hub = EventHub(
        store,
        sessions_reader_factory=lambda: None,
        poll_interval_s=0.01,
        degraded_after_failures=1,
        restart_after_failures=2,
        max_tailer_restarts=2,
        restart_backoff_s=0.01,
    )

    async def _run() -> dict[str, Any]:
        sub = hub.subscribe(wants_heartbeats=False, wants_sessions=False)
        try:
            deadline = time.monotonic() + 4.0
            while time.monotonic() < deadline:
                st = hub.status()
                if st["tailer_restarts"] >= 2 and st["tailer_alive"]:
                    break
                await asyncio.sleep(0.02)
            return hub.status()
        finally:
            sub.close()
            hub.stop(timeout=1.0)

    status = asyncio.run(_run())
    assert status["degraded"] is True
    assert status["tailer_restarts"] == 2
    assert status["max_tailer_restarts"] == 2
    assert status["tailer_alive"] is True
    # Cap reached — no infinite restart loop.
    assert status["tailer_restarts"] <= status["max_tailer_restarts"]


def test_explicit_stop_during_restart_backoff_cannot_resurrect_tailer() -> None:
    """Stop intent wins after restart selection and throughout its backoff."""
    store = _FlakyStore(failures_remaining=1000)
    hub = EventHub(
        store,
        sessions_reader_factory=lambda: None,
        poll_interval_s=0.005,
        degraded_after_failures=1,
        restart_after_failures=1,
        max_tailer_restarts=1,
        restart_backoff_s=0.25,
    )

    async def _run() -> tuple[dict[str, Any], dict[str, Any]]:
        sub = hub.subscribe(wants_heartbeats=False, wants_sessions=False)
        try:
            deadline = time.monotonic() + 2.0
            while time.monotonic() < deadline:
                if hub.status()["tailer_restarts"] == 1:
                    break
                await asyncio.sleep(0.002)
            assert hub.status()["tailer_restarts"] == 1, "tailer never entered backoff"

            # The restart has selected itself and cleared _thread before sleeping.
            # Keep the subscriber registered, exactly as process shutdown can,
            # and explicitly stop while the restart is outside the lock.
            hub.stop(timeout=None)
            stopped = hub.status()
            calls_after_stop = store.calls
            with hub._lock:
                generation_after_stop = hub._generation
                cursor_after_stop = hub._cursor
                ring_after_stop = list(hub._ring)
                pending_after_stop = list(hub._pending_status_frames)

            await asyncio.sleep(0.35)
            after_backoff = hub.status()
            with hub._lock:
                assert hub._generation == generation_after_stop
                assert hub._cursor == cursor_after_stop
                assert list(hub._ring) == ring_after_stop
                assert hub._pending_status_frames == pending_after_stop
            assert hub.snapshot() == ([], [])
            assert store.calls == calls_after_stop
            return stopped, after_backoff
        finally:
            sub.close()
            hub.stop(timeout=1.0)

    stopped, after_backoff = asyncio.run(_run())
    assert stopped["subscriber_count"] == 1
    assert stopped["tailer_alive"] is False
    assert after_backoff["subscriber_count"] == 1
    assert after_backoff["tailer_alive"] is False
    assert after_backoff["tailer_restarts"] == 1
    assert after_backoff["consecutive_failures"] == stopped["consecutive_failures"]
    assert after_backoff["last_error"] == stopped["last_error"]
    assert after_backoff["last_failure_at"] == stopped["last_failure_at"]
    assert after_backoff["last_success_at"] == stopped["last_success_at"]


def test_health_includes_event_hub_status(asgi_client: httpx.AsyncClient) -> None:
    response = asyncio.run(asgi_client.get("/api/health"))
    assert response.status_code == 200
    body = response.json()
    assert "event_hub" in body
    assert body["event_hub"]["contract_version"] == 1
    assert body["event_hub"]["state"] in {"ok", "degraded", "unknown"}
    assert "degraded" in body["event_hub"]


def test_health_route_fails_closed_when_store_is_unavailable(
    asgi_client: httpx.AsyncClient,
) -> None:
    """The real route never labels a dead DB/event source as healthy."""
    from omniagentos.api.deps import get_store

    class _DeadStore:
        def get_heartbeats(self) -> list[dict[str, Any]]:
            raise RuntimeError("sqlite unavailable")

    previous = app.dependency_overrides[get_store]
    app.dependency_overrides[get_store] = lambda: _DeadStore()
    try:
        response = asyncio.run(asgi_client.get("/api/health"))
    finally:
        app.dependency_overrides[get_store] = previous

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "degraded"
    assert body["db"] is False
    assert body["event_hub"]["state"] == "degraded"
    assert body["event_hub"]["degraded"] is True
    assert "sqlite unavailable" in str(body["event_hub"]["last_error"])


def test_sse_forwards_eventbus_status_on_degraded() -> None:
    """Connecting EventSource clients receive eventbus.status frames (H-41)."""
    store = FakeStore()

    class _BrokenHub:
        def subscribe(self, **kwargs: Any) -> Any:
            del kwargs

            class _Sub:
                def snapshot(self) -> tuple[list, list]:
                    return [], []

                def ring_replay(self, after_id: int) -> tuple[list, bool]:
                    return [], True

                async def drain(self, timeout: float) -> tuple[list, bool]:
                    del timeout
                    return (
                        [
                            (
                                FRAME_DEGRADED,
                                {
                                    "contract_version": 1,
                                    "type": "eventbus.status",
                                    "state": "degraded",
                                    "reason": "persistent_tail_failure",
                                    "degraded": True,
                                    "consecutive_failures": 3,
                                    "last_error": "event_sample: RuntimeError: store down",
                                    "tailer_restarts": 0,
                                    "max_tailer_restarts": 5,
                                    "ts": 1.0,
                                },
                            )
                        ],
                        False,
                    )

                def close(self) -> None:
                    return None

            return _Sub()

    async def _request() -> Request:
        async def receive() -> dict[str, str]:
            return {"type": "http.request"}

        return Request(
            {"type": "http", "method": "GET", "path": "/api/events", "headers": []},
            receive,
        )

    async def _collect() -> str:
        with patch("omniagentos.api.eventbus.get_event_hub", return_value=_BrokenHub()):
            response = await events(
                await _request(), store, after_id=0, types=None, last_event_id=None
            )
            chunks: list[str] = []
            async for raw in response.body_iterator:
                text = raw.decode() if isinstance(raw, bytes) else raw
                chunks.append(text)
                if "eventbus.status" in text:
                    break
            await response.body_iterator.aclose()
            return "".join(chunks)

    body = asyncio.run(_collect())
    assert "event: eventbus.status" in body
    assert "persistent_tail_failure" in body
    assert '"degraded": true' in body or '"degraded":true' in body


def test_reconnect_after_already_degraded_receives_pending_status() -> None:
    """A late subscriber must still receive the pending eventbus.status frame.

    When the hub enters degraded, FRAME_DEGRADED is queued on
    ``_pending_status_frames``. If the first subscriber disconnects before the
    next tick fans it out, the frame stays pending; a reconnecting subscriber
    must receive it (H-41 reconnect path).
    """
    store = _FlakyStore(failures_remaining=100)
    # Long poll interval so we can unsubscribe after degrade and before the
    # pending status frame is delivered on the next tick.
    hub = EventHub(
        store,
        sessions_reader_factory=lambda: None,
        poll_interval_s=1.0,
        degraded_after_failures=1,
        restart_after_failures=50,
        max_tailer_restarts=0,
    )

    async def _run() -> list[tuple[str, dict[str, Any]]]:
        sub1 = hub.subscribe(wants_heartbeats=False, wants_sessions=False)
        try:
            deadline = time.monotonic() + 3.0
            while time.monotonic() < deadline:
                if hub.is_degraded():
                    break
                await asyncio.sleep(0.01)
            assert hub.is_degraded(), "first subscriber should drive hub into degraded"
            # Do not drain sub1: leave the pending FRAME_DEGRADED for the next tick.
        finally:
            sub1.close()

        assert hub.is_degraded(), "degraded state must survive last-subscriber disconnect"

        sub2 = hub.subscribe(wants_heartbeats=False, wants_sessions=False)
        try:
            found: list[tuple[str, dict[str, Any]]] = []
            deadline = time.monotonic() + 3.0
            while time.monotonic() < deadline:
                frames, _ = await sub2.drain(0.2)
                found.extend(
                    (kind, payload)
                    for kind, payload in frames
                    if kind == FRAME_DEGRADED and payload.get("state") == "degraded"
                )
                if found:
                    break
            return found
        finally:
            sub2.close()
            hub.stop(timeout=1.0)

    found = asyncio.run(_run())
    assert found, "reconnect after already-degraded must receive pending eventbus.status frame"
    payload = found[0][1]
    assert payload["type"] == "eventbus.status"
    assert payload["state"] == "degraded"
    assert payload["degraded"] is True
    assert payload["reason"] == "persistent_tail_failure"


# ---------------------------------------------------------------------------
# H-42 — design spawn failure must not approve
# ---------------------------------------------------------------------------


class _AgentRequestStore(FakeStore):
    """Warning-clean fake for the agent-request slice exercised by L12.

    Importing the broad reliability-routes test module also imports FastAPI's
    deprecated ``TestClient`` compatibility shim. Keep this focused test's
    persistence seam local so warning-strict collection exercises only the
    production behavior under review.
    """

    def __init__(self) -> None:
        super().__init__()
        self.agent_requests: dict[str, dict[str, Any]] = {}
        self._next_agent_request = 0

    def create_agent_request(
        self,
        description: str,
        requested_by: str = "owner",
    ) -> str:
        self._next_agent_request += 1
        request_id = f"areq_l12_{self._next_agent_request}"
        self.agent_requests[request_id] = {
            "id": request_id,
            "description": description,
            "requested_by": requested_by,
            "status": AgentRequestStatus.PENDING.value,
            "design_json": {},
            "improvement_id": None,
            "agent_id": None,
            "created_at": "2026-07-25T00:00:00Z",
            "updated_at": "2026-07-25T00:00:00Z",
        }
        return request_id

    def get_agent_request(self, request_id: str) -> dict[str, Any] | None:
        return self.agent_requests.get(request_id)

    def update_agent_request_status(
        self,
        request_id: str,
        status: str,
        design_json: dict[str, Any] | None = None,
        improvement_id: str | None = None,
        agent_id: str | None = None,
    ) -> None:
        request = self.agent_requests.get(request_id)
        if request is None:
            return
        request.update(
            {
                "status": status,
                "design_json": design_json or {},
                "improvement_id": improvement_id,
                "agent_id": agent_id,
                "updated_at": "2026-07-25T00:00:01Z",
            }
        )


def test_agent_request_spawn_failure_fails_closed(
    asgi_client: httpx.AsyncClient,
    auth_headers: dict[str, str],
) -> None:
    """If Popen raises, status is failed, no approved event, HTTP 503."""
    from omniagentos.api.deps import get_store

    rel = _AgentRequestStore()
    request_id = rel.create_agent_request("Need a design agent")
    app.dependency_overrides[get_store] = lambda: rel
    try:
        with patch(
            "omniagentos.api.routes.org.subprocess.Popen",
            side_effect=OSError("exec format error"),
        ):
            response = asyncio.run(
                asgi_client.post(
                    f"/api/org/agent-requests/{request_id}/approve",
                    headers=auth_headers,
                    json={"decided_by": "owner"},
                )
            )
        assert response.status_code == 503
        body = response.json()
        assert body["error"]["code"] == "spawn_failed"
        assert body["error"]["detail"]["status"] == AgentRequestStatus.FAILED.value

        updated = rel.get_agent_request(request_id)
        assert updated is not None
        assert updated["status"] == AgentRequestStatus.FAILED.value
        design = updated.get("design_json") or {}
        assert design.get("phase") == "spawn"
        assert "spawn failed" in str(design.get("error", "")).lower()

        actions = [e.get("action") for e in rel.events]
        assert "agent_request.approved" not in actions
        assert "agent_request.spawn_failed" in actions
    finally:
        app.dependency_overrides.clear()


def test_agent_request_approve_still_succeeds_when_spawn_works(
    asgi_client: httpx.AsyncClient,
    auth_headers: dict[str, str],
) -> None:
    from omniagentos.api.deps import get_store

    rel = _AgentRequestStore()
    request_id = rel.create_agent_request("Need a design agent")
    app.dependency_overrides[get_store] = lambda: rel
    try:
        with patch("omniagentos.api.routes.org.subprocess.Popen") as popen:
            popen.return_value = object()
            response = asyncio.run(
                asgi_client.post(
                    f"/api/org/agent-requests/{request_id}/approve",
                    headers=auth_headers,
                    json={"decided_by": "owner"},
                )
            )
        assert response.status_code == 202
        assert response.json()["status"] == AgentRequestStatus.DESIGNING.value
        actions = [e.get("action") for e in rel.events]
        assert "agent_request.approved" in actions
        assert "agent_request.spawn_failed" not in actions
    finally:
        app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# L-01 — /api/projects/tree route uniqueness
# ---------------------------------------------------------------------------


def _iter_api_routes(routes: list[Any] | None = None) -> list[Any]:
    """Flatten FastAPI 0.139 `_IncludedRouter` nesting into concrete APIRoutes."""
    out: list[Any] = []
    for route in routes if routes is not None else app.routes:
        original = getattr(route, "original_router", None)
        if original is not None:
            out.extend(_iter_api_routes(list(original.routes)))
            continue
        nested = getattr(route, "routes", None)
        if nested is not None and not hasattr(route, "endpoint"):
            out.extend(_iter_api_routes(list(nested)))
            continue
        out.append(route)
    return out


def test_projects_tree_has_exactly_one_get_registration() -> None:
    get_routes = [
        route
        for route in _iter_api_routes()
        if getattr(route, "path", None) == "/api/projects/tree"
        and "GET" in (getattr(route, "methods", set()) or set())
    ]
    assert len(get_routes) == 1
    endpoint = getattr(get_routes[0], "endpoint", None)
    assert endpoint is not None
    assert endpoint.__module__ == "omniagentos.api.routes.hierarchy"
    assert endpoint.__name__ == "project_tree"

    # OpenAPI also exposes exactly one GET (no duplicate method shadowing).
    schema = app.openapi()
    assert list(schema["paths"]["/api/projects/tree"].keys()) == ["get"]

    # Flat projects router must not re-register /tree (L-01 ownership).
    from omniagentos.api.routes import projects as projects_routes

    project_tree_paths = [
        getattr(r, "path", None)
        for r in projects_routes.router.routes
        if getattr(r, "path", None) in {"/tree", "/api/projects/tree"}
    ]
    assert project_tree_paths == []


def test_projects_tree_is_owned_not_project_id_param() -> None:
    """Literal /tree must not be claimed by /{project_id} on the flat router."""
    from omniagentos.api.routes import hierarchy, projects

    hierarchy_paths = {getattr(r, "path", None) for r in hierarchy.router.routes}
    assert "/api/projects/tree" in hierarchy_paths

    flat_paths = {getattr(r, "path", None) for r in projects.router.routes}
    assert "/tree" not in flat_paths
    assert "/api/projects/tree" not in flat_paths
    # Detail route remains; Starlette prefers the literal when both exist.
    assert any(
        getattr(r, "path", None) == "/api/projects/{project_id}" for r in projects.router.routes
    )


# ---------------------------------------------------------------------------
# L-05 — revenue undercount warning
# ---------------------------------------------------------------------------


def test_undercount_warning_surfaces_when_truncated_with_zero_failures() -> None:
    facts = [
        {
            "day": "2026-07-10",
            "vertical": "AcmeUni",
            "source": "stripe",
            "campaign_id": None,
            "campaign_name": None,
            "revenue_usd": 100.0,
            "ad_spend_usd": 0.0,
            "roas": None,
            "payment_failures": 0,
            "refunds_usd": 0.0,
            "meta": {"truncated": True, "charges": 1000},
        }
    ]
    report = build_revenue_report("2026-07-10", facts, generated_at="x")
    notes = report["data_quality"]
    undercount = [
        n
        for n in notes
        if n["level"] == "warn" and "UNDERCOUNT" in n["message"] and "AcmeUni" in n["message"]
    ]
    assert undercount, f"expected UNDERCOUNT note, got {notes}"
    assert "2026-07-10" in undercount[0]["message"]


def test_undercount_warning_absent_when_not_truncated() -> None:
    facts = [
        {
            "day": "2026-07-10",
            "vertical": "AcmeUni",
            "source": "stripe",
            "campaign_id": None,
            "campaign_name": None,
            "revenue_usd": 100.0,
            "ad_spend_usd": 0.0,
            "roas": None,
            "payment_failures": 2,
            "refunds_usd": 0.0,
            "meta": {"charges": 10},
        }
    ]
    report = build_revenue_report("2026-07-10", facts, generated_at="x")
    assert not any("UNDERCOUNT" in n["message"] for n in report["data_quality"])
    assert any("failed payment" in n["message"] for n in report["data_quality"])


def test_undercount_and_failures_both_surface_when_both_apply() -> None:
    facts = [
        {
            "day": "2026-07-10",
            "vertical": "Initech",
            "source": "stripe",
            "campaign_id": None,
            "campaign_name": None,
            "revenue_usd": 50.0,
            "ad_spend_usd": 0.0,
            "roas": None,
            "payment_failures": 12,
            "refunds_usd": 0.0,
            "meta": {"truncated": True},
        }
    ]
    report = build_revenue_report("2026-07-10", facts, generated_at="x")
    messages = [n["message"] for n in report["data_quality"]]
    assert any("UNDERCOUNT" in m for m in messages)
    assert any("12 failed payment" in m for m in messages)


def test_versioned_fixture_contract_is_published() -> None:
    fixture = _load_fixture()
    assert fixture["contract_version"] == 1
    assert set(fixture["findings"]) == {"C-08", "H-41", "H-42", "L-01", "L-05"}
    assert fixture["origins"]["default_dashboard_port"] == 3001
    assert fixture["event_hub"]["sse_event"] == "eventbus.status"
    assert fixture["organization"]["spawn_failure"]["approved_event_emitted"] is False
    assert fixture["routes"]["projects_tree"]["unique_get_registrations"] == 1
