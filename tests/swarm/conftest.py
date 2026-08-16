"""Swarm suite isolation.

``write_summary`` appends the run's terminal manifest to the ledger
(``default_ledger_dir()`` honors ``OMNIAGENTOS_LEDGER_DIR``). Tests that
exercise the summary path — directly or through the scheduler's terminal
hook — must never append to the REPO's ``ledger/`` directory, so the whole
suite gets a per-test tmp ledger root. Tests asserting ledger content pass an
explicit ``ledger_dir=`` and are unaffected.
"""

from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _tmp_ledger_dir(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("OMNIAGENTOS_LEDGER_DIR", str(tmp_path / "test-ledger"))

@pytest.fixture(autouse=True)
def _stub_knowledge_ingest(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stub knowledge ingest to avoid live LLM calls during testing.

    Tests in this suite verify ledger manifests, metrics, and effort tracking.
    None assert anything about knowledge extraction, so using a no-op stub
    removes ~195s of live Claude CLI calls from the suite without weakening
    any test assertions.
    """
    def noop_ingest(*args: object, **kwargs: object) -> None:
        pass

    monkeypatch.setattr(
        "omniagentos.knowledge.ingest.ingest_episode",
        noop_ingest,
    )
