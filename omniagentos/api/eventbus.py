"""Process-wide in-process fan-out hub for the SSE event stream (T1.5).

Before this module, ``GET /api/events`` was ``O(connections)``: every connected
dashboard ran its OWN 4 Hz poll loop *on the asyncio event-loop thread*, and each
tick issued three SYNCHRONOUS sqlite calls — ``store.get_heartbeats()``,
``sessions_dal.list_sessions(limit=200)`` and ``store.get_events_after(...)``.
Measured at ~19.3 CPU-ms of loop time per wall-second per connection, so roughly
fifty dashboards saturated the single event-loop thread (fewer as the sessions
table grew), and every one of those sqlite calls blocked *all* other requests.

The hub turns ``N x 3 x 4Hz`` into ``1 x 3 x 4Hz``:

* ONE tailer runs in a **dedicated OS thread** — never the event loop — so the
  loop never blocks on sqlite for the stream.
* The tailer samples heartbeats, sessions and new events ONCE per tick and fans
  the result out to every subscriber's :class:`asyncio.Queue` via
  ``loop.call_soon_threadsafe``, which is the only thread-safe way to touch a
  queue owned by the loop.
* A ring buffer of the last :data:`RING_CAPACITY` events lets a subscriber that
  briefly fell behind catch up from memory instead of re-querying sqlite.

Non-replayable by design (``omniagentos/contracts.py`` ``Events.SSE_ONLY``):
``worker.heartbeat`` and ``session.updated`` are SSE-only synthesized types that
are NEVER written to the events table, precisely so a heartbeat flood cannot
evict real events from the replay window. They therefore cannot be replayed from
a cursor — a connecting client is caught up with :meth:`EventHub.snapshot`
instead, which is served from the tailer's cached sample (no per-connection
query).

Deliberately NOT Postgres ``LISTEN/NOTIFY``: the API is a single uvicorn process
(see the Makefile), so an in-process hub is sufficient and strictly simpler.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
import weakref
from collections import deque
from enum import Enum
from typing import Any, Protocol

LOG = logging.getLogger(__name__)

__all__ = [
    "DEGRADED_AFTER_FAILURES",
    "FRAME_DEGRADED",
    "FRAME_EVENT",
    "FRAME_HEARTBEAT",
    "FRAME_SESSION",
    "MAX_TAILER_RESTARTS",
    "RESTART_AFTER_FAILURES",
    "EventHub",
    "Subscription",
    "get_event_hub",
    "shutdown_event_hubs",
]

#: How many recent event rows the hub keeps for in-memory lag recovery.
RING_CAPACITY = 2000
#: Tailer cadence. Matches the 4 Hz the per-connection loops used to poll at, so
#: end-to-end latency is unchanged — only the *number* of pollers drops to one.
POLL_INTERVAL_S = 0.25
#: A worker's heartbeat is re-announced at most this often (was per-connection).
HEARTBEAT_MIN_INTERVAL_S = 15.0
#: Mirrors the old per-connection ``list_sessions(limit=200)``.
SESSION_SAMPLE_LIMIT = 200
#: Mirrors the old per-connection ``get_events_after(..., limit=500)``.
EVENT_BATCH_LIMIT = 500
#: Per-subscriber backlog before it is declared lagged and resynced from sqlite.
SUBSCRIBER_QUEUE_MAXSIZE = 2048
#: Consecutive tick/sample failures before the hub is operator-visible degraded.
DEGRADED_AFTER_FAILURES = 3
#: Consecutive failures before a bounded tailer restart is attempted.
RESTART_AFTER_FAILURES = 8
#: Hard cap on automatic tailer restarts per hub lifetime (bounded recovery).
MAX_TAILER_RESTARTS = 5
#: Floor for restart backoff after a degraded-window restart (seconds).
RESTART_BACKOFF_S = 0.5

FRAME_EVENT = "event"
FRAME_HEARTBEAT = "heartbeat"
FRAME_SESSION = "session"
#: Operator-visible degraded/recovery notice fanned out to SSE subscribers.
FRAME_DEGRADED = "degraded"

#: ``(kind, row)`` where ``kind`` is one of the ``FRAME_*`` constants and ``row``
#: is the RAW store row. Formatting stays in ``routes/control.py`` so the SSE
#: framing has exactly one owner.
Frame = tuple[str, dict[str, Any]]


class _TickOutcome(Enum):
    """Whether one tailer iteration produced authoritative health evidence."""

    IDLE = "idle"
    SAMPLED_OK = "sampled_ok"
    SAMPLED_FAILED = "sampled_failed"


class EventSource(Protocol):
    """The slice of the frozen ``Store`` contract the tailer needs."""

    def get_events_after(
        self, after_id: int, types: list[str] | None = None, limit: int = 500
    ) -> list[dict[str, Any]]: ...

    def latest_event_id(self) -> int: ...

    def get_heartbeats(self) -> list[dict[str, Any]]: ...


def _default_sessions_reader() -> Any | None:
    """Return the shared process-lifetime sessions reader, or ``None``.

    Resolved through ``routes.control._open_sessions_reader`` — a LATE attribute
    lookup, deliberately — so that seam stays the single place the reader is
    obtained (and stays monkeypatchable, as ``tests/sessions/api`` relies on).

    T-OPS-006: it hands back the SAME long-lived DAL the sessions routes use (one
    sqlite connection, internally serialized with an RLock and opened with
    ``check_same_thread=False``), so calling it from the tailer thread is safe.
    It is process-lifetime and must NEVER be closed by us.
    """
    try:
        from omniagentos.api.routes import control

        return control._open_sessions_reader()
    except Exception:  # pragma: no cover - EventHub surfaces requested-source failure
        LOG.debug("sessions reader unavailable for the event hub", exc_info=True)
        return None


class Subscription:
    """One SSE connection's view of the hub.

    Owned by the event loop it was created on. The tailer thread only ever calls
    :meth:`_publish`, which hands off with ``call_soon_threadsafe``; every other
    attribute is touched exclusively from that loop.
    """

    __slots__ = (
        "_closed",
        "_hub",
        "_lagged",
        "_loop",
        "_queue",
        "wants_heartbeats",
        "wants_sessions",
    )

    def __init__(
        self,
        hub: EventHub,
        loop: asyncio.AbstractEventLoop,
        *,
        wants_heartbeats: bool,
        wants_sessions: bool,
    ) -> None:
        self._hub = hub
        self._loop = loop
        self._queue: asyncio.Queue[Frame] = asyncio.Queue(maxsize=SUBSCRIBER_QUEUE_MAXSIZE)
        self._lagged = False
        self._closed = False
        self.wants_heartbeats = wants_heartbeats
        self.wants_sessions = wants_sessions

    # -- tailer-thread side -------------------------------------------------
    def _publish(self, frames: list[Frame]) -> None:
        """Hand a tick's frames to the owning loop (called from the tailer)."""
        if self._closed:
            return
        try:
            self._loop.call_soon_threadsafe(self._deliver, frames)
        except RuntimeError:
            # The loop is closed or shutting down (common in tests): the
            # subscriber is gone, so stop trying to reach it.
            self._closed = True

    # -- loop-thread side ---------------------------------------------------
    def _deliver(self, frames: list[Frame]) -> None:
        if self._closed:
            return
        for frame in frames:
            try:
                self._queue.put_nowait(frame)
            except asyncio.QueueFull:
                # A consumer this far behind is resynced from the durable log
                # rather than served a silently truncated stream.
                self._lagged = True
                return

    async def drain(self, timeout: float) -> tuple[list[Frame], bool]:
        """Wait up to ``timeout`` for frames, then return everything buffered.

        Returns ``(frames, lagged)``. When ``lagged`` is set the queue overflowed,
        so buffered EVENT frames are dropped (the caller refetches them from the
        durable log by cursor); heartbeat/session frames are kept because they
        are not replayable. ``timeout <= 0`` polls without waiting.
        """
        frames: list[Frame] = []
        if timeout > 0:
            try:
                frames.append(await asyncio.wait_for(self._queue.get(), timeout))
            except TimeoutError:
                pass
        while True:
            try:
                frames.append(self._queue.get_nowait())
            except asyncio.QueueEmpty:
                break
        lagged = self._lagged
        if lagged:
            self._lagged = False
            frames = [frame for frame in frames if frame[0] != FRAME_EVENT]
        return frames, lagged

    def snapshot(self) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """Current ``(heartbeat_rows, session_rows)`` for a connect-time catch-up."""
        return self._hub.snapshot()

    def ring_replay(self, after_id: int) -> tuple[list[dict[str, Any]], bool]:
        """Catch up from the hub's in-memory ring; ``False`` if it cannot."""
        return self._hub.ring_replay(after_id)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._hub.unsubscribe(self)


