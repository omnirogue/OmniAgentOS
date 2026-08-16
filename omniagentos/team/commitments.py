"""Daily commitments: what each dev said they would finish, and what happened.

Deterministic end to end — there is no LLM anywhere in this module, by design
(spec §3). A commitment is generated FROM THE QUEUE (the cards a person already
holds), and resolved FROM THE BOARD (the events the board already recorded).
Nothing here judges significance by reading prose; the significance proxy is
evidence, size and verification.

DAYS ARE LOCAL DATES
--------------------
``day`` is always a ``YYYY-MM-DD`` date on the HOST's system timezone — the
wall clock a person reads in the 06:55 DM, the same clock ``notify``'s daybrief
keys on. Every stored timestamp stays UTC. Comparisons therefore convert: a
local day is the HALF-OPEN UTC interval ``[local 00:00, next local 00:00)``
(:func:`local_day_bounds`), so a card finished at 23:30 local — 06:30Z the next
morning — counts for the day the person actually worked it, and no instant
belongs to two days.

THE DAILY SLOTS
---------------
Every active dev gets, every day: one commitment per active/due card (capped at
:data:`~omniagentos.team.contracts.COMMITMENT_TASK_CAP`), ONE improvement slot,
and :data:`~omniagentos.team.contracts.AUTOMATION_SLOTS_PER_DAY` automation
slots (the operator's ruling 2026-08-14 — three new automations or skills a day). Card
commitments CARRY when missed; slots do not, because the slot is a daily
expectation that renews rather than a piece of work that survives the night.

ORCHESTRATION ORDER (review S2)
-------------------------------
There is ONE order, and :func:`run_daily` is it: ``resolve_day(yesterday)``
first, then ``generate_for_day(today)``. Resolving first means the carry a miss
mints is already in place when the generator runs, and the generator's
``INSERT OR IGNORE`` then finds it instead of racing it. The 07:00 report may
call ``resolve_day(yesterday)`` again — every already-resolved row is a strict
no-op — and then only reads.

HISTORY IS IMMUTABLE (review S10)
---------------------------------
A resolved row (``delivered``/``missed``) is never rewritten. Misses are the
accountability record; the only edit the API admits on a resolved row is an
APPENDED operator note. ``carried`` rows are minted here and nowhere else — and
``carried`` is an OPEN state, not a terminal one: it records provenance ("this
slipped from yesterday"), and :func:`resolve_day` judges it on its own day like
any other commitment, carrying it again if it slips again.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from datetime import UTC, date, datetime, timedelta
from typing import Any

from omniagentos.collab.contracts import BoardTaskStatus
from omniagentos.contracts import utc_now_iso
from omniagentos.team.contracts import (
    AUTOMATION_SLOTS_PER_DAY,
    COMMITMENT_TASK_CAP,
    IMPROVEMENT_COMPANY_SLUG,
    OPEN_COMMITMENT_STATUSES,
    OPERATOR_EMPLOYEE_ID,
)
from omniagentos.team.store import TeamStore, completion_state

__all__ = [
    "IMPROVEMENT_EXPECTED_OUTCOME",
    "IMPROVEMENT_TITLE",
    "generate_for_day",
    "local_day",
    "local_day_bounds",
    "resolve_day",
    "run_daily",
]

#: The standing slot every dev carries every day (spec's governing principle).
IMPROVEMENT_TITLE = "One significant OmniAgentOS improvement"

#: The three daily automation slots (the operator's ruling 2026-08-14, answering Q1).
#: Titled with their slot number so a person reading the morning DM can see at
#: a glance which of the three is still open.
AUTOMATION_TITLE_TEMPLATE = "New automation or skill ({slot}/{total})"
AUTOMATION_EXPECTED_OUTCOME = (
    "an automation or reusable skill shipped as a done card with pass-gated evidence "
    "and automation_maturity set (assisted or above), or a graduated skill"
)

#: The maturity values that mean the SYSTEM took work off a person. 'human' is
#: excluded on purpose: a card done entirely by hand is work, and good work, but
#: it is not the thing this slot exists to count.
_AUTOMATING_MATURITY: tuple[str, ...] = (
    "assisted",
    "partially_automated",
    "autonomous",
    "autonomous_verified",
)
IMPROVEMENT_EXPECTED_OUTCOME = (
    "An evidence-backed card on the OmniAgentOS company goal reaches done today "
    "(size M/L, or verified)"
)

#: Evidence that shows WORK, not just a comment. A 'note' row is something a
#: person typed; it must not be able to satisfy the improvement slot on its own
#: (review S7 round 3).
_SUBSTANTIVE_EVIDENCE_KINDS: tuple[str, ...] = ("commit", "pr", "test_run", "deploy", "doc")

#: Sizes that count as significant without a verification behind them (S8).
_SIGNIFICANT_SIZES: tuple[str, ...] = ("M", "L")

_PRIORITY_RANK = {"urgent": 0, "high": 1, "normal": 2, "low": 3}

_TERMINAL_FOR_CARRY = frozenset({BoardTaskStatus.CANCELLED.value})

#: Strips a previous day's carry prefix so a repeatedly-slipping commitment
#: re-states "carried from <yesterday>" instead of nesting one per day.
_CARRY_PREFIX_RE = re.compile(r"^carried from \d{4}-\d{2}-\d{2}: ")

#: The card a delivered automation slot claimed, written into its resolution
#: note as a parseable token. A structured id (not just the human-readable ref)
#: because the retry path has to EXCLUDE that card from the next snapshot, and
#: refs are optional while ids are not.
_CARD_TOKEN_RE = re.compile(r"card=(?P<card_id>\S+)")


# --------------------------------------------------------------------------
# local-day arithmetic
# --------------------------------------------------------------------------


def _parsed(timestamp: str) -> datetime:
    """One stored ``...Z`` timestamp as an aware UTC datetime."""
    text = str(timestamp).strip().replace("Z", "+00:00")
    moment = datetime.fromisoformat(text)
    return moment.replace(tzinfo=UTC) if moment.tzinfo is None else moment


def local_day(timestamp: str) -> str:
    """The LOCAL ``YYYY-MM-DD`` a stored UTC timestamp falls on."""
    return _parsed(timestamp).astimezone().date().isoformat()


def local_day_bounds(day: str) -> tuple[str, str]:
    """``day`` as the half-open UTC interval ``[start, end)``, in stored format.

    Half-open on purpose: an inclusive end would put midnight in two days at
    once, and the one number that must never be double-counted here is a
    delivery.
    """
    start_local = datetime.combine(date.fromisoformat(day), datetime.min.time()).astimezone()
    end_local = start_local + timedelta(days=1)
    fmt = "%Y-%m-%dT%H:%M:%SZ"
    return (
        start_local.astimezone(UTC).strftime(fmt),
        end_local.astimezone(UTC).strftime(fmt),
    )


def _next_day(day: str) -> str:
    return (date.fromisoformat(day) + timedelta(days=1)).isoformat()


def _previous_day(day: str) -> str:
    return (date.fromisoformat(day) - timedelta(days=1)).isoformat()


def local_today() -> str:
    """Today on the host's wall clock — the day a generated commitment is for."""
    return datetime.now().astimezone().date().isoformat()


