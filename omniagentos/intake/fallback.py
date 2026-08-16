"""Planner model fallback chain — provider resilience for planning.

A swarm dispatch must NEVER degrade to a flat solo plan because one model or
one provider had a bad minute. This module is where that promise is kept: it
runs a prompt down a chain of rungs and returns the first successful JSON
parse, or None (heuristic fallback preserved).

The default chain is subscription-CLI first and cross-provider, eight rungs
deep, so an entire lineage can be down without planning noticing:

    fable (claude CLI) → opus (claude CLI) → sol (codex CLI) → grok (grok CLI)
    → gemini-3.6-flash (LiteLLM api_base) → kimi (kimi CLI)
    → gemini-3.5-flash-lite (LiteLLM api_base) → openrouter (low-cost API rung)

Rungs whose CLI binary is missing or whose provider accounts are all disabled
are dropped while the DEFAULT chain is built (an api rung is dropped when its
adapter reports unhealthy — e.g. no ``OPENROUTER_API_KEY``), so the chain
matches the machine it is running on. An EXPLICIT chain — the
``OMNIAGENTOS_PLANNER_FALLBACKS`` env override or the per-call ``chain``
argument — is honored exactly as written: that is an operator's deliberate
choice, not an inferred default.

The three api rungs are hard-gated by ``omniagentos/routing/api_policy.py``:
claude/anthropic lineage and gpt-*/codex lineage can NEVER leave their
subscription CLI. A violation raises ``ApiRoutePolicyError`` at chain-build
time (fail closed) and is deliberately NOT swallowed by the run loop.

Error classification (why a rung falls through vs. gives up):

* ``api_error`` + zero output tokens — the claude CLI's "the API blipped, I
  produced nothing" terminal (observed live 2026-07-26 14:40: two swarm
  dispatches silently degraded to flat solo plans because this landed in the
  "not a limit/unavailable" branch). It is RETRYABLE: one short backoff retry
  on the SAME model, then advance the chain. "Zero tokens" must be SHOWN
  (explicit ``usage.output_tokens == 0`` or ``"output_tokens": 0`` in the
  envelope) — a truncated envelope proves nothing and gets no retry. The retry's
  result goes back through the SAME classifier, so an auth/invalid-model failure
  on the retry is treated as what it is instead of quietly advancing.
* limit / rate-limit / unavailable / auth / timeout — advance the chain.
* "ran fine but gave a bad answer" — the claude/codex rungs still do NOT waste
  a fallback (asking Opus does not fix Fable's malformed JSON). The
  cross-lineage rungs (gemini/grok/kimi/api) fall through on ANY failure:
  their structured-output path is not battle-tested and planning must never
  degrade to heuristics while another rung is still available.

Speed → chain mapping (D10 Mode dial):

  fast   → ``gemini:fable``    gemini-3.6-flash plans, Fable is the fallback
  auto   → ``fable:opus:sol``  Fable plans at xhigh effort (Auto = Fable X High)
  ultra  → ``fable``           Fable alone, effort max

The gemini CLI has no effort/thinking flag (see adapters/gemini.py), so effort
is a documented no-op on that rung — it is deliberately NOT faked into argv.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import time
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from omniagentos.contracts import (
    AgentInput,
    AgentResult,
    BudgetSpec,
    HarnessType,
    ResultStatus,
    new_id,
)
from omniagentos.routing.api_policy import (
    API_PATH_DIRECT,
    API_PATH_LITELLM,
    API_PATH_OPENROUTER,
    ApiRoutePolicyError,
    assert_api_route_allowed,
)

LOG = logging.getLogger(__name__)

# The explicit model id Fast Mode planning runs on. Passed straight to the
# gemini adapter's ``-m`` flag — NO modelintel registry lookup is involved, so
# a stale var/modelintel/registry.json (which may not contain gemini-3.6-flash
# yet) can never break Fast Mode. configs/modelintel.yaml already declares the
# model; the runtime registry catches up at its next refresh.
FAST_PLANNER_MODEL = "gemini-3.6-flash"
LITE_PLANNER_MODEL = "gemini-3.5-flash-lite"

# API-tier adapter registry keys (omniagentos/adapters/registry.py).
_HARNESS_API_LITELLM = "api-litellm"
_HARNESS_API_KIMI_K3 = "api-kimi-k3"
_HARNESS_API_OPENROUTER = "api-openrouter"

#: Hard ceiling on chain length. Eight rungs is already "every provider on the
#: machine plus two api tiers"; anything longer is a config mistake, and a
#: runaway chain burns wall-clock the caller budgeted for planning.
MAX_CHAIN_RUNGS = 8

#: One short backoff before re-trying the SAME model after a zero-token
#: ``api_error`` terminal. Long enough for a provider blip to clear, short
#: enough that a dead provider costs the chain ~3s, not a minute.
API_ERROR_RETRY_BACKOFF_SECONDS = 3.0


@dataclass(frozen=True)
class Rung:
    """One chain rung: which adapter, which model, and how to tell it is usable.

    ``provider``/``cli`` are the availability probes for a subscription-CLI rung
    (accounts.yaml enablement + ``shutil.which``); ``api_path`` marks a rung that
    leaves the CLIs and therefore must clear
    :func:`omniagentos.routing.api_policy.assert_api_route_allowed`.
    """

    name: str
    harness: HarnessType | str
    model: str
    provider: str | None = None
    cli: str | None = None
    api_path: str | None = None


# Known chain rungs, by name. The colon-joined chain strings
# ("gemini:fable", OMNIAGENTOS_PLANNER_FALLBACKS) resolve through this table.
_RUNGS: dict[str, Rung] = {
    "fable": Rung("fable", HarnessType.CLI_CLAUDE, "fable", provider="claude", cli="claude"),
    "opus": Rung("opus", HarnessType.CLI_CLAUDE, "opus", provider="claude", cli="claude"),
    "sol": Rung("sol", HarnessType.CLI_CODEX, "gpt-5.6-sol", provider="codex", cli="codex"),
    "grok": Rung("grok", HarnessType.CLI_GROK, "grok-4.5", provider="grok", cli="grok"),
    # The gemini CLI rung (D10 Fast Mode); distinct from the api rungs below,
    # which reach the same models through the LiteLLM proxy instead.
    "gemini": Rung(
        "gemini", HarnessType.CLI_GEMINI, FAST_PLANNER_MODEL, provider="gemini", cli="gemini"
    ),
    # kimi omits -m when the model is empty: its -m takes a LOCAL alias from the
    # operator's config.toml, not a portable model id (see adapters/kimi.py).
    "kimi": Rung("kimi", HarnessType.CLI_KIMI, "", provider="kimi", cli="kimi"),
    # Default K3 route: Fireworks first; the adapter itself falls back to
    # Moonshot only for network/5xx outage, never for a spend-cap refusal.
    "kimi-k3-api": Rung(
        "kimi-k3-api",
        _HARNESS_API_KIMI_K3,
        "kimi-k3",
        provider="fireworks",
        api_path=API_PATH_DIRECT,
    ),
    "gemini-flash-api": Rung(
        "gemini-flash-api",
        _HARNESS_API_LITELLM,
        FAST_PLANNER_MODEL,
        api_path=API_PATH_LITELLM,
    ),
    "gemini-lite-api": Rung(
        "gemini-lite-api",
        _HARNESS_API_LITELLM,
        LITE_PLANNER_MODEL,
        api_path=API_PATH_LITELLM,
    ),
    # The model is resolved from configs/swarm.yaml api_fallback.openrouter_models
    # at build time (see _openrouter_rung); the adapter walks the rest of the list.
    "openrouter": Rung("openrouter", _HARNESS_API_OPENROUTER, "", api_path=API_PATH_OPENROUTER),
}

#: The DEFAULT chain (availability-filtered at build time, capped at
#: :data:`MAX_CHAIN_RUNGS`). Cross-provider on purpose: two claude rungs, then
#: every other lineage the machine can reach, then the api tier.
DEFAULT_CHAIN: tuple[str, ...] = (
    "fable",
    "opus",
    "sol",
    "grok",
    "gemini-flash-api",
    "kimi-k3-api",
    "gemini-lite-api",
    "openrouter",
)

# Rungs that fall through the chain on ANY failure, not just limit/unavailable:
# cross-lineage rungs whose structured-output path is not battle-tested. The
# claude/codex rungs keep the don't-waste-fallback rule (a bad answer from
# Fable is not made better by asking Opus).
_FALL_THROUGH_ON_BAD_OUTPUT = frozenset(
    {
        "gemini",
        "grok",
        "kimi",
        "kimi-k3-api",
        "gemini-flash-api",
        "gemini-lite-api",
        "openrouter",
    }
)

# The zero-token ``api_error`` terminal, matched against the CLI error text (the
# adapters surface the tail of the raw envelope, so this is a text probe by
# necessity — see the module docstring for the live occurrence).
#
# TWO accepted forms, and both are load-bearing after the reliability-scale
# integration: the RAW envelope (`"terminal_reason": "api_error"`), and the
# COMPOSED string `adapters.common.stderr_tail` now returns on a failing turn
# (`api_error 429: You've hit your monthly spend limit`), which carries the VALUE
# but not the JSON key. Matching only the raw form silently made this whole rung
# dead code for exactly the claude envelope it was written for.
_API_ERROR_MARKER = re.compile(r'"terminal_reason"\s*:\s*"api_error"|\bapi_error\b')
_POSITIVE_OUTPUT_TOKENS = re.compile(r'"output_tokens"\s*:\s*[1-9]')
#: Explicit zero in the envelope. Zero has to be SHOWN, not inferred from a
#: truncated body — see :func:`_is_zero_token_api_error`.
_ZERO_OUTPUT_TOKENS = re.compile(r'"output_tokens"\s*:\s*0(?![0-9])')

# D10 Mode dial speeds.
SPEED_FAST = "fast"
SPEED_AUTO = "auto"
SPEED_ULTRA = "ultra"
SPEEDS = frozenset({SPEED_FAST, SPEED_AUTO, SPEED_ULTRA})

_SPEED_CHAINS: dict[str, str] = {
    SPEED_FAST: "gemini:fable",
    SPEED_AUTO: "fable:opus:sol",
    SPEED_ULTRA: "fable",
}


def _normalize_speed(speed: str | None) -> str:
    value = str(speed or SPEED_AUTO).strip().lower()
    return value if value in SPEEDS else SPEED_AUTO


def speed_planner_chain(speed: str | None) -> str:
    """The planner chain for a Mode-dial speed (D10; unknown/None → auto)."""
    return _SPEED_CHAINS[_normalize_speed(speed)]


def speed_planner_effort(speed: str | None) -> str | None:
    """The per-speed planner effort override.

    auto → ``xhigh`` (D10: Auto = Fable X High); ultra → ``max``; fast → None
    (keep the caller's complexity-derived effort — the gemini CLI has no effort
    flag anyway, and the Fable FALLBACK rung of the fast chain should not burn
    deep-reasoning budget on a fast-lane plan).
    """
    normalized = _normalize_speed(speed)
    if normalized == SPEED_ULTRA:
        return "max"
    if normalized == SPEED_FAST:
        return None
    return "xhigh"


def speed_planner_llm(
    speed: str | None,
    *,
    allowed_providers: Sequence[str] | None = None,
) -> Any:
    """A PlannerLLM-shaped seam ``(prompt, schema, effort) -> dict | None`` that
    plans through the speed's chain at the speed's effort (D10 Mode dial).

    The per-call chain override always wins over the ``OMNIAGENTOS_PLANNER_FALLBACKS``
    env default — picking a speed on the dial is a deliberate operator choice.

    ``allowed_providers`` is the D5 per-run allowlist, forwarded into
    :func:`run_with_fallback` so a pin like ``[codex, grok, gemini]`` cannot
    fall through to a Claude CLI planner rung under enforce mode.
    """
    chain = speed_planner_chain(speed)
    effort_override = speed_planner_effort(speed)

    def _llm(prompt: str, schema: dict[str, Any], effort: str) -> dict[str, Any] | None:
        return run_with_fallback(
            prompt,
            schema,
            effort=effort_override or effort,
            max_turns=3,
            wall_ms=300_000,
            chain=chain,
            allowed_providers=allowed_providers,
        )

    return _llm


# ---------------------------------------------------------------------------
# Availability probes (DEFAULT chain only)
# ---------------------------------------------------------------------------


def _cli_available(cli: str) -> bool:
    """True when ``cli`` resolves to an executable on this machine.

    ``claude`` additionally honors the adapter's own resolution order
    (``OMNIAGENTOS_CLAUDE_CLI``, then ``~/.local/bin/claude``), so a daemon PATH
    that lacks the shim does not make the whole claude tier vanish."""
    if not cli:
        return True
    if cli == "claude":
        try:
            from omniagentos.adapters.claude import _resolve_claude_cli

            resolved = _resolve_claude_cli()
        except Exception:  # noqa: BLE001 - probing must never break planning
            resolved = "claude"
        if os.path.isabs(resolved):
            return os.access(resolved, os.X_OK)
        return shutil.which(resolved) is not None
    return shutil.which(cli) is not None


def _provider_enabled(provider: str) -> bool:
    """True when configs/accounts.yaml has at least one enabled account.

    A provider absent from the config is treated as ENABLED: accounts.yaml is
    an opt-in pool, and an unlisted provider still runs on its ambient CLI
    login. Only an explicit "every account disabled" drops the rung.

    Unparseable / unreadable accounts config is NOT treated as enabled: that is
    a measurement failure, not a favourable enablement result. Fail closed so
    the availability-filtered default chain never presents unknown as good.
    """
    try:
        from omniagentos.routing.config import load_accounts_config

        pool = load_accounts_config().providers.get(provider)
    except Exception:  # noqa: BLE001 - unknown must never render as enabled
        return False
    if pool is None or not pool.accounts:
        return True
    return any(account.enabled for account in pool.accounts)


def _api_rung_available(rung: Rung) -> bool:
    """True when the api rung's adapter is configured (no network probe)."""
    try:
        from omniagentos.adapters.registry import resolve_adapter

        return bool(resolve_adapter(rung.harness).health().healthy)
    except ApiRoutePolicyError:
        raise
    except Exception:  # noqa: BLE001 - an unresolvable adapter is just unavailable
        LOG.debug("api rung %s could not be probed; skipping", rung.name, exc_info=True)
        return False


