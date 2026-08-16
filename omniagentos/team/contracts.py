"""Shapes and closed vocabularies for the Team Work OS (migration 123).

Mirrors ``omniagentos.company_goals.models``: the pydantic models here are the
row shapes, the tuples are the vocabularies migration 123 pins with CHECK
constraints, and nothing in this module opens a database.
"""

from __future__ import annotations

import json
from typing import Any

from pydantic import BaseModel, Field

from omniagentos.collab.contracts import AUTOMATION_MATURITY as _AUTOMATION_MATURITY
from omniagentos.collab.contracts import (
    AUTOMATION_PROPOSAL_SOURCE as _AUTOMATION_PROPOSAL_SOURCE,
)
from omniagentos.collab.contracts import OPERATOR_EMPLOYEE_ID as _OPERATOR_EMPLOYEE_ID
from omniagentos.collab.contracts import TASK_ADHOC_SOURCE as _TASK_ADHOC_SOURCE
from omniagentos.contracts import new_id, utc_now_iso

EVIDENCE_ID_PREFIX = "tev"
EVENT_ID_PREFIX = "tve"

# What a piece of evidence IS. Pinned by migration 123's CHECK — this tuple and
# that CHECK must agree, and ``tests/team/test_schema.py`` proves they do.
EVIDENCE_KINDS: tuple[str, ...] = (
    "commit",
    "pr",
    "review",
    "session",
    "test_run",
    "deploy",
    "doc",
    "customer_reply",
    "research",
    "note",
)

# How the evidence got attached to its card. A 'manual' row is a human
# correction and no deterministic sweep may overwrite it (``TeamStore.is_manual``).
ATTRIBUTIONS: tuple[str, ...] = ("deterministic", "manual")

# Not every artifact is CREDIT. A rejected review, a reverted commit or a card
# that took excessive attempts is evidence that the work happened AND that it
# did not land — counting it as output is how a productivity number lies.
QUALITY_GATES: tuple[str, ...] = ("pass", "rejected", "reverted", "excessive_attempts")

# Evidence kinds a machine can grade on its own. Their presence (at
# ``quality_gate='pass'``) is what lets ANYONE verify a card, because the claim
# being made is the test runner's, not the verifier's.
MECHANICAL_EVIDENCE_KINDS: frozenset[str] = frozenset({"test_run", "pr"})

TASK_EVENTS: tuple[str, ...] = (
    "status_change",
    "assign",
    "verify",
    "unverify",
    # Migration 132. A verification that was REFUSED, with the reason in the
    # note. The card's verification_failed_* columns are cleared by a later good
    # verify; this row is the only place the refusal survives that repair.
    "verify_failed",
    "block",
    "comment",
    "evidence",
    "create",
)

# How much of the work the SYSTEM did (migration 132, spec §9). Defined in
# ``collab.contracts`` — the collab store validates the PATCH against it and
# cannot import this package, exactly like ``TASK_PRIORITIES`` — and re-exported
# here, where every team reader already looks.
AUTOMATION_MATURITY = _AUTOMATION_MATURITY

# --- daily commitments (migration 132) -------------------------------------

COMMITMENT_ID_PREFIX = "tcm"

# What a commitment IS: a named CARD ("finish PR-12 today"), the standing
# improvement slot, or one of the daily AUTOMATION slots (the operator's ruling
# 2026-08-14, answering the spec's Q1 — three new automations or skills a day,
# per dev). Pinned by migration 133's CHECK; ``tests/team/test_schema.py``
# proves the tuple and the constraint agree.
COMMITMENT_KINDS: tuple[str, ...] = ("task", "improvement", "automation")

# The kinds that occupy numbered SLOTS rather than naming a card. Migration
# 133's partial unique index is keyed on (day, employee, kind, slot) over
# exactly these, which is what makes the generator idempotent and caps the
# daily count at the numbers below.
SLOTTED_COMMITMENT_KINDS: tuple[str, ...] = ("improvement", "automation")

