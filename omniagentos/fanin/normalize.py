"""Normalize and dedupe fan-in candidates by content hash."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping
from typing import Any

from omniagentos.fanin.types import Candidate, NormalizedCandidate

__all__ = ["canonical_json", "content_hash", "normalize_candidates"]


def canonical_json(value: Any) -> str:
    """Deterministic JSON for hashing (sorted keys, compact separators)."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str, ensure_ascii=False)


def content_hash(content: Any) -> str:
    """SHA-256 of canonical content JSON."""
    return hashlib.sha256(canonical_json(content).encode("utf-8")).hexdigest()


def _as_candidate(raw: Candidate | Mapping[str, Any]) -> Candidate:
    if isinstance(raw, Candidate):
        if not str(raw.id).strip():
            raise ValueError("candidate id must be non-empty")
        return raw
    if not isinstance(raw, Mapping):
        raise ValueError(f"candidate must be Candidate or mapping, got {type(raw)!r}")
    if "id" not in raw and "ref" not in raw and "artifact_ref" not in raw:
        raise ValueError("candidate mapping requires 'id' (or ref/artifact_ref)")
    if "content" not in raw and "body" not in raw:
        raise ValueError("candidate mapping requires 'content' (or body)")
    cid = str(raw.get("id") or raw.get("ref") or raw.get("artifact_ref") or "").strip()
    if not cid:
        raise ValueError("candidate id must be non-empty")
    content = raw["content"] if "content" in raw else raw["body"]
    score = raw.get("score")
    confidence = raw.get("confidence")
    return Candidate(
        id=cid,
        content=content,
        score=float(score) if isinstance(score, (int, float)) else None,
        confidence=float(confidence) if isinstance(confidence, (int, float)) else None,
        source=str(raw.get("source") or ""),
        metadata=dict(raw.get("metadata") or {}),
    )


def normalize_candidates(
    candidates: Iterable[Candidate | Mapping[str, Any]],
) -> list[NormalizedCandidate]:
    """Schema-check candidates, hash content, drop duplicates by content_hash.

    Hard schema violations (missing id/content) raise ``ValueError``.
    Duplicates (same content hash) keep the first occurrence in input order.
    """
    seen: set[str] = set()
    out: list[NormalizedCandidate] = []
    for raw in candidates:
        cand = _as_candidate(raw)
        digest = content_hash(cand.content)
        if digest in seen:
            continue
        seen.add(digest)
        out.append(
            NormalizedCandidate(
                id=cand.id,
                content=cand.content,
                content_hash=digest,
                score=cand.score,
                confidence=cand.confidence,
                source=cand.source,
                metadata=dict(cand.metadata),
            )
        )
    return out