def _rung_available(rung: Rung) -> bool:
    if rung.api_path is not None:
        return _api_rung_available(rung)
    if rung.cli is not None and not _cli_available(rung.cli):
        LOG.debug("chain rung %s skipped: %s CLI not found", rung.name, rung.cli)
        return False
    if rung.provider is not None and not _provider_enabled(rung.provider):
        LOG.debug("chain rung %s skipped: %s accounts all disabled", rung.name, rung.provider)
        return False
    return True


def _prepared_rung(rung: Rung) -> Rung | None:
    """Policy-check an api rung and bind the openrouter rung to its model list.

    Returns None when the rung has nothing to run (an empty
    ``api_fallback.openrouter_models``). Raises ``ApiRoutePolicyError`` when a
    candidate is denied — EVERY configured openrouter candidate is checked, not
    just the one that gets used, so an anthropic/openai id in the config fails
    the build loudly instead of sitting there waiting to be reached.
    """
    api_path = rung.api_path
    if api_path is None:
        return rung
    if rung.name == "openrouter":
        from omniagentos.routing.api_policy import openrouter_models

        models = [model for model in openrouter_models() if model]
        for model in models:
            assert_api_route_allowed(model, path=api_path)
        if not models:
            return None
        return Rung(rung.name, rung.harness, models[0], api_path=api_path)
    assert_api_route_allowed(rung.model, path=api_path)
    return rung


