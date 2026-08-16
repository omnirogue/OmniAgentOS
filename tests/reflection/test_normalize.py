"""Parametrized coverage for provider identity normalization (S3)."""

from __future__ import annotations

import pytest

from omniagentos.reflection.normalize import CANONICAL_PROVIDERS, normalize_provider

# Every convention listed in the S1/S3 brief step 4, plus unknown fallback.
CASES: list[tuple[str | None, str]] = [
    # Filename stems
    ("claude.log", "claude"),
    ("codex.log", "codex"),
    ("gemini.log", "gemini"),
    ("grok.log", "grok"),
    ("kimi.log", "kimi"),
    ("qwen.log", "qwen"),
    ("deepseek.log", "deepseek"),
    ("glm.log", "glm"),
    ("minimax.log", "minimax"),
    ("muse.log", "muse"),
    # Provider / vendor fields
    ("openai", "codex"),
    ("anthropic", "claude"),
    ("moonshot", "kimi"),
    ("moonshotai", "kimi"),
    ("x-ai", "grok"),
    ("xai", "grok"),
    ("google", "gemini"),
    ("alibaba", "qwen"),
    ("z-ai", "glm"),
    ("deepseek", "deepseek"),
    ("minimax", "minimax"),
    ("meta", "muse"),
    ("claude", "claude"),
    ("codex", "codex"),
    ("gemini", "gemini"),
    ("grok", "grok"),
    ("kimi", "kimi"),
    ("qwen", "qwen"),
    ("glm", "glm"),
    ("muse", "muse"),
    # Model ids
    ("gpt-5.6-sol", "codex"),
    ("gpt-5.6-terra", "codex"),
    ("gpt-5.6-luna", "codex"),
    ("claude-opus-5", "claude"),
    ("claude-sonnet-4", "claude"),
    ("claude-haiku-3", "claude"),
    ("fable", "claude"),
    ("opus", "claude"),
    ("sonnet", "claude"),
    ("haiku", "claude"),
    ("gemini-3.1-pro", "gemini"),
    ("grok-4.5", "grok"),
    ("kimi-k2.6", "kimi"),
    ("qwen3-coder-flash", "qwen"),
    ("glm-5.2", "glm"),
    ("deepseek-r1", "deepseek"),
    ("minimax-m3", "minimax"),
    ("z-ai/glm-5.2", "glm"),
    ("moonshotai/kimi-k2.6", "kimi"),
    ("anthropic/claude-opus-5", "claude"),
    ("openai/gpt-5.6-sol", "codex"),
    ("x-ai/grok-4.5", "grok"),
    # Verdict prose — reviewer's model wins over other models in the sentence
    (
        "Reviewer: gpt-5.6-sol (OpenAI), first-pass cross-lineage reviewer of grok-4.5",
        "codex",
    ),
    (
        "Reviewer: claude-opus-5 (Anthropic) reviewed a grok-4.5 lane",
        "claude",
    ),
    # Unresolvable reviewer model → parenthesized vendor; never the other model
    (
        "Reviewer: mystery-model (OpenAI), first-pass cross-lineage reviewer of grok-4.5",
        "codex",
    ),
    (
        "Reviewer: mystery-model (Anthropic), first-pass cross-lineage reviewer of grok-4.5",
        "claude",
    ),
    # Unknown / empty fallback
    (None, "unknown"),
    ("", "unknown"),
    ("   ", "unknown"),
    ("totally-made-up-vendor-xyz", "unknown"),
    ("solar-10.7b", "unknown"),
]


@pytest.mark.parametrize(("raw", "expected"), CASES)
def test_normalize_provider_conventions(raw: str | None, expected: str) -> None:
    assert expected in CANONICAL_PROVIDERS
    assert normalize_provider(raw) == expected


def test_normalize_provider_never_raises() -> None:
    # Pathological inputs must degrade to unknown, not raise.
    for raw in (None, "", "\x00", object()):  # type: ignore[list-item]
        assert normalize_provider(raw) in CANONICAL_PROVIDERS  # type: ignore[arg-type]
