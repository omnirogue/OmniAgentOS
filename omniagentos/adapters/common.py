"""Shared plumbing for the subscription CLI adapters.

The provider modules deliberately contain only their command construction and
response parsing.  Keeping process, timeout, logging, and structured-output
handling here makes their safety behaviour identical.

Env scrubbing (AC-policy money boundary): _scrubbed_env() is an ALLOWLIST, not
a denylist. A denylist provably leaks -- the prior version kept
ACMEUNI_STRIPE_PRIMARY_SECRET_KEY / SLASH_API_KEY / DATABASE_URL and every other
payment/bank/infra credential, so an agent subprocess could `curl $STRIPE_KEY`
straight past the broker. The allowlist passes ONLY process-hygiene / locale vars,
the provider CONFIG-DIR pointers, and the macOS session handles the Keychain needs;
EVERYTHING else -- payment/bank/infra keys, model-provider API keys, admin DSNs,
OPERATOR_TOKEN / any self-approve token, unknown secrets -- is stripped. The
credential broker (in the parent process, never a subprocess) is the SOLE path to
money.

Two additions were forced by live failures and are load-bearing:

* **Provider config-dir pointers** (CLAUDE_CONFIG_DIR, CODEX_HOME, GROK_HOME,
  GEMINI_CLI_HOME, KIMI_CODE_HOME, QWEN_HOME) are PATHS, never credentials. Dropping them
  does not make a spawn safer -- it makes the child CLI silently fall back to
  ``~/.claude`` (or the provider's default dir), i.e. a DIFFERENT account than
  the parent is authenticated as. Live 2026-07-26: the planner's `--model fable`
  turn fell back to ~/.claude, hit that account's monthly spend limit, and the
  CLI returned `terminal_reason: api_error` with zero tokens -- which reads as a
  mystery API blip, not "you routed me to the wrong account". Identical argv +
  identical scrubbed env + CLAUDE_CONFIG_DIR restored => clean success.
* **macOS session handles** (SECURITYSESSIONID, __CF_USER_TEXT_ENCODING,
  XPC_*, LaunchInstanceID, __CFBundleIdentifier, TERM_SESSION_ID) are opaque
  per-login-session identifiers, not secrets; securityd/Keychain lookups can
  need them depending on how the parent was launched (an interactive shell has
  them, a launchd job may not).

Model-provider API keys are deliberately NOT passed (they used to be, via the
broker's "injectable" group). A subscription CLI handed ANTHROPIC_API_KEY /
OPENAI_API_KEY silently switches to metered per-token API billing instead of the
subscription -- a cost-policy break as well as a wider credential blast radius.
Anything that genuinely needs a model API key gets it from the broker, explicitly.
"""

from __future__ import annotations

import functools
import json
import math
import os
import re
import signal
import subprocess
import time
from abc import ABC, abstractmethod
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, localcontext
from pathlib import Path
from typing import Any

from omniagentos.contracts import (
    AgentInput,
    AgentResult,
    AgentUsage,
    CostObservation,
    CostQuality,
    HealthStatus,
    ProviderCallStage,
    ProviderRequestState,
    ReasoningEffort,
    ResultStatus,
    _parse_cost_usd_decimal,
    estimate_tokens,
    new_id,
)

DEFAULT_TIMEOUT_MS = 300_000
STDERR_TAIL_LENGTH = 500
POST_KILL_REAP_TIMEOUT_SECONDS = 2.0
# Backward-compatible name used by older adapter cleanup callers/tests.
_ADAPTER_CLEANUP_TIMEOUT_S = POST_KILL_REAP_TIMEOUT_SECONDS

# Public sentinel for payload readers that need to distinguish an absent key
# from a present JSON null. Both normalize to UNKNOWN; the reason retains which
# provider report was observed.
PROVIDER_COST_MISSING = object()


@dataclass(frozen=True)
class NormalizedCost:
    """Provider cost normalized without manufacturing a zero-dollar fact."""

    quality: CostQuality
    cost_usd_decimal: str | None = None
    cost_usd_nanos: int | None = None
    cost_usd: float | None = None
    reason: str | None = None


def _unknown_cost(reason: str) -> NormalizedCost:
    return NormalizedCost(quality=CostQuality.UNKNOWN, reason=reason)


def normalize_provider_cost(raw: Any = PROVIDER_COST_MISSING) -> NormalizedCost:
    """Normalize one provider-reported USD value at nano-USD precision.

    Plain decimal strings are preserved byte-for-byte. Numeric values are
    converted through ``Decimal``'s non-scientific representation and rejected
    when they cannot be represented exactly in nano-USD. Missing, null,
    negative, non-finite, boolean, scientific-text, and otherwise invalid values
    remain UNKNOWN with no numeric fields. In particular, this function never
    uses truthiness to conflate missing cost with explicit zero.
    """

    if raw is PROVIDER_COST_MISSING:
        return _unknown_cost("missing provider cost")
    if raw is None:
        return _unknown_cost("provider cost is null")
    if isinstance(raw, bool):
        return _unknown_cost("boolean is not a provider cost")

    text: str
    if isinstance(raw, str):
        text = raw
    elif isinstance(raw, int):
        if raw < 0:
            return _unknown_cost("provider cost is negative")
        text = str(raw)
    elif isinstance(raw, float):
        if not math.isfinite(raw):
            return _unknown_cost("provider cost is not finite")
        if raw < 0:
            return _unknown_cost("provider cost is negative")
        text = format(Decimal(str(raw)), "f")
    elif isinstance(raw, Decimal):
        if not raw.is_finite():
            return _unknown_cost("provider cost is not finite")
        if raw < 0:
            return _unknown_cost("provider cost is negative")
        text = format(raw, "f")
    else:
        return _unknown_cost(f"unsupported provider cost type: {type(raw).__name__}")

    try:
        exact_text, nanos = _parse_cost_usd_decimal(text)
    except (TypeError, ValueError, InvalidOperation) as exc:
        return _unknown_cost(f"invalid provider cost: {exc}")

    # Decimal avoids a second string parser and keeps conversion independent of
    # the ambient decimal context even for large, valid provider reports.
    try:
        with localcontext() as context:
            context.prec = max(28, len(exact_text) + 9)
            as_float = float(Decimal(exact_text))
    except (InvalidOperation, OverflowError, ValueError) as exc:
        return _unknown_cost(f"invalid provider cost: {exc}")
    if not math.isfinite(as_float):
        return _unknown_cost("provider cost cannot be represented as a finite float")
    return NormalizedCost(
        quality=CostQuality.EXACT,
        cost_usd_decimal=exact_text,
        cost_usd_nanos=nanos,
        cost_usd=as_float,
    )


def _optional_metadata_text(metadata: dict[str, Any], key: str) -> str | None:
    value = metadata.get(key)
    return str(value) if value not in (None, "") else None


