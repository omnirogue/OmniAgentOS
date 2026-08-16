"""pr_reconcile.py — closes findings/ whose PR resolved on GitHub.

No network: every `gh` call goes through pr_reconcile._gh, which is
monkeypatched per test. All writes go to tmp_path; the real loopqueue is never
touched.
"""

from __future__ import annotations

import json
import os
import pathlib
import sys
import threading
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "bridge"))

import pr_reconcile  # noqa: E402
from github_bridge import TerminalError, finding_from_pr  # noqa: E402


def _prep_root(tmp_path: pathlib.Path) -> pathlib.Path:
    root = tmp_path / "loopqueue"
    (root / "findings").mkdir(parents=True)
    return root


def _write_finding(root: pathlib.Path, finding: dict) -> pathlib.Path:
    stem = finding["id"].replace(":", "_")
    path = root / "findings" / f"{stem}.json"
    path.write_text(json.dumps(finding, indent=2), encoding="utf-8")
    return path


def _pr_finding(number: int, title: str = "a PR") -> dict:
    """Build a finding the same way github_bridge.finding_from_pr does, so the
    fixture matches the real envelope shape exactly."""
    return finding_from_pr({
        "number": number,
        "title": title,
        "url": f"https://github.com/acme/widgets/pull/{number}",
    })


def _non_pr_finding() -> dict:
    return {
        "contract": "v1.1",
        "id": "sha256:" + "0" * 64,
        "kind": "finding",
        "title": "disk burning",
        "created_at": "2026-08-08T00:00:00Z",
        "producer": {"role": "implementer", "actor": "integration-loop"},
        "origin": "internal",
        "source_url": "",
        "payload": {
            "symptom": "disk burning",
            "class": "candidate-defect",
            "source": "integration-iteration-scan",
        },
    }


# ---------------------------------------------------------------- (a) merged


def test_merged_pr_closes_once(tmp_path, monkeypatch):
    root = _prep_root(tmp_path)
    finding = _pr_finding(101, "fix: thing")
    _write_finding(root, finding)

    def fake_gh(args, *, dry_run=False):
        assert args[:2] == ["pr", "list"]
        return [{
            "number": 101, "state": "MERGED", "mergedAt": "2026-08-01T12:00:00Z",
            "closedAt": None, "title": "fix: thing",
            "url": "https://github.com/acme/widgets/pull/101",
        }]

    monkeypatch.setattr(pr_reconcile, "_gh", fake_gh)

    counts = pr_reconcile.reconcile_once("acme/widgets", root)
    assert counts["closed"] == 1
    assert counts["errors"] == 0
    assert counts["open"] == 0

    stem = finding["id"].replace(":", "_")
    tombstone_path = root / "rejected" / f"{stem}.json"
    assert tombstone_path.exists()
    tombstone = json.loads(tombstone_path.read_text())
    assert tombstone["id"] == finding["id"]
    assert tombstone["class"] == "answered"
    assert tombstone["remedy"] == "drop"
    assert "expires_at" in tombstone
    assert "merged" in tombstone["reason"]

    ledger_lines = (root / "ledger.jsonl").read_text().strip().splitlines()
    assert len(ledger_lines) == 1
    event = json.loads(ledger_lines[0])
    assert event["role"] == "external"
    assert event["event"] == "rejected"
    assert event["actor"] == "github-reconciler"
    assert event["id"] == finding["id"]
    assert event["detail"]["class"] == "answered"
    assert event["detail"]["reason"] == tombstone["reason"]
    assert "expires_at" in event["detail"]


# ---------------------------------------------------------------- (b) idempotent


def test_second_run_no_duplicate(tmp_path, monkeypatch):
    root = _prep_root(tmp_path)
    finding = _pr_finding(102, "fix: thing2")
    _write_finding(root, finding)

    calls = {"n": 0}

    def fake_gh(args, *, dry_run=False):
        calls["n"] += 1
        return [{
            "number": 102, "state": "MERGED", "mergedAt": "2026-08-01T12:00:00Z",
            "closedAt": None, "title": "fix: thing2",
            "url": "https://github.com/acme/widgets/pull/102",
        }]

    monkeypatch.setattr(pr_reconcile, "_gh", fake_gh)
    first = pr_reconcile.reconcile_once("acme/widgets", root)
    assert first["closed"] == 1
    assert calls["n"] == 1

    def fail_gh(args, *, dry_run=False):
        raise AssertionError("gh must not be called for an already-closed finding")

    monkeypatch.setattr(pr_reconcile, "_gh", fail_gh)
    second = pr_reconcile.reconcile_once("acme/widgets", root)
    assert second["closed"] == 0
    assert second["skipped"] == 1

    ledger_lines = (root / "ledger.jsonl").read_text().strip().splitlines()
    assert len(ledger_lines) == 1  # still just the one rejected event


