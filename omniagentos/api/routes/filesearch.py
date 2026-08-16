"""HTTP surface for file search across local + iCloud + Google Drive + Dropbox.

- ``GET  /api/filesearch?q=&mode=keyword|semantic|hybrid&scope=&limit=`` — ranked hits
  (live federated search; unchanged legacy behavior).
- ``GET  /api/filesearch?q=&root=&category=&sort=recency|name&limit=`` — metadata-
  filtered catalog listing (any of root/category/sort switches to the catalog path).
- ``GET  /api/filesearch/semantic?q=&root=&category=&limit=`` — deep chunk-level
  semantic search (pgvector in the knowledge Postgres; LOCAL embeddings only).
- ``POST /api/filesearch/reveal`` — reveal an INDEXED file in Finder (macOS only).
- ``POST /api/filesearch/reindex`` — rebuild the semantic catalog in the background.
- ``GET  /api/filesearch/stats`` — catalog size / embedding coverage.

Every search response carries a uniform ``rows`` key
(``{path, root, category, mtime(epoch s), size?, score?, excerpt?}``); the legacy
keyword/hybrid path ALSO keeps its original ``hits`` shape so existing callers are
unchanged when the new params are absent.

Token-gated (reveals file paths/content). The search is placeholder-safe — see
:mod:`omniagentos.filesearch`.
"""

from __future__ import annotations

import os
import platform
import subprocess
from dataclasses import asdict
from typing import Annotated, Any

from fastapi import APIRouter, BackgroundTasks, Depends, Header, Query
from pydantic import BaseModel

from omniagentos.api.services import fail
from omniagentos.filesearch import catalog, catalog_stats, reindex, search
from omniagentos.filesearch.semantic import SemanticUnavailable, semantic_query
from omniagentos.filesearch.service import SearchHit
from omniagentos.sessions.token import verify_token

router = APIRouter(prefix="/api", tags=["filesearch"])

_ROOT_PATTERN = "^(desktop|icloud|gdrive|repo|other-mount)$"
_CATEGORY_PATTERN = (
    "^(documents|spreadsheets|presentations|images|video|audio|code|archives|other)$"
)
_SORT_PATTERN = "^(recency|name)$"

# Same kill switch as the board-files reveal surface: any truthy value disables
# local reveal outright (403) before any filesystem work.
_DISABLE_REVEAL_ENV = "OMNIAGENTOS_DISABLE_LOCAL_REVEAL"
_REVEAL_TIMEOUT_S = 5


def _require_token(x_session_token: str | None = Header(None, alias="X-Session-Token")) -> None:
    # Self-contained auth (verify_token is the stable primitive) so this route does not
    # import from the sessions route module, which is under active concurrent edit.
    if not verify_token(x_session_token):
        fail(401, "unauthorized", "invalid session token")


def _hit_row(hit: SearchHit) -> dict[str, Any]:
    """A live-search hit in the uniform ``rows`` shape (root/category derived)."""
    ext = "." + hit.kind if hit.kind not in {"folder", "file"} else ""
    return {
        "path": hit.path,
        "root": catalog.root_label(hit.path, hit.source),
        "category": catalog.category_for(ext),
        "mtime": hit._mtime,
        "size": hit.size,
        "score": hit.score,
        "excerpt": hit.snippet,
    }


@router.get("/filesearch")
def file_search(
    _: Annotated[None, Depends(_require_token)],
    q: str = Query("", description="content + filename query"),
    mode: str = Query("hybrid", pattern="^(keyword|semantic|hybrid)$"),
    scope: str = Query(
        "", description="comma of local,icloud,gdrive,dropbox (default local,icloud)"
    ),
    limit: int = Query(20, ge=1, le=100),
    root: str | None = Query(None, pattern=_ROOT_PATTERN),
    category: str | None = Query(None, pattern=_CATEGORY_PATTERN),
    sort: str | None = Query(None, pattern=_SORT_PATTERN),
) -> dict[str, Any]:
    """Search files across every mounted drive — ranked live search, or (when any of
    ``root``/``category``/``sort`` is present) a metadata-filtered catalog listing."""
    del _
    if root or category or sort:
        rows = catalog.filter_files(
            q=q, root=root, category=category, sort=sort or "recency", limit=limit
        )
        return {"query": q, "mode": "catalog", "count": len(rows), "rows": rows}
    if not q.strip():
        fail(422, "validation", "q is required when no root/category/sort filter is given")
    scopes = [s.strip() for s in scope.split(",") if s.strip()] or None
    hits = search(q, mode=mode, scopes=scopes, limit=limit)
    return {
        "query": q,
        "mode": mode,
        "count": len(hits),
        "hits": [
            {key: value for key, value in asdict(hit).items() if not key.startswith("_")}
            for hit in hits
        ],
        "rows": [_hit_row(hit) for hit in hits],
    }


