"""A persistent, incrementally-updated catalog of the user's FILES — the backbone
for semantic search and metadata filtering.

Stores one compact, embeddable "document" per file (name + folder path + a head of
extractable text) and its embedding, in a dedicated SQLite DB (``var/filesearch/
catalog.db``), WITHOUT moving any file — reorganize in metadata space, not on disk. Only
a CURATED set of scopes (Documents/Desktop/Work/Coding Projects/iCloud/Google Drive/
Dropbox) is indexed. Document-like file types get an embeddable doc + embedding; media/
archive types (images, video, audio, archives) are cataloged METADATA-ONLY (name, size,
mtime, category — no content read, no embedding) so they are filterable/sortable.
Every row carries a derived ``category`` (documents|spreadsheets|presentations|images|
video|audio|code|archives|other) and a ``root`` label (desktop|icloud|gdrive|repo|
other-mount) for API-level filtering. Indexing is incremental (skips unchanged files by
mtime) and placeholder-safe (never reads a dataless cloud file — it uses Spotlight
metadata instead, so cataloging can't trigger cloud downloads).
"""

from __future__ import annotations

import glob
import os
import sqlite3
import time
from pathlib import Path
from typing import Any

import numpy as np

from omniagentos.contracts import default_db_path
from omniagentos.filesearch import embeddings
from omniagentos.filesearch.service import _TEXT_EXT, _UF_DATALESS, Runner, _is_noise, _run

# Personal-docs folders walk FIRST; code-checkout areas LAST (see curated_roots: the
# cloud drives walk in between). Order matters under the sanity cap — when it is hit,
# the personal + cloud scopes must win over bulk repo trees (repomap covers code).
_CURATED_LOCAL_RELS: tuple[str, ...] = ("Documents", "Desktop", "Notes")
_CURATED_REPO_RELS: tuple[str, ...] = ("Work", "Coding Projects", "Projects")
_TEXTUTIL_EXT: frozenset[str] = frozenset({".docx", ".doc", ".rtf", ".html", ".htm", ".odt"})
_OTHER_DOC_EXT: frozenset[str] = frozenset(
    {".pdf", ".key", ".pages", ".numbers", ".epub", ".pptx", ".xlsx", ".ppt", ".xls"}
)
_INDEXABLE_EXT: frozenset[str] = _TEXT_EXT | _TEXTUTIL_EXT | _OTHER_DOC_EXT

_DOC_HEAD_CHARS = 1800
_MAX_TEXT_BYTES = 1_000_000
_MAX_OFFICE_BYTES = 4_000_000
# Raised from 8000 (that cap was hit by local+iCloud alone), then from 30000 when
# metadata-only media/archive types joined the catalog (60k was hit immediately — repo
# trees + media pushed past it and truncated Google Drive). Override with
# OMNI_FILESEARCH_MAX_FILES for even fuller coverage. First full index is a one-time
# background cost (~45 docs/sec); subsequent runs are incremental (changed files only).
_DEFAULT_MAX_FILES = int(os.environ.get("OMNI_FILESEARCH_MAX_FILES", "100000"))
_EMBED_BATCH = 32

