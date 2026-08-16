"""xAI / Grok voice provider — probe-driven from steward config only.

No xAI TTS URL is hardcoded. Candidates come exclusively from
``cfg.voice.xai_candidates`` (see ``configs/steward.yaml``).
"""

from __future__ import annotations

import base64
import json
from typing import Any

import httpx

from omniagentos.connectors import broker, load_registry
from omniagentos.steward.config import VoiceConfig
from omniagentos.voice.providers import TTSResult

_TIMEOUT = 30.0
_DETAIL_CAP = 500
_UNAVAILABLE_EMPTY = "no verified xAI TTS endpoint configured; see configs/steward.yaml"


class XaiProvider:
    """Capability-probed xAI TTS backend (config-driven candidates only)."""

    name = "xai"

    def __init__(self, cfg: VoiceConfig | None = None) -> None:
        self._cfg = cfg if cfg is not None else VoiceConfig()

    def _api_key(self) -> str | None:
        try:
            key = broker.resolve_one_for(
                load_registry().capability("xai_voice.tts"), "XAI_API_KEY"
            )
        except broker.BrokerDenied:
            return None
        if not key.strip():
            return None
        return key

    def _candidates(self) -> list[dict[str, Any]]:
        raw = self._cfg.xai_candidates or []
        return [c for c in raw if isinstance(c, dict)]

    def status(self) -> tuple[str, str]:
        if not self._candidates():
            return ("unavailable", _UNAVAILABLE_EMPTY)
        if self._api_key() is None:
            return ("no_key", "XAI_API_KEY is not set")
        return ("ready", "")

    def synthesize(self, text: str, *, voice_id: str | None = None) -> TTSResult:
        del voice_id  # xAI candidate templates do not use voice_id yet
        candidates = self._candidates()
        if not candidates:
            return TTSResult(
                ok=False,
                audio=None,
                status="unavailable",
                detail=_UNAVAILABLE_EMPTY,
            )

        api_key = self._api_key()
        if api_key is None:
            return TTSResult(
                ok=False,
                audio=None,
                status="no_key",
                detail="XAI_API_KEY is not set",
            )

        probe_notes: list[str] = []
        for index, candidate in enumerate(candidates):
            url = candidate.get("url")
            method = str(candidate.get("method") or "POST").upper()
            if not url or not isinstance(url, str):
                probe_notes.append(f"[{index}] missing url")
                continue

            body = _render_body(candidate.get("body_template"), text)
            headers = {
                "Authorization": f"Bearer {api_key}",
                "Accept": "audio/mpeg, application/json",
            }
            try:
                request_kwargs: dict[str, Any] = {
                    "headers": headers,
                    "timeout": _TIMEOUT,
                }
                if body is not None and method not in {"GET", "HEAD"}:
                    if isinstance(body, (dict, list)):
                        request_kwargs["json"] = body
                    elif isinstance(body, str):
                        try:
                            request_kwargs["json"] = json.loads(body)
                        except json.JSONDecodeError:
                            request_kwargs["content"] = body.encode("utf-8")
                            headers.setdefault("Content-Type", "text/plain")
                    else:
                        request_kwargs["json"] = body

                response = httpx.request(method, url, **request_kwargs)
            except httpx.HTTPError as exc:
                probe_notes.append(f"[{index}] transport: {_truncate(str(exc), 80)}")
                continue

            audio = _extract_audio(response, candidate.get("audio_field"))
            if audio:
                return TTSResult(ok=True, audio=audio, status="ready", detail="")

            content_type = response.headers.get("content-type", "")
            probe_notes.append(
                f"[{index}] HTTP {response.status_code} ct={content_type!r} "
                f"bytes={len(response.content)}"
            )

        summary = "; ".join(probe_notes) if probe_notes else "all candidates failed"
        return TTSResult(
            ok=False,
            audio=None,
            status="unavailable",
            detail=_truncate(f"xAI TTS probe failed: {summary}"),
        )


def _render_body(template: Any, text: str) -> Any:
    if template is None:
        return {"text": text}
    if isinstance(template, str):
        return template.replace("{text}", text)
    if isinstance(template, dict):
        return {key: _render_body(value, text) for key, value in template.items()}
    if isinstance(template, list):
        return [_render_body(item, text) for item in template]
    return template


def _extract_audio(response: httpx.Response, audio_field: Any) -> bytes | None:
    content_type = (response.headers.get("content-type") or "").lower()
    if response.status_code == 200 and content_type.startswith("audio/"):
        return response.content if response.content else None

    if audio_field and isinstance(audio_field, str) and response.status_code == 200:
        try:
            payload: Any = response.json()
        except ValueError:
            return None
        if not isinstance(payload, dict):
            return None
        encoded = payload.get(audio_field)
        if not isinstance(encoded, str) or not encoded:
            return None
        try:
            return base64.b64decode(encoded, validate=False)
        except Exception:
            return None
    return None


def _truncate(detail: str, limit: int = _DETAIL_CAP) -> str:
    if len(detail) <= limit:
        return detail
    return detail[: limit - 3] + "..."


__all__ = ["XaiProvider"]
