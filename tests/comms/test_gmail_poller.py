"""Gmail poller unit tests — a fully mocked broker, never any live network.

Covers the contract the EDC ingest depends on: new messages land with the
per-mailbox ``source`` and the owner from ``edc.accounts``; the durable
``internal_date_cursor`` advances and drives an ``after:`` re-poll; a re-fetch
dedupes to a no-op; a broker failure is sanitized to the exception type (no OAuth
token leak) and freezes the cursor; and F01 — two owners with the SAME
``Message-ID`` produce TWO rows because the source is per-mailbox.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from omniagentos.comms.pollers import gmail as gmail_poller
from omniagentos.company_goals.store import CompanyGoalsStore
from omniagentos.db.store import SqliteStore
from omniagentos.edc.accounts import SourceOwner
from omniagentos.steward.store import StewardStore
from tests.support.db_template import make_store

_ACCOUNTS = {
    "gmail_ownera": SourceOwner("emp_owner", "", "gmail_ownera"),
    "gmail_initech": SourceOwner("emp_owner", "initech", "gmail_initech"),
}


@pytest.fixture(autouse=True)
def _static_accounts(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin the account map so the test never depends on the packaged YAML."""
    monkeypatch.setattr(gmail_poller, "accounts_map", lambda *a, **k: dict(_ACCOUNTS))


@pytest.fixture
def steward(tmp_path: Path) -> StewardStore:
    store = make_store(SqliteStore, tmp_path / "gmail.db")
    # owner_employee_id on comms_messages is an FK to employees(id) (migration
    # 130): seed emp_owner so the owner-stamped inserts satisfy the constraint.
    CompanyGoalsStore(store).ensure_employee(employee_id="emp_owner", name="the operator", role="operator")
    return StewardStore(store)


def _gmail_message(
    *,
    message_id: str,
    internal_date: str,
    subject: str = "Test",
    sender: str = "sender@example.com",
    body: str = "hello",
    thread_id: str = "t1",
) -> dict[str, Any]:
    """One ``users.messages.get?format=full`` JSON envelope (text/plain leaf)."""
    import base64

    encoded = base64.urlsafe_b64encode(body.encode("utf-8")).decode("ascii").rstrip("=")
    return {
        "id": message_id.strip("<>").split("@")[0],
        "threadId": thread_id,
        "internalDate": internal_date,
        "payload": {
            "headers": [
                {"name": "Message-ID", "value": message_id},
                {"name": "From", "value": sender},
                {"name": "To", "value": "owner@initech.example"},
                {"name": "Subject", "value": subject},
                {"name": "List-Unsubscribe", "value": "<mailto:x@example.com>"},
            ],
            "mimeType": "text/plain",
            "body": {"data": encoded},
        },
    }