# Three a day, every day, per dev (the operator, 2026-08-14). Not a stretch target and
# not an average: the slots are minted every morning and judged every morning,
# so a day that produced two automations reads as two delivered and one missed
# rather than as a rounded-up success.
AUTOMATION_SLOTS_PER_DAY = 3

# ``carried`` is minted ONLY by ``commitments.resolve_day`` (the next-day
# follow-up for a miss). Nothing else may write it — see the PATCH rules in
# :mod:`omniagentos.team.commitments`.
COMMITMENT_STATUSES: tuple[str, ...] = ("committed", "delivered", "missed", "carried")

# The OPEN states — the rows a morning resolution still has to judge.
# ``carried`` is in here deliberately: it records PROVENANCE ("this slipped from
# yesterday"), not an outcome. The person still owes the work, so a carried row
# resolves 'delivered' or 'missed' on its own day exactly like any other
# commitment — and a miss carries again, chaining through ``carried_from``.
# Treating carried as terminal left carried work permanently unjudged, which is
# precisely the hole this table exists to close.
OPEN_COMMITMENT_STATUSES: tuple[str, ...] = ("committed", "carried")

COMMITMENT_SOURCES: tuple[str, ...] = ("auto", "operator", "self")

# The derived completion state of a DONE card (``TeamStore.completion_state``).
# A non-done card has no completion state at all — None, not 'unverified'.
COMPLETION_STATES: tuple[str, ...] = ("verified", "failed_verification", "unverified")

# The company whose goal the standing improvement slot is measured against:
# "one significant OmniAgentOS improvement" is a claim about THIS system.
IMPROVEMENT_COMPANY_SLUG = "omniagentos"

# --- the automation backlog (the operator's GO, 2026-08-14) -------------------------

# The long-term goal every automation category hangs under: "Automate 100% of
# the operator's tasks". A LIVE-DATA ID, not a schema constant — the row lives in
# ``company_goals`` on the production database and this build creates no
# migration for it. Category goals are its ``short_term`` children, titled
# ``Automations — <name>``; nothing here assumes any particular set of them,
# so a category added in the API tomorrow resolves without a code change.
# A database that does not have this goal (a fresh test fixture) simply
# resolves every category to None, which the callers treat as "no category".
AUTOMATION_PARENT_GOAL_ID = "cgl_45d2ac0cfda14960ab75"

# The title prefix that marks a goal as an automation CATEGORY. Everything
# after it is the category name a ``#token`` matches against.
AUTOMATION_CATEGORY_PREFIX = "Automations — "

# ``board_tasks.source`` for a card created by ``/task propose``. Together with
# ``status='awaiting_approval'`` this is the INTERIM carrier of the proposal
# state — deliberately not a new column. The ratified 2026-08-13 workqueue plan
# owns the real ``approval_state`` machinery (its WP1/WP3); this pair upgrades
# into it cleanly, and shipping a mirror of that state now would mean two
# sources of truth for the same question. Defined in ``collab.contracts``
# (the collab store enforces the immutability rules on it and cannot import
# this package) and re-exported here, where every team reader already looks.
AUTOMATION_PROPOSAL_SOURCE = _AUTOMATION_PROPOSAL_SOURCE

# Who a proposal may be aimed at. 'ai' is not an employee: it routes the
# approved card to the compute pool (org_json.dispatch.target) instead of to a
# person, which is why it cannot simply be an id from the roster.
PROPOSAL_ASSIGNEE_HINTS: tuple[str, ...] = ("owner", "alice", "bob", "ai")

# At most four task commitments per person per day. A list longer than a day is
# not a commitment, it is a backlog with a date on it.
COMMITMENT_TASK_CAP = 4

# The one line every Slack surface and doc puts the day's numbers UNDER. Kept
# here, exported once, because a north star that is retyped per renderer is a
# north star that drifts — and a goal three surfaces state differently is three
# goals. Under 60 characters so it survives a Slack header unwrapped.
NORTH_STAR = "🎯 100% of the operator's tasks automated · 10× verified dev speed"

