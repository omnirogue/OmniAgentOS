"""LiveSim: live API routing / auth / error shapes on :8485.

Read-only GET probes against the running FastAPI service:

  * positive   — the public dashboard surface (health, today, approvals) serves
                 200 with coherent JSON.
  * permission — /api/accounts enforces auth (401 both with no token and with a
                 bogus token; the July serve-unauth defect stays closed).
  * negative   — unknown routes return the documented 404 envelope
                 ({"error":{"code":"not_found",...}}); a wrong-method GET on a
                 POST-only route returns 405 (envelope code observed as
                 "internal" — documented, not repaired).
  * security   — interactive docs (/docs, /redoc, /openapi.json) are disabled
                 in prod; error bodies do not leak stack traces.
  * boundary   — the /api/board projection stays under the 51MB regression
                 bound; actual size/latency recorded as data, not asserted.
  * concurrency— parallel GETs to /api/health all succeed.
  * drift      — every paramless GET route the checked-in contract
                 (contracts/openapi.json) declares under */health* is actually
                 served live (not 404), and the core routes we exercise are
                 declared in the contract.

No POST/PUT/DELETE is ever issued (live_api refuses them without
allow_write=True, and these tests never pass it). No rows or files are created
on the live system, so cleanup is trivially clean.
"""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

pytestmark = pytest.mark.livesim

REPO = Path(__file__).resolve().parents[3]
OPENAPI_CONTRACT = REPO / "contracts" / "openapi.json"

# Regression bound: the board projection served 51MB before it was fixed.
# Anything at or past the old defect size is a regression; the actual size is
# recorded as a datum (it is environment-dependent), not asserted exactly.
BOARD_SIZE_REGRESSION_BOUND = 51_000_000


def _load_contract_paths() -> dict:
    if not OPENAPI_CONTRACT.exists():
        pytest.skip(f"openapi contract not found at {OPENAPI_CONTRACT}")
    return json.loads(OPENAPI_CONTRACT.read_text())["paths"]


def _require_live(status: int, body, path: str) -> None:
    """Skip (never fail) when the live API is unreachable — status 0 is the
    LiveApi sentinel for a connection-level error."""
    if status == 0:
        pytest.skip(f"live API unreachable for GET {path}: {body}")


# ---------------------------------------------------------------------------
# positive — public dashboard surface serves coherent JSON
# ---------------------------------------------------------------------------


@pytest.mark.positive
def test_health_returns_coherent_json(livesim, live_api):
    """GET /api/health is 200 with the structured health document: status,
    db reachability, worker heartbeat, event-hub block."""
    livesim.target("api")
    status, body, headers = live_api.get("/api/health")
    _require_live(status, body, "/api/health")
    livesim.record(inputs={"path": "/api/health"}, outputs=body)
    assert status == 200
    assert isinstance(body, dict)
    assert body.get("status") == "ok"
    assert isinstance(body.get("db"), bool) and body["db"] is True
    assert isinstance(body.get("worker"), dict) and "alive" in body["worker"]
    assert isinstance(body.get("event_hub"), dict)
    content_type = next((v for k, v in headers.items() if k.lower() == "content-type"), "")
    assert "application/json" in content_type
    livesim.cleanup(True)


@pytest.mark.positive
def test_dashboard_today_returns_coherent_json(livesim, live_api):
    """GET /api/dashboard/today is 200 with today's counters. Counts are
    environment-dependent, so structure (types, non-negativity) is asserted and
    the values are recorded as data."""
    livesim.target("api")
    status, body, _ = live_api.get("/api/dashboard/today")
    _require_live(status, body, "/api/dashboard/today")
    livesim.record(inputs={"path": "/api/dashboard/today"}, outputs=body)
    assert status == 200
    assert isinstance(body, dict)
    for key in ("started_today", "completed_today"):
        assert key in body, f"missing counter {key}"
        assert isinstance(body[key], int) and body[key] >= 0
    for key in ("completion_by_provider", "end_reasons", "escalations"):
        assert isinstance(body.get(key), list), f"{key} should be a list projection"
    livesim.extra(started_today=body["started_today"], completed_today=body["completed_today"])
    livesim.cleanup(True)


@pytest.mark.positive
def test_approvals_list_shape(livesim, live_api):
    """GET /api/approvals is 200 with a list; every entry is a dict carrying an
    apr_-prefixed id and an action_class. Count is a datum, not an assertion."""
    livesim.target("api")
    status, body, _ = live_api.get("/api/approvals")
    _require_live(status, body, "/api/approvals")
    livesim.record(inputs={"path": "/api/approvals"}, outputs={"count": len(body) if isinstance(body, list) else None, "first": body[0] if isinstance(body, list) and body else None})
    assert status == 200
    assert isinstance(body, list)
    for entry in body:
        assert isinstance(entry, dict)
        assert str(entry.get("id", "")).startswith("apr_"), f"unexpected approval id: {entry.get('id')}"
        assert "action_class" in entry
    livesim.extra(pending_approvals=len(body))
    livesim.cleanup(True)


