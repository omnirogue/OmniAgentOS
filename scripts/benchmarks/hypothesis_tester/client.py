"""Minimal stdlib OpenRouter chat client with retries and cost telemetry.

Deliberately dependency-free (urllib only) so the harness runs from any
worktree without a venv. Cost is computed from prices pinned at design time —
telemetry only, never used for routing (working-rules doctrine).
"""

from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

CONNECTIONS_ENV = Path.home() / ".config" / "omni" / "connections.env"
KEY_NAMES = ("OMNIAGENTOS_OPENROUTER_API_KEY", "OPENROUTER_API_KEY")
BASE_URL = "https://openrouter.ai/api/v1"

RETRYABLE_STATUS = {408, 429, 500, 502, 503, 504, 520, 522, 524}
BACKOFF_S = [1, 2, 4, 8, 16]
# OpenRouter/OpenAI-style bearer key shapes, scrubbed from logged error bodies.
_KEY_SHAPE_RE = re.compile(r"\bsk-[A-Za-z0-9_-]{8,}")


@dataclass(frozen=True)
class ModelSpec:
    alias: str
    model_id: str
    price_in: float  # USD per 1M prompt tokens (pinned 2026-08-12)
    price_out: float  # USD per 1M completion tokens


MODELS: dict[str, ModelSpec] = {
    "nemo": ModelSpec("nemo", "mistralai/mistral-nemo", 0.019, 0.03),
    "llama8b": ModelSpec("llama8b", "meta-llama/llama-3.1-8b-instruct", 0.05, 0.08),
    "gptoss": ModelSpec("gptoss", "openai/gpt-oss-20b", 0.03, 0.13),
    # Round-10 screening candidates (prices pinned 2026-08-12): small budget
    # models with stronger exploration, screened for the ability to WIN a
    # quirk-carrying trapcli task at least once. Only a candidate that passes
    # screening is promoted into the pinned set by amendment.
    "qwen30b": ModelSpec("qwen30b", "qwen/qwen3-30b-a3b", 0.09, 0.45),
    "gptoss120": ModelSpec("gptoss120", "openai/gpt-oss-120b", 0.09, 0.45),
    "mistral24b": ModelSpec("mistral24b", "mistralai/mistral-small-3.2-24b-instruct", 0.06, 0.18),
}


@dataclass
class CallResult:
    ok: bool
    text: str = ""
    error: str | None = None
    status: int | None = None
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cost_usd: float = 0.0
    latency_s: float = 0.0
    provider: str | None = None
    attempts: int = 0


def load_api_key() -> str:
    """Resolve the OpenRouter key from connections.env (never logged)."""
    import os

    for name in KEY_NAMES:
        if os.environ.get(name):
            return os.environ[name]
    if CONNECTIONS_ENV.exists():
        found: dict[str, str] = {}
        for raw in CONNECTIONS_ENV.read_text().splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            key = key.removeprefix("export ").strip()
            val = re.split(r"\s+#", val.strip(), maxsplit=1)[0].strip().strip("'\"")
            found[key] = val
        for name in KEY_NAMES:
            if found.get(name):
                return found[name]
    raise RuntimeError(f"no OpenRouter key found (looked for {KEY_NAMES})")


class OpenRouterClient:
    def __init__(self, api_key: str | None = None, timeout: float = 90.0) -> None:
        self.api_key = api_key or load_api_key()
        self.timeout = timeout

    def chat(
        self,
        spec: ModelSpec,
        messages: list[dict[str, str]],
        # 2500, was 700: gpt-oss-20b routinely spent the whole 700 on its
        # reasoning channel and returned EMPTY content — 18/18 of its late
        # gatekeeper MALFORMEDs in pilot-recallv2g were exactly-700-token
        # empty completions. Scoring a reasoning model 0 because it could not
        # SPEAK is an instrument artifact, not a memory effect (HT-011).
        # HT-011b: 2500 still starved gpt-oss on hard episodes (6 late
        # trapcli MALFORMEDs at exactly-2500 empty completions in 20260812-ep32).
        max_tokens: int = 8000,
        temperature: float = 0.0,
        seed: int = 42,
    ) -> CallResult:
        body = json.dumps(
            {
                "model": spec.model_id,
                "messages": messages,
                "temperature": temperature,
                "top_p": 1.0,
                "max_tokens": max_tokens,
                "seed": seed,
            }
        ).encode()
        start = time.monotonic()
        last_err, last_status = "unknown", None
        attempts = 0
        for attempt in range(len(BACKOFF_S)):
            attempts = attempt + 1
            req = urllib.request.Request(
                f"{BASE_URL}/chat/completions",
                data=body,
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                },
            )
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    payload = json.load(resp)
                choice = (payload.get("choices") or [{}])[0]
                text = (choice.get("message") or {}).get("content") or ""
                usage = payload.get("usage") or {}
                pt = int(usage.get("prompt_tokens") or 0)
                ct = int(usage.get("completion_tokens") or 0)
                # Some providers surface transient upstream errors inside a 200.
                if payload.get("error"):
                    last_err = str(payload["error"])[:300]
                    self._sleep_unless_last(attempt)
                    continue
                return CallResult(
                    ok=True,
                    text=text,
                    prompt_tokens=pt,
                    completion_tokens=ct,
                    cost_usd=(pt * spec.price_in + ct * spec.price_out) / 1e6,
                    latency_s=round(time.monotonic() - start, 3),
                    provider=payload.get("provider"),
                    attempts=attempts,
                )
            except urllib.error.HTTPError as e:
                last_status = e.code
                try:
                    last_err = e.read(300).decode(errors="replace")
                except Exception:
                    last_err = str(e)
                if e.code not in RETRYABLE_STATUS:
                    break
                self._sleep_unless_last(attempt)
            except (urllib.error.URLError, TimeoutError, OSError, ValueError) as e:
                last_err = str(e)[:300]
                self._sleep_unless_last(attempt)
        return CallResult(
            ok=False,
            error=self._redact(last_err),
            status=last_status,
            latency_s=round(time.monotonic() - start, 3),
            attempts=attempts,
        )

    @staticmethod
    def _sleep_unless_last(attempt: int) -> None:
        # No trailing sleep after the final request (HT-007).
        if attempt + 1 < len(BACKOFF_S):
            time.sleep(BACKOFF_S[attempt])

    def _redact(self, text: str) -> str:
        """Scrub key material from provider/proxy error bodies before they can
        reach the append-only episode log (HT-006)."""
        if not text:
            return text
        scrubbed = text.replace(self.api_key, "[redacted-key]")
        return _KEY_SHAPE_RE.sub("[redacted-key]", scrubbed)
