"""Slack inbound task updates (P6): reply in the report channel, update the board.

the operator/Alice/Bob can reply to the daily Team Work OS report in Slack with a short
deterministic command — ``done U3 landed it``, ``progress S5 talked to the
customer``, ``blocked OPS-2 waiting on Alice``, ``claim UP-1``, ``!top U3``,
``my queue``, ``report``, ``@bob task fix the login bug !top #initech`` —
instead of touching the board or a commit. No LLM: every command
either matches the fixed grammar in :func:`parse_command` or it is ordinary
channel chatter and is silently ignored.

THE ``/task`` COMMAND FAMILY (v3, the operator's ruling 2026-08-13) rides the same
handler as a literal message prefix: ``/task add|assign|claim|done|note|
reassign|queue|mine|help``. Every bare verb above keeps working unchanged;
the slash family adds the shared-queue permission matrix (the operator-only ``add``,
the operator/Alice queue delegation, ad-hoc ``assign`` for everyone-but-self,
owner-only ``done``), natural trailing deadlines stored in
``board_tasks.due_date``, and one DM per action (assign/reassign -> the
assignee, done/note -> the assigner) through the notifier's egress scrubber.
Shared helpers (deadline parsing, assigner resolution, guarded ownership
writes) live in :mod:`omniagentos.team.tasks`. Unlike ordinary chatter, a
malformed ``/task ...`` message IS answered (with a pointer at ``/task
help``) — the prefix is an unambiguous attempt to command the bot.

FLAG-GATED, off by default: ``OMNIAGENTOS_TEAM_SLACK_UPDATES`` (unset/anything
other than a truthy value = current behaviour, byte-identical — every new code
path here runs only when the flag is on). The actual gating (flag + channel
allowlist) lives in the caller, :func:`omniagentos.comms.sockets.slack`'s
envelope hook, so this module's entry point (:func:`team_updates_handle`) is
reachable directly in tests without an env var dance.

CHANNEL ALLOWLIST: ``OMNI_TEAM_REPORT_CHANNEL`` (default ``C0000EXAMPLE``,
``#dev-agentic-alerts`` — see ``daily-dev-scoreboard.py``), also enforced by the
caller. Messages elsewhere are never parsed as commands, so ordinary Slack use
outside the report channel is completely unaffected.

DM NOTE: :func:`team_updates_handle` itself is channel-neutral, so its existing
``my queue`` and ``claim <ref>`` paths work for a Slack ``im`` event and retain
the same sender/cross-owner checks.  The current Socket Mode caller still
allowlists only the report channel; admitting DM envelopes requires that
transport-level change and is intentionally outside this module's ownership.

SENDER MAP: ``configs/team_slack_map.yaml`` (Slack user id -> employee id).
:func:`load_slack_map` validates every mapped employee id against the known
roster at load time (raises on an unknown one — a typo'd id must fail loudly,
not route nobody's commands); a Slack sender who is not IN the map is a normal,
expected case (someone in the channel who is not on the roster) and is only
logged, one stderr line, never an error.

CREDENTIALS: the reply is posted with the bot token resolved through the
credential broker (``slack_ingest.stream``, the same capability
``omniagentos.comms.sockets.slack`` already resolves) rather than an unbrokered
``os.environ`` read — U-R9 (``tests/llm/test_unbrokered_credentials.py``) scans
every file under ``omniagentos/`` for a new direct credential read, and this
module intentionally has none.
"""

from __future__ import annotations

import inspect
import json
import re
import sqlite3
import sys
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from omniagentos.collab.contracts import BASELINE_SOURCE, BoardTask
from omniagentos.collab.store import CollabStore, append_task_event
from omniagentos.connectors import load_registry
from omniagentos.connectors.broker import AuditContext, resolve_for
from omniagentos.contracts import utc_now_iso
from omniagentos.team import tasks as team_tasks
from omniagentos.team.contracts import ACTIVE_QUEUE_FLOOR, OPERATOR_EMPLOYEE_ID
from omniagentos.team.store import TeamStore

__all__ = [
    "AmbiguousMatch",
    "Command",
    "KNOWN_EMPLOYEES",
    "NOT_OWNED",
    "TASK_DM_VERBS",
    "TASK_VERBS",
    "apply",
    "load_slack_map",
    "parse_command",
    "permalink",
    "post_reply",
    "resolve_task",
    "team_updates_handle",
]

# --------------------------------------------------------------------------
# grammar
# --------------------------------------------------------------------------

#: A board task ref: letters, an optional single hyphen, then digits — ``U3``,
#: ``S5``, ``OPS-2``, ``UP-1``. Anchored full-token so ``U3`` and ``U33`` are
#: never confused with one another.
_REF_TOKEN_PATTERN = r"(?:[A-Za-z]+-?\d+|btk_[A-Za-z0-9_-]+)"
_REF_TOKEN_RE = re.compile(rf"^{_REF_TOKEN_PATTERN}$")
_MY_QUEUE_RE = re.compile(r"^my\s+queue$", re.IGNORECASE)
_REPORT_RE = re.compile(r"^report$", re.IGNORECASE)
#: ``!top`` rides the ref-verb grammar: ``!top <REF>`` escalates an EXISTING
#: card to urgent, the same token the ``task`` verb accepts at create — one
#: spelling of "this is on fire", whether the card exists yet or not. The
#: parsed verb is ``top`` (the ``!`` is Slack-side spelling, not identity).
_VERB_RE = re.compile(r"^(done|progress|blocked|claim|!top)\s+(.+)$", re.IGNORECASE | re.DOTALL)
_QUOTED_RE = re.compile(r'^"([^"]+)"\s*(.*)$', re.DOTALL)

#: ``task``: the only verb that CREATES a card. Two orders, because Slack puts
#: the mention where the sender typed it: ``<@U…> task <title>`` (the natural
#: "hey you, task X") and ``task <@U…> <title>`` (verb first).
_MENTION = r"<@([A-Za-z0-9]+)(?:\|[^>]*)?>"
_TASK_MENTION_FIRST_RE = re.compile(rf"^{_MENTION}\s+task\s+(.+)$", re.IGNORECASE | re.DOTALL)
_TASK_VERB_FIRST_RE = re.compile(rf"^task\s+{_MENTION}\s+(.+)$", re.IGNORECASE | re.DOTALL)
#: Any REMAINING Slack mention/channel/broadcast token in a title. Stripped, not
#: kept: a title is echoed back in a threaded reply, and a surviving ``<!here>``
#: would turn one person's task into a channel-wide ping.
_ANY_MENTION_RE = re.compile(r"<[!@#][^>]{0,60}>")
#: Title flags, accepted ANYWHERE in the title and removed from it.
_TOP_FLAG_RE = re.compile(r"(?:^|\s)!top(?=\s|$)", re.IGNORECASE)
_COMPANY_FLAG_RE = re.compile(r"(?:^|\s)#([A-Za-z0-9-]+)(?=\s|$)")

#: /task family (v3). The slash prefix is matched case-insensitively; the
#: sub-verb decides the shape of the rest.
_TASK_SLASH_RE = re.compile(r"^/task(?:\s+(.*))?$", re.IGNORECASE | re.DOTALL)
#: The three-level priority flag the /task grammar adds on top of ``!top``.
_PRIORITY_FLAG_RE = re.compile(r"(?:^|\s)!(top|high|low)(?=\s|$)", re.IGNORECASE)
_PRIORITY_BY_FLAG: Mapping[str, str] = {"top": "urgent", "high": "high", "low": "low"}
#: ``| ac: <criteria>`` — the explicit acceptance-criteria suffix on /task add.
_AC_SUFFIX_RE = re.compile(r"\|\s*ac:\s*(.+)$", re.IGNORECASE | re.DOTALL)

