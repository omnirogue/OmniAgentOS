"""PostgreSQL-backed source/generation semantics (M-26, M-27) and memory/PG parity.

test_knowledge_integrity.py proves the semantics against the in-memory ledger. This file
proves the SAME semantics against the real database — the registry table, the CTE
predicate, and the migration-007 role/trigger boundary — and then asserts the two
backends return identical verdicts for identical histories. A structural assertion that
both call ``evaluate_source_groups`` is not enough: the PG side also has to feed that
function the right rows and the right current generations.

Isolation: the generation registry is deliberately global and monotonic, so it is NOT
reset between tests (conftest truncates facts and cascades to evidence, but the registry
has no FK). Every test therefore mints unique source identities, which is also what
exercises the cross-fact behaviour we care about.
"""

from __future__ import annotations

import uuid

import psycopg
import pytest

from omniagentos.knowledge.config import require_pg
from omniagentos.knowledge.consolidate import _has_system_verified_outcome
from omniagentos.knowledge.contracts import KnowledgeError
from omniagentos.knowledge.embeddings import FakeEmbedding
from omniagentos.knowledge.evidence import (
    EvidenceLedger,
    EvidenceStatus,
    PromotionEvidence,
    VerifierOutcome,
)
from omniagentos.knowledge.memory_store import InMemoryKnowledgeStore
from omniagentos.knowledge.store import KnowledgeStore

pytestmark = pytest.mark.skipif(
    not require_pg(),
    reason="OMNIAGENTOS_REQUIRE_PG not set; skipping live DB tests",
)


def _src(label: str) -> str:
    """A source identity unique to this test run — the registry outlives truncation."""
    return f"run:{label}-{uuid.uuid4().hex[:12]}"


def _fact(store: KnowledgeStore, statement: str = "A claim under verification") -> int:
    episode = store.add_episode(source="human", content=statement, source_ref=uuid.uuid4().hex)
    return store.add_fact(statement=statement, episode_id=episode, provenance="extracted")


