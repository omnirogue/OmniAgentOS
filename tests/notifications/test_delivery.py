"""Delivery rail: transport fan-out (P1.1) and the push allowlist (P1.2).

Context: no notification had ever left this Mac. Two independent causes, one
per half of this file:

1. ``sessions.notify.push`` treated ntfy as a FALLBACK for terminal-notifier and
   returned as soon as the local banner was posted -- so the phone was only ever
   reached on a machine where the Mac banner could not be posted, i.e. never on
   the machine that runs the system. And the banner it did post opened
   ``:3000``, a port nothing serves.
2. Every persisted row also pushed, so 78% of the feed (done/info/swarm_failed)
   buzzed the device -- the fastest possible route to a muted channel.

The review round added a third: gating push at the WRITE SEAM only left the
transport's direct callers (runner escalation, supervisor default notifier,
health-sentinel blocked-session alert) reaching the phone ungated, so the
allowlist is now enforced at the transport too -- plus what ntfy is allowed to
carry off-box, and how a misconfigured environment is surfaced.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest

from omniagentos.notifications import policy, service
from omniagentos.notifications.dal import NotificationsDal
from omniagentos.sessions import notify

# Captured at import, BEFORE the session-wide conftest fixture replaces the
# delivery seam with a no-op: the allowlist lives inside service._push, so these
# tests must exercise the real function.
_REAL_SERVICE_PUSH = service._push


class _Transports:
    """Records what each transport was asked to send."""

    def __init__(self) -> None:
        self.terminal: list[list[str]] = []
        self.ntfy: list[tuple[str, str, dict[str, str]]] = []


@pytest.fixture(autouse=True)
def _reset_once_per_process_logs(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """The env-hygiene warnings latch for the process; unlatch them per test."""
    monkeypatch.setattr(notify, "_DASHBOARD_URL_WARNED", False)
    monkeypatch.setattr(notify, "_NTFY_DISABLED_LOGGED", False)
    yield


@pytest.fixture
def transports(monkeypatch: pytest.MonkeyPatch) -> _Transports:
    recorded = _Transports()

    def fake_run(argv: list[str], **_kwargs: Any) -> Any:
        recorded.terminal.append(list(argv))
        return None

    def fake_post(endpoint: str, **kwargs: Any) -> Any:
        recorded.ntfy.append((endpoint, kwargs.get("content", ""), kwargs.get("headers", {})))
        return None

    monkeypatch.setattr(notify.shutil, "which", lambda _name: "/usr/local/bin/terminal-notifier")
    monkeypatch.setattr(notify.subprocess, "run", fake_run)
    monkeypatch.setattr(notify.httpx, "post", fake_post)
    monkeypatch.setattr(notify, "_sender_available", lambda: False)
    monkeypatch.setenv("OMNI_NTFY_URL", "https://ntfy.example/omni")
    return recorded


# --- P1.1 fan-out -------------------------------------------------------------


def test_fanout_reaches_ntfy_when_terminal_notifier_succeeds(transports: _Transports) -> None:
    """The regression: a SUCCESSFUL local banner must not consume the push.

    Before the fix, ``push`` returned immediately after terminal-notifier, so
    ntfy (the only transport that reaches a phone) was dead code in production.
    """
    notify.push("Approval required", "session ses_1 is waiting", kind="approval")

    assert len(transports.terminal) == 1, "local banner not posted"
    assert len(transports.ntfy) == 1, (
        "ntfy was skipped after a successful terminal-notifier -- fan-out regressed to fallback"
    )
    endpoint, _body, headers = transports.ntfy[0]
    assert endpoint == "https://ntfy.example/omni"
    assert headers["Title"] == "Approval required"


def test_terminal_notifier_failure_does_not_suppress_ntfy(
    transports: _Transports, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(*_args: Any, **_kwargs: Any) -> Any:
        raise OSError("terminal-notifier exploded")

    monkeypatch.setattr(notify.subprocess, "run", boom)

    notify.push("Approval required", "still needs to reach the phone", kind="approval")

    assert len(transports.ntfy) == 1


def test_missing_terminal_notifier_does_not_suppress_ntfy(
    transports: _Transports, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(notify.shutil, "which", lambda _name: None)

    notify.push("Approval required", "no local binary here", kind="approval")

    assert transports.terminal == []
    assert len(transports.ntfy) == 1


def test_ntfy_failure_does_not_suppress_local_banner(
    transports: _Transports, monkeypatch: pytest.MonkeyPatch
) -> None:
    def offline(*_args: Any, **_kwargs: Any) -> Any:
        raise httpx.ConnectError("offline")

    monkeypatch.setattr(notify.httpx, "post", offline)

    notify.push("Approval required", "local half must still fire", kind="approval")

    assert len(transports.terminal) == 1


def test_delivery_failures_are_swallowed(monkeypatch: pytest.MonkeyPatch) -> None:
    """Delivery is presentation: neither transport may raise into the caller."""
    monkeypatch.setattr(notify.shutil, "which", lambda _name: "/usr/local/bin/terminal-notifier")
    monkeypatch.setattr(
        notify.subprocess, "run", lambda *_a, **_k: (_ for _ in ()).throw(OSError())
    )
    monkeypatch.setattr(
        notify.httpx, "post", lambda *_a, **_k: (_ for _ in ()).throw(httpx.ConnectError("x"))
    )
    monkeypatch.setenv("OMNI_NTFY_URL", "https://ntfy.example/omni")

    notify.push("title", "body", kind="approval")  # must not raise


def test_unknown_transport_results_are_not_delivery_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A control-affecting delivery carrier may not fail open on a test fake."""
    monkeypatch.setattr(notify.shutil, "which", lambda _name: "/usr/local/bin/terminal-notifier")
    monkeypatch.setattr(notify.subprocess, "run", lambda *_args, **_kwargs: None)
    assert notify._push_terminal_notifier("title", "body", "https://dashboard.invalid", subtitle=None, group=None) is False

    monkeypatch.setattr(notify.httpx, "post", lambda *_args, **_kwargs: None)
    monkeypatch.setenv("OMNI_NTFY_URL", "https://ntfy.example/omni")
    monkeypatch.setenv("OPS_ALERT_SLACK_WEBHOOK_URL", "https://hooks.slack.com/services/test")
    assert notify._push_ntfy("title", "https://dashboard.invalid") is False
    assert notify._push_slack("title", "https://dashboard.invalid") is False


