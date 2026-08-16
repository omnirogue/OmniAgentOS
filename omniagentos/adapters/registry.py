"""Lazy AgentAdapter registry.

Harness extras remain optional because importing their modules is deferred until
the associated harness is selected.

The ``api-*`` keys are the API-TIER adapters (OpenAI-compatible HTTP, no
subprocess): they are the planner chain's last-resort rungs and every one of
them is gated by ``omniagentos/routing/api_policy.py``. They are registry keys
rather than ``HarnessType`` members because ``HarnessType`` is a committed
contract enum and nothing outside the planner chain routes to them yet — the
resolver keys on the string value either way.
"""

from __future__ import annotations

import importlib

from omniagentos.contracts import AgentAdapter, HarnessType

# API-tier registry keys (see omniagentos/adapters/api_base.py).
HARNESS_API_LITELLM = "api-litellm"
HARNESS_API_KIMI_K3 = "api-kimi-k3"
HARNESS_API_OPENROUTER = "api-openrouter"

_ADAPTERS: dict[str, tuple[str, str]] = {
    "cli-claude": ("omniagentos.adapters.claude", "ClaudeAdapter"),
    "cli-codex": ("omniagentos.adapters.codex", "CodexAdapter"),
    "cli-grok": ("omniagentos.adapters.grok", "GrokAdapter"),
    "cli-kimi": ("omniagentos.adapters.kimi", "KimiAdapter"),
    "cli-gemini": ("omniagentos.adapters.gemini", "GeminiAdapter"),
    "cli-qwen": ("omniagentos.adapters.qwen", "QwenAdapter"),
    HARNESS_API_LITELLM: ("omniagentos.adapters.api_base", "LiteLLMAdapter"),
    HARNESS_API_KIMI_K3: (
        "omniagentos.adapters.kimi_k3_api",
        "FireworksKimiK3Adapter",
    ),
    HARNESS_API_OPENROUTER: ("omniagentos.adapters.openrouter", "OpenRouterAdapter"),
    "mock": ("omniagentos.mock_adapter", "MockAdapter"),
    "mini-swe": ("omniagentos.harnesses.miniswe.adapter", "MiniSweAdapter"),
    "openhands": ("omniagentos.harnesses.openhands.adapter", "OpenHandsAdapter"),
    "agentdeck": ("omniagentos.harnesses.agentdeck.adapter", "AgentDeckAdapter"),
    "fusion": ("omniagentos.harnesses.fusion", "FusionHarness"),
}
_CACHE: dict[str, AgentAdapter] = {}


def resolve_adapter(harness: HarnessType | str) -> AgentAdapter:
    """Instantiate and cache the adapter selected by *harness*."""

    key = str(harness)
    if key not in _ADAPTERS:
        known = ", ".join(sorted(_ADAPTERS))
        raise KeyError(f"Unknown harness {key!r}; known adapters: {known}")
    if key not in _CACHE:
        module_path, class_name = _ADAPTERS[key]
        module = importlib.import_module(module_path)
        adapter_class = getattr(module, class_name)
        _CACHE[key] = adapter_class()
    return _CACHE[key]