class FakeBroker:
    """Minimal broker double: records calls, serves a scripted search + fetch.

    ``token`` is embedded in NOTHING it returns — the poller must never surface a
    credential in an error, so the failure variant raises with the token in the
    exception text to prove it is dropped.
    """

    def __init__(self, messages: list[dict[str, Any]], *, fail_status: int | None = None) -> None:
        self._by_id = {str(m["id"]): m for m in messages}
        self._search_ids = [str(m["id"]) for m in messages]
        self.fail_status = fail_status
        self.calls: list[tuple[str, str, dict[str, Any]]] = []

    def call(
        self,
        cap_id: str,
        granted: list[str] | None = None,
        *,
        method: str = "GET",
        path: str = "/",
        query: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        assert method == "GET", "the gmail poller is read-only"
        assert granted == [cap_id]
        self.calls.append((cap_id, path, dict(query or {})))
        if self.fail_status is not None:
            return {"capability": cap_id, "status": self.fail_status, "ok": False, "body": {}}
        if path == "/gmail/v1/users/me/messages":
            return {
                "capability": cap_id,
                "status": 200,
                "ok": True,
                "body": {"messages": [{"id": mid} for mid in self._search_ids]},
            }
        message_id = path.rsplit("/", 1)[-1]
        assert (query or {}).get("format") == "full"
        return {
            "capability": cap_id,
            "status": 200,
            "ok": True,
            "body": self._by_id[message_id],
        }


def test_new_messages_land_with_per_mailbox_source_and_owner(steward: StewardStore) -> None:
    broker = FakeBroker(
        [
            _gmail_message(message_id="<a@example.com>", internal_date="1700000001000"),
            _gmail_message(message_id="<b@example.com>", internal_date="1700000002000"),
        ]
    )
    result = gmail_poller.poll_once(steward, "gmail_ownera", broker=broker)
    assert result == {
        "source": "gmail_ownera",
        "status": "active",
        "error": "",
        "fetched": 2,
        "created": 2,
    }
    stored = steward.list_comms_messages(source="gmail_ownera")
    assert {row["external_id"] for row in stored} == {"<a@example.com>", "<b@example.com>"}
    # Per-mailbox source (F01) + owner from edc.accounts (§8).
    assert all(row["source"] == "gmail_ownera" for row in stored)
    assert all(row["owner_employee_id"] == "emp_owner" for row in stored)
    assert all(row["body_text"] == "hello" for row in stored)
    # First run used the bounded newer_than window (no cursor yet).
    search_calls = [c for c in broker.calls if c[1] == "/gmail/v1/users/me/messages"]
    assert search_calls and search_calls[0][2]["q"] == "newer_than:7d"


def test_cursor_advances_and_drives_after_requery(steward: StewardStore) -> None:
    first = FakeBroker([_gmail_message(message_id="<a@example.com>", internal_date="1700000002000")])
    gmail_poller.poll_once(steward, "gmail_ownera", broker=first)
    source = steward.list_comms_sources()[0]
    assert source["config"]["internal_date_cursor"] == 1700000002000

    # Second poll: the same message is re-listed under the after: window and
    # dedupes to a no-op (created == 0), cursor unchanged.
    second = FakeBroker(
        [_gmail_message(message_id="<a@example.com>", internal_date="1700000002000")]
    )
    result = gmail_poller.poll_once(steward, "gmail_ownera", broker=second)
    assert result["fetched"] == 1 and result["created"] == 0
    search_calls = [c for c in second.calls if c[1] == "/gmail/v1/users/me/messages"]
    assert search_calls[0][2]["q"] == "after:1700000002"  # ms cursor -> seconds
    assert len(steward.list_comms_messages(source="gmail_ownera")) == 1


def test_broker_failure_is_sanitized_and_freezes_cursor(steward: StewardStore) -> None:
    token = "ya29.super-secret-oauth-access-token"

    class LeakyBroker(FakeBroker):
        def call(self, cap_id: str, granted: list[str] | None = None, **kw: Any) -> dict[str, Any]:
            raise RuntimeError(f"401 Unauthorized for {token}")

    result = gmail_poller.poll_once(steward, "gmail_ownera", broker=LeakyBroker([]))
    assert result["status"] == "error"
    assert result["error"] == "gmail poll failed: RuntimeError"
    assert token not in result["error"]
    source = steward.list_comms_sources()[0]
    assert token not in source["last_error"]
    assert source["last_error"] == result["error"]
    assert source["config"].get("internal_date_cursor", 0) == 0  # never advanced


def test_non_2xx_status_is_an_error_not_silent_success(steward: StewardStore) -> None:
    broker = FakeBroker([], fail_status=403)
    result = gmail_poller.poll_once(steward, "gmail_ownera", broker=broker)
    assert result["status"] == "error"
    assert result["error"] == "gmail poll failed: _GmailHttpError"
    source = steward.list_comms_sources()[0]
    assert source["status"] == "error"


def test_f01_two_owners_same_message_id_yield_two_rows(steward: StewardStore) -> None:
    """The load-bearing F01 guarantee: a shared Message-ID across two mailboxes
    must NOT collide on UNIQUE(source, external_id) — each owner keeps its row."""
    shared_id = "<forwarded-thread@example.com>"
    ownera = FakeBroker([_gmail_message(message_id=shared_id, internal_date="1700000003000")])
    initech = FakeBroker(
        [_gmail_message(message_id=shared_id, internal_date="1700000003000", thread_id="t2")]
    )

    r1 = gmail_poller.poll_once(steward, "gmail_ownera", broker=ownera)
    r2 = gmail_poller.poll_once(steward, "gmail_initech", broker=initech)
    assert r1["created"] == 1 and r2["created"] == 1

    ownera_rows = steward.list_comms_messages(source="gmail_ownera")
    initech_rows = steward.list_comms_messages(source="gmail_initech")
    assert len(ownera_rows) == 1 and len(initech_rows) == 1
    assert ownera_rows[0]["external_id"] == shared_id
    assert initech_rows[0]["external_id"] == shared_id
    # Same Message-ID, two distinct per-mailbox sources -> two durable rows.
    assert ownera_rows[0]["source"] == "gmail_ownera"
    assert initech_rows[0]["source"] == "gmail_initech"
    # Both owned by the operator; company_slug is resolved by the EDC adapter from the
    # account map (it is not a comms_messages column), so it is not asserted here.
    assert initech_rows[0]["owner_employee_id"] == "emp_owner"