def _api_path_for_harness(harness: HarnessType | str) -> str | None:
    key = str(harness)
    if key == _HARNESS_API_OPENROUTER:
        return API_PATH_OPENROUTER
    if key == _HARNESS_API_LITELLM:
        return API_PATH_LITELLM
    if key == _HARNESS_API_KIMI_K3:
        return API_PATH_DIRECT
    return None


# ---------------------------------------------------------------------------
# Chain resolution
# ---------------------------------------------------------------------------


def _env_chain_names() -> list[str] | None:
    """``OMNIAGENTOS_PLANNER_FALLBACKS`` as rung names, or None when unset."""
    raw = os.environ.get("OMNIAGENTOS_PLANNER_FALLBACKS", "").strip()
    if not raw:
        return None
    return [part.strip().lower() for part in raw.lower().split(":") if part.strip()]


def default_chain_rungs() -> list[Rung]:
    """The DEFAULT chain for THIS machine: known rungs, available ones only.

    Capped at :data:`MAX_CHAIN_RUNGS`. Raises ``ApiRoutePolicyError`` when a
    configured api candidate is denied by policy (fail closed).
    """
    rungs: list[Rung] = []
    for name in DEFAULT_CHAIN:
        known = _RUNGS.get(name)
        if known is None:  # pragma: no cover - DEFAULT_CHAIN is a literal
            continue
        rung = _prepared_rung(known)
        if rung is None or not _rung_available(rung):
            continue
        rungs.append(rung)
        if len(rungs) >= MAX_CHAIN_RUNGS:
            break
    return rungs


