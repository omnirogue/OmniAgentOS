"""Hermetic tests for the semantic pin-category classifier (PKG-SEMANTIC-PINS).

Every semantic router is an INJECTED fake -- the real ``semantic_router`` is
never imported (it may be absent in CI, exactly like the dev venv). The shared
build/cache machinery lives in :mod:`omniagentos.dispatch.gate`; the wiring test
monkeypatches ``gate._build_semantic_router`` to prove the classifier drives it
with the ``category_routes`` route group (no real model, no real package).
"""

from __future__ import annotations

from typing import Any

import pytest

from omniagentos.dispatch import categories as cat
from omniagentos.dispatch import gate
from omniagentos.dispatch.categories import classify_category

# ---------------------------------------------------------------------------
# Singleton reset (categories owns its OWN cache + degrade flag; the dispatch
# conftest already resets the gate's cells)
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_category_singletons() -> None:
    cat._CATEGORY_ROUTER_CACHE.clear()
    cat._DEGRADE_LOGGED = False
    gate._ROUTER_CACHE.clear()
    gate._ENCODER_CACHE.clear()
    yield
    cat._CATEGORY_ROUTER_CACHE.clear()
    cat._DEGRADE_LOGGED = False
    gate._ROUTER_CACHE.clear()
    gate._ENCODER_CACHE.clear()


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _FakeRouter:
    def __init__(self, name: str | None, score: float) -> None:
        self._name = name
        self._score = score

    def classify(self, text: str) -> tuple[str | None, float]:
        return self._name, self._score


def _factory(name: str | None, score: float):
    return lambda _config: _FakeRouter(name, score)


def _raising_factory(exc: Exception):
    def _factory_fn(_config: dict[str, Any]) -> Any:
        raise exc

    return _factory_fn


# ---------------------------------------------------------------------------
# Mapping + threshold
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "category",
    ["web_research", "adversarial_review", "independent_critique", "science_engineering"],
)
def test_confident_route_returns_category_and_score(category: str) -> None:
    result = classify_category(
        "tear this design apart",
        config={},
        router_factory=_factory(category, 0.90),
    )
    assert result == (category, pytest.approx(0.90))


def test_below_default_threshold_returns_none() -> None:
    # Default semantic_pin_confidence is 0.65; 0.50 is not trusted -> no pin.
    result = classify_category(
        "look up the latest news",
        config={},
        router_factory=_factory("web_research", 0.50),
    )
    assert result is None


def test_exactly_at_threshold_is_trusted() -> None:
    # >= threshold is a pin (the boundary is inclusive).
    result = classify_category(
        "review this proposal",
        config={"thresholds": {"semantic_pin_confidence": 0.65}},
        router_factory=_factory("independent_critique", 0.65),
    )
    assert result == ("independent_critique", pytest.approx(0.65))


def test_custom_threshold_from_config() -> None:
    cfg = {"thresholds": {"semantic_pin_confidence": 0.80}}
    # 0.70 < 0.80 -> None.
    assert (
        classify_category("critique this", config=cfg, router_factory=_factory("x", 0.70)) is None
    )
    # 0.85 >= 0.80 -> pinned.
    assert classify_category(
        "critique this", config=cfg, router_factory=_factory("independent_critique", 0.85)
    ) == ("independent_critique", pytest.approx(0.85))


def test_malformed_threshold_falls_back_to_default() -> None:
    # A non-numeric threshold must not crash; the 0.65 default applies.
    cfg = {"thresholds": {"semantic_pin_confidence": "high"}}
    assert classify_category(
        "research recent news", config=cfg, router_factory=_factory("web_research", 0.66)
    ) == ("web_research", pytest.approx(0.66))
    assert (
        classify_category(
            "research recent news", config=cfg, router_factory=_factory("web_research", 0.64)
        )
        is None
    )


# ---------------------------------------------------------------------------
# Degradation -- any failure yields None, never raises
# ---------------------------------------------------------------------------


def test_no_route_returns_none() -> None:
    assert (
        classify_category("ambiguous text", config={}, router_factory=_factory(None, 0.0)) is None
    )


def test_factory_raises_degrades_to_none() -> None:
    assert (
        classify_category(
            "anything",
            config={},
            router_factory=_raising_factory(ImportError("no semantic_router")),
        )
        is None
    )


def test_classify_raises_degrades_to_none() -> None:
    class _BoomRouter:
        def classify(self, _text: str) -> tuple[str | None, float]:
            raise RuntimeError("classify exploded")

    assert classify_category("anything", config={}, router_factory=lambda _c: _BoomRouter()) is None


