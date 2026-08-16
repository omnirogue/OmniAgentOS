"""Batch curation + extraction pass over stored comms rows.

Run as ``python -m omniagentos.comms.extract_batch``. This is the ONLY place
untrusted comms text may reach an LLM extractor (via
:func:`omniagentos.comms.knowledge_bridge.extract_message`); the inbound webhook
path never calls this module. Always exits 0 — including when the knowledge
subsystem is disabled — since a batch job failing loudly would otherwise page an
operator over a config flag rather than a real error.
"""

from __future__ import annotations

import json
from typing import Any

from omniagentos.comms.curate import select_for_extraction
from omniagentos.comms.knowledge_bridge import extract_message
from omniagentos.contracts import default_db_path
from omniagentos.db.store import SqliteStore
from omniagentos.knowledge.contracts import Extractor
from omniagentos.knowledge.store import KnowledgeStore
from omniagentos.steward.config import StewardConfig, load_steward_config
from omniagentos.steward.store import StewardStore

__all__ = ["main", "run_batch"]


def _pending_rows(steward: StewardStore, batch_limit: int) -> list[dict[str, Any]]:
    """Newest-first rows with ``kb_status IN ('none', 'selected', 'failed')``, capped
    at the batch limit.

    ``StewardStore.list_comms_messages`` filters on a single ``kb_status`` value, so
    each status is fetched separately and merged/re-capped here.

    M1 (retry): ``failed`` is intentionally NOT terminal — it re-enters this pool
    just like a fresh row, so a transient extraction failure (LLM provider hiccup,
    knowledge-store outage) gets another attempt on a later run instead of being
    lost forever. There is deliberately no persistent per-message attempt counter:
    ``comms_messages`` has no free column for one, and its schema
    (``omniagentos/steward/store.py``) is owned by a different repair package
    (RA), so no migration is available here. A message whose extraction fails
    deterministically forever will keep re-entering this pool and losing its
    batch slot to newer traffic once the mailbox is busy (rows are newest-first
    and capped at ``batch_limit``) — this is the accepted trade-off named
    explicitly in this repair's own brief ("just re-select 'failed' up to the
    batch each night, which is acceptable") over the strictly worse status quo
    of a permanent dead end for anything that fails even once.
    """
    none_rows = steward.list_comms_messages(kb_status="none", limit=batch_limit)
    selected_rows = steward.list_comms_messages(kb_status="selected", limit=batch_limit)
    failed_rows = steward.list_comms_messages(kb_status="failed", limit=batch_limit)
    merged = sorted(
        none_rows + selected_rows + failed_rows, key=lambda row: int(row["id"]), reverse=True
    )
    return merged[:batch_limit]


def run_batch(
    steward: StewardStore,
    cfg: StewardConfig,
    *,
    extractor: Extractor | None = None,
    knowledge_store: KnowledgeStore | None = None,
) -> dict[str, int]:
    """Curate and extract one batch; returns the summary counters."""
    goals = steward.list_goals("active")
    summary = {"scanned": 0, "extracted": 0, "skipped": 0, "failed": 0}

    for row in _pending_rows(steward, cfg.curation.batch_limit):
        summary["scanned"] += 1
        selected, _reason, discipline = select_for_extraction(row, cfg, goals)
        if not selected:
            steward.set_message_kb(row["id"], kb_status="skipped")
            summary["skipped"] += 1
            continue

        episode_id = extract_message(
            row, discipline=discipline, extractor=extractor, store=knowledge_store
        )
        if episode_id is None:
            steward.set_message_kb(row["id"], kb_status="failed")
            summary["failed"] += 1
        else:
            steward.set_message_kb(row["id"], kb_status="extracted", episode_id=episode_id)
            summary["extracted"] += 1

    return summary


def main(argv: list[str] | None = None) -> int:
    del argv  # No CLI flags today; kept for a stable `python -m` entry point signature.
    steward = StewardStore(SqliteStore(default_db_path()))
    cfg = load_steward_config()
    summary = run_batch(steward, cfg)
    print(json.dumps(summary))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