def _resolve_chain(chain: str | Sequence[Any] | None) -> list[Rung]:
    """Normalize a chain spec into :class:`Rung` entries.

    Accepts a colon-joined string of known rung names (``"gemini:fable"``), a
    sequence mixing names and explicit ``(name, harness, model)`` tuples (the
    orchestrator's model-pin path), or None.

    None means "the default": ``OMNIAGENTOS_PLANNER_FALLBACKS`` when set,
    otherwise :func:`default_chain_rungs` (the only path that filters by what
    this machine can actually run). Unknown names are skipped with a warning —
    a typo'd chain must degrade to its valid rungs, never crash planning.
    """
    if chain is None:
        env_names = _env_chain_names()
        if env_names is None:
            return default_chain_rungs()
        raw: Sequence[Any] = env_names
    elif isinstance(chain, str):
        raw = [part for part in chain.split(":") if part.strip()]
    else:
        raw = list(chain)

    entries: list[Rung] = []
    for entry in raw:
        if isinstance(entry, str):
            name = entry.strip().lower()
            known = _RUNGS.get(name)
            if known is None:
                LOG.warning("unknown planner chain entry %r; skipping", entry)
                continue
            candidate = known
        else:
            name, harness_type, model_param = entry
            candidate = Rung(
                str(name).strip().lower(),
                harness_type,
                str(model_param),
                api_path=_api_path_for_harness(harness_type),
            )
        rung = _prepared_rung(candidate)
        if rung is None:
            LOG.warning("chain rung %r has no runnable model; skipping", candidate.name)
            continue
        entries.append(rung)
        if len(entries) >= MAX_CHAIN_RUNGS:
            LOG.warning("planner chain truncated to %d rungs", MAX_CHAIN_RUNGS)
            break
    return entries


