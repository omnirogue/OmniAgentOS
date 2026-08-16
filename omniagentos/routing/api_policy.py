"""HARD deny-list for direct/paid API routing (the one enforcement point).

Everything that leaves the subscription CLIs and speaks HTTP to a model — the
LiteLLM proxy (``api_base``) and OpenRouter — MUST clear this module first.
Both the planner fallback chain builder (``omniagentos/intake/fallback.py``)
and the api-tier adapters themselves (``omniagentos/adapters/api_base.py``,
``omniagentos/adapters/openrouter.py``) call :func:`assert_api_route_allowed`,
so there is no path to an API provider that skips the check.

The invariant, in order of precedence:

1. **claude / anthropic lineage NEVER routes via an API path.** Not OpenRouter,
   not a proxy, not "just this once". The Claude subscription CLI is the only
   sanctioned execution path for that lineage.
2. **gpt-\\* / codex lineage NEVER routes via a paid API path.** The Codex
   subscription CLI only.
3. API paths are allowed ONLY for grok, gemini, and models explicitly listed in
   ``configs/swarm.yaml``'s ``api_fallback.openrouter_models`` — and a listed
   model must ALSO resolve authoritatively to a known, non-denied lineage.
   Listing is necessary, never sufficient.
4. Anything else — an id whose lineage cannot be established, or whose own
   segments disagree about what it is — is DENIED. Fail closed: an unrecognized
   id is exactly the case where a mistake is expensive.

How rules 1 and 2 are enforced (this is the part that has to be paranoid):
the COMPLETE identifier is scanned first — every ``/`` segment, every
punctuation-delimited token, and the punctuation-stripped whole — after being
unicode-folded (NFKC/NFKD, format+combining characters dropped, homoglyphs
mapped to ASCII, casefolded). A denied marker ANYWHERE wins over every other
signal, so a vendor prefix cannot launder a denied name: ``google/claude-opus-5``
and ``x-ai/openai/gpt-5.6-sol`` are claude/codex lineage and are DENIED, not
gemini/grok. Conflicting non-denied signals (``x-ai/qwen3.7-max``) resolve to
``unknown``, which is also denied.

Rules 1 and 2 are checked BEFORE the allow-list, so adding
``anthropic/claude-opus-5`` to ``openrouter_models`` does not enable it; it
makes the candidate build raise.

A violation raises :class:`ApiRoutePolicyError` at candidate-build time rather
than degrading quietly — the callers deliberately do NOT swallow it.
"""

from __future__ import annotations

import os
import re
import unicodedata
from pathlib import Path
from typing import Any

import yaml

# --- API paths -------------------------------------------------------------

API_PATH_LITELLM = "litellm"
API_PATH_OPENROUTER = "openrouter"
#: A direct provider HTTP endpoint (e.g. modelintel's xAI router call). Same
#: rules — the path name only shapes the message; the lineage decides.
API_PATH_DIRECT = "direct"
API_PATHS = frozenset({API_PATH_LITELLM, API_PATH_OPENROUTER, API_PATH_DIRECT})
SPEND_CAPS_CONFIG_ENV = "OMNIAGENTOS_SPEND_CAPS_CONFIG"
DEFAULT_SPEND_CAPS_PATH = Path(__file__).resolve().parents[2] / "configs" / "spend-caps.yaml"

# --- Lineages --------------------------------------------------------------

LINEAGE_UNKNOWN = "unknown"

#: Lineages that are subscription-CLI ONLY. Never routable over any API path.
DENIED_API_LINEAGES = frozenset({"claude", "anthropic", "codex", "openai", "gpt"})

#: Lineages whose models may be routed over an API path without being listed.
ALLOWED_API_LINEAGES = frozenset({"grok", "xai", "gemini", "google"})

