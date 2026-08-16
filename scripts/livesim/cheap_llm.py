#!/usr/bin/env python3
"""Cheapest-reliable live-LLM probe for LiveSim tier that needs a real model.

Priority order (first reachable wins), each returning a normalized result with
model, provider, cost_usd, cost_quality, tokens, latency:

  1. LiteLLM proxy on :4000  (local alias router; exact usage.cost when present)
  2. Claude CLI `claude -p`  (haiku; subscription — no per-call cost, cost_quality=unreported)

The kimi CLI is deliberately NOT used as a probe: its default model routes to
Fireworks (metered) after the 2026-08-05 Moonshot billing pause, and the
OAuth-subscription path is reserved for the review step. LiveSim never bills a
metered LLM org for a probe.

If nothing is reachable, `probe()` returns available=False and the caller skips
(a degradation-marked test asserts graceful skip, not a hard failure).
"""

from __future__ import annotations

import json
import subprocess
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any


@dataclass
class LlmResult:
    available: bool
    text: str = ""
    model: str | None = None
    provider: str | None = None
    cost_usd: float = 0.0
    cost_quality: str = "unreported"  # exact | approximate | unreported
    tokens_in: int | None = None
    tokens_out: int | None = None
    latency_ms: float = 0.0
    source: str = ""
    error: str = ""
    raw: dict[str, Any] = field(default_factory=dict)


LITELLM_BASE = "http://127.0.0.1:4000"
# Cheap alias candidates served by the local LiteLLM (see var/memories: the
# proxy serves gemini as `gemini36`/`gemini25-flash-lite`).
LITELLM_MODELS = ("gemini25-flash-lite", "gemini-3.5-flash-lite", "gemini36", "gpt-5.6-luna")


def _litellm_models_up(timeout: float = 3.0) -> list[str]:
    try:
        req = urllib.request.Request(f"{LITELLM_BASE}/v1/models", headers={"Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
            data = json.loads(resp.read().decode("utf-8", "replace"))
        return [m.get("id") for m in data.get("data", []) if m.get("id")]
    except Exception:
        return []


def _try_litellm(prompt: str, max_tokens: int = 32, timeout: float = 20.0) -> LlmResult:
    served = _litellm_models_up()
    if not served:
        return LlmResult(available=False, source="litellm", error="litellm :4000 not reachable")
    # COST SAFETY: only ever call a model on the known-cheap allowlist. Falling back
    # to served[0] could hit a model the local proxy routes to a METERED org (the
    # 2026-08-05 Kimi/Moonshot billing incident is why this suite exists to be
    # cash-safe). If none of the cheap aliases are served, treat LiteLLM as
    # unavailable rather than bill an unknown model.
    model = next((m for m in LITELLM_MODELS if m in served), None)
    if model is None:
        return LlmResult(
            available=False,
            source="litellm",
            error=f"no allowlisted cheap model served (have {served[:5]}); refusing unknown-cost fallback",
        )
    body = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": max_tokens,
        "temperature": 0,
    }
    t0 = time.perf_counter()
    try:
        req = urllib.request.Request(
            f"{LITELLM_BASE}/v1/chat/completions",
            data=json.dumps(body).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
            data = json.loads(resp.read().decode("utf-8", "replace"))
    except Exception as e:  # noqa: BLE001
        return LlmResult(available=False, source="litellm", error=str(e))
    latency = round((time.perf_counter() - t0) * 1000.0, 1)
    text = ""
    try:
        text = data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError):
        pass
    usage = data.get("usage", {}) or {}
    cost = usage.get("cost") or (data.get("_response_cost"))
    return LlmResult(
        available=True,
        text=text,
        model=model,
        provider="litellm",
        cost_usd=float(cost) if isinstance(cost, (int, float)) else 0.0,
        cost_quality="exact" if isinstance(cost, (int, float)) else "unreported",
        tokens_in=usage.get("prompt_tokens"),
        tokens_out=usage.get("completion_tokens"),
        latency_ms=latency,
        source="litellm",
        raw={"usage": usage},
    )


def _try_claude_cli(prompt: str, timeout: float = 60.0) -> LlmResult:
    import shutil

    binary = shutil.which("claude")
    if not binary:
        return LlmResult(available=False, source="claude-cli", error="claude CLI not on PATH")
    t0 = time.perf_counter()
    try:
        out = subprocess.run(
            [binary, "-p", prompt, "--model", "haiku", "--output-format", "text"],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as e:
        return LlmResult(available=False, source="claude-cli", error=str(e))
    latency = round((time.perf_counter() - t0) * 1000.0, 1)
    if out.returncode != 0:
        return LlmResult(
            available=False, source="claude-cli", error=(out.stderr or "nonzero exit")[:200]
        )
    return LlmResult(
        available=True,
        text=(out.stdout or "").strip(),
        model="haiku",
        provider="anthropic-cli",
        cost_usd=0.0,
        cost_quality="unreported",  # subscription CLI reports no per-call cost
        latency_ms=latency,
        source="claude-cli",
    )


def probe(prompt: str, *, prefer: str | None = None, max_tokens: int = 32) -> LlmResult:
    """Return the first reachable cheap LLM's result. prefer='litellm'|'claude-cli'."""
    order = [_try_litellm, _try_claude_cli]
    if prefer == "claude-cli":
        order = [_try_claude_cli, _try_litellm]
    last = LlmResult(available=False, error="no probe attempted")
    for fn in order:
        res = fn(prompt, max_tokens=max_tokens) if fn is _try_litellm else fn(prompt)
        if res.available:
            return res
        last = res
    return last


if __name__ == "__main__":
    import sys

    r = probe(sys.argv[1] if len(sys.argv) > 1 else "Reply with exactly: ok")
    print(json.dumps(r.__dict__, indent=2, default=str))
