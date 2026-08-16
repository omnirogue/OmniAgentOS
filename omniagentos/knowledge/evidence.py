"""System-authored promotion evidence — the only verification path for single-source promotion.

Model-authored text such as ``fact_id=<N>`` in run output is NEVER evidence. Only rows
written by a system/admin path (this module / consolidator / operator) with explicit
provenance, source identity, verifier outcome, and promotion generation may satisfy the
system-verified half of the promotion predicate.

Authoritative source/generation semantics (M-26/M-27)
-----------------------------------------------------
A *generation* is a property of a SOURCE, never of a fact. It identifies the revision of
the source artifact that a verifier looked at. The authoritative current generation of a
source is::

    current_generation(S) = max(
        registry[S],                                   # explicitly advanced
        max(active evidence generation by S, ANY fact) # implied by what S has recorded
    )

This is deliberately NOT fact-local: if S advances (re-runs, re-ingests, or records
evidence at a higher generation for some *other* fact), then S's older evidence for THIS
fact is stale and stops counting. Staleness is fail-closed — a source that advanced
without re-verifying this fact blocks promotion exactly like a FAIL, because we no longer
know whether the claim survived the source's newer revision.

Evidence is *current* iff ``promotion_generation == current_generation(source_identity)``.
Because ``current_generation`` is monotonic, a stale PASS can never become valid again.

Promotion predicate (see ``evaluate_source_groups``):
  - at least one source has a current, valid PASS for the fact, AND
  - every source with promoting evidence for the fact has current evidence, AND
  - every current row of every such source is a valid PASS.

Lineage:
  - status=active: currently counts toward promotion
  - status=revoked: permanently invalid; never counts
  - status=superseded: replaced by a newer evidence row (superseded_by lineage)

Idempotency: re-recording the same (fact_id, source_identity, promotion_generation,
verifier_outcome) while active is a no-op, so verifier reruns are stable.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from threading import RLock
from typing import Any


class VerifierOutcome(StrEnum):
    """Outcome recorded by a system verifier (never agent prose)."""

    PASS = "pass"
    FAIL = "fail"
    INCONCLUSIVE = "inconclusive"


class EvidenceStatus(StrEnum):
    ACTIVE = "active"
    REVOKED = "revoked"
    SUPERSEDED = "superseded"


# The low-privilege agent role. Rows authored by it are never evidence.
AGENT_ROLE = "knowledge_agent"

# Provenance labels that may be written only by system/admin code paths.
SYSTEM_PROVENANCE = frozenset(
    {
        "system_run_verifier",
        "system_operator",
        "system_consolidator",
        "system_lab_eval",
    }
)

# Subset of SYSTEM_PROVENANCE that may SATISFY the single-source promotion predicate.
#
# ``system_consolidator`` is deliberately excluded: the consolidator is the component that
# performs promotion, so letting it author promotion-qualifying evidence would let it
# launder its own decisions. Without this exclusion a fact promoted on the ≥2-source-class
# branch would acquire a consolidator PASS, and an operator demotion could then be undone
# by the next consolidation run via the single-source branch. Consolidator rows are audit
# lineage only: they neither promote nor block.
PROMOTING_PROVENANCE = frozenset(
    {
        "system_run_verifier",
        "system_operator",
        "system_lab_eval",
    }
)

# Provenance written for audit/lineage only — recorded, never predicate-bearing.
AUDIT_PROVENANCE = SYSTEM_PROVENANCE - PROMOTING_PROVENANCE

# Well-known source identities for the production writers. Stable strings, because a
# source identity keys the generation registry — inventing a new one per call would make
# every write look like a brand-new source.
OPERATOR_SOURCE_IDENTITY = "operator_console"
CONSOLIDATOR_SOURCE_IDENTITY = "knowledge_consolidator"

# Agent-reachable / model-authored labels — always rejected.
FORBIDDEN_PROVENANCE = frozenset(
    {
        "agent",
        "model",
        "run_output",
        "agent_reflection",
        "model_authored",
        "fact_id_marker",
    }
)


@dataclass(frozen=True)
class PromotionEvidence:
    """One system-authored verification row."""

    id: int
    fact_id: int
    provenance: str
    source_identity: str
    verifier_outcome: VerifierOutcome
    promotion_generation: int
    status: EvidenceStatus = EvidenceStatus.ACTIVE
    recorded_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    revoked_at: datetime | None = None
    revoke_reason: str | None = None
    superseded_by: int | None = None
    author_role: str = "system"

    def is_promoting(self) -> bool:
        """True if this row participates in the promotion predicate at all."""
        return (
            self.status == EvidenceStatus.ACTIVE
            and self.provenance in PROMOTING_PROVENANCE
            and self.author_role != AGENT_ROLE
        )

    def is_valid_pass(self) -> bool:
        return self.is_promoting() and self.verifier_outcome == VerifierOutcome.PASS


@dataclass(frozen=True)
class SourceEvidenceGroup:
    """One source's promoting evidence for a single fact, normalized for adjudication.

    ``rows_at_current`` holds only the source's rows whose generation equals
    ``current_generation``. An empty tuple means the source advanced past everything it
    recorded for this fact (stale) and must fail closed.
    """

    source_identity: str
    current_generation: int
    rows_at_current: tuple[PromotionEvidence, ...]


def evaluate_source_groups(groups: Sequence[SourceEvidenceGroup]) -> bool:
    """Single authority for the system-verified predicate, shared by memory and PostgreSQL.

    Both backends normalize their storage into ``SourceEvidenceGroup`` and call this, so
    the two paths cannot drift. Returns True iff at least one source contributes a current
    valid PASS and no source is stale or non-passing.
    """
    if not groups:
        return False
    any_pass = False
    for group in groups:
        if not group.rows_at_current:
            # Source advanced to a newer generation without evidence for this fact.
            return False
        for row in group.rows_at_current:
            if row.promotion_generation != group.current_generation:
                return False  # Stale row leaked into the current set.
            if not row.is_valid_pass():
                return False  # FAIL / INCONCLUSIVE / bad provenance / agent author.
            any_pass = True
    return any_pass


def build_source_groups(
    rows: Iterable[PromotionEvidence],
    current_generation: dict[str, int],
) -> list[SourceEvidenceGroup]:
    """Group active promoting rows for one fact by source, keeping only current rows."""
    by_source: dict[str, list[PromotionEvidence]] = {}
    for row in rows:
        if not row.is_promoting():
            continue  # Audit rows and revoked/superseded rows never participate.
        by_source.setdefault(row.source_identity, []).append(row)

    groups: list[SourceEvidenceGroup] = []
    for source_identity in sorted(by_source):
        gen = current_generation.get(source_identity, 0)
        current = tuple(
            sorted(
                (r for r in by_source[source_identity] if r.promotion_generation == gen),
                key=lambda r: r.id,
            )
        )
        groups.append(
            SourceEvidenceGroup(
                source_identity=source_identity,
                current_generation=gen,
                rows_at_current=current,
            )
        )
    return groups


class EvidenceError(Exception):
    """Raised when evidence cannot be recorded or is forged."""


class EvidenceLedger:
    """In-memory evidence ledger (tests + process-local system writers).

    Production PostgreSQL backing is optional via KnowledgeStore methods that mirror
    this API; consolidator accepts any object implementing the same methods.
    """

    def __init__(self) -> None:
        self._lock = RLock()
        self._rows: dict[int, PromotionEvidence] = {}
        self._next_id = 1
        # Explicitly advanced per-SOURCE generation (monotonic). Never keyed by fact.
        self._source_generation: dict[str, int] = {}

    def _current_source_generation_locked(self, source_identity: str) -> int:
        """Authoritative generation: max(explicit registry, highest recorded, across facts)."""
        gen = self._source_generation.get(source_identity, 0)
        for row in self._rows.values():
            if (
                row.source_identity == source_identity
                and row.status == EvidenceStatus.ACTIVE
                and row.promotion_generation > gen
            ):
                gen = row.promotion_generation
        return gen

    def current_source_generation(self, source_identity: str) -> int:
        """Current authoritative generation of a source (0 if the source is unknown)."""
        with self._lock:
            return self._current_source_generation_locked(source_identity)

    def advance_source_generation(self, source_identity: str) -> int:
        """Advance a source to its next generation WITHOUT recording evidence.

        Every prior PASS from this source immediately stops counting, for every fact, until
        the source re-verifies at the new generation.

        RESIDUAL — no production caller. Nothing outside tests calls this today, so
        staleness is only ever triggered implicitly, by ``record()`` writing a generation
        above the source's high-water mark (the promote route's path). An ingest/re-run path
        that learns a source artifact changed but records no new evidence is the caller this
        is FOR, and it does not exist yet; until it does, a silently mutated source keeps
        counting as verified. Wiring that in means deciding what counts as "the artifact
        changed" across ingest, which is a different change than this one — it is left
        stated rather than implied by an unqualified docstring.
        """
        if not source_identity or not str(source_identity).strip():
            raise EvidenceError("source_identity is required")
        with self._lock:
            gen = self._current_source_generation_locked(source_identity) + 1
            self._source_generation[source_identity] = gen
            return gen

    def record(
        self,
        *,
        fact_id: int,
        provenance: str,
        source_identity: str,
        verifier_outcome: str | VerifierOutcome,
        promotion_generation: int | None = None,
        author_role: str = "system",
        recorded_at: datetime | None = None,
    ) -> PromotionEvidence:
        """Record system-authored evidence. Rejects agent/model provenance and agent role.

        ``promotion_generation`` defaults to the source's current authoritative generation
        (at least 1) — i.e. "this verifier looked at the source as it stands now". Passing
        a higher generation explicitly ADVANCES the source, which is what makes a source's
        older evidence for other facts stale.
        """
        if author_role == AGENT_ROLE:
            raise EvidenceError("knowledge_agent cannot author promotion evidence")
        if provenance in FORBIDDEN_PROVENANCE or provenance not in SYSTEM_PROVENANCE:
            raise EvidenceError(
                f"provenance {provenance!r} is not a system-authored evidence class"
            )
        if not source_identity or not str(source_identity).strip():
            raise EvidenceError("source_identity is required")
        outcome = VerifierOutcome(verifier_outcome)
        when = recorded_at or datetime.now(UTC)
        source_identity = str(source_identity)

        with self._lock:
            current = self._current_source_generation_locked(source_identity)
            if promotion_generation is None:
                promotion_generation = max(current, 1)
            if promotion_generation < 1:
                raise EvidenceError("promotion_generation must be >= 1")
            # Recording at generation N asserts the source is at N: raise the high-water
            # mark so older evidence from this source (for ANY fact) becomes stale.
            if promotion_generation > current:
                self._source_generation[source_identity] = promotion_generation

            # Idempotent re-record of the same active pass/fail. The key is EXACTLY the
            # PostgreSQL unique index (migration 006 promotion_evidence_idempotent_idx on
            # fact_id, source_identity, promotion_generation, verifier_outcome WHERE
            # status = 'active') — provenance is deliberately NOT part of it. Matching on
            # provenance too would let this ledger hold a second active row that the
            # database physically cannot, so the same history replayed through the two
            # backends would end with different rows. The collision is fail-closed either
            # way: an existing audit row absorbs a later promoting write, never the
            # reverse, so nothing is promoted that the database would not promote.
            for row in self._rows.values():
                if (
                    row.fact_id == fact_id
                    and row.source_identity == source_identity
                    and row.promotion_generation == promotion_generation
                    and row.verifier_outcome == outcome
                    and row.status == EvidenceStatus.ACTIVE
                ):
                    return row

            eid = self._next_id
            self._next_id += 1
            row = PromotionEvidence(
                id=eid,
                fact_id=fact_id,
                provenance=provenance,
                source_identity=str(source_identity),
                verifier_outcome=outcome,
                promotion_generation=promotion_generation,
                status=EvidenceStatus.ACTIVE,
                recorded_at=when,
                author_role=author_role,
            )
            self._rows[eid] = row
            return row

    def revoke(
        self,
        evidence_id: int,
        *,
        reason: str,
        when: datetime | None = None,
    ) -> PromotionEvidence:
        with self._lock:
            row = self._rows.get(evidence_id)
            if row is None:
                raise EvidenceError(f"evidence {evidence_id} not found")
            if row.status == EvidenceStatus.REVOKED:
                return row
            updated = PromotionEvidence(
                id=row.id,
                fact_id=row.fact_id,
                provenance=row.provenance,
                source_identity=row.source_identity,
                verifier_outcome=row.verifier_outcome,
                promotion_generation=row.promotion_generation,
                status=EvidenceStatus.REVOKED,
                recorded_at=row.recorded_at,
                revoked_at=when or datetime.now(UTC),
                revoke_reason=reason,
                superseded_by=row.superseded_by,
                author_role=row.author_role,
            )
            self._rows[evidence_id] = updated
            return updated

    def revoke_for_source(self, fact_id: int, source_identity: str, *, reason: str) -> list[int]:
        """Revoke every active row this source recorded for this fact. Returns the ids.

        Used when a decision is withdrawn (e.g. an operator demotes a fact they promoted):
        the evidence that justified the promotion must stop counting, or the next
        consolidation run would silently re-promote on the single-source branch.
        """
        with self._lock:
            ids = [
                r.id
                for r in self._rows.values()
                if r.fact_id == fact_id
                and r.source_identity == source_identity
                and r.status == EvidenceStatus.ACTIVE
            ]
            for eid in ids:
                self.revoke(eid, reason=reason)
            return sorted(ids)

    def supersede(
        self,
        old_evidence_id: int,
        new_evidence: PromotionEvidence,
    ) -> PromotionEvidence:
        """Mark old evidence superseded by a newer row; returns the updated old row.

        RESIDUAL — memory-only writer, and no production caller in either backend. The
        PostgreSQL withdrawal path is revoke-only (``revoke_promotion_evidence_for_source``),
        so ``status='superseded'`` is reachable in the schema (migration 006) but nothing
        writes it there. What must hold regardless is that BOTH backends refuse to count a
        superseded row, which is asserted against the live database in
        ``tests/knowledge/test_source_generation_pg.py::TestEvidenceLineageParity``. A PG
        writer with no caller would be surface without a user, so it is not added here.
        """
        with self._lock:
            old = self._rows.get(old_evidence_id)
            if old is None:
                raise EvidenceError(f"evidence {old_evidence_id} not found")
            if new_evidence.id not in self._rows:
                self._rows[new_evidence.id] = new_evidence
            updated = PromotionEvidence(
                id=old.id,
                fact_id=old.fact_id,
                provenance=old.provenance,
                source_identity=old.source_identity,
                verifier_outcome=old.verifier_outcome,
                promotion_generation=old.promotion_generation,
                status=EvidenceStatus.SUPERSEDED,
                recorded_at=old.recorded_at,
                revoked_at=old.revoked_at,
                revoke_reason=old.revoke_reason,
                superseded_by=new_evidence.id,
                author_role=old.author_role,
            )
            self._rows[old_evidence_id] = updated
            return updated

    def list_for_fact(
        self, fact_id: int, *, include_inactive: bool = False
    ) -> list[PromotionEvidence]:
        with self._lock:
            rows = [r for r in self._rows.values() if r.fact_id == fact_id]
            if not include_inactive:
                rows = [r for r in rows if r.status == EvidenceStatus.ACTIVE]
            return sorted(rows, key=lambda r: (r.promotion_generation, r.id))

    def source_groups_for_fact(self, fact_id: int) -> list[SourceEvidenceGroup]:
        """Normalize this fact's promoting evidence into per-source current-generation groups."""
        with self._lock:
            rows = [r for r in self._rows.values() if r.fact_id == fact_id]
            sources = {r.source_identity for r in rows}
            current = {s: self._current_source_generation_locked(s) for s in sources}
            return build_source_groups(rows, current)

    def has_valid_verification(self, fact_id: int) -> bool:
        """True iff every source with promoting evidence for this fact currently PASSes.

        Authoritative and NOT fact-local (M-26/M-27). A source's generation is a property
        of the source across all facts, so:
        - gen1 PASS, source still at gen1 → True
        - gen1 PASS, source advanced to gen2 with a FAIL → False
        - gen1 PASS, source advanced to gen2 with no evidence for this fact → False
        - gen1 PASS that is stale because the source recorded gen2 for another fact → False
        - re-recording the same PASS at the same generation → unchanged (idempotent)
        """
        return evaluate_source_groups(self.source_groups_for_fact(fact_id))

    def latest_generation_for_source(self, fact_id: int, source_identity: str) -> int:
        """Return the highest recorded generation for this fact from this source, or 0.

        Fact-local by definition — diagnostic only. Never use this to decide promotion;
        use ``current_source_generation`` for the authoritative value.
        """
        with self._lock:
            gens = [
                r.promotion_generation
                for r in self._rows.values()
                if r.fact_id == fact_id
                and r.source_identity == source_identity
                and r.status == EvidenceStatus.ACTIVE
            ]
            return max(gens) if gens else 0

    def snapshot(self) -> list[dict[str, Any]]:
        with self._lock:
            return [
                {
                    "id": r.id,
                    "fact_id": r.fact_id,
                    "provenance": r.provenance,
                    "source_identity": r.source_identity,
                    "verifier_outcome": r.verifier_outcome.value,
                    "promotion_generation": r.promotion_generation,
                    "status": r.status.value,
                    "author_role": r.author_role,
                    "superseded_by": r.superseded_by,
                    "revoke_reason": r.revoke_reason,
                }
                for r in sorted(self._rows.values(), key=lambda x: x.id)
            ]