#: The automation backlog's two tails. ``#category`` accepts the SAME token
#: grammar as the dashboard route — boundary-checked ``[A-Za-z0-9_-]+``,
#: underscores included, because ``#dev_tooling`` and ``#dev-tooling`` name the
#: same category and a surface that swallowed only half of one ("#dev") would
#: file the work somewhere else entirely. It reuses the company flag's SHAPE
#: but not its taxonomy — it resolves against the automation goal ladder
#: (``team.tasks.resolve_automation_category``), which is why it has its own
#: pattern rather than borrowing ``_COMPANY_FLAG_RE``'s name. ``for <who>`` is
#: anchored to the END like every other tail here, so a title may contain "for"
#: mid-sentence without losing it.
_CATEGORY_FLAG_RE = re.compile(r"(?:^|\s)#([A-Za-z0-9_-]+)(?=\s|$)")
_FOR_HINT_RE = re.compile(
    r"(?:^|\s)for\s+(owner|alice|bob|ai)\s*$",
    re.IGNORECASE,
)
_REASSIGN_REF_FIRST_RE = re.compile(rf"^({_REF_TOKEN_PATTERN})\s+{_MENTION}$")
_REASSIGN_MENTION_FIRST_RE = re.compile(rf"^{_MENTION}\s+({_REF_TOKEN_PATTERN})$")
_ASSIGN_RE = re.compile(rf"^{_MENTION}\s+(.+)$", re.DOTALL)
_QUEUE_COMPANY_RE = re.compile(r"^#?([a-z0-9-]+)$", re.IGNORECASE)

#: Longest title a Slack line may create. A Slack message has no practical
#: length limit; a board title rendered in a queue column does.
TASK_TITLE_MAX = 500

#: Verbs whose trailing text is not optional colour but the whole point of the
#: message (a reason, a status note) — a blocked/progress command with nothing
#: after the ref is malformed, not a command with an empty note.
_VERBS_REQUIRING_NOTE = frozenset({"progress", "blocked"})

VERBS: frozenset[str] = frozenset(
    {"done", "progress", "blocked", "claim", "my_queue", "report", "task", "top"}
)

#: The /task family's own verbs (only ``/task mine`` reuses an existing verb,
#: ``my_queue`` — a pure read). ``/task claim`` gets its OWN verb so it passes
#: the family's active-roster gate; the bare ``claim`` stays grandfathered.
TASK_VERBS: frozenset[str] = frozenset(
    {
        "task_add",
        "task_approve",
        "task_assign",
        "task_claim",
        "task_done",
        "task_note",
        "task_propose",
        "task_reassign",
        "task_reject",
        "task_queue",
        "task_help",
        "task_unknown",
    }
)

#: /task verbs that send a DM on success — the handler only resolves a Slack
#: token (and builds a notifier) when one of these arrives.
TASK_DM_VERBS: frozenset[str] = frozenset(
    {
        "task_assign",
        "task_done",
        "task_note",
        "task_reassign",
        # The automation backlog: propose DMs the operator, approve/reject DM the
        # proposer — one action, one DM, same as every verb above.
        "task_propose",
        "task_approve",
        "task_reject",
    }
)


@dataclass(frozen=True)
class Command:
    """One parsed Slack command. ``ref`` XOR ``title_prefix`` is set for the
    four task verbs; both are ``None`` for ``my_queue``/``report``.

    ``assignee_slack_id``/``title``/``priority``/``company``/``raw_text`` are set
    only by the card-creating verbs (``task``, ``task_add``, ``task_assign``),
    and default to the values every other verb already implies. ``deadline``
    is the RAW trailing phrase (``"tomorrow"``, ``"in 2 hours"``) — parsing to
    an ISO timestamp happens at apply time (:func:`omniagentos.team.tasks.
    parse_deadline`), keeping this dataclass wall-clock-free and comparable in
    tests.
    """

    verb: str
    ref: str | None = None
    title_prefix: str | None = None
    note: str = ""
    assignee_slack_id: str | None = None
    title: str | None = None
    priority: str = "normal"
    company: str | None = None
    raw_text: str = ""
    deadline: str | None = None
    # The automation backlog (2026-08-14). ``category`` is the ``#token`` a
    # proposal files under (resolved against the automation goal ladder, NOT
    # the company one — different taxonomy, so deliberately not ``company``);
    # ``assignee_hint`` is the ``for <owner|alice|bob|ai>`` tail.
    category: str | None = None
    assignee_hint: str | None = None


def parse_command(text: str) -> Command | None:
    """Deterministic grammar, no LLM. Anything that does not match is ``None``
    (ordinary chatter) — never an error, because normal channel conversation
    must never be treated as a malformed command.

    ``done <ref> [note...]`` | ``progress <ref> <note...>`` |
    ``blocked <ref> <reason...>`` | ``claim <ref>`` | ``!top <ref>`` |
    ``my queue`` | ``report`` |
    ``<@U…> task <title...>`` / ``task <@U…> <title...>``

    ``<ref>`` is either a bare board ref token (``U3``, ``OPS-2``) or a
    double-quoted title prefix (``"Fix login bug"``).
    """
    if not text or not text.strip():
        return None
    stripped = text.strip()

    slash = _parse_slash_task(stripped)
    if slash is not None:
        return slash

    if _MY_QUEUE_RE.match(stripped):
        return Command(verb="my_queue")
    if _REPORT_RE.match(stripped):
        return Command(verb="report")

    task = _parse_task(stripped)
    if task is not None:
        return task

    match = _VERB_RE.match(stripped)
    if match is None:
        return None
    verb = match.group(1).lower().lstrip("!")
    rest = match.group(2).strip()
    if not rest:
        return None

    ref: str | None = None
    title_prefix: str | None = None
    note = ""

    quoted = _QUOTED_RE.match(rest)
    if quoted is not None:
        title_prefix = quoted.group(1).strip()
        if not title_prefix:
            return None
        note = quoted.group(2).strip()
    else:
        parts = rest.split(None, 1)
        token = parts[0]
        if not _REF_TOKEN_RE.match(token):
            return None
        ref = token
        note = parts[1].strip() if len(parts) > 1 else ""

    if verb in _VERBS_REQUIRING_NOTE and not note:
        return None

    return Command(verb=verb, ref=ref, title_prefix=title_prefix, note=note)


def _parse_task(stripped: str) -> Command | None:
    """The ``task`` verb, or ``None`` when the text is not one.

    Deterministic like the rest of the grammar: the assignee is a Slack mention
    (resolved to an employee later, where the sender map lives), and the flags
    are fixed tokens, not intent inferred from prose.
    """
    match = _TASK_MENTION_FIRST_RE.match(stripped) or _TASK_VERB_FIRST_RE.match(stripped)
    if match is None:
        return None
    assignee_slack_id = match.group(1)
    body = match.group(2)

    priority = "normal"
    if _TOP_FLAG_RE.search(body):
        priority = "urgent"
        body = _TOP_FLAG_RE.sub(" ", body)
    company: str | None = None
    company_match = _COMPANY_FLAG_RE.search(body)
    if company_match is not None:
        company = company_match.group(1)
        body = _COMPANY_FLAG_RE.sub(" ", body)

    title = " ".join(_ANY_MENTION_RE.sub(" ", body).split())
    if not title:
        return None
    return Command(
        verb="task",
        assignee_slack_id=assignee_slack_id,
        title=title[:TASK_TITLE_MAX],
        priority=priority,
        company=company,
        raw_text=stripped,
    )