# ONE extension→category mapping table (the single source of truth for the API's
# category= filter and the semantic layer's category column).
_CATEGORY_BY_EXT: dict[str, str] = {
    # documents
    ".txt": "documents",
    ".md": "documents",
    ".markdown": "documents",
    ".rtf": "documents",
    ".pdf": "documents",
    ".doc": "documents",
    ".docx": "documents",
    ".odt": "documents",
    ".pages": "documents",
    ".tex": "documents",
    ".rst": "documents",
    ".epub": "documents",
    ".html": "documents",
    ".htm": "documents",
    ".log": "documents",
    # spreadsheets
    ".csv": "spreadsheets",
    ".tsv": "spreadsheets",
    ".xls": "spreadsheets",
    ".xlsx": "spreadsheets",
    ".numbers": "spreadsheets",
    ".ods": "spreadsheets",
    # presentations
    ".ppt": "presentations",
    ".pptx": "presentations",
    ".key": "presentations",
    ".odp": "presentations",
    # images
    ".png": "images",
    ".jpg": "images",
    ".jpeg": "images",
    ".gif": "images",
    ".heic": "images",
    ".heif": "images",
    ".webp": "images",
    ".tiff": "images",
    ".tif": "images",
    ".bmp": "images",
    ".svg": "images",
    ".psd": "images",
    ".ai": "images",
    # video
    ".mp4": "video",
    ".mov": "video",
    ".m4v": "video",
    ".avi": "video",
    ".mkv": "video",
    ".webm": "video",
    # audio
    ".mp3": "audio",
    ".m4a": "audio",
    ".wav": "audio",
    ".aiff": "audio",
    ".aif": "audio",
    ".flac": "audio",
    ".ogg": "audio",
    # code
    ".py": "code",
    ".ts": "code",
    ".tsx": "code",
    ".js": "code",
    ".jsx": "code",
    ".json": "code",
    ".yaml": "code",
    ".yml": "code",
    ".sh": "code",
    ".zsh": "code",
    ".sql": "code",
    ".rs": "code",
    ".go": "code",
    ".java": "code",
    ".rb": "code",
    ".toml": "code",
    ".ini": "code",
    ".cfg": "code",
    ".swift": "code",
    ".c": "code",
    ".h": "code",
    ".cpp": "code",
    ".php": "code",
    ".css": "code",
    ".scss": "code",
    ".xml": "code",
    ".env": "code",
    ".ipynb": "code",
    # archives
    ".zip": "archives",
    ".tar": "archives",
    ".gz": "archives",
    ".tgz": "archives",
    ".rar": "archives",
    ".7z": "archives",
    ".dmg": "archives",
}

CATEGORIES: tuple[str, ...] = (
    "documents",
    "spreadsheets",
    "presentations",
    "images",
    "video",
    "audio",
    "code",
    "archives",
    "other",
)
ROOT_LABELS: tuple[str, ...] = ("desktop", "icloud", "gdrive", "repo", "other-mount")

# Media/archive types: cataloged metadata-only (no content read, no embedding).
_METADATA_ONLY_EXT: frozenset[str] = frozenset(
    ext for ext, cat in _CATEGORY_BY_EXT.items() if cat in {"images", "video", "audio", "archives"}
)
_CATALOG_EXT: frozenset[str] = _INDEXABLE_EXT | _METADATA_ONLY_EXT

# Local folders whose contents are code checkouts → root label "repo".
_REPO_RELS: tuple[str, ...] = ("Coding Projects", "Work", "Projects")


def category_for(ext: str) -> str:
    """Derived file category from an extension (with leading dot), else ``other``."""
    return _CATEGORY_BY_EXT.get(ext.lower(), "other")


def root_label(path: str, source: str) -> str:
    """Root label for filtering: desktop|icloud|gdrive|repo|other-mount.

    Cloud sources map directly; local paths are classified by their curated top-level
    folder (``~/Desktop`` → desktop, code checkout areas → repo, the rest → other-mount).
    """
    if source == "icloud":
        return "icloud"
    if source == "gdrive":
        return "gdrive"
    home = os.path.expanduser("~")
    if path.startswith(os.path.join(home, "Desktop") + os.sep):
        return "desktop"
    for rel in _REPO_RELS:
        if path.startswith(os.path.join(home, rel) + os.sep):
            return "repo"
    return "other-mount"


