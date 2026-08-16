"""HTTP handlers for managing multiple Claude accounts."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel, ConfigDict

from omniagentos.accounts.service import (
    add_account,
    get_account,
    list_accounts,
    pause_account,
    remove_account,
    resume_account,
    set_default,
    set_enabled,
)
from omniagentos.api.routes.collab import fail

router = APIRouter(prefix="/api")

# A pause is the SELF-EXPIRING lever; anything longer than a week is almost
# certainly a typo'd unit (hours meant as minutes) that would quietly shrink the
# fleet for a month. The permanent lever is `enabled=false`, one PATCH away.
_MAX_PAUSE_MINUTES = 7 * 24 * 60


class AccountModel(BaseModel):
    model_config = ConfigDict(extra="ignore")


class CreateAccountRequest(AccountModel):
    label: str
    auth_type: str = "config_dir"
    config_dir: str | None = None
    secret: str | None = None
    enabled: bool = True


class UpdateAccountRequest(AccountModel):
    enabled: bool | None = None
    is_default: bool | None = None


@router.get("/accounts")
def list_all_accounts() -> dict[str, Any]:
    """List all registered Claude accounts.

    Returns accounts in order: default-first, then enabled-first.
    Secrets are never included — only has_secret boolean.
    """
    accounts = list_accounts()
    return {"accounts": accounts}


@router.get("/accounts/usage")
def account_usage() -> dict[str, Any]:
    """Cached quota snapshots per provider account — how much budget is LEFT.

    Deliberately a separate endpoint rather than a field on `/api/accounts`:
    collecting reads files off disk (each Claude config dir, plus a scan of the
    newest codex session rollouts), and the account roster is fetched by callers
    that have no interest in quota. This way the I/O is paid only by the page
    that renders it.

    Every row carries `fetched_at` / `age_seconds` / `stale` because these are
    caches written by each CLI when it last ran — an idle account's numbers go
    stale, and a stale number presented as live is worse than no number.
    Providers with no on-disk quota telemetry (grok, gemini, kimi, qwen) return
    `available: false` with a reason rather than a fabricated zero.
    """
    from omniagentos.accounts.usage import collect_all

    return {"usage": [snapshot.model_dump() for snapshot in collect_all()]}


@router.post("/accounts", status_code=201)
def create_account(body: CreateAccountRequest) -> dict[str, Any]:
    """Register a new Claude account.

    For config_dir accounts, validates that the directory exists and reads its email.
    For token/api_key accounts, stores the secret in var/secrets (0600, unreadable by agents).
    Returns the created account dict (secrets never included).
    """
    try:
        account = add_account(
            label=body.label,
            auth_type=body.auth_type,
            config_dir=body.config_dir,
            secret=body.secret,
            enabled=body.enabled,
        )
        return account
    except ValueError as exc:
        fail(400, "validation", str(exc))


@router.patch("/accounts/{account_id}")
def update_account(account_id: str, body: UpdateAccountRequest) -> dict[str, Any]:
    """Update an account's enabled or default status.

    If is_default=true, makes this account the default (implicitly enables it).
    If enabled is present, toggles whether the account participates in spawn rotation.
    Returns the updated account dict or 404 if not found.
    """
    existing = get_account(account_id)
    if existing is None:
        fail(404, "not_found", f"account {account_id} not found")

    if body.is_default:
        if not set_default(account_id):
            fail(404, "not_found", f"account {account_id} not found")

    if body.enabled is not None:
        if not set_enabled(account_id, body.enabled):
            fail(404, "not_found", f"account {account_id} not found")

    updated = get_account(account_id)
    if updated is None:
        fail(404, "not_found", f"account {account_id} not found")
    return updated


class PauseAccountRequest(AccountModel):
    minutes: int
    reason: str = ""


@router.post("/accounts/{account_id}/pause")
def pause(account_id: str, body: PauseAccountRequest) -> dict[str, Any]:
    """Temporarily drop an account out of spawn rotation, self-expiring.

    Distinct from `enabled=false` (permanent, operator must remember to undo)
    and from the automatic `cooldown_until` that limit_state writes on a rate
    limit. Rotation resumes on its own once the window elapses -- nothing has to
    sweep it, because availability is evaluated per query.

    A pause does not cancel an in-flight session on that account; it stops the
    account being handed out for NEW spawns.
    """
    if body.minutes <= 0:
        fail(400, "validation", "minutes must be positive")
    if body.minutes > _MAX_PAUSE_MINUTES:
        fail(
            400,
            "validation",
            f"pause is capped at {_MAX_PAUSE_MINUTES} minutes (7 days); "
            "to hold an account back indefinitely, disable it instead",
        )
    until = datetime.now(UTC) + timedelta(minutes=body.minutes)
    if not pause_account(account_id, until.isoformat(), body.reason):
        fail(404, "not_found", f"account {account_id} not found")
    updated = get_account(account_id)
    if updated is None:
        fail(404, "not_found", f"account {account_id} not found")
    return updated


@router.post("/accounts/{account_id}/resume")
def resume(account_id: str) -> dict[str, Any]:
    """Lift an operator pause early.

    Clears only the pause. An account still under a provider cooldown stays out
    of rotation until that expires -- see `resume_account`.
    """
    if not resume_account(account_id):
        fail(404, "not_found", f"account {account_id} not found")
    updated = get_account(account_id)
    if updated is None:
        fail(404, "not_found", f"account {account_id} not found")
    return updated


@router.delete("/accounts/{account_id}")
def delete_account(account_id: str) -> dict[str, Any]:
    """Remove an account from the registry.

    If the account has a stored secret, it is deleted from var/secrets as well.
    Returns 404 if the account was not found.
    """
    if not remove_account(account_id):
        fail(404, "not_found", f"account {account_id} not found")
    return {"removed": True}