#: Shipped default for ``api_fallback.openrouter_models`` (mirrors the config).
DEFAULT_OPENROUTER_MODELS: tuple[str, ...] = (
    "deepseek/deepseek-v4-pro",
    "qwen/qwen3.7-max",
    # Registered 2026-07-26 for the low-cost test profile. MUST stay in sync with
    # configs/swarm.yaml's api_fallback.openrouter_models: this tuple is the
    # fallback used whenever that section is missing or unloadable, so an id
    # present only in the YAML is silently denied in the degraded path.
    # tests/routing/test_api_policy.py pins exact sequence (order + multiplicity).
    "qwen/qwen3-coder-flash",
    "x-ai/grok-4.5",
    # Registered 2026-07-29: OpenRouter-verified READY. Keep in lockstep with
    # configs/swarm.yaml api_fallback.openrouter_models (and modelintel lineage).
    "z-ai/glm-5.2",
    "moonshotai/kimi-k2.6",
    # Registered 2026-07-31 as the cheapest OpenRouter rung. Keep in lockstep with
    # configs/swarm.yaml api_fallback.openrouter_models (lineage: modelintel key
    # gemini-3.5-flash-lite).
    "google/gemini-3.5-flash-lite",
    # SI-loop revival (operator approval 2026-08-12). A FREE candidate (lineage
    # gemini via the google vendor namespace); rate-limited on the free pool.
    # Keep in lockstep with configs/swarm.yaml api_fallback.openrouter_models.
    "google/gemma-4-31b-it:free",
    # SI-loop revival — "cheapest reliable models" directive (2026-08-12). Ultra-
    # cheap paid draft models. qwen/qwen3.7-flash is the reflection proposer
    # PRIMARY. inclusionai/ling ids carry lineage `ling` via configs/modelintel.yaml.
    # Keep in lockstep with configs/swarm.yaml api_fallback.openrouter_models.
    "qwen/qwen3.7-flash",
    "deepseek/deepseek-v4-flash-0731",
    "inclusionai/ling-3.0-flash",
    "inclusionai/ling-2.6-flash",
)

# Bare planner-rung / marketing names that carry no vendor namespace. These are
# the ids the planner chain itself uses ("fable", "opus", "sol"), so they must
# resolve to their real lineage or the deny-list would be trivially bypassed.
_EXACT_LINEAGE: dict[str, str] = {
    "claude": "claude",
    "fable": "claude",
    "mythos": "claude",
    "opus": "claude",
    "sonnet": "claude",
    "haiku": "claude",
    "codex": "codex",
    "gpt": "codex",
    "sol": "codex",
    "terra": "codex",
    "luna": "codex",
    "grok": "grok",
    "gemini": "gemini",
    "kimi": "kimi",
}

# Vendor namespace of an OpenRouter-style id ("anthropic/claude-opus-5").
_VENDOR_LINEAGE: dict[str, str] = {
    "anthropic": "claude",
    "openai": "codex",
    "x-ai": "grok",
    "xai": "grok",
    "google": "gemini",
    "deepseek": "deepseek",
    "qwen": "qwen",
    "alibaba": "qwen",
    "moonshotai": "kimi",
    "moonshot": "kimi",
    "minimax": "minimax",
    "meta": "muse",
}

# Denied-lineage markers matched as a SUBSTRING of the punctuation-stripped id
# ("google/claude-opus-5" -> "googleclaudeopus5"). Only markers that cannot
# plausibly hide inside an unrelated model name live here, because a match is a
# hard deny: it survives separator tricks ("cl.aude", "c l a u d e") without
# denying "upstage/solar-10.7b" (which is why "sol" is a TOKEN marker below,
# not a substring one).
_DENIED_SQUASH_MARKERS: tuple[tuple[str, str], ...] = (
    ("anthropic", "claude"),
    ("claude", "claude"),
    ("opus", "claude"),
    ("sonnet", "claude"),
    ("haiku", "claude"),
    ("fable", "claude"),
    ("mythos", "claude"),
    ("openai", "codex"),
    ("chatgpt", "codex"),
    ("codex", "codex"),
    ("gpt", "codex"),
)

