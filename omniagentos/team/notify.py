"""Slack queue DMs and the short-interval Team Work OS event watcher.

``--morning`` renders each mapped employee's queue as a direct message.
``--pulse`` renders one compact, channel-wide queue snapshot; the FIRST pulse
of a local day renders the channel-wide morning brief instead (per-company
queue + per-person load), tracked by a var-root state file.
``--daybrief`` posts that morning brief explicitly (``--test`` prefixes the
header so a live demo post is unambiguous).
``--watch-once`` performs exactly one bounded event scan for launchd.  Its
cursor is the append-only ``task_events.rowid`` rather than a UUID or timestamp:
the store deliberately uses rowid to preserve events written in one second.

This module deliberately has no sleep loop.  launchd owns the five-minute
cadence, and Slack failures are stderr diagnostics rather than exceptions that
could stop the next run.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import tempfile
import time
import urllib.error
import urllib.request
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from omniagentos.collab.contracts import BASELINE_SOURCE, BoardTaskStatus
from omniagentos.contracts import default_db_path
from omniagentos.runtime_paths import resolve_var_root
from omniagentos.team import commitments as team_commitments
from omniagentos.team import tasks as team_tasks
from omniagentos.team.contracts import (
    NORTH_STAR,
    OPERATOR_EMPLOYEE_ID,
    POOL_CARD_LIMIT,
    READY_QUEUE_FLOOR,
    QueueCard,
    TeamQueueBuckets,
)
from omniagentos.team.report import CHANNEL_ENV, DEFAULT_CHANNEL, load_slack_env
from omniagentos.team.slack_updates import load_slack_map
from omniagentos.team.store import _COMPANY_JOIN, BOARD_TABLE, EVENTS_TABLE, TeamStore

_CHAT_POST_MESSAGE_URL = "https://slack.com/api/chat.postMessage"
_CONVERSATIONS_OPEN_URL = "https://slack.com/api/conversations.open"
_CURSOR_FILENAME = "team-notify-cursor.json"
_INFERENCE_EVENTS = frozenset({"create", "status_change", "verify"})
_POOL_STATUSES = (BoardTaskStatus.PENDING.value, BoardTaskStatus.OPEN.value)
_URL_RE = re.compile(r"https?://\S+", re.IGNORECASE)
_TOKEN_RE = re.compile(r"\b(?:xox[baprs]-|sk-|api[_-]?key[=:])\S+", re.IGNORECASE)
# Slack broadcast/user mentions (<!channel>, <!here>, <@U…>, <#C…>) must never
# survive egress: a card TITLE containing one would ping the whole channel.
_MENTION_RE = re.compile(r"<[!@#][^>]{0,60}>")
_OWNER_TOKEN_RE = re.compile(r"(?:^|\s)owner:([A-Za-z0-9_-]+|none)(?=\s|$)")
_TERMINAL_SLACK_ERRORS = frozenset(
    {"user_not_found", "channel_not_found", "is_archived", "account_inactive"}
)
_TRANSIENT_SLACK_ERRORS = frozenset({"rate_limited", "ratelimited", "internal_error"})
_EVENT_LIMIT = 500
try:  # p1's C1 contract adds the active-only floor.
    from omniagentos.team.contracts import ACTIVE_QUEUE_FLOOR
except ImportError:  # Compatibility with the pre-C1 contracts during train assembly.
    ACTIVE_QUEUE_FLOOR = READY_QUEUE_FLOOR
try:  # p1's C1 contract owns the available-pool floor.
    from omniagentos.team.contracts import POOL_DEPTH_FLOOR  # type: ignore[attr-defined]
except ImportError:  # Compatibility with the pre-C1 contracts during train assembly.
    POOL_DEPTH_FLOOR = 10

PULSE_CHANNEL_ENV = "OMNI_TEAM_PULSE_CHANNEL"
REPO_ROOT_PATH = Path("/Users/youruser/OmniAgentOS")

# --- Morning brief (multi-company Work OS v3, 2026-08-13) --------------------
# The five companies, in the FIXED order the brief renders them (spec:
# devtasks/multi-company-workos-0813/PLAN-v3-task-commands.md 'Alerts'). Slugs
# are ``org_companies.slug`` values (the same vocabulary as
# ``configs/company_repos.yaml``); the display names are what a human scans.
COMPANY_ORDER: tuple[tuple[str, str], ...] = (
    ("globex", "Globex"),
    ("acmeuni", "AcmeUni"),
    ("hooli", "Hooli"),
    ("initech", "Initech"),
    ("omniagentos", "OmniAgentOS"),
)
#: Priority glyphs (spec): 🔥 urgent / ⬆ high / • normal / ⬇ low.
PRIORITY_GLYPHS: Mapping[str, str] = {"urgent": "🔥", "high": "⬆", "normal": "•", "low": "⬇"}
#: One shared claim/assign footer on every channel alert.
TASK_FOOTER = "📌 claim: /task claim <REF> · assign: /task assign @name <REF> · help: /task help"
#: Phone-scannable caps: top cards shown per company / per person ('+N more').
_BRIEF_COMPANY_CARD_LIMIT = 5
_BRIEF_PERSON_CARD_LIMIT = 5
#: Display order for the per-person section (spec: the operator, Alice, Bob). These
#: are EMPLOYEE ids (roster facts), never Slack user ids — the Slack side is
#: always resolved through ``configs/team_slack_map.yaml``. Unknown ids sort
#: after, alphabetically, so a roster change degrades to a stable order.
_PERSON_DISPLAY_ORDER: tuple[str, ...] = (OPERATOR_EMPLOYEE_ID, "emp_alice", "emp_bob")
_DAYBRIEF_STATE_FILENAME = "team-daybrief-state.json"


def _safe_title(value: object) -> str:
    """Sanitize the small textual egress surface shared by every Slack post."""
    cleaned = _TOKEN_RE.sub("[token omitted]", _URL_RE.sub("[link omitted]", str(value or "")))
    return _MENTION_RE.sub("[mention omitted]", cleaned)


def cursor_path(override: str | Path | None = None) -> Path:
    """The event-watch cursor under the normal runtime var root."""
    return Path(override) if override is not None else Path(resolve_var_root()) / _CURSOR_FILENAME


def _slack_call(
    url: str, payload: Mapping[str, Any], token: str, *, timeout: int = 30
) -> dict[str, Any]:
    """Make one Slack API call and retain a machine-readable failure category."""
    try:
        request = urllib.request.Request(
            url,
            data=json.dumps(dict(payload)).encode("utf-8"),
            headers={
                "Content-Type": "application/json; charset=utf-8",
                "Authorization": f"Bearer {token}",
            },
        )
        with urllib.request.urlopen(request, timeout=timeout) as response:
            response_payload = json.load(response)
    except urllib.error.HTTPError as exc:
        retry_after = exc.headers.get("Retry-After") if exc.headers is not None else None
        http_error = "rate_limited" if exc.code == 429 else f"http_{exc.code}"
        print(f"team-notify: Slack API call failed: {http_error}", file=sys.stderr)
        return {"ok": False, "_notify_error": http_error, "_retry_after": retry_after}
    except (urllib.error.URLError, OSError, TypeError, ValueError) as exc:
        print(f"team-notify: Slack API call failed: {exc}", file=sys.stderr)
        return {"ok": False, "_notify_error": "transport"}
    if not isinstance(response_payload, dict) or not response_payload.get("ok"):
        error: object = (
            response_payload.get("error") if isinstance(response_payload, dict) else "bad response"
        )
        print(f"team-notify: Slack API error: {error}", file=sys.stderr)
        return {
            "ok": False,
            "_notify_error": str(error or "unknown"),
            "_retry_after": response_payload.get("retry_after")
            if isinstance(response_payload, dict)
            else None,
        }
    return response_payload


class SlackNotifier:
    """A tiny per-run Slack client; opened DM channels are cached by Slack user id."""

    def __init__(self, token: str, *, channel: str | None = None) -> None:
        self.token = token
        self.channel = channel or os.environ.get(CHANNEL_ENV) or DEFAULT_CHANNEL
        self._dm_channels: dict[str, str] = {}
        self.last_error: str | None = None
        self.retry_after: float | None = None

    def _call(self, url: str, payload: Mapping[str, Any]) -> dict[str, Any] | None:
        result = _slack_call(url, payload, self.token)
        if result.get("ok"):
            self.last_error = None
            self.retry_after = None
            return result
        self.last_error = str(result.get("_notify_error") or "unknown")
        raw_retry = result.get("_retry_after")
        try:
            self.retry_after = max(0.0, float(raw_retry)) if raw_retry is not None else None
        except (TypeError, ValueError):
            self.retry_after = None
        return None

    @staticmethod
    def _payload(channel: str, text: str, blocks: list | None, color: str | None) -> dict:
        payload: dict[str, Any] = {
            "channel": channel,
            "text": _safe_title(text),
            "unfurl_links": False,
            "unfurl_media": False,
        }
        if blocks:
            # Blocks ride in one attachment so the color side-bar applies; the
            # top-level text stays the audited plain rendering (notifications
            # and no-blocks surfaces show exactly what they always did).
            payload["attachments"] = [{"color": color or "#868686", "blocks": blocks}]
        return payload

    def post_channel(
        self, text: str, *, blocks: list | None = None, color: str | None = None
    ) -> bool:
        return (
            self._call(_CHAT_POST_MESSAGE_URL, self._payload(self.channel, text, blocks, color))
            is not None
        )

    def post_dm(
        self,
        slack_user_id: str,
        text: str,
        *,
        blocks: list | None = None,
        color: str | None = None,
    ) -> bool:
        channel = self.open_dm(slack_user_id)
        if channel is None:
            return False
        return (
            self._call(_CHAT_POST_MESSAGE_URL, self._payload(channel, text, blocks, color))
            is not None
        )

    def open_dm(self, slack_user_id: str) -> str | None:
        """Open/reopen and return a DM channel so durable callers can persist it."""
        channel = self._dm_channels.get(slack_user_id)
        if channel is not None:
            return channel
        opened = self._call(_CONVERSATIONS_OPEN_URL, {"users": slack_user_id})
        raw_channel = opened.get("channel") if opened is not None else None
        channel_id = raw_channel.get("id") if isinstance(raw_channel, dict) else None
        if not channel_id:
            print(
                f"team-notify: conversations.open returned no channel for {slack_user_id!r}",
                file=sys.stderr,
            )
            return None
        channel = str(channel_id)
        self._dm_channels[slack_user_id] = channel
        return channel


class _DryRunNotifier:
    """Slack-shaped preview sink used by the event watcher without network I/O."""

    last_error: str | None = None
    retry_after: float | None = None

    def post_channel(
        self, text: str, *, blocks: list | None = None, color: str | None = None
    ) -> bool:
        print(
            json.dumps(
                {
                    "channel": "event-watch",
                    "text": _safe_title(text),
                    "blocks": len(blocks or []),
                    "color": color,
                }
            )
        )
        return True

    def post_dm(
        self,
        slack_user_id: str,
        text: str,
        *,
        blocks: list | None = None,
        color: str | None = None,
    ) -> bool:
        print(
            json.dumps(
                {
                    "slack_user_id": slack_user_id,
                    "text": _safe_title(text),
                    "blocks": len(blocks or []),
                    "color": color,
                }
            )
        )
        return True


def _card_line(card: QueueCard | Mapping[str, Any], *, claim_cta: bool = True) -> str:
    if isinstance(card, Mapping):
        ref = card.get("ref")
        title = card.get("title")
        card_id = card.get("id")
    else:
        ref, title, card_id = card.ref, card.title, card.id
    identifier = ref or card_id
    hint = (
        f" claim {identifier}"
        if claim_cta and not ref and str(identifier).startswith("btk_")
        else ""
    )
    return f'{identifier} "{_safe_title(title)}"{hint}'


def _pool_cards(store: TeamStore, *, limit: int = 5) -> list[dict[str, Any]] | None:
    """Use the integration helper when present; otherwise make the allowed read-only seam query."""
    helper = getattr(store, "pool_cards", None)
    try:
        if callable(helper):
            try:
                cards = helper(limit=limit)
            except TypeError:  # Legacy p1 helper accepted no keyword argument.
                cards = helper()
            return [dict(card) for card in cards[:limit]]
        rows = store._connection.execute(
            f"SELECT id, ref, title, status, source, due_date, created_at FROM {BOARD_TABLE} "
            "WHERE status = ? AND owner_employee_id IS NULL AND archived_at IS NULL "
            "AND parent_task_id IS NULL AND source <> ? AND goal_id IS NOT NULL "
            "AND TRIM(acceptance_criteria) <> '' "
            "ORDER BY created_at ASC, id ASC LIMIT ?",
            (BoardTaskStatus.OPEN.value, BASELINE_SOURCE, limit),
        ).fetchall()
        return [dict(row) for row in rows]
    except Exception as exc:  # The morning message must survive a pool read outage.
        print(f"team-notify: pool unavailable: {exc}", file=sys.stderr)
        return None


def _pool_depth(store: TeamStore) -> int | None:
    """Return the available-pool count using the same eligibility as ``_pool_cards``."""
    try:
        if hasattr(store, "pool_depth"):
            depth = store.pool_depth()  # type: ignore[attr-defined]
            return int(depth)
        row = store._connection.execute(
            f"SELECT COUNT(*) AS depth FROM {BOARD_TABLE} "
            "WHERE status = ? AND owner_employee_id IS NULL AND archived_at IS NULL "
            "AND parent_task_id IS NULL AND source <> ? AND goal_id IS NOT NULL "
            "AND TRIM(acceptance_criteria) <> ''",
            (BoardTaskStatus.OPEN.value, BASELINE_SOURCE),
        ).fetchone()
        return 0 if row is None else int(row["depth"])
    except Exception as exc:
        print(f"team-notify: pool depth unavailable: {exc}", file=sys.stderr)
        return None


def _active_below_floor(bucket: TeamQueueBuckets) -> bool:
    flagged = getattr(bucket, "active_below_5", None)
    return len(bucket.active) < ACTIVE_QUEUE_FLOOR if flagged is None else bool(flagged)


def render_morning_message(
    bucket: TeamQueueBuckets, pool: Sequence[Mapping[str, Any]] | None
) -> str:
    """One deterministic queue DM, including empty buckets so no state is implied."""

    def section(title: str, cards: Sequence[QueueCard]) -> list[str]:
        lines = [title]
        lines.extend(f"• {_card_line(card)}" for card in cards) if cards else lines.append(
            "• (none)"
        )
        return lines

    lines = ["*Morning queue*"]
    lines.extend(section("Active (claimed + in progress):", bucket.active))
    lines.extend(section("Assigned open:", bucket.ready))
    if _active_below_floor(bucket):
        lines.append(
            f"Capacity: {len(bucket.active)} of {ACTIVE_QUEUE_FLOOR} active — room for more."
        )
    lines.append("Pool — grab one: reply `claim <REF>` (or `claim btk_…`)")
    if pool is None:
        lines.append("• pool unavailable")
    elif pool:
        lines.extend(f"• {_card_line(card)}" for card in pool)
    else:
        lines.append("• (none)")
    return "\n".join(lines)


def _reverse_slack_map(slack_map: Mapping[str, str]) -> dict[str, str]:
    reverse: dict[str, str] = {}
    for slack_id, employee_id in slack_map.items():
        previous = reverse.setdefault(employee_id, slack_id)
        if previous != slack_id:
            print(
                f"team-notify: Slack map collision for {employee_id!r}: keeping {previous!r}, "
                f"ignoring {slack_id!r}",
                file=sys.stderr,
            )
    return reverse


def morning_messages(store: TeamStore) -> list[tuple[str, str, str]]:
    """Return ``(employee_id, slack_user_id, message)`` triples for mapped employees."""
    return [
        (employee_id, slack_user_id, message)
        for employee_id, slack_user_id, message, _bucket, _pool in _morning_gathered(store)
    ]


def _morning_gathered(
    store: TeamStore,
) -> list[tuple[str, str, str, TeamQueueBuckets, list | None]]:
    """One gather for both renderings — text and blocks can never diverge."""
    reverse_map = _reverse_slack_map(load_slack_map())
    pool = _pool_cards(store)
    rows: list[tuple[str, str, str, TeamQueueBuckets, list | None]] = []
    for employee_id, bucket in store.team_queues().items():
        slack_user_id = reverse_map.get(employee_id)
        if slack_user_id is None:
            print(f"team-notify: morning skip {employee_id!r}: no Slack mapping", file=sys.stderr)
            continue
        rows.append(
            (employee_id, slack_user_id, render_morning_message(bucket, pool), bucket, pool)
        )
    return rows


def _morning_blocks(
    employee_id: str,
    bucket: TeamQueueBuckets,
    pool: list | None,
    commitment_lines: list[str] | None = None,
    *,
    targets_line: str | None = None,
) -> tuple[str, list]:
    """Styled companion to render_morning_message, from the SAME gathered data."""
    from omniagentos.team import slack_blocks  # local: slack_blocks imports this module

    capacity = f"capacity: {len(bucket.active)} of {ACTIVE_QUEUE_FLOOR} active"
    if _active_below_floor(bucket):
        capacity += " — room for more"
    pool_lines = (
        ["• pool unavailable"] if pool is None else [f"• {_card_line(card)}" for card in pool]
    )
    return slack_blocks.morning_dm_blocks(
        employee_id.removeprefix("emp_").capitalize(),
        [f"• {_card_line(card)}" for card in bucket.active],
        [f"• {_card_line(card)}" for card in bucket.ready],
        capacity,
        pool_lines,
        stamp="morning queue · reply `claim <REF>` / `my queue` here",
        commitments_lines=commitment_lines,
        targets_line=targets_line,
    )


def _generate_daily_commitments(store: TeamStore) -> tuple[list[dict[str, Any]], bool]:
    """Run the ONE daily commitments orchestration for the morning job (M3).

    ``commitments.run_daily`` is the single order the design names: resolve
    yesterday, then generate today (S2) — calling it here, once, before the
    per-employee DM loop, is what keeps the 06:55 job and the 07:00 report
    agreeing on that order without either module reaching into the other.

    Fail-safe but NEVER silently favourable: an exception here must not stop
    a single DM from sending, so it is caught and reported through the return
    value, never raised — but the caller must render an explicit "unavailable"
    state, never an absent or falsely-empty section (a generation outage must
    read as an outage, not as "nobody has any commitments today").
    """
    try:
        result = team_commitments.run_daily(store)
        return list(result.get("generated") or []), False
    except Exception as exc:  # defensive: a commitments outage must not block the DM
        print(f"team-notify: commitments generation failed: {exc}", file=sys.stderr)
        return [], True


def _commitment_ref(store: TeamStore, task_id: Any) -> str | None:
    """Best-effort card REF for a task commitment's linked card. Never raises."""
    if not task_id:
        return None
    try:
        row = store._connection.execute(
            f"SELECT ref FROM {BOARD_TABLE} WHERE id = ?", (str(task_id),)
        ).fetchone()
    except Exception:
        return None
    return None if row is None else row["ref"]


