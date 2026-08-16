"""Poller unit tests with mocked transports — no real network, one per source format."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx
import pytest

from omniagentos.comms.pollers import imap as imap_poller
from omniagentos.comms.pollers import slack as slack_poller
from omniagentos.comms.pollers import telegram as telegram_poller
from omniagentos.db.store import SqliteStore
from omniagentos.steward.store import StewardStore
from tests.support.db_template import make_store


@pytest.fixture
def steward(tmp_path: Path) -> StewardStore:
    return StewardStore(make_store(SqliteStore, tmp_path / "pollers.db"))


# --- Telegram ----------------------------------------------------------------


def test_telegram_missing_token_is_pending_setup_and_never_crashes(
    steward: StewardStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(telegram_poller.TOKEN_ENV, raising=False)
    result = telegram_poller.poll_once(steward, "telegram")
    assert result["status"] == "pending_setup"
    assert telegram_poller.TOKEN_ENV in result["error"]
    source = steward.list_comms_sources()[0]
    assert source["status"] == "pending_setup"
    assert telegram_poller.TOKEN_ENV in source["last_error"]


def test_telegram_poll_normalizes_and_advances_offset(
    steward: StewardStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(telegram_poller.TOKEN_ENV, "fake-token")

    def handler(request: httpx.Request) -> httpx.Response:
        assert "getUpdates" in str(request.url)
        return httpx.Response(
            200,
            json={
                "ok": True,
                "result": [
                    {
                        "update_id": 5,
                        "message": {
                            "message_id": 1,
                            "date": 1_700_000_000,
                            "chat": {"id": 42},
                            "from": {"username": "alice"},
                            "text": "hello",
                        },
                    }
                ],
            },
        )

    client = httpx.Client(transport=httpx.MockTransport(handler))
    result = telegram_poller.poll_once(steward, "telegram", client=client)
    assert result == {
        "source": "telegram",
        "status": "active",
        "error": "",
        "fetched": 1,
        "created": 1,
    }
    stored = steward.list_comms_messages(source="telegram")
    assert stored[0]["sender"] == "alice" and stored[0]["body_text"] == "hello"
    source = steward.list_comms_sources()[0]
    assert source["config"] == {"offset": 6}

    # Re-poll: same update_id would dedupe if the server replayed it; simulate the
    # bot API since offset 6 correctly returning nothing new.
    empty_client = httpx.Client(
        transport=httpx.MockTransport(
            lambda _r: httpx.Response(200, json={"ok": True, "result": []})
        )
    )
    second = telegram_poller.poll_once(steward, "telegram", client=empty_client)
    assert second["fetched"] == 0 and second["created"] == 0


def test_telegram_api_error_sets_error_status(
    steward: StewardStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(telegram_poller.TOKEN_ENV, "fake-token")
    client = httpx.Client(
        transport=httpx.MockTransport(
            lambda _r: httpx.Response(200, json={"ok": False, "description": "boom"})
        )
    )
    result = telegram_poller.poll_once(steward, "telegram", client=client)
    assert result["status"] == "error"
    assert "boom" in result["error"]


def test_telegram_non_2xx_error_never_leaks_bot_token(
    steward: StewardStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """H7: the token lives in the request PATH (`/bot<token>/getUpdates`), so a
    real ``httpx.HTTPStatusError`` from a non-2xx response embeds the full URL
    (including the token) in its own ``str()``. ``last_error`` — served by the
    /comms API + dashboard — must never contain it.
    """
    token = "123456789:AA-super-secret-telegram-bot-token"
    monkeypatch.setenv(telegram_poller.TOKEN_ENV, token)

    def handler(request: httpx.Request) -> httpx.Response:
        assert token in str(request.url)  # sanity: the token really is in the URL
        return httpx.Response(401, json={"ok": False, "description": "Unauthorized"})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    result = telegram_poller.poll_once(steward, "telegram", client=client)
    assert result["status"] == "error"
    assert token not in result["error"]
    assert "api.telegram.org" not in result["error"]
    assert result["error"] == "telegram poll failed: Telegram API error (HTTP 401)"
    source = steward.list_comms_sources()[0]
    assert token not in source["last_error"]
    assert source["last_error"] == result["error"]


# --- Slack ---------------------------------------------------------------------


def test_slack_missing_token_is_pending_setup(
    steward: StewardStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(slack_poller.TOKEN_ENV, raising=False)
    result = slack_poller.poll_once(steward, "slack")
    assert result["status"] == "pending_setup"
    assert slack_poller.TOKEN_ENV in result["error"]


def test_slack_poll_fetches_history_across_channels(
    steward: StewardStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(slack_poller.TOKEN_ENV, "xoxb-fake")

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        assert request.headers["authorization"] == "Bearer xoxb-fake"
        if "conversations.list" in url:
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
        assert "conversations.history" in url
        channel = request.url.params.get("channel")
        if channel == "C1":
            return httpx.Response(
                200,
                json={
                    "ok": True,
                    "messages": [{"user": "U1", "text": "hi", "ts": "1700000000.0001"}],
                },
            )
        return httpx.Response(200, json={"ok": True, "messages": []})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    result = slack_poller.poll_once(steward, "slack", client=client)
    assert result["status"] == "active" and result["fetched"] == 1 and result["created"] == 1
    stored = steward.list_comms_messages(source="slack")
    assert stored[0]["thread_id"] == "C1" and stored[0]["body_text"] == "hi"
    source = steward.list_comms_sources()[0]
    # The message cursor is PER CHANNEL. `last_poll_ts` is now only the floor for
    # a channel this sweep has never read, so it tracks the pass start, not a ts.
    assert source["config"]["last_poll_ts_by_channel"] == {"C1": "1700000000.0001", "C2": "0"}


def test_slack_poll_follows_pagination_across_channel_list_and_history(
    steward: StewardStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """M2: both ``conversations.list`` (channels) AND ``conversations.history``
    (messages) can each span multiple pages (``response_metadata.next_cursor`` /
    ``has_more``) — every page must be followed, or channels/messages past the
    first page are silently dropped.
    """
    monkeypatch.setenv(slack_poller.TOKEN_ENV, "xoxb-fake")
    seen_history_oldest: set[str] = set()

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        params = request.url.params
        if "conversations.list" in url:
            cursor = params.get("cursor")
            if not cursor:
                return httpx.Response(
                    200,
                    json={
                        "ok": True,
                        "channels": [{"id": "C1", "is_member": True}],
                        "response_metadata": {"next_cursor": "list-page-2"},
                    },
                )
            assert cursor == "list-page-2"
            return httpx.Response(
                200,
                json={
                    "ok": True,
                    "channels": [{"id": "C2", "is_member": True}],
                    "response_metadata": {"next_cursor": ""},
                },
            )

        assert "conversations.history" in url
        channel = params.get("channel")
        seen_history_oldest.add(params.get("oldest") or "")
        if channel == "C1":
            cursor = params.get("cursor")
            if not cursor:
                # page 1: newest message first, more (older, still > since) pages available
                return httpx.Response(
                    200,
                    json={
                        "ok": True,
                        "messages": [{"user": "U1", "text": "page1", "ts": "1700000002.0001"}],
                        "has_more": True,
                        "response_metadata": {"next_cursor": "hist-page-2"},
                    },
                )
            assert cursor == "hist-page-2"
            return httpx.Response(
                200,
                json={
                    "ok": True,
                    "messages": [{"user": "U1", "text": "page2", "ts": "1700000001.0001"}],
                    "has_more": False,
                    "response_metadata": {"next_cursor": ""},
                },
            )
        return httpx.Response(200, json={"ok": True, "messages": [], "has_more": False})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    result = slack_poller.poll_once(steward, "slack", client=client)
    assert result["status"] == "active"
    assert result["fetched"] == 2 and result["created"] == 2
    stored = {row["body_text"] for row in steward.list_comms_messages(source="slack")}
    assert stored == {"page1", "page2"}  # BOTH history pages landed, not just page 1
    # every history page for C1 carried the SAME `oldest` boundary — pagination
    # never widens the requested time window.
    assert seen_history_oldest == {"0"}
    source = steward.list_comms_sources()[0]
    # newest ts seen across BOTH pages, on C1's own cursor
    assert source["config"]["last_poll_ts_by_channel"]["C1"] == "1700000002.0001"


def test_slack_api_error_sets_error_status(
    steward: StewardStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(slack_poller.TOKEN_ENV, "xoxb-fake")
    client = httpx.Client(
        transport=httpx.MockTransport(
            lambda _r: httpx.Response(200, json={"ok": False, "error": "invalid_auth"})
        )
    )
    result = slack_poller.poll_once(steward, "slack", client=client)
    assert result["status"] == "error"
    assert "invalid_auth" in result["error"]


# --- IMAP ------------------------------------------------------------------------


class _FakeImapConnection:
    """Mimics the imaplib.IMAP4_SSL UID subset our poller uses (UID SEARCH / UID
    FETCH), backed by a small in-memory mailbox of UID -> synthesized message.
    """

    def __init__(self, host: str, *, uids: tuple[int, ...] = (1, 2)) -> None:
        self.host = host
        self.logged_in: tuple[str, str] | None = None
        self.selected: str | None = None
        self.available_uids = list(uids)
        self.fetch_specs: list[str] = []
        self.fetched_uids: list[int] = []

    def __enter__(self) -> _FakeImapConnection:
        return self

    def __exit__(self, *exc_info: object) -> None:
        return None

    def login(self, user: str, password: str) -> None:
        self.logged_in = (user, password)

    def select(self, mailbox: str) -> None:
        self.selected = mailbox

    def uid(self, command: str, *args: Any) -> tuple[str, list[Any]]:
        if command == "search":
            _charset, criteria = args
            assert criteria.startswith("UID ") and ":*" in criteria
            low = int(criteria[len("UID ") : criteria.index(":")])
            matching = [uid for uid in self.available_uids if uid >= low]
            return (
                ("OK", [b" ".join(str(uid).encode() for uid in matching)])
                if matching
                else ("OK", [b""])
            )
        if command == "fetch":
            message_set, parts = args
            self.fetch_specs.append(parts)
            uid = int(message_set)
            self.fetched_uids.append(uid)
            raw = (
                b"Message-ID: <msg-" + str(uid).encode() + b"@example.com>\r\n"
                b"From: sender@example.com\r\n"
                b"Subject: Test\r\n\r\nBody " + str(uid).encode()
            )
            return "OK", [(f"{uid} (BODY[] {{123}}".encode(), raw)]
        raise AssertionError(f"unexpected IMAP UID command: {command!r}")


class _FailOnceStore:
    """Wraps a REAL StewardStore so ``insert_comms_message`` raises exactly once
    for one external_id (simulating a transient DB error on one message), then
    delegates everything — including subsequent calls — to the real store.
    """

    def __init__(self, steward: StewardStore, *, fail_external_id: str) -> None:
        self._steward = steward
        self._fail_external_id = fail_external_id
        self._raised = False

    def insert_comms_message(self, msg: dict[str, Any]) -> tuple[dict[str, Any], bool]:
        if not self._raised and msg.get("external_id") == self._fail_external_id:
            self._raised = True
            raise RuntimeError("simulated transient store failure")
        return self._steward.insert_comms_message(msg)

    def __getattr__(self, item: str) -> Any:
        return getattr(self._steward, item)


def test_imap_missing_env_is_pending_setup_and_persists_config(
    steward: StewardStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("IMAP_HOST_GMAIL", raising=False)
    monkeypatch.delenv("IMAP_USER_GMAIL", raising=False)
    monkeypatch.delenv("IMAP_PASSWORD_GMAIL", raising=False)
    result = imap_poller.poll_once(steward, "gmail")
    assert result["status"] == "pending_setup"
    assert "IMAP_HOST_GMAIL" in result["error"]
    source = steward.list_comms_sources()[0]
    assert source["config"] == {
        "host_env": "IMAP_HOST_GMAIL",
        "user_env": "IMAP_USER_GMAIL",
        "password_env": "IMAP_PASSWORD_GMAIL",
    }


def test_imap_poll_uses_body_peek_and_advances_uid_cursor(
    steward: StewardStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """H8: fetching must use BODY.PEEK[] (never RFC822, which would mark the
    operator's mail \\Seen as a side effect of us merely reading it), and the
    durable UID cursor — not \\Seen state — is what prevents a re-fetch.
    """
    monkeypatch.setenv("IMAP_HOST_GMAIL", "imap.example.com")
    monkeypatch.setenv("IMAP_USER_GMAIL", "user@example.com")
    monkeypatch.setenv("IMAP_PASSWORD_GMAIL", "app-password")

    connection = _FakeImapConnection("imap.example.com")
    result = imap_poller.poll_once(steward, "gmail", connection_factory=lambda host: connection)
    assert result == {
        "source": "gmail",
        "status": "active",
        "error": "",
        "fetched": 2,
        "created": 2,
    }
    assert connection.fetch_specs == ["(BODY.PEEK[])", "(BODY.PEEK[])"]
    # EDC F01: ingest now stamps the PER-MAILBOX source (the poller name, here
    # 'gmail') rather than a shared 'imap', so two owners' mail with the same
    # Message-ID can never collide on UNIQUE(source, external_id).
    stored = steward.list_comms_messages(source="gmail")
    assert {row["external_id"] for row in stored} == {"<msg-1@example.com>", "<msg-2@example.com>"}
    source = steward.list_comms_sources()[0]
    assert source["config"]["uid_cursor"] == 2

    # Re-poll: the fake server would happily return the same two UIDs again (a
    # real IMAP server wouldn't lie like this, but BODY.PEEK never marks
    # anything seen so it COULD) — the durable cursor is what stops the refetch.
    second_connection = _FakeImapConnection("imap.example.com")
    second = imap_poller.poll_once(
        steward, "gmail", connection_factory=lambda host: second_connection
    )
    assert second == {
        "source": "gmail",
        "status": "active",
        "error": "",
        "fetched": 0,
        "created": 0,
    }
    assert second_connection.fetch_specs == []


def test_imap_message_id_dedupe_survives_a_cursor_reset(
    steward: StewardStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("IMAP_HOST_GMAIL", "imap.example.com")
    monkeypatch.setenv("IMAP_USER_GMAIL", "user@example.com")
    monkeypatch.setenv("IMAP_PASSWORD_GMAIL", "app-password")

    imap_poller.poll_once(
        steward, "gmail", connection_factory=lambda host: _FakeImapConnection("h")
    )
    source = steward.list_comms_sources()[0]
    assert source["config"]["uid_cursor"] == 2

    # An operator/mailbox-migration cursor reset must not create duplicate rows
    # — Message-ID dedupe is belt-and-braces on top of the cursor.
    steward.upsert_comms_source(
        "gmail", imap_poller.KIND, config={**source["config"], "uid_cursor": 0}
    )
    second = imap_poller.poll_once(
        steward, "gmail", connection_factory=lambda host: _FakeImapConnection("h")
    )
    assert second["fetched"] == 2 and second["created"] == 0
    assert len(steward.list_comms_messages(source="gmail")) == 2


class _FetchGapImapConnection(_FakeImapConnection):
    """UID SEARCH returns two UIDs; the second UID FETCH fails soft (non-OK).

    This is the path where the poller ``break``s out of the UID loop without
    raising — historically reported as ``status=active`` with an empty
    ``last_error`` while the cursor froze on the gap, so a permanently
    unfetchable UID looked healthy forever.
    """

    def uid(self, command: str, *args: Any) -> tuple[str, list[Any]]:
        if command == "fetch":
            message_set, parts = args
            self.fetch_specs.append(parts)
            uid = int(message_set)
            self.fetched_uids.append(uid)
            if uid == 2:
                # Soft failure: no exception, just a non-OK status. The poller
                # must not launder this into "active"/"error":"".
                return "NO", [b""]
            raw = (
                b"Message-ID: <msg-" + str(uid).encode() + b"@example.com>\r\n"
                b"From: sender@example.com\r\n"
                b"Subject: Test\r\n\r\nBody " + str(uid).encode()
            )
            return "OK", [(f"{uid} (BODY[] {{123}}".encode(), raw)]
        return super().uid(command, *args)


def test_imap_fetch_gap_is_not_reported_as_active_healthy(
    steward: StewardStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Non-result-as-favourable: a soft UID FETCH failure must not look healthy.

    Counterfeits this catches:
    - return dict says ``status=error`` but source row stays ``active`` / clears
      ``last_error`` (dashboard health still green)
    - ``status=active`` with a non-empty ``error`` string only (status-based
      monitors still treat the source as fine)
    - cursor advances past the unfetchable UID (silent message loss)
    - empty mailbox still works (see harper empty-connection test) — this
      case is a *failed measure*, not a measured empty inbox
    """
    monkeypatch.setenv("IMAP_HOST_GMAIL", "imap.example.com")
    monkeypatch.setenv("IMAP_USER_GMAIL", "user@example.com")
    monkeypatch.setenv("IMAP_PASSWORD_GMAIL", "app-password")

    connection = _FetchGapImapConnection("imap.example.com", uids=(1, 2))
    result = imap_poller.poll_once(steward, "gmail", connection_factory=lambda host: connection)

    assert result["status"] == "error", (
        f"soft fetch gap must not report status=active (healthy) — got {result!r}"
    )
    assert result["error"], "error detail must be non-empty (not laundered to '')"
    assert "fetch" in result["error"].lower() or "gap" in result["error"].lower()
    assert result["fetched"] == 1
    assert result["created"] == 1

    source = steward.list_comms_sources()[0]
    assert source["status"] == "error", (
        "persisted source status must be error, not active with a cleared last_error"
    )
    assert source["last_error"] == result["error"]
    assert source["last_error"] != ""
    # Cursor frozen at the last successfully stored UID, not past the gap.
    assert source["config"]["uid_cursor"] == 1
    stored = steward.list_comms_messages(source="gmail")
    assert {row["external_id"] for row in stored} == {"<msg-1@example.com>"}


def test_imap_unreadable_fetch_payload_is_not_reported_as_active(
    steward: StewardStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Same defect class: FETCH returns OK but no message bytes → unreadable.

    Must not clear last_error and claim active; must freeze the cursor.
    """
    monkeypatch.setenv("IMAP_HOST_GMAIL", "imap.example.com")
    monkeypatch.setenv("IMAP_USER_GMAIL", "user@example.com")
    monkeypatch.setenv("IMAP_PASSWORD_GMAIL", "app-password")

    class _UnreadableFetch(_FakeImapConnection):
        def uid(self, command: str, *args: Any) -> tuple[str, list[Any]]:
            if command == "fetch":
                message_set, parts = args
                self.fetch_specs.append(parts)
                uid = int(message_set)
                self.fetched_uids.append(uid)
                # OK status but no (meta, bytes) tuple — _raw_message_bytes → None
                return "OK", [b"FLAGS (\\Seen)"]
            return super().uid(command, *args)

    connection = _UnreadableFetch("imap.example.com", uids=(1,))
    result = imap_poller.poll_once(steward, "gmail", connection_factory=lambda host: connection)
    assert result["status"] == "error"
    assert result["error"]
    source = steward.list_comms_sources()[0]
    assert source["status"] == "error"
    assert source["last_error"] == result["error"]
    assert source["config"]["uid_cursor"] == 0  # never successfully stored anything


def test_imap_insert_failure_freezes_cursor_and_message_is_retried_next_cycle(
    steward: StewardStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """H8: a store error on one message must not lose it — the cursor freezes
    BEFORE the failing UID, so the next poll re-fetches it (not skips it).
    """
    monkeypatch.setenv("IMAP_HOST_GMAIL", "imap.example.com")
    monkeypatch.setenv("IMAP_USER_GMAIL", "user@example.com")
    monkeypatch.setenv("IMAP_PASSWORD_GMAIL", "app-password")

    failing_steward = _FailOnceStore(steward, fail_external_id="<msg-2@example.com>")
    connection = _FakeImapConnection("imap.example.com")
    result = imap_poller.poll_once(
        failing_steward, "gmail", connection_factory=lambda host: connection
    )
    assert result["status"] == "error"
    assert result["fetched"] == 2  # both UIDs were fetched over the wire...
    assert result["created"] == 1  # ...but UID 2's insert raised before landing

    stored = steward.list_comms_messages(source="gmail")
    assert {row["external_id"] for row in stored} == {"<msg-1@example.com>"}
    source = steward.list_comms_sources()[0]
    assert source["config"]["uid_cursor"] == 1  # frozen strictly before the failing UID

    # Next poll: UID 2 is re-fetched (not silently skipped) and now succeeds.
    second_connection = _FakeImapConnection("imap.example.com")
    second = imap_poller.poll_once(
        steward, "gmail", connection_factory=lambda host: second_connection
    )
    assert second["status"] == "active"
    assert second["fetched"] == 1 and second["created"] == 1
    assert second_connection.fetched_uids == [2]
    stored_after = steward.list_comms_messages(source="gmail")
    assert {row["external_id"] for row in stored_after} == {
        "<msg-1@example.com>",
        "<msg-2@example.com>",
    }


def test_imap_connection_error_sets_error_status(
    steward: StewardStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("IMAP_HOST_GMAIL", "imap.example.com")
    monkeypatch.setenv("IMAP_USER_GMAIL", "user@example.com")
    monkeypatch.setenv("IMAP_PASSWORD_GMAIL", "app-password")

    def _boom(host: str) -> Any:
        # Include the app-password in the exception text to prove it is NEVER
        # echoed into last_error (sanitized like the telegram/slack pollers).
        raise OSError("connection refused for app-password")

    result = imap_poller.poll_once(steward, "gmail", connection_factory=_boom)
    assert result["status"] == "error"
    assert result["error"] == "imap poll failed: OSError"
    assert "app-password" not in result["error"]
    source = next(s for s in steward.list_comms_sources() if s["name"] == "gmail")
    assert "app-password" not in source["last_error"]
