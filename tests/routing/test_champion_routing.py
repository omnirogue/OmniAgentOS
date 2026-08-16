"""D1 routing-side tests: select_worker respects champion mode flags."""

from __future__ import annotations

import sys
import types
from collections.abc import Iterator
from typing import Any

import pytest

from omniagentos.cbm.champion_routing import CHAMPION_ROUTING_MODE_ENV, clear_champion_cache
from omniagentos.routing.workers import WorkerEndpoint, select_worker


@pytest.fixture(autouse=True)
def _clean(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    clear_champion_cache()
    monkeypatch.delenv(CHAMPION_ROUTING_MODE_ENV, raising=False)
    sys.modules.pop("omniagentos.lab.runtime", None)
    yield
    clear_champion_cache()
    sys.modules.pop("omniagentos.lab.runtime", None)


def _endpoints() -> list[WorkerEndpoint]:
    return [
        WorkerEndpoint("claude:a", "claude", "a", "terminal_cli"),
        WorkerEndpoint("codex:b", "codex", "b", "terminal_cli"),
        WorkerEndpoint("grok:c", "grok", "c", "terminal_cli"),
    ]


def _fake(monkeypatch: pytest.MonkeyPatch, rows: list[dict[str, Any]]) -> None:
    mod = types.ModuleType("omniagentos.lab.runtime")
    mod.get_promoted_model_assignments = lambda: list(rows)  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "omniagentos.lab.runtime", mod)
    import omniagentos.lab as lab_pkg

    monkeypatch.setattr(lab_pkg, "runtime", mod, raising=False)


def test_shadow_keeps_baseline_selection(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(CHAMPION_ROUTING_MODE_ENV, "shadow")
    _fake(monkeypatch, [{"model_role": "fast_implementer", "provider": "grok"}])
    sel = select_worker(
        tier="fast",
        effort="low",
        preferred_providers=["claude", "codex", "grok"],
        endpoints=_endpoints(),
        model_role="fast_implementer",
    )
    assert sel.endpoint is not None
    assert sel.endpoint.provider == "claude"


def test_off_keeps_baseline_selection(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(CHAMPION_ROUTING_MODE_ENV, "off")
    _fake(monkeypatch, [{"model_role": "fast_implementer", "provider": "grok"}])
    sel = select_worker(
        tier="fast",
        effort="low",
        preferred_providers=["claude", "codex", "grok"],
        endpoints=_endpoints(),
        model_role="fast_implementer",
    )
    assert sel.endpoint is not None
    assert sel.endpoint.provider == "claude"