def build_cost_observation(
    *,
    input: AgentInput,
    normalized: NormalizedCost,
    provider: str,
    requested_model: str,
    effective_model: str | None = None,
    transport: str,
    adapter_key: str,
    billing_provider: str | None = None,
    model_lineage: str | None = None,
    attempt_index: int = 0,
    request_state: ProviderRequestState = "sent",
    provider_outcome: str | None = None,
    provider_request_id: str | None = None,
    input_tokens: int | None = None,
    output_tokens: int | None = None,
    cost_source: str = "provider-report",
) -> CostObservation:
    """Construct the frozen cost contract from adapter/call identity.

    ``request_id`` and ``execution_id`` are accepted from caller metadata and
    allocated when absent. The returned model is pure; adapters deliberately
    serialize and append it to ``input.metadata["cost_observations"]``, a
    caller-owned attempt stream that survives fallback between candidates.
    """

    metadata = input.metadata
    raw_stage = metadata.get("provider_call_stage", metadata.get("stage"))
    if isinstance(raw_stage, ProviderCallStage):
        stage = raw_stage
    else:
        try:
            stage = ProviderCallStage(str(raw_stage))
        except (TypeError, ValueError):
            stage = ProviderCallStage.WORKER

    effective = effective_model or requested_model
    source = cost_source
    if normalized.quality == CostQuality.UNKNOWN and normalized.reason:
        source = f"{cost_source}:{normalized.reason}"
    total_tokens = (
        input_tokens + output_tokens
        if isinstance(input_tokens, int)
        and not isinstance(input_tokens, bool)
        and isinstance(output_tokens, int)
        and not isinstance(output_tokens, bool)
        else None
    )
    return CostObservation(
        call_id=str(metadata.get("call_id") or new_id("call")),
        request_id=str(metadata.get("request_id") or new_id("req")),
        execution_id=str(metadata.get("execution_id") or new_id("exe")),
        run_id=input.run_id or None,
        campaign_id=_optional_metadata_text(metadata, "campaign_id"),
        reservation_id=_optional_metadata_text(metadata, "reservation_id"),
        task_id=input.task_id or None,
        attempt_id=_optional_metadata_text(metadata, "attempt_id"),
        session_id=_optional_metadata_text(metadata, "session_id"),
        work_id=_optional_metadata_text(metadata, "work_id"),
        root_trace_id=_optional_metadata_text(metadata, "root_trace_id"),
        stage=stage,
        attempt_index=attempt_index,
        provider=provider,
        transport=transport,
        requested_model=requested_model,
        effective_model=effective,
        model_lineage=model_lineage or effective,
        billing_provider=billing_provider or provider,
        adapter_key=adapter_key,
        provider_request_id=provider_request_id,
        request_state=request_state,
        provider_outcome=provider_outcome,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        total_tokens=total_tokens,
        cost_usd_decimal=normalized.cost_usd_decimal,
        cost_usd_nanos=normalized.cost_usd_nanos,
        cost_quality=normalized.quality,
        cost_source=source,
    )


def observation_from_adapter_result(
    *,
    input: AgentInput,
    result: AgentResult,
    provider: str,
    requested_model: str,
    effective_model: str | None = None,
    transport: str,
    adapter_key: str,
    attempt_index: int = 0,
) -> CostObservation:
    """Build an observation for adapters that only expose an ``AgentResult``."""

    usage = result.usage
    return build_cost_observation(
        input=input,
        normalized=normalize_provider_cost(usage.cost_usd),
        provider=provider,
        requested_model=requested_model,
        effective_model=effective_model,
        transport=transport,
        adapter_key=adapter_key,
        attempt_index=attempt_index,
        provider_outcome=result.status.value,
        input_tokens=usage.input_tokens,
        output_tokens=usage.output_tokens,
        cost_source=usage.source,
    )


def append_cost_observation(input: AgentInput, observation: CostObservation) -> None:
    """Append a JSON-safe observation to the caller-owned metadata stream."""

    stream = input.metadata.get("cost_observations")
    if not isinstance(stream, list):
        stream = []
        input.metadata["cost_observations"] = stream
    stream.append(observation.model_dump(mode="json"))


def _default_agent_workspace() -> str:
    """A safe var-anchored fallback working dir — NEVER the repo root / cwd.

    Anchored to ``OMNIAGENTOS_VAR_DIR`` when set (the same knob the log root and
    intake/provision workspaces use), else the repo's ``var/`` subtree. Returns a
    dedicated ``agent-workspace`` subdir so an adapter-direct CLI turn with no
    threaded ``working_dir`` writes into throwaway space, never the repository."""
    base = os.environ.get("OMNIAGENTOS_VAR_DIR")
    if not base:
        base = os.path.join(
            os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
            "var",
        )
    workspace = os.path.join(base, "agent-workspace")
    try:
        os.makedirs(workspace, exist_ok=True)
    except OSError:
        return base
    return workspace


# The tiny safe allowlist: process hygiene, locale, terminal, and TLS-trust vars
# that a subprocess legitimately needs and that carry no credential.
_SAFE_ENV_HYGIENE = frozenset(
    {
        "HOME",
        "PATH",
        "TERM",
        "TZ",
        "TMPDIR",
        "TMP",
        "TEMP",
        "USER",
        "LOGNAME",
        "SHELL",
        "PWD",
        "OLDPWD",
        "HOSTNAME",
        "SHLVL",
        "LANG",
        "LANGUAGE",
        "COLUMNS",
        "LINES",
        "COLORTERM",
        "DISPLAY",
        "SSL_CERT_FILE",
        "SSL_CERT_DIR",
        "CURL_CA_BUNDLE",
        "NODE_EXTRA_CA_CERTS",
        "REQUESTS_CA_BUNDLE",
    }
)

# WHICH account/state dir a subscription CLI uses. Filesystem paths only -- the
# auth material lives INSIDE the directory and is never read by this process
# (see omniagentos/routing/config.py). Names mirror
# swarm/provider_exec.py::PROVIDER_CONFIG_ENV; keep the two in sync.
#
# Stripping these is not a safety win, it is an account-routing BUG: the child
# CLI falls back to the provider's default dir and runs as a different account
# (live 2026-07-26 -- see the module docstring).
_PROVIDER_CONFIG_DIR_ENV = frozenset(
    {
        "CLAUDE_CONFIG_DIR",
        "CODEX_HOME",
        "GROK_HOME",
        "GEMINI_CLI_HOME",
        "KIMI_CODE_HOME",
        "QWEN_HOME",
    }
)

# macOS per-login-session handles. Opaque identifiers, NOT secrets: they name the
# session that securityd/Keychain lookups resolve against. SSH_AUTH_SOCK is
# deliberately absent -- an agent socket IS a credential channel.
_MACOS_SESSION_ENV = frozenset(
    {
        "SECURITYSESSIONID",
        "__CF_USER_TEXT_ENCODING",
        "__CFBundleIdentifier",
        "LaunchInstanceID",
        "TERM_SESSION_ID",
        "XPC_FLAGS",
        "XPC_SERVICE_NAME",
    }
)

_SAFE_ENV_EXACT = _SAFE_ENV_HYGIENE | _PROVIDER_CONFIG_DIR_ENV | _MACOS_SESSION_ENV

# Force-deny, applied BEFORE the allowlist: any name shaped like a credential can
# never be passed, even if a future allowlist edit names it. Belt and braces on top
# of deny-by-default -- an allowlist is only as good as its next edit.
_FORCE_DENY_SUBSTRINGS = (
    "API_KEY",
    "APIKEY",
    "SECRET",
    "TOKEN",
    "PASSWORD",
    "PASSWD",
    "CREDENTIAL",
    "PRIVATE_KEY",
    "ACCESS_KEY",
    "SESSION_KEY",
    "_DSN",
    "CONNECTION_STRING",
)

# This is deliberately broader than ``_FORCE_DENY_SUBSTRINGS``.  The latter is
# the unconditional denylist for every name in the parent environment; this
# list closes the two prefix rules below after they have admitted a name.  In
# particular, ``XDG_AUTH`` and ``OMNIAGENTOS_BRIDGE_SESSION_AUTH`` are not safe
# merely because their leading component normally names a filesystem pointer or
# an opaque session identifier.
_CREDENTIAL_SHAPED_FRAGMENTS = ("AUTH", "TOKEN", "SECRET", "KEY", "PASSWORD", "CREDENTIAL")


def _is_credential_shaped_env_name(key: str) -> bool:
    """Return whether ``key`` contains a credential fragment, ignoring case/separators."""
    normalized = re.sub(r"[^A-Z0-9]", "", key.upper())
    return any(fragment in normalized for fragment in _CREDENTIAL_SHAPED_FRAGMENTS)


# Prefixes kept wholesale: LC_* (locale), XDG_* (config/cache dir pointers), and
# the Session Bridge session id the hook plumbing sets.
_SAFE_ENV_PREFIXES = ("LC_", "XDG_", "OMNIAGENTOS_BRIDGE_SESSION")