class TestPostgresSourceGenerationSemantics:
    """The predicate is source-authoritative in the database, not fact-local."""

    def test_newer_generation_without_evidence_blocks(
        self, knowledge_store_admin: KnowledgeStore
    ) -> None:
        """The blocking defect, against real SQL."""
        store = knowledge_store_admin
        fact_id = _fact(store)
        source = _src("verifier")
        store.record_promotion_evidence(
            fact_id=fact_id,
            provenance="system_run_verifier",
            source_identity=source,
            verifier_outcome="pass",
        )
        assert store.has_valid_promotion_evidence(fact_id)

        assert store.advance_source_generation(source) == 2
        assert not store.has_valid_promotion_evidence(fact_id), (
            "a source that advanced without re-verifying must fail closed in PG too"
        )

    def test_stale_pass_is_fact_global_not_fact_local(
        self, knowledge_store_admin: KnowledgeStore
    ) -> None:
        """The exact case the old fact-local CTE missed: the source moved on elsewhere."""
        store = knowledge_store_admin
        fact_a, fact_b = _fact(store, "Claim A"), _fact(store, "Claim B")
        source = _src("verifier")
        store.record_promotion_evidence(
            fact_id=fact_a,
            provenance="system_run_verifier",
            source_identity=source,
            verifier_outcome="pass",
        )
        assert store.has_valid_promotion_evidence(fact_a)

        # Same source, DIFFERENT fact, newer generation.
        store.record_promotion_evidence(
            fact_id=fact_b,
            provenance="system_run_verifier",
            source_identity=source,
            verifier_outcome="pass",
            promotion_generation=2,
        )
        assert store.current_source_generation(source) == 2
        assert not store.has_valid_promotion_evidence(fact_a), (
            "the gen1 PASS is stale everywhere once the source reached gen2"
        )
        assert store.has_valid_promotion_evidence(fact_b)

    def test_newer_fail_blocks(self, knowledge_store_admin: KnowledgeStore) -> None:
        store = knowledge_store_admin
        fact_id = _fact(store)
        source = _src("verifier")
        store.record_promotion_evidence(
            fact_id=fact_id,
            provenance="system_run_verifier",
            source_identity=source,
            verifier_outcome="pass",
        )
        store.record_promotion_evidence(
            fact_id=fact_id,
            provenance="system_run_verifier",
            source_identity=source,
            verifier_outcome="fail",
            promotion_generation=2,
        )
        assert not store.has_valid_promotion_evidence(fact_id)

    def test_revoking_a_newer_fail_does_not_resurrect_the_stale_pass(
        self, knowledge_store_admin: KnowledgeStore
    ) -> None:
        """The registry row is what keeps the source at gen2 after the FAIL is withdrawn."""
        store = knowledge_store_admin
        fact_id = _fact(store)
        source = _src("verifier")
        store.record_promotion_evidence(
            fact_id=fact_id,
            provenance="system_run_verifier",
            source_identity=source,
            verifier_outcome="pass",
        )
        fail_id = store.record_promotion_evidence(
            fact_id=fact_id,
            provenance="system_run_verifier",
            source_identity=source,
            verifier_outcome="fail",
            promotion_generation=2,
        )
        store.revoke_promotion_evidence(fail_id, reason="verifier bug")
        assert store.current_source_generation(source) == 2
        assert not store.has_valid_promotion_evidence(fact_id)

    def test_reverifying_at_the_current_generation_restores_promotion(
        self, knowledge_store_admin: KnowledgeStore
    ) -> None:
        store = knowledge_store_admin
        fact_id = _fact(store)
        source = _src("verifier")
        store.record_promotion_evidence(
            fact_id=fact_id,
            provenance="system_run_verifier",
            source_identity=source,
            verifier_outcome="pass",
        )
        store.advance_source_generation(source)
        assert not store.has_valid_promotion_evidence(fact_id)

        store.record_promotion_evidence(
            fact_id=fact_id,
            provenance="system_run_verifier",
            source_identity=source,
            verifier_outcome="pass",
        )
        assert store.has_valid_promotion_evidence(fact_id)

    def test_another_sources_advance_does_not_stale_this_source(
        self, knowledge_store_admin: KnowledgeStore
    ) -> None:
        store = knowledge_store_admin
        fact_id = _fact(store)
        source_a, source_b = _src("a"), _src("b")
        store.record_promotion_evidence(
            fact_id=fact_id,
            provenance="system_run_verifier",
            source_identity=source_a,
            verifier_outcome="pass",
        )
        store.advance_source_generation(source_b)
        store.advance_source_generation(source_b)
        assert store.current_source_generation(source_a) == 1
        assert store.has_valid_promotion_evidence(fact_id)

    def test_rerun_is_idempotent(self, knowledge_store_admin: KnowledgeStore) -> None:
        """A verifier rerun must not insert duplicate rows or advance the generation."""
        store = knowledge_store_admin
        fact_id = _fact(store)
        source = _src("verifier")
        ids = {
            store.record_promotion_evidence(
                fact_id=fact_id,
                provenance="system_run_verifier",
                source_identity=source,
                verifier_outcome="pass",
            )
            for _ in range(4)
        }
        assert len(ids) == 1, "the unique partial index must make reruns a no-op"
        assert store.current_source_generation(source) == 1
        assert store.has_valid_promotion_evidence(fact_id)

    def test_generation_registry_never_walks_backwards(
        self, knowledge_store_admin: KnowledgeStore
    ) -> None:
        store = knowledge_store_admin
        fact_id = _fact(store)
        source = _src("verifier")
        store.record_promotion_evidence(
            fact_id=fact_id,
            provenance="system_run_verifier",
            source_identity=source,
            verifier_outcome="pass",
            promotion_generation=5,
        )
        assert store.current_source_generation(source) == 5
        store.record_promotion_evidence(
            fact_id=fact_id,
            provenance="system_run_verifier",
            source_identity=source,
            verifier_outcome="pass",
            promotion_generation=2,
        )
        assert store.current_source_generation(source) == 5
        assert store.has_valid_promotion_evidence(fact_id)

    def test_zero_generation_is_rejected(self, knowledge_store_admin: KnowledgeStore) -> None:
        store = knowledge_store_admin
        fact_id = _fact(store)
        with pytest.raises(KnowledgeError):
            store.record_promotion_evidence(
                fact_id=fact_id,
                provenance="system_run_verifier",
                source_identity=_src("verifier"),
                verifier_outcome="pass",
                promotion_generation=0,
            )

    def test_consolidator_evidence_is_audit_only(
        self, knowledge_store_admin: KnowledgeStore
    ) -> None:
        """system_consolidator is written to PG but never satisfies the predicate."""
        store = knowledge_store_admin
        fact_id = _fact(store)
        store.record_promotion_evidence(
            fact_id=fact_id,
            provenance="system_consolidator",
            source_identity=_src("consolidator"),
            verifier_outcome="pass",
        )
        assert not store.has_valid_promotion_evidence(fact_id), (
            "the consolidator must not be able to launder its own promotions through PG"
        )

    def test_revoking_all_evidence_for_a_source_blocks(
        self, knowledge_store_admin: KnowledgeStore
    ) -> None:
        """What the operator demote route relies on."""
        store = knowledge_store_admin
        fact_id = _fact(store)
        source = _src("operator")
        store.record_promotion_evidence(
            fact_id=fact_id,
            provenance="system_operator",
            source_identity=source,
            verifier_outcome="pass",
        )
        assert store.has_valid_promotion_evidence(fact_id)
        revoked = store.revoke_promotion_evidence_for_source(
            fact_id, source, reason="operator demoted the fact"
        )
        assert len(revoked) == 1
        assert not store.has_valid_promotion_evidence(fact_id)


