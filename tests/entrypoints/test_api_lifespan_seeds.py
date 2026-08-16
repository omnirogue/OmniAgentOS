"""The real API startup seeds the two built-in routines.

Entry point: the real ASGI app's ``lifespan`` (``omniagentos.api:app``),
which is what ``make api`` / ``uvicorn omniagentos.api:app`` runs.

Mechanism observed through the public API: after startup with
``OMNIAGENTOS_SEED_ROUTINES_ON_STARTUP=1``, ``GET /api/routines`` lists the
improve-dispatcher and lab-jobs-drain routines, and a second lifespan entry
does not duplicate them (the ``ensure_*`` helpers are idempotent by name).
The session-wide conftest pin keeps this flag off everywhere else.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from omniagentos.scheduler.routines import (
    IMPROVE_DISPATCHER_ROUTINE_NAME,
    LAB_JOBS_ROUTINE_NAME,
)


def _routine_names(client: TestClient) -> list[str]:
    response = client.get("/api/routines")
    assert response.status_code == 200
    return [str(r.get("name")) for r in response.json()]


def test_api_startup_seeds_builtin_routines(
    campaign_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Real lifespan must ensure both built-in routines, exactly once each."""
    monkeypatch.setenv("OMNIAGENTOS_SEED_ROUTINES_ON_STARTUP", "1")

    # Import the production app object — do not hand-assemble a FastAPI().
    from omniagentos.api import app as production_app

    # Entering the context runs the real lifespan startup hook.
    with TestClient(production_app) as client:
        names = _routine_names(client)

    assert IMPROVE_DISPATCHER_ROUTINE_NAME in names
    assert LAB_JOBS_ROUTINE_NAME in names

    # A second lifespan entry is idempotent by name — no duplicate routines.
    with TestClient(production_app) as client:
        names = _routine_names(client)

    assert names.count(IMPROVE_DISPATCHER_ROUTINE_NAME) == 1
    assert names.count(LAB_JOBS_ROUTINE_NAME) == 1
