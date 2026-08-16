"""Session source adapter — live agent sessions as EDC SUGGESTIONS ONLY.

Adapter #3 (after email and the internal ``rule_proposal`` writer). It turns the
``sessions`` table — the live Session Bridge state the dashboard's Sessions panel
renders — into normalized :class:`SourceEvent`s so a session that is *waiting on
the operator*, *quietly idle*, or *failed* shows up in the ordinary Decisions tab with a
concrete recommended action, instead of only being visible to whoever happens to
be looking at the panel.

**What this module deliberately is NOT.**

* **No executor, no delivery, no session writes.** Nothing here (or reachable
  from here) enqueues a session message, approves a session approval, kills,
  resumes or steers a session. The adapter is a read-only SELECT over the
  sessions DAL, exactly like :class:`~omniagentos.edc.adapters.email.EmailAdapter`
  is a read-only SELECT over ``comms_messages``. The decision it files says
  "open the Sessions panel and do X" — the human does X in the panel that
  already owns that authority.
* **Not a second approval loop.** The synthesis forbids a parallel
  suggestion/approval channel; session approvals keep their existing
  ``/api/sessions`` path. A session decision is a POINTER at that surface. To
  make that structural rather than a promise, ``available_actions_for`` limits an
  open ``source='session'`` decision to snooze/dismiss/note — the reply, delegate
  and defer executors are not reachable from a session decision at all.
* **Never an outcome signal.** Synthesis §7 lists "session activity" as a WEAK
  signal with no code path in the completion sweep. That stays true: this module
  is imported by triage only, never by ``edc/sweep.py``.
* **No LLM.** Every verdict here is produced by :func:`session_verdict` from row
  fields and clock arithmetic. The adapter carries its own ``classify_event`` so
  a session event never enters ``classify``'s ambiguity path (which would call
  ``ShortCallClient``) — triage asks the adapter first and only falls back to the
  shared classifier for adapters that do not provide one.

**The three deterministic conditions** (:func:`session_conditions`), evaluated
per row, at most one decision each:

1. ``needs_input`` — ``attention_state == 'needs_input'`` for more than
   :data:`NEEDS_INPUT_MIN_SECONDS`. The dwell is required: a session that just
   raised its hand is not yet a decision.
2. ``idle`` — a live (non-terminal) session quiet for more than
   :data:`IDLE_MIN_HOURS`; recommends closing or offloading it, carrying the
   resume note so closing is not a loss.
3. ``failed`` — ``state == 'failed'`` within :data:`FAILED_WINDOW_HOURS`;
   recommends triaging the failure.

The ``attention_*``/``company``/``agent_*`` columns are being added by a parallel
package and are read defensively (``row.get``) — an older schema simply yields no
``needs_input`` events, never an error.

**Idempotency.** ``source_ref`` is ``<session_id>|<condition>|<episode>`` where
*episode* is the stamp that condition started at. The ``UNIQUE(source,
source_ref, owner)`` backstop therefore gives exactly ONE decision per (session,
condition, episode): a 5-minute tick re-files nothing, while a NEW waiting
episode after the first one cleared is legitimately a new decision.
:func:`sweep_cleared_session_decisions` closes the other side — an open session
decision whose ref is no longer live (condition cleared, session gone, or a newer
episode superseded it) is EXPIRED by the system, so a stale suggestion never
outlives the state that justified it.

**Enablement.** Two independent gates, both off by default:
:data:`SESSIONS_FLAG_ENV` (``OMNIAGENTOS_EDC_SESSIONS``) must be truthy, and
ownership resolves through the same ``edc.accounts`` map every other source uses
(key :data:`SESSION_SOURCE`) with the operator as the documented fallback owner —
sessions run on the operator's own machine, so unlike a mailbox there is no
second candidate owner to guess between.
"""

from __future__ import annotations

import logging
import os
import shlex
from datetime import UTC, datetime, timedelta
from typing import Any, NamedTuple

from omniagentos.edc.accounts import owner_for_source
from omniagentos.edc.adapters.base import SourceEvent

logger = logging.getLogger(__name__)

