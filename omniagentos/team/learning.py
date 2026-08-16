"""Feed a verified (or refused) card into the metacog learning ladder.

The spec's second half: every finished task should also make the system more
capable of doing the next one. Nothing in the estate read ``board_tasks`` for
learning before this module — the three existing pipelines all watch agent runs
— so a person's verified work produced no memory candidate at all.

What this is NOT: a judge. There is no LLM here. The hook registers the card's
evidence as metacog artifacts and files ONE deterministic memory candidate
against them; every existing guard downstream stays exactly as it is (candidate
-> shadow -> promoted, evidence fail-closed, the recurrence gate on
``synthesize_skill``). The graduation ladder is inherited, not rebuilt.

WHERE IT IS CALLED FROM, AND WHY IT NEVER RAISES
------------------------------------------------
The API ROUTE layer calls it strictly AFTER the verify/fail store transaction
has COMMITTED — never inside it. A learning failure must never roll back a
verification: the verification is the truth the board owes its readers, and a
memory candidate is a nice-to-have derived from it. Consequently:

* the hook returns ``None`` instead of raising, for every failure mode;
* a crash between the store commit and this call loses ONE candidate. That is
  an accepted residual: the ``verify``/``verify_failed`` events remain the
  durable record, and the ``learning_capture`` marker below is exactly what a
  future backfill sweep would key on to find the gap;
* the reverse residual also exists — a candidate created and then the process
  dies before the marker event lands — and would produce one duplicate
  candidate on a later re-verify. One duplicate in the pending queue is a
  strictly smaller harm than a rolled-back verification, and metacog's
  promotion path already tolerates duplicates.

IDEMPOTENCY (review S5, round-3 §5)
-----------------------------------
On success the hook appends a durable ``comment`` task event::

    learning_capture: <candidate_id> outcome=<procedure|lesson> event=<tve_ id>

and dedupes on ``(task_id, triggering event id, outcome)`` before creating
anything. Both the event id and the "was this the first success?" decision are
handed in by the STORE, which read them inside the verification transaction
(:class:`~omniagentos.team.store.VerificationResult`) — this module never
re-reads the trail to guess which event it is following, and the route never
infers first-success from ``verified_at`` (a stamp that a refusal or a reopen
clears, making a third pass look like a first one).

Both dedupe keys are load-bearing. The OUTCOME key: a card that was verified
(procedure), then refused (lesson), then verified again must still file the
lesson — a bare "already captured" check would have suppressed it because a
procedure marker already existed. The EVENT ID: ``task_events.id`` is unique and
immutable, while two verifications inside the same second share a timestamp, so
keying on a timestamp could suppress a genuinely distinct capture.

A REAL failure (metacog raised) logs a warning AND best-effort appends
``learning_capture_failed: <ExcClass>``, so a refusal leaves durable evidence
that is distinguishable from "no candidate was warranted". A zero-evidence
human verify is the latter: it SKIPS with an info line, because a verify with
nothing to point at is a legitimate judgement call, not a failure.
"""

from __future__ import annotations

import json
import logging
import os
from collections.abc import Sequence
from typing import Any

from omniagentos.db.store import SqliteStore
from omniagentos.team.store import TeamStore

_LOG = logging.getLogger(__name__)

#: Kill switch. Default ON; "0" disables (the estate's flag idiom).
LEARNING_FLAG = "OMNIAGENTOS_TEAM_LEARNING"

#: Deliberately low. A candidate is a hypothesis about a repeatable procedure,
#: not a fact — the promotion ladder is what raises confidence, from recurrence.
CANDIDATE_CONFIDENCE = 0.6

_MARKER_PREFIX = "learning_capture:"
_FAILURE_PREFIX = "learning_capture_failed:"

#: The outcome each triggering event maps to. Written into every marker note, so
#: a future backfill sweep can key on the same pair the hook deduped on.
OUTCOME_BY_EVENT = {"verify": "procedure", "verify_failed": "lesson"}


def learning_enabled() -> bool:
    return os.environ.get(LEARNING_FLAG, "1").strip() != "0"


def _team_store(target: TeamStore | SqliteStore | str) -> TeamStore:
    """Accept whatever the caller already holds — store, base store, or path."""
    if isinstance(target, TeamStore):
        return target
    return TeamStore(target)


def _marker_note(candidate_id: str, outcome: str, event_id: str) -> str:
    return f"{_MARKER_PREFIX} {candidate_id} outcome={outcome} event={event_id}"