@functools.lru_cache(maxsize=1)
def _registry_env_names() -> frozenset[str]:
    """Env var names for EVERY connector in the registry.

    The registry is the system's inventory of credentials, so every name in it is
    force-denied: an agent subprocess gets its credentials from the broker or not
    at all. (This used to carve out the broker's "injectable" AI-provider group so
    model CLIs could authenticate by API key -- but the subscription CLIs carry
    their own auth in their config dir, and handing them an API key silently
    switches them to metered billing.)"""
    try:
        from omniagentos.connectors import load_registry

        registry = load_registry()
        names: set[str] = set()
        for connector in registry.connectors.values():
            names.update(connector.env)
        return frozenset(names)
    except Exception:  # noqa: BLE001 -- a registry problem must fail CLOSED (strip more)
        return frozenset()


@functools.lru_cache(maxsize=1)
def _registry_danger_env_names() -> frozenset[str]:
    """Env var names for every connector in a DANGER group (payments/ads/infra).

    Force-stripped even if some future allowlist edit would otherwise pass them:
    the registry's own ``danger`` group flag is the source of truth for "this
    credential can move money or destroy infrastructure"."""
    try:
        from omniagentos.connectors import load_registry

        registry = load_registry()
        danger_groups = {g for g, spec in registry.groups.items() if spec.danger}
        names: set[str] = set()
        for connector in registry.connectors.values():
            if connector.group in danger_groups:
                names.update(connector.env)
        return frozenset(names)
    except Exception:  # noqa: BLE001 -- fail closed: if unsure, the allowlist still gates.
        return frozenset()


def _is_safe_env_name(key: str) -> bool:
    return key in _SAFE_ENV_EXACT or key.startswith(_SAFE_ENV_PREFIXES)


def _is_force_denied_env_name(key: str) -> bool:
    """True for anything shaped like, or registered as, a credential."""
    upper = key.upper()
    if any(marker in upper for marker in _FORCE_DENY_SUBSTRINGS):
        return True
    return key in _registry_env_names() or key in _registry_danger_env_names()


# CLAUDE_CONFIG_DIR is the account-pool's routing knob (a filesystem PATH, never
# a credential -- see omniagentos/routing/account_pool.py). It is allowlisted
# above so an INHERITED value survives the scrub, and _invoke() also re-applies a
# pooled account's value via env_overrides AFTER the scrub, so an explicit account
# selection always wins over whatever the parent happened to export.


def _scrubbed_env(base: dict[str, str] | None = None) -> dict[str, str]:
    """Return an allowlisted copy of the environment for an agent/runner subprocess.

    KEEPS only: the process-hygiene/locale allowlist, the provider config-dir
    pointers (paths that select WHICH account the CLI runs as), and the macOS
    per-session handles Keychain lookups resolve against. STRIPS everything else --
    every payment/bank/infra credential, model-provider API key, admin DSN,
    OPERATOR_TOKEN / self-approve token, and any unknown secret. Registry-known and
    credential-SHAPED names are force-denied before the allowlist is consulted.

    EVERY admitted name then receives a second credential-shape check — the
    ``_SAFE_ENV_EXACT`` entries as well as the prefix-admitted ones, not just
    the prefixes. That is deliberate and it is the same doctrine as the
    force-deny list above: an allowlist is only as good as its next edit, so a
    future exact entry that happens to be credential-shaped is refused rather
    than trusted for having been typed out. No current exact entry is
    credential-shaped, so the check costs nothing today; it exists for the edit
    nobody has made yet. Widening ``_CREDENTIAL_SHAPED_FRAGMENTS`` can therefore
    silently drop an exact-allowlisted name — which is the intended failure
    direction, but check this list when you widen that one.

    Args:
        base: Environment dict to scrub (default: os.environ)

    Returns:
        Allowlisted copy of the environment
    """
    source: Iterable[tuple[str, str]] = (base if base is not None else dict(os.environ)).items()

    result: dict[str, str] = {}
    for key, value in source:
        if _is_force_denied_env_name(key):
            continue  # never hand a credential to a subprocess
        if not _is_safe_env_name(key):
            continue
        if _is_credential_shaped_env_name(key):
            continue  # admission by prefix OR by exact entry cannot launder a credential
        result[key] = value
    return result


@dataclass
class Invocation:
    """The transport-level result of a single CLI invocation."""

    stdout: str = ""
    stderr: str = ""
    returncode: int | None = None
    timed_out: bool = False
    exception: str | None = None


@dataclass
class ParsedResponse:
    """Provider-neutral data extracted from a successful CLI response."""

    text: str
    session_ref: str | None
    payload: dict[str, Any]


def sandbox_level(input: AgentInput) -> str:
    """Return the frozen sandbox level, defaulting to the safe option."""

    sandbox = input.metadata.get("sandbox", {})
    if isinstance(sandbox, dict) and sandbox.get("level") == "workspace_write":
        return "workspace_write"
    return "read_only"


def granted_write_roots(input: AgentInput) -> list[str]:
    """The project's granted external dirs to widen the OS sandbox write-scope by.

    Drive-under-full-auto wiring: the runner threads a project's
    ``root_dirs``+``allowed_dirs`` (incl. any granted Drive subfolder, the SAME
    source of truth ``--add-dir`` uses) onto ``metadata["extra_dirs"]`` (see
    ``runner/core.py::_project_extra_dirs``). The OS sandbox that confines a sub-CLI
    MUST add those as write roots, otherwise a Drive-scoped agent running under
    full-auto is physically denied from writing its own granted folder. Everything
    NOT in this list (and not a working-dir / CLI-state root) still hard-stops.

    Returns only non-empty string entries; realpath resolution happens in the
    sandbox profile builder. An absent/malformed value degrades to ``[]`` --
    byte-for-byte unchanged confinement for every run without granted dirs."""
    raw = input.metadata.get("extra_dirs")
    if not isinstance(raw, list):
        return []
    roots: list[str] = []
    for entry in raw:
        value = str(entry).strip()
        if value and value not in roots:
            roots.append(value)
    return roots


# ---------------------------------------------------------------------------
# Reasoning effort: ONE resolver, three sources (T4.5).
#
# Effort used to reach the adapters down two pipes that never met:
#   metadata["effort"]            written by intake/fable.py, intake/fallback.py,
#                                 lab/executor -- read ONLY by adapters/claude.py
#   metadata["reasoning_effort"]  written by swarm/provider_exec.py -- read ONLY
#                                 by codex/grok, via requested_reasoning_effort()
# So swarm's ROUTER-DECIDED effort was silently dropped for every Claude worker
# (configs/swarm.yaml router.effort_by_tier, and effort_overrides_by_tier which
# pins claude-opus-5 to xhigh at the complex tier -- that pin went nowhere), and
# intake/lab's effort was silently dropped for codex and grok.
#
# This is the READ side converging: every adapter now resolves the same three
# sources in the same precedence order. The writers are deliberately unchanged.
# ---------------------------------------------------------------------------

_CONTRACT_EFFORTS = frozenset(member.value for member in ReasoningEffort)

# "max" is a fourth wrinkle and it is LIVE, not hypothetical: intake/fable.py:46
# declares ``Effort = Literal["low", "medium", "high", "max"]`` and
# orchestrator/intent.py routes "max" too, while ``claude --help`` documents
# ``--effort <level>  (low, medium, high, xhigh, max)``. contracts.ReasoningEffort
# tops out at XHIGH, so normalizing strictly through the enum would silently
# DOWNGRADE a value that works today. It is carried here as an explicit
# above-XHIGH extension until the enum gains a MAX member (contracts.py is
# Tier-P/committed; reported to the lead rather than edited).
_EFFORT_ABOVE_XHIGH = "max"

