"""Deterministic Jaccard clustering of episodic events into candidates.

Derived from agentic-stack ``cluster.py`` design (Jaccard over normalised
tokens, threshold 0.3); behaviour modified: a cluster whose canonical
text would be empty is **rejected**, never returned with ``claim=""``.
Contracts already refuse empty claims — inventing filler text to work
around that is the same non-result-as-good defect.

Pure functions only: no I/O, no clock reads. Stable ordering guarantees
byte-identical output for identical inputs (regardless of input order).
"""

from __future__ import annotations

import hashlib
import re
from datetime import UTC, datetime

from omniagentos.memlife.contracts import (
    Candidate,
    CandidateStatus,
    Decision,
    DecisionAction,
    EpisodicEvent,
)

DEFAULT_THRESHOLD = 0.3
_TOKEN_RE = re.compile(r"[a-z0-9]+")
_STAGE_ACTOR = "cluster"


def normalise_tokens(text: str) -> frozenset[str]:
    """Lowercase alphanumeric tokens; empty string → empty set."""
    return frozenset(_TOKEN_RE.findall(text.lower()))


def jaccard(a: frozenset[str], b: frozenset[str]) -> float:
    """Jaccard similarity. Both empty → 1.0 (identical emptiness)."""
    if not a and not b:
        return 1.0
    union = a | b
    if not union:
        return 0.0
    return len(a & b) / len(union)


def canonical_text(event: EpisodicEvent) -> str:
    """Text that would become a candidate claim for this event.

    Prefer non-empty ``reflection``; fall back to ``action``. Whitespace
    alone is empty.
    """
    reflection = (event.reflection or "").strip()
    if reflection:
        return reflection
    return (event.action or "").strip()


def _token_set(event: EpisodicEvent) -> frozenset[str]:
    return normalise_tokens(canonical_text(event))


class _UnionFind:
    def __init__(self, n: int) -> None:
        self._parent = list(range(n))
        self._rank = [0] * n

    def find(self, i: int) -> int:
        while self._parent[i] != i:
            self._parent[i] = self._parent[self._parent[i]]
            i = self._parent[i]
        return i

    def union(self, i: int, j: int) -> None:
        ri, rj = self.find(i), self.find(j)
        if ri == rj:
            return
        if self._rank[ri] < self._rank[rj]:
            self._parent[ri] = rj
        elif self._rank[ri] > self._rank[rj]:
            self._parent[rj] = ri
        else:
            self._parent[rj] = ri
            self._rank[ri] += 1


def _cluster_claim(members: list[EpisodicEvent]) -> str | None:
    """Lexicographically first non-empty canonical text, or None if all empty."""
    texts = sorted({canonical_text(e) for e in members if canonical_text(e)})
    if not texts:
        return None
    return texts[0]


def _cluster_key(skill: str, claim: str) -> str:
    slug_src = " ".join(sorted(normalise_tokens(claim)))
    digest = hashlib.sha256(f"{skill}|{slug_src}".encode()).hexdigest()[:12]
    skill_part = skill.strip() or "unknown"
    return f"{skill_part}/{digest}"


def _cluster_id(key: str, evidence_ids: list[str]) -> str:
    material = key + "|" + ",".join(evidence_ids)
    return "cand_" + hashlib.sha256(material.encode()).hexdigest()[:12]


def _majority_skill(members: list[EpisodicEvent]) -> str:
    """Most frequent skill; ties broken lexicographically for stability."""
    counts: dict[str, int] = {}
    for e in members:
        counts[e.skill] = counts.get(e.skill, 0) + 1
    return sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[0][0]


def _stage_at(members: list[EpisodicEvent]) -> datetime:
    """Newest event timestamp among members (deterministic, no clock)."""
    latest = max(members, key=lambda e: (e.ts, e.id)).ts
    return latest if latest.tzinfo is not None else latest.replace(tzinfo=UTC)


def _to_candidate(members: list[EpisodicEvent]) -> Candidate | None:
    claim = _cluster_claim(members)
    if claim is None:
        # Empty canonical — reject. Do not invent text; do not return claim="".
        return None

    members_sorted = sorted(members, key=lambda e: e.id)
    evidence_ids = [e.id for e in members_sorted]
    skill = _majority_skill(members_sorted)
    key = _cluster_key(skill, claim)
    cand_id = _cluster_id(key, evidence_ids)

    decision = Decision(
        action=DecisionAction.STAGE,
        at=_stage_at(members_sorted),
        actor=_STAGE_ACTOR,
        reason=f"clustered {len(members_sorted)} events",
    )
    return Candidate(
        id=cand_id,
        key=key,
        claim=claim,
        evidence_ids=evidence_ids,
        cluster_size=len(members_sorted),
        status=CandidateStatus.STAGED,
        decisions=[decision],
        salience=None,
    )


def cluster(
    events: list[EpisodicEvent],
    threshold: float = DEFAULT_THRESHOLD,
) -> list[Candidate]:
    """Group events by Jaccard token similarity ≥ ``threshold``.

    Returns a stable-ordered list of ``Candidate``s. Clusters whose
    canonical claim would be empty are omitted (rejected), never emitted
    with a blank claim.
    """
    if not events:
        return []

    # Sort once so union-find component discovery is input-order independent.
    ordered = sorted(events, key=lambda e: e.id)
    n = len(ordered)
    tokens = [_token_set(e) for e in ordered]
    uf = _UnionFind(n)

    for i in range(n):
        for j in range(i + 1, n):
            if jaccard(tokens[i], tokens[j]) >= threshold:
                uf.union(i, j)

    groups: dict[int, list[EpisodicEvent]] = {}
    for i, event in enumerate(ordered):
        root = uf.find(i)
        groups.setdefault(root, []).append(event)

    candidates: list[Candidate] = []
    for members in groups.values():
        cand = _to_candidate(members)
        if cand is not None:
            candidates.append(cand)

    # Stable output order: by key, then id.
    candidates.sort(key=lambda c: (c.key, c.id))
    return candidates


__all__ = [
    "DEFAULT_THRESHOLD",
    "canonical_text",
    "cluster",
    "jaccard",
    "normalise_tokens",
]
