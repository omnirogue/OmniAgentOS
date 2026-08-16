-- REPO-MAP tag cache (PKG-REPOMAP). One row per distinct blob of source the
-- repo-map has ever parsed. The key is CONTENT, not path+mtime:
-- sha256(lang \0 raw-file-bytes), so the same bytes always resolve to the same
-- row no matter where they live. A branch switch, a fresh worktree, a `git
-- revert`, a rebase or a bare `touch` therefore costs ZERO parsing -- all four
-- change mtimes without changing content, which is exactly what an mtime-keyed
-- cache misses on. Two identical files (vendored copies, generated twins) also
-- share one row.
--
-- PURE CACHE -- safe to delete and rebuild at ANY time. Every row is
-- reconstructible by re-reading the source and re-running the stdlib extractors
-- in omniagentos/repomap/tags.py; nothing else in the schema references this
-- table and no foreign key points at it. `DELETE FROM repomap_tag_cache` (or
-- dropping the table and re-running this migration) is always safe: the only
-- consequence is that the next build_repo_map() call is slower once.
--
-- The payload is deliberately PATH-INDEPENDENT (definitions carry no rel_path);
-- the reader rebinds whatever path it found the bytes at. See TagCache in
-- omniagentos/repomap/service.py, which also swallows every error here -- an
-- unreachable, locked or un-migrated database degrades to an in-process cache.
CREATE TABLE repomap_tag_cache (
    content_hash TEXT PRIMARY KEY,   -- sha256 hex of (lang \0 raw file bytes)
    lang         TEXT NOT NULL,      -- python|jsts -- which extractor produced the payload
    byte_size    INTEGER NOT NULL,   -- size of the hashed source, for cache accounting
    tags_json    TEXT NOT NULL,      -- {"d":[[name,kind,line,signature],...],"r":{name:count}}
    first_seen   TEXT NOT NULL,      -- ISO8601 UTC: when these bytes were first parsed
    last_used    TEXT NOT NULL       -- ISO8601 UTC: last build that read the row (LRU pruning)
);

-- Supports LRU pruning (TagCache.prune) of a cache that would otherwise keep a
-- row for every version of every file ever seen.
CREATE INDEX idx_repomap_tag_cache_last_used ON repomap_tag_cache(last_used);
