"""HTTP API tests for /api/voice (ASGI, FakeStore, no network)."""

from __future__ import annotations

import asyncio
from pathlib import Path

import httpx
import pytest

import omniagentos.api.routes.voice as voice_routes
from omniagentos.contracts import digest, new_id, utc_now_iso
from omniagentos.voice.service import VOICE_RUN_ID
from tests.api.fake_store import FakeStore
from tests.voice.conftest import install_httpx_transport
from tests.voice.test_elevenlabs import FAKE_AUDIO


def test_get_providers_no_env(asgi_client: httpx.AsyncClient, clear_voice_env: None) -> None:
    response = asyncio.run(asgi_client.get("/api/voice/providers"))
    assert response.status_code == 200
    rows = response.json()
    by_name = {r["provider"]: r for r in rows}
    assert by_name["elevenlabs"]["status"] == "no_key"
    assert by_name["xai"]["status"] == "unavailable"


def test_speak_empty_text_400(asgi_client: httpx.AsyncClient) -> None:
    response = asyncio.run(asgi_client.post("/api/voice/speak", json={"text": "  "}))
    assert response.status_code == 400


def test_speak_503_no_key(
    asgi_client: httpx.AsyncClient,
    clear_voice_env: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("ELEVENLABS_API_KEY", raising=False)
    response = asyncio.run(
        asgi_client.post(
            "/api/voice/speak",
            json={"text": "Hello", "provider": "elevenlabs"},
        )
    )
    assert response.status_code == 503
    body = response.json()
    assert body["status"] == "no_key"
    assert "detail" in body


def test_speak_503_xai_unavailable(
    asgi_client: httpx.AsyncClient,
    clear_voice_env: None,
) -> None:
    response = asyncio.run(
        asgi_client.post(
            "/api/voice/speak",
            json={"text": "Hello", "provider": "xai"},
        )
    )
    assert response.status_code == 503
    body = response.json()
    assert body["status"] == "unavailable"
    assert "detail" in body


def test_speak_text_cap_413(asgi_client: httpx.AsyncClient) -> None:
    response = asyncio.run(
        asgi_client.post(
            "/api/voice/speak",
            json={"text": "y" * 5001},
        )
    )
    assert response.status_code == 413


def test_speak_storage_error_is_coded_500(
    asgi_client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    def raise_storage_error(*args: object, **kwargs: object) -> dict[str, object]:
        del args, kwargs
        raise RuntimeError("storage unavailable")

    monkeypatch.setattr(voice_routes, "synthesize_to_artifact", raise_storage_error)

    response = asyncio.run(asgi_client.post("/api/voice/speak", json={"text": "Hello"}))

    assert response.status_code == 500
    assert response.json()["error"] == {
        "code": "voice_error",
        "message": "voice synthesis failed",
        "detail": "storage unavailable",
    }


def test_speak_happy_path(
    asgi_client: httpx.AsyncClient,
    store: FakeStore,
    vault_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ELEVENLABS_API_KEY", "el-api-key")
    monkeypatch.setenv("OMNIAGENTOS_VAULT_DIR", str(vault_dir))

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and "/voices" in str(request.url):
            return httpx.Response(200, json={"voices": [{"voice_id": "v_auto"}]})
        if request.method == "POST" and "/text-to-speech/" in str(request.url):
            return httpx.Response(200, content=FAKE_AUDIO, headers={"content-type": "audio/mpeg"})
        return httpx.Response(404)

    install_httpx_transport(monkeypatch, handler)

    response = asyncio.run(
        asgi_client.post(
            "/api/voice/speak",
            json={"text": "Dashboard briefing", "provider": "elevenlabs"},
        )
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["ok"] is True
    assert payload["artifact_id"]
    art_id = payload["artifact_id"]

    audio_resp = asyncio.run(asgi_client.get(f"/api/voice/audio/{art_id}"))
    assert audio_resp.status_code == 200
    assert audio_resp.headers["content-type"].startswith("audio/mpeg")
    assert audio_resp.content == FAKE_AUDIO
    assert digest(audio_resp.content) == digest(FAKE_AUDIO)


def test_audio_unknown_404(asgi_client: httpx.AsyncClient) -> None:
    response = asyncio.run(asgi_client.get("/api/voice/audio/art_does_not_exist"))
    assert response.status_code == 404


def test_audio_path_traversal_denied(
    asgi_client: httpx.AsyncClient,
    store: FakeStore,
    vault_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OMNIAGENTOS_VAULT_DIR", str(vault_dir))

    outside = tmp_path / "outside.mp3"
    outside.write_bytes(b"secret-bytes")

    art_id = new_id("art")
    store.add_artifact(
        {
            "id": art_id,
            "run_id": VOICE_RUN_ID,
            "type": "audio/mpeg",
            "uri": str(outside.resolve()),
            "sha256": digest(b"secret-bytes"),
            "bytes": len(b"secret-bytes"),
            "created_at": utc_now_iso(),
        }
    )

    response = asyncio.run(asgi_client.get(f"/api/voice/audio/{art_id}"))
    assert response.status_code in {404, 400}
    assert b"secret-bytes" not in response.content


def test_audio_relative_escape_denied(
    asgi_client: httpx.AsyncClient,
    store: FakeStore,
    vault_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OMNIAGENTOS_VAULT_DIR", str(vault_dir))
    victim = tmp_path / "etc" / "passwd-sim"
    victim.parent.mkdir(parents=True)
    victim.write_bytes(b"root:x:0:0")

    art_id = new_id("art")
    store.add_artifact(
        {
            "id": art_id,
            "run_id": VOICE_RUN_ID,
            "type": "audio/mpeg",
            "uri": str(victim.resolve()),
            "sha256": "",
            "bytes": 0,
            "created_at": utc_now_iso(),
        }
    )
    response = asyncio.run(asgi_client.get(f"/api/voice/audio/{art_id}"))
    assert response.status_code in {404, 400}
    assert b"root:x" not in response.content
