"""Deep semantic layer: chunker, incremental (path, mtime) skip, time-budget cursor
resume, no-text markers, top-k SQL shape, and query-side dedupe.

Hermetic — a fake in-memory store stands in for the knowledge Postgres and a
deterministic fake embedder stands in for Ollama. No network, no DB.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest

from omniagentos.filesearch import catalog, semantic


class FakeStore:
    """In-memory stand-in for PgFileStore (same duck-typed surface)."""

    def __init__(self) -> None:
        self.files: dict[str, dict[str, Any]] = {}
        self.replace_calls = 0

    def existing_mtimes(self) -> dict[str, float]:
        return {path: row["mtime"] for path, row in self.files.items()}

    def replace(self, path: str, root: str, category: str, mtime: float, chunks: list) -> None:
        self.replace_calls += 1
        self.files[path] = {
            "root": root,
            "category": category,
            "mtime": mtime,
            "chunks": chunks,
        }

    def delete_paths(self, paths: list[str]) -> int:
        removed = 0
        for path in paths:
            if path in self.files:
                del self.files[path]
                removed += 1
        return removed


class FakeEmbedder:
    def __init__(self) -> None:
        self.calls: list[int] = []

    def embed(self, texts: list[str]) -> list[list[float]]:
        self.calls.append(len(texts))
        return [[1.0, 0.0, 0.0] for _ in texts]


class DownEmbedder:
    def embed(self, texts: list[str]) -> list[list[float]]:
        raise RuntimeError("ollama is down")


# --- chunker --------------------------------------------------------------


def test_chunk_text_prefers_paragraph_boundaries_and_caps_size() -> None:
    text = "intro paragraph\n\n" + ("body " * 400).strip() + "\n\nclosing paragraph"
    chunks = semantic.chunk_text(text, size=1500)
    assert all(len(c) <= 1500 for c in chunks)
    assert chunks[0].startswith("intro paragraph")
    assert chunks[-1].endswith("closing paragraph")


def test_chunk_text_pathological_input_progresses() -> None:
    chunks = semantic.chunk_text("a" * 4000, size=1500)
    assert [len(c) for c in chunks] == [1500, 1500, 1000]
    assert semantic.chunk_text("") == []
    assert semantic.chunk_text("short") == ["short"]


# --- indexer fixtures ------------------------------------------------------


@pytest.fixture
def corpus(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Three real .md files cataloged in a tmp catalog DB (the indexer's source)."""
    monkeypatch.setattr(catalog, "catalog_path", lambda: str(tmp_path / "catalog.db"))
    docs = tmp_path / "docs"
    docs.mkdir()
    conn = catalog._connect()
    for name in ("a.md", "b.md", "c.md"):
        path = docs / name
        path.write_text(f"contents of {name}: " + "text " * 50)
        conn.execute(
            "INSERT INTO files (path, source, name, ext, size, mtime, indexed_at, doc, "
            "category, root) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                str(path),
                "local",
                name,
                ".md",
                100,
                os.stat(path).st_mtime,
                0.0,
                name,
                "documents",
                "desktop",
            ),
        )
    conn.commit()
    conn.close()
    return docs


def _state(tmp_path: Path) -> str:
    return str(tmp_path / "cursor.json")


# --- incremental + cursor ---------------------------------------------------


def test_semantic_index_embeds_then_skips_unchanged(corpus: Path, tmp_path: Path) -> None:
    store, embedder = FakeStore(), FakeEmbedder()
    first = semantic.semantic_index(
        budget_seconds=60, store=store, embedder=embedder, state_path=_state(tmp_path)
    )
    assert first["embedded_files"] == 3
    assert first["chunks"] >= 3
    assert first["completed_pass"] is True
    assert first["cursor"] == ""
    assert set(store.files) == {str(corpus / n) for n in ("a.md", "b.md", "c.md")}
    assert store.files[str(corpus / "a.md")]["root"] == "desktop"

    second = semantic.semantic_index(
        budget_seconds=60, store=store, embedder=embedder, state_path=_state(tmp_path)
    )
    assert second["embedded_files"] == 0
    assert second["skipped_unchanged"] == 3

    # touch one file (and its catalog mtime) → exactly that one re-embeds
    target = corpus / "b.md"
    new_mtime = os.stat(target).st_mtime + 100
    os.utime(target, (new_mtime, new_mtime))
    conn = catalog._connect()
    conn.execute("UPDATE files SET mtime = ? WHERE path = ?", (new_mtime, str(target)))
    conn.commit()
    conn.close()
    third = semantic.semantic_index(
        budget_seconds=60, store=store, embedder=embedder, state_path=_state(tmp_path)
    )
    assert third["embedded_files"] == 1
    assert third["skipped_unchanged"] == 2


