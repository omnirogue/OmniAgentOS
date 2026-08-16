"""Shared helpers for the ``/task`` command engine (v3, the operator's ruling 2026-08-13).

``omniagentos.team.slack_updates`` owns the parse -> resolve -> authorize ->
apply -> reply loop; this module holds the pieces of the /task grammar that are
useful on their own and would otherwise bloat that file:

* **Deadline parsing** — the natural trailing phrases (``immediately``,
  ``in 2 hours``, ``today``, ``tomorrow``, ``by friday``) to an ISO local
  timestamp for ``board_tasks.due_date``.
* **The permission matrix** — who may add to the shared queue (the operator only),
  who may delegate from it (the operator + Alice), who may reassign (the operator/Alice always,
  else the current owner).
* **Assigner resolution** — the actor of the latest ``assign`` task event by
  someone other than the owner, falling back to the card's creator; this is
  who a ``done``/``note`` DM goes to.
* **Guarded ownership writes** — assignment/reassignment as compare-and-set
  UPDATEs with the audit event in the same transaction, mirroring the idiom
  the (now machine-only) dispatcher established. Event notes deliberately
  avoid the ``owner:<emp>`` token so the event watcher never sends a second
  DM on top of the one the /task engine sends: one action, one DM.

NOTE on identities: employee ids (``emp_alice``, ``emp_bob``) are stable
roster identifiers seeded by ``omniagentos/company_goals/seed_employees.py``;
Slack user ids are NEVER hardcoded — they resolve at runtime through
``configs/team_slack_map.yaml``.
"""

from __future__ import annotations

import json
import re
import sqlite3
import sys
import time
from collections.abc import Mapping
from datetime import datetime, timedelta
from typing import Any, NamedTuple, cast

from omniagentos.collab.contracts import BASELINE_SOURCE, BoardTask, BoardTaskStatus
from omniagentos.collab.store import CollabStore, append_task_event
from omniagentos.contracts import utc_now_iso
from omniagentos.team.contracts import (
    AUTOMATION_CATEGORY_PREFIX,
    AUTOMATION_PARENT_GOAL_ID,
    AUTOMATION_PROPOSAL_SOURCE,
    OPERATOR_EMPLOYEE_ID,
    POOL_CARD_LIMIT,
    PROPOSAL_ASSIGNEE_HINTS,
    TASK_ADHOC_SOURCE,
)
from omniagentos.team.dispatch import COMPUTE_POOL_TARGET
from omniagentos.team.store import TeamStore

__all__ = [
    "AUTOMATION_PARENT_GOAL_ID",
    "COMPANY_ORDER",
    "DEADLINE_TAIL_RE",
    "DELEGATORS",
    "PRIORITY_GLYPHS",
    "TASK_ADHOC_SOURCE",
    "active_employee_ids",
    "add_pool_task",
    "approve_automation",
    "automation_categories",
    "automation_goal_for",
    "match_automation_category",
    "append_comment",
    "assign_adhoc_task",
    "assign_pool_card",
    "can_add",
    "can_delegate_queue",
    "can_reassign",
    "deadline_suffix",
    "display_name",
    "find_task_by_ref",
    "help_card",
    "is_adhoc_task",
    "local_today",
    "parse_deadline",
    "propose_automation",
    "proposal_hint",
    "proposer_of",
    "reassign_card",
    "reject_automation",
    "resolve_automation_category",
    "render_due",
    "render_queue",
    "resolve_assigner",
    "resolve_company_goal",
    "send_dm",
    "split_deadline",
]

#: The company's "General engineering —" goal — the ladder the shared queue and
#: EDC delegations file a card under. Identical to the slack engine's private
#: lookup; exposed here as the PUBLIC seam so callers outside the /task grammar
#: (the Executive Decision Center's delegate/defer executors) reuse the same
#: resolution instead of re-implementing it (RESOLUTIONS-2 F07).
_GENERAL_GOAL_SQL = (
    "SELECT cg.id FROM company_goals cg "
    "JOIN org_companies oc ON cg.org_company_id = oc.id "
    "WHERE oc.slug = ? AND cg.title LIKE 'General engineering%' "
    "ORDER BY cg.created_at ASC LIMIT 1"
)


def resolve_company_goal(collab: CollabStore, slug: str) -> str | None:
    """The company's general-engineering goal id for ``slug``, or ``None``.

    Never raises: a slug nobody recognises returns ``None`` so the caller can
    decide (an owned card is valid goal-less; a pool card is not). This is the
    public twin of ``slack_updates._resolve_company_goal`` — one query, one
    place, so the EDC glue does not fork a second copy (F07).
    """
    try:
        row = collab._store._connection.execute(_GENERAL_GOAL_SQL, (slug,)).fetchone()
    except sqlite3.Error as exc:  # pragma: no cover -- schema drift, not a user path
        print(f"team-tasks: company goal lookup failed for {slug!r}: {exc}", file=sys.stderr)
        return None
    return None if row is None else str(row[0])


def assign_adhoc_task(
    collab: CollabStore,
    *,
    title: str,
    description: str,
    owner_employee_id: str,
    actor: str,
    goal_id: str | None = None,
    ref: str | None = None,
    acceptance_criteria: str = "",
    due_date: str | None = None,
    priority: str = "normal",
    source: str = TASK_ADHOC_SOURCE,
) -> BoardTask:
    """Create ONE owned board card — the ad-hoc-assign primitive, made public.

    This is the exact ``BoardTask`` + ``create_board_task`` the /task engine's
    ad-hoc branch builds; exposing it here lets the EDC delegate executor reuse
    the SAME card + ``create`` event write instead of re-implementing card/event
    logic (F07). The ``create`` event's note is the card title — deliberately
    NO ``owner:<emp>`` token, so a task-event watcher never DMs on top of the
    one DM the caller sends (one action, one DM). Returns the created model so
    the caller can record its ``id``/``ref`` linkage. ``source`` defaults to the
    zero-point ad-hoc discriminator; a caller creating scored Work (an EDC
    delegation) passes its own value.
    """
    task = BoardTask(
        title=title,
        description=description,
        priority=priority,
        size="M",
        owner_employee_id=owner_employee_id,
        goal_id=goal_id,
        ref=ref,
        acceptance_criteria=acceptance_criteria,
        due_date=due_date,
        source=source,
    )
    collab.create_board_task(task, actor=actor)
    return task


