"""The nightly self-repair and self-learning reflection loop package."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from omniagentos.reflection.contracts import ReflectionEvidence


def harvest_evidence(date_str: str | None = None) -> ReflectionEvidence:
    """Lazily import and run the mechanical evidence harvest."""
    from omniagentos.reflection.harvest import harvest_evidence as _harvest

    return _harvest(date_str)
