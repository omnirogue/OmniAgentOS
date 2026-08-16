"""Provider-identity normalizer for reflection telemetry.

Maps any store's identity convention (filename stem, provider/vendor field,
model id, free-prose verdict text) onto a single canonical label set used by
per-run retros and ledger readers.

This is telemetry normalization, not lineage policy. Unknown input becomes
``"unknown"`` — it never raises. Fail-closed refusal semantics stay in
:mod:`omniagentos.formation.lineage`.
"""

from __future__ import annotations

import re
from pathlib import PurePath

from omniagentos.formation import lineage as _lineage

#: Canonical provider labels emitted by :func:`normalize_provider`.
CANONICAL_PROVIDERS: frozenset[str] = frozenset(
    {
        "claude",
        "codex",
        "gemini",
        "grok",
        "kimi",
        "qwen",
        "deepseek",
        "glm",
        "minimax",
        "muse",
        "unknown",
    }
)

# Vendor / bare-provider fields (openai→codex, anthropic→claude, …).
_VENDOR_ALIASES: dict[str, str] = {
    "claude": "claude",
    "anthropic": "claude",
    "cli-claude": "claude",
    "codex": "codex",
    "openai": "codex",
    "cli-codex": "codex",
    "chatgpt": "codex",
    "gpt": "codex",
    "gemini": "gemini",
    "google": "gemini",
    "gemma": "gemini",
    "cli-gemini": "gemini",
    "grok": "grok",
    "xai": "grok",
    "x-ai": "grok",
    "cli-grok": "grok",
    "kimi": "kimi",
    "moonshot": "kimi",
    "moonshotai": "kimi",
    "qwen": "qwen",
    "alibaba": "qwen",
    "deepseek": "deepseek",
    "glm": "glm",
    "z-ai": "glm",
    "zhipu": "glm",
    "zai": "glm",
    "minimax": "minimax",
    "muse": "muse",
    "meta": "muse",
    "meta-llama": "muse",
}

# formation.lineage Lineage → our telemetry label.
_LINEAGE_TO_PROVIDER: dict[str, str] = {
    "anthropic": "claude",
    "openai": "codex",
    "xai": "grok",
    "google": "gemini",
    "alibaba": "qwen",
    "deepseek": "deepseek",
    "moonshot": "kimi",
}

# Bare planner-rung / marketing names that should resolve without a prefix.
_EXACT_MODEL: dict[str, str] = {
    "fable": "claude",
    "mythos": "claude",
    "opus": "claude",
    "sonnet": "claude",
    "haiku": "claude",
    "sol": "codex",
    "terra": "codex",
    "luna": "codex",
}

# Ordered model-family prefixes (longest-first where it matters).
_MODEL_PREFIXES: tuple[tuple[str, str], ...] = (
    ("gpt-5.6-sol", "codex"),
    ("gpt-5.6-terra", "codex"),
    ("gpt-5.6-luna", "codex"),
    ("gpt-5.6", "codex"),
    ("gpt-", "codex"),
    ("claude-", "claude"),
    ("claude", "claude"),
    ("gemini-", "gemini"),
    ("gemini", "gemini"),
    ("gemma-", "gemini"),
    ("grok-", "grok"),
    ("grok", "grok"),
    ("kimi-", "kimi"),
    ("kimi", "kimi"),
    ("qwen", "qwen"),
    ("glm-", "glm"),
    ("glm", "glm"),
    ("deepseek", "deepseek"),
    ("minimax", "minimax"),
    ("muse-", "muse"),
    ("muse", "muse"),
    ("codex", "codex"),
    ("o1", "codex"),
    ("o3", "codex"),
    ("o4", "codex"),
)

# Free-prose "Reviewer: <model> (Vendor)" — model token, optional parenthesized vendor.
_REVIEWER_RE = re.compile(
    r"(?i)\breviewer\s*:\s*([A-Za-z0-9][A-Za-z0-9._/-]*)"
    r"(?:\s*\(([^)]+)\))?",
)
# Model-ish tokens inside free prose (left-to-right first match wins).
_TOKEN_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/-]*")


def _from_lineage_tables(token: str) -> str | None:
    """Best-effort reuse of formation.lineage tables; never raises."""
    try:
        resolved = _lineage.lineage_for_model(token)
        return _LINEAGE_TO_PROVIDER.get(str(resolved))
    except Exception:
        return None


def _resolve_token(token: str) -> str | None:
    """Map a single cleaned token to a canonical provider, or None."""
    raw = (token or "").strip().lower()
    if not raw:
        return None
    # Strip trailing punctuation that often clings in prose.
    raw = raw.strip(".,;:()[]{}\"'")
    if not raw:
        return None

    # Filename stem already stripped of .log by caller, but tolerate again.
    if raw.endswith(".log"):
        raw = PurePath(raw).stem

    if raw in CANONICAL_PROVIDERS and raw != "unknown":
        return raw

    if raw in _VENDOR_ALIASES:
        return _VENDOR_ALIASES[raw]

    if raw in _EXACT_MODEL:
        return _EXACT_MODEL[raw]

    # vendor/model or vendor:model — prefer the model segment, vendor as fallback.
    for sep in ("/", ":"):
        if sep in raw:
            left, right = raw.split(sep, 1)
            right_hit = _resolve_token(right)
            if right_hit is not None:
                return right_hit
            left_hit = _VENDOR_ALIASES.get(left) or _resolve_token(left)
            if left_hit is not None:
                return left_hit
            break

    for prefix, provider in _MODEL_PREFIXES:
        if raw == prefix or raw.startswith(prefix):
            return provider

    # Token-end family markers: gpt-5.6-sol, claude-opus-5, …
    for marker, provider in (
        ("-sol", "codex"),
        ("-terra", "codex"),
        ("-luna", "codex"),
        ("-opus", "claude"),
        ("-sonnet", "claude"),
        ("-haiku", "claude"),
        ("-fable", "claude"),
    ):
        if raw.endswith(marker) or marker in raw:
            # Require a separator so "solar" does not counterfeit "-sol".
            if marker.startswith("-") and (
                raw.endswith(marker) or f"{marker}-" in raw or f"{marker}." in raw
            ):
                return provider

    lineage_hit = _from_lineage_tables(raw)
    if lineage_hit is not None:
        return lineage_hit

    return None