# Non-denied structural markers, same substring scan. Used both to resolve a
# bare id and to detect CONFLICT ("x-ai/qwen3.7-max" claims two lineages, so it
# resolves to unknown and is denied).
_OTHER_SQUASH_MARKERS: tuple[tuple[str, str], ...] = (
    ("grok", "grok"),
    ("gemini", "gemini"),
    ("kimi", "kimi"),
    ("deepseek", "deepseek"),
    ("qwen", "qwen"),
    ("minimax", "minimax"),
)

# Denied markers that are only safe as a WHOLE token (split on any non-alnum):
# "sol" must catch "gpt-5.6-sol" and a bare "sol" without catching "solar".
_DENIED_TOKEN_MARKERS: dict[str, str] = {
    "sol": "codex",
    "terra": "codex",
    "luna": "codex",
    "davinci": "codex",
}

# OpenAI's "o-series" ids (o1, o3, o4-mini): a whole token of the form o<digit>.
_DENIED_TOKEN_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"^o[1-9][0-9]*$"), "codex"),
)

# Non-ASCII lookalikes folded to the ASCII letter they imitate, so a Cyrillic
# "с" cannot smuggle "claude" past the markers. Belt AND braces: an id that is
# still non-ASCII after folding resolves to LINEAGE_UNKNOWN, which is denied.
_HOMOGLYPHS: dict[str, str] = {
    "а": "a",
    "ɑ": "a",
    "α": "a",
    "ａ": "a",
    "в": "b",
    "ь": "b",
    "β": "b",
    "с": "c",
    "ϲ": "c",
    "ς": "c",
    "ԁ": "d",
    "ⅾ": "d",
    "е": "e",
    "ε": "e",
    "ё": "e",
    "є": "e",
    "ƒ": "f",
    "ɡ": "g",
    "ց": "g",
    "һ": "h",
    "н": "h",
    "ĥ": "h",
    "і": "i",
    "ι": "i",
    "ⅰ": "i",
    "í": "i",
    "ï": "i",
    "ј": "j",
    "к": "k",
    "κ": "k",
    "ӏ": "l",
    "ⅼ": "l",
    "ł": "l",
    "м": "m",
    "ⅿ": "m",
    "п": "n",
    "ո": "n",
    "η": "n",
    "о": "o",
    "ο": "o",
    "օ": "o",
    "ø": "o",
    "ᴏ": "o",
    "р": "p",
    "ρ": "p",
    "ԛ": "q",
    "г": "r",
    "ʀ": "r",
    "ѕ": "s",
    "ș": "s",
    "т": "t",
    "τ": "t",
    "ц": "u",
    "υ": "u",
    "ս": "u",
    "ν": "v",
    "ѵ": "v",
    "ԝ": "w",
    "ω": "w",
    "х": "x",
    "χ": "x",
    "у": "y",
    "γ": "y",
    "ү": "y",
    "з": "z",
    "ᴢ": "z",
}
_HOMOGLYPH_TABLE = str.maketrans(_HOMOGLYPHS)

#: Unicode categories dropped during folding: format/control characters (a
#: zero-width space inside "cla​ude") and combining marks (an accent on an
#: otherwise ordinary letter).
_STRIP_CATEGORIES = frozenset({"Cc", "Cf", "Mn", "Me"})

#: Any run of characters that cannot appear inside a model-name token.
_TOKEN_SPLIT = re.compile(r"[^0-9a-z]+")


class ApiRoutePolicyError(RuntimeError):
    """A model/API-path pair the deny-list forbids. Raised, never returned."""


def _repo_root() -> Path:
    return Path(__file__).resolve().parent.parent.parent


def _normalized(model: object) -> str:
    """Fold an identifier to its comparison form.

    NFKC (fullwidth/compatibility forms) -> homoglyph fold -> NFKD + drop
    format/combining characters -> casefold -> strip. Applied to BOTH sides of
    every comparison (request ids, config entries, registry keys), so the
    deny-list cannot be dodged with presentation tricks. ASCII ids pass through
    unchanged apart from lowercasing.
    """
    text = unicodedata.normalize("NFKC", str(model or ""))
    text = text.translate(_HOMOGLYPH_TABLE)
    text = unicodedata.normalize("NFKD", text)
    text = "".join(ch for ch in text if unicodedata.category(ch) not in _STRIP_CATEGORIES)
    return text.casefold().strip()


