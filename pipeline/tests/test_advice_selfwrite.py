"""Pins three fixes from sha256:999b41c1... (2026-08-08, "R2", incl. the
coordinator's post-review sibling-sweep correction):

1. bridge/integration.py's `reject()` used to write
   `"base_sha": cand.base_sha or None` into a ledger event. base_sha is
   schema-typed as a required-format STRING with no null variant
   (schema/ledger-event.schema.json), so `_self_validate` raised
   `RuntimeError` and `main()` returned 1 -- the adapter crashed exactly
   when refusing the candidate it exists to refuse (a malformed/missing
   base_sha IS often the reason for the refusal). This file pins that a
   refusal-path event with an invalid/missing base_sha (a) never crashes
   the adapter and (b) never writes a null base_sha into the ledger --
   the key is omitted instead, which the schema already allows (base_sha
   is not in `required`).

2. Two SAME-CLASS siblings in `instrument_inquiry()`, both
   `str(verdict.receipt) if verdict.receipt else None`: one feeding
   `detail.receipt` on the `instrument_error` ledger event
   (ledger-event.schema.json types it as a string, no null), and one
   feeding `evidence[].receipt` on the inquiry envelope written to
   inquiries/ (envelope.schema.json ALSO types it as a string, no null --
   confirmed by direct `_schema_validate` probe, see
   test_instrument_inquiry_evidence_receipt_*). Same remedy: omit the key
   when there is no real receipt, never write null.

3. bridge/advice_writer.py -- the ADVISORY-ONLY wrapper that runs
   `integration.py --once --dry-run` on a timer and records the report to
   state/advice.json with a `generated_at` stamp, plus a staleness check
   that files a LOUD alert (never silent) when advice.json is older than
   the threshold -- the queue.json failure shape (a stale snapshot read as
   current) must not repeat here.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

PKG = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PKG))

from bridge import advice_writer as A  # noqa: E402
from bridge import integration as I  # noqa: E402
from bridge.canonical import content_id  # noqa: E402

assert Path(I.__file__).resolve() == (PKG / "bridge" / "integration.py").resolve(), \
    f"imported the WRONG integration.py: {I.__file__}"
assert Path(A.__file__).resolve() == (PKG / "bridge" / "advice_writer.py").resolve(), \
    f"imported the WRONG advice_writer.py: {A.__file__}"

PY = sys.executable
TS_FMT = "%Y-%m-%dT%H:%M:%SZ"


def _iso(dt: datetime) -> str:
    return dt.strftime(TS_FMT)


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def loops_root(tmp_path) -> Path:
    root = tmp_path / "loopqueue"
    for d in ("state", "candidates", "rejected", "parked", "receipts", "inquiries", "proposals"):
        (root / d).mkdir(parents=True)
    (root / "ledger.jsonl").write_text("")
    (root / "ALERTS.md").write_text("# Loop alerts\n\n")
    budget = {
        "updated_at": _iso(datetime.now(UTC)),
        "disk_free_gb": 100,
        "disk_free_gb_min": 20,
        "wip_cap": 4,
    }
    (root / "state" / "budget.json").write_text(json.dumps(budget))
    return root


@pytest.fixture
def repo(tmp_path) -> Path:
    r = tmp_path / "repo"
    r.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=r, check=True)
    return r


def _write_malformed_candidate(loops_root: Path, *, base_sha_value) -> str:
    """A COMPLETE candidate envelope (passes every schema check except
    base_sha) whose base_sha is missing/short -- exactly the input the
    reject() path exists to refuse. Returns the candidate id."""
    payload = {"why": "regression fixture -- malformed base_sha", "urgency": "p3"}
    ident = content_id(payload)
    art: dict = {
        "id": ident, "kind": "candidate", "title": "fixture: malformed base_sha",
        "created_at": _iso(datetime.now(UTC)),
        "contract": "v1.1",
        "producer": {"role": "external", "actor": "fixture"},
        "branch": "fixture/malformed-base-sha", "paths": ["some/file.py"],
        "payload": payload,
        "evidence": [{"claim": "x", "verified_by": "execution", "command": "x"}],
    }
    if base_sha_value:
        art["base_sha"] = base_sha_value
    (loops_root / "candidates" / f"{ident.split(':', 1)[1]}.json").write_text(json.dumps(art))
    return ident


# ---------------------------------------------------------------------------
# 1) reject() must not crash on an invalid/missing base_sha, and must never
#    write a null base_sha into the ledger.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("base_sha_value", ["", "deadbeef", None])
def test_reject_with_invalid_base_sha_does_not_crash(loops_root, repo, base_sha_value):
    """RED before the fix: `cand.base_sha or None` -> null -> _self_validate
    raises RuntimeError -> this call raises. GREEN after: reject() completes
    and the ledger line it wrote is schema-shaped (no null base_sha)."""
    it = I.Integration(loops_root, repo, repo.parent / "gate", PKG / "schema", apply=True)
    ident = f"sha256:{'a' * 64}"
    cand = I.Candidate(ident=ident, path=loops_root / "candidates" / "x.json", art={},
                       base_sha=base_sha_value or "")
    refusal = I.Refusal(
        f"base_sha must be the FULL 40 hex chars, got {base_sha_value!r} -- abbreviations "
        "have collided here", "candidate-defect", "replan")

    it.reject(cand, refusal)  # must not raise

    lines = [ln for ln in (loops_root / "ledger.jsonl").read_text().splitlines() if ln.strip()]
    assert lines, "reject() did not append a ledger line at all"
    event = json.loads(lines[-1])
    assert event["event"] == "rejected"
    assert event["id"] == ident
    # THE regression: never a null. Either the key is absent (omitted, which
    # the schema already allows -- base_sha is not in `required`), or it is
    # a real, schema-valid 40-hex string. It must NEVER be present-with-null.
    assert "base_sha" not in event or event["base_sha"] is not None, \
        "reject() wrote a null base_sha into the ledger -- this is the exact schema violation"
    if "base_sha" in event:
        assert re.fullmatch(r"[0-9a-f]{40}", event["base_sha"])


def test_reject_with_valid_base_sha_still_records_it(loops_root, repo):
    """The fix must not regress the happy path: a real 40-hex base_sha is
    still written, not dropped."""
    it = I.Integration(loops_root, repo, repo.parent / "gate", PKG / "schema", apply=True)
    good_sha = "b" * 40
    ident = f"sha256:{'c' * 64}"
    cand = I.Candidate(ident=ident, path=loops_root / "candidates" / "y.json", art={},
                       base_sha=good_sha)
    refusal = I.Refusal("unrelated refusal reason", "candidate-defect", "replan")

    it.reject(cand, refusal)

    lines = [ln for ln in (loops_root / "ledger.jsonl").read_text().splitlines() if ln.strip()]
    event = json.loads(lines[-1])
    assert event["base_sha"] == good_sha


def test_end_to_end_iterate_refuses_malformed_base_sha_without_crashing(loops_root, repo):
    """Full path: a real malformed candidate on disk, run through
    Integration.iterate() (as `--once --dry-run` does), must not raise and
    must record exactly one `rejected` event with no null base_sha."""
    ident = _write_malformed_candidate(loops_root, base_sha_value="tooshort")
    it = I.Integration(loops_root, repo, repo.parent / "gate", PKG / "schema", apply=True)

    it.iterate()  # must not raise

    events = [json.loads(ln) for ln in (loops_root / "ledger.jsonl").read_text().splitlines()
              if ln.strip()]
    rejected_events = [e for e in events if e.get("event") == "rejected" and e.get("id") == ident]
    assert len(rejected_events) == 1
    assert "base_sha" not in rejected_events[0] or re.fullmatch(
        r"[0-9a-f]{40}", rejected_events[0]["base_sha"])


# ---------------------------------------------------------------------------
# 1b) SAME-CLASS sibling: instrument_inquiry() wrote
#     `str(verdict.receipt) if verdict.receipt else None` into TWO
#     schema-typed string fields with no null variant -- detail.receipt on
#     the instrument_error ledger event, and evidence[].receipt on the
#     inquiry envelope. Flagged in the first round of this candidate, fixed
#     here per the coordinator's follow-up (line 1101 is nowhere near the
#     concurrent validation lane at line 541 -- no region-disjoint conflict).
# ---------------------------------------------------------------------------

def _make_instrument_verdict(receipt):
    return I.GateVerdict(result="instrument-error", exit_code=124, slug="timeout",
                         reason="gate exceeded timeout -- host/instrument, says nothing "
                                "about the code",
                         receipt=receipt, stdout_tail="", duration_s=1.0)


def test_instrument_inquiry_with_no_receipt_does_not_crash(loops_root, repo):
    """RED before the fix (both call sites): _self_validate raises
    RuntimeError because detail.receipt / evidence[].receipt is written as
    null. GREEN after: instrument_inquiry() completes, and neither the
    ledger event nor the inquiry envelope carries a null receipt."""
    it = I.Integration(loops_root, repo, repo.parent / "gate", PKG / "schema", apply=True)
    ident = f"sha256:{'d' * 64}"
    cand = I.Candidate(ident=ident, path=loops_root / "candidates" / "z.json", art={},
                       branch="fixture/instrument", tip_sha="e" * 40, base_sha="f" * 40)
    verdict = _make_instrument_verdict(receipt=None)  # gate crashed before writing a receipt

    it.instrument_inquiry(cand, verdict, "headsha:0")  # must not raise

    # ledger side: instrument_error event, detail.receipt
    events = [json.loads(ln) for ln in (loops_root / "ledger.jsonl").read_text().splitlines()
              if ln.strip()]
    inst_events = [e for e in events if e.get("event") == "instrument_error" and e.get("id") == ident]
    assert len(inst_events) == 1
    detail = inst_events[0]["detail"]
    assert "receipt" not in detail or detail["receipt"] is not None, \
        "instrument_error event wrote a null detail.receipt -- the exact schema violation"

    # envelope side: the inquiry written to inquiries/*.json, evidence[].receipt
    inquiry_files = list((loops_root / "inquiries").glob("*.json"))
    assert len(inquiry_files) == 1
    art = json.loads(inquiry_files[0].read_text())
    ev0 = art["evidence"][0]
    assert "receipt" not in ev0 or ev0["receipt"] is not None, \
        "inquiry envelope wrote a null evidence[].receipt -- the exact schema violation"


def test_instrument_inquiry_with_real_receipt_still_records_it(loops_root, repo, tmp_path):
    """Happy path must not regress: a real receipt path is still written."""
    receipt_path = tmp_path / "receipts" / "merge-gate-abc123.json"
    receipt_path.parent.mkdir(parents=True)
    receipt_path.write_text("{}")
    it = I.Integration(loops_root, repo, repo.parent / "gate", PKG / "schema", apply=True)
    ident = f"sha256:{'1' * 64}"
    cand = I.Candidate(ident=ident, path=loops_root / "candidates" / "z2.json", art={},
                       branch="fixture/instrument2", tip_sha="2" * 40, base_sha="3" * 40)
    verdict = _make_instrument_verdict(receipt=receipt_path)

    it.instrument_inquiry(cand, verdict, "headsha:1")

    events = [json.loads(ln) for ln in (loops_root / "ledger.jsonl").read_text().splitlines()
              if ln.strip()]
    inst_events = [e for e in events if e.get("event") == "instrument_error" and e.get("id") == ident]
    assert inst_events[0]["detail"]["receipt"] == str(receipt_path)

    inquiry_files = list((loops_root / "inquiries").glob("*.json"))
    art = json.loads(inquiry_files[0].read_text())
    assert art["evidence"][0]["receipt"] == str(receipt_path)


def test_instrument_inquiry_evidence_receipt_is_schema_typed_string_no_null():
    """Direct schema probe (independent of the code fix): confirms
    evidence[].receipt in envelope.schema.json is `{"type": "string"}` with
    no null variant, so the earlier (WRONG) ruling that this site was
    unconstrained is corrected here with a standalone assertion, not just
    the code-path test above."""
    obj = {
        "contract": "v1.1", "id": f"sha256:{'a' * 64}", "kind": "inquiry", "title": "x",
        "created_at": "2026-08-08T00:00:00Z",
        "producer": {"role": "implementer", "actor": "x"},
        "payload": {"area": "tooling", "observation": "x", "why_not_a_fix": "x"},
        "evidence": [{"claim": "x", "verified_by": "execution", "command": "x",
                      "exit_code": 1, "receipt": None}],
    }
    status, msg = I._schema_validate(obj, PKG / "schema", "envelope.schema.json")
    assert status == "fail"
    assert "receipt" in msg and "not of type" in msg


# ---------------------------------------------------------------------------
# 2) the adapter completes `--once --dry-run` rc 0 on a fixture queue,
#    including one that carries a malformed-base_sha candidate.
# ---------------------------------------------------------------------------

def test_cli_once_dry_run_rc0_on_fixture_queue(loops_root, repo):
    _write_malformed_candidate(loops_root, base_sha_value="")
    p = subprocess.run(
        [PY, str(PKG / "bridge" / "integration.py"), "--loops-root", str(loops_root),
         "--repo", str(repo), "--once", "--dry-run"],
        # Load-robust: the merge-gate runs this whole suite in parallel, so on a
        # busy box this real-work dry-run can balloon past a tight wall-clock
        # timeout. A genuine hang is still caught by the gate's overall deadline.
        capture_output=True, text=True, timeout=240)
    assert p.returncode == 0, f"stdout={p.stdout!r} stderr={p.stderr!r}"
    idx = p.stdout.find("{")
    assert idx != -1, f"no JSON summary printed: {p.stdout!r}"
    summary, _ = json.JSONDecoder().raw_decode(p.stdout[idx:])
    assert not summary.get("error"), f"adapter reported an error: {summary.get('error')}"


# ---------------------------------------------------------------------------
# 3) advice_writer: run_once + write_advice, and staleness alerting.
# ---------------------------------------------------------------------------

def test_advice_writer_run_once_writes_advice_json(loops_root, repo):
    advice = A.run_once(loops_root, repo, PY, timeout=240)  # load-robust under concurrent gate suite (see above)
    assert advice["rc"] == 0, advice
    target = A.write_advice(loops_root, advice)
    assert target == loops_root / "state" / "advice.json"
    data = json.loads(target.read_text())
    assert "generated_at" in data
    assert data["rc"] == 0
    assert isinstance(data["summary"], dict)


def test_run_once_adapter_error_never_null_on_nonzero_rc_without_error_key(monkeypatch, loops_root):
    """Pins the cross-lineage F3 finding: a subprocess that exits non-zero
    but whose stdout JSON has no top-level "error" key used to leave
    advice["adapter_error"] = None -- a hard crash reading as falsy through
    `if advice.get("adapter_error"):`. Same "None reads as favourable"
    class as base_sha/receipt. Mirrors .fusion/repro/F3.py, pinned into the
    tracked suite (`.fusion/` is not git-tracked)."""
    class DummyProc:
        returncode = 1
        stdout = '{"some": "data"}'
        stderr = "fatal crash"

    monkeypatch.setattr(subprocess, "run", lambda *a, **kw: DummyProc())

    advice = A.run_once(loops_root, None, PY)

    assert advice.get("adapter_error") is not None, \
        "adapter_error was left None despite returncode 1 -- a real failure read as favourable"
    assert "1" in advice["adapter_error"] or "exited" in advice["adapter_error"]


def test_staleness_is_loud_not_silent(loops_root):
    """The core Kimi ask: advice older than 10 min files an ALERT, not
    nothing. Executed, not reasoned about."""
    state_dir = loops_root / "state"
    state_dir.mkdir(exist_ok=True)
    stale_ts = _iso(datetime.now(UTC) - timedelta(minutes=25))
    (state_dir / "advice.json").write_text(json.dumps({"generated_at": stale_ts, "rc": 0}))

    alert = A.check_staleness(loops_root, max_age_s=600)

    assert alert is not None, "a 25-minute-old advice.json produced no alert -- silent staleness"
    assert "25" in alert or "stale" in alert.lower() or "min" in alert.lower()
    alerts_text = (loops_root / "ALERTS.md").read_text()
    assert alert.split(" -- ")[0].strip()[:20] in alerts_text or "advice.json" in alerts_text


def test_freshness_produces_no_alert(loops_root):
    state_dir = loops_root / "state"
    state_dir.mkdir(exist_ok=True)
    fresh_ts = _iso(datetime.now(UTC) - timedelta(minutes=1))
    (state_dir / "advice.json").write_text(json.dumps({"generated_at": fresh_ts, "rc": 0}))

    alert = A.check_staleness(loops_root, max_age_s=600)

    assert alert is None


def test_future_generated_at_is_loud_not_silent(loops_root):
    """Pins the cross-lineage F4 finding (gemini-3.1-pro-preview, 2026-08-08):
    a generated_at in the FUTURE (clock skew, or tampering) makes
    age_s negative, so the plain `age_s > max_age_s` check never fires and
    check_staleness() used to return None -- silent. Same favourable-
    absence class as F1, a different hat. Mirrors the reviewer's
    .fusion/repro/F4.py (not git-tracked), pinned here permanently. Also
    checks the message names the fault as a CLOCK problem, not staleness
    -- conflating the two would send whoever reads ALERTS.md to debug the
    wrong thing."""
    state_dir = loops_root / "state"
    state_dir.mkdir(exist_ok=True)
    future_ts = _iso(datetime.now(UTC) + timedelta(hours=5))
    (state_dir / "advice.json").write_text(json.dumps({"generated_at": future_ts, "rc": 0}))

    alert = A.check_staleness(loops_root, max_age_s=600)

    assert alert is not None, "a future generated_at silently suppressed the staleness alert"
    assert "future" in alert.lower() or "clock" in alert.lower()
    assert "advice.json" in (loops_root / "ALERTS.md").read_text()


def test_generated_at_within_clock_skew_tolerance_is_not_flagged_as_future(loops_root):
    """A few seconds of ordinary NTP/host jitter must not be misread as
    tampering -- the tolerance is explicit (CLOCK_SKEW_TOLERANCE_S) and
    bounded, not a second silent grace window."""
    state_dir = loops_root / "state"
    state_dir.mkdir(exist_ok=True)
    barely_future_ts = _iso(datetime.now(UTC) + timedelta(seconds=5))
    (state_dir / "advice.json").write_text(json.dumps({"generated_at": barely_future_ts, "rc": 0}))

    assert A.check_staleness(loops_root, max_age_s=600) is None


def test_staleness_absent_advice_json_is_loud_not_silent(loops_root):
    """CORRECTED (cross-lineage review, gemini-3.1-pro-preview, 2026-08-08):
    this test used to assert `check_staleness(...) is None` on a totally
    missing advice.json and call it "bootstrap, not staleness" -- that was
    test-weakening: it certified the exact favourable-absence bug the
    staleness feature exists to prevent (a timer that has NEVER fired is
    indistinguishable, from a missing file alone, from one that fired once
    and then died -- both must be loud). Rewritten to assert the LOUD
    behaviour instead. Shown RED against the pre-fix code via
    `git stash` + `pytest -k absent_advice_json`, GREEN after (see the
    candidate envelope evidence for the exact commands/output)."""
    alert = A.check_staleness(loops_root, max_age_s=600)
    assert alert is not None, \
        "a MISSING advice.json returned None (silent) instead of a loud alert"
    assert "missing" in alert.lower() or "advice.json" in alert
    assert "advice.json" in (loops_root / "ALERTS.md").read_text()
    assert not (loops_root / "state" / "advice.json").exists()


def test_check_only_cli_prints_alert_and_writes_nothing(loops_root):
    state_dir = loops_root / "state"
    state_dir.mkdir(exist_ok=True)
    stale_ts = _iso(datetime.now(UTC) - timedelta(minutes=45))
    advice_path = state_dir / "advice.json"
    advice_path.write_text(json.dumps({"generated_at": stale_ts, "rc": 0}))
    before = advice_path.read_text()

    p = subprocess.run(
        [PY, str(PKG / "bridge" / "advice_writer.py"), "--loops-root", str(loops_root),
         "--check-only"],
        capture_output=True, text=True, timeout=120)  # load-robust under concurrent gate suite

    assert p.returncode == 0
    assert "ALERT" in p.stdout
    assert advice_path.read_text() == before, "--check-only must not overwrite advice.json"
