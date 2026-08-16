"""Unified recall() front door — deterministic fusion + graceful leg failure.

Legs are injected through the fusion registry exactly the way
tests/retrieval/test_recall.py does (recall._ensure_backends leaves
pre-registered names alone), so no live Postgres, vault, or conversation DB is
touched.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest

from omniagentos.retrieval import fusion
from omniagentos.retrieval.recall import RecallLine, recall


@pytest.fixture(autouse=True)
def _isolate_registry_and_hooks() -> Iterator[None]:
    """Snapshot the process-global fusion registry + recall hooks; restore after."""
    import omniagentos.retrieval.recall as recall_mod

    before = fusion.registered_backends()
    for name in list(before):
        fusion.unregister_backend(name)
    saved_hooks = (
        recall_mod._conversation_store_factory,
        recall_mod._metacog_store_factory,
        recall_mod._knowledge_search,
        recall_mod._vault_search,
    )
    recall_mod._conversation_store_factory = None
    recall_mod._metacog_store_factory = None
    recall_mod._knowledge_search = None
    recall_mod._vault_search = None
    try:
        yield
    finally:
        for name in list(fusion.registered_backends()):
            fusion.unregister_backend(name)
        for spec in before.values():
            fusion.register_backend(spec)
        (
            recall_mod._conversation_store_factory,
            recall_mod._metacog_store_factory,
            recall_mod._knowledge_search,
            recall_mod._vault_search,
        ) = saved_hooks


class _Mem:
    def __init__(self, mid: str, statement: str) -> None:
        self.id = mid
        self.statement = statement


class _Hit:
    def __init__(self, relpath: str, snippet: str) -> None:
        self.relpath = relpath
        self.snippet = snippet
        self.title = relpath


def _register(
    name: str,
    items: list[Any],
    id_fn: Any,
    *,
    raise_exc: BaseException | None = None,
) -> None:
    def search(query: str, limit: int) -> list[Any]:
        del query
        if raise_exc is not None:
            raise raise_exc
        return items[:limit]

    fusion.register_backend(fusion.BackendSpec(name=name, search=search, id_fn=id_fn))


def _register_two_legs(*, vault_raises: bool = False) -> None:
    _register(
        "metacog",
        [_Mem("mem_1", "meta one"), _Mem("mem_2", "meta two")],
        id_fn=lambda m: str(m.id),
    )
    _register(
        "vault",
        [_Hit("moc/a.md", "vault alpha"), _Hit("moc/b.md", "vault beta")],
        id_fn=lambda h: h.relpath,
        raise_exc=RuntimeError("vault leg down") if vault_raises else None,
    )


def test_two_seeded_legs_fuse_deterministically() -> None:
    _register_two_legs()
    first = recall("q", top_k=10, sources=["metacog", "vault"])

    assert {line.source for line in first} == {"metacog", "vault"}
    assert len(first) == 4
    assert all(isinstance(line, RecallLine) for line in first)
    # Fused order is a deterministic RRF interleave: identical inputs must
    # produce the identical ranked (ref, source, score) sequence every call.
    for _ in range(3):
        again = recall("q", top_k=10, sources=["metacog", "vault"])
        assert [(x.ref, x.source, x.score) for x in again] == [
            (x.ref, x.source, x.score) for x in first
        ]
    # Rank-1 items of each leg outrank each leg's rank-2 item (1/(k+1) > 1/(k+2)).
    position = {line.ref: i for i, line in enumerate(first)}
    assert position["mem_1"] < position["mem_2"]
    assert position["moc/a.md"] < position["moc/b.md"]
    # Scores are best-first.
    scores = [line.score for line in first]
    assert scores == sorted(scores, reverse=True)


def test_one_raising_leg_degrades_gracefully() -> None:
    _register_two_legs(vault_raises=True)

    lines = recall("q", top_k=10, sources=["metacog", "vault"])

    # No exception escaped, and the healthy leg still answered in full.
    assert {line.source for line in lines} == {"metacog"}
    assert [line.ref for line in lines] == ["mem_1", "mem_2"]
    assert all("meta" in line.text for line in lines)