def add_pool_task(
    collab: CollabStore,
    *,
    title: str,
    description: str,
    actor: str,
    goal_id: str,
    acceptance_criteria: str,
    ref: str | None = None,
    due_date: str | None = None,
    priority: str = "normal",
    source: str = "decision",
) -> BoardTask:
    """Queue ONE ownerless, pool-eligible card — the ``/task add`` primitive.

    Public twin of ``slack_updates._apply_task_add``'s card write: a pool card
    must be goal-laddered AND carry acceptance criteria (that is what makes it
    claimable Work rather than a goal-less orphan), so both are required. The
    caller (the operator-only, per the matrix — enforced by the caller) owns the
    authorization decision; this seam only writes the conformant card + its
    ``create`` event. Returns the created model for id/ref linkage.
    """
    task = BoardTask(
        title=title,
        description=description,
        priority=priority,
        size="M",
        goal_id=goal_id,
        owner_employee_id=None,
        ref=ref,
        acceptance_criteria=acceptance_criteria,
        due_date=due_date,
        source=source,
    )
    collab.create_board_task(task, actor=actor)
    return task


# --------------------------------------------------------------------------
# permission matrix (v3 addendum, the operator 2026-08-13)
# --------------------------------------------------------------------------

#: Who may delegate a card OUT of the shared queue, and who may reassign any
#: card regardless of ownership. The operator plus Alice — the two people the
#: v3 ruling names. Bob (and everyone else) delegates nothing and reassigns
#: only cards they currently own (a hand-off).
DELEGATORS: frozenset[str] = frozenset({OPERATOR_EMPLOYEE_ID, "emp_alice"})


def can_add(employee_id: str) -> bool:
    """Only the operator adds/approves cards INTO the shared queue — adding IS approval."""
    return employee_id == OPERATOR_EMPLOYEE_ID


def can_delegate_queue(employee_id: str) -> bool:
    """the operator + Alice may hand a queue card to someone."""
    return employee_id in DELEGATORS


def can_reassign(employee_id: str, owner_employee_id: str | None) -> bool:
    """the operator/Alice always; otherwise only the card's CURRENT owner (a hand-off)."""
    if employee_id in DELEGATORS:
        return True
    return owner_employee_id is not None and employee_id == owner_employee_id


def active_employee_ids(store: Any) -> frozenset[str]:
    """Employee ids with ``status='active'`` — the assignable roster half.

    ``store`` is the shared :class:`SqliteStore` (``collab._store`` /
    ``team._store``); imported lazily so this module never grows a hard
    dependency on the company-goals package at import time.
    """
    from omniagentos.company_goals.store import CompanyGoalsStore

    return frozenset(
        str(row["id"]) for row in CompanyGoalsStore(store).list_employees(status="active")
    )


def display_name(employee_id: str) -> str:
    """``emp_bob`` -> ``Bob`` — the same rendering the morning DM uses."""
    return employee_id.removeprefix("emp_").capitalize()


# --------------------------------------------------------------------------
# deadlines — natural trailing phrase -> ISO local timestamp
# --------------------------------------------------------------------------

_WEEKDAY_INDEX: dict[str, int] = {
    "monday": 0, "mon": 0,
    "tuesday": 1, "tue": 1, "tues": 1,
    "wednesday": 2, "wed": 2,
    "thursday": 3, "thu": 3, "thur": 3, "thurs": 3,
    "friday": 4, "fri": 4,
    "saturday": 5, "sat": 5,
    "sunday": 6, "sun": 6,
}  # fmt: skip

_WEEKDAY_WORDS = "|".join(sorted(_WEEKDAY_INDEX, key=len, reverse=True))

#: The whole deadline vocabulary, anchored to the END of the text — a deadline
#: is always the LAST phrase of a /task message, so a title may still contain
#: these words mid-sentence without losing them.
DEADLINE_TAIL_RE = re.compile(
    rf"(?:^|\s)(immediately|today|tomorrow"
    rf"|in\s+\d+\s+(?:minutes?|mins?|hours?|hrs?|days?)"
    rf"|by\s+(?:{_WEEKDAY_WORDS}))\s*$",
    re.IGNORECASE,
)

_IN_RE = re.compile(r"^in\s+(\d+)\s+(minutes?|mins?|hours?|hrs?|days?)$", re.IGNORECASE)
_BY_RE = re.compile(rf"^by\s+({_WEEKDAY_WORDS})$", re.IGNORECASE)

#: Fixed local times the day-phrases resolve to (the v3 spec pins them).
_TODAY_HOUR = 18
_MORNING_HOUR = 10


def split_deadline(text: str) -> tuple[str, str | None]:
    """``('fix the login page', 'by friday')`` — or ``(text, None)``.

    Only the TAIL is a deadline; everything before it stays untouched.
    """
    match = DEADLINE_TAIL_RE.search(text)
    if match is None:
        return text.strip(), None
    return text[: match.start()].strip(), " ".join(match.group(1).split())