# Per-adapter CLI capability. These differ for real, so every value a given CLI
# does not accept is EXPLICITLY mapped or dropped here rather than passed
# through into argv:
#
#   codex/grok -- `-c model_reasoning_effort="..."` / `--reasoning-effort`.
#       Accept "minimal" (which the Claude CLI does not) up to "xhigh"; this is
#       the pre-existing shipped vocabulary, unchanged. No "max" =>
#       MAPPED DOWN to "xhigh", their strongest: the caller asked for the most
#       reasoning available, and dropping would hand back the CLI default.
#   claude -- `--effort <level>` documented as (low, medium, high, xhigh, max),
#       verified against the installed CLI. No "minimal" => MAPPED UP to "low",
#       its weakest, for the mirror-image reason: dropping would hand back the
#       session default, which is stronger than the minimal that was asked for.
#   gemini/kimi/qwen -- absent from both tables on purpose: they have no effort knob
#       at all, so they resolve to None and emit nothing.
_CODEX_FAMILY_EFFORTS = frozenset({"minimal", "low", "medium", "high", "xhigh"})
_CLI_EFFORT_VOCAB: dict[str, frozenset[str]] = {
    "codex": _CODEX_FAMILY_EFFORTS,
    "grok": _CODEX_FAMILY_EFFORTS,
    "claude": frozenset({"low", "medium", "high", "xhigh", _EFFORT_ABOVE_XHIGH}),
}
_CLI_EFFORT_ALIASES: dict[str, dict[str, str]] = {
    "codex": {_EFFORT_ABOVE_XHIGH: "xhigh"},
    "grok": {_EFFORT_ABOVE_XHIGH: "xhigh"},
    "claude": {"minimal": "low"},
}


def _canonical_effort(raw: object) -> str | None:
    """Normalize one candidate value to the canonical vocabulary, or ``None``.

    Metadata is untrusted config-shaped input and the value lands in argv, so
    anything outside the vocabulary degrades to ``None`` rather than being
    forwarded to a CLI."""
    if isinstance(raw, ReasoningEffort):
        return raw.value
    if not isinstance(raw, str):
        return None
    value = raw.strip().lower()
    if value in _CONTRACT_EFFORTS or value == _EFFORT_ABOVE_XHIGH:
        return value
    return None


def resolve_reasoning_effort(input: AgentInput) -> str | None:
    """The caller-requested reasoning effort, provider-independent.

    Precedence, highest first:
      1. ``input.envelope.effort``          -- the TN.0 typed field, the new
                                              source of truth.
      2. ``metadata["reasoning_effort"]``   -- legacy, written by swarm.
      3. ``metadata["effort"]``             -- legacy, written by intake/lab.

    An unrecognized value at a higher-precedence source is not a "set" value, so
    resolution FALLS THROUGH to the next source rather than hard-stopping at
    ``None``; that keeps a garbage metadata key from masking a good one.

    Returns the canonical value, NOT a per-CLI one -- see
    :func:`cli_reasoning_effort` for the capability mapping."""
    # getattr keeps this helper usable with the duck-typed AgentInput stand-ins
    # some call sites build; on a real AgentInput ``envelope`` always exists.
    envelope = getattr(input, "envelope", None)
    for raw in (
        getattr(envelope, "effort", None),
        input.metadata.get("reasoning_effort"),
        input.metadata.get("effort"),
    ):
        value = _canonical_effort(raw)
        if value is not None:
            return value
    return None


def cli_reasoning_effort(input: AgentInput, provider: str) -> str | None:
    """The resolved effort expressed in ``provider``'s own CLI vocabulary.

    ``None`` means "emit no effort flag": either nothing was requested, or the
    provider has no effort knob, or the requested value has no honest
    equivalent on that CLI."""
    value = resolve_reasoning_effort(input)
    if value is None:
        return None
    vocab = _CLI_EFFORT_VOCAB.get(provider)
    if vocab is None:
        return None  # provider has no effort knob (gemini/kimi/qwen)
    if value in vocab:
        return value
    return _CLI_EFFORT_ALIASES.get(provider, {}).get(value)


def requested_reasoning_effort(input: AgentInput) -> str | None:
    """The caller-requested effort in the codex/grok CLI vocabulary.

    Name and signature preserved: ``adapters/codex.py`` and ``adapters/grok.py``
    call this with no provider argument. Codex and grok share one vocabulary
    (before this change a single module-level effort set served both), so
    resolving against "codex" covers both."""
    return cli_reasoning_effort(input, "codex")


# Self-sandboxing CLIs cannot nest their own Seatbelt under the outer OS wrap:
# macOS denies sandbox_apply inside an already-sandboxed process, so the CLI's
# own sandbox init fails and either every write silently dies (codex, live
# failures swr_d3855d2d/swr_5b42d209) or the CLI refuses to start (grok). Under
# the outer profile the inner sandbox must therefore be disabled; confinement is
# unchanged because the outer write-confinement profile remains the enforcement
# layer (plan cross-cutting decision #2 / lead decision D2).
#
# Per-provider vocabulary (D2):
#   codex -> rewrite the ``--sandbox`` value to ``danger-full-access``
#            (codex's documented no-sandbox mode, live-verified).
#   grok  -> REMOVE the ``--sandbox <value>`` pair entirely. grok is NOT
#            codex-derived here: it rejects codex vocabulary ("Custom sandbox
#            profile 'danger-full-access' not found ... refusing to start",
#            live ses_e9f5956b) and its documented default profile is "off"
#            (~/.grok/docs/user-guide/18-sandbox.md), so omitting the flag is
#            the only verified-safe way to run outer-only.
_INNER_SANDBOX_DISABLE: dict[str, str | None] = {
    "codex": "danger-full-access",
    "grok": None,  # None => strip the "--sandbox <value>" pair
    # claude is handled additively below: Claude Code has no --sandbox argv
    # flag; its Seatbelt is disabled via a --settings override. Nested
    # Seatbelt was the 2026-07-26 bridge failure (3/3 claude sessions: Read
    # fine, every mutating tool died as opaque "api-error"). gemini/kimi
    # still need the same audit — neither self-sandboxes on macOS today.
    "claude": "__settings__",
}

_CLAUDE_SANDBOX_OFF_SETTINGS = '{"sandbox": {"enabled": false}}'


def disable_inner_sandbox(command: list[str], provider: str) -> list[str]:
    """Neutralize a self-sandboxing CLI's own confinement under the outer wrap.

    Callers MUST gate this on the outer wrap actually engaging
    (``runner.sandbox.wrap_available``): with no outer profile the CLI's own
    sandbox is the sole confinement and must be preserved. codex/grok carry a
    ``--sandbox`` argv pair (rewritten/stripped); claude self-sandboxes by
    default with no argv flag, so it gets an appended ``--settings`` override.
    Providers not listed (gemini/kimi/qwen) are returned unchanged."""
    if provider == "claude":
        if "--settings" in command:
            return command  # caller already controls settings; don't fight it
        return [*command, "--settings", _CLAUDE_SANDBOX_OFF_SETTINGS]
    if provider not in _INNER_SANDBOX_DISABLE:
        return command
    replacement = _INNER_SANDBOX_DISABLE[provider]
    rewritten: list[str] = []
    skip_next = False
    for index, token in enumerate(command):
        if skip_next:
            skip_next = False
            continue
        if token == "--sandbox" and index + 1 < len(command):
            skip_next = True
            if replacement is not None:
                rewritten.extend([token, replacement])
            continue
        rewritten.append(token)
    return rewritten


def extract_trailing_json(
    stream: str,
    *,
    envelope_keys: Iterable[str],
    error_keys: Iterable[str] = ("error",),
) -> dict[str, Any] | None:
    """The last SUCCESS-shaped top-level JSON object in a noisy merged stream,
    falling back to the last error-keyed object, else None.

    The swarm provider-exec path merges stderr into stdout
    (``stderr=subprocess.STDOUT``), so a genuinely clean CLI exit can carry
    warning/noise lines before its JSON envelope — a strict ``json.loads`` over
    the whole stream then misclassifies a clean completion as FAILED. This
    helper only engages AFTER a strict parse failed. A candidate is
    SUCCESS-shaped when it contains one of the provider's ``envelope_keys``
    (response/session_id for gemini, text/sessionId for grok) and NONE of
    ``error_keys``; the LAST success-shaped object wins, so a JSON blob the
    model printed in prose loses to the real trailing envelope, and a stray
    error blob dumped AFTER a clean success envelope (observed live: gemini
    error-report JSON on the merged stream) cannot mask a clean completion.
    Only when no success-shaped object exists does the last error-keyed object
    surface, so a genuine error envelope still raises its real message."""
    keys = tuple(envelope_keys)
    err_keys = tuple(error_keys)
    decoder = json.JSONDecoder()
    success: dict[str, Any] | None = None
    error: dict[str, Any] | None = None
    index = 0
    while True:
        start = stream.find("{", index)
        if start == -1:
            break
        try:
            candidate, end = decoder.raw_decode(stream, start)
        except json.JSONDecodeError:
            index = start + 1
            continue
        if isinstance(candidate, dict):
            if any(key in candidate for key in err_keys):
                error = candidate
            elif any(key in candidate for key in keys):
                success = candidate
        index = max(end, start + 1)
    return success if success is not None else error


