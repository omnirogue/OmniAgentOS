"""Slack Socket Mode ingestion — the PUSH half of the hybrid Slack pipeline.

    python -m omniagentos.comms.sockets.slack

WHAT THIS IS AND WHAT IT IS NOT
-------------------------------
This client carries LATENCY: a message reaches ``comms_messages`` sub-second
instead of within a poll interval. It carries NO durability of its own. Slack
Socket Mode does not replay events that occurred while the client was
disconnected — it fires and forgets — so this process is only safe to run
because ``omniagentos.comms.pollers.slack`` keeps running behind it as a
low-frequency reconciliation sweep. The two are ONE design:

* ``KeepAlive`` makes a dead client SURVIVABLE,
* the sweep makes a dead client HARMLESS,
* the health sentinel makes a dead client VISIBLE.

Any two of those without the third is a documented outage shape in this repo.

THE CURSOR RULE
---------------
This module writes the ``slack-socket`` ``comms_sources`` row and NOTHING else
on the source table. It must never touch ``config["last_poll_ts"]`` on the
poller's ``slack`` row: that value is the sweep's entire backfill window, and
advancing it here would erase exactly the window the sweep exists to re-read,
producing an architecture strictly worse than the poller alone with no visible
symptom. Message rows ARE shared — ``normalize_slack`` hardcodes
``source="slack"`` — and that convergence is the point.

IDEMPOTENCY
-----------
``UNIQUE(source, external_id)`` on ``comms_messages`` (contract 007), enforced by
``INSERT OR IGNORE`` inside ``StewardStore.insert_comms_message``, which reports
the outcome as ``created``. That single database constraint absorbs all four
duplicate sources — Slack redelivery after a missed ack, the sweep re-reading a
window this client already ingested, two connections overlapping during a
``refresh_requested`` handover, and a socket restart mid-backlog — because both
paths produce byte-identical ``("slack", str(event["ts"]))`` tuples. The
``event_id`` memory below is an OBSERVABILITY counter and never gates a write.

ACCEPTED LIMITATIONS OF THE COUNTERS (stated because a counter that quietly
under-reports is worse than no counter)

* ``redelivered`` resets on process restart — the ``event_id`` ring is in
  memory by design (a persistent one would become a NEW source of loss). After
  a crash, Slack's redeliveries of pre-crash envelopes are not counted as
  redelivered, though dedupe still makes them no-ops. The AUTHORITATIVE
  socket-miss signal is the sweep's ``reconciled_total``, which is durable and
  watermarked by the health sentinel; ``redelivered`` is colour, not evidence.
* ``store_latency_ms_max`` measures wall time around ``insert_comms_message``,
  which includes waiting on the process-wide store lock and SQLite's 5s
  ``busy_timeout`` — it is a CONTENTION metric, not a database-speed metric, and
  multi-second values under concurrent API writes are expected rather than
  alarming. ``store_slow_writes`` (monotonic count over ``SLOW_WRITE_MS``) is
  what the sentinel watches, because a max never decays and would latch forever.

KNOWN LIMITS, STATED RATHER THAN PAPERED OVER
---------------------------------------------
* **Private channels and DMs are unreachable by BOTH paths.** The app has no
  ``groups:history`` / ``im:history`` / ``mpim:history`` scope, so it cannot even
  subscribe to ``message.groups`` / ``message.im`` / ``message.mpim``. Public
  channels the bot is a member of are the whole surface.
* **Thread replies are the hybrid's one genuine hole.** The Events API delivers
  every reply; ``conversations.history`` does not return them. The socket
  therefore ingests a strict SUPERSET of the sweep's set, and the sweep CANNOT
  backfill a thread reply lost during a socket outage. "Self-heals" is true for
  top-level channel messages and FALSE for thread replies until a
  ``conversations.replies`` pass lands in ``poll_once``.
* **Edits and deletions are tracked by neither path.** ``message_changed`` /
  ``message_deleted`` are skipped and counted here (feeding them to
  ``normalize_slack`` produces a junk row keyed on the EDIT's timestamp with an
  empty sender and body) and ``conversations.history`` never returns them at
  all. Skipping is the CONSISTENT choice, not a complete one; real tracking
  needs new schema and a contract.
* ``thread_ts`` is ignored: ``thread_id`` is the CHANNEL id on both paths. Slack
  thread structure survives only inside ``raw``.

OBSERVED SDK BEHAVIOUR (slack_sdk 3.43.0, read from the installed source)
------------------------------------------------------------------------
* ``SocketModeClient.connect()`` constructs and CONNECTS the replacement session
  before calling ``old_session.close()``, so a ``refresh_requested`` handover
  OVERLAPS rather than gapping. Overlap is affordable only because dedupe is a
  hard DB constraint — a duplicate costs one ``created=False``.
* ``disconnect`` envelopes never reach ``message_listeners``: both
  ``process_message`` and ``run_message_listeners`` intercept them to call
  ``connect_to_new_endpoint(force=True)``. The disconnect signal this client can
  actually observe is therefore ``on_close_listeners``; the ``disconnect`` branch
  in ``_on_message`` is kept because it costs nothing and a future SDK may
  deliver it.
* ``issue_new_wss_url`` already honours ``Retry-After`` on ``ratelimited`` and
  raises on ``invalid_auth`` / ``account_inactive`` / ``token_revoked``, which the
  supervisor below turns into an ``error`` source row and a capped retry — never
  an exit, because a process that exits on bad auth is respawned by ``KeepAlive``
  in a loop and never surfaces the reason.
"""

