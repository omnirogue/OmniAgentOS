"""Unit tests for omniagentos.comms.knowledge_bridge, independent of PostgreSQL.

The REAL, PG-backed end-to-end path is covered separately by
test_injection_e2e.py; these tests exercise extract_message's own dedupe
guard (M1) against small in-memory doubles.
"""

from __future__ import annotations

from typing import Any

from omniagentos.comms.knowledge_bridge import extract_message
from omniagentos.knowledge.extract import FixtureExtractor


class _FakeKnowledgeStore:
    """The minimal surface ingest_episode touches; no network, no PG."""

    def __init__(self) -> None:
        self.episodes: list[dict[str, Any]] = []
        self.facts: list[dict[str, Any]] = []
        self._next_episode_id = 1
        self._next_fact_id = 1

    def add_episode(
        self,
        *,
        source: str,
        content: str,
        source_ref: str | None,
        agent_id: str | None,
        discipline: str | None,
    ) -> int:
        episode_id = self._next_episode_id
        self._next_episode_id += 1
        self.episodes.append(
            {
                "id": episode_id,
                "source": source,
                "content": content,
                "source_ref": source_ref,
                "discipline": discipline,
            }
        )
        return episode_id

    def add_fact(
        self,
        *,
        statement: str,
        episode_id: int,
        discipline: str | None,
        provenance: str,
        trust: float,
        confidence: float,
        importance: float,
        embedding: list[float] | None,
    ) -> int:
        fact_id = self._next_fact_id
        self._next_fact_id += 1
        self.facts.append({"id": fact_id, "statement": statement, "episode_id": episode_id})
        return fact_id

    def get_entity(self, name: str, kind: str) -> None:
        return None

    def upsert_entity(
        self, *, name: str, kind: str, summary: str | None = None, name_embedding: Any = None
    ) -> int:
        return 0

    def add_edge(self, **kwargs: Any) -> int:
        return 0


class _FakeCursor:
    def __init__(self, rows_by_source_ref: dict[str, int]) -> None:
        self._rows_by_source_ref = rows_by_source_ref
        self._result: int | None = None

    def __enter__(self) -> _FakeCursor:
        return self

    def __exit__(self, *exc_info: object) -> bool:
        return False

    def execute(self, _sql: str, params: tuple[Any, ...]) -> None:
        self._result = self._rows_by_source_ref.get(params[0])

    def fetchone(self) -> tuple[int] | None:
        return None if self._result is None else (self._result,)


class _FakeConnection:
    def __init__(self, rows_by_source_ref: dict[str, int]) -> None:
        self._rows_by_source_ref = rows_by_source_ref

    def cursor(self) -> _FakeCursor:
        return _FakeCursor(self._rows_by_source_ref)


class _FakeLock:
    def __enter__(self) -> _FakeLock:
        return self

    def __exit__(self, *exc_info: object) -> bool:
        return False


class _FakeKnowledgeStoreWithLookup(_FakeKnowledgeStore):
    """Adds the private ``_lock``/``_conn()`` surface ``_existing_episode_id``
    uses, pre-seeded with an existing episode for one ``source_ref`` —
    simulating a crash between a PRIOR run's successful ``ingest_episode`` and
    its follow-up ``set_message_kb`` call ever landing.
    """

    def __init__(self, *, rows_by_source_ref: dict[str, int]) -> None:
        super().__init__()
        self._lock = _FakeLock()
        self._rows_by_source_ref = rows_by_source_ref

    def _conn(self) -> _FakeConnection:
        return _FakeConnection(self._rows_by_source_ref)


def _row(**overrides: Any) -> dict[str, Any]:
    base = {
        "id": 1,
        "source": "zapier",
        "external_id": "ext-1",
        "sender": "vip@example.com",
        "sent_at": "2026-01-01T00:00:00Z",
        "subject": "hi",
        "body_text": "body",
    }
    base.update(overrides)
    return base


def test_extract_message_skips_reingest_when_row_already_has_episode_id() -> None:
    """The cheap, in-row check: if the row already carries an episode_id (e.g.
    a defensive caller-supplied value), never re-ingest.
    """
    fake_store = _FakeKnowledgeStore()
    row = _row(external_id="already-done", episode_id=42)

    episode_id = extract_message(
        row, discipline=None, extractor=FixtureExtractor(), store=fake_store
    )

    assert episode_id == 42
    assert fake_store.episodes == []  # no episode was (re-)created


def test_extract_message_dedupes_against_existing_episode_after_simulated_crash() -> None:
    """M1 atomicity: a crash between a PRIOR run's successful ingest_episode and
    its set_message_kb call leaves the row selectable again (no episode_id on
    the row) even though the episode already exists in the graph. extract_message
    must find it by source_ref and return it rather than ingesting a duplicate.
    """
    source_ref = "comms:zapier:crash-recover-1"
    fake_store = _FakeKnowledgeStoreWithLookup(rows_by_source_ref={source_ref: 99})
    row = _row(external_id="crash-recover-1")  # no episode_id: the crash lost it

    episode_id = extract_message(
        row, discipline=None, extractor=FixtureExtractor(), store=fake_store
    )

    assert episode_id == 99
    assert fake_store.episodes == []  # ingest_episode was never called again


def test_extract_message_ingests_normally_when_no_prior_episode_exists() -> None:
    fake_store = _FakeKnowledgeStoreWithLookup(rows_by_source_ref={})
    row = _row(external_id="brand-new-1")

    episode_id = extract_message(
        row, discipline=None, extractor=FixtureExtractor(), store=fake_store
    )

    assert episode_id == 1
    assert len(fake_store.episodes) == 1
    assert fake_store.episodes[0]["source_ref"] == "comms:zapier:brand-new-1"
