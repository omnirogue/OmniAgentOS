"""Streaming clients for the three arena contestants.

All three speak the OpenAI chat-completions dialect, so a single timing path
covers them and the latency numbers stay comparable:

  qwen35   self-hosted llama.cpp `llama-server` on RunPod. The model is GGUF-only,
           so there is no vLLM/SGLang path; it exposes the OpenAI surface but
           answers /v1/models with a "models" key rather than "data".
  grok     api.x.ai/v1
  gemini   generativelanguage.googleapis.com/v1beta/openai

Two first-token numbers are recorded per call. ``ttft_s`` is the first delta of
any kind; ``ttfvt_s`` is the first *visible* content delta. For a reasoning
model the gap between them is thinking time the user genuinely waits through,
so both are reported rather than collapsed into one.
"""

from __future__ import annotations

import json
import os
import re
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

import httpx

CONNECTIONS_ENV = Path.home() / ".config" / "omni" / "connections.env"


def load_connections(path: Path = CONNECTIONS_ENV) -> dict[str, str]:
    """Parse the shell-style credentials file without executing it."""
    out: dict[str, str] = {}
    if not path.exists():
        return out
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.removeprefix("export ").strip()
        val = val.strip()
        if val[:1] in {"'", '"'} and val[:1] == val[-1:] and len(val) > 1:
            val = val[1:-1]
        else:
            # Unquoted values end at an inline comment, dotenv-style. Several
            # entries in connections.env carry trailing `  # note` annotations.
            val = re.split(r"\s+#", val, maxsplit=1)[0].strip()
        if key.isidentifier():
            out[key] = val
    return out


@dataclass
class Provider:
    name: str
    base_url: str
    api_key: str
    model: str
    # Reasoning models reject `temperature`; drop it rather than fail the call.
    supports_temperature: bool = True
    extra_body: dict = field(default_factory=dict)
    # USD per 1M tokens, for cost-per-answer telemetry. Never used to choose a
    # model — only to report what a given capability costs.
    price_in: float | None = None
    price_out: float | None = None
    context_length: int | None = None


@dataclass
class Completion:
    provider: str
    model: str
    ok: bool
    text: str = ""
    error: str | None = None
    ttft_s: float | None = None
    ttfvt_s: float | None = None
    total_s: float = 0.0
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    reasoning_tokens: int | None = None
    output_tps: float | None = None
    finish_reason: str | None = None
    http_status: int | None = None
    content_chunks: int = 0
    # True when the reply arrived in a single chunk, so there is no generation
    # window to measure and output_tps falls back to tokens/total_s.
    tps_end_to_end: bool = False
    cost_usd: float | None = None

    def as_dict(self) -> dict:
        return asdict(self)


def _delta_parts(delta: dict) -> tuple[str, str]:
    """Return (visible_text, hidden_reasoning_text) from a stream delta."""
    visible = delta.get("content") or ""
    if isinstance(visible, list):  # some servers chunk content as parts
        visible = "".join(p.get("text", "") for p in visible if isinstance(p, dict))
    hidden = delta.get("reasoning_content") or delta.get("reasoning") or ""
    if not isinstance(hidden, str):
        hidden = ""
    return visible, hidden


