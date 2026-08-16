"""KnowledgeStore — PostgreSQL+pgvector implementation of the knowledge subsystem."""

from __future__ import annotations

import contextlib
from datetime import UTC, datetime
from threading import RLock
from typing import TYPE_CHECKING, Any

import psycopg

from omniagentos.knowledge.config import dsn as default_dsn
from omniagentos.knowledge.contracts import (
    EMBED_DIM,
    Edge,
    EmbeddingProvider,
    EmbeddingUnavailable,
    Entity,
    Episode,
    Fact,
    FactStatus,
    KnowledgeError,
    KnowledgeUnavailable,
    PromotionDenied,
)
from omniagentos.knowledge.evidence import (
    EvidenceStatus,
    PromotionEvidence,
    VerifierOutcome,
    build_source_groups,
    evaluate_source_groups,
)

if TYPE_CHECKING:
    from omniagentos.knowledge.contracts import PromotionGate


def _scalar(cur: psycopg.Cursor) -> Any:
    """Fetch a single scalar from a RETURNING/SELECT, asserting a row exists.

    Keeps mypy honest (cur.fetchone() is Optional) and turns a silent None-index crash
    into a clear error if an INSERT ... RETURNING unexpectedly produced no row.
    """
    row = cur.fetchone()
    if row is None:
        raise KnowledgeError("expected a row but got none")
    return row[0]