def parse_deadline(phrase: str | None, *, now: datetime | None = None) -> str | None:
    """One deadline phrase -> ISO-8601 local timestamp (minute precision).

    Local time on purpose: "tomorrow 10:00" means the team's wall clock, and
    the aware offset in the ISO string keeps it unambiguous for every reader.
    ``None``/unknown phrases return ``None`` — the caller simply creates the
    card without a due date, never an error.
    """
    if not phrase:
        return None
    text = " ".join(phrase.split()).lower()
    moment = now if now is not None else datetime.now().astimezone()
    if moment.tzinfo is None:
        moment = moment.astimezone()

    target: datetime | None = None
    if text == "immediately":
        target = moment
    elif text == "today":
        target = moment.replace(hour=_TODAY_HOUR, minute=0, second=0, microsecond=0)
    elif text == "tomorrow":
        target = (moment + timedelta(days=1)).replace(
            hour=_MORNING_HOUR, minute=0, second=0, microsecond=0
        )
    else:
        relative = _IN_RE.match(text)
        if relative is not None:
            amount = int(relative.group(1))
            unit = relative.group(2)
            try:
                if unit.startswith("min"):
                    target = moment + timedelta(minutes=amount)
                elif unit.startswith(("hour", "hr")):
                    target = moment + timedelta(hours=amount)
                else:
                    target = moment + timedelta(days=amount)
            except OverflowError:
                # "in 99999999 days" — an absurd amount is no deadline, and the
                # always-reply contract must survive it.
                return None
        else:
            weekday = _BY_RE.match(text)
            if weekday is not None:
                index = _WEEKDAY_INDEX[weekday.group(1).lower()]
                ahead = (index - moment.weekday()) % 7
                candidate = (moment + timedelta(days=ahead)).replace(
                    hour=_MORNING_HOUR, minute=0, second=0, microsecond=0
                )
                if candidate <= moment:  # already past this week's slot
                    candidate += timedelta(days=7)
                target = candidate
    if target is None:
        return None
    return target.isoformat(timespec="minutes")


def render_due(due_iso: str | None) -> str:
    """`` ⏰ due 2026-08-14 10:00`` (leading space) or ``''`` — reply/DM suffix."""
    if not due_iso:
        return ""
    return f" ⏰ due {due_iso[:16].replace('T', ' ')}"


def deadline_suffix(due: object, *, today: str) -> str:
    """`` ⏰2026-08-14`` for a deadline, `` 🔴⏰…`` once its DAY has passed.

    The ONE deadline-glyph vocabulary every queue surface uses (brief, pulse,
    ``/task queue``, ``/task mine``). Date-granular on purpose: deadlines are
    stored as ISO dates or datetimes, and a card due 'today 18:00' stays ⏰
    until tomorrow rather than flipping 🔴 mid-afternoon on a clock the reader
    cannot see. No deadline renders as ``''``, never a placeholder.
    """
    if not due:
        return ""
    day = str(due)[:10]
    return f" 🔴⏰{day}" if day < today else f" ⏰{day}"


def local_today() -> str:
    """The LOCAL ``YYYY-MM-DD`` day every deadline glyph compares against —
    the same wall clock the daybrief keys on (deadlines are a human promise,
    not a UTC event)."""
    return time.strftime("%Y-%m-%d")


# --------------------------------------------------------------------------
# resolution
# --------------------------------------------------------------------------


def find_task_by_ref(collab: CollabStore, ref: str | None) -> dict[str, Any] | None:
    """Exact ref (or displayed ``btk_`` id) lookup across every non-archived
    card, ANY owner — the /task engine authorizes per-verb, so unlike
    ``resolve_task`` this never refuses on ownership."""
    text = (ref or "").strip()
    if not text:
        return None
    for task in collab.list_board_tasks(archived=0):
        candidate = task.get("ref")
        if candidate is not None and str(candidate).upper() == text.upper():
            return task
        if text.lower().startswith("btk_") and str(task.get("id") or "").lower() == text.lower():
            return task
    return None


def is_queue_card(task: Mapping[str, Any]) -> bool:
    """The shared-queue half of the pool predicate: goal-laddered, with
    acceptance criteria, top-level, and not a frozen baseline import.

    Delegation must not reach agent/system cards that merely happen to be
    ownerless and open (owner/status are checked by the caller's guarded
    write) — a swarm's working card pulled into a human queue interleaves the
    human done-gate with the agent claim CAS.
    """
    if task.get("parent_task_id") is not None:
        return False
    if not task.get("goal_id"):
        return False
    if not str(task.get("acceptance_criteria") or "").strip():
        return False
    return str(task.get("source") or "") != BASELINE_SOURCE


def is_adhoc_task(card: Any) -> bool:
    """True when ``card`` is a minor ad-hoc **Task** (v4 Work-vs-Tasks split).

    The single source of truth is ``source == 'task-adhoc'``, stamped at
    creation by the two ad-hoc paths (``/task assign @name <free title>`` and
    the bare ``task`` verb). Accepts a board-row mapping or a model
    (:class:`~omniagentos.team.contracts.QueueCard` / ``BoardTask``) so every
    renderer can ask the same question of whatever shape it already holds.
    """
    if isinstance(card, Mapping):
        source = card.get("source")
    else:
        source = getattr(card, "source", None)
    return str(source or "") == TASK_ADHOC_SOURCE


def resolve_assigner(team: TeamStore, task: Mapping[str, Any]) -> str | None:
    """Who assigned this card: the actor of the latest ``assign`` task event
    by someone other than the owner; fallback, the card's creator."""
    owner = str(task.get("owner_employee_id") or "")
    events = team.list_events(str(task["id"]))
    for event in reversed(events):
        actor = str(event.get("actor") or "")
        if str(event.get("event")) == "assign" and actor and actor != owner:
            return actor
    for event in events:
        if str(event.get("event")) == "create":
            return str(event.get("actor") or "") or None
    return None


# --------------------------------------------------------------------------
# guarded ownership writes (event rides the same transaction)
# --------------------------------------------------------------------------


