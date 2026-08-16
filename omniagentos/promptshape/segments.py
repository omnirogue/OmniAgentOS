"""Ordered prompt segments — the lost-in-the-middle discipline (2307.03172).

A ``Segment`` is a labeled chunk of prompt text tagged with its role: ``stable``
(cacheable prefix content that is identical across every sample/call in a batch —
system instructions, repo maps, file contents), ``bulk`` (volatile, per-call filler
that changes but isn't the thing the model must act on), or ``task`` (the actual
instruction/question, which must sit at the tail where attention is strongest).

``render`` always emits stable segments first (in the order given), then bulk, then
task last, regardless of the order callers pass them in — callers build segments in
whatever order is natural and rely on ``render`` to re-order for attention discipline
and cache-hit determinism. The join is a single fixed separator with no timestamps
or other non-deterministic content, so two calls with the same input segments render
byte-identical output (verified by ``stable_prefix`` for the stable portion alone,
which is what a provider's prompt cache keys on).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

SegmentKind = Literal["stable", "bulk", "task"]

_SEPARATOR = "\n\n"


@dataclass(frozen=True)
class Segment:
    kind: SegmentKind
    label: str
    text: str


def _ordered(segments: Sequence[Segment]) -> list[Segment]:
    stable = [s for s in segments if s.kind == "stable"]
    bulk = [s for s in segments if s.kind == "bulk"]
    task = [s for s in segments if s.kind == "task"]
    return [*stable, *bulk, *task]


def render(segments: Sequence[Segment]) -> str:
    """Render segments to a single prompt string: stable first, bulk, task last."""
    return _SEPARATOR.join(segment.text for segment in _ordered(segments))


def stable_prefix(segments: Sequence[Segment]) -> str:
    """The rendered string for JUST the stable segments, in input order.

    This is exactly the leading substring of ``render(segments)`` whenever at least
    one bulk or task segment follows (the separator makes it a true prefix), so
    callers can assert byte-identical prefixes across N calls to confirm a
    provider's prompt cache would hit."""
    stable = [s for s in segments if s.kind == "stable"]
    return _SEPARATOR.join(segment.text for segment in stable)
