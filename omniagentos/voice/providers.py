"""Shared TTS provider contract."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable


@dataclass(slots=True)
class TTSResult:
    """Outcome of a single synthesis attempt."""

    ok: bool
    audio: bytes | None
    status: str
    detail: str = ""


@runtime_checkable
class TTSProvider(Protocol):
    """One TTS backend (ElevenLabs, xAI probe, …)."""

    name: str

    def status(self) -> tuple[str, str]:
        """Return ``(status, detail)`` without performing synthesis."""
        ...

    def synthesize(self, text: str, *, voice_id: str | None = None) -> TTSResult:
        """Turn ``text`` into MPEG audio bytes when the backend is ready."""
        ...


__all__ = ["TTSProvider", "TTSResult"]
