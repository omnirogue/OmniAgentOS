"""Slack poller — ``conversations.list`` + ``conversations.history`` since the last poll.

Credential: ``SLACK_BOT_TOKEN`` (a bot token with ``channels:history``,
``channels:read``, ``groups:history``, ``groups:read`` scopes — see
``scripts/comms/SETUP.md``).

Both ``conversations.list`` and ``conversations.history`` are paginated by Slack
(``response_metadata.next_cursor`` / ``has_more``); this poller follows every
page of both — a channel list truncated at the API's default page size would
silently miss channels, and a history response truncated the same way would
silently drop older-but-still-``> since`` messages. The ``oldest`` boundary
passed on a history page's FIRST request is never recomputed or dropped across
that channel's subsequent pages, so pagination never widens the window past
what was actually requested.

RECONCILIATION ROLE
-------------------
Since ``omniagentos.comms.sockets.slack`` exists, this poller is no longer the
primary ingest path — it is the RECONCILIATION SWEEP behind it. Socket Mode does
not replay events that occurred while the client was disconnected, so a socket
alone loses everything in a gap, permanently and silently. This poller closes
that gap.

In steady state with a healthy socket, ``created`` MUST be 0 on every sweep —
the socket got there first and every insert is an ``INSERT OR IGNORE`` no-op.
``created > 0`` is therefore not a heuristic but a CONSEQUENCE: it is proof the
socket missed something. It is recorded on the source row
(``reconciled_total`` / ``reconciled_last_at`` / ``reconciled_last_count``) and
logged at WARNING so an operator sees it without parsing the JSON line.

WHY THE CURSOR IS PER CHANNEL, AND CLAMPED TO THE PASS START
------------------------------------------------------------
A single global ``max(ts)`` cursor is WRONG for a multi-channel sweep, and the
bug is silent, permanent loss:

    C1 is read (empty) -> a message lands in C1 at ts=100.5 -> C2 is read and
    yields ts=100.9 -> the global cursor jumps to 100.9 -> C1's 100.5 is now
    BELOW the cursor and is never requested again by any future pass.

Two independent guards, because this sweep is the system's only durability
mechanism for Slack:

1. ``config["last_poll_ts_by_channel"][channel_id]`` — each channel advances only
   over history that channel actually returned. A channel whose read FAILED
   keeps its window (``setdefault`` pins the boundary it used), so a per-channel
   error can never be skipped past by another channel's progress.
2. Every cursor is clamped to ``pass_started``, sampled BEFORE the first HTTP
   call. Anything that arrives while the pass is walking channels is re-read on
   the next pass and absorbed for free by ``INSERT OR IGNORE``.

``config["last_poll_ts"]`` survives as the FLOOR for a channel this sweep has
never read before (newly created, or newly joined). It is set to ``pass_started``
of the previous successful pass, so a newly discovered channel is read from one
sweep interval ago rather than from the beginning of time.

ONE UNREADABLE CHANNEL MUST NEVER STOP THE SWEEP
------------------------------------------------
``conversations.list`` returns every public channel in the workspace, not just
the ones the bot is in, and ``conversations.history`` answers ``not_in_channel``
for the rest. A pass that raises on the first such channel reconciles NOTHING,
forever — the safety net dead on arrival while the socket row stays green. So:
non-member channels are skipped by ``is_member``, and every per-channel failure
is counted onto the source row (``channel_errors``) and stepped over.

RATE LIMITS ARE EXPECTED, NOT EXCEPTIONAL
-----------------------------------------
Slack's 2025 rate-limit change caps ``conversations.history`` far more tightly
for non-Marketplace apps created after 2025-05-29. A 429 is therefore an
ordinary event on this path, and it is retried with the ``Retry-After`` the API
supplies (bounded per request and per pass) rather than raised — an unhandled
429 would abort the whole pass exactly like the ``not_in_channel`` above.

Two honesty caveats, in the code because they are easy to forget:

* ``created == 0`` is NOT proof the socket is alive — ``conversations.history``
  never returns THREAD REPLIES, so a reply lost during a socket outage can never
  be backfilled here. Liveness is the health sentinel's job, not this sweep's.
* the socket client must NEVER write ``config["last_poll_ts"]`` on this row. It
  owns a separate ``slack-socket`` row for exactly that reason.
"""