from __future__ import annotations

import argparse
import logging
import os
import random
import threading
import time
from collections import OrderedDict
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass
from typing import Any

from omniagentos.comms.normalize import normalize_slack

# The poller owns the secret-safe error shaping (`str(exc)` on an httpx failure
# embeds the request URL) and the self-message predicate. Imported rather than
# reimplemented so the two paths cannot drift: a self-filter on one side only
# would make the reconciliation counter permanently non-zero.
from omniagentos.comms.pollers.slack import (
    _error_detail,
    _sanitize,
    is_self_message,
    scrub_tokens,
)
from omniagentos.connectors import load_registry
from omniagentos.connectors.broker import AuditContext, BrokerDenied, resolve_for
from omniagentos.contracts import default_db_path, utc_now_iso
from omniagentos.db.store import SqliteStore
from omniagentos.steward.store import StewardStore

__all__ = ["main", "run"]

logger = logging.getLogger(__name__)

KIND = "slack_socket"
SOURCE_NAME = "slack-socket"
POLLER_SOURCE_NAME = "slack"
CAPABILITY_ID = "slack_ingest.stream"
AUDIT_HOLDER = "job:comms-slack-socket"
APP_TOKEN_ENV = "SLACK_APP_TOKEN"
BOT_TOKEN_ENV = "SLACK_BOT_TOKEN"

HEARTBEAT_SECONDS = 60.0
HEARTBEAT_ENV = "OMNIAGENTOS_SLACK_SOCKET_HEARTBEAT_SECONDS"
BACKOFF_BASE_SECONDS = 1.0
BACKOFF_CAP_SECONDS = 60.0
STABLE_CONNECTION_SECONDS = 60.0
#: How often the supervisor re-asks "is the socket still up?".
SUPERVISOR_POLL_SECONDS = 5.0
#: How long the SDK's own auto-reconnect gets to recover before the supervisor
#: tears the client down and rebuilds it. Longer than one ping interval.
SUPERVISOR_GRACE_SECONDS = 30.0
#: Bounded, purely for the `redelivered` counter. Never a gate: an in-memory set
#: that dies with the process would become a NEW source of loss on restart,
#: which is precisely the inversion this design exists to avoid. It also means
#: `redelivered` restarts at 0 with the process — see the docstring's accepted
#: limitations; the sweep's `reconciled_total` is the durable signal.
EVENT_ID_MEMORY = 2048
#: A store write at or above this is counted (`store_slow_writes`) so the health
#: sentinel can watch the RATE of slow writes rather than a max that never
#: decays. 1000ms is well under Slack's 3s ack budget and well over a healthy
#: uncontended INSERT; it is a contention tripwire, not a database benchmark.
SLOW_WRITE_MS = 1000
#: Listener workers. Pinned rather than left to the SDK default so the queueing
#: behind the ack is an explicit property of this design — see `_build_client`.
SOCKET_LISTENER_CONCURRENCY = 10
#: Re-resolving credentials on every supervisor iteration writes a durable
#: broker intent+finalization audit PAIR each time. On the unlimited-retry
#: denial path that is ~2,880 rows/day into the same SQLite file the ingest path
#: writes to, forever. Resolution is therefore cached, and a denial is retried on
#: this slower clock than the connect backoff.
CREDENTIAL_RETRY_SECONDS = 300.0
#: Cached credentials are re-resolved this often even while everything is
#: healthy, so a rotated token is picked up without an operator restart.
CREDENTIAL_REFRESH_SECONDS = 3600.0
#: Substrings that mean "these tokens are no longer valid" — the one condition
#: that invalidates the cache immediately instead of on the refresh clock.
_AUTH_FAILURE_MARKERS = (
    "invalid_auth",
    "not_authed",
    "account_inactive",
    "token_revoked",
    "token_expired",
)

#: Subtypes whose top-level `ts` is NOT the message's id. `message_changed`
#: carries the edit's timestamp with the real message at `event["message"]`;
#: `message_deleted` carries a tombstone with the target at `deleted_ts`. Both
#: arrive `hidden: true`. Normalizing either produces a junk row with an empty
#: sender and body under a timestamp that identifies nothing.
_SKIP_SUBTYPES = frozenset({"message_changed", "message_deleted", "message_replied"})

_PENDING_SETUP_REASONS = frozenset({"credential_missing", "capability_unprovisioned"})

