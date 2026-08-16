"""Content-side secret redaction for tool results and agent output.

The bottom half of this module is the SOURCE-SEAM API. Per-writer scrubbing of
agent output was proven structurally incomplete (``AgentResult`` is built by six
independent adapters, and the session-capture path never builds one at all), so
callers redact at two boundaries instead:

* :func:`scrub_agent_result` -- the runner's adapter-result receipt boundary.
* :func:`scrub_optional_text` / :func:`scrub_run_manifest` -- the raw-transcript
  and imported-manifest capture points (sessions supervisor, provider exec,
  durable wrapper).

Every seam reuses the patterns below; there is deliberately no second scrubber.
"""

from __future__ import annotations

import re
from typing import Any, overload

REDACTED = "[REDACTED]"

_PRIVATE_KEY = re.compile(
    r"-----BEGIN(?: [A-Z0-9]+)* PRIVATE KEY-----.*?-----END(?: [A-Z0-9]+)* PRIVATE KEY-----",
    re.DOTALL,
)
_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\bsk-[A-Za-z0-9_-]{12,}\b"),
    re.compile(r"\bsk_live_[A-Za-z0-9_-]{8,}\b"),  # Stripe live keys (underscore form)
    re.compile(r"\bsk_test_[A-Za-z0-9_-]{8,}\b"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"(?i)(\bBearer\s+)[A-Za-z0-9._~+/=-]{8,}"),
    re.compile(r"(?i)(\b(?:api[_-]?key|x-api-key)\s*[:=]\s*)[^\s,;\"']+"),
    re.compile(
        r"\b[A-Z][A-Z0-9_]*(?:API[_-]?KEY|TOKEN|SECRET|PASSWORD|ACCESS[_-]?KEY|SECRET_KEY)\s*=\s*[^\s,;\"']+"
    ),
    re.compile(r"(?i)\b(?:password|passwd|secret)\s*[:=]\s*[^\s,;\"']+"),
    # Connection URIs with embedded credentials
    re.compile(
        r"(?i)\b(?:postgres|postgresql|mysql|mongodb|redis|amqp)://[^\s\"']+:[^\s\"']+@[^\s\"']+"
    ),
)


def scrub_text(value: str) -> str:
    """Replace common credential material without changing unrelated output."""
    scrubbed = _PRIVATE_KEY.sub(REDACTED, value)
    for pattern in _PATTERNS:
        if pattern.pattern.startswith("(?i)(\\bBearer"):
            scrubbed = pattern.sub(r"\1" + REDACTED, scrubbed)
        elif pattern.pattern.startswith("(?i)(\\b(?:api"):
            scrubbed = pattern.sub(r"\1" + REDACTED, scrubbed)
        else:
            scrubbed = pattern.sub(REDACTED, scrubbed)
    return scrubbed


def scrub_value(value: Any) -> Any:
    """Return a recursively scrubbed copy of JSON-ish tool output."""
    if isinstance(value, str):
        return scrub_text(value)
    if isinstance(value, dict):
        return {scrub_text(str(key)): scrub_value(item) for key, item in value.items()}
    if isinstance(value, list):
        return [scrub_value(item) for item in value]
    if isinstance(value, tuple):
        return tuple(scrub_value(item) for item in value)
    if isinstance(value, set):
        return [scrub_value(item) for item in sorted(value, key=repr)]
    return value


# ---------------------------------------------------------------------------
# Source seams for agent output
# ---------------------------------------------------------------------------

#: Fields on an adapter result that carry agent-authored free text.
AGENT_TEXT_FIELDS: tuple[str, ...] = ("output_text", "error")
#: Fields on an adapter result that carry agent-authored structured output.
AGENT_VALUE_FIELDS: tuple[str, ...] = ("output_json",)


def scrub_optional_text(value: str | None) -> str | None:
    """``scrub_text`` that passes ``None`` through.

    Clearing an error is not a redaction, so ``None`` must survive as ``None``
    rather than becoming an empty string at a persistence boundary.
    """
    if value is None:
        return None
    return scrub_text(value)


def _copy_with[ModelT](model: ModelT, updates: dict[str, Any]) -> ModelT:
    """Return a copy of *model* with *updates* applied, never mutating *model*.

    Pydantic models get ``model_copy``; anything else (a stub in a test, a
    dataclass an adapter hands back) is copied shallowly so the caller's object
    is left alone either way.
    """
    copier = getattr(model, "model_copy", None)
    if callable(copier):
        return copier(update=updates)  # type: ignore[no-any-return]
    import copy as _copy

    clone = _copy.copy(model)
    for field, value in updates.items():
        setattr(clone, field, value)
    return clone


@overload
def scrub_agent_result(result: None) -> None: ...


@overload
def scrub_agent_result[ModelT](result: ModelT) -> ModelT: ...


def scrub_agent_result[ModelT](result: ModelT | None) -> ModelT | None:
    """Redact an adapter result at the runner's receipt boundary.

    This is SEAM 1. Every persistence and forwarding surface downstream of the
    runner -- ``runs.output_text``/``output_json``/``error``,
    ``steps.result_json``, the run manifest, the ledger line, the vault note,
    memory turns and reviewer prompts -- reads these fields, so redacting once
    here is what makes the guarantee provable instead of per-writer.

    Returns *result* itself when there was nothing to redact, so clean output is
    byte-identical and the hot path pays no copy.
    """
    if result is None:
        return None
    updates: dict[str, Any] = {}
    for field in AGENT_TEXT_FIELDS:
        current = getattr(result, field, None)
        if isinstance(current, str) and current:
            scrubbed = scrub_text(current)
            if scrubbed != current:
                updates[field] = scrubbed
    for field in AGENT_VALUE_FIELDS:
        current = getattr(result, field, None)
        if current is None:
            continue
        scrubbed_value = scrub_value(current)
        if scrubbed_value != current:
            updates[field] = scrubbed_value
    if not updates:
        return result
    return _copy_with(result, updates)


@overload
def scrub_run_manifest(manifest: None) -> None: ...


@overload
def scrub_run_manifest[ModelT](manifest: ModelT) -> ModelT: ...


def scrub_run_manifest[ModelT](manifest: ModelT | None) -> ModelT | None:
    """Redact the free-form ``extra`` of a run manifest before it is written.

    The ledger JSONL is append-only and fsynced: a credential that reaches it is
    permanent. ``extra`` is the only manifest field that carries agent-authored
    content (everything else is ids, timings and digests), so it is the one
    field this has to cover.

    Returns *manifest* itself when there was nothing to redact.
    """
    if manifest is None:
        return None
    extra = getattr(manifest, "extra", None)
    if not extra:
        return manifest
    scrubbed = scrub_value(extra)
    if scrubbed == extra:
        return manifest
    return _copy_with(manifest, {"extra": scrubbed})