# --------------------------------------------------------------------------
# generation (06:55)
# --------------------------------------------------------------------------


def active_devs(store: TeamStore) -> list[str]:
    """Active employees who carry commitments: the roster minus the operator.

    The operator is excluded for the same reason they are exempt from the
    verification counter-signature: they set the queue, they do not answer a
    morning commitment check to themselves.
    """
    from omniagentos.company_goals.store import CompanyGoalsStore

    return [
        str(row["id"])
        for row in CompanyGoalsStore(store._store).list_employees(status="active")
        if str(row["id"]) != OPERATOR_EMPLOYEE_ID
    ]


def _committed_cards(store: TeamStore, employee_id: str, day: str) -> list[Any]:
    """The cards this person's day is made of, priority-ordered, capped.

    Two sources, in this order: what they are ALREADY working (the Active
    bucket — claimed/in_progress), then anything assigned and open that is DUE
    today. Capped at :data:`COMMITMENT_TASK_CAP`, because a list longer than a
    day is a backlog with a date on it, not a commitment.
    """
    buckets = store.team_queues(employee_ids=[employee_id])
    bucket = buckets.get(employee_id)
    if bucket is None:  # pragma: no cover -- the id came from the roster
        return []
    due_today = [card for card in bucket.ready if str(card.due_date or "")[:10] == day]
    candidates = [*bucket.active, *due_today]
    # Stable sort: the buckets already carry the store's priority order, so this
    # only interleaves the two lists — it never re-orders within one.
    candidates.sort(key=lambda card: _PRIORITY_RANK.get(str(card.priority), 4))
    return candidates[:COMMITMENT_TASK_CAP]