class KnowledgeStore:
    """PostgreSQL-backed knowledge store with pgvector embeddings."""

    def __init__(
        self, dsn: str | None = None, *, embedder: EmbeddingProvider | None = None
    ) -> None:
        """Initialize the store.

        Args:
            dsn: PostgreSQL connection string. Defaults to config.dsn()
            embedder: EmbeddingProvider instance. Defaults to OllamaEmbedding()

        Raises:
            KnowledgeError: If dim mismatch between provider and kb_meta
            KnowledgeUnavailable: If PG connection fails
        """
        self._lock = RLock()
        self._dsn = dsn or default_dsn()
        self._embedder = embedder
        self._role: str | None = None
        self._connection: psycopg.Connection | None = None
        # Lazily probed capability flag; see _has_surfaced_column(). Reset on reconnect.
        self._surfaced_col: bool | None = None

        # Graceful absence (G3-1.5): a connectivity failure must NEVER raise from the
        # constructor. The store is meant to be a long-lived, process-wide singleton in
        # the runner — it has to survive a PG restart and a KB that is simply down. So we
        # attempt to connect eagerly, but on OperationalError we degrade to a dormant
        # store (ping() -> False; operations lazily reconnect and raise KnowledgeUnavailable
        # on use, which the runner's safe_recall_block catches). A dim MISMATCH, by
        # contrast, is a real misconfiguration and does raise.
        try:
            self._open()
        except psycopg.OperationalError:
            self._connection = None  # dormant; ping() False, operations reconnect-or-raise

    def _open(self) -> None:
        """(Re)establish the connection and record role. Raises on connect failure.

        autocommit=True is deliberate (council blocker): with the psycopg default
        (autocommit=False) a single server-side query error — statement_timeout, a bad
        halfvec cast, a deadlock — leaves this long-lived process-wide connection in an
        aborted-but-alive transaction (closed=False, so _conn() never reconnects), which
        silently kills recall for the entire runner process. Reads need no transaction;
        every write is a single statement (or an idempotent upsert loop), so per-statement
        commit is correct and self-healing.
        """
        self._surfaced_col = None  # re-probe schema capabilities on every new connection
        conn = psycopg.connect(self._dsn, connect_timeout=5, autocommit=True)
        from pgvector.psycopg import register_vector

        register_vector(conn)
        with conn.cursor() as cur:
            cur.execute("SELECT current_user")
            self._role = _scalar(cur)
        if self._embedder:
            # Never let an embedder outage (Ollama down) raise out of construction — that
            # would kill recall entirely (not even the FTS fallback) and re-probe Ollama on
            # every hot-path recall (council MAJOR). Defer dim reconciliation to first embed.
            try:
                with conn.cursor() as cur:
                    cur.execute("SELECT value FROM kb_meta WHERE key = 'embed_dim'")
                    row = cur.fetchone()
                    if row:
                        stored_dim = int(row[0])
                        provider_dim = self._embedder.dim
                        if stored_dim != provider_dim:
                            conn.close()
                            raise KnowledgeError(
                                f"Embedding dimension mismatch: kb_meta has {stored_dim}, "
                                f"provider has {provider_dim}. Only {EMBED_DIM}d is supported."
                            )
            except EmbeddingUnavailable:
                pass  # embedder offline — construct anyway; recall degrades to FTS-only
        self._connection = conn

    def _conn(self) -> psycopg.Connection:
        """Return a live connection, lazily reconnecting once. Raises KnowledgeUnavailable
        if the KB is unreachable — callers (safe_recall_block) catch this and degrade."""
        if self._connection is None or self._connection.closed:
            try:
                self._open()
            except psycopg.OperationalError as e:
                raise KnowledgeUnavailable(f"Knowledge DB unreachable: {e}") from e
        assert self._connection is not None  # _open() sets it or raised
        return self._connection

    def __enter__(self) -> KnowledgeStore:
        """Context manager entry."""
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        """Context manager exit."""
        self.close()

    def close(self) -> None:
        """Close the database connection."""
        with self._lock:
            if self._connection:
                self._connection.close()
                self._connection = None

    def ping(self) -> bool:
        """Check if the database is reachable."""
        try:
            with self._lock:
                if not self._connection:
                    return False
                with self._connection.cursor() as cur:
                    cur.execute("SELECT 1")
                    return True
        except Exception:
            return False

    def _check_admin_role(self, operation: str) -> None:
        """Raise PromotionDenied if this store is using an agent role."""
        if self._role == "knowledge_agent":
            raise PromotionDenied(
                f"Operation '{operation}' requires admin role, but store is connected as {self._role}"
            )

    # --- Write operations ---

    def add_episode(
        self,
        *,
        source: str,
        content: str,
        source_ref: str | None = None,
        agent_id: str | None = None,
        discipline: str | None = None,
    ) -> int:
        """Add an episode and return its ID."""
        with self._lock:
            conn = self._conn()
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO episodes (source, content, source_ref, agent_id, discipline)
                    VALUES (%s, %s, %s, %s, %s)
                    RETURNING id
                    """,
                    (source, content, source_ref, agent_id, discipline),
                )
                new_id = _scalar(cur)
            conn.commit()
            return new_id

    def add_fact(
        self,
        *,
        statement: str,
        episode_id: int,
        discipline: str | None = None,
        scope: str = "global",
        provenance: str = "extracted",
        trust: float = 0.5,
        confidence: float = 0.7,
        importance: float = 0.5,
        embedding: list[float] | None = None,
        capability_scope: str | None = None,
        company_id: str | None = None,
        domains: list[str] | None = None,
        capability_kind: str | None = None,
        capability_provenance: str | None = None,
        last_verified: str | None = None,
        promoted_from: int | None = None,
    ) -> int:
        """Add a fact and return its ID."""
        with self._lock:
            conn = self._conn()
            with conn.cursor() as cur:
                # Convert embedding to halfvec if provided
                embedding_val = None
                if embedding:
                    # pgvector stores as bytes
                    from pgvector import Vector

                    embedding_val = Vector(embedding)

                cur.execute(
                    """
                    INSERT INTO facts
                    (statement, episode_id, discipline, scope, provenance, trust, confidence,
                     importance, embedding, capability_scope, company_id, domains,
                     capability_kind, capability_provenance, last_verified, promoted_from)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    RETURNING id
                    """,
                    (
                        statement,
                        episode_id,
                        discipline,
                        scope,
                        provenance,
                        trust,
                        confidence,
                        importance,
                        embedding_val,
                        capability_scope,
                        company_id,
                        list(domains or []),
                        capability_kind,
                        capability_provenance,
                        last_verified,
                        promoted_from,
                    ),
                )
                fact_id = _scalar(cur)
            conn.commit()
            return fact_id

    def upsert_entity(
        self,
        *,
        name: str,
        kind: str,
        summary: str | None = None,
        name_embedding: list[float] | None = None,
    ) -> int:
        """Insert or ignore an entity (due to UNIQUE(name, kind) constraint)."""
        with self._lock:
            conn = self._conn()
            with conn.cursor() as cur:
                embedding_val = None
                if name_embedding:
                    from pgvector import Vector

                    embedding_val = Vector(name_embedding)

                cur.execute(
                    """
                    INSERT INTO entities (name, kind, summary, name_embedding)
                    VALUES (%s, %s, %s, %s)
                    ON CONFLICT (name, kind) DO NOTHING
                    RETURNING id
                    """,
                    (name, kind, summary, embedding_val),
                )
                row = cur.fetchone()
                if row:
                    entity_id = row[0]
                else:
                    # Already existed, fetch the ID
                    cur.execute(
                        "SELECT id FROM entities WHERE name = %s AND kind = %s",
                        (name, kind),
                    )
                    entity_id = _scalar(cur)
            conn.commit()
            return entity_id

    def add_edge(
        self,
        *,
        src_kind: str,
        src_id: int,
        dst_kind: str,
        dst_id: int,
        edge_type: str,
        weight: float = 0.5,
    ) -> int:
        """Add an edge or return existing if already present (UNIQUE constraint)."""
        with self._lock:
            conn = self._conn()
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO edges (src_kind, src_id, dst_kind, dst_id, edge_type, weight)
                    VALUES (%s, %s, %s, %s, %s, %s)
                    ON CONFLICT (src_kind, src_id, dst_kind, dst_id, edge_type) DO NOTHING
                    RETURNING id
                    """,
                    (src_kind, src_id, dst_kind, dst_id, edge_type, weight),
                )
                row = cur.fetchone()
                if row:
                    edge_id = row[0]
                else:
                    # Already existed, fetch the ID
                    cur.execute(
                        """
                        SELECT id FROM edges
                        WHERE src_kind = %s AND src_id = %s AND dst_kind = %s AND dst_id = %s AND edge_type = %s
                        """,
                        (src_kind, src_id, dst_kind, dst_id, edge_type),
                    )
                    edge_id = _scalar(cur)
            conn.commit()
            return edge_id

    def supersede_fact(self, loser_id: int, winner_id: int) -> None:
        """Mark a fact as superseded by another."""
        self._check_admin_role("supersede_fact")
        with self._lock:
            conn = self._conn()
            with conn.cursor() as cur:
                now = datetime.now(UTC)
                cur.execute(
                    """
                    UPDATE facts
                    SET status = 'superseded', invalid_at = %s, superseded_by = %s
                    WHERE id = %s
                    """,
                    (now, winner_id, loser_id),
                )
            conn.commit()

    def invalidate_edge(self, edge_id: int) -> None:
        """Mark an edge as invalid."""
        self._check_admin_role("invalidate_edge")
        with self._lock:
            conn = self._conn()
            with conn.cursor() as cur:
                now = datetime.now(UTC)
                cur.execute("UPDATE edges SET invalid_at = %s WHERE id = %s", (now, edge_id))
            conn.commit()

    def promote_fact(self, fact_id: int, gate: PromotionGate) -> None:
        """Promote a fact to active status (requires PromotionGate)."""
        self._check_admin_role("promote_fact")
        with self._lock:
            conn = self._conn()
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE facts SET status = %s WHERE id = %s",
                    (FactStatus.ACTIVE.value, fact_id),
                )
            conn.commit()

    def activate_capability(self, fact_id: int) -> None:
        """Activate a verified capability inside its already-classified namespace."""
        self._check_admin_role("activate_capability")
        with self._lock:
            conn = self._conn()
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE facts SET status = 'active'
                    WHERE id = %s AND capability_scope IS NOT NULL
                    """,
                    (fact_id,),
                )
                if cur.rowcount != 1:
                    raise KnowledgeError("activate_capability requires a capability fact")
            conn.commit()

    def promote_capability_to_estate(
        self,
        source_fact_id: int,
        *,
        statement: str,
        actor: str,
        last_verified: str,
        embedding: list[float] | None = None,
    ) -> int:
        """Clone one company capability into estate scope and audit it atomically.

        This is intentionally separate from :meth:`promote_fact`: capture activates a
        verified note inside its company, while this operation changes who may see it.
        The source row is retained verbatim for provenance and incident review.
        """
        self._check_admin_role("promote_capability_to_estate")
        with self._lock:
            conn = self._conn()
            embedding_val = None
            if embedding:
                from pgvector import Vector

                embedding_val = Vector(embedding)
            try:
                with conn.transaction(), conn.cursor() as cur:
                    cur.execute(
                        """
                        SELECT episode_id, discipline, provenance, trust, confidence,
                               importance, capability_scope, company_id, domains,
                               capability_kind, capability_provenance
                        FROM facts
                        WHERE id = %s AND invalid_at IS NULL
                        FOR UPDATE
                        """,
                        (source_fact_id,),
                    )
                    source = cur.fetchone()
                    if source is None or source[6] != "company" or not source[7]:
                        raise KnowledgeError("estate promotion requires a company capability note")
                    cur.execute(
                        """
                        INSERT INTO facts
                          (statement, episode_id, discipline, scope, provenance, trust,
                           confidence, importance, embedding, status, capability_scope,
                           company_id, domains, capability_kind, capability_provenance,
                           last_verified, promoted_from)
                        VALUES
                          (%s, %s, %s, 'global', %s, %s, %s, %s, %s, 'active',
                           'estate', NULL, %s, %s, %s, %s, %s)
                        RETURNING id
                        """,
                        (
                            statement,
                            source[0],
                            source[1],
                            source[2],
                            source[3],
                            source[4],
                            source[5],
                            embedding_val,
                            source[8],
                            source[9],
                            source[10],
                            last_verified,
                            source_fact_id,
                        ),
                    )
                    estate_fact_id = int(_scalar(cur))
                    cur.execute(
                        """
                        INSERT INTO capability_promotion_log
                          (source_fact_id, estate_fact_id, source_company_id,
                           source_statement, estate_statement, actor)
                        SELECT %s, %s, company_id, statement, %s, %s
                        FROM facts WHERE id = %s
                        """,
                        (source_fact_id, estate_fact_id, statement, actor, source_fact_id),
                    )
                return estate_fact_id
            except Exception:
                with contextlib.suppress(Exception):
                    conn.rollback()
                raise

    def demote_fact(self, fact_id: int, gate: PromotionGate) -> None:
        """Demote a fact back to quarantined status."""
        self._check_admin_role("demote_fact")
        with self._lock:
            conn = self._conn()
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE facts SET status = %s WHERE id = %s",
                    (FactStatus.QUARANTINED.value, fact_id),
                )
            conn.commit()

    def set_embedding(self, fact_id: int, embedding: list[float]) -> None:
        """Set the embedding for a fact (admin-only)."""
        self._check_admin_role("set_embedding")
        with self._lock:
            conn = self._conn()
            with conn.cursor() as cur:
                from pgvector import Vector

                embedding_val = Vector(embedding)
                cur.execute(
                    "UPDATE facts SET embedding = %s WHERE id = %s",
                    (embedding_val, fact_id),
                )
            conn.commit()

    # --- Read operations ---

    def get_fact(self, fact_id: int) -> Fact | None:
        """Get a fact by ID for internal workflows, without applying a visibility scope.

        Capability promotion needs this unscoped point read. Operator/agent surfaces must
        apply their namespace policy after hydration; the general fact-detail API rejects
        every non-NULL ``capability_scope``.
        """
        with self._lock:
            conn = self._conn()
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT id, statement, discipline, scope, episode_id, provenance, trust,
                           confidence, status, valid_at, recorded_at, invalid_at, superseded_by,
                           importance, access_count, last_accessed, helped_count,
                           capability_scope, company_id, domains, capability_kind,
                           capability_provenance, last_verified, promoted_from
                    FROM facts WHERE id = %s
                    """,
                    (fact_id,),
                )
                row = cur.fetchone()
                if not row:
                    return None
                return self._row_to_fact(row)

    def get_episode(self, episode_id: int) -> Episode | None:
        """Get an episode by ID."""
        with self._lock:
            conn = self._conn()
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT id, source, source_ref, agent_id, discipline, content, occurred_at
                    FROM episodes WHERE id = %s
                    """,
                    (episode_id,),
                )
                row = cur.fetchone()
                if not row:
                    return None
                return self._row_to_episode(row)

    def get_entity(self, name: str, kind: str) -> Entity | None:
        """Get an entity by name and kind."""
        with self._lock:
            conn = self._conn()
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT id, name, kind, summary, created_at
                    FROM entities WHERE name = %s AND kind = %s
                    """,
                    (name, kind),
                )
                row = cur.fetchone()
                if not row:
                    return None
                return self._row_to_entity(row)

    def facts_missing_embedding(self, limit: int = 100) -> list[Fact]:
        """Get NULL-embedding facts across all namespaces for internal repair."""
        with self._lock:
            conn = self._conn()
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT id, statement, discipline, scope, episode_id, provenance, trust,
                           confidence, status, valid_at, recorded_at, invalid_at, superseded_by,
                           importance, access_count, last_accessed, helped_count,
                           capability_scope, company_id, domains, capability_kind,
                           capability_provenance, last_verified, promoted_from
                    FROM facts WHERE embedding IS NULL LIMIT %s
                    """,
                    (limit,),
                )
                return [self._row_to_fact(row) for row in cur.fetchall()]

    # --- Consolidator / integrity helpers (H-10, M-26, M-27) ---

    def list_quarantined_fact_ids(self) -> list[int]:
        """Return live quarantined fact ids."""
        with self._lock:
            conn = self._conn()
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT id FROM facts WHERE status = %s AND invalid_at IS NULL "
                    "AND capability_scope IS NULL ORDER BY id",
                    (FactStatus.QUARANTINED.value,),
                )
                return [int(row[0]) for row in cur.fetchall()]

    def count_non_agent_source_classes(self, fact_id: int) -> int:
        """Count distinct non-agent trust classes for a fact + same-statement peers.

        Agent-authored facts never contribute a trust class (see consolidate predicate).
        """
        fact = self.get_fact(fact_id)
        if not fact:
            return 0
        with self._lock:
            conn = self._conn()
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
                    if author_role != "knowledge_agent":
                        classes.add(str(source))
                return len(classes)

    def list_unresolved_contradiction_peers(self, fact_id: int) -> list[int]:
        """Live peer facts linked by open CONTRADICTS edges (not superseded/invalid)."""
        with self._lock:
            conn = self._conn()
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT CASE WHEN e.src_id = %s THEN e.dst_id ELSE e.src_id END AS peer_id
                    FROM edges e
                    JOIN facts peer ON peer.id = CASE WHEN e.src_id = %s THEN e.dst_id ELSE e.src_id END
                    WHERE e.edge_type = 'contradicts'
                      AND e.invalid_at IS NULL
                      AND e.src_kind = 'fact' AND e.dst_kind = 'fact'
                      AND (e.src_id = %s OR e.dst_id = %s)
                      AND peer.invalid_at IS NULL
                      AND peer.status <> 'superseded'
                      AND peer.capability_scope IS NULL
                    ORDER BY peer_id
                    """,
                    (fact_id, fact_id, fact_id, fact_id),
                )
                return [int(row[0]) for row in cur.fetchall()]

    def list_contradicts_edges(self) -> list[tuple[int, int]]:
        """Open CONTRADICTS fact→fact edges."""
        with self._lock:
            conn = self._conn()
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT src_id, dst_id FROM edges
                    WHERE edge_type = 'contradicts' AND invalid_at IS NULL
                      AND src_kind = 'fact' AND dst_kind = 'fact'
                      AND NOT EXISTS (
                          SELECT 1 FROM facts scoped
                          WHERE scoped.capability_scope IS NOT NULL
                            AND scoped.id IN (edges.src_id, edges.dst_id)
                      )
                    ORDER BY src_id, dst_id
                    """
                )
                return [(int(a), int(b)) for a, b in cur.fetchall()]

    def list_active_embedded(self) -> list[tuple[int, Any, float]]:
        """Active general facts with embeddings: (id, embedding, trust).

        Capability notes have their own namespace-aware lifecycle and must never be
        merged by the general-fact consolidator across company boundaries.
        """
        with self._lock:
            conn = self._conn()
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
                return [(int(r[0]), r[1], float(r[2])) for r in cur.fetchall()]

    # SQL for the authoritative per-source generation. Shared by the predicate and the
    # writers so the two can never disagree about what "current" means. See
    # migrations/007_source_generation.sql and knowledge/evidence.py.
    _CURRENT_SOURCE_GENERATION_SQL = """
        SELECT GREATEST(
            COALESCE((
                SELECT r.current_generation FROM knowledge_source_generation r
                WHERE r.source_identity = %(source)s
            ), 0),
            COALESCE((
                SELECT MAX(pe.promotion_generation) FROM promotion_evidence pe
                WHERE pe.source_identity = %(source)s AND pe.status = 'active'
            ), 0)
        )
    """

    def _current_source_generation_locked(self, cur: psycopg.Cursor, source_identity: str) -> int:
        cur.execute(self._CURRENT_SOURCE_GENERATION_SQL, {"source": source_identity})
        return int(_scalar(cur))

    def current_source_generation(self, source_identity: str) -> int:
        """Authoritative current generation of a source (0 if unknown). Not fact-local."""
        with self._lock:
            conn = self._conn()
            with conn.cursor() as cur:
                return self._current_source_generation_locked(cur, source_identity)

    def advance_source_generation(self, source_identity: str) -> int:
        """Advance a source to its next generation WITHOUT recording evidence (admin-only).

        Every prior PASS from this source immediately stops counting, for every fact, until
        the source re-verifies at the new generation.

        RESIDUAL — no production caller (see ``EvidenceLedger.advance_source_generation``).
        """
        self._check_admin_role("advance_source_generation")
        with self._lock:
            conn = self._conn()
            with conn.cursor() as cur:
                nxt = self._current_source_generation_locked(cur, source_identity) + 1
                cur.execute(
                    """
                    INSERT INTO knowledge_source_generation
                      (source_identity, current_generation)
                    VALUES (%s, %s)
                    ON CONFLICT (source_identity) DO UPDATE
                      SET current_generation = GREATEST(
                              knowledge_source_generation.current_generation,
                              EXCLUDED.current_generation
                          ),
                          updated_at = now()
                    RETURNING current_generation
                    """,
                    (source_identity, nxt),
                )
                result = int(_scalar(cur))
            conn.commit()
            return result

    def has_valid_promotion_evidence(self, fact_id: int) -> bool:
        """True iff every source with promoting evidence for this fact currently PASSes.

        Authoritative and NOT fact-local (M-26/M-27): a generation belongs to the SOURCE,
        so a source that advanced past what it recorded for this fact is stale and blocks
        promotion exactly like a FAIL. The decision itself is delegated to
        ``evidence.evaluate_source_groups`` — the same function the in-memory ledger uses,
        so the two backends cannot drift.

        Fails closed (False) if the evidence tables are absent or unreadable.
        """
        with self._lock:
            conn = self._conn()
            try:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        WITH fact_rows AS (
                            SELECT id, source_identity, provenance, verifier_outcome,
                                   promotion_generation, author_role
                            FROM promotion_evidence
                            WHERE fact_id = %(fact)s AND status = 'active'
                        ),
                        cur_gen AS (
                            SELECT DISTINCT f.source_identity,
                                   GREATEST(
                                       COALESCE((
                                           SELECT r.current_generation
                                           FROM knowledge_source_generation r
                                           WHERE r.source_identity = f.source_identity
                                       ), 0),
                                       COALESCE((
                                           SELECT MAX(pe.promotion_generation)
                                           FROM promotion_evidence pe
                                           WHERE pe.source_identity = f.source_identity
                                             AND pe.status = 'active'
                                       ), 0)
                                   ) AS current_generation
                            FROM fact_rows f
                        )
                        SELECT f.id, f.source_identity, f.provenance, f.verifier_outcome,
                               f.promotion_generation, f.author_role, c.current_generation
                        FROM fact_rows f
                        JOIN cur_gen c ON c.source_identity = f.source_identity
                        ORDER BY f.id
                        """,
                        {"fact": fact_id},
                    )
                    rows = cur.fetchall()
            except Exception:
                # Fail closed, but roll back first: a failed statement leaves the
                # connection in an aborted transaction, and every later query on it
                # would then fail too — turning one missing table into a dead store.
                with contextlib.suppress(Exception):
                    conn.rollback()
                return False

        if not rows:
            return False
        current: dict[str, int] = {}
        evidence_rows: list[PromotionEvidence] = []
        for eid, source, provenance, outcome, generation, author_role, cur_gen in rows:
            current[str(source)] = int(cur_gen)
            evidence_rows.append(
                PromotionEvidence(
                    id=int(eid),
                    fact_id=fact_id,
                    provenance=str(provenance),
                    source_identity=str(source),
                    verifier_outcome=VerifierOutcome(outcome),
                    promotion_generation=int(generation),
                    status=EvidenceStatus.ACTIVE,
                    author_role=str(author_role),
                )
            )
        return evaluate_source_groups(build_source_groups(evidence_rows, current))

    def record_promotion_evidence(
        self,
        *,
        fact_id: int,
        provenance: str,
        source_identity: str,
        verifier_outcome: str,
        promotion_generation: int | None = None,
    ) -> int:
        """Insert a system-authored evidence row (admin-only). Idempotent on unique key.

        ``promotion_generation`` defaults to the source's current authoritative generation
        (at least 1) — "this verifier looked at the source as it stands now". Passing a
        higher generation explicitly ADVANCES the source, which makes that source's older
        evidence stale for every fact.
        """
        self._check_admin_role("record_promotion_evidence")
        with self._lock:
            conn = self._conn()
            with conn.cursor() as cur:
                current = self._current_source_generation_locked(cur, source_identity)
                if promotion_generation is None:
                    promotion_generation = max(current, 1)
                if promotion_generation < 1:
                    raise KnowledgeError("promotion_generation must be >= 1")
                # Raise the source high-water mark so older evidence from this source (for
                # ANY fact) becomes stale. Monotonic — GREATEST never walks it backwards.
                cur.execute(
                    """
                    INSERT INTO knowledge_source_generation
                      (source_identity, current_generation)
                    VALUES (%s, %s)
                    ON CONFLICT (source_identity) DO UPDATE
                      SET current_generation = GREATEST(
                              knowledge_source_generation.current_generation,
                              EXCLUDED.current_generation
                          ),
                          updated_at = now()
                    """,
                    (source_identity, promotion_generation),
                )
                # Idempotent: return existing active row if present.
                cur.execute(
                    """
                    SELECT id FROM promotion_evidence
                    WHERE fact_id = %s
                      AND source_identity = %s
                      AND promotion_generation = %s
                      AND verifier_outcome = %s
                      AND status = 'active'
                    LIMIT 1
                    """,
                    (fact_id, source_identity, promotion_generation, verifier_outcome),
                )
                existing = cur.fetchone()
                if existing:
                    conn.commit()
                    return int(existing[0])
                cur.execute(
                    """
                    INSERT INTO promotion_evidence
                      (fact_id, provenance, source_identity, verifier_outcome, promotion_generation)
                    VALUES (%s, %s, %s, %s, %s)
                    RETURNING id
                    """,
                    (
                        fact_id,
                        provenance,
                        source_identity,
                        verifier_outcome,
                        promotion_generation,
                    ),
                )
                new_id = int(_scalar(cur))
            conn.commit()
            return new_id

    def revoke_promotion_evidence(self, evidence_id: int, *, reason: str) -> None:
        """Revoke an evidence row (admin-only)."""
        self._check_admin_role("revoke_promotion_evidence")
        with self._lock:
            conn = self._conn()
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE promotion_evidence
                    SET status = 'revoked', revoked_at = now(), revoke_reason = %s
                    WHERE id = %s AND status = 'active'
                    """,
                    (reason, evidence_id),
                )
            conn.commit()

    def revoke_promotion_evidence_for_source(
        self, fact_id: int, source_identity: str, *, reason: str
    ) -> list[int]:
        """Revoke every active row this source recorded for this fact (admin-only).

        Used when a decision is withdrawn (e.g. an operator demotes a fact they promoted):
        the evidence that justified the promotion must stop counting, or the next
        consolidation run would silently re-promote on the single-source branch.
        """
        self._check_admin_role("revoke_promotion_evidence_for_source")
        with self._lock:
            conn = self._conn()
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE promotion_evidence
                    SET status = 'revoked', revoked_at = now(), revoke_reason = %s
                    WHERE fact_id = %s AND source_identity = %s AND status = 'active'
                    RETURNING id
                    """,
                    (reason, fact_id, source_identity),
                )
                ids = sorted(int(row[0]) for row in cur.fetchall())
            conn.commit()
            return ids

    def resolve_contradiction_edge(self, loser_id: int, winner_id: int, now: datetime) -> None:
        """Mark contradiction edges between loser and winner as resolved (invalid_at).

        Sets invalid_at on edges in both directions so reruns skip them.
        """
        self._check_admin_role("resolve_contradiction_edge")
        with self._lock:
            conn = self._conn()
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE edges
                    SET invalid_at = %s
                    WHERE edge_type = 'contradicts'
                      AND invalid_at IS NULL
                      AND src_kind = 'fact' AND dst_kind = 'fact'
                      AND (
                        (src_id = %s AND dst_id = %s)
                        OR (src_id = %s AND dst_id = %s)
                      )
                    """,
                    (now, loser_id, winner_id, winner_id, loser_id),
                )
            conn.commit()

    def neighbors(self, node_kind: str, node_id: int, *, limit: int = 50) -> list[Edge]:
        """Get edges connected to a node."""
        with self._lock:
            conn = self._conn()
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT id, src_kind, src_id, dst_kind, dst_id, edge_type, weight, valid_at, invalid_at
                    FROM edges
                    WHERE ((src_kind = %s AND src_id = %s) OR (dst_kind = %s AND dst_id = %s))
                      AND invalid_at IS NULL
                    LIMIT %s
                    """,
                    (node_kind, node_id, node_kind, node_id, limit),
                )
                return [self._row_to_edge(row) for row in cur.fetchall()]

    def graph_snapshot(
        self, *, limit_nodes: int = 500, include_quarantined: bool = False
    ) -> dict[str, Any]:
        """Get a snapshot of the graph for visualization.

        The status and general-fact predicates are applied HERE, before the LIMIT. Both callers
        (api/routes/knowledge.py:get_graph and briefing/gather.py:_promoted_facts) keep
        only active facts, and quarantined is the default and dominant status — migration
        001 defaults to it, the 002 trigger forces it for agent writes, and promotion
        needs >=2 non-agent source classes. Filtering after the LIMIT therefore fills the
        whole window with rows the caller discards, and both surfaces silently report an
        empty graph on a KB that holds active facts. Capability notes are intentionally
        absent from this general/operator graph: their company namespace is meaningful
        only to capability recall. ORDER BY makes which rows the window holds a property
        of the query rather than of heap layout.
        """
        status_filter = "" if include_quarantined else "AND status = 'active'"
        with self._lock:
            conn = self._conn()
            with conn.cursor() as cur:
                # Get facts
                cur.execute(
                    f"""
                    SELECT id, statement, discipline, status, importance, trust
                    FROM facts
                    WHERE invalid_at IS NULL
                      AND capability_scope IS NULL
                      {status_filter}
                    ORDER BY id
                    LIMIT %s
                    """,
                    (limit_nodes,),
                )
                facts = [
                    {
                        "id": row[0],
                        "statement": row[1],
                        "discipline": row[2],
                        "status": row[3],
                        "importance": row[4],
                        "trust": row[5],
                    }
                    for row in cur.fetchall()
                ]

                # Get entities
                cur.execute(
                    """
                    SELECT id, name, kind FROM entities ORDER BY id LIMIT %s
                    """,
                    (limit_nodes,),
                )
                entities = [
                    {"id": row[0], "name": row[1], "kind": row[2]} for row in cur.fetchall()
                ]

                # Get edges
                cur.execute(
                    """
                    SELECT id, src_kind, src_id, dst_kind, dst_id, edge_type, weight
                    FROM edges e
                    WHERE e.invalid_at IS NULL
                      AND NOT EXISTS (
                          SELECT 1
                          FROM facts scoped
                          WHERE scoped.capability_scope IS NOT NULL
                            AND ((e.src_kind = 'fact' AND scoped.id = e.src_id)
                              OR (e.dst_kind = 'fact' AND scoped.id = e.dst_id))
                      )
                    ORDER BY e.id
                    LIMIT %s
                    """,
                    (limit_nodes * 2,),
                )
                edges = [
                    {
                        "id": row[0],
                        "src_kind": row[1],
                        "src_id": row[2],
                        "dst_kind": row[3],
                        "dst_id": row[4],
                        "type": row[5],
                        "weight": row[6],
                    }
                    for row in cur.fetchall()
                ]

            return {"facts": facts, "entities": entities, "edges": edges}

    def stats(self) -> dict[str, Any]:
        """Get statistics about the knowledge base."""
        with self._lock:
            conn = self._conn()
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT
                        COUNT(*) as total,
                        COUNT(CASE WHEN status = 'active' THEN 1 END) as active,
                        COUNT(CASE WHEN status = 'quarantined' THEN 1 END) as quarantined,
                        COUNT(CASE WHEN status = 'superseded' THEN 1 END) as superseded
                    FROM facts
                    WHERE capability_scope IS NULL
                    """
                )
                facts_row = cur.fetchone() or (0, 0, 0, 0)

                cur.execute("SELECT COUNT(*) FROM entities")
                entities_count = _scalar(cur)

                cur.execute("SELECT COUNT(*) FROM edges")
                edges_count = _scalar(cur)

                cur.execute("SELECT COUNT(*) FROM episodes")
                episodes_count = _scalar(cur)

                cur.execute(
                    """
                    SELECT COUNT(*), AVG(latency_ms), AVG(tokens)
                    FROM recall_log
                    """
                )
                recall_row = cur.fetchone() or (0, 0.0, 0.0)

            return {
                "facts": {
                    "total": facts_row[0],
                    "active": facts_row[1],
                    "quarantined": facts_row[2],
                    "superseded": facts_row[3],
                },
                "entities": entities_count,
                "edges": edges_count,
                "episodes": episodes_count,
                "recalls": {
                    "count": recall_row[0] or 0,
                    "avg_latency_ms": float(recall_row[1] or 0),
                    "avg_tokens": float(recall_row[2] or 0),
                },
            }

    def recall_candidates(
        self,
        *,
        embedding: list[float] | None,
        query_text: str,
        discipline: str | None = None,
        agent_id: str | None = None,
        include_quarantined: bool = False,
        k: int = 50,
        mode: str = "full",
        capability_only: bool = False,
        company_id: str | None = None,
        domains: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        """Recall candidate facts using vector + FTS + (full mode) 2-hop graph spread.

        One round-trip. Returns dicts carrying EVERY facts column (to hydrate a Fact with
        zero extra queries) plus the frozen signal keys: vector_rank (int|None), fts_rank
        (int|None), graph_activation (float). Ranks are real 1-based ROW_NUMBER ranks within
        each leg (NULL when the fact was not surfaced by that leg) so recall.py can RRF-fuse
        them. Graph-reached facts respect the same status filter — a quarantined 2-hop
        neighbour never leaks into active recall.
        """
        if embedding is None and not query_text:
            return []

        with self._lock:
            conn = self._conn()
            with conn.cursor() as cur:
                cur.execute("SET LOCAL hnsw.iterative_scan = relaxed_order")

                status_filter = "" if include_quarantined else "AND f.status = 'active'"
                disc_filter = "AND f.discipline = %(disc)s" if discipline else ""
                capability_filter = "AND f.capability_scope IS NULL"
                if capability_only:
                    capability_filter = (
                        "AND f.capability_scope IS NOT NULL "
                        "AND (f.capability_scope = 'estate' "
                        "OR (f.capability_scope = 'company' AND f.company_id = %(company)s))"
                    )
                normalized_domains = [str(domain) for domain in (domains or []) if str(domain)]
                params: dict[str, Any] = {
                    "k": k,
                    "disc": discipline,
                    "company": company_id,
                    "domains": normalized_domains,
                }

                legs: list[str] = []
                if embedding is not None:
                    from pgvector import Vector

                    params["emb"] = Vector(embedding)
                    legs.append(f"""
                    vec AS (
                        SELECT f.id,
                               ROW_NUMBER() OVER (ORDER BY f.embedding <=> %(emb)s::halfvec) AS rank
                        FROM facts f
                        WHERE f.embedding IS NOT NULL {status_filter} {disc_filter} {capability_filter}
                        ORDER BY f.embedding <=> %(emb)s::halfvec
                        LIMIT %(k)s
                    )""")
                else:
                    legs.append(
                        "vec AS (SELECT NULL::bigint AS id, NULL::bigint AS rank WHERE false)"
                    )

                if query_text:
                    params["qt"] = query_text
                    legs.append(f"""
                    fts AS (
                        SELECT f.id,
                               ROW_NUMBER() OVER (
                                   ORDER BY ts_rank_cd(f.search_tsv, plainto_tsquery('simple', %(qt)s)) DESC
                               ) AS rank
                        FROM facts f
                        WHERE f.search_tsv @@ plainto_tsquery('simple', %(qt)s)
                              {status_filter} {disc_filter} {capability_filter}
                        LIMIT %(k)s
                    )""")
                else:
                    legs.append(
                        "fts AS (SELECT NULL::bigint AS id, NULL::bigint AS rank WHERE false)"
                    )

                spread_cte = ""
                cand_union = "SELECT id FROM vec UNION SELECT id FROM fts"
                if mode == "full":
                    # Bounded 2-hop spreading activation over fact->fact edges from the
                    # vector+FTS seed. Depth-capped (<=2) and floor-gated (>=0.05) so it
                    # always terminates even with cycles. decay = 0.5 per hop.
                    spread_cte = """,
                    seed AS (SELECT id FROM vec WHERE id IS NOT NULL
                             UNION SELECT id FROM fts WHERE id IS NOT NULL),
                    rspread AS (
                        WITH RECURSIVE r(fact_id, activation, depth) AS (
                            SELECT e.src_id, (e.weight::real * 0.5)::real, 1
                            FROM edges e
                            WHERE e.dst_kind = 'fact' AND e.src_kind = 'fact'
                              AND e.invalid_at IS NULL
                              AND e.dst_id IN (SELECT id FROM seed)
                            UNION ALL
                            SELECT e.src_id, (r.activation * e.weight::real * 0.5)::real, r.depth + 1
                            FROM edges e
                            JOIN r ON e.dst_id = r.fact_id
                            WHERE e.dst_kind = 'fact' AND e.src_kind = 'fact'
                              AND e.invalid_at IS NULL
                              AND r.depth < 2
                              AND (r.activation * e.weight::real * 0.5) >= 0.05
                        )
                        SELECT fact_id, SUM(activation)::real AS activation
                        FROM r GROUP BY fact_id
                    )"""
                    cand_union = (
                        "SELECT id FROM vec WHERE id IS NOT NULL "
                        "UNION SELECT id FROM fts WHERE id IS NOT NULL "
                        "UNION SELECT fact_id AS id FROM rspread"
                    )

                spread_join = "LEFT JOIN rspread s ON s.fact_id = f.id" if mode == "full" else ""
                spread_sel = "COALESCE(s.activation, 0.0)::real" if mode == "full" else "0.0::real"
                domain_sel = (
                    "CASE WHEN cardinality(%(domains)s::text[]) > 0 "
                    "AND f.domains && %(domains)s::text[] THEN 1.0 ELSE 0.0 END"
                )

                final_query = f"""
                WITH {", ".join(legs)}{spread_cte},
                cand AS ({cand_union})
                SELECT f.*,
                       v.rank  AS vector_rank,
                       ft.rank AS fts_rank,
                       {spread_sel} AS graph_activation,
                       {domain_sel} AS domain_match
                FROM cand c
                JOIN facts f ON f.id = c.id
                LEFT JOIN vec v  ON v.id  = f.id
                LEFT JOIN fts ft ON ft.id = f.id
                {spread_join}
                WHERE f.invalid_at IS NULL {status_filter} {capability_filter}
                ORDER BY LEAST(COALESCE(v.rank, 1000000), COALESCE(ft.rank, 1000000))
                LIMIT %(k)s
                """

                cur.execute(final_query, params)
                cols = [d.name for d in (cur.description or [])]
                results = [dict(zip(cols, row, strict=False)) for row in cur.fetchall()]

                return results

    def bump_access(self, fact_ids: list[int]) -> None:
        """Increment access_count and update last_accessed for facts."""
        if not fact_ids:
            return
        with self._lock:
            conn = self._conn()
            with conn.cursor() as cur:
                now = datetime.now(UTC)
                cur.execute(
                    """
                    UPDATE facts
                    SET access_count = access_count + 1,
                        last_accessed = %s
                    WHERE id = ANY(%s)
                    """,
                    (now, fact_ids),
                )
            conn.commit()

    def strengthen_co_recall(
        self, fact_ids: list[int], *, delta: float = 0.05, cap: float = 1.0
    ) -> None:
        """Strengthen edges between facts that were co-recalled.

        Routed through the admin-owned SECURITY DEFINER function strengthen_co_occurrence
        (migration 002): the agent role no longer holds direct INSERT/UPDATE on edges, so
        it cannot forge arbitrary high-weight edges or zero legitimate ones to steer other
        agents' graph recall. The function only applies a bounded +delta to co_occurs edges.
        """
        if not fact_ids or len(fact_ids) < 2:
            return
        with self._lock:
            conn = self._conn()
            with conn.cursor() as cur:
                for i, src_id in enumerate(fact_ids):
                    for dst_id in fact_ids[i + 1 :]:
                        cur.execute(
                            "SELECT strengthen_co_occurrence(%s::bigint, %s::bigint, %s::real, %s::real)",
                            (src_id, dst_id, delta, cap),
                        )
            conn.commit()

    def _has_surfaced_column(self) -> bool:
        """Whether recall_log.surfaced_fact_ids exists (knowledge migration 005).

        Per-fact attribution must not become a hard dependency of recall. On a database
        that has not had 005 applied yet, an INSERT naming the column raises
        UndefinedColumn — and because record_recall runs inside recall(), whose only
        caller-facing wrapper (safe_recall_block) swallows every exception, the visible
        symptom would be the runner silently injecting NO knowledge at all. So probe
        once and fall back to the legacy whole-run path instead.

        Cached per connection and cleared by _open(), so applying the migration takes
        effect on the next reconnect without restarting the runner process.
        """
        if self._surfaced_col is None:
            with self._conn().cursor() as cur:
                # to_regclass resolves through search_path exactly as the INSERT below
                # will, so the probe can never answer for a same-named table in a
                # different schema than the one actually written to.
                cur.execute(
                    "SELECT 1 FROM pg_attribute "
                    "WHERE attrelid = to_regclass('recall_log') "
                    "AND attname = 'surfaced_fact_ids' AND NOT attisdropped"
                )
                self._surfaced_col = cur.fetchone() is not None
        return self._surfaced_col

    def record_recall(
        self,
        *,
        run_id: str,
        agent_id: str | None,
        discipline: str | None,
        query_digest: str,
        fact_ids: list[int],
        tokens: int,
        latency_ms: float,
        surfaced_fact_ids: list[int] | None = None,
    ) -> int:
        """Record a recall event.

        ``fact_ids`` is everything that was RANKED. ``surfaced_fact_ids`` is the subset
        the renderer actually emitted into the injected block within the character
        budget — the only facts that could possibly have influenced the agent. Passing
        None writes NULL, which record_helped() reads as "attribution unknown" and
        treats with the legacy whole-run fallback (see migration 005).
        """
        with self._lock:
            conn = self._conn()
            per_fact = self._has_surfaced_column()
            with conn.cursor() as cur:
                if per_fact:
                    cur.execute(
                        """
                        INSERT INTO recall_log (run_id, agent_id, discipline, query_digest,
                                                fact_ids, tokens, latency_ms, surfaced_fact_ids)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                        RETURNING id
                        """,
                        (
                            run_id,
                            agent_id,
                            discipline,
                            query_digest,
                            fact_ids,
                            tokens,
                            latency_ms,
                            None if surfaced_fact_ids is None else list(surfaced_fact_ids),
                        ),
                    )
                else:
                    cur.execute(
                        """
                        INSERT INTO recall_log (run_id, agent_id, discipline, query_digest, fact_ids, tokens, latency_ms)
                        VALUES (%s, %s, %s, %s, %s, %s, %s)
                        RETURNING id
                        """,
                        (run_id, agent_id, discipline, query_digest, fact_ids, tokens, latency_ms),
                    )
                new_id = _scalar(cur)
            conn.commit()
            return new_id

    def record_helped(self, run_id: str) -> None:
        """Increment helped_count for the facts this run actually SAW.

        Credit is per-fact, not per-run: recall ranks up to 50 candidates and logs all
        of them, but only the prefix that fits the render budget reaches the agent. The
        truncated tail demonstrably influenced nothing, so crediting it would feed the
        ranking's usefulness term (recall._usefulness) with pure noise. Rows written
        before migration 005 carry NULL attribution and keep the old whole-run
        behaviour via COALESCE rather than losing their history.
        """
        with self._lock:
            conn = self._conn()
            # Literal chosen from a fixed two-element set — never caller-influenced.
            credited = (
                "COALESCE(surfaced_fact_ids, fact_ids)"
                if self._has_surfaced_column()
                else "fact_ids"
            )
            with conn.cursor() as cur:
                cur.execute(
                    f"""
                    UPDATE facts
                    SET helped_count = helped_count + 1
                    WHERE id = ANY(
                        SELECT UNNEST({credited}) FROM recall_log WHERE run_id = %s
                    )
                    """,
                    (run_id,),
                )
            conn.commit()

    # --- Helper methods ---

    def _row_to_fact(self, row: tuple[Any, ...]) -> Fact:
        """Convert a database row to a Fact model."""
        return Fact(
            id=row[0],
            statement=row[1],
            discipline=row[2],
            scope=row[3],
            episode_id=row[4],
            provenance=row[5],
            trust=row[6],
            confidence=row[7],
            status=FactStatus(row[8]),
            valid_at=row[9],
            recorded_at=row[10],
            invalid_at=row[11],
            superseded_by=row[12],
            importance=row[13],
            access_count=row[14],
            last_accessed=row[15],
            helped_count=row[16],
            capability_scope=row[17],
            company_id=row[18],
            domains=list(row[19] or []),
            capability_kind=row[20],
            capability_provenance=row[21],
            last_verified=row[22],
            promoted_from=row[23],
        )

    def _row_to_episode(self, row: tuple[Any, ...]) -> Episode:
        """Convert a database row to an Episode model."""
        return Episode(
            id=row[0],
            source=row[1],
            source_ref=row[2],
            agent_id=row[3],
            discipline=row[4],
            content=row[5],
            occurred_at=row[6],
        )

    def _row_to_entity(self, row: tuple[Any, ...]) -> Entity:
        """Convert a database row to an Entity model."""
        return Entity(id=row[0], name=row[1], kind=row[2], summary=row[3], created_at=row[4])

    def _row_to_edge(self, row: tuple[Any, ...]) -> Edge:
        """Convert a database row to an Edge model."""
        return Edge(
            id=row[0],
            src_kind=row[1],
            src_id=row[2],
            dst_kind=row[3],
            dst_id=row[4],
            edge_type=row[5],
            weight=row[6],
            valid_at=row[7],
            invalid_at=row[8],
        )