__all__ = [
    "FAILED_CONDITION",
    "IDLE_CONDITION",
    "NEEDS_INPUT_CONDITION",
    "SESSIONS_FLAG_ENV",
    "SESSION_SOURCE",
    "SessionAdapter",
    "SessionCondition",
    "SessionSnapshot",
    "session_condition_scan",
    "session_conditions",
    "session_event",
    "session_verdict",
    "sessions_source_enabled",
    "sweep_cleared_session_decisions",
]

#: The stable adapter/source name — the ``decisions.source`` value and the key
#: an ``edc.accounts:`` entry uses to bind an owner to this source.
SESSION_SOURCE = "session"

#: The rollout flag. OFF by default: absent/``0``/``false`` means the collector is
#: not even constructed, so triage behaves exactly as it does today.
SESSIONS_FLAG_ENV = "OMNIAGENTOS_EDC_SESSIONS"

NEEDS_INPUT_CONDITION = "needs_input"
IDLE_CONDITION = "idle"
FAILED_CONDITION = "failed"

#: A raised hand only becomes a decision after this dwell — a session that pauses
#: for a couple of seconds mid-turn is noise, not something to interrupt the operator with.
NEEDS_INPUT_MIN_SECONDS = 60
#: A live session quiet for longer than this is a close/offload candidate.
IDLE_MIN_HOURS = 8
#: Failures older than this are history; the panel and the logs still have them.
FAILED_WINDOW_HOURS = 24

#: Non-terminal states (mirrors ``sessions.dal.TERMINAL_SESSION_STATES``, kept as a
#: literal so this Tier-low adapter does not import the sessions package to read
#: one constant; a drift test in ``tests/edc/test_sessions_adapter.py`` pins them).
LIVE_SESSION_STATES = frozenset({"starting", "running", "awaiting_approval", "resuming"})

#: Where the owner acts. Every session recommendation points here — the Sessions
#: panel is the ONE surface with authority over a session (no parallel loop).
SESSIONS_DEEP_LINK = "/sessions"

#: The operator. Sessions are spawned on the operator's own machine, so — unlike a
#: mailbox, where guessing an owner would leak someone's private mail — there is
#: exactly one candidate owner. An ``edc.accounts: session:`` entry overrides it.
_OPERATOR_EMPLOYEE_ID = "emp_owner"

#: Cap on rows pulled per tick, mirroring ``EmailAdapter``'s batch limit.
_BATCH_LIMIT = 500

_TRUTHY = frozenset({"1", "true", "yes", "on"})


#: The episode token used when a condition is provably live but its START is not
#: recorded (``attention_since`` absent). It must be STABLE, never a moving
#: stamp: an identity derived from a value that drifts every tick would mint a
#: fresh decision every tick. Recurrence after a clear is handled instead by the
#: sweep's revive path, which reopens the system-expired row.
UNKNOWN_EPISODE = "unknown"

#: ``<provider> → how you actually reattach``. Explicit, because the CLIs disagree
#: (``claude --resume <ref>`` vs ``codex resume <ref>``) and a confidently wrong
#: command is worse than none. An unknown/absent provider gets NO command.
_RESUME_SYNTAX: dict[str, str] = {
    "claude": "claude --resume {ref}",
    "codex": "codex resume {ref}",
    "gemini": "gemini --resume {ref}",
}


class SessionCondition(NamedTuple):
    """One deterministic condition detected on one session row.

    ``episode`` is the stamp the condition STARTED at (or :data:`UNKNOWN_EPISODE`
    when the source does not record one); it makes the decision's ``source_ref``
    unique per occurrence so a recurrence after a resolved one is a new decision
    rather than a silent dedupe against the closed row.
    """

    condition: str
    episode: str
    detail: str


class SessionSnapshot(NamedTuple):
    """One tick's read of the sessions table, WITH its own reliability.

    ``complete`` is False when the DAL page was filled, i.e. the read may have
    omitted rows. ``indeterminate`` holds ``<session_id>|<condition>`` keys the
    read could not adjudicate (a condition flag is set but its stamp is missing
    or malformed, so the age is unknown). Both exist for the same reason: the
    sweep's expiry is DESTRUCTIVE, and absence-from-a-partial-read is not
    evidence that a condition cleared.
    """

    events: list[SourceEvent]
    complete: bool
    indeterminate: set[str]


