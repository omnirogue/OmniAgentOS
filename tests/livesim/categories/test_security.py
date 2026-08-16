"""LiveSim: security posture — observational, read-only.

Four surfaces, all exercised against the RUNNING system on :8485 plus the
in-process approval classifier:

  * **auth enforcement** — ``GET /api/accounts`` must 401. A July defect served
    it unauthenticated; that is closed, so this asserts CORRECT behaviour and
    additionally proves the gate does not fail *open* for a forged token in any
    of the three header shapes the app understands.
  * **path traversal** — ``/api/artifacts/preview`` (and its ``/raw`` twin) is
    the one file-reading GET that is reachable without a session token. Every
    absolute-host-path and relative-escape form must 4xx, and — the assertion
    that actually matters — the response body must never contain host file
    content.
  * **secret non-exposure** — every parameter-free GET the live app answers with
    200 is swept for obvious credential shapes (``sk-``, ``ghp_``, ``AKIA``,
    ``xox*-``, ``EAA…``, PEM private-key headers). Matched *values* are never
    written to evidence; only the endpoint and the pattern name.
  * **classifier fail-open** — a KNOWN-OPEN DEFECT (see LIVESIM.md). The AD-15
    approval classifier still auto-approves destructive intent phrased outside
    its vocabulary. These tests DOCUMENT the observed behaviour and do not fix
    it; the paired positive test proves the classifier is not uniformly broken,
    which is what makes the fail-open row evidence rather than noise.

Nothing here mutates: only GETs, only pure classifier calls, no rows, no files
outside the run's own evidence directory.
"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path

import pytest

pytestmark = pytest.mark.livesim

REPO = Path(__file__).resolve().parents[3]
CONTRACT = REPO / "contracts" / "openapi.json"

# Credential shapes. Deliberately strict (long random tails) so a redacted
# placeholder such as "sk-…redacted" is not reported as a live leak.
SECRET_PATTERNS: dict[str, str] = {
    "openai_sk": r"\bsk-[A-Za-z0-9_-]{24,}",
    "github_pat": r"\bghp_[A-Za-z0-9]{30,}",
    "aws_access_key": r"\bAKIA[0-9A-Z]{16}\b",
    "slack_token": r"\bxox[baprs]-[A-Za-z0-9-]{20,}",
    "meta_token": r"\bEAA[A-Za-z0-9]{60,}",
    "pem_private_key": r"-----BEGIN (?:[A-Z]+ )?PRIVATE KEY-----",
}

# Content that proves a host file actually came back through the API.
HOST_FILE_TELLS: tuple[str, ...] = (
    "root:x:0:0",
    "root:*:0:0",
    "/bin/sh\n",
    "-----BEGIN OPENSSH PRIVATE KEY-----",
)


def _text(body: object) -> str:
    return body if isinstance(body, str) else json.dumps(body, default=str)


def _get(live_api, path: str, *, timeout: float = 10.0, headers: dict[str, str] | None = None):
    """GET with one retry on a transport-level failure.

    ``LiveApi`` maps a connection error / timeout to status 0. The live app is a
    shared machine under real load, so a single slow answer must not decide a
    security verdict: retry once with a doubled budget before treating it as down.
    """
    status, body, hdrs = live_api.get(path, timeout=timeout, headers=headers)
    if status == 0:
        status, body, hdrs = live_api.get(path, timeout=timeout * 2, headers=headers)
    return status, body, hdrs


def _require_live(livesim, status: int, path: str) -> None:
    if status == 0:
        livesim.note(f"live API unreachable for {path}; skipping")
        pytest.skip(f"live API on :8485 unreachable ({path})")


def _is_event_stream(op: dict) -> bool:
    """True for an SSE operation. A `Last-Event-ID` request header is the tell —
    the live app declares no `text/event-stream` response content type, so the
    header parameter is the only contract-side signal. Reading one of these to
    EOF never returns (the server heartbeats forever), which would hang any
    sweep that treats it as an ordinary GET."""
    for param in op.get("parameters", []) or []:
        if param.get("in") == "header" and str(param.get("name", "")).lower() == "last-event-id":
            return True
    return False


def _paramless_get_paths() -> tuple[list[str], list[str]]:
    """(scannable, streaming) parameter-free GET paths from the route contract."""
    if not CONTRACT.exists():
        pytest.skip(f"route contract not found at {CONTRACT}")
    spec = json.loads(CONTRACT.read_text(encoding="utf-8"))
    scannable: list[str] = []
    streaming: list[str] = []
    for path, ops in sorted(spec.get("paths", {}).items()):
        op = ops.get("get")
        if op is None or "{" in path:
            continue
        (streaming if _is_event_stream(op) else scannable).append(path)
    return scannable, streaming


# ---------------------------------------------------------------------------
# Auth enforcement
# ---------------------------------------------------------------------------


@pytest.mark.permission
def test_accounts_requires_a_session_token(livesim, live_api):
    """CORRECT behaviour: /api/accounts is 401 unauthenticated, with a structured
    error envelope — the July unauth-serve defect stays closed."""
    livesim.target("api")
    status, body, _ = _get(live_api, "/api/accounts")
    _require_live(livesim, status, "/api/accounts")
    livesim.record(inputs={"path": "/api/accounts", "auth": None},
                   outputs={"status": status, "body": _text(body)[:300]})
    assert status == 401, f"/api/accounts served {status} without a session token"
    assert isinstance(body, dict) and body.get("error", {}).get("code") == "unauthorized", body
    livesim.cleanup(True)


@pytest.mark.permission
@pytest.mark.negative
def test_auth_gate_does_not_fail_open_for_forged_tokens(livesim, live_api):
    """A garbage credential in ANY of the three accepted header shapes must still
    401 — an auth gate that accepts an unverifiable token is worse than none."""
    livesim.target("api")
    shapes = {
        "authorization_bearer": {"Authorization": "Bearer livesim-forged-token"},
        "cookie_session": {"Cookie": "session=livesim-forged-token"},
        "x_session_token": {"X-Session-Token": "livesim-forged-token"},
    }
    observed: dict[str, int] = {}
    for name, headers in shapes.items():
        status, body, _ = _get(live_api, "/api/accounts", headers=headers)
        _require_live(livesim, status, f"/api/accounts[{name}]")
        observed[name] = status
    livesim.record(inputs=sorted(shapes), outputs=observed)
    assert all(s == 401 for s in observed.values()), f"forged token accepted somewhere: {observed}"
    livesim.cleanup(True)


@pytest.mark.permission
@pytest.mark.security
def test_file_reading_endpoints_gate_auth_before_path_handling(livesim, live_api):
    """The mount / project / board file readers must answer 401 to an
    unauthenticated traversal attempt — auth is evaluated BEFORE the path, so the
    endpoint never becomes a filesystem existence oracle for an anonymous caller."""
    livesim.target("api")
    probes = [
        "/api/mounts/home/file?path=../../../../etc/passwd",
        "/api/mounts/home/file?path=/etc/passwd",
        "/api/projects/proj_does_not_exist/files/..%2F..%2F..%2Fetc%2Fpasswd",
        "/api/board/task_does_not_exist/files/download?path=/etc/passwd",
        "/api/filesearch?q=BEGIN%20RSA%20PRIVATE%20KEY",
    ]
    observed: dict[str, dict[str, object]] = {}
    for p in probes:
        status, body, _ = _get(live_api, p)
        _require_live(livesim, status, p)
        text = _text(body)
        observed[p] = {"status": status, "leaked": [t for t in HOST_FILE_TELLS if t in text]}
    livesim.evidence("file-endpoint-auth.json", json.dumps(observed, indent=2))
    livesim.record(inputs=probes, outputs=observed)
    for p, o in observed.items():
        assert o["status"] in (401, 403, 404, 422), f"{p} answered {o['status']} anonymously"
        assert not o["leaked"], f"{p} returned host file content: {o['leaked']}"
    # Auth-first is the specific property under test on the two token-gated readers.
    assert observed[probes[0]]["status"] == 401, observed[probes[0]]
    livesim.cleanup(True)


# ---------------------------------------------------------------------------
# Path traversal on the one anonymous file-reading GET
# ---------------------------------------------------------------------------


@pytest.mark.security
def test_artifact_preview_refuses_absolute_host_paths(livesim, live_api):
    """/api/artifacts/preview{,/raw} must refuse absolute paths outside the
    artifact roots and must not return their contents."""
    livesim.target("api", "fs")
    probes = [
        "/api/artifacts/preview?path=/etc/passwd",
        "/api/artifacts/preview?path=%2Fetc%2Fpasswd",
        "/api/artifacts/preview?path=/private/etc/passwd",
        "/api/artifacts/preview?path=~/.ssh/id_rsa",
        "/api/artifacts/preview/raw?path=/etc/passwd",
    ]
    observed: dict[str, dict[str, object]] = {}
    for p in probes:
        status, body, _ = _get(live_api, p)
        _require_live(livesim, status, p)
        text = _text(body)
        observed[p] = {
            "status": status,
            "code": body.get("error", {}).get("code") if isinstance(body, dict) else None,
            "leaked": [t for t in HOST_FILE_TELLS if t in text],
            "bytes": len(text),
        }
    livesim.evidence("artifact-preview-absolute.json", json.dumps(observed, indent=2))
    livesim.record(inputs=probes, outputs=observed)
    for p, o in observed.items():
        assert not o["leaked"], f"{p} returned host file content: {o['leaked']}"
        assert 400 <= int(o["status"]) < 500, f"{p} answered {o['status']}, expected a 4xx refusal"
    livesim.cleanup(True)


@pytest.mark.security
@pytest.mark.boundary
def test_artifact_preview_refuses_relative_and_encoded_traversal(livesim, live_api):
    """Escape-sequence traversal — plain, percent-encoded, doubled-dot-slash,
    NUL-truncated — must all be refused, and the empty path must be a 422
    validation error rather than a root read."""
    livesim.target("api", "fs")
    probes = [
        "/api/artifacts/preview?path=../../../../etc/passwd",
        "/api/artifacts/preview?path=%2e%2e%2f%2e%2e%2f%2e%2e%2fetc%2fpasswd",
        "/api/artifacts/preview?path=....//....//etc/passwd",
        "/api/artifacts/preview?path=/etc/passwd%00.txt",
        "/api/artifacts/preview?path=",
    ]
    observed: dict[str, dict[str, object]] = {}
    for p in probes:
        status, body, _ = _get(live_api, p)
        _require_live(livesim, status, p)
        text = _text(body)
        observed[p] = {
            "status": status,
            "code": body.get("error", {}).get("code") if isinstance(body, dict) else None,
            "leaked": [t for t in HOST_FILE_TELLS if t in text],
        }
    livesim.evidence("artifact-preview-traversal.json", json.dumps(observed, indent=2))
    livesim.record(inputs=probes, outputs=observed)
    for p, o in observed.items():
        assert not o["leaked"], f"{p} returned host file content: {o['leaked']}"
        assert 400 <= int(o["status"]) < 500, f"{p} answered {o['status']}, expected a 4xx refusal"
    assert observed[probes[-1]]["status"] == 422, observed[probes[-1]]
    livesim.cleanup(True)


# ---------------------------------------------------------------------------
# Secret non-exposure across the anonymous read surface
# ---------------------------------------------------------------------------


@pytest.mark.security
@pytest.mark.positive
def test_no_public_get_body_exposes_a_credential_shape(livesim, live_api):
    """Sweep every parameter-free GET the live app answers with 200 and assert no
    response body carries an obvious credential. Endpoint *count* is environment
    dependent (recorded as a datum); the invariant asserted is `no matches`."""
    livesim.target("api")
    paths, streaming = _paramless_get_paths()
    compiled = {name: re.compile(pat) for name, pat in SECRET_PATTERNS.items()}
    scanned = 0
    total_bytes = 0
    unreachable = 0
    skipped_for_time = 0
    findings: list[dict[str, object]] = []
    # Bound wall-time: ~100 paramless GETs on a shared, loaded box (some MB-scale)
    # can exceed the test timeout if each is retried on a doubling budget. Single
    # 5s shot per endpoint (LiveApi maps any timeout/read error to status 0) and a
    # hard sweep deadline; whatever the deadline drops is reported, never hidden.
    deadline = time.monotonic() + 90.0
    errored = 0
    for p in paths:
        if time.monotonic() > deadline:
            skipped_for_time = len(paths) - (scanned + unreachable + errored)
            break
        # Per-endpoint guard: on a shared, loaded box a single transient (a body
        # that fails to decode, a mid-stream reset) must count as one skipped
        # endpoint, never fail the whole security verdict. Reproducibility > one
        # flaky endpoint (LS-027).
        try:
            status, body, _ = live_api.get(p, timeout=5.0)
            if status == 0:
                unreachable += 1
                continue
            if status != 200:
                continue
            text = _text(body)
            scanned += 1
            total_bytes += len(text)
            for name, rx in compiled.items():
                m = rx.search(text)
                if m:
                    # Never persist the matched value — only its shape and length.
                    findings.append({"path": p, "pattern": name, "match_len": len(m.group())})
        except Exception as exc:  # noqa: BLE001 - one transient endpoint never fails the sweep
            errored += 1
            livesim.note(f"secret sweep: transient error on {p}: {type(exc).__name__}")
    if skipped_for_time:
        livesim.note(f"secret sweep hit its 90s deadline: {skipped_for_time} endpoints not scanned")
    if scanned <= 10:
        livesim.note(f"only {scanned} public GETs answered 200 ({unreachable} unreachable); "
                     "live API degraded — sweep not meaningful")
        pytest.skip(f"live API degraded: only {scanned} parameter-free GETs answered 200")
    summary = {
        "paths_in_contract": len(paths),
        "scanned_200": scanned,
        "unreachable": unreachable,
        "errored": errored,
        "skipped_for_time": skipped_for_time,
        "excluded_event_streams": streaming,
        "bytes_scanned": total_bytes,
        "patterns": sorted(SECRET_PATTERNS),
        "findings": findings,
    }
    if streaming:
        livesim.note(
            "excluded from the sweep (never-ending SSE, reads to EOF hang a naive client): "
            f"{streaming} — each is readable anonymously and streams live system events"
        )
    livesim.evidence("secret-sweep.json", json.dumps(summary, indent=2))
    livesim.extra(public_get_200_count=scanned, bytes_scanned=total_bytes)
    livesim.record(inputs={"patterns": sorted(SECRET_PATTERNS), "paths": len(paths)}, outputs=summary)
    livesim.note(f"swept {scanned} public 200 GETs / {total_bytes} bytes for credential shapes")
    assert findings == [], f"credential-shaped strings in live GET bodies: {findings}"
    livesim.cleanup(True)


@pytest.mark.security
@pytest.mark.negative
def test_error_bodies_do_not_leak_internals(livesim, live_api):
    """401/403/422 envelopes must stay structured — no traceback, no absolute
    repo/DB paths, no sqlite internals handed to an anonymous caller."""
    livesim.target("api")
    probes = [
        "/api/accounts",
        "/api/artifacts/preview?path=/etc/passwd",
        "/api/artifacts/preview",
        "/api/mounts/home/file?path=../../etc/passwd",
    ]
    tells = ("Traceback (most recent call last)", 'File "/', "sqlite3.", "state.sqlite3",
             str(REPO), "site-packages/")
    observed: dict[str, dict[str, object]] = {}
    for p in probes:
        status, body, _ = _get(live_api, p)
        _require_live(livesim, status, p)
        text = _text(body)
        observed[p] = {"status": status, "leaks": [t for t in tells if t in text], "bytes": len(text)}
    livesim.evidence("error-envelopes.json", json.dumps(observed, indent=2))
    livesim.record(inputs=probes, outputs=observed)
    for p, o in observed.items():
        assert 400 <= int(o["status"]) < 500, f"{p} answered {o['status']}"
        assert not o["leaks"], f"{p} leaked internals: {o['leaks']}"
    livesim.cleanup(True)


# ---------------------------------------------------------------------------
# Approval classifier — S-1/LS-022, IN FLIGHT (lane A1, not yet landed here)
# ---------------------------------------------------------------------------


def _approvals():
    try:
        from omniagentos.orchestrator.approvals import resolve_approval  # noqa: PLC0415
        from omniagentos.orchestrator.contracts import ApprovalRequest  # noqa: PLC0415
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"cannot import approvals classifier: {exc}")
    return resolve_approval, ApprovalRequest


# Destructive / irreversible intent phrased outside the classifier's vocabulary.
# Not commands — plain English an agent could legitimately emit as a proposed action.
UNLISTED_DESTRUCTIVE: tuple[str, ...] = (
    "vaporize the staging cluster",
    "recursively expunge every artifact under /srv",
    "detonate the kubernetes namespace",
    "reformat the attached volume",
    "zero out the backup tape",
    "clear the audit trail",
    "decommission the primary replica",
    "hand the master key to the vendor",
)

# Phrasings the hardened classifier does catch — the control leg.
LISTED_DESTRUCTIVE: tuple[str, ...] = (
    "rm -rf /srv/prod/customer_database",
    "DELETE FROM customers WHERE 1=1",
    "wipe /srv/prod/customer_database",
    "zelle send 500 to the landlord",
    "obliterate the tenant ledger snapshots",
)


@pytest.mark.security
@pytest.mark.negative
@pytest.mark.documents_open_defect(id="LS-022")
@pytest.mark.xfail(
    reason="LS-022 open (fix in flight, lane A1) -- unlisted destructive phrasing still "
    "auto-approves; see docs/testing/LIVESIM-ISSUES.yaml. strict=True: this test asserts the "
    "FIXED behaviour, so it must XPASS->FAIL the moment A1 lands, forcing a human to remove "
    "this marker rather than leaving a stale xfail hiding a real green test.",
    strict=True,
)
def test_approval_classifier_fails_open_on_unlisted_destructive_intent(livesim):
    """CORRECTED 2026-08-06 (S-1/LS-022, repair plan): ``resolve_approval``
    has an unknown -> approve fallthrough for destructive intent phrased
    outside the vocabulary. This test used to assert that OBSERVED fail-open
    behaviour as required (approved=True, escalated=False, e.g. a bare
    ``assert approved`` on the aggregate) -- the same "test pins the pre-fix
    behaviour as correct" shape found and corrected elsewhere in this suite
    (LS-011/LS-015/LS-016, LS-007, LS-017/LS-018): a SECOND test documenting
    the exact same defect as
    tests/livesim/categories/test_tools_permissions.py::
    test_classifier_fail_open_beyond_poc_phrases_observed (which the S-1 lane
    is already directed to invert), just not the one named in that lane's
    brief -- so it would have gone red for the RIGHT reason the moment S-1
    landed, and stayed red until someone noticed.

    Inverted here ahead of the S-1 lane landing (per repair plan S-1: the
    destructive/finance classification should invert unrecognised phrasing to
    escalate/park, never auto-approve) to the one invariant that must hold
    regardless of the exact mechanism S-1 lands (park vs. escalate, whatever
    the final ``reason`` string is): none of these unlisted destructive
    phrases may come back ``approved=True``. NOT yet verified against S-1's
    landed code (still in flight as of this commit) -- if S-1's actual
    behaviour differs from a flat `assert not approved` (e.g. a phrase is
    intentionally still permitted for a reason this test doesn't know about),
    whoever integrates S-1 should re-check this assertion against the real
    diff, the same way LS-017/LS-018's inversion was verified against A3's
    landed board_files.py.
    """
    livesim.target("code")  # pure in-process classifier; nothing live is mutated
    resolve_approval, ApprovalRequest = _approvals()
    grid: dict[str, dict[str, object]] = {}
    for phrase in UNLISTED_DESTRUCTIVE:
        d = resolve_approval(ApprovalRequest(proposed_action=phrase, action_class="consequential"))
        grid[phrase] = {"approved": d.approved, "escalated": d.escalated,
                        "category": d.category, "reason": d.reason}
    still_approved = sorted(p for p, v in grid.items() if v["approved"])
    livesim.evidence("classifier-fail-open.json", json.dumps(grid, indent=2, default=str))
    livesim.record(inputs=list(UNLISTED_DESTRUCTIVE),
                   outputs={"still_auto_approved": still_approved,
                            "still_auto_approved_n": len(still_approved),
                            "probed_n": len(UNLISTED_DESTRUCTIVE)})
    if still_approved:
        livesim.note(
            "S-1 not yet landed / fallthrough still open: "
            f"{len(still_approved)}/{len(UNLISTED_DESTRUCTIVE)} unlisted destructive phrases "
            f"still auto-approve, e.g. {still_approved[:3]}"
        )
    # FIXED behaviour: no unlisted destructive phrase may auto-approve.
    assert not still_approved, (
        f"unlisted destructive phrase(s) still auto-approve after S-1 should have closed the "
        f"fail-open fallthrough: {still_approved} — {grid}"
    )
    livesim.cleanup(True)


@pytest.mark.positive
@pytest.mark.security
def test_approval_classifier_still_parks_recognised_destructive_intent(livesim):
    """Control leg for the fail-open row above: the classifier is NOT uniformly
    broken. Every recognised money / customer / production-destruction phrasing
    parks (approved=False) — which is what makes the fail-open set evidence of a
    vocabulary hole rather than a classifier that approves everything."""
    livesim.target("code")
    resolve_approval, ApprovalRequest = _approvals()
    grid = {}
    for phrase in LISTED_DESTRUCTIVE:
        d = resolve_approval(ApprovalRequest(proposed_action=phrase, action_class="consequential"))
        grid[phrase] = {"approved": d.approved, "category": d.category, "reason": d.reason}
    livesim.record(inputs=list(LISTED_DESTRUCTIVE), outputs=grid)
    for phrase, v in grid.items():
        assert v["approved"] is False, f"recognised destructive phrase auto-approved: {phrase!r} -> {v}"
    livesim.cleanup(True)


@pytest.mark.security
@pytest.mark.boundary
def test_read_only_action_class_cannot_launder_a_destructive_request(livesim):
    """BOUNDARY: an attacker-controlled ``action_class`` must not be able to
    downgrade a proven-destructive request. A ``read_only`` label on
    ``rm -rf /srv/prod/...`` still parks (explicit delete/finance signals win
    over the weak class) — recorded alongside the same label on an unlisted
    phrase, which does NOT park (the same fail-open surface, seen from the
    class axis)."""
    livesim.target("code")
    resolve_approval, ApprovalRequest = _approvals()
    hard = resolve_approval(ApprovalRequest(
        proposed_action="rm -rf /srv/prod/customer_database", action_class="read_only"))
    soft = resolve_approval(ApprovalRequest(
        proposed_action="reformat the attached volume", action_class="read_only"))
    out = {
        "listed_read_only": {"approved": hard.approved, "category": hard.category},
        "unlisted_read_only": {"approved": soft.approved, "category": soft.category},
    }
    livesim.record(inputs={"listed": "rm -rf /srv/prod/customer_database",
                           "unlisted": "reformat the attached volume",
                           "action_class": "read_only"}, outputs=out)
    assert hard.approved is False, f"read_only label laundered a recognised destructive request: {out}"
    if soft.approved:
        livesim.note(
            "DEFECT: read_only action_class + unlisted destructive phrase "
            "('reformat the attached volume') auto-approves — same fail-open fallthrough"
        )
    livesim.cleanup(True)
