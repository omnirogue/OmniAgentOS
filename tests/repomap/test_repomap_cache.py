"""Repo-map tag cache: content addressing, persistence, and fail-open behaviour.

The point of these tests is the *content*-addressed key. An mtime cache is correct but
useless in the situations that actually occur — branch switch, new worktree, rebase,
revert — because all of them rewrite mtimes without changing a byte. ``touch`` is the
smallest reproduction of that, and ``test_touch_without_content_change_is_a_hit`` is the
test the whole design exists for.
"""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path

import pytest

from omniagentos.db.migrate import migrate
from omniagentos.repomap.service import TagCache, build_repo_map
from omniagentos.repomap.tags import TagPayload, content_hash, extract_bytes

_SOURCE = "class Alpha:\n    def go(self):\n        return beta_helper()\n"
_OTHER = "def beta_helper():\n    return 1\n"


def _write(base: Path, rel: str, text: str) -> None:
    path = base / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    _write(root, "a.py", _SOURCE)
    _write(root, "b.py", _OTHER)
    return root


@pytest.fixture
def db_path(tmp_path: Path) -> str:
    """A migrated database — the persistent cache tier refuses to create one itself."""
    path = tmp_path / "cache.db"
    migrate(str(path))
    return str(path)


def _rows(db_path: str) -> list[sqlite3.Row]:
    connection = sqlite3.connect(db_path)
    connection.row_factory = sqlite3.Row
    try:
        return connection.execute("SELECT * FROM repomap_tag_cache").fetchall()
    finally:
        connection.close()


# --- the headline: content, not mtime ---------------------------------------


def test_touch_without_content_change_is_a_hit(repo: Path, db_path: str) -> None:
    cache = TagCache(db_path=db_path)
    first = build_repo_map(str(repo), max_tokens=300, cache=cache)
    assert cache.parses == 2

    for name in ("a.py", "b.py"):  # bump mtimes, leave every byte alone
        target = repo / name
        stamp = target.stat().st_mtime + 10_000
        os.utime(target, (stamp, stamp))

    assert build_repo_map(str(repo), max_tokens=300, cache=cache) == first
    assert cache.parses == 2, "an mtime bump must not cost a single parse"

    fresh = TagCache(db_path=db_path)  # and the persistent tier hits on it too
    assert build_repo_map(str(repo), max_tokens=300, cache=fresh) == first
    assert fresh.parses == 0 and fresh.db_hits == 2


def test_content_change_is_a_miss_and_the_map_follows(repo: Path, db_path: str) -> None:
    cache = TagCache(db_path=db_path)
    first = build_repo_map(str(repo), max_tokens=400, cache=cache)
    assert "class Alpha" in first

    _write(repo, "a.py", "class Renamed:\n    def go(self):\n        return 2\n")
    second = build_repo_map(str(repo), max_tokens=400, cache=cache)

    assert cache.parses == 3, "only the changed file re-parses"
    assert "class Renamed" in second and "class Alpha" not in second


def test_reverting_content_reuses_the_original_parse(repo: Path, db_path: str) -> None:
    """The mtime cache's worst case: a revert restores old bytes with a new mtime."""
    cache = TagCache(db_path=db_path)
    original = build_repo_map(str(repo), max_tokens=400, cache=cache)
    _write(repo, "a.py", "def temporary():\n    return 0\n")
    build_repo_map(str(repo), max_tokens=400, cache=cache)
    after_edit = cache.parses

    _write(repo, "a.py", _SOURCE)  # revert
    assert build_repo_map(str(repo), max_tokens=400, cache=cache) == original
    assert cache.parses == after_edit


def test_duplicate_and_moved_files_parse_once(tmp_path: Path, db_path: str) -> None:
    root = tmp_path / "repo"
    _write(root, "one.py", _SOURCE)
    _write(root, "nested/copy.py", _SOURCE)  # byte-identical twin
    cache = TagCache(db_path=db_path)

    out = build_repo_map(str(root), max_tokens=600, cache=cache)
    assert cache.parses == 1, "identical bytes are one cache entry, whatever the path"
    assert "one.py:" in out and os.path.join("nested", "copy.py") + ":" in out

    (root / "one.py").rename(root / "renamed.py")  # a move keeps the parse
    build_repo_map(str(root), max_tokens=600, cache=cache)
    assert cache.parses == 1


def test_a_deleted_file_leaves_the_map(repo: Path, db_path: str) -> None:
    cache = TagCache(db_path=db_path)
    assert "b.py:" in build_repo_map(str(repo), max_tokens=400, cache=cache)
    (repo / "b.py").unlink()
    assert "b.py:" not in build_repo_map(str(repo), max_tokens=400, cache=cache)


def test_caching_changes_nothing_observable(repo: Path, db_path: str) -> None:
    _write(repo, "c.tsx", "export function Button() {\n  return null;\n}\n")
    uncached = build_repo_map(str(repo), max_tokens=500, cache=TagCache(persist=False))
    cached = build_repo_map(str(repo), max_tokens=500, cache=TagCache(db_path=db_path))
    warm = build_repo_map(str(repo), max_tokens=500, cache=TagCache(db_path=db_path))
    assert uncached == cached == warm


