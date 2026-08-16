"""Frozen collaboration domain — the agent task board + messaging + conversation log.

The multi-agent coordination substrate the operator asked for: a shared to-do board
agents READ and SELF-ASSIGN from based on their expertise, direct + group chat between
agents, and a persisted, human-readable log of every conversation. Builds on H1
(new_id, utc_now_iso) + additive migration 004. Owned by the lead; collab packages
import from here and must not edit it.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field

from omniagentos.contracts import new_id, utc_now_iso

# The seeded pre-launch week (Team Work OS / migration 123). Cards carrying this
# ``source`` are a RECORD OF A PRIOR PERIOD and the denominator of every
# ``production_x`` ratio, so the write paths refuse to let a PATCH move one.
# Defined HERE rather than in ``omniagentos.team.scoring`` because
# ``collab.store`` enforces the rule and must not import the team package (the
# dependency runs the other way); ``scoring`` re-exports this name.
BASELINE_SOURCE = "baseline-2026-08-03"

# The ad-hoc **Task** discriminator (multi-company Work OS v4, 2026-08-13).
# Stamped at creation by the two ad-hoc Slack paths; a card carrying it earns
# ZERO points. Defined HERE for the same layering reason as BASELINE_SOURCE:
# ``collab.store`` refuses PATCHes that launder a card across the Work/Tasks
# boundary, and must not import the team package. ``team.contracts`` re-exports.
TASK_ADHOC_SOURCE = "task-adhoc"

# The operator. The one identity allowed to verify a card they own (they have
# nobody to counter-sign with) and to re-size a card that is already verified.
# Same placement rationale as BASELINE_SOURCE; ``team.contracts`` re-exports it.
OPERATOR_EMPLOYEE_ID = "emp_owner"

# ``board_tasks.source`` for a card created by ``/task propose`` (the automation
# backlog, the operator's GO 2026-08-14). Together with ``status='awaiting_approval'``
# it is the interim carrier of proposal state — no ``approval_state`` column.
#
# Defined HERE, with BASELINE_SOURCE and TASK_ADHOC_SOURCE, for the same
# layering reason: ``collab.store`` enforces two rules on it (the source is
# immutable, and a generic PATCH may not move it out of ``awaiting_approval``)
# and must not import the team package. ``team.contracts`` re-exports it.
AUTOMATION_PROPOSAL_SOURCE = "automation-proposal"

# Card priority, escalation order. Migration 004 left the column CHECK-free, so
# this tuple is the one seam the PATCH path validates against — an unknown
# priority used to write straight through, and the queue rank then filed it
# LAST (the store's CASE ranks unknown text after 'low'), which silently
# demoted the card its author thought they had escalated. Defined HERE for the
# same layering reason as BASELINE_SOURCE (``collab.store`` enforces it and
# must not import the team package).
TASK_PRIORITIES: tuple[str, ...] = ("low", "normal", "high", "urgent")

# How much of a card's work the SYSTEM did (migration 132, spec §9), from "a
# person did all of it" to "the system did it and proved it". NULL on a card
# means UNTRACKED — never 'human', because an unmeasured card and a hand-done
# card are different answers. Migration 132 leaves the column CHECK-free (the
# board's mutable columns deliberately are), so like TASK_PRIORITIES this tuple
# is the seam ``CollabStore.update_board_task`` validates a PATCH against, and
# it is defined HERE for the same layering reason: the collab store enforces it
# and must not import the team package (``team.contracts`` re-exports it).
AUTOMATION_MATURITY: tuple[str, ...] = (
    "human",
    "assisted",
    "partially_automated",
    "autonomous",
    "autonomous_verified",
)


class AgentStatus(StrEnum):
    IDLE = "idle"
    BUSY = "busy"
    OFFLINE = "offline"


class BoardTaskStatus(StrEnum):
    PENDING = "pending"  # plan drafted; waiting for an operator to promote it
    OPEN = "open"  # unclaimed — any agent whose expertise matches may claim
    CLAIMED = "claimed"
    IN_PROGRESS = "in_progress"
    AWAITING_APPROVAL = "awaiting_approval"  # live, but cannot advance until a human decides
    BLOCKED = "blocked"
    DONE = "done"
    CANCELLED = "cancelled"


class ChannelKind(StrEnum):
    DIRECT = "direct"  # 1:1 between two agents
    GROUP = "group"  # a named group chat
    BROADCAST = "broadcast"  # system/all


class MessageKind(StrEnum):
    MESSAGE = "message"
    HANDOFF = "handoff"  # "I'm passing task X to you"
    STATUS = "status"
    CLAIM = "claim"  # board self-assignment announcement
    NOTE = "note"


# ---------------------------------------------------------------------------
# Agent registry — who can work, and what they are good at
# ---------------------------------------------------------------------------


class Agent(BaseModel):
    id: str = Field(default_factory=lambda: new_id("agt"))
    name: str
    lineage: str = ""  # 'claude' | 'cli-codex' | 'cli-grok' | 'mock' | ...
    model: str | None = None
    expertise: list[str] = Field(
        default_factory=list
    )  # discipline/skill tags — how self-assignment matches
    trust_level: str = "T1"  # blueprint trust ladder (T0..T4)
    status: AgentStatus = AgentStatus.IDLE
    created_at: str = Field(default_factory=utc_now_iso)
    updated_at: str = Field(default_factory=utc_now_iso)


# ---------------------------------------------------------------------------
# Task board — agents read OPEN tasks matching their expertise and self-assign
# ---------------------------------------------------------------------------


class BoardTask(BaseModel):
    id: str = Field(default_factory=lambda: new_id("btk"))
    title: str
    description: str = ""
    required_expertise: list[str] = Field(
        default_factory=list
    )  # a claimer must overlap ≥1 (empty = anyone)
    discipline: str | None = None
    priority: str = "normal"  # low | normal | high | urgent
    status: BoardTaskStatus = BoardTaskStatus.OPEN
    claimed_by: str | None = None  # agent id
    claim_version: int = 0  # compare-and-swap: only ONE agent wins a claim race
    result_ref: str | None = None  # run/experiment/note id when done
    # Project scope (migration 087, wired at CREATE by M1). None = unscoped,
    # explicitly: a card whose creator named no project is not silently filed
    # under a default one, because the project axis is what budgets and
    # capability-grant binding (migration 115) key off.
    project_id: str | None = None
    # --- Team Work OS (migration 123) --------------------------------------
    # A card the TEAM shares with the agents. Every field below is optional, so
    # an agent card is exactly what it was before 123: no parent, no owning
    # person, no acceptance criteria — and therefore untouched by the
    # evidence-backed done-gate in ``CollabStore.update_board_task``.
    parent_task_id: str | None = None  # depth is capped at ONE (a parent has no parent)
    goal_id: str | None = None  # company_goals.id — the ladder from work to why
    owner_employee_id: str | None = None  # the PERSON accountable (agents use claimed_by)
    ref: str | None = None  # external handle (PR/issue/doc key); UNIQUE when present
    size: str = "M"  # S | M | L
    acceptance_criteria: str = ""  # non-empty is what makes a card verifiable
    blocked_reason: str = ""  # required to move an OWNED card to blocked
    # Written ONLY by TeamStore.verify_task/unverify_task — never PATCH-able, the
    # same discipline claimed_by has against claim_task.
    verified_at: str | None = None
    verified_by: str | None = None
    due_date: str | None = None
    source: str = ""  # standup | transcript | customer_reply | ...
    created_at: str = Field(default_factory=utc_now_iso)
    updated_at: str = Field(default_factory=utc_now_iso)


def can_claim(task_expertise: list[str], agent_expertise: list[str]) -> bool:
    """An agent may self-assign a board task if the task lists no requirement, or the
    agent's expertise overlaps the task's required expertise by ≥1 tag."""
    if not task_expertise:
        return True
    return bool(set(task_expertise) & set(agent_expertise))


# ---------------------------------------------------------------------------
# Messaging + conversation log — individual + group chat, all persisted
# ---------------------------------------------------------------------------


class Channel(BaseModel):
    id: str = Field(default_factory=lambda: new_id("chn"))
    name: str
    kind: ChannelKind
    members: list[str] = Field(default_factory=list)  # agent ids (DIRECT = exactly 2)
    topic: str = ""
    created_at: str = Field(default_factory=utc_now_iso)


class Message(BaseModel):
    id: str = Field(default_factory=lambda: new_id("msg"))
    channel_id: str
    from_agent: str  # agent id or 'system' | 'operator'
    to_agent: str | None = None  # for DIRECT; None for group
    kind: MessageKind = MessageKind.MESSAGE
    body: str
    ref: str | None = None  # optional linked task/run/experiment id
    ts: str = Field(default_factory=utc_now_iso)


class CollabEvents:
    AGENT_UPDATED = "agent.updated"
    BOARD_UPDATED = "board.updated"
    TASK_CLAIMED = "board.claimed"
    MESSAGE_POSTED = "message.posted"
    CHANNEL_UPDATED = "channel.updated"

    ALL: tuple[str, ...] = (
        AGENT_UPDATED,
        BOARD_UPDATED,
        TASK_CLAIMED,
        MESSAGE_POSTED,
        CHANNEL_UPDATED,
    )


def conversation_digest(messages: list[dict[str, Any]]) -> str:
    """Compact human-readable rendering of a channel's messages for the vault log-book."""
    lines = []
    for m in messages:
        who = m.get("from_agent", "?")
        kind = m.get("kind", "message")
        tag = "" if kind == "message" else f" [{kind}]"
        lines.append(f"- **{who}**{tag}: {m.get('body', '')}")
    return "\n".join(lines)