# ---------------------------------------------------------------- (c) open


def test_open_pr_untouched(tmp_path, monkeypatch):
    root = _prep_root(tmp_path)
    finding = _pr_finding(103, "wip: thing3")
    _write_finding(root, finding)

    def fake_gh(args, *, dry_run=False):
        return [{
            "number": 103, "state": "OPEN", "mergedAt": None, "closedAt": None,
            "title": "wip: thing3", "url": "https://github.com/acme/widgets/pull/103",
        }]

    monkeypatch.setattr(pr_reconcile, "_gh", fake_gh)
    counts = pr_reconcile.reconcile_once("acme/widgets", root)
    assert counts["open"] == 1
    assert counts["closed"] == 0

    stem = finding["id"].replace(":", "_")
    assert not (root / "rejected" / f"{stem}.json").exists()
    assert not (root / "ledger.jsonl").exists()


# ---------------------------------------------------------------- (d) non-PR


def test_non_pr_finding_skipped(tmp_path, monkeypatch):
    root = _prep_root(tmp_path)
    finding = _non_pr_finding()
    _write_finding(root, finding)

    def fail_gh(args, *, dry_run=False):
        raise AssertionError("gh must not be called for a non-PR finding")

    monkeypatch.setattr(pr_reconcile, "_gh", fail_gh)
    counts = pr_reconcile.reconcile_once("acme/widgets", root)
    assert counts["closed"] == 0
    assert counts["skipped"] == 0
    assert counts["open"] == 0

    stem = finding["id"].replace(":", "_")
    assert not (root / "rejected" / f"{stem}.json").exists()


# ---------------------------------------------------------------- (e) fail-soft


def test_gh_failure_on_one_pr_others_still_processed(tmp_path, monkeypatch):
    root = _prep_root(tmp_path)
    f_merged = _pr_finding(110, "merged one")
    f_fail = _pr_finding(111, "flaky one")
    f_open = _pr_finding(112, "open one")
    for f in (f_merged, f_fail, f_open):
        _write_finding(root, f)

    def fake_gh(args, *, dry_run=False):
        if args[:2] == ["pr", "list"]:
            # Batch call fails: forces the per-PR fallback path.
            raise RuntimeError("list transiently unavailable")
        assert args[:2] == ["pr", "view"]
        num = int(args[2])
        if num == 111:
            raise RuntimeError("boom on 111")
        if num == 110:
            return {
                "number": 110, "state": "MERGED", "mergedAt": "2026-08-01T00:00:00Z",
                "closedAt": None, "title": "merged one",
                "url": "https://github.com/acme/widgets/pull/110",
            }
        if num == 112:
            return {
                "number": 112, "state": "OPEN", "mergedAt": None, "closedAt": None,
                "title": "open one", "url": "https://github.com/acme/widgets/pull/112",
            }
        raise AssertionError(f"unexpected pr number {num}")

    monkeypatch.setattr(pr_reconcile, "_gh", fake_gh)
    counts = pr_reconcile.reconcile_once("acme/widgets", root)
    assert counts["closed"] == 1
    assert counts["open"] == 1
    assert counts["errors"] == 1

    assert (root / "rejected" / f"{f_merged['id'].replace(':', '_')}.json").exists()
    assert not (root / "rejected" / f"{f_fail['id'].replace(':', '_')}.json").exists()
    assert not (root / "rejected" / f"{f_open['id'].replace(':', '_')}.json").exists()


# ---------------------------------------------------------------- terminal errors