class EventHub:
    """One tailer thread fanning the event/heartbeat/session stream out to N SSE connections."""

    def __init__(
        self,
        store: EventSource,
        *,
        sessions_reader_factory: Any = _default_sessions_reader,
        poll_interval_s: float = POLL_INTERVAL_S,
        degraded_after_failures: int = DEGRADED_AFTER_FAILURES,
        restart_after_failures: int = RESTART_AFTER_FAILURES,
        max_tailer_restarts: int = MAX_TAILER_RESTARTS,
        restart_backoff_s: float = RESTART_BACKOFF_S,
    ) -> None:
        self._store = store
        self._sessions_reader_factory = sessions_reader_factory
        self._poll_interval_s = poll_interval_s
        self._degraded_after_failures = max(1, int(degraded_after_failures))
        self._restart_after_failures = max(
            self._degraded_after_failures, int(restart_after_failures)
        )
        self._max_tailer_restarts = max(0, int(max_tailer_restarts))
        self._restart_backoff_s = max(0.0, float(restart_backoff_s))
        self._lock = threading.Lock()
        self._subs: set[Subscription] = set()
        self._ring: deque[dict[str, Any]] = deque(maxlen=RING_CAPACITY)
        self._cursor = 0
        self._primed = False
        self._thread: threading.Thread | None = None
        self._stop: threading.Event | None = None
        self._sessions_reader: Any | None = None
        self._sessions_reader_resolved = False
        self._sessions_active = False
        self._heartbeats_active = False
        # Snapshot caches. Rebuilt by REPLACING the dict each tick (never mutated
        # in place) so a reader on the loop thread always sees a consistent map
        # without taking the lock.
        self._heartbeats: dict[str, dict[str, Any]] = {}
        self._heartbeat_sent_at: dict[str, float] = {}
        self._sessions: dict[str, dict[str, Any]] = {}
        self._session_stamps: dict[str, str] = {}
        # Operator-visible degraded state (H-41). Persistent tick/sample failure
        # is no longer debug-only: after DEGRADED_AFTER_FAILURES consecutive
        # failures the hub exposes degraded=True and fans a FRAME_DEGRADED notice
        # to subscribers. Bounded recovery restarts the tailer a capped number of
        # times; a successful tick clears degraded.
        self._consecutive_failures = 0
        self._degraded = False
        self._last_error: str | None = None
        self._last_failure_at: float | None = None
        self._last_success_at: float | None = None
        self._tailer_restarts = 0
        self._pending_status_frames: list[Frame] = []
        self._generation = 0

    # -- subscriber management ---------------------------------------------
    def subscribe(
        self, *, wants_heartbeats: bool = True, wants_sessions: bool = True
    ) -> Subscription:
        """Register the calling coroutine's connection and start the tailer."""
        loop = asyncio.get_running_loop()
        subscription = Subscription(
            self, loop, wants_heartbeats=wants_heartbeats, wants_sessions=wants_sessions
        )
        with self._lock:
            self._subs.add(subscription)
            self._prime_locked()
            self._start_locked()
        return subscription

    def unsubscribe(self, subscription: Subscription) -> None:
        """Drop a connection; stop the tailer once the last one goes away."""
        with self._lock:
            self._subs.discard(subscription)
            if not self._subs:
                self._stop_locked()

    def subscriber_count(self) -> int:
        with self._lock:
            return len(self._subs)

    def is_degraded(self) -> bool:
        """True when the tailer has failed persistently (operator-visible)."""
        with self._lock:
            return self._degraded

    def status(self) -> dict[str, Any]:
        """Operator-visible hub health snapshot (versioned for L13 consumers)."""
        with self._lock:
            return {
                "contract_version": 1,
                "state": "degraded" if self._degraded else "ok",
                "degraded": self._degraded,
                "consecutive_failures": self._consecutive_failures,
                "last_error": self._last_error,
                "last_failure_at": self._last_failure_at,
                "last_success_at": self._last_success_at,
                "tailer_alive": bool(self._thread is not None and self._thread.is_alive()),
                "tailer_restarts": self._tailer_restarts,
                "max_tailer_restarts": self._max_tailer_restarts,
                "subscriber_count": len(self._subs),
                "degraded_after_failures": self._degraded_after_failures,
                "restart_after_failures": self._restart_after_failures,
            }

    # -- snapshot / replay --------------------------------------------------
    def snapshot(self) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """Return ``(heartbeat_rows, session_rows)`` as of the last tick.

        This is what a NEW connection is caught up with: ``worker.heartbeat`` and
        ``session.updated`` are SSE-only and never persisted, so they cannot be
        replayed from the cursor. Served from the tailer's cached sample, so a
        connect costs no extra query.
        """
        # Sampling commits both maps under this lock. Reading them under the same
        # lock prevents a reconnect from observing half of a coherent tick.
        with self._lock:
            return list(self._heartbeats.values()), list(self._sessions.values())

    def ring_replay(self, after_id: int) -> tuple[list[dict[str, Any]], bool]:
        """Return buffered events with ``id > after_id``; ``False`` if the ring missed some."""
        with self._lock:
            ring = list(self._ring)
        if not ring:
            return [], True
        if after_id + 1 < int(ring[0]["id"]):
            return [], False
        return [row for row in ring if int(row["id"]) > after_id], True

    # -- lifecycle ----------------------------------------------------------
    def start(self) -> None:
        """Start the tailer (idempotent) without querying until a subscriber exists."""
        with self._lock:
            self._start_locked()

    def stop(self, timeout: float | None = None) -> None:
        """Stop the tailer (idempotent). ``timeout`` joins the thread if given."""
        with self._lock:
            thread = self._thread
            self._stop_locked()
        if timeout is not None and thread is not None and thread.is_alive():
            thread.join(timeout)

    def _start_locked(self) -> None:
        if self._thread is not None:
            return
        stop = threading.Event()
        self._generation += 1
        generation = self._generation
        thread = threading.Thread(
            target=self._run,
            args=(stop, generation),
            name="api-eventhub-tailer",
            daemon=True,
        )
        self._stop = stop
        self._thread = thread
        thread.start()

    def _stop_locked(self) -> None:
        # Treat every explicit/last-subscriber stop as a new lifecycle epoch.
        # A bounded restart may already have passed its failure predicate or be
        # sleeping outside the lock. Advancing the generation is the durable
        # stop intent that prevents that in-flight restart from starting a new
        # tailer after this method returns.
        self._generation += 1
        if self._stop is not None:
            self._stop.set()
        # Deliberately not joined here: unsubscribe runs on the event loop and
        # must never block it. The thread checks its stop flag every tick (and
        # its `wait` returns immediately once set), and the generation guard in
        # `_run` keeps a winding-down tailer from publishing after a restart.
        self._stop = None
        self._thread = None
        self._primed = False
        self._ring.clear()
        self._sessions_active = False
        self._heartbeats_active = False
        self._session_stamps = {}
        self._sessions = {}
        self._heartbeat_sent_at = {}
        self._heartbeats = {}
        # Reset reader resolution with the rest of the generation state. Active
        # requested-source failures retry every tick; this reset additionally
        # guarantees the next 0->1 subscriber transition never inherits a stale
        # handle from the disconnected generation.
        self._sessions_reader_resolved = False
        self._sessions_reader = None

    def _prime_locked(self) -> None:
        """Seed the cursor and the heartbeat snapshot before the first tick.

        Runs on the loop thread, but only on the 0 -> 1 subscriber transition (a
        connect-time cost, never a 4 Hz one), and only against the store already
        in hand. Sessions are deliberately NOT sampled here: that would open the
        sessions DAL on the event-loop thread. The tailer's first tick (<=250ms
        later) publishes every session as changed, exactly as the old first loop
        iteration did.
        """
        if self._primed:
            return
        self._primed = True
        try:
            self._cursor = int(self._store.latest_event_id())
        except Exception:
            LOG.debug("event hub could not read latest_event_id", exc_info=True)
            self._cursor = 0
        now = time.monotonic()
        heartbeats: dict[str, dict[str, Any]] = {}
        sent_at: dict[str, float] = {}
        try:
            beats = self._store.get_heartbeats()
        except Exception:
            LOG.debug("event hub could not read heartbeats", exc_info=True)
            beats = []
        for beat in beats:
            worker_id = str(beat.get("worker_id", ""))
            if not worker_id:
                continue
            heartbeats[worker_id] = dict(beat)
            sent_at[worker_id] = now
        self._heartbeats = heartbeats
        self._heartbeat_sent_at = sent_at
        # The cache is populated, so the "nobody wants heartbeats any more" branch
        # in _sample_heartbeats is the one that gets to drop it.
        self._heartbeats_active = True

    # -- tailer -------------------------------------------------------------
    def _run(self, stop: threading.Event, generation: int) -> None:
        while not stop.is_set():
            # A newer generation owns the hub; exit without racing its tick.
            if generation != self._generation:
                return
            started = time.monotonic()
            try:
                outcome = self._tick(stop, generation)
                if outcome is _TickOutcome.SAMPLED_OK:
                    self._note_tick_ok(stop, generation)
            except Exception as exc:  # pragma: no cover - the tailer must never die
                self._note_tick_failure(
                    exc,
                    where="tick",
                    stop=stop,
                    generation=generation,
                )
            # Bounded recovery: restart this tailer after a streak of failures.
            if self._should_restart_tailer(stop, generation):
                self._restart_tailer_async(stop, generation)
                return
            elapsed = time.monotonic() - started
            stop.wait(max(0.0, self._poll_interval_s - elapsed))

    def _tick(self, stop: threading.Event, generation: int) -> _TickOutcome:
        """Run one tailer iteration without treating idle as health evidence.

        A zero-subscriber iteration performs no queries and returns ``IDLE``.
        Only a complete configured sample that still has an active subscriber
        can return ``SAMPLED_OK`` and clear degraded state. Blocking reads are
        staged locally. A single ownership fence commits the coherent tick, so
        an old generation can neither mutate caches nor advance the event cursor.
        """
        sample_errors: list[tuple[str, BaseException]] = []
        with self._lock:
            if not self._tick_owned_locked(stop, generation):
                return _TickOutcome.IDLE
            subs = list(self._subs)
            pending_status = list(self._pending_status_frames)
            prior_heartbeats = dict(self._heartbeats)
            prior_heartbeat_sent_at = dict(self._heartbeat_sent_at)
            prior_sessions = dict(self._sessions)
            prior_session_stamps = dict(self._session_stamps)
            event_cursor = self._cursor
            sessions_reader = self._sessions_reader
            sessions_reader_resolved = self._sessions_reader_resolved
        wants_heartbeats = any(sub.wants_heartbeats for sub in subs)
        wants_sessions = any(sub.wants_sessions for sub in subs)

        (
            heartbeats_active,
            sampled_heartbeats,
            sampled_heartbeat_sent_at,
            heartbeat_frames,
        ) = self._sample_heartbeats(
            wants_heartbeats,
            sample_errors,
            prior_heartbeats=prior_heartbeats,
            prior_sent_at=prior_heartbeat_sent_at,
        )
        # A blocking heartbeat read may have overlapped last-close/reconnect or
        # bounded restart. Do not start another query once this tick is stale.
        if not self._tick_owned(stop, generation):
            return _TickOutcome.IDLE

        if wants_sessions and not sessions_reader_resolved:
            factory = self._sessions_reader_factory
            try:
                sessions_reader = factory() if factory is not None else None
            except Exception as reader_exc:
                self._note_sample_failure(
                    sample_errors,
                    "sessions_reader",
                    reader_exc,
                )
                sessions_reader = None
            if sessions_reader is None:
                # Unavailability is not a resolved source. Keep retrying on the
                # next tick; a connected dashboard must not need a full
                # disconnect/reconnect to recover session.updated.
                sessions_reader_resolved = False
            else:
                sessions_reader_resolved = True
            # Reader creation may itself open a DAL. Treat it as a blocking
            # boundary and never follow it with a sample for a stale generation.
            if not self._tick_owned(stop, generation):
                return _TickOutcome.IDLE

        (
            sessions_active,
            sampled_sessions,
            sampled_session_stamps,
            session_frames,
            sessions_reader_ok,
        ) = self._sample_sessions(
            wants_sessions,
            sessions_reader,
            sample_errors,
            prior_sessions=prior_sessions,
            prior_stamps=prior_session_stamps,
        )
        if wants_sessions and not sessions_reader_ok:
            # A persistent reader or factory failure participates in the same
            # degraded/restart policy as heartbeat/event failures. Drop the
            # failed handle so the next tick (and any bounded restart) retries
            # the factory instead of certifying a permanently stalled stream.
            sessions_reader = None
            sessions_reader_resolved = False
        # As above, stop before the event query if session sampling invalidated.
        if not self._tick_owned(stop, generation):
            return _TickOutcome.IDLE

        sampled_cursor, sampled_event_rows, event_frames = self._sample_events(
            sample_errors,
            after_id=event_cursor,
        )

        # The last subscriber may close while a store call is in flight.
        # Completion after that boundary is neither recovery nor a new failure:
        # the work is no longer an authoritative active-subscriber sample. All
        # sample state is committed together while this ownership fence is held.
        with self._lock:
            if not self._tick_owned_locked(stop, generation):
                return _TickOutcome.IDLE
            self._heartbeats_active = heartbeats_active
            self._heartbeats = sampled_heartbeats
            self._heartbeat_sent_at = sampled_heartbeat_sent_at
            self._sessions_active = sessions_active
            self._sessions = sampled_sessions
            self._session_stamps = sampled_session_stamps
            self._sessions_reader = sessions_reader
            self._sessions_reader_resolved = sessions_reader_resolved
            self._cursor = sampled_cursor
            self._ring.extend(sampled_event_rows)
            # Consume only the status frames this tick observed. Frames appended
            # while sampling remain pending for the following tick.
            for frame in pending_status:
                try:
                    self._pending_status_frames.remove(frame)
                except ValueError:
                    pass
            current_subs = list(self._subs)

        frames: list[Frame] = [
            *pending_status,
            *heartbeat_frames,
            *session_frames,
            *event_frames,
        ]

        if frames:
            for sub in current_subs:
                sub._publish(frames)
        if sample_errors:
            where, exc = sample_errors[0]
            if self._note_tick_failure(
                exc,
                where=where,
                stop=stop,
                generation=generation,
            ):
                return _TickOutcome.SAMPLED_FAILED
            return _TickOutcome.IDLE
        return _TickOutcome.SAMPLED_OK

    def _tick_owned_locked(self, stop: threading.Event, generation: int) -> bool:
        """Return whether this tick still owns a live subscribed generation."""
        return (
            bool(self._subs)
            and generation == self._generation
            and not stop.is_set()
            and self._stop is stop
        )

    def _tick_owned(self, stop: threading.Event, generation: int) -> bool:
        with self._lock:
            return self._tick_owned_locked(stop, generation)

    def _note_tick_ok(self, stop: threading.Event, generation: int) -> bool:
        """Commit recovery only while the sampled generation still has a listener."""
        with self._lock:
            if (
                generation != self._generation
                or stop.is_set()
                or self._stop is not stop
                or not self._subs
            ):
                return False
            self._consecutive_failures = 0
            self._last_success_at = time.time()
            was_degraded = self._degraded
            self._degraded = False
            # State=ok means the current configured sample succeeded. Clear the
            # sticky error before constructing the recovery frame so both
            # status() and the versioned SSE payload tell the same truth.
            self._last_error = None
            if was_degraded:
                frame = self._status_frame_locked(
                    state="ok",
                    reason="tailer_recovered",
                )
                self._pending_status_frames.append(frame)
                LOG.warning("event hub recovered from degraded state")
            return True

    def _note_tick_failure(
        self,
        exc: BaseException,
        *,
        where: str,
        stop: threading.Event | None = None,
        generation: int | None = None,
    ) -> bool:
        message = f"{where}: {type(exc).__name__}: {exc}"
        entered_degraded = False
        with self._lock:
            if (stop is None) != (generation is None):
                raise ValueError("stop and generation must be provided together")
            if stop is not None and (
                generation != self._generation
                or stop.is_set()
                or self._stop is not stop
                or not self._subs
            ):
                return False
            self._consecutive_failures += 1
            self._last_error = message
            self._last_failure_at = time.time()
            if not self._degraded and self._consecutive_failures >= self._degraded_after_failures:
                self._degraded = True
                entered_degraded = True
                self._pending_status_frames.append(
                    self._status_frame_locked(
                        state="degraded",
                        reason="persistent_tail_failure",
                    )
                )
        if entered_degraded:
            LOG.warning(
                "event hub degraded after %s consecutive failures: %s",
                self._consecutive_failures,
                message,
                exc_info=True,
            )
        else:
            LOG.warning("event hub %s failed: %s", where, message, exc_info=True)
        return True

    @staticmethod
    def _note_sample_failure(
        sample_errors: list[tuple[str, BaseException]],
        where: str,
        exc: BaseException,
    ) -> None:
        """Record a swallowed sample error; counted once per tick at the end."""
        # Keep the first error of the tick for the operator-visible message.
        if not sample_errors:
            sample_errors.append((where, exc))
        LOG.debug("event hub %s failed: %s", where, exc, exc_info=True)

    def _status_frame_locked(self, *, state: str, reason: str) -> Frame:
        payload = {
            "contract_version": 1,
            "type": "eventbus.status",
            "state": state,
            "reason": reason,
            "degraded": state == "degraded",
            "consecutive_failures": self._consecutive_failures,
            "last_error": self._last_error,
            "tailer_restarts": self._tailer_restarts,
            "max_tailer_restarts": self._max_tailer_restarts,
            "ts": time.time(),
        }
        return (FRAME_DEGRADED, payload)

    def _should_restart_tailer(self, stop: threading.Event, generation: int) -> bool:
        with self._lock:
            if generation != self._generation or stop.is_set() or self._stop is not stop:
                return False
            if self._consecutive_failures < self._restart_after_failures:
                return False
            if self._tailer_restarts >= self._max_tailer_restarts:
                return False
            if not self._subs:
                return False
            return True

    def _restart_tailer_async(self, stop: threading.Event, generation: int) -> None:
        """Stop this generation and start a replacement (bounded recovery)."""
        with self._lock:
            if generation != self._generation or stop.is_set() or self._stop is not stop:
                return
            if self._tailer_restarts >= self._max_tailer_restarts:
                return
            self._tailer_restarts += 1
            restart_n = self._tailer_restarts
            # Give the new generation a fresh failure window so we do not
            # immediately re-trip the restart threshold on the first tick.
            self._consecutive_failures = 0
            # Signal the current stop event; _start_locked will create a new one.
            if self._stop is not None:
                self._stop.set()
            self._stop = None
            self._thread = None
            # Re-resolve the shared process-lifetime DAL after a persistent
            # session sampling failure. Keeping a failed handle across the
            # bounded restart would make the restart cosmetic.
            self._sessions_reader = None
            self._sessions_reader_resolved = False
            # Keep degraded true across the restart; success will clear it.
            self._pending_status_frames.append(
                self._status_frame_locked(
                    state="degraded",
                    reason=f"tailer_restart_{restart_n}",
                )
            )
            LOG.warning(
                "event hub restarting tailer (%s/%s) after persistent failure",
                restart_n,
                self._max_tailer_restarts,
            )
        # Backoff outside the lock so a hard-failing store cannot pin the hub.
        if self._restart_backoff_s > 0:
            time.sleep(self._restart_backoff_s)
        with self._lock:
            # `generation` is also the restart lease. An explicit stop advances
            # it while holding this lock, so stop intent wins even when it
            # arrives after the restart decision or during this backoff.
            if generation == self._generation and self._subs and self._thread is None:
                self._start_locked()

    def _sample_heartbeats(
        self,
        wanted: bool,
        sample_errors: list[tuple[str, BaseException]],
        *,
        prior_heartbeats: dict[str, dict[str, Any]],
        prior_sent_at: dict[str, float],
    ) -> tuple[
        bool,
        dict[str, dict[str, Any]],
        dict[str, float],
        list[Frame],
    ]:
        if not wanted:
            # Nobody is filtering worker.heartbeat in any more. Drop the cached
            # sample and dedupe clock on commit so the next interested
            # subscriber gets a fresh snapshot instead of silence.
            return False, {}, {}, []
        try:
            beats = self._store.get_heartbeats()
        except Exception as exc:
            self._note_sample_failure(sample_errors, "heartbeat_sample", exc)
            return True, prior_heartbeats, prior_sent_at, []
        now = time.monotonic()
        current: dict[str, dict[str, Any]] = {}
        sent_at = dict(prior_sent_at)
        frames: list[Frame] = []
        for beat in beats:
            worker_id = str(beat.get("worker_id", ""))
            if not worker_id:
                continue
            row = dict(beat)
            current[worker_id] = row
            last = sent_at.get(worker_id)
            if last is None or now - last >= HEARTBEAT_MIN_INTERVAL_S:
                sent_at[worker_id] = now
                frames.append((FRAME_HEARTBEAT, row))
        for worker_id in list(sent_at):
            if worker_id not in current:
                del sent_at[worker_id]
        return True, current, sent_at, frames

    def _sample_sessions(
        self,
        wanted: bool,
        reader: Any | None,
        sample_errors: list[tuple[str, BaseException]],
        *,
        prior_sessions: dict[str, dict[str, Any]],
        prior_stamps: dict[str, str],
    ) -> tuple[
        bool,
        dict[str, dict[str, Any]],
        dict[str, str],
        list[Frame],
        bool,
    ]:
        if not wanted:
            # Nobody is listening for session.updated any more. Forget the
            # dedupe stamps on commit so the next interested subscriber gets a
            # full republish rather than silence.
            return False, {}, {}, [], True
        if reader is None:
            if not sample_errors:
                self._note_sample_failure(
                    sample_errors,
                    "sessions_reader",
                    RuntimeError("sessions reader unavailable"),
                )
            return True, prior_sessions, prior_stamps, [], False
        try:
            rows = reader.list_sessions(limit=SESSION_SAMPLE_LIMIT)
        except Exception as exc:
            self._note_sample_failure(sample_errors, "session_sample", exc)
            return True, prior_sessions, prior_stamps, [], False
        current: dict[str, dict[str, Any]] = {}
        stamps = dict(prior_stamps)
        frames: list[Frame] = []
        for row in rows:
            session_id = str(row.get("id") or "")
            if not session_id:
                continue
            stamp = str(row.get("updated_at") or "")
            snapshot_row = dict(row)
            current[session_id] = snapshot_row
            if stamps.get(session_id) != stamp:
                stamps[session_id] = stamp
                frames.append((FRAME_SESSION, snapshot_row))
        for session_id in list(stamps):
            if session_id not in current:
                del stamps[session_id]
        return True, current, stamps, frames, True

    def _sample_events(
        self,
        sample_errors: list[tuple[str, BaseException]],
        *,
        after_id: int,
    ) -> tuple[int, list[dict[str, Any]], list[Frame]]:
        try:
            rows = self._store.get_events_after(after_id, None, EVENT_BATCH_LIMIT)
        except Exception as exc:
            self._note_sample_failure(sample_errors, "event_sample", exc)
            return after_id, [], []
        if not rows:
            return after_id, [], []
        cursor = after_id
        accepted_rows: list[dict[str, Any]] = []
        frames: list[Frame] = []
        for row in rows:
            try:
                event_id = int(row["id"])
            except (KeyError, TypeError, ValueError):
                continue
            if event_id <= cursor:
                continue
            cursor = event_id
            accepted_rows.append(row)
            frames.append((FRAME_EVENT, row))
        return cursor, accepted_rows, frames


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------
# In production there is exactly one store (``deps.get_store`` is lru_cached), so
# this is a process-wide singleton. Keying by store keeps test stores isolated
# from each other; the weak keys let a discarded store's hub be collected.
_REGISTRY_LOCK = threading.Lock()
_HUBS: weakref.WeakKeyDictionary[Any, EventHub] = weakref.WeakKeyDictionary()
_FALLBACK_HUB: EventHub | None = None


def get_event_hub(store: EventSource) -> EventHub:
    """Return the process-wide hub for ``store``, creating it on first use."""
    global _FALLBACK_HUB
    with _REGISTRY_LOCK:
        try:
            hub = _HUBS.get(store)
            if hub is None:
                hub = EventHub(store)
                _HUBS[store] = hub
            return hub
        except TypeError:
            # Store is unhashable / not weak-referenceable: one shared hub still
            # beats N per-connection pollers.
            if _FALLBACK_HUB is None:
                _FALLBACK_HUB = EventHub(store)
            return _FALLBACK_HUB


def shutdown_event_hubs(timeout: float | None = 2.0) -> None:
    """Stop every tailer. Intended for an application shutdown/lifespan hook."""
    global _FALLBACK_HUB
    with _REGISTRY_LOCK:
        hubs = list(_HUBS.values())
        if _FALLBACK_HUB is not None:
            hubs.append(_FALLBACK_HUB)
        _HUBS.clear()
        _FALLBACK_HUB = None
    for hub in hubs:
        hub.stop(timeout)
