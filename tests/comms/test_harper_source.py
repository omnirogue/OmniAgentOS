"""Harper's named IMAP source uses the expected operator env convention."""

from __future__ import annotations

from typing import Any

import pytest

from omniagentos.comms.pollers import imap as imap_poller
from omniagentos.steward.store import StewardStore


class _EmptyImapConnection:
    def __init__(self, host: str) -> None:
        self.host = host

    def __enter__(self) -> _EmptyImapConnection:
        return self

    def __exit__(self, *exc_info: object) -> None:
        return None

    def login(self, user: str, password: str) -> None:
        self.credentials = (user, password)

    def select(self, mailbox: str) -> None:
        self.mailbox = mailbox

    def uid(self, command: str, *args: Any) -> tuple[str, list[Any]]:
        assert command == "search"
        return "OK", [b""]


def test_harper_env_resolution_and_missing_credential(
    steward: StewardStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = {
        "IMAP_HOST_HARPER": "imap.example.com",
        "IMAP_USER_HARPER": "harper@example.com",
        "IMAP_PASSWORD_HARPER": "app-password",
    }
    for name, value in env.items():
        monkeypatch.setenv(name, value)

    config = imap_poller._resolve_config(steward, "harper")
    assert config == {
        "host_env": "IMAP_HOST_HARPER",
        "user_env": "IMAP_USER_HARPER",
        "password_env": "IMAP_PASSWORD_HARPER",
    }
    result = imap_poller.poll_once(steward, "harper", connection_factory=_EmptyImapConnection)
    assert result == {
        "source": "harper",
        "status": "active",
        "error": "",
        "fetched": 0,
        "created": 0,
    }

    monkeypatch.delenv("IMAP_PASSWORD_HARPER")
    result = imap_poller.poll_once(steward, "harper")
    assert result == {
        "source": "harper",
        "status": "pending_setup",
        "error": "missing environment variable: IMAP_PASSWORD_HARPER",
        "fetched": 0,
        "created": 0,
    }