from __future__ import annotations

import logging
import os
import re
import time
from collections.abc import Callable, Mapping
from typing import Any

import httpx

from omniagentos.comms.normalize import normalize_slack
from omniagentos.contracts import utc_now_iso
from omniagentos.steward.store import StewardStore

__all__ = ["is_self_message", "poll_once"]

logger = logging.getLogger(__name__)

KIND = "slack"
TOKEN_ENV = "SLACK_BOT_TOKEN"
_API_BASE = "https://slack.com/api"

#: Retries for ONE rate-limited request. Beyond this the request fails, which is
#: now a per-channel failure rather than a whole-pass abort.
RATE_LIMIT_RETRIES = 3
#: Never honour an absurd ``Retry-After``; a launchd sweep must not pin a process
#: for an hour. Exceeding the budget degrades that channel to a counter.
RATE_LIMIT_SLEEP_CAP_SECONDS = 60.0
#: Total seconds one pass may spend sleeping on 429s, across all channels.
RATE_LIMIT_PASS_BUDGET_SECONDS = 120.0
#: How many per-channel failures are recorded verbatim on the source row.
CHANNEL_ERROR_RECORD_LIMIT = 10
#: Only public channels: the app has no ``groups:history`` (private) scope, so
#: asking for private conversations produces guaranteed ``not_in_channel`` noise.
CHANNEL_TYPES = "public_channel"

#: Bot id of THIS app's own bot user (``initech_jira`` in the "AI Pro
#: University" workspace). Not a credential — a bot id is public metadata that
#: appears on every message the bot posts. Overridable so a second workspace
#: does not need a code change; the name is deliberately NOT credential-shaped
#: (see tests/llm/test_unbrokered_credentials.py::_CREDENTIAL_NAME_RE).
SELF_BOT_ID_ENV = "OMNIAGENTOS_SLACK_SELF_BOT_ID"
DEFAULT_SELF_BOT_ID = "B0000EXAMPLE"


def _self_bot_id() -> str:
    return (os.environ.get(SELF_BOT_ID_ENV) or DEFAULT_SELF_BOT_ID).strip()


def is_self_message(event: Mapping[str, Any]) -> bool:
    """True when this Slack message was posted by THIS app's own bot user.

    Shared by BOTH ingest paths on purpose. A self-filter applied to only one of
    them would make the reconciliation counter permanently non-zero (the sweep
    would keep "catching" messages the socket deliberately dropped), which
    destroys the one signal that proves the socket is not missing real traffic.

    Deliberately narrow: it matches OUR bot id, never the ``bot_message``
    subtype in general. Other bots' messages (this workspace's whole reason for
    a Jira bot) are real content and are ingested by both paths.
    """
    own = _self_bot_id()
    return bool(own) and str(event.get("bot_id") or "") == own


def _stored_config(steward: StewardStore, name: str) -> dict[str, Any]:
    sources = {row["name"]: row for row in steward.list_comms_sources()}
    config = (sources.get(name) or {}).get("config") or {}
    return dict(config) if isinstance(config, Mapping) else {}


def _stored_since(steward: StewardStore, name: str) -> str:
    return str(_stored_config(steward, name).get("last_poll_ts") or "0")


def _as_float(value: str) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


#: Anything token-shaped, whether or not this process holds that exact token.
#: ``_sanitize`` can only strip a token it was handed; this catches the rest
#: (a token echoed by a proxy, an SDK error quoting a header) on paths that have
#: no token in scope at all, such as the socket client's ingest error handler.
_TOKEN_RE = re.compile(r"\bx(?:oxb|oxp|oxa|oxs|app)-[A-Za-z0-9-]+")


def _sanitize(message: str, token: str | None) -> str:
    """Strip the bot token, if it appears verbatim, from an error message.

    The token is sent as an ``Authorization: Bearer <token>`` header rather
    than in the URL, but is stripped defensively anyway in case it is ever
    echoed back (e.g. a future refactor that logs headers, or a proxy that
    reflects them in an error body).
    """
    return message.replace(token, "[REDACTED]") if token else message


def scrub_tokens(message: str) -> str:
    """Redact anything Slack-token-shaped, with no token in hand to compare to."""
    return _TOKEN_RE.sub("[REDACTED]", message)


