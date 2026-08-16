"""Bridge from a curated comms row into the Synapse knowledge graph (Tier 2).

This is the ONLY place in the comms package that may touch an LLM or the knowledge
subsystem, and it runs exclusively from the offline batch job
(:mod:`omniagentos.comms.extract_batch`) — never from the inbound webhook request
path. The message body is attacker-controlled, so it is delimited with
``quote_untrusted`` before it can reach an extraction prompt; regardless of what an
extractor returns, ``ingest_episode`` writes on the low-privilege ``knowledge_agent``
role at ``trust=0.3``, which the database triggers unconditionally quarantine and
which can never self-promote (see ``omniagentos.knowledge.consolidate``).
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

from omniagentos.knowledge.config import dsn as knowledge_dsn
from omniagentos.knowledge.config import knowledge_enabled
from omniagentos.knowledge.contracts import Extractor
from omniagentos.knowledge.embeddings import OllamaEmbedding
from omniagentos.knowledge.ingest import ingest_episode
from omniagentos.knowledge.store import KnowledgeStore
from omniagentos.steward.quoting import quote_untrusted

__all__ = ["extract_message"]

logger = logging.getLogger(__name__)

_disabled_logged = False


def _existing_episode_id(store: KnowledgeStore, source_ref: str) -> int | None:
    """Look up an already-ingested episode for this ``source_ref``.

    Guards against a DUPLICATE episode if the batch process crashes between a
    successful ``ingest_episode`` and the follow-up ``set_message_kb`` call
    (M1): on the next run the row is still ``none``/``selected``/``failed``
    (never marked ``extracted``), but the knowledge-graph episode may already
    exist. ``KnowledgeStore`` has no public lookup by ``source_ref`` (see
    ``omniagentos/knowledge/vault_sync.py``'s identical ``_previous_episode``
    helper), so this queries directly via the same lock/connection pattern. A
    minimal test double without that private surface (e.g. the in-memory
    double in ``tests/comms/test_extract_batch.py``) is treated as having no
    prior episode — there is nothing to dedupe against in a unit test that
    never persists across runs anyway.
    """
    try:
        lock = store._lock  # noqa: SLF001 - store has no public source_ref lookup
        conn = store._conn  # noqa: SLF001
    except AttributeError:
        return None
    with lock, conn().cursor() as cur:  # noqa: SLF001
        cur.execute(
            "SELECT id FROM episodes WHERE source_ref = %s ORDER BY id DESC LIMIT 1",
            (source_ref,),
        )
        row = cur.fetchone()
    return int(row[0]) if row else None


def _default_store() -> KnowledgeStore | None:
    global _disabled_logged
    if not knowledge_enabled():
        if not _disabled_logged:
            logger.info(
                "Knowledge subsystem disabled (OMNIAGENTOS_KNOWLEDGE unset); "
                "comms extraction is a no-op"
            )
            _disabled_logged = True
        return None
    try:
        return KnowledgeStore(dsn=knowledge_dsn(), embedder=OllamaEmbedding())
    except Exception as exc:  # pragma: no cover - defensive; store construction rarely raises
        logger.warning("Knowledge store unavailable for comms extraction: %s", exc)
        return None


def extract_message(
    row: Mapping[str, Any],
    *,
    discipline: str | None,
    extractor: Extractor | None = None,
    store: KnowledgeStore | None = None,
) -> int | None:
    """Ingest one curated comms row as a quarantined knowledge episode.

    Returns the new episode id, or ``None`` when knowledge is disabled/unavailable or
    the ingest raised for ANY reason — this is a batch job and must never crash the
    caller over one poisoned or malformed message.
    """
    knowledge_store = store if store is not None else _default_store()
    if knowledge_store is None:
        return None

    source = str(row.get("source") or "")
    sender = str(row.get("sender") or "")
    sent_at = str(row.get("sent_at") or "")
    subject = str(row.get("subject") or "")
    body_text = str(row.get("body_text") or "")
    external_id = str(row.get("external_id") or "")

    # EVERY field here is attacker-controlled — an email's From/Subject are as forgeable
    # as its body — and this content string is what an LLM extractor (ingest_episode's
    # default) sees. Delimit the WHOLE transcript so no attacker field (sender/subject as
    # well as body) can escape the untrusted block into the extraction prompt, per
    # omniagentos/steward/quoting.py. (Graft from compete arm p2-comms-a, finding CMP-002.)
    transcript = f"[{source}] from {sender} at {sent_at}: {subject}\n{body_text}"
    content = quote_untrusted(transcript, source=f"comms:{source}")
    source_ref = f"comms:{source}:{external_id}"

    try:
        # M1 (crash-atomicity): a message can already carry its own episode_id
        # (a cheap, in-row check), or an episode with this exact source_ref may
        # already exist in the knowledge graph from a run that crashed after
        # ingest_episode but before set_message_kb recorded it. Either way,
        # skip re-ingesting — source_ref is unique per message, so this is a
        # safe, idempotent no-op rather than a duplicate episode.
        existing_episode_id = row.get("episode_id")
        if isinstance(existing_episode_id, int):
            return existing_episode_id
        found_episode_id = _existing_episode_id(knowledge_store, source_ref)
        if found_episode_id is not None:
            return found_episode_id

        result = ingest_episode(
            knowledge_store,
            source="chat",
            content=content,
            source_ref=source_ref,
            discipline=discipline,
            extractor=extractor,
            trust=0.3,
        )
        return result.episode_id
    except Exception:
        logger.exception("Comms knowledge extraction failed for message id=%s", row.get("id"))
        return None