def generate_for_day(
    store: TeamStore, day: str, *, employee_ids: list[str] | None = None
) -> list[dict[str, Any]]:
    """Ensure every active dev's commitments for ``day`` exist. Idempotent.

    Per active dev: up to :data:`COMMITMENT_TASK_CAP` card commitments, ONE
    improvement slot, and :data:`AUTOMATION_SLOTS_PER_DAY` automation slots —
    at most eight rows, all deterministic.

    Returns the rows this call ENSURED (created or already present), so a caller
    rendering the morning DM never has to re-read. Re-running is safe by
    construction: the unique keys make a second run collide, and a collision
    returns the existing row rather than a second commitment.
    """
    ensured: list[dict[str, Any]] = []
    for employee_id in employee_ids or active_devs(store):
        for card in _committed_cards(store, employee_id, day):
            row, _outcome = store.create_commitment(
                day=day,
                employee_id=employee_id,
                kind="task",
                task_id=card.id,
                title=card.title,
                expected_outcome=(
                    f"{card.ref or card.id} reaches done with evidence by end of {day}"
                ),
                source="auto",
            )
            ensured.append(row)
        row, _outcome = store.create_commitment(
            day=day,
            employee_id=employee_id,
            kind="improvement",
            title=IMPROVEMENT_TITLE,
            expected_outcome=IMPROVEMENT_EXPECTED_OUTCOME,
            source="auto",
        )
        ensured.append(row)
        for slot in range(1, AUTOMATION_SLOTS_PER_DAY + 1):
            row, _outcome = store.create_commitment(
                day=day,
                employee_id=employee_id,
                kind="automation",
                slot=slot,
                title=AUTOMATION_TITLE_TEMPLATE.format(slot=slot, total=AUTOMATION_SLOTS_PER_DAY),
                expected_outcome=AUTOMATION_EXPECTED_OUTCOME,
                source="auto",
            )
            ensured.append(row)
    return ensured


# --------------------------------------------------------------------------
# resolution (the next morning)
# --------------------------------------------------------------------------


def _card(store: TeamStore, task_id: str) -> dict[str, Any] | None:
    row = store._store._connection.execute(
        "SELECT * FROM board_tasks WHERE id = ?", (task_id,)
    ).fetchone()
    return None if row is None else dict(row)


def _reached_done_by(store: TeamStore, task_id: str, day: str) -> str | None:
    """The first ``status_change -> done`` event on/before ``day``, LOCAL.

    The board's own account of when the work finished, rather than the card's
    mutable ``updated_at`` (which any later edit moves). Returns the event's
    stored UTC timestamp, or None.
    """
    _start, end = local_day_bounds(day)
    row = store._store._connection.execute(
        "SELECT created_at FROM task_events WHERE task_id = ? AND event = 'status_change' "
        "AND to_status = ? AND created_at < ? ORDER BY created_at ASC, rowid ASC LIMIT 1",
        (task_id, BoardTaskStatus.DONE.value, end),
    ).fetchone()
    return None if row is None else str(row["created_at"])


def _done_within_day(store: TeamStore, task_id: str, day: str) -> tuple[str, int] | None:
    """The first ``status_change -> done`` event INSIDE the local ``day``.

    Different question from :func:`_reached_done_by`, and the difference is a
    real defect it exists to fix. That one answers "did this card ever reach
    done by the end of ``day``?" and returns the EARLIEST such event — right for
    a card commitment, wrong for the slot qualifiers, which then asked whether
    that earliest event fell inside the day. A card finished last week, reopened
    and re-completed this morning fails that test: its first done event is last
    week's, so today's completion is invisible and the work does not count for
    the day it was actually done. Windowing the SELECT instead of filtering its
    result answers the question the qualifiers are actually asking.

    Ordered by ``(created_at, rowid)`` — the documented tiebreaker for this
    append-only table, since ``created_at`` is second-resolution.
    """
    start, end = local_day_bounds(day)
    row = store._store._connection.execute(
        "SELECT created_at, rowid FROM task_events WHERE task_id = ? AND event = 'status_change' "
        "AND to_status = ? AND created_at >= ? AND created_at < ? "
        "ORDER BY created_at ASC, rowid ASC LIMIT 1",
        (task_id, BoardTaskStatus.DONE.value, start, end),
    ).fetchone()
    return None if row is None else (str(row["created_at"]), int(row["rowid"]))


