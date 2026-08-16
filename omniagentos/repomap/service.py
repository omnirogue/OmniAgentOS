"""Public repo-map API: turn a repository into a compact, task-aware, ranked map.

``build_repo_map(repo, focus_files=..., focus_terms=..., max_tokens=...)`` is the
one-shot entry point, and it is cached: parsing every file is ~90% of the work
(~1.3s vs ~0.1s to rank, on this repo), so ``TagCache`` sits in front of the
extractors and a repeated call over unchanged files does zero parsing.

The cache is keyed by CONTENT (sha256 of the file's bytes), not by path+mtime.
That is the whole design: a branch switch, a fresh worktree, a rebase, a revert
or a bare ``touch`` all change mtimes without changing a byte, so an mtime cache
misses on every one of them while a content-addressed cache hits. It also means
two identical files parse once, and a moved file keeps its parse. There are two
tiers — a process-local dict and the ``repomap_tag_cache`` SQLite table
(migration 057) that survives restarts and is shared between processes. Both are
pure optimisation: either can vanish mid-flight and the only effect is a slower
build.
"""

from __future__ import annotations

import os
import sqlite3
from collections import OrderedDict, defaultdict
from threading import RLock

from omniagentos.repomap.ranking import (
    build_graph,
    build_index,
    common_names,
    pagerank,
    personalization_vector,
    rank_definitions,
)
from omniagentos.repomap.tags import (
    Definition,
    FileTags,
    TagPayload,
    content_hash,
    extract_bytes,
    iter_source_files,
    lang_for,
    read_source_bytes,
)

_CHARS_PER_TOKEN = 4  # rough, model-agnostic budget heuristic
_ELISION = "    ⋮"
_MAX_PER_FILE = 8  # so one dominant file can't monopolize the budget -> breadth

# Distinct blobs kept parsed in-process. ~2.4KB of payload per file here, so 25k
# entries is tens of MB worst case while covering this repo (~1.2k files) many
# edits over. The SQLite tier is the durable one; this is just the hot path.
_MEM_CACHE_MAX = 25_000
# SQLite's default parameter ceiling is 999 on older builds; stay well under it.
_SQL_CHUNK = 400
_CACHE_TABLE = "repomap_tag_cache"
_DISABLED = frozenset({"0", "false", "off", "no"})


def render_map(
    ranked: list[Definition], max_tokens: int = 1024, max_per_file: int = _MAX_PER_FILE
) -> str:
    """Render the top-ranked definitions within a token budget as a compact,
    file-grouped signature map (bodies elided). Greedy in rank order but capped at
    ``max_per_file`` symbols per file, so the map spans many files (breadth) instead of
    exhausting one; files appear in rank order, symbols within a file in line order."""
    max_chars = max(1, max_tokens) * _CHARS_PER_TOKEN
    included: list[Definition] = []
    seen_files: set[str] = set()
    file_order: list[str] = []
    per_file: dict[str, int] = defaultdict(int)
    used = 0
    for definition in ranked:
        if per_file[definition.rel_path] >= max_per_file:
            continue
        cost = len(definition.signature) + 5  # "    <sig>\n"
        if definition.rel_path not in seen_files:
            cost += len(definition.rel_path) + 2  # "<path>:\n"
        if used + cost > max_chars and included:
            break
        included.append(definition)
        used += cost
        per_file[definition.rel_path] += 1
        if definition.rel_path not in seen_files:
            seen_files.add(definition.rel_path)
            file_order.append(definition.rel_path)

    by_file: dict[str, list[Definition]] = defaultdict(list)
    for definition in included:
        by_file[definition.rel_path].append(definition)

    lines: list[str] = []
    for path in file_order:
        lines.append(f"{path}:")
        for definition in sorted(by_file[path], key=lambda d: d.line):
            lines.append(f"    {definition.signature}")
        lines.append(_ELISION)
    return "\n".join(lines).rstrip()