# ---------------------------------------------------------------------------
# Error classification
# ---------------------------------------------------------------------------


def _is_zero_token_api_error(result: AgentResult, stderr: str = "") -> bool:
    """Detect the ``api_error`` terminal that produced no tokens at all.

    This is a PROVIDER fault, not a bad answer: the CLI exited with a terminal
    ``api_error`` and zero output tokens, so nothing about the prompt or the
    model was actually exercised. Treated as retryable-with-fallback (one short
    same-model retry, then advance) — the alternative, observed live, is a swarm
    dispatch silently degrading to a flat solo plan.

    Zero output tokens must be POSITIVE evidence — an explicit
    ``usage.output_tokens == 0`` from the adapter, or an explicit
    ``"output_tokens": 0`` in the envelope text. MISSING evidence is NOT zero:
    the adapters surface only the TAIL of the raw envelope, so a body truncated
    above the usage block says nothing about what the model produced, and
    treating that as "produced nothing" hands a retry to failures that never
    earned one. Undecidable therefore falls through to the ordinary classifier.

    Any sign of production — a positive structured count, a positive count in
    the text, or output text on the result — disqualifies the blip class first.
    """
    if result.status not in (ResultStatus.ERROR, ResultStatus.TIMEOUT):
        return False
    combined = f"{result.error or ''} {stderr}"
    if not _API_ERROR_MARKER.search(combined):
        return False
    reported = getattr(result.usage, "output_tokens", None)
    if isinstance(reported, int) and reported > 0:
        return False
    if result.output_text.strip():
        return False
    if _POSITIVE_OUTPUT_TOKENS.search(combined):
        return False
    if isinstance(reported, int) and reported == 0:
        return True
    return bool(_ZERO_OUTPUT_TOKENS.search(combined))