def assign_pool_card(
    collab: CollabStore,
    task_id: str,
    new_owner: str,
    actor: str,
    *,
    due_date: str | None = None,
    priority: str | None = None,
) -> bool:
    """Delegate one ownerless OPEN card. ``False`` = lost the race.

    The event's actor is the ASSIGNER (that is what ``resolve_assigner`` reads
    back for the done/note DMs) and its note deliberately avoids the
    ``owner:<emp>`` token so the event watcher does not DM on top of the
    /task engine's own DM.
    """

    def body(connection: sqlite3.Connection) -> bool:
        sets = ["owner_employee_id = ?", "updated_at = ?"]
        parameters: list[Any] = [new_owner, utc_now_iso()]
        if due_date is not None:
            sets.append("due_date = ?")
            parameters.append(due_date)
        if priority is not None:
            sets.append("priority = ?")
            parameters.append(priority)
        parameters.append(task_id)
        cursor = connection.execute(
            f"UPDATE board_tasks SET {', '.join(sets)} "
            "WHERE id = ? AND owner_employee_id IS NULL AND status = 'open'",
            parameters,
        )
        if cursor.rowcount <= 0:
            return False
        append_task_event(
            connection,
            task_id=task_id,
            actor=actor,
            event="assign",
            note=f"slack_delegate to {new_owner}",
        )
        return True

    return bool(collab._store._execute_write_txn(body, op="team_slack.task_delegate"))


def reassign_card(
    collab: CollabStore,
    task_id: str,
    expect_owner: str | None,
    new_owner: str,
    actor: str,
) -> bool:
    """Move an existing card to ``new_owner``, CAS-guarded on the CURRENT owner
    so two racing reassigns cannot both win. The event note names the old
    owner (the "notes the old owner" half of the spec) — again without the
    ``owner:`` watcher token."""

    def body(connection: sqlite3.Connection) -> bool:
        if expect_owner is None:
            guard = "owner_employee_id IS NULL"
            parameters: list[Any] = [new_owner, utc_now_iso(), task_id]
        else:
            guard = "owner_employee_id = ?"
            parameters = [new_owner, utc_now_iso(), task_id, expect_owner]
        cursor = connection.execute(
            f"UPDATE board_tasks SET owner_employee_id = ?, updated_at = ? "
            f"WHERE id = ? AND {guard}",
            parameters,
        )
        if cursor.rowcount <= 0:
            return False
        append_task_event(
            connection,
            task_id=task_id,
            actor=actor,
            event="assign",
            note=f"slack_reassign to {new_owner} (was {expect_owner or 'pool'})",
        )
        return True

    return bool(collab._store._execute_write_txn(body, op="team_slack.task_reassign"))


def append_comment(collab: CollabStore, task_id: str, actor: str, text: str) -> None:
    """One ``comment`` task event — the /task note audit row."""

    def body(connection: sqlite3.Connection) -> str:
        return append_task_event(
            connection, task_id=task_id, actor=actor, event="comment", note=text
        )

    collab._store._execute_write_txn(body, op="team_slack.task_note")


# --------------------------------------------------------------------------
# DMs
# --------------------------------------------------------------------------


def send_dm(
    notifier: Any,
    reverse_map: Mapping[str, str],
    employee_id: str,
    text: str,
) -> bool:
    """One DM through the notifier's egress scrubber. Never raises.

    ``notifier`` is duck-typed (``post_dm``) so this module never imports
    :mod:`omniagentos.team.notify` (which imports ``slack_updates``, which
    imports this module — a top-level import here would be the cycle).
    False = not delivered (no notifier, unmapped employee, Slack failure);
    the caller words its reply accordingly rather than claiming a DM it
    cannot prove.
    """
    if notifier is None:
        return False
    slack_user_id = reverse_map.get(employee_id)
    if slack_user_id is None:
        print(f"team-tasks: no Slack mapping for {employee_id!r}; DM skipped", file=sys.stderr)
        return False
    try:
        return bool(notifier.post_dm(slack_user_id, text))
    except Exception as exc:  # noqa: BLE001 -- a DM must never sink the applied command
        print(f"team-tasks: DM to {employee_id!r} failed: {exc}", file=sys.stderr)
        return False


# --------------------------------------------------------------------------
# renders
# --------------------------------------------------------------------------

#: The five companies, in the fixed order every /task surface uses.
COMPANY_ORDER: tuple[str, ...] = ("globex", "acmeuni", "hooli", "initech", "omniagentos")

PRIORITY_GLYPHS: Mapping[str, str] = {"urgent": "🔥", "high": "⬆", "normal": "•", "low": "⬇"}


def _queue_line(card: Any, *, today: str) -> str:
    glyph = PRIORITY_GLYPHS.get(str(card.priority), "•")
    identifier = card.ref or card.id
    due = deadline_suffix(getattr(card, "due_date", None), today=today)
    return f"{glyph} {identifier} {card.title}{due}"


def render_queue(team: TeamStore, company_slug: str | None = None) -> str:
    """The shared queue, compact, grouped by company (fixed order first).

    Queue cards are all **Work** by construction (Tasks are owned from birth,
    the pool is ownerless), so this surface needs no Tasks split — but card
    deadlines render with the shared ⏰/🔴 glyphs (v4: deadlines stay
    front-and-center on every queue surface).
    """
    today = local_today()
    cards = team.pool_cards(limit=POOL_CARD_LIMIT)
    if company_slug is not None:
        mine = [card for card in cards if card.company_slug == company_slug]
        if not mine:
            return f"📋 Shared queue #{company_slug} — empty"
        lines = [f"📋 Shared queue #{company_slug} — {len(mine)}"]
        lines.extend(_queue_line(card, today=today) for card in mine)
        lines.append("📌 claim: `/task claim <REF>` · help: `/task help`")
        return "\n".join(lines)

    if not cards:
        return "📋 Shared queue — empty (the operator adds with `/task add <title> #company`)"
    grouped: dict[str, list[Any]] = {}
    for card in cards:
        grouped.setdefault(card.company_slug or "no company", []).append(card)
    ordered = [slug for slug in COMPANY_ORDER if slug in grouped]
    ordered.extend(sorted(slug for slug in grouped if slug not in COMPANY_ORDER))
    lines = [f"📋 Shared queue — {len(cards)}"]
    for slug in ordered:
        lines.append(f"*#{slug}* ({len(grouped[slug])})")
        lines.extend(_queue_line(card, today=today) for card in grouped[slug])
    lines.append("📌 claim: `/task claim <REF>` · help: `/task help`")
    return "\n".join(lines)