def sessions_source_enabled(env: dict[str, str] | None = None) -> bool:
    """Whether the session collector is armed (:data:`SESSIONS_FLAG_ENV`, default OFF)."""
    table = env if env is not None else os.environ
    return str(table.get(SESSIONS_FLAG_ENV, "")).strip().lower() in _TRUTHY


def _as_utc(value: Any) -> datetime | None:
    """Parse an ISO stamp to aware UTC, or ``None`` when it is absent/malformed."""
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _now_utc(value: datetime | None) -> datetime:
    """Normalize a caller's clock to aware UTC, reading NAIVE as UTC.

    A naive ``now`` would otherwise be interpreted by ``astimezone`` in the HOST
    timezone while naive row stamps are read as UTC (:func:`_as_utc`) — the two
    halves of every age comparison would then sit hours apart depending on where
    the machine thinks it is. One rule for both sides: naive means UTC.
    """
    if value is None:
        return datetime.now(UTC)
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _text(row: dict[str, Any], key: str) -> str:
    value = row.get(key)
    return "" if value is None else str(value).strip()


def _label(row: dict[str, Any]) -> str:
    """The most human name available for a session: agent, then title, then id."""
    return _text(row, "agent_name") or _text(row, "title") or _text(row, "id") or "(unnamed)"


def _where(row: dict[str, Any]) -> str:
    """The parenthetical scope of a session — company if known, else project dir."""
    company = _text(row, "company")
    if company:
        return company
    project = _text(row, "project_dir")
    return project.rsplit("/", 1)[-1] if project else ""


def _resume_note(row: dict[str, Any]) -> str:
    """How to pick this session back up, so "close it" is never a loss of work.

    Three rules, all learned the hard way: the provider's syntax is looked up,
    never assumed (``codex resume`` is not ``codex --resume``, and an unknown
    provider gets no command at all rather than a plausible wrong one); every
    path is ``shlex.quote``d, because this line is rendered for a human to paste
    into a shell and a project dir with a space — let alone a ``;`` — would
    otherwise produce a command that does something else entirely; and an absent
    ref degrades to a location, not to a fabricated one.
    """
    project = _text(row, "project_dir")
    ref = _text(row, "session_ref")
    syntax = _RESUME_SYNTAX.get(_text(row, "provider").lower())
    if project and ref and syntax:
        return f"cd {shlex.quote(project)} && {syntax.format(ref=shlex.quote(ref))}"
    if project:
        return f"reattach from {shlex.quote(project)} via the Sessions panel"
    return "no resume reference recorded"


def session_conditions(row: dict[str, Any], *, now: datetime) -> list[SessionCondition]:
    """Every condition this session row currently satisfies (deterministic).

    Pure and side-effect-free: the same row + clock always yields the same list,
    which is what makes the whole source safe to run every tick. Missing or
    unparseable stamps fail CLOSED (no condition) — the adapter never asserts a
    dwell it cannot prove. Use :func:`session_condition_scan` when you also need
    to know WHICH conditions were unprovable, because "could not adjudicate" and
    "definitely not true" must not be conflated by a destructive sweep.
    """
    return session_condition_scan(row, now=now)[0]