def _qualifying_evidence_kinds(store: TeamStore, task_id: str) -> set[str]:
    """Evidence kinds on this card that a machine graded PASS.

    ``quality_gate`` filtering is the point: a rejected review, a reverted
    commit or a card that took excessive attempts is evidence that the work
    HAPPENED, not that it landed — counting it toward the improvement slot is
    exactly how an output number learns to lie.
    """
    return {
        str(row["kind"])
        for row in store._store._connection.execute(
            "SELECT DISTINCT kind FROM task_evidence WHERE task_id = ? AND quality_gate = 'pass'",
            (task_id,),
        )
    }


def _resolve_task_commitment(
    store: TeamStore, row: dict[str, Any], day: str, *, resolved_at: str
) -> str:
    """Resolve one 'task' commitment; returns the status written."""
    task_id = row["task_id"]
    if task_id is None:
        store.resolve_commitment(
            str(row["id"]),
            status="missed",
            resolution_note="the card this commitment named no longer exists",
            resolved_at=resolved_at,
        )
        return "missed"
    card = _card(store, str(task_id))
    reference = str((card or {}).get("ref") or task_id)
    state = None if card is None else completion_state(card)
    done_at = _reached_done_by(store, str(task_id), day)

    if done_at is not None and state != "failed_verification":
        store.resolve_commitment(
            str(row["id"]),
            status="delivered",
            resolution_note=f"{reference} reached done {local_day(done_at)} ({state or 'reopened'})",
            resolved_at=resolved_at,
        )
        return "delivered"

    if state == "failed_verification":
        reason = str((card or {}).get("verification_failed_reason") or "")
        note = f"{reference} done but verification failed: {reason}".strip()
    elif done_at is None:
        note = f"{reference} did not reach done by end of {day}"
    else:  # pragma: no cover -- defensive: both branches above are exhaustive
        note = f"{reference} unresolved"

    carry = _carry_payload(row, card, day)
    store.resolve_commitment(
        str(row["id"]),
        status="missed",
        resolution_note=note,
        resolved_at=resolved_at,
        carry=carry,
    )
    return "missed"


def _carry_payload(
    row: dict[str, Any], card: dict[str, Any] | None, day: str
) -> dict[str, Any] | None:
    """The next-day follow-up for a miss, or None when carrying makes no sense.

    A cancelled or archived card is work the team decided NOT to do; carrying it
    would re-commit somebody to it every morning forever.

    Works for a SECOND-generation carry too (a carried row that missed again):
    the ``carried from`` prefix is re-stated for the new day rather than nested,
    so a commitment that slips a week does not accumulate a sentence per day.
    The full chain stays readable through ``carried_from``, which is the field
    that actually models it.
    """
    if card is None:
        return None
    if str(card.get("status") or "") in _TERMINAL_FOR_CARRY or card.get("archived_at") is not None:
        return None
    return {
        "day": _next_day(day),
        "employee_id": str(row["employee_id"]),
        "task_id": str(row["task_id"]),
        "title": str(row["title"]),
        "expected_outcome": f"carried from {day}: {_CARRY_PREFIX_RE.sub('', str(row['expected_outcome']))}",
    }