def help_card() -> str:
    """The one-screen usage card — the condensed twin of
    ``docs/operations/task-commands.md`` (keep the two in step)."""
    return (
        "*🧭 /task — the shared work queue*\n"
        "• `/task add <title> #company [!top|!high|!low] [deadline]` — the operator queues a card"
        " (`| ac: <criteria>` sets acceptance)\n"
        "• `/task assign @name <REF> [deadline]` — hand a queue card over (the operator/Alice)\n"
        "• `/task assign @name <title> [#company] [!priority] [deadline]` — ad-hoc task"
        " for a teammate (never yourself)\n"
        "• `/task claim <REF>` — grab queue work for yourself\n"
        "• `/task done <REF> [note]` — owner marks it done; the assigner gets a DM\n"
        "• `/task note <REF> <text>` — comment on a card; the assigner is DMed\n"
        "• `/task reassign <REF> @name` — the operator/Alice, or the card's current owner\n"
        "• `/task queue [#company]` — the shared queue · `/task mine` — your cards\n"
        "Deadlines (last phrase): `immediately` · `in 2 hours` · `today` (18:00) ·"
        " `tomorrow` (10:00) · `by friday` (10:00)\n"
        "Companies: #globex #acmeuni #hooli #initech #grok\n"
        "Work vs Tasks: queue cards are Work (points, keep 5 ongoing);"
        " `/task assign @name <title>` makes an ad-hoc Task — zero points, deadline-first"
    )


# --------------------------------------------------------------------------
# the automation backlog (the operator's GO, 2026-08-14)
#
# Categories are GOAL-LADDER CHILDREN, not a new table: each is a short_term
# goal titled "Automations — <name>" under AUTOMATION_PARENT_GOAL_ID. That is
# the whole reason this package ships zero migrations — a category is a row an
# operator can add through the existing goals API, and the resolution below
# picks it up on the next command with no deploy.
# --------------------------------------------------------------------------


class AutomationDecision(NamedTuple):
    """What one approve/reject attempt actually did.

    An OUTCOME CODE rather than a bool or an exception, because the four ways
    this can end are genuinely different answers that the Slack layer has to
    word differently — and because ``already_decided`` is a normal race, not a
    failure: two people approving the same proposal at once is a thing that
    happens in a channel, and the loser deserves "already decided", not a stack
    trace.
    """

    outcome: str  # applied | already_decided | not_found | not_a_proposal | forbidden
    task: dict[str, Any] | None = None
    assignee: str | None = None  # the employee the card was handed to, if any
    dispatched: bool = False  # the compute-pool envelope was written


#: What ``for ai`` actually writes. ``target`` is the routing key
#: :mod:`omniagentos.team.dispatch` reads; ``ready`` is the honest half.
#:
#: An approved AI card is NOT dispatchable yet and saying otherwise would be a
#: favourable false success: the dispatcher also needs an ``acceptance_cmd`` and
#: non-empty ``owned_paths`` — an executable spec that neither the proposer nor
#: the operator types in a Slack verb — and without them it skips the card on every pass,
#: forever, with nothing repairing it. ``ready=false`` says so in the envelope,
#: the reply says so in words, and the dispatcher names the gap in its skip
#: rather than passing over the card silently.
_AI_DISPATCH_ENVELOPE: dict[str, Any] = {"target": COMPUTE_POOL_TARGET, "ready": False}

#: Card statuses a proposal may still be decided from. Exactly one, and the
#: narrowness is the guard: ``approve`` must never be able to resurrect an
#: arbitrary card sitting in the review bucket for its own reasons.
_PROPOSAL_OPEN_STATUS = "awaiting_approval"


def _connection(store: Any) -> sqlite3.Connection:
    """The live connection behind a CollabStore, a TeamStore, or a bare store."""
    inner = getattr(store, "_store", store)
    return cast("sqlite3.Connection", inner._connection)


def _normalized_category(text: str) -> str:
    """``Dev-Tooling`` / ``dev tooling`` / ``DEV_TOOLING`` -> ``dev tooling``."""
    return " ".join(str(text).replace("-", " ").replace("_", " ").casefold().split())


def automation_categories(store: Any) -> list[dict[str, Any]]:
    """Every automation category goal, oldest first. Never raises.

    Reads the CHILDREN of :data:`AUTOMATION_PARENT_GOAL_ID` whose title carries
    the category prefix. A database without the parent goal (every test fixture
    that has not seeded it) returns ``[]`` rather than failing — the callers
    treat "no categories" and "no match" identically, which is what keeps the
    command usable on the day before the goals are created.
    """
    try:
        rows = _connection(store).execute(
            "SELECT id, title FROM company_goals WHERE parent_goal_id = ? "
            "AND title LIKE ? ORDER BY created_at ASC, id ASC",
            (AUTOMATION_PARENT_GOAL_ID, f"{AUTOMATION_CATEGORY_PREFIX}%"),
        ).fetchall()
    except sqlite3.Error as exc:  # pragma: no cover -- schema drift, not a user path
        print(f"team-tasks: automation category lookup failed: {exc}", file=sys.stderr)
        return []
    return [
        {
            "id": str(row["id"]),
            "title": str(row["title"]),
            "name": str(row["title"])[len(AUTOMATION_CATEGORY_PREFIX) :].strip(),
        }
        for row in rows
    ]


