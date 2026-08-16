"""Tier3 production-endpoint sweep — live, read-only, serial, low-CPU.

Covers the ground ``test_live_probes.py`` does not: an API route-group
census, the new Team Work OS reads, and a per-page sweep of the Next.js
dashboard at :3003 for Next.js hard-failure markers. Same doubly-opt-in shape
as ``test_live_probes.py`` (file-level ``live`` marker + ``FH_LIVE_PROBES=1``),
plus one thing that file does not do: a module-scoped fixture that reports an
unreachable :8485 or :3003 ONCE, as a single clear "production endpoint down"
failure, instead of a wall of connection-refused errors from every test. It is
a failure and not a skip on purpose: the opt-in key is the caller asserting
that this surface is meant to be up, so a dead box must not record as a clean
tier3 cell (a skip is a favourable absence the ledger cannot distinguish from
"nothing was wrong").

GET-only by contract: nothing here ever issues a POST/PUT/DELETE against the
live control plane.

Two things verified during authoring that changed this file's shape from the
brief's first draft, both recorded here rather than papered over:

* ``GET /openapi.json`` is intentionally disabled live (``docs_url=None``,
  ``openapi_url=None`` in ``omniagentos/api/main.py``) -- confirmed with a
  curl before writing a single assertion, and already documented by
  ``tests/livesim/categories/test_api_endpoints.py::test_openapi_docs_surfaces_disabled``.
  The route-group "census" below reads the checked-in mirror
  (``contracts/openapi.json``) instead, and separately asserts the live 404
  stays true.
* ``GET /api/team/board`` and ``GET /api/team/scoreboard`` are NOT plain
  token-gated reads: ``/api/team`` sits in ``omniagentos.api.main``'s
  ``_GATED_READ_NAMESPACES`` (the ``cross_principal_read`` deny class), which
  refuses even a valid ``X-Session-Token`` presented as the raw machine
  principal with 403 ``system_principal_forbidden`` -- confirmed live, both
  directly against :8485 and through the dashboard's own same-origin proxy at
  :3003 (which answered ``{"error":{"message":"trusted proxy required"}}`` --
  it needs a real browser session, not just the shared token). The test below
  still asserts the brief's literal 200 contract; a live 403 here is not a
  bug in this test, it is the finding.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import httpx
import pytest

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(
        os.environ.get("FH_LIVE_PROBES") != "1",
        reason="production sweep is opt-in: set FH_LIVE_PROBES=1 (run.sh --live-probes)",
    ),
]

_API_BASE = os.environ.get("FH_LIVE_API_BASE", "http://127.0.0.1:8485")
_DASHBOARD_BASE = os.environ.get("FH_LIVE_DASHBOARD_BASE", "http://127.0.0.1:3003")
_TIMEOUT_S = 5.0

_LANE_REPO_ROOT = Path(__file__).resolve().parents[3]
# The live :8485/:3003 pair is always served from the pinned main checkout
# (Git-Isolation-Doctrine: a checkout live daemons run from is never a
# worktree), which mints its own var/secrets/sessions-token. A worktree
# running this lane has no such file of its own, so fall back to the
# well-known serving checkout's path -- read-only, never written here.
_MAIN_CHECKOUT_ROOT = Path("/Users/youruser/OmniAgentOS")
_TOKEN_PATHS = (
    _LANE_REPO_ROOT / "var" / "secrets" / "sessions-token",
    _MAIN_CHECKOUT_ROOT / "var" / "secrets" / "sessions-token",
)

# Verified live with one curl (2026-08-11) against contracts/openapi.json:
# every prefix below except /api/engine is a real top-level route group.
# /api/engine does not exist as its own group -- it is nested under
# /api/routines/engine -- so it is left out per the brief's own "leave it out
# and note it" instruction rather than asserted falsely.
_EXPECTED_ROUTE_GROUPS = (
    "/api/health",
    "/api/team",
    "/api/collab",
    "/api/swarm",
    "/api/routines",
    "/api/goals",
    "/api/knowledge",
    "/api/skills",
    "/api/sessions",
    "/api/memlife",
    "/api/reliability",
    "/api/pulse",
    "/api/accounts",
)

_ERROR_MARKERS = (
    "Application error: a client-side exception",
    "Internal Server Error",
)

_DASHBOARD_PAGES = (
    "/",
    "/team",
    "/board",
    "/today",
    "/goals",
    "/sessions",
    "/approvals",
    "/control-plane",
    "/routines",
    "/knowledge",
    "/memory",
    "/skills",
    "/projects",
    "/reliability",
    "/pulse",
    "/system",
    "/updates",
    "/chats",
    "/swarm",
    "/accounts",
)


def _headers() -> dict[str, str]:
    for candidate in _TOKEN_PATHS:
        try:
            token = candidate.read_text().strip()
        except OSError:
            continue
        if token:
            return {"X-Session-Token": token}
    return {}


@pytest.fixture(scope="module", autouse=True)
def _require_live_services() -> None:
    """An unreachable production endpoint is a FAILURE, not a skip.

    The opt-in gate is the module-level ``FH_LIVE_PROBES`` skipif above: with
    the key unset nothing here runs at all. Once a caller HAS opted in
    (``run.sh --live-probes``), it has asserted the production surface is
    supposed to be up, and "the box did not answer" is the single most
    important thing this sweep can report. Skipping there is the favourable
    absence — the ledger records a clean tier3 cell for a run that measured a
    dead endpoint, and the historical rollup stays false even after a later
    run repairs the present.
    """
    for base, path in ((_API_BASE, "/api/health"), (_DASHBOARD_BASE, "/")):
        try:
            httpx.get(f"{base}{path}", timeout=_TIMEOUT_S)
        except httpx.HTTPError as exc:
            pytest.fail(
                f"production endpoint down: GET {base}{path} is unreachable ({exc}). "
                "FH_LIVE_PROBES=1 opted this sweep in, so an unreachable production "
                "surface is a failure, not a skip."
            )


# ---------------------------------------------------------------------------
# a. live API surface census
# ---------------------------------------------------------------------------


def test_openapi_route_groups_present_in_checked_in_contract() -> None:
    """``/openapi.json`` is disabled live by design; assert that stays true,
    then take the route-group census from the checked-in mirror instead."""
    response = httpx.get(f"{_API_BASE}/openapi.json", timeout=_TIMEOUT_S)
    assert response.status_code == 404, (
        "GET /openapi.json is no longer 404 live "
        f"(got {response.status_code}) -- if docs/openapi were re-enabled in "
        "prod, switch this census to read the live response directly"
    )
    contract_path = _LANE_REPO_ROOT / "contracts" / "openapi.json"
    if not contract_path.exists():
        pytest.skip(f"no checked-in contract at {contract_path}")
    paths = json.loads(contract_path.read_text())["paths"]
    prefixes = {
        "/" + "/".join(path.strip("/").split("/")[:2])
        for path in paths
        if len(path.strip("/").split("/")) >= 2
    }
    missing = [group for group in _EXPECTED_ROUTE_GROUPS if group not in prefixes]
    assert not missing, f"route groups missing from contracts/openapi.json: {missing}"


# ---------------------------------------------------------------------------
# b. live API health
# ---------------------------------------------------------------------------


def test_live_health_status_ok_db_true_worker_alive() -> None:
    response = httpx.get(f"{_API_BASE}/api/health", timeout=_TIMEOUT_S)
    assert response.status_code == 200, response.text
    body = response.json()
    worker = body.get("worker") or {}
    assert body.get("status") == "ok", f"status={body.get('status')!r}"
    assert body.get("db") is True, f"db={body.get('db')!r}"
    assert worker.get("alive") is True, f"worker.alive={worker.get('alive')!r}"


# ---------------------------------------------------------------------------
# c. new-feature live reads
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("path", ["/api/team/board", "/api/team/scoreboard"])
def test_live_team_read_responds_and_parses(path: str) -> None:
    response = httpx.get(f"{_API_BASE}{path}", headers=_headers(), timeout=_TIMEOUT_S)
    assert response.status_code == 200, response.text
    response.json()  # must be parseable JSON


# ---------------------------------------------------------------------------
# d. production dashboard page sweep
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("page", _DASHBOARD_PAGES)
def test_live_dashboard_page_serves_html_without_hard_failure(page: str) -> None:
    response = httpx.get(f"{_DASHBOARD_BASE}{page}", timeout=_TIMEOUT_S)
    assert response.status_code == 200, f"GET {page} -> {response.status_code}"
    content_type = response.headers.get("content-type", "")
    assert "html" in content_type, f"GET {page} content-type {content_type!r} is not html"
    body = response.text
    for marker in _ERROR_MARKERS:
        assert marker not in body, f"GET {page} body contains hard-failure marker {marker!r}"