def _commitment_task_line(store: TeamStore, row: Mapping[str, Any]) -> str:
    """One ``REF — title`` line for a 'task' commitment (deliverable 1)."""
    task_id = row.get("task_id")
    identifier = _commitment_ref(store, task_id) or task_id or "?"
    return f"{identifier} — {_safe_title(str(row.get('title') or ''))}"


def _targets_line(prefix: str = "YOUR TARGETS") -> str:
    """The one north-star line every dev-facing surface renders (2026-08-14).

    ``NORTH_STAR`` already carries its own leading 🎯 (for contexts that print
    it bare); this strips that off before re-prefixing so the glyph never
    doubles. Renders as ``🎯 YOUR TARGETS: 100% of the operator's tasks automated ·
    10× verified dev speed`` — the SAME string the daybrief and the 07:00
    report header render, so the goal reads identically everywhere.
    """
    return f"🎯 {prefix}: {NORTH_STAR.removeprefix('🎯').strip()}"


def _automations_open_line(rows: list[dict[str, Any]]) -> str | None:
    """Today's automation slots are always freshly ``committed`` — nobody has
    judged them yet, so the morning DM states the OPEN count ("N slots open
    today"), never a "N/3 shipped" ratio that would read as already-judged
    (that ratio is the RESOLVED-day rendering — see ``report.py``). ``None``
    when this dev has no automation slots today (a day with no commitments at
    all, or a pre-migration board) — omitted, never rendered as zero.
    """
    total = sum(1 for row in rows if str(row.get("kind")) == "automation")
    if total == 0:
        return None
    return f"{total} automation/skill slot{'' if total == 1 else 's'} open today"