def test_semantic_index_budget_cursor_resumes(corpus: Path, tmp_path: Path) -> None:
    store, embedder = FakeStore(), FakeEmbedder()
    # clock: deadline computed at t=0 (budget 10); a.md checked at t=1 (ok),
    # b.md checked at t=11 (over budget) → pass interrupts after ONE file.
    ticks = iter([0.0, 1.0, 11.0])
    first = semantic.semantic_index(
        budget_seconds=10,
        store=store,
        embedder=embedder,
        clock=lambda: next(ticks),
        state_path=_state(tmp_path),
    )
    assert first["embedded_files"] == 1
    assert first["completed_pass"] is False
    assert first["cursor"] == str(corpus / "a.md")
    saved = json.loads(Path(_state(tmp_path)).read_text())
    assert saved["cursor"] == str(corpus / "a.md")

    # next cycle resumes strictly AFTER the cursor and finishes the pass
    second = semantic.semantic_index(
        budget_seconds=60, store=store, embedder=embedder, state_path=_state(tmp_path)
    )
    assert second["embedded_files"] == 2
    assert second["examined"] == 2  # a.md was NOT re-examined
    assert second["completed_pass"] is True
    assert second["cursor"] == ""
    assert len(store.files) == 3


def test_semantic_index_prunes_deleted_on_completed_pass(corpus: Path, tmp_path: Path) -> None:
    store, embedder = FakeStore(), FakeEmbedder()
    semantic.semantic_index(
        budget_seconds=60, store=store, embedder=embedder, state_path=_state(tmp_path)
    )
    # remove one file from the catalog → next full pass prunes its embeddings
    gone = str(corpus / "c.md")
    conn = catalog._connect()
    conn.execute("DELETE FROM files WHERE path = ?", (gone,))
    conn.commit()
    conn.close()
    result = semantic.semantic_index(
        budget_seconds=60, store=store, embedder=embedder, state_path=_state(tmp_path)
    )
    assert result["pruned"] == 1
    assert gone not in store.files


def test_semantic_index_marks_no_text_and_stops_reprobing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(catalog, "catalog_path", lambda: str(tmp_path / "catalog.db"))
    pdf = tmp_path / "scan.pdf"
    pdf.write_bytes(b"%PDF fake, no text layer")
    conn = catalog._connect()
    conn.execute(
        "INSERT INTO files (path, source, name, ext, size, mtime, indexed_at, doc, "
        "category, root) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (
            str(pdf),
            "local",
            "scan.pdf",
            ".pdf",
            24,
            os.stat(pdf).st_mtime,
            0.0,
            "scan",
            "documents",
            "other-mount",
        ),
    )
    conn.commit()
    conn.close()

    probes: list[list[str]] = []

    def runner(argv: list[str]) -> str:
        probes.append(argv)
        return ""  # no extractor output

    store, embedder = FakeStore(), FakeEmbedder()
    first = semantic.semantic_index(
        budget_seconds=60,
        store=store,
        embedder=embedder,
        runner=runner,
        state_path=_state(tmp_path),
    )
    assert first["skipped_no_text"] == 1
    assert store.files == {}
    probes_after_first = len(probes)

    second = semantic.semantic_index(
        budget_seconds=60,
        store=store,
        embedder=embedder,
        runner=runner,
        state_path=_state(tmp_path),
    )
    assert second["skipped_no_text"] == 1
    assert len(probes) == probes_after_first  # remembered — not re-probed