def test_terminal_error_stops_sweep(tmp_path, monkeypatch):
    root = _prep_root(tmp_path)
    finding = _pr_finding(120, "auth issue")
    _write_finding(root, finding)

    def fake_gh(args, *, dry_run=False):
        raise TerminalError("401 Bad credentials")

    monkeypatch.setattr(pr_reconcile, "_gh", fake_gh)
    counts = pr_reconcile.reconcile_once("acme/widgets", root)
    assert "error" in counts
    assert counts["closed"] == 0

    stem = finding["id"].replace(":", "_")
    assert not (root / "rejected" / f"{stem}.json").exists()


# ---------------------------------------------------------------- dry run


def test_dry_run_reports_but_does_not_write(tmp_path, monkeypatch):
    root = _prep_root(tmp_path)
    finding = _pr_finding(130, "dry run pr")
    _write_finding(root, finding)

    def fake_gh(args, *, dry_run=False):
        return [{
            "number": 130, "state": "CLOSED", "mergedAt": None,
            "closedAt": "2026-08-02T00:00:00Z", "title": "dry run pr",
            "url": "https://github.com/acme/widgets/pull/130",
        }]

    monkeypatch.setattr(pr_reconcile, "_gh", fake_gh)
    counts = pr_reconcile.reconcile_once("acme/widgets", root, dry_run=True)
    assert counts["closed"] == 1
    assert counts["details"][0]["pr"] == 130
    assert counts["details"][0]["state"] == "CLOSED"

    stem = finding["id"].replace(":", "_")
    assert not (root / "rejected" / f"{stem}.json").exists()
    assert not (root / "ledger.jsonl").exists()


# --------------------------------------------------- (b2) ledger-only idempotency


def test_second_run_no_duplicate_via_ledger_event_alone(tmp_path, monkeypatch):
    """The tombstone check alone is not enough: a finding can be terminal by
    ledger event only (e.g. the Implementer's own `merged`, with no tombstone
    ever written for it — exactly what the live queue actually contains).
    That must ALSO stop this reconciler from touching it, with no gh call at
    all. This is the case the mutation repro (prr-ledger-test-gap.py) proves
    the old test suite never exercised."""
    root = _prep_root(tmp_path)
    finding = _pr_finding(150, "already merged by the implementer")
    _write_finding(root, finding)

    from github_bridge import _append_ledger as real_append_ledger

    real_append_ledger(root, {
        "ts": "2026-08-08T06:00:00Z",
        "role": "implementer",
        "event": "merged",
        "id": finding["id"],
        "actor": "integration-loop",
        "detail": {"merge_sha": "a" * 40, "result": "pass", "receipt": "receipts/x"},
    })

    def fail_gh(args, *, dry_run=False):
        raise AssertionError("gh must not be called for a finding already terminal by ledger event")

    monkeypatch.setattr(pr_reconcile, "_gh", fail_gh)
    counts = pr_reconcile.reconcile_once("acme/widgets", root)
    assert counts["closed"] == 0
    assert counts["repaired"] == 0
    assert counts["skipped"] == 1

    stem = finding["id"].replace(":", "_")
    assert not (root / "rejected" / f"{stem}.json").exists()  # not ours to backfill

    ledger_lines = (root / "ledger.jsonl").read_text().strip().splitlines()
    assert len(ledger_lines) == 1  # only the pre-existing merged event, nothing appended
    assert json.loads(ledger_lines[0])["event"] == "merged"


# --------------------------------------------------- (f) race: terminal event lands mid-sweep


def test_terminal_event_landing_mid_sweep_is_not_duplicated(tmp_path, monkeypatch):
    """finding #1: the terminal-id snapshot is taken before the GitHub
    round-trip. If another producer's terminal event lands for this id
    between that snapshot and the write, `_close_finding`'s race recheck
    must back off rather than append a second terminal event."""
    root = _prep_root(tmp_path)
    finding = _pr_finding(151, "race")
    _write_finding(root, finding)

    from github_bridge import _append_ledger as real_append_ledger

    def terminal_arrives_after_scan(repo, numbers):
        real_append_ledger(root, {
            "ts": "2026-08-08T12:00:00Z",
            "role": "implementer",
            "event": "merged",
            "id": finding["id"],
            "actor": "integration-loop",
            "detail": {"merge_sha": "b" * 40},
        })
        return {151: {
            "number": 151, "state": "MERGED", "mergedAt": "2026-08-08T12:00:00Z",
            "closedAt": None, "url": "https://github.com/acme/widgets/pull/151",
            "title": "race",
        }}

    monkeypatch.setattr(pr_reconcile, "_fetch_pr_states", terminal_arrives_after_scan)
    counts = pr_reconcile.reconcile_once("acme/widgets", root)
    assert counts["closed"] == 0
    assert counts["race_skipped"] == 1

    stem = finding["id"].replace(":", "_")
    assert not (root / "rejected" / f"{stem}.json").exists()

    events = [json.loads(line) for line in (root / "ledger.jsonl").read_text().strip().splitlines()]
    terminal = [e for e in events if e["event"] in ("merged", "rejected")]
    assert len(terminal) == 1 and terminal[0]["event"] == "merged"


