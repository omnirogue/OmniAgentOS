"""Text-to-speech providers and artifact materialization."""

from __future__ import annotations

from omniagentos.voice.providers import TTSProvider, TTSResult
from omniagentos.voice.service import synthesize_to_artifact

__all__ = [
    "TTSProvider",
    "TTSResult",
    "synthesize_to_artifact",
]