def _tokens(folded: str) -> tuple[str, ...]:
    """Alphanumeric tokens of a folded id ("gpt-5.6-sol" -> gpt, 5, 6, sol)."""
    return tuple(part for part in _TOKEN_SPLIT.split(folded) if part)


def _squashed(folded: str) -> str:
    """The folded id with every separator removed ("gpt-5.6-sol" -> gpt56sol)."""
    return "".join(ch for ch in folded if ch.isascii() and ch.isalnum())


# --- configs/swarm.yaml: api_fallback --------------------------------------


def _swarm_config() -> dict[str, Any]:
    """``configs/swarm.yaml`` as a dict; unreadable/broken -> ``{}``.

    Deliberately re-read per call (no cache): the config path is env-driven
    (``OMNIAGENTOS_SWARM_CONFIG``) and a stale cache in a deny-list is a
    security bug, not a performance win. Callers hit this a handful of times
    per planning call.
    """
    try:
        from omniagentos.routing.limit_state import load_swarm_config

        return load_swarm_config()
    except Exception:  # noqa: BLE001 - config trouble must never open the gate
        pass
    try:
        override = os.environ.get("OMNIAGENTOS_SWARM_CONFIG")
        path = Path(override) if override else _repo_root() / "configs" / "swarm.yaml"
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        return raw if isinstance(raw, dict) else {}
    except Exception:  # noqa: BLE001
        return {}


def api_fallback_section() -> dict[str, Any]:
    """The ``api_fallback`` block of configs/swarm.yaml (``{}`` when absent)."""
    section = _swarm_config().get("api_fallback")
    return section if isinstance(section, dict) else {}


def openrouter_models() -> tuple[str, ...]:
    """Configured low-cost OpenRouter rung models (config, else the default).

    Entries are returned VERBATIM (only whitespace/case normalized) — this is a
    candidate list, not an allow decision: every entry still has to clear
    :func:`assert_api_route_allowed`, which is what makes an anthropic/openai id
    dropped in here a build-time failure rather than a bypass.
    """
    raw = api_fallback_section().get("openrouter_models")
    if isinstance(raw, list):
        models = tuple(_normalized(entry) for entry in raw if _normalized(entry))
        if models:
            return models
    return DEFAULT_OPENROUTER_MODELS


def litellm_api_base() -> str:
    """OpenAI-compatible base URL of the local LiteLLM proxy.

    ``OMNIAGENTOS_LITELLM_API_BASE`` > ``api_fallback.litellm_api_base`` >
    the loopback default (mirrors configs/modelintel.yaml router_gemini).
    """
    override = os.environ.get("OMNIAGENTOS_LITELLM_API_BASE", "").strip()
    if override:
        return override.rstrip("/")
    configured = api_fallback_section().get("litellm_api_base")
    if isinstance(configured, str) and configured.strip():
        return configured.strip().rstrip("/")
    return "http://localhost:4000/v1"


# --- Lineage resolution ----------------------------------------------------


def _registry_lineage(name: str) -> str | None:
    """Lineage from configs/modelintel.yaml aliases, or None.

    Guarded end to end: a missing/broken model registry must not decide policy
    (the structural rules below still do), and it must never raise into a
    caller that is mid-candidate-build.
    """
    for key, lineage, aliases in _model_specs():
        if name == key or name in aliases:
            return lineage
    return None


#: (cache key, specs) for the model roster. The key is (path, mtime_ns), so an
#: edit to configs/modelintel.yaml invalidates it immediately — a lineage table
#: is read several times per candidate build and re-parsing the roster each time
#: is pure waste, but a cache that can go stale in a deny-list is not acceptable.
_MODEL_SPEC_CACHE: dict[tuple[str, int], tuple[tuple[str, str, frozenset[str]], ...]] = {}