def _parse_slash_task(stripped: str) -> Command | None:
    """The ``/task`` family, or ``None`` when the text does not start with it.

    Unlike the bare-verb grammar, a recognised ``/task`` prefix with a
    malformed body parses to ``task_unknown`` rather than ``None``: typing
    ``/task`` is an unambiguous attempt to command the bot, and silence would
    read as breakage. Ordinary chatter (no prefix) still returns ``None``.
    """
    match = _TASK_SLASH_RE.match(stripped)
    if match is None:
        return None
    body = (match.group(1) or "").strip()
    if not body:
        return Command(verb="task_help", raw_text=stripped)
    parts = body.split(None, 1)
    sub = parts[0].lower()
    rest = parts[1].strip() if len(parts) > 1 else ""

    if sub == "help" and not rest:
        return Command(verb="task_help", raw_text=stripped)
    if sub == "mine" and not rest:
        return Command(verb="my_queue", raw_text=stripped)
    if sub == "queue":
        if not rest:
            return Command(verb="task_queue", raw_text=stripped)
        company = _QUEUE_COMPANY_RE.match(rest)
        if company is not None:
            return Command(verb="task_queue", company=company.group(1).lower(), raw_text=stripped)
        return Command(verb="task_unknown", raw_text=stripped)
    if sub == "claim":
        if _REF_TOKEN_RE.match(rest):
            return Command(verb="task_claim", ref=rest, raw_text=stripped)
        return Command(verb="task_unknown", raw_text=stripped)
    if sub in {"done", "note"}:
        pieces = rest.split(None, 1)
        if not pieces or not _REF_TOKEN_RE.match(pieces[0]):
            return Command(verb="task_unknown", raw_text=stripped)
        note = pieces[1].strip() if len(pieces) > 1 else ""
        if sub == "note" and not note:
            return Command(verb="task_unknown", raw_text=stripped)
        return Command(verb=f"task_{sub}", ref=pieces[0], note=note, raw_text=stripped)
    if sub == "reassign":
        ref_first = _REASSIGN_REF_FIRST_RE.match(rest)
        if ref_first is not None:
            return Command(
                verb="task_reassign",
                ref=ref_first.group(1),
                assignee_slack_id=ref_first.group(2),
                raw_text=stripped,
            )
        mention_first = _REASSIGN_MENTION_FIRST_RE.match(rest)
        if mention_first is not None:
            return Command(
                verb="task_reassign",
                ref=mention_first.group(2),
                assignee_slack_id=mention_first.group(1),
                raw_text=stripped,
            )
        return Command(verb="task_unknown", raw_text=stripped)
    if sub == "add":
        return _parse_task_add(rest, stripped)
    if sub == "assign":
        return _parse_task_assign(rest, stripped)
    if sub == "propose":
        return _parse_task_propose(rest, stripped)
    if sub in {"approve", "reject"}:
        return _parse_task_decision(sub, rest, stripped)
    return Command(verb="task_unknown", raw_text=stripped)


def _strip_task_flags(body: str) -> tuple[str, str, str | None, str | None]:
    """``(title, priority, company, deadline)`` from one /task add/assign body.

    Order of extraction: the trailing deadline phrase first (it is always the
    LAST phrase of the message), then the priority and company flags (accepted
    anywhere), then mention/whitespace cleanup — the same normalisation the
    bare ``task`` verb applies.
    """
    head, deadline = team_tasks.split_deadline(body)
    priority = "normal"
    flag = _PRIORITY_FLAG_RE.search(head)
    if flag is not None:
        priority = _PRIORITY_BY_FLAG[flag.group(1).lower()]
        head = _PRIORITY_FLAG_RE.sub(" ", head)
    company: str | None = None
    company_match = _COMPANY_FLAG_RE.search(head)
    if company_match is not None:
        company = company_match.group(1).lower()
        head = _COMPANY_FLAG_RE.sub(" ", head)
    title = " ".join(_ANY_MENTION_RE.sub(" ", head).split())
    return title, priority, company, deadline


def _parse_task_add(rest: str, raw: str) -> Command:
    """``/task add <title> #company [!top|!high|!low] [deadline] [| ac: …]``."""
    if not rest:
        return Command(verb="task_unknown", raw_text=raw)
    head, deadline = team_tasks.split_deadline(rest)
    criteria: str | None = None
    ac = _AC_SUFFIX_RE.search(head)
    if ac is not None:
        criteria = " ".join(ac.group(1).split())
        head = head[: ac.start()].strip()
    title, priority, company, tail_deadline = _strip_task_flags(head)
    deadline = deadline or tail_deadline
    if not title:
        return Command(verb="task_unknown", raw_text=raw)
    return Command(
        verb="task_add",
        title=title[:TASK_TITLE_MAX],
        note=criteria or "",
        priority=priority,
        company=company,
        deadline=deadline,
        raw_text=raw,
    )


def _parse_task_assign(rest: str, raw: str) -> Command:
    """``/task assign @person <title or REF> [#company] [!priority] [deadline]``.

    A single ref-shaped token is a QUEUE DELEGATION (``ref`` set); anything
    else is a new ad-hoc owned card (``title`` set). The apply layer keys the
    permission cell on which one it is.
    """
    match = _ASSIGN_RE.match(rest)
    if match is None:
        return Command(verb="task_unknown", raw_text=raw)
    assignee_slack_id = match.group(1)
    title, priority, company, deadline = _strip_task_flags(match.group(2))
    if not title:
        return Command(verb="task_unknown", raw_text=raw)
    if _REF_TOKEN_RE.match(title):
        return Command(
            verb="task_assign",
            ref=title,
            assignee_slack_id=assignee_slack_id,
            priority=priority,
            company=company,
            deadline=deadline,
            raw_text=raw,
        )
    return Command(
        verb="task_assign",
        title=title[:TASK_TITLE_MAX],
        assignee_slack_id=assignee_slack_id,
        priority=priority,
        company=company,
        deadline=deadline,
        raw_text=raw,
    )


def _split_assignee_hint(text: str) -> tuple[str, str | None]:
    """``('write the digest', 'ai')`` — the trailing ``for <who>`` phrase.

    Anchored to the END like every other tail in this grammar, so a title may
    contain the word "for" mid-sentence ("a script for the weekly digest")
    without losing it.
    """
    match = _FOR_HINT_RE.search(text)
    if match is None:
        return text.strip(), None
    return text[: match.start()].strip(), match.group(1).lower()


def _parse_task_propose(rest: str, raw: str) -> Command:
    """``/task propose <title> [#category] [for <who>] [| ac: <criteria>]``.

    No deadline grammar on purpose: a proposal is not scheduled work. It has no
    owner, no start and no due date until the operator approves it, and accepting a
    deadline here would put a promise on a card nobody has agreed to do.
    """
    if not rest.strip():
        return Command(verb="task_unknown", raw_text=raw)
    head = rest.strip()
    criteria: str | None = None
    ac = _AC_SUFFIX_RE.search(head)
    if ac is not None:
        criteria = " ".join(ac.group(1).split())
        head = head[: ac.start()].strip()
    head, hint = _split_assignee_hint(head)
    category: str | None = None
    flag = _CATEGORY_FLAG_RE.search(head)
    if flag is not None:
        category = flag.group(1).lower()
        head = _CATEGORY_FLAG_RE.sub(" ", head)
    title = " ".join(_ANY_MENTION_RE.sub(" ", head).split())
    if not title:
        return Command(verb="task_unknown", raw_text=raw)
    return Command(
        verb="task_propose",
        title=title[:TASK_TITLE_MAX],
        note=criteria or "",
        category=category,
        assignee_hint=hint,
        raw_text=raw,
    )


def _parse_task_decision(sub: str, rest: str, raw: str) -> Command:
    """``/task approve <REF> [for <who>]`` | ``/task reject <REF> [reason...]``."""
    pieces = rest.split(None, 1)
    if not pieces or not _REF_TOKEN_RE.match(pieces[0]):
        return Command(verb="task_unknown", raw_text=raw)
    tail = pieces[1].strip() if len(pieces) > 1 else ""
    if sub == "approve":
        remainder, hint = _split_assignee_hint(tail)
        if remainder:
            # A trailing phrase that is not a recognised hint is a typo'd one
            # ("for the ads team"), and guessing which teammate was meant is
            # exactly the mistake this grammar refuses to make.
            return Command(verb="task_unknown", raw_text=raw)
        return Command(
            verb="task_approve", ref=pieces[0], assignee_hint=hint, raw_text=raw
        )
    return Command(verb="task_reject", ref=pieces[0], note=tail, raw_text=raw)


# --------------------------------------------------------------------------
# task resolution
# --------------------------------------------------------------------------

#: Sentinel returned by :func:`resolve_task` when a task was found by exact ref
#: but the sender does not own it (and is not the operator override).
NOT_OWNED = "not_owned"

_TERMINAL_STATUSES = frozenset({"done", "cancelled"})


@dataclass(frozen=True)
class AmbiguousMatch:
    """2+ of the sender's own cards share the requested title prefix."""

    candidates: list[dict[str, Any]]


