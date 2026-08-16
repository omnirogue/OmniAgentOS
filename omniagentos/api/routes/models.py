"""Model catalog endpoint — single-source-of-truth for the dashboard picker.

``GET /api/models`` → ``{"models": [{id, label, provider, tier, available,
lineage}]}`` — the first entry is always the auto-router synthetic
``{"id": "auto", "label": "Auto — router decides", ...}``. Every cascade
ladder entry (configs/cascade.yaml) becomes a concrete model row; providers
with no healthy account are marked ``available: false`` so the picker can
grey them out.

Read-only GET with no session-token gate (matches the ``GET /api/accounts``
pattern — the dashboard's unauthenticated home-landing case needs the picker
before any operator action). Registration: one ``include_router`` line in
``omniagentos.api.main`` — Kimi handles that after all slices land.

Graceful degradation: missing/partial cascade.yaml or empty accounts table
still returns the auto entry + whatever could be parsed.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Query

from omniagentos.modelintel.catalog import build_model_catalog

LOG = logging.getLogger(__name__)

router = APIRouter(prefix="/api/models", tags=["models"])


@router.get("")
def list_models(
    cascade_path: str | None = Query(None, description="Override cascade.yaml path (tests)"),
    db_path: str | None = Query(None, description="Override accounts DB path (tests)"),
) -> dict[str, Any]:
    """Aggregate the model catalog from the cascade ladder + provider health.

    Query params exist only for test injection — production callers send no
    params and get the canonical locations. Both default to ``None``, which
    :func:`build_model_catalog` resolves to the production paths.
    """
    cascade = Path(cascade_path) if cascade_path else None
    try:
        return build_model_catalog(cascade_path=cascade, db_path=db_path)
    except Exception:  # noqa: BLE001 — catalog must never raise to the caller
        LOG.warning("model catalog build failed — returning auto-only", exc_info=True)
        return {"models": [_auto_entry()], "updated_at": _now()}


def _auto_entry() -> dict[str, Any]:
    """The minimum viable response — auto-only, for catastrophic failures."""
    return {
        "id": "auto",
        "label": "Auto — router decides",
        "provider": "router",
        "tier": None,
        "available": True,
        "lineage": None,
    }


def _now() -> str:
    from omniagentos.contracts import utc_now_iso

    return utc_now_iso()
