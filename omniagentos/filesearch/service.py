"""Federated file search across the local filesystem, iCloud Drive, Google Drive, and
Dropbox — one call an agent makes to FIND files anywhere on this machine.

Design (see [[project-omniagentos]] research): the search index already exists — macOS
Spotlight indexes local + iCloud content in milliseconds — so this federates ``mdfind``
per scope rather than rebuilding an index, merges + reranks across sources, and is
PLACEHOLDER-SAFE: it searches the index and only ever reads the CONTENT of the few
top-ranked hits (never a cloud-only/dataless file), so a search never triggers a
gigabyte of Google Drive / iCloud downloads.

Cloud drives whose provider Spotlight does not index fall back to a bounded filename
walk (placeholders still expose their names). No new auth, no reorganization of your
files — it meets the mess where it is.
"""

from __future__ import annotations

import os
import subprocess
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

# A command runner: argv -> stdout (injectable so the Spotlight backend is testable).
Runner = Callable[[list[str]], str]

# macOS: a cloud-only (evicted) file is flagged UF_DATALESS; reading it triggers a
# download. We check this before ever opening a file for a snippet.
_UF_DATALESS = 0x40000000

_TEXT_EXT: frozenset[str] = frozenset(
    {
        ".txt",
        ".md",
        ".markdown",
        ".py",
        ".ts",
        ".tsx",
        ".js",
        ".jsx",
        ".json",
        ".yaml",
        ".yml",
        ".csv",
        ".tsv",
        ".html",
        ".css",
        ".scss",
        ".sh",
        ".zsh",
        ".sql",
        ".rs",
        ".go",
        ".java",
        ".rb",
        ".toml",
        ".ini",
        ".cfg",
        ".log",
        ".tex",
        ".rst",
        ".env",
        ".xml",
        ".swift",
        ".c",
        ".h",
        ".cpp",
        ".php",
    }
)
_SNIPPET_MAX_BYTES = 512_000
_SEARCH_STOPWORDS: frozenset[str] = frozenset(
    {"the", "a", "an", "and", "or", "of", "to", "in", "on", "for", "my", "file", "files"}
)
# Default to the Spotlight-indexed scopes (local + iCloud) — both sub-second. Google
# Drive / Dropbox are opt-in (``--scope gdrive``): their provider isn't Spotlight-indexed,
# so they need a bounded directory walk that's fine on request but too slow for a default.
# (A fast Google Drive search would use the Drive API — a phase-2, OAuth-gated add.)
_DEFAULT_SCOPES: tuple[str, ...] = ("local", "icloud")
_ALL_SCOPES: tuple[str, ...] = ("local", "icloud", "gdrive", "dropbox")


@dataclass
class SearchHit:
    path: str
    name: str
    source: str  # local | icloud | gdrive | dropbox
    kind: str
    size: int | None
    modified: str | None
    snippet: str
    score: float
    _mtime: float = 0.0


def _run(argv: list[str]) -> str:
    """Run a command, returning stdout on success else ''. Never raises.

    5s cap: multiple scopes each shell out to mdfind, so a per-call timeout keeps a
    single slow/starved provider from stalling the whole federated search."""
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=5.0)
    except (OSError, subprocess.SubprocessError):
        return ""
    return proc.stdout if proc.returncode == 0 else ""


def _home() -> str:
    return os.path.expanduser("~")


def resolve_roots(scopes: list[str]) -> list[tuple[str, str]]:
    """``(root_dir, source_label)`` for each requested, present scope."""
    home = _home()
    roots: list[tuple[str, str]] = []
    if "local" in scopes:
        roots.append((home, "local"))
    if "icloud" in scopes:
        icloud = os.path.join(home, "Library", "Mobile Documents", "com~apple~CloudDocs")
        if os.path.isdir(icloud):
            roots.append((icloud, "icloud"))
    if "gdrive" in scopes:
        try:
            from omniagentos.drive import google_drive_workspace

            workspace = google_drive_workspace()
        except Exception:  # noqa: BLE001 -- Drive resolution is best-effort.
            workspace = None
        if workspace and os.path.isdir(workspace):
            roots.append((workspace, "gdrive"))
    if "dropbox" in scopes:
        dropbox = os.path.join(home, "Library", "CloudStorage", "Dropbox")
        if os.path.isdir(dropbox):
            roots.append((dropbox, "dropbox"))
    return roots


def _query_terms(query: str) -> list[str]:
    return [
        token
        for token in query.lower().split()
        if len(token) > 1 and token not in _SEARCH_STOPWORDS
    ]


def _is_noise(path: str, source: str) -> bool:
    """Drop system/build/cache clutter. The local scope is the home dir, so ~/Library,
    Trash, and app caches are excluded; cloud scopes (themselves under ~/Library/…) keep
    their /Library/ path but still drop build/cache dirs."""
    low = path.lower()
    always = (
        "/.trash",
        "/node_modules/",
        "/__pycache__/",
        "/.git/",
        "/.venv/",
        "/.cache/",
        "/caches/",
        "/.npm/",
        "/.cargo/",
        "/.pytest_cache/",
    )
    if any(seg in low for seg in always):
        return True
    if source == "local" and ("/library/" in low or "/applications/" in low):
        return True
    return False