#: P6 (Slack inbound task updates). Off by default — unset or anything other
#: than a truthy value below is byte-identical to pre-P6 behaviour. See
#: ``omniagentos.team.slack_updates`` for the command grammar and semantics;
#: this module only decides WHETHER a message is eligible to reach it.
TEAM_SLACK_UPDATES_FLAG_ENV = "OMNIAGENTOS_TEAM_SLACK_UPDATES"
_TRUTHY = frozenset({"1", "true", "yes", "on"})
#: The report channel the inbound-update commands are read from
#: (``#dev-agentic-alerts``, see ``daily-dev-scoreboard.py``). Overridable so a
#: staging channel can be used without a code change.
TEAM_REPORT_CHANNEL_ENV = "OMNI_TEAM_REPORT_CHANNEL"
DEFAULT_TEAM_REPORT_CHANNEL = "C0000EXAMPLE"


@dataclass
class _Counters:
    """Everything the heartbeat publishes into the source row's ``config``."""

    connected_at: str = ""
    disconnected_at: str = ""
    connects: int = 0
    disconnects: int = 0
    num_connections: int = 0
    envelopes: int = 0
    ingested: int = 0
    created: int = 0
    redelivered: int = 0
    skipped_hidden: int = 0
    skipped_self: int = 0
    skipped_no_ts: int = 0
    skipped_other: int = 0
    store_failures: int = 0
    store_latency_ms_max: int = 0
    store_slow_writes: int = 0
    last_event_ts: str = ""
    last_disconnect_reason: str = ""


def _heartbeat_seconds() -> float:
    raw = (os.environ.get(HEARTBEAT_ENV) or "").strip()
    try:
        value = float(raw) if raw else HEARTBEAT_SECONDS
    except ValueError:
        logger.warning("%s=%r is not a number; using %s", HEARTBEAT_ENV, raw, HEARTBEAT_SECONDS)
        return HEARTBEAT_SECONDS
    return value if value > 0 else HEARTBEAT_SECONDS


def _backoff_delay(attempt: int, rand: Callable[[float, float], float] = random.uniform) -> float:
    """Full-jitter backoff: ``uniform(0, min(cap, base * 2**attempt))``.

    Full jitter rather than a fixed schedule because several processes racing the
    same ``apps.connections.open`` after a Slack-side blip is exactly how a
    reconnect storm is manufactured. The ceiling is capped; ``attempt`` is not,
    and retries are unlimited — a give-up is a silent death.

    Floored at 10ms: full jitter can legitimately draw 0.0, and while the 5s
    supervisor poll already keeps a retry loop from becoming a hammer, a delay
    that is explicitly never zero does not depend on that coincidence.
    """
    exponent = min(max(attempt, 0), 32)
    ceiling = min(BACKOFF_CAP_SECONDS, BACKOFF_BASE_SECONDS * float(2**exponent))
    return max(0.01, rand(0.0, ceiling))


def _envelope_from_request(request: Any) -> dict[str, Any]:
    """Flatten a ``SocketModeRequest`` into the plain envelope shape.

    A Mapping passes through unchanged, so the wire shape itself is a valid
    input. That is what lets the skip rules and the normalization be exercised
    without slack_sdk's transport ever being importable, and it also means a
    future SDK that hands the listener a dict does not silently degrade every
    envelope to ``type=""``.
    """
    if isinstance(request, Mapping):
        payload = request.get("payload")
        return {
            "type": str(request.get("type") or ""),
            "envelope_id": str(request.get("envelope_id") or ""),
            "payload": payload if isinstance(payload, Mapping) else {},
            "retry_attempt": int(request.get("retry_attempt") or 0),
            "retry_reason": str(request.get("retry_reason") or ""),
        }
    payload = getattr(request, "payload", None)
    return {
        "type": str(getattr(request, "type", "") or ""),
        "envelope_id": str(getattr(request, "envelope_id", "") or ""),
        "payload": payload if isinstance(payload, Mapping) else {},
        "retry_attempt": int(getattr(request, "retry_attempt", 0) or 0),
        "retry_reason": str(getattr(request, "retry_reason", "") or ""),
    }


def _maybe_handle_team_update(event: Mapping[str, Any]) -> None:
    """P6: hand an eligible message to the Slack inbound task-update parser.

    Gated on TWO things, both re-checked on every call (env can flip without a
    restart): the feature flag (default OFF — byte-identical pre-P6 behaviour
    when unset) and the report-channel allowlist. The mapped-human check is
    ``omniagentos.team.slack_updates``'s job, not this one's; a bot/self
    message never reaches here at all — the caller only invokes this AFTER
    ``is_self_message`` has already filtered it out.

    Imports ``omniagentos.team.slack_updates`` LAZILY so this module carries
    ZERO new import-time dependency when the flag is unset (the common case in
    every environment that has not opted in). Any exception — a bad config
    file, a store error, a Slack API failure — is caught here and logged ONE
    stderr line; it must never reach the caller, which is inside the ingest
    path that has ALREADY durably stored the raw message and must keep running
    regardless of what this optional side-channel does.
    """
    flag = (os.environ.get(TEAM_SLACK_UPDATES_FLAG_ENV) or "").strip().lower()
    if flag not in _TRUTHY:
        return
    allowlisted = (os.environ.get(TEAM_REPORT_CHANNEL_ENV) or "").strip() or (
        DEFAULT_TEAM_REPORT_CHANNEL
    )
    if str(event.get("channel") or "") != allowlisted:
        return
    try:
        from omniagentos.team.slack_updates import team_updates_handle

        team_updates_handle(event)
    except Exception as exc:  # noqa: BLE001 -- must never affect ingestion.
        logger.warning("slack team update handling failed: %s", _error_detail(exc))