def _is_limit_or_unavailable_error(result: AgentResult, stderr: str = "") -> bool:
    """Detect whether an error is a limit/rate-limit/unavailable class.

    Returns True if the error looks like a quota exceeded, rate limit, or service
    unavailability. Returns False for validation errors, malformed responses, and
    other "ran fine but gave bad output" scenarios.
    """
    if result.status not in (ResultStatus.ERROR, ResultStatus.TIMEOUT):
        return False

    error_text = (result.error or "").lower()
    combined = f"{error_text} {stderr}".lower()

    # Indicators of limit/rate-limit/unavailable that warrant a fallback
    limit_patterns = (
        "rate limit",
        "rate_limit",
        "usage quota",
        "usage limit",
        "quota exceeded",
        "limit reached",
        "limit exceeded",
        "429",  # HTTP 429 Too Many Requests
        "token limit",
        "context limit",
        "unavailable",
        "not available",
        "service unavailable",
        "temporarily unavailable",
        "overloaded",
        "temporarily blocked",
        "429 too many requests",
        "timed out",  # includes timeout which is a retry-able error
    )

    for pattern in limit_patterns:
        if pattern in combined:
            return True

    # Auth/connection errors are also fallback-worthy
    auth_patterns = (
        "401",
        "403",
        "unauthorized",
        "forbidden",
        "authentication failed",
        "connection refused",
        "connection error",
    )
    for pattern in auth_patterns:
        if pattern in combined:
            return True

    return False


# CLI envelope / usage vocabulary a model error message would never use.
# ``output_tokens`` is deliberately excluded: adapters.common.stderr_tail
# appends `` {"output_tokens": N}`` to legitimate composed error messages, so
# counting it would misclassify real model errors as infrastructure failures.
_CLI_TELEMETRY_KEYS: frozenset[str] = frozenset(
    {
        "cache_creation",
        "cache_creation_input_tokens",
        "cache_read_input_tokens",
        "ephemeral_1h_input_tokens",
        "ephemeral_5m_input_tokens",
        "inference_geo",
        "input_tokens",
        "iterations",
        "server_tool_use",
        "service_tier",
        "web_fetch_requests",
        "web_search_requests",
        "duration_api_ms",
        "duration_ms",
        "num_turns",
        "session_id",
        "total_cost_usd",
        "permission_denials",
        "modelUsage",
        "uuid",
    }
)

#: Mid-token JSON key slice at the start of a truncated envelope tail
#: (e.g. ``ut_tokens": 70850`` cut from ``cache_read_input_tokens``).
_MID_TOKEN_JSON_KEY = re.compile(r"^[A-Za-z0-9_]+\"\s*:")
#: JSON key separators (quote + optional whitespace + colon).
_JSON_KEY_SEPARATOR = re.compile(r'"\s*:')


def _count_cli_telemetry_keys(text: str) -> int:
    """Count distinct CLI telemetry keys in JSON-key position.

    A key counts when it appears as ``"<key>"\\s*:``. The first key may also be
    truncated on its left edge (stderr_tail cut mid-key), so at position 0 we
    also accept ``<key>"\\s*:`` without the leading quote.
    """
    found: set[str] = set()
    for key in _CLI_TELEMETRY_KEYS:
        if re.search(rf'"{re.escape(key)}"\s*:', text):
            found.add(key)
            continue
        # Truncated left edge: opening quote (and possibly earlier keys) cut off.
        if re.match(rf'{re.escape(key)}"\s*:', text):
            found.add(key)
    return len(found)


def _is_raw_or_truncated_json_dump(text: str) -> bool:
    """Gate B: raw or truncated JSON dump, not a composed human message."""
    # B1 — begins mid-token (slice through a JSON key).
    if _MID_TOKEN_JSON_KEY.match(text):
        return True
    # B2 / B3 — parse attempt distinguishes truncated dump vs raw dump.
    try:
        decoded = json.loads(text)
    except (json.JSONDecodeError, TypeError, ValueError):
        # B2 — unparseable and dense with key separators → truncated JSON dump.
        return len(_JSON_KEY_SEPARATOR.findall(text)) >= 3
    # B3 — parseable dict/list sitting verbatim in the error slot.
    return isinstance(decoded, (dict, list))


def _is_unparseable_cli_payload(result: AgentResult, stderr: str = "") -> bool:
    """True when ``result.error`` is raw/truncated CLI machine output.

    Unparseable CLI output is an INFRASTRUCTURE failure (the adapter handed us a
    tail-sliced envelope with no usable message key), not a coherent model
    error. It must advance the fallback chain, same as a timeout.

    Both Gate A (machine telemetry keys) and Gate B (raw/truncated JSON shape)
    must hold — being conservative is the whole point so genuine model errors
    still give up on claude/codex rungs.
    """
    if result.status not in (ResultStatus.ERROR, ResultStatus.TIMEOUT):
        return False
    text = (result.error or "").strip() or (stderr or "").strip()
    if not text:
        return False
    # Gate A — at least two distinct telemetry keys in JSON-key position.
    if _count_cli_telemetry_keys(text) < 2:
        return False
    # Gate B — raw or truncated JSON dump, not composed prose.
    return _is_raw_or_truncated_json_dump(text)


