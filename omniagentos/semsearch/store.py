"""PostgreSQL/pgvector persistence for the unified semantic index."""

from __future__ import annotations

import hashlib
import os
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Literal

import psycopg
from pgvector import Vector

from omniagentos.knowledge.contracts import ENV_DSN

from .constants import MAX_RESULT_COUNT

SemKind = Literal["skill", "tool", "capability"]
ALL_KINDS: tuple[SemKind, ...] = ("skill", "tool", "capability")
CONNECTION_OPTIONS = "-c statement_timeout=5000 -c lock_timeout=3000"


class SemsearchUnavailable(RuntimeError):
    """The optional semantic index cannot currently be reached."""


@dataclass(frozen=True, slots=True)
class EmbeddedDocument:
    """One corpus document and its already-computed embedding."""

    kind: SemKind
    ref_id: str
    text: str
    content_hash: str
    embedding: list[float]
    model_id: str


@dataclass(frozen=True, slots=True)
class StoredHit:
    """Raw vector result before presentation titles are resolved."""

    kind: SemKind
    ref_id: str
    text: str
    score: float


def content_hash(text: str) -> str:
    """Return the stable digest used to skip unchanged corpus documents."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def configured_dsn() -> str:
    """Return the explicitly configured knowledge DSN, or fail closed.

    Unlike the core knowledge store, semantic search is an optional enhancement.
    It must not probe the production-shaped default when the shared DSN variable
    is absent; callers can immediately take the lexical fallback instead.
    """
    dsn = os.environ.get(ENV_DSN, "").strip()
    if not dsn:
        raise SemsearchUnavailable(f"{ENV_DSN} is unset")
    return dsn


class SemsearchStore:
    """Thin, autocommit pgvector store with lazy connection establishment."""

    def __init__(self, dsn: str | None = None) -> None:
        self._dsn = dsn if dsn is not None else configured_dsn()
        if not self._dsn.strip():
            raise SemsearchUnavailable(f"{ENV_DSN} is unset")
        self._connection: psycopg.Connection | None = None

    def _conn(self) -> psycopg.Connection:
        if self._connection is None or self._connection.closed:
            try:
                connection = psycopg.connect(
                    self._dsn,
                    connect_timeout=5,
                    options=CONNECTION_OPTIONS,
                    autocommit=True,
                )
                from pgvector.psycopg import register_vector

                register_vector(connection)
                self._connection = connection
            except psycopg.Error as exc:
                raise SemsearchUnavailable(f"semantic Postgres unreachable: {exc}") from exc
        return self._connection

    def close(self) -> None:
        """Close the cached connection, if one was opened."""
        if self._connection is not None:
            self._connection.close()
            self._connection = None

    def __enter__(self) -> SemsearchStore:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    def signatures(self, kind: SemKind) -> dict[str, tuple[str, str]]:
        """Return ``ref_id -> (content_hash, model_id)`` for one corpus kind."""
        try:
            with self._conn().cursor() as cursor:
                cursor.execute(
                    "SELECT ref_id, content_hash, model_id "
                    "FROM semsearch_embeddings WHERE kind = %s",
                    (kind,),
                )
                return {str(row[0]): (str(row[1]), str(row[2])) for row in cursor.fetchall()}
        except psycopg.Error as exc:
            raise SemsearchUnavailable(f"semantic signature query failed: {exc}") from exc

    def upsert_many(self, documents: Iterable[EmbeddedDocument]) -> int:
        """Idempotently upsert documents and return the number actually changed."""
        rows = [
            (
                document.kind,
                document.ref_id,
                document.text,
                document.content_hash,
                Vector(document.embedding),
                document.model_id,
            )
            for document in documents
        ]
        if not rows:
            return 0
        try:
            with self._conn().cursor() as cursor:
                cursor.executemany(
                    """
                    INSERT INTO semsearch_embeddings
                        (kind, ref_id, text, content_hash, embedding, model_id)
                    VALUES (%s, %s, %s, %s, %s, %s)
                    ON CONFLICT (kind, ref_id) DO UPDATE SET
                        text = EXCLUDED.text,
                        content_hash = EXCLUDED.content_hash,
                        embedding = EXCLUDED.embedding,
                        model_id = EXCLUDED.model_id,
                        updated_at = now()
                    WHERE semsearch_embeddings.content_hash <> EXCLUDED.content_hash
                       OR semsearch_embeddings.model_id <> EXCLUDED.model_id
                    """,
                    rows,
                )
                return max(0, cursor.rowcount or 0)
        except psycopg.Error as exc:
            raise SemsearchUnavailable(f"semantic upsert failed: {exc}") from exc

    def delete_refs(self, kind: SemKind, ref_ids: Iterable[str]) -> int:
        """Delete corpus rows that are no longer servable/enumerable."""
        stale = list(ref_ids)
        if not stale:
            return 0
        try:
            with self._conn().cursor() as cursor:
                cursor.execute(
                    "DELETE FROM semsearch_embeddings WHERE kind = %s AND ref_id = ANY(%s)",
                    (kind, stale),
                )
                return max(0, cursor.rowcount or 0)
        except psycopg.Error as exc:
            raise SemsearchUnavailable(f"semantic stale-row cleanup failed: {exc}") from exc

    def query(
        self,
        embedding: list[float],
        *,
        model_id: str,
        kind: SemKind | None,
        limit: int,
    ) -> list[StoredHit]:
        """Cosine-rank rows for the active embedding model and optional kind."""
        clauses = ["model_id = %(model_id)s"]
        params: dict[str, object] = {
            "embedding": Vector(embedding),
            "model_id": model_id,
            "limit": min(MAX_RESULT_COUNT, max(0, int(limit))),
        }
        if kind is not None:
            clauses.append("kind = %(kind)s")
            params["kind"] = kind
        sql = (
            "SELECT kind, ref_id, text, "
            "1 - (embedding <=> %(embedding)s::halfvec) AS score "
            "FROM semsearch_embeddings WHERE "
            + " AND ".join(clauses)
            + " ORDER BY embedding <=> %(embedding)s::halfvec, kind, ref_id LIMIT %(limit)s"
        )
        try:
            with self._conn().cursor() as cursor:
                cursor.execute(sql, params)
                return [
                    StoredHit(
                        kind=str(row[0]),  # type: ignore[arg-type]
                        ref_id=str(row[1]),
                        text=str(row[2]),
                        score=float(row[3]),
                    )
                    for row in cursor.fetchall()
                ]
        except psycopg.Error as exc:
            raise SemsearchUnavailable(f"semantic vector query failed: {exc}") from exc