class CategoryMatch(NamedTuple):
    """What a ``#token`` resolved to, and — when it did not — WHY.

    ``ambiguous`` carries the candidate names so the caller can print them.
    Silently taking the oldest of several prefix matches was the old behaviour
    and it is the wrong answer: with "customer service" and "customer success"
    both on the ladder, ``#customer`` filed work under whichever goal happened
    to be created first, and nothing told the person who typed it. A category is
    where work goes to be FOUND later; guessing is worse than asking.
    """

    goal_id: str | None = None
    name: str | None = None
    ambiguous: tuple[str, ...] = ()


def match_automation_category(store: Any, token: str | None) -> CategoryMatch:
    """``#ads`` -> the "Automations — ads" category, or the reason it did not.

    Matching is forgiving in ONE direction only. After normalisation (case,
    ``-``/``_`` -> space) a token matches when it is the whole name, a whole
    WORD of it (``#comms`` -> "email & comms"), or a word-bounded leading prefix
    (``#email``, ``#finance``). It never matches a mid-word fragment.

    Precedence is exact > word > prefix, and it is not cosmetic: an exact name
    must never lose to a longer category that merely starts with it. Anything
    that leaves more than one candidate in the winning tier is AMBIGUOUS —
    reported, not guessed.
    """
    wanted = _normalized_category(token or "")
    if not wanted:
        return CategoryMatch()
    exact: list[dict[str, Any]] = []
    words: list[dict[str, Any]] = []
    prefixes: list[dict[str, Any]] = []
    for category in automation_categories(store):
        name = _normalized_category(category["name"])
        if name == wanted:
            exact.append(category)
        elif wanted in name.split():
            words.append(category)
        elif name.startswith(f"{wanted} "):
            prefixes.append(category)
    for tier in (exact, words, prefixes):
        if len(tier) == 1:
            return CategoryMatch(goal_id=str(tier[0]["id"]), name=str(tier[0]["name"]))
        if len(tier) > 1:
            return CategoryMatch(ambiguous=tuple(str(entry["name"]) for entry in tier))
    return CategoryMatch()


def resolve_automation_category(store: Any, token: str | None) -> str | None:
    """``#ads`` -> the "Automations — ads" goal id, or ``None``.

    Matching is deliberately forgiving in ONE direction only. A token matches a
    category when, after normalisation (case, ``-``/``_`` -> space), it is the
    whole name, a leading WORD-BOUNDED prefix of it (``#email`` ->
    "email & comms", ``#finance`` -> "finance & ops"), or one of its words
    (``#comms``). It never matches a mid-word fragment, because a category is
    where somebody's work goes to be found later and a fuzzy match files it
    somewhere nobody will look.

    Ambiguity does NOT resolve to the oldest goal — see
    :func:`match_automation_category`, which this is the id-only spelling of.
    ``None`` (no token, no parent goal, no match, or an ambiguous one) is a
    first-class answer; callers that need to TELL the user which categories a
    token could have meant use the richer function.
    """
    match = match_automation_category(store, token)
    return match.goal_id


def automation_goal_for(store: Any, token: str | None) -> str | None:
    """The goal an automation proposal lands on: its category, else the PARENT.

    A pool card with no goal is not pool-eligible (``TeamStore.pool_cards``
    requires one), so falling back to the parent is what keeps an uncategorised
    proposal claimable once approved — and keeps the mis-filing visible: it sits
    at the top of the ladder until somebody files it under a category.

    ``None`` means the PARENT GOAL IS MISSING from this database, which is a
    configuration fault rather than a routing outcome. Callers must not persist
    a card with it (see :func:`propose_automation`).
    """
    resolved = resolve_automation_category(store, token)
    if resolved is not None:
        return resolved
    row = _connection(store).execute(
        "SELECT id FROM company_goals WHERE id = ?", (AUTOMATION_PARENT_GOAL_ID,)
    ).fetchone()
    return None if row is None else str(row["id"])


def _org_envelope(task: Mapping[str, Any]) -> dict[str, Any]:
    """A card's ``org_json`` as a dict, from either row shape.

    ``CollabStore.get_board_task`` parses the column into ``org``; a raw SELECT
    hands back ``org_json`` as text. Both are read here so a caller never has to
    know which read it holds.
    """
    parsed = task.get("org")
    if isinstance(parsed, dict):
        return dict(parsed)
    raw = task.get("org_json")
    if isinstance(raw, str) and raw.strip():
        try:
            loaded = json.loads(raw)
        except (TypeError, json.JSONDecodeError):
            return {}
        return dict(loaded) if isinstance(loaded, dict) else {}
    return {}


def _merged_org_json(task: Mapping[str, Any], addition: Mapping[str, Any]) -> str:
    """The card's org envelope with ``addition`` merged in, as JSON text.

    MERGED, never replaced: ``org_json`` is a shared envelope — the orgdims
    classifier writes company/product/workstream keys into it, and the swarm
    reads its own. A proposal that clobbered it would silently delete another
    subsystem's classification, and nothing would report the loss.
    """
    envelope = _org_envelope(task)
    for key, value in addition.items():
        existing = envelope.get(key)
        if isinstance(existing, dict) and isinstance(value, dict):
            merged = dict(existing)
            merged.update(value)
            envelope[key] = merged
        else:
            envelope[key] = value
    return json.dumps(envelope, separators=(",", ":"), sort_keys=True)