# --------------------------------------------------- (g) repair path


def test_repairs_own_orphaned_half_write(tmp_path, monkeypatch):
    """finding #2: a crash between the ledger append and the tombstone write
    in a previous run must be self-healing on the next one -- the missing
    tombstone gets backfilled, with no second ledger event."""
    root = _prep_root(tmp_path)
    finding = _pr_finding(152, "half-written previously")
    _write_finding(root, finding)

    from github_bridge import _append_ledger as real_append_ledger

    real_append_ledger(root, {
        "ts": "2026-08-08T05:00:00Z",
        "role": "external",
        "event": "rejected",
        "id": finding["id"],
        "actor": "github-reconciler",
        "detail": {
            "reason": "PR #152 merged on GitHub 2026-08-08T05:00:00Z",
            "class": "answered",
            "expires_at": "2026-08-15T05:00:00Z",
        },
    })

    def fail_gh(args, *, dry_run=False):
        raise AssertionError("a repair must not need a fresh gh call")

    monkeypatch.setattr(pr_reconcile, "_gh", fail_gh)
    counts = pr_reconcile.reconcile_once("acme/widgets", root)
    assert counts["repaired"] == 1
    assert counts["closed"] == 0

    stem = finding["id"].replace(":", "_")
    tombstone_path = root / "rejected" / f"{stem}.json"
    assert tombstone_path.exists()
    tombstone = json.loads(tombstone_path.read_text())
    assert tombstone["reason"] == "PR #152 merged on GitHub 2026-08-08T05:00:00Z"
    assert tombstone["expires_at"] == "2026-08-15T05:00:00Z"

    ledger_lines = (root / "ledger.jsonl").read_text().strip().splitlines()
    assert len(ledger_lines) == 1  # no new ledger event written by the repair


# --------------------------------------------------- (h) repo mismatch


def test_repo_mismatch_is_never_closed(tmp_path, monkeypatch):
    """finding #3: a same-numbered PR in a DIFFERENT repo must never be used
    to judge this finding."""
    root = _prep_root(tmp_path)
    finding = finding_from_pr({
        "number": 7,
        "title": "foreign PR",
        "url": "https://github.com/other/project/pull/7",
    })
    _write_finding(root, finding)

    def fail_gh(args, *, dry_run=False):
        raise AssertionError("a repo-mismatched finding must never reach a gh lookup")

    monkeypatch.setattr(pr_reconcile, "_gh", fail_gh)
    counts = pr_reconcile.reconcile_once("acme/widgets", root)
    assert counts["closed"] == 0
    assert counts["repo_mismatch"] == 1
    assert counts["details"][0]["source_repo"] == "other/project"

    stem = finding["id"].replace(":", "_")
    assert not (root / "rejected" / f"{stem}.json").exists()
    assert not (root / "ledger.jsonl").exists()


# --------------------------------------------------- (i) malformed id


def test_malformed_id_refused_before_any_write(tmp_path, monkeypatch):
    """finding #5: a historical/malformed id must be refused, never normalized,
    and never written anywhere."""
    root = _prep_root(tmp_path)
    finding = {
        "contract": "v1.1",
        "id": "sha256:" + "b" * 40,  # 40 hex chars, not the required 64
        "kind": "finding",
        "title": "historical truncated id",
        "source_url": "https://github.com/acme/widgets/pull/153",
        "payload": {
            "source": "external-pr",
            "source_ref": "https://github.com/acme/widgets/pull/153",
        },
    }
    _write_finding(root, finding)

    def fail_gh(args, *, dry_run=False):
        raise AssertionError("a malformed id must be refused before any gh lookup")

    monkeypatch.setattr(pr_reconcile, "_gh", fail_gh)
    counts = pr_reconcile.reconcile_once("acme/widgets", root)
    assert counts["errors"] == 1
    assert counts["closed"] == 0
    assert not (root / "ledger.jsonl").exists()