def _model_specs() -> tuple[tuple[str, str, frozenset[str]], ...]:
    """``(key, lineage, aliases)`` for every configs/modelintel.yaml model."""
    try:
        from omniagentos.modelintel.config import config_path, load_config

        path = config_path()
        cache_key = (str(path), path.stat().st_mtime_ns)
        cached = _MODEL_SPEC_CACHE.get(cache_key)
        if cached is not None:
            return cached
        config = load_config(path)
    except Exception:  # noqa: BLE001 - registry trouble is not a policy decision
        return ()
    specs: list[tuple[str, str, frozenset[str]]] = []
    for spec in config.models:
        specs.append(
            (
                _normalized(spec.key),
                _normalized(spec.lineage) or LINEAGE_UNKNOWN,
                frozenset(_normalized(alias) for alias in spec.aliases),
            )
        )
    resolved = tuple(specs)
    _MODEL_SPEC_CACHE.clear()  # single-entry cache: only the current file matters
    _MODEL_SPEC_CACHE[cache_key] = resolved
    return resolved


def _lineage_signals(folded: str) -> tuple[set[str], set[str]]:
    """Every lineage the COMPLETE identifier hints at, as (denied, other).

    Scans all of it — each ``/`` segment, each punctuation-delimited token, the
    punctuation-stripped whole, and the model registry — because a single
    trustworthy-looking segment must never speak for the rest of the id. This
    runs BEFORE any allow decision: ``google/claude-opus-5`` reports a claude
    signal from its tail and is denied on it.
    """
    denied: set[str] = set()
    other: set[str] = set()

    def _record(lineage: str | None) -> None:
        if not lineage:
            return
        (denied if lineage in DENIED_API_LINEAGES else other).add(lineage)

    _record(_EXACT_LINEAGE.get(folded))
    for segment in folded.split("/"):
        _record(_VENDOR_LINEAGE.get(segment.strip()))
    for token in _tokens(folded):
        _record(_EXACT_LINEAGE.get(token))
        _record(_VENDOR_LINEAGE.get(token))
        _record(_DENIED_TOKEN_MARKERS.get(token))
        for pattern, lineage in _DENIED_TOKEN_PATTERNS:
            if pattern.match(token):
                _record(lineage)
    squashed = _squashed(folded)
    for marker, lineage in (*_DENIED_SQUASH_MARKERS, *_OTHER_SQUASH_MARKERS):
        if marker in squashed:
            _record(lineage)
    _record(_registry_lineage(folded))
    return denied, other


def _denied_verdict(denied: set[str]) -> str:
    """One deterministic lineage name for an id that tripped several markers."""
    if denied & {"claude", "anthropic"}:
        return "claude"
    if denied & {"codex", "openai", "gpt"}:
        return "codex"
    return sorted(denied)[0]


def _authoritative_lineage(folded: str) -> str:
    """The lineage an id can be held to, or ``unknown`` (never a guess).

    Authority order: exact bare planner name -> configs/modelintel.yaml
    key/alias -> the vendor namespace of a ``vendor/model`` id -> structural
    markers on a BARE id. A ``vendor/model`` id whose vendor namespace is not
    a known one is deliberately NOT resolved from its tail: otherwise
    ``madeupvendor/gemini-3.6-flash`` would inherit an allowed lineage from a
    string anyone can type.
    """
    if not folded or not folded.isascii():
        # Anything still non-ASCII after folding cannot be held to a lineage.
        return LINEAGE_UNKNOWN
    exact = _EXACT_LINEAGE.get(folded)
    if exact is not None:
        return exact
    registry = _registry_lineage(folded)
    if registry is not None:
        return registry
    if "/" in folded:
        vendor = _VENDOR_LINEAGE.get(folded.split("/", 1)[0].strip())
        return vendor if vendor is not None else LINEAGE_UNKNOWN
    squashed = _squashed(folded)
    for marker, lineage in (*_DENIED_SQUASH_MARKERS, *_OTHER_SQUASH_MARKERS):
        if marker in squashed:
            return lineage
    for token in _tokens(folded):
        marker_lineage = _DENIED_TOKEN_MARKERS.get(token)
        if marker_lineage is not None:
            return marker_lineage
        for pattern, lineage in _DENIED_TOKEN_PATTERNS:
            if pattern.match(token):
                return lineage
    return LINEAGE_UNKNOWN


