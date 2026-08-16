"""ElevenLabs provider and artifact happy-path tests (mocked httpx)."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from omniagentos.contracts import digest
from omniagentos.steward.config import VoiceConfig
from omniagentos.voice.elevenlabs import ElevenLabsProvider
from omniagentos.voice.service import VOICE_RUN_ID, synthesize_to_artifact
from tests.api.fake_store import FakeStore
from tests.voice.conftest import install_httpx_transport

FAKE_AUDIO = b"ID3fake-mp3-bytes-for-tests"


def test_elevenlabs_status_no_key(clear_voice_env: None) -> None:
    status, detail = ElevenLabsProvider().status()
    assert status == "no_key"
    assert "ELEVENLABS" in detail


def test_elevenlabs_status_ready(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ELEVENLABS_API_KEY", "test-key-not-real")
    status, detail = ElevenLabsProvider().status()
    assert status == "ready"
    assert detail == ""


def test_elevenlabs_happy_path_artifact(
    monkeypatch: pytest.MonkeyPatch,
    store: FakeStore,
    vault_dir: Path,
    voice_cfg: VoiceConfig,
) -> None:
    monkeypatch.setenv("ELEVENLABS_API_KEY", "el-test-key")

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers.get("xi-api-key") == "el-test-key"
        if request.method == "POST" and "/text-to-speech/" in str(request.url):
            assert request.headers.get("accept") == "audio/mpeg" or "audio/mpeg" in (
                request.headers.get("Accept") or ""
            )
            body = json.loads(request.content.decode("utf-8"))
            assert body == {"text": "Hello steward", "model_id": "eleven_multilingual_v2"}
            assert str(request.url).endswith("/text-to-speech/voice_test_1")
            return httpx.Response(200, content=FAKE_AUDIO, headers={"content-type": "audio/mpeg"})
        return httpx.Response(404, text="unexpected")

    transport = install_httpx_transport(monkeypatch, handler)

    result = synthesize_to_artifact(
        "Hello steward",
        provider="elevenlabs",
        store=store,
        cfg=voice_cfg,
        vault_dir=str(vault_dir),
    )

    assert result["ok"] is True
    assert result["provider"] == "elevenlabs"
    assert result["status"] == "ready"
    assert result.get("artifact_id")
    path = Path(result["path"])
    assert path.is_file()
    assert path.read_bytes() == FAKE_AUDIO
    assert path.suffix == ".mp3"
    assert (vault_dir / "artifacts" / "voice").is_dir()

    arts = store.get_artifacts(VOICE_RUN_ID)
    assert len(arts) == 1
    row = arts[0]
    assert row["id"] == result["artifact_id"]
    assert row["type"] == "audio/mpeg"
    assert row["run_id"] == VOICE_RUN_ID
    assert row["sha256"] == digest(FAKE_AUDIO)
    assert row["bytes"] == len(FAKE_AUDIO)
    assert Path(row["uri"]).resolve() == path.resolve()

    assert any(r.method == "POST" for r in transport.requests)


def test_elevenlabs_fetches_first_voice_when_unset(
    monkeypatch: pytest.MonkeyPatch,
    store: FakeStore,
    vault_dir: Path,
) -> None:
    monkeypatch.setenv("ELEVENLABS_API_KEY", "el-test-key")
    cfg = VoiceConfig(default_provider="elevenlabs", elevenlabs_voice_id=None)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and str(request.url).endswith("/voices"):
            assert request.headers.get("xi-api-key") == "el-test-key"
            return httpx.Response(
                200,
                json={"voices": [{"voice_id": "auto_voice_99", "name": "Test"}]},
            )
        if request.method == "POST" and "/text-to-speech/auto_voice_99" in str(request.url):
            return httpx.Response(200, content=FAKE_AUDIO, headers={"content-type": "audio/mpeg"})
        return httpx.Response(500, text="unexpected path")

    install_httpx_transport(monkeypatch, handler)
    result = synthesize_to_artifact(
        "Auto voice",
        store=store,
        cfg=cfg,
        vault_dir=str(vault_dir),
    )
    assert result["ok"] is True


def test_elevenlabs_non_200_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ELEVENLABS_API_KEY", "el-test-key")
    cfg = VoiceConfig(elevenlabs_voice_id="v1")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, text="unauthorized detail message")

    install_httpx_transport(monkeypatch, handler)
    result = ElevenLabsProvider(cfg).synthesize("hi")
    assert result.ok is False
    assert result.status == "error"
    assert "401" in result.detail
    assert "unauthorized" in result.detail
