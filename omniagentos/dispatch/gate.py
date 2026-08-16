"""The three-gate FAST DISPATCH classifier.

``decide(brief)`` runs three gates in order and returns the FIRST confident
verdict as a :class:`GateDecision`:

* **Gate 0** -- pure functions, 0 ms. Risk patterns (payments/migrations/auth/
  delete/deploy) short-circuit to ``fallthrough`` with ``risk_flagged`` so risky
  work NEVER fast-lanes; sweep phrasing routes to ``swarm``; a short, single-
  artifact, complexity-simple brief routes to ``solo_fast``. No verdict -> Gate 1.
* **Gate 1** -- a local semantic router (semantic-router + FastEmbed, ~3 ms warm).
  Imported LAZILY inside the default factory ONLY: a missing package, missing
  model, or any exception degrades to Gate 2 (logged once, never raised). A route
  score below the configured threshold also falls to Gate 2.
* **Gate 2** -- a one-shot Haiku call (~1 s), used only when the cheaper gates were
  ambiguous. ``None``/invalid output -> ``fallthrough``.

Every path returns in-band. ``decide`` itself is exception-proof: any unforeseen
error inside it degrades to ``fallthrough`` carrying the error class in ``reason``
-- the classifier must NEVER break a dispatch (same posture as the swarm learner).

The whole feature sits behind ``OMNIAGENTOS_FAST_DISPATCH`` at the call site
(:mod:`omniagentos.intake.service`); this module is pure and side-effect free
apart from the per-process router singleton cache.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import re
import threading
import time
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

LOG = logging.getLogger(__name__)

# The five terminal decisions. ``fallthrough`` == today's heavyweight inline-Fable
# planning path (unchanged); the other four are fast-lane routes.
_DECISIONS = ("solo_fast", "solo_standard", "solo_complex", "swarm", "fallthrough")

# Semantic route name -> (decision, risk_flagged). ``risk`` mirrors Gate 0a.
_ROUTE_TO_DECISION: dict[str, tuple[str, bool]] = {
    "solo_fast": ("solo_fast", False),
    "solo_standard": ("solo_standard", False),
    "solo_complex": ("solo_complex", False),
    "swarm": ("swarm", False),
    "risk": ("fallthrough", True),
}


@dataclass(frozen=True)
class GateDecision:
    """The outcome of one :func:`decide` call.

    ``decision`` is one of :data:`_DECISIONS`. ``gate`` names which gate produced
    it (``rules``/``semantic``/``llm``/``fallthrough``). ``speed_hint`` is threaded
    into the swarm dispatch's ``speed`` seam when set. ``applied`` (whether routing
    actually changed) is decided by the CALLER, not here.
    """

    decision: str
    risk_flagged: bool = False
    speed_hint: str | None = None
    confidence: float = 0.0
    gate: str = "fallthrough"
    latency_ms: float = 0.0
    reason: str = ""


# ---------------------------------------------------------------------------
# Config (best-effort YAML over hard-coded defaults)
# ---------------------------------------------------------------------------

_DEFAULT_RISK_PATTERNS = (
    # Payments / money movement, incl. credit-card collection.
    r"\b(payment|payments|stripe|paypal|billing|invoice|charge|payout|refund|"
    r"checkout|credit ?cards?)\b",
    # Money webhooks.
    r"\bweb ?hook\b.*\b(pay|money|charge|billing|stripe)\b",
    # Schema / migration structure changes. Bare `drop` is NOT risk (it hits
    # "drop shadow"); only drop <table|column|database|schema|index>.
    r"\b(migration|migrate|schema change|alter table|"
    r"drop (table|column|database|schema|index))\b",
    # Database / data-model structure or version changes ("update the users
    # database to v2", "restructure the schema").
    r"\b(update|change|migrate|alter|modify|upgrade|move|rename|revamp|"
    r"restructure)\b[^.]*\b(database|schema|data ?model)\b",
    r"\bdatabase\b[^.]*\b(to v\d|version|schema|migration)\b",
    # Auth surfaces: authn/authz, SSO, login/signup, keys/secrets/passwords.
    r"\b(auth|authentication|authorization|oauth|sso|single sign-?on|login|"
    r"log in|sign-?up|sign-?in|api ?keys?|secrets?|credentials?|password|"
    r"permissions?|rbac|acl)\b",
    # Tokens ONLY in a security context (never bare "design token").
    r"\b(api|auth|access|oauth|bearer|refresh|csrf|session) ?tokens?\b",
    # Bulk-destructive removal of records/data/users.
    r"\b(remove|delete|drop|wipe|purge|clear|erase)\s+(all|every)\b",
    r"\b(delete|truncate|wipe|purge|destroy|erase)\b",
    # Deploy / production / live-site release.
    r"\b(deploy|deployment|production|prod release|rollout|ship to prod|go live)\b",
    r"\b(live|production|prod) (site|server|environment|env|deployment|"
    r"release|build)\b",
)

_DEFAULT_SWEEP_PATTERNS = (
    r"\bevery \w+",
    r"\ball (the|of)\b",
    r"\beach (of|one)\b",
    r"\baudit all\b",
    r"\bacross (all|every|the (whole|entire))\b",
    r"\b(repo-wide|repo wide|codebase-wide|fleet-wide|system-wide)\b",
    r"\bfor all \w+",
    r"\bthroughout the (codebase|repo|project)\b",
)

_DEFAULT_SEMANTIC_CONFIDENCE = 0.72
_DEFAULT_SOLO_FAST_WORD_CAP = 40
_DEFAULT_ENCODER = "BAAI/bge-small-en-v1.5"
# Gate 1: how long a build FAILURE poisons the singleton before a retry is
# allowed (a transient model-download failure must not disable the gate until
# process restart). Gate 2: the minimum confidence a Haiku verdict must carry to
# be trusted -- below it, fall through to today's heavyweight path.
_DEFAULT_GATE1_RETRY_SECONDS = 300.0
_DEFAULT_GATE2_MIN_CONFIDENCE = 0.5

# Bounded single-artifact verbs that mark a candidate solo_fast brief. Risk verbs
# (delete/drop/migrate) are stripped by Gate 0a first, so they never appear here.
_SOLO_FAST_VERBS = frozenset(
    {
        "fix",
        "rename",
        "add",
        "update",
        "create",
        "tweak",
        "remove",
        "adjust",
        "correct",
        "bump",
        "change",
        "set",
        "edit",
        "append",
    }
)

# Action verbs used to count independent deliverables for the sweep heuristic.
_ACTION_VERBS = frozenset(
    _SOLO_FAST_VERBS
    | {
        "build",
        "write",
        "implement",
        "refactor",
        "port",
        "convert",
        "test",
        "document",
        "review",
        "audit",
        "design",
        "wire",
        "replace",
        "upgrade",
        "migrate",
        "split",
        "integrate",
    }
)

_SEGMENT_SPLIT = re.compile(r"[,;\n]| and | then | plus ", re.IGNORECASE)
_BULLET = re.compile(r"(?m)^\s*(?:[-*•]|\d+[.)])\s+")


def _config_path() -> Path:
    import os

    override = os.environ.get("OMNIAGENTOS_DISPATCH_CONFIG")
    if override:
        return Path(override)
    # omniagentos/dispatch/gate.py -> repo root -> configs/dispatch.yaml
    return Path(__file__).resolve().parents[2] / "configs" / "dispatch.yaml"


def load_config(path: Path | None = None) -> dict[str, Any]:
    """configs/dispatch.yaml as a plain dict; missing/broken -> ``{}``.

    Best-effort by design (limit_state idiom): a fresh checkout or a broken file
    must leave the classifier working on the hard-coded defaults below.
    """
    try:
        raw = yaml.safe_load((path or _config_path()).read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError):
        return {}
    return raw if isinstance(raw, dict) else {}


def _thresholds(config: dict[str, Any]) -> tuple[float, int]:
    section_raw = config.get("thresholds")
    section = section_raw if isinstance(section_raw, dict) else {}
    # Gate 1's floor goes through the SAME finite-checked reader as Gate 2's
    # (``gate2_min_confidence``): ``float("nan")`` raises nothing, and a NaN
    # floor makes ``score < threshold`` False for EVERY score -- i.e. a config
    # typo switches the confidence floor off rather than tightening it.
    semantic = _float_threshold(config, "semantic_confidence", _DEFAULT_SEMANTIC_CONFIDENCE)
    try:
        word_cap = int(section.get("solo_fast_word_cap", _DEFAULT_SOLO_FAST_WORD_CAP))
    except (TypeError, ValueError):
        word_cap = _DEFAULT_SOLO_FAST_WORD_CAP
    return semantic, word_cap


def _float_threshold(config: dict[str, Any], key: str, default: float) -> float:
    section_raw = config.get("thresholds")
    section = section_raw if isinstance(section_raw, dict) else {}
    try:
        value = float(section.get(key, default))
    except (TypeError, ValueError):
        return default
    return value if math.isfinite(value) else default


def _patterns(config: dict[str, Any], key: str, default: tuple[str, ...]) -> list[re.Pattern[str]]:
    raw = config.get(key)
    source = raw if isinstance(raw, list) and raw else list(default)
    compiled: list[re.Pattern[str]] = []
    for entry in source:
        try:
            compiled.append(re.compile(str(entry), re.IGNORECASE))
        except re.error:
            LOG.debug("dispatch: skipping bad %s pattern %r", key, entry, exc_info=True)
    return compiled


# ---------------------------------------------------------------------------
# Gate 0 -- pure functions
# ---------------------------------------------------------------------------


def _estimate_complexity(brief: str) -> str:
    """Reuse intake's no-LLM heuristic (Gate-0 signal). Never raises."""
    try:
        from omniagentos.intake.planner import estimate_complexity

        return str(estimate_complexity(brief)).strip().lower()
    except Exception:  # noqa: BLE001 -- a heuristic fault biases UP (never fast-lane).
        LOG.debug("dispatch: estimate_complexity failed", exc_info=True)
        return "complex"