def model_lineage(model: str) -> str:
    """The lineage a model id must be held to (``"unknown"`` when undecidable).

    DENIED signals win over everything: if any part of the id says claude or
    codex, that is the answer, whatever the vendor prefix claims. Otherwise the
    id must resolve authoritatively AND agree with its own structural markers;
    a disagreement (or nothing to go on) is ``"unknown"``, which
    :func:`assert_api_route_allowed` denies.
    """
    folded = _normalized(model)
    if not folded:
        return LINEAGE_UNKNOWN
    denied, other = _lineage_signals(folded)
    if denied:
        return _denied_verdict(denied)
    if len(other) > 1:  # the id contradicts itself
        return LINEAGE_UNKNOWN
    authoritative = _authoritative_lineage(folded)
    if authoritative == LINEAGE_UNKNOWN:
        return LINEAGE_UNKNOWN
    if other and authoritative not in other:  # authority vs. the id's own markers
        return LINEAGE_UNKNOWN
    return authoritative


def _listed_for_api(name: str) -> bool:
    """True when ``name`` (or an alias of it) is in ``openrouter_models``.

    Necessary but NOT sufficient — :func:`assert_api_route_allowed` requires a
    known, non-denied lineage as well, so dropping an id into the config cannot
    by itself buy it an API route.
    """
    listed = set(openrouter_models())
    if name in listed:
        return True
    for key, _lineage, aliases in _model_specs():
        if name != key and name not in aliases:
            continue
        if key in listed or listed & set(aliases):
            return True
    return False


def _has_spend_pricing(name: str) -> bool:
    """Whether an enabled provider has a concrete spend-cap pricing row."""

    path = Path(os.environ.get(SPEND_CAPS_CONFIG_ENV) or DEFAULT_SPEND_CAPS_PATH)
    try:
        document = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError):
        return False
    providers = document.get("providers") if isinstance(document, dict) else None
    if not isinstance(providers, dict):
        return False
    normalized_name = _normalized(name)
    for provider in providers.values():
        if not isinstance(provider, dict) or provider.get("enabled") is not True:
            continue
        models = provider.get("models")
        if not isinstance(models, dict):
            continue
        for model_id, pricing in models.items():
            if _normalized(str(model_id)) == normalized_name and isinstance(pricing, dict):
                return True
    return False


# --- The gate --------------------------------------------------------------


def is_api_route_allowed(model: str, *, path: str) -> bool:
    """Non-raising form of :func:`assert_api_route_allowed`."""
    try:
        assert_api_route_allowed(model, path=path)
    except ApiRoutePolicyError:
        return False
    return True


def api_path_for_base(api_base: str) -> str:
    """Which API path a base URL is: the local LiteLLM proxy, or a direct vendor.

    Cosmetic only — the deny-list is identical on every path — but an honest
    path name makes a refusal message point at the right thing.
    """
    base = str(api_base or "").strip().rstrip("/").casefold()
    try:
        proxy = litellm_api_base().strip().rstrip("/").casefold()
    except Exception:  # noqa: BLE001 - config trouble never decides policy
        proxy = ""
    if base and base == proxy:
        return API_PATH_LITELLM
    if "localhost" in base or "127.0.0.1" in base or "[::1]" in base:
        return API_PATH_LITELLM
    return API_PATH_DIRECT