# --------------------------------------------------- (j) unparseable PR reference


def test_unparseable_pr_reference_is_distinguishable_from_healthy_skip(tmp_path, monkeypatch):
    """finding #6: an unresolvable live finding must not read exactly like a
    healthy already-closed skip."""
    root = _prep_root(tmp_path)
    finding = {
        "id": "sha256:" + "c" * 64,
        "kind": "finding",
        "title": "external PR with no usable reference",
        "source_url": "",
        "payload": {"source": "external-pr", "source_ref": "unknown"},
    }
    _write_finding(root, finding)

    def fail_gh(args, *, dry_run=False):
        raise AssertionError("an unparseable reference must never reach a gh lookup")

    monkeypatch.setattr(pr_reconcile, "_gh", fail_gh)
    counts = pr_reconcile.reconcile_once("acme/widgets", root)
    assert counts["errors"] == 1
    assert counts["skipped"] == 0
    assert any(d.get("id") == finding["id"] and d.get("error") for d in counts["details"])


# --------------------------------------------------------- (k) mixed ownership


def test_mixed_ownership_never_repairs_over_a_foreign_close(tmp_path, monkeypatch):
    """new finding #1 (round 2): an id with BOTH this reconciler's own
    orphaned `rejected` AND another producer's `merged` must never get a
    reconciler tombstone written over the foreign closure. Foreign wins,
    unconditionally, checked before the repair branch."""
    root = _prep_root(tmp_path)
    finding = _pr_finding(160, "mixed ownership")
    _write_finding(root, finding)

    from github_bridge import _append_ledger as real_append_ledger

    real_append_ledger(root, {
        "ts": "2026-08-08T13:00:00Z", "role": "external", "event": "rejected",
        "id": finding["id"], "actor": "github-reconciler",
        "detail": {"reason": "PR #160 merged on GitHub", "class": "answered",
                   "expires_at": "2026-08-15T13:00:00Z"},
    })
    real_append_ledger(root, {
        "ts": "2026-08-08T13:00:01Z", "role": "implementer", "event": "merged",
        "id": finding["id"], "actor": "integration-loop",
        "detail": {"merge_sha": "b" * 40},
    })

    def fail_gh(args, *, dry_run=False):
        raise AssertionError("mixed ownership must skip before any gh call")

    monkeypatch.setattr(pr_reconcile, "_gh", fail_gh)
    counts = pr_reconcile.reconcile_once("acme/widgets", root)
    assert counts["repaired"] == 0
    assert counts["skipped"] == 1

    stem = finding["id"].replace(":", "_")
    assert not (root / "rejected" / f"{stem}.json").exists()


def test_foreign_merged_only_is_never_repaired(tmp_path, monkeypatch):
    """new finding #1 sibling: the pure-foreign case (no own orphan at all)
    must keep working exactly as before — regression guard alongside the
    mixed case above."""
    root = _prep_root(tmp_path)
    finding = _pr_finding(161, "implemented elsewhere")
    _write_finding(root, finding)

    from github_bridge import _append_ledger as real_append_ledger

    real_append_ledger(root, {
        "ts": "2026-08-08T13:00:00Z", "role": "implementer", "event": "merged",
        "id": finding["id"], "actor": "integration-loop",
        "detail": {"merge_sha": "a" * 40},
    })

    def fail_gh(args, *, dry_run=False):
        raise AssertionError("foreign terminal event must skip before any gh call")

    monkeypatch.setattr(pr_reconcile, "_gh", fail_gh)
    counts = pr_reconcile.reconcile_once("acme/widgets", root)
    assert counts["repaired"] == 0
    assert counts["skipped"] == 1

    stem = finding["id"].replace(":", "_")
    assert not (root / "rejected" / f"{stem}.json").exists()


# --------------------------------------------------------------- (l) claims


