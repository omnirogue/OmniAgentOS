"""Hermetic tests for the three-gate FAST DISPATCH classifier.

Every semantic gate is an INJECTED fake -- the real ``semantic_router`` is never
imported (it may be absent in CI, exactly like the dev venv). The default lazy
factory's degradation path is asserted explicitly by monkeypatching it to raise
``ImportError``.
"""

from __future__ import annotations

from typing import Any

import pytest

from omniagentos.dispatch import gate as gate_mod
from omniagentos.dispatch.gate import GateDecision, decide

# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _FakeRouter:
    def __init__(self, name: str | None, score: float) -> None:
        self._name = name
        self._score = score

    def classify(self, brief: str) -> tuple[str | None, float]:
        return self._name, self._score


def _router_factory(name: str | None, score: float):
    return lambda _config: _FakeRouter(name, score)


def _raising_factory(exc: Exception):
    def _factory(_config: dict[str, Any]) -> Any:
        raise exc

    return _factory


# ---------------------------------------------------------------------------
# Gate 0 -- rules (table-driven)
# ---------------------------------------------------------------------------

_RISK_BRIEFS = [
    "wire up Stripe payments for the subscription checkout",
    "run a database migration that alters the orders table",
    "rotate the production API keys and update the secrets manager",
    "deploy the new build to production",
    "delete the stale user rows from the accounts table",
    "change the OAuth permissions for admin users",
]


@pytest.mark.parametrize("brief", _RISK_BRIEFS)
def test_gate0_risk_always_fallthrough_and_flagged(brief: str) -> None:
    # A risky brief must NEVER reach the semantic/LLM gates -- so even a fake
    # router that would classify it solo_fast must be ignored.
    d = decide(brief, router_factory=_router_factory("solo_fast", 0.99))
    assert d.decision == "fallthrough"
    assert d.risk_flagged is True
    assert d.gate == "rules"


_SWEEP_BRIEFS = [
    "add type hints across the entire codebase",
    "audit all the modules for missing error handling",
    "document every public function in the library",
    "update the license header in every source file",
]


@pytest.mark.parametrize("brief", _SWEEP_BRIEFS)
def test_gate0_sweep_routes_to_swarm(brief: str) -> None:
    d = decide(brief, router_factory=_router_factory("solo_fast", 0.99))
    assert d.decision == "swarm"
    assert d.gate == "rules"
    assert d.risk_flagged is False


def test_gate0_enumeration_of_three_deliverables_is_swarm() -> None:
    # Three independent deliverables, no risk terms (login/auth would risk-flag).
    d = decide("add a search endpoint, write the tests and update the docs")
    assert d.decision == "swarm"
    assert d.gate == "rules"


def test_gate0_simple_sweep_gets_fast_speed_hint() -> None:
    d = decide("rename the logger in every module")
    assert d.decision == "swarm"
    assert d.speed_hint == "fast"


_SOLO_FAST_BRIEFS = [
    "fix a typo in the README",
    "add a docstring to the parse_config function",
    "rename the variable foo to bar in utils.py",
    "bump the version string to 1.2.3",
    "update the copyright year in the footer",
]


@pytest.mark.parametrize("brief", _SOLO_FAST_BRIEFS)
def test_gate0_tiny_brief_is_solo_fast(brief: str) -> None:
    # No router/LLM needed -- Gate 0 resolves these on pure functions.
    d = decide(brief, router_factory=_raising_factory(ImportError("nope")))
    assert d.decision == "solo_fast"
    assert d.gate == "rules"
    assert d.risk_flagged is False


def test_gate0_empty_brief_is_fallthrough() -> None:
    d = decide("   ")
    assert d.decision == "fallthrough"
    assert d.gate == "rules"


# ---------------------------------------------------------------------------
# Gate 1 -- semantic mapping + threshold
# ---------------------------------------------------------------------------

# A brief Gate 0 cannot bound (no risk, no sweep, no bounded solo verb) -> Gate 1.
_AMBIGUOUS = "improve the reliability of the background worker somehow"


@pytest.mark.parametrize(
    ("route", "expected"),
    [
        ("solo_standard", "solo_standard"),
        ("solo_complex", "solo_complex"),
        ("swarm", "swarm"),
    ],
)
def test_gate1_route_maps_to_decision(route: str, expected: str) -> None:
    d = decide(_AMBIGUOUS, router_factory=_router_factory(route, 0.90))
    assert d.decision == expected
    assert d.gate == "semantic"
    assert d.confidence == pytest.approx(0.90)