def _error_detail(exc: Exception) -> str:
    """A fixed, non-secret-bearing shape for the exception — never ``str(exc)``
    for an HTTP-status failure, since httpx embeds the full request URL/body."""
    if isinstance(exc, httpx.HTTPStatusError):
        return f"Slack API error (HTTP {exc.response.status_code})"
    if isinstance(exc, httpx.TimeoutException):
        return "Slack API error (timeout)"
    if isinstance(exc, httpx.RequestError):
        return f"Slack API error (request failed: {type(exc).__name__})"
    return str(exc)


def _next_cursor(payload: dict[str, Any]) -> str:
    return str((payload.get("response_metadata") or {}).get("next_cursor") or "")


def _retry_after(response: httpx.Response) -> float:
    """Slack's own back-pressure instruction, in seconds. Never trusted blindly."""
    raw = response.headers.get("Retry-After") or response.headers.get("retry-after") or ""
    try:
        return max(0.0, float(str(raw).strip()))
    except (TypeError, ValueError):
        return 1.0


class _RateLimitBudget:
    """How much sleeping ONE pass is allowed to do on 429s, in total.

    A per-request cap alone is not enough: 23 channels each honouring a 30s
    ``Retry-After`` would pin a launchd job for eleven minutes and overlap the
    next sweep. When the budget is gone the request fails, which degrades that
    channel to a counted error instead of taking the pass down.
    """

    def __init__(self, total: float = RATE_LIMIT_PASS_BUDGET_SECONDS) -> None:
        self.remaining = total
        self.waits = 0
        self.slept = 0.0

    def spend(self, seconds: float) -> float | None:
        delay = max(0.0, min(seconds, RATE_LIMIT_SLEEP_CAP_SECONDS))
        if delay > self.remaining:
            return None
        self.remaining -= delay
        self.slept += delay
        self.waits += 1
        return delay


def _slack_get(
    http_client: httpx.Client,
    method: str,
    *,
    headers: Mapping[str, str],
    params: Mapping[str, Any],
    budget: _RateLimitBudget,
    sleep: Callable[[float], None],
) -> dict[str, Any]:
    """One Slack Web API GET, retried on rate limiting, raising on anything else.

    Slack signals rate limiting two ways — HTTP 429 with ``Retry-After``, and
    (on some paths/proxies) HTTP 200 with ``{"ok": false, "error":
    "ratelimited"}``. Both are handled here so no caller has to remember.
    """
    attempt = 0
    while True:
        response = http_client.get(f"{_API_BASE}/{method}", headers=dict(headers), params=params)
        payload: dict[str, Any] = {}
        rate_limited = response.status_code == 429
        if not rate_limited:
            response.raise_for_status()
            payload = response.json()
            rate_limited = (
                not payload.get("ok", False) and str(payload.get("error") or "") == "ratelimited"
            )
        if rate_limited:
            delay = budget.spend(_retry_after(response)) if attempt < RATE_LIMIT_RETRIES else None
            if delay is None:
                raise RuntimeError(
                    f"Slack API error: ratelimited ({method}, retry budget exhausted)"
                )
            logger.info("slack %s rate-limited; sleeping %.1fs (Retry-After)", method, delay)
            sleep(delay)
            attempt += 1
            continue
        if not payload.get("ok", False):
            raise RuntimeError(f"Slack API error: {payload.get('error')}")
        return payload


def _list_channels(
    http_client: httpx.Client,
    headers: Mapping[str, str],
    *,
    budget: _RateLimitBudget,
    sleep: Callable[[float], None],
) -> list[dict[str, Any]]:
    """Every page of ``conversations.list``. Raising here IS a whole-pass failure:
    without the channel list there is nothing to sweep and no cursor may move."""
    channels: list[dict[str, Any]] = []
    cursor = ""
    while True:
        params: dict[str, Any] = {
            "limit": 200,
            # Without `types` Slack defaults to public_channel anyway, but the
            # default is Slack's to change; pin it. `exclude_archived` drops
            # channels that can never receive another message.
            "types": CHANNEL_TYPES,
            "exclude_archived": "true",
        }
        if cursor:
            params["cursor"] = cursor
        payload = _slack_get(
            http_client,
            "conversations.list",
            headers=headers,
            params=params,
            budget=budget,
            sleep=sleep,
        )
        listed = payload.get("channels")
        if isinstance(listed, list):
            channels.extend(item for item in listed if isinstance(item, dict))
        cursor = _next_cursor(payload)
        if not cursor:
            return channels