def test_blank_text_returns_none_without_building() -> None:
    calls: list[str] = []

    def _spy_factory(_config: dict[str, Any]) -> Any:
        calls.append("built")
        return _FakeRouter("web_research", 0.99)

    assert classify_category("   ", config={}, router_factory=_spy_factory) is None
    assert calls == []  # blank text short-circuits before any build


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf")])
def test_non_finite_score_returns_none(bad: float) -> None:
    """A non-finite score is never a confident match (finding F3): NaN makes
    ``score < threshold`` false (would leak through) and +inf would spuriously
    pass. All of NaN / +inf / -inf must degrade to None."""
    assert (
        classify_category("text", config={}, router_factory=_factory("web_research", bad)) is None
    )


def test_non_numeric_score_returns_none() -> None:
    assert (
        classify_category(
            "text",
            config={},
            router_factory=_factory("web_research", "high"),  # type: ignore[arg-type]
        )
        is None
    )


def test_never_raises_on_broken_config() -> None:
    class _Explode(dict):
        def get(self, *_a: Any, **_k: Any) -> Any:  # type: ignore[override]
            raise RuntimeError("config is on fire")

    assert (
        classify_category("text", config=_Explode(), router_factory=_factory("web_research", 0.9))
        is None
    )


# ---------------------------------------------------------------------------
# Config absent -> baked-in defaults
# ---------------------------------------------------------------------------


def test_config_absent_reads_dispatch_yaml_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    # config=None must not require a caller to pass config; load_config is the
    # seam. Stub it (no real file dependency) and prove the default 0.65 applies.
    monkeypatch.setattr(gate, "load_config", lambda path=None: {})
    assert classify_category(
        "research the latest", router_factory=_factory("web_research", 0.70)
    ) == ("web_research", pytest.approx(0.70))
    assert (
        classify_category("research the latest", router_factory=_factory("web_research", 0.60))
        is None
    )


# ---------------------------------------------------------------------------
# Shared build/cache machinery (gate._cached_router_build via category_routes)
# ---------------------------------------------------------------------------


def test_default_factory_builds_from_category_routes_and_caches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The default factory drives gate's SHARED builder with the
    ``category_routes`` route group and caches the result per-process (no real
    semantic_router import)."""
    builds: list[dict[str, Any]] = []

    def _fake_build(config: dict[str, Any], routes_key: str = "routes", label: str = "semantic"):
        builds.append({"routes_key": routes_key, "label": label})
        return gate._SemanticRouter(lambda _t: None)

    monkeypatch.setattr(gate, "_build_semantic_router", _fake_build)
    monkeypatch.setattr(gate, "_clock", lambda: 0.0)

    cfg = {"category_routes": {"web_research": ["research the news"]}}
    first = cat._default_router_factory(cfg)
    second = cat._default_router_factory(cfg)

    assert isinstance(first, gate._SemanticRouter)
    assert first is second  # cached per signature, built once
    assert builds == [{"routes_key": "category_routes", "label": "category"}]


def test_default_factory_build_failure_degrades(monkeypatch: pytest.MonkeyPatch) -> None:
    """A build failure is cached + re-raised (classify_category then returns
    None), proving the failure-TTL machinery is shared, not re-derived."""

    def _boom_build(_config: dict[str, Any], routes_key: str = "routes", label: str = "semantic"):
        raise RuntimeError("model download timed out")

    monkeypatch.setattr(gate, "_build_semantic_router", _boom_build)
    monkeypatch.setattr(gate, "_clock", lambda: 0.0)

    cfg = {"category_routes": {"web_research": ["x"]}}
    # The factory re-raises (classify_category swallows it to None).
    with pytest.raises(RuntimeError):
        cat._default_router_factory(cfg)
    # End-to-end through classify_category: same broken default factory -> None.
    assert classify_category("research the news", config=cfg) is None


# ---------------------------------------------------------------------------
# Shared encoder across route groups (finding F4)
# ---------------------------------------------------------------------------


def test_encoder_built_once_across_both_route_groups(monkeypatch: pytest.MonkeyPatch) -> None:
    """The FastEmbedEncoder is constructed AT MOST ONCE per process across both
    route groups: the lane gate and the category classifier both funnel through
    ``gate._shared_encoder``, so a second group reuses the warm encoder object
    instead of paying the ~2s warmup / double memory again. Counted via an
    injected encoder factory — no real model, no semantic_router import."""
    builds: list[str] = []

    def _fake_make_encoder(name: str) -> Any:
        builds.append(name)
        return object()  # opaque encoder sentinel

    monkeypatch.setattr(gate, "_make_encoder", _fake_make_encoder)

    name = "BAAI/bge-small-en-v1.5"
    lane_encoder = gate._shared_encoder(name)  # lane gate (routes)
    category_encoder = gate._shared_encoder(name)  # category classifier (category_routes)

    assert lane_encoder is category_encoder  # the SAME object is reused
    assert builds == [name]  # constructed exactly once across both groups