def proposal_hint(task: Mapping[str, Any]) -> str | None:
    """The ``for X`` hint stored on a proposal at creation, or ``None``."""
    proposal = _org_envelope(task).get("proposal")
    if not isinstance(proposal, dict):
        return None
    hint = proposal.get("assignee_hint")
    return None if hint is None else str(hint)


def proposer_of(task: Mapping[str, Any]) -> str | None:
    """Who proposed this card — the person a decision DM goes back to."""
    proposal = _org_envelope(task).get("proposal")
    if not isinstance(proposal, dict):
        return None
    proposed_by = proposal.get("proposed_by")
    return None if proposed_by is None else str(proposed_by)


def propose_automation(
    collab: CollabStore,
    *,
    title: str,
    proposed_by: str,
    category: str | None = None,
    assignee_hint: str | None = None,
    acceptance_criteria: str = "",
    description: str = "",
) -> BoardTask:
    """Create ONE automation proposal, awaiting the operator's approval.

    Open to any ACTIVE roster member — deliberately wider than ``/task add``,
    which is the operator-only because adding to the shared queue IS approval. A proposal
    approves nothing: it lands in ``awaiting_approval`` where it cannot be
    claimed, cannot be dispatched and counts toward nobody's queue until the operator
    decides. That is what makes it safe to let everyone file one.

    The ``for X`` hint is STORED, not applied (``org_json.proposal``): the
    proposer's opinion about who should do the work is worth keeping, and it is
    not the proposer's decision. Approval reads it back.

    Acceptance criteria default to the TITLE (never empty) for the same reason
    every other creation path does: the store's evidence-before-done gate binds
    on owner + acceptance criteria, and a proposal with neither could later
    reach done with nothing behind it.
    """
    if proposed_by not in active_employee_ids(collab._store):
        raise ValueError(f"{proposed_by} is not on the active roster")
    if assignee_hint is not None and assignee_hint not in PROPOSAL_ASSIGNEE_HINTS:
        raise ValueError(f"assignee hint must be one of {sorted(PROPOSAL_ASSIGNEE_HINTS)}")
    clean_title = " ".join(str(title).split())
    if not clean_title:
        raise ValueError("a proposal needs a title")
    match = match_automation_category(collab, category)
    if match.ambiguous:
        raise ValueError(
            f"#{category} matches {len(match.ambiguous)} categories "
            f"({', '.join(match.ambiguous)}) — name one exactly"
        )
    goal_id = match.goal_id or automation_goal_for(collab, None)
    if goal_id is None:
        # A goal-less card is not pool-eligible, so approving one would mint
        # work nobody can claim — with a success receipt on the way in and no
        # backfill when the goal is created later. Refuse LOUDLY at creation:
        # this is a configuration fault (the parent goal has not been created on
        # this database), not something the proposer did wrong.
        raise ValueError(
            f"automation_backlog_unconfigured: the parent goal "
            f"{AUTOMATION_PARENT_GOAL_ID} does not exist on this database — "
            "create it (and the 'Automations — <name>' category goals) first"
        )
    task = BoardTask(
        title=clean_title,
        description=description or f"Proposed by {proposed_by} via /task propose.",
        size="M",
        status=BoardTaskStatus.AWAITING_APPROVAL,
        goal_id=goal_id,
        acceptance_criteria=" ".join(str(acceptance_criteria).split()) or clean_title,
        source=AUTOMATION_PROPOSAL_SOURCE,
    )
    collab.create_board_task(task, actor=proposed_by)
    # The envelope is a SECOND write because ``BoardTask``/``create_board_task``
    # do not carry ``org_json`` — the orgdims classifier patches it after
    # creation too, and this follows that established path rather than forking
    # a private INSERT that would skip the create event and the board rules.
    # Residual, accepted and bounded: a crash between the two leaves a proposal
    # whose structured proposer is missing. It is not LOST — the description
    # above names the proposer in prose, and the card sits in
    # ``awaiting_approval`` where it cannot be claimed or dispatched, so the
    # worst case is a decision DM that goes nowhere.
    collab.update_board_task(
        task.id,
        {
            "org_json": _merged_org_json(
                {},
                {"proposal": {"proposed_by": proposed_by, "assignee_hint": assignee_hint}},
            )
        },
        actor=proposed_by,
    )
    return task


def _fresh_row(connection: sqlite3.Connection, task_id: str) -> dict[str, Any] | None:
    """One card, re-read INSIDE the caller's transaction."""
    row = connection.execute("SELECT * FROM board_tasks WHERE id = ?", (task_id,)).fetchone()
    return None if row is None else dict(row)


def _decidable_inside_txn(
    connection: sqlite3.Connection, task_id: str
) -> tuple[dict[str, Any] | None, str]:
    """``(task, outcome)`` for a decision verb — evaluated under the write lock.

    Refuses anything that is not a LIVE ``awaiting_approval`` card stamped
    :data:`AUTOMATION_PROPOSAL_SOURCE`. The review bucket holds cards that got
    there for entirely different reasons (a swarm awaiting a human call, a
    promoted plan) and these verbs must not be able to resurrect one of those
    into the open pool — which is exactly what a status-only check would allow.

    INSIDE the transaction, not before it: a guard that reads the row outside
    ``BEGIN IMMEDIATE`` validates a snapshot, and between that read and the
    UPDATE another writer can change the very fields it validated. The row this
    returns is the row the UPDATE will act on, and the reply that quotes it is
    quoting current state rather than a stale one.
    """
    task = _fresh_row(connection, task_id)
    if task is None:
        return None, "not_found"
    if str(task.get("source") or "") != AUTOMATION_PROPOSAL_SOURCE:
        return task, "not_a_proposal"
    if task.get("archived_at") is not None:
        return task, "not_a_proposal"
    if str(task.get("status") or "") != _PROPOSAL_OPEN_STATUS:
        return task, "already_decided"
    return task, "applied"