def test_gate1_risk_route_falls_through_and_flags() -> None:
    d = decide(_AMBIGUOUS, router_factory=_router_factory("risk", 0.95))
    assert d.decision == "fallthrough"
    assert d.risk_flagged is True
    assert d.gate == "semantic"


def test_gate1_below_threshold_falls_to_gate2() -> None:
    calls: list[str] = []

    def _llm(brief: str) -> dict[str, Any]:
        calls.append(brief)
        return {"decision": "solo_standard", "confidence": 0.8, "reason": "haiku says so"}

    # Score below the 0.72 default -> Gate 2 must break the tie.
    d = decide(
        _AMBIGUOUS,
        router_factory=_router_factory("solo_fast", 0.40),
        llm_fn=_llm,
    )
    assert calls == [_AMBIGUOUS]
    assert d.gate == "llm"
    assert d.decision == "solo_standard"


def test_gate1_unknown_route_falls_to_gate2() -> None:
    d = decide(
        _AMBIGUOUS,
        router_factory=_router_factory("mystery", 0.99),
        llm_fn=lambda _b: None,
    )
    assert d.decision == "fallthrough"


# Gate 1's floor must reject a NON-FINITE score or threshold, the same way
# categories.py:143 already does for the pin classifier (finding F3): NaN fails
# every comparison, so a bare ``score < threshold`` ACCEPTS it, and +inf passes
# any finite floor. Gate 2's floor already had the guard (_float_threshold);
# Gate 1's did not, so a `.nan` in configs/dispatch.yaml silently switched the
# confidence floor off for the one gate that is on in production.


@pytest.mark.parametrize("score", [float("nan"), float("inf")])
def test_gate1_non_finite_score_falls_to_gate2(score: float) -> None:
    d = decide(
        _AMBIGUOUS,
        router_factory=_router_factory("solo_fast", score),
        llm_fn=lambda _b: None,
    )
    assert d.decision == "fallthrough"
    assert d.gate == "fallthrough"


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf")])
def test_gate1_non_finite_configured_threshold_falls_back_to_default(bad: float) -> None:
    # A non-finite semantic_confidence must degrade to the baked-in 0.72 floor
    # (exactly what _float_threshold already does for gate2_min_confidence),
    # NOT disable the floor -- a 0.001 route is never a confident verdict.
    cfg = {"thresholds": {"semantic_confidence": bad}}
    assert gate_mod._thresholds(cfg)[0] == pytest.approx(gate_mod._DEFAULT_SEMANTIC_CONFIDENCE)
    d = decide(
        _AMBIGUOUS,
        config=cfg,
        router_factory=_router_factory("solo_fast", 0.001),
        llm_fn=lambda _b: None,
    )
    assert d.decision == "fallthrough"
    assert d.gate == "fallthrough"


# ---------------------------------------------------------------------------
# Gate 2 -- LLM tiebreaker
# ---------------------------------------------------------------------------


def _to_gate2_factory():
    # A router that reports "no route" cleanly -> Gate 1 returns None -> Gate 2.
    return _router_factory(None, 0.0)


def test_gate2_none_is_fallthrough() -> None:
    d = decide(_AMBIGUOUS, router_factory=_to_gate2_factory(), llm_fn=lambda _b: None)
    assert d.decision == "fallthrough"
    assert d.gate == "fallthrough"


def test_gate2_invalid_decision_is_fallthrough() -> None:
    d = decide(
        _AMBIGUOUS,
        router_factory=_to_gate2_factory(),
        llm_fn=lambda _b: {"decision": "banana", "confidence": 0.9, "reason": "x"},
    )
    assert d.decision == "fallthrough"
    assert d.gate == "fallthrough"


def test_gate2_valid_decision_applied() -> None:
    d = decide(
        _AMBIGUOUS,
        router_factory=_to_gate2_factory(),
        llm_fn=lambda _b: {"decision": "solo_complex", "confidence": 0.7, "reason": "hard"},
    )
    assert d.decision == "solo_complex"
    assert d.gate == "llm"
    assert d.reason == "hard"


# ---------------------------------------------------------------------------
# Degradation + exception-proofing
# ---------------------------------------------------------------------------


def test_default_factory_importerror_degrades_to_gate2(monkeypatch: pytest.MonkeyPatch) -> None:
    # Simulate the real posture: semantic_router absent -> the DEFAULT factory
    # raises ImportError. decide() (router_factory=None) must degrade to Gate 2,
    # never raise.
    monkeypatch.setattr(
        gate_mod,
        "_default_router_factory",
        _raising_factory(ImportError("No module named 'semantic_router'")),
    )
    d = decide(_AMBIGUOUS, llm_fn=lambda _b: None)
    assert d.decision == "fallthrough"
    assert d.gate == "fallthrough"


