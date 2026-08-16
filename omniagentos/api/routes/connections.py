"""Connections control center — a Stripe-sleek panel of every integration.

The single endpoint exposes the operator's real stack as a categorised grid
with per-integration status derived entirely from vault-key presence. It never
returns credential VALUES — only counts, presence booleans and family labels.
A leak of this response is an inventory, not a compromise.

Vault path is injectable via ``OMNIAGENTOS_CONNECTIONS_VAULT`` for tests and
containerised environments; otherwise it reads from the canonical
``~/.config/omni/connections.env``. If the vault is unreadable the endpoint
does NOT fail 500: every integration surfaces ``status="error"`` with a
``detail="Vault unreadable"`` so the page renders a useful empty grid
instead of a crash screen.

Response contract (pinned in FINAL-PLAN.md section B)::

    GET /api/connections
    {
      "categories": [
        {
          "id": str,
          "label": str,
          "integrations": [
            {
              "id": str,
              "name": str,
              "logo": str,
              "status": "connected"|"configured"|"not_configured"|"error",
              "instances": [{"label": str, "status": str}],
              "detail": str,
              "docs_url": str|null
            }
          ]
        }
      ]
    }
"""

from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel, ConfigDict

from omniagentos.connectors.registry import (
    CategoryView,
    build_connections_view,
)

router = APIRouter(prefix="/api/connections", tags=["connections"])


# ── Response schema ────────────────────────────────────────────────────────


class InstanceOut(BaseModel):
    model_config = ConfigDict(extra="forbid")

    label: str
    status: str


class IntegrationOut(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    name: str
    logo: str
    status: str
    instances: list[InstanceOut]
    detail: str
    docs_url: str | None


class CategoryOut(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    label: str
    integrations: list[IntegrationOut]


class ConnectionsResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    categories: list[CategoryOut]
    connected_count: int
    total_count: int


# ── View -> DTO ────────────────────────────────────────────────────────────


def _view_to_dto(
    categories: list[CategoryView],
) -> tuple[list[CategoryOut], int, int]:
    """Convert the registry view into the pinned API DTO shape."""
    out: list[CategoryOut] = []
    connected = 0
    total = 0

    for cat in categories:
        ints: list[IntegrationOut] = []
        for view in cat.integrations:
            total += 1
            if view.status == "connected":
                connected += 1
            ints.append(
                IntegrationOut(
                    id=view.id,
                    name=view.name,
                    logo=view.logo,
                    status=view.status,
                    instances=[
                        InstanceOut(label=inst.label, status=inst.status)
                        for inst in view.instances
                    ],
                    detail=view.detail,
                    docs_url=view.docs_url,
                )
            )
        out.append(
            CategoryOut(id=cat.id, label=cat.label, integrations=ints)
        )

    return out, connected, total


# ── Route ──────────────────────────────────────────────────────────────────


@router.get("", response_model=ConnectionsResponse)
def list_connections() -> ConnectionsResponse:
    """Return the categorised integration panel.

    Never fails on vault errors — every integration reports ``error`` instead
    so the page always renders.
    """
    categories, _vault_readable = build_connections_view()
    cats, connected, total = _view_to_dto(categories)
    return ConnectionsResponse(
        categories=cats, connected_count=connected, total_count=total
    )


__all__ = ["router", "ConnectionsResponse", "CategoryOut", "IntegrationOut"]