def _commitments_lines(
    store: TeamStore,
    employee_id: str,
    *,
    generated: list[dict[str, Any]],
    generation_failed: bool,
) -> list[str]:
    """The bullet body of one person's "Today's commitments" section.

    THREE renderings, never collapsed into one another (WP-B brief — fail-safe
    but never silently favourable):

    (a) commitments exist -> one "REF — title" line per task commitment, plus
        the improvement slot as its own line;
    (b) genuinely zero commitments for this person today -> the explicit
        "no commitments recorded" line;
    (c) generation RAISED -> the explicit "⚠ commitments unavailable
        (generation failed)" line. An exception must never read as (b) — a
        silent absence is exactly the failure this section exists to prevent.

    Shared by the plain-text DM and its Block Kit companion, so the two
    surfaces can never disagree about what a person committed to.
    """
    if generation_failed:
        return ["⚠ commitments unavailable (generation failed)"]
    rows = [row for row in generated if str(row.get("employee_id")) == employee_id]
    if not rows:
        return ["no commitments recorded"]
    lines = [_commitment_task_line(store, row) for row in rows if str(row.get("kind")) == "task"]
    improvement = next((row for row in rows if str(row.get("kind")) == "improvement"), None)
    if improvement is not None:
        lines.append(f"Improvement: {_safe_title(str(improvement.get('title') or ''))}")
    automations_line = _automations_open_line(rows)
    if automations_line is not None:
        lines.append(automations_line)
    return lines


def _commitments_section(lines: list[str]) -> str:
    body = "\n".join(f"• {line}" for line in lines)
    return f"*Today's commitments*\n{body}"


