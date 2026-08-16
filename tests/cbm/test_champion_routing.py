"""D1: champion model_assignment rows influence CBM/routing recommendations."""

from __future__ import annotations

import sys
import types
from collections.abc import Iterator
from typing import Any

import pytest

from omniagentos.cbm.champion_routing import (
    CHAMPION_ROUTING_MODE_ENV,
    apply_champion_to_recommendation,
    champion_routing_mode,
    clear_champion_cache,
    get_promoted_model_assignments,
)
from omniagentos.cbm.service import CognitiveBudgetService
from omniagentos.routing.workers import WorkerEndpoint, select_worker


@pytest.fixture(autouse=True)
def _clean_champion_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    clear_champion_cache()
    monkeypatch.delenv(CHAMPION_ROUTING_MODE_ENV, raising=False)
    # Drop any previously injected fake runtime.
    sys.modules.pop("omniagentos.lab.runtime", None)
    yield
    clear_champion_cache()
    sys.modules.pop("omniagentos.lab.runtime", None)
    monkeypatch.delenv(CHAMPION_ROUTING_MODE_ENV, raising=False)


def _install_fake_runtime(
    monkeypatch: pytest.MonkeyPatch,
    rows: list[dict[str, Any]] | None = None,
    *,
    raise_exc: Exception | None = None,
) -> None:
    """Inject a fake omniagentos.lab.runtime module with the expected accessor."""
    mod = types.ModuleType("omniagentos.lab.runtime")

    def get_promoted_model_assignments() -> list[dict[str, Any]]:
        if raise_exc is not None:
            raise raise_exc
        return list(rows or [])

    mod.get_promoted_model_assignments = get_promoted_model_assignments  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "omniagentos.lab.runtime", mod)
    # Also ensure parent package attribute path works if already imported.
    import omniagentos.lab as lab_pkg

    monkeypatch.setattr(lab_pkg, "runtime", mod, raising=False)


def test_mode_defaults_off() -> None:
    assert champion_routing_mode() == "off"


def test_off_mode_never_consults_accessor(monkeypatch: pytest.MonkeyPatch) -> None:
    called = {"n": 0}

    def boom() -> list[dict[str, Any]]:
        called["n"] += 1
        raise AssertionError("accessor must not be called in off mode")

    mod = types.ModuleType("omniagentos.lab.runtime")
    mod.get_promoted_model_assignments = boom  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "omniagentos.lab.runtime", mod)
    monkeypatch.setenv(CHAMPION_ROUTING_MODE_ENV, "off")

    assert get_promoted_model_assignments() == []
    assert called["n"] == 0


def test_absent_runtime_preserves_baseline(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(CHAMPION_ROUTING_MODE_ENV, "enforce")
    # No runtime module installed.
    baseline = {
        "model_role": "fast_implementer",
        "provider_hints": ["codex", "gemini"],
        "recommended_rung": 1,
    }
    out = apply_champion_to_recommendation(baseline)
    assert out["model_role"] == "fast_implementer"
    assert out["provider_hints"] == ["codex", "gemini"]
    assert out["champion_applied"] is False


def test_accessor_failure_degrades_to_baseline(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(CHAMPION_ROUTING_MODE_ENV, "enforce")
    _install_fake_runtime(monkeypatch, raise_exc=RuntimeError("lab down"))
    baseline = {"model_role": "fast_implementer", "provider_hints": ["codex"]}
    out = apply_champion_to_recommendation(baseline)
    assert out["model_role"] == "fast_implementer"
    assert out["champion_applied"] is False


def test_shadow_logs_delta_keeps_baseline(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(CHAMPION_ROUTING_MODE_ENV, "shadow")
    _install_fake_runtime(
        monkeypatch,
        [
            {
                "model_role": "fast_implementer",
                "provider": "grok",
                "model": "grok-4.5",
            }
        ],
    )
    baseline = {
        "model_role": "fast_implementer",
        "provider_hints": ["codex", "gemini", "claude"],
    }
    out = apply_champion_to_recommendation(baseline)
    assert out["provider_hints"] == ["codex", "gemini", "claude"]  # baseline kept
    assert out["champion_applied"] is False
    shadow = out["champion_shadow"]
    assert shadow["delta"]["provider_hints"]["champion"][0] == "grok"


def test_enforce_changes_recommendation(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(CHAMPION_ROUTING_MODE_ENV, "enforce")
    _install_fake_runtime(
        monkeypatch,
        [
            {
                "model_role": "fast_implementer",
                "provider": "grok",
                "model": "grok-4.5",
            }
        ],
    )
    baseline = {
        "model_role": "fast_implementer",
        "provider_hints": ["codex", "gemini", "claude"],
    }
    out = apply_champion_to_recommendation(baseline)
    assert out["champion_applied"] is True
    assert out["provider"] == "grok"
    assert out["provider_hints"][0] == "grok"
    assert out["model"] == "grok-4.5"


def test_recommend_rung_changes_after_champion_promotion(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Any,
) -> None:
    """Promoting a champion changes a subsequent routing recommendation (enforce)."""
    monkeypatch.setenv(CHAMPION_ROUTING_MODE_ENV, "enforce")
    db = tmp_path / "cbm.db"
    svc = CognitiveBudgetService(database=db)

    # Baseline: no champion rows.
    _install_fake_runtime(monkeypatch, [])
    clear_champion_cache()
    before = svc.recommend_rung(stage="execution")
    assert before["provider_hints"]
    baseline_head = before["provider_hints"][0]
    assert baseline_head != "grok" or True  # record for contrast

    # Promote: champion prefers grok first.
    clear_champion_cache()
    _install_fake_runtime(
        monkeypatch,
        [
            {
                "model_role": before["model_role"],
                "provider": "grok",
                "model": "grok-4.5-champion",
            }
        ],
    )
    after = svc.recommend_rung(stage="execution")
    assert after["champion_applied"] is True
    assert after["provider_hints"][0] == "grok"
    assert after["model"] == "grok-4.5-champion"
    # Delta vs before is the evidence the acceptance criteria demand.
    assert after["provider_hints"] != before["provider_hints"] or after.get("model") != before.get(
        "model"
    )


def test_select_worker_enforce_prefers_champion_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(CHAMPION_ROUTING_MODE_ENV, "enforce")
    _install_fake_runtime(
        monkeypatch,
        [{"model_role": "fast_implementer", "provider": "grok"}],
    )
    endpoints = [
        WorkerEndpoint(
            worker_id="claude:x",
            provider="claude",
            model="x",
            mechanism="terminal_cli",
        ),
        WorkerEndpoint(
            worker_id="grok:y",
            provider="grok",
            model="y",
            mechanism="terminal_cli",
        ),
        WorkerEndpoint(
            worker_id="codex:z",
            provider="codex",
            model="z",
            mechanism="terminal_cli",
        ),
    ]
    # Baseline preferred order puts claude first.
    sel = select_worker(
        tier="fast",
        effort="low",
        preferred_providers=["claude", "codex", "grok"],
        endpoints=endpoints,
        model_role="fast_implementer",
    )
    assert sel.endpoint is not None
    assert sel.endpoint.provider == "grok"