class TestConsolidatorReadsThePostgresPredicate:
    """The promotion path must consult the PG predicate, not just the in-memory ledger."""

    def test_stale_source_blocks_the_consolidator_predicate(
        self, knowledge_store_admin: KnowledgeStore
    ) -> None:
        store = knowledge_store_admin
        fact_id = _fact(store)
        source = _src("verifier")
        store.record_promotion_evidence(
            fact_id=fact_id,
            provenance="system_run_verifier",
            source_identity=source,
            verifier_outcome="pass",
        )
        assert _has_system_verified_outcome(fact_id, store=store)

        store.advance_source_generation(source)
        assert not _has_system_verified_outcome(fact_id, store=store), (
            "the consolidator must see PG staleness, not just the in-memory ledger's"
        )


class TestMigration007RoleAndTriggerControls:
    """Migration 007 must be at least as strict as 006 — never a relaxation."""

    def test_agent_role_store_cannot_advance_a_generation(
        self, knowledge_store: KnowledgeStore
    ) -> None:
        with pytest.raises(KnowledgeError):
            knowledge_store.advance_source_generation(_src("verifier"))

    def test_agent_role_store_cannot_record_evidence(self, knowledge_store: KnowledgeStore) -> None:
        with pytest.raises(KnowledgeError):
            knowledge_store.record_promotion_evidence(
                fact_id=1,
                provenance="system_run_verifier",
                source_identity=_src("verifier"),
                verifier_outcome="pass",
            )

    def test_database_denies_agent_writes_to_the_registry(
        self, knowledge_store_admin: KnowledgeStore
    ) -> None:
        """The F1 boundary is the GRANT, not the Python check — prove it with raw psycopg."""
        dbname = knowledge_store_admin._dsn.rsplit("/", 1)[-1]
        agent_dsn = f"postgresql://knowledge_agent@localhost/{dbname}"
        with psycopg.connect(agent_dsn) as conn:
            with conn.cursor() as cur:
                # SELECT is granted — transparency.
                cur.execute("SELECT count(*) FROM knowledge_source_generation")
                assert cur.fetchone() is not None
            with pytest.raises(psycopg.errors.InsufficientPrivilege), conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO knowledge_source_generation "
                    "(source_identity, current_generation) VALUES ('spoof', 1)"
                )

    def test_registry_author_role_is_forced_to_the_connecting_role(
        self, knowledge_store_admin: KnowledgeStore
    ) -> None:
        """The trigger overwrites a spoofed author_role rather than trusting the insert."""
        store = knowledge_store_admin
        source = _src("verifier")
        store.advance_source_generation(source)
        with store._conn().cursor() as cur:
            cur.execute(
                "SELECT author_role FROM knowledge_source_generation WHERE source_identity = %s",
                (source,),
            )
            assert cur.fetchone()[0] == "knowledge_admin"

    def test_registry_generation_is_monotonic_at_the_database_level(
        self, knowledge_store_admin: KnowledgeStore
    ) -> None:
        """Even a direct admin UPDATE cannot walk a source backwards."""
        store = knowledge_store_admin
        source = _src("verifier")
        store.advance_source_generation(source)
        store.advance_source_generation(source)
        conn = store._conn()
        with pytest.raises(psycopg.errors.CheckViolation), conn.cursor() as cur:
            cur.execute(
                "UPDATE knowledge_source_generation SET current_generation = 1 "
                "WHERE source_identity = %s",
                (source,),
            )
        conn.rollback()
        assert store.current_source_generation(source) == 2


