"""Real-store regression coverage for persisted voice artifacts."""

from __future__ import annotations

from pathlib import Path

import pytest

import omniagentos.voice.service as voice_service
from omniagentos.db.store import SqliteStore
from omniagentos.steward.config import VoiceConfig
from omniagentos.voice.providers import TTSResult
from omniagentos.voice.service import VOICE_RUN_ID, synthesize_to_artifact


class _AudioProvider:
    name = "fake"

    def synthesize(self, text: str, *, voice_id: str | None = None) -> TTSResult:
        del text, voice_id
        return TTSResult(ok=True, audio=b"ID3-real-store-regression", status="ready")


def test_synthesize_persists_artifact_with_terminal_voice_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A real FK-enforcing store accepts voice artifacts and never queues its owner."""
    store = SqliteStore(str(tmp_path / "voice.db"))
    monkeypatch.setattr(voice_service, "get_provider", lambda *_: _AudioProvider())

    result = synthesize_to_artifact(
        "voice FK regression guard",
        store=store,
        cfg=VoiceConfig(default_provider="fake"),
        vault_dir=str(tmp_path / "vault"),
    )

    assert result["ok"] is True
    artifacts = store.get_artifacts(VOICE_RUN_ID)
    assert len(artifacts) == 1
    assert artifacts[0]["id"] == result["artifact_id"]
    task = store.get_task(VOICE_RUN_ID)
    run = store.get_run(VOICE_RUN_ID)
    assert task is not None
    assert task["state"] == "completed"
    assert run is not None
    assert run["state"] == "completed"
    assert store.claim_next_run("voice-regression-worker") is None