def test_decide_is_exception_proof_when_factory_and_llm_both_raise() -> None:
    def _boom_llm(_b: str) -> dict[str, Any]:
        raise RuntimeError("llm exploded mid-call")

    d = decide(
        _AMBIGUOUS,
        router_factory=_raising_factory(RuntimeError("factory exploded mid-call")),
        llm_fn=_boom_llm,
    )
    assert d.decision == "fallthrough"
    assert d.gate == "fallthrough"


def test_decide_never_raises_on_broken_config() -> None:
    # A config object that blows up on access still yields a decision.
    class _Explode(dict):
        def get(self, *_a: Any, **_k: Any) -> Any:  # type: ignore[override]
            raise RuntimeError("config is on fire")

    d = decide("fix a typo", config=_Explode())
    assert d.decision == "fallthrough"
    assert d.gate == "fallthrough"
    assert "decide error" in d.reason


# ---------------------------------------------------------------------------
# Config defaults + latency
# ---------------------------------------------------------------------------


def test_config_absent_uses_sane_defaults() -> None:
    # Empty config -> baked-in defaults still classify risk + solo_fast correctly.
    assert decide("wire up Stripe checkout", config={}).risk_flagged is True
    assert decide("fix a typo in the README", config={}).decision == "solo_fast"


def test_latency_ms_uses_injected_clock() -> None:
    ticks = iter([10.0, 10.5])  # 0.5 s -> 500 ms
    d = decide("fix a typo", now=lambda: next(ticks))
    assert d.latency_ms == pytest.approx(500.0)


def test_gate_decision_is_frozen() -> None:
    from dataclasses import FrozenInstanceError

    d = GateDecision(decision="solo_fast")
    with pytest.raises(FrozenInstanceError):
        d.decision = "swarm"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# F1 -- broadened risk scope + regex-quality false positives (exactly as briefed)
# ---------------------------------------------------------------------------

# These MUST classify as risk-flagged fallthrough (a fake router that would call
# them solo_fast is deliberately injected to prove risk wins).
_F1_MUST_RISK = [
    "remove all customer records from the staging database",
    "add login with SSO",
    "update the users database to v2",
    "update the live site with the new build",
    "add credit card collection to signup",
]

# These MUST NOT risk-flag (the old bare \btoken\b / \bdrop\b false positives).
_F1_MUST_NOT_RISK = [
    "update design token names",
    "add a CSS drop shadow",
]


@pytest.mark.parametrize("brief", _F1_MUST_RISK)
def test_f1_broadened_risk_is_fallthrough_flagged(brief: str) -> None:
    d = decide(brief, router_factory=_router_factory("solo_fast", 0.99))
    assert d.decision == "fallthrough", brief
    assert d.risk_flagged is True, brief
    assert d.gate == "rules", brief


@pytest.mark.parametrize("brief", _F1_MUST_NOT_RISK)
def test_f1_false_positives_do_not_risk_flag(brief: str) -> None:
    d = decide(brief, router_factory=_raising_factory(ImportError("x")), llm_fn=lambda _b: None)
    assert d.risk_flagged is False, brief
    assert d.decision != "fallthrough", brief


# ---------------------------------------------------------------------------
# F3 -- Gate-1 cache: failure TTL + build lock (build injected, no real package)
# ---------------------------------------------------------------------------


class _FakeChoice:
    def __init__(self, name: str, score: float) -> None:
        self.name = name
        self.similarity_score = score


def _make_semantic_router(name: str = "solo_standard", score: float = 0.9) -> Any:
    # A real gate_mod._SemanticRouter (so isinstance checks in the cache pass),
    # wrapping a fake inner callable -- no semantic_router import.
    return gate_mod._SemanticRouter(lambda _brief: _FakeChoice(name, score))


