"""Offline sleep-time consolidator — dedup, decay, promotion adjudication, embedding backfill.

This module runs OFFLINE (not during agent runs) with admin-role KnowledgeStore.
It is the ONLY path that may promote facts from quarantined to active status,
subject to the FROZEN predicate (R2-7): ≥2 DISTINCT SOURCE TYPES or 1 system-recorded
verified outcome, AND zero unresolved contradiction flags.

Key invariant: an agent can NEVER author its own promotion or manufacture the
predicate (e.g., via competing run-reflections or forged ``fact_id=<N>`` markers).
System-verified promotion requires system-authored evidence rows (see evidence.py),
not model-authored run output text.
"""

from __future__ import annotations

import logging
from contextlib import nullcontext
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from omniagentos.knowledge.contracts import FactStatus
from omniagentos.knowledge.evidence import CONSOLIDATOR_SOURCE_IDENTITY, EvidenceLedger
from omniagentos.knowledge.promotion import gate

logger = logging.getLogger(__name__)

# The low-privilege agent role. The promotion predicate + migration 002/003 triggers all
# key trust on this exact name (facts/episodes authored by it are never a trust class).
# Single source of truth so the three checks can't drift; any INSERT-capable role that is
# NOT this one is treated as trusted, so keep the DB grant set to {knowledge_agent, admin}.
_AGENT_ROLE = "knowledge_agent"

# Merge threshold: cosine similarity for near-duplicate facts
MERGE_THRESHOLD_SIMILARITY = 0.92

# Decay constants
IMPORTANCE_DECAY_DAYS = 30
WEIGHT_DECAY_DAYS = 60
DECAY_HALF_LIFE_DAYS = 90

# Soft-archive thresholds (invalidate, never delete)
MIN_IMPORTANCE_THRESHOLD = 0.1
MIN_WEIGHT_THRESHOLD = 0.05

# Consolidation outcome status values
STATUS_OK = "ok"
STATUS_PARTIAL = "partial"
STATUS_FAILED = "failed"


def _days_old(recorded_at: datetime, now: datetime) -> float:
    """Calculate days since recorded_at."""
    delta = now - recorded_at
    return delta.total_seconds() / 86400.0


def _decay_multiplier(days_old: float, half_life_days: float = DECAY_HALF_LIFE_DAYS) -> float:
    """Exponential decay: 0.5^(days_old / half_life)."""
    if days_old <= 0:
        return 1.0
    return 0.5 ** (days_old / half_life_days)