def _qualifying_improvement(
    store: TeamStore, employee_id: str, day: str
) -> tuple[dict[str, Any], str] | None:
    """The card that satisfies this person's improvement slot for ``day``.

    Four conditions, all deterministic (S8 + round-3 §7):

    * owned by this person, on the OmniAgentOS company goal;
    * it reached done during ``day`` (local, half-open — a card reopened and
      re-completed today counts for today);
    * it carries at least one SUBSTANTIVE evidence row at ``quality_gate='pass'``
      — a bare ``note`` is a sentence somebody typed, and a reverted commit is
      work that did not land;
    * it is not in ``failed_verification``, and it is either M/L or verified —
      a cosmetic S-size card nobody checked does not make the system more
      capable.

    Returns ``(card, basis)`` or None.
    """
    rows = store._store._connection.execute(
        "SELECT b.* FROM board_tasks b "
        "JOIN company_goals cg ON cg.id = b.goal_id "
        "JOIN org_companies oc ON oc.id = cg.org_company_id "
        "WHERE b.owner_employee_id = ? AND oc.slug = ? AND b.status = ? "
        "AND b.archived_at IS NULL ORDER BY b.created_at ASC, b.id ASC",
        (employee_id, IMPROVEMENT_COMPANY_SLUG, BoardTaskStatus.DONE.value),
    ).fetchall()
    for raw in rows:
        card = dict(raw)
        if _done_within_day(store, str(card["id"]), day) is None:
            continue
        if completion_state(card) == "failed_verification":
            continue
        if not _qualifying_evidence_kinds(store, str(card["id"])) & set(
            _SUBSTANTIVE_EVIDENCE_KINDS
        ):
            continue
        verified = card.get("verified_at") is not None
        size = str(card.get("size") or "")
        if not (verified or size in _SIGNIFICANT_SIZES):
            continue
        return card, ("verified" if verified else f"size {size}")
    return None


def _resolve_improvement_commitment(
    store: TeamStore, row: dict[str, Any], day: str, *, resolved_at: str
) -> str:
    match = _qualifying_improvement(store, str(row["employee_id"]), day)
    if match is None:
        store.resolve_commitment(
            str(row["id"]),
            status="missed",
            resolution_note=(
                f"no evidence-backed OmniAgentOS card reached done on {day} "
                "(M/L or verified, substantive evidence)"
            ),
            resolved_at=resolved_at,
        )
        return "missed"
    card, basis = match
    reference = str(card.get("ref") or card["id"])
    store.resolve_commitment(
        str(row["id"]),
        status="delivered",
        resolution_note=f"{reference} {card['title']} ({basis})",
        resolved_at=resolved_at,
    )
    return "delivered"


def _qualifying_automations(store: TeamStore, employee_id: str, day: str) -> list[dict[str, Any]]:
    """Every card that counts as an AUTOMATION this person shipped on ``day``.

    the operator's ruling (2026-08-14) makes this the daily bar: three new automations
    or skills. A card qualifies when all three hold:

    * it is owned by this person and reached done during ``day`` (local,
      half-open) — the same window every other slot uses, and one that counts a
      card reopened and re-completed today for today;
    * it carries at least one SUBSTANTIVE evidence row at ``quality_gate='pass'``
      — a bare ``note`` is a sentence somebody typed, and a reverted commit is
      work that did not land;
    * its ``automation_maturity`` is ``assisted`` or above. This is the
      discriminating condition: 'human' and NULL do not qualify, because the
      slot counts work the SYSTEM took over, not work that got done. An
      unset field reads as not-an-automation rather than as a favourable
      default — the person who automated something says so on the card.

    Deliberately NOT scoped to the OmniAgentOS goal (unlike the improvement
    slot): automating a Globex support flow is exactly the thing being
    asked for, and it does not live in this repo's goal.

    Ordered by WHEN THE CARD REACHED DONE (then created_at, then id), so slot 1
    is the day's first automation and slot 3 its third — the order a person
    would tell you they shipped them in. Ordering by ``created_at`` alone looks
    equivalent and is not: cards created in the same second tie, and the
    tiebreak falls through to a uuid, which assigns slots in an order that
    matches nothing anybody did. The full key is total, so the list — and
    therefore the slot each card fills — is stable across runs.
    """
    rows = store._store._connection.execute(
        "SELECT * FROM board_tasks WHERE owner_employee_id = ? AND status = ? "
        "AND archived_at IS NULL AND automation_maturity IS NOT NULL "
        "ORDER BY created_at ASC, id ASC",
        (employee_id, BoardTaskStatus.DONE.value),
    ).fetchall()
    qualifying: list[tuple[tuple[str, int], str, dict[str, Any]]] = []
    for raw in rows:
        card = dict(raw)
        if str(card.get("automation_maturity") or "") not in _AUTOMATING_MATURITY:
            continue
        done_at = _done_within_day(store, str(card["id"]), day)
        if done_at is None:
            continue
        if completion_state(card) == "failed_verification":
            continue
        if not _qualifying_evidence_kinds(store, str(card["id"])) & set(
            _SUBSTANTIVE_EVIDENCE_KINDS
        ):
            continue
        # done_at is (created_at, rowid): the event rowid is the CAUSAL
        # tiebreaker for same-second completions — two cards done in one
        # second order by which done event landed first, never by card
        # metadata (review round-2, disposition 5).
        qualifying.append((done_at, str(card["id"]), card))
    return [card for _done_at, _id, card in sorted(qualifying, key=lambda e: e[:2])]