def _already_captured(events: list[dict[str, Any]], outcome: str, event_id: str) -> bool:
    """Whether THIS (event, outcome) pair has already produced a candidate."""
    tokens = (f"outcome={outcome}", f"event={event_id}")
    return any(
        str(row.get("event")) == "comment"
        and str(row.get("note") or "").startswith(_MARKER_PREFIX)
        and all(token in str(row.get("note") or "") for token in tokens)
        for row in events
    )


def _company_slug(store: TeamStore, card: dict[str, Any]) -> str | None:
    """The card's company slug via ``goal_id -> company_goals -> org_companies``.

    ``None`` means exactly one thing: this card is GOAL-LESS (or its goal's
    company row is gone), so there is genuinely no company to scope to.

    It deliberately does NOT catch lookup errors. Swallowing a failed join here
    would hand back the same ``None`` a goal-less card produces, mint an
    unscoped candidate, and report success — the favourable-absence shape, where
    a broken instrument reads as an honest negative. A raise instead reaches
    :func:`_capture`'s failure recorder, which writes the durable
    ``learning_capture_failed`` marker and mints nothing: the capture is lost
    LOUDLY, which is the only acceptable way to lose it.
    """
    goal_id = card.get("goal_id")
    if not goal_id:
        return None
    row = store._store._connection.execute(
        "SELECT oc.slug FROM company_goals cg "
        "JOIN org_companies oc ON oc.id = cg.org_company_id WHERE cg.id = ?",
        (str(goal_id),),
    ).fetchone()
    return None if row is None else str(row["slug"])


def _evidence_phrase(rows: Sequence[Any]) -> str:
    """Per-evidence detail, never a bare count (review S12).

    "3 pieces of evidence" tells a future reader nothing they can act on; a
    kind, a repo, a ref and the machine's verdict are all searchable.
    """
    parts: list[str] = []
    for row in rows:
        item = dict(row)
        repo = str(item.get("repo") or "")
        ref = str(item.get("ref") or "")
        where = f"{repo}@{ref}" if repo else ref
        parts.append(f"{item.get('kind')} {where} [{item.get('quality_gate')}]")
    return "; ".join(parts) if parts else "none recorded"


def _statement(task_row: Any, evidence_rows: Sequence[Any], *, outcome: str, note: str) -> str:
    """The deterministic candidate statement. Same inputs -> same bytes."""
    card = dict(task_row)
    reference = str(card.get("ref") or card.get("id") or "")
    verb = "was verified" if outcome == "procedure" else "FAILED verification"
    maturity = card.get("automation_maturity") or "untracked"
    automation_note = str(card.get("automation_note") or "").strip()
    pieces = [
        f"Board card {reference} '{card.get('title')}' (owner "
        f"{card.get('owner_employee_id') or 'unowned'}, size {card.get('size')}, "
        f"company_goal {card.get('goal_id') or 'none'}) {verb}.",
        f"Acceptance: {str(card.get('acceptance_criteria') or '').strip() or 'none recorded'}.",
        f"Evidence: {_evidence_phrase(evidence_rows)}.",
        f"Automation maturity: {maturity}.",
    ]
    if automation_note:
        pieces.append(f"Next time the system could: {automation_note}.")
    if note:
        pieces.append(f"Reason: {note}.")
    return " ".join(pieces)


def _register_artifacts(
    service: Any, task_row: Any, evidence_rows: Sequence[Any], *, outcome: str, note: str
) -> list[str]:
    """Every evidence row as a metacog artifact; the ids the candidate links.

    On the FAILURE path the refusal itself is registered as an artifact even
    when the card carries no evidence: ``create_memory_candidate`` is
    fail-closed on evidence, and "this was refused, and why" is precisely the
    thing worth remembering. On the success path a card with no evidence never
    reaches here (the caller skips it).
    """
    card = dict(task_row)
    task_id = str(card.get("id") or "")
    artifact_ids: list[str] = []
    for row in evidence_rows:
        item = dict(row)
        envelope = service.register_artifact(
            artifact_type=f"task_evidence.{item.get('kind')}",
            content=json.dumps(
                {
                    "board_task_id": task_id,
                    "ref": card.get("ref"),
                    "kind": item.get("kind"),
                    "repo": item.get("repo"),
                    "evidence_ref": item.get("ref"),
                    "quality_gate": item.get("quality_gate"),
                    "title": item.get("title"),
                },
                separators=(",", ":"),
                sort_keys=True,
            ),
            task_id=task_id,
            producer_agent_id=str(card.get("owner_employee_id") or "") or None,
        )
        artifact_ids.append(str(envelope.id))
    if outcome == "lesson":
        envelope = service.register_artifact(
            artifact_type="task_verification_failure",
            content=json.dumps(
                {"board_task_id": task_id, "ref": card.get("ref"), "reason": note},
                separators=(",", ":"),
                sort_keys=True,
            ),
            task_id=task_id,
        )
        artifact_ids.append(str(envelope.id))
    return artifact_ids


