"""xAI provider probe tests — no hardcoded endpoints, zero network."""

from __future__ import annotations

import base64
import json

import httpx
import pytest

from omniagentos.steward.config import VoiceConfig
from omniagentos.voice.xai import XaiProvider
from tests.voice.conftest import install_httpx_transport

FAKE_AUDIO = b"\xff\xfbaudio-from-xai-probe"


def test_xai_unavailable_by_default(clear_voice_env: None) -> None:
    provider = XaiProvider(VoiceConfig())
    status, detail = provider.status()
    assert status == "unavailable"
    assert "steward.yaml" in detail

    result = provider.synthesize("hello")
    assert result.ok is False
    assert result.status == "unavailable"
    assert "steward.yaml" in result.detail


def test_xai_no_key_when_candidates_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("XAI_API_KEY", raising=False)
    cfg = VoiceConfig(
        xai_candidates=[
            {
                "url": "https://example.test/tts",
                "method": "POST",
                "body_template": {"text": "{text}"},
            }
        ]
    )
    status, detail = XaiProvider(cfg).status()
    assert status == "no_key"
    assert "XAI_API_KEY" in detail


def test_xai_candidate_success_audio_content_type(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("XAI_API_KEY", "xai-test-key")
    cfg = VoiceConfig(
        xai_candidates=[
            {
                "url": "https://probe.example/v1/tts",
                "method": "POST",
                "body_template": {"input": "{text}"},
            }
        ]
    )

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers.get("Authorization") == "Bearer xai-test-key"
        assert str(request.url) == "https://probe.example/v1/tts"
        body = json.loads(request.content.decode("utf-8"))
        assert body == {"input": "Brief me"}
        return httpx.Response(200, content=FAKE_AUDIO, headers={"content-type": "audio/mpeg"})

    install_httpx_transport(monkeypatch, handler)
    result = XaiProvider(cfg).synthesize("Brief me")
    assert result.ok is True
    assert result.audio == FAKE_AUDIO
    assert result.status == "ready"


def test_xai_candidate_success_base64_field(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("XAI_API_KEY", "xai-test-key")
    encoded = base64.b64encode(FAKE_AUDIO).decode("ascii")
    cfg = VoiceConfig(
        xai_candidates=[
            {
                "url": "https://probe.example/json-tts",
                "method": "POST",
                "body_template": {"text": "{text}"},
                "audio_field": "audio",
            }
        ]
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"audio": encoded},
            headers={"content-type": "application/json"},
        )

    install_httpx_transport(monkeypatch, handler)
    result = XaiProvider(cfg).synthesize("json path")
    assert result.ok is True
    assert result.audio == FAKE_AUDIO


def test_xai_all_candidates_fail(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("XAI_API_KEY", "xai-test-key")
    cfg = VoiceConfig(
        xai_candidates=[
            {"url": "https://probe.example/a", "method": "POST", "body_template": {"t": "{text}"}},
            {"url": "https://probe.example/b", "method": "POST", "body_template": {"t": "{text}"}},
        ]
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, text="down", headers={"content-type": "text/plain"})

    install_httpx_transport(monkeypatch, handler)
    result = XaiProvider(cfg).synthesize("fail both")
    assert result.ok is False
    assert result.status == "unavailable"
    assert "probe" in result.detail.lower() or "failed" in result.detail.lower()


def test_xai_no_hardcoded_api_urls() -> None:
    """Guardrail: source must not invent an xAI TTS REST surface."""
    from pathlib import Path

    src = Path(__file__).resolve().parents[2] / "omniagentos" / "voice" / "xai.py"
    text = src.read_text(encoding="utf-8")
    for needle in ("api.x.ai", "api.xai.com", "tts.x.ai"):
        assert needle not in text