# The operator. The one identity allowed to verify a card they own, because the
# alternative — an operator working alone with nobody to counter-sign — is a
# card that can never be verified at all. Defined in ``collab.contracts`` (the
# collab store enforces the same exemption on re-sizing and cannot import this
# package) and re-exported here, where every team reader already looks.
OPERATOR_EMPLOYEE_ID = _OPERATOR_EMPLOYEE_ID

# The Work-vs-Tasks discriminator (the operator's ruling 2026-08-13, v4 addendum).
# Stamped on ``board_tasks.source`` at creation by BOTH ad-hoc paths —
# ``/task assign @name <free title>`` and the bare ``task`` verb — and by
# nothing else. A card carrying it is a minor ad-hoc **Task**: worth ZERO
# points, excluded from scoring at the card-gathering stage, and rendered
# ABOVE Work with its deadline front-and-center. Everything else (existing
# cards, ``/task add``, imports, agent cards) is **Work** by default — no
# backfill needed, because migration 123 pins ``source NOT NULL DEFAULT ''``.
# Defined in ``collab.contracts`` (the PATCH guard enforces it there and the
# dependency runs collab -> team); re-exported here for the team package.
TASK_ADHOC_SOURCE = _TASK_ADHOC_SOURCE

# A person with fewer than this many Ready cards is about to run dry; the queue
# view flags it so the shortage is visible BEFORE they go idle.
READY_QUEUE_FLOOR = 5

# A person can carry at most four claimed/in-progress cards before the queue
# view asks for capacity. Kept separate from READY_QUEUE_FLOOR because ready
# depth remains the production-report grooming signal.
ACTIVE_QUEUE_FLOOR = 5

# The shared pool should carry enough queue-ready work for two active-capacity
# windows, while its high-frequency board payload stays bounded.
POOL_DEPTH_FLOOR = 10
POOL_CARD_LIMIT = 50


class TaskEvidence(BaseModel):
    """One artifact attributed (or not yet attributed) to a card."""

    id: str = Field(default_factory=lambda: new_id(EVIDENCE_ID_PREFIX))
    task_id: str | None = None  # None = unattributed, awaiting an operator
    kind: str
    ref: str  # sha | PR number | session id | url
    repo: str = ""
    actor: str = ""
    title: str = ""
    attribution: str = "deterministic"
    confidence: float = 1.0
    quality_gate: str = "pass"
    meta: dict[str, Any] = Field(default_factory=dict)
    created_at: str = Field(default_factory=utc_now_iso)

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> TaskEvidence:
        return cls.model_validate(_with_meta(row))


class TaskEvent(BaseModel):
    """One row of a card's append-only audit trail."""

    id: str = Field(default_factory=lambda: new_id(EVENT_ID_PREFIX))
    task_id: str
    actor: str
    event: str
    from_status: str | None = None
    to_status: str | None = None
    note: str = ""
    created_at: str = Field(default_factory=utc_now_iso)

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> TaskEvent:
        return cls.model_validate(dict(row))


