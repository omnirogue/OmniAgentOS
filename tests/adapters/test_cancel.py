"""cancel() must not report success when it could not deliver the kill signal.

Non-result-as-favourable: PermissionError / OSError from killpg used to be
swallowed, then cancel() returned True — callers (runner._cancel) believed the
CLI was stopped when it was still running.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest

from omniagentos.adapters.common import CliAdapter, ParsedResponse
from omniagentos.contracts import AgentUsage


class _TinyAdapter(CliAdapter):
    name = "tiny"
    cli = "tiny"

    def _command(self, *args: Any, **kwargs: Any) -> list[str]:
        return ["tiny"]

    def _parse(self, stdout: str) -> ParsedResponse:
        return ParsedResponse(text=stdout, session_ref=None, payload={})

    def _usage(self, *args: Any, **kwargs: Any) -> AgentUsage:
        return AgentUsage(wall_ms=1, estimated=True, source="estimator")


def test_cancel_returns_false_when_kill_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    """Kill signal not delivered → cancel is False, not a fake success.

    Counterfeit: still ``return True`` after ``_kill_process_group`` (old code),
    or return True whenever the session_ref was found in ``_active``.
    """
    adapter = _TinyAdapter()
    proc = MagicMock()
    proc.poll.return_value = None  # still running
    proc.pid = 4242
    adapter._active["ses-1"] = proc

    monkeypatch.setattr("omniagentos.adapters.common.os.getpgid", lambda pid: pid)

    def _refuse(pgid: int, signum: int) -> None:
        raise PermissionError("operation not permitted")

    monkeypatch.setattr("omniagentos.adapters.common.os.killpg", _refuse)

    assert adapter.cancel("ses-1") is False
    # Process was not removed under a false claim of success.
    assert adapter._active.get("ses-1") is proc
    assert proc.poll() is None


def test_cancel_returns_true_when_signal_is_delivered(monkeypatch: pytest.MonkeyPatch) -> None:
    """A real kill delivery is success — not collateral damage of the fix."""
    adapter = _TinyAdapter()
    proc = MagicMock()
    proc.poll.return_value = None
    proc.pid = 4243
    adapter._active["ses-2"] = proc

    monkeypatch.setattr("omniagentos.adapters.common.os.getpgid", lambda pid: pid)
    delivered: list[int] = []

    def _ok(pgid: int, signum: int) -> None:
        delivered.append(signum)
        proc.poll.return_value = -signum  # reaped

    monkeypatch.setattr("omniagentos.adapters.common.os.killpg", _ok)

    assert adapter.cancel("ses-2") is True
    assert delivered  # signal actually went out


def test_cancel_unknown_session_is_false() -> None:
    assert _TinyAdapter().cancel("never-seen") is False