# (source label, generation, outcome) events applied in order to BOTH backends.
_HISTORIES: list[tuple[str, list[tuple[str, int | None, str]], bool]] = [
    ("single pass", [("a", None, "pass")], True),
    ("single fail", [("a", None, "fail")], False),
    ("single inconclusive", [("a", None, "inconclusive")], False),
    ("two sources pass", [("a", None, "pass"), ("b", None, "pass")], True),
    ("one source fails", [("a", None, "pass"), ("b", None, "fail")], False),
    ("newer fail same source", [("a", 1, "pass"), ("a", 2, "fail")], False),
    ("newer pass same source", [("a", 1, "pass"), ("a", 2, "pass")], True),
    ("stale pass, gen gap", [("a", 1, "pass"), ("a", 3, "pass")], True),
]


class TestMemoryAndPostgresAgreeOnRealHistories:
    """Same events, same verdict — replayed through both real backends."""

    @pytest.mark.parametrize(
        ("label", "events", "expected"), _HISTORIES, ids=[h[0] for h in _HISTORIES]
    )
    def test_backends_agree(
        self,
        knowledge_store_admin: KnowledgeStore,
        label: str,
        events: list[tuple[str, int | None, str]],
        expected: bool,
    ) -> None:
        pg = knowledge_store_admin
        memory = InMemoryKnowledgeStore(role="knowledge_admin", embedder=FakeEmbedding(dim=1024))

        pg_fact = _fact(pg, label)
        mem_episode = memory.add_episode(source="human", content=label, source_ref=label)
        mem_fact = memory.add_fact(statement=label, episode_id=mem_episode, provenance="extracted")

        # Source identities must be unique per replay (the PG registry is not truncated)
        # but consistent across the two backends within this replay.
        names = {label_: _src(label_) for label_, _, _ in events}
        for source_label, generation, outcome in events:
            for store, fact_id in ((pg, pg_fact), (memory, mem_fact)):
                store.record_promotion_evidence(
                    fact_id=fact_id,
                    provenance="system_run_verifier",
                    source_identity=names[source_label],
                    verifier_outcome=outcome,
                    promotion_generation=generation,
                )

        pg_verdict = pg.has_valid_promotion_evidence(pg_fact)
        mem_verdict = memory.has_valid_promotion_evidence(mem_fact)
        assert pg_verdict == mem_verdict, f"backends disagree on {label!r}"
        assert pg_verdict is expected

    def test_advance_then_query_agrees(self, knowledge_store_admin: KnowledgeStore) -> None:
        """The registry path (advance without evidence) must agree across backends too."""
        pg = knowledge_store_admin
        memory = InMemoryKnowledgeStore(role="knowledge_admin", embedder=FakeEmbedding(dim=1024))
        source = _src("verifier")

        pg_fact = _fact(pg)
        mem_episode = memory.add_episode(source="human", content="c", source_ref="c")
        mem_fact = memory.add_fact(statement="c", episode_id=mem_episode, provenance="extracted")

        for store, fact_id in ((pg, pg_fact), (memory, mem_fact)):
            store.record_promotion_evidence(
                fact_id=fact_id,
                provenance="system_run_verifier",
                source_identity=source,
                verifier_outcome="pass",
            )
        assert pg.has_valid_promotion_evidence(pg_fact)
        assert memory.has_valid_promotion_evidence(mem_fact)

        assert pg.advance_source_generation(source) == memory.advance_source_generation(source) == 2
        assert pg.has_valid_promotion_evidence(pg_fact) is False
        assert memory.has_valid_promotion_evidence(mem_fact) is False

    def test_ledger_and_store_normalize_outcomes_identically(self) -> None:
        """The memory store's string API and the ledger's enum API must not diverge."""
        ledger = EvidenceLedger()
        ledger.record(
            fact_id=1,
            provenance="system_run_verifier",
            source_identity="source:enum",
            verifier_outcome=VerifierOutcome.PASS,
        )
        memory = InMemoryKnowledgeStore(role="knowledge_admin", embedder=FakeEmbedding(dim=1024))
        episode = memory.add_episode(source="human", content="c", source_ref="c")
        fact_id = memory.add_fact(statement="c", episode_id=episode, provenance="extracted")
        memory.record_promotion_evidence(
            fact_id=fact_id,
            provenance="system_run_verifier",
            source_identity="source:string",
            verifier_outcome="pass",
        )
        assert ledger.has_valid_verification(1)
        assert memory.has_valid_promotion_evidence(fact_id)