def structured_prompt(prompt: str, schema: dict[str, Any]) -> str:
    """Append the JSON-schema instruction every structured-output call uses.

    Module-level so the api-tier adapters (``adapters/api_base.py``) build the
    SAME prompt the CLI adapters do — a planner rung must not get a different
    contract just because it left the CLIs. ``CliAdapter._structured_prompt``
    delegates here."""
    schema_text = json.dumps(schema, sort_keys=True)
    return f"{prompt}\n\nYou MUST respond ONLY with valid JSON matching this schema: {schema_text}"


def validate_structured_json(
    text: str, schema: dict[str, Any]
) -> tuple[dict[str, Any] | None, str | None]:
    """Parse+validate a model's structured answer -> ``(json, error)``.

    Tolerates a fenced ```json block (CLIs fence routinely). Shared with the
    api-tier adapters for the same reason as :func:`structured_prompt`;
    ``CliAdapter._validate_json`` delegates here."""
    stripped = text.strip()
    if stripped.startswith("```"):
        # CLIs under structured_prompt still fence their JSON routinely
        # (haiku via `claude -p` especially) — a fenced-but-valid object
        # must not become an error result.
        stripped = re.sub(r"^```[a-zA-Z]*\s*|\s*```$", "", stripped).strip()
    try:
        decoded = json.loads(stripped)
    except json.JSONDecodeError as exc:
        return None, f"response is not valid JSON: {exc.msg}"
    if not isinstance(decoded, dict):
        return None, "response JSON must be an object"
    required = schema.get("required", [])
    if not isinstance(required, list):
        return None, "output schema required must be a list"
    missing = [key for key in required if isinstance(key, str) and key not in decoded]
    if missing:
        return None, f"response JSON is missing required keys: {', '.join(missing)}"
    return decoded, None


# Keys a failing CLI envelope carries its human-readable cause in. `result` is
# claude's (on a FAILED turn it holds the error text, not an answer -- this
# helper only ever runs on a failure path); `message`/`error`/`detail` cover the
# others.
_ERROR_MESSAGE_KEYS = ("message", "error", "result", "detail")
# ...and the machine-readable classification worth keeping in front of it.
_ERROR_STATUS_KEYS = ("api_error_status", "status_code", "status")
_ERROR_REASON_KEYS = ("terminal_reason", "subtype")


def stderr_tail(stderr: str) -> str:
    """Make CLI JSON errors readable while retaining a bounded error value.

    The composed form is ``"<reason> <status>: <message>"``, which matters far
    more than it looks: the fallback classifier
    (``omniagentos.intake.fallback._is_limit_or_unavailable_error``) reads this
    string to decide whether to try the next model. Live 2026-07-26, a spend-limit
    429 arrived as a claude envelope with NO ``message``/``error`` key, so this
    helper fell through to a sorted JSON dump truncated to its last 500 chars --
    which cut off both ``api_error_status: 429`` and ``result: "You've hit your
    monthly spend limit"``. The classifier saw no limit signal, called it
    non-retryable, and planning died instead of falling back to the next model.
    """

    text = stderr.strip()
    try:
        decoded = json.loads(text)
    except json.JSONDecodeError:
        return text[-STDERR_TAIL_LENGTH:]
    if not isinstance(decoded, dict):
        return text[-STDERR_TAIL_LENGTH:]

    message = next(
        (
            value
            for key in _ERROR_MESSAGE_KEYS
            if isinstance(value := decoded.get(key), str) and value.strip()
        ),
        None,
    )
    if message is None:
        return json.dumps(decoded, sort_keys=True)[-STDERR_TAIL_LENGTH:]

    prefix_parts = [
        str(decoded[key])
        for key in (*_ERROR_REASON_KEYS, *_ERROR_STATUS_KEYS)
        if decoded.get(key) not in (None, "", "success")
    ]
    prefix = " ".join(prefix_parts)
    composed = f"{prefix}: {message}" if prefix else message
    # INTEGRATION (reliability-scale): composing a human string dropped the usage
    # block, and the fallback classifier's zero-token probe
    # (`intake.fallback._ZERO_OUTPUT_TOKENS`) reads `"output_tokens": N` out of
    # this very string — `_error_usage()` leaves `AgentUsage.output_tokens` None on
    # this path, so the text IS the only evidence. Re-attach it verbatim, in the
    # JSON form the probe already matches, so the composed message stays readable
    # AND the `api_error`-with-no-tokens retry rung stays reachable.
    evidence = _output_token_evidence(decoded)
    # HEAD, not tail: on a composed message the diagnosis is at the front. The
    # evidence suffix is budgeted OUT of the 500 chars, never appended past it.
    return composed[: STDERR_TAIL_LENGTH - len(evidence)] + evidence


def _output_token_evidence(decoded: dict[str, Any]) -> str:
    """``' {"output_tokens": N}'`` when the envelope reported one, else ``''``.

    Only emitted when the provider actually reported a count: an envelope with no
    usage block says nothing about what the model produced, and inventing a zero
    there would hand a retry to failures that never earned one.
    """
    usage = decoded.get("usage")
    if not isinstance(usage, dict):
        return ""
    tokens = usage.get("output_tokens")
    # bool is an int subclass; a JSON `true` here is not a token count.
    if not isinstance(tokens, int) or isinstance(tokens, bool):
        return ""
    return f' {{"output_tokens": {tokens}}}'