def _assigned_card_ids(rows: Iterable[dict[str, Any]]) -> set[str]:
    """Card ids already frozen into RESOLVED automation slots for this day.

    Parsed back out of the ``card=<id>`` token
    (:data:`_CARD_TOKEN_RE`) every delivered slot writes into its resolution
    note. A resolved row is history and is never re-judged, so on a retry its
    card must be taken OFF the table before the remaining slots are filled —
    otherwise a snapshot that has since gained an earlier-done card shifts the
    indices under the frozen assignment and hands the same automation to two
    slots.
    """
    assigned: set[str] = set()
    for row in rows:
        match = _CARD_TOKEN_RE.search(str(row.get("resolution_note") or ""))
        if match is not None:
            assigned.add(match.group("card_id"))
    return assigned


def _resolve_automation_slots(
    store: TeamStore, employee_id: str, day: str, *, resolved_at: str
) -> dict[str, int]:
    """Judge ALL of one person's automation slots for ``day``, atomically.

    Per-employee-day rather than per-row, and that is the correctness boundary,
    not a batching convenience. Every open slot is judged against ONE snapshot
    of the qualifying cards and written in ONE transaction, so a crash between
    two slots cannot leave a day half-judged against a snapshot that no longer
    exists. Slot n takes the n-th REMAINING card, so each card fills exactly one
    slot — three slots means three THINGS.

    Retry safety has two halves: already-terminal slots are never rewritten (the
    store's OPEN-only guard), and the cards they already claimed are excluded
    from the list before the remaining slots are filled
    (:func:`_assigned_card_ids`).

    AUTOMATION SLOTS NEVER CARRY, a deliberate divergence from the task rule. A
    missed CARD is work that still has to happen, so it carries. A missed
    automation SLOT is a day that did not produce one; tomorrow already mints
    three fresh slots, so carrying would stack yesterday's three on today's and
    reach fifteen open commitments by Friday — a number produced by arithmetic
    rather than by anything anybody did. The miss stays as history; the
    expectation renews.

    Returns ``{'delivered': n, 'missed': n}``.
    """
    rows = [
        dict(row)
        for row in store.list_commitments(day=day, employee_id=employee_id)
        if str(row["kind"]) == "automation"
    ]
    open_rows = [row for row in rows if str(row["status"]) in OPEN_COMMITMENT_STATUSES]
    counts = {"delivered": 0, "missed": 0}
    if not open_rows:
        return counts

    already_assigned = _assigned_card_ids(row for row in rows if row not in open_rows)
    qualifying = [
        card
        for card in _qualifying_automations(store, employee_id, day)
        if str(card["id"]) not in already_assigned
    ]

    entries: list[tuple[str, str, str]] = []
    for index, row in enumerate(sorted(open_rows, key=lambda entry: int(entry["slot"] or 1))):
        # Position among the rows STILL OPEN, not the raw slot number: with slot
        # 1 already delivered, the first open slot must take the first remaining
        # card, and indexing by slot would skip it.
        if index >= len(qualifying):
            entries.append(
                (
                    str(row["id"]),
                    "missed",
                    f"no {_ordinal(int(row['slot'] or 1))} automation on {day} "
                    f"({len(qualifying) + len(already_assigned)} of "
                    f"{AUTOMATION_SLOTS_PER_DAY} shipped)",
                )
            )
            counts["missed"] += 1
            continue
        card = qualifying[index]
        reference = str(card.get("ref") or card["id"])
        entries.append(
            (
                str(row["id"]),
                "delivered",
                f"card={card['id']} {reference} {card['title']} ({card['automation_maturity']})",
            )
        )
        counts["delivered"] += 1
    store.resolve_commitments(entries, resolved_at=resolved_at)
    return counts


def _ordinal(number: int) -> str:
    """``1 -> '1st'`` — the resolution note reads as a sentence, not a tuple."""
    suffix = {1: "st", 2: "nd", 3: "rd"}.get(number if number < 20 else number % 10, "th")
    return f"{number}{suffix}"


