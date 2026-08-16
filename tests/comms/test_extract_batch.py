"""Unit tests for omniagentos.comms.extract_batch, independent of PostgreSQL.

A tiny in-memory double stands in for KnowledgeStore at exactly the surface
``ingest_episode`` needs (see omniagentos/knowledge/ingest.py) so these tests run
regardless of PG availability. The REAL, PG-backed end-to-end path (webhook ->
curate -> extract_batch -> quarantine) is covered separately by
test_injection_e2e.py.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

from omniagentos.comms.extract_batch import main, run_batch
from omniagentos.db.store import SqliteStore
from omniagentos.knowledge.contracts import CandidateFact, ExtractionResult
from omniagentos.knowledge.extract import FixtureExtractor
from omniagentos.steward.config import AlertsConfig, CurationConfig, StewardConfig
from omniagentos.steward.store import StewardStore
from tests.support.db_template import make_store


class _FakeKnowledgeStore:
    """The minimal surface ingest_episode touches; no network, no PG."""

    def __init__(self, *, fail_on_episode: bool = False) -> None:
        self._embedder = None
        self._fail_on_episode = fail_on_episode
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
        if self._fail_on_episode:
            raise RuntimeError("simulated storage failure")
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
        self.facts.append(
            {
                "id": fact_id,
                "statement": statement,
                "episode_id": episode_id,
                "trust": trust,
                "status": "quarantined",
            }
        )
        return fact_id

    def get_entity(self, name: str, kind: str) -> None:
        return None

    def upsert_entity(
        self, *, name: str, kind: str, summary: str | None = None, name_embedding: Any = None
    ) -> int:
        return 0

    def add_edge(self, **kwargs: Any) -> int:
        return 0


def _cfg(*, batch_limit: int = 50, vip: list[str] | None = None) -> StewardConfig:
    cfg = StewardConfig()
    cfg.alerts = AlertsConfig(vip_senders=vip or ["vip@example.com"])
    cfg.curation = CurationConfig(
        extract_vip=True, extract_goal_keywords=True, batch_limit=batch_limit
    )
    return cfg


def _steward(tmp_path: Path) -> StewardStore:
    return StewardStore(make_store(SqliteStore, tmp_path / "batch.db"))


def _insert(steward: StewardStore, external_id: str, **overrides: Any) -> dict[str, Any]:
    row = {
        "source": "zapier",
        "external_id": external_id,
        "sender": "someone@example.com",
        "subject": "hi",
        "body_text": "just checking in",
    }
    row.update(overrides)
    inserted, _created = steward.insert_comms_message(row)
    return inserted


def test_selected_row_is_extracted_and_marked(tmp_path: Path) -> None:
    steward = _steward(tmp_path)
    _insert(steward, "vip-1", sender="vip@example.com", subject="urgent", body_text="please help")
    fake_store = _FakeKnowledgeStore()
    extractor = FixtureExtractor(ExtractionResult(facts=[CandidateFact(statement="A fact")]))

    summary = run_batch(steward, _cfg(), extractor=extractor, knowledge_store=fake_store)

    assert summary == {"scanned": 1, "extracted": 1, "skipped": 0, "failed": 0}
    row = steward.list_comms_messages(kb_status="extracted")[0]
    assert row["episode_id"] == 1
    assert len(fake_store.episodes) == 1
    assert len(fake_store.facts) == 1
    assert fake_store.facts[0]["status"] == "quarantined"


def test_unselected_row_is_skipped(tmp_path: Path) -> None:
    steward = _steward(tmp_path)
    _insert(
        steward, "plain-1", sender="nobody@example.com", subject="hi", body_text="nothing special"
    )
    summary = run_batch(
        steward, _cfg(), extractor=FixtureExtractor(), knowledge_store=_FakeKnowledgeStore()
    )
    assert summary == {"scanned": 1, "extracted": 0, "skipped": 1, "failed": 0}
    assert steward.list_comms_messages(kb_status="skipped")[0]["external_id"] == "plain-1"


def test_extraction_exception_marks_failed(tmp_path: Path) -> None:
    steward = _steward(tmp_path)
    _insert(steward, "vip-fail", sender="vip@example.com")
    summary = run_batch(
        steward,
        _cfg(),
        extractor=FixtureExtractor(),
        knowledge_store=_FakeKnowledgeStore(fail_on_episode=True),
    )
    assert summary == {"scanned": 1, "extracted": 0, "skipped": 0, "failed": 1}
    assert steward.list_comms_messages(kb_status="failed")[0]["external_id"] == "vip-fail"


def test_failed_row_is_retried_on_a_later_batch_run(tmp_path: Path) -> None:
    """M1: 'failed' must not be a terminal dead end — once whatever caused the
    transient failure clears, a later run picks the SAME row back up.
    """
    steward = _steward(tmp_path)
    _insert(steward, "vip-retry", sender="vip@example.com")

    first = run_batch(
        steward,
        _cfg(),
        extractor=FixtureExtractor(),
        knowledge_store=_FakeKnowledgeStore(fail_on_episode=True),
    )
    assert first == {"scanned": 1, "extracted": 0, "skipped": 0, "failed": 1}
    assert steward.list_comms_messages(kb_status="failed")[0]["external_id"] == "vip-retry"

    second = run_batch(
        steward, _cfg(), extractor=FixtureExtractor(), knowledge_store=_FakeKnowledgeStore()
    )
    assert second == {"scanned": 1, "extracted": 1, "skipped": 0, "failed": 0}
    extracted = steward.list_comms_messages(kb_status="extracted")
    assert extracted and extracted[0]["external_id"] == "vip-retry"
    assert steward.list_comms_messages(kb_status="failed") == []


def test_batch_limit_caps_scanned_newest_first(tmp_path: Path) -> None:
    steward = _steward(tmp_path)
    for index in range(5):
        _insert(steward, f"row-{index}", sender="nobody@example.com")
    summary = run_batch(
        steward,
        _cfg(batch_limit=2),
        extractor=FixtureExtractor(),
        knowledge_store=_FakeKnowledgeStore(),
    )
    assert summary["scanned"] == 2
    skipped = steward.list_comms_messages(kb_status="skipped")
    assert {row["external_id"] for row in skipped} == {"row-3", "row-4"}


def test_scans_both_none_and_selected_statuses(tmp_path: Path) -> None:
    steward = _steward(tmp_path)
    _insert(steward, "none-row", sender="nobody@example.com")
    operator_row = _insert(steward, "operator-row", sender="nobody@example.com")
    steward.set_message_kb(operator_row["id"], kb_status="selected")

    summary = run_batch(
        steward, _cfg(vip=[]), extractor=FixtureExtractor(), knowledge_store=_FakeKnowledgeStore()
    )
    assert summary["scanned"] == 2
    assert summary["extracted"] == 1  # only the operator-flagged row
    assert summary["skipped"] == 1


def test_knowledge_disabled_still_exits_zero_and_marks_failed(
    tmp_path: Path, monkeypatch: Any
) -> None:
    monkeypatch.delenv("OMNIAGENTOS_KNOWLEDGE", raising=False)
    steward = _steward(tmp_path)
    _insert(steward, "vip-disabled", sender="vip@example.com")
    # No knowledge_store/extractor injected: exercises the real extract_message ->
    # _default_store() path, which returns None when the feature flag is unset.
    summary = run_batch(steward, _cfg())
    assert summary == {"scanned": 1, "extracted": 0, "skipped": 0, "failed": 1}


def test_cli_main_prints_json_summary_and_exits_zero(tmp_path: Path, monkeypatch: Any) -> None:
    monkeypatch.delenv("OMNIAGENTOS_KNOWLEDGE", raising=False)
    monkeypatch.setenv("OMNIAGENTOS_DB", str(tmp_path / "cli.db"))
    exit_code = main([])
    assert exit_code == 0


def test_cli_subprocess_prints_valid_json_and_exits_zero(tmp_path: Path) -> None:
    env = dict(os.environ)
    env.pop("OMNIAGENTOS_KNOWLEDGE", None)
    env["OMNIAGENTOS_DB"] = str(tmp_path / "subprocess.db")
    result = subprocess.run(
        [sys.executable, "-m", "omniagentos.comms.extract_batch"],
        capture_output=True,
        text=True,
        env=env,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0
    summary = json.loads(result.stdout.strip())
    assert set(summary) == {"scanned", "extracted", "skipped", "failed"}
