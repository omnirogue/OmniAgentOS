"""Unified trace event model every adapter normalizes into.

The model is deliberately lossy-but-sufficient: metrics and mining only need
the *shape* of a session (who acted, which tool, did it error, how big was the
payload), never the full content. Adapters may attach a short content excerpt
for taxonomy matching and evidence quotes; they must never store full blobs.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum


class EventKind(StrEnum):
    """Normalized vocabulary of things that happen in an agent trace."""

    USER = "user"
    ASSISTANT = "assistant"
    TOOL_CALL = "tool_call"
    TOOL_RESULT = "tool_result"
    SYSTEM = "system"
    META = "meta"


class Outcome(StrEnum):
    """Ground-truth resolution of a trace, when the source records one."""

    SUCCESS = "success"
    FAILURE = "failure"
    UNKNOWN = "unknown"


#: Excerpts are capped so profiles and evidence stay quotable, never blobs.
EXCERPT_MAX_CHARS = 400


@dataclass(slots=True)
class TraceEvent:
    """One normalized step inside a trace."""

    seq: int
    kind: EventKind
    content_chars: int = 0
    excerpt: str = ""
    tool_name: str = ""
    # ``None`` means the source did not record whether this event failed.
    # Treating omission as ``False`` fabricated a successful result and could
    # falsely recover a preceding error episode in downstream metrics.
    is_error: bool | None = None
    model: str = ""
    tokens_in: int = 0
    tokens_out: int = 0
    ts: str = ""

    def __post_init__(self) -> None:
        if len(self.excerpt) > EXCERPT_MAX_CHARS:
            self.excerpt = self.excerpt[:EXCERPT_MAX_CHARS]


@dataclass(slots=True)
class Trace:
    """One agent session/trajectory, normalized."""

    trace_id: str
    dataset: str
    source_path: str
    events: list[TraceEvent] = field(default_factory=list)
    outcome: Outcome = Outcome.UNKNOWN
    outcome_field: str = ""
    meta: dict[str, str] = field(default_factory=dict)

    @property
    def n_events(self) -> int:
        return len(self.events)