def _spotlight_paths(root: str, query: str, runner: Runner, limit: int) -> list[str]:
    if not query.strip():
        return []
    out = runner(["mdfind", "-onlyin", root, query])
    return [line for line in out.splitlines() if line.strip()][:limit]


def _filename_walk(
    root: str, terms: list[str], limit: int, deadline: float, max_entries: int = 8_000
) -> list[str]:
    """Placeholder-safe fallback for cloud roots Spotlight doesn't index: bounded walk
    matching any query term against filenames (names exist even for dataless files).

    Triple-bounded — entry count, hit count, AND a wall-clock ``deadline`` — because a
    Google Drive / Dropbox mount can be enormous and slow to traverse (placeholders)."""
    hits: list[str] = []
    scanned = 0
    for dirpath, dirs, files in os.walk(root):
        if time.monotonic() > deadline:
            break
        dirs[:] = [d for d in dirs if not d.startswith(".") and d != "node_modules"]
        for name in files:
            scanned += 1
            if scanned > max_entries or len(hits) >= limit:
                return hits
            if any(term in name.lower() for term in terms):
                hits.append(os.path.join(dirpath, name))
    return hits


def _meta_snippet(path: str, runner: Runner) -> str:
    out = runner(["mdls", "-name", "kMDItemKind", "-name", "kMDItemTitle", path])
    fields: list[str] = []
    for line in out.splitlines():
        if "=" in line:
            value = line.split("=", 1)[1].strip().strip('"')
            if value and value != "(null)":
                fields.append(value)
    return " · ".join(dict.fromkeys(fields))[:200]


def _snippet(path: str, terms: list[str], runner: Runner) -> str:
    """A one-line preview — matching content for a small LOCAL text file, else metadata.
    NEVER reads a dataless (cloud-only) file, so it can't trigger a download."""
    try:
        stat = os.stat(path)
    except OSError:
        return ""
    if getattr(stat, "st_flags", 0) & _UF_DATALESS:
        return _meta_snippet(path, runner)
    ext = os.path.splitext(path)[1].lower()
    if ext in _TEXT_EXT and 0 < stat.st_size < _SNIPPET_MAX_BYTES:
        try:
            with open(path, encoding="utf-8", errors="ignore") as handle:
                lines = handle.read().splitlines()
        except OSError:
            return _meta_snippet(path, runner)
        for line in lines:
            low = line.lower()
            if any(term in low for term in terms):
                return line.strip()[:200]
        return lines[0].strip()[:200] if lines else ""
    return _meta_snippet(path, runner)


def _build_hit(path: str, source: str) -> SearchHit | None:
    try:
        stat = os.stat(path)
        size: int | None = stat.st_size
        mtime = stat.st_mtime
        modified = time.strftime("%Y-%m-%d", time.localtime(mtime))
    except OSError:
        size, mtime, modified = None, 0.0, None
    name = os.path.basename(path)
    ext = os.path.splitext(name)[1].lstrip(".").lower()
    kind = "folder" if os.path.isdir(path) else (ext or "file")
    return SearchHit(
        path=path,
        name=name,
        source=source,
        kind=kind,
        size=size,
        modified=modified,
        snippet="",
        score=0.0,
        _mtime=mtime,
    )


def _score(hit: SearchHit, terms: list[str], now: float) -> float:
    score = 0.0
    name = hit.name.lower()
    for term in terms:
        if term in name:
            score += 3.0
    if terms and all(term in name for term in terms):
        score += 3.0
    if hit._mtime:
        age_days = (now - hit._mtime) / 86_400
        score += max(0.0, 2.5 - age_days / 120.0)  # recency: strong <120d, fades ~1yr
    if hit.kind in {"pdf", "md", "docx", "doc", "txt", "key", "pages", "numbers"}:
        score += 0.5
    return score



def _gdrive_api_hits(
    query: str, terms: list[str], now: float, limit: int, seen: set[str]
) -> list[SearchHit]:
    """Google Drive results via the cloud-native Drive API (full-text, no download). ``[]``
    when the API isn't authed — the caller then falls back to the bounded mount walk."""
    try:
        from omniagentos.filesearch import gdrive_api

        if not gdrive_api.is_available():
            return []
        raw = gdrive_api.search_drive(query, limit=limit)
    except Exception:  # noqa: BLE001 -- Drive is best-effort; never break the search.
        return []
    results: list[SearchHit] = []
    for item in raw:
        path = str(item.get("path") or "")
        if not path or path in seen:
            continue
        seen.add(path)
        size_raw = item.get("size")
        modified_raw = item.get("modified")
        hit = SearchHit(
            path=path,
            name=str(item.get("name", "")),
            source="gdrive",
            kind=str(item.get("kind", "file")),
            size=size_raw if isinstance(size_raw, int) else None,
            modified=str(modified_raw) if modified_raw is not None else None,
            snippet=str(item.get("snippet", "")),
            score=0.0,
            _mtime=0.0,
        )
        hit.score = _score(hit, terms, now)
        results.append(hit)
    return results