def _resolve_reviewer_prose(text: str) -> str | None:
    """Resolve free-prose reviewer lines: model first, parenthesized vendor next.

    Returns a canonical provider when a ``Reviewer:`` clause is present, or
    ``"unknown"`` when the clause is present but neither model nor vendor
    resolves. Returns ``None`` when the text is not reviewer prose (caller
    continues with generic extraction).

    Critically: never falls through to an unrelated model name mentioned later
    in the same sentence (e.g. the reviewed subject's model).
    """
    m = _REVIEWER_RE.search(text)
    if not m:
        return None

    model_tok = m.group(1)
    model_hit = _resolve_token(model_tok)
    if model_hit is not None:
        return model_hit

    # Model unresolvable → parenthesized vendor fallback (OpenAI→codex, …).
    vendor_raw = (m.group(2) or "").strip()
    if vendor_raw:
        # Prefer first token (handles "OpenAI" and multi-word vendors).
        vtok = _TOKEN_RE.search(vendor_raw)
        if vtok is not None:
            vendor_hit = _resolve_token(vtok.group(0))
            if vendor_hit is not None:
                return vendor_hit
        vendor_hit = _resolve_token(vendor_raw)
        if vendor_hit is not None:
            return vendor_hit

    # Reviewer clause present but neither model nor vendor resolved.
    return "unknown"


def _extract_candidate(text: str) -> str:
    """Pick the most specific identity token from raw input.

    For free prose (e.g. verdict text), the reviewer's own model wins over any
    other model mentioned later in the sentence. Vendor parenthetical is only a
    fallback when no model token resolves (handled in
    :func:`_resolve_reviewer_prose` / :func:`normalize_provider`).
    """
    stripped = text.strip()
    if not stripped:
        return ""

    # Single-token / path-like inputs: return as-is (after .log stem).
    if " " not in stripped and "\n" not in stripped and "\t" not in stripped:
        if stripped.lower().endswith(".log"):
            return PurePath(stripped).stem
        return stripped

    # Prose: reviewer model token first (vendor handled by _resolve_reviewer_prose).
    m = _REVIEWER_RE.search(stripped)
    if m:
        model_tok = m.group(1)
        if _resolve_token(model_tok) is not None:
            return model_tok
        vendor_raw = (m.group(2) or "").strip()
        if vendor_raw:
            vtok = _TOKEN_RE.search(vendor_raw)
            if vtok is not None and _resolve_token(vtok.group(0)) is not None:
                return vtok.group(0)
            if _resolve_token(vendor_raw) is not None:
                return vendor_raw
        # Unresolvable reviewer identity: return model token so caller yields
        # "unknown" rather than scanning the rest of the sentence.
        return model_tok

    # Left-to-right: first token that resolves as a known provider/model.
    vendor_fallback: str | None = None
    for tok in _TOKEN_RE.findall(stripped):
        hit = _resolve_token(tok)
        if hit is None:
            continue
        # Prefer model-ish tokens over pure vendor words when both appear.
        lower = tok.lower().strip(".,;:()[]{}\"'")
        if lower in _VENDOR_ALIASES and lower not in CANONICAL_PROVIDERS:
            if vendor_fallback is None:
                vendor_fallback = hit
            continue
        return tok

    if vendor_fallback is not None:
        # Return a vendor alias key so resolve can re-map cleanly.
        return vendor_fallback

    # No resolvable token: return first non-trivial token for unknown path.
    tokens = _TOKEN_RE.findall(stripped)
    return tokens[0] if tokens else stripped


def normalize_provider(raw: str | None) -> str:
    """Normalize any provider/model/filename/prose identity to a canonical label.

    Returns one of :data:`CANONICAL_PROVIDERS`. Never raises; unknown →
    ``"unknown"``.
    """
    try:
        if raw is None:
            return "unknown"
        text = str(raw)
        if not text.strip():
            return "unknown"

        # Reviewer prose commits to model → vendor → unknown and never picks up
        # an unrelated model mentioned later in the same line.
        reviewer_hit = _resolve_reviewer_prose(text)
        if reviewer_hit is not None:
            return reviewer_hit

        candidate = _extract_candidate(text)
        # When prose extraction already returned a canonical label (vendor
        # fallback path), accept it.
        if candidate in CANONICAL_PROVIDERS and candidate != "unknown":
            return candidate

        resolved = _resolve_token(candidate)
        if resolved is not None:
            return resolved

        # Last resort: scan whole lowercased string for known stems.
        # Skipped for reviewer prose (handled above) so unrelated models later
        # in a verdict line cannot counterfeit the reviewer's identity.
        lower = text.lower()
        for name in (
            "claude",
            "codex",
            "gemini",
            "grok",
            "kimi",
            "qwen",
            "deepseek",
            "minimax",
            "muse",
            "glm",
        ):
            if re.search(rf"(?<![a-z0-9]){re.escape(name)}(?![a-z0-9])", lower):
                return name

        return "unknown"
    except Exception:
        return "unknown"