def _deliverable_count(brief: str) -> int:
    """Approximate the number of DISTINCT independent deliverables in ``brief``.

    Bullet/numbered list markers count directly; otherwise segments split on
    conjunctions/commas that carry an action verb count as one deliverable each.

    Segments that repeat or overlap are merged: the full spec text is
    ``title + description + acceptance_criteria``, and a title restated in the
    description (or an acceptance line echoing the ask) is the SAME deliverable,
    not two -- without this, an ordinary single-artifact spec looks like a swarm.
    """
    bullets = len(_BULLET.findall(brief))
    if bullets >= 1:
        return bullets
    distinct: list[str] = []
    for segment in _SEGMENT_SPLIT.split(brief):
        norm = " ".join(segment.lower().split()).strip(" .:;-")
        if not norm:
            continue
        if not (set(norm.split()) & _ACTION_VERBS):
            continue
        # Merge with any kept segment one contains the other (title echoed in the
        # description, a summary line repeated) -- same deliverable.
        if any(norm in kept or kept in norm for kept in distinct):
            continue
        distinct.append(norm)
    return len(distinct)


def _normalize_risk_text(text: str) -> tuple[str, str]:
    """Fold obfuscation before scanning (B4): NFKC (full-width/compatibility
    forms → ASCII), drop Unicode ``Cf`` format chars (zero-width/joiners),
    casefold, then produce two views — ``spaced`` (whitespace runs collapsed to
    single spaces, preserving word boundaries for ``\\s+`` patterns) and
    ``tight`` (ALL whitespace removed, so a keyword split across a newline/space
    is reunited)."""
    normalized = unicodedata.normalize("NFKC", text)
    normalized = "".join(ch for ch in normalized if unicodedata.category(ch) != "Cf")
    normalized = normalized.casefold()
    parts = normalized.split()
    return " ".join(parts), "".join(parts)