def stream_chat(
    provider: Provider,
    prompt: str,
    *,
    system: str | None = None,
    max_tokens: int = 4096,
    temperature: float = 0.0,
    timeout: float = 600.0,
) -> Completion:
    """One streamed chat completion, fully instrumented."""
    messages: list[dict] = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})

    body: dict = {
        "model": provider.model,
        "messages": messages,
        "max_tokens": max_tokens,
        "stream": True,
        "stream_options": {"include_usage": True},
    }
    if provider.supports_temperature:
        body["temperature"] = temperature
    body.update(provider.extra_body)

    url = provider.base_url.rstrip("/") + "/chat/completions"
    headers = {
        "Authorization": f"Bearer {provider.api_key}",
        "Content-Type": "application/json",
    }

    result = Completion(provider=provider.name, model=provider.model, ok=False)
    visible: list[str] = []
    reasoning_chars = 0
    start = time.perf_counter()

    try:
        with httpx.Client(timeout=httpx.Timeout(timeout, connect=30.0)) as client:
            with client.stream("POST", url, headers=headers, json=body) as resp:
                result.http_status = resp.status_code
                if resp.status_code != 200:
                    detail = resp.read().decode("utf-8", "replace")[:600]
                    result.error = f"HTTP {resp.status_code}: {detail}"
                    result.total_s = time.perf_counter() - start
                    return result

                for line in resp.iter_lines():
                    if not line:
                        continue
                    if line.startswith("data: "):
                        line = line[6:]
                    if line.strip() == "[DONE]":
                        break
                    try:
                        chunk = json.loads(line)
                    except json.JSONDecodeError:
                        continue

                    usage = chunk.get("usage")
                    if usage:
                        result.prompt_tokens = usage.get("prompt_tokens")
                        result.completion_tokens = usage.get("completion_tokens")
                        details = usage.get("completion_tokens_details") or {}
                        result.reasoning_tokens = details.get("reasoning_tokens")
                        # Gemini reports no reasoning_tokens field; its thinking
                        # shows up only as the gap in total_tokens. Recover it so
                        # throughput reflects all the work the model actually did.
                        if result.reasoning_tokens is None:
                            total = usage.get("total_tokens")
                            pt, ct = result.prompt_tokens, result.completion_tokens
                            if total and pt is not None and ct is not None and total > pt + ct:
                                result.reasoning_tokens = total - pt - ct

                    for choice in chunk.get("choices") or []:
                        delta = choice.get("delta") or {}
                        vis, hid = _delta_parts(delta)
                        if (vis or hid) and result.ttft_s is None:
                            result.ttft_s = time.perf_counter() - start
                        if hid:
                            reasoning_chars += len(hid)
                        if vis:
                            if result.ttfvt_s is None:
                                result.ttfvt_s = time.perf_counter() - start
                            visible.append(vis)
                            result.content_chunks += 1
                        if choice.get("finish_reason"):
                            result.finish_reason = choice["finish_reason"]

        result.total_s = time.perf_counter() - start
        result.text = "".join(visible)
        result.ok = bool(result.text.strip())
        if not result.ok and not result.error:
            result.error = "empty completion"

        # Fall back to a character estimate when the server omits usage.
        if result.completion_tokens is None and result.text:
            result.completion_tokens = max(1, round(len(result.text) / 3.7))
        if result.reasoning_tokens is None and reasoning_chars:
            result.reasoning_tokens = round(reasoning_chars / 3.7)

        # Generation-phase throughput: exclude the wait before the first token.
        gen_window = result.total_s - (result.ttft_s or 0.0)
        billed = (result.completion_tokens or 0) + (result.reasoning_tokens or 0)
        if billed and result.total_s > 0.05:
            # Only trust the generation window when the reply genuinely arrived
            # incrementally. Gemini's OpenAI surface buffers and then flushes, so
            # the window can be 3% of total latency and dividing by it reports a
            # fictional four-figure tok/s. Fall back to end-to-end rate and say so.
            truly_streamed = result.content_chunks >= 3 and gen_window >= 0.2 * result.total_s
            if truly_streamed:
                result.output_tps = billed / gen_window
            else:
                result.output_tps = billed / result.total_s
                result.tps_end_to_end = True

        if provider.price_in is not None and provider.price_out is not None:
            # Thinking tokens bill at the output rate, so charge them as output.
            out_tok = (result.completion_tokens or 0) + (result.reasoning_tokens or 0)
            result.cost_usd = (
                (result.prompt_tokens or 0) * provider.price_in + out_tok * provider.price_out
            ) / 1_000_000
    except Exception as exc:  # noqa: BLE001 - a dead pod must not kill the run
        result.total_s = time.perf_counter() - start
        result.error = f"{type(exc).__name__}: {exc}"

    return result


def discover_model(base_url: str, api_key: str, timeout: float = 30.0) -> str | None:
    """Ask an OpenAI-compatible server what it actually serves."""
    try:
        resp = httpx.get(
            base_url.rstrip("/") + "/models",
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=timeout,
        )
        resp.raise_for_status()
        payload = resp.json()
        # OpenAI/vLLM use "data" with "id"; llama.cpp answers with "models"/"name".
        entries = payload.get("data") or payload.get("models") or []
        if not entries:
            return None
        first = entries[0]
        return first.get("id") or first.get("model") or first.get("name")
    except Exception:
        return None


def build_providers(
    conn: dict[str, str] | None = None, *, qwen_base_url: str | None = None
) -> list[Provider]:
    """Assemble the three contestants from the credentials file plus env overrides."""
    conn = conn if conn is not None else load_connections()

    def get(key: str, default: str = "") -> str:
        return os.environ.get(key) or conn.get(key, default)

    qwen_base = (qwen_base_url or get("QWEN35_BASE_URL")).rstrip("/")
    qwen_key = get("QWEN35_API_KEY")
    qwen_model = os.environ.get("QWEN35_MODEL") or discover_model(qwen_base, qwen_key) or "qwen35"

    return [
        Provider(name="qwen35", base_url=qwen_base, api_key=qwen_key, model=qwen_model),
        Provider(
            name="grok-4.5",
            base_url="https://api.x.ai/v1",
            api_key=get("XAI_API_KEY"),
            model=os.environ.get("GROK_MODEL", "grok-4.5"),
        ),
        Provider(
            name="gemini-3.6-flash",
            base_url="https://generativelanguage.googleapis.com/v1beta/openai",
            api_key=get("GEMINI_API_KEY"),
            model=os.environ.get("GEMINI_MODEL", "gemini-3.6-flash"),
        ),
    ]
