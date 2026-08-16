"""Tier2 live probe: real LLM knowledge extraction into the Postgres test DB.

OMNIAGENTOS_KNOWLEDGE_EXTRACT_MODEL stays pinned cheap (haiku — the default),
OMNIAGENTOS_KNOWLEDGE_EMBED=fake keeps GPU/Ollama out of the loop entirely.
Skips (with a precise reason) when Postgres is down / the test DB is absent or
when the claude CLI is unavailable; the extraction itself is a real
cli-claude harness call (record_cli_call — cost unreported for CLI harnesses).
DSN idiom follows tests/knowledge/conftest.py (knowledge.config.test_dsn).
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

pytestmark = pytest.mark.live

_PARAGRAPH = (
    "The Orion deploy pipeline requires a signed release manifest before any "
    "production promotion. The manifest is produced by the build server and is "
    "checked by the deploy gate."
)


def _pg_skip_reason() -> str | None:
    """Probe the knowledge test DB; return a skip reason or None when usable."""
    import psycopg

    from omniagentos.knowledge.config import test_dsn

    try:
        with psycopg.connect(test_dsn(), connect_timeout=3):
            pass
    except Exception as exc:  # noqa: BLE001 - any connect failure is the gate
        return f"postgres knowledge test DB unreachable at {test_dsn()!r}: {exc}"
    return None


def test_llm_extraction_yields_candidate_fact(
    fh_budget, monkeypatch: pytest.MonkeyPatch
) -> None:
    reason = _pg_skip_reason()
    if reason:
        pytest.skip(reason)
    if shutil.which("claude") is None:
        pytest.skip("claude CLI binary not on PATH — cannot run the haiku extraction call")
    if not (Path.home() / ".claude").exists() and not list(
        Path.home().glob(".claude-account-*")
    ):
        pytest.skip("no claude CLI auth dir (~/.claude or ~/.claude-account-*) on this host")
    fh_budget.require_headroom(cli=True)

    # Cheap model pin (haiku is the documented default; pin it explicitly so an
    # operator override can never make this lane expensive) + zero-GPU embedder.
    monkeypatch.setenv("OMNIAGENTOS_KNOWLEDGE_EXTRACT_MODEL", "haiku")
    monkeypatch.setenv("OMNIAGENTOS_KNOWLEDGE_EMBED", "fake")

    from omniagentos.knowledge.config import test_dsn
    from omniagentos.knowledge.ingest import ingest_episode
    from omniagentos.knowledge.migrate import migrate
    from omniagentos.knowledge.store import KnowledgeStore
    from omniagentos.knowledge.testing import make_fake_embedder

    try:
        migrate(test_dsn())
    except Exception as exc:  # noqa: BLE001 - unprovisioned cluster == DB absent
        pytest.skip(f"knowledge test DB not provisioned (migrate failed): {exc}")

    store = KnowledgeStore(test_dsn(), embedder=make_fake_embedder())

    fh_budget.record_cli_call()
    # extractor=None -> the real LLMExtractor over the cli-claude adapter.
    result = ingest_episode(
        store,
        source="human",
        content=_PARAGRAPH,
        source_ref="fh-tier2-knowledge-probe",
        discipline="deployment",
    )

    assert result.episode_id > 0
    assert len(result.fact_ids) >= 1, (
        "live haiku extraction produced zero candidate facts from a two-sentence "
        f"factual paragraph (episode_id={result.episode_id}) — extraction path unhealthy"
    )
    # The candidate fact really landed in Postgres (quarantined by default).
    fact = store.get_fact(result.fact_ids[0])
    assert fact is not None, (
        f"extracted fact id {result.fact_ids[0]} not readable back from the store"
    )