def test_claim_blocks_a_concurrent_close_then_succeeds_once_released(tmp_path, monkeypatch):
    """design decision #1 (round 2): a LIVE claim held by another writer
    blocks this reconciler from WRITING anything for that finding -- no
    tombstone, no ledger event -- and is counted `claim_held`, not `errors`
    or `skipped`. (The claim gates the write, not the read-only gh lookup
    that determines whether a write would even be attempted -- the sweep
    still resolves the PR's state before finding out the claim is held.)
    Once that claim is released, an ordinary run closes it normally."""
    root = _prep_root(tmp_path)
    finding = _pr_finding(170, "claimed elsewhere")
    _write_finding(root, finding)

    # Simulate another writer (a concurrent reconciler instance, in spirit)
    # holding a live claim on this exact finding id.
    result = pr_reconcile._acquire_claim(root, finding["id"])
    assert result is not None
    claim_path, _stale = result

    def fake_gh(args, *, dry_run=False):
        return [{
            "number": 170, "state": "MERGED", "mergedAt": "2026-08-08T13:00:00Z",
            "closedAt": None, "title": "claimed elsewhere",
            "url": "https://github.com/acme/widgets/pull/170",
        }]

    monkeypatch.setattr(pr_reconcile, "_gh", fake_gh)
    counts = pr_reconcile.reconcile_once("acme/widgets", root)
    assert counts["claim_held"] == 1
    assert counts["closed"] == 0
    stem = finding["id"].replace(":", "_")
    assert not (root / "rejected" / f"{stem}.json").exists()
    assert not (root / "ledger.jsonl").exists()

    # Release the claim (the other writer finished, or crashed and something
    # cleaned up) and confirm a normal run now succeeds.
    claim_path.unlink()

    def fake_gh(args, *, dry_run=False):
        return [{
            "number": 170, "state": "MERGED", "mergedAt": "2026-08-08T13:05:00Z",
            "closedAt": None, "title": "claimed elsewhere",
            "url": "https://github.com/acme/widgets/pull/170",
        }]

    monkeypatch.setattr(pr_reconcile, "_gh", fake_gh)
    counts2 = pr_reconcile.reconcile_once("acme/widgets", root)
    assert counts2["closed"] == 1
    assert (root / "rejected" / f"{stem}.json").exists()


def test_expired_claim_is_reclaimed_not_parked_forever(tmp_path, monkeypatch):
    """design decision #1: a claim left behind by a crashed writer (or a
    previous run of this same reconciler) must not block a finding
    indefinitely -- once CLAIM_TTL_SECONDS has passed, the NEXT sweep
    reclaims the marker and closes normally, rather than waiting on the
    daily janitor sweep to clear it."""
    root = _prep_root(tmp_path)
    finding = _pr_finding(171, "leaked claim")
    _write_finding(root, finding)

    claims_dir = root / "claims"
    claims_dir.mkdir(parents=True)
    stem = finding["id"].replace(":", "_")
    leaked = claims_dir / f"{stem}.claim"
    leaked.write_text(json.dumps({
        "actor": "github-reconciler",
        "at": "2020-01-01T00:00:00Z",
        "expires_at": "2020-01-01T00:05:00Z",  # long expired
    }))

    def fake_gh(args, *, dry_run=False):
        return [{
            "number": 171, "state": "CLOSED", "mergedAt": None,
            "closedAt": "2026-08-08T13:10:00Z", "title": "leaked claim",
            "url": "https://github.com/acme/widgets/pull/171",
        }]

    monkeypatch.setattr(pr_reconcile, "_gh", fake_gh)
    counts = pr_reconcile.reconcile_once("acme/widgets", root)
    assert counts["claim_held"] == 0
    assert counts["closed"] == 1
    assert (root / "rejected" / f"{stem}.json").exists()
    # The reclaimed marker was released again after the close, not leaked a
    # second time.
    assert not leaked.exists()


def test_live_unexpired_claim_from_a_previous_run_is_not_stolen(tmp_path):
    """the flip side of the reclaim test: a marker whose expires_at is still
    in the future must be left completely alone, even if it is old-looking
    (mtime is not what governs a marker WITH a parseable expires_at)."""
    root = _prep_root(tmp_path)
    finding = _pr_finding(172, "live claim")
    _write_finding(root, finding)

    claims_dir = root / "claims"
    claims_dir.mkdir(parents=True)
    stem = finding["id"].replace(":", "_")
    live = claims_dir / f"{stem}.claim"
    future = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + 3600))
    live.write_text(json.dumps({"actor": "someone-else", "at": pr_reconcile._now_iso(),
                                 "expires_at": future}))

    acquired = pr_reconcile._acquire_claim(root, finding["id"])
    assert acquired is None
    assert live.exists()
    body = json.loads(live.read_text())
    assert body["actor"] == "someone-else"  # untouched, not overwritten