def resolve_task(
    collab: CollabStore, ref_or_prefix: str | None, employee_id: str
) -> dict[str, Any] | str | AmbiguousMatch | None:
    """Resolve a command's ``<ref>`` to exactly one board task.

    Two strategies, tried in order named by the SHAPE of the input:

    * A ref-shaped token (``U3`` or a displayed ``btk_`` id) is looked up EXACTLY
      (case-insensitive)
      across every non-archived card, any owner. A hit whose owner is neither
      the sender is refused unless the sender is ``emp_owner`` (the operator
      override).  An ownerless, top-level OPEN card is claimable by any roster
      sender; this is the shared pool's explicit exception.
    * Anything else is treated as a quoted title prefix and matched
      case-insensitively against the SENDER'S OWN non-terminal cards only —
      no operator override here, a title search is scoped to your own board on
      purpose. Zero hits is ``None``; 2+ is an :class:`AmbiguousMatch`.

    Returns the task dict on a single authorized match, or ``None`` when
    nothing matches at all.
    """
    text = (ref_or_prefix or "").strip()
    if not text:
        return None

    tasks = collab.list_board_tasks(archived=0)

    if _REF_TOKEN_RE.match(text):
        for task in tasks:
            ref = task.get("ref")
            exact = (ref is not None and str(ref).upper() == text.upper()) or (
                text.lower().startswith("btk_")
                and str(task.get("id") or "").lower() == text.lower()
            )
            if exact:
                owner = task.get("owner_employee_id")
                if (
                    owner is None
                    and task.get("parent_task_id") is None
                    and str(task.get("status") or "") == "open"
                    and str(task.get("source") or "") != BASELINE_SOURCE
                    and task.get("goal_id") is not None
                    and str(task.get("acceptance_criteria") or "").strip()
                ):
                    return task
                if employee_id != OPERATOR_EMPLOYEE_ID and str(owner or "") != employee_id:
                    return NOT_OWNED
                return task
        return None

    prefix = text.lower()
    candidates = [
        task
        for task in tasks
        if str(task.get("owner_employee_id") or "") == employee_id
        and str(task.get("status") or "") not in _TERMINAL_STATUSES
        and str(task.get("title") or "").lower().startswith(prefix)
    ]
    if not candidates:
        return None
    if len(candidates) > 1:
        return AmbiguousMatch(candidates=candidates)
    return candidates[0]


# --------------------------------------------------------------------------
# apply
# --------------------------------------------------------------------------


def _display_ref(task: Mapping[str, Any]) -> str:
    return str(task.get("ref") or task.get("id"))


def _apply_done(
    collab: CollabStore,
    team: TeamStore,
    task: Mapping[str, Any],
    employee_id: str,
    ref_permalink: str,
    note: str,
) -> str:
    task_id = str(task["id"])
    display = _display_ref(task)
    try:
        moved = collab.update_board_task(task_id, {"status": "done"}, actor=employee_id)
    except ValueError as exc:
        if "cannot be done without evidence" not in str(exc):
            return f"could not mark {display} done: {exc}"
        # Auto-attach the Slack message itself as the evidence a card with
        # acceptance criteria needs before it may become done, then retry
        # ONCE. This never turns "done" into "verified" — no verify_task call
        # here — it only satisfies the evidence gate the store already runs.
        try:
            team.add_evidence(
                kind="note",
                ref=ref_permalink,
                task_id=task_id,
                actor=employee_id,
                title=note or "completed via Slack",
                attribution="manual",
            )
        except ValueError as evidence_exc:
            return f"could not record evidence for {display}: {evidence_exc}"
        try:
            moved = collab.update_board_task(task_id, {"status": "done"}, actor=employee_id)
        except ValueError as retry_exc:
            return f"could not mark {display} done: {retry_exc}"
    if not moved:
        return f"could not mark {display} done (it changed underneath — try again)"
    return f"✓ {display} → done (recorded)"


def _apply_progress(
    collab: CollabStore,
    team: TeamStore,
    task: Mapping[str, Any],
    employee_id: str,
    ref_permalink: str,
    note: str,
) -> str:
    task_id = str(task["id"])
    display = _display_ref(task)
    try:
        team.add_evidence(
            kind="note",
            ref=f"{ref_permalink}#p",
            task_id=task_id,
            actor=employee_id,
            title=note,
            attribution="manual",
        )
    except ValueError as exc:
        return f"could not record progress on {display}: {exc}"

    def _body(connection: sqlite3.Connection) -> str:
        return append_task_event(
            connection, task_id=task_id, actor=employee_id, event="comment", note=note
        )

    try:
        collab._store._execute_write_txn(_body, op="team_slack.progress_comment")
    except Exception as exc:  # noqa: BLE001 -- evidence already landed; say so.
        return f"recorded progress on {display} but could not log the comment: {exc}"
    return f"✓ {display} → progress: {note} (recorded)"


def _apply_blocked(
    collab: CollabStore, task: Mapping[str, Any], employee_id: str, reason: str
) -> str:
    task_id = str(task["id"])
    display = _display_ref(task)
    try:
        moved = collab.update_board_task(
            task_id, {"status": "blocked", "blocked_reason": reason}, actor=employee_id
        )
    except ValueError as exc:
        return f"could not block {display}: {exc}"
    if not moved:
        return f"could not block {display} (it changed underneath — try again)"
    return f"✓ {display} → blocked: {reason} (recorded)"


def _apply_claim(collab: CollabStore, task: Mapping[str, Any], employee_id: str) -> str:
    task_id = str(task["id"])
    display = _display_ref(task)
    expect_version = int(task.get("claim_version") or 0)
    kwargs: dict[str, Any] = {"actor": employee_id}
    if "owner_employee_id" in inspect.signature(collab.claim_task).parameters:
        kwargs["owner_employee_id"] = employee_id
    won = collab.claim_task(task_id, f"human:{employee_id}", expect_version, **kwargs)
    if not won:
        return f"could not claim {display} — already claimed or changed (conflict)"
    return f"✓ {display} → claimed (recorded)"


def _apply_top(collab: CollabStore, task: Mapping[str, Any], employee_id: str) -> str:
    """Escalate an EXISTING card to urgent — ``!top``'s create-flag, as a verb.

    One idempotent PATCH: the priority-ranked queues and the pulse's 🔥
    markers pick the change up on their next read, so this writes nothing
    else. An already-urgent card replies as such rather than re-writing it —
    the second escalation of the same fire is a fact worth reflecting back.
    """
    task_id = str(task["id"])
    display = _display_ref(task)
    if str(task.get("priority") or "") == "urgent":
        return f"{display} is already urgent"
    try:
        moved = collab.update_board_task(task_id, {"priority": "urgent"}, actor=employee_id)
    except ValueError as exc:
        return f"could not escalate {display}: {exc}"
    if not moved:
        return f"could not escalate {display} (it changed underneath — try again)"
    return f"✓ {display} → priority urgent (recorded)"


#: Short names people actually type in Slack, mapped to the ``org_companies``
#: slug they mean. Unknown slugs are not guessed — the card is created without a
#: goal and the reply says so.
_COMPANY_SLUG_ALIASES: Mapping[str, str] = {
    "grok": "omniagentos",
    "omnios": "omniagentos",
    "omniagentos": "omniagentos",
    "omni": "initech",
}

#: The catch-all goal every company keeps for work that is not yet laddered to a
#: specific outcome. Oldest first, so a company that has two of them keeps using
#: the original rather than drifting onto whichever was created last.
_GENERAL_GOAL_SQL = (
    "SELECT cg.id FROM company_goals cg "
    "JOIN org_companies oc ON cg.org_company_id = oc.id "
    "WHERE oc.slug = ? AND cg.title LIKE 'General engineering%' "
    "ORDER BY cg.created_at ASC LIMIT 1"
)


def _resolve_company_goal(collab: CollabStore, slug: str) -> str | None:
    """The company's general-engineering goal id, or ``None`` (never an error).

    A slug nobody recognises must not cost the sender their card: goal-less is a
    valid owned card, and the reply names the miss so it can be fixed by hand.
    """
    try:
        row = collab._store._connection.execute(_GENERAL_GOAL_SQL, (slug,)).fetchone()
    except sqlite3.Error as exc:  # pragma: no cover -- schema drift, not a user path
        print(f"slack_updates: company goal lookup failed for {slug!r}: {exc}", file=sys.stderr)
        return None
    return None if row is None else str(row[0])