def _handle_envelope(steward: StewardStore, envelope: Mapping[str, Any]) -> dict[str, Any]:
    """Normalize + store ONE Socket Mode envelope. NEVER raises.

    Returns ``{"ingested", "created", "skipped", "external_id", "event_id",
    "error", "store_ms"}``. The caller has ALREADY acked (see ``_on_request``);
    a store failure is counted and surfaced on the source row, never propagated —
    an exception escaping a listener kills the connection, which loses far more
    than the one message that failed.
    """
    outcome: dict[str, Any] = {
        "ingested": 0,
        "created": False,
        "skipped": "",
        "external_id": "",
        "event_id": "",
        "error": "",
        "store_ms": 0,
    }
    try:
        if str(envelope.get("type") or "") != "events_api":
            outcome["skipped"] = "not_events_api"
            return outcome

        payload = envelope.get("payload")
        payload = payload if isinstance(payload, Mapping) else {}
        outcome["event_id"] = str(payload.get("event_id") or "")

        event = payload.get("event")
        if not isinstance(event, Mapping):
            outcome["skipped"] = "malformed"
            return outcome
        if str(event.get("type") or "") != "message":
            outcome["skipped"] = "not_message"
            return outcome

        # G4: skip-and-count. `conversations.history` never returns these, so
        # skipping them does NOT desynchronize the two paths.
        if event.get("hidden") is True or str(event.get("subtype") or "") in _SKIP_SUBTYPES:
            outcome["skipped"] = "hidden"
            return outcome

        # Applied identically by the poller (`is_self_message`), so the
        # reconciliation counter stays a clean signal.
        if is_self_message(event):
            outcome["skipped"] = "self"
            return outcome

        # G10: a tsless event normalizes to external_id="" and EVERY later
        # tsless event would then collapse into the single ("slack", "") row.
        ts = str(event.get("ts") or "")
        if not ts:
            outcome["skipped"] = "no_ts"
            return outcome
        outcome["external_id"] = ts

        # No `channel=` kwarg: the Events API embeds the channel ID on the event
        # itself, which is why the poller (reading `conversations.history`, which
        # omits it) has to pass it explicitly. Both therefore produce the channel
        # ID. NEVER resolve it to a name — `thread_id` is not part of the
        # uniqueness key, so a name on one path would not duplicate rows, it
        # would make `thread_id` racy and first-writer-wins.
        message = normalize_slack(event)
        started = time.monotonic()
        _row, created = steward.insert_comms_message(message)
        outcome["store_ms"] = int((time.monotonic() - started) * 1000)
        outcome["ingested"] = 1
        outcome["created"] = bool(created)

        # P6, strictly AFTER the row above is durably stored, and only for a
        # message seen for the FIRST time: `created=False` means dedupe
        # already absorbed a Slack redelivery or a sweep/socket overlap (see
        # the module docstring's IDEMPOTENCY section), and re-applying a
        # command on every redelivery would double-post confirmations and
        # (for `claim`) spuriously conflict with itself. Whatever this does or
        # fails to do must never change whether the raw message was ingested —
        # see `_maybe_handle_team_update` for the flag/channel gate.
        if created:
            _maybe_handle_team_update(event)
    except Exception as exc:  # noqa: BLE001 -- a listener must never raise.
        outcome["skipped"] = "store_failure"
        # `scrub_tokens` rather than `_sanitize`: this path holds no token to
        # compare against, and an SDK/proxy error can quote one back.
        outcome["error"] = scrub_tokens(f"slack socket ingest failed: {_error_detail(exc)}")
        logger.warning("%s", outcome["error"])
    return outcome


def _resolve_tokens(audit_store: Any = None) -> tuple[str, str]:
    """Resolve ``(app_token, bot_token)`` through the credential broker.

    Deliberately NOT ``os.environ``: a new credential-shaped environment read in
    ``omniagentos/`` is refused by U-R9, and the broker gives a strictly better
    answer anyway — a connector-scoped resolution with a durable
    intent→finalization audit pair per call, and typed denials that map cleanly
    onto the source row's ``pending_setup`` / ``error`` states.
    """
    capability = load_registry().capability(CAPABILITY_ID)
    resolved = resolve_for(
        capability,
        audit_store=audit_store,
        audit_context=AuditContext(holder=AUDIT_HOLDER),
    )
    app_token = resolved.get(APP_TOKEN_ENV, "")
    bot_token = resolved.get(BOT_TOKEN_ENV, "")
    if not app_token or not bot_token:
        missing = APP_TOKEN_ENV if not app_token else BOT_TOKEN_ENV
        raise BrokerDenied("credential_missing", missing, f"{missing} resolved empty")
    return app_token, bot_token