def _risk_pattern_sources(config: dict[str, Any]) -> list[str]:
    raw = config.get("risk_patterns")
    return (
        [str(entry) for entry in raw]
        if isinstance(raw, list) and raw
        else list(_DEFAULT_RISK_PATTERNS)
    )


def _strict_risk_patterns(config: dict[str, Any]) -> list[re.Pattern[str]]:
    """Compile the risk patterns for the STRICT scan: a bad regex RAISES (so an
    all-invalid configured ``risk_patterns`` cannot silently degrade to an empty
    list that matches nothing — fail CLOSED, B4). An empty result also raises."""
    compiled = [re.compile(src, re.IGNORECASE) for src in _risk_pattern_sources(config)]
    if not compiled:
        raise re.error("no risk patterns to scan against")
    return compiled


def _loose_risk_patterns(config: dict[str, Any]) -> list[re.Pattern[str]]:
    """De-anchored variants for the ``tight`` (whitespace-stripped) scan: drop
    ``\\b`` word boundaries and relax mandatory whitespace (``\\s+`` and literal
    spaces → ``\\s*``) so a keyword split mid-word (``del\\nete all users`` →
    ``deleteallusers``) still matches. Best-effort — a variant that fails to
    compile is skipped; this layer only ever ADDS matches (fail CLOSED)."""
    loose: list[re.Pattern[str]] = []
    for src in _risk_pattern_sources(config):
        relaxed = src.replace(r"\b", "").replace(r"\s+", r"\s*").replace(" ", r"\s*")
        try:
            loose.append(re.compile(relaxed, re.IGNORECASE))
        except re.error:
            LOG.debug("dispatch: skipping un-loosenable risk pattern %r", src, exc_info=True)
    return loose