def _sweep_channel(
    steward: StewardStore,
    http_client: httpx.Client,
    headers: Mapping[str, str],
    *,
    channel_id: str,
    since: str,
    counts: dict[str, int],
    budget: _RateLimitBudget,
    sleep: Callable[[float], None],
) -> str:
    """Read one channel's history since ``since``; return the newest ts observed.

    ``counts`` is mutated as work completes rather than returned, so a failure
    part-way through a paginated channel still reports what it managed to store.
    """
    latest = since
    cursor = ""
    while True:
        # `oldest` is fixed once per channel to the poll's `since` boundary —
        # only `cursor` changes across pages — so pagination never widens the
        # requested time window and never drops a message above `since` that
        # happened to land on a later page.
        params: dict[str, Any] = {"channel": channel_id, "oldest": since}
        if cursor:
            params["cursor"] = cursor
        payload = _slack_get(
            http_client,
            "conversations.history",
            headers=headers,
            params=params,
            budget=budget,
            sleep=sleep,
        )
        for event in payload.get("messages", []):
            if not isinstance(event, Mapping):
                continue
            counts["fetched"] += 1
            ts = str(event.get("ts") or "0")
            # The cursor advances over a self-message too: it was seen, it is
            # just not ingested. Not advancing would re-read it forever.
            if _as_float(ts) > _as_float(latest):
                latest = ts
            if is_self_message(event):
                continue
            message = normalize_slack(event, channel=channel_id)
            _row, was_created = steward.insert_comms_message(message)
            if was_created:
                counts["created"] += 1
        cursor = _next_cursor(payload)
        if not cursor or not bool(payload.get("has_more", False)):
            return latest


def _channel_cursors(config: Mapping[str, Any]) -> dict[str, str]:
    stored = config.get("last_poll_ts_by_channel")
    if not isinstance(stored, Mapping):
        return {}
    return {str(key): str(value) for key, value in stored.items()}


def _clamp_cursor(latest: str, floor: str, pass_started: float) -> str:
    """Never below the window we asked for, never above when the pass STARTED.

    The upper clamp is the fix for the interleaving loss: a message that lands
    in an already-read channel while a later channel is still being read must be
    re-read next pass, and it only is if the cursor never passes ``pass_started``.
    """
    value = _as_float(latest)
    if value <= _as_float(floor):
        return floor
    if value > pass_started:
        return floor if _as_float(floor) >= pass_started else f"{pass_started:.6f}"
    return latest