def test_repair_path_is_also_claim_protected(tmp_path, monkeypatch):
    """design decision #1 explicitly names "_close_finding (and the repair
    path)" -- a live claim must block a repair exactly as it blocks a close."""
    root = _prep_root(tmp_path)
    finding = _pr_finding(173, "half-written, claim held")
    _write_finding(root, finding)

    from github_bridge import _append_ledger as real_append_ledger

    real_append_ledger(root, {
        "ts": "2026-08-08T13:00:00Z", "role": "external", "event": "rejected",
        "id": finding["id"], "actor": "github-reconciler",
        "detail": {"reason": "PR #173 merged on GitHub", "class": "answered",
                   "expires_at": "2026-08-15T13:00:00Z"},
    })

    result = pr_reconcile._acquire_claim(root, finding["id"])
    assert result is not None
    claim_path, _stale = result

    def fail_gh(args, *, dry_run=False):
        raise AssertionError("a claim-held repair must never reach a gh lookup")

    monkeypatch.setattr(pr_reconcile, "_gh", fail_gh)
    counts = pr_reconcile.reconcile_once("acme/widgets", root)
    assert counts["claim_held"] == 1
    assert counts["repaired"] == 0
    stem = finding["id"].replace(":", "_")
    assert not (root / "rejected" / f"{stem}.json").exists()


# ------------------------------------------------------ (m) round-3 hardening


def test_concurrent_expiry_reclaim_exactly_one_winner_live_claim_survives(tmp_path):
    """round 3, finding #1: an expired marker being reclaimed by two
    concurrent claimants must yield exactly one winner, and the winner's
    freshly-created LIVE marker must never be destroyed by the loser.

    History matters here: an earlier version of this fix relied on
    `rename()`'s own exclusivity alone (rename the stale marker away, then
    recreate). That was EMPIRICALLY flaky under real thread scheduling on
    this host, not just theoretically imperfect — a slower reclaimer's
    `rename()` call could execute strictly AFTER a faster reclaimer had
    already renamed-and-recreated, and `rename()` doesn't know or care what
    currently occupies its source path, so the slow reclaimer ended up
    moving the FAST reclaimer's brand-new LIVE marker away instead of the
    stale one it originally identified. The actual fix serializes the whole
    reclaim critical section behind a dedicated per-id O_EXCL lock (see
    `_acquire_claim`) — this test runs many concurrent attempts, not just
    one, because the earlier bug was intermittent and a single pass would
    not have caught it either."""
    for attempt in range(15):
        root = tmp_path / f"loopqueue-{attempt}"
        (root / "claims").mkdir(parents=True)
        finding_id = "sha256:" + "7" * 64
        path = pr_reconcile._claim_path(root, finding_id)
        path.write_text(json.dumps({
            "actor": "expired-holder",
            "at": "2020-01-01T00:00:00Z",
            "expires_at": "2020-01-01T00:05:00Z",
        }), encoding="utf-8")

        start = threading.Barrier(2)
        outcomes: dict[str, object] = {}

        def acquire(
            start=start, outcomes=outcomes, root=root, finding_id=finding_id
        ) -> None:
            start.wait(timeout=5)
            outcomes[threading.current_thread().name] = pr_reconcile._acquire_claim(
                root, finding_id
            )

        a = threading.Thread(target=acquire, name="reclaimer-A")
        b = threading.Thread(target=acquire, name="reclaimer-B")
        a.start()
        b.start()
        a.join(timeout=10)
        b.join(timeout=10)

        acquired = [name for name, result in outcomes.items() if result is not None]
        assert len(acquired) == 1, f"attempt {attempt}: multiple reclaimers acquired: {outcomes}"

        # The winner's LIVE marker must still exist and be genuinely live
        # (not destroyed by the loser touching the wrong path).
        winner_marker, _winner_stale = outcomes[acquired[0]]
        assert winner_marker.exists(), f"attempt {attempt}: winner's marker missing"
        body = json.loads(winner_marker.read_text())
        assert body["actor"] == "github-reconciler"