def text_hits_risk(
    text: str, config: dict[str, Any] | None = None, *, strict: bool = False
) -> bool:
    """True if ``text`` matches any risk pattern (payments / migrations / auth /
    delete / deploy) — the Gate-0a scan, exposed as a standalone predicate so
    other subsystems can reuse the SAME risk vocabulary (e.g. the swarm
    coordinator vetting a worker's subtasks_request.json).

    ``strict`` (B4, for security guards that must fail CLOSED): pattern
    compilation failures RAISE rather than being silently dropped, so an
    all-invalid configured ``risk_patterns`` denies instead of matching nothing;
    and the scanned text is obfuscation-normalized (NFKC, Cf-strip, casefold,
    whitespace folding) with a de-anchored pass over the whitespace-stripped
    view to catch split-keyword evasions. The default (lenient) path keeps the
    lane classifier's behavior unchanged."""
    if not text:
        return False
    cfg = config if config is not None else load_config()
    if not strict:
        return any(
            pattern.search(text) is not None
            for pattern in _patterns(cfg, "risk_patterns", _DEFAULT_RISK_PATTERNS)
        )
    spaced, tight = _normalize_risk_text(text)
    if any(pattern.search(spaced) is not None for pattern in _strict_risk_patterns(cfg)):
        return True
    return any(pattern.search(tight) is not None for pattern in _loose_risk_patterns(cfg))


def _gate0(
    brief: str,
    risk_patterns: list[re.Pattern[str]],
    sweep_patterns: list[re.Pattern[str]],
    word_cap: int,
) -> GateDecision | None:
    text = brief.strip()
    if not text:
        # An empty brief is nothing the fast lane can bound -- let the real path see it.
        return GateDecision(
            decision="fallthrough",
            gate="rules",
            confidence=1.0,
            reason="empty brief",
        )

    # (a) RISK -- highest precedence; risky work never fast-lanes.
    for pattern in risk_patterns:
        match = pattern.search(text)
        if match is not None:
            return GateDecision(
                decision="fallthrough",
                risk_flagged=True,
                gate="rules",
                confidence=1.0,
                reason=f"risk pattern matched: {match.group(0)!r}",
            )

    deliverables = _deliverable_count(text)

    # (b) SWARM -- sweep phrasing or >=3 independent deliverables.
    sweep_hit = next((p.group(0) for p in (m.search(text) for m in sweep_patterns) if p), None)
    if sweep_hit is not None or deliverables >= 3:
        complexity = _estimate_complexity(text)
        speed_hint = "fast" if complexity == "simple" else None
        reason = (
            f"sweep phrasing matched: {sweep_hit!r}"
            if sweep_hit is not None
            else f"{deliverables} independent deliverables"
        )
        return GateDecision(
            decision="swarm",
            gate="rules",
            confidence=0.9,
            speed_hint=speed_hint,
            reason=reason,
        )

    # (c) SOLO-FAST -- short, single bounded artifact, complexity simple.
    words = text.split()
    first_word = words[0].strip(".:").lower() if words else ""
    has_bounded_verb = bool({w.strip(".:").lower() for w in words} & _SOLO_FAST_VERBS)
    if (
        len(words) <= word_cap
        and deliverables <= 1
        and (first_word in _SOLO_FAST_VERBS or has_bounded_verb)
        and _estimate_complexity(text) == "simple"
    ):
        return GateDecision(
            decision="solo_fast",
            gate="rules",
            confidence=0.85,
            reason=f"short single-artifact brief ({len(words)} words)",
        )

    # (d) No rule verdict -> Gate 1.
    return None


