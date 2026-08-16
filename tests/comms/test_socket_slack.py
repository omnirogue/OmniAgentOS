"""Slack Socket Mode client — no network, no credentials, no slack_sdk transport.

Everything runs against the REAL tmp-path SQLite store (never a FakeStore): a
FakeStore that re-implements the store is how this repo previously masked a live
``sqlite3.Row`` ``AttributeError``, and the whole determinism claim here IS the
store's ``UNIQUE(source, external_id)`` constraint, so faking it would be faking
the thing under test.

Envelopes are fed to the client's own listener as plain dicts, which is exactly
the shape ``_envelope_from_request`` produces from a ``SocketModeRequest``.
"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Any

import pytest

from omniagentos.comms.pollers import slack as slack_poller
from omniagentos.comms.sockets import slack as slack_socket
from omniagentos.connectors.broker import BrokerDenied
from omniagentos.db.store import SqliteStore
from omniagentos.steward.store import StewardStore
from tests.support.db_template import make_store

CHANNEL = "C0SOCKET01"
BOT_ID = slack_poller.DEFAULT_SELF_BOT_ID


@pytest.fixture
def steward(tmp_path: Path) -> StewardStore:
    return StewardStore(make_store(SqliteStore, tmp_path / "socket.db"))


def _envelope(
    event: dict[str, Any],
    *,
    envelope_id: str = "env-1",
    event_id: str = "Ev0001",
    envelope_type: str = "events_api",
    retry_attempt: int = 0,
) -> dict[str, Any]:
    return {
        "type": envelope_type,
        "envelope_id": envelope_id,
        "retry_attempt": retry_attempt,
        "retry_reason": "timeout" if retry_attempt else "",
        "payload": {
            "type": "event_callback",
            "event_id": event_id,
            "team_id": "T0ACMEUNI",
            "event": event,
        },
    }


def _message_event(ts: str = "1700000000.000100", **overrides: Any) -> dict[str, Any]:
    event: dict[str, Any] = {
        "type": "message",
        "channel": CHANNEL,
        "channel_type": "channel",
        "user": "U0ALICE",
        "text": "socket hello",
        "ts": ts,
        "event_ts": ts,
    }
    event.update(overrides)
    return event


class _FakeClient:
    """Stands in for ``SocketModeClient``: records acks, holds the listeners."""

    def __init__(self) -> None:
        self.socket_mode_request_listeners: list[Any] = []
        self.message_listeners: list[Any] = []
        self.on_close_listeners: list[Any] = []
        self.acks: list[str] = []
        self.connected = False
        self.closed = 0
        self.connect_error: Exception | None = None

    def connect(self) -> None:
        if self.connect_error is not None:
            raise self.connect_error
        self.connected = True

    def is_connected(self) -> bool:
        return self.connected

    def close(self) -> None:
        self.connected = False
        self.closed += 1

    def send_socket_mode_response(self, response: Any) -> None:
        self.acks.append(getattr(response, "envelope_id", str(response)))


def _ingest(steward: StewardStore, **kwargs: Any) -> slack_socket._SocketIngest:
    kwargs.setdefault("ack", lambda client, envelope_id: client.acks.append(envelope_id))
    return slack_socket._SocketIngest(steward, heartbeat_seconds=3600.0, **kwargs)


# --- envelope -> normalized message -----------------------------------------


def test_envelope_maps_to_the_same_frozen_shape_the_poller_produces(
    steward: StewardStore,
) -> None:
    outcome = slack_socket._handle_envelope(steward, _envelope(_message_event()))

    assert outcome["ingested"] == 1
    assert outcome["created"] is True
    assert outcome["external_id"] == "1700000000.000100"

    stored = steward.list_comms_messages(source="slack")
    assert len(stored) == 1
    row = stored[0]
    assert row["source"] == "slack"
    assert row["external_id"] == "1700000000.000100"
    # The channel ID, never a name: `thread_id` is not part of the uniqueness
    # key, so a name on one path would make it racy/first-writer-wins.
    assert row["thread_id"] == CHANNEL
    assert row["recipients"] == [CHANNEL]
    assert row["sender"] == "U0ALICE"
    assert row["body_text"] == "socket hello"
    assert row["subject"] == ""
    assert row["sent_at"] == "2023-11-14T22:13:20Z"


def test_files_survive_as_attachments(steward: StewardStore) -> None:
    event = _message_event(
        subtype="file_share",
        files=[{"id": "F01", "name": "roadmap.pdf", "url_private": "https://example/x"}],
    )
    slack_socket._handle_envelope(steward, _envelope(event))
    row = steward.list_comms_messages(source="slack")[0]
    assert row["attachments"] == [{"type": "file", "id": "F01", "name": "roadmap.pdf"}]


def test_envelope_from_a_socket_mode_request_object_flattens_to_the_same_shape() -> None:
    class _Req:
        type = "events_api"
        envelope_id = "env-9"
        payload = {"event": {"type": "message", "ts": "1.0"}}
        retry_attempt = 2
        retry_reason = "timeout"

    assert slack_socket._envelope_from_request(_Req()) == {
        "type": "events_api",
        "envelope_id": "env-9",
        "payload": {"event": {"type": "message", "ts": "1.0"}},
        "retry_attempt": 2,
        "retry_reason": "timeout",
    }


# --- ack semantics -----------------------------------------------------------


def test_ack_is_sent_for_every_envelope_before_the_store_write(
    steward: StewardStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    order: list[str] = []
    client = _FakeClient()
    real_insert = steward.insert_comms_message

    def _record_insert(msg: dict[str, Any]) -> tuple[dict[str, Any], bool]:
        order.append("store")
        return real_insert(msg)

    monkeypatch.setattr(steward, "insert_comms_message", _record_insert)

    def _ack(target: Any, envelope_id: str) -> None:
        order.append("ack")
        target.acks.append(envelope_id)

    ingest = _ingest(steward, ack=_ack)
    ingest.on_request(client, _envelope(_message_event(), envelope_id="env-A"))

    assert client.acks == ["env-A"]
    assert order == ["ack", "store"], "ack must precede the store write, unconditionally"


def test_every_envelope_is_acked_even_when_the_event_is_skipped(steward: StewardStore) -> None:
    client = _FakeClient()
    ingest = _ingest(steward)
    ingest.on_request(client, _envelope(_message_event(subtype="message_deleted", hidden=True)))
    ingest.on_request(client, _envelope({"type": "reaction_added"}, envelope_id="env-B"))
    ingest.on_request(client, _envelope(_message_event(ts=""), envelope_id="env-C"))

    assert client.acks == ["env-1", "env-B", "env-C"]
    assert steward.list_comms_messages(source="slack") == []


def test_a_failing_ack_never_skips_the_ingest(steward: StewardStore) -> None:
    def _boom(client: Any, envelope_id: str) -> None:
        raise RuntimeError("websocket write failed")

    ingest = _ingest(steward, ack=_boom)
    ingest.on_request(_FakeClient(), _envelope(_message_event()))
    assert len(steward.list_comms_messages(source="slack")) == 1


# --- idempotency -------------------------------------------------------------


def test_a_redelivered_envelope_is_ingested_exactly_once(steward: StewardStore) -> None:
    ingest = _ingest(steward)
    event = _message_event()
    first = ingest.ingest(_envelope(event, envelope_id="env-1", event_id="Ev77"))
    second = ingest.ingest(
        _envelope(event, envelope_id="env-2", event_id="Ev77", retry_attempt=1)
    )

    assert first["created"] is True
    assert second["created"] is False, "the duplicate must be an INSERT OR IGNORE no-op"
    assert len(steward.list_comms_messages(source="slack")) == 1

    ingest.publish("active")  # counters reach the row on the heartbeat, not per message
    config = _source_config(steward, slack_socket.SOURCE_NAME)
    assert config["ingested"] == 2
    assert config["created"] == 1
    assert config["redelivered"] == 1


def test_the_event_id_memory_is_a_counter_and_never_a_gate(steward: StewardStore) -> None:
    """A duplicate event_id whose ts differs is still STORED.

    The gate is the DB constraint. An in-memory set that dies with the process
    would otherwise become a new source of loss on restart.
    """
    ingest = _ingest(steward)
    ingest.ingest(_envelope(_message_event(ts="1700000000.000001"), event_id="EvSame"))
    ingest.ingest(_envelope(_message_event(ts="1700000000.000002"), event_id="EvSame"))
    assert len(steward.list_comms_messages(source="slack")) == 2


def test_poller_then_socket_and_socket_then_poller_both_yield_exactly_one_row(
    tmp_path: Path,
) -> None:
    event = _message_event()

    poller_first = StewardStore(make_store(SqliteStore, tmp_path / "a.db"))
    from omniagentos.comms.normalize import normalize_slack

    poller_first.insert_comms_message(normalize_slack(event, channel=CHANNEL))
    slack_socket._handle_envelope(poller_first, _envelope(event))

    socket_first = StewardStore(make_store(SqliteStore, tmp_path / "b.db"))
    slack_socket._handle_envelope(socket_first, _envelope(event))
    socket_first.insert_comms_message(normalize_slack(event, channel=CHANNEL))

    rows_a = poller_first.list_comms_messages(source="slack")
    rows_b = socket_first.list_comms_messages(source="slack")
    assert len(rows_a) == 1 and len(rows_b) == 1
    assert rows_a[0]["external_id"] == rows_b[0]["external_id"]
    assert rows_a[0]["thread_id"] == rows_b[0]["thread_id"] == CHANNEL


# --- skip rules --------------------------------------------------------------


@pytest.mark.parametrize(
    ("event", "reason"),
    [
        ({"type": "message", "ts": "1.0", "hidden": True, "channel": CHANNEL}, "hidden"),
        (
            {
                "type": "message",
                "subtype": "message_changed",
                "channel": CHANNEL,
                "ts": "1700000009.000000",
                "message": {"ts": "1700000000.000100", "text": "edited"},
            },
            "hidden",
        ),
        (
            {
                "type": "message",
                "subtype": "message_deleted",
                "channel": CHANNEL,
                "ts": "1700000009.000000",
                "deleted_ts": "1700000000.000100",
            },
            "hidden",
        ),
        ({"type": "message", "channel": CHANNEL, "text": "no ts"}, "no_ts"),
        ({"type": "reaction_added", "ts": "1.0"}, "not_message"),
    ],
)
def test_events_that_must_never_be_stored(
    steward: StewardStore, event: dict[str, Any], reason: str
) -> None:
    outcome = slack_socket._handle_envelope(steward, _envelope(event))
    assert outcome["skipped"] == reason
    assert outcome["ingested"] == 0
    assert steward.list_comms_messages(source="slack") == []


def test_two_tsless_events_never_collapse_into_one_row(steward: StewardStore) -> None:
    ingest = _ingest(steward)
    ingest.ingest(_envelope({"type": "message", "channel": CHANNEL, "text": "a"}))
    ingest.ingest(_envelope({"type": "message", "channel": CHANNEL, "text": "b"}))
    assert steward.list_comms_messages(source="slack") == []
    ingest.publish("active")
    assert _source_config(steward, slack_socket.SOURCE_NAME)["skipped_no_ts"] == 2


def test_the_bots_own_message_is_ignored(steward: StewardStore) -> None:
    own = _message_event(ts="1700000000.000200", bot_id=BOT_ID, subtype="bot_message")
    own.pop("user")
    outcome = slack_socket._handle_envelope(steward, _envelope(own))
    assert outcome["skipped"] == "self"
    assert steward.list_comms_messages(source="slack") == []


def test_another_bots_message_is_still_real_content(steward: StewardStore) -> None:
    """The self-filter is narrow on purpose — a Jira bot's posts are the payload."""
    other = _message_event(ts="1700000000.000300", bot_id="B0OTHERBOT", subtype="bot_message")
    other.pop("user")
    outcome = slack_socket._handle_envelope(steward, _envelope(other))
    assert outcome["skipped"] == ""
    assert steward.list_comms_messages(source="slack")[0]["sender"] == "B0OTHERBOT"