def test_semantic_index_stops_cleanly_when_embedder_dies(corpus: Path, tmp_path: Path) -> None:
    store = FakeStore()
    result = semantic.semantic_index(
        budget_seconds=60, store=store, embedder=DownEmbedder(), state_path=_state(tmp_path)
    )
    assert result["embedded_files"] == 0
    assert "error" in result
    assert result["completed_pass"] is False
    # the failed file was NOT skipped past: cursor stays before it for a retry
    assert result["cursor"] == ""


# --- SQL shape + query ------------------------------------------------------


def test_semantic_sql_shape_with_filters() -> None:
    sql, params = semantic.semantic_sql("desktop", "documents", 20)
    assert "FROM file_embeddings" in sql
    assert "root = %(root)s" in sql
    assert "category = %(category)s" in sql
    assert sql.index("WHERE") < sql.index("ORDER BY")
    assert sql.rstrip().endswith("ORDER BY embedding <=> %(vec)s::vector LIMIT %(limit)s")
    assert "1 - (embedding <=> %(vec)s::vector) AS score" in sql
    assert params == {"root": "desktop", "category": "documents", "limit": 20}


def test_semantic_sql_shape_without_filters() -> None:
    sql, params = semantic.semantic_sql(None, None, 5)
    assert "WHERE" not in sql
    assert params == {"root": None, "category": None, "limit": 5}
    sql_root_only, _ = semantic.semantic_sql("gdrive", None, 5)
    assert "root = %(root)s" in sql_root_only
    assert "category = %(category)s" not in sql_root_only


def test_semantic_query_dedupes_to_best_chunk_per_file() -> None:
    class QueryStore:
        def query(self, vec: Any, root: str | None, category: str | None, limit: int) -> list:
            return [
                {
                    "path": "/a.md",
                    "root": "desktop",
                    "category": "documents",
                    "mtime": 1.0,
                    "chunk_ix": 0,
                    "excerpt": "worse",
                    "score": 0.70,
                },
                {
                    "path": "/a.md",
                    "root": "desktop",
                    "category": "documents",
                    "mtime": 1.0,
                    "chunk_ix": 3,
                    "excerpt": "best",
                    "score": 0.90,
                },
                {
                    "path": "/b.md",
                    "root": "gdrive",
                    "category": "documents",
                    "mtime": 2.0,
                    "chunk_ix": 0,
                    "excerpt": "mid",
                    "score": 0.80,
                },
            ]

    rows = semantic.semantic_query("q", store=QueryStore(), embedder=FakeEmbedder(), limit=10)
    assert [r["path"] for r in rows] == ["/a.md", "/b.md"]
    assert rows[0]["excerpt"] == "best"
    assert set(rows[0]) == {"path", "root", "category", "mtime", "score", "excerpt"}


def test_semantic_query_raises_semantic_unavailable_when_embedder_down() -> None:
    with pytest.raises(semantic.SemanticUnavailable):
        semantic.semantic_query("q", store=FakeStore(), embedder=DownEmbedder())


# --- extraction coverage ----------------------------------------------------


def test_extract_text_direct_and_textutil(tmp_path: Path) -> None:
    md = tmp_path / "x.md"
    md.write_text("hello direct read")
    assert semantic.extract_text(str(md), ".md") == "hello direct read"

    docx = tmp_path / "x.docx"
    docx.write_bytes(b"fake office bytes")
    calls: list[list[str]] = []

    def runner(argv: list[str]) -> str:
        calls.append(argv)
        return "textutil extracted this"

    assert semantic.extract_text(str(docx), ".docx", runner) == "textutil extracted this"
    assert calls[0][:3] == ["textutil", "-convert", "txt"]

    assert semantic.extract_text(str(tmp_path / "missing.md"), ".md") == ""
    unknown = tmp_path / "x.sketch"
    unknown.write_bytes(b"binary")
    assert semantic.extract_text(str(unknown), ".sketch") == ""