# --- persistent tier ---------------------------------------------------------


def test_a_fresh_cache_object_hits_the_database(repo: Path, db_path: str) -> None:
    cold = TagCache(db_path=db_path)
    first = build_repo_map(str(repo), max_tokens=300, cache=cold)
    assert cold.parses == 2 and cold.db_hits == 0

    warm = TagCache(db_path=db_path)  # stands in for a new process
    assert build_repo_map(str(repo), max_tokens=300, cache=warm) == first
    assert warm.parses == 0, "a restart must not re-parse unchanged content"
    assert warm.db_hits == 2


def test_stored_rows_carry_the_documented_columns(repo: Path, db_path: str) -> None:
    build_repo_map(str(repo), max_tokens=300, cache=TagCache(db_path=db_path))
    rows = {row["content_hash"]: row for row in _rows(db_path)}
    assert len(rows) == 2

    key = content_hash("a.py", _SOURCE.encode())
    row = rows[key]
    assert row["lang"] == "python"
    assert row["byte_size"] == len(_SOURCE.encode())
    assert row["first_seen"] and row["last_used"]
    payload = TagPayload.from_json(row["tags_json"])
    assert payload is not None
    assert payload == TagPayload.of(extract_bytes("a.py", _SOURCE.encode()))
    assert all("a.py" not in field for entry in payload.definitions for field in entry[:2]), (
        "the payload must stay path-independent"
    )


def test_the_table_is_a_pure_cache_and_can_be_emptied(repo: Path, db_path: str) -> None:
    build_repo_map(str(repo), max_tokens=300, cache=TagCache(db_path=db_path))
    expected = build_repo_map(str(repo), max_tokens=300, cache=TagCache(db_path=db_path))

    connection = sqlite3.connect(db_path)
    with connection:
        connection.execute("DELETE FROM repomap_tag_cache")
    connection.close()

    rebuilt = TagCache(db_path=db_path)
    assert build_repo_map(str(repo), max_tokens=300, cache=rebuilt) == expected
    assert rebuilt.parses == 2  # rebuilt from source, transparently
    assert len(_rows(db_path)) == 2


def test_a_corrupt_row_is_re_parsed(repo: Path, db_path: str) -> None:
    expected = build_repo_map(str(repo), max_tokens=300, cache=TagCache(db_path=db_path))
    connection = sqlite3.connect(db_path)
    with connection:
        connection.execute("UPDATE repomap_tag_cache SET tags_json = '{not json'")
    connection.close()

    cache = TagCache(db_path=db_path)
    assert build_repo_map(str(repo), max_tokens=300, cache=cache) == expected
    assert cache.parses == 2 and cache.db_hits == 0


def test_prune_keeps_the_most_recent_rows(repo: Path, db_path: str) -> None:
    cache = TagCache(db_path=db_path)
    build_repo_map(str(repo), max_tokens=300, cache=cache)
    assert len(_rows(db_path)) == 2
    assert cache.prune(keep=1) == 1
    assert len(_rows(db_path)) == 1


# --- fail-open ---------------------------------------------------------------


def test_a_missing_database_degrades_to_memory_only(repo: Path, tmp_path: Path) -> None:
    absent = tmp_path / "nope" / "missing.db"
    cache = TagCache(db_path=str(absent))
    first = build_repo_map(str(repo), max_tokens=300, cache=cache)

    assert not absent.exists(), "a cache must never conjure a database"
    assert build_repo_map(str(repo), max_tokens=300, cache=cache) == first
    assert cache.parses == 2 and cache.mem_hits == 2  # in-process tier still works


def test_an_unmigrated_database_degrades_to_memory_only(repo: Path, tmp_path: Path) -> None:
    bare = tmp_path / "bare.db"
    sqlite3.connect(bare).close()
    cache = TagCache(db_path=str(bare))
    assert build_repo_map(str(repo), max_tokens=300, cache=cache)
    assert cache.parses == 2 and cache.db_hits == 0


def test_env_flag_disables_the_persistent_tier(
    repo: Path, db_path: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OMNIAGENTOS_REPOMAP_CACHE", "0")
    build_repo_map(str(repo), max_tokens=300, cache=TagCache(db_path=db_path))
    assert _rows(db_path) == []


# --- payload isolation -------------------------------------------------------


def test_bound_tags_are_independent_of_the_cached_payload(repo: Path, db_path: str) -> None:
    cache = TagCache(db_path=db_path)
    first = cache.tags_for_repo(str(repo))
    first[0].definitions.clear()
    first[0].references["injected"] = 99

    second = cache.tags_for_repo(str(repo))
    assert second[0].definitions, "a mutating caller must not poison the cache"
    assert "injected" not in second[0].references


def test_content_hash_separates_languages_but_not_paths() -> None:
    same = b"function f() {}\n"
    assert content_hash("x.py", same) != content_hash("x.ts", same)
    assert content_hash("dir/x.py", same) == content_hash("other/x.py", same)
