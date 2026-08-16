"""Source adapters for the Executive Decision Center.

Each adapter turns a source's raw records into normalized
:class:`~omniagentos.edc.adapters.base.SourceEvent` values the source-agnostic
pipeline consumes. Email is adapter #1; live agent sessions
(:mod:`omniagentos.edc.adapters.sessions`, suggestions-only and flag-gated OFF)
are adapter #2. The boundary itself lives in
:mod:`omniagentos.edc.adapters.base` and is the entire speculative surface for
future sources (slack, billing, agent, …).
"""

from __future__ import annotations

from omniagentos.edc.adapters.base import SourceAdapter, SourceEvent
from omniagentos.edc.adapters.sessions import SessionAdapter, sessions_source_enabled

__all__ = ["SessionAdapter", "SourceAdapter", "SourceEvent", "sessions_source_enabled"]