def test_the_self_filter_is_identical_on_both_paths(monkeypatch: pytest.MonkeyPatch) -> None:
    own = _message_event(bot_id=BOT_ID)
    assert slack_poller.is_self_message(own) is True
    monkeypatch.setenv(slack_poller.SELF_BOT_ID_ENV, "B0OTHERBOT")
    assert slack_poller.is_self_message(own) is False


def test_a_malformed_envelope_does_not_crash_the_loop(steward: StewardStore) -> None:
    client = _FakeClient()
    ingest = _ingest(steward)
    for bad in (
        {},
        {"type": "events_api"},
        {"type": "events_api", "payload": None},
        {"type": "events_api", "payload": {"event": "not-a-mapping"}},
        {"type": "events_api", "payload": {"event": {}}},
        {"type": "hello", "num_connections": 1},
    ):
        ingest.on_request(client, bad)
    # Then a good one still lands: the listener survived every malformed input.
    ingest.on_request(client, _envelope(_message_event(), envelope_id="env-good"))
    assert len(steward.list_comms_messages(source="slack")) == 1
    assert client.acks[-1] == "env-good"


def test_a_store_failure_is_counted_and_surfaced_but_never_raised(
    steward: StewardStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _explode(msg: dict[str, Any]) -> tuple[dict[str, Any], bool]:
        raise RuntimeError("database is locked")

    monkeypatch.setattr(steward, "insert_comms_message", _explode)
    ingest = _ingest(steward)
    ingest.on_request(_FakeClient(), _envelope(_message_event()))

    row = _source_row(steward, slack_socket.SOURCE_NAME)
    assert row["status"] == "error"
    assert "database is locked" in row["last_error"]
    assert row["config"]["store_failures"] == 1


# --- the cursor rule ---------------------------------------------------------


def _source_row(steward: StewardStore, name: str) -> dict[str, Any]:
    rows = {row["name"]: row for row in steward.list_comms_sources()}
    assert name in rows, f"{name} row was never written (have: {sorted(rows)})"
    return rows[name]


def _source_config(steward: StewardStore, name: str) -> dict[str, Any]:
    config = _source_row(steward, name)["config"]
    assert isinstance(config, dict)
    return config


def test_socket_ingest_never_advances_the_pollers_cursor(steward: StewardStore) -> None:
    """The single most dangerous mistake available in this design.

    ``poll_once``'s whole backfill window is ``config["last_poll_ts"]`` on the
    ``slack`` row. If the socket advanced it, it would erase exactly the window
    the sweep exists to re-read — a hybrid strictly worse than the poller alone,
    with no visible symptom.
    """
    steward.upsert_comms_source(
        slack_socket.POLLER_SOURCE_NAME, "slack", config={"last_poll_ts": "1699999999.000000"}
    )
    ingest = _ingest(steward)
    ingest.on_request(_FakeClient(), _envelope(_message_event()))
    ingest.publish("active")

    poller_row = _source_row(steward, slack_socket.POLLER_SOURCE_NAME)
    assert poller_row["config"] == {"last_poll_ts": "1699999999.000000"}
    assert poller_row["kind"] == "slack"

    socket_row = _source_row(steward, slack_socket.SOURCE_NAME)
    assert socket_row["kind"] == slack_socket.KIND
    assert "last_poll_ts" not in socket_row["config"]


def test_the_two_rows_are_separate_and_the_message_namespace_is_shared(
    steward: StewardStore,
) -> None:
    ingest = _ingest(steward)
    ingest.ingest(_envelope(_message_event()))
    ingest.publish("active")
    names = {row["name"] for row in steward.list_comms_sources()}
    assert slack_socket.SOURCE_NAME in names
    assert steward.list_comms_messages(source="slack")[0]["source"] == "slack"


# --- liveness ----------------------------------------------------------------


def test_the_heartbeat_is_fresh_under_zero_traffic(steward: StewardStore) -> None:
    """Timer-driven, not message-driven: a heartbeat that ticks only on message
    arrival cannot distinguish a quiet channel from a dead socket."""
    ingest = _ingest(steward, ack=lambda client, envelope_id: None)
    ingest._heartbeat_seconds = 0.01
    thread = threading.Thread(target=ingest.heartbeat_forever, daemon=True)
    ingest.publish("active")
    first = _source_row(steward, slack_socket.SOURCE_NAME)["last_poll_at"]
    thread.start()
    try:
        deadline = 5.0
        step = 0.02
        waited = 0.0
        while waited < deadline:
            if _source_row(steward, slack_socket.SOURCE_NAME)["last_poll_at"] != first:
                break
            threading.Event().wait(step)
            waited += step
    finally:
        ingest.stop_heartbeat()
        thread.join(timeout=2.0)
    row = _source_row(steward, slack_socket.SOURCE_NAME)
    assert row["status"] == "active"
    assert row["config"]["envelopes"] == 0, "no traffic, yet the row is still being refreshed"


def test_connect_and_disconnect_are_recorded_with_timestamps(steward: StewardStore) -> None:
    ingest = _ingest(steward)
    ingest.mark_connected()
    ingest.on_message(None, {"type": "hello", "num_connections": 2})
    connected = _source_config(steward, slack_socket.SOURCE_NAME)
    assert connected["connected_at"] and connected["num_connections"] == 2
    # `hello` and the supervisor's transport-up BOTH fire for one connection;
    # only the supervisor counts, or every connect is counted twice.
    assert connected["connects"] == 1
    assert _source_row(steward, slack_socket.SOURCE_NAME)["status"] == "active"

    ingest.on_close(1001, "refresh_requested")
    row = _source_row(steward, slack_socket.SOURCE_NAME)
    assert row["config"]["disconnects"] == 1
    # (disconnected_at, connected_at) IS the operator-visible gap window.
    assert row["config"]["disconnected_at"] and row["config"]["connected_at"]
    # A close is NOT a failure — Slack cycles the connection on refresh_requested
    # as routine housekeeping and the SDK reconnects itself. Flipping to `error`
    # here would make the sentinel FAIL on ordinary maintenance.
    assert row["status"] == "active"
    assert row["last_error"] == ""


def test_only_a_connection_that_did_not_recover_is_an_error(steward: StewardStore) -> None:
    ingest = _ingest(steward)
    ingest.mark_connected()
    ingest.mark_disconnected("supervisor: not reconnected within 30s", status="error")
    row = _source_row(steward, slack_socket.SOURCE_NAME)
    assert row["status"] == "error"
    assert "not reconnected" in row["last_error"]


# --- reconnect backoff -------------------------------------------------------


def test_backoff_is_full_jitter_capped_at_sixty_seconds() -> None:
    highs: list[float] = []

    def _rand(low: float, high: float) -> float:
        assert low == 0.0
        highs.append(high)
        return high

    for attempt in range(0, 10):
        slack_socket._backoff_delay(attempt, _rand)
    assert highs[:7] == [1.0, 2.0, 4.0, 8.0, 16.0, 32.0, 60.0]
    assert all(value <= slack_socket.BACKOFF_CAP_SECONDS for value in highs)


def test_backoff_never_overflows_on_a_long_outage() -> None:
    delay = slack_socket._backoff_delay(10_000, lambda low, high: high)
    assert delay == slack_socket.BACKOFF_CAP_SECONDS


def test_backoff_is_never_zero_even_when_the_jitter_draws_zero() -> None:
    """Full jitter can legitimately draw 0.0; a retry delay must not be 0ms.

    The 5s supervisor poll already defends against a hammer, but a floor that
    does not depend on that coincidence is what makes the defence explicit.
    """
    assert slack_socket._backoff_delay(5, lambda low, high: low) == 0.01
    assert all(
        slack_socket._backoff_delay(attempt, lambda low, high: low) > 0.0 for attempt in range(12)
    )


# --- fail closed on missing credentials --------------------------------------


def test_missing_credentials_fail_closed_and_never_look_healthy(
    steward: StewardStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _denied(audit_store: Any = None) -> tuple[str, str]:
        raise BrokerDenied(
            "capability_unprovisioned",
            slack_socket.CAPABILITY_ID,
            "slack_ingest has no provisioned credential names",
        )

    monkeypatch.setattr(slack_socket, "_resolve_tokens", _denied)
    built: list[Any] = []
    monkeypatch.setattr(slack_socket, "_build_client", lambda *a, **k: built.append(a))

    code = run_once(steward)

    assert code == 2, "a half-configured start must be a non-zero exit, not a quiet no-op"
    assert built == [], "no connection may be attempted without both tokens"
    row = _source_row(steward, slack_socket.SOURCE_NAME)
    assert row["status"] == "pending_setup"
    assert slack_socket.BOT_TOKEN_ENV in row["last_error"]
    assert slack_socket.APP_TOKEN_ENV in row["last_error"]


def test_one_token_present_is_still_a_refusal(
    steward: StewardStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _denied(audit_store: Any = None) -> tuple[str, str]:
        raise BrokerDenied(
            "credential_missing",
            slack_socket.APP_TOKEN_ENV,
            f"{slack_socket.APP_TOKEN_ENV} is not present in the broker environment",
        )

    monkeypatch.setattr(slack_socket, "_resolve_tokens", _denied)
    assert run_once(steward) == 2
    row = _source_row(steward, slack_socket.SOURCE_NAME)
    assert row["status"] == "pending_setup"
    assert slack_socket.APP_TOKEN_ENV in row["last_error"]


def test_the_module_reads_no_credential_from_the_environment() -> None:
    """U-R9: the tokens come from the broker, so this file has no env read at all."""
    from tests.llm.test_unbrokered_credentials import _find_credential_reads

    path = Path(slack_socket.__file__)
    assert _find_credential_reads(path) == []


def test_an_invalid_auth_connect_failure_sets_error_and_retries_rather_than_exiting(
    steward: StewardStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(slack_socket, "_resolve_tokens", lambda audit_store=None: ("xapp", "xoxb"))
    client = _FakeClient()
    client.connect_error = RuntimeError("invalid_auth")

    code = run_once(steward, client=client)

    assert code == 1
    row = _source_row(steward, slack_socket.SOURCE_NAME)
    assert row["status"] == "error"
    assert "invalid_auth" in row["last_error"]


def test_a_connect_error_never_echoes_a_token(
    steward: StewardStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    app_token = "xapp-1-SECRETAPP"
    bot_token = "xoxb-SECRETBOT"
    monkeypatch.setattr(
        slack_socket, "_resolve_tokens", lambda audit_store=None: (app_token, bot_token)
    )
    client = _FakeClient()
    client.connect_error = RuntimeError(f"refused url=wss://x?token={app_token}&b={bot_token}")

    run_once(steward, client=client)

    last_error = _source_row(steward, slack_socket.SOURCE_NAME)["last_error"]
    assert app_token not in last_error
    assert bot_token not in last_error
    assert "[REDACTED]" in last_error


def run_once(steward: StewardStore, *, client: Any = None) -> int:
    """One supervisor cycle with `once=True`, so every failure is terminal."""
    return slack_socket.run(
        steward,
        client=client,
        stop=threading.Event(),
        once=True,
        heartbeat_seconds=3600.0,
    )


def test_run_attaches_its_listeners_to_the_client(
    steward: StewardStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(slack_socket, "_resolve_tokens", lambda audit_store=None: ("a", "b"))
    client = _FakeClient()
    stop = threading.Event()

    def _connect() -> None:
        client.connected = True
        stop.set()

    monkeypatch.setattr(client, "connect", _connect)
    code = slack_socket.run(steward, client=client, stop=stop, once=True, heartbeat_seconds=3600.0)

    assert code == 0
    assert len(client.socket_mode_request_listeners) == 1
    assert len(client.message_listeners) == 1
    assert len(client.on_close_listeners) == 1
    assert client.closed == 1
    assert _source_row(steward, slack_socket.SOURCE_NAME)["config"]["connects"] == 1


# --- the reconciliation half -------------------------------------------------


def test_the_sweep_records_what_it_caught(
    steward: StewardStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`created > 0` on a sweep is, by construction, proof the socket missed something."""
    import httpx

    monkeypatch.setenv(slack_poller.TOKEN_ENV, "xoxb-test")
    event = _message_event(ts="1700000000.000900")

    def handler(request: httpx.Request) -> httpx.Response:
        if "conversations.list" in str(request.url):
            return httpx.Response(
                200, json={"ok": True, "channels": [{"id": CHANNEL, "is_member": True}]}
            )
        return httpx.Response(200, json={"ok": True, "messages": [event], "has_more": False})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    first = slack_poller.poll_once(steward, "slack", client=client)
    assert first["created"] == 1
    config = _source_config(steward, "slack")
    assert config["reconciled_total"] == 1
    assert config["reconciled_last_count"] == 1
    assert config["reconciled_last_at"]
    assert config["last_poll_ts_by_channel"][CHANNEL] == "1700000000.000900"

    # A clean sweep behind a healthy socket reports zero and keeps the total.
    # `reconciled_last_count` is RESET here — which is exactly why the health
    # sentinel watermarks the monotonic `reconciled_total` instead of reading
    # this field (the sweep runs 6x per sentinel interval, so five catches in
    # six would be erased before anyone looked).
    second = slack_poller.poll_once(steward, "slack", client=client)
    assert second["created"] == 0
    config = _source_config(steward, "slack")
    assert config["reconciled_total"] == 1
    assert config["reconciled_last_count"] == 0
    assert config["reconciled_last_at"], "the catch's TIMESTAMP survives a later clean sweep"
    client.close()


def test_the_sweep_skips_the_bots_own_message_but_still_advances_its_cursor(
    steward: StewardStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    import httpx

    monkeypatch.setenv(slack_poller.TOKEN_ENV, "xoxb-test")
    own = _message_event(ts="1700000000.001000", bot_id=BOT_ID, subtype="bot_message")
    own.pop("user")

    def handler(request: httpx.Request) -> httpx.Response:
        if "conversations.list" in str(request.url):
            return httpx.Response(
                200, json={"ok": True, "channels": [{"id": CHANNEL, "is_member": True}]}
            )
        return httpx.Response(200, json={"ok": True, "messages": [own], "has_more": False})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    result = slack_poller.poll_once(steward, "slack", client=client)
    client.close()

    assert result["fetched"] == 1
    assert result["created"] == 0
    assert steward.list_comms_messages(source="slack") == []
    cursors = _source_config(steward, "slack")["last_poll_ts_by_channel"]
    assert cursors[CHANNEL] == "1700000000.001000"


# --- the sweep must be able to COMPLETE A PASS in a real workspace -----------
#
# Every test below is a regression for a defect that made the sweep — the only
# thing that makes ack-first Socket Mode safe — unable to do its job, while the
# socket row stayed green. They are the reason this design is allowed to replace
# a working poller.


def _sweep(
    steward: StewardStore,
    handler: Any,
    *,
    now: Any = None,
    sleep: Any = None,
) -> dict[str, Any]:
    import httpx

    client = httpx.Client(transport=httpx.MockTransport(handler))
    try:
        return slack_poller.poll_once(
            steward,
            "slack",
            client=client,
            **({} if now is None else {"now": now}),
            **({} if sleep is None else {"sleep": sleep}),
        )
    finally:
        client.close()


def test_a_channel_the_bot_is_not_in_is_never_asked_for_history(
    steward: StewardStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`conversations.list` returns the WHOLE workspace, not the bot's channels.

    Live in this workspace that is 23 public channels for 2 memberships. Calling
    `conversations.history` on the other 21 answers `not_in_channel`, and before
    this guard the first one aborted the entire pass — every sweep, forever.
    """
    import httpx

    monkeypatch.setenv(slack_poller.TOKEN_ENV, "xoxb-test")
    asked: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if "conversations.list" in str(request.url):
            return httpx.Response(
                200,
                json={
                    "ok": True,
                    "channels": [
                        {"id": "C0OUTSIDE1", "is_member": False},
                        {"id": "C0OUTSIDE2"},  # field absent entirely
                        {"id": CHANNEL, "is_member": True},
                    ],
                },
            )
        channel = request.url.params.get("channel") or ""
        asked.append(channel)
        if channel != CHANNEL:  # what Slack really answers for a non-member
            return httpx.Response(200, json={"ok": False, "error": "not_in_channel"})
        return httpx.Response(
            200,
            json={"ok": True, "messages": [_message_event(ts="1700000000.002000")]},
        )

    result = _sweep(steward, handler)

    assert asked == [CHANNEL], "history was requested for a channel the bot is not in"
    assert result["status"] == "active"
    assert result["created"] == 1
    assert result["member_channels"] == 1
    assert result["channel_errors"] == 0


def test_the_channel_list_asks_only_for_live_public_channels(
    steward: StewardStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    import httpx

    monkeypatch.setenv(slack_poller.TOKEN_ENV, "xoxb-test")
    seen: list[dict[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if "conversations.list" in str(request.url):
            seen.append(dict(request.url.params))
            return httpx.Response(200, json={"ok": True, "channels": []})
        raise AssertionError("no channels, so no history call may happen")

    _sweep(steward, handler)

    assert seen and seen[0]["types"] == "public_channel"
    assert seen[0]["exclude_archived"] == "true"


def test_one_unreadable_channel_never_stops_the_sweep(
    steward: StewardStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The whole-pass abort was the defect: channel #1 failing meant channels
    2..N were never reconciled, the cursor never moved, and `last_poll_at` was
    never written — while the sentinel's message read like "not deployed yet"."""
    import httpx

    monkeypatch.setenv(slack_poller.TOKEN_ENV, "xoxb-test")

    def handler(request: httpx.Request) -> httpx.Response:
        if "conversations.list" in str(request.url):
            return httpx.Response(
                200,
                json={
                    "ok": True,
                    "channels": [
                        {"id": "C0BROKEN", "is_member": True},
                        {"id": CHANNEL, "is_member": True},
                    ],
                },
            )
        if (request.url.params.get("channel") or "") == "C0BROKEN":
            return httpx.Response(500, json={"ok": False, "error": "internal_error"})
        return httpx.Response(
            200, json={"ok": True, "messages": [_message_event(ts="1700000000.003000")]}
        )

    result = _sweep(steward, handler)

    assert result["status"] == "active"
    assert result["created"] == 1, "the healthy channel was still reconciled"
    assert result["channel_errors"] == 1
    row = _source_row(steward, "slack")
    assert row["last_poll_at"], "the sweep ran, and the sentinel must be able to see that"
    assert "C0BROKEN" in row["last_error"]
    config = row["config"]
    assert config["channel_error_count"] == 1
    assert config["channels_swept"] == 1 and config["member_channels"] == 2
    # The failed channel keeps the window it was reading; the healthy channel's
    # progress cannot skip past it.
    assert config["last_poll_ts_by_channel"]["C0BROKEN"] == "0"
    assert config["last_poll_ts_by_channel"][CHANNEL] == "1700000000.003000"


def test_a_pass_where_every_member_channel_fails_advances_nothing(
    steward: StewardStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    import httpx

    monkeypatch.setenv(slack_poller.TOKEN_ENV, "xoxb-test")

    def handler(request: httpx.Request) -> httpx.Response:
        if "conversations.list" in str(request.url):
            return httpx.Response(
                200, json={"ok": True, "channels": [{"id": CHANNEL, "is_member": True}]}
            )
        return httpx.Response(503, json={"ok": False, "error": "service_unavailable"})

    result = _sweep(steward, handler)

    assert result["status"] == "error"
    row = _source_row(steward, "slack")
    assert not row["last_poll_at"], (
        "nothing was reconciled, so the sweep-freshness signal must NOT be refreshed"
    )
    assert "last_poll_ts_by_channel" not in (row["config"] or {})


def test_a_message_that_lands_during_the_pass_is_not_skipped_by_the_cursor(
    steward: StewardStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Reproduces the silent permanent loss a single global `max(ts)` cursor caused.

    C1 is read (empty) -> a message lands in C1 -> C2 is read and returns a
    HIGHER ts -> the global cursor jumps past C1's message, which is then below
    the cursor forever. Under the per-channel cursor plus the pass-start clamp,
    the next pass re-reads it.
    """
    import httpx

    monkeypatch.setenv(slack_poller.TOKEN_ENV, "xoxb-test")
    late = _message_event(ts="1700000100.500000", text="posted to C1 mid-pass")
    later_elsewhere = _message_event(ts="1700000100.900000", text="in C2")
    c1_messages: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if "conversations.list" in str(request.url):
            return httpx.Response(
                200,
                json={
                    "ok": True,
                    "channels": [
                        {"id": "C1", "is_member": True},
                        {"id": "C2", "is_member": True},
                    ],
                },
            )
        channel = request.url.params.get("channel") or ""
        oldest = float(request.url.params.get("oldest") or 0.0)
        if channel == "C1":
            body = [event for event in c1_messages if float(event["ts"]) > oldest]
            if not c1_messages:
                # ...and the message arrives the instant after C1 was read.
                c1_messages.append(late)
            return httpx.Response(200, json={"ok": True, "messages": body})
        return httpx.Response(
            200,
            json={
                "ok": True,
                "messages": [later_elsewhere] if float(later_elsewhere["ts"]) > oldest else [],
            },
        )

    # A clock pinned BELOW both timestamps proves the clamp itself: everything
    # observed above `pass_started` must be re-read next pass.
    first = _sweep(steward, handler, now=lambda: 1700000100.000000)
    assert first["created"] == 1
    assert {row["body_text"] for row in steward.list_comms_messages(source="slack")} == {"in C2"}

    second = _sweep(steward, handler, now=lambda: 1700000200.000000)
    bodies = {row["body_text"] for row in steward.list_comms_messages(source="slack")}
    assert "posted to C1 mid-pass" in bodies, "the mid-pass message was lost forever"
    assert second["created"] == 1, "and it is reported as a message the socket missed"


def test_a_channel_that_failed_keeps_its_own_window_while_the_others_advance(
    steward: StewardStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The reason the cursor had to become per channel once per-channel failures
    stopped being fatal.

    With one global cursor, a channel that could not be read this pass has its
    window skipped past by every OTHER channel's progress — the non-fatal error
    handling would have quietly manufactured the very loss it was added to
    prevent. Here C0BROKEN's message predates the pass that failed on it, so it
    is only ever recovered if that channel's own boundary was pinned.
    """
    import httpx

    monkeypatch.setenv(slack_poller.TOKEN_ENV, "xoxb-test")
    broken = {"down": True}
    old_message = _message_event(ts="999.500000", text="posted before the failing pass")

    def handler(request: httpx.Request) -> httpx.Response:
        if "conversations.list" in str(request.url):
            return httpx.Response(
                200,
                json={
                    "ok": True,
                    "channels": [
                        {"id": "C0BROKEN", "is_member": True},
                        {"id": "C0FINE", "is_member": True},
                    ],
                },
            )
        channel = request.url.params.get("channel") or ""
        oldest = float(request.url.params.get("oldest") or 0.0)
        if channel == "C0BROKEN":
            if broken["down"]:
                return httpx.Response(500, json={"ok": False, "error": "internal_error"})
            body = [event for event in [old_message] if float(event["ts"]) > oldest]
            return httpx.Response(200, json={"ok": True, "messages": body})
        healthy = _message_event(ts="999.900000", text="fine")
        return httpx.Response(
            200,
            json={"ok": True, "messages": [healthy] if float(healthy["ts"]) > oldest else []},
        )

    first = _sweep(steward, handler, now=lambda: 1000.0)
    assert first["channel_errors"] == 1 and first["created"] == 1

    broken["down"] = False
    second = _sweep(steward, handler, now=lambda: 2000.0)

    bodies = {row["body_text"] for row in steward.list_comms_messages(source="slack")}
    assert "posted before the failing pass" in bodies, (
        "the failed channel's window was skipped past by the healthy channel's progress"
    )
    assert second["created"] == 1


def test_the_stored_cursor_never_passes_the_moment_the_pass_started(
    steward: StewardStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The second guard: a cursor may never move above ``pass_started``.

    A sweep walks N channels over several seconds of HTTP. Anything Slack made
    visible DURING that walk — a message still in flight when its channel was
    read, then returned on a later channel's request or simply visible a moment
    later — sits inside the window the pass already believes it covered. Clamping
    to the pass start means that window is re-read next time and absorbed for
    free by INSERT OR IGNORE. Without the clamp it is below the cursor forever.
    """
    import httpx

    monkeypatch.setenv(slack_poller.TOKEN_ENV, "xoxb-test")
    pass_one_started = 1700000000.000000
    # Read during pass 1 (stamped AFTER the pass began — it landed mid-walk).
    visible: list[dict[str, Any]] = [_message_event(ts="1700000005.000000", text="mid-walk")]
    # Was in flight when the channel was read; the API returns it a moment later.
    delayed = _message_event(ts="1700000002.000000", text="in flight during pass 1")

    def handler(request: httpx.Request) -> httpx.Response:
        if "conversations.list" in str(request.url):
            return httpx.Response(
                200, json={"ok": True, "channels": [{"id": CHANNEL, "is_member": True}]}
            )
        # A faithful `oldest`: Slack never returns anything at or below it.
        oldest = float(request.url.params.get("oldest") or 0.0)
        return httpx.Response(
            200,
            json={
                "ok": True,
                "messages": [event for event in visible if float(event["ts"]) > oldest],
            },
        )

    first = _sweep(steward, handler, now=lambda: pass_one_started)
    assert first["created"] == 1
    cursor = _source_config(steward, "slack")["last_poll_ts_by_channel"][CHANNEL]
    assert cursor == "1700000000.000000", "the cursor claimed a window the pass never covered"

    visible.append(delayed)
    second = _sweep(steward, handler, now=lambda: 1700000100.000000)

    bodies = {row["body_text"] for row in steward.list_comms_messages(source="slack")}
    assert "in flight during pass 1" in bodies, "a message inside the pass window was lost forever"
    assert second["created"] == 1


def test_a_rate_limited_request_is_retried_after_the_slack_supplied_delay(
    steward: StewardStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A 429 must not abort the pass. Slack's 2025 tiers make 429 ordinary."""
    import httpx

    monkeypatch.setenv(slack_poller.TOKEN_ENV, "xoxb-test")
    slept: list[float] = []
    calls = {"history": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if "conversations.list" in str(request.url):
            return httpx.Response(
                200, json={"ok": True, "channels": [{"id": CHANNEL, "is_member": True}]}
            )
        calls["history"] += 1
        if calls["history"] == 1:
            return httpx.Response(429, headers={"Retry-After": "7"}, json={"ok": False})
        return httpx.Response(
            200, json={"ok": True, "messages": [_message_event(ts="1700000000.004000")]}
        )

    result = _sweep(steward, handler, sleep=slept.append)

    assert slept == [7.0], "Slack's own Retry-After must be honoured, not a guess"
    assert result["status"] == "active" and result["created"] == 1


def test_a_two_hundred_with_ratelimited_is_also_retried(
    steward: StewardStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    import httpx

    monkeypatch.setenv(slack_poller.TOKEN_ENV, "xoxb-test")
    slept: list[float] = []
    calls = {"list": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if "conversations.list" in str(request.url):
            calls["list"] += 1
            if calls["list"] == 1:
                return httpx.Response(200, json={"ok": False, "error": "ratelimited"})
            return httpx.Response(
                200, json={"ok": True, "channels": [{"id": CHANNEL, "is_member": True}]}
            )
        return httpx.Response(200, json={"ok": True, "messages": []})

    result = _sweep(steward, handler, sleep=slept.append)

    assert slept == [1.0], "no Retry-After header -> a bounded default, still not a raise"
    assert result["status"] == "active"


def test_a_channel_that_stays_rate_limited_degrades_to_a_counter(
    steward: StewardStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    import httpx

    monkeypatch.setenv(slack_poller.TOKEN_ENV, "xoxb-test")
    slept: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if "conversations.list" in str(request.url):
            return httpx.Response(
                200,
                json={
                    "ok": True,
                    "channels": [
                        {"id": "C0THROTTLED", "is_member": True},
                        {"id": CHANNEL, "is_member": True},
                    ],
                },
            )
        if (request.url.params.get("channel") or "") == "C0THROTTLED":
            return httpx.Response(429, headers={"Retry-After": "3"}, json={"ok": False})
        return httpx.Response(
            200, json={"ok": True, "messages": [_message_event(ts="1700000000.005000")]}
        )

    result = _sweep(steward, handler, sleep=slept.append)

    assert len(slept) == slack_poller.RATE_LIMIT_RETRIES, "retries are BOUNDED per request"
    assert result["status"] == "active"
    assert result["channel_errors"] == 1 and result["created"] == 1
    assert "ratelimited" in _source_row(steward, "slack")["last_error"]


def test_the_pass_rate_limit_sleep_budget_is_bounded(
    steward: StewardStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A launchd sweep must never pin a process for minutes honouring 429s."""
    import httpx

    monkeypatch.setenv(slack_poller.TOKEN_ENV, "xoxb-test")
    slept: list[float] = []
    channels = [{"id": f"C{index:04d}", "is_member": True} for index in range(40)]

    def handler(request: httpx.Request) -> httpx.Response:
        if "conversations.list" in str(request.url):
            return httpx.Response(200, json={"ok": True, "channels": channels})
        return httpx.Response(429, headers={"Retry-After": "60"}, json={"ok": False})

    result = _sweep(steward, handler, sleep=slept.append)

    assert sum(slept) <= slack_poller.RATE_LIMIT_PASS_BUDGET_SECONDS
    assert result["status"] == "error", "no channel was readable, so nothing may advance"


def test_a_channel_that_disappears_from_the_list_loses_its_cursor_entry(
    steward: StewardStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The per-channel blob is bounded by the workspace, not by history."""
    import httpx

    monkeypatch.setenv(slack_poller.TOKEN_ENV, "xoxb-test")
    listing = {"channels": [{"id": "C1", "is_member": True}, {"id": "C2", "is_member": True}]}

    def handler(request: httpx.Request) -> httpx.Response:
        if "conversations.list" in str(request.url):
            return httpx.Response(200, json={"ok": True, **listing})
        return httpx.Response(200, json={"ok": True, "messages": []})

    _sweep(steward, handler)
    assert set(_source_config(steward, "slack")["last_poll_ts_by_channel"]) == {"C1", "C2"}

    listing["channels"] = [{"id": "C1", "is_member": True}]
    _sweep(steward, handler)
    assert set(_source_config(steward, "slack")["last_poll_ts_by_channel"]) == {"C1"}


def test_seeding_the_rollout_cursor_never_erases_the_reconciliation_record(
    steward: StewardStore,
) -> None:
    """`upsert_comms_source` REPLACES config wholesale, so the ad-hoc one-liner
    the installer used to print destroyed `reconciled_total` and every
    per-channel cursor on a re-run — the only durable evidence that the socket
    has ever missed anything."""
    from omniagentos.comms import poll as comms_poll

    steward.upsert_comms_source(
        "slack",
        "slack",
        config={
            "last_poll_ts": "1.0",
            "reconciled_total": 4,
            "last_poll_ts_by_channel": {CHANNEL: "1700000000.000001"},
        },
    )

    result = comms_poll.seed_cursor(steward, "slack", at=1800000000.0)

    assert result["seeded_last_poll_ts"] == "1800000000.000000"
    config = _source_config(steward, "slack")
    assert config["last_poll_ts"] == "1800000000.000000"
    assert config["reconciled_total"] == 4
    assert config["last_poll_ts_by_channel"] == {CHANNEL: "1700000000.000001"}


# --- one heartbeat row must represent ONE moment -----------------------------


def test_no_thread_mutates_the_published_state_without_holding_the_lock(
    steward: StewardStore,
) -> None:
    """The deterministic form of the race below.

    While ANOTHER thread holds the lock, a publisher must be unable to change
    the status or the error at all. If it can, then its update and its read-back
    are two separate moments and a third thread can be interleaved between them.
    """
    import time as _time

    ingest = _ingest(steward)
    assert ingest._status == "pending_setup"

    ingest._lock.acquire()
    publisher = threading.Thread(
        target=lambda: ingest.publish("error", last_error="database is locked"), daemon=True
    )
    try:
        publisher.start()
        _time.sleep(0.25)  # ample time for an unlocked write to land
        assert ingest._status == "pending_setup", "status was mutated outside the lock"
        assert ingest._last_error == "", "last_error was mutated outside the lock"
    finally:
        ingest._lock.release()

    publisher.join(timeout=5.0)
    assert not publisher.is_alive()
    row = _source_row(steward, slack_socket.SOURCE_NAME)
    assert row["status"] == "error" and "database is locked" in row["last_error"]


def test_the_published_status_and_error_can_never_come_from_different_moments(
    steward: StewardStore,
) -> None:
    """Three threads write this state: the heartbeat timer, the SDK's listener
    workers, and the supervisor. Applying an update and then re-reading the
    fields outside the lock lets one thread's status pair with another thread's
    error — an ``active`` row carrying a live error string, or ``error`` with
    none. Either way the operator is told something that was never true.
    """
    import sys

    seen: list[tuple[str, str]] = []
    lock = threading.Lock()

    def _capture(name: str, kind: str, **fields: Any) -> dict[str, Any]:
        with lock:
            seen.append((str(fields.get("status")), str(fields.get("last_error"))))
        return {}

    ingest = _ingest(steward)
    ingest._steward = type("S", (), {"upsert_comms_source": staticmethod(_capture)})()  # type: ignore[assignment]

    valid = {("active", ""), ("error", "database is locked")}
    barrier = threading.Barrier(2)

    def _flap(status: str, message: str) -> None:
        barrier.wait()
        for _ in range(2000):
            ingest.publish(status, last_error=message)

    previous = sys.getswitchinterval()
    sys.setswitchinterval(1e-6)  # maximise the interleaving this test hunts for
    try:
        threads = [
            threading.Thread(target=_flap, args=("active", "")),
            threading.Thread(target=_flap, args=("error", "database is locked")),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30.0)
    finally:
        sys.setswitchinterval(previous)

    assert len(seen) == 4000
    torn = [pair for pair in seen if pair not in valid]
    assert torn == [], f"a heartbeat row mixed two moments: {torn[:5]}"


def test_a_transient_store_failure_heals_on_the_next_successful_write(
    steward: StewardStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One `database is locked` at 03:00 must not pin status=error until the next
    connection handshake — Slack cycles roughly hourly, so the sentinel would
    alarm for an hour about a fault that lasted a millisecond, and an operator
    who learns to ignore THAT alarm ignores the reconciliation signal too."""
    calls = {"n": 0}
    real_insert = steward.insert_comms_message

    def _flaky(msg: dict[str, Any]) -> tuple[dict[str, Any], bool]:
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("database is locked")
        return real_insert(msg)

    monkeypatch.setattr(steward, "insert_comms_message", _flaky)
    ingest = _ingest(steward)

    ingest.ingest(_envelope(_message_event(ts="1700000000.010000")))
    row = _source_row(steward, slack_socket.SOURCE_NAME)
    assert row["status"] == "error" and "database is locked" in row["last_error"]

    ingest.ingest(_envelope(_message_event(ts="1700000000.010001"), event_id="Ev2"))
    row = _source_row(steward, slack_socket.SOURCE_NAME)
    assert row["status"] == "active", "a completed write is evidence the store works again"
    assert row["last_error"] == ""
    # ...and the fact that it happened at all survives as a counter, which is
    # what the sentinel watermarks. A latched status cannot express a RATE.
    assert row["config"]["store_failures"] == 1


def test_a_slow_store_write_is_counted_for_the_sentinel(
    steward: StewardStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`store_latency_ms_max` never decays, so it latches; the sentinel watches
    the monotonic COUNT of slow writes instead and compares it to a watermark."""
    real_insert = steward.insert_comms_message
    clock = {"t": 0.0}

    def _slow(msg: dict[str, Any]) -> tuple[dict[str, Any], bool]:
        clock["t"] += slack_socket.SLOW_WRITE_MS / 1000.0
        return real_insert(msg)

    monkeypatch.setattr(steward, "insert_comms_message", _slow)
    monkeypatch.setattr(slack_socket.time, "monotonic", lambda: clock["t"])
    ingest = _ingest(steward)
    ingest.ingest(_envelope(_message_event(ts="1700000000.011000")))
    ingest.publish("active")

    config = _source_config(steward, slack_socket.SOURCE_NAME)
    assert config["store_slow_writes"] == 1
    assert config["store_latency_ms_max"] >= slack_socket.SLOW_WRITE_MS


def test_an_ingest_error_can_never_carry_a_token_shaped_string(
    steward: StewardStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _explode(msg: dict[str, Any]) -> tuple[dict[str, Any], bool]:
        raise RuntimeError("upstream said xoxb-9999-SECRET and xapp-1-A-2-SECRET")

    monkeypatch.setattr(steward, "insert_comms_message", _explode)
    ingest = _ingest(steward)
    ingest.ingest(_envelope(_message_event()))

    last_error = _source_row(steward, slack_socket.SOURCE_NAME)["last_error"]
    assert "SECRET" not in last_error and "[REDACTED]" in last_error


# --- the credential path must not audit itself into the ground ---------------


class _RecordingStop(threading.Event):
    """Records every wait duration, then ends the loop, so a retry CADENCE is
    testable without actually sleeping for it."""

    def __init__(self, stop_after: int = 2) -> None:
        super().__init__()
        self.waits: list[float | None] = []
        self._stop_after = stop_after

    def wait(self, timeout: float | None = None) -> bool:  # type: ignore[override]
        self.waits.append(timeout)
        if len(self.waits) >= self._stop_after:
            self.set()
        return super().wait(0)


def test_credentials_are_resolved_once_across_repeated_reconnects(
    steward: StewardStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every `resolve_for` writes a durable broker intent+finalization audit
    PAIR. Re-resolving on each reconnect turns one bad night into thousands of
    rows in the same SQLite file the ingest path writes to."""
    resolutions: list[int] = []

    def _resolve(audit_store: Any = None) -> tuple[str, str]:
        resolutions.append(1)
        return ("xapp-token", "xoxb-token")

    monkeypatch.setattr(slack_socket, "_resolve_tokens", _resolve)
    monkeypatch.setattr(slack_socket, "_backoff_delay", lambda *a, **k: 0.0)
    stop = threading.Event()
    client = _FakeClient()
    attempts = {"n": 0}

    def _connect() -> None:
        attempts["n"] += 1
        if attempts["n"] >= 3:
            stop.set()
        raise RuntimeError("transport blip")

    monkeypatch.setattr(client, "connect", _connect)
    slack_socket.run(steward, client=client, stop=stop, heartbeat_seconds=3600.0)

    assert attempts["n"] == 3
    assert len(resolutions) == 1, "cached across reconnects, not re-audited each time"


def test_an_auth_failure_invalidates_the_cached_credentials(
    steward: StewardStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Caching must not outlive the one failure class it cannot survive."""
    resolutions: list[int] = []
    monkeypatch.setattr(
        slack_socket,
        "_resolve_tokens",
        lambda audit_store=None: (resolutions.append(1), ("xapp-token", "xoxb-token"))[1],
    )
    monkeypatch.setattr(slack_socket, "_backoff_delay", lambda *a, **k: 0.0)
    stop = threading.Event()
    client = _FakeClient()
    attempts = {"n": 0}

    def _connect() -> None:
        attempts["n"] += 1
        if attempts["n"] >= 3:
            stop.set()
        raise RuntimeError("invalid_auth")

    monkeypatch.setattr(client, "connect", _connect)
    slack_socket.run(steward, client=client, stop=stop, heartbeat_seconds=3600.0)

    assert len(resolutions) == 3, "a revoked token must be re-resolved, not held for an hour"


def test_an_unprovisioned_credential_is_retried_on_the_slow_clock(
    steward: StewardStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The denial path has unlimited retries. On the connect backoff that is two
    audit rows plus an ERROR log line every 60s, forever."""

    def _denied(audit_store: Any = None) -> tuple[str, str]:
        raise BrokerDenied("credential_missing", "SLACK_APP_TOKEN", "not provisioned")

    monkeypatch.setattr(slack_socket, "_resolve_tokens", _denied)
    monkeypatch.setattr(slack_socket, "_backoff_delay", lambda *a, **k: 0.5)
    stop = _RecordingStop(stop_after=2)

    slack_socket.run(steward, client=_FakeClient(), stop=stop, heartbeat_seconds=3600.0)

    waits = [wait for wait in stop.waits if wait is not None]
    assert waits, "the denial path must wait between attempts"
    assert min(waits) >= slack_socket.CREDENTIAL_RETRY_SECONDS
    assert _source_row(steward, slack_socket.SOURCE_NAME)["status"] == "pending_setup"


def test_a_repeated_denial_is_logged_once_and_the_row_stays_current(
    steward: StewardStore, caplog: pytest.LogCaptureFixture
) -> None:
    ingest = _ingest(steward)
    with caplog.at_level("ERROR", logger=slack_socket.logger.name):
        for _ in range(5):
            ingest.mark_error("pending_setup", "slack socket refusing to start: credential_missing")

    assert len(caplog.records) == 1, "an unrotated launchd log must not fill with one sentence"
    row = _source_row(steward, slack_socket.SOURCE_NAME)
    assert row["status"] == "pending_setup"
    assert "credential_missing" in row["last_error"], "the row the sentinel reads stays current"


def test_the_supervisor_carries_the_last_close_reason_into_its_error(
    steward: StewardStore,
) -> None:
    """"not reconnected within 30s" alone sends an operator to another log to
    find out whether this was auth, a rate limit, or a dead network."""
    ingest = _ingest(steward)
    ingest.mark_connected()
    ingest.on_close(1006, "abnormal closure")
    ingest.mark_disconnected(
        f"supervisor: not reconnected within 30s (last close: {ingest.last_disconnect_reason()})",
        status="error",
    )

    last_error = _source_row(steward, slack_socket.SOURCE_NAME)["last_error"]
    assert "abnormal closure" in last_error and "not reconnected" in last_error


def test_the_slack_sdk_logger_is_pinned_so_verbosity_cannot_leak_a_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """At DEBUG, `WebClient.api_call` logs the apps.connections.open request —
    which carries SLACK_APP_TOKEN — into the plist's StandardOutPath."""
    import logging

    monkeypatch.setattr(slack_socket, "run", lambda **kwargs: 0)
    logging.getLogger("slack_sdk").setLevel(logging.DEBUG)
    try:
        slack_socket.main([])
        assert logging.getLogger("slack_sdk").level == logging.INFO
    finally:
        logging.getLogger("slack_sdk").setLevel(logging.NOTSET)