class TestEvidenceLineageParity:
    """Row identity and non-active lineage must mean the same thing in both backends."""

    def test_provenance_is_not_part_of_the_idempotency_key(
        self, knowledge_store_admin: KnowledgeStore
    ) -> None:
        """Re-recording under a different provenance is a no-op, in BOTH backends.

        Migration 006's unique index is (fact_id, source_identity, promotion_generation,
        verifier_outcome) WHERE status='active' — provenance is not in it, so PostgreSQL
        physically cannot hold a second active row that differs only by provenance. The
        in-memory ledger used to match on provenance as well, so the same history replayed
        through the two backends ended with different row sets: memory grew a second row
        (and could promote on it) where the database returned the first.
        """
        pg = knowledge_store_admin
        memory = InMemoryKnowledgeStore(role="knowledge_admin", embedder=FakeEmbedding(dim=1024))
        source = _src("verifier")

        pg_fact = _fact(pg)
        mem_episode = memory.add_episode(source="human", content="c", source_ref="c")
        mem_fact = memory.add_fact(statement="c", episode_id=mem_episode, provenance="extracted")

        first: dict[str, int] = {}
        second: dict[str, int] = {}
        for name, store, fact_id in (("pg", pg, pg_fact), ("memory", memory, mem_fact)):
            first[name] = store.record_promotion_evidence(
                fact_id=fact_id,
                provenance="system_run_verifier",
                source_identity=source,
                verifier_outcome="pass",
            )
            second[name] = store.record_promotion_evidence(
                fact_id=fact_id,
                provenance="system_consolidator",
                source_identity=source,
                verifier_outcome="pass",
            )
        assert second["pg"] == first["pg"]
        assert second["memory"] == first["memory"], (
            "the ledger must dedupe on the same key as the database unique index"
        )

        with pg._conn().cursor() as cur:
            cur.execute(
                "SELECT count(*), min(provenance) FROM promotion_evidence "
                "WHERE fact_id = %s AND status = 'active'",
                (pg_fact,),
            )
            pg_count, pg_provenance = cur.fetchone()
        mem_rows = memory.evidence.list_for_fact(mem_fact)
        assert pg_count == len(mem_rows) == 1
        assert pg_provenance == mem_rows[0].provenance == "system_run_verifier", (
            "the first writer keeps the row; a later audit write never overwrites it"
        )
        assert pg.has_valid_promotion_evidence(pg_fact)
        assert memory.has_valid_promotion_evidence(mem_fact)

    def test_superseded_evidence_never_counts_in_postgres_either(
        self, knowledge_store_admin: KnowledgeStore
    ) -> None:
        """Both backends read status='superseded' as non-counting.

        RESIDUAL (documented in knowledge/evidence.py): only the in-memory ledger has a
        WRITER for this state (``EvidenceLedger.supersede``); PostgreSQL's withdrawal path
        is revoke-only, and no production caller supersedes evidence in either backend. The
        state is reachable in the schema (migration 006 status CHECK + superseded_by FK),
        so what has to be true — and is asserted here — is that the PG predicate refuses to
        count it exactly as the ledger does. Adding a PG writer with no caller would be
        surface without a user; leaving the reader untested would be a real gap.
        """
        pg = knowledge_store_admin
        source = _src("verifier")
        fact_id = _fact(pg)
        evidence_id = pg.record_promotion_evidence(
            fact_id=fact_id,
            provenance="system_run_verifier",
            source_identity=source,
            verifier_outcome="pass",
        )
        assert pg.has_valid_promotion_evidence(fact_id)

        with pg._conn().cursor() as cur:
            cur.execute(
                "UPDATE promotion_evidence SET status = 'superseded' WHERE id = %s",
                (evidence_id,),
            )
        assert not pg.has_valid_promotion_evidence(fact_id)

        ledger = EvidenceLedger()
        old = ledger.record(
            fact_id=1,
            provenance="system_run_verifier",
            source_identity=source,
            verifier_outcome="pass",
        )
        assert ledger.has_valid_verification(1)
        newer = PromotionEvidence(
            id=999,
            fact_id=1,
            provenance="system_run_verifier",
            source_identity=source,
            verifier_outcome=VerifierOutcome.PASS,
            promotion_generation=old.promotion_generation,
            status=EvidenceStatus.SUPERSEDED,
        )
        ledger.supersede(old.id, newer)
        assert not ledger.has_valid_verification(1), "the two backends must agree"
