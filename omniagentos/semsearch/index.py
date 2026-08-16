"""Corpus enumeration and incremental semantic indexing."""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass

from omniagentos.connectors import load_registry
from omniagentos.knowledge import config as knowledge_config
from omniagentos.knowledge.contracts import EmbeddingProvider
from omniagentos.knowledge.embeddings import FakeEmbedding, OllamaEmbedding
from omniagentos.skills import list_skills
from omniagentos.toolplane.catalog import default_catalog

from .store import (
    ALL_KINDS,
    EmbeddedDocument,
    SemKind,
    SemsearchStore,
    content_hash,
)

_STATUS_MATCHABLE = frozenset({"active", "deprecated", "experimental"})
INDEX_BATCH_SIZE = 64
SKILL_SCAN_LIMIT = 10_000


@dataclass(frozen=True, slots=True)
class CorpusDocument:
    """Text and presentation metadata extracted from one source corpus."""

    kind: SemKind
    ref_id: str
    title: str
    text: str


@dataclass(frozen=True, slots=True)
class IndexStats:
    """Incremental-index work performed for one corpus kind."""

    scanned: int
    embedded: int
    skipped: int


class CorpusDocuments(list[CorpusDocument]):
    """Corpus rows plus whether the loader exhausted its authoritative source."""

    def __init__(self, documents: Iterable[CorpusDocument], *, complete: bool = True) -> None:
        super().__init__(documents)
        self.complete = complete


CorpusLoader = Callable[[], Iterable[CorpusDocument]]


def build_embedder() -> EmbeddingProvider:
    """Select the existing knowledge embedder from its shared environment flag."""
    if knowledge_config.embed_spec() == "fake":
        return FakeEmbedding()
    return OllamaEmbedding()


def skill_documents() -> CorpusDocuments:
    """Enumerate current versions of every servable skill."""
    documents: list[CorpusDocument] = []
    rows = list_skills(include_archived=True, limit=SKILL_SCAN_LIMIT + 1)
    complete = len(rows) <= SKILL_SCAN_LIMIT
    for row in rows[:SKILL_SCAN_LIMIT]:
        if str(row.get("status") or "").strip().lower() not in _STATUS_MATCHABLE:
            continue
        title = str(row.get("title") or row.get("name") or row.get("id") or "").strip()
        ref_id = str(row.get("id") or row.get("slug") or row.get("name") or "").strip()
        if not ref_id or not title:
            continue
        summary = str(row.get("summary") or "").strip()
        method = str(row.get("preferred_method") or "").strip()
        text = " ".join(part for part in (title, summary, method) if part)
        documents.append(CorpusDocument("skill", ref_id, title, text))
    return CorpusDocuments(documents, complete=complete)


def tool_documents() -> list[CorpusDocument]:
    """Enumerate the unified tool catalog."""
    return [
        CorpusDocument("tool", entry.id, entry.label, f"{entry.id}: {entry.description}")
        for entry in default_catalog().values()
    ]


def capability_documents() -> list[CorpusDocument]:
    """Enumerate grantable connector capabilities from the reviewed registry."""
    registry = load_registry()
    documents = []
    for ref_id, capability in registry.capabilities.items():
        title = capability.label.strip() or ref_id
        hint = capability.resolved_compact_hint.strip()
        description = title if hint == title else f"{title}. {hint}"
        documents.append(CorpusDocument("capability", ref_id, title, f"{ref_id}: {description}"))
    return documents


DEFAULT_LOADERS: Mapping[SemKind, CorpusLoader] = {
    "skill": skill_documents,
    "tool": tool_documents,
    "capability": capability_documents,
}


def _validated_kinds(kinds: Sequence[str]) -> tuple[SemKind, ...]:
    resolved: list[SemKind] = []
    for kind in kinds:
        if kind not in ALL_KINDS:
            raise ValueError(f"unknown semantic corpus kind: {kind}")
        typed_kind: SemKind = kind  # type: ignore[assignment]
        if typed_kind not in resolved:
            resolved.append(typed_kind)
    return tuple(resolved)


def reindex(
    kinds: Sequence[str] = ALL_KINDS,
    *,
    store: SemsearchStore | None = None,
    embedder: EmbeddingProvider | None = None,
    loaders: Mapping[SemKind, CorpusLoader] | None = None,
) -> dict[SemKind, IndexStats]:
    """Embed changed corpus rows and return per-kind incremental-work counts."""
    selected = _validated_kinds(kinds)
    active_store = store or SemsearchStore()
    active_embedder = embedder or build_embedder()
    active_loaders = loaders or DEFAULT_LOADERS
    summary: dict[SemKind, IndexStats] = {}
    try:
        for kind in selected:
            loaded = active_loaders[kind]()
            enumeration_complete = bool(getattr(loaded, "complete", True))
            documents = sorted(list(loaded), key=lambda document: (document.kind, document.ref_id))
            signatures = active_store.signatures(kind)
            changed = [
                document
                for document in documents
                if signatures.get(document.ref_id)
                != (content_hash(document.text), active_embedder.model_id)
            ]
            embedded = 0
            expected_dimension = active_embedder.dim if changed else 0
            for offset in range(0, len(changed), INDEX_BATCH_SIZE):
                chunk = changed[offset : offset + INDEX_BATCH_SIZE]
                vectors = active_embedder.embed([document.text for document in chunk])
                if len(vectors) != len(chunk):
                    raise RuntimeError(
                        f"embedder returned {len(vectors)} vectors for {len(chunk)} documents"
                    )
                for vector in vectors:
                    if len(vector) != expected_dimension:
                        raise RuntimeError(
                            f"embedder returned dimension {len(vector)}; "
                            f"expected {expected_dimension}"
                        )
                rows = [
                    EmbeddedDocument(
                        kind=document.kind,
                        ref_id=document.ref_id,
                        text=document.text,
                        content_hash=content_hash(document.text),
                        embedding=vector,
                        model_id=active_embedder.model_id,
                    )
                    for document, vector in zip(chunk, vectors, strict=True)
                ]
                active_store.upsert_many(rows)
                embedded += len(chunk)
            if enumeration_complete:
                active_store.delete_refs(
                    kind,
                    set(signatures) - {document.ref_id for document in documents},
                )
            summary[kind] = IndexStats(
                scanned=len(documents),
                embedded=embedded,
                skipped=len(documents) - len(changed),
            )
    finally:
        if store is None:
            active_store.close()
    return summary


def titles_for(kind: SemKind) -> dict[str, str]:
    """Resolve current presentation titles without putting them in vector storage."""
    try:
        return {document.ref_id: document.title for document in DEFAULT_LOADERS[kind]()}
    except Exception:
        return {}
