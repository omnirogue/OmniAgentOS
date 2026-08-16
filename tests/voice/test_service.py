"""Service-layer tests: text cap, unknown provider, status listing."""

from __future__ import annotations

from pathlib import Path

from omniagentos.steward.config import VoiceConfig
from omniagentos.voice.service import TEXT_CAP, list_provider_statuses, synthesize_to_artifact
from tests.api.fake_store import FakeStore


def test_text_cap(store: FakeStore, vault_dir: Path, voice_cfg: VoiceConfig) -> None:
    too_long = "x" * (TEXT_CAP + 1)
    result = synthesize_to_artifact(
        too_long,
        store=store,
        cfg=voice_cfg,
        vault_dir=str(vault_dir),
    )
    assert result["ok"] is False
    assert result["status"] == "error"
    assert str(TEXT_CAP) in result["detail"]
    assert store.get_artifacts("voice") == []


def test_unknown_provider(store: FakeStore, vault_dir: Path) -> None:
    result = synthesize_to_artifact(
        "hello",
        provider="not-a-provider",
        store=store,
        cfg=VoiceConfig(),
        vault_dir=str(vault_dir),
    )
    assert result["ok"] is False
    assert result["status"] == "error"
    assert "unknown" in result["detail"].lower()


def test_empty_text(store: FakeStore, vault_dir: Path, voice_cfg: VoiceConfig) -> None:
    result = synthesize_to_artifact(
        "   ",
        store=store,
        cfg=voice_cfg,
        vault_dir=str(vault_dir),
    )
    assert result["ok"] is False
    assert result["status"] == "error"


def test_providers_status_no_env(clear_voice_env: None) -> None:
    rows = list_provider_statuses(VoiceConfig())
    by_name = {r["provider"]: r for r in rows}
    assert by_name["elevenlabs"]["status"] == "no_key"
    assert by_name["xai"]["status"] == "unavailable"