# ---------------------------------------------------------------------------
# Gate 1 -- local semantic router (lazy, degrades to Gate 2)
# ---------------------------------------------------------------------------

# Per-process singleton cache for the built router, keyed by config signature so a
# changed encoder/route set rebuilds. A value is either a built ``_SemanticRouter``
# (permanent -- a success is NEVER overwritten by a later failure) or a
# ``_RouterBuildFailure`` (retryable after a TTL, so a transient model-download
# failure does not poison the process until restart). ``_BUILD_LOCK`` serialises
# concurrent first-calls so two threads can't race the expensive build.
_ROUTER_CACHE: dict[str, Any] = {}
_BUILD_LOCK = threading.Lock()
_DEGRADE_LOGGED = False


class _RouterBuildFailure:
    """A cached build failure, stamped so it becomes retryable after a TTL."""

    __slots__ = ("at",)

    def __init__(self, at: float) -> None:
        self.at = at


def _clock() -> float:
    """Monotonic wall clock for the failure TTL (injectable in tests)."""
    return time.monotonic()


def _log_degrade_once(reason: str, exc: BaseException | None = None) -> None:
    global _DEGRADE_LOGGED
    if not _DEGRADE_LOGGED:
        _DEGRADE_LOGGED = True
        LOG.warning("dispatch: semantic gate unavailable (%s); degrading to Gate 2", reason)
        if exc is not None:
            LOG.debug("dispatch: semantic gate error", exc_info=exc)


def _config_signature(config: dict[str, Any], routes_key: str = "routes") -> str:
    encoder = str(config.get("encoder", _DEFAULT_ENCODER))
    routes = config.get(routes_key) if isinstance(config.get(routes_key), dict) else {}
    blob = json.dumps(
        {"encoder": encoder, "routes_key": routes_key, "routes": routes},
        sort_keys=True,
        default=str,
    )
    return hashlib.sha1(blob.encode("utf-8")).hexdigest()


class _SemanticRouter:
    """Thin wrapper exposing ``classify(brief) -> (route_name, score)``.

    Encapsulates every semantic-router API specific so :func:`decide` stays
    decoupled from the package (and so tests inject fakes without importing it).
    """

    def __init__(self, router: Any) -> None:
        self._router = router

    def classify(self, brief: str) -> tuple[str | None, float]:
        choice = self._router(brief)
        name = getattr(choice, "name", None)
        score = getattr(choice, "similarity_score", None)
        if score is None:
            score = getattr(choice, "score", None)
        try:
            score_f = float(score) if score is not None else 0.0
        except (TypeError, ValueError):
            score_f = 0.0
        return (str(name) if name else None), score_f


# One FastEmbedEncoder per (process, encoder name), SHARED across route groups.
# The lane gate and the category classifier keep ISOLATED route caches / failure
# sentinels (a build failure in one group must not poison the other), but the
# encoder is heavy (~2 s warmup, hundreds of MB) and identical across groups, so
# building it once and reusing the object halves both the warmup cost and memory.
_ENCODER_CACHE: dict[str, Any] = {}
_ENCODER_LOCK = threading.Lock()


def _make_encoder(name: str) -> Any:
    """Construct a FastEmbedEncoder (injectable seam; lazy import)."""
    from semantic_router.encoders import FastEmbedEncoder  # type: ignore[import-not-found]

    return FastEmbedEncoder(name=name)


def _shared_encoder(name: str) -> Any:
    """The process-wide encoder for ``name``, built at most once across BOTH route
    groups (finding F4). Route caches stay per-group; only this encoder is shared."""
    cached = _ENCODER_CACHE.get(name)
    if cached is not None:
        return cached
    with _ENCODER_LOCK:
        cached = _ENCODER_CACHE.get(name)
        if cached is not None:
            return cached
        encoder = _make_encoder(name)
        _ENCODER_CACHE[name] = encoder
        return encoder