class QueueCard(BaseModel):
    """The card as a queue column renders it — never the whole row.

    ``company_slug``/``company_name`` arrive through the goal join
    (``goal_id -> company_goals -> org_companies``) and are ``None`` when the
    card has no goal (or the goal's company row is gone) — server truth, so no
    client ever re-derives a company from ids. ``owner_employee_id`` names the
    accountable person on the card face; the pool and the "Agents & unowned"
    bucket are the surfaces that render it.
    """

    id: str
    title: str
    ref: str | None = None
    status: str
    size: str = "M"
    priority: str = "normal"
    owner_employee_id: str | None = None
    company_slug: str | None = None
    company_name: str | None = None
    # v4 Work-vs-Tasks (2026-08-13): ``source`` carries the discriminator
    # (:data:`TASK_ADHOC_SOURCE`) so renderers can split Tasks from Work
    # without a second read, and ``due_date`` replaces notify's direct
    # ``_due_dates`` SELECT — the queue projection is the deadline authority.
    source: str | None = None
    due_date: str | None = None

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> QueueCard:
        # ``priority`` — and every 2026-08-13 additive field — is read with
        # .get(): rows from older SELECTs that never projected the column must
        # still render as ordinary cards.
        return cls(
            id=str(row["id"]),
            title=str(row["title"]),
            ref=None if row["ref"] is None else str(row["ref"]),
            status=str(row["status"]),
            size=str(row["size"] or "M"),
            priority=str(row.get("priority") or "normal"),
            owner_employee_id=(
                None if row.get("owner_employee_id") is None else str(row["owner_employee_id"])
            ),
            company_slug=(None if row.get("company_slug") is None else str(row["company_slug"])),
            company_name=(None if row.get("company_name") is None else str(row["company_name"])),
            source=(str(row["source"]) if row.get("source") else None),
            due_date=(str(row["due_date"]) if row.get("due_date") else None),
        )


class TeamQueueBuckets(BaseModel):
    """One person's board, bucketed.

    ``counts`` is derived from the lists rather than counted separately: two
    numbers computed by two queries are two numbers that can disagree.
    """

    employee_id: str
    ready: list[QueueCard] = Field(default_factory=list)
    active: list[QueueCard] = Field(default_factory=list)
    blocked: list[QueueCard] = Field(default_factory=list)
    review: list[QueueCard] = Field(default_factory=list)
    done_today: list[QueueCard] = Field(default_factory=list)

    @property
    def counts(self) -> dict[str, int]:
        return {
            "ready": len(self.ready),
            "active": len(self.active),
            "blocked": len(self.blocked),
            "review": len(self.review),
            "done_today": len(self.done_today),
        }

    @property
    def ready_below_5(self) -> bool:
        """True when this person is about to run out of startable work."""
        return len(self.ready) < READY_QUEUE_FLOOR

    @property
    def active_below_5(self) -> bool:
        """True while this person has capacity for another active card."""
        return len(self.active) < ACTIVE_QUEUE_FLOOR

    def model_dump_with_counts(self) -> dict[str, Any]:
        """Serialization for the API: the buckets plus the two derived fields."""
        payload = self.model_dump()
        payload["counts"] = self.counts
        payload["ready_below_5"] = self.ready_below_5
        payload["active_below_5"] = self.active_below_5
        return payload


class ProdSnapshot(BaseModel):
    """One immutable (day, employee) productivity row.

    The nullable fields are the ones that are genuinely UNMEASURABLE on a day
    with no sessions. They stay None rather than collapsing to a favourable 0 —
    "we did not measure" and "we measured zero" are different answers.
    """

    day: str  # YYYY-MM-DD (UTC)
    employee_id: str
    verified_points: int = 0
    verified_outcomes: int = 0
    avg_active_sessions: float | None = None
    peak_sessions: int | None = None
    merged_prs: int | None = None
    first_pass_rate: float | None = None
    production_x: float | None = None
    breakdown: dict[str, Any] = Field(default_factory=dict)

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> ProdSnapshot:
        out = dict(row)
        out["breakdown"] = _parse_json_object(out.pop("breakdown_json", "{}"))
        return cls.model_validate(out)


def _parse_json_object(raw: Any) -> dict[str, Any]:
    """A JSON object column as a dict; anything unparseable reads as ``{}``."""
    if isinstance(raw, dict):
        return dict(raw)
    if raw is None or raw == "":
        return {}
    try:
        parsed = json.loads(str(raw))
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _with_meta(row: dict[str, Any]) -> dict[str, Any]:
    out = dict(row)
    out["meta"] = _parse_json_object(out.pop("meta_json", "{}"))
    return out