def _record_failure(store: TeamStore, task_id: str, exc: BaseException) -> None:
    """Leave durable evidence that a capture was ATTEMPTED and refused."""
    _LOG.warning("team-learning: capture failed for %s: %r", task_id, exc)
    try:
        store.append_comment(
            task_id, actor="system", note=f"{_FAILURE_PREFIX} {type(exc).__name__}"
        )
    except Exception as inner:  # noqa: BLE001 -- the hook cannot raise, ever
        _LOG.warning("team-learning: could not record the failure for %s: %r", task_id, inner)


def _capture(
    target: TeamStore | SqliteStore | str,
    task_row: Any,
    evidence_rows: Sequence[Any],
    *,
    outcome: str,
    event_id: str,
    note: str = "",
) -> str | None:
    """The shared body of both hooks. Returns the candidate id, or None.

    ``event_id`` is the id the STORE minted inside the verification transaction
    (:class:`~omniagentos.team.store.VerificationResult`) and is never
    re-derived here. Scanning the trail for "the last verify event" attributes
    this capture to whichever verification landed most recently, which under
    any concurrency need not be the one that triggered it (Sol review, item 3).
    """
    if not learning_enabled():
        return None
    store = _team_store(target)
    card = dict(task_row)
    task_id = str(card.get("id") or "")
    if not task_id:  # pragma: no cover -- defensive; routes always pass a row
        return None
    events = store.list_events(task_id)
    if _already_captured(events, outcome, event_id):
        _LOG.info("team-learning: %s already captured for %s; skipping", outcome, task_id)
        return None
    if outcome == "procedure" and not evidence_rows:
        # A legitimate learning-free verify (the human path allows one), not a
        # failure: there is nothing for a candidate to point at.
        _LOG.info("team-learning: no evidence on %s; nothing to learn from", task_id)
        return None
    try:
        from omniagentos.metacog.service import MetacogService

        # Scope FIRST: it is the only step that can fail without having written
        # anything, so a broken join costs no orphan artifacts on its way out.
        company = _company_slug(store, card)
        service = MetacogService(db_path=store._store._db_path)
        artifact_ids = _register_artifacts(service, card, evidence_rows, outcome=outcome, note=note)
        candidate = service.create_memory_candidate(
            statement=_statement(card, evidence_rows, outcome=outcome, note=note),
            memory_type=outcome,
            evidence=artifact_ids,
            applicability={
                "board_task_id": task_id,
                "ref": card.get("ref"),
                # The company this work was FOR, resolved through the card's
                # goal (the same join the improvement slot uses). Without it a
                # candidate cannot be scoped when it is retrieved, and a lesson
                # from one brand's work would surface as advice for another.
                "company": company,
            },
            confidence=CANDIDATE_CONFIDENCE,
        )
    except Exception as exc:  # noqa: BLE001 -- a learning failure is never the caller's problem
        _record_failure(store, task_id, exc)
        return None
    candidate_id = str(candidate.id)
    try:
        store.append_comment(
            task_id, actor="system", note=_marker_note(candidate_id, outcome, event_id)
        )
    except Exception as exc:  # noqa: BLE001 -- the candidate exists; the marker is bookkeeping
        _LOG.warning("team-learning: marker write failed for %s: %r", task_id, exc)
    return candidate_id


def on_task_verified(
    target: TeamStore | SqliteStore | str,
    task_row: Any,
    evidence_rows: Sequence[Any] = (),
    *,
    event_id: str,
) -> str | None:
    """File a ``procedure`` candidate for a card that was just verified.

    Call ONLY on the FIRST successful verification and only after the store
    transaction committed. Both of those facts come from the store's
    :class:`~omniagentos.team.store.VerificationResult`
    (``first_success`` / ``event_id``); the caller must not re-derive either.
    """
    return _capture(target, task_row, evidence_rows, outcome="procedure", event_id=event_id)


def on_verification_failed(
    target: TeamStore | SqliteStore | str,
    task_row: Any,
    evidence_rows: Sequence[Any] = (),
    *,
    event_id: str,
    reason: str = "",
) -> str | None:
    """File a ``lesson`` candidate for a card whose verification was refused.

    Failure evidence is retained deliberately (spec §4): the estate learns more
    from a refused card than from an unremarkable pass, and the reason is the
    part worth remembering.
    """
    return _capture(
        target,
        task_row,
        evidence_rows,
        outcome="lesson",
        event_id=event_id,
        note=str(reason or "").strip(),
    )