# --- P1.1 dashboard URL -------------------------------------------------------


def test_dashboard_url_defaults_to_the_served_port(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OMNI_DASHBOARD_URL", raising=False)
    resolved = notify.dashboard_url()
    assert resolved == "http://127.0.0.1:3003"
    assert ":3000" not in resolved, "the legacy dead port is back"


def test_dashboard_url_is_env_driven(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OMNI_DASHBOARD_URL", "http://omni-host:3003")
    assert notify.dashboard_url() == "http://omni-host:3003"


def test_blank_dashboard_url_falls_back(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OMNI_DASHBOARD_URL", "   ")
    assert notify.dashboard_url() == "http://127.0.0.1:3003"


def test_push_opens_the_configured_url_on_both_transports(
    transports: _Transports, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OMNI_DASHBOARD_URL", "http://omni-host:3003")

    notify.push("Approval required", "body", kind="approval")

    argv = transports.terminal[0]
    assert argv[argv.index("-open") + 1] == "http://omni-host:3003"
    assert transports.ntfy[0][2]["Click"] == "http://omni-host:3003"


def test_explicit_url_still_wins(transports: _Transports, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OMNI_DASHBOARD_URL", "http://omni-host:3003")

    notify.push("Approval required", "body", "http://explicit/target", kind="approval")

    argv = transports.terminal[0]
    assert argv[argv.index("-open") + 1] == "http://explicit/target"
    assert transports.ntfy[0][2]["Click"] == "http://explicit/target"


def test_no_ntfy_endpoint_configured_is_a_no_op(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[Any] = []
    monkeypatch.delenv("OMNI_NTFY_URL", raising=False)
    monkeypatch.setattr(notify.shutil, "which", lambda _name: None)
    monkeypatch.setattr(notify.httpx, "post", lambda *a, **k: calls.append((a, k)))

    # Labelled deliberately: an unlabelled push would never reach the endpoint
    # check at all, so the no-op would be pinned by the wrong mechanism.
    notify.push("title", "body", kind="approval")

    assert calls == []


# --- REVIEW FIX 4: env hygiene ------------------------------------------------


@pytest.mark.parametrize(
    "configured",
    [
        "omni-host:3003",  # scheme-less, the most likely typo
        "ftp://omni-host/dash",
        "file:///tmp/dash.html",
        "javascript:alert(1)",
        "://omni-host:3003",
    ],
)
def test_non_http_dashboard_url_is_ignored(
    configured: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A URL the OS cannot open must not be handed to a banner or a phone."""
    monkeypatch.setenv("OMNI_DASHBOARD_URL", configured)
    assert notify.dashboard_url() == "http://127.0.0.1:3003"


def test_https_dashboard_url_is_honoured(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OMNI_DASHBOARD_URL", "https://omni.example.ts.net")
    assert notify.dashboard_url() == "https://omni.example.ts.net"


def test_invalid_dashboard_url_warns_exactly_once_per_process(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setenv("OMNI_DASHBOARD_URL", "omni-host:3003")

    with caplog.at_level(logging.WARNING, logger=notify.logger.name):
        for _ in range(3):
            notify.dashboard_url()

    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1, f"expected one warning per process, got {len(warnings)}"
    assert "OMNI_DASHBOARD_URL" in warnings[0].getMessage()


def test_unset_ntfy_url_is_logged_once_and_distinguishable_from_filtering(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A disabled transport and a misconfigured one must not look identical."""
    monkeypatch.delenv("OMNI_NTFY_URL", raising=False)
    monkeypatch.setattr(notify.shutil, "which", lambda _name: None)

    with caplog.at_level(logging.DEBUG, logger=notify.logger.name):
        for _ in range(3):
            notify.push("Approval required", "body", kind="approval")

    disabled = [r for r in caplog.records if "OMNI_NTFY_URL unset" in r.getMessage()]
    assert len(disabled) == 1, f"expected one disabled notice per process, got {len(disabled)}"
    assert disabled[0].levelno <= logging.INFO, "a disabled transport is not a warning"


def test_configured_ntfy_url_logs_no_disabled_notice(
    transports: _Transports, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.DEBUG, logger=notify.logger.name):
        notify.push("Approval required", "body", kind="approval")

    assert [r for r in caplog.records if "OMNI_NTFY_URL unset" in r.getMessage()] == []


# --- REVIEW FIX 1: the allowlist is enforced at the TRANSPORT ------------------
#
# The gate in service._push is invisible to notify.push's DIRECT callers:
# runner/core.py's _notify_escalation, the Supervisor's default notifier, and
# scripts/health-sentinel/blocked_sessions.py all call the transport straight.
# Before this fix every one of them buzzed the phone on every run completion.


def test_unlabelled_direct_push_keeps_the_banner_but_never_buzzes(
    transports: _Transports,
) -> None:
    """The regression Kimi found: legacy callers pass no kind, so they must not
    reach ntfy -- while still getting the local banner they have always had."""
    notify.push("OmniAgentOS [done]", "run run_1: finished")

    assert len(transports.terminal) == 1, "the local banner leg must be unconditional"
    assert transports.ntfy == [], "an unlabelled legacy push reached the phone ungated"


@pytest.mark.parametrize(
    ("kind", "severity", "expected_ntfy"),
    [
        ("approval", "warning", 1),
        ("escalation", "warning", 1),
        ("blocked", "info", 1),
        ("  Approval ", "info", 1),  # normalized, like the seam
        ("alert", "high", 1),  # severity override
        ("done", "critical", 1),
        ("done", "info", 0),
        ("info", "info", 0),
        ("swarm_failed", "warning", 0),
        ("alert", "info", 0),
        (None, "info", 0),
        (None, None, 0),  # fail closed
    ],
)
def test_transport_gate_matches_the_shared_allowlist(
    transports: _Transports,
    kind: str | None,
    severity: str | None,
    expected_ntfy: int,
) -> None:
    notify.push("title", "detail", kind=kind, severity=severity)

    assert len(transports.terminal) == 1, "the banner is never gated"
    assert len(transports.ntfy) == expected_ntfy
    assert len(transports.ntfy) == int(policy.should_push(kind, severity))


def test_transport_gate_uses_the_shared_policy_module(
    transports: _Transports, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One predicate, two enforcement points -- not two copies that can drift."""
    monkeypatch.setattr(policy, "should_push", lambda _kind, _severity: False)

    notify.push("Approval required", "body", kind="approval")

    assert transports.ntfy == [], "notify.push kept a private copy of the allowlist"


def test_unimportable_policy_fails_closed(
    transports: _Transports, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A broken gate suppresses the buzz; it never crashes and never opens up."""
    import builtins

    real_import = builtins.__import__

    def blocked(name: str, *args: Any, **kwargs: Any) -> Any:
        if name == "omniagentos.notifications.policy":
            raise ImportError("policy is unavailable")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", blocked)

    notify.push("Approval required", "body", kind="approval")

    assert len(transports.terminal) == 1
    assert transports.ntfy == []


def test_service_forwards_kind_and_severity_to_the_transport(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The seam must LABEL its pushes, or the transport gate drops them all."""
    recorded: list[dict[str, Any]] = []
    monkeypatch.setattr(service, "_push", _REAL_SERVICE_PUSH)
    monkeypatch.setattr(notify, "push", lambda *_a, **kwargs: recorded.append(dict(kwargs)))

    _REAL_SERVICE_PUSH("Approval required", "detail", kind="approval", severity="warning")

    assert recorded == [
        {"subtitle": None, "group": None, "kind": "approval", "severity": "warning"}
    ]


def test_service_should_push_is_the_policy_predicate() -> None:
    """``service.should_push`` stays the public spelling (re-export, not a copy)."""
    assert service.should_push is policy.should_push
    assert service.PUSH_KINDS is policy.PUSH_KINDS
    assert service.PUSH_SEVERITIES is policy.PUSH_SEVERITIES
    assert service.ACTIONABLE_KINDS is policy.PUSH_KINDS


# --- REVIEW FIX 2: ntfy carries the title and the link, nothing else ----------


def test_ntfy_request_carries_only_the_title_and_the_click_url(
    transports: _Transports,
) -> None:
    """The ntfy host may be third-party; the body routinely names sessions/paths."""
    secret_detail = "session ses_7 cwd=/Users/owner/private/client-work blocked on Bash"

    notify.push("Session blocked 111m", secret_detail, kind="blocked", group="omniagentos.x.y")

    endpoint, body, headers = transports.ntfy[0]
    assert endpoint == "https://ntfy.example/omni"
    assert set(headers) == {"Title", "Click"}, f"ntfy carried extra fields: {sorted(headers)}"
    assert headers["Title"] == "Session blocked 111m"
    assert headers["Click"] == "http://127.0.0.1:3003"
    assert body == "Session blocked 111m", "ntfy body must be the short title only"
    assert secret_detail not in body
    assert not any(secret_detail in value for value in headers.values())
    assert "ses_7" not in body and "ses_7" not in "".join(headers.values())


def test_banner_still_carries_the_full_message(transports: _Transports) -> None:
    """Minimization is off-box only: the LOCAL banner keeps the detail."""
    detail = "session ses_7 cwd=/Users/owner/private/client-work blocked on Bash"

    notify.push("Session blocked 111m", detail, kind="blocked", subtitle="account-3")

    argv = transports.terminal[0]
    assert argv[argv.index("-message") + 1] == detail
    assert argv[argv.index("-subtitle") + 1] == "account-3"


def test_ntfy_title_is_clipped_like_the_banner(transports: _Transports) -> None:
    notify.push("T" * 200, "detail", kind="approval")

    _endpoint, body, headers = transports.ntfy[0]
    assert len(headers["Title"]) == 60
    assert body == headers["Title"]


# --- P1.2 push allowlist ------------------------------------------------------


@pytest.fixture
def dal(tmp_path: Path) -> NotificationsDal:
    return NotificationsDal(str(tmp_path / "delivery.db"))


@pytest.fixture
def pushes(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, str]]:
    """Restore the REAL service._push and record what reaches the transport."""
    recorded: list[tuple[str, str]] = []

    monkeypatch.setattr(service, "_push", _REAL_SERVICE_PUSH)
    monkeypatch.setattr(notify, "push", lambda title, body, **_k: recorded.append((title, body)))
    return recorded


@pytest.mark.parametrize(
    ("kind", "severity", "expected"),
    [
        ("approval", "warning", True),
        ("escalation", "warning", True),
        ("blocked", "info", True),
        ("done", "info", False),
        ("info", "info", False),
        ("swarm_failed", "warning", False),
        ("alert", "info", False),
        # Severity override: any kind at high/critical still reaches the device.
        ("alert", "high", True),
        ("done", "critical", True),
        # Normalization + fail-closed defaults.
        ("  Approval ", "info", True),
        (None, None, False),
        ("", "", False),
    ],
)
def test_should_push_allowlist(kind: str | None, severity: str | None, expected: bool) -> None:
    assert service.should_push(kind, severity) is expected


def test_noise_kinds_are_recorded_but_not_pushed(
    dal: NotificationsDal, pushes: list[tuple[str, str]]
) -> None:
    """The durable feed is unchanged; only the buzz is gated."""
    for kind in ("done", "info", "swarm_failed"):
        notification_id = service.record_notification(
            kind=kind,
            title=f"{kind} row",
            severity="info" if kind != "swarm_failed" else "warning",
            dal=dal,
            push=True,
        )
        assert notification_id is not None, f"{kind} row must still persist"
        assert dal.get(notification_id) is not None

    assert len(dal.list()) == 3, "durable feed must record every kind"
    assert pushes == [], f"noise kinds buzzed the device: {pushes!r}"


def test_approval_kind_still_pushes(dal: NotificationsDal, pushes: list[tuple[str, str]]) -> None:
    service.notify_approval_requested(
        approval_id="apr_push",
        proposed_action="Write /etc/hosts",
        action_class="consequential",
        source="runner",
        dal=dal,
    )
    assert [title for title, _ in pushes] == ["Approval required"]


def test_high_severity_alert_pushes(dal: NotificationsDal, pushes: list[tuple[str, str]]) -> None:
    service.notify_alert(
        alert_id="42",
        title="ROAS floor breached",
        body="detail",
        severity="critical",
        dal=dal,
    )
    assert [title for title, _ in pushes] == ["ROAS floor breached"]


def test_task_done_records_without_pushing(
    dal: NotificationsDal, pushes: list[tuple[str, str]]
) -> None:
    notification_id = service.notify_task_done(
        board_task_id="tsk_1", task_title="ship it", dal=dal, push=True
    )
    assert notification_id is not None
    assert pushes == []


def test_unlabelled_push_fails_closed(pushes: list[tuple[str, str]]) -> None:
    """A call that names no kind/severity must not buzz (fail closed)."""
    service._push("mystery", "no labels")
    assert pushes == []