def api_route_denial(model: str, *, api_base: str) -> str | None:
    """None when ``model`` may be called at ``api_base``; else the deny reason.

    The form for HTTP call sites whose contract is to DEGRADE rather than raise
    (modelintel's router and research sweeps, which callers expect to always
    return a verdict/result). The check still happens before any request object
    exists, and an unevaluatable policy is itself a denial — fail closed.
    """
    try:
        assert_api_route_allowed(model, path=api_path_for_base(api_base))
    except ApiRoutePolicyError as exc:
        return str(exc)
    except Exception as exc:  # noqa: BLE001 - an unusable gate still fails closed
        return f"POLICY DENY: api route policy could not be evaluated ({exc})"
    return None


def assert_api_route_allowed(model: str, *, path: str) -> None:
    """Raise :class:`ApiRoutePolicyError` unless ``model`` may use ``path``.

    Called at candidate-build time by the planner chain builder AND again
    inside every api-tier adapter, so neither a hand-built chain nor a direct
    adapter call can reach a denied provider.
    """
    api_path = _normalized(path)
    if api_path not in API_PATHS:
        raise ApiRoutePolicyError(
            f"unknown api path {path!r}; allowed: {', '.join(sorted(API_PATHS))}"
        )

    name = _normalized(model)
    if not name:
        raise ApiRoutePolicyError(f"empty model id is not routable via {api_path}")

    lineage = model_lineage(name)
    if lineage in DENIED_API_LINEAGES:
        cli = "claude" if lineage in {"claude", "anthropic"} else "codex"
        raise ApiRoutePolicyError(
            f"POLICY DENY: {model!r} is {lineage} lineage and must never route via "
            f"the {api_path} API path — the {cli} subscription CLI is its only "
            "sanctioned execution path (a vendor prefix does not change the "
            "lineage of the model it names)"
        )
    if lineage == LINEAGE_UNKNOWN:
        # Fail closed. Either nothing in the id establishes a lineage, or its
        # own segments disagree — both are exactly the case where guessing is
        # expensive. Config listing does NOT rescue this: an operator who wants
        # a new model on the api tier registers it in configs/modelintel.yaml
        # with a lineage first.
        raise ApiRoutePolicyError(
            f"POLICY DENY: {model!r} has no authoritative lineage (unknown or "
            f"self-contradicting), so it cannot use the {api_path} API path — "
            "register it in configs/modelintel.yaml with a lineage before "
            "listing it in configs/swarm.yaml api_fallback.openrouter_models"
        )
    if lineage in ALLOWED_API_LINEAGES:
        return
    # Kimi's direct provider endpoints are an explicitly governed paid route.
    # This does NOT allow the historically broken OpenRouter kimi-k3 route:
    # that path still requires an exact openrouter_models listing.
    if api_path == API_PATH_DIRECT and lineage == "kimi":
        if _has_spend_pricing(name):
            return
        raise ApiRoutePolicyError(
            f"POLICY DENY: direct Kimi model {model!r} has no pricing row in "
            "configs/spend-caps.yaml"
        )
    if _listed_for_api(name):
        return
    raise ApiRoutePolicyError(
        f"POLICY DENY: {model!r} (lineage {lineage}) is not an approved {api_path} "
        "candidate — allowed lineages are "
        f"{', '.join(sorted(ALLOWED_API_LINEAGES))} plus the models listed in "
        "configs/swarm.yaml api_fallback.openrouter_models"
    )


__all__ = [
    "ALLOWED_API_LINEAGES",
    "API_PATHS",
    "API_PATH_DIRECT",
    "API_PATH_LITELLM",
    "API_PATH_OPENROUTER",
    "DEFAULT_OPENROUTER_MODELS",
    "DENIED_API_LINEAGES",
    "LINEAGE_UNKNOWN",
    "ApiRoutePolicyError",
    "api_fallback_section",
    "api_path_for_base",
    "api_route_denial",
    "assert_api_route_allowed",
    "is_api_route_allowed",
    "litellm_api_base",
    "model_lineage",
    "openrouter_models",
]