def _build_semantic_router(
    config: dict[str, Any],
    routes_key: str = "routes",
    label: str = "semantic",
) -> _SemanticRouter:
    """Actually build a semantic router (no caching). Injectable in tests.

    Imports semantic-router LAZILY -- this is the ONLY place the package is
    touched. The first successful build pays the encoder's cold load (~2 s warmup
    on this machine); warm classify calls are ~3-10 ms. The encoder itself is
    shared process-wide via :func:`_shared_encoder`, so a SECOND route group
    (e.g. the category classifier) reuses the already-warm encoder rather than
    paying the warmup again. ``routes_key`` selects the config section the routes
    are seeded from (``routes`` for the fast-dispatch lane gate, ``category_routes``
    for the swarm semantic-pin classifier), so the same builder serves both route
    groups.
    """
    from semantic_router import Route  # type: ignore[import-not-found]
    from semantic_router.routers import SemanticRouter  # type: ignore[import-not-found]

    encoder_name = str(config.get("encoder", _DEFAULT_ENCODER))
    routes_raw = config.get(routes_key)
    routes_cfg = routes_raw if isinstance(routes_raw, dict) else {}
    routes = [
        Route(name=name, utterances=list(utterances))
        for name, utterances in routes_cfg.items()
        if isinstance(utterances, list) and utterances
    ]
    if not routes:
        raise RuntimeError(f"no routes configured for {label} router")
    t0 = time.perf_counter()
    encoder = _shared_encoder(encoder_name)
    router = SemanticRouter(encoder=encoder, routes=routes, auto_sync="local")
    LOG.info(
        "dispatch: %s router built (%d routes, encoder=%s) in %.1fs "
        "(first call per process pays the encoder warmup; warm classify ~3-10ms)",
        label,
        len(routes),
        encoder_name,
        time.perf_counter() - t0,
    )
    return _SemanticRouter(router)


def _cached_router_build(
    config: dict[str, Any],
    *,
    cache: dict[str, Any],
    lock: threading.Lock,
    build_fn: Callable[[dict[str, Any]], _SemanticRouter],
    signature: str,
    retry_seconds: float,
    clock: Callable[[], float],
    degrade_log: Callable[[str, BaseException | None], None],
) -> _SemanticRouter:
    """The shared per-process cached-build algorithm (failure TTL + build lock).

    Route groups differ only in their storage cells and build function; the
    lifecycle is identical, so both the fast-dispatch lane gate and the swarm
    semantic-pin classifier drive this ONE implementation rather than each
    re-deriving the singleton/TTL/lock pattern:

    * A successful ``_SemanticRouter`` is cached permanently and is NEVER
      overwritten by a later failure (both the fast path and the locked re-check
      return it before any rebuild is attempted).
    * A build FAILURE is cached with a timestamp and re-raised (the caller
      degrades). Within ``retry_seconds`` a subsequent call re-raises without
      rebuilding; after the TTL elapses the next call retries the build (a
      transient model-download failure self-heals).
    * ``lock`` serialises concurrent first-calls so the expensive build runs at
      most once per signature.
    """
    # Fast path: an already-built router is returned lock-free.
    cached = cache.get(signature)
    if isinstance(cached, _SemanticRouter):
        return cached

    with lock:
        # Re-check under the lock: another thread may have built (or failed) while
        # we waited. A success here is authoritative and never rebuilt.
        cached = cache.get(signature)
        if isinstance(cached, _SemanticRouter):
            return cached
        if isinstance(cached, _RouterBuildFailure):
            if (clock() - cached.at) < retry_seconds:
                raise RuntimeError("semantic router build failed within retry TTL")
            # TTL elapsed -> fall through and retry the build once more.
        try:
            wrapper = build_fn(config)
        except BaseException as exc:  # noqa: BLE001 -- degrade, never crash a route.
            cache[signature] = _RouterBuildFailure(clock())
            degrade_log("router build failed", exc)
            raise
        cache[signature] = wrapper
        return wrapper


