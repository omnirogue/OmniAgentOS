"""Catalog metadata: the category mapping table, root labels, filtering/sorting,
and reindex writing category/root (+ metadata-only media rows, no embedding).

Hermetic — tmp catalog DB, fake embedding backend, tmp document roots.
"""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path

import pytest

from omniagentos.filesearch import catalog, embeddings

_HOME = os.path.expanduser("~")


def test_category_mapping_single_table() -> None:
    assert catalog.category_for(".md") == "documents"
    assert catalog.category_for(".PDF") == "documents"  # case-insensitive
    assert catalog.category_for(".xlsx") == "spreadsheets"
    assert catalog.category_for(".key") == "presentations"
    assert catalog.category_for(".heic") == "images"
    assert catalog.category_for(".mov") == "video"
    assert catalog.category_for(".mp3") == "audio"
    assert catalog.category_for(".py") == "code"
    assert catalog.category_for(".zip") == "archives"
    assert catalog.category_for(".xyz") == "other"
    assert catalog.category_for("") == "other"
    # every category value the API patterns accept is producible from the one table
    assert set(catalog._CATEGORY_BY_EXT.values()) | {"other"} == set(catalog.CATEGORIES)


def test_root_label_mapping() -> None:
    assert catalog.root_label(f"{_HOME}/Desktop/a.md", "local") == "desktop"
    assert catalog.root_label(f"{_HOME}/Coding Projects/x/y.py", "local") == "repo"
    assert catalog.root_label(f"{_HOME}/Work/plan.md", "local") == "repo"
    assert catalog.root_label("/anything/at/all", "icloud") == "icloud"
    assert catalog.root_label("/anything/at/all", "gdrive") == "gdrive"
    assert catalog.root_label(f"{_HOME}/Documents/a.md", "local") == "other-mount"
    assert catalog.root_label(f"{_HOME}/Dropbox/a.md", "dropbox") == "other-mount"
    # "Desktopish" prefixes must not leak into desktop
    assert catalog.root_label(f"{_HOME}/DesktopBackup/a.md", "local") == "other-mount"


@pytest.fixture
def seeded(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(catalog, "catalog_path", lambda: str(tmp_path / "catalog.db"))
    conn = catalog._connect()
    rows = [
        ("/d/report.docx", "local", "report.docx", ".docx", 10, 300.0, "documents", "desktop"),
        ("/d/budget.xlsx", "local", "budget.xlsx", ".xlsx", 11, 200.0, "spreadsheets", "desktop"),
        ("/g/notes.md", "gdrive", "notes.md", ".md", 12, 100.0, "documents", "gdrive"),
    ]
    for path, source, name, ext, size, mtime, category, root in rows:
        conn.execute(
            "INSERT INTO files (path, source, name, ext, size, mtime, indexed_at, doc, "
            "embedding, dim, model, category, root) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (path, source, name, ext, size, mtime, 0.0, name, None, None, None, category, root),
        )
    conn.commit()
    conn.close()


def test_filter_files_by_root_category_and_query(seeded: None) -> None:
    desktop = catalog.filter_files(root="desktop")
    assert {r["name"] for r in desktop} == {"report.docx", "budget.xlsx"}
    assert desktop[0]["name"] == "report.docx"  # recency default: mtime 300 first

    docs = catalog.filter_files(category="documents")
    assert {r["name"] for r in docs} == {"report.docx", "notes.md"}

    assert [r["name"] for r in catalog.filter_files(q="budget")] == ["budget.xlsx"]
    both = catalog.filter_files(root="desktop", category="spreadsheets")
    assert [r["name"] for r in both] == ["budget.xlsx"]
    assert catalog.filter_files(root="icloud") == []


def test_filter_files_sorting_and_limit(seeded: None) -> None:
    by_name = catalog.filter_files(sort="name")
    assert [r["name"] for r in by_name] == ["budget.xlsx", "notes.md", "report.docx"]
    by_recency = catalog.filter_files(sort="recency")
    assert [r["mtime"] for r in by_recency] == [300.0, 200.0, 100.0]
    assert len(catalog.filter_files(limit=2)) == 2
    # row shape carries the metadata the API contract promises
    row = by_recency[0]
    assert {"path", "root", "category", "mtime", "size", "name"} <= set(row)


def test_reindex_writes_category_root_and_skips_media_embedding(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(catalog, "catalog_path", lambda: str(tmp_path / "catalog.db"))
    monkeypatch.setattr(
        embeddings, "embed", lambda texts, model="x", timeout=0.0: [[1.0, 0.0] for _ in texts]
    )
    monkeypatch.setattr(embeddings, "is_available", lambda model=embeddings.DEFAULT_MODEL: True)
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "notes.md").write_text("meeting notes about the plan")
    (docs / "photo.png").write_bytes(b"\x89PNG fake")
    monkeypatch.setattr(catalog, "curated_roots", lambda: [(str(docs), "local")])

    result = catalog.reindex()
    assert result["indexed"] == 2  # media rows ARE cataloged...
    assert result["embedded"] == 1  # ...but only the document row is embedded

    conn = sqlite3.connect(catalog.catalog_path())
    conn.row_factory = sqlite3.Row
    rows = {r["name"]: r for r in conn.execute("SELECT * FROM files")}
    conn.close()
    assert rows["notes.md"]["category"] == "documents"
    assert rows["photo.png"]["category"] == "images"
    assert rows["photo.png"]["embedding"] is None
    assert rows["notes.md"]["embedding"] is not None
    assert rows["notes.md"]["root"] == "other-mount"  # tmp dir is no curated top-level


def test_reindex_backfills_missing_meta(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A row written before the category/root columns existed gets them filled in."""
    monkeypatch.setattr(catalog, "catalog_path", lambda: str(tmp_path / "catalog.db"))
    monkeypatch.setattr(embeddings, "is_available", lambda model=embeddings.DEFAULT_MODEL: False)
    docs = tmp_path / "docs"
    docs.mkdir()
    old = docs / "old.md"
    old.write_text("legacy row")
    monkeypatch.setattr(catalog, "curated_roots", lambda: [(str(docs), "local")])
    conn = catalog._connect()
    conn.execute(
        "INSERT INTO files (path, source, name, ext, size, mtime, indexed_at, doc) "
        "VALUES (?,?,?,?,?,?,?,?)",
        (str(old), "local", "old.md", ".md", 10, os.stat(old).st_mtime, 0.0, "old"),
    )
    conn.commit()
    conn.close()

    result = catalog.reindex()
    assert result["backfilled"] == 1
    assert result["indexed"] == 0  # unchanged mtime → not re-extracted
    row = catalog.lookup(str(old))
    assert row is not None
    assert row["category"] == "documents"
    assert row["root"] == "other-mount"


def test_lookup_exact_path_only(seeded: None) -> None:
    assert catalog.lookup("/d/report.docx") is not None
    assert catalog.lookup("/d/../d/report.docx") is None  # no normalization: exact rows only
    assert catalog.lookup("/d/REPORT.docx") is None