#: What the run loop does with one attempt's result. There is exactly ONE
#: classifier (:func:`_classify_attempt`) and both the first attempt and the
#: same-model retry go through it — a retry that fails on auth or an invalid
#: model must not be laundered into "advance the chain" just because it was a
#: retry.
ACTION_SERVED = "served"
ACTION_RETRY_SAME = "retry-same-model"
ACTION_ADVANCE = "advance-chain"
ACTION_GIVE_UP = "give-up"


def _classify_attempt(rung_name: str, result: AgentResult, *, allow_retry: bool) -> str:
    """The single decision function for an attempt's result.

    ``allow_retry`` is False for the retry itself: a second zero-token blip
    advances the chain rather than retrying forever.
    """
    if result.status == ResultStatus.OK and result.output_json is not None:
        return ACTION_SERVED
    # The zero-token api_error is checked BEFORE the generic limit classifier
    # because it is the more specific signal and it earns a same-model retry
    # the others do not.
    if _is_zero_token_api_error(result, result.error or ""):
        return ACTION_RETRY_SAME if allow_retry else ACTION_ADVANCE
    if _is_limit_or_unavailable_error(result, result.error or ""):
        return ACTION_ADVANCE
    # Truncated CLI envelope tails (stderr_tail of a big usage JSON with no
    # usable message key) are infrastructure failures — advance the chain so
    # remaining healthy rungs still get a chance. Checked after limit/auth so
    # those existing owners keep their responsibility.
    if _is_unparseable_cli_payload(result, result.error or ""):
        return ACTION_ADVANCE
    if rung_name in _FALL_THROUGH_ON_BAD_OUTPUT:
        # Cross-lineage rung (gemini/grok/kimi/api tier): ANY failure —
        # including a format break / missing structured output — falls through
        # the chain. Planning must never silently degrade to heuristics while
        # another rung is still available.
        return ACTION_ADVANCE
    return ACTION_GIVE_UP


