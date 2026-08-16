"""Default :data:`MemoryRecaller` backed by metacog ``metacog_memory_records``.

Kept behind a lazy import and a never-raising wrapper so the memory layer neither hard-
depends on the metacog subsystem nor fails assembly when metacog is disabled or
unavailable. Lessons injected here reach every agent lineage via the shared context
block — not only the Anthropic memory-tool path.
"""

from __future__ import annotations

from threading import RLock
from typing import Any

_state_lock = RLock()
_metacog_store: Any | None = None


def _get_store() -> Any | None:
    """Lazy-initialize and return the process-wide metacog store.

    Uses module-level caching to pay connection + migration cost only once
    per process, and to skip the never-needed ensure_builtin_strategies()
    which opens a WRITE transaction on every dispatch turn.

    Returns None if initialization fails, for a never-raising recaller.
    """
    global _metacog_store
    try:
        from omniagentos.contracts import default_db_path
        from omniagentos.metacog.store import MetacogStore

        with _state_lock:
            if _metacog_store is None:
                _metacog_store = MetacogStore(database=default_db_path())
            return _metacog_store
    except Exception:  # noqa: BLE001
        return None


def default_memory_recaller(
    query: str,
    top_k: int,
    *,
    task_id: str | None = None,
    run_id: str | None = None,
) -> list[str]:
    """Return up to ``top_k`` rendered metacog memory records relevant to ``query``.

    Returns ``[]`` (never raises) when metacog is disabled or unavailable.
    Queries the memory store and renders statements as trimmed lines,
    preferring records with higher confidence and success ratio.

    When called from a task context, ``task_id`` and ``run_id`` record one
    selected retrieval event per returned memory.  Generic callers can omit both
    context fields and retain the read-only recaller behavior.
    """
    if not query.strip() or top_k <= 0:
        return []
    try:
        from omniagentos.metacog import config as metacog_config

        if metacog_config.metacog_mode() == "off":
            return []

        store = _get_store()
        if store is None:
            return []

        # Grab extra candidates so we can re-rank by confidence + success ratio.
        candidates = store.search_memory(
            query,
            statuses=["promoted"],  # shadow/pending candidates must not bypass the promotion gate
            limit=top_k * 2,
        )
    except Exception:  # noqa: BLE001 -- recall is best-effort context, never a hard dep.
        return []

    def _rank_key(mem: Any) -> float:
        confidence = float(getattr(mem, "confidence", 0.0) or 0.0)
        success_count = int(getattr(mem, "success_count", 0) or 0)
        sample_count = int(getattr(mem, "sample_count", 0) or 0)
        return confidence + (success_count / max(sample_count, 1))

    ranked = sorted(candidates, key=_rank_key, reverse=True)

    lines: list[str] = []
    for rank, mem in enumerate(ranked[:top_k]):
        statement = " ".join(str(getattr(mem, "statement", "") or "").split())
        if statement:
            lines.append(statement)
            if task_id is not None:
                try:
                    store.record_memory_retrieval_event(
                        memory_id=str(mem.id),
                        task_id=task_id,
                        run_id=run_id,
                        query=query,
                        rank=rank,
                    )
                except Exception:  # noqa: BLE001 -- telemetry cannot suppress a lesson.
                    pass
    return lines


__all__ = ["default_memory_recaller"]