def _apply_task(
    collab: CollabStore,
    command: Command,
    employee_id: str,
    slack_map: Mapping[str, str],
) -> str:
    """Create one owned card from ``task`` and confirm it with its ``btk_`` id.

    NO ref is minted: a Slack-created card is tracked by the id printed here,
    which the grammar already accepts for done/progress/blocked, and inventing a
    ref would put this path into the ref-collision machinery for nothing.
    """
    lookup = {str(key).upper(): value for key, value in slack_map.items()}
    assignee = lookup.get(str(command.assignee_slack_id or "").upper())
    if assignee is None:
        return (
            f"unknown assignee {command.assignee_slack_id} — add them to "
            "configs/team_slack_map.yaml"
        )

    title = str(command.title or "")
    slug = (
        None
        if command.company is None
        else _COMPANY_SLUG_ALIASES.get(command.company, command.company)
    )
    goal_id = None if slug is None else _resolve_company_goal(collab, slug)

    task = BoardTask(
        title=title,
        description=f"Assigned by {employee_id} via Slack.\n{command.raw_text}",
        priority=command.priority,
        size="M",
        owner_employee_id=assignee,
        goal_id=goal_id,
        acceptance_criteria=f"Assigner ({employee_id}) confirms completion in thread.",
        # v4 Work-vs-Tasks: the bare verb is an ad-hoc path, so the card is a
        # zero-point Task. The stamp is the ONLY change here — the grammar,
        # authorization, and reply below stay byte-identical.
        source=team_tasks.TASK_ADHOC_SOURCE,
    )
    try:
        collab.create_board_task(task, actor=employee_id)
    except ValueError as exc:
        return f"could not create the task: {exc}"

    scope = command.priority if goal_id is None else f"{command.priority}, {slug}"
    reply = f"Created {task.id}: {title} → {assignee} ({scope}). Track with: done {task.id}"
    if slug is not None and goal_id is None:
        reply += f" (no company goal matched #{command.company} — created without one)"
    return reply


# --------------------------------------------------------------------------
# /task apply glue (v3) — parse -> resolve -> authorize -> apply -> reply,
# with at most ONE DM per action, routed per the spec's DM-flow table.
# --------------------------------------------------------------------------


def _resolve_slug(company: str | None) -> str | None:
    return None if company is None else _COMPANY_SLUG_ALIASES.get(company, company)


def _employee_for_mention(
    slack_map: Mapping[str, str], assignee_slack_id: str | None
) -> str | None:
    lookup = {str(key).upper(): value for key, value in slack_map.items()}
    return lookup.get(str(assignee_slack_id or "").upper())


def _apply_task_add(collab: CollabStore, command: Command, employee_id: str) -> str:
    """the operator queues one pool-eligible card. Adding IS approval — that is why the
    gate is the operator alone, and why the card must land pool-conformant
    (goal + acceptance criteria), never a goal-less orphan."""
    if not team_tasks.can_add(employee_id):
        return (
            "sorry — only the operator adds cards to the shared queue. "
            "`/task assign @name <title>` creates an ad-hoc task, or ask the operator to queue it."
        )
    slug = _resolve_slug(command.company)
    if slug is None:
        return (
            "the shared queue needs a company: `/task add <title> #company` "
            "(#globex #acmeuni #hooli #initech #grok)"
        )
    goal_id = _resolve_company_goal(collab, slug)
    if goal_id is None:
        return (
            f"no company goal matched #{command.company} — not queued. "
            "Known: #globex #acmeuni #hooli #initech #grok"
        )
    title = str(command.title or "")
    due_iso = team_tasks.parse_deadline(command.deadline)
    task = BoardTask(
        title=title,
        description=f"Queued by {employee_id} via /task add.\n{command.raw_text}",
        priority=command.priority,
        size="M",
        goal_id=goal_id,
        acceptance_criteria=str(command.note or "") or title,
        due_date=due_iso,
    )
    try:
        collab.create_board_task(task, actor=employee_id)
    except ValueError as exc:
        return f"could not queue the card: {exc}"
    due = team_tasks.render_due(due_iso)
    return (
        f"✓ queued {task.id}: {title} [#{slug}] ({command.priority}){due} — "
        f"claim with `/task claim {task.id}`"
    )


def _apply_task_assign(
    collab: CollabStore,
    team: TeamStore,
    command: Command,
    employee_id: str,
    slack_map: Mapping[str, str],
    notifier: Any,
) -> str:
    """Queue delegation (ref, the operator/Alice) or a new ad-hoc owned card (title,
    anyone-but-self). Either way: set the owner, DM the assignee once."""
    assignee = _employee_for_mention(slack_map, command.assignee_slack_id)
    if assignee is None:
        return (
            f"unknown assignee {command.assignee_slack_id} — add them to "
            "configs/team_slack_map.yaml"
        )
    if assignee not in team_tasks.active_employee_ids(team._store):
        return f"{team_tasks.display_name(assignee)} is not on the active roster — not assigned"
    due_iso = team_tasks.parse_deadline(command.deadline)
    due = team_tasks.render_due(due_iso)
    reverse_map = _reverse_map(slack_map)
    actor_name = team_tasks.display_name(employee_id)

    if command.ref is not None:  # ---- delegation from the shared queue
        if not team_tasks.can_delegate_queue(employee_id):
            return (
                "sorry — queue delegation is the operator/Alice only. "
                f"`/task claim {command.ref}` grabs it for yourself."
            )
        task = team_tasks.find_task_by_ref(collab, command.ref)
        if task is None:
            return "no matching task"
        display = _display_ref(task)
        owner = task.get("owner_employee_id")
        if owner is not None:
            return (
                f"{display} is owned by {team_tasks.display_name(str(owner))} — "
                f"`/task reassign {display} @name` moves it"
            )
        if str(task.get("status") or "") != "open":
            return f"{display} is not open (status: {task.get('status')}) — cannot delegate it"
        if not team_tasks.is_queue_card(task):
            return (
                f"{display} isn't a shared-queue card (agent/system work) — "
                "delegation covers the queue only"
            )
        won = team_tasks.assign_pool_card(
            collab,
            str(task["id"]),
            assignee,
            employee_id,
            due_date=due_iso,
            priority=None if command.priority == "normal" else command.priority,
        )
        if not won:
            return f"could not delegate {display} — someone grabbed it first (try again)"
        dm_sent = employee_id != assignee and team_tasks.send_dm(
            notifier,
            reverse_map,
            assignee,
            f"{actor_name} assigned you {display} — {task.get('title')}{due}",
        )
        suffix = f" — DMed {team_tasks.display_name(assignee)}" if dm_sent else ""
        return f"✓ {display} → {assignee} (delegated){due}{suffix}"

    # ---- ad-hoc owned card (free title)
    if assignee == employee_id:
        return (
            "assign is for handing work to a teammate — "
            "`/task claim <REF>` grabs queue work for yourself"
        )
    title = str(command.title or "")
    slug = _resolve_slug(command.company)
    goal_id = None if slug is None else _resolve_company_goal(collab, slug)
    task_model = BoardTask(
        title=title,
        description=f"Assigned by {employee_id} via /task assign.\n{command.raw_text}",
        priority=command.priority,
        size="M",
        owner_employee_id=assignee,
        goal_id=goal_id,
        acceptance_criteria=f"Assigner ({employee_id}) confirms completion in thread.",
        due_date=due_iso,
        # v4 Work-vs-Tasks: a free-title assign is the other ad-hoc path —
        # a zero-point Task (the ref-shaped delegation branch above stays
        # unstamped: a delegated queue card remains Work).
        source=team_tasks.TASK_ADHOC_SOURCE,
    )
    try:
        collab.create_board_task(task_model, actor=employee_id)
    except ValueError as exc:
        return f"could not create the task: {exc}"
    dm_sent = team_tasks.send_dm(
        notifier,
        reverse_map,
        assignee,
        f"{actor_name} assigned you {task_model.id} — {title}{due}",
    )
    scope = command.priority if goal_id is None else f"{command.priority}, {slug}"
    suffix = f" — DMed {team_tasks.display_name(assignee)}" if dm_sent else ""
    reply = f"✓ created {task_model.id}: {title} → {assignee} ({scope}){due}{suffix}"
    if slug is not None and goal_id is None:
        reply += f" (no company goal matched #{command.company} — created without one)"
    return reply