def _count_source_types(store: Any, fact_id: int) -> int:
    """Count distinct non-agent trust classes for a fact's corroborators.

    Prefers store.count_non_agent_source_classes (PG + in-memory). Falls back to
    a direct SQL path for older store duck-types.
    """
    if hasattr(store, "count_non_agent_source_classes"):
        try:
            return int(store.count_non_agent_source_classes(fact_id))
        except Exception as e:  # noqa: BLE001
            logger.warning("Failed to count sources for fact %s: %s", fact_id, e)
            return 0

    fact = store.get_fact(fact_id)
    if not fact:
        return 0

    try:
        conn = store._conn()
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT f.author_role, ep.source
                FROM facts f
                JOIN episodes ep ON f.episode_id = ep.id
                WHERE (f.id = %s OR f.statement = %s)
                  AND f.invalid_at IS NULL
                  AND f.capability_scope IS NULL
                """,
                (fact_id, fact.statement),
            )
            classes: set[str] = set()
            for author_role, source in cur.fetchall():
                if author_role != _AGENT_ROLE:
                    classes.add(str(source))
        return len(classes)
    except Exception as e:  # noqa: BLE001
        logger.warning("Failed to count sources for fact %s: %s", fact_id, e)
        return 0


def _has_system_verified_outcome(
    fact_id: int,
    *,
    store: Any | None = None,
    evidence: EvidenceLedger | None = None,
) -> bool:
    """Return True only when system-authored evidence validates the fact.

    NEVER accepts model-authored ``fact_id=<N>`` markers in run output/plan text.
    Evidence must carry provenance, source identity, verifier outcome, and a
    promotion generation, and must not be revoked or superseded.
    """
    if evidence is not None:
        return evidence.has_valid_verification(fact_id)

    # InMemory store carries an attached ledger.
    if store is not None and hasattr(store, "evidence"):
        ledger = getattr(store, "evidence", None)
        if isinstance(ledger, EvidenceLedger):
            return ledger.has_valid_verification(fact_id)

    # PostgreSQL-backed store with promotion_evidence table.
    if store is not None and hasattr(store, "has_valid_promotion_evidence"):
        try:
            return bool(store.has_valid_promotion_evidence(fact_id))
        except Exception as exc:  # noqa: BLE001 — fail closed
            logger.warning("system-verified evidence check failed for fact %s: %s", fact_id, exc)
            return False

    return False


def _record_promotion_audit(store: Any, fact_id: int) -> None:
    """Write the consolidator's own audit row for a promotion it just performed.

    ``system_consolidator`` is in AUDIT_PROVENANCE, not PROMOTING_PROVENANCE, so this row
    is lineage only: it can never satisfy the single-source branch of the predicate on a
    later run. That exclusion is what stops the consolidator from laundering its own
    ≥2-source-class decisions into "system-verified" evidence.

    Non-fatal: a promotion that already happened must not be reported as failed because
    the audit write did not land.
    """
    recorder = getattr(store, "record_promotion_evidence", None)
    if recorder is None:
        return
    try:
        recorder(
            fact_id=fact_id,
            provenance="system_consolidator",
            source_identity=CONSOLIDATOR_SOURCE_IDENTITY,
            verifier_outcome="pass",
        )
    except Exception as e:  # noqa: BLE001
        logger.warning("promotion audit row failed for fact %s: %s", fact_id, e)


def _unresolved_contradiction_peers(store: Any, fact_id: int) -> list[int]:
    """Peer facts with open CONTRADICTS edges that block promotion."""
    if hasattr(store, "list_unresolved_contradiction_peers"):
        try:
            return list(store.list_unresolved_contradiction_peers(fact_id))
        except Exception as e:  # noqa: BLE001
            logger.warning("contradiction peer query failed for fact %s: %s", fact_id, e)
            # Fail closed: treat as unresolved if we cannot check.
            return [-1]

    try:
        peers: set[int] = set()
        for edge in store.neighbors("fact", fact_id, limit=200):
            if str(getattr(edge, "edge_type", "")) != "contradicts":
                continue
            if getattr(edge, "invalid_at", None) is not None:
                continue
            other = edge.dst_id if edge.src_id == fact_id else edge.src_id
            peer = store.get_fact(other)
            if peer is None or peer.invalid_at is not None:
                continue
            status = peer.status.value if hasattr(peer.status, "value") else str(peer.status)
            if status == FactStatus.SUPERSEDED.value:
                continue
            peers.add(int(other))
        return sorted(peers)
    except Exception as e:  # noqa: BLE001
        logger.warning("contradiction peer fallback failed for fact %s: %s", fact_id, e)
        return [-1]


def _check_promotion_predicate(
    store: Any,
    fact_id: int,
    now: datetime,
    *,
    evidence: EvidenceLedger | None = None,
) -> tuple[bool, str]:
    """Check if a fact meets the promotion predicate (R2-7).

    Returns (should_promote, reason).

    Predicate: (≥2 DISTINCT SOURCE TYPES OR 1 system-recorded verified outcome)
    AND zero unresolved contradiction flags.

    Agent-origin corroboration ALONE can NEVER satisfy this: the agent-writable sources
    {run, chat, web} collapse to a SINGLE trust class, so reaching ≥2 distinct classes
    requires at least one admin-only source (human/vault/curator) — which migration 002
    prevents an agent from writing — or a system-authored evidence row with verifier
    outcome PASS. Model-authored ``fact_id=<N>`` text is never evidence.
    """
    # M-26: contradiction gate is mandatory, not an assumed pass.
    peers = _unresolved_contradiction_peers(store, fact_id)
    if peers:
        if peers == [-1]:
            return False, "contradiction check unavailable (fail-closed)"
        return False, f"unresolved contradiction with fact(s) {peers}"

    # Check for ≥2 distinct trust CLASSES (agent-origin sources count as zero).
    source_count = _count_source_types(store, fact_id)
    if source_count >= 2:
        return True, f"≥2 source classes ({source_count})"

    # System-verified outcome via system-authored evidence only.
    if _has_system_verified_outcome(fact_id, store=store, evidence=evidence):
        return True, "system-verified evidence"

    return False, f"insufficient sources (only {source_count} trust class(es), no verified outcome)"


def _adjudicate_contradiction(
    store: Any,
    challenger_id: int,
    incumbent_id: int,
    now: datetime,
) -> tuple[int, str]:
    """Adjudicate a contradiction between two facts.

    Returns (winner_id, reason).

    Asymmetry (R2-10): A quarantined ≤0.6-trust CHALLENGER never defeats an ACTIVE
    incumbent absent independent corroboration or operator confirmation.
    A lost adjudication drops the CHALLENGER, never the incumbent.

    Deterministic: equal scores keep the lower fact id as winner (stable tie-break).
    """
    challenger = store.get_fact(challenger_id)
    incumbent = store.get_fact(incumbent_id)

    if not challenger or not incumbent:
        logger.warning(
            "Could not load both facts for contradiction: %s, %s",
            challenger_id,
            incumbent_id,
        )
        # Prefer the lower id when both missing data — deterministic, no silent drop of both.
        return (incumbent_id if incumbent else challenger_id), "could_not_load_facts"

    ch_status = (
        challenger.status.value if hasattr(challenger.status, "value") else str(challenger.status)
    )
    in_status = (
        incumbent.status.value if hasattr(incumbent.status, "value") else str(incumbent.status)
    )

    # Asymmetry: if challenger is quarantined AND has low trust, it loses
    if (
        ch_status == FactStatus.QUARANTINED.value
        and challenger.trust <= 0.6
        and in_status == FactStatus.ACTIVE.value
    ):
        return incumbent_id, f"quarantined_low_trust_challenger_loses (trust={challenger.trust})"

    # Otherwise, compare confidence + trust + recency
    challenger_score = (
        challenger.confidence * 0.4
        + challenger.trust * 0.4
        + (1.0 - _days_old(challenger.recorded_at, now) / 365.0) * 0.2
    )
    incumbent_score = (
        incumbent.confidence * 0.4
        + incumbent.trust * 0.4
        + (1.0 - _days_old(incumbent.recorded_at, now) / 365.0) * 0.2
    )

    if challenger_score > incumbent_score:
        return (
            challenger_id,
            f"challenger_wins_on_score ({challenger_score:.3f} > {incumbent_score:.3f})",
        )
    if incumbent_score > challenger_score:
        return (
            incumbent_id,
            f"incumbent_wins_on_score ({incumbent_score:.3f} > {challenger_score:.3f})",
        )
    # Deterministic tie-break: lower fact id wins (stable lineage).
    winner = min(challenger_id, incumbent_id)
    return winner, f"tie_break_lower_id ({challenger_score:.3f} == {incumbent_score:.3f})"


def _mark_contradiction_edge_resolved(
    store: Any,
    loser_id: int,
    winner_id: int,
    now: datetime,
) -> bool:
    """Mark the contradiction edge as resolved (invalid_at) so reruns skip it.

    Sets invalid_at on both directions of the CONTRADICTS edge (A→B and B→A).
    Returns True only when the edge is known to be closed; a False must be surfaced by
    the caller as a retryable phase error, never reported as a clean run.

    ORDERING CONTRACT (M-27): callers MUST supersede the loser BEFORE calling this. An
    open CONTRADICTS edge is the only durable record that the conflict exists, so
    closing it first means a crash before supersession erases the conflict forever —
    the next run finds no edge, adjudicates nothing, and reports status=ok with two
    contradictory facts still active. The mirror case is safe: a superseded loser makes
    every rerun skip the pair (see the adjudication loop), stops the pair blocking
    promotion (``list_unresolved_contradiction_peers`` ignores superseded peers), and
    the still-open edge is closed by the next run's settled-pair sweep.
    """
    if hasattr(store, "resolve_contradiction_edge"):
        try:
            store.resolve_contradiction_edge(loser_id, winner_id, now)
            return True
        except Exception as e:  # noqa: BLE001
            logger.warning("Failed to resolve contradiction edge %s↔%s: %s", loser_id, winner_id, e)
            return False

    # Fallback for InMemoryKnowledgeStore without the method.
    if hasattr(store, "_edges"):
        with getattr(store, "_lock", nullcontext()):
            for edge in list(getattr(store, "_edges", {}).values()):
                if edge.edge_type != "contradicts":
                    continue
                if edge.invalid_at is not None:
                    continue
                if (edge.src_id == loser_id and edge.dst_id == winner_id) or (
                    edge.src_id == winner_id and edge.dst_id == loser_id
                ):
                    edge.invalid_at = now
        return True

    return False


def _contradiction_pair_state(store: Any, a_id: int, b_id: int) -> str:
    """Classify a CONTRADICTS pair from CURRENT fact state.

    ``"open"``    — both endpoints are live and unsettled; the conflict still stands.
    ``"settled"`` — an endpoint is superseded or soft-archived, so the conflict is
                    already decided: by an earlier pair in this same run, or by a run
                    that died after superseding but before closing the edge.
    ``"missing"`` — an endpoint no longer exists; nothing can be concluded.
    """
    for fact_id in (a_id, b_id):
        fact = store.get_fact(fact_id)
        if fact is None:
            return "missing"
        status = fact.status.value if hasattr(fact.status, "value") else str(fact.status)
        if status == FactStatus.SUPERSEDED.value or fact.invalid_at is not None:
            return "settled"
    return "open"


def _phase_error(phase: str, error: str, *, retryable: bool = True) -> dict[str, Any]:
    return {"phase": phase, "error": error, "retryable": retryable}


def _empty_result(*, dry_run: bool) -> dict[str, Any]:
    return {
        "promotions": [],
        "merges": [],
        "decayed_facts": 0,
        "decayed_edges": 0,
        "adjudications": [],
        "embeddings_backfilled": 0,
        "vault_notes_written": 0,
        "dry_run": dry_run,
        "status": STATUS_OK,
        "phase_errors": [],
        "deferred_promotions": [],
        "retryable": False,
    }


def _finalize_status(result: dict[str, Any]) -> dict[str, Any]:
    """Set status/retryable from phase_errors without claiming full success on failure."""
    errors = result.get("phase_errors") or []
    if not errors:
        result["status"] = STATUS_OK
        result["retryable"] = False
        return result

    made_progress = bool(
        result.get("promotions")
        or result.get("merges")
        or result.get("adjudications")
        or result.get("embeddings_backfilled")
        or result.get("decayed_facts")
        or result.get("decayed_edges")
        or result.get("vault_notes_written")
    )
    # Deferred promotions mean work remains; that is partial even without hard errors.
    if result.get("deferred_promotions") or made_progress:
        result["status"] = STATUS_PARTIAL
    else:
        result["status"] = STATUS_FAILED
    result["retryable"] = any(e.get("retryable", True) for e in errors) or bool(
        result.get("deferred_promotions")
    )
    return result


def consolidate(
    store: Any,
    vault_dir: str | Path,
    *,
    dry_run: bool = False,
    now: datetime | None = None,
    evidence: EvidenceLedger | None = None,
) -> dict[str, Any]:
    """Run the offline consolidation pass.

    This function:
    1. Promotes quarantined→active per the frozen predicate (evidence + contradictions)
    2. Merges near-duplicate facts (vector-nearest, high threshold)
    3. Decays edge weights and importance over time
    4. Adjudicates contradiction-flagged pairs (with asymmetry)
    5. Backfills missing embeddings (length mismatch → partial/degraded, no silent drop)
    6. Writes per-discipline summary notes to vault (Obsidian)

    Args:
        store: KnowledgeStore (or InMemoryKnowledgeStore) connected as admin role
        vault_dir: Path to the vault directory (Obsidian, git-versioned)
        dry_run: If True, compute but don't write anything
        now: Current datetime (default: datetime.now(UTC))
        evidence: Optional EvidenceLedger; defaults to store.evidence when present

    Returns:
        dict with promotions/merges/... plus:
            - status: "ok" | "partial" | "failed"
            - phase_errors: list of {phase, error, retryable}
            - deferred_promotions: fact ids left quarantined for retry
            - retryable: bool

    Raises:
        PromotionDenied: If store is not connected as admin role
        KnowledgeUnavailable: If DB is unreachable
    """
    if now is None:
        now = datetime.now(UTC)

    vault_path = Path(vault_dir)
    ledger = evidence
    if ledger is None and hasattr(store, "evidence") and isinstance(store.evidence, EvidenceLedger):
        ledger = store.evidence

    result = _empty_result(dry_run=dry_run)
    phase_errors: list[dict[str, Any]] = result["phase_errors"]

    # Injected phase failures (InMemoryKnowledgeStore.fail_phases) for adversarial tests.
    fail_phases: set[str] = set(getattr(store, "fail_phases", set()) or set())

    # === PROMOTION: quarantined → active ===
    try:
        if "promotion" in fail_phases:
            raise RuntimeError("injected promotion phase failure")

        if hasattr(store, "list_quarantined_fact_ids"):
            quarantined_ids = list(store.list_quarantined_fact_ids())
        else:
            conn = store._conn()
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT id FROM facts WHERE status = %s AND invalid_at IS NULL "
                    "AND capability_scope IS NULL",
                    (FactStatus.QUARANTINED.value,),
                )
                quarantined_ids = [row[0] for row in cur.fetchall()]

        promotions: list[int] = []
        for qid in quarantined_ids:
            should_promote, reason = _check_promotion_predicate(store, qid, now, evidence=ledger)
            if should_promote:
                promotions.append(qid)
                logger.info("Promoting fact %s: %s", qid, reason)

        if not dry_run and promotions:
            g = gate()
            for qid in promotions:
                # RE-EMBED from the statement at promotion, FAIL-CLOSED.
                if store._embedder is None:
                    logger.warning("Deferring promotion of fact %s: no embedder to re-embed", qid)
                    result["deferred_promotions"].append(qid)
                    phase_errors.append(
                        _phase_error(
                            "promotion",
                            f"fact {qid} deferred: no embedder",
                            retryable=True,
                        )
                    )
                    continue
                try:
                    pf = store.get_fact(qid)
                    if pf is None:
                        phase_errors.append(
                            _phase_error(
                                "promotion",
                                f"fact {qid} disappeared before promote",
                                retryable=True,
                            )
                        )
                        result["deferred_promotions"].append(qid)
                        continue
                    vectors = store._embedder.embed([pf.statement])
                    if len(vectors) != 1:
                        # Length mismatch: never promote with a missing/extra vector.
                        msg = (
                            f"fact {qid} embed length mismatch: "
                            f"expected 1 vector, got {len(vectors)}"
                        )
                        logger.warning("Deferring promotion: %s", msg)
                        result["deferred_promotions"].append(qid)
                        phase_errors.append(_phase_error("promotion", msg, retryable=True))
                        continue
                    store.set_embedding(qid, vectors[0])
                except Exception as e:  # noqa: BLE001
                    logger.warning("Deferring promotion of fact %s: re-embed failed: %s", qid, e)
                    result["deferred_promotions"].append(qid)
                    phase_errors.append(
                        _phase_error(
                            "promotion",
                            f"fact {qid} re-embed failed: {e}",
                            retryable=True,
                        )
                    )
                    continue
                store.promote_fact(qid, g)
                _record_promotion_audit(store, qid)
                result["promotions"].append(qid)
        elif dry_run:
            result["promotions"] = list(promotions)
    except Exception as e:  # noqa: BLE001
        logger.warning("Promotion pass failed: %s", e)
        phase_errors.append(_phase_error("promotion", str(e), retryable=True))

    # === MERGE: near-duplicate facts ===
    try:
        if "merge" in fail_phases:
            raise RuntimeError("injected merge phase failure")

        merges: list[tuple[int, int]] = []
        if hasattr(store, "list_active_embedded"):
            facts_data = list(store.list_active_embedded())
        else:
            conn = store._conn()
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT id, embedding, trust
                    FROM facts
                    WHERE status = %s AND embedding IS NOT NULL AND invalid_at IS NULL
                      AND capability_scope IS NULL
                    ORDER BY id
                    """,
                    (FactStatus.ACTIVE.value,),
                )
                facts_data = cur.fetchall()

        # Prefer pgvector distance when a real connection exists; otherwise cosine on lists.
        use_pg = hasattr(store, "_conn") and not hasattr(store, "evidence")

        for i, (fact_id_1, emb_1, trust_1) in enumerate(facts_data):
            if fact_id_1 in [m[0] for m in merges]:
                continue
            for fact_id_2, emb_2, trust_2 in facts_data[i + 1 :]:
                if fact_id_2 in [m[0] for m in merges]:
                    continue
                if emb_1 is None or emb_2 is None:
                    continue
                similarity = 0.0
                if use_pg:
                    try:
                        conn = store._conn()
                        with conn.cursor() as cur:
                            cur.execute(
                                "SELECT %s::halfvec <=> %s::halfvec AS distance",
                                (emb_1, emb_2),
                            )
                            row = cur.fetchone()
                            distance = row[0] if row else None
                            similarity = 1.0 - (distance / 2.0) if distance is not None else 0.0
                    except Exception:  # noqa: BLE001
                        similarity = _cosine_similarity(emb_1, emb_2)
                else:
                    similarity = _cosine_similarity(emb_1, emb_2)

                if similarity >= MERGE_THRESHOLD_SIMILARITY:
                    if trust_1 >= trust_2:
                        merges.append((fact_id_2, fact_id_1))
                        logger.info(
                            "Merging fact %s into %s (similarity=%.3f)",
                            fact_id_2,
                            fact_id_1,
                            similarity,
                        )
                    else:
                        merges.append((fact_id_1, fact_id_2))
                        logger.info(
                            "Merging fact %s into %s (similarity=%.3f)",
                            fact_id_1,
                            fact_id_2,
                            similarity,
                        )

        if not dry_run and merges:
            for loser_id, winner_id in merges:
                store.supersede_fact(loser_id, winner_id)
                try:
                    store.add_edge(
                        src_kind="fact",
                        src_id=loser_id,
                        dst_kind="fact",
                        dst_id=winner_id,
                        edge_type="refines",
                        weight=1.0,
                    )
                except Exception as e:  # noqa: BLE001
                    logger.warning("Could not add refines edge: %s", e)
                result["merges"].append((loser_id, winner_id))
        elif dry_run:
            result["merges"] = list(merges)
    except Exception as e:  # noqa: BLE001
        logger.warning("Merge pass failed: %s", e)
        phase_errors.append(_phase_error("merge", str(e), retryable=True))

    # === DECAY: edge weights + importance ===
    try:
        if "decay" in fail_phases:
            raise RuntimeError("injected decay phase failure")

        edges_decayed = 0
        facts_decayed = 0

        if not dry_run and hasattr(store, "_conn"):
            conn = store._conn()
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT id, valid_at, weight FROM edges WHERE invalid_at IS NULL",
                )
                edges = cur.fetchall()

            for edge_id, valid_at, weight in edges:
                days = _days_old(valid_at, now)
                if days >= WEIGHT_DECAY_DAYS:
                    multiplier = _decay_multiplier(days)
                    new_weight = weight * multiplier
                    with conn.cursor() as cur:
                        if new_weight < MIN_WEIGHT_THRESHOLD:
                            cur.execute(
                                "UPDATE edges SET invalid_at = %s WHERE id = %s",
                                (now, edge_id),
                            )
                        else:
                            cur.execute(
                                "UPDATE edges SET weight = %s WHERE id = %s",
                                (new_weight, edge_id),
                            )
                    edges_decayed += 1

            with conn.cursor() as cur:
                cur.execute(
                    "SELECT id, recorded_at, importance FROM facts WHERE invalid_at IS NULL "
                    "AND capability_scope IS NULL",
                )
                facts = cur.fetchall()

            for fact_id, recorded_at, importance in facts:
                days = _days_old(recorded_at, now)
                if days >= IMPORTANCE_DECAY_DAYS:
                    multiplier = _decay_multiplier(days)
                    new_importance = importance * multiplier
                    with conn.cursor() as cur:
                        if new_importance < MIN_IMPORTANCE_THRESHOLD:
                            cur.execute(
                                "UPDATE facts SET invalid_at = %s WHERE id = %s",
                                (now, fact_id),
                            )
                        else:
                            cur.execute(
                                "UPDATE facts SET importance = %s WHERE id = %s",
                                (new_importance, fact_id),
                            )
                    facts_decayed += 1

            result["decayed_edges"] = edges_decayed
            result["decayed_facts"] = facts_decayed
    except Exception as e:  # noqa: BLE001
        logger.warning("Decay pass failed: %s", e)
        phase_errors.append(_phase_error("decay", str(e), retryable=True))

    # === ADJUDICATION: contradiction-flagged pairs ===
    try:
        if "adjudication" in fail_phases:
            raise RuntimeError("injected adjudication phase failure")

        adjudications: list[tuple[int, int, str]] = []
        if hasattr(store, "list_contradicts_edges"):
            contradiction_edges = list(store.list_contradicts_edges())
        else:
            conn = store._conn()
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT src_id, dst_id
                    FROM edges
                    WHERE edge_type = 'contradicts' AND invalid_at IS NULL
                      AND src_kind = 'fact' AND dst_kind = 'fact'
                    ORDER BY src_id, dst_id
                    """,
                )
                contradiction_edges = cur.fetchall()

        contradiction_edges = sorted(
            [(int(src), int(dst)) for src, dst in contradiction_edges],
            key=lambda pair: (pair[0], pair[1]),
        )

        # Open edges whose conflict is already decided — left behind by an interrupted
        # run (superseded loser, edge never closed) or by decay soft-archiving an
        # endpoint. Closing them is finishing someone else's work, not a new decision,
        # so they never enter `adjudications` and reruns stay byte-stable.
        settled_edges: list[tuple[int, int]] = []

        for src_id, dst_id in contradiction_edges:
            state = _contradiction_pair_state(store, src_id, dst_id)
            if state == "missing":
                continue  # Fact was deleted or never existed.
            if state == "settled":
                settled_edges.append((src_id, dst_id))
                continue

            winner_id, reason = _adjudicate_contradiction(store, src_id, dst_id, now)
            loser_id = dst_id if winner_id == src_id else src_id
            adjudications.append((loser_id, winner_id, reason))
            logger.info(
                "Adjudicated contradiction: %s loses to %s (%s)",
                loser_id,
                winner_id,
                reason,
            )

        if not dry_run:
            for a_id, b_id in settled_edges:
                if not _mark_contradiction_edge_resolved(store, a_id, b_id, now):
                    phase_errors.append(
                        _phase_error(
                            "adjudication",
                            f"settled contradiction edge {a_id}↔{b_id} could not be closed",
                            retryable=True,
                        )
                    )

            for loser_id, winner_id, reason in adjudications:
                # Re-check against CURRENT state: every decision above was taken from one
                # pre-write snapshot, and an earlier pair in this same loop may since have
                # settled one of these facts. Writing anyway would relink an already
                # settled fact to a fact that has itself just lost, so the lineage a crash
                # leaves behind would differ from the lineage an uninterrupted run leaves.
                if _contradiction_pair_state(store, loser_id, winner_id) != "open":
                    if not _mark_contradiction_edge_resolved(store, loser_id, winner_id, now):
                        phase_errors.append(
                            _phase_error(
                                "adjudication",
                                f"settled contradiction edge {loser_id}↔{winner_id} "
                                "could not be closed",
                                retryable=True,
                            )
                        )
                    continue

                # ORDER IS THE INVARIANT (M-27): supersede the loser, THEN close the edge.
                # A failure here leaves the edge open and both facts live, so the next run
                # re-adjudicates from scratch. The reverse order would durably erase the
                # conflict and strand two contradictory active facts.
                try:
                    store.supersede_fact(loser_id, winner_id)
                except Exception as e:  # noqa: BLE001
                    logger.warning(
                        "Adjudication supersede failed for %s→%s: %s", loser_id, winner_id, e
                    )
                    phase_errors.append(
                        _phase_error(
                            "adjudication",
                            f"supersede {loser_id}→{winner_id} failed: {e}",
                            retryable=True,
                        )
                    )
                    continue

                result["adjudications"].append((loser_id, winner_id, reason))
                if not _mark_contradiction_edge_resolved(store, loser_id, winner_id, now):
                    # The decision IS durable (loser superseded), so this is not a lost
                    # adjudication — but the run did not finish, and the next sweep must
                    # close the edge. Never report ok with work outstanding.
                    phase_errors.append(
                        _phase_error(
                            "adjudication",
                            f"contradiction edge {loser_id}↔{winner_id} left open after "
                            f"superseding {loser_id}",
                            retryable=True,
                        )
                    )
        else:
            result["adjudications"] = list(adjudications)
    except Exception as e:  # noqa: BLE001
        logger.warning("Adjudication pass failed: %s", e)
        phase_errors.append(_phase_error("adjudication", str(e), retryable=True))

    # === EMBEDDING BACKFILL ===
    try:
        if "embedding_backfill" in fail_phases:
            raise RuntimeError("injected embedding_backfill phase failure")

        missing_facts = store.facts_missing_embedding(limit=100)
        if missing_facts and store._embedder:
            statements = [f.statement for f in missing_facts]
            try:
                embeddings = store._embedder.embed(statements)
                # Optional adversarial truncation hook on InMemoryKnowledgeStore.
                trunc = getattr(store, "embed_return_count", None)
                if trunc is not None:
                    embeddings = embeddings[: int(trunc)]

                if len(embeddings) != len(missing_facts):
                    msg = (
                        f"embedding length mismatch: facts={len(missing_facts)} "
                        f"vectors={len(embeddings)}; refusing silent drop"
                    )
                    logger.warning(msg)
                    phase_errors.append(_phase_error("embedding_backfill", msg, retryable=True))
                    # Preserve retryable state: leave embeddings NULL; do not partial-write
                    # a truncated zip that would permanently drop the tail facts.
                elif not dry_run:
                    for fact, emb in zip(missing_facts, embeddings, strict=True):
                        store.set_embedding(fact.id, emb)
                        result["embeddings_backfilled"] += 1
                else:
                    result["embeddings_backfilled"] = len(missing_facts)
            except Exception as e:  # noqa: BLE001
                logger.warning("Embedding backfill failed: %s", e)
                phase_errors.append(_phase_error("embedding_backfill", str(e), retryable=True))
    except Exception as e:  # noqa: BLE001
        logger.warning("Embedding backfill query failed: %s", e)
        phase_errors.append(_phase_error("embedding_backfill", str(e), retryable=True))

    # === VAULT NOTES ===
    if not dry_run and vault_path.exists() and hasattr(store, "_conn"):
        try:
            if "vault" in fail_phases:
                raise RuntimeError("injected vault phase failure")
            conn = store._conn()
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT discipline, COUNT(*) as count,
                           COUNT(CASE WHEN status = 'active' THEN 1 END) as active_count
                    FROM facts
                    WHERE invalid_at IS NULL AND capability_scope IS NULL
                    GROUP BY discipline
                    """,
                )
                discipline_stats = cur.fetchall()

            for discipline, total_count, active_count in discipline_stats:
                if not discipline:
                    discipline = "unclassified"
                note_path = vault_path / f"consolidation_{discipline}.md"
                try:
                    with open(note_path, "w") as f:
                        f.write(f"# Consolidation Report: {discipline}\n\n")
                        f.write(f"- Total facts: {total_count}\n")
                        f.write(f"- Active facts: {active_count}\n")
                        f.write(f"- Quarantined facts: {total_count - active_count}\n")
                        f.write(f"- Timestamp: {now.isoformat()}\n")
                    result["vault_notes_written"] += 1
                except Exception as e:  # noqa: BLE001
                    logger.warning("Could not write vault note for %s: %s", discipline, e)
                    phase_errors.append(_phase_error("vault", f"{discipline}: {e}", retryable=True))
        except Exception as e:  # noqa: BLE001
            logger.warning("Vault notes pass failed: %s", e)
            phase_errors.append(_phase_error("vault", str(e), retryable=True))

    # Deferred promotions without other errors still mark partial/retryable.
    if result["deferred_promotions"] and not any(e["phase"] == "promotion" for e in phase_errors):
        phase_errors.append(
            _phase_error(
                "promotion",
                f"deferred {len(result['deferred_promotions'])} fact(s) for retry",
                retryable=True,
            )
        )

    _finalize_status(result)

    logger.info(
        "Consolidation complete: status=%s promotions=%s merges=%s "
        "adjudications=%s embeddings=%s phase_errors=%s dry_run=%s",
        result["status"],
        len(result["promotions"]),
        len(result["merges"]),
        len(result["adjudications"]),
        result["embeddings_backfilled"],
        len(result["phase_errors"]),
        dry_run,
    )
    return result


def _cosine_similarity(a: Any, b: Any) -> float:
    """Cosine similarity for list/tuple vectors (in-memory merge path)."""
    try:
        va = list(a)
        vb = list(b)
        if not va or not vb or len(va) != len(vb):
            return 0.0
        dot = sum(x * y for x, y in zip(va, vb, strict=True))
        na = sum(x * x for x in va) ** 0.5
        nb = sum(y * y for y in vb) ** 0.5
        if na == 0 or nb == 0:
            return 0.0
        return float(dot / (na * nb))
    except Exception:  # noqa: BLE001
        return 0.0
