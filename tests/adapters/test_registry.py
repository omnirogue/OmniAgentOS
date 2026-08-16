from __future__ import annotations

import pytest

from omniagentos.adapters.claude import ClaudeAdapter
from omniagentos.adapters.gemini import GeminiAdapter
from omniagentos.adapters.kimi import KimiAdapter
from omniagentos.adapters.qwen import QwenAdapter
from omniagentos.adapters.registry import resolve_adapter
from omniagentos.contracts import HarnessType
from omniagentos.mock_adapter import MockAdapter


def test_registry_resolves_and_caches_known_adapters() -> None:
    assert isinstance(resolve_adapter(HarnessType.CLI_CLAUDE), ClaudeAdapter)
    assert resolve_adapter(HarnessType.CLI_CLAUDE) is resolve_adapter(HarnessType.CLI_CLAUDE)
    assert isinstance(resolve_adapter(HarnessType.MOCK), MockAdapter)
    assert isinstance(resolve_adapter(HarnessType.CLI_KIMI), KimiAdapter)
    assert isinstance(resolve_adapter(HarnessType.CLI_GEMINI), GeminiAdapter)
    assert isinstance(resolve_adapter(HarnessType.CLI_QWEN), QwenAdapter)


def test_registry_unknown_harness_lists_known_adapters() -> None:
    with pytest.raises(KeyError, match="cli-claude.*mini-swe.*mock"):
        resolve_adapter("unknown")  # type: ignore[arg-type]