def catalog_path() -> str:
    path = Path(default_db_path()).resolve().parent / "filesearch" / "catalog.db"
    path.parent.mkdir(parents=True, exist_ok=True)
    return str(path)


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(catalog_path(), timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")  # live searches read while a reindex writes
    conn.execute(
        "CREATE TABLE IF NOT EXISTS files ("
        "path TEXT PRIMARY KEY, source TEXT, name TEXT, ext TEXT, size INTEGER, "
        "mtime REAL, indexed_at REAL, doc TEXT, embedding BLOB, dim INTEGER, model TEXT)"
    )
    # Additive schema migration: category + root columns (older catalogs lack them;
    # reindex() backfills NULL values from path/source/ext).
    columns = {row[1] for row in conn.execute("PRAGMA table_info(files)")}
    for column in ("category", "root"):
        if column not in columns:
            conn.execute(f"ALTER TABLE files ADD COLUMN {column} TEXT")
    conn.commit()
    return conn


def curated_roots() -> list[tuple[str, str]]:
    """The scopes to index, in cap-priority order: personal local folders, iCloud Drive,
    Google Drive, Dropbox, then code-checkout areas (all only if they exist)."""
    home = os.path.expanduser("~")
    roots: list[tuple[str, str]] = []
    for rel in _CURATED_LOCAL_RELS:
        candidate = os.path.join(home, rel)
        if os.path.isdir(candidate):
            roots.append((candidate, "local"))
    icloud = os.path.join(home, "Library", "Mobile Documents", "com~apple~CloudDocs")
    if os.path.isdir(icloud):
        roots.append((icloud, "icloud"))
    # Whole Google Drive ("My Drive", one per signed-in account; the ~/Google Drive
    # symlink resolves here) + Dropbox mount(s), so cloud-drive files are searchable via
    # the catalog (by name/metadata for placeholders, by content for locally-cached
    # files) WITHOUT the Drive API/OAuth. Placeholder-safe: the walk reads only
    # directory entries + stat metadata, never a dataless file's bytes.
    for gdrive in sorted(
        glob.glob(os.path.join(home, "Library", "CloudStorage", "GoogleDrive-*", "My Drive"))
    ):
        if os.path.isdir(gdrive):
            roots.append((os.path.realpath(gdrive), "gdrive"))
    for dropbox in sorted(glob.glob(os.path.join(home, "Library", "CloudStorage", "Dropbox*"))):
        if os.path.isdir(dropbox):
            roots.append((os.path.realpath(dropbox), "dropbox"))
    for rel in _CURATED_REPO_RELS:
        candidate = os.path.join(home, rel)
        if os.path.isdir(candidate):
            roots.append((candidate, "local"))
    return roots


def _iter_files(roots: list[tuple[str, str]], max_files: int) -> list[tuple[str, str]]:
    found: list[tuple[str, str]] = []
    for root, source in roots:
        for dirpath, dirs, files in os.walk(root):
            dirs[:] = [d for d in dirs if not d.startswith(".") and d != "node_modules"]
            for name in files:
                if name.startswith("."):
                    continue
                if os.path.splitext(name)[1].lower() not in _CATALOG_EXT:
                    continue
                path = os.path.join(dirpath, name)
                if _is_noise(path, source):
                    continue
                found.append((path, source))
                if len(found) >= max_files:
                    return found
    return found


def _extract_doc(
    path: str, name: str, rel: str, ext: str, size: int, dataless: bool, runner: Runner
) -> str:
    """A short embeddable representation: name + folder + a head of text where safely
    extractable (local text files, or office docs via macOS ``textutil``), else Spotlight
    title/keywords. Never reads a dataless cloud file."""
    parts = [name, rel.replace("/", " › ")]
    if not dataless:
        if ext in _TEXT_EXT and 0 < size < _MAX_TEXT_BYTES:
            try:
                with open(path, encoding="utf-8", errors="ignore") as handle:
                    parts.append(handle.read(_DOC_HEAD_CHARS * 3)[:_DOC_HEAD_CHARS])
            except OSError:
                pass
        elif ext in _TEXTUTIL_EXT and 0 < size < _MAX_OFFICE_BYTES:
            text = runner(["textutil", "-convert", "txt", "-stdout", path])
            if text:
                parts.append(" ".join(text.split())[:_DOC_HEAD_CHARS])
    if len(parts) == 2:  # nothing extracted -> lean on Spotlight metadata
        meta = runner(["mdls", "-name", "kMDItemTitle", "-name", "kMDItemKeywords", path])
        for line in meta.splitlines():
            if "=" in line:
                value = line.split("=", 1)[1].strip().strip('"')
                if value and value != "(null)":
                    parts.append(value)
    return "\n".join(part for part in parts if part)[:4000]


def reindex(
    max_files: int = _DEFAULT_MAX_FILES,
    model: str = embeddings.DEFAULT_MODEL,
    runner: Runner | None = None,
) -> dict[str, Any]:
    """Incrementally (re)build the catalog: index new/changed documents, embed them, and
    prune deleted ones. Safe to call repeatedly (a scheduled routine or on demand)."""
    runner = runner or _run
    home = os.path.expanduser("~")
    conn = _connect()
    try:
        existing = {
            row["path"]: row["mtime"] for row in conn.execute("SELECT path, mtime FROM files")
        }
        backfilled = _backfill_meta(conn)
        files = _iter_files(curated_roots(), max_files)
        seen: set[str] = set()
        pending: list[tuple[str, str, str, str, int, float, str]] = []
        for path, source in files:
            seen.add(path)
            try:
                stat = os.stat(path)
            except OSError:
                continue
            previous = existing.get(path)
            if previous is not None and abs(previous - stat.st_mtime) < 1.0:
                continue  # unchanged since last index
            name = os.path.basename(path)
            ext = os.path.splitext(name)[1].lower()
            rel = os.path.dirname(path).replace(home, "~")
            if ext in _METADATA_ONLY_EXT:
                # Media/archives: metadata-only — name + folder as the doc, never opened.
                doc = "\n".join((name, rel.replace("/", " › ")))
            else:
                dataless = bool(getattr(stat, "st_flags", 0) & _UF_DATALESS)
                doc = _extract_doc(path, name, rel, ext, stat.st_size, dataless, runner)
            pending.append((path, source, name, ext, stat.st_size, stat.st_mtime, doc))

        embedded_ok = embeddings.is_available(model)
        now = time.time()
        indexed = 0
        for start in range(0, len(pending), _EMBED_BATCH):
            batch = pending[start : start + _EMBED_BATCH]
            # Only document-like rows are embedded; metadata-only rows store no vector.
            embed_rows = [row for row in batch if row[3] in _INDEXABLE_EXT]
            vectors = (
                embeddings.embed([row[6] for row in embed_rows], model=model)
                if embedded_ok and embed_rows
                else None
            )
            if embedded_ok and embed_rows and vectors is None:
                embedded_ok = False  # backend died mid-run: keep going, store docs sans embedding
            vector_by_path: dict[str, Any] = (
                {row[0]: vectors[i] for i, row in enumerate(embed_rows)} if vectors else {}
            )
            for path, source, name, ext, size, mtime, doc in batch:
                blob: bytes | None = None
                dim: int | None = None
                used_model: str | None = None
                vector = vector_by_path.get(path)
                if vector is not None:
                    arr = np.asarray(vector, dtype=np.float32)
                    blob, dim, used_model = arr.tobytes(), int(arr.shape[0]), model
                conn.execute(
                    "INSERT OR REPLACE INTO files "
                    "(path, source, name, ext, size, mtime, indexed_at, doc, embedding, dim, "
                    "model, category, root) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        path,
                        source,
                        name,
                        ext,
                        size,
                        mtime,
                        now,
                        doc,
                        blob,
                        dim,
                        used_model,
                        category_for(ext),
                        root_label(path, source),
                    ),
                )
                indexed += 1
            conn.commit()

        deleted = 0
        for path in set(existing) - seen:
            conn.execute("DELETE FROM files WHERE path = ?", (path,))
            deleted += 1
        conn.commit()
        total = conn.execute("SELECT COUNT(*) FROM files").fetchone()[0]
        with_embeddings = conn.execute(
            "SELECT COUNT(*) FROM files WHERE embedding IS NOT NULL"
        ).fetchone()[0]
        return {
            "scanned": len(files),
            "indexed": indexed,
            "deleted": deleted,
            "backfilled": backfilled,
            "total": total,
            "embedded": with_embeddings,
            "backend_up": embedded_ok,
        }
    finally:
        conn.close()