def _edc_morning_section(store: TeamStore, employee_id: str) -> str | None:
    """The per-owner Executive Decision Center block for the morning DM (P1).

    Additive, lazy hook (synthesis §11): rendered by ``edc/digest.py`` from the
    SAME composed store, owner-scoped. Returns ``None`` when the owner has no
    NEEDS_OWNER/MAYBE decisions, and never raises into the morning path — a digest
    problem must not block the queue DM. Works pre-#380 (no team→edc cycle: the
    import is local and edc/digest imports nothing from team).
    """
    base = getattr(store, "_store", None)
    if base is None:
        return None
    try:
        from omniagentos.edc.digest import render_owner_section
        from omniagentos.edc.store import DecisionStore

        return render_owner_section(DecisionStore(base), employee_id)
    except Exception as exc:  # pragma: no cover - defensive; never fail morning DMs
        print(
            f"team-notify: EDC morning section skipped for {employee_id!r}: {exc}", file=sys.stderr
        )
        return None


def run_morning(store: TeamStore, notifier: SlackNotifier | None, *, dry_run: bool = False) -> bool:
    """Send (or print) all morning DMs. Returns false when any delivery failed."""
    success = True
    generated, generation_failed = _generate_daily_commitments(store)
    for employee_id, slack_user_id, message, bucket, pool in _morning_gathered(store):
        # the operator's 2026-08-14 ruling: the standing targets are FOR the devs, not
        # the operator — they set the targets, they do not read them addressed
        # at themselves (same exemption ``active_devs`` already applies).
        targets_line = _targets_line() if employee_id != OPERATOR_EMPLOYEE_ID else None
        if targets_line is not None:
            message = f"{targets_line}\n\n{message}"
        commitment_lines = _commitments_lines(
            store, employee_id, generated=generated, generation_failed=generation_failed
        )
        message = f"{message}\n\n{_commitments_section(commitment_lines)}"
        edc_section = _edc_morning_section(store, employee_id)
        if edc_section:
            message = f"{message}\n\n{edc_section}"
        safe_message = _safe_title(message)
        color, blocks = _morning_blocks(
            employee_id, bucket, pool, commitment_lines, targets_line=targets_line
        )
        if dry_run:
            print(
                json.dumps(
                    {
                        "employee_id": employee_id,
                        "slack_user_id": slack_user_id,
                        "text": safe_message,
                        "blocks": len(blocks or []),
                        "color": color,
                    }
                )
            )
        elif notifier is None or not notifier.post_dm(
            slack_user_id, safe_message, blocks=blocks, color=color
        ):
            success = False
    return success


# ---------------------------------------------------------------------------
# Morning brief (the channel-wide first pulse of the local day)
# ---------------------------------------------------------------------------

_BRIEF_TITLE_CHARS = 80


def _display_name(employee_id: str) -> str:
    """``emp_bob`` → ``Bob`` — the existing morning-DM naming idiom."""
    return employee_id.removeprefix("emp_").capitalize()


def _person_order(employee_ids: Sequence[str]) -> list[str]:
    """Fixed display order (the operator, Alice, Bob), then any newcomers by id."""
    rank = {employee_id: index for index, employee_id in enumerate(_PERSON_DISPLAY_ORDER)}
    return sorted(employee_ids, key=lambda eid: (rank.get(eid, len(rank)), eid))


def _card_field(card: QueueCard | Mapping[str, Any], name: str) -> Any:
    """One field off a card that may be a model or a mapping (the pool is dicts)."""
    if isinstance(card, Mapping):
        return card.get(name)
    return getattr(card, name, None)


def _card_handle(card: QueueCard | Mapping[str, Any]) -> str:
    """The REF a human types into ``/task claim`` — id prefix when unrefed."""
    ref = _card_field(card, "ref")
    return str(ref) if ref else str(_card_field(card, "id") or "")[:8]


# v4 (2026-08-13): the old ``_due_dates`` direct SELECT is gone — QueueCard
# projects ``due_date`` (and ``source``) from the queue reads themselves, so
# every renderer reads the deadline off the card it already holds. The glyph
# vocabulary lives in :func:`omniagentos.team.tasks.deadline_suffix`; the
# module-level alias keeps this module's rendering call sites (and tests)
# addressing it where they always did.
_deadline_suffix = team_tasks.deadline_suffix


def _brief_card_line(card: QueueCard | Mapping[str, Any], *, today: str) -> str:
    """``🔥 CF1 Fix checkout — urgent 🔴⏰2026-08-10`` (spec card grammar)."""
    priority = str(_card_field(card, "priority") or "normal")
    glyph = PRIORITY_GLYPHS.get(priority, "•")
    title = _safe_title(_card_field(card, "title"))[:_BRIEF_TITLE_CHARS]
    due = _card_field(card, "due_date")
    return f"{glyph} {_card_handle(card)} {title} — {priority}{_deadline_suffix(due, today=today)}"


def _company_depths(store: TeamStore) -> dict[str | None, int] | None:
    """Pool depth per company slug — the SAME eligibility predicate as
    ``pool_cards``/``pool_depth``, grouped over the read-only company join."""
    try:
        rows = store._connection.execute(
            f"SELECT oc.slug AS company_slug, COUNT(*) AS depth FROM {BOARD_TABLE} b "
            f"{_COMPANY_JOIN} "
            "WHERE b.status = ? AND b.owner_employee_id IS NULL AND b.archived_at IS NULL "
            "AND b.parent_task_id IS NULL AND b.source <> ? AND b.goal_id IS NOT NULL "
            "AND TRIM(b.acceptance_criteria) <> '' GROUP BY oc.slug",
            (BoardTaskStatus.OPEN.value, BASELINE_SOURCE),
        ).fetchall()
        return {
            (None if row["company_slug"] is None else str(row["company_slug"])): int(row["depth"])
            for row in rows
        }
    except Exception as exc:
        print(f"team-notify: company depths unavailable: {exc}", file=sys.stderr)
        return None


def _companies_depth_line(depths: Mapping[str | None, int]) -> str:
    """One hourly-pulse line: ``🏢 Globex 3 · AcmeUni 0 · … · other 1``."""
    known = {slug for slug, _ in COMPANY_ORDER}
    parts = [f"{name} {depths.get(slug, 0)}" for slug, name in COMPANY_ORDER]
    other = sum(depth for slug, depth in depths.items() if slug not in known)
    if other:
        parts.append(f"other {other}")
    return "🏢 " + " · ".join(parts)


def _capped_lines(lines: list[str], total: int, cap: int) -> list[str]:
    """The first ``cap`` lines plus a ``+N more`` marker for what the cap hid."""
    shown = lines[:cap]
    hidden = max(total, len(lines)) - len(shown)
    if hidden > 0:
        shown.append(f"+{hidden} more")
    return shown


def _daybrief_company_sections(
    pool: Sequence[Mapping[str, Any]] | None,
    depths: Mapping[str | None, int] | None,
    *,
    today: str,
) -> list[tuple[str, list[str]]]:
    """(section title, card lines) per company — all five, fixed order.

    Unchanged by the v4 split on purpose: the company queue IS the Work
    supply, so there is no Tasks stream to separate here.
    """
    if pool is None:
        return [("⚠ pool unavailable — queue counts unknown", [])]
    by_slug: dict[str | None, list[Mapping[str, Any]]] = {}
    for card in pool:  # pool_cards is already priority-then-age ordered
        slug = _card_field(card, "company_slug")
        by_slug.setdefault(None if slug is None else str(slug), []).append(card)
    known = {slug for slug, _ in COMPANY_ORDER}
    sections: list[tuple[str, list[str]]] = []
    for slug, name in COMPANY_ORDER:
        cards = by_slug.get(slug, [])
        count = len(cards) if depths is None else max(depths.get(slug, 0), len(cards))
        if count == 0:
            sections.append((f"*{name}* — empty", []))
            continue
        lines = [_brief_card_line(card, today=today) for card in cards]
        sections.append(
            (f"*{name}* — {count} queued", _capped_lines(lines, count, _BRIEF_COMPANY_CARD_LIMIT))
        )
    other_cards = [
        card
        for slug, cards in sorted(by_slug.items(), key=lambda kv: str(kv[0]))
        if slug not in known
        for card in cards
    ]
    if other_cards:
        lines = [_brief_card_line(card, today=today) for card in other_cards]
        sections.append(
            (
                f"*Other* — {len(other_cards)} queued",
                _capped_lines(lines, len(other_cards), _BRIEF_COMPANY_CARD_LIMIT),
            )
        )
    return sections


