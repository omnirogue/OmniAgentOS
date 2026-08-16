"""Semantic + hybrid file search over the catalog, plus the DEEP chunk-level index.

``semantic_search`` finds "the doc about X" by MEANING (cosine similarity over the
catalog's embeddings — numpy brute force, instant at the curated thousands-of-files
scale). ``hybrid_search`` fuses that with the live keyword search (Spotlight) via
Reciprocal Rank Fusion — the scale-free combiner the retrieval literature favors, so a
result that BOTH backends rank highly wins. Everything degrades to keyword-only when no
embedding backend / no catalog is present.

DEEP SEMANTIC LAYER (``semantic_index`` / ``semantic_query``): a chunk-level pgvector
index in the knowledge Postgres — its OWN table (``file_embeddings``, knowledge
migration 004), never mixed into knowledge facts. Full text (capped) is extracted,
chunked ~1500 chars, embedded, and stored with root/category/mtime so queries can
filter in SQL and rank by cosine similarity.

PRIVACY: embeddings are computed by the knowledge subsystem's LOCAL Ollama embedder
(bge-m3 at localhost:11434) and stored in the LOCAL knowledge Postgres. File content
NEVER leaves this machine — no cloud API is involved anywhere in this layer.

Extraction coverage (best-effort, placeholder-safe — dataless cloud files are never
read, so indexing can't trigger downloads):
  * read directly: .txt .md .markdown .rst .tex .log .csv .tsv
  * via macOS ``textutil``: .rtf .doc .docx .html .htm .odt
  * .pdf: ``pdftotext`` when installed (poppler), else Spotlight's
    kMDItemTextContent via ``mdls`` — rarely populated, so most PDFs remain
    name-only (still findable via the catalog layer above).
Files whose extraction yields nothing are remembered by (path, mtime) so they are
not re-probed every cycle. Indexing is incremental by (path, mtime) and budgeted:
each pass stops after ``budget_seconds`` and persists a path cursor
(``var/filesearch/semantic_cursor.json``) so the next scheduled cycle resumes
where this one stopped.
"""

from __future__ import annotations

import json
import os
import shutil
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import numpy as np

from omniagentos.filesearch import catalog, embeddings
from omniagentos.filesearch.service import _UF_DATALESS, Runner, SearchHit, _run, search_files
from omniagentos.retrieval import fusion

_RRF_K = fusion.RRF_K  # the ONE rank-fusion implementation lives in omniagentos.retrieval


def _row_to_hit(row: dict, score: float) -> SearchHit:
    mtime = row["mtime"] or 0.0
    modified = time.strftime("%Y-%m-%d", time.localtime(mtime)) if mtime else None
    return SearchHit(
        path=row["path"],
        name=row["name"],
        source=row["source"],
        kind=row["ext"] or "file",
        size=row["size"],
        modified=modified,
        snippet="",
        score=float(score),
        _mtime=mtime,
    )


