"""Best-effort local and optional remote notifications.

Delivery is presentation, never control flow: every failure here is swallowed so
supervision cannot depend on a banner being shown.

**FAN-OUT, not fallback.** The transports reach different places: a
terminal-notifier banner reaches the Mac that is running the process, ntfy
reaches the operator's phone, and Slack reaches the ops channel. Each is
attempted independently so one dead transport cannot suppress another.

**The two legs are gated differently, on purpose.** The banner is local and free
-- it lands on the Mac the operator is already looking at -- so it fires for
EVERY caller. The remote legs reach humans elsewhere, so they fire only for
what the delivery allowlist (:mod:`omniagentos.notifications.policy`) says is
worth an interruption. That gate has to live HERE, not only in the notifications write seam,
because ``push`` has direct legacy callers that never touch the seam (the
runner's escalation ping, the supervisor's default notifier, the health
sentinel's blocked-session alert). Without a transport-level gate those callers
reached the phone ungated and re-created the exact noise the allowlist exists to
prevent. An unlabelled call (no ``kind``/``severity``) therefore fails CLOSED for
ntfy or Slack -- it still gets its banner, it just does not interrupt anyone.

**What remote transports are allowed to carry.** Only the short title and the
click-through URL. Remote endpoints may be someone else's host, and the message
body routinely contains session/run detail (paths, cwd, proposed actions), so
the body is never sent off-box; the full message stays in the local banner.

**Why `-sender` and not an image path.** macOS renders the icon of the app that
POSTED a notification; a banner cannot carry an arbitrary image, and
terminal-notifier's ``-appIcon`` has been ignored since Big Sur. Posting with
``-sender com.omniagentos.launcher`` makes the banner come from the installed
OmniAgentOS app, so it shows the OmniAgentOS icon and clicking it activates that
app. ``scripts/install-notification-icon.sh`` puts the brand icon on that bundle.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
from dataclasses import dataclass

import httpx

logger = logging.getLogger(__name__)

# The installed launcher bundle whose icon and identity banners borrow.
_SENDER_BUNDLE_ID = "com.omniagentos.launcher"
_SENDER_APP_PATH = "/Applications/OmniAgentOS.app"
# Where a banner/push points when the caller supplies no explicit URL. The
# dashboard is served on :3003 (OMNIAGENTOS_DASH_PORT); :3000 was a legacy Omni port,
# so every banner ever posted opened a dead link. The deployment (launchd plist)
# overrides this with the reachable-from-phone URL via OMNI_DASHBOARD_URL --
# NEVER hardcode a tailnet/host-specific address in repo code.
_DEFAULT_DASHBOARD_URL = "http://127.0.0.1:3003"
# The only schemes a banner/push may be told to open. A value without one is a
# misconfiguration (a bare host:port, a shell-mangled export, a file:// path),
# and handing it to `terminal-notifier -open` or ntfy's Click header produces a
# link that silently does nothing -- indistinguishable, to the operator, from a
# notification that was never sent.
_ALLOWED_URL_SCHEMES = ("http://", "https://")
# macOS truncates hard; past these a banner shows an ellipsis instead of content.
_TITLE_MAX = 60
_SUBTITLE_MAX = 80
_BODY_MAX = 180

# Env-misconfiguration is logged ONCE per process: these fire on every push, and
# a per-push warning would bury the one line that matters under thousands of
# copies. Module-level flags (not lru_cache) so a test can reset them.
_DASHBOARD_URL_WARNED = False
_NTFY_DISABLED_LOGGED = False
_SLACK_DISABLED_LOGGED = False
_SLACK_URL_WARNED = False


@dataclass(frozen=True)
class PushOutcome:
    """Per-transport result for one best-effort notification.

    ``accepted`` preserves :func:`push`'s legacy meaning: a local banner is a
    useful presentation success for its many banner-only callers.  Approval
    delivery tracking must instead use ``remote_accepted``: a banner on the
    process host is not evidence that an operator elsewhere was reached.
    """

    banner: bool
    ntfy: bool
    slack: bool

    @property
    def accepted(self) -> bool:
        return self.banner or self.ntfy or self.slack

    @property
    def remote_accepted(self) -> bool:
        return self.ntfy or self.slack


def _clip(text: str, limit: int) -> str:
    """Collapse whitespace and trim to *limit*, so a banner never renders half a word."""
    collapsed = " ".join((text or "").split())
    if len(collapsed) <= limit:
        return collapsed
    return collapsed[: limit - 1].rstrip() + "…"


def _sender_available() -> bool:
    """True when the launcher bundle exists to post as.

    Without it ``-sender`` silently produces NO banner, so the caller falls back
    to an un-sendered notification rather than losing the alert entirely.
    """
    return os.path.isdir(_SENDER_APP_PATH)


def dashboard_url() -> str:
    """The URL a banner/push opens, from ``OMNI_DASHBOARD_URL`` (env-driven).

    Read per call, not at import: the launchd job exports the operator-reachable
    URL, and a process that imported this module before that export must still
    honour it. A blank/whitespace value is treated as unset.

    A value that is not ``http://`` or ``https://`` is IGNORED (the local
    fallback is used instead) and warned about once per process. Passing it
    through would ship a dead link to every banner and every phone push, and the
    failure is invisible at the point of use -- the operator taps, nothing
    opens, and nothing anywhere says why.
    """
    global _DASHBOARD_URL_WARNED

    configured = (os.environ.get("OMNI_DASHBOARD_URL") or "").strip()
    if not configured:
        return _DEFAULT_DASHBOARD_URL
    if configured.lower().startswith(_ALLOWED_URL_SCHEMES):
        return configured
    if not _DASHBOARD_URL_WARNED:
        _DASHBOARD_URL_WARNED = True
        logger.warning(
            "ignoring OMNI_DASHBOARD_URL=%r: expected an http:// or https:// URL; "
            "notifications will open %s instead",
            configured,
            _DEFAULT_DASHBOARD_URL,
        )
    return _DEFAULT_DASHBOARD_URL


def _push_terminal_notifier(
    title: str,
    body: str,
    url: str,
    *,
    subtitle: str | None,
    group: str | None,
) -> bool:
    """Post the local macOS banner. Returns whether the command was invoked."""
    if not shutil.which("terminal-notifier"):
        return False
    argv = [
        "terminal-notifier",
        "-title",
        _clip(title, _TITLE_MAX),
        # An empty -message renders an empty banner; a space keeps it valid.
        "-message",
        _clip(body, _BODY_MAX) or " ",
        "-open",
        url,
    ]
    if subtitle:
        argv.extend(["-subtitle", _clip(subtitle, _SUBTITLE_MAX)])
    if group:
        argv.extend(["-group", group])
    if _sender_available():
        argv.extend(["-sender", _SENDER_BUNDLE_ID])
    try:
        result = subprocess.run(argv, check=False, capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return False
    return result is not None and result.returncode == 0


def ntfy_url_from_env() -> str:
    """``OMNI_NTFY_URL`` from the process environment.

    This file is the inventoried unbrokered reader for that name
    (``tests/llm/test_unbrokered_credentials.py`` ``KNOWN_VIOLATIONS``).
    Other modules must call this instead of reading the env themselves.
    """
    return (os.environ.get("OMNI_NTFY_URL") or "").strip()


def _push_ntfy(title: str, url: str) -> bool:
    """POST to the configured ntfy topic. Returns whether the request was sent.

    Deliberately takes NO body/subtitle/group parameter: the request carries the
    short title and the click-through URL and nothing else, so no future edit
    can leak session detail to a host that may not be ours. The full message
    stays in the local banner (see the module docstring).

    The title doubles as the ntfy message because an empty message body is not
    guaranteed to render as a readable push on every ntfy client/server, and the
    title is already the one string cleared for off-box transmission.
    """
    global _NTFY_DISABLED_LOGGED

    endpoint = ntfy_url_from_env()
    if not endpoint:
        # Distinguish "switched off" from "misconfigured": without this line an
        # operator whose phone never buzzes cannot tell whether the endpoint is
        # unset or the allowlist is filtering everything out.
        if not _NTFY_DISABLED_LOGGED:
            _NTFY_DISABLED_LOGGED = True
            logger.info("ntfy push disabled -- OMNI_NTFY_URL unset")
        return False
    headline = _clip(title, _TITLE_MAX)
    try:
        result = httpx.post(
            endpoint,
            content=headline,
            headers={"Title": headline, "Click": url},
            timeout=3.0,
        )
    except Exception:  # noqa: BLE001 - delivery is presentation, never control flow
        logger.warning("ntfy push failed", exc_info=True)
        return False
    return bool(getattr(result, "is_success", False))


def _push_slack(title: str, url: str) -> bool:
    """POST a compact, clickable alert to the configured ops Slack webhook.

    Deliberately takes NO body parameter: like :func:`_push_ntfy`, Slack may
    carry only the title and click-through URL so a future caller cannot leak
    session details off-box.  A webhook may be absent, expired, or temporarily
    unavailable, but presentation must never steer supervision control flow.
    """
    global _SLACK_DISABLED_LOGGED, _SLACK_URL_WARNED

    endpoint = (os.environ.get("OPS_ALERT_SLACK_WEBHOOK_URL") or "").strip()
    if not endpoint:
        if not _SLACK_DISABLED_LOGGED:
            _SLACK_DISABLED_LOGGED = True
            logger.info("Slack push disabled -- OPS_ALERT_SLACK_WEBHOOK_URL unset")
        return False
    if not endpoint.startswith("https://hooks.slack.com/"):
        if not _SLACK_URL_WARNED:
            _SLACK_URL_WARNED = True
            logger.warning(
                "ignoring OPS_ALERT_SLACK_WEBHOOK_URL=%r: expected a "
                "https://hooks.slack.com/ webhook URL",
                endpoint,
            )
        return False
    text = "\n".join(part for part in (_clip(title, _TITLE_MAX), url) if part)
    try:
        result = httpx.post(endpoint, json={"text": text}, timeout=3.0)
    except Exception:  # noqa: BLE001 - delivery is presentation, never control flow
        logger.warning("Slack push failed", exc_info=True)
        return False
    return bool(getattr(result, "is_success", False))


def _ntfy_allowed(kind: str | None, severity: str | None) -> bool:
    """Whether this push may reach the phone, per the shared delivery allowlist.

    Imported lazily and fail-CLOSED. This module is the lowest-level transport in
    the system -- the runner, the supervisor and the health sentinel all import
    it -- so it must not take a module-level dependency on the notifications
    package (sqlite, the DAL, and the write seam that imports this module back).
    An unimportable policy means no buzz, never a crash and never an ungated
    push: delivery is presentation, and this is the safe direction to fail.
    """
    try:
        from omniagentos.notifications.policy import should_push

        return should_push(kind, severity)
    except Exception:  # noqa: BLE001 - never let the gate itself break a push
        logger.debug("delivery allowlist unavailable; suppressing ntfy", exc_info=True)
        return False


def push_outcome(
    title: str,
    body: str,
    url: str | None = None,
    *,
    subtitle: str | None = None,
    group: str | None = None,
    kind: str | None = None,
    severity: str | None = None,
) -> PushOutcome:
    """Push a notification and return each leg's independent outcome.

    ``subtitle`` gives the banner a readable second line (what/where), and
    ``group`` coalesces: a repeat with the same group REPLACES the previous
    banner instead of stacking another copy in Notification Center.

    ``kind``/``severity`` label the push for the delivery allowlist. The local
    banner is posted for EVERY caller regardless; the remote ntfy (phone) and
    Slack legs are sent only when the labels pass :func:`_ntfy_allowed`, and an
    unlabelled call is banner-only.  Callers recording durable approval
    delivery must use :attr:`PushOutcome.remote_accepted`, never the local
    banner bit.
    """
    target = url or dashboard_url()
    banner = _push_terminal_notifier(title, body, target, subtitle=subtitle, group=group)
    ntfy = False
    slack = False
    if _ntfy_allowed(kind, severity):
        ntfy = _push_ntfy(title, target)
        slack = _push_slack(title, target)
    return PushOutcome(banner=banner, ntfy=ntfy, slack=slack)


def push(
    title: str,
    body: str,
    url: str | None = None,
    *,
    subtitle: str | None = None,
    group: str | None = None,
    kind: str | None = None,
    severity: str | None = None,
) -> bool:
    """Push a notification, preserving the legacy any-transport result."""
    return push_outcome(
        title,
        body,
        url,
        subtitle=subtitle,
        group=group,
        kind=kind,
        severity=severity,
    ).accepted


__all__ = ["PushOutcome", "dashboard_url", "push", "push_outcome"]