def _default_router_factory(config: dict[str, Any]) -> _SemanticRouter:
    """Per-process cached router build for the fast-dispatch lane gate.

    A thin binding of :func:`_cached_router_build` to the lane gate's own cache,
    lock, and (``routes``-keyed) builder. The build function and clock are looked
    up as module globals at call time so tests may monkeypatch them.
    """
    signature = _config_signature(config)
    retry_seconds = _float_threshold(config, "gate1_retry_seconds", _DEFAULT_GATE1_RETRY_SECONDS)
    return _cached_router_build(
        config,
        cache=_ROUTER_CACHE,
        lock=_BUILD_LOCK,
        build_fn=_build_semantic_router,
        signature=signature,
        retry_seconds=retry_seconds,
        clock=_clock,
        degrade_log=_log_degrade_once,
    )


def _gate1(
    brief: str,
    config: dict[str, Any],
    router_factory: Callable[[dict[str, Any]], Any] | None,
    threshold: float,
) -> GateDecision | None:
    """Return a semantic verdict, or ``None`` to fall through to Gate 2.

    Any exception (missing package, build error, classify error) degrades to
    Gate 2 -- logged once, never raised.
    """
    factory = router_factory or _default_router_factory
    try:
        router = factory(config)
        name, score = router.classify(brief)
    except Exception as exc:  # noqa: BLE001 -- degrade to Gate 2, never break a route.
        _log_degrade_once("router unavailable", exc)
        return None

    if name is None:
        return None
    mapped = _ROUTE_TO_DECISION.get(name)
    if mapped is None:
        LOG.debug("dispatch: unknown semantic route %r -> Gate 2", name)
        return None
    # A non-finite score is never a confident verdict (the pin classifier's
    # finding F3, categories.py): NaN makes every comparison false, so a bare
    # ``score < threshold`` would LET IT THROUGH, and +inf would spuriously
    # clear any finite floor. Require finite AND >=.
    if not (math.isfinite(score) and score >= threshold):
        # Not confident enough -> let Gate 2 (Haiku) break the tie.
        return None
    decision, risk_flagged = mapped
    return GateDecision(
        decision=decision,
        risk_flagged=risk_flagged,
        gate="semantic",
        confidence=score,
        reason=f"semantic route {name!r} @ {score:.3f}",
    )


# ---------------------------------------------------------------------------
# Gate 2 -- one-shot Haiku, only on ambiguity
# ---------------------------------------------------------------------------

_LLM_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["decision", "confidence", "reason"],
    "properties": {
        "decision": {"type": "string", "enum": list(_DECISIONS)},
        "confidence": {"type": "number"},
        "reason": {"type": "string"},
    },
}

_LLM_PROMPT = """You are a fast dispatch classifier for a coding agent platform.
Classify the task brief below into exactly one routing decision:

- solo_fast: a tiny, bounded edit -- one file, one small change (typo, rename, add a constant).
- solo_standard: a bounded feature/endpoint/test that one agent finishes in one pass.
- solo_complex: one hard task for one agent -- debugging a race, refactoring a subsystem, designing a component.
- swarm: many independent deliverables -- a repo-wide audit, a multi-component build, an N-service migration.
- fallthrough: anything risky (payments, migrations, auth/secrets, deletes, deploys) or too ambiguous to bound.

Respond with ONLY the JSON object: {"decision": ..., "confidence": 0..1, "reason": "<one short line>"}.

Task brief:
{brief}
"""


def _default_llm_fn(brief: str) -> dict[str, Any] | None:
    from omniagentos.contracts import HarnessType
    from omniagentos.intake.fallback import run_with_fallback

    # Explicit haiku rung: run_fable_json's `model` arg is IGNORED whenever the
    # env fallback chain is enabled (fallback.py: "kept for API compat"), which
    # silently served this call with fable at ~4.4s — the exact latency Gate 2
    # exists to avoid. The per-call chain override is the documented control.
    return run_with_fallback(
        _LLM_PROMPT.replace("{brief}", brief),
        _LLM_SCHEMA,
        effort="low",
        max_turns=1,
        wall_ms=15000,
        chain=[("haiku", HarnessType.CLI_CLAUDE, "haiku")],
    )


def _validated_confidence(raw: Any) -> float | None:
    """Return a finite confidence in [0, 1], or ``None`` if it must be rejected.

    Absent/non-numeric/NaN/inf/negative -> ``None`` (the caller falls through).
    A value above 1.0 is clamped to 1.0 (an over-eager model is trusted, not
    rejected)."""
    if raw is None:
        return None
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(value) or value < 0.0:
        return None
    return min(value, 1.0)


