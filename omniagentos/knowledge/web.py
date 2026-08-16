"""Governed web ingestion.

Fetched text is persisted exactly as received.  It is inert episode data; this
module does not interpret, execute, or promote anything contained in it.
"""

from __future__ import annotations

from omniagentos.knowledge.contracts import EpisodeSource, IngestResult
from omniagentos.knowledge.ingest import ingest_episode
from omniagentos.knowledge.store import KnowledgeStore


def ingest_web(
    store: KnowledgeStore,
    *,
    url: str,
    title: str,
    content: str,
    discipline: str | None = None,
) -> IngestResult:
    """Ingest raw web content through the agent-routed low-trust write path.

    ``title`` is intentionally metadata-only: changing it must not alter the
    raw episode body that is later recalled as delimited data.
    """
    del title
    return ingest_episode(
        store,
        source=EpisodeSource.WEB,
        content=content,
        source_ref=url,
        discipline=discipline,
    )
