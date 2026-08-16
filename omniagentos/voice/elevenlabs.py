"""ElevenLabs text-to-speech provider."""

from __future__ import annotations

from typing import Any

import httpx

from omniagentos.connectors import broker, load_registry
from omniagentos.steward.config import VoiceConfig
from omniagentos.voice.providers import TTSResult

_API_BASE = "https://api.elevenlabs.io/v1"
_TIMEOUT = 30.0
_MODEL_ID = "eleven_multilingual_v2"
_DETAIL_CAP = 500


class ElevenLabsProvider:
    """Fully implemented ElevenLabs TTS backend."""

    name = "elevenlabs"

    def __init__(self, cfg: VoiceConfig | None = None) -> None:
        self._cfg = cfg if cfg is not None else VoiceConfig()

    def _api_key(self) -> str | None:
        try:
            key = broker.resolve_one_for(
                load_registry().capability("elevenlabs.tts"), "ELEVENLABS_API_KEY"
            )
        except broker.BrokerDenied:
            return None
        if not key.strip():
            return None
        return key

    def status(self) -> tuple[str, str]:
        if self._api_key() is None:
            return ("no_key", "ELEVENLABS_API_KEY is not set")
        return ("ready", "")

    def synthesize(self, text: str, *, voice_id: str | None = None) -> TTSResult:
        api_key = self._api_key()
        if api_key is None:
            return TTSResult(
                ok=False,
                audio=None,
                status="no_key",
                detail="ELEVENLABS_API_KEY is not set",
            )

        resolved_voice = voice_id or self._cfg.elevenlabs_voice_id
        headers = {
            "xi-api-key": api_key,
            "Accept": "audio/mpeg",
        }

        try:
            if not resolved_voice:
                resolved_voice = self._first_voice_id(api_key)
                if not resolved_voice:
                    return TTSResult(
                        ok=False,
                        audio=None,
                        status="error",
                        detail="no ElevenLabs voice available",
                    )

            response = httpx.post(
                f"{_API_BASE}/text-to-speech/{resolved_voice}",
                headers=headers,
                json={"text": text, "model_id": _MODEL_ID},
                timeout=_TIMEOUT,
            )
        except httpx.HTTPError as exc:
            return TTSResult(
                ok=False,
                audio=None,
                status="error",
                detail=_truncate(str(exc)),
            )

        if response.status_code != 200:
            return TTSResult(
                ok=False,
                audio=None,
                status="error",
                detail=_truncate(f"HTTP {response.status_code}: {response.text}"),
            )

        return TTSResult(ok=True, audio=response.content, status="ready", detail="")

    def _first_voice_id(self, api_key: str) -> str | None:
        try:
            response = httpx.get(
                f"{_API_BASE}/voices",
                headers={"xi-api-key": api_key},
                timeout=_TIMEOUT,
            )
        except httpx.HTTPError:
            return None
        if response.status_code != 200:
            return None
        payload: Any
        try:
            payload = response.json()
        except ValueError:
            return None
        voices = payload.get("voices") if isinstance(payload, dict) else None
        if not isinstance(voices, list) or not voices:
            return None
        first = voices[0]
        if not isinstance(first, dict):
            return None
        voice_id = first.get("voice_id")
        return str(voice_id) if voice_id else None


def _truncate(detail: str, limit: int = _DETAIL_CAP) -> str:
    if len(detail) <= limit:
        return detail
    return detail[: limit - 3] + "..."


__all__ = ["ElevenLabsProvider"]
