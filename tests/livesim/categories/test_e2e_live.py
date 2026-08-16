"""LiveSim: end-to-end user-visible flows on the live API (:8485).

These are the surfaces a human operator actually looks at: the today
dashboard, the board projection, the approvals inbox, and session detail.
Every test here is a read-only monitor — it asserts STRUCTURAL invariants
(counts coherent, sizes bounded, auth enforced, error shapes stable) and
records the live numbers as data rather than pinning environment-dependent
values.

Ground truth verified 2026-08-06 (LS-TEST-001 correction): LS-007/O-1 is
STILL OPEN in this worktree, in main, and on the live server -- today.py
still reads `FROM swarm_attempts` for started_today/completed_today (3x), so
the dashboard reports 0/0 on a day with real session activity. The INTENDED
fix (lane O-1, not yet landed): started_today = COUNT(sessions) WHERE
DATE(created_at)=today; completed_today = same cohort WHERE state='completed'
only (the other six session states — starting/running/awaiting_approval/
resuming/failed/cancelled/killed — deliberately excluded, so completed_today
<= started_today always holds once fixed). completion_by_provider/end_reasons
are intended to stay swarm_attempts-sourced (a different question: the swarm
loop's own per-attempt outcome distribution). See
test_dashboard_today_matches_live_db_sessions below, which asserts this
FIXED equality invariant under strict xfail (LS-007) rather than the
tautological `>=` an earlier pass on this file shipped by mistake.
  * /api/board was 51MB before the projection fix; live payload today is
    ~4.8MB — under the 5MB regression bound but creeping back up.
  * /api/sessions/{id} requires auth and 401s before disclosing whether the
    id exists (no enumeration oracle).
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime

import pytest

pytestmark = pytest.mark.livesim

BOARD_SIZE_LIMIT_BYTES = 5 * 1024 * 1024  # regression guard on the 51MB->2MB fix
DASHBOARD_URL = "http://127.0.0.1:3003"


@pytest.mark.e2e_live
@pytest.mark.degradation
def test_dashboard_shell_loads_and_records_api_reachability(livesim):
    """OBSERVATIONAL, read-only: the Next.js dashboard shell (:3003) serves, and
    we record whether its embedded /api/* fetch path is reachable. This is the
    repeatable, no-browser counterpart to the browser-operator finding LS-003
    (every UI /api/* call 403s 'trusted proxy required'): if the shell 200s but
    the API leg is unreachable through the dashboard's own proxy, the UI is dark
    for users even though direct :8485 is healthy. Never logs in, never mutates."""
    livesim.target("ui", "api")
    # 1) shell loads?
    shell_status = 0
    shell_bytes = 0
    try:
        with urllib.request.urlopen(DASHBOARD_URL, timeout=8) as r:  # noqa: S310 localhost
            body = r.read()
            shell_status, shell_bytes = r.status, len(body)
    except urllib.error.HTTPError as e:
        shell_status = e.code
    except (urllib.error.URLError, OSError) as e:
        livesim.note(f"dashboard :3003 unreachable: {e}")
        pytest.skip("dashboard :3003 not serving")
    # 2) does the dashboard's server-side API proxy answer? Probe a path the UI
    #    itself polls; the dashboard proxies /api/* to :8485 with a trusted-hop
    #    header, so a 403 here while direct :8485 is 200 IS the LS-003 signature.
    proxy_status = 0
    proxy_body = ""
    for candidate in ("/api/health", "/api/dashboard/today"):
        try:
            with urllib.request.urlopen(DASHBOARD_URL + candidate, timeout=8) as r:  # noqa: S310
                proxy_status, proxy_body = r.status, r.read()[:200].decode("utf-8", "replace")
                break
        except urllib.error.HTTPError as e:
            proxy_status = e.code
            proxy_body = (e.read()[:200].decode("utf-8", "replace") if e.fp else "")
            break
        except (urllib.error.URLError, OSError):
            continue
    out = {
        "shell_status": shell_status,
        "shell_bytes": shell_bytes,
        "proxy_status": proxy_status,
        "proxy_body": proxy_body[:160],
        "trusted_proxy_403": proxy_status == 403 and "trusted proxy" in proxy_body.lower(),
    }
    livesim.evidence("dashboard-shell-check.json", json.dumps(out, indent=2))
    livesim.record(inputs={"url": DASHBOARD_URL}, outputs=out)
    if out["trusted_proxy_403"]:
        livesim.note("DEFECT LS-003 reproduced: dashboard proxy 403s 'trusted proxy required'")
    # The shell must at least serve HTML; the API-leg status is recorded as the datum.
    assert shell_status == 200, f"dashboard shell did not load: {shell_status}"


def _utc_today() -> str:
    return datetime.now(UTC).date().isoformat()


# ---------------------------------------------------------------------------
# Dashboard today — internal coherence + cross-source check against the DB
# ---------------------------------------------------------------------------


@pytest.mark.positive
@pytest.mark.e2e_live
def test_dashboard_today_counts_are_internally_coherent(livesim, live_api):
    """GET /api/dashboard/today returns counts that cannot contradict each
    other: non-negative ints, completed <= started overall and per provider,
    escalation entries well-formed. Live numbers are recorded as data."""
    livesim.target("api")
    status, body, _ = live_api.get("/api/dashboard/today")
    if status == 0:
        pytest.skip(f"live API unreachable: {body}")
    assert status == 200, body
    assert isinstance(body, dict), type(body)

    started = body["started_today"]
    completed = body["completed_today"]
    assert isinstance(started, int) and started >= 0
    assert isinstance(completed, int) and completed >= 0
    assert completed <= started, f"completed_today {completed} > started_today {started}"

    for row in body["completion_by_provider"]:
        assert row["completed"] <= row["started"], row
        assert row["started"] >= 0 and row["completed"] >= 0, row

    assert isinstance(body["end_reasons"], list) and len(body["end_reasons"]) <= 3
    assert isinstance(body["escalations"], list)
    for esc in body["escalations"]:
        assert esc.get("id") and esc.get("created_at"), esc

    awaiting = body["sessions_awaiting_approval"]
    assert isinstance(awaiting, int) and awaiting >= 0

    out = {
        "started_today": started,
        "completed_today": completed,
        "providers": len(body["completion_by_provider"]),
        "escalations": len(body["escalations"]),
        "sessions_awaiting_approval": awaiting,
    }
    livesim.record(inputs={"path": "/api/dashboard/today"}, outputs=out)


@pytest.mark.boundary
@pytest.mark.e2e_live
@pytest.mark.documents_open_defect(id="LS-007")
@pytest.mark.xfail(
    reason="LS-007 open (fix in flight, lane O-1) -- /api/dashboard/today's started_today/"
    "completed_today are still swarm_attempts-sourced (0/0 on a day with real session "
    "activity); see docs/testing/LIVESIM-ISSUES.yaml. strict=True: this test asserts the "
    "FIXED equality invariant, so it XPASS->FAILs the run the instant O-1 lands.",
    strict=True,
)
def test_dashboard_today_matches_live_db_sessions(livesim, live_api, live_db_ro):
    """CORRECTED 2026-08-06 (LS-TEST-001, blocker): the previous version of
    this test asserted `db_count >= api_count` for both started/completed.
    While the route is still swarm_attempts-sourced (LS-007 open), the API
    returns 0, and `N >= 0` is a TAUTOLOGY for any N -- the test could never
    go red no matter how broken the route was, and it deleted the ONE thing
    that used to catch this (a `sessions_started_today > 0 and api_started
    == 0` favourable-absence note). A third assertion additionally compared
    DB to DB and never touched the product at all. The docstring claiming
    "fixed 2026-08-06 for LS-007/O-1" was also false in every checkout
    (worktree, main, and the live server all still read `FROM swarm_attempts`
    in omniagentos/api/routes/today.py as of this commit).

    Tightened to EQUALITY (api_started == db_sessions_started_today, same for
    completed) -- `>=` against an append-only source will never discriminate
    a route that is wired to the wrong table from one that reports the right
    numbers a moment late; only equality does. There is a small residual
    race (a session could be created between the API read and the DB read
    even after O-1 lands, making the two reads briefly diverge) -- accepted
    deliberately: this is an xfail-guarded coherence check, not a hard merge
    gate, and a rare non-deterministic XFAIL-instead-of-XPASS is a vastly
    smaller cost than the tautology it replaces. Wrapped in strict xfail
    (not a bare assertion) so the suite ships green while LS-007 is open and
    self-alarms (XPASS->FAIL) the moment O-1 lands, instead of staying
    silently green forever the way the tautology did.
    """
    livesim.target("api", "db")
    day_before = _utc_today()
    status, body, _ = live_api.get("/api/dashboard/today")
    if status == 0:
        pytest.skip(f"live API unreachable: {body}")
    assert status == 200, body
    api_started = body["started_today"]
    api_completed = body["completed_today"]

    db_sessions_started_today = live_db_ro.execute(
        "SELECT COUNT(*) n FROM sessions WHERE DATE(created_at) = ?",
        (day_before,),
    ).fetchone()["n"]
    db_sessions_completed_today = live_db_ro.execute(
        "SELECT COUNT(*) n FROM sessions WHERE DATE(created_at) = ? AND state = 'completed'",
        (day_before,),
    ).fetchone()["n"]
    db_swarm_today = live_db_ro.execute(
        "SELECT COUNT(*) n FROM swarm_attempts WHERE DATE(started_at) = ?",
        (day_before,),
    ).fetchone()["n"]

    if _utc_today() != day_before:
        pytest.skip("UTC date rolled over between API and DB reads; comparison invalid")

    out = {
        "api_started_today": api_started,
        "api_completed_today": api_completed,
        "db_sessions_started_today": db_sessions_started_today,
        "db_sessions_completed_today": db_sessions_completed_today,
        "db_swarm_attempts_today": db_swarm_today,
    }
    livesim.record(inputs={"utc_day": day_before}, outputs=out)
    if db_sessions_started_today > 0 and api_started == 0:
        livesim.note(
            "LS-007 still open: dashboard shows 0 started while "
            f"{db_sessions_started_today} sessions started today -- "
            "/api/dashboard/today is still swarm_attempts-sourced."
        )

    # FIXED behaviour: the API's counters must equal the sessions-table
    # counts they are supposed to be sourced from, not merely be <= them.
    assert api_started == db_sessions_started_today, (
        f"API started_today ({api_started}) != DB sessions started today "
        f"({db_sessions_started_today})"
    )
    assert api_completed == db_sessions_completed_today, (
        f"API completed_today ({api_completed}) != DB sessions completed today "
        f"({db_sessions_completed_today})"
    )
    # Real (non-vacuous alongside the two checks above, not a substitute for
    # them) DB-side cohort invariant: completed_today is scoped to the SAME
    # cohort as started_today (sessions created today, state='completed'
    # only), so completed <= started must hold on the DB read too.
    assert db_sessions_completed_today <= db_sessions_started_today, (
        f"DB sessions completed today ({db_sessions_completed_today}) exceeds "
        f"DB sessions started today ({db_sessions_started_today}) — cohort mismatch"
    )


# ---------------------------------------------------------------------------
# Board projection — the 51MB->2MB fix must not regress
# ---------------------------------------------------------------------------


@pytest.mark.boundary
@pytest.mark.e2e_live
def test_board_projection_stays_small_and_well_formed(livesim, live_api):
    """GET /api/board serializes to < 5MB (guard on the 51MB->2MB projection
    fix) and is a list of task dicts with unique ids and status fields."""
    livesim.target("api")
    status, body, _ = live_api.get("/api/board", timeout=30.0)
    if status == 0:
        pytest.skip(f"live API unreachable: {body}")
    assert status == 200, body
    assert isinstance(body, list), f"/api/board must project a list, got {type(body)}"

    serialized = json.dumps(body)
    size_bytes = len(serialized.encode("utf-8"))
    assert size_bytes < BOARD_SIZE_LIMIT_BYTES, (
        f"board projection is {size_bytes} bytes (>= {BOARD_SIZE_LIMIT_BYTES}) — "
        "the 51MB payload regression is back"
    )

    ids = [t.get("id") for t in body]
    assert all(ids), "every board task must carry an id"
    assert len(ids) == len(set(ids)), "board task ids must be unique"
    for task in body[:50]:  # shape-check a prefix; full scan adds nothing
        assert "status" in task and "created_at" in task, task.get("id")

    out = {"tasks": len(body), "size_bytes": size_bytes,
           "pct_of_limit": round(size_bytes / BOARD_SIZE_LIMIT_BYTES * 100, 1)}
    livesim.record(inputs={"path": "/api/board"}, outputs=out)
    if size_bytes > BOARD_SIZE_LIMIT_BYTES * 0.8:
        livesim.note(
            f"WATCH: board projection at {out['pct_of_limit']}% of the 5MB bound "
            f"({size_bytes} bytes, {len(body)} tasks) — the 2MB post-fix payload "
            "has crept back up; growth-rate check recommended."
        )


# ---------------------------------------------------------------------------
# Session lifecycle — terminal states must be terminally consistent
# ---------------------------------------------------------------------------


@pytest.mark.positive
@pytest.mark.e2e_live
def test_completed_sessions_are_terminal_consistent(livesim, live_db_ro):
    """A session in state='completed' must look finished: end markers present
    (updated_at set, not before created_at) and no kill attribution — a
    completed row with killed_by or kill_requested is a state-machine breach."""
    livesim.target("db")
    rows = live_db_ro.execute(
        "SELECT id, created_at, updated_at, killed_by, kill_requested "
        "FROM sessions WHERE state='completed' ORDER BY updated_at DESC LIMIT 50"
    ).fetchall()
    if not rows:
        pytest.skip("no completed sessions in the live DB to examine")

    violations = []
    for r in rows:
        if not r["updated_at"]:
            violations.append((r["id"], "missing updated_at end marker"))
        elif r["created_at"] and r["updated_at"] < r["created_at"]:
            violations.append((r["id"], f"updated_at {r['updated_at']} < created_at {r['created_at']}"))
        if r["killed_by"] is not None:
            violations.append((r["id"], f"completed but killed_by={r['killed_by']}"))
        if r["kill_requested"]:
            violations.append((r["id"], "completed but kill_requested=1"))

    total_completed = live_db_ro.execute(
        "SELECT COUNT(*) n FROM sessions WHERE state='completed'"
    ).fetchone()["n"]
    out = {"examined": len(rows), "total_completed": total_completed,
           "violations": violations, "most_recent": rows[0]["id"]}
    livesim.record(inputs={"query": "recent 50 completed sessions"}, outputs=out)
    assert not violations, violations


# ---------------------------------------------------------------------------
# Approvals inbox
# ---------------------------------------------------------------------------


@pytest.mark.positive
@pytest.mark.e2e_live
def test_approvals_inbox_is_well_formed(livesim, live_api):
    """GET /api/approvals returns a list of approval dicts with unique ids,
    an action_class, and params_json that parses as JSON when present."""
    livesim.target("api")
    status, body, _ = live_api.get("/api/approvals")
    if status == 0:
        pytest.skip(f"live API unreachable: {body}")
    assert status == 200, body
    assert isinstance(body, list), type(body)

    ids = []
    for entry in body:
        assert isinstance(entry, dict), entry
        assert entry.get("id"), entry
        ids.append(entry["id"])
        assert "action_class" in entry and "proposed_action" in entry, entry["id"]
        pj = entry.get("params_json")
        if pj:
            parsed = json.loads(pj)  # raises -> fail: corrupt approval payload
            assert isinstance(parsed, (dict, list)), entry["id"]
    assert len(ids) == len(set(ids)), "approval ids must be unique"

    livesim.record(inputs={"path": "/api/approvals"},
                   outputs={"pending": len(body), "ids_preview": ids[:5]})


# ---------------------------------------------------------------------------
# Auth + error-shape invariants a user-facing API must hold
# ---------------------------------------------------------------------------


@pytest.mark.security
@pytest.mark.permission
@pytest.mark.e2e_live
def test_session_detail_requires_auth_even_for_unknown_ids(livesim, live_api, live_db_ro):
    """GET /api/sessions/{id} is auth-gated (401) for BOTH a real session id
    and a nonexistent one — auth is checked before existence, so an
    unauthenticated caller cannot enumerate which session ids exist."""
    livesim.target("api", "db")
    row = live_db_ro.execute(
        "SELECT id FROM sessions WHERE state='completed' ORDER BY updated_at DESC LIMIT 1"
    ).fetchone()
    real_id = row["id"] if row else "ses_none_in_db"

    s_real, b_real, _ = live_api.get(f"/api/sessions/{real_id}")
    s_fake, b_fake, _ = live_api.get("/api/sessions/ses_livesim_does_not_exist")
    if s_real == 0 or s_fake == 0:
        pytest.skip("live API unreachable")

    assert s_real == 401, f"real session id leaked without auth: {s_real} {b_real}"
    assert s_fake == 401, f"unknown id gave {s_fake}, not 401 — status oracle"
    # identical status for real vs fake => no existence oracle
    assert s_real == s_fake
    livesim.record(
        inputs={"real_id": real_id, "fake_id": "ses_livesim_does_not_exist"},
        outputs={"real_status": s_real, "fake_status": s_fake},
    )


@pytest.mark.negative
@pytest.mark.e2e_live
def test_wrong_method_and_unknown_route_error_shapes(livesim, live_api, livesim_ns):
    """User-visible failure modes stay structured: GET on the POST-only
    /api/collab/board is 405 (never a 500), and an unknown path is a 404 with
    the standard {"error": {"code": "not_found"}} envelope."""
    livesim.target("api")
    s_405, b_405, _ = live_api.get("/api/collab/board")
    s_404, b_404, _ = live_api.get(f"/api/nope/{livesim_ns}")
    if s_405 == 0 or s_404 == 0:
        pytest.skip("live API unreachable")

    assert s_405 == 405, f"GET /api/collab/board gave {s_405} (board reads live at GET /api/board)"
    assert s_404 == 404, (s_404, b_404)
    assert isinstance(b_404, dict) and b_404.get("error", {}).get("code") == "not_found", b_404
    livesim.record(
        inputs={"wrong_method": "GET /api/collab/board", "unknown_path": f"/api/nope/{livesim_ns}"},
        outputs={"wrong_method_status": s_405, "unknown_path_status": s_404,
                 "unknown_path_body": b_404},
    )
    livesim.cleanup(True)  # nothing created; ns only appeared in a GET path


# ---------------------------------------------------------------------------
# Concurrency — the dashboard is a fan-out surface; parallel reads must agree
# ---------------------------------------------------------------------------


@pytest.mark.concurrency
@pytest.mark.e2e_live
def test_dashboard_survives_parallel_reads(livesim, live_api):
    """Six concurrent GET /api/dashboard/today requests all succeed, share
    one schema, and each response is independently coherent — no torn reads
    or per-request 500s under read concurrency."""
    livesim.target("api")

    def fetch(_i: int):
        return live_api.get("/api/dashboard/today", timeout=15.0)

    with ThreadPoolExecutor(max_workers=6) as pool:
        results = list(pool.map(fetch, range(6)))

    if all(status == 0 for status, _, _ in results):
        pytest.skip("live API unreachable for all parallel reads")

    statuses = [s for s, _, _ in results]
    assert all(s == 200 for s in statuses), statuses
    keysets = {tuple(sorted(body.keys())) for _, body, _ in results}
    assert len(keysets) == 1, f"parallel responses disagree on schema: {keysets}"
    for _, body, _ in results:
        assert body["completed_today"] <= body["started_today"]
        assert body["sessions_awaiting_approval"] >= 0

    livesim.record(
        inputs={"parallel_requests": 6, "path": "/api/dashboard/today"},
        outputs={"statuses": statuses,
                 "started_values": [b["started_today"] for _, b, _ in results]},
    )