# ---------------------------------------------------------------------------
# permission — auth is enforced on protected routes
# ---------------------------------------------------------------------------


@pytest.mark.permission
@pytest.mark.security
def test_accounts_requires_auth(livesim, live_api):
    """GET /api/accounts is 401 with the structured unauthorized envelope, both
    with no credentials and with a bogus bearer token. (A July defect where it
    served unauthenticated is closed; this pins it closed.) The error body must
    not leak internals."""
    livesim.target("api")
    status_bare, body_bare, _ = live_api.get("/api/accounts")
    _require_live(status_bare, body_bare, "/api/accounts")
    status_bogus, body_bogus, _ = live_api.get(
        "/api/accounts", headers={"Authorization": "Bearer livesim-bogus-token"}
    )
    livesim.record(
        inputs={"path": "/api/accounts", "variants": ["no-token", "bogus-bearer"]},
        outputs={"no_token": [status_bare, body_bare], "bogus": [status_bogus, body_bogus]},
    )
    for status, body in ((status_bare, body_bare), (status_bogus, body_bogus)):
        assert status == 401, f"auth not enforced: got {status}"
        assert isinstance(body, dict) and isinstance(body.get("error"), dict)
        assert body["error"].get("code") == "unauthorized"
        text = json.dumps(body)
        assert "Traceback" not in text and "sqlite" not in text.lower(), "401 body leaks internals"
    livesim.cleanup(True)


# ---------------------------------------------------------------------------
# negative — error envelopes for unknown routes and wrong methods
# ---------------------------------------------------------------------------


@pytest.mark.negative
def test_unknown_route_returns_not_found_envelope(livesim, live_api, livesim_ns):
    """An unknown path returns 404 with the documented error envelope
    {"error": {"code": "not_found", ...}} — consistently, for more than one
    unknown path (one of them namespaced so it can never collide with a real
    route)."""
    livesim.target("api")
    probes = [f"/api/{livesim_ns}", "/api/definitely-not-a-route/livesim"]
    results = {}
    for path in probes:
        status, body, _ = live_api.get(path)
        _require_live(status, body, path)
        results[path] = [status, body]
        assert status == 404, f"{path}: expected 404, got {status}"
        assert isinstance(body, dict) and isinstance(body.get("error"), dict)
        assert body["error"].get("code") == "not_found"
    livesim.record(inputs={"probes": probes}, outputs=results)
    livesim.cleanup(True)  # GETs to nonexistent routes create nothing


@pytest.mark.negative
@pytest.mark.documents_open_defect(
    id="LS-005", verified_fixed_pending_promotion="probed-live-2026-08-06T:GET /api/collab/board 405 error.code=method_not_allowed"
)
def test_wrong_method_returns_405(livesim, live_api):
    """GET on a POST-only route (/api/collab/board per contracts/openapi.json)
    returns 405. CORRECTED 2026-08-06 (found incidentally, same defect-class
    sweep as LS-TEST-001 through 008): this test used to assert the OBSERVED
    (buggy) envelope code `== "internal"` as a bare, non-xfail pass -- the
    exact "test pins pre-fix behaviour as correct" shape this session's
    convention exists to catch, just not one of the eight items named in the
    rework brief. Discovered live: the API now returns error.code =
    "method_not_allowed", so the bare assertion had already started failing.

    First converted to strict xfail (same treatment as LS-007/LS-017/
    LS-018/LS-022), which correctly XPASS(strict)->FAILed since the fix is
    already live -- but that meant this test shipped RED with no way to make
    it plainly green without either overstating a fixed verdict (a product/
    deployment call outside this LiveSim-only lane's authority) or dropping
    the YAML linkage entirely. Uses the third composition state instead
    (verified_fixed_pending_promotion="<receipt>", added for exactly this
    gap and hardened, LS-TEST-010, to require a real evidence string rather
    than a bare boolean): asserts the FIXED code PLAINLY below, passes, and
    keeps the marker -- with a receipt naming what was actually checked and
    when -- so a human still has to decide whether to promote LS-005's YAML
    status to a terminal `fixed`. This test does not make that call for
    them; the receipt and the terminal-summary line it produces
    (tests/livesim/conftest.py::pytest_terminal_summary) are what let that
    human find it instead of trusting an unaudited claim."""
    livesim.target("api")
    contract = _load_contract_paths()
    assert "post" in contract.get("/api/collab/board", {}) and "get" not in contract.get("/api/collab/board", {}), (
        "precondition drifted: /api/collab/board is no longer POST-only in the contract"
    )
    status, body, _ = live_api.get("/api/collab/board")
    _require_live(status, body, "/api/collab/board")
    livesim.record(inputs={"path": "/api/collab/board", "method": "GET"}, outputs=[status, body])
    assert status == 405
    assert isinstance(body, dict) and isinstance(body.get("error"), dict)
    # FIXED behaviour: a method-specific code, not the generic exception envelope.
    assert body["error"].get("code") == "method_not_allowed", (
        f"405 envelope code is not yet method-specific: {body['error'].get('code')!r} "
        "-- LS-005 not fixed here"
    )
    assert body["error"].get("message") == "Method Not Allowed"
    livesim.cleanup(True)