def poll_once(
    steward: StewardStore,
    name: str = "slack",
    *,
    client: httpx.Client | None = None,
    sleep: Callable[[float], None] = time.sleep,
    now: Callable[[], float] = time.time,
) -> dict[str, Any]:
    """Sweep every public channel the bot is a MEMBER of since that channel's cursor."""
    token = os.environ.get(TOKEN_ENV)
    if not token:
        error = f"missing environment variable: {TOKEN_ENV}"
        steward.upsert_comms_source(name, KIND, status="pending_setup", last_error=error)
        return {
            "source": name,
            "status": "pending_setup",
            "error": error,
            "fetched": 0,
            "created": 0,
        }

    previous_config = _stored_config(steward, name)
    global_since = str(previous_config.get("last_poll_ts") or "0")
    cursors = _channel_cursors(previous_config)
    # Sampled BEFORE the first request: everything above it is re-read next pass.
    pass_started = float(now())
    headers = {"Authorization": f"Bearer {token}"}
    owns_client = client is None
    http_client = client or httpx.Client(timeout=15)
    budget = _RateLimitBudget()
    counts = {"fetched": 0, "created": 0}
    channel_errors: list[str] = []
    listed_ids: set[str] = set()
    member_channels = 0
    swept = 0

    def _failed(exc: Exception) -> dict[str, Any]:
        error = _sanitize(f"slack poll failed: {_error_detail(exc)}", token)
        logger.warning(error)
        steward.upsert_comms_source(name, KIND, status="error", last_error=error)
        return {
            "source": name,
            "status": "error",
            "error": error,
            "fetched": counts["fetched"],
            "created": counts["created"],
        }

    try:
        try:
            channels = _list_channels(http_client, headers, budget=budget, sleep=sleep)
        except Exception as exc:
            return _failed(exc)

        for channel in channels:
            channel_id = str(channel.get("id") or "")
            if not channel_id:
                continue
            listed_ids.add(channel_id)
            # `conversations.history` on a channel the bot is not in answers
            # `not_in_channel`. Asking anyway wastes the rate-limit budget and,
            # before this guard, killed the whole pass on the first one.
            if channel.get("is_member") is not True:
                continue
            member_channels += 1
            since = cursors.get(channel_id) or global_since
            try:
                latest = _sweep_channel(
                    steward,
                    http_client,
                    headers,
                    channel_id=channel_id,
                    since=since,
                    counts=counts,
                    budget=budget,
                    sleep=sleep,
                )
            except Exception as exc:  # noqa: BLE001 -- one channel, not the sweep.
                channel_errors.append(f"{channel_id}: {_sanitize(_error_detail(exc), token)}")
                # Pin the window this channel was reading so no OTHER channel's
                # progress (or the global floor's advance below) can skip past it.
                cursors.setdefault(channel_id, since)
                continue
            swept += 1
            cursors[channel_id] = _clamp_cursor(latest, since, pass_started)
    finally:
        if owns_client:
            http_client.close()

    if member_channels and not swept:
        # Every readable channel failed: nothing was reconciled, so nothing may
        # advance — not the cursors, and not `last_poll_at`, which is the
        # sentinel's proof that the safety net ran.
        error = "slack poll failed: every member channel errored — " + "; ".join(
            channel_errors[:CHANNEL_ERROR_RECORD_LIMIT]
        )
        logger.warning(error)
        steward.upsert_comms_source(name, KIND, status="error", last_error=error)
        return {
            "source": name,
            "status": "error",
            "error": error,
            "fetched": counts["fetched"],
            "created": counts["created"],
        }

    # Reconciliation bookkeeping (purely additive; `_SOURCE_FIELDS` already
    # permits arbitrary `config` JSON, so no migration is involved). Written on
    # the SAME success path as the cursors, so a failed poll changes neither.
    created = counts["created"]
    stamp = utc_now_iso()
    config: dict[str, Any] = dict(previous_config)
    # Prune cursors for channels the (authoritative, fully paginated) list no
    # longer contains, so the blob cannot grow without bound. A channel merely
    # missing `is_member` keeps its cursor, so leaving and rejoining resumes
    # where it left off.
    config["last_poll_ts_by_channel"] = {
        channel_id: cursor for channel_id, cursor in cursors.items() if channel_id in listed_ids
    }
    # The FLOOR for channels never read before — not a message cursor. Every
    # known channel reads from its own entry above.
    config["last_poll_ts"] = f"{pass_started:.6f}"
    config["reconciled_total"] = int(previous_config.get("reconciled_total") or 0) + created
    config["reconciled_last_count"] = created
    config["member_channels"] = member_channels
    config["channels_listed"] = len(listed_ids)
    config["channels_swept"] = swept
    config["channel_errors"] = channel_errors[:CHANNEL_ERROR_RECORD_LIMIT]
    config["channel_error_count"] = len(channel_errors)
    config["rate_limit_waits"] = budget.waits
    if created:
        config["reconciled_last_at"] = stamp
        logger.warning(
            "slack reconciliation caught %d message(s) the socket missed "
            "(channels swept=%d of %d member, %d listed)",
            created,
            swept,
            member_channels,
            len(listed_ids),
        )
    else:
        config.setdefault("reconciled_last_at", "")
    if channel_errors:
        logger.warning(
            "slack sweep skipped %d channel(s) it could not read: %s",
            len(channel_errors),
            "; ".join(channel_errors[:CHANNEL_ERROR_RECORD_LIMIT]),
        )

    steward.upsert_comms_source(
        name,
        KIND,
        status="active",
        config=config,
        last_poll_at=stamp,
        last_error=(
            "" if not channel_errors else "unreadable channels: " + "; ".join(channel_errors[:3])
        ),
    )
    return {
        "source": name,
        "status": "active",
        "error": "",
        "fetched": counts["fetched"],
        "created": created,
        "member_channels": member_channels,
        "channels_swept": swept,
        "channel_errors": len(channel_errors),
    }
