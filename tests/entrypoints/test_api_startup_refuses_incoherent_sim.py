"""N-6 — a half-set simulation environment must not boot the real API.

Entry point: the real ASGI app's ``lifespan`` (``omniagentos.api:app``),
which is what ``make api`` / ``uvicorn omniagentos.api:app`` runs. That bare
route is the one operators actually take, and no launcher hardening covers it.

Mechanism observed WITHOUT importing it: with ``OMNIAGENTOS_SIM_MODE=1`` and
no ``OMNIAGENTOS_SIM_CAMPAIGN``, entering the app must raise instead of
serving, and the message must name the offending variable. This deliberately
does NOT import ``assert_startup_coherence`` or ``SimGateError`` — the thing
under test is the wiring, not the predicate.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient


def test_api_startup_refuses_incoherent_sim_environment(
    campaign_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OMNIAGENTOS_SIM_MODE", "1")
    monkeypatch.delenv("OMNIAGENTOS_SIM_CAMPAIGN", raising=False)

    # Import the production app object — do not hand-assemble a FastAPI().
    from omniagentos.api import app as production_app

    with pytest.raises(
        RuntimeError, match=r"REFUSING TO START: .*OMNIAGENTOS_SIM_CAMPAIGN"
    ):
        with TestClient(production_app):
            pass