def _repair_missing_carries(store: TeamStore, day: str, *, resolved_at: str) -> int:
    """Mint the carry for any ``missed`` row that has none (crash repair).

    The miss and its carry are written in ONE transaction, so the gap this pass
    closes should not exist — but "should not exist" is not a guarantee across a
    process kill, and a miss whose work nobody picked back up is exactly the
    silent hole the carry rule exists to prevent.

    It repairs TWO shapes, and the second is the one a bare existence check
    misses (Sol review, item 2): a next-day row that EXISTS but is UNLINKED,
    typically minted by the 06:55 generator for the same still-active card.
    Asking "does a row exist?" reads that as "already carried" and leaves the
    miss unchained forever — so this uses ``mint_carry``, the same insert-or-LINK
    primitive the atomic path uses, and counts a link as a repair. A row already
    linked to some miss is left exactly as it is (``exists``): a chain of slips
    must read back in the order it happened.
    """
    repaired = 0
    for row in store.list_commitments(day=day, status="missed"):
        # Card commitments only. A missed improvement or automation SLOT never
        # carries — tomorrow mints fresh ones, and stacking them would reach
        # fifteen open automation rows by Friday (see
        # :func:`_resolve_automation_commitment`).
        if str(row["kind"]) != "task" or row["task_id"] is None:
            continue
        card = _card(store, str(row["task_id"]))
        payload = _carry_payload(dict(row), card, day)
        if payload is None:
            continue
        _carry_id, outcome = store.mint_carry(
            carried_from=str(row["id"]), at=resolved_at, **payload
        )
        if outcome in ("created", "linked"):
            repaired += 1
    return repaired


def resolve_day(store: TeamStore, day: str, *, resolved_at: str | None = None) -> dict[str, int]:
    """Resolve every still-OPEN row for ``day``. Idempotent.

    OPEN is two states, not one (:data:`OPEN_COMMITMENT_STATUSES`): ``committed``
    AND ``carried``. A carried row is yesterday's miss brought forward — the
    person still owes the work — so its own day must judge it, and a second miss
    carries it again, chaining through ``carried_from``. Judging only
    ``committed`` rows left every carried-forward commitment sitting terminal
    and unjudged forever, which is the exact accountability hole this table
    exists to close.

    Returns ``{'delivered': n, 'missed': n, 'carries_repaired': n}``. Rows that
    are already RESOLVED (delivered/missed) are skipped entirely — which is what
    lets the 06:55 job and the 07:00 report both call this for yesterday without
    the second call changing a single number.
    """
    now = resolved_at or utc_now_iso()
    counts = {"delivered": 0, "missed": 0, "carries_repaired": 0}
    # Automation slots are judged per EMPLOYEE-DAY, not per row: all of one
    # person's slots come from one snapshot in one transaction (see
    # :func:`_resolve_automation_slots`). This set remembers who has been done.
    automation_done: set[str] = set()
    for row in store.list_commitments(day=day, status=OPEN_COMMITMENT_STATUSES):
        record = dict(row)
        kind = str(record["kind"])
        if kind == "improvement":
            outcome = _resolve_improvement_commitment(store, record, day, resolved_at=now)
        elif kind == "automation":
            employee_id = str(record["employee_id"])
            if employee_id in automation_done:
                continue
            automation_done.add(employee_id)
            slots = _resolve_automation_slots(store, employee_id, day, resolved_at=now)
            counts["delivered"] += slots["delivered"]
            counts["missed"] += slots["missed"]
            continue
        else:
            outcome = _resolve_task_commitment(store, record, day, resolved_at=now)
        counts[outcome] += 1
    counts["carries_repaired"] = _repair_missing_carries(store, day, resolved_at=now)
    return counts


# --------------------------------------------------------------------------
# the one entrypoint the morning job uses
# --------------------------------------------------------------------------


def run_daily(store: TeamStore, *, today: str | None = None) -> dict[str, Any]:
    """Yesterday's resolution, then today's generation — in THAT order (S2).

    The single orchestration seam. Resolving first puts every carried follow-up
    in place before the generator runs, so the generator finds it (its
    ``INSERT OR IGNORE`` returns the existing row) instead of colliding with it.
    Any other order makes the outcome depend on which job happened to run first.
    """
    day = today or local_today()
    resolved = resolve_day(store, _previous_day(day))
    generated = generate_for_day(store, day)
    return {"day": day, "resolved": resolved, "generated": generated}