def _apply_task_done(
    collab: CollabStore,
    team: TeamStore,
    command: Command,
    employee_id: str,
    ref_permalink: str,
    slack_map: Mapping[str, str],
    notifier: Any,
) -> str:
    """Owner-only done (NO operator override — the matrix says the owner),
    then one DM to the assigner: '<owner> completed <REF> — <title>'."""
    task = team_tasks.find_task_by_ref(collab, command.ref)
    if task is None:
        return "no matching task"
    display = _display_ref(task)
    owner = task.get("owner_employee_id")
    if owner is None:
        return f"{display} has no owner — `/task claim {display}` it first"
    if str(owner) != employee_id:
        return (
            f"only the owner ({team_tasks.display_name(str(owner))}) can mark {display} done — "
            f"you can `/task note {display} <text>` instead"
        )
    reply = _apply_done(collab, team, task, employee_id, ref_permalink, command.note)
    if not reply.startswith("✓"):
        return reply
    assigner = team_tasks.resolve_assigner(team, task)
    if assigner and assigner != employee_id:
        sent = team_tasks.send_dm(
            notifier,
            _reverse_map(slack_map),
            assigner,
            f"{team_tasks.display_name(employee_id)} completed {display} — {task.get('title')}",
        )
        if sent:
            reply += f" — DMed {team_tasks.display_name(assigner)}"
    return reply


def _apply_task_note(
    collab: CollabStore,
    team: TeamStore,
    command: Command,
    employee_id: str,
    slack_map: Mapping[str, str],
    notifier: Any,
) -> str:
    """Anyone on the roster comments; the assigner is DMed (or the owner,
    when the noter IS the assigner). Never more than one DM."""
    task = team_tasks.find_task_by_ref(collab, command.ref)
    if task is None:
        return "no matching task"
    display = _display_ref(task)
    try:
        team_tasks.append_comment(collab, str(task["id"]), employee_id, command.note)
    except Exception as exc:  # noqa: BLE001 -- surface the refusal, never a traceback
        return f"could not note {display}: {exc}"
    assigner = team_tasks.resolve_assigner(team, task)
    owner = task.get("owner_employee_id")
    recipient = assigner if assigner != employee_id else (None if owner is None else str(owner))
    reply = f"✓ noted on {display}: {command.note}"
    if recipient and recipient != employee_id:
        sent = team_tasks.send_dm(
            notifier,
            _reverse_map(slack_map),
            recipient,
            f"{team_tasks.display_name(employee_id)} on {display} — "
            f"{task.get('title')}: {command.note}",
        )
        if sent:
            reply += f" — DMed {team_tasks.display_name(recipient)}"
    return reply


def _apply_task_reassign(
    collab: CollabStore,
    team: TeamStore,
    command: Command,
    employee_id: str,
    slack_map: Mapping[str, str],
    notifier: Any,
) -> str:
    """the operator/Alice always; otherwise the current owner hands their own card off.
    DMs the new assignee; the reply (and the event note) names the old owner."""
    assignee = _employee_for_mention(slack_map, command.assignee_slack_id)
    if assignee is None:
        return (
            f"unknown assignee {command.assignee_slack_id} — add them to "
            "configs/team_slack_map.yaml"
        )
    if assignee not in team_tasks.active_employee_ids(team._store):
        return f"{team_tasks.display_name(assignee)} is not on the active roster — not reassigned"
    task = team_tasks.find_task_by_ref(collab, command.ref)
    if task is None:
        return "no matching task"
    display = _display_ref(task)
    owner = None if task.get("owner_employee_id") is None else str(task["owner_employee_id"])
    if not team_tasks.can_reassign(employee_id, owner):
        holder = "nobody" if owner is None else team_tasks.display_name(owner)
        return (
            f"sorry — {display} is {holder}'s card; only the operator/Alice or the current owner "
            "may reassign it"
        )
    if owner == assignee:
        return f"{display} is already {team_tasks.display_name(assignee)}'s"
    won = team_tasks.reassign_card(collab, str(task["id"]), owner, assignee, employee_id)
    if not won:
        return f"could not reassign {display} (it changed underneath — try again)"
    dm_sent = employee_id != assignee and team_tasks.send_dm(
        notifier,
        _reverse_map(slack_map),
        assignee,
        f"{team_tasks.display_name(employee_id)} reassigned {display} to you — "
        f"{task.get('title')} (from {'the pool' if owner is None else team_tasks.display_name(owner)})",
    )
    suffix = f" — DMed {team_tasks.display_name(assignee)}" if dm_sent else ""
    return f"✓ {display} → {assignee} (was {owner or 'pool'}){suffix}"


def _reverse_map(slack_map: Mapping[str, str]) -> dict[str, str]:
    """employee id -> Slack user id, first mapping wins (collision logged by
    the notify module's twin; this one stays quiet — the DM path already logs
    an unmapped recipient)."""
    reverse: dict[str, str] = {}
    for slack_id, employee_id in slack_map.items():
        reverse.setdefault(str(employee_id), str(slack_id))
    return reverse


def _undelivered_suffix(delivery: Mapping[str, bool]) -> str:
    """`` — ⚠ DM to the operator not delivered`` for every recipient that did not get one.

    The board write has already happened and is not conditional on Slack, so the
    reply must not be either — but it must not CLAIM the notification. A silent
    failure here is the whole point of the verb going missing: the operator never learns
    a proposal is waiting, and the person who typed it believes they told him.
    """
    missed = [team_tasks.display_name(employee) for employee, ok in delivery.items() if not ok]
    if not missed:
        return ""
    return " — ⚠ DM to " + ", ".join(sorted(missed)) + " not delivered"


def _proposal_reply_prefix(decision: team_tasks.AutomationDecision, ref: str | None) -> str | None:
    """The shared refusal wording for approve/reject, or None when it applied."""
    if decision.outcome == "forbidden":
        return "sorry — only the operator approves or rejects automation proposals"
    if decision.outcome == "not_found":
        return "no matching proposal"
    if decision.outcome == "not_a_proposal":
        return (
            f"{ref} isn't an automation proposal — these verbs only decide cards "
            "created by `/task propose`"
        )
    if decision.outcome == "already_decided":
        status = "" if decision.task is None else str(decision.task.get("status") or "")
        return f"{ref} was already decided (status: {status})"
    return None


def _apply_task_propose(
    collab: CollabStore,
    team: TeamStore,
    command: Command,
    employee_id: str,
    slack_map: Mapping[str, str],
    notifier: Any,
) -> str:
    """Anyone on the active roster files an automation proposal for the operator.

    Wider than ``/task add`` (the operator-only) precisely because it approves nothing:
    the card lands in ``awaiting_approval``, where it is unclaimable and
    undispatchable until the operator decides. The DM to the operator is the whole point of the
    verb — a backlog nobody is told about is a backlog nobody reads.
    """
    try:
        task = team_tasks.propose_automation(
            collab,
            title=str(command.title or ""),
            proposed_by=employee_id,
            category=command.category,
            assignee_hint=command.assignee_hint,
            acceptance_criteria=str(command.note or ""),
            description=(
                f"Proposed by {employee_id} via /task propose.\n{command.raw_text}"
            ),
        )
    except ValueError as exc:
        return f"could not file the proposal: {exc}"
    hint = f" for {command.assignee_hint}" if command.assignee_hint else ""
    category = f" [#{command.category}]" if command.category else ""
    proposer = team_tasks.display_name(employee_id)
    delivered = team_tasks.send_dm(
        notifier,
        _reverse_map(slack_map),
        OPERATOR_EMPLOYEE_ID,
        f"💡 {proposer} proposed an automation: {task.id} — {task.title}{category}{hint} "
        f"(`/task approve {task.id}` · `/task reject {task.id} <reason>`)",
    )
    return (
        f"✓ proposed {task.id}: {task.title}{category}{hint} — awaiting the operator's approval"
        + _undelivered_suffix({OPERATOR_EMPLOYEE_ID: delivered})
    )


def _apply_task_approve(
    collab: CollabStore,
    team: TeamStore,
    command: Command,
    employee_id: str,
    slack_map: Mapping[str, str],
    notifier: Any,
) -> str:
    """the operator turns one proposal into queue work. The reply names WHERE it landed."""
    try:
        decision = team_tasks.approve_automation(
            collab, command.ref, actor=employee_id, assignee_hint=command.assignee_hint
        )
    except ValueError as exc:
        return f"could not approve: {exc}"
    refusal = _proposal_reply_prefix(decision, command.ref)
    if refusal is not None:
        return refusal
    assert decision.task is not None
    display = _display_ref(decision.task)
    title = str(decision.task.get("title") or "")
    if decision.assignee is not None:
        landing = f"assigned to {team_tasks.display_name(decision.assignee)}"
    elif decision.dispatched:
        # HONEST, not favourable: the card is routed to the pool but the
        # dispatcher cannot run it until somebody writes the executable spec.
        # "queued" would read as work in flight and nobody would go looking.
        landing = (
            "marked for the AI pool — dispatch needs an executable spec "
            "(acceptance command + owned paths); a coordinator completes it"
        )
    else:
        landing = "in the shared queue (claimable)"
    reverse_map = _reverse_map(slack_map)
    delivery: dict[str, bool] = {}
    proposer = team_tasks.proposer_of(decision.task)
    if proposer is not None and proposer != employee_id:
        delivery[proposer] = team_tasks.send_dm(
            notifier, reverse_map, proposer, f"✅ the operator approved {display} — {title} ({landing})"
        )
    if decision.assignee is not None and decision.assignee != employee_id:
        delivery[decision.assignee] = team_tasks.send_dm(
            notifier,
            reverse_map,
            decision.assignee,
            f"the operator assigned you {display} — {title} (approved automation)",
        )
    return f"✓ approved {display}: {title} — {landing}" + _undelivered_suffix(delivery)


