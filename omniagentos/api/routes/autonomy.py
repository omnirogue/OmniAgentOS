"""API routes for autonomy settings (§9, Tier P — requires X-Autonomy-Token header)."""

from __future__ import annotations

import hmac
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any, cast

from fastapi import APIRouter, Depends, Header
from pydantic import BaseModel

from omniagentos.api.deps import StoreDep
from omniagentos.api.routes.control import _authorized, _emit, fail
from omniagentos.reliability.contracts import ReliabilityStore

if TYPE_CHECKING:
    from omniagentos.reliability.store import SqliteReliabilityStore

router = APIRouter(prefix="/api/autonomy", tags=["autonomy"])
AUTONOMY_TOKEN_PATH = Path("var/secrets/autonomy-token")


class AutonomyUpdateRequest(BaseModel):
    """Request to update autonomy settings."""

    scope_type: str  # 'global' | 'department' | 'agent' | 'kind'
    scope_id: str = ""
    mode: str  # 'approve' | 'auto'
    max_auto_risk: int  # 0 | 1 | 2


def _rel_store(store: StoreDep) -> ReliabilityStore:
    """Cast generic store to ReliabilityStore protocol."""
    return cast(ReliabilityStore, store)  # Already implements ReliabilityStore protocol


def _verify_autonomy_token(token: str | None) -> bool:
    """Verify X-Autonomy-Token against var/secrets/autonomy-token."""
    if not token:
        return False

    if not AUTONOMY_TOKEN_PATH.exists():
        return False

    try:
        with open(AUTONOMY_TOKEN_PATH) as f:
            expected_token = f.read().strip()
    except Exception:
        return False

    # Constant-time compare
    return hmac.compare_digest(token, expected_token)


def _get_autonomy_token(
    x_autonomy_token: str | None = Header(None, alias="X-Autonomy-Token"),
) -> str | None:
    """Extract and validate autonomy token from headers."""
    return x_autonomy_token


def requires_autonomy_token(
    x_autonomy_token: str | None = Header(None, alias="X-Autonomy-Token"),
) -> None:
    """Require the Tier-P token using the constant-time verifier."""
    if not _verify_autonomy_token(x_autonomy_token):
        fail(403, "forbidden", "invalid or missing autonomy token")


@router.get("")
def get_autonomy(
    store: StoreDep,
    _: Annotated[None, Depends(_authorized)] = None,
) -> dict[str, Any]:
    """Get effective autonomy settings tree."""
    del _
    rel_store = _rel_store(store)

    # Fetch all autonomy settings
    all_settings = rel_store.list_autonomy_settings()

    def get_attr(obj: Any, attr: str) -> Any:
        """Get attribute from dataclass or dict."""
        return getattr(obj, attr) if hasattr(obj, attr) else obj.get(attr)

    # Group by scope type (handle both dataclass and dict objects)
    global_setting = next(
        (s for s in all_settings if get_attr(s, "scope_type") == "global"),
        None,
    )
    dept_settings = [s for s in all_settings if get_attr(s, "scope_type") == "department"]
    agent_settings = [s for s in all_settings if get_attr(s, "scope_type") == "agent"]
    kind_settings = [s for s in all_settings if get_attr(s, "scope_type") == "kind"]

    return {
        "global": {
            "mode": get_attr(global_setting, "mode") if global_setting else "approve",
            "max_auto_risk": get_attr(global_setting, "max_auto_risk") if global_setting else 0,
        }
        if global_setting
        else {"mode": "approve", "max_auto_risk": 0},
        "departments": [
            {
                "id": get_attr(s, "scope_id"),
                "mode": get_attr(s, "mode"),
                "max_auto_risk": get_attr(s, "max_auto_risk"),
            }
            for s in dept_settings
        ],
        "agents": [
            {
                "id": get_attr(s, "scope_id"),
                "mode": get_attr(s, "mode"),
                "max_auto_risk": get_attr(s, "max_auto_risk"),
            }
            for s in agent_settings
        ],
        "kinds": [
            {
                "id": get_attr(s, "scope_id"),
                "mode": get_attr(s, "mode"),
                "max_auto_risk": get_attr(s, "max_auto_risk"),
            }
            for s in kind_settings
        ],
    }


@router.put("", status_code=200)
def update_autonomy(
    store: StoreDep,
    body: AutonomyUpdateRequest,
    _: Annotated[None, Depends(requires_autonomy_token)],
) -> dict[str, Any]:
    """Update autonomy settings (requires X-Autonomy-Token header)."""
    del _

    # Validate request
    valid_scopes = {"global", "department", "agent", "kind"}
    if body.scope_type not in valid_scopes:
        fail(
            422,
            "validation",
            "invalid scope_type",
            {"scope_type": body.scope_type},
        )

    valid_modes = {"approve", "auto"}
    if body.mode not in valid_modes:
        fail(
            422,
            "validation",
            "invalid mode",
            {"mode": body.mode},
        )

    if body.max_auto_risk not in {0, 1, 2}:
        fail(
            422,
            "validation",
            "invalid max_auto_risk",
            {"max_auto_risk": body.max_auto_risk},
        )

    # ``upsert_autonomy_setting`` is intentionally NOT on the frozen ReliabilityStore
    # protocol (human-token API surface only) — narrow to the concrete store.
    rel_store = cast("SqliteReliabilityStore", _rel_store(store))
    setting = rel_store.upsert_autonomy_setting(
        scope_type=body.scope_type,
        scope_id=body.scope_id,
        mode=body.mode,
        max_auto_risk=body.max_auto_risk,
        updated_by="human:operator",
    )

    # Emit autonomy.changed event
    _emit(
        store,
        "autonomy.changed",
        "autonomy.updated",
        target_type="autonomy",
        target_id=f"{body.scope_type}:{body.scope_id}",
        payload={
            "scope_type": body.scope_type,
            "scope_id": body.scope_id,
            "mode": body.mode,
            "max_auto_risk": body.max_auto_risk,
        },
    )

    return {
        "scope_type": setting.scope_type,
        "scope_id": setting.scope_id,
        "mode": setting.mode,
        "max_auto_risk": setting.max_auto_risk,
        "updated_at": setting.updated_at,
    }