def _scope_paths(
    root: str,
    source: str,
    query: str,
    terms: list[str],
    runner: Runner,
    per_scope_limit: int,
    deadline: float,
) -> tuple[str, list[str]]:
    """One scope's raw candidate paths (Spotlight, or a bounded walk fallback for a
    cloud mount Spotlight doesn't index). Run per-scope in a thread by search_files."""
    paths = _spotlight_paths(root, query, runner, per_scope_limit)
    if not paths and source in {"gdrive", "dropbox", "icloud"} and terms:
        # Always fall back to the bounded mount walk for cloud scopes. The Drive
        # API (when authed) is *additive* in search_files — it must not suppress
        # this walk. ``is_available()`` is only a local token-file check; treating
        # it as "API will answer" skipped the walk and presented a dead token /
        # swallowed network error as a clean empty result ("no files found").
        paths = _filename_walk(root, terms, per_scope_limit, min(deadline, time.monotonic() + 3.0))
    return source, paths


def search_files(
    query: str,
    scopes: list[str] | None = None,
    limit: int = 20,
    per_scope_limit: int = 300,
    time_budget: float = 12.0,
    runner: Runner | None = None,
) -> list[SearchHit]:
    """Federated, reranked, placeholder-safe file search.

    ``scopes`` ⊆ {local, icloud, gdrive, dropbox} (default: all present). Returns up to
    ``limit`` :class:`SearchHit` ranked across sources; content snippets are computed
    only for the returned hits (lazy + never for cloud-only files)."""
    runner = runner or _run
    scopes = scopes or list(_DEFAULT_SCOPES)
    terms = _query_terms(query)
    now = time.time()
    deadline = time.monotonic() + time_budget

    # Search every scope CONCURRENTLY so total wall-clock is the slowest single scope,
    # not the sum — critical when the machine is CPU-saturated (each mdfind can take its
    # full timeout). Collect within the deadline; shut down non-blocking so a stuck
    # provider walk (bounded by its own deadline) never wedges the request.
    roots = resolve_roots(scopes)
    gathered: list[tuple[str, list[str]]] = []
    if roots:
        executor = ThreadPoolExecutor(max_workers=len(roots))
        try:
            futures = [
                executor.submit(
                    _scope_paths, root, source, query, terms, runner, per_scope_limit, deadline
                )
                for root, source in roots
            ]
            for future in futures:
                try:
                    gathered.append(future.result(timeout=max(0.1, deadline - time.monotonic())))
                except Exception:  # noqa: BLE001 -- a slow/failed scope is skipped, not fatal.
                    continue
        finally:
            executor.shutdown(wait=False, cancel_futures=True)

    seen: set[str] = set()
    hits: list[SearchHit] = []
    for source, paths in gathered:
        for path in paths:
            real = os.path.realpath(path)
            if real in seen or _is_noise(path, source):
                continue
            seen.add(real)
            hit = _build_hit(path, source)
            if hit is not None:
                hit.score = _score(hit, terms, now)
                hits.append(hit)

    # Google Drive via the cloud API (fast, full-text) when authed; the walk above is
    # skipped for gdrive in that case. Dormant until `gcloud auth application-default
    # login` is run — see gdrive_api.auth_instructions().
    if "gdrive" in scopes:
        hits.extend(_gdrive_api_hits(query, terms, now, per_scope_limit, seen))

    hits.sort(key=lambda h: h.score, reverse=True)
    top = hits[:limit]
    for hit in top:  # snippets only for what we return (lazy, download-safe)
        hit.snippet = _snippet(hit.path, terms, runner)
    return top


def _human_size(size: int | None) -> str:
    if not size:
        return "?"
    value = float(size)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{value:.0f}{unit}" if unit == "B" else f"{value:.1f}{unit}"
        value /= 1024
    return f"{value:.1f}GB"


def render_hits(hits: list[SearchHit]) -> str:
    """A clean, agent-readable listing of hits."""
    if not hits:
        return "No files found."
    home = _home()
    lines: list[str] = []
    for i, hit in enumerate(hits, 1):
        location = os.path.dirname(hit.path).replace(home, "~")
        meta = f"{hit.kind}, {_human_size(hit.size)}, {hit.modified or '?'}"
        lines.append(f"{i}. [{hit.source}] {hit.name}  ({meta})  —  {location}")
        lines.append(f"   {hit.path}")
        if hit.snippet:
            lines.append(f"   … {hit.snippet}")
    return "\n".join(lines)


def search_files_text(query: str, scopes: list[str] | None = None, limit: int = 20) -> str:
    """One-call convenience for an agent/CLI: search and render to text."""
    return render_hits(search_files(query, scopes=scopes, limit=limit))
