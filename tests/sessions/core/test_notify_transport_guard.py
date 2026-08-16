"""A raising transport must never escape push/push_outcome.

``omniagentos/sessions/notify.py`` states the module invariant explicitly:
"Delivery is presentation, never control flow: every failure here is
swallowed so supervision cannot depend on a banner being shown." Both
``_push_ntfy`` and ``_push_slack`` used to guard only ``except httpx.HTTPError``,
so any OTHER exception raised by the transport (a socket-level error, a test
harness's offline-lane guard, a DNS failure surfaced as OSError, ...) escaped
the function and propagated into the caller -- observed in production as
``RuntimeError: No response returned`` from the ASGI layer when a park/escalate
request's notify leg raised mid-response.

These tests monkeypatch ``httpx.post`` to raise a non-``httpx.HTTPError``
exception and assert the push entrypoints degrade to ``False``/gated-off
instead of propagating.
"""

from __future__ import annotations

import pytest

from omniagentos.sessions import notify


class _NotAnHTTPError(RuntimeError):
    """Stand-in for a transport failure that is not an httpx.HTTPError.

    Mirrors tests/conftest.py's OfflineLaneViolation (BaseException) in spirit:
    a broad-enough guard must catch this class of failure whether it derives
    from Exception or not is a separate question -- here we assert the
    ordinary Exception case, which was already broken before this fix.
    """


def _raise(*_args: object, **_kwargs: object) -> None:
    raise _NotAnHTTPError("simulated non-HTTPError transport failure")


def test_push_ntfy_survives_non_http_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OMNI_NTFY_URL", "http://example.invalid/x")
    monkeypatch.setattr(notify.httpx, "post", _raise)

    result = notify._push_ntfy("title", "http://127.0.0.1:3003")

    assert result is False


def test_push_slack_survives_non_http_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPS_ALERT_SLACK_WEBHOOK_URL", "https://hooks.slack.com/services/x")
    monkeypatch.setattr(notify.httpx, "post", _raise)

    result = notify._push_slack("title", "http://127.0.0.1:3003")

    assert result is False


def test_push_outcome_survives_non_http_error_on_remote_legs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OMNI_NTFY_URL", "http://example.invalid/x")
    monkeypatch.setenv("OPS_ALERT_SLACK_WEBHOOK_URL", "https://hooks.slack.com/services/x")
    monkeypatch.setattr(notify.httpx, "post", _raise)
    monkeypatch.setattr(notify, "_ntfy_allowed", lambda kind, severity: True)
    monkeypatch.setattr(notify, "_push_terminal_notifier", lambda *a, **k: False)

    outcome = notify.push_outcome("title", "body", kind="approval", severity="high")

    assert outcome.ntfy is False
    assert outcome.slack is False
    assert outcome.remote_accepted is False