def test_empty_marker_older_than_grace_is_reclaimed(tmp_path):
    """round 3, finding #2: an EMPTY marker (O_EXCL created it, body never
    landed -- a crash mid-creation) older than CLAIM_EMPTY_GRACE_SECONDS must
    become reclaimable. The bug: a JSONDecodeError on the empty body used to
    reset the age basis to "now" on every attempt, so the marker aged out
    only ever restarted the clock and was never reclaimed."""
    root = tmp_path / "loopqueue"
    (root / "claims").mkdir(parents=True)
    finding_id = "sha256:" + "e" * 64
    marker = pr_reconcile._claim_path(root, finding_id)
    marker.touch()
    old = time.time() - (pr_reconcile.CLAIM_EMPTY_GRACE_SECONDS + 30)
    os.utime(marker, (old, old))

    result = pr_reconcile._acquire_claim(root, finding_id)
    assert result is not None
    acquired, stale = result
    assert acquired == marker
    # The renamed-away stale marker exists until whoever holds the new claim
    # releases it (see `_claim`'s finally, which deletes both paths) -- this
    # low-level call is not itself responsible for that cleanup.
    assert stale is not None and stale.exists()


def test_wrong_shape_json_marker_follows_unparseable_grace(tmp_path):
    """round 3, finding #2 sibling: valid JSON of the wrong shape (e.g. a
    bare list) must be treated exactly like unparseable JSON -- held until
    the grace period, never a crash at `.get()` on a non-dict."""
    root = tmp_path / "loopqueue"
    (root / "claims").mkdir(parents=True)
    finding_id = "sha256:" + "d" * 64
    marker = pr_reconcile._claim_path(root, finding_id)
    marker.write_text("[]", encoding="utf-8")
    old = time.time() - 60  # well within the 600s grace
    os.utime(marker, (old, old))

    result = pr_reconcile._acquire_claim(root, finding_id)  # must not raise
    assert result is None
    assert marker.exists()

    # And past the grace period, the same wrong-shape marker IS reclaimable.
    old2 = time.time() - (pr_reconcile.CLAIM_EMPTY_GRACE_SECONDS + 30)
    os.utime(marker, (old2, old2))
    result2 = pr_reconcile._acquire_claim(root, finding_id)
    assert result2 is not None
    acquired, _stale = result2
    assert acquired == marker


def test_repair_yields_to_foreign_claim_landed_after_snapshot(tmp_path):
    """round 3, finding #3: _repair_tombstone must re-check terminal
    ownership INSIDE its held claim, not trust the sweep's initial snapshot
    -- a foreign claim-respecting writer recording its OWN terminal event
    strictly between the snapshot and this claim being granted must win."""
    root = tmp_path / "loopqueue"
    root.mkdir(parents=True)

    from github_bridge import _append_ledger as real_append_ledger

    finding_id = "sha256:" + "9" * 64
    real_append_ledger(root, {
        "ts": "2026-08-08T12:00:00Z", "role": "external", "event": "rejected",
        "id": finding_id, "actor": "github-reconciler",
        "detail": {"reason": "orphaned first half", "class": "answered",
                   "expires_at": "2026-08-15T12:00:00Z"},
    })

    foreign, own_orphans = pr_reconcile._terminal_ledger_state(root)
    assert foreign == set() and finding_id in own_orphans

    # Between the sweep's snapshot above and the repair call below, a fully
    # claim-respecting foreign writer wins the SAME claim, records `merged`,
    # and releases.
    with pr_reconcile._claim(root, finding_id) as acquired:
        assert acquired
        real_append_ledger(root, {
            "ts": "2026-08-08T12:00:01Z", "role": "implementer", "event": "merged",
            "id": finding_id, "actor": "integration-loop",
            "detail": {"merge_sha": "a" * 40},
        })

    tombstone, outcome = pr_reconcile._repair_tombstone(
        root, finding_id, own_orphans[finding_id], dry_run=False
    )
    stem = finding_id.replace(":", "_")
    path = root / "rejected" / f"{stem}.json"

    assert outcome == "foreign_won"
    assert tombstone is None and not path.exists(), (
        "repair used a stale initial snapshot and wrote over a completed foreign close"
    )
    events = [json.loads(line) for line in (root / "ledger.jsonl").read_text().splitlines()]
    assert len(events) == 2  # the original orphan + the foreign merged; no new event added