@router.get("/filesearch/semantic")
def semantic_file_search(
    _: Annotated[None, Depends(_require_token)],
    q: str = Query(..., min_length=1, description="meaning-based query"),
    root: str | None = Query(None, pattern=_ROOT_PATTERN),
    category: str | None = Query(None, pattern=_CATEGORY_PATTERN),
    limit: int = Query(20, ge=1, le=100),
) -> dict[str, Any]:
    """Deep chunk-level semantic search (pgvector top-k with SQL root/category
    filters). Embeds the query LOCALLY; 503 when Postgres/Ollama are down."""
    del _
    try:
        rows = semantic_query(q, root=root, category=category, limit=limit)
    except SemanticUnavailable as exc:
        fail(503, "semantic_unavailable", str(exc))
    return {"query": q, "count": len(rows), "rows": rows}


class RevealRequest(BaseModel):
    """Body for ``POST /filesearch/reveal``: an ABSOLUTE path that must be a row in
    the live filesearch index, its (optional) expected root label, and an app key
    (only ``finder`` is supported on this surface)."""

    path: str
    root: str | None = None
    app: str | None = None


@router.post("/filesearch/reveal")
def reveal_file(_: Annotated[None, Depends(_require_token)], body: RevealRequest) -> dict[str, Any]:
    """Reveal an indexed file in Finder (localhost convenience; macOS only).

    The filesearch analogue of the board-files reveal, with the INDEX-MEMBERSHIP
    FLOOR in place of the workspace floor: the client-supplied path is re-resolved
    against the live catalog and anything that is not byte-for-byte a row of the
    index is refused (403) — a client-arbitrary path is never trusted and never
    reaches the launcher. Checks in order: unknown app 422 → kill switch 403 →
    index floor 403 → host gate 501 → gone-from-disk 404. The argv is FIXED
    (``/usr/bin/open -R <indexed path>``), never ``shell=True``, hard-bounded.
    """
    del _
    app_key = (body.app or "finder").strip().lower()
    if app_key != "finder":
        fail(422, "validation", "unknown reveal app", {"app": app_key})

    if os.environ.get(_DISABLE_REVEAL_ENV):
        fail(403, "forbidden", "local reveal is disabled on this host")

    row = catalog.lookup(body.path)
    if row is None:
        fail(403, "forbidden", "path is not in the filesearch index", {"path": body.path})
    if body.root and row.get("root") and body.root != row["root"]:
        fail(403, "forbidden", "root does not match the indexed row", {"root": body.root})

    if platform.system() != "Darwin":
        fail(501, "not_implemented", "local reveal is only available on macOS")

    target = row["path"]  # the INDEXED path string, never the raw client value
    if not os.path.exists(target):
        fail(404, "not_found", "file no longer exists on disk", {"path": target})

    argv = ["/usr/bin/open", "-R", target]
    try:
        result = subprocess.run(  # noqa: S603 - fixed argv, never shell=True
            argv, capture_output=True, timeout=_REVEAL_TIMEOUT_S, check=False
        )
    except subprocess.TimeoutExpired:
        fail(504, "reveal_timeout", "the reveal command timed out", {"app": app_key})
    except OSError:
        fail(500, "reveal_failed", "could not launch the reveal command", {"app": app_key})
    if result.returncode != 0:
        fail(502, "reveal_failed", "the reveal command failed", {"app": app_key})
    return {"revealed": True, "app": app_key, "path": target, "root": row.get("root")}


@router.post("/filesearch/reindex")
def reindex_catalog(
    _: Annotated[None, Depends(_require_token)], background: BackgroundTasks
) -> dict[str, Any]:
    """Kick off an incremental catalog rebuild in the background (returns immediately)."""
    del _
    background.add_task(reindex)
    return {"status": "reindexing", "detail": "catalog rebuild started in the background"}


@router.get("/filesearch/stats")
def catalog_statistics(_: Annotated[None, Depends(_require_token)]) -> dict[str, Any]:
    """Catalog size + how many entries have embeddings (with root/category breakdowns)."""
    del _
    return catalog_stats()