def _rank(
    all_tags: list[FileTags],
    focus_files: list[str] | None,
    focus_terms: list[str] | None,
) -> list[Definition]:
    index = build_index(all_tags)
    common = common_names(all_tags)
    nodes, out_edges = build_graph(all_tags, index, common)
    teleport = personalization_vector(all_tags, focus_files, focus_terms)
    file_rank = pagerank(nodes, out_edges, personalization=teleport)
    if teleport:
        # A task-focused query: the teleport (15% of PageRank) alone can't lift a new
        # or low-centrality file above a hub like contracts.py. Add a decisive post-hoc
        # boost proportional to the top rank, so the files the task NAMES lead the map
        # while their relative order (and every other file's rank) is preserved. Generic
        # (no-focus) maps skip this entirely.
        top = max(file_rank.values(), default=0.0) or 1.0
        for path, weight in teleport.items():
            if path in file_rank:
                # Decisive: seat named files above the busiest hub (whose symbols still
                # get an inbound-reference multiplier downstream), ordered by focus weight.
                file_rank[path] = top * (8.0 + weight)
    return rank_definitions(all_tags, index, file_rank, common)


class TagCache:
    """Content-addressed cache in front of the (expensive) tag extractors.

    Two tiers, both keyed by :func:`omniagentos.repomap.tags.content_hash` — the file's
    BYTES, never its mtime:

    * an in-process LRU dict, which makes a repeated build in one process nearly free;
    * the ``repomap_tag_cache`` SQLite table (migration 057), which carries hits across
      restarts and between processes sharing a database.

    Everything about the persistent tier is best-effort. A missing database, an
    un-migrated schema, a read-only file, a lock, a corrupt row — each degrades to
    "parse it again", never to an exception, because a cache must not be able to break
    the thing it accelerates. Set ``OMNIAGENTOS_REPOMAP_CACHE=0`` to switch the SQLite
    tier off entirely.

    ``parses`` / ``mem_hits`` / ``db_hits`` are live counters, useful for asserting in
    tests (and in a benchmark) that an unchanged repo really did zero parsing."""

    def __init__(
        self,
        db_path: str | None = None,
        *,
        persist: bool = True,
        mem_max: int = _MEM_CACHE_MAX,
    ) -> None:
        self._mem: OrderedDict[str, TagPayload] = OrderedDict()
        self._mem_max = mem_max
        self._db_path = db_path
        self._persist = (
            persist
            and os.environ.get("OMNIAGENTOS_REPOMAP_CACHE", "").strip().lower() not in _DISABLED
        )
        self._connection: sqlite3.Connection | None = None
        self._connect_attempted = False
        self._lock = RLock()
        self.parses = 0
        self.mem_hits = 0
        self.db_hits = 0

    # --- the one public operation ------------------------------------------

    def tags_for_repo(self, repo_dir: str) -> list[FileTags]:
        """Tags for every source file under ``repo_dir``, parsing only unseen content.

        Walks once, hashes what it reads, resolves as much as possible from the two
        cache tiers, parses only the remainder, and writes the new parses back."""
        repo_dir = os.path.abspath(repo_dir)
        order: list[tuple[str, str | None]] = []  # (rel_path, content hash | None)
        resolved: dict[str, TagPayload] = {}
        pending: dict[str, tuple[str, bytes]] = {}  # hash -> (a rel_path, bytes)

        for path in iter_source_files(repo_dir):
            rel = os.path.relpath(path, repo_dir)
            data = read_source_bytes(path)
            if data is None:  # unreadable or oversized: contributes nothing, as before
                order.append((rel, None))
                continue
            key = content_hash(rel, data)
            order.append((rel, key))
            if key in resolved or key in pending:
                continue  # a duplicate blob in this same walk — hash it, parse it once
            cached = self._from_memory(key)
            if cached is not None:
                resolved[key] = cached
                self.mem_hits += 1
            else:
                pending[key] = (rel, data)

        if pending:
            for key, payload in self._fetch(list(pending)).items():
                self._remember(key, payload)
                resolved[key] = payload
                pending.pop(key, None)
                self.db_hits += 1

        if pending:
            fresh: list[tuple[str, str, int, str]] = []
            for key, (rel, data) in pending.items():
                payload = TagPayload.of(extract_bytes(rel, data))
                self.parses += 1
                self._remember(key, payload)
                resolved[key] = payload
                fresh.append((key, lang_for(rel), len(data), payload.to_json()))
            self._store(fresh)

        # Rebind paths last: one cached payload can serve several paths, and ``bind``
        # hands each of them its own containers.
        return [resolved[key].bind(rel) if key is not None else FileTags(rel) for rel, key in order]

    # --- in-process tier ----------------------------------------------------

    # Locked because the default cache is process-wide and the API serves concurrent
    # requests; an interleaved move_to_end/popitem on an OrderedDict is not safe.

    def _from_memory(self, key: str) -> TagPayload | None:
        with self._lock:
            payload = self._mem.get(key)
            if payload is not None:
                self._mem.move_to_end(key)
            return payload

    def _remember(self, key: str, payload: TagPayload) -> None:
        with self._lock:
            self._mem[key] = payload
            self._mem.move_to_end(key)
            while len(self._mem) > self._mem_max:
                self._mem.popitem(last=False)

    # --- SQLite tier (entirely best-effort) ---------------------------------

    def _open(self) -> sqlite3.Connection | None:
        """Connect once, or decide (once) that there is no usable persistent tier."""
        with self._lock:
            if self._connection is not None or self._connect_attempted:
                return self._connection
            self._connect_attempted = True
            if not self._persist:
                return None
            try:
                path = self._db_path
                if path is None:
                    # Imported lazily so this package stays importable with nothing but
                    # the stdlib; if contracts is unavailable we simply have no DB tier.
                    from omniagentos.contracts import default_db_path

                    path = default_db_path()
                if path != ":memory:" and not os.path.exists(path):
                    return None  # never CREATE a database just to hold a cache
                connection = sqlite3.connect(
                    path, isolation_level=None, timeout=5.0, check_same_thread=False
                )
                connection.execute("PRAGMA busy_timeout=5000")
                exists = connection.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                    (_CACHE_TABLE,),
                ).fetchone()
                if exists is None:  # database not migrated to 057 yet
                    connection.close()
                    return None
                self._connection = connection
            except Exception:  # noqa: BLE001 -- no DB just means "parse it again"
                self._connection = None
            return self._connection

    def _fetch(self, keys: list[str]) -> dict[str, TagPayload]:
        connection = self._open()
        if connection is None:
            return {}
        found: dict[str, TagPayload] = {}
        stale: list[str] = []
        today = _now_iso()[:10]
        try:
            with self._lock:
                for start in range(0, len(keys), _SQL_CHUNK):
                    chunk = keys[start : start + _SQL_CHUNK]
                    placeholders = ",".join("?" * len(chunk))
                    rows = connection.execute(
                        f"SELECT content_hash, tags_json, last_used FROM {_CACHE_TABLE} "  # table name is a module constant, never user input
                        f"WHERE content_hash IN ({placeholders})",
                        chunk,
                    ).fetchall()
                    for key, raw, last_used in rows:
                        payload = TagPayload.from_json(raw)
                        if payload is None:
                            continue  # corrupt row -> treat as a miss and re-parse
                        found[key] = payload
                        if not str(last_used).startswith(today):
                            stale.append(key)
        except Exception:  # noqa: BLE001
            return found
        if stale:
            # Only rows whose last_used predates today are touched, so the warm read
            # path writes at most once per row per day instead of on every build.
            self._touch(stale)
        return found

    def _store(self, rows: list[tuple[str, str, int, str]]) -> None:
        connection = self._open()
        if connection is None or not rows:
            return
        now = _now_iso()
        try:
            with self._lock:
                connection.execute("BEGIN IMMEDIATE")
                connection.executemany(
                    f"INSERT INTO {_CACHE_TABLE} "  # table name is a module constant, never user input
                    "(content_hash, lang, byte_size, tags_json, first_seen, last_used) "
                    "VALUES (?, ?, ?, ?, ?, ?) "
                    "ON CONFLICT(content_hash) DO UPDATE SET last_used = excluded.last_used",
                    [(*row, now, now) for row in rows],
                )
                connection.commit()
        except Exception:  # noqa: BLE001 -- a cache write must never fail a build
            self._rollback(connection)

    def _touch(self, keys: list[str]) -> None:
        connection = self._open()
        if connection is None:
            return
        now = _now_iso()
        try:
            with self._lock:
                connection.execute("BEGIN IMMEDIATE")
                for start in range(0, len(keys), _SQL_CHUNK):
                    chunk = keys[start : start + _SQL_CHUNK]
                    placeholders = ",".join("?" * len(chunk))
                    connection.execute(
                        f"UPDATE {_CACHE_TABLE} SET last_used = ? "  # table name is a module constant, never user input
                        f"WHERE content_hash IN ({placeholders})",
                        [now, *chunk],
                    )
                connection.commit()
        except Exception:  # noqa: BLE001
            self._rollback(connection)

    @staticmethod
    def _rollback(connection: sqlite3.Connection) -> None:
        try:
            if connection.in_transaction:
                connection.rollback()
        except Exception:  # noqa: BLE001
            pass

    # --- maintenance --------------------------------------------------------

    def prune(self, keep: int = 200_000) -> int:
        """Drop all but the ``keep`` most recently used rows; returns rows deleted.

        Never called on the build path (a cache must not add surprise latency); this is
        for a maintenance job. Deleting the whole table instead is equally safe."""
        connection = self._open()
        if connection is None:
            return 0
        try:
            with self._lock:
                connection.execute("BEGIN IMMEDIATE")
                cursor = connection.execute(
                    f"DELETE FROM {_CACHE_TABLE} WHERE content_hash IN ("  # table name is a module constant, never user input
                    f"SELECT content_hash FROM {_CACHE_TABLE} "
                    "ORDER BY last_used DESC, first_seen DESC LIMIT -1 OFFSET ?)",
                    (max(0, keep),),
                )
                deleted = cursor.rowcount or 0
                connection.commit()
                return deleted
        except Exception:  # noqa: BLE001
            self._rollback(connection)
            return 0

    def clear_memory(self) -> None:
        """Forget the in-process tier (the persistent one is untouched)."""
        with self._lock:
            self._mem.clear()

    def close(self) -> None:
        with self._lock:
            if self._connection is not None:
                try:
                    self._connection.close()
                except Exception:  # noqa: BLE001
                    pass
                self._connection = None
            self._connect_attempted = False