def semantic_search(
    query: str, limit: int = 20, model: str = embeddings.DEFAULT_MODEL
) -> list[SearchHit]:
    """Meaning-based search over the catalog. ``[]`` when the query can't be embedded or
    the catalog has no embeddings (caller falls back to keyword)."""
    query_vec = embeddings.embed_one(query, model=model)
    if query_vec is None:
        return []
    q = np.asarray(query_vec, dtype=np.float32)
    q = q / (np.linalg.norm(q) or 1.0)

    conn = catalog._connect()
    try:
        rows = conn.execute(
            "SELECT path, source, name, ext, size, mtime, embedding FROM files "
            "WHERE embedding IS NOT NULL AND dim = ?",
            (int(q.shape[0]),),
        ).fetchall()
    finally:
        conn.close()
    if not rows:
        return []

    matrix = np.stack([np.frombuffer(row["embedding"], dtype=np.float32) for row in rows])
    norms = np.linalg.norm(matrix, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    sims = (matrix / norms) @ q
    order = np.argsort(-sims)[:limit]
    return [_row_to_hit(rows[i], float(sims[i])) for i in order]


def hybrid_search(
    query: str,
    limit: int = 20,
    scopes: list[str] | None = None,
    model: str = embeddings.DEFAULT_MODEL,
) -> list[SearchHit]:
    """Keyword (Spotlight) + semantic (catalog), fused with Reciprocal Rank Fusion."""
    keyword = search_files(query, scopes=scopes, limit=limit * 2)
    semantic = semantic_search(query, limit=limit * 2, model=model)

    # Identity across the two legs is the realpath (Spotlight and the catalog can spell
    # the same file differently). The keyword leg is listed FIRST so its hit — the one
    # carrying the snippet — is the payload that survives on an overlap.
    # start=0 and dedupe=False reproduce this call site's long-standing scoring exactly.
    def _key(hit: SearchHit) -> str:
        return os.path.realpath(hit.path)

    ranked = fusion.fuse(
        {
            "keyword": fusion.to_ranked(keyword, _key, start=0, dedupe=False),
            "semantic": fusion.to_ranked(semantic, _key, start=0, dedupe=False),
        },
        k=_RRF_K,
    )[:limit]
    results: list[SearchHit] = []
    for entry in ranked:
        hit: SearchHit = entry.payload
        hit.score = round(float(entry.score), 5)
        results.append(hit)
    return results


# ---------------------------------------------------------------------------
# Deep semantic layer: chunk-level pgvector index (knowledge Postgres,
# table `file_embeddings` — knowledge migration 004). See module docstring for
# the privacy statement and extraction coverage.
# ---------------------------------------------------------------------------

SEMANTIC_CHUNK_CHARS = 1500  # ~1500-char chunks, paragraph-aware
SEMANTIC_MAX_TEXT_CHARS = 2_000_000  # 2 MB text cap per file
SEMANTIC_MAX_CHUNKS_PER_FILE = 120  # one huge file can't eat a whole cycle
SEMANTIC_BUDGET_SECONDS = 600.0  # ~10 min per 2h index cycle; cursor resumes
SEMANTIC_EXCERPT_CHARS = 400
_SEM_EMBED_BATCH = 16

_SEM_DIRECT_EXT = frozenset({".txt", ".md", ".markdown", ".rst", ".tex", ".log", ".csv", ".tsv"})
_SEM_TEXTUTIL_EXT = frozenset({".rtf", ".doc", ".docx", ".html", ".htm", ".odt"})
_SEM_PDF_EXT = frozenset({".pdf"})
SEMANTIC_EXTRACTABLE_EXT = _SEM_DIRECT_EXT | _SEM_TEXTUTIL_EXT | _SEM_PDF_EXT


class SemanticUnavailable(RuntimeError):
    """The deep layer's backend (knowledge Postgres or local Ollama embedder) is down."""


def chunk_text(text: str, size: int = SEMANTIC_CHUNK_CHARS) -> list[str]:
    """Split into ~``size``-char chunks, preferring paragraph then line boundaries.

    Boundaries are searched in the back half of the window so progress is always
    at least ``size // 2`` chars — no pathological input can loop.
    """
    text = text.strip()
    chunks: list[str] = []
    while text:
        if len(text) <= size:
            chunks.append(text)
            break
        cut = text.rfind("\n\n", size // 2, size)
        if cut == -1:
            cut = text.rfind("\n", size // 2, size)
        if cut == -1:
            cut = size
        chunks.append(text[:cut].strip())
        text = text[cut:].strip()
    return [chunk for chunk in chunks if chunk]


def extract_text(path: str, ext: str, runner: Runner | None = None) -> str:
    """Best-effort plain text for a file (capped at 2 MB); '' when not extractable.

    NEVER reads a dataless (cloud-only) file — extraction can't trigger a download.
    """
    runner = runner or _run
    try:
        stat = os.stat(path)
    except OSError:
        return ""
    if getattr(stat, "st_flags", 0) & _UF_DATALESS or stat.st_size <= 0:
        return ""
    ext = ext.lower()
    if ext in _SEM_DIRECT_EXT:
        try:
            with open(path, encoding="utf-8", errors="ignore") as handle:
                return handle.read(SEMANTIC_MAX_TEXT_CHARS)
        except OSError:
            return ""
    if ext in _SEM_TEXTUTIL_EXT:
        return runner(["textutil", "-convert", "txt", "-stdout", path])[:SEMANTIC_MAX_TEXT_CHARS]
    if ext in _SEM_PDF_EXT:
        if shutil.which("pdftotext"):
            out = runner(["pdftotext", "-q", path, "-"])
            if out.strip():
                return out[:SEMANTIC_MAX_TEXT_CHARS]
        out = runner(["mdls", "-name", "kMDItemTextContent", "-raw", path]).strip()
        if out and out != "(null)":
            return out[:SEMANTIC_MAX_TEXT_CHARS]
        return ""
    return ""


def _vec_literal(vec: Any) -> str:
    """Serialize a vector to pgvector's text form (cast with ``::vector`` in SQL)."""
    return "[" + ",".join(f"{float(v):.6f}" for v in vec) + "]"


def semantic_sql(root: str | None, category: str | None, limit: int) -> tuple[str, dict[str, Any]]:
    """The top-k cosine query SQL + params. Pure (unit-testable without Postgres).

    The query vector binds as ``%(vec)s`` (a pgvector text literal); score is
    cosine similarity (``1 - cosine_distance``).
    """
    sql = (
        "SELECT path, root, category, mtime, chunk_ix, excerpt, "
        "1 - (embedding <=> %(vec)s::vector) AS score FROM file_embeddings"
    )
    clauses: list[str] = []
    if root:
        clauses.append("root = %(root)s")
    if category:
        clauses.append("category = %(category)s")
    if clauses:
        sql += " WHERE " + " AND ".join(clauses)
    sql += " ORDER BY embedding <=> %(vec)s::vector LIMIT %(limit)s"
    return sql, {"root": root, "category": category, "limit": int(limit)}


class PgFileStore:
    """Thin psycopg wrapper for ``file_embeddings`` in the knowledge Postgres.

    DSN wiring reuses the knowledge subsystem's config exactly
    (:func:`omniagentos.knowledge.config.dsn` — the knowledge_agent role, which
    migration 004 grants full DML on this one table). Injectable in tests.
    """

    def __init__(self, dsn: str | None = None) -> None:
        if dsn is None:
            from omniagentos.knowledge.config import dsn as knowledge_dsn

            dsn = knowledge_dsn()
        self._dsn = dsn
        self._conn: Any = None

    def _connection(self) -> Any:
        import psycopg

        if self._conn is None or self._conn.closed:
            try:
                self._conn = psycopg.connect(self._dsn, connect_timeout=5, autocommit=True)
            except psycopg.OperationalError as exc:
                raise SemanticUnavailable(f"knowledge Postgres unreachable: {exc}") from exc
        return self._conn

    def existing_mtimes(self) -> dict[str, float]:
        with self._connection().cursor() as cur:
            cur.execute("SELECT path, max(mtime) FROM file_embeddings GROUP BY path")
            return {row[0]: float(row[1]) for row in cur.fetchall()}

    def replace(
        self,
        path: str,
        root: str,
        category: str,
        mtime: float,
        chunks: list[tuple[int, list[float], str]],
    ) -> None:
        conn = self._connection()
        with conn.cursor() as cur:
            cur.execute("DELETE FROM file_embeddings WHERE path = %s", (path,))
            for chunk_ix, vec, excerpt in chunks:
                cur.execute(
                    "INSERT INTO file_embeddings "
                    "(path, root, category, mtime, chunk_ix, embedding, excerpt) "
                    "VALUES (%s,%s,%s,%s,%s,%s::vector,%s)",
                    (path, root, category, mtime, chunk_ix, _vec_literal(vec), excerpt),
                )

    def delete_paths(self, paths: list[str]) -> int:
        conn = self._connection()
        removed = 0
        with conn.cursor() as cur:
            for path in paths:
                cur.execute("DELETE FROM file_embeddings WHERE path = %s", (path,))
                removed += cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0
        return removed

    def query(
        self, vec: Any, root: str | None = None, category: str | None = None, limit: int = 20
    ) -> list[dict[str, Any]]:
        import psycopg

        sql, params = semantic_sql(root, category, max(limit * 4, limit))
        params = {**params, "vec": _vec_literal(vec)}
        try:
            with self._connection().cursor() as cur:
                cur.execute(sql, params)
                rows = cur.fetchall()
        except psycopg.Error as exc:
            raise SemanticUnavailable(f"file_embeddings query failed: {exc}") from exc
        return [
            {
                "path": row[0],
                "root": row[1],
                "category": row[2],
                "mtime": float(row[3]),
                "chunk_ix": int(row[4]),
                "excerpt": row[5],
                "score": float(row[6]),
            }
            for row in rows
        ]


def _default_embedder() -> Any:
    """The knowledge subsystem's LOCAL embedder (bge-m3 via Ollama; 'fake' in tests)."""
    from omniagentos.knowledge import config as knowledge_config
    from omniagentos.knowledge.embeddings import FakeEmbedding, OllamaEmbedding

    if knowledge_config.embed_spec() == "fake":
        return FakeEmbedding()
    return OllamaEmbedding()


def _state_path() -> str:
    return str(Path(catalog.catalog_path()).parent / "semantic_cursor.json")


def _load_state(path: str) -> dict[str, Any]:
    try:
        with open(path, encoding="utf-8") as handle:
            state = json.load(handle)
        if isinstance(state, dict):
            return state
    except (OSError, ValueError):
        pass
    return {}


def _save_state(path: str, state: dict[str, Any]) -> None:
    try:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(state, handle)
    except OSError:
        pass  # a lost cursor only means re-scanning already-indexed (skipped) files


def _candidates_from_catalog() -> list[dict[str, Any]]:
    """Extractable files from the metadata catalog, path-ordered (the cursor's order)."""
    exts = sorted(SEMANTIC_EXTRACTABLE_EXT)
    placeholders = ",".join("?" for _ in exts)
    conn = catalog._connect()
    try:
        rows = conn.execute(
            "SELECT path, source, ext, mtime, category, root FROM files "
            f"WHERE ext IN ({placeholders}) ORDER BY path",
            exts,
        ).fetchall()
    finally:
        conn.close()
    out: list[dict[str, Any]] = []
    for row in rows:
        ext = row["ext"] or ""
        out.append(
            {
                "path": row["path"],
                "ext": ext,
                "mtime": float(row["mtime"] or 0.0),
                "category": row["category"] or catalog.category_for(ext),
                "root": row["root"] or catalog.root_label(row["path"], row["source"] or ""),
            }
        )
    return out


def semantic_index(
    budget_seconds: float = SEMANTIC_BUDGET_SECONDS,
    store: Any | None = None,
    embedder: Any | None = None,
    runner: Runner | None = None,
    clock: Callable[[], float] = time.monotonic,
    state_path: str | None = None,
) -> dict[str, Any]:
    """One budgeted, incremental pass of the deep chunk index.

    Candidates come from the metadata catalog (already walked, filtered, and
    labeled). Incremental by (path, mtime); files with no extractable text are
    remembered so they are not re-probed. When the budget expires mid-pass the
    path cursor is persisted and the NEXT cycle resumes after it; a completed
    pass prunes rows for deleted files and resets the cursor.
    """
    runner = runner or _run
    state_path = state_path or _state_path()
    state = _load_state(state_path)
    counts: dict[str, Any] = {
        "examined": 0,
        "embedded_files": 0,
        "chunks": 0,
        "skipped_unchanged": 0,
        "skipped_no_text": 0,
        "pruned": 0,
        "completed_pass": False,
        "cursor": state.get("cursor") or "",
    }
    try:
        store = store or PgFileStore()
        existing = store.existing_mtimes()
    except SemanticUnavailable as exc:
        counts["error"] = str(exc)
        return counts
    if embedder is None:
        try:
            embedder = _default_embedder()
        except Exception as exc:  # noqa: BLE001 -- embedder wiring is best-effort
            counts["error"] = f"embedder unavailable: {exc}"
            return counts

    candidates = _candidates_from_catalog()
    no_text: dict[str, float] = {str(k): float(v) for k, v in (state.get("no_text") or {}).items()}
    cursor = str(state.get("cursor") or "")
    start_ix = 0
    if cursor:
        for i, row in enumerate(candidates):
            if row["path"] > cursor:
                start_ix = i
                break
        else:
            start_ix = len(candidates)

    deadline = clock() + budget_seconds
    interrupted = False
    for row in candidates[start_ix:]:
        if clock() >= deadline:
            interrupted = True
            break
        path = row["path"]
        counts["examined"] += 1
        prev = existing.get(path)
        if prev is not None and abs(prev - row["mtime"]) < 1.0:
            counts["skipped_unchanged"] += 1
            cursor = path
            continue
        marked = no_text.get(path)
        if marked is not None and abs(marked - row["mtime"]) < 1.0:
            counts["skipped_no_text"] += 1
            cursor = path
            continue
        text = extract_text(path, row["ext"], runner)
        if not text.strip():
            no_text[path] = row["mtime"]
            counts["skipped_no_text"] += 1
            cursor = path
            continue
        chunks = chunk_text(text)[:SEMANTIC_MAX_CHUNKS_PER_FILE]
        vectors: list[list[float]] = []
        try:
            for batch_start in range(0, len(chunks), _SEM_EMBED_BATCH):
                vectors.extend(embedder.embed(chunks[batch_start : batch_start + _SEM_EMBED_BATCH]))
        except Exception as exc:  # noqa: BLE001 -- Ollama outage mid-pass: stop, resume next cycle
            counts["error"] = f"embedding backend failed: {exc}"
            interrupted = True
            break  # cursor stays at the previous file so this one retries next cycle
        try:
            store.replace(
                path,
                row["root"],
                row["category"],
                row["mtime"],
                [
                    (ix, vec, chunk[:SEMANTIC_EXCERPT_CHARS])
                    for ix, (vec, chunk) in enumerate(zip(vectors, chunks, strict=True))
                ],
            )
        except SemanticUnavailable as exc:
            counts["error"] = str(exc)
            interrupted = True
            break
        counts["embedded_files"] += 1
        counts["chunks"] += len(chunks)
        cursor = path

    if not interrupted:
        present = {row["path"] for row in candidates}
        stale = [path for path in existing if path not in present]
        if stale:
            try:
                counts["pruned"] = store.delete_paths(stale)
            except SemanticUnavailable as exc:
                counts["error"] = str(exc)
        cursor = ""
        counts["completed_pass"] = True
        no_text = {path: mtime for path, mtime in no_text.items() if path in present}

    state.update({"cursor": cursor, "no_text": no_text, "updated_at": time.time()})
    _save_state(state_path, state)
    counts["cursor"] = cursor
    return counts


def semantic_query(
    query: str,
    root: str | None = None,
    category: str | None = None,
    limit: int = 20,
    store: Any | None = None,
    embedder: Any | None = None,
) -> list[dict[str, Any]]:
    """Meaning-based top-k over the deep chunk index, deduped to the best chunk per
    file. Returns ``{path, root, category, mtime, score, excerpt}`` dicts sorted by
    score. Raises :class:`SemanticUnavailable` when Postgres/Ollama are down (the
    API maps that to a 503)."""
    if embedder is None:
        embedder = _default_embedder()
    try:
        vec = embedder.embed([query])[0]
    except Exception as exc:  # noqa: BLE001 -- local embedder down → clean 503 upstream
        raise SemanticUnavailable(f"local embedder unavailable: {exc}") from exc
    store = store or PgFileStore()
    rows = store.query(vec, root=root, category=category, limit=limit)
    best: dict[str, dict[str, Any]] = {}
    for row in rows:
        current = best.get(row["path"])
        if current is None or row["score"] > current["score"]:
            best[row["path"]] = row
    ranked = sorted(best.values(), key=lambda row: -row["score"])[:limit]
    return [
        {
            "path": row["path"],
            "root": row["root"],
            "category": row["category"],
            "mtime": row["mtime"],
            "score": round(row["score"], 5),
            "excerpt": row["excerpt"],
        }
        for row in ranked
    ]