def _required_env_names() -> str:
    try:
        registry = load_registry()
        capability = registry.capability(CAPABILITY_ID)
        return ", ".join(registry.connectors[capability.connector].env)
    except Exception:  # noqa: BLE001 -- naming the names is best-effort diagnostics.
        return f"{BOT_TOKEN_ENV}, {APP_TOKEN_ENV}"


def _build_client(app_token: str, bot_token: str) -> Any:
    """The built-in (stdlib socket/ssl) SocketModeClient.

    Imported lazily so a missing ``slack-sdk`` surfaces as an ``error`` source row
    the sentinel can read, rather than as an ImportError at module load that also
    takes out anything importing this module. The aiohttp and websocket-client
    flavours would each add a transport dependency for no benefit here.

    ACCEPTED RISK — ack latency under store contention (declared, not solved).
    ``on_request`` acks before it stores, but the SDK dispatches listeners
    through a ``ThreadPoolExecutor(max_workers=concurrency)``, so the ack of the
    Nth QUEUED envelope waits for a free worker, and every worker serialises on
    the one process-wide store lock. A pathological 5s SQLite busy wait can
    therefore push a queued envelope's ack past Slack's 3s budget, which costs a
    redelivery (free — dedupe is a DB constraint) and, if sustained, a connection
    cycle (repaired by the sweep). ``concurrency`` is pinned explicitly here so
    the queueing is a stated property rather than an SDK default, and
    ``store_slow_writes`` makes the precondition observable to the health
    sentinel BEFORE it becomes an outage. The full fix — ack, then hand the
    envelope to a bounded queue drained by one writer thread — is deliberately
    NOT taken yet: it widens the acked-but-unwritten window from one message to
    the whole queue depth, and that window is repairable by the sweep for
    top-level messages but NOT for thread replies. It is the right change only
    once ``store_slow_writes`` shows real pressure. See scripts/comms/SETUP.md.
    """
    from slack_sdk.socket_mode import SocketModeClient
    from slack_sdk.web import WebClient

    return SocketModeClient(
        app_token=app_token,
        web_client=WebClient(token=bot_token),
        auto_reconnect_enabled=True,
        trace_enabled=False,
        concurrency=SOCKET_LISTENER_CONCURRENCY,
    )


def _send_ack(client: Any, envelope_id: str) -> None:
    from slack_sdk.socket_mode.response import SocketModeResponse

    client.send_socket_mode_response(SocketModeResponse(envelope_id=envelope_id))


