"""Shared fixtures for the FAST DISPATCH suite.

The gate keeps a per-process router singleton cache and a "degraded once" flag;
reset both between tests so ordering never leaks state. NONE of these tests import
the real ``semantic_router`` package -- every semantic gate is driven by an
injected fake factory (the package may be absent in CI, exactly like this venv).
"""

from __future__ import annotations

import pytest

from omniagentos.dispatch import gate as _gate


@pytest.fixture(autouse=True)
def _reset_gate_singletons() -> None:
    _gate._ROUTER_CACHE.clear()
    _gate._DEGRADE_LOGGED = False
    yield
    _gate._ROUTER_CACHE.clear()
    _gate._DEGRADE_LOGGED = False