def test_gate1_build_failure_retries_after_ttl(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = {"n": 0}

    def _flaky_build(_config: dict[str, Any]) -> Any:
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("model download timed out")
        return _make_semantic_router()

    clock = {"t": 1000.0}
    monkeypatch.setattr(gate_mod, "_build_semantic_router", _flaky_build)
    monkeypatch.setattr(gate_mod, "_clock", lambda: clock["t"])
    cfg = {"routes": {"solo_standard": ["x"]}, "thresholds": {"gate1_retry_seconds": 300}}

    # First call: build fails -> factory raises (decide would degrade to Gate 2).
    with pytest.raises(RuntimeError):
        gate_mod._default_router_factory(cfg)
    assert calls["n"] == 1

    # Within the TTL: still poisoned, re-raises WITHOUT rebuilding.
    clock["t"] = 1000.0 + 100.0
    with pytest.raises(RuntimeError):
        gate_mod._default_router_factory(cfg)
    assert calls["n"] == 1  # no rebuild attempt inside the TTL

    # After the TTL elapses: retries the build once more and succeeds.
    clock["t"] = 1000.0 + 400.0
    router = gate_mod._default_router_factory(cfg)
    assert calls["n"] == 2
    assert router.classify("anything") == ("solo_standard", 0.9)

    # A subsequent call returns the cached success lock-free (no more builds).
    again = gate_mod._default_router_factory(cfg)
    assert again is router
    assert calls["n"] == 2


def test_gate1_concurrent_build_never_downgrades_success(monkeypatch: pytest.MonkeyPatch) -> None:
    import threading

    build_calls = {"n": 0}
    count_lock = threading.Lock()

    def _build_once(_config: dict[str, Any]) -> Any:
        with count_lock:
            build_calls["n"] += 1
            n = build_calls["n"]
        if n == 1:
            return _make_semantic_router()
        # If the lock ever let a second build run, it would fail and (without the
        # success-is-permanent guarantee) could overwrite the cached success.
        raise RuntimeError("second concurrent build must never run after a success")

    monkeypatch.setattr(gate_mod, "_build_semantic_router", _build_once)
    monkeypatch.setattr(gate_mod, "_clock", lambda: 0.0)
    cfg = {"routes": {"solo_standard": ["x"]}}

    results: list[Any] = []
    errors: list[Exception] = []
    barrier = threading.Barrier(2)

    def _worker() -> None:
        barrier.wait()
        try:
            results.append(gate_mod._default_router_factory(cfg))
        except Exception as exc:  # noqa: BLE001 -- captured for the assertion.
            errors.append(exc)

    threads = [threading.Thread(target=_worker) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert errors == []  # the losing thread returned the cached success, never failed
    assert build_calls["n"] == 1  # the build lock serialised to exactly one build
    assert len(results) == 2
    assert results[0] is results[1]  # both got the same permanent wrapper


# ---------------------------------------------------------------------------
# F5 -- Gate-2 confidence validation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "conf",
    [float("nan"), float("inf"), float("-inf"), -0.1, "high", None],
)
def test_gate2_non_finite_confidence_is_fallthrough(conf: Any) -> None:
    d = decide(
        _AMBIGUOUS,
        router_factory=_to_gate2_factory(),
        llm_fn=lambda _b: {"decision": "solo_standard", "confidence": conf, "reason": "x"},
    )
    assert d.decision == "fallthrough"
    assert d.gate == "fallthrough"


def test_gate2_absent_confidence_is_fallthrough() -> None:
    d = decide(
        _AMBIGUOUS,
        router_factory=_to_gate2_factory(),
        llm_fn=lambda _b: {"decision": "solo_standard", "reason": "x"},
    )
    assert d.decision == "fallthrough"


def test_gate2_below_floor_is_fallthrough() -> None:
    # Default floor is 0.5 (configs/dispatch.yaml); 0.3 is not trusted.
    d = decide(
        _AMBIGUOUS,
        router_factory=_to_gate2_factory(),
        llm_fn=lambda _b: {"decision": "solo_fast", "confidence": 0.3, "reason": "meh"},
    )
    assert d.decision == "fallthrough"
    assert d.gate == "fallthrough"


def test_gate2_custom_floor_from_config() -> None:
    cfg = {"thresholds": {"gate2_min_confidence": 0.8, "semantic_confidence": 0.6}}
    d = decide(
        _AMBIGUOUS,
        config=cfg,
        router_factory=_to_gate2_factory(),
        llm_fn=lambda _b: {"decision": "solo_complex", "confidence": 0.7, "reason": "x"},
    )
    # 0.7 < 0.8 floor -> fallthrough.
    assert d.decision == "fallthrough"


def test_gate2_confidence_above_one_is_clamped() -> None:
    d = decide(
        _AMBIGUOUS,
        router_factory=_to_gate2_factory(),
        llm_fn=lambda _b: {"decision": "solo_standard", "confidence": 1.5, "reason": "sure"},
    )
    assert d.decision == "solo_standard"
    assert d.gate == "llm"
    assert d.confidence == 1.0