def _split_bucket(bucket: TeamQueueBuckets) -> tuple[list[QueueCard], dict[str, list[QueueCard]]]:
    """``(adhoc_tasks, work_by_bucket)`` — the v4 Work-vs-Tasks partition.

    Tasks gather from every live bucket (active first, so an in-progress Task
    leads its list); Work keeps the bucket structure the renderers already
    consume. One partition, used by the brief AND the pulse, so the two
    surfaces can never disagree about which stream a card is in.
    """
    adhoc: list[QueueCard] = []
    work: dict[str, list[QueueCard]] = {}
    for name in ("active", "ready", "blocked", "review"):
        cards = list(getattr(bucket, name))
        work[name] = [card for card in cards if not team_tasks.is_adhoc_task(card)]
        adhoc.extend(card for card in cards if team_tasks.is_adhoc_task(card))
    return adhoc, work


def _ongoing_work(work: Mapping[str, list[QueueCard]]) -> int:
    """Ongoing Work per the v4 spec: open + claimed + in_progress + blocked.

    ``review`` (awaiting_approval) is deliberately outside the count — the
    spec enumerates the four ongoing statuses, and a card waiting on someone
    else's approval is not a slot the owner can fill.
    """
    return len(work["ready"]) + len(work["active"]) + len(work["blocked"])


def _work_floor_line(ongoing: int) -> str:
    """``🔧 Work 3/5 ⚠ below floor`` — supply visibility, never a block."""
    warning = "" if ongoing >= ACTIVE_QUEUE_FLOOR else " ⚠ below floor"
    return f"🔧 Work {ongoing}/{ACTIVE_QUEUE_FLOOR}{warning}"


def _task_line(card: QueueCard, *, today: str) -> str:
    """``▫️ REF title ⏰deadline`` (🔴 when overdue; deadline-less = no ⏰)."""
    return (
        f"▫️ {_card_handle(card)} {_safe_title(card.title)[:_BRIEF_TITLE_CHARS]}"
        f"{_deadline_suffix(card.due_date, today=today)}"
    )


def _daybrief_person_sections(
    queues: Mapping[str, TeamQueueBuckets],
    reverse_map: Mapping[str, str],
    *,
    today: str,
) -> list[tuple[str, list[str]]]:
    """(header line, section lines) per mapped person — v4 order: Tasks on top.

    Each person's section renders ``📌 Tasks (N)`` first (omitted at zero
    tasks, deadlines front-and-center), then the ``🔧 Work x/5`` floor line,
    then the Work cards — in-progress first, then queued, capped as before.
    """
    sections: list[tuple[str, list[str]]] = []
    for employee_id in _person_order([eid for eid in queues if eid in reverse_map]):
        adhoc, work = _split_bucket(queues[employee_id])
        header = (
            f"👤 {_display_name(employee_id)} — in progress {len(work['active'])}"
            f" · queued {len(work['ready'])}"
        )
        lines: list[str] = []
        if adhoc:
            lines.append(f"📌 Tasks ({len(adhoc)})")
            task_lines = [_task_line(card, today=today) for card in adhoc]
            lines.extend(_capped_lines(task_lines, len(adhoc), _BRIEF_PERSON_CARD_LIMIT))
        lines.append(_work_floor_line(_ongoing_work(work)))
        cards = [("▶️", card) for card in work["active"]] + [("▫️", card) for card in work["ready"]]
        card_lines = [
            f"{dot} {_card_handle(card)} {_safe_title(card.title)[:_BRIEF_TITLE_CHARS]}"
            f"{_deadline_suffix(card.due_date, today=today)}"
            for dot, card in cards
        ]
        lines.extend(_capped_lines(card_lines, len(cards), _BRIEF_PERSON_CARD_LIMIT))
        sections.append((header, lines))
    return sections


def daybrief_payload(
    store: TeamStore, *, today: str | None = None, test: bool = False
) -> tuple[str, list, str]:
    """(text, blocks, color) for the channel-wide morning brief.

    One gather feeds both renderings so text and Block Kit can never disagree.
    ``today`` (YYYY-MM-DD) pins the date for tests; production uses the LOCAL
    day because the brief is 'first thing in the operator's morning', not a UTC event.
    """
    from omniagentos.team import slack_blocks  # local: slack_blocks imports this module

    day = today or time.strftime("%Y-%m-%d")
    reverse_map = _reverse_slack_map(load_slack_map())
    queues = store.team_queues()
    pool = _pool_cards(store, limit=POOL_CARD_LIMIT)
    depths = _company_depths(store)

    company_sections = _daybrief_company_sections(pool, depths, today=day)
    person_sections = _daybrief_person_sections(queues, reverse_map, today=day)
    # Overdue is read off the projected due_date of the cards ABOUT TO RENDER
    # (pool + every live person bucket, Tasks included) — the v4 QueueCard
    # widening made the old per-render _due_dates SELECT redundant.
    dues = [_card_field(card, "due_date") for card in pool or []]
    for bucket in queues.values():
        dues.extend(
            card.due_date
            for name in ("active", "ready", "blocked", "review")
            for card in getattr(bucket, name)
        )
    overdue = any(due and str(due)[:10] < day for due in dues)

    header = f"☀️ Work queue — {day}"
    if test:
        header = f"🧪 TEST — {header}"
    # the operator's 2026-08-14 ruling: same targets line as the morning DM, once,
    # under the title — the channel-wide brief is not per-person, so it is
    # not gated the way the DM's operator exemption is.
    text_lines = [f"*{header}*", _targets_line()]
    for title, lines in company_sections + person_sections:
        text_lines.append(title)
        text_lines.extend(lines)
    text_lines.append(TASK_FOOTER)
    text = _safe_title("\n".join(text_lines))
    color, blocks = slack_blocks.daybrief_blocks(
        header,
        company_sections,
        person_sections,
        footer=TASK_FOOTER,
        stamp="morning brief · shared queue + per-person load",
        alarming=overdue,
        targets_line=_targets_line(),
    )
    return text, blocks, color


def run_daybrief(
    store: TeamStore,
    notifier: SlackNotifier | None,
    *,
    dry_run: bool = False,
    test: bool = False,
) -> bool:
    """Post (or JSON-print) the channel-wide morning brief."""
    text, blocks, color = daybrief_payload(store, test=test)
    if dry_run:
        channel = (
            os.environ.get(PULSE_CHANNEL_ENV) or os.environ.get(CHANNEL_ENV) or DEFAULT_CHANNEL
        )
        print(json.dumps({"channel": channel, "text": text, "blocks": len(blocks), "color": color}))
        return True
    return notifier is not None and notifier.post_channel(text, blocks=blocks, color=color)


def daybrief_state_path(override: str | Path | None = None) -> Path:
    """The last-daybrief-date state file, beside the watcher cursor under var."""
    return (
        Path(override)
        if override is not None
        else Path(resolve_var_root()) / _DAYBRIEF_STATE_FILENAME
    )


