"""Default :data:`KnowledgeRecaller` backed by Synapse + rendered memlife lessons.

Kept behind a lazy import and a never-raising wrapper so the memory layer neither hard-
depends on the knowledge subsystem nor fails assembly when the graph is disabled or
unavailable. Assembly is a preview (``run_id=None``) — it never reinforces the graph;
per-run reinforcement is the runner's own knowledge hook.

Memlife: ACCEPTED lessons rendered to ``var/memories/<project>/LESSONS.md`` are
indexed here via :func:`omniagentos.memlife.render.search_rendered_lessons` so they
surface through the same knowledge-recaller front-door used by context assembly.
"""

from __future__ import annotations


def _memlife_lesson_lines(query: str, top_k: int) -> list[str]:
    """Accepted rendered lessons relevant to ``query``, or ``[]`` on any fault."""
    if not query.strip() or top_k <= 0:
        return []
    try:
        from omniagentos.memlife.render import search_rendered_lessons

        # search_rendered_lessons verifies both the render HMAC and the
        # authoritative accepted row before any lesson reaches recall.
        return list(search_rendered_lessons(query, top_k=top_k))
    except Exception:  # noqa: BLE001 -- recall is best-effort context, never a hard dep.
        return []


def default_knowledge_recaller(query: str, top_k: int) -> list[str]:
    """Return up to ``top_k`` fact/lesson statements relevant to ``query``, or ``[]``.

    Combines (1) rendered memlife ACCEPTED lessons from LESSONS.md and (2) Synapse
    knowledge facts when the graph is enabled. Either leg may be empty; faults
    degrade that leg only. Never raises.
    """
    if not query.strip() or top_k <= 0:
        return []

    lines: list[str] = []
    seen: set[str] = set()

    def _add(statement: str) -> None:
        text = " ".join(str(statement).split())
        if not text:
            return
        key = text.casefold()
        if key in seen:
            return
        seen.add(key)
        lines.append(text)

    # Memlife lessons first: local disk, no Postgres, accepted-only by construction.
    for claim in _memlife_lesson_lines(query, top_k):
        _add(claim)
        if len(lines) >= top_k:
            return lines[:top_k]

    try:
        from omniagentos.knowledge import config as knowledge_config

        if knowledge_config.knowledge_enabled():
            from omniagentos.knowledge.recall import _get_store, recall

            result = recall(_get_store(), prompt=query, run_id=None)
            for item in result.facts:
                statement = getattr(getattr(item, "fact", None), "statement", None)
                if statement is None:
                    statement = getattr(item, "statement", "")
                _add(str(statement))
                if len(lines) >= top_k:
                    break
    except Exception:  # noqa: BLE001 -- recall is best-effort context, never a hard dep.
        pass

    return lines[:top_k]


__all__ = ["default_knowledge_recaller"]