def _backfill_meta(conn: sqlite3.Connection) -> int:
    """Fill category/root for rows written before those columns existed (one-time cost)."""
    rows = conn.execute(
        "SELECT path, source, ext FROM files WHERE category IS NULL OR root IS NULL"
    ).fetchall()
    for row in rows:
        conn.execute(
            "UPDATE files SET category = ?, root = ? WHERE path = ?",
            (
                category_for(row["ext"] or ""),
                root_label(row["path"], row["source"] or ""),
                row["path"],
            ),
        )
    if rows:
        conn.commit()
    return len(rows)


def filter_files(
    q: str = "",
    root: str | None = None,
    category: str | None = None,
    sort: str = "recency",
    limit: int = 50,
) -> list[dict[str, Any]]:
    """Metadata-filtered catalog listing — pure SQLite, no Spotlight, no embeddings.

    ``q`` terms must ALL appear in the path (name included, case-insensitive);
    ``root``/``category`` are exact matches on the derived columns; ``sort`` is
    ``recency`` (mtime desc, default) or ``name``. Backs the API's root=/category=/
    sort= parameters on GET /api/filesearch.
    """
    where: list[str] = []
    params: list[Any] = []
    for term in q.lower().split():
        where.append("instr(lower(path), ?) > 0")
        params.append(term)
    if root:
        where.append("root = ?")
        params.append(root)
    if category:
        where.append("category = ?")
        params.append(category)
    order = "name COLLATE NOCASE ASC, path ASC" if sort == "name" else "mtime DESC"
    sql = "SELECT path, source, name, ext, size, mtime, category, root FROM files"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += f" ORDER BY {order} LIMIT ?"
    params.append(max(1, min(int(limit), 500)))
    conn = _connect()
    try:
        rows = conn.execute(sql, params).fetchall()
    finally:
        conn.close()
    results: list[dict[str, Any]] = []
    for row in rows:
        mtime = row["mtime"] or 0.0
        results.append(
            {
                "path": row["path"],
                "name": row["name"],
                "source": row["source"],
                "kind": (row["ext"] or "").lstrip(".") or "file",
                "size": row["size"],
                "mtime": mtime,
                "modified": time.strftime("%Y-%m-%d", time.localtime(mtime)) if mtime else None,
                "category": row["category"],
                "root": row["root"],
                "snippet": "",
                "score": 0.0,
            }
        )
    return results


def lookup(path: str) -> dict[str, Any] | None:
    """Exact-path catalog row, or None. The reveal endpoint's index-membership floor:
    only a path that IS a row in the live index (byte-for-byte) may be revealed."""
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT path, source, name, ext, size, mtime, category, root FROM files WHERE path = ?",
            (path,),
        ).fetchone()
    finally:
        conn.close()
    return dict(row) if row else None


def stats() -> dict[str, Any]:
    conn = _connect()
    try:
        total = conn.execute("SELECT COUNT(*) FROM files").fetchone()[0]
        with_embeddings = conn.execute(
            "SELECT COUNT(*) FROM files WHERE embedding IS NOT NULL"
        ).fetchone()[0]
        by_root = dict(
            conn.execute(
                "SELECT COALESCE(root, 'unknown'), COUNT(*) FROM files GROUP BY 1"
            ).fetchall()
        )
        by_category = dict(
            conn.execute(
                "SELECT COALESCE(category, 'unknown'), COUNT(*) FROM files GROUP BY 1"
            ).fetchall()
        )
        return {
            "total": total,
            "embedded": with_embeddings,
            "path": catalog_path(),
            "by_root": by_root,
            "by_category": by_category,
        }
    finally:
        conn.close()