def _daybrief_sent_today(day: str) -> bool:
    """True when the state file records a brief already sent for ``day``.

    Unreadable/absent state reads as 'not sent': the worst case is one extra
    morning brief, never a silently skipped one.
    """
    try:
        payload = json.loads(daybrief_state_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    return isinstance(payload, dict) and payload.get("date") == day


def _mark_daybrief_sent(day: str) -> None:
    _write_json(daybrief_state_path(), {"date": day})


def _daybrief_entry(
    store: TeamStore, notifier: SlackNotifier | None, *, dry_run: bool, test: bool
) -> bool:
    """Explicit ``--daybrief``: post, and consume the day's slot on a REAL post.

    A ``--test`` or ``--dry-run`` brief never consumes the slot — the demo post
    must not suppress the real first-pulse-of-the-day brief.
    """
    ok = run_daybrief(store, notifier, dry_run=dry_run, test=test)
    if ok and not dry_run and not test:
        _mark_daybrief_sent(time.strftime("%Y-%m-%d"))
    return ok


def _pulse_entry(
    store: TeamStore,
    notifier: SlackNotifier | None,
    *,
    dry_run: bool,
    overnight: bool,
    test: bool,
) -> bool:
    """The ``--pulse`` cadence: morning brief on the day's first run, then pulses."""
    day = time.strftime("%Y-%m-%d")
    if not _daybrief_sent_today(day):
        ok = run_daybrief(store, notifier, dry_run=dry_run, test=test)
        if ok and not dry_run and not test:
            _mark_daybrief_sent(day)
        return ok
    return run_pulse(store, notifier, dry_run=dry_run, overnight=overnight, test=test)


# ---------------------------------------------------------------------------
# Hourly pulse
# ---------------------------------------------------------------------------

_URGENT_MARKER_LIMIT = 2
_URGENT_TITLE_CHARS = 40


def _urgent_markers(cards: Sequence[QueueCard]) -> list[str]:
    """Up to two ``🔥`` markers for this person's urgent open/claimed Work.

    Reads the cards in the order the store returned them — ``team_queues``
    already ranks urgent first — so this never re-sorts and never disagrees with
    what the queue view shows.
    """
    markers = []
    for card in cards:
        if card.priority != "urgent":
            continue
        identifier = card.ref or card.id[:8]
        markers.append(f"🔥 {identifier} {str(card.title)[:_URGENT_TITLE_CHARS]}")
        if len(markers) == _URGENT_MARKER_LIMIT:
            break
    return markers


def _task_due_run(adhoc: Sequence[QueueCard], *, today: str) -> str:
    """The pulse's second line: task refs + deadlines, when one is due/overdue.

    Renders ``📌 T1 ⏰2026-08-13 · T2 🔴⏰2026-08-10`` — every DATED task, but
    only once at least one of them is due today or overdue (the spec's
    trigger); quiet otherwise, so a far-future task never adds a line.
    """
    dated = [card for card in adhoc if card.due_date]
    if not any(str(card.due_date)[:10] <= today for card in dated):
        return ""
    return "📌 " + " · ".join(
        f"{card.ref or card.id[:8]}{_deadline_suffix(card.due_date, today=today)}" for card in dated
    )


def _pulse_person_line(employee_id: str, bucket: TeamQueueBuckets, *, today: str) -> str:
    """One compact line per person — v4 order: Tasks first, then Work.

    ``👤 Bob — 📌 2 tasks · 🔧 Work 3/5 ⚠`` (the 📌 segment is omitted at
    zero tasks; ⚠ marks the below-floor ongoing count — supply visibility,
    never a block), keeping the blocked segment and 🔥 urgent markers on the
    Work stream, plus a second line with the task refs/deadlines whenever a
    task is due today or overdue.
    """
    adhoc, work = _split_bucket(bucket)
    line = f"👤 {_display_name(employee_id)} — "
    if adhoc:
        line += f"📌 {len(adhoc)} task{'' if len(adhoc) == 1 else 's'} · "
    ongoing = _ongoing_work(work)
    line += f"🔧 Work {ongoing}/{ACTIVE_QUEUE_FLOOR}"
    if ongoing < ACTIVE_QUEUE_FLOOR:
        line += " ⚠"
    if work["blocked"]:
        line += f" · blocked {len(work['blocked'])} {_card_line(work['blocked'][0])}"
        if len(work["blocked"]) >= 2:
            line += " — needs attention"
    markers = _urgent_markers(list(work["ready"]) + list(work["active"]))
    if markers:
        line += " " + " ".join(markers)
    due_run = _task_due_run(adhoc, today=today)
    if due_run:
        line += "\n" + due_run
    return _safe_title(line)


def _overnight_suggestions(
    employee_id: str,
    bucket: TeamQueueBuckets,
    pool: Sequence[Mapping[str, Any]] | None,
    *,
    pool_index: int,
) -> list[str]:
    """Build no more than three deterministic, copy-pasteable next actions."""
    if not bucket.ready and not bucket.active and not bucket.blocked:
        if pool and pool_index < len(pool):
            return [
                _safe_title(
                    f"• {employee_id}: queue clear — grab from pool {_card_line(pool[pool_index], claim_cta=False)}"
                )
            ]
        if pool:
            return []
        return [
            f"• {employee_id}: queue clear — pool empty"
            if pool == []
            else f"• {employee_id}: queue clear — pool unavailable"
        ]

    suggestions: list[str] = []
    if bucket.ready:
        suggestions.append(
            _safe_title(
                f"• {employee_id}: start an overnight loop on {_card_line(bucket.ready[0])}"
            )
        )
    if _active_below_floor(bucket) and pool and pool_index < len(pool):
        suggestions.append(
            _safe_title(f"• {employee_id}: claim {_card_line(pool[pool_index], claim_cta=False)}")
        )
    if bucket.blocked:
        suggestions.append(
            _safe_title(f"• {employee_id}: unblock tonight? {_card_line(bucket.blocked[0])}")
        )
    return suggestions[:3]


def render_pulse_message(
    queues: Mapping[str, TeamQueueBuckets],
    slack_map: Mapping[str, str],
    pool: Sequence[Mapping[str, Any]] | None,
    pool_depth: int | None,
    *,
    overnight: bool = False,
    pace_lines: Sequence[str] | None = None,
    company_depths: Mapping[str | None, int] | None = None,
    today: str | None = None,
) -> str:
    """Render the channel pulse from queue data.

    ``pace_lines`` (multi-company Work OS, 2026-08-13) are pre-rendered
    Friday-pace lines from :func:`_pace_lines` — passed IN rather than computed
    here so this function stays time-independent and the pulse tests stay
    deterministic. ``None``/empty renders exactly the pre-pace pulse.
    ``company_depths`` (v3 alerts) adds the one-line per-company queue depth;
    ``None`` omits the line. ``today`` (a local ``YYYY-MM-DD``) pins the
    deadline glyphs for tests — the ONLY clock this function reads, and only
    when a card actually carries a due date; ``None`` resolves the local day.
    """
    day = today or team_tasks.local_today()
    reverse_map = _reverse_slack_map(slack_map)
    for employee_id in queues:
        if employee_id not in reverse_map:
            print(f"team-notify: pulse skip {employee_id!r}: no Slack mapping", file=sys.stderr)
    mapped: list[tuple[str, TeamQueueBuckets]] = [
        (employee_id, queues[employee_id])
        for employee_id in _person_order([eid for eid in queues if eid in reverse_map])
    ]
    lines = ["*Team pulse*"]
    lines.extend(
        _pulse_person_line(employee_id, bucket, today=day) for employee_id, bucket in mapped
    )
    if pace_lines:
        lines.extend(f"• {line}" for line in pace_lines)
    if company_depths is not None:
        lines.append(_companies_depth_line(company_depths))
    if pool_depth is None:
        lines.append("Pool: unavailable")
    else:
        pool_line = f"Pool: {pool_depth}"
        if pool_depth < POOL_DEPTH_FLOOR:
            pool_line += f" ⚠ low (<{POOL_DEPTH_FLOOR})"
        lines.append(pool_line)
    if overnight:
        lines.append("*Overnight suggestions*")
        for index, (employee_id, bucket) in enumerate(mapped):
            lines.extend(_overnight_suggestions(employee_id, bucket, pool, pool_index=index))
    lines.append(TASK_FOOTER)
    return _safe_title("\n".join(lines))


def _pace_lines(store: TeamStore, slack_map: Mapping[str, str]) -> list[str]:
    """Friday-pace lines for the pulse's active mapped DEVS (never the operator).

    Best-effort by contract: a pace failure (missing config, a scoring read
    error) costs the pace lines, never the pulse — the queue snapshot is the
    message; pace is decoration on top of it.
    """
    try:
        from omniagentos.company_goals.store import CompanyGoalsStore
        from omniagentos.team import points

        active = {
            str(row["id"])
            for row in CompanyGoalsStore(store._store).list_employees(status="active")
        }
        dev_ids = points.active_dev_ids(sorted(set(slack_map.values()) & active))
        if not dev_ids:
            return []
        config = points.load_points_config()
        statuses = points.pace_statuses(store, dev_ids, config=config)
        lines = [points.pace_line(statuses[dev_id]) for dev_id in dev_ids]
        announcement = points.friday_announcement(config, points.utc_today())
        if announcement is not None:
            lines.append(announcement)
        return lines
    except Exception as exc:  # noqa: BLE001 -- pace must never sink the pulse
        print(f"team-notify: pace lines unavailable: {exc}", file=sys.stderr)
        return []


def pulse_message(store: TeamStore, *, overnight: bool = False) -> str:
    """Gather the bounded pulse inputs once, then render one team-channel message."""
    slack_map = load_slack_map()
    return render_pulse_message(
        store.team_queues(),
        slack_map,
        _pool_cards(store),
        _pool_depth(store),
        overnight=overnight,
        pace_lines=_pace_lines(store, slack_map),
        company_depths=_company_depths(store),
    )


def pulse_payload(
    store: TeamStore, *, overnight: bool = False, test: bool = False
) -> tuple[str, list, str]:
    """(text, blocks, color) for one pulse — gathered once so both renderings agree."""
    from omniagentos.team import slack_blocks  # local: slack_blocks imports this module

    slack_map = load_slack_map()
    queues = store.team_queues()
    pool = _pool_cards(store)
    pool_depth = _pool_depth(store)
    pace = _pace_lines(store, slack_map)
    company_depths = _company_depths(store)
    day = team_tasks.local_today()
    text = render_pulse_message(
        queues,
        slack_map,
        pool,
        pool_depth,
        overnight=overnight,
        pace_lines=pace,
        company_depths=company_depths,
        today=day,
    )
    if test:
        text = _safe_title("🧪 TEST — ") + text
    reverse_map = _reverse_slack_map(slack_map)
    # Same person order as the text rendering and the morning brief.
    mapped = [(eid, queues[eid]) for eid in _person_order([e for e in queues if e in reverse_map])]
    person_lines = [
        (_pulse_person_line(eid, bucket, today=day), len(bucket.blocked) >= 2)
        for eid, bucket in mapped
    ]
    # Pace rides the same person-line channel in the styled rendering; a ⚠
    # pace line is alarming for the same reason two blocked cards are.
    person_lines.extend((line, line.startswith("⚠")) for line in pace)
    overnight_lines: list[str] = []
    if overnight:
        for index, (eid, bucket) in enumerate(mapped):
            overnight_lines.extend(_overnight_suggestions(eid, bucket, pool, pool_index=index))
        # Register each person's top actionable card as a NUMBERED overnight
        # decision (shared allocator with repairs) so one reply runs it tonight.
        try:
            from omniagentos.team.overnight import register_suggestions, render_numbered

            structured = []
            for eid, bucket in mapped:
                candidates = list(bucket.ready) + list(bucket.active)
                if candidates:
                    card = candidates[0]
                    structured.append(
                        {
                            "employee_id": eid,
                            "card_id": card.id,
                            "ref": card.ref,
                            "title": card.title,
                        }
                    )
            pending = register_suggestions(REPO_ROOT_PATH, structured)
            overnight_lines.extend(render_numbered(pending))
        except Exception as exc:  # noqa: BLE001 — numbering must never sink the pulse
            print(f"team-notify: overnight numbering unavailable: {exc}", file=sys.stderr)
    color, blocks = slack_blocks.pulse_blocks(
        person_lines,
        pool_depth,
        pool_depth is not None and pool_depth < POOL_DEPTH_FLOOR,
        stamp="hourly team pulse",
        overnight=overnight_lines or None,
        companies_line=None if company_depths is None else _companies_depth_line(company_depths),
        footer=TASK_FOOTER,
        test=test,
    )
    return text, blocks, color


def run_pulse(
    store: TeamStore,
    notifier: SlackNotifier | None,
    *,
    dry_run: bool = False,
    overnight: bool = False,
    test: bool = False,
) -> bool:
    """Post (or JSON-print) the one channel-wide pulse."""
    message, blocks, color = pulse_payload(store, overnight=overnight, test=test)
    if dry_run:
        channel = (
            os.environ.get(PULSE_CHANNEL_ENV) or os.environ.get(CHANNEL_ENV) or DEFAULT_CHANNEL
        )
        print(
            json.dumps({"channel": channel, "text": message, "blocks": len(blocks), "color": color})
        )
        return True
    return notifier is not None and notifier.post_channel(message, blocks=blocks, color=color)


def _load_cursor(store: TeamStore, path: Path) -> int | None:
    """Read a rowid cursor; ``None`` means an intentionally fresh install.

    A malformed cursor is never treated as fresh: replaying an event log is a
    Slack incident, not a recovery mechanism.  The returned legacy cursor is
    migrated once by resolving its immutable event id to the append-order row.
    """
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError) as exc:
        raise ValueError(f"corrupt cursor {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"corrupt cursor {path}: expected JSON object")
    rowid = payload.get("rowid")
    if isinstance(rowid, int) and not isinstance(rowid, bool) and rowid >= 0:
        return rowid
    event_id = payload.get("id")
    if isinstance(event_id, str) and event_id:
        row = store._connection.execute(
            f"SELECT rowid FROM {EVENTS_TABLE} WHERE id = ?", (event_id,)
        ).fetchone()
        if row is None:
            raise ValueError(f"corrupt cursor {path}: legacy event id is unresolvable")
        return int(row["rowid"])
    raise ValueError(f"corrupt cursor {path}: missing rowid")


def _write_cursor(path: Path, rowid: int) -> None:
    """Atomically persist a cursor, retaining normal world-readable config mode."""
    _write_json(path, {"rowid": rowid})


def _write_json(path: Path, payload: Mapping[str, Any]) -> None:
    """Atomically persist one small JSON state object (cursor, daybrief date)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(dict(payload), handle, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o644)
        os.replace(temporary, path)
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except BaseException:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


def _latest_event_rowid(store: TeamStore) -> int:
    row = store._connection.execute(
        f"SELECT COALESCE(MAX(rowid), 0) AS rowid FROM {EVENTS_TABLE}"
    ).fetchone()
    return 0 if row is None else int(row["rowid"])


def _events_after(store: TeamStore, cursor: int) -> list[dict[str, Any]]:
    rows = store._connection.execute(
        f"SELECT e.rowid AS event_rowid, e.id, e.task_id, e.actor, e.event, e.from_status, e.to_status, e.note, e.created_at, "
        f"b.ref, b.title, b.owner_employee_id, b.status, b.source FROM {EVENTS_TABLE} e "
        f"JOIN {BOARD_TABLE} b ON b.id = e.task_id "
        "WHERE e.rowid > ? ORDER BY e.rowid ASC LIMIT ?",
        (cursor, _EVENT_LIMIT),
    ).fetchall()
    return [dict(row) for row in rows]


def _assignment_message(event: Mapping[str, Any]) -> str:
    return (
        f"You've been assigned {_card_line(event)} — starts in your queue; "
        "reply `claim <REF>` / it's waiting when you're free."
    )


def _inference_message(event: Mapping[str, Any]) -> str:
    # Inference nudges are deliberately narrower than a generic card render:
    # no description/body ever crosses this egress boundary, and a malformed
    # title cannot smuggle a URL or recognizable credential prefix into Slack.
    ref_title = _card_line(event)
    status = str(event.get("to_status") or event.get("status") or "updated")
    owner = str(event.get("owner_employee_id") or "unassigned")
    source = str(event.get("source") or "unknown")
    context = f" (owner: {owner}; source: {source})"
    if str(event.get("event")) == "create":
        return _safe_title(f"🤖 created {ref_title} → {status}{context}")
    return _safe_title(f"🤖 {ref_title} → {status}{context}")


def _assignment_owner(event: Mapping[str, Any]) -> str | None:
    """The C3v2 event note is the authority for a notification recipient."""
    note = str(event.get("note") or "")
    match = _OWNER_TOKEN_RE.search(note)
    if match is not None:
        owner = match.group(1)
        return None if owner == "none" else owner
    return None


def _delivery_outcome(notifier: Any, send: Any) -> str:
    """Return success, terminal skip, or transient park after at most 3 tries."""
    for attempt in range(3):
        if send():
            return "success"
        error = str(getattr(notifier, "last_error", "") or "unknown").lower()
        if error in _TERMINAL_SLACK_ERRORS:
            print(f"team-notify: terminal Slack error {error}; skipping event", file=sys.stderr)
            return "terminal"
        transient = (
            error in _TRANSIENT_SLACK_ERRORS
            or error.startswith("http_5")
            or error in {"transport", "unknown"}
        )
        if not transient:
            print(f"team-notify: terminal Slack error {error}; skipping event", file=sys.stderr)
            return "terminal"
        if attempt < 2:
            retry_after = getattr(notifier, "retry_after", None)
            time.sleep(float(retry_after) if retry_after is not None else 1.0)
    print("team-notify: transient Slack failure after 3 attempts; watcher parked", file=sys.stderr)
    return "park"


def run_watch_once(
    store: TeamStore, notifier: Any, *, cursor_file: str | Path | None = None
) -> bool:
    """Deliver one bounded event batch, advancing after every delivered/terminal event."""
    target = cursor_path(cursor_file)
    try:
        cursor = _load_cursor(store, target)
    except ValueError as exc:
        print(f"team-notify: {exc}", file=sys.stderr)
        return False
    if cursor is None:
        _write_cursor(target, _latest_event_rowid(store))
        print("team-notify: watcher bootstrapped cursor at now (0 events)")
        return True
    events = _events_after(store, cursor)
    reverse_map = _reverse_slack_map(load_slack_map())
    print(f"team-notify: event batch size {len(events)}")
    for event in events:
        if str(event["event"]) == "assign":
            owner = _assignment_owner(event)
            if owner and str(event["actor"]) != str(owner):
                slack_user_id = reverse_map.get(str(owner))
                if slack_user_id is None:
                    print(
                        f"team-notify: assignment skip {owner!r}: no Slack mapping", file=sys.stderr
                    )
                else:
                    outcome = _delivery_outcome(
                        notifier,
                        lambda user=slack_user_id, item=event: notifier.post_dm(
                            user, _assignment_message(item)
                        ),
                    )
                    if outcome == "park":
                        return False
        elif str(event["actor"]) == "inference" and str(event["event"]) in _INFERENCE_EVENTS:
            outcome = _delivery_outcome(
                notifier, lambda item=event: notifier.post_channel(_inference_message(item))
            )
            if outcome == "park":
                return False
        _write_cursor(target, int(event["event_rowid"]))
    return True


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Send Team Work OS Slack DMs and event nudges.")
    mode = parser.add_mutually_exclusive_group(required=False)
    mode.add_argument("--morning", action="store_true", help="send the daily queue DMs")
    mode.add_argument("--pulse", action="store_true", help="post one compact team queue pulse")
    mode.add_argument(
        "--daybrief", action="store_true", help="post the channel-wide morning brief now"
    )
    mode.add_argument("--watch-once", action="store_true", help="process one event-watch batch")
    parser.add_argument(
        "--bootstrap",
        action="store_true",
        help="standalone: set the watcher cursor to now without posting to Slack",
    )
    parser.add_argument("--dry-run", action="store_true", help="print messages instead of posting")
    parser.add_argument(
        "--overnight", action="store_true", help="append overnight suggestions to a pulse"
    )
    parser.add_argument(
        "--test",
        action="store_true",
        help="prefix the header with '🧪 TEST —' so a live demo post is unambiguous",
    )
    parser.add_argument("--db", default=None, help="control-plane database path")
    args = parser.parse_args(argv)

    if not (args.morning or args.pulse or args.daybrief or args.watch_once or args.bootstrap):
        parser.error(
            "one of --morning, --pulse, --daybrief, --watch-once, or --bootstrap is required"
        )
    if args.bootstrap and (args.watch_once or args.morning or args.pulse or args.daybrief):
        parser.error("--bootstrap is standalone")
    if args.overnight and not args.pulse:
        parser.error("--overnight requires --pulse")
    if args.test and not (args.pulse or args.daybrief):
        parser.error("--test requires --pulse or --daybrief")

    store = TeamStore(args.db or default_db_path())
    load_slack_env()
    if args.morning and args.dry_run:
        return 0 if run_morning(store, None, dry_run=True) else 1
    if args.pulse and args.dry_run:
        ok = _pulse_entry(store, None, dry_run=True, overnight=args.overnight, test=args.test)
        return 0 if ok else 1
    if args.daybrief and args.dry_run:
        return 0 if _daybrief_entry(store, None, dry_run=True, test=args.test) else 1
    if args.bootstrap:
        _write_cursor(cursor_path(), _latest_event_rowid(store))
        print("team-notify: watcher cursor bootstrapped at now")
        return 0
    if args.watch_once and args.dry_run:
        return 0 if run_watch_once(store, _DryRunNotifier()) else 1
    token = os.environ.get("SLACK_BOT_TOKEN")
    if not token:
        print("team-notify: no SLACK_BOT_TOKEN — not posted", file=sys.stderr)
        return 1
    notifier = SlackNotifier(
        token,
        channel=os.environ.get(PULSE_CHANNEL_ENV) if (args.pulse or args.daybrief) else None,
    )
    if args.morning:
        ok = run_morning(store, notifier)
    elif args.pulse:
        ok = _pulse_entry(store, notifier, dry_run=False, overnight=args.overnight, test=args.test)
    elif args.daybrief:
        ok = _daybrief_entry(store, notifier, dry_run=False, test=args.test)
    else:
        ok = run_watch_once(store, notifier)
    return 0 if ok else 1


if __name__ == "__main__":  # pragma: no cover - module entry point
    raise SystemExit(main())