def _apply_task_reject(
    collab: CollabStore,
    team: TeamStore,
    command: Command,
    employee_id: str,
    slack_map: Mapping[str, str],
    notifier: Any,
) -> str:
    """the operator declines one proposal. The reason is DMed back, not just recorded."""
    decision = team_tasks.reject_automation(
        collab, command.ref, actor=employee_id, reason=command.note
    )
    refusal = _proposal_reply_prefix(decision, command.ref)
    if refusal is not None:
        return refusal
    assert decision.task is not None
    display = _display_ref(decision.task)
    reason = " ".join(str(command.note).split())
    delivery: dict[str, bool] = {}
    proposer = team_tasks.proposer_of(decision.task)
    if proposer is not None and proposer != employee_id:
        because = f" — {reason}" if reason else ""
        delivery[proposer] = team_tasks.send_dm(
            notifier,
            _reverse_map(slack_map),
            proposer,
            f"🚫 the operator declined {display} — {decision.task.get('title')}{because}",
        )
    return (
        f"✓ rejected {display}"
        + (f" — {reason}" if reason else "")
        + _undelivered_suffix(delivery)
    )


def _apply_task_family(
    command: Command,
    employee_id: str,
    ref_permalink: str,
    collab: CollabStore,
    team: TeamStore,
    slack_map: Mapping[str, str],
    notifier: Any,
) -> str:
    """Dispatch one ``task_*`` verb. The roster half of the permission model:
    the sender must be an ACTIVE employee for every mutating verb (the Slack
    map gated mere membership upstream)."""
    if command.verb == "task_help":
        return team_tasks.help_card()
    if command.verb == "task_unknown":
        return "that's not a /task command I know — try `/task help`"
    if command.verb == "task_queue":
        return team_tasks.render_queue(team, _resolve_slug(command.company))
    if employee_id not in team_tasks.active_employee_ids(team._store):
        return "sorry — you're not on the active roster; ask the operator"
    if command.verb == "task_claim":
        resolved = resolve_task(collab, command.ref, employee_id)
        if resolved is None:
            return "no matching task"
        if isinstance(resolved, str):  # NOT_OWNED sentinel
            return "not your task"
        if isinstance(resolved, AmbiguousMatch):
            listing = "; ".join(
                f"{_display_ref(candidate)} {candidate.get('title')}"
                for candidate in resolved.candidates
            )
            return f"which one did you mean? {listing}"
        return _apply_claim(collab, resolved, employee_id)
    if command.verb == "task_add":
        return _apply_task_add(collab, command, employee_id)
    if command.verb == "task_assign":
        return _apply_task_assign(collab, team, command, employee_id, slack_map, notifier)
    if command.verb == "task_done":
        return _apply_task_done(
            collab, team, command, employee_id, ref_permalink, slack_map, notifier
        )
    if command.verb == "task_note":
        return _apply_task_note(collab, team, command, employee_id, slack_map, notifier)
    if command.verb == "task_reassign":
        return _apply_task_reassign(collab, team, command, employee_id, slack_map, notifier)
    if command.verb == "task_propose":
        return _apply_task_propose(collab, team, command, employee_id, slack_map, notifier)
    if command.verb == "task_approve":
        return _apply_task_approve(collab, team, command, employee_id, slack_map, notifier)
    if command.verb == "task_reject":
        return _apply_task_reject(collab, team, command, employee_id, slack_map, notifier)
    return "unsupported command"  # pragma: no cover -- TASK_VERBS is closed


def _render_my_queue(team: TeamStore, employee_id: str) -> str:
    """``/task mine`` / ``my queue`` — the two streams split, Tasks on top (v4).

    Section order is the spec's: ``📌 Tasks (N)`` first (omitted at zero, each
    line deadline-glyphed), then ``🔧 Work x/5`` where x = ongoing Work (open +
    claimed + in_progress + blocked, ad-hoc Tasks excluded) with the below-floor
    ⚠ — supply visibility, never a block — then the Work buckets as before.
    """
    queues = team.team_queues(employee_ids=[employee_id])
    bucket = queues.get(employee_id)
    if bucket is None:
        return "no queue found for you"
    today = team_tasks.local_today()

    def _work(cards: list[Any]) -> list[Any]:
        return [card for card in cards if not team_tasks.is_adhoc_task(card)]

    adhoc = [
        card
        for cards in (bucket.active, bucket.ready, bucket.blocked, bucket.review)
        for card in cards
        if team_tasks.is_adhoc_task(card)
    ]
    ready, active = _work(bucket.ready), _work(bucket.active)
    blocked, review = _work(bucket.blocked), _work(bucket.review)
    ongoing = len(ready) + len(active) + len(blocked)

    def _fmt(cards: list[Any]) -> str:
        if not cards:
            return "  (none)"
        return "\n".join(
            f"  {card.ref or card.id} {card.title}"
            f"{team_tasks.deadline_suffix(getattr(card, 'due_date', None), today=today)}"
            for card in cards
        )

    lines = ["*Your queue*"]
    if adhoc:
        lines.append(f"📌 Tasks ({len(adhoc)}):")
        lines.append(_fmt(adhoc))
    floor = "" if ongoing >= ACTIVE_QUEUE_FLOOR else " ⚠ below floor"
    lines.append(f"🔧 Work {ongoing}/{ACTIVE_QUEUE_FLOOR}{floor}")
    lines.extend(
        [
            f"Ready:\n{_fmt(ready)}",
            f"Active:\n{_fmt(active)}",
            f"Blocked:\n{_fmt(blocked)}",
            f"Review:\n{_fmt(review)}",
        ]
    )
    return "\n".join(lines)


def _render_report(team: TeamStore) -> str:
    try:
        from omniagentos.team.report import gather, render  # type: ignore[import-untyped]
    except ImportError:
        return "report module not yet installed"
    data = gather(team, utc_now_iso()[:10])
    return str(render(data))


def apply(
    command: Command,
    employee_id: str,
    ref_permalink: str,
    *,
    collab: CollabStore,
    team: TeamStore,
    slack_map: Mapping[str, str] | None = None,
    notifier: Any = None,
) -> str:
    """Mutate the board (or render a read) for one parsed command.

    Returns the confirmation/error text — EVERY applied update gets a threaded
    reply, success or refusal, so a Slack sender always knows what happened.

    ``slack_map`` is needed only by the assigning verbs (the assignee is a
    Slack mention); it defaults to the configured map so every other caller is
    unchanged. ``notifier`` (duck-typed ``post_dm``) carries the /task family's
    one-DM-per-action flows; ``None`` simply means no DM is sent — the board
    mutation and the threaded reply are never notifier-dependent.
    """
    if command.verb == "my_queue":
        return _render_my_queue(team, employee_id)
    if command.verb == "report":
        return _render_report(team)
    if command.verb == "task":
        return _apply_task(
            collab, command, employee_id, slack_map if slack_map is not None else _slack_map()
        )
    if command.verb in TASK_VERBS:
        return _apply_task_family(
            command,
            employee_id,
            ref_permalink,
            collab,
            team,
            slack_map if slack_map is not None else _slack_map(),
            notifier,
        )

    ref_or_prefix = command.ref if command.ref is not None else command.title_prefix
    resolved = resolve_task(collab, ref_or_prefix, employee_id)
    if resolved is None:
        return "no matching task"
    if isinstance(resolved, str):  # the only str resolve_task returns is NOT_OWNED
        return "not your task"
    if isinstance(resolved, AmbiguousMatch):
        listing = "; ".join(
            f"{_display_ref(candidate)} {candidate.get('title')}"
            for candidate in resolved.candidates
        )
        return f"which one did you mean? {listing}"

    task = resolved
    if command.verb == "done":
        return _apply_done(collab, team, task, employee_id, ref_permalink, command.note)
    if command.verb == "progress":
        return _apply_progress(collab, team, task, employee_id, ref_permalink, command.note)
    if command.verb == "blocked":
        return _apply_blocked(collab, task, employee_id, command.note)
    if command.verb == "claim":
        return _apply_claim(collab, task, employee_id)
    if command.verb == "top":
        return _apply_top(collab, task, employee_id)
    return "unsupported command"  # pragma: no cover -- parse_command's verb set is closed