def _now_iso() -> str:
    """Canonical timestamp. Imported lazily — only the SQLite tier needs it, and that
    tier already required :mod:`omniagentos.contracts` to resolve the database path."""
    from omniagentos.contracts import utc_now_iso

    return utc_now_iso()


_default_cache: TagCache | None = None
_default_cache_lock = RLock()


def default_cache() -> TagCache:
    """The process-wide cache every un-parameterized ``build_repo_map`` call shares."""
    global _default_cache
    with _default_cache_lock:
        if _default_cache is None:
            _default_cache = TagCache()
        return _default_cache


def build_repo_map(
    repo_dir: str,
    focus_files: list[str] | None = None,
    focus_terms: list[str] | None = None,
    max_tokens: int = 1024,
    cache: TagCache | None = None,
) -> str:
    """A compact, ranked, task-aware map of ``repo_dir``.

    ``focus_files`` (paths the task mentions) and ``focus_terms`` (symbols/keywords the
    task mentions) bias the ranking toward what matters for THIS task; omit both for a
    generic "what is this repo" map. ``max_tokens`` bounds the output.

    Extraction goes through a content-addressed :class:`TagCache` (the process-wide
    :func:`default_cache` unless one is passed), so a second call over an unchanged
    repo re-ranks without re-parsing; ranking is deliberately NOT cached, since it is
    both cheap and dependent on this call's focus."""
    all_tags = (cache or default_cache()).tags_for_repo(repo_dir)
    if not all_tags:
        return ""
    ranked = _rank(all_tags, focus_files, focus_terms)
    return render_map(ranked, max_tokens=max_tokens)


class RepoMap:
    """Reusable repo-map builder bound to one repository.

    Thin wrapper over :class:`TagCache` — extraction (ast/regex over every file) is the
    cost, and the cache removes it for content already seen, so a per-task call only
    parses genuinely new bytes and then re-ranks. Safe to hold for a process lifetime
    and call repeatedly with different task focus."""

    def __init__(self, repo_dir: str, cache: TagCache | None = None) -> None:
        self.repo_dir = os.path.abspath(repo_dir)
        self.cache = cache or default_cache()

    def build(
        self,
        focus_files: list[str] | None = None,
        focus_terms: list[str] | None = None,
        max_tokens: int = 1024,
    ) -> str:
        return build_repo_map(
            self.repo_dir,
            focus_files=focus_files,
            focus_terms=focus_terms,
            max_tokens=max_tokens,
            cache=self.cache,
        )