def approve_automation(
    collab: CollabStore,
    ref: str | None,
    *,
    actor: str,
    assignee_hint: str | None = None,
) -> AutomationDecision:
    """the operator approves one proposal into the queue. ONE transaction, CAS-guarded.

    Three landings, decided by the hint (the verb's, else the one stored at
    proposal time):

    * a PERSON (``owner``/``alice``/``bob``) — an owned open card;
    * ``ai`` — an OWNERLESS open card carrying
      ``org_json.dispatch.target = 'compute-pool'``, the exact envelope
      :mod:`omniagentos.team.dispatch` reads. Ownerless is not an oversight:
      the dispatcher only ever looks at pool cards, so an owner would hide the
      card from the very daemon the hint asks for;
    * no hint — a plain pool card anyone may claim.

    The status move is a compare-and-set on ``awaiting_approval``. Two people
    approving the same proposal in a channel is an ordinary race, and the loser
    gets ``already_decided`` rather than a second, contradictory decision.
    """
    if not can_add(actor):
        return AutomationDecision(outcome="forbidden")
    if assignee_hint is not None and assignee_hint not in PROPOSAL_ASSIGNEE_HINTS:
        raise ValueError(f"assignee hint must be one of {sorted(PROPOSAL_ASSIGNEE_HINTS)}")
    located = find_task_by_ref(collab, ref)
    if located is None:
        return AutomationDecision(outcome="not_found")
    task_id = str(located["id"])
    now = utc_now_iso()

    def body(connection: sqlite3.Connection) -> AutomationDecision:
        task, outcome = _decidable_inside_txn(connection, task_id)
        if outcome != "applied":
            return AutomationDecision(outcome=outcome, task=task)
        assert task is not None
        hint = assignee_hint or proposal_hint(task)
        owner = f"emp_{hint}" if hint in {"owner", "alice", "bob"} else None
        dispatched = hint == "ai"
        sets = ["status = ?", "updated_at = ?"]
        parameters: list[Any] = [BoardTaskStatus.OPEN.value, now]
        if owner is not None:
            sets.append("owner_employee_id = ?")
            parameters.append(owner)
        if dispatched:
            sets.append("org_json = ?")
            # Merged against the row re-read HERE, so a classification that
            # landed since the proposal is preserved rather than rolled back.
            parameters.append(_merged_org_json(task, {"dispatch": _AI_DISPATCH_ENVELOPE}))
        parameters.append(task_id)
        # ``source`` rides the predicate with ``status``: the guard above and
        # this UPDATE must agree about the SAME row, and a concurrent write
        # that changed either one must lose rather than be acted on.
        cursor = connection.execute(
            f"UPDATE board_tasks SET {', '.join(sets)} WHERE id = ? AND status = ? "
            "AND source = ?",
            (*parameters, _PROPOSAL_OPEN_STATUS, AUTOMATION_PROPOSAL_SOURCE),
        )
        if cursor.rowcount <= 0:
            # The FRESH row, never the pre-race snapshot: a receipt that quotes
            # a status the board no longer holds is worse than no receipt.
            return AutomationDecision(
                outcome="already_decided", task=_fresh_row(connection, task_id)
            )
        append_task_event(
            connection,
            task_id=task_id,
            actor=actor,
            event="status_change",
            from_status=_PROPOSAL_OPEN_STATUS,
            to_status=BoardTaskStatus.OPEN.value,
            note="automation proposal approved",
        )
        if owner is not None:
            append_task_event(
                connection,
                task_id=task_id,
                actor=actor,
                event="assign",
                note=f"approved to {owner}",
            )
        return AutomationDecision(
            outcome="applied", task=task, assignee=owner, dispatched=dispatched
        )

    return cast(
        AutomationDecision,
        collab._store._execute_write_txn(body, op="team_tasks.approve_automation"),
    )


def reject_automation(
    collab: CollabStore, ref: str | None, *, actor: str, reason: str = ""
) -> AutomationDecision:
    """the operator declines one proposal. Same guards, same CAS, ``cancelled``.

    The reason rides a ``comment`` event rather than overwriting anything: a
    proposal that was turned down is a decision somebody may need to read back,
    and "why" is the only part of it that is not already obvious from the
    status.
    """
    if not can_add(actor):
        return AutomationDecision(outcome="forbidden")
    located = find_task_by_ref(collab, ref)
    if located is None:
        return AutomationDecision(outcome="not_found")
    task_id = str(located["id"])
    note = " ".join(str(reason).split())

    def body(connection: sqlite3.Connection) -> AutomationDecision:
        task, outcome = _decidable_inside_txn(connection, task_id)
        if outcome != "applied":
            return AutomationDecision(outcome=outcome, task=task)
        assert task is not None
        cursor = connection.execute(
            "UPDATE board_tasks SET status = ?, updated_at = ? WHERE id = ? AND status = ? "
            "AND source = ?",
            (
                BoardTaskStatus.CANCELLED.value,
                utc_now_iso(),
                task_id,
                _PROPOSAL_OPEN_STATUS,
                AUTOMATION_PROPOSAL_SOURCE,
            ),
        )
        if cursor.rowcount <= 0:
            return AutomationDecision(
                outcome="already_decided", task=_fresh_row(connection, task_id)
            )
        append_task_event(
            connection,
            task_id=task_id,
            actor=actor,
            event="status_change",
            from_status=_PROPOSAL_OPEN_STATUS,
            to_status=BoardTaskStatus.CANCELLED.value,
            note="automation proposal rejected",
        )
        if note:
            append_task_event(
                connection, task_id=task_id, actor=actor, event="comment", note=note
            )
        return AutomationDecision(outcome="applied", task=task)

    return cast(
        AutomationDecision,
        collab._store._execute_write_txn(body, op="team_tasks.reject_automation"),
    )