class _SocketIngest:
    """Listener state: counters, the heartbeat writer, and the ack-first path."""

    def __init__(
        self,
        steward: StewardStore,
        *,
        heartbeat_seconds: float | None = None,
        ack: Callable[[Any, str], None] = _send_ack,
    ) -> None:
        self._steward = steward
        self._ack = ack
        self._heartbeat_seconds = heartbeat_seconds or _heartbeat_seconds()
        self._counters = _Counters()
        self._seen_event_ids: OrderedDict[str, None] = OrderedDict()
        # Reentrant: `publish` snapshots status, error AND counters under ONE
        # acquisition, and helpers it may call (`_config`) take the same lock.
        self._lock = threading.RLock()
        self._shutdown = threading.Event()
        self._status = "pending_setup"
        self._last_error = ""
        self._logged_error = ""

    # -- source row ---------------------------------------------------------

    def _config(self) -> dict[str, Any]:
        with self._lock:
            return asdict(self._counters)

    def _snapshot(
        self, status: str | None, last_error: str | None
    ) -> tuple[str, str, dict[str, Any]]:
        """Apply the update and read back status, error and counters ATOMICALLY.

        Three threads write this state — the heartbeat timer, the SDK's listener
        workers, and the supervisor. Applying the update and then re-reading the
        fields outside the lock lets one thread's status pair with another
        thread's error, so a row can claim ``active`` while carrying a live error
        string (or ``error`` with none). One heartbeat row must represent one
        moment, or an operator is being told something that was never true.
        """
        with self._lock:
            if status is not None:
                self._status = status
            if last_error is not None:
                self._last_error = last_error
            return self._status, self._last_error, asdict(self._counters)

    def publish(self, status: str | None = None, *, last_error: str | None = None) -> None:
        """Write the heartbeat row. A failure here must never end the process."""
        status_now, error_now, config = self._snapshot(status, last_error)
        try:
            self._steward.upsert_comms_source(
                SOURCE_NAME,
                KIND,
                status=status_now,
                # THE heartbeat timestamp. Timer-driven, not message-driven: a
                # heartbeat that ticks only on message arrival cannot tell a
                # quiet channel from a dead socket, which is the whole question.
                last_poll_at=utc_now_iso(),
                config=config,
                last_error=error_now,
            )
        except Exception as exc:  # noqa: BLE001 -- observability, not control flow.
            logger.warning("slack socket heartbeat write failed: %s", _error_detail(exc))

    def heartbeat_forever(self) -> None:
        while not self._shutdown.wait(self._heartbeat_seconds):
            self.publish()

    def stop_heartbeat(self) -> None:
        self._shutdown.set()

    # -- transitions --------------------------------------------------------

    def mark_connected(self) -> None:
        """The SUPERVISOR's transport-up transition — the only thing that counts.

        Separate from :meth:`note_hello` on purpose: both fire for one successful
        connection, and folding them together double-counted every connect.
        """
        with self._lock:
            self._counters.connected_at = utc_now_iso()
            self._counters.connects += 1
            connected_at = self._counters.connected_at
        logger.info("slack socket connected at %s", connected_at)
        self.publish("active", last_error="")

    def note_hello(self, num_connections: int | None) -> None:
        """Slack's own ``hello``: carries ``num_connections``, not a new connect."""
        with self._lock:
            self._counters.connected_at = utc_now_iso()
            if num_connections is not None:
                self._counters.num_connections = num_connections
            observed = self._counters.num_connections
        logger.info("slack socket hello (num_connections=%s)", observed)
        self.publish("active", last_error="")

    def mark_disconnected(self, reason: str, *, status: str | None = None) -> None:
        """Record a gap. ``status=None`` deliberately leaves the verdict alone.

        A websocket close is NOT by itself a failure: Slack cycles the
        connection on ``refresh_requested`` as routine housekeeping, and
        slack_sdk reconnects itself. Flipping the row to ``error`` on every close
        would make the health sentinel FAIL on ordinary maintenance, which is how
        an alarm gets ignored. Only the SUPERVISOR — the one component that knows
        whether the connection came back — passes ``status="error"``.
        """
        with self._lock:
            self._counters.disconnected_at = utc_now_iso()
            self._counters.disconnects += 1
            self._counters.last_disconnect_reason = reason
        # The (disconnected_at, connected_at) pair in config_json IS the
        # operator-visible gap window. This client deliberately does NOT
        # self-backfill it: a second cursor is a second thing to get wrong and a
        # real risk of racing the poller's. Backfill is the sweep's single job.
        logger.info(
            "slack socket disconnected at %s (reason=%s, disconnects=%d)",
            self._counters.disconnected_at,
            reason,
            self._counters.disconnects,
        )
        if status is None:
            self.publish()
        else:
            self.publish(status, last_error=f"slack socket disconnected: {reason}")

    def mark_error(self, status: str, error: str) -> None:
        """Record a failure. The LOG is deduped; the source row never is.

        The unlimited-retry loop below re-reports the same unprovisioned
        credential every ``CREDENTIAL_RETRY_SECONDS`` forever. Logging it at
        ERROR every time fills an unrotated launchd log with one repeated
        sentence; the row's ``last_error`` — which is what the sentinel actually
        reads — is refreshed on every attempt regardless.
        """
        with self._lock:
            repeat = error == self._logged_error
            self._logged_error = error
        if repeat:
            logger.debug("%s (unchanged)", error)
        else:
            logger.error("%s", error)
        self.publish(status, last_error=error)

    def clear_error(self) -> None:
        with self._lock:
            self._logged_error = ""

    def last_disconnect_reason(self) -> str:
        with self._lock:
            return self._counters.last_disconnect_reason

    # -- listeners ----------------------------------------------------------

    def on_request(self, client: Any, request: Any) -> None:
        """ACK FIRST, unconditionally, then ingest.

        The 3-second deadline is a property of the CONNECTION, not the message:
        Slack redelivers an unacked envelope and cycles the connection of a
        consumer that keeps missing the window. So the blast radius of a late ack
        is not "one duplicate", it is "the socket goes away" — the exact failure
        this whole design exists to prevent. Meanwhile a single
        ``INSERT OR IGNORE`` can legitimately block for up to five seconds
        (SqliteStore opens connections with ``timeout=5.0`` and every StewardStore
        method serialises on a process-wide lock shared with the API process),
        which is 167% of the ack budget.

        Process-then-ack does not even remove the loss window, it only moves it
        from (ack → commit) to (commit → ack), and the redelivery it buys is FREE
        because dedupe is a hard DB constraint. Ack-first is safe BECAUSE dedupe
        is mechanical and the reconciliation sweep exists — not because the
        window is small. Ack-first WITHOUT the sweep is the silent-permanent-loss
        failure. If the sweep is ever removed, this ordering must be revisited.
        """
        envelope = _envelope_from_request(request)
        envelope_id = str(envelope.get("envelope_id") or "")
        if envelope_id:
            try:
                self._ack(client, envelope_id)
            except Exception as exc:  # noqa: BLE001 -- never skip the ingest below.
                logger.warning("slack socket ack failed: %s", _error_detail(exc))
        self.ingest(envelope)

    def ingest(self, envelope: Mapping[str, Any]) -> dict[str, Any]:
        outcome = _handle_envelope(self._steward, envelope)
        healed = False
        with self._lock:
            counters = self._counters
            counters.envelopes += 1
            event_id = str(outcome.get("event_id") or "")
            if event_id:
                if event_id in self._seen_event_ids:
                    counters.redelivered += 1
                    self._seen_event_ids.move_to_end(event_id)
                else:
                    self._seen_event_ids[event_id] = None
                    while len(self._seen_event_ids) > EVENT_ID_MEMORY:
                        self._seen_event_ids.popitem(last=False)
            skipped = str(outcome.get("skipped") or "")
            if skipped == "hidden":
                counters.skipped_hidden += 1
            elif skipped == "self":
                counters.skipped_self += 1
            elif skipped == "no_ts":
                counters.skipped_no_ts += 1
            elif skipped == "store_failure":
                counters.store_failures += 1
            elif skipped:
                counters.skipped_other += 1
            if outcome.get("ingested"):
                counters.ingested += 1
                counters.last_event_ts = str(outcome.get("external_id") or "")
                store_ms = int(outcome.get("store_ms") or 0)
                counters.store_latency_ms_max = max(counters.store_latency_ms_max, store_ms)
                if store_ms >= SLOW_WRITE_MS:
                    counters.store_slow_writes += 1
                # A completed write is EVIDENCE the store works again. Without
                # this, one transient `database is locked` at 03:00 latches
                # status=error until the next connection handshake (Slack cycles
                # roughly hourly), so the sentinel alarms for an hour about a
                # fault that lasted a millisecond — and an operator who learns to
                # ignore this alarm also ignores the real reconciliation signal.
                # Persistence is expressed by the `store_failures` counter's RATE
                # instead, which the sentinel watermarks.
                healed = self._status == "error"
            if outcome.get("created"):
                counters.created += 1
        if outcome.get("error"):
            self.publish("error", last_error=str(outcome["error"]))
        elif healed:
            self.clear_error()
            self.publish("active", last_error="")
        return outcome

    def on_message(self, client: Any, message: Mapping[str, Any], raw: Any = None) -> None:
        del client, raw
        message_type = str(message.get("type") or "")
        if message_type == "hello":
            raw_count = message.get("num_connections")
            self.note_hello(int(raw_count) if isinstance(raw_count, int) else None)
        elif message_type == "disconnect":
            # Unreachable on slack_sdk 3.43.0 (both dispatch sites intercept
            # `disconnect` first); kept because it is free and version-proof.
            self.mark_disconnected(f"envelope reason={message.get('reason') or ''}")

    def on_close(self, code: int, reason: str | None = None) -> None:
        self.mark_disconnected(f"websocket close code={code} reason={reason or ''}")

    def attach(self, client: Any) -> None:
        for attribute, listener in (
            ("socket_mode_request_listeners", self.on_request),
            ("message_listeners", self.on_message),
            ("on_close_listeners", self.on_close),
        ):
            listeners = getattr(client, attribute, None)
            if listeners is None:
                continue
            if listener not in listeners:
                listeners.append(listener)