def run_with_fallback(
    prompt: str,
    schema: dict[str, Any],
    *,
    model: str = "fable",
    effort: str = "medium",
    max_turns: int = 2,
    wall_ms: int = 120_000,
    chain: str | Sequence[Any] | None = None,
    allowed_providers: Sequence[str] | None = None,
) -> dict[str, Any] | None:
    """Run prompt through a model chain and return the first successful JSON parse.

    Returns None if the whole chain fails (heuristic fallback preserved). Logs
    which model actually served the result.

    Args:
        prompt: The prompt text.
        schema: JSON schema for validation.
        model: Initial model to try (default: "fable", currently ignored but kept for API compat).
        effort: Reasoning effort level (applied to the fable rung only — see below).
        max_turns: Maximum CLI turns.
        wall_ms: Wall-time budget in milliseconds.
        chain: Per-call chain override (wins over the default). Either a
            colon-joined string of known rung names (``"gemini:fable"``) or a
            sequence of names / explicit ``(name, harness, model)`` tuples.
            None keeps the default chain (``OMNIAGENTOS_PLANNER_FALLBACKS``
            when set, else :func:`default_chain_rungs`).
        allowed_providers: Optional per-run provider allowlist (D5). Enforced
            under ``OMNIAGENTOS_ALLOWED_PROVIDERS_MODE``; when every rung is
            filtered out, raises :class:`ProviderNotAllowed` rather than
            leaking to a banned Claude CLI (or any other disallowed lineage).

    Returns:
        Parsed JSON dict from the first successful run, or None.

    Raises:
        ApiRoutePolicyError: an api rung was asked to carry a lineage the
            deny-list forbids. Deliberately not swallowed.
        ProviderNotAllowed: the allowlist (when enforced) left zero rungs.
    """
    from omniagentos.adapters.kimi_k3_api import ProviderAuthRefusal
    from omniagentos.adapters.registry import resolve_adapter
    from omniagentos.adapters.spend_guard import SpendGuardRefusal
    from omniagentos.providers.constraints import filter_allowed

    resolved_chain = filter_allowed(
        _resolve_chain(chain),
        allowed_providers,
        stage="planner_fallback",
    )

    def _attempt(rung: Rung) -> AgentResult:
        adapter = resolve_adapter(rung.harness)
        metadata: dict[str, Any] = {}
        if rung.name == "fable":
            # Only Fable uses effort; others get ignored. The gemini CLI in
            # particular has no effort/thinking flag (adapters/gemini.py):
            # effort is a documented no-op on that rung, never faked.
            metadata["effort"] = str(effort)
        input_obj = AgentInput(
            run_id=new_id("run"),
            task_id=new_id("tsk"),
            prompt=prompt,
            model=rung.model,
            output_schema=schema,
            budget=BudgetSpec(max_turns=max_turns, wall_ms_max=wall_ms),
            metadata=metadata,
        )
        return adapter.run(input_obj)

    for rung in resolved_chain:
        model_name = rung.name
        LOG.debug(f"Attempting {model_name} for LLM call (effort={effort})")

        try:
            result: AgentResult = _attempt(rung)
            action = _classify_attempt(model_name, result, allow_retry=True)

            if action == ACTION_RETRY_SAME:
                LOG.warning(
                    f"Model {model_name} returned a zero-token api_error terminal: "
                    f"{result.error}; retrying the same model once after "
                    f"{API_ERROR_RETRY_BACKOFF_SECONDS}s"
                )
                time.sleep(API_ERROR_RETRY_BACKOFF_SECONDS)
                result = _attempt(rung)
                # The retry is re-classified by the SAME rules as the first
                # attempt (minus a second retry). An auth failure or an invalid
                # model on the retry is not "the provider blipped again" and
                # must not silently advance the chain.
                action = _classify_attempt(model_name, result, allow_retry=False)
                if action == ACTION_SERVED:
                    LOG.info(
                        f"Model {model_name} served LLM result on api_error retry (effort={effort})"
                    )
                    return result.output_json
                LOG.warning(
                    f"Model {model_name} failed again after the api_error retry "
                    f"({result.error}); classified {action}"
                )

            if action == ACTION_SERVED:
                LOG.info(f"Model {model_name} served LLM result (effort={effort})")
                return result.output_json

            if action == ACTION_ADVANCE:
                if _is_unparseable_cli_payload(result, result.error or ""):
                    # Distinguish "CLI handed us garbage" from a real model error
                    # so ops can see infrastructure fall-through in the logs.
                    LOG.warning(
                        f"Model {model_name} returned unparseable CLI output "
                        f"(infrastructure failure, status={result.status}, "
                        f"error={result.error}); advancing fallback chain"
                    )
                else:
                    LOG.warning(
                        f"Model {model_name} failed (status={result.status}, "
                        f"error={result.error}); trying fallback"
                    )
                continue  # Try next in chain

            # Non-retryable error: don't waste fallback, return None
            LOG.warning(
                f"Model {model_name} returned non-retryable error: {result.error}; "
                f"giving up (not a limit/unavailable)"
            )
            return None

        except (ApiRoutePolicyError, SpendGuardRefusal, ProviderAuthRefusal):
            # Policy, spend-cap, and quota/suspension/auth refusals are never
            # degraded into "try the next model": fail closed, loudly, at the
            # call site. A ProviderAuthRefusal advancing here would defeat a
            # per-provider spend cap through a different door than the
            # billing-identity split Blocker 1 fixed (2026-08-06 review).
            raise
        except Exception as exc:  # noqa: BLE001
            from omniagentos.providers.constraints import ProviderNotAllowed

            if isinstance(exc, ProviderNotAllowed):
                raise
            # CLI unavailable or other hard error: fall back
            LOG.warning(f"Model {model_name} raised exception: {exc}; trying fallback")
            continue

    # All models exhausted
    LOG.error("All models in fallback chain exhausted; returning None for heuristic fallback")
    return None


__all__ = [
    "ACTION_ADVANCE",
    "ACTION_GIVE_UP",
    "ACTION_RETRY_SAME",
    "ACTION_SERVED",
    "API_ERROR_RETRY_BACKOFF_SECONDS",
    "DEFAULT_CHAIN",
    "FAST_PLANNER_MODEL",
    "LITE_PLANNER_MODEL",
    "MAX_CHAIN_RUNGS",
    "SPEEDS",
    "SPEED_AUTO",
    "SPEED_FAST",
    "SPEED_ULTRA",
    "Rung",
    "default_chain_rungs",
    "run_with_fallback",
    "speed_planner_chain",
    "speed_planner_effort",
    "speed_planner_llm",
]
