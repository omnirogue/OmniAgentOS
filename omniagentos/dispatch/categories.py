"""Local semantic classifier for the swarm's category PINS (PKG-SEMANTIC-PINS).

The SwarmRouter's ``router.category_pins`` (configs/swarm.yaml) pin a discipline
(``web_research`` / ``adversarial_review`` / ``independent_critique`` /
``science_engineering``) to a specific model. That match is normally on an
EXPLICIT ``discipline`` field. This module gives those pins a SEMANTIC fallback:
``classify_category(text)`` returns the pin category a task's free text most
looks like, so a task whose TITLE/DESCRIPTION is clearly research- or
red-team-shaped gets pinned even when the field is unset.

It is a SEPARATE route group from the fast-dispatch lane gate
(:mod:`omniagentos.dispatch.gate`): the routes ARE the pin categories, seeded
from the ``category_routes:`` section of configs/dispatch.yaml. The build /
caching machinery is SHARED with the gate (``gate._cached_router_build`` +
``gate._build_semantic_router``) rather than re-derived -- this module only owns
its own storage cells (cache, lock, degrade flag) and its ``category_routes``
routes key.

Degradation posture mirrors the gate exactly: the semantic-router package is
imported lazily and ONLY inside the default factory; a missing package, missing
model, or any exception -> ``None`` (logged once, never raised). A route scoring
below ``thresholds.semantic_pin_confidence`` also returns ``None``. The whole
feature is a fallback -- it must NEVER break a route.
"""

from __future__ import annotations

import logging
import math
import threading
from collections.abc import Callable
from typing import Any

from omniagentos.dispatch import gate

LOG = logging.getLogger(__name__)

# The config section the pin-category routes are seeded from (separate from the
# lane gate's ``routes``); this is the ONLY route group this module builds.
_CATEGORY_ROUTES_KEY = "category_routes"

# A semantic verdict below this cosine score is not trusted -> no pin.
_DEFAULT_SEMANTIC_PIN_CONFIDENCE = 0.65

# Per-process singleton cache for the built category router, keyed by config
# signature (encoder + category_routes) so a changed route set rebuilds. The
# lifecycle algorithm is gate's shared ``_cached_router_build``; only these
# storage cells are this module's own -- kept separate from the gate's cache so
# the two route groups have independent lifecycles and reset independently.
_CATEGORY_ROUTER_CACHE: dict[str, Any] = {}
_CATEGORY_BUILD_LOCK = threading.Lock()
_DEGRADE_LOGGED = False


def _log_degrade_once(reason: str, exc: BaseException | None = None) -> None:
    global _DEGRADE_LOGGED
    if not _DEGRADE_LOGGED:
        _DEGRADE_LOGGED = True
        LOG.warning(
            "dispatch: semantic category classifier unavailable (%s); no semantic pin",
            reason,
        )
        if exc is not None:
            LOG.debug("dispatch: category classifier error", exc_info=exc)


def _build_category_router(config: dict[str, Any]) -> gate._SemanticRouter:
    """Build the category router from ``category_routes`` (no caching)."""
    return gate._build_semantic_router(config, routes_key=_CATEGORY_ROUTES_KEY, label="category")


def _default_router_factory(config: dict[str, Any]) -> gate._SemanticRouter:
    """Per-process cached category router build (failure TTL + build lock).

    Binds gate's shared ``_cached_router_build`` to THIS module's own cache /
    lock / degrade log and the ``category_routes``-keyed builder. The build
    function and clock are module globals looked up at call time so tests may
    monkeypatch them.
    """
    signature = gate._config_signature(config, routes_key=_CATEGORY_ROUTES_KEY)
    retry_seconds = gate._float_threshold(
        config, "gate1_retry_seconds", gate._DEFAULT_GATE1_RETRY_SECONDS
    )
    return gate._cached_router_build(
        config,
        cache=_CATEGORY_ROUTER_CACHE,
        lock=_CATEGORY_BUILD_LOCK,
        build_fn=_build_category_router,
        signature=signature,
        retry_seconds=retry_seconds,
        clock=gate._clock,
        degrade_log=_log_degrade_once,
    )


def _pin_threshold(config: dict[str, Any]) -> float:
    return gate._float_threshold(
        config, "semantic_pin_confidence", _DEFAULT_SEMANTIC_PIN_CONFIDENCE
    )


def classify_category(
    text: str,
    *,
    config: dict[str, Any] | None = None,
    router_factory: Callable[[dict[str, Any]], Any] | None = None,
) -> tuple[str, float] | None:
    """Classify ``text`` into a pin CATEGORY, or ``None`` when there is no
    confident semantic match.

    Returns ``(category, score)`` where ``category`` is one of the
    ``category_routes`` route names and ``score`` is the cosine similarity, only
    when ``score >= thresholds.semantic_pin_confidence`` (default
    ``0.65``). Every other outcome -- blank text, an unavailable semantic router,
    no route, a below-threshold score, or ANY exception -- returns ``None``.

    Injectable seams keep it hermetic: ``config`` defaults to
    configs/dispatch.yaml over baked-in defaults (via :func:`gate.load_config`);
    ``router_factory`` defaults to the lazy, cached category-router builder. NEVER
    raises.
    """
    try:
        if not text or not text.strip():
            return None
        cfg = config if config is not None else gate.load_config()
        threshold = _pin_threshold(cfg)
        factory = router_factory or _default_router_factory
        try:
            router = factory(cfg)
            name, score = router.classify(text)
        except Exception as exc:  # noqa: BLE001 -- degrade to no-pin, never raise.
            _log_degrade_once("router unavailable", exc)
            return None
        if name is None:
            return None
        try:
            score_f = float(score)
        except (TypeError, ValueError):
            return None
        # A non-finite score is never a confident match (finding F3): NaN makes
        # every comparison false, so ``score < threshold`` would let it through,
        # and +inf would spuriously pass the threshold. Require finite AND >=.
        if not (math.isfinite(score_f) and score_f >= threshold):
            # Not confident enough to pin -- the explicit-field path (or plain
            # ranking) stays in control.
            return None
        return str(name), score_f
    except Exception:  # noqa: BLE001 -- classify_category must NEVER break a route.
        LOG.debug("dispatch: classify_category degraded to None", exc_info=True)
        return None


__all__ = ["classify_category"]