def session_condition_scan(
    row: dict[str, Any], *, now: datetime
) -> tuple[list[SessionCondition], set[str]]:
    """``(conditions, indeterminate)`` — the detector plus its own uncertainty.

    A key lands in ``indeterminate`` (as ``<session_id>|<condition>``) when the
    row ASSERTS the condition's precondition but the timestamp needed to age it
    is absent or malformed. That is the difference between "the session answered"
    and "one read could not tell", and only the first may retire a suggestion.
    """
    moment = _now_utc(now)
    session_id = _text(row, "id")
    state = _text(row, "state")
    found: list[SessionCondition] = []
    unknown: set[str] = set()

    # (a) waiting on a human. Requires BOTH the attention flag and a provable dwell.
    if _text(row, "attention_state") == NEEDS_INPUT_CONDITION:
        started = _as_utc(row.get("attention_since"))
        # The fallback proves the DWELL only. It must never become the episode
        # identity: last_activity_at moves while the same hand stays raised, so an
        # identity derived from it would mint a new decision on every tick.
        provable = started or _as_utc(row.get("last_activity_at"))
        if provable is None:
            unknown.add(f"{session_id}|{NEEDS_INPUT_CONDITION}")
        elif (moment - provable) > timedelta(seconds=NEEDS_INPUT_MIN_SECONDS):
            found.append(
                SessionCondition(
                    condition=NEEDS_INPUT_CONDITION,
                    episode=_stamp(started) if started is not None else UNKNOWN_EPISODE,
                    detail=_text(row, "attention_reason") or "needs your input",
                )
            )

    # (b) a live session gone quiet — a close/offload candidate, never a failure.
    if state in LIVE_SESSION_STATES:
        quiet_since = _as_utc(row.get("last_activity_at"))
        if quiet_since is None:
            unknown.add(f"{session_id}|{IDLE_CONDITION}")
        else:
            quiet_for = moment - quiet_since
            if quiet_for >= timedelta(hours=IDLE_MIN_HOURS):
                found.append(
                    SessionCondition(
                        condition=IDLE_CONDITION,
                        episode=_stamp(quiet_since),
                        detail=f"no activity for {int(quiet_for.total_seconds() // 3600)}h",
                    )
                )

    # (c) a recent failure to triage. "Recent" is 0 <= age < 24h: a stamp in the
    # FUTURE (clock skew, a bad writer) is not evidence of a failure in the past
    # 24h, and letting a negative age through would keep such a row permanently
    # "recent". The upper bound is exclusive, matching "within the last 24h".
    if state == FAILED_CONDITION:
        failed_at = _as_utc(row.get("updated_at")) or _as_utc(row.get("last_activity_at"))
        if failed_at is None:
            unknown.add(f"{session_id}|{FAILED_CONDITION}")
        elif timedelta(0) <= (moment - failed_at) < timedelta(hours=FAILED_WINDOW_HOURS):
            error = _text(row, "error") or _text(row, "session_error") or "no error text recorded"
            found.append(
                SessionCondition(
                    condition=FAILED_CONDITION,
                    episode=_stamp(failed_at),
                    detail=error[:300],
                )
            )
    return found, unknown


