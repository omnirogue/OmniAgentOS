"""Jira read-only HTTP surface (JG-1).

Public routes only (C7/C8):
  GET /api/jira/health
  GET /api/jira/projects
  GET /api/jira/projects/{key}/statuses

No issue-get and no raw-JQL passthrough.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter

from omniagentos.api.routes.control import fail
from omniagentos.connectors import broker, load_registry
from omniagentos.connectors.jira_client import JiraClient, JiraError

router = APIRouter(prefix="/api/jira", tags=["jira"])
_LOG = logging.getLogger(__name__)


def _client() -> JiraClient:
    return JiraClient()


def _configured() -> bool:
    """Resolve Jira's auth pair through the broker before constructing a client."""
    capability = load_registry().capability("jira.read")
    try:
        return bool(
            broker.resolve_one_for(capability, "JIRA_API_TOKEN")
            and broker.resolve_one_for(capability, "JIRA_EMAIL")
        )
    except broker.BrokerDenied:
        return False


def _sanitized_failure(exc: Exception) -> str:
    """Never surface secret-bearing exception text to clients or logs."""
    if isinstance(exc, JiraError):
        return str(exc)
    if isinstance(exc, Exception):
        # Fixed shape only — do not embed str(exc) (may carry request URL/auth).
        return f"Jira unavailable ({type(exc).__name__})"
    return "Jira unavailable"


@router.get("/health")
def _jira_health() -> dict[str, Any]:
    """Probe Jira credentials via ``myself``; return displayName + accountId."""
    if not _configured():
        fail(
            503,
            "jira_unconfigured",
            "Jira credentials not configured",
            {"configured": False},
        )
    try:
        with _client() as client:
            me = client.myself()
    except JiraError as exc:
        # Log only the sanitized message (JiraError is already scrubbed).
        _LOG.warning("jira health failed: %s", exc)
        code = 401 if exc.status_code == 401 else 502
        fail(code, "jira_error", str(exc), {"status_code": exc.status_code})
    except Exception as exc:  # noqa: BLE001 — sanitize any unexpected failure
        msg = _sanitized_failure(exc)
        _LOG.warning("jira health failed: %s", msg)
        fail(502, "jira_error", msg, None)
    return {
        "ok": True,
        "displayName": me.display_name,
        "accountId": me.account_id,
    }


@router.get("/projects")
def _jira_projects() -> list[dict[str, Any]]:
    """List live Jira projects visible to the bot account."""
    try:
        with _client() as client:
            projects = client.list_projects()
    except JiraError as exc:
        _LOG.warning("jira list projects failed: %s", exc)
        fail(502, "jira_error", str(exc), {"status_code": exc.status_code})
    except Exception as exc:  # noqa: BLE001
        msg = _sanitized_failure(exc)
        _LOG.warning("jira list projects failed: %s", msg)
        fail(502, "jira_error", msg, None)
    return [
        {
            "id": p.id,
            "key": p.key,
            "name": p.name,
            "projectTypeKey": p.project_type_key,
        }
        for p in projects
    ]


@router.get("/projects/{key}/statuses")
def _jira_project_statuses(key: str) -> list[dict[str, Any]]:
    """Statuses for a project, deduplicated by name (queue edit dialog — U5)."""
    try:
        with _client() as client:
            statuses = client.project_statuses(key)
    except JiraError as exc:
        _LOG.warning("jira project statuses failed: %s", exc)
        code = 404 if exc.status_code == 404 else 502
        fail(code, "jira_error", str(exc), {"status_code": exc.status_code, "key": key})
    except Exception as exc:  # noqa: BLE001
        msg = _sanitized_failure(exc)
        _LOG.warning("jira project statuses failed: %s", msg)
        fail(502, "jira_error", msg, {"key": key})
    return [
        {
            "id": s.id,
            "name": s.name,
            "statusCategoryKey": s.status_category_key,
        }
        for s in statuses
    ]


__all__ = ["router"]