# ---------------------------------------------------------------------------
# security — interactive docs are disabled in prod
# ---------------------------------------------------------------------------


@pytest.mark.security
def test_openapi_docs_surfaces_disabled(livesim, live_api):
    """/docs, /redoc and /openapi.json are 404 on the live service — the schema
    is a checked-in contract (contracts/openapi.json), not a live surface an
    unauthenticated caller can enumerate."""
    livesim.target("api")
    results = {}
    for path in ("/docs", "/redoc", "/openapi.json"):
        status, body, _ = live_api.get(path)
        _require_live(status, body, path)
        results[path] = status
        assert status == 404, f"{path} is exposed on the live service (got {status})"
    livesim.record(inputs={"paths": list(results)}, outputs=results)
    livesim.cleanup(True)


# ---------------------------------------------------------------------------
# boundary — the board projection stays under the old-defect size bound
# ---------------------------------------------------------------------------


@pytest.mark.boundary
def test_board_projection_under_size_bound(livesim, live_api):
    """GET /api/board serves a JSON list under the 51MB regression bound (the
    size it reached before the projection fix). Actual size and latency are
    environment-dependent, so they are recorded as data; the assertions are
    structural: 200, a list of dicts with btk_-prefixed ids, size < bound."""
    import time

    livesim.target("api")
    t0 = time.perf_counter()
    status, body, headers = live_api.get("/api/board", timeout=30.0)
    latency_ms = round((time.perf_counter() - t0) * 1000.0, 1)
    _require_live(status, body, "/api/board")
    size_bytes = len(json.dumps(body)) if not isinstance(body, str) else len(body)
    livesim.record(
        inputs={"path": "/api/board"},
        outputs={"status": status, "size_bytes": size_bytes, "latency_ms": latency_ms,
                 "tasks": len(body) if isinstance(body, list) else None},
    )
    livesim.extra(board_size_bytes=size_bytes, board_latency_ms=latency_ms)
    assert status == 200
    assert isinstance(body, list)
    for entry in body[:25]:  # structural spot-check, not an exhaustive scan
        assert isinstance(entry, dict)
        assert str(entry.get("id", "")).startswith("btk_")
    assert size_bytes < BOARD_SIZE_REGRESSION_BOUND, (
        f"/api/board is {size_bytes}B — at/past the old 51MB projection defect"
    )
    if size_bytes > 1_000_000:
        livesim.note(
            f"OBSERVATION: /api/board full projection is {size_bytes} bytes "
            f"({len(body)} tasks, {latency_ms}ms) — under the 51MB regression bound "
            "but still a multi-MB unpaginated dump."
        )
    livesim.cleanup(True)


# ---------------------------------------------------------------------------
# concurrency — parallel reads do not degrade the health surface
# ---------------------------------------------------------------------------


@pytest.mark.concurrency
def test_concurrent_health_reads_all_succeed(livesim, live_api):
    """8 parallel GET /api/health requests all return 200 with status=ok — the
    live service serves concurrent read traffic without erroring or wedging."""
    livesim.target("api")

    def probe(_: int):
        return live_api.get("/api/health")

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(probe, range(8)))
    statuses = [s for s, _, _ in results]
    if all(s == 0 for s in statuses):
        pytest.skip(f"live API unreachable: {results[0][1]}")
    livesim.record(inputs={"path": "/api/health", "parallel": 8}, outputs={"statuses": statuses})
    assert statuses == [200] * 8, f"concurrent health reads degraded: {statuses}"
    for _, body, _ in results:
        assert isinstance(body, dict) and body.get("status") == "ok"
    livesim.cleanup(True)


# ---------------------------------------------------------------------------
# drift — the checked-in contract matches what the live server serves
# ---------------------------------------------------------------------------


@pytest.mark.positive
@pytest.mark.boundary
def test_openapi_contract_health_routes_served_live(livesim, live_api):
    """Contract→live drift: every paramless GET route under */health* that
    contracts/openapi.json declares is actually served by the live server
    (anything but 404 counts as served; 401/403 would still prove routing).
    Live→contract: the core routes the rest of this module exercises are all
    declared in the contract."""
    livesim.target("api")
    contract = _load_contract_paths()
    health_routes = sorted(
        p for p, methods in contract.items()
        if "get" in methods and "health" in p and "{" not in p
    )
    assert health_routes, "contract declares no paramless GET health routes — drift test is vacuous"
    served = {}
    for path in health_routes:
        status, body, _ = live_api.get(path)
        _require_live(status, body, path)
        served[path] = status
        assert status != 404, f"contract declares GET {path} but live server 404s it (drift)"
    # reverse direction: everything this module hits is in the contract
    for path in ("/api/health", "/api/dashboard/today", "/api/approvals", "/api/board", "/api/accounts"):
        assert path in contract and "get" in contract[path], f"{path} exercised live but absent from contract"
    livesim.record(inputs={"health_routes": health_routes}, outputs=served)
    livesim.extra(health_routes_checked=len(health_routes))
    livesim.cleanup(True)