def _is_connected(client: Any) -> bool:
    try:
        return bool(client.is_connected())
    except Exception:  # noqa: BLE001 -- an unanswerable probe is not a healthy one.
        return False


def _close_quietly(client: Any) -> None:
    try:
        client.close()
    except Exception as exc:  # noqa: BLE001
        logger.warning("slack socket close failed: %s", _error_detail(exc))


def run(
    steward: StewardStore | None = None,
    *,
    client: Any = None,
    stop: threading.Event | None = None,
    audit_store: Any = None,
    once: bool = False,
    heartbeat_seconds: float | None = None,
) -> int:
    """Connect and ingest until ``stop`` is set; return a process exit code.

    Never returns on a transient failure — it reconnects with full-jitter backoff
    and unlimited retries. ``once=True`` (operator/test use) makes every failure
    terminal instead, so the exit code means something.
    """
    stop = stop or threading.Event()
    own_store: SqliteStore | None = None
    if steward is None:
        own_store = SqliteStore(default_db_path())
        steward = StewardStore(own_store)

    ingest = _SocketIngest(steward, heartbeat_seconds=heartbeat_seconds)
    heartbeat = threading.Thread(
        target=ingest.heartbeat_forever, name="slack-socket-heartbeat", daemon=True
    )
    heartbeat.start()

    attempt = 0
    exit_code = 0
    tokens = ("", "")
    resolved_at = 0.0
    try:
        while not stop.is_set():
            # -- credentials -------------------------------------------------
            # Cached across supervisor iterations: every `resolve_for` writes a
            # durable broker intent+finalization audit PAIR, so re-resolving on
            # each reconnect turns a bad night into thousands of audit rows in
            # the same SQLite file the ingest path writes to. Invalidated on an
            # auth-class connect failure (below) and on a slow refresh clock, so
            # a rotated token still lands without an operator restart.
            age = time.monotonic() - resolved_at
            if not all(tokens) or age >= CREDENTIAL_REFRESH_SECONDS:
                try:
                    tokens = _resolve_tokens(audit_store)
                    resolved_at = time.monotonic()
                except BrokerDenied as exc:
                    status = "pending_setup" if exc.reason in _PENDING_SETUP_REASONS else "error"
                    ingest.mark_error(
                        status,
                        f"slack socket refusing to start: {exc.reason} ({exc.cap_id}); "
                        f"requires {_required_env_names()}",
                    )
                    exit_code = 2
                    # A denial is a human-scale problem (a token nobody has
                    # provisioned). Retrying it on the connect backoff would
                    # re-audit it every 60s forever; this clock is the throttle.
                    if once or stop.wait(max(_backoff_delay(attempt), CREDENTIAL_RETRY_SECONDS)):
                        break
                    attempt += 1
                    continue
                except Exception as exc:  # noqa: BLE001 -- registry/audit failure.
                    ingest.mark_error(
                        "error", f"slack socket credential resolution failed: {_error_detail(exc)}"
                    )
                    exit_code = 2
                    if once or stop.wait(max(_backoff_delay(attempt), CREDENTIAL_RETRY_SECONDS)):
                        break
                    attempt += 1
                    continue

            # -- connect -----------------------------------------------------
            active = client
            try:
                if active is None:
                    active = _build_client(*tokens)
                ingest.attach(active)
                active.connect()
            except Exception as exc:  # noqa: BLE001 -- every connect failure retries.
                detail = _sanitize(
                    _sanitize(f"slack socket connect failed: {_error_detail(exc)}", tokens[0]),
                    tokens[1],
                )
                ingest.mark_error("error", scrub_tokens(detail))
                # The one failure class the cached credentials cannot survive:
                # if Slack says these tokens are not valid, holding them for an
                # hour retries a guaranteed failure. Drop them and re-resolve.
                if any(marker in detail.lower() for marker in _AUTH_FAILURE_MARKERS):
                    tokens = ("", "")
                    resolved_at = 0.0
                exit_code = 1
                if active is not None and client is None:
                    _close_quietly(active)
                if once or stop.wait(_backoff_delay(attempt)):
                    break
                attempt += 1
                continue

            exit_code = 0
            ingest.mark_connected()
            connected_since = time.monotonic()

            # -- supervise ---------------------------------------------------
            # slack_sdk's own auto-reconnect can give up (repeated
            # apps.connections.open failures), and a give-up is a silent death.
            # The supervisor never gives up.
            while not stop.wait(SUPERVISOR_POLL_SECONDS):
                if _is_connected(active):
                    # Reset the backoff only after the connection has STAYED up:
                    # resetting on connect makes a flapping socket hammer at low
                    # backoff forever.
                    if time.monotonic() - connected_since >= STABLE_CONNECTION_SECONDS:
                        attempt = 0
                    continue
                grace_deadline = time.monotonic() + SUPERVISOR_GRACE_SECONDS
                while time.monotonic() < grace_deadline and not stop.is_set():
                    if _is_connected(active):
                        break
                    stop.wait(SUPERVISOR_POLL_SECONDS)
                if not _is_connected(active):
                    # Carry the LAST OBSERVED CLOSE REASON into the error the
                    # operator reads. "not reconnected within 30s" alone sends
                    # them to a separate log to find out whether this was auth,
                    # a rate limit or a dead network; the SDK's own close reason
                    # is the closest thing to a root cause this layer can see.
                    ingest.mark_disconnected(
                        f"supervisor: not reconnected within {SUPERVISOR_GRACE_SECONDS:.0f}s "
                        f"(last close: {ingest.last_disconnect_reason() or 'none observed'})",
                        status="error",
                    )
                    break
                connected_since = time.monotonic()

            _close_quietly(active)
            if once or stop.is_set():
                break
            stop.wait(_backoff_delay(attempt))
            attempt += 1
    finally:
        ingest.stop_heartbeat()
        heartbeat.join(timeout=5.0)
        ingest.publish()
        if own_store is not None:
            own_store.close()
    return exit_code


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m omniagentos.comms.sockets.slack")
    parser.add_argument(
        "--once",
        action="store_true",
        help="attempt a single connect cycle and exit (no reconnect loop)",
    )
    parser.add_argument(
        "--heartbeat-seconds",
        type=float,
        default=None,
        help=f"source-row heartbeat cadence (default {HEARTBEAT_SECONDS}, or ${HEARTBEAT_ENV})",
    )
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s"
    )
    # basicConfig configures only the ROOT logger, and slack_sdk's loggers are
    # created with `getLogger(__name__)` and no explicit level, so they inherit
    # it. At DEBUG, `WebClient.api_call` logs the outgoing apps.connections.open
    # request — which carries SLACK_APP_TOKEN — and the built-in client logs
    # every frame, straight into the plist's StandardOutPath. Pin the floor so
    # raising this process's verbosity can never do that.
    logging.getLogger("slack_sdk").setLevel(logging.INFO)
    return run(once=args.once, heartbeat_seconds=args.heartbeat_seconds)


if __name__ == "__main__":
    raise SystemExit(main())