class CliAdapter(ABC):
    """Base class for a single-turn command-line AgentAdapter."""

    name: str
    version = "1.0"
    cli: str
    supports_resume = False
    # True when the CLI honors a read-only sandbox level via argv. A CLI that
    # force-auto-approves its own tools regardless of flags (kimi's headless
    # `forceAuto()`) sets this False, and is REFUSED for ANY unattended agent step
    # (not just workspace_write) unless a human explicitly elevates it, because it
    # cannot honor any sandbox level (AC-policy #B1 / fix4 BLOCKER 2).
    honors_read_only_sandbox = True

    def __init__(self) -> None:
        self._active: dict[str, subprocess.Popen[str]] = {}
        self._health: HealthStatus | None = None

    @abstractmethod
    def _command(self, input: AgentInput, prompt: str, session_ref: str | None) -> list[str]:
        """Build argv for one new or resumed CLI turn."""

    @abstractmethod
    def _parse(self, stdout: str) -> ParsedResponse:
        """Parse provider output after a zero exit code."""

    @abstractmethod
    def _usage(self, input: AgentInput, parsed: ParsedResponse, wall_ms: int) -> AgentUsage:
        """Build provider-specific usage for a successfully parsed response."""

    def _working_dir(self, input: AgentInput) -> str:
        """The dir a CLI turn runs in.

        AC-policy fix7: a ``None``/empty ``working_dir`` must NOT fall back to
        ``os.getcwd()`` — under the control plane that is the repo root, so an
        agent step (or a schema-only classification LLM) would run with the
        repository itself as its writable working dir and could overwrite
        ``configs/policy.yaml``. Runner agent steps always thread the per-run
        workspace (see ``runner/core.py``); this fallback covers adapter-DIRECT
        callers (provisioning/intake classification LLMs) by pinning them to a
        throwaway var-anchored workspace instead of the repo.
        """
        return input.working_dir or _default_agent_workspace()

    def _stdin_payload(self, prompt: str) -> str | None:
        """Bytes sent on stdin for one invocation.

        Most wrapped CLIs either consume the prompt from stdin (Codex) or ignore
        stdin when their prompt flag is present. Providers whose CLI combines
        stdin with the prompt flag can override this to avoid duplicating the
        request text.
        """
        return prompt

    def _timeout_usage(self, wall_ms: int) -> AgentUsage:
        return AgentUsage(wall_ms=wall_ms, estimated=True, source="estimator")

    def _error_usage(self, wall_ms: int) -> AgentUsage:
        return AgentUsage(wall_ms=wall_ms, estimated=True, source="estimator")

    def _kill_process_group(self, proc: subprocess.Popen[str]) -> bool:
        """Terminate the CLI and children created for its tool calls.

        Returns True when the kill signal was delivered (or the process is
        already gone). Returns False when the signal could not be sent — callers
        such as ``cancel()`` must not report that as success.
        """

        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
            return True
        except ProcessLookupError:
            # Already reaped — the process is gone, which is the goal.
            return True
        except (PermissionError, OSError):
            return False

    @staticmethod
    def _bounded_communicate(proc: subprocess.Popen[str], timeout_s: float) -> tuple[str, str]:
        """Reap a killed CLI child without blocking the adapter slot indefinitely."""
        try:
            stdout, stderr = proc.communicate(timeout=max(0.1, timeout_s))
            return stdout or "", stderr or ""
        except subprocess.TimeoutExpired:
            try:
                proc.kill()
            except (ProcessLookupError, OSError, AttributeError):
                # AttributeError: test doubles / already-reaped handles.
                pass
            try:
                stdout, stderr = proc.communicate(timeout=1.0)
                return stdout or "", stderr or ""
            except (subprocess.TimeoutExpired, ProcessLookupError, OSError):
                # Child is gone or still wedged; free the slot with empty output.
                return "", ""
        except (ProcessLookupError, OSError):
            return "", ""

    @staticmethod
    def _sandboxed_launch(
        command: list[str],
        working_dir: str,
        extra_write_roots: list[str] | None = None,
        *,
        provider: str | None = None,
        provider_config_dir: str | None = None,
        workspace_writable: bool = True,
        write_roots_override: list[str] | None = None,
        net_mode: str = "open",
        net_loopback_ports: Sequence[int] = (),
        write_deny_literals: Sequence[str] = (),
    ) -> list[str]:
        """Wrap a sub-CLI argv in the OS write-confinement sandbox (AC-policy #B1).

        The sandbox write-scope is the sub-CLI's own state dirs
        (``adapter_write_roots``) PLUS any ``extra_write_roots`` the project has
        been granted (Drive-under-full-auto: a granted Drive subfolder threaded via
        ``metadata["extra_dirs"]`` -> ``granted_write_roots``). The working dir is
        added by ``wrap_command`` itself -- unless ``workspace_writable=False``
        (a read-only invocation: the outer profile then grants ONLY the CLI
        state/config roots, never the workspace). Everything outside this union
        still hard-stops at the OS boundary.

        Imported lazily to avoid a runner<->adapters import cycle. Returns the argv
        unchanged when the sandbox is unavailable/unresolvable.

        ``write_roots_override`` REPLACES the computed root set with an explicit one
        (the autonomy lease's ``fs_write_roots`` under enforce mode -- see
        ``omniagentos.lease.seam``), so that when a lease is in force the profile is
        built from the roots the lease actually granted and recorded, not from a
        second independent computation that could drift from it. ``net_mode`` /
        ``net_loopback_ports`` select the profile's egress directives. All three
        default to the historical behavior, so every existing call site -- including
        ``swarm/provider_exec.py`` and ``longhaul/engine.py`` -- is unchanged."""
        from omniagentos.runner import sandbox

        if write_roots_override is not None:
            roots = list(write_roots_override)
        else:
            roots = (
                sandbox.adapter_write_roots()
                if provider is None and provider_config_dir is None
                else sandbox.adapter_write_roots(provider, provider_config_dir)
            )
            if extra_write_roots:
                roots = [*roots, *extra_write_roots]
        try:  # pre-create Claude Code's Bash scratch dir (an allowed write root)
            os.makedirs(sandbox.claude_tmp_root(), exist_ok=True)
        except OSError:
            pass
        return sandbox.wrap_command(
            command,
            working_dir,
            extra_write_roots=roots,
            workspace_writable=workspace_writable,
            net_mode=net_mode,
            net_loopback_ports=net_loopback_ports,
            extra_write_deny_literals=write_deny_literals,
        )

    @staticmethod
    def _adapter_roots(extra_write_roots: list[str] | None) -> list[str]:
        """The write roots ``_sandboxed_launch`` computes for an ``_invoke`` spawn.

        Factored out so the lease seam can be handed the SAME set the profile will
        be built from (see ``_sandboxed_launch``'s ``write_roots_override``). This
        mirrors the no-provider branch exactly, because ``_invoke`` calls
        ``_sandboxed_launch`` without ``provider``/``provider_config_dir``."""
        from omniagentos.runner import sandbox

        roots = sandbox.adapter_write_roots()
        if extra_write_roots:
            roots = [*roots, *extra_write_roots]
        return roots

    def _invoke(
        self,
        command: list[str],
        *,
        prompt: str,
        working_dir: str,
        timeout_ms: int,
        session_ref: str | None,
        env_overrides: dict[str, str] | None = None,
        extra_write_roots: list[str] | None = None,
        sandbox_level: str = "read_only",
        lease_subject: dict[str, str] | None = None,
    ) -> Invocation:
        """Run one command in a new process group and collect all output.

        ``env_overrides`` (e.g. a pooled account's ``CLAUDE_CONFIG_DIR`` --
        see omniagentos/routing/account_pool.py) is merged in AFTER the
        env-scrub below, so an explicit override always reaches the
        subprocess regardless of the scrub denylist. Never used to pass a
        credential -- only path-like routing values.

        ``sandbox_level`` is the caller-REQUESTED level (see
        ``sandbox_level(input)``); it decides whether the outer Seatbelt
        profile grants the workspace as a write root. Defaults to
        ``read_only`` (fail closed): a caller that does not thread the level
        gets an outer profile whose workspace is unwritable.

        ``lease_subject`` carries the run/task/session ids the autonomy lease
        binds itself to. It is used ONLY when the lease flag is on; with the flag
        off (the default) nothing here reads it.
        """

        proc: subprocess.Popen[str] | None = None
        plan: Any = None
        try:
            # SPAWN SITE 1: agent sub-CLI invocation. env-scrub (no money keys,
            # F1/R2-12) + a pooled account's env override (CLAUDE_CONFIG_DIR,
            # account_pool.py) merged AFTER the scrub, + OS sandbox (AC-policy #B1):
            # confine the sub-CLI's file writes+deletes to the working dir + the
            # CLI's own state dirs + any project-granted external dirs
            # (Drive-under-full-auto), so an agent step routed to a force-auto CLI
            # (kimi/gemini) cannot destroy or overwrite an out-of-scope file. The
            # sandbox wrap is a no-op when sandbox-exec is unavailable/unresolvable.
            env = _scrubbed_env()  # Apply env-scrub here
            if env_overrides:
                env.update(env_overrides)
            # Inner-sandbox disable for self-sandboxing CLIs (codex/grok):
            # applied ONLY when the outer Seatbelt wrap will actually engage
            # (wrap_available) — on a host without the OS sandbox the CLI's own
            # --sandbox stays as the sole confinement. Mirrors dad0034's worker
            # path fix; this covers every adapter-direct caller (swarm
            # reviewer, orchestrator reviewer, classification LLMs), where the
            # nested sandbox_apply denial broke reads and surfaced as bogus
            # review DENYs (run swr_850835).
            if self.name in _INNER_SANDBOX_DISABLE:
                from omniagentos.runner import sandbox

                if sandbox.wrap_available(command, working_dir):
                    command = disable_inner_sandbox(command, self.name)
            # Reviewer write-capability fix: when the REQUESTED level is
            # read_only, the outer profile must not grant the workspace as a
            # write root (with the inner sandbox off under the wrap, the outer
            # layer is what keeps a read-only reviewer actually read-only).
            # Only the CLI state/config roots stay writable; project-granted
            # extra roots are write-grants too, so they are dropped as well.
            workspace_writable = sandbox_level == "workspace_write"
            granted = extra_write_roots if workspace_writable else None
            # AUTONOMY LEASE (Track D). Gated entirely on the flag: with
            # OMNIAGENTOS_AUTONOMY_LEASE unset/off this is one config lookup and
            # every launch parameter below keeps the value it has always had.
            # shadow -> a lease is issued and recorded, plan stays inert.
            # enforce -> the lease supplies the write roots, clamps the timeout,
            # selects the SBPL egress mode, and contributes a setrlimit preexec;
            # an invalid/expired/unenforceable lease refuses the launch outright.
            write_roots_override: list[str] | None = None
            net_mode = "open"
            net_loopback_ports: Sequence[int] = ()
            write_deny_literals: Sequence[str] = ()
            preexec: Callable[[], None] | None = None
            from omniagentos.lease import seam as lease_seam

            if lease_seam.lease_active():
                subject = lease_subject or {}
                plan = lease_seam.plan_launch(
                    provider=self.name,
                    argv=command,
                    working_dir=working_dir,
                    workspace_writable=workspace_writable,
                    write_roots=self._adapter_roots(granted),
                    timeout_ms=timeout_ms,
                    run_id=subject.get("run_id", ""),
                    task_id=subject.get("task_id", ""),
                    session_id=subject.get("session_id", ""),
                )
                if plan.refusal is not None:
                    # Fail closed, exactly like _refuse_unattended_launch: nothing
                    # is spawned and the caller turns this into an ERROR result.
                    return Invocation(exception=plan.refusal)
                if plan.write_roots is not None:
                    write_roots_override = list(plan.write_roots)
                if plan.timeout_ms is not None:
                    timeout_ms = plan.timeout_ms
                net_mode = plan.net_mode
                net_loopback_ports = plan.net_loopback_ports
                write_deny_literals = plan.write_deny_literals
                preexec = plan.preexec_fn
                if plan.env_additions:
                    env.update(plan.env_additions)
            launch = self._sandboxed_launch(
                command,
                working_dir,
                granted,
                workspace_writable=workspace_writable,
                write_roots_override=write_roots_override,
                net_mode=net_mode,
                net_loopback_ports=net_loopback_ports,
                write_deny_literals=write_deny_literals,
            )
            if plan is not None:
                # Re-confirm the lease AT the spawn boundary. Everything between
                # plan_launch and here -- proxy bind, ledger flock+fsync, profile
                # construction -- takes real time, and a lease can expire or be
                # revoked inside that window. The timeout is re-derived from the
                # absolute deadline for the same reason.
                refusal, deadline_ms = lease_seam.confirm_at_spawn(plan, self.name)
                if refusal is not None:
                    return Invocation(exception=refusal)
                if deadline_ms is not None:
                    timeout_ms = deadline_ms
            proc = subprocess.Popen(
                launch,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                cwd=working_dir,
                start_new_session=True,
                env=env,
                preexec_fn=preexec,
            )
            if session_ref is not None:
                self._active[session_ref] = proc
            try:
                stdout, stderr = proc.communicate(
                    self._stdin_payload(prompt), timeout=timeout_ms / 1000
                )
                return Invocation(stdout=stdout, stderr=stderr, returncode=proc.returncode)
            except subprocess.TimeoutExpired:
                self._kill_process_group(proc)
                try:
                    stdout, stderr = proc.communicate(timeout=1)
                except subprocess.TimeoutExpired:
                    # SIGTERM is the required first action; SIGKILL prevents a
                    # stubborn child process from holding the runner indefinitely.
                    try:
                        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
                    except (ProcessLookupError, PermissionError, OSError):
                        pass
                    # Never unbounded: a daemonized grandchild can keep pipes open
                    # forever after SIGKILL of the group leader (L-18).
                    stdout, stderr = self._bounded_communicate(proc, POST_KILL_REAP_TIMEOUT_SECONDS)
                return Invocation(
                    stdout=stdout,
                    stderr=stderr,
                    returncode=proc.returncode,
                    timed_out=True,
                )
        except (OSError, subprocess.SubprocessError) as exc:
            return Invocation(exception=str(exc))
        finally:
            if session_ref is not None and self._active.get(session_ref) is proc:
                self._active.pop(session_ref, None)
            if plan is not None:
                # Releases the enforce-mode egress proxy's listening socket and
                # daemon thread. A no-op for every other plan (and for the flag
                # being off, where plan is None and this branch never runs).
                from omniagentos.lease import seam as _lease_seam

                _lease_seam.close_plan(plan)

    def _log(self, run_id: str, invocations: list[Invocation]) -> str:
        # Best-effort logging: anchored to a stable var/ dir (not cwd — council
        # INT-003) and NEVER raises, since _log is called from except blocks and an
        # exception here would escape run() and violate the adapter contract.
        try:
            base = os.environ.get("OMNIAGENTOS_VAR_DIR") or os.path.join(
                os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "var"
            )
            path = Path(base) / "logs" / run_id / f"{self.name}.log"
            path.parent.mkdir(parents=True, exist_ok=True)
            content = "\n\n".join(
                f"--- invocation {number} stdout ---\n{result.stdout}\n"
                f"--- invocation {number} stderr ---\n{result.stderr}\n"
                f"--- invocation {number} exception ---\n{result.exception or ''}"
                for number, result in enumerate(invocations, start=1)
            )
            path.write_text(content, encoding="utf-8")
            return str(path)
        except OSError:
            return ""

    def _structured_prompt(self, prompt: str, schema: dict[str, Any]) -> str:
        return structured_prompt(prompt, schema)

    def _validate_json(
        self, text: str, schema: dict[str, Any]
    ) -> tuple[dict[str, Any] | None, str | None]:
        return validate_structured_json(text, schema)

    def _unattended_elevated(self, input: AgentInput) -> bool:
        """True when a human explicitly elevated this unattended sub-CLI step."""
        meta = input.metadata
        return bool(meta.get("cli_workspace_write_elevated")) or bool(
            meta.get("cli_unattended_elevated")
        )

    def _refuse_unattended_launch(self, input: AgentInput) -> AgentResult | None:
        """Fail-closed floor for launching an unattended runner sub-CLI (AC-policy fix4).

        A runner AGENT sub-CLI installs NO gating hook and has NO classifier gate --
        its ONLY confinement is the OS sandbox. Two conditions make a launch unsafe
        and are refused unless a human explicitly elevated the step:

          1. A force-auto CLI (kimi, ``honors_read_only_sandbox=False``) auto-approves
             EVERY tool call and cannot honor ANY sandbox level, so even a read_only
             step is uncontrolled -- refuse it (BLOCKER 2). Once secrets are
             read-denied by the sandbox there is nothing to exfiltrate, but a
             force-auto CLI that also loses the OS sandbox has zero confinement, so
             the floor removes the "just don't route a step to kimi" dependency.
          2. The OS sandbox is unavailable/unproven -> the sub-CLI would run with NO
             write/secret/network confinement AND no classifier fallback. Fail
             CLOSED and refuse every write/network-capable agent CLI (HIGH).
        """
        if self._unattended_elevated(input):
            return None
        if not self.honors_read_only_sandbox:
            return AgentResult(
                status=ResultStatus.ERROR,
                usage=self._error_usage(1),
                error=(
                    f"unattended_forceauto_refused:cli-{self.name} force-auto-approves "
                    "its tools and cannot honor any sandbox level; an unattended step "
                    "needs explicit human elevation"
                ),
            )
        from omniagentos.runner import sandbox

        if not sandbox.sandbox_available():
            return AgentResult(
                status=ResultStatus.ERROR,
                usage=self._error_usage(1),
                error=(
                    f"unattended_no_sandbox_refused:cli-{self.name} the OS sandbox is "
                    "unavailable, so a sub-CLI would run with no write/secret/network "
                    "confinement and no classifier fallback; refusing (fail closed)"
                ),
            )
        return None

    def run(self, input: AgentInput) -> AgentResult:
        """Execute a CLI turn; adapter failures always become AgentResults."""
        return self._run_once(input)

    def _run_once(
        self, input: AgentInput, *, env_overrides: dict[str, str] | None = None
    ) -> AgentResult:
        """The actual single-account CLI turn behind ``run()``.

        Split out (unchanged logic, just parameterized) so a subclass can loop
        it across multiple accounts -- see ``ClaudeAdapter.run()`` /
        omniagentos/routing/account_pool.py, which is the only current caller
        that ever passes ``env_overrides``. Every other adapter's public
        ``run()`` still just calls this with no overrides, byte-for-byte the
        same as before this existed.
        """

        started = time.perf_counter()
        refusal = self._refuse_unattended_launch(input)
        if refusal is not None:
            return refusal
        invocations: list[Invocation] = []
        parsed: ParsedResponse | None = None
        # Project-granted external dirs (incl. any granted Drive folder) that the OS
        # sandbox must widen its write-scope by, so a Drive-scoped sub-CLI running
        # under full-auto can write its granted folder while everything else is still
        # physically confined. Empty for every run without a Drive/dir grant.
        granted_roots = granted_write_roots(input)
        # The REQUESTED sandbox level (read_only unless metadata explicitly asks
        # for workspace_write) -- threaded to _invoke so the OUTER Seatbelt
        # profile only grants the workspace as a write root when the caller
        # actually requested write capability.
        requested_sandbox = sandbox_level(input)
        lease_subject: dict[str, str] | None = None
        try:
            # Identity the autonomy lease binds itself to (Track D). Built INSIDE
            # the try so that a hostile/odd metadata value whose __str__ raises
            # cannot escape _run_once and turn a launch that succeeds on main into
            # an uncaught exception -- with the flag off nothing consumes this, and
            # the additive guarantee has to hold for malformed input too (security
            # review finding #12).
            lease_subject = {
                "run_id": input.run_id,
                "task_id": input.task_id,
                "session_id": str(input.metadata.get("session_id") or ""),
            }
            prompt = input.prompt
            if input.output_schema is not None:
                prompt = self._structured_prompt(prompt, input.output_schema)
            timeout_ms = (
                input.budget.wall_ms_max
                if input.budget.wall_ms_max is not None
                else DEFAULT_TIMEOUT_MS
            )
            timeout_ms = max(1, timeout_ms)
            invocation = self._invoke(
                self._command(input, prompt, None),
                prompt=prompt,
                working_dir=self._working_dir(input),
                timeout_ms=timeout_ms,
                session_ref=None,
                env_overrides=env_overrides,
                extra_write_roots=granted_roots,
                sandbox_level=requested_sandbox,
                lease_subject=lease_subject,
            )
            invocations.append(invocation)
            wall_ms = max(1, int((time.perf_counter() - started) * 1000))
            log_path = self._log(input.run_id, invocations)

            if invocation.timed_out:
                return AgentResult(
                    status=ResultStatus.TIMEOUT,
                    usage=self._timeout_usage(wall_ms),
                    log_path=log_path,
                    error="CLI invocation timed out",
                )
            if invocation.exception is not None:
                return AgentResult(
                    status=ResultStatus.ERROR,
                    usage=self._error_usage(wall_ms),
                    log_path=log_path,
                    error=invocation.exception[-STDERR_TAIL_LENGTH:],
                )
            if invocation.returncode != 0:
                # Some CLIs (codex --json) report failures as stdout events with
                # an empty stderr — an empty error string is undebuggable.
                return AgentResult(
                    status=ResultStatus.ERROR,
                    usage=self._error_usage(wall_ms),
                    log_path=log_path,
                    error=stderr_tail(invocation.stderr) or stderr_tail(invocation.stdout),
                )

            parsed = self._parse(invocation.stdout)
            if input.output_schema is None:
                return AgentResult(
                    status=ResultStatus.OK,
                    output_text=parsed.text,
                    session_ref=parsed.session_ref,
                    usage=self._usage(input, parsed, wall_ms),
                    log_path=log_path,
                )

            output_json, validation_error = self._validate_json(parsed.text, input.output_schema)
            if output_json is not None:
                return AgentResult(
                    status=ResultStatus.OK,
                    output_text=parsed.text,
                    output_json=output_json,
                    session_ref=parsed.session_ref,
                    usage=self._usage(input, parsed, wall_ms),
                    log_path=log_path,
                )

            repair_prompt = (
                f"Your previous response failed validation: {json.dumps(validation_error)}. "
                "Respond ONLY with corrected valid JSON matching the requested schema."
            )
            resume_ref = parsed.session_ref if self.supports_resume else None
            remaining_timeout_ms = max(1, timeout_ms - wall_ms)
            repair = self._invoke(
                self._command(input, repair_prompt, resume_ref),
                prompt=repair_prompt,
                working_dir=self._working_dir(input),
                timeout_ms=remaining_timeout_ms,
                session_ref=resume_ref,
                env_overrides=env_overrides,
                extra_write_roots=granted_roots,
                sandbox_level=requested_sandbox,
                lease_subject=lease_subject,
            )
            invocations.append(repair)
            wall_ms = max(1, int((time.perf_counter() - started) * 1000))
            log_path = self._log(input.run_id, invocations)
            if repair.timed_out:
                return AgentResult(
                    status=ResultStatus.TIMEOUT,
                    usage=self._timeout_usage(wall_ms),
                    log_path=log_path,
                    error="CLI invocation timed out during structured-output repair",
                )
            if repair.exception is not None or repair.returncode != 0:
                error = repair.exception or stderr_tail(repair.stderr)
                return AgentResult(
                    status=ResultStatus.ERROR,
                    usage=self._error_usage(wall_ms),
                    log_path=log_path,
                    error=error[-STDERR_TAIL_LENGTH:],
                )
            parsed = self._parse(repair.stdout)
            output_json, validation_error = self._validate_json(parsed.text, input.output_schema)
            if output_json is None:
                return AgentResult(
                    status=ResultStatus.ERROR,
                    output_text=parsed.text,
                    session_ref=parsed.session_ref,
                    usage=self._usage(input, parsed, wall_ms),
                    log_path=log_path,
                    error=validation_error,
                )
            return AgentResult(
                status=ResultStatus.OK,
                output_text=parsed.text,
                output_json=output_json,
                session_ref=parsed.session_ref,
                usage=self._usage(input, parsed, wall_ms),
                log_path=log_path,
            )
        except Exception as exc:  # Provider envelopes must never escape the adapter boundary.
            wall_ms = max(1, int((time.perf_counter() - started) * 1000))
            log_path = self._log(input.run_id, invocations)
            return AgentResult(
                status=ResultStatus.ERROR,
                usage=self._error_usage(wall_ms),
                log_path=log_path,
                error=str(exc)[-STDERR_TAIL_LENGTH:],
            )

    def cancel(self, session_ref: str) -> bool:
        """Request cancellation of an in-flight CLI process.

        Returns True only when a kill signal was delivered (or the process is
        already gone). A refused/failed kill is False — never a fake success.
        """
        proc = self._active.get(session_ref)
        if proc is None or proc.poll() is not None:
            return False
        return self._kill_process_group(proc)

    def health(self) -> HealthStatus:
        if self._health is not None:
            return self._health
        try:
            # SPAWN SITE 2: version probe with scrubbed env (F1/R2-12)
            probe = subprocess.run(
                [self.cli, "--version"],
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
                env=_scrubbed_env(),  # Apply env-scrub here
            )
            detail = (probe.stdout or probe.stderr).strip()[-STDERR_TAIL_LENGTH:]
            self._health = HealthStatus(
                healthy=probe.returncode == 0,
                detail=detail,
                capabilities={"live_runs": probe.returncode == 0},
            )
        except (OSError, subprocess.SubprocessError) as exc:
            self._health = HealthStatus(
                healthy=False, detail=str(exc), capabilities={"live_runs": False}
            )
        return self._health


def estimated_usage(input: AgentInput, output: str, wall_ms: int) -> AgentUsage:
    """Shared fallback for CLIs that omit some or all usage information."""

    return AgentUsage(
        wall_ms=wall_ms,
        turns=1,
        input_tokens=estimate_tokens(input.prompt),
        output_tokens=estimate_tokens(output),
        cost_usd=None,
        estimated=True,
        source="estimator",
    )