def _gate2(
    brief: str,
    llm_fn: Callable[[str], dict[str, Any] | None] | None,
    min_confidence: float,
) -> GateDecision:
    fn = llm_fn or _default_llm_fn
    try:
        result = fn(brief)
    except Exception as exc:  # noqa: BLE001 -- an unavailable model must degrade, not crash.
        LOG.debug("dispatch: Gate 2 llm_fn raised", exc_info=True)
        return GateDecision(
            decision="fallthrough",
            gate="fallthrough",
            reason=f"gate2 error: {type(exc).__name__}",
        )
    if not isinstance(result, dict):
        return GateDecision(decision="fallthrough", gate="fallthrough", reason="gate2 no result")
    decision = str(result.get("decision", "")).strip().lower()
    if decision not in _DECISIONS:
        return GateDecision(
            decision="fallthrough",
            gate="fallthrough",
            reason=f"gate2 invalid decision {decision!r}",
        )
    confidence = _validated_confidence(result.get("confidence"))
    if confidence is None:
        return GateDecision(
            decision="fallthrough",
            gate="fallthrough",
            reason=f"gate2 non-finite confidence {result.get('confidence')!r}",
        )
    if confidence < min_confidence:
        # A low-confidence verdict is not trusted -> today's heavyweight path.
        return GateDecision(
            decision="fallthrough",
            gate="fallthrough",
            confidence=confidence,
            reason=f"gate2 confidence {confidence:.2f} < floor {min_confidence:.2f}",
        )
    reason = str(result.get("reason", "")).strip()[:200] or "gate2 verdict"
    return GateDecision(
        decision=decision,
        gate="llm",
        confidence=confidence,
        reason=reason,
    )


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def decide(
    brief: str,
    *,
    config: dict[str, Any] | None = None,
    router_factory: Callable[[dict[str, Any]], Any] | None = None,
    llm_fn: Callable[[str], dict[str, Any] | None] | None = None,
    now: Callable[[], float] | None = None,
) -> GateDecision:
    """Classify ``brief`` through the three gates and return a :class:`GateDecision`.

    Injectable seams keep the whole thing hermetic: ``config`` (defaults to
    configs/dispatch.yaml over baked-in defaults), ``router_factory`` (defaults to
    the lazy semantic-router builder), ``llm_fn`` (defaults to a Haiku call), and
    ``now`` (a monotonic clock, defaults to ``time.perf_counter``).

    NEVER raises: any unforeseen error degrades to ``fallthrough`` with the error
    class in ``reason``.
    """
    clock = now or time.perf_counter
    start = clock()

    def _stamp(decision: GateDecision) -> GateDecision:
        from dataclasses import replace

        return replace(decision, latency_ms=max(0.0, (clock() - start) * 1000.0))

    try:
        cfg = config if config is not None else load_config()
        threshold, word_cap = _thresholds(cfg)
        risk_patterns = _patterns(cfg, "risk_patterns", _DEFAULT_RISK_PATTERNS)
        sweep_patterns = _patterns(cfg, "sweep_patterns", _DEFAULT_SWEEP_PATTERNS)

        gate0 = _gate0(brief, risk_patterns, sweep_patterns, word_cap)
        if gate0 is not None:
            return _stamp(gate0)

        gate1 = _gate1(brief, cfg, router_factory, threshold)
        if gate1 is not None:
            return _stamp(gate1)

        min_confidence = _float_threshold(
            cfg, "gate2_min_confidence", _DEFAULT_GATE2_MIN_CONFIDENCE
        )
        return _stamp(_gate2(brief, llm_fn, min_confidence))
    except Exception as exc:  # noqa: BLE001 -- decide() must never break a dispatch.
        LOG.warning("dispatch: decide() degraded to fallthrough (%s)", type(exc).__name__)
        LOG.debug("dispatch: decide() error", exc_info=True)
        return _stamp(
            GateDecision(
                decision="fallthrough",
                gate="fallthrough",
                reason=f"decide error: {type(exc).__name__}",
            )
        )


__all__ = ["GateDecision", "decide", "load_config", "text_hits_risk"]
