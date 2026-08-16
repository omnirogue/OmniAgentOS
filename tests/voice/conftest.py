"""Fixtures for voice package tests — zero real network."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest

from omniagentos.api.deps import get_store
from omniagentos.api.main import app
from omniagentos.steward.config import StewardConfig, VoiceConfig
from tests.api.fake_store import FakeStore


@pytest.fixture
def store() -> FakeStore:
    return FakeStore()


@pytest.fixture
def vault_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    vault = tmp_path / "vault"
    vault.mkdir()
    monkeypatch.setenv("OMNIAGENTOS_VAULT_DIR", str(vault))
    return vault


@pytest.fixture
def voice_cfg() -> VoiceConfig:
    return VoiceConfig(default_provider="elevenlabs", elevenlabs_voice_id="voice_test_1")


@pytest.fixture
def steward_cfg(voice_cfg: VoiceConfig) -> StewardConfig:
    return StewardConfig(voice=voice_cfg)


@pytest.fixture
def clear_voice_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ELEVENLABS_API_KEY", raising=False)
    monkeypatch.delenv("XAI_API_KEY", raising=False)


@pytest.fixture
def asgi_client(store: FakeStore) -> Iterator[httpx.AsyncClient]:
    app.dependency_overrides[get_store] = lambda: store
    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://testserver",
    )
    try:
        yield client
    finally:
        app.dependency_overrides.clear()


class RecordingTransport(httpx.BaseTransport):
    """In-memory httpx transport that records requests for assertions."""

    def __init__(self, handler: Any) -> None:
        self.handler = handler
        self.requests: list[httpx.Request] = []

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self.handler(request)


def install_httpx_transport(monkeypatch: pytest.MonkeyPatch, handler: Any) -> RecordingTransport:
    """Patch httpx Client/request entry points to use an in-process transport."""
    transport = RecordingTransport(handler)

    real_client = httpx.Client

    def client_factory(*args: Any, **kwargs: Any) -> httpx.Client:
        kwargs["transport"] = transport
        kwargs.setdefault("trust_env", False)
        return real_client(*args, **kwargs)

    monkeypatch.setattr(httpx, "Client", client_factory)

    def _request(method: str, url: str, **kwargs: Any) -> httpx.Response:
        with client_factory() as client:
            return client.request(method, url, **kwargs)

    def _get(url: str, **kwargs: Any) -> httpx.Response:
        return _request("GET", url, **kwargs)

    def _post(url: str, **kwargs: Any) -> httpx.Response:
        return _request("POST", url, **kwargs)

    monkeypatch.setattr(httpx, "request", _request)
    monkeypatch.setattr(httpx, "get", _get)
    monkeypatch.setattr(httpx, "post", _post)
    return transport
