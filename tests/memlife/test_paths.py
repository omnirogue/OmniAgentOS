"""Lane 0: single path resolver for memlife store + lessons.

Decisive invariant (SPEC §2):
  lessons_path(MEMLIFE_PROJECT, memories_root=memories_root())
    == memlife_store_root() / "LESSONS.md"

under the default (no env) and under OMNIAGENTOS_MEMORIES_DIR. If those two
drift, Lane C's write end and read end address different files and the whole
chain silently returns nothing.

Named counterfeit: ``counterfeit_store_ignores_memories_root`` — resolve the
store as a hardcoded ``<repo>/var/memories/memlife`` while ``memories_root()``
honours ``OMNIAGENTOS_MEMORIES_DIR``. A naive "default store path looks right"
test stays green; the decisive invariant under an env override goes red.

Revert-check: restore ``routes/memlife.memlife_store_root`` to the pre-Lane-0
independent resolver (hardcoded ``var/memories/memlife``, ignores
``OMNIAGENTOS_MEMORIES_DIR``). Then
``routes.memlife_store_root() != paths.memlife_store_root()`` under a
memories-dir override, and the delegation test fails.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from omniagentos.memlife.paths import (
    ENV_MEMORIES,
    ENV_STORE,
    MEMLIFE_PROJECT,
    memlife_store_root,
    memories_root,
)
from omniagentos.memlife.render import LESSONS_FILENAME, lessons_path


def _clear_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(ENV_STORE, raising=False)
    monkeypatch.delenv(ENV_MEMORIES, raising=False)
    monkeypatch.delenv("OMNIAGENTOS_MEMLIFE_STORE", raising=False)
    monkeypatch.delenv("OMNIAGENTOS_MEMORIES_DIR", raising=False)


# ---------------------------------------------------------------------------
# Decisive invariant
# ---------------------------------------------------------------------------


class TestLessonsPathAgreesWithStoreRoot:
    """The one assertion that makes a split-brain Lane C impossible to miss."""

    def test_default_paths_agree(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _clear_env(monkeypatch)
        expected = memlife_store_root() / LESSONS_FILENAME
        actual = lessons_path(MEMLIFE_PROJECT, memories_root=memories_root())
        assert actual == expected
        # Defaults land under the package-anchored repo, never cwd.
        assert actual.parts[-3:] == ("memories", "memlife", "LESSONS.md")

    def test_memories_dir_override_keeps_agreement(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _clear_env(monkeypatch)
        root = tmp_path / "custom-memories"
        root.mkdir()
        monkeypatch.setenv(ENV_MEMORIES, str(root))

        expected = memlife_store_root() / LESSONS_FILENAME
        actual = lessons_path(MEMLIFE_PROJECT, memories_root=memories_root())
        assert actual == expected
        assert actual == root / MEMLIFE_PROJECT / LESSONS_FILENAME

    def test_store_override_is_honoured(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """ENV_STORE wins for the store root; lessons still use memories_root.

        When operators set only OMNIAGENTOS_MEMLIFE_STORE they are pointing the
        review surface at a specific tree — the invariant under §2 is about the
        *shared* memories root, not a forced equality under a store-only
        override. Assert the store override is load-bearing, and that when
        ENV_STORE is set to memories_root()/MEMLIFE_PROJECT the paths still
        agree.
        """
        _clear_env(monkeypatch)
        memories = tmp_path / "memories"
        memories.mkdir()
        monkeypatch.setenv(ENV_MEMORIES, str(memories))

        store = memories / MEMLIFE_PROJECT
        monkeypatch.setenv(ENV_STORE, str(store))
        assert memlife_store_root() == store
        assert lessons_path(MEMLIFE_PROJECT, memories_root=memories_root()) == (
            store / LESSONS_FILENAME
        )

        other = tmp_path / "elsewhere" / "memlife"
        monkeypatch.setenv(ENV_STORE, str(other))
        assert memlife_store_root() == other


# ---------------------------------------------------------------------------
# Anchoring discipline (never cwd)
# ---------------------------------------------------------------------------


class TestRepoAnchoredNotCwd:
    def test_defaults_ignore_process_cwd(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _clear_env(monkeypatch)
        before_mem = memories_root()
        before_store = memlife_store_root()
        monkeypatch.chdir(tmp_path)
        assert memories_root() == before_mem
        assert memlife_store_root() == before_store
        # And they are not under the fake cwd.
        assert not str(memories_root()).startswith(str(tmp_path))
        assert not str(memlife_store_root()).startswith(str(tmp_path))


# ---------------------------------------------------------------------------
# Routes delegate to paths (Lane 0 edit surface)
# ---------------------------------------------------------------------------


class TestRoutesDelegateToPaths:
    def test_routes_memlife_store_root_matches_paths(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from omniagentos.api.routes import memlife as memlife_routes

        _clear_env(monkeypatch)
        memories = tmp_path / "memories-via-routes"
        memories.mkdir()
        monkeypatch.setenv(ENV_MEMORIES, str(memories))
        if hasattr(memlife_routes, "_clear_store_root_cache"):
            memlife_routes._clear_store_root_cache()

        # Under a memories-dir override the pre-Lane-0 routes resolver returned
        # <repo>/var/memories/memlife and ignored ENV_MEMORIES. Delegation must
        # follow paths so the two surfaces agree.
        assert memlife_routes.memlife_store_root() == memlife_store_root()
        assert memlife_routes.memlife_store_root() == memories / MEMLIFE_PROJECT

    def test_routes_store_env_override(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from omniagentos.api.routes import memlife as memlife_routes

        _clear_env(monkeypatch)
        store = tmp_path / "explicit-store"
        monkeypatch.setenv(ENV_STORE, str(store))
        if hasattr(memlife_routes, "_clear_store_root_cache"):
            memlife_routes._clear_store_root_cache()
        assert memlife_routes.memlife_store_root() == store
        assert memlife_routes.memlife_store_root() == memlife_store_root()


# ---------------------------------------------------------------------------
# Named counterfeit — must go RED if the store ignores memories_root
# ---------------------------------------------------------------------------


def test_counterfeit_store_ignores_memories_root_is_detectable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """counterfeit_store_ignores_memories_root.

    Simulate the pre-Lane-0 bug: store default is always
    ``<repo>/var/memories/memlife`` even when ``OMNIAGENTOS_MEMORIES_DIR`` is
    set. The decisive invariant under that override must fail — proving the
    test catches the split-brain the counterfeit would reintroduce.
    """
    _clear_env(monkeypatch)
    root = tmp_path / "alt-memories"
    root.mkdir()
    monkeypatch.setenv(ENV_MEMORIES, str(root))

    # Real production resolvers: still agree.
    assert lessons_path(MEMLIFE_PROJECT, memories_root=memories_root()) == (
        memlife_store_root() / LESSONS_FILENAME
    )

    # Counterfeit: hardcode the old routes default (repo-anchored, ignores env).
    repo_root = Path(__file__).resolve().parents[2]
    counterfeit_store = repo_root / "var" / "memories" / "memlife"
    lessons = lessons_path(MEMLIFE_PROJECT, memories_root=memories_root())
    assert lessons != counterfeit_store / LESSONS_FILENAME, (
        "counterfeit must diverge under OMNIAGENTOS_MEMORIES_DIR — if this "
        "equality holds, the decisive test cannot kill the counterfeit"
    )
