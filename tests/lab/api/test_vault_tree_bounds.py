"""``GET /api/lab/vault/tree`` is bounded by default and cached briefly.

Before this lane the route walked the whole vault — ~1k files, each opened and
frontmatter-parsed, up to 50s — on EVERY request, unauthenticated, with no way
for a caller to ask for less. It now answers with a bounded slice by default
(depth 3 / 500 notes) and says so in the payload; the full walk is still
available, but only when a caller explicitly asks for it.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest

from omniagentos.lab.api.routes.lab import _reset_vault_tree_cache

_FRONTMATTER = (
    "---\n"
    "id: {note_id}\n"
    "type: run\n"
    "discipline: writing\n"
    "created: '2026-01-01T00:00:00Z'\n"
    "source_run: null\n"
    "confidence: high\n"
    "status: active\n"
    "supersedes: null\n"
    "---\n"
    "# {title}\n"
)


def _run(coro: Any) -> httpx.Response:
    return asyncio.run(coro)


@pytest.fixture(autouse=True)
def _clean_cache() -> Iterator[None]:
    _reset_vault_tree_cache()
    yield
    _reset_vault_tree_cache()


@pytest.fixture
def vault(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A vault with notes at depth 1, 2, 3 and 4."""
    root = tmp_path / "vault"
    for depth in range(1, 5):
        folder = root
        for level in range(1, depth):
            folder = folder / f"level{level}"
        folder.mkdir(parents=True, exist_ok=True)
        (folder / f"note-d{depth}.md").write_text(
            _FRONTMATTER.format(note_id=f"note-d{depth}", title=f"Depth {depth}"),
            encoding="utf-8",
        )
    monkeypatch.setenv("OMNIAGENTOS_VAULT_DIR", str(root))
    return root


def _note_ids(payload: dict[str, Any]) -> set[str]:
    found = {note["id"] for note in payload["notes"]}
    for folder in payload["folders"]:
        found |= _note_ids(folder)
    return found


def test_default_read_is_bounded_and_says_so(asgi_client: httpx.AsyncClient, vault: Path) -> None:
    body = _run(asgi_client.get("/api/lab/vault/tree")).json()
    assert body["depth"] == 3
    assert body["limit"] == 500
    # depth 4 is BELOW the default bound and is left out; 1..3 are returned.
    assert _note_ids(body) == {"note-d1", "note-d2", "note-d3"}
    assert body["note_count"] == 3
    assert body["truncated"] is False


def test_depth_can_be_widened(asgi_client: httpx.AsyncClient, vault: Path) -> None:
    body = _run(asgi_client.get("/api/lab/vault/tree", params={"depth": 4})).json()
    assert _note_ids(body) == {"note-d1", "note-d2", "note-d3", "note-d4"}


def test_full_walk_is_available_on_request(asgi_client: httpx.AsyncClient, vault: Path) -> None:
    body = _run(asgi_client.get("/api/lab/vault/tree", params={"full": 1})).json()
    assert body["depth"] is None
    assert body["limit"] is None
    assert _note_ids(body) == {"note-d1", "note-d2", "note-d3", "note-d4"}
    assert body["truncated"] is False


def test_limit_caps_the_notes_opened_and_reports_truncation(
    asgi_client: httpx.AsyncClient, vault: Path
) -> None:
    body = _run(asgi_client.get("/api/lab/vault/tree", params={"limit": 2})).json()
    assert body["note_count"] == 2
    assert body["truncated"] is True
    assert len(_note_ids(body)) == 2


def test_a_limit_equal_to_the_note_count_is_not_truncation(
    asgi_client: httpx.AsyncClient, vault: Path
) -> None:
    """Nothing was skipped, so nothing was truncated.

    The vault holds 3 notes within the default depth; asking for exactly 3 is a
    COMPLETE answer. Reporting ``truncated`` here makes a client render "you are
    looking at a slice" over the whole vault, and there is no way for it to tell
    the difference — the flag is the only signal it has.
    """
    body = _run(asgi_client.get("/api/lab/vault/tree", params={"limit": 3})).json()
    assert body["note_count"] == 3
    assert body["truncated"] is False


def test_a_full_walk_limited_to_every_note_is_not_truncation(
    asgi_client: httpx.AsyncClient, vault: Path
) -> None:
    """Depth 4 holds 4 notes; a limit of 4 leaves nothing out."""
    body = _run(asgi_client.get("/api/lab/vault/tree", params={"depth": 4, "limit": 4})).json()
    assert body["note_count"] == 4
    assert body["truncated"] is False


def test_bounds_are_deterministic_not_filesystem_order(
    asgi_client: httpx.AsyncClient, vault: Path
) -> None:
    """The same bounded request must always return the same notes."""
    first = _note_ids(_run(asgi_client.get("/api/lab/vault/tree", params={"limit": 2})).json())
    _reset_vault_tree_cache()
    second = _note_ids(_run(asgi_client.get("/api/lab/vault/tree", params={"limit": 2})).json())
    assert first == second


def test_repeat_reads_are_served_from_the_ttl_cache(
    asgi_client: httpx.AsyncClient, vault: Path
) -> None:
    """A note written between two reads is NOT seen until the cache expires.

    Asserting the cache exists by its only observable consequence. A few seconds
    of staleness is the trade for not re-walking ~1k files per request; the
    reset hook is what a writer (or a test) uses to make a new note visible now.
    """
    first = _run(asgi_client.get("/api/lab/vault/tree")).json()
    (vault / "note-new.md").write_text(
        _FRONTMATTER.format(note_id="note-new", title="New"), encoding="utf-8"
    )
    cached = _run(asgi_client.get("/api/lab/vault/tree")).json()
    assert _note_ids(cached) == _note_ids(first)
    assert "note-new" not in _note_ids(cached)

    _reset_vault_tree_cache()
    fresh = _run(asgi_client.get("/api/lab/vault/tree")).json()
    assert "note-new" in _note_ids(fresh)


def test_cache_is_keyed_per_bound(asgi_client: httpx.AsyncClient, vault: Path) -> None:
    """A cached bounded tree must never be served for a different bound."""
    bounded = _run(asgi_client.get("/api/lab/vault/tree", params={"limit": 1})).json()
    full = _run(asgi_client.get("/api/lab/vault/tree", params={"full": 1})).json()
    assert bounded["note_count"] == 1
    assert full["note_count"] == 4


def test_invalid_bounds_are_refused(asgi_client: httpx.AsyncClient, vault: Path) -> None:
    assert _run(asgi_client.get("/api/lab/vault/tree", params={"depth": 0})).status_code == 422
    assert _run(asgi_client.get("/api/lab/vault/tree", params={"limit": 0})).status_code == 422