# --------------------------------------------------------------------------
# Slack sender map
# --------------------------------------------------------------------------

#: The roster :func:`load_slack_map` validates every mapped employee id
#: against (mirrors ``omniagentos/company_goals/seed_employees.py``).
KNOWN_EMPLOYEES = frozenset({"emp_owner", "emp_alice", "emp_bob", "emp_frank"})

_DEFAULT_SLACK_MAP_PATH = Path(__file__).resolve().parent.parent.parent / (
    "configs/team_slack_map.yaml"
)


def load_slack_map(path: Path | None = None) -> dict[str, str]:
    """``configs/team_slack_map.yaml`` as a ``{slack_user_id: employee_id}`` dict.

    Raises ``ValueError`` on an employee id outside :data:`KNOWN_EMPLOYEES` — a
    typo'd id must fail loudly at load, not silently route nobody's commands. A
    missing/unparsable file degrades to ``{}`` (every sender then reads as
    unmapped and is logged+ignored, never an ingestion failure).
    """
    target = path or _DEFAULT_SLACK_MAP_PATH
    try:
        raw = yaml.safe_load(target.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        print(f"slack_updates: could not load {target}: {exc}", file=sys.stderr)
        return {}
    mapping = raw if isinstance(raw, dict) else {}
    result: dict[str, str] = {}
    for slack_id, employee_id in mapping.items():
        employee_id_str = str(employee_id)
        if employee_id_str not in KNOWN_EMPLOYEES:
            raise ValueError(
                f"{target}: unknown employee id {employee_id_str!r} for Slack user "
                f"{slack_id!r}; known ids are {sorted(KNOWN_EMPLOYEES)}"
            )
        result[str(slack_id)] = employee_id_str
    return result


_SLACK_MAP_CACHE: dict[str, str] | None = None


def _slack_map() -> dict[str, str]:
    global _SLACK_MAP_CACHE
    if _SLACK_MAP_CACHE is None:
        _SLACK_MAP_CACHE = load_slack_map()
    return _SLACK_MAP_CACHE


# --------------------------------------------------------------------------
# reply posting
# --------------------------------------------------------------------------

_CAPABILITY_ID = "slack_ingest.stream"
_BOT_TOKEN_ENV = "SLACK_BOT_TOKEN"
_AUDIT_HOLDER = "job:team-slack-updates"
_CHAT_POST_MESSAGE_URL = "https://slack.com/api/chat.postMessage"


def _resolve_bot_token() -> str:
    """The Slack bot token, through the credential broker (never a raw env read).

    Uses the SAME capability ``omniagentos.comms.sockets.slack`` already
    resolves (``slack_ingest.stream``) rather than a second, parallel
    credential path. Never raises — a resolution failure means no reply is
    posted, logged once, and the caller (an inbound command) is otherwise
    unaffected.
    """
    try:
        capability = load_registry().capability(_CAPABILITY_ID)
        resolved = resolve_for(capability, audit_context=AuditContext(holder=_AUDIT_HOLDER))
    except Exception as exc:  # noqa: BLE001 -- must never raise into post_reply.
        print(f"slack_updates: could not resolve {_BOT_TOKEN_ENV}: {exc}", file=sys.stderr)
        return ""
    return resolved.get(_BOT_TOKEN_ENV, "")


def permalink(channel: str, ts: str) -> str:
    """Best-effort Slack permalink: ``https://slack.com/archives/<channel>/p<ts-no-dot>``."""
    return f"https://slack.com/archives/{channel}/p{ts.replace('.', '')}"


def post_reply(
    channel: str,
    thread_ts: str,
    text: str,
    *,
    token_resolver: Callable[[], str] = _resolve_bot_token,
) -> None:
    """Post one threaded confirmation. NEVER raises.

    A failure to post (missing token, a Slack error, a network error) is a
    stderr line, never an exception — the command was already applied to the
    board; a lost confirmation must not look like a lost update.
    """
    token = token_resolver()
    if not token:
        print("slack_updates: no SLACK_BOT_TOKEN available; reply not sent", file=sys.stderr)
        return
    body = json.dumps(
        {"channel": channel, "thread_ts": thread_ts, "text": text, "unfurl_links": False}
    ).encode("utf-8")
    request = urllib.request.Request(
        _CHAT_POST_MESSAGE_URL,
        data=body,
        headers={
            "Content-Type": "application/json; charset=utf-8",
            "Authorization": f"Bearer {token}",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, ValueError, OSError) as exc:
        print(f"slack_updates: chat.postMessage failed: {exc}", file=sys.stderr)
        return
    if not payload.get("ok"):
        print(f"slack_updates: slack error: {payload.get('error')}", file=sys.stderr)


# --------------------------------------------------------------------------
# entry point (called from omniagentos.comms.sockets.slack)
# --------------------------------------------------------------------------

_COLLAB_STORE_CACHE: CollabStore | None = None


def _collab_store() -> CollabStore:
    global _COLLAB_STORE_CACHE
    if _COLLAB_STORE_CACHE is None:
        from omniagentos import contracts

        _COLLAB_STORE_CACHE = CollabStore(db_path=contracts.default_db_path())
    return _COLLAB_STORE_CACHE


def team_updates_handle(
    event: Mapping[str, Any],
    *,
    collab: CollabStore | None = None,
    team: TeamStore | None = None,
    slack_map: Mapping[str, str] | None = None,
    poster: Callable[[str, str, str], None] | None = None,
    notifier: Any = None,
) -> None:
    """Parse one Slack message event and, if it is a command, apply + reply.

    The caller (``omniagentos.comms.sockets.slack``) has normally gated this on
    the feature flag and channel allowlist; when the transport admits direct
    messages this handler is already DM-compatible. It does exactly two more
    checks of its own: is the text a
    recognised command (silently ignored if not — normal chatter must never
    error), and is the sender on the roster (logged, one stderr line, and
    ignored if not — the channel is not roster-exclusive).

    ``collab``/``team``/``slack_map``/``poster`` are injection points for
    tests; production calls (from the socket hook) pass none of them and get
    the real store/config/poster.
    """
    text = str(event.get("text") or "")
    command = parse_command(text)
    if command is None:
        return

    sender_map = slack_map if slack_map is not None else _slack_map()
    slack_user_id = str(event.get("user") or "")
    employee_id = sender_map.get(slack_user_id)
    if employee_id is None:
        print(
            f"slack_updates: ignoring command from unmapped Slack sender "
            f"{slack_user_id!r} (not in configs/team_slack_map.yaml)",
            file=sys.stderr,
        )
        return

    channel = str(event.get("channel") or "")
    ts = str(event.get("ts") or "")
    thread_ts = str(event.get("thread_ts") or ts) or ts

    collab_store = collab if collab is not None else _collab_store()
    team_store = team if team is not None else TeamStore(collab_store._store)

    dm_notifier = notifier
    if dm_notifier is None and command.verb in TASK_DM_VERBS:
        # Built lazily, and only for verbs that DM: a claim/queue read must
        # never spend a credential-broker resolution. The token rides the
        # same brokered capability post_reply resolves.
        token = _resolve_bot_token()
        if token:
            from omniagentos.team.notify import SlackNotifier  # local: notify imports this module

            dm_notifier = SlackNotifier(token)

    reply = apply(
        command,
        employee_id,
        permalink(channel, ts),
        collab=collab_store,
        team=team_store,
        slack_map=sender_map,
        notifier=dm_notifier,
    )

    reply_fn = poster if poster is not None else post_reply
    reply_fn(channel, thread_ts, reply)