def _stamp(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def source_ref_for(session_id: str, found: SessionCondition) -> str:
    """The idempotency key: one decision per (session, condition, episode)."""
    return f"{session_id}|{found.condition}|{found.episode}"


def session_event(
    row: dict[str, Any],
    found: SessionCondition,
    *,
    owner_employee_id: str,
    company_slug: str = "",
) -> SourceEvent:
    """Normalize one (row, condition) pair into a :class:`SourceEvent`.

    ``sender_verified`` is ``True`` and deliberately so: unlike email there is no
    spoofable sender — the "counterparty" is the estate's own sessions table read
    through its own DAL. It is inert either way, because a session verdict can
    never be URGENT (see :func:`session_verdict`).
    """
    session_id = _text(row, "id")
    scope = _where(row)
    label = _label(row)
    where = f" ({scope})" if scope else ""
    if found.condition == NEEDS_INPUT_CONDITION:
        title = f"Session {label}{where} is waiting: {found.detail}"
    elif found.condition == IDLE_CONDITION:
        title = f"Session {label}{where} is idle: {found.detail}"
    else:
        title = f"Session {label}{where} failed: {found.detail}"

    return SourceEvent(
        source=SESSION_SOURCE,
        source_ref=source_ref_for(session_id, found),
        source_account=SESSION_SOURCE,
        owner_employee_id=owner_employee_id,
        company_slug=company_slug or _text(row, "company"),
        occurred_at=found.episode,
        title=title[:400],
        body=found.detail,
        counterparty=f"session:{session_id}",
        sender_verified=True,
        metadata={
            "session_id": session_id,
            "condition": found.condition,
            "episode": found.episode,
            "state": _text(row, "state"),
            "agent_status": _text(row, "agent_status"),
            "project_dir": _text(row, "project_dir"),
            "provider": _text(row, "provider"),
            "session_ref": _text(row, "session_ref"),
            "resume": _resume_note(row),
            "deep_link": SESSIONS_DEEP_LINK,
        },
    )


#: The ONLY affordances a session suggestion carries. Mirrored by
#: ``store.available_actions_for`` so the server, not this list, is authoritative.
SESSION_ACTIONS = ["snooze", "dismiss", "note"]


def session_verdict(event: SourceEvent) -> dict[str, Any]:
    """The deterministic verdict for a session event — no LLM, never URGENT.

    NEEDS_OWNER is the ceiling on purpose. URGENT is the class that DMs the operator, and a
    quiet agent session is precisely the thing that must not interrupt him; the
    Decisions tab is where these belong. Because the class can never be urgent,
    ``main._surface_urgent`` is unreachable for this source.
    """
    metadata = event.get("metadata") or {}
    condition = str(metadata.get("condition") or "")
    session_id = str(metadata.get("session_id") or "")
    params: dict[str, Any] = {
        "session_id": session_id,
        "condition": condition,
        "deep_link": SESSIONS_DEEP_LINK,
        "project_dir": metadata.get("project_dir", ""),
    }

    if condition == NEEDS_INPUT_CONDITION:
        human_line = "Open the Sessions panel and approve/deny"
        reason = "a live session has been waiting on a human answer for over a minute"
        confidence = 0.9
    elif condition == IDLE_CONDITION:
        resume = str(metadata.get("resume") or "")
        params["resume"] = resume
        human_line = f"Close or offload this idle session — resume later with: {resume}"
        reason = "a live session has produced no activity for hours; it is holding a slot"
        confidence = 0.7
    else:
        params["state"] = metadata.get("state", "")
        human_line = "Open the Sessions panel and triage the failure"
        reason = "a session ended in the failed state and nobody has looked at it"
        confidence = 0.8

    return {
        "classification": "needs_owner",
        "consequence": "none",
        "deadline_at": None,
        "likelihood": 0.9,
        "confidence": confidence,
        "reason": reason,
        "classifier": "deterministic",
        "rule_matches": [f"session:{condition}"],
        "recommended": {"kind": "review", "human_line": human_line, "params": params},
        "available_actions": list(SESSION_ACTIONS),
        "status": "open",
        "surfaced": 0,
    }


def _owner_binding(config: Any) -> tuple[str, str]:
    """``(owner_employee_id, company_slug)`` for the session source.

    Prefers an explicit ``edc.accounts: session:`` binding (the same static,
    git-reviewable map email uses). Falls back to the operator — documented at
    :data:`_OPERATOR_EMPLOYEE_ID` — because a session is by construction the
    operator's own process, not a mailbox whose owner could be anyone.
    """
    binding = owner_for_source(SESSION_SOURCE, config)
    if binding is None:
        return _OPERATOR_EMPLOYEE_ID, ""
    return binding.owner_employee_id, binding.company_slug


class SessionAdapter:
    """Yields the currently-attention-worthy sessions as :class:`SourceEvent`s.

    Read-only over the sessions table, and STATELESS across ticks: it re-derives
    every live condition each pass and leans on the ``UNIQUE(source, source_ref,
    owner)`` backstop for idempotency rather than an ``edc_source_cursor``
    watermark. A watermark would be wrong here — email rows are append-only and
    monotonic, session rows MUTATE (a session becomes idle, then answers, then
    fails), so "everything after id N" would miss exactly the transitions this
    source exists to notice.
    """

    name = SESSION_SOURCE

    def __init__(
        self,
        *,
        owner_employee_id: str | None = None,
        company_slug: str = "",
        config: Any = None,
        dal: Any = None,
        now: datetime | None = None,
        batch_limit: int = _BATCH_LIMIT,
    ) -> None:
        resolved_owner, resolved_company = (
            (owner_employee_id, company_slug) if owner_employee_id else _owner_binding(config)
        )
        self._owner = resolved_owner
        self._company = company_slug or resolved_company
        self._dal = dal
        # The tick's clock. ``pending_events`` takes no ``now`` (the SourceAdapter
        # Protocol has none), so pinning it here is how a caller — the triage tick
        # that already fixed a moment, or a test — keeps the dwell arithmetic on
        # the SAME instant the rest of the pass uses instead of re-reading the
        # wall clock mid-tick.
        self._now = now
        self._batch_limit = batch_limit

    @property
    def owner_employee_id(self) -> str:
        return self._owner

    def _rows(self, store: Any) -> tuple[list[dict[str, Any]], bool]:
        """``(rows, complete)`` — live + recently-failed sessions, via the sessions DAL.

        Composed like every other EDC source: the sibling DAL over the SAME
        control-plane database (``store._store._db_path``), never an HTTP call
        into the local API — the estate's EDC jobs read DALs directly.

        ``complete`` is False when either page came back FULL. A full page means
        the read may have been truncated, and the sweep treats absence as proof
        of a cleared condition — so the cap has to travel with the data instead
        of being silently forgotten one call up the stack.
        """
        dal = self._dal
        owns_dal = dal is None
        if dal is None:
            from omniagentos.sessions.dal import SessionsDal

            dal = SessionsDal(store._store._db_path)
        try:
            live = list(dal.list_live_sessions(limit=self._batch_limit))
            failed = list(dal.list_sessions(state=FAILED_CONDITION, limit=self._batch_limit))
        finally:
            if owns_dal:
                dal.close()
        complete = len(live) < self._batch_limit and len(failed) < self._batch_limit
        return [dict(row) for row in (*live, *failed)], complete

    def live_snapshot(self, store: Any, *, now: datetime | None = None) -> SessionSnapshot:
        """One tick's conditions, with the read's completeness and uncertainty.

        The same snapshot serves BOTH directions of the loop: triage creates a
        decision per event, and :func:`sweep_cleared_session_decisions` retires
        the session decisions whose refs are absent from it — which is exactly
        why the sweep needs the ``complete``/``indeterminate`` flags rather than
        a bare list it would have to trust unconditionally.
        """
        moment = _now_utc(now or self._now)
        rows, complete = self._rows(store)
        events: list[SourceEvent] = []
        indeterminate: set[str] = set()
        for row in rows:
            if not _text(row, "id"):
                continue
            found, unknown = session_condition_scan(row, now=moment)
            indeterminate |= unknown
            events.extend(
                session_event(
                    row,
                    condition,
                    owner_employee_id=self._owner,
                    company_slug=self._company,
                )
                for condition in found
            )
        return SessionSnapshot(events=events, complete=complete, indeterminate=indeterminate)

    def live_events(self, store: Any, *, now: datetime | None = None) -> list[SourceEvent]:
        """Just the events of :meth:`live_snapshot` (the triage half of the loop)."""
        return self.live_snapshot(store, now=now).events

    def pending_events(self, store: Any) -> list[SourceEvent]:
        """:class:`~omniagentos.edc.adapters.base.SourceAdapter` entry point."""
        return self.live_events(store)

    def classify_event(self, event: SourceEvent, *, now: datetime | None = None) -> dict[str, Any]:
        """The adapter's OWN deterministic verdict (triage prefers it over ``classify``).

        Present so a session event never reaches ``classify``'s ambiguity branch,
        which would spend an ``edc_classify`` LLM call to re-derive what the row
        already states outright.
        """
        del now  # the verdict is a pure function of the event
        return session_verdict(event)


#: Statuses the sweep adjudicates. ``snoozed`` is included deliberately: a snooze
#: defers WHEN the owner looks at an item, it does not promise the item still
#: exists — a snoozed card whose session finished would otherwise pop back up in
#: three days pointing at nothing. Retiring it preserves the audit fact that it
#: was snoozed (the resolution and the snooze event both stay on the row).
_SWEPT_STATUSES = ("open", "snoozed")

#: Statuses the revive path may resurrect from. ONLY ``expired`` — the status
#: this sweep itself writes. An owner's ``dismissed``/``denied`` is a decision and
#: is never undone by a machine.
_REVIVABLE_STATUS = "expired"


def sweep_cleared_session_decisions(
    decisions: Any,
    *,
    snapshot: SessionSnapshot,
    owner_employee_id: str,
) -> dict[str, int]:
    """Reconcile the open session suggestions against one snapshot of reality.

    Three moves, in order of how much damage each could do if it were wrong:

    * **expire** a suggestion whose condition is gone (the session answered,
      resumed, was closed, or moved to a newer episode). Marked ``expired`` by
      the SYSTEM, never ``dismissed``: dismiss is an owner resolution and would
      both fake a decision the operator never made and feed the learner a pattern he never
      taught it. The transition is the store's CAS+audit unit
      (:meth:`DecisionStore.expire_decision`), so an owner resolving the row in
      the same instant WINS and a crash cannot persist a status without its
      receipt.
    * **revive** a system-expired suggestion whose condition is live again under
      the SAME identity. Without it, the ``UNIQUE(source, source_ref, owner)``
      dedupe would hand triage the retired row and the item would be invisible
      forever — a false expiry could never repair itself.
    * **refresh** an open suggestion whose rendered advice has drifted (a resume
      reference that only landed after the card was minted). Same idea, and the
      same seam, as ``learn._refresh_proposal_decision``: the owner must act on
      what the source says NOW, not on what it said when the row was created.

    **Expiry is destructive, so it demands positive evidence.** An incomplete
    read (``snapshot.complete`` false — a filled DAL page that may have omitted
    rows) suppresses the expiry pass entirely, and a condition the read could not
    adjudicate (``snapshot.indeterminate``) is skipped row by row. Absence in a
    partial or uncertain observation is not evidence of a cleared condition; the
    next healthy tick will retire whatever genuinely cleared.
    """
    by_ref = {event["source_ref"]: event for event in snapshot.events}
    stats = {
        "session_expired": 0,
        "session_revived": 0,
        "session_refreshed": 0,
        "session_expiry_skipped": 0,
    }

    for decision in decisions.list_source_decisions(
        owner_employee_id=owner_employee_id,
        source=SESSION_SOURCE,
        statuses=(*_SWEPT_STATUSES, _REVIVABLE_STATUS),
    ):
        ref = str(decision.get("source_ref") or "")
        status = str(decision.get("status") or "")
        event = by_ref.get(ref)

        if event is not None:
            if status == _REVIVABLE_STATUS:
                revived = decisions.reopen(
                    decision["id"],
                    owner_employee_id=owner_employee_id,
                    from_status=_REVIVABLE_STATUS,
                    event="surface",
                    note="session condition observed again",
                )
                if revived is not None:
                    stats["session_revived"] += 1
                    decision = revived
                    status = str(revived.get("status") or "")
            if status in _SWEPT_STATUSES and _refresh_recommendation(
                decisions, decision, event, owner_employee_id=owner_employee_id
            ):
                stats["session_refreshed"] += 1
            continue

        if status not in _SWEPT_STATUSES:
            continue
        if not snapshot.complete or _condition_key(ref) in snapshot.indeterminate:
            stats["session_expiry_skipped"] += 1
            continue
        if (
            decisions.expire_decision(
                decision["id"],
                owner_employee_id=owner_employee_id,
                from_status=status,
                note="session condition cleared",
                actor="system:session",
            )
            is not None
        ):
            stats["session_expired"] += 1
    return stats


def _condition_key(source_ref: str) -> str:
    """``<session_id>|<condition>`` — a ref stripped of its episode."""
    parts = source_ref.split("|")
    return "|".join(parts[:2]) if len(parts) >= 2 else source_ref


def _refresh_recommendation(
    decisions: Any,
    decision: dict[str, Any],
    event: SourceEvent,
    *,
    owner_employee_id: str,
) -> bool:
    """Re-render a still-open suggestion when the source's own advice moved.

    Only the presentation is rewritten (title + recommended). The verdict, the
    identity and the audit trail are untouched — this is not a reclassification,
    it is the card catching up with the row it points at.
    """
    verdict = session_verdict(event)
    title = str(event.get("title") or "")
    recommended = verdict["recommended"]
    if decision.get("title") == title and decision.get("recommended") == recommended:
        return False
    updated = decisions.update_decision(
        decision["id"],
        owner_employee_id=owner_employee_id,
        fields={"title": title, "recommended": recommended},
    )
    return updated is not None
