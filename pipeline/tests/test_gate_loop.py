"""gate_loop.py — the mechanical lander.

No network. Every test builds a throwaway git repo in tmp_path and drives the
daemon against a FAKE `offload` (a tiny script whose gate rc/slug/receipt are set
by env), so the real merge-gate and twin are never touched. The invariants under
test are the load-bearing ones the daemon exists to hold:

  * two approved candidates on DIFFERENT bases assemble into ONE train and both
    ff-land in a single gate.
  * an instrument-error gate result RE-GATES; it never rejects the candidate.
  * the ff-merge to main is serialised behind the single O_EXCL lockfile — a
    held lock stops a second instance landing anything.
  * an unknown refusal slug classifies to instrument-error, not candidate-defect.
  * a MISSING gate-status file classifies to instrument-error, NEVER a pass
    (the favourable-absence guard).
"""

from __future__ import annotations

import fcntl
import json
import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from bridge import gate_loop as gl  # noqa: E402
from bridge.canonical import content_id  # noqa: E402
from bridge.gate_host import GATE_LADDER_WORKERS  # noqa: E402
from bridge.gate_loop import (  # noqa: E402
    GateLoop,
    Lock,
    gate_state_path,
    iso_state_path,
    local_gate_command,
    read_gate_verdict,
    run_gate_child,
)
from bridge.integration import Candidate, GateVerdict, LedgerView  # noqa: E402
from bridge.review_policy import approved_cross_lineage, risky_review_paths  # noqa: E402
from bridge.train_assembler import Train, assemble_trains  # noqa: E402

# ------------------------------------------------------------------ helpers


def _git(repo: Path, *args: str, check: bool = True) -> str:
    p = subprocess.run(["git", "-C", str(repo), *args], capture_output=True,
                       text=True, check=False)
    if check and p.returncode != 0:
        raise AssertionError(f"git {' '.join(args)} failed: {p.stderr or p.stdout}")
    return p.stdout.strip()


def _init_repo(path: Path) -> Path:
    path.mkdir(parents=True)
    _git(path, "init", "-q", "-b", "main")
    _git(path, "config", "user.email", "t@example.invalid")
    _git(path, "config", "user.name", "tester")
    (path / "README.md").write_text("baseline\n", encoding="utf-8")
    _git(path, "add", "-A")
    _git(path, "commit", "-qm", "baseline")
    return path


def _commit_on(repo: Path, branch: str, start: str, filename: str, content: str) -> str:
    """Create `branch` at `start`, add one file, commit. Returns the branch tip."""
    _git(repo, "checkout", "-q", "-B", branch, start)
    target = repo / filename
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", f"{branch}: add {filename}")
    tip = _git(repo, "rev-parse", "HEAD")
    _git(repo, "checkout", "-q", "main")
    return tip


def _write_candidate(loops_root: Path, ident_hex: str, branch: str, base: str,
                     paths: list[str], *, producer_lineage: str = "anthropic",
                     verdict_lineage: str | None = "openai",
                     head_sha: str | None = None,
                     landing_repo: str | None = None,
                     resolves: object = "x") -> str:
    ident = f"sha256:{ident_hex}"
    if head_sha is None:
        candidate_repo = loops_root.parent / "repo"
        probe = subprocess.run(
            ["git", "-C", str(candidate_repo), "rev-parse", "--verify",
             f"{branch}^{{commit}}"], capture_output=True, text=True, check=False)
        if probe.returncode == 0:
            head_sha = probe.stdout.strip()
    art = {
        "contract": "v1.1", "id": ident, "kind": "candidate",
        "title": f"cand {branch}", "created_at": "2026-08-09T00:00:00Z",
        "producer": {"role": "implementer", "actor": "impl@x", "lineage": producer_lineage},
        "base_sha": base, "head_sha": head_sha, "branch": branch, "paths": paths,
        "evidence": [{"claim": "built", "verified_by": "execution",
                      "command": "pytest", "exit_code": 0}],
        "payload": {"resolves": resolves},
    }
    if landing_repo is not None:
        art["payload"]["landing_repo"] = landing_repo
    if verdict_lineage is not None:
        art["verdicts"] = [{"lineage": verdict_lineage, "model": "m",
                             "reviewed_sha": head_sha, "verdict": "approve"}]
    cdir = loops_root / "candidates"
    cdir.mkdir(parents=True, exist_ok=True)
    (cdir / f"sha256_{ident_hex}.json").write_text(json.dumps(art), encoding="utf-8")
    return ident


def _fake_offload(tmp_path: Path) -> str:
    """A stand-in for `offload gate`. Reads --tip/--receipt; writes a receipt (so
    receipt-less-defect downgrades do not fire spuriously) unless told not to;
    prints `refusing: <slug>` if FAKE_GATE_SLUG is set; exits FAKE_GATE_RC."""
    script = tmp_path / "fake_offload"
    script.write_text(
        "#!/usr/bin/env python3\n"
        "import os, sys, json\n"
        "args = sys.argv[1:]\n"
        "receipt = None\n"
        "for i, a in enumerate(args):\n"
        "    if a == '--receipt':\n"
        "        receipt = args[i+1]\n"
        "rc = int(os.environ.get('FAKE_GATE_RC', '0'))\n"
        "slug = os.environ.get('FAKE_GATE_SLUG', '')\n"
        "if receipt and os.environ.get('FAKE_GATE_NO_RECEIPT') != '1':\n"
        "    with open(receipt, 'w') as fh:\n"
        "        json.dump({'signed': True, 'rc': rc}, fh)\n"
        "if slug:\n"
        "    print('refusing: ' + slug)\n"
        "sys.exit(rc)\n",
        encoding="utf-8")
    script.chmod(0o755)
    return str(script)


def _fake_gate_workspace(base: Path, repo: Path | None = None) -> Path:
    """A pinned gate workspace whose scripts/merge-gate.sh is a stand-in gate,
    driven by the same FAKE_GATE_* env as the fake offload. The daemon runs LOCAL
    gates directly from THIS copy (mode=direct). Idempotent: reused across loops."""
    gw = base / "fake-gate"
    if repo is not None and not gw.exists():
        subprocess.run(["git", "-C", str(repo), "worktree", "add", "--detach",
                        "--force", str(gw), "main"], capture_output=True,
                       text=True, check=True)
    script = gw / "scripts" / "merge-gate.sh"
    if not script.exists():
        script.parent.mkdir(parents=True, exist_ok=True)
        script.write_text(
            "#!/bin/bash\n"
            "receipt=\"\"\n"
            "while [ $# -gt 0 ]; do\n"
            "  case \"$1\" in\n"
            "    --emit-receipt) receipt=\"$2\"; shift 2;;\n"
            "    *) shift;;\n"
            "  esac\n"
            "done\n"
            "rc=\"${FAKE_GATE_RC:-0}\"\n"
            "slug=\"${FAKE_GATE_SLUG:-}\"\n"
            "if [ -n \"$receipt\" ] && [ \"${FAKE_GATE_NO_RECEIPT:-}\" != \"1\" ]; then\n"
            "  printf '{\"signed\": true}\\n' > \"$receipt\"\n"
            "fi\n"
            "if [ -n \"$slug\" ]; then echo \"refusing: $slug\" >&2; fi\n"
            "exit \"$rc\"\n",
            encoding="utf-8")
        script.chmod(0o755)
    if repo is not None:
        # The production mint helper signs into records/merge-gate/<tip>.json.
        # This no-network stand-in preserves that filesystem contract while the
        # gate-loop tests focus on orchestration rather than cryptography.
        minter = gw / "scripts" / "mint-merge-candidate.py"
        minter.write_text(
            "#!/usr/bin/env python3\n"
            "import argparse, json\n"
            "from pathlib import Path\n"
            "p = argparse.ArgumentParser()\n"
            "p.add_argument('--candidate-sha', required=True)\n"
            "p.add_argument('--merge-base-sha', required=True)\n"
            "p.add_argument('--evidence-root', required=True)\n"
            "p.add_argument('--workspace', required=True)\n"
            "a = p.parse_args()\n"
            "r = Path(a.evidence_root) / 'records' / 'merge-gate' / (a.candidate_sha + '.json')\n"
            "r.parent.mkdir(parents=True, exist_ok=True)\n"
            "r.write_text(json.dumps({'signed': True, 'candidate_sha': a.candidate_sha}))\n",
            encoding="utf-8")
        minter.chmod(0o755)
    return gw


def _make_loop(loops_root: Path, repo: Path, offload: str, *, gate_ws: Path | None = None,
               **kw) -> GateLoop:
    (loops_root / "state").mkdir(parents=True, exist_ok=True)
    if gate_ws is None:
        gate_ws = _fake_gate_workspace(loops_root.parent, repo)
    kw.setdefault("remote", None)
    kw.setdefault("push", False)
    return GateLoop(loops_root, repo, offload_bin=offload, gate_ws=gate_ws,
                    python=sys.executable, **kw)


def _cand(ident_hex: str, branch: str, base: str,
          created: str = "2026-08-09T00:00:00Z") -> Candidate:
    art = {"id": f"sha256:{ident_hex}", "created_at": created}
    return Candidate(f"sha256:{ident_hex}", Path("x"), art, branch=branch, base_sha=base)


def _assemble(repo: Path, cands: list[Candidate], main_sha: str, tmp: Path, **kw):
    """Run assemble_trains in a fresh scratch worktree (removed afterwards)."""
    builder = tmp / f"builder-{time.monotonic_ns()}"
    subprocess.run(["git", "-C", str(repo), "worktree", "add", "--detach", "--force",
                    str(builder), "main"], capture_output=True, text=True, check=True)
    try:
        return assemble_trains(repo, builder, cands, main_sha, **kw)
    finally:
        subprocess.run(["git", "-C", str(repo), "worktree", "remove", "--force",
                        str(builder)], capture_output=True, text=True, check=False)


def _wait_done(sf: Path, timeout: float = 15.0) -> None:
    """Wait for the detached gate child to overwrite the state file to done."""
    end = time.time() + timeout
    while time.time() < end:
        if sf.exists():
            try:
                if json.loads(sf.read_text()).get("state") == "done":
                    return
            except (OSError, json.JSONDecodeError):
                pass
        time.sleep(0.05)
    raise AssertionError(f"gate child never finished: {sf}")


# ------------------------------------------------ lander heartbeat / builder recovery


def test_normal_tick_writes_ok_lander_heartbeat(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path / "repo")
    loop = _make_loop(tmp_path / "loops", repo, _fake_offload(tmp_path))
    assert loop.run_once() == []
    heartbeat = json.loads((loop.root / "state" / "landers.json").read_text())
    assert heartbeat["status"] == "ok"
    assert heartbeat["repo"] == loop.repo_key


def test_main_resolution_failure_publishes_degraded_heartbeat(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo = _init_repo(tmp_path / "repo")
    loop = _make_loop(tmp_path / "loops", repo, _fake_offload(tmp_path))
    monkeypatch.setattr(gl, "git", lambda *_a, **_kw: (1, "", "forced failure"))
    assert loop.run_once() == []
    heartbeat = json.loads((loop.root / "state" / "landers.json").read_text())
    assert heartbeat["status"] == "degraded"
    assert "resolve main" in heartbeat["reason"]


def test_builder_open_failure_publishes_degraded_heartbeat(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo = _init_repo(tmp_path / "repo")
    loop = _make_loop(tmp_path / "loops", repo, _fake_offload(tmp_path))
    candidate = _cand("a" * 64, "unused", "b" * 40)
    monkeypatch.setattr(loop, "load_candidates", lambda _terminal: [candidate])
    monkeypatch.setattr(loop, "_reconcile_already_merged", lambda cands, _main: cands)
    monkeypatch.setattr(loop, "_open_builder", lambda: None)
    monkeypatch.setattr(gl, "is_ancestor_of_main", lambda *_a: True)
    monkeypatch.setattr(gl, "git", lambda *_a, **_kw: (0, "c" * 40, ""))
    assert loop.run_once() == []
    heartbeat = json.loads((loop.root / "state" / "landers.json").read_text())
    assert heartbeat["status"] == "degraded"
    assert "open builder" in heartbeat["reason"]


def test_open_builder_refuses_active_or_orphaned_marker(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path / "repo")
    loop = _make_loop(tmp_path / "loops", repo, _fake_offload(tmp_path))
    scratch = loop._scratch()
    scratch.mkdir(parents=True)
    (scratch / "preserve.txt").write_text("live\n")
    loop._builder_active_path().write_text(json.dumps({"pid": 999999, "path": str(scratch)}))
    assert loop._open_builder() is None
    assert (scratch / "preserve.txt").read_text() == "live\n"
    assert any("active/orphaned" in alert for alert in loop.alerts)


def test_open_builder_quarantines_only_proven_unregistered_debris(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path / "repo")
    loop = _make_loop(tmp_path / "loops", repo, _fake_offload(tmp_path))
    scratch = loop._scratch()
    scratch.mkdir(parents=True)
    (scratch / "preserve.txt").write_text("debris\n")
    opened = loop._open_builder()
    try:
        quarantined = list(scratch.parent.glob(f"{scratch.name}.stale-*"))
        assert opened == scratch
        assert len(quarantined) == 1
        assert (quarantined[0] / "preserve.txt").read_text() == "debris\n"
    finally:
        loop._close_builder()


def test_open_builder_registry_failure_refuses_rename(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo = _init_repo(tmp_path / "repo")
    loop = _make_loop(tmp_path / "loops", repo, _fake_offload(tmp_path))
    scratch = loop._scratch()
    scratch.mkdir(parents=True)
    marker = scratch / "preserve.txt"
    marker.write_text("unknown registration\n")
    real_run = gl.subprocess.run

    def fail_registry(argv, *args, **kwargs):
        if isinstance(argv, list) and argv[-3:] == ["worktree", "list", "--porcelain"]:
            return subprocess.CompletedProcess(argv, 1, "", "registry unavailable")
        return real_run(argv, *args, **kwargs)

    monkeypatch.setattr(gl.subprocess, "run", fail_registry)
    assert loop._open_builder() is None
    assert marker.read_text() == "unknown registration\n"
    assert not list(scratch.parent.glob(f"{scratch.name}.stale-*"))


def test_builder_recovery_never_runs_unscoped_worktree_prune(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo = _init_repo(tmp_path / "repo")
    loop = _make_loop(tmp_path / "loops", repo, _fake_offload(tmp_path))
    calls: list[list[str]] = []
    real_run = gl.subprocess.run

    def record(argv, *args, **kwargs):
        if isinstance(argv, list):
            calls.append(argv)
        return real_run(argv, *args, **kwargs)

    monkeypatch.setattr(gl.subprocess, "run", record)
    assert loop._open_builder() == loop._scratch()
    loop._close_builder()
    assert not any(argv[-2:] == ["worktree", "prune"] for argv in calls)


def _orphan_initializing_builder(loop: GateLoop, repo: Path, *,
                                 reason: str = "initializing",
                                 age_s: float | None = None) -> tuple[Path, Path]:
    """Reproduce the debris an INTERRUPTED `git worktree add` leaves behind: a
    registered gate-loop-build worktree whose git admin dir carries a `locked`
    file (git's own transient `initializing` marker, verified as `initializing\\n`
    on git 2.43). Returns (scratch, locked_file). `age_s` backdates the marker's
    mtime so the staleness guard can be exercised in both directions.
    """
    scratch = loop._scratch()
    subprocess.run(["git", "-C", str(repo), "worktree", "add", "--detach", "--force",
                    str(scratch), "main"], capture_output=True, text=True, check=True)
    dotgit = (scratch / ".git").read_text(encoding="utf-8").strip()
    assert dotgit.startswith("gitdir: "), dotgit
    admin = Path(dotgit.removeprefix("gitdir: "))
    locked = admin / "locked"
    locked.write_text(reason + "\n", encoding="utf-8")
    if age_s is not None:
        old = time.time() - age_s
        os.utime(locked, (old, old))
    return scratch, locked


def test_open_builder_reclaims_orphaned_initializing_lock(tmp_path: Path) -> None:
    """The recurring production halt: a load-spike-timed-out `git worktree add`
    left git's transient `initializing` lock on gate-loop-build, and EVERY later
    tick then refused to build (all landing halted until a human cleared it). The
    daemon must RECLAIM that provably-orphaned marker and recreate a working
    builder, not refuse forever."""
    repo = _init_repo(tmp_path / "repo")
    loop = _make_loop(tmp_path / "loops", repo, _fake_offload(tmp_path))
    scratch, locked = _orphan_initializing_builder(
        loop, repo, age_s=gl.WORKTREE_ADD_TIMEOUT_S + 60)
    # Pre-fix, the lock indicator made _open_builder refuse outright.
    assert loop._builder_lock_reason(scratch) is not None
    assert loop._orphaned_initializing_lock(scratch) is True

    opened = loop._open_builder()
    try:
        assert opened == scratch
        assert scratch.exists() and (scratch / ".git").exists()
        assert not locked.exists()                       # git's transient marker cleared
        assert scratch.resolve() in (loop._registered_worktree_paths() or set())
        assert loop._builder_active_path().exists()      # a live, usable builder
        assert any("reclaiming orphaned" in a for a in loop.alerts)
        assert not any("refusing to disturb" in a for a in loop.alerts)
    finally:
        loop._close_builder()


def test_open_builder_refuses_non_initializing_lock(tmp_path: Path) -> None:
    """A lock whose reason is ANYTHING other than git's transient `initializing`
    (a human `git worktree lock --reason ...`, an empty manual lock) is a real
    hold. The self-heal must leave it completely untouched and keep refusing —
    never reclaim someone else's deliberate lock."""
    repo = _init_repo(tmp_path / "repo")
    loop = _make_loop(tmp_path / "loops", repo, _fake_offload(tmp_path))
    held = "held: operator investigating a wedged gate"
    scratch, locked = _orphan_initializing_builder(
        loop, repo, reason=held, age_s=gl.WORKTREE_ADD_TIMEOUT_S + 600)  # old, but NOT initializing
    (scratch / "preserve.txt").write_text("real work\n", encoding="utf-8")

    assert loop._orphaned_initializing_lock(scratch) is False
    assert loop._open_builder() is None
    assert any("refusing to disturb" in a for a in loop.alerts)
    assert not any("reclaiming orphaned" in a for a in loop.alerts)
    # untouched: the lock, its exact reason, the working dir, and its files
    assert locked.read_text(encoding="utf-8").strip() == held
    assert (scratch / "preserve.txt").read_text() == "real work\n"
    assert scratch.resolve() in (loop._registered_worktree_paths() or set())


def test_open_builder_does_not_reclaim_young_initializing_lock(tmp_path: Path) -> None:
    """Staleness guard. An `initializing` lock YOUNGER than the add timeout could
    belong to a live, in-progress add, so the daemon must NOT reclaim it — only a
    stale one that no live add could still hold. A young marker is left for a later
    tick (a bounded wait, never the permanent halt the old code produced)."""
    repo = _init_repo(tmp_path / "repo")
    loop = _make_loop(tmp_path / "loops", repo, _fake_offload(tmp_path))
    scratch, locked = _orphan_initializing_builder(loop, repo, age_s=5)  # 5s << timeout

    assert loop._orphaned_initializing_lock(scratch) is False
    assert loop._open_builder() is None
    assert any("refusing to disturb" in a for a in loop.alerts)
    assert not any("reclaiming orphaned" in a for a in loop.alerts)
    assert locked.exists()                                # left intact for a later tick
    assert scratch.resolve() in (loop._registered_worktree_paths() or set())


def test_open_builder_reclaim_is_path_scoped_and_spares_sibling_worktrees(
        tmp_path: Path) -> None:
    """F2 regression. Reclaiming gate-loop-build must be PATH-SCOPED and must NOT
    run a repo-global `git worktree prune`, which sweeps EVERY worktree whose
    working dir is merely missing-and-unlocked — silently deregistering an
    unrelated human/lane worktree that is mid-teardown or on an unmounted volume.
    A sibling in exactly that prune-bait state must survive the reclaim."""
    repo = _init_repo(tmp_path / "repo")
    loop = _make_loop(tmp_path / "loops", repo, _fake_offload(tmp_path))
    # A sibling worktree that is missing-on-disk yet still registered + unlocked:
    # precisely what a repo-global prune would collect as collateral.
    sibling = tmp_path / "sibling-lane"
    subprocess.run(["git", "-C", str(repo), "worktree", "add", "--detach", "--force",
                    str(sibling), "main"], capture_output=True, text=True, check=True)
    shutil.rmtree(sibling)
    assert sibling.resolve() in (loop._registered_worktree_paths() or set())

    # An orphaned, stale `initializing` lock on gate-loop-build triggers a reclaim.
    scratch, _ = _orphan_initializing_builder(
        loop, repo, age_s=gl.WORKTREE_ADD_TIMEOUT_S + 60)
    opened = loop._open_builder()
    try:
        assert opened == scratch                        # gate-loop-build reclaimed + recreated
        assert any("reclaiming orphaned" in a for a in loop.alerts)
        # THE F2 ASSERTION: the sibling was NOT swept as collateral.
        assert sibling.resolve() in (loop._registered_worktree_paths() or set())
    finally:
        loop._close_builder()


def _ledger_events(loops_root: Path) -> list[dict]:
    path = loops_root / "ledger.jsonl"
    if not path.exists():
        return []
    return [json.loads(ln) for ln in path.read_text().splitlines() if ln.strip()]


# ------------------------------------------------------------------ approval


def test_cross_lineage_required_for_approval():
    sha = "a" * 40
    base = {"producer": {"lineage": "anthropic"}}
    assert approved_cross_lineage({
        **base, "verdicts": [{"lineage": "openai", "model": "m",
                               "reviewed_sha": sha, "verdict": "approve"}],
    }, sha) is True
    # same lineage as producer -> NOT approved (the whole point of the rule)
    assert approved_cross_lineage(
        {**base, "verdicts": [{"lineage": "Anthropic"}]}, sha) is False
    # absent / empty verdicts -> NOT approved
    assert approved_cross_lineage({**base}, sha) is False
    assert approved_cross_lineage({**base, "verdicts": []}, sha) is False
    assert approved_cross_lineage({**base, "verdicts": [{"model": "m"}]}, sha) is False
    # Unknown/unattested labels are not evidence on a risky surface.
    assert approved_cross_lineage({
        "producer": {"lineage": "unattested"},
        "verdicts": [{"lineage": "openai"}],
    }, sha) is False
    assert approved_cross_lineage({
        "producer": {"lineage": "anthropic"},
        "verdicts": [{"lineage": "mixed", "model": "m", "verdict": "approve"}],
    }, sha) is False
    assert approved_cross_lineage({
        "producer": {"lineage": "anthropic"},
        "verdicts": [{"lineage": "openai", "model": "m",
                      "verdict": "request-changes"}],
    }, sha) is False
    assert approved_cross_lineage({
        "producer": {"lineage": "anthropic"},
        "verdicts": [{"lineage": "openai", "model": "m",
                      "verdict": "approve-with-fix"}],
    }, sha) is False
    for ambiguous in ("confirmed", "agree", "not refuted"):
        assert approved_cross_lineage({
            **base, "verdicts": [{"lineage": "openai", "model": "m",
                                   "reviewed_sha": sha, "verdict": ambiguous}],
        }, sha) is False
    assert approved_cross_lineage({
        "producer": {"lineage": "anthropic"},
        "verdicts": [{"lineage": "openai", "verdict": "approve"}],
    }, sha) is False  # no reviewer/model identity -> not genuine evidence
    assert approved_cross_lineage({
        **base, "verdicts": [{"lineage": "openai", "model": "m",
                               "reviewed_sha": "b" * 40, "verdict": "approve"}],
    }, sha) is False


def test_risky_review_paths_cover_self_governing_and_money_surfaces():
    paths = {
        ".mcp.json",
        "docs/readme.md",
        ".github/workflows/ci.yml",
        "configs/mcp-approved.yaml",
        "omniagentos/db/migrations/123_money.sql",
        "omniagentos/policy/approvals.py",
        "pipeline/bridge/gate_host.py",
        "pipeline/bridge/gate_loop.py",
        "pipeline/bridge/review_policy.py",
        "pipeline/launchd/install.sh",
        "scripts/merge-gate.sh",
        "services/stripe/refunds.py",
        "tests/test_routine_widget.py",
    }
    assert risky_review_paths(paths) == [
        ".github/workflows/ci.yml",
        ".mcp.json",
        "configs/mcp-approved.yaml",
        "omniagentos/db/migrations/123_money.sql",
        "omniagentos/policy/approvals.py",
        "pipeline/bridge/gate_host.py",
        "pipeline/bridge/gate_loop.py",
        "pipeline/bridge/review_policy.py",
        "pipeline/launchd/install.sh",
        "scripts/merge-gate.sh",
        "services/stripe/refunds.py",
    ]


def test_routine_candidate_needs_no_separate_verdict_but_still_dispatches_gate(tmp_path):
    repo = _init_repo(tmp_path / "repo")
    base = _git(repo, "rev-parse", "HEAD")
    tip = _commit_on(repo, "routine", base, "omniagentos/widgets/render.py", "ok\n")
    loops_root = tmp_path / "loops"
    _write_candidate(loops_root, "7" * 64, "routine", base,
                     ["omniagentos/widgets/render.py"], verdict_lineage=None,
                     head_sha=tip, landing_repo="repo")
    loop = _make_loop(loops_root, repo, _fake_offload(tmp_path))

    cands = loop.load_candidates(set())
    assert len(cands) == 1
    out = loop.run_once()
    assert len(out) == 1 and out[0].action == "dispatched", out


def test_risky_candidate_without_cross_lineage_verdict_is_not_gate_eligible(tmp_path):
    repo = _init_repo(tmp_path / "repo")
    base = _git(repo, "rev-parse", "HEAD")
    tip = _commit_on(repo, "risky", base, "omniagentos/policy/access.py", "deny\n")
    loops_root = tmp_path / "loops"
    _write_candidate(loops_root, "8" * 64, "risky", base,
                     ["omniagentos/policy/access.py"], verdict_lineage=None,
                     head_sha=tip, landing_repo="repo")
    loop = _make_loop(loops_root, repo, _fake_offload(tmp_path))

    assert loop.load_candidates(set()) == []
    assert any("risky diff requires" in line for line in loop.lines)


def test_risky_tier_uses_real_diff_not_understated_envelope_paths(tmp_path):
    repo = _init_repo(tmp_path / "repo")
    base = _git(repo, "rev-parse", "HEAD")
    tip = _commit_on(repo, "understated", base, "payments/refund.py", "refund\n")
    loops_root = tmp_path / "loops"
    _write_candidate(loops_root, "9" * 64, "understated", base,
                     ["docs/readme.md"], verdict_lineage=None,
                     head_sha=tip, landing_repo="repo")
    loop = _make_loop(loops_root, repo, _fake_offload(tmp_path))

    assert loop.load_candidates(set()) == []
    assert any("payments/refund.py" in line for line in loop.lines)


def test_risky_candidate_with_genuine_cross_lineage_verdict_is_eligible(tmp_path):
    repo = _init_repo(tmp_path / "repo")
    base = _git(repo, "rev-parse", "HEAD")
    tip = _commit_on(repo, "reviewed", base, "schema/access.json", "{}\n")
    loops_root = tmp_path / "loops"
    _write_candidate(loops_root, "a" * 64, "reviewed", base,
                     ["schema/access.json"], producer_lineage="anthropic",
                     verdict_lineage="openai", head_sha=tip, landing_repo="repo")
    loop = _make_loop(loops_root, repo, _fake_offload(tmp_path))

    assert len(loop.load_candidates(set())) == 1


# ----------------------- converged repo / immutable identity ----------------


def test_legacy_repo_label_no_longer_routes_but_missing_exact_sha_refuses(tmp_path):
    repo = _init_repo(tmp_path / "repo")
    base = _git(repo, "rev-parse", "HEAD")
    loops_root = tmp_path / "loops"
    _write_candidate(loops_root, "1" * 64, "branch-that-is-not-here", base,
                     ["bridge/x.py"], landing_repo="ThreeLoops")
    loop = _make_loop(loops_root, repo, _fake_offload(tmp_path))

    assert loop.load_candidates(set()) == []
    assert any("missing full immutable head_sha" in line for line in loop.lines), loop.lines
    assert not any("targets repo" in line for line in loop.lines)


def test_missing_builder_branch_falls_back_to_approved_head_sha(tmp_path):
    repo = _init_repo(tmp_path / "repo")
    base = _git(repo, "rev-parse", "HEAD")
    tip = _commit_on(repo, "object-anchor", base, "x.txt", "approved\n")
    loops_root = tmp_path / "loops"
    _write_candidate(loops_root, "2" * 64, "deleted-builder-branch", base,
                     ["x.txt"], head_sha=tip, landing_repo="repo")
    loop = _make_loop(loops_root, repo, _fake_offload(tmp_path))

    cands = loop.load_candidates(set())
    assert len(cands) == 1
    assert cands[0].tip_sha == tip
    assert cands[0].branch == tip  # exact immutable ref, not a mutable fallback


def test_moved_branch_is_not_substituted_for_approved_head(tmp_path):
    repo = _init_repo(tmp_path / "repo")
    base = _git(repo, "rev-parse", "HEAD")
    approved_tip = _commit_on(repo, "candidate", base, "x.txt", "approved\n")
    _git(repo, "checkout", "-q", "candidate")
    (repo / "x.txt").write_text("moved after review\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "move candidate after approval")
    _git(repo, "checkout", "-q", "main")
    loops_root = tmp_path / "loops"
    _write_candidate(loops_root, "3" * 64, "candidate", base, ["x.txt"],
                     head_sha=approved_tip, landing_repo="repo")
    loop = _make_loop(loops_root, repo, _fake_offload(tmp_path))

    assert loop.load_candidates(set()) == []
    assert any("moved away from approved head_sha" in line for line in loop.lines)


def _rewrite_candidate(loops_root: Path, ident_hex: str, mutate) -> None:
    p = loops_root / "candidates" / f"sha256_{ident_hex}.json"
    art = json.loads(p.read_text())
    mutate(art)
    p.write_text(json.dumps(art), encoding="utf-8")


def _findings(loops_root: Path) -> list[Path]:
    fdir = loops_root / "findings"
    return sorted(fdir.glob("*.json")) if fdir.is_dir() else []


def test_payload_head_sha_is_hoisted_and_eligible(tmp_path):
    """The ThreeLoops-side schema never required head_sha, so a whole producer
    class nests it under payload. The payload is the content id, so that value
    is immutable producer-authored provenance — the loader accepts it."""
    repo = _init_repo(tmp_path / "repo")
    base = _git(repo, "rev-parse", "HEAD")
    tip = _commit_on(repo, "lane-nested", base, "x.txt", "work\n")
    loops_root = tmp_path / "loops"
    _write_candidate(loops_root, "4" * 64, "lane-nested", base, ["x.txt"], head_sha=tip)

    def nest(art):
        art["payload"]["head_sha"] = art.pop("head_sha")
        art["id"] = content_id(art["payload"])   # the hoist trusts only a bound payload
    _rewrite_candidate(loops_root, "4" * 64, nest)
    loop = _make_loop(loops_root, repo, _fake_offload(tmp_path))

    cands = loop.load_candidates(set())
    assert len(cands) == 1
    assert cands[0].tip_sha == tip
    assert cands[0].branch == tip  # frozen to the resolved commit, not the ref
    assert any("hoisted from payload" in line for line in loop.lines)
    assert _findings(loops_root) == []  # eligible -> nothing to announce


def test_payload_head_sha_moved_branch_is_still_refused(tmp_path):
    """Hoisting must not weaken the identity rule: a branch that moved away
    from the payload-approved head is still never silently substituted."""
    repo = _init_repo(tmp_path / "repo")
    base = _git(repo, "rev-parse", "HEAD")
    approved_tip = _commit_on(repo, "lane-moved", base, "x.txt", "approved\n")
    _git(repo, "checkout", "-q", "lane-moved")
    (repo / "x.txt").write_text("moved after review\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "move after approval")
    _git(repo, "checkout", "-q", "main")
    loops_root = tmp_path / "loops"
    _write_candidate(loops_root, "6" * 64, "lane-moved", base, ["x.txt"],
                     head_sha=approved_tip)

    def nest(art):
        art["payload"]["head_sha"] = art.pop("head_sha")
        art["id"] = content_id(art["payload"])
    _rewrite_candidate(loops_root, "6" * 64, nest)
    loop = _make_loop(loops_root, repo, _fake_offload(tmp_path))

    assert loop.load_candidates(set()) == []
    assert any("moved away from approved head_sha" in line for line in loop.lines)
    finds = _findings(loops_root)
    assert len(finds) == 1
    assert json.loads(finds[0].read_text())["payload"]["reason_class"] == "branch-moved"


def test_missing_head_sha_files_one_deduped_finding_with_remedy(tmp_path):
    """The silent-skip starvation guard: a candidate with no head_sha anywhere
    is announced ONCE as a finding naming its own remedy, with a `found` ledger
    event — and a second tick neither re-files nor re-logs it to the ledger."""
    repo = _init_repo(tmp_path / "repo")
    base = _git(repo, "rev-parse", "HEAD")
    tip = _commit_on(repo, "lane-bare", base, "y.txt", "work\n")
    loops_root = tmp_path / "loops"
    _write_candidate(loops_root, "5" * 64, "lane-bare", base, ["y.txt"])

    def strip(art):
        art.pop("head_sha", None)
        art["payload"].pop("head_sha", None)
    _rewrite_candidate(loops_root, "5" * 64, strip)
    loop = _make_loop(loops_root, repo, _fake_offload(tmp_path))

    assert loop.load_candidates(set()) == []
    finds = _findings(loops_root)
    assert len(finds) == 1
    art = json.loads(finds[0].read_text())
    assert art["kind"] == "finding"
    assert art["payload"]["reason_class"] == "missing-head-sha"
    assert art["payload"]["symptom"]                       # schema-required field
    assert "head_sha" in art["payload"]["remedy"]          # names its own remedy
    # evidence carries the branch's CURRENT tip so the remedy is actionable
    assert any(tip in (e.get("claim") or "") for e in art["evidence"])
    found = [e for e in _ledger_events(loops_root) if e.get("event") == "found"]
    assert len(found) == 1 and found[0]["id"] == art["id"]

    assert loop.load_candidates(set()) == []               # second tick
    assert len(_findings(loops_root)) == 1                 # no re-file
    assert len([e for e in _ledger_events(loops_root)
                if e.get("event") == "found"]) == 1        # no re-log


def test_short_or_garbage_payload_head_sha_is_not_used(tmp_path):
    repo = _init_repo(tmp_path / "repo")
    base = _git(repo, "rev-parse", "HEAD")
    _commit_on(repo, "lane-garbage", base, "z.txt", "work\n")
    loops_root = tmp_path / "loops"
    _write_candidate(loops_root, "7" * 64, "lane-garbage", base, ["z.txt"])

    def corrupt(art):
        art.pop("head_sha", None)
        art["payload"]["head_sha"] = "deadbeef"            # short, not 40 hex
    _rewrite_candidate(loops_root, "7" * 64, corrupt)
    loop = _make_loop(loops_root, repo, _fake_offload(tmp_path))

    assert loop.load_candidates(set()) == []
    assert any("missing full immutable head_sha" in line for line in loop.lines)
    finds = _findings(loops_root)
    assert len(finds) == 1
    assert json.loads(finds[0].read_text())["payload"]["reason_class"] == "missing-head-sha"


def test_hoist_refused_when_payload_not_bound_to_id(tmp_path):
    """A payload head_sha is only trusted because the envelope id hashes the
    payload. An id that does NOT hash this payload proves nothing — a mutated
    payload could smuggle in any tip — so the hoist refuses, loudly."""
    repo = _init_repo(tmp_path / "repo")
    base = _git(repo, "rev-parse", "HEAD")
    tip = _commit_on(repo, "lane-tamper", base, "x.txt", "work\n")
    loops_root = tmp_path / "loops"
    _write_candidate(loops_root, "9" * 64, "lane-tamper", base, ["x.txt"], head_sha=tip)

    def nest_without_rebinding(art):
        art["payload"]["head_sha"] = art.pop("head_sha")
        # id left as sha256:999... — does NOT hash this payload
    _rewrite_candidate(loops_root, "9" * 64, nest_without_rebinding)
    loop = _make_loop(loops_root, repo, _fake_offload(tmp_path))

    assert loop.load_candidates(set()) == []
    assert any("does not hash to the envelope id" in line for line in loop.lines)
    finds = _findings(loops_root)
    assert len(finds) == 1
    assert json.loads(finds[0].read_text())["payload"]["reason_class"] == "payload-id-mismatch"


def test_conflicting_top_level_and_payload_head_sha_refused(tmp_path):
    """When both carriers name a head they must agree: the top-level field is
    outside the content id and may never override the hashed payload."""
    repo = _init_repo(tmp_path / "repo")
    base = _git(repo, "rev-parse", "HEAD")
    tip_a = _commit_on(repo, "lane-a", base, "a.txt", "a\n")
    tip_b = _commit_on(repo, "lane-b", base, "b.txt", "b\n")
    loops_root = tmp_path / "loops"
    _write_candidate(loops_root, "a1" * 32, "lane-a", base, ["a.txt"], head_sha=tip_a)

    def conflict(art):
        art["payload"]["head_sha"] = tip_b        # hashed carrier disagrees
    _rewrite_candidate(loops_root, "a1" * 32, conflict)
    loop = _make_loop(loops_root, repo, _fake_offload(tmp_path))

    assert loop.load_candidates(set()) == []
    assert any("conflicts with payload head_sha" in line for line in loop.lines)
    finds = _findings(loops_root)
    assert len(finds) == 1
    assert json.loads(finds[0].read_text())["payload"]["reason_class"] == "head-sha-conflict"

    # agreeing carriers stay eligible
    def agree(art):
        art["payload"]["head_sha"] = tip_a
    _rewrite_candidate(loops_root, "a1" * 32, agree)
    loop2 = _make_loop(loops_root, repo, _fake_offload(tmp_path))
    cands = loop2.load_candidates(set())
    assert len(cands) == 1 and cands[0].tip_sha == tip_a


def test_found_event_healed_when_artifact_exists_without_it(tmp_path):
    """A crash between the finding write and the ledger append must not freeze
    the finding invisible: the next tick heals the missing `found` event."""
    repo = _init_repo(tmp_path / "repo")
    base = _git(repo, "rev-parse", "HEAD")
    _commit_on(repo, "lane-crash", base, "c.txt", "work\n")
    loops_root = tmp_path / "loops"
    _write_candidate(loops_root, "b2" * 32, "lane-crash", base, ["c.txt"])

    def strip(art):
        art.pop("head_sha", None)
        art["payload"].pop("head_sha", None)
    _rewrite_candidate(loops_root, "b2" * 32, strip)
    loop = _make_loop(loops_root, repo, _fake_offload(tmp_path))

    assert loop.load_candidates(set()) == []
    assert len(_findings(loops_root)) == 1
    # simulate the crash window: artifact persisted, ledger append lost
    (loops_root / "ledger.jsonl").write_text("", encoding="utf-8")

    assert loop.load_candidates(set()) == []
    found = [e for e in _ledger_events(loops_root) if e.get("event") == "found"]
    assert len(found) == 1                       # healed, exactly once
    assert found[0]["detail"].get("healed") is True
    assert len(_findings(loops_root)) == 1       # artifact not duplicated

    assert loop.load_candidates(set()) == []     # and the heal does not repeat
    assert len([e for e in _ledger_events(loops_root)
                if e.get("event") == "found"]) == 1


def test_ineligibility_finding_validates_against_envelope_schema(tmp_path):
    """The daemon must never publish an envelope the shared queue schema
    rejects (execution evidence requires an integer exit_code)."""
    jsonschema = pytest.importorskip("jsonschema")
    repo = _init_repo(tmp_path / "repo")
    base = _git(repo, "rev-parse", "HEAD")
    _commit_on(repo, "lane-schema", base, "s.txt", "work\n")
    loops_root = tmp_path / "loops"
    _write_candidate(loops_root, "c3" * 32, "lane-schema", base, ["s.txt"])

    def strip(art):
        art.pop("head_sha", None)
        art["payload"].pop("head_sha", None)
    _rewrite_candidate(loops_root, "c3" * 32, strip)
    loop = _make_loop(loops_root, repo, _fake_offload(tmp_path))
    assert loop.load_candidates(set()) == []

    finds = _findings(loops_root)
    assert len(finds) == 1
    schema = json.loads((ROOT / "schema" / "envelope.schema.json").read_text())
    jsonschema.Draft202012Validator(schema).validate(json.loads(finds[0].read_text()))


def test_ineligibility_finding_suppressed_by_rejected_or_parked_marker(tmp_path):
    """Moving the finding to rejected/ (or parked/) must suppress re-filing —
    the reviewer's disposition is not overridden by the next tick."""
    repo = _init_repo(tmp_path / "repo")
    base = _git(repo, "rev-parse", "HEAD")
    _commit_on(repo, "lane-park", base, "w.txt", "work\n")
    loops_root = tmp_path / "loops"
    _write_candidate(loops_root, "8" * 64, "lane-park", base, ["w.txt"])

    def strip(art):
        art.pop("head_sha", None)
        art["payload"].pop("head_sha", None)
    _rewrite_candidate(loops_root, "8" * 64, strip)
    loop = _make_loop(loops_root, repo, _fake_offload(tmp_path))

    assert loop.load_candidates(set()) == []
    finds = _findings(loops_root)
    assert len(finds) == 1
    stem = finds[0].name
    (loops_root / "rejected").mkdir(parents=True, exist_ok=True)
    finds[0].rename(loops_root / "rejected" / stem)

    assert loop.load_candidates(set()) == []
    assert _findings(loops_root) == []                     # not re-filed


def test_already_on_main_candidate_is_reconciled_without_a_gate(tmp_path):
    repo = _init_repo(tmp_path / "repo")
    main = _git(repo, "rev-parse", "HEAD")
    loops_root = tmp_path / "loops"
    ident = _write_candidate(loops_root, "4" * 64, "already-deleted", main,
                             ["README.md"], head_sha=main, landing_repo="repo")
    loop = _make_loop(loops_root, repo, _fake_offload(tmp_path))

    assert loop.run_once() == []
    merged = [e for e in _ledger_events(loops_root)
              if e.get("event") == "merged" and e.get("id") == ident]
    assert len(merged) == 1
    assert merged[0]["detail"]["reconciled"] is True
    assert merged[0]["detail"]["merge_sha"] == main
    assert not list((loops_root / "state" / "gates").glob("*.json"))


# ----------------------- protected surface scheduling -----------------------


def test_gate_surface_builds_a_one_member_train(tmp_path):
    repo = _init_repo(tmp_path / "repo")
    base = _git(repo, "rev-parse", "HEAD")
    _commit_on(repo, "gate-change", base, "scripts/merge-gate.sh", "safe change\n")
    trains, excluded = _assemble(
        repo, [_cand("5" * 64, "gate-change", base)], base, tmp_path)

    assert len(trains) == 1
    assert len(trains[0].members) == 1
    assert trains[0].paths == ["scripts/merge-gate.sh"]
    assert not any(e.get("id") == "sha256:" + "5" * 64 for e in excluded)


@pytest.mark.parametrize("critical_path", [
    "pipeline/bridge/gate_loop.py",
    "pipeline/bridge/gate_host.py",
    "pipeline/bridge/train_assembler.py",
    "pipeline/bridge/integration.py",
    "pipeline/bridge/review_policy.py",
    "omniagentos/scheduler/gate_evidence.py",
])
def test_converged_pipeline_critical_surface_is_one_member(tmp_path, critical_path):
    repo = _init_repo(tmp_path / "repo")
    base = _git(repo, "rev-parse", "HEAD")
    _commit_on(repo, "pipeline-change", base, critical_path, "safe change\n")
    trains, excluded = _assemble(
        repo, [_cand("5" * 64, "pipeline-change", base)], base, tmp_path)

    assert len(trains) == 1
    assert len(trains[0].members) == 1
    assert trains[0].paths == [critical_path]
    assert excluded == []


def test_accounts_surface_remains_human_only(tmp_path):
    repo = _init_repo(tmp_path / "repo")
    base = _git(repo, "rev-parse", "HEAD")
    _commit_on(repo, "secret-change", base, "configs/accounts.yaml", "secret: no\n")
    trains, excluded = _assemble(
        repo, [_cand("6" * 64, "secret-change", base)], base, tmp_path)

    assert trains == []
    assert any("human-only secret surface" in e.get("why", "") for e in excluded)


def test_private_key_surface_remains_human_only(tmp_path):
    repo = _init_repo(tmp_path / "repo")
    base = _git(repo, "rev-parse", "HEAD")
    _commit_on(repo, "secret-change", base, "keys/production.pem", "not-a-key\n")
    trains, excluded = _assemble(
        repo, [_cand("6" * 64, "secret-change", base)], base, tmp_path)

    assert trains == []
    assert any("human-only secret surface" in e.get("why", "") for e in excluded)


# ----------------------- two approved, different bases, one train, both land --


def test_two_approved_different_bases_one_train_both_land(tmp_path):
    repo = _init_repo(tmp_path / "repo")
    m0 = _git(repo, "rev-parse", "HEAD")
    # candidate A branches from M0 (will fall behind), touches a.txt
    _commit_on(repo, "cand-a", m0, "a.txt", "AAA\n")
    # advance main to M1
    (repo / "m.txt").write_text("main-moved\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "main advances to M1")
    m1 = _git(repo, "rev-parse", "HEAD")
    # candidate B branches from M1, touches b.txt (disjoint from A)
    _commit_on(repo, "cand-b", m1, "b.txt", "BBB\n")

    loops_root = tmp_path / "loops"
    id_a = _write_candidate(loops_root, "a" * 64, "cand-a", m0, ["a.txt"])
    id_b = _write_candidate(loops_root, "b" * 64, "cand-b", m1, ["b.txt"])

    offload = _fake_offload(tmp_path)
    os.environ["FAKE_GATE_RC"] = "0"
    os.environ.pop("FAKE_GATE_SLUG", None)
    try:
        loop = _make_loop(loops_root, repo, offload)
        # tick 1: assemble ONE train (A rebased forward onto M1, disjoint from B)
        out1 = loop.run_once()
        assert len(out1) == 1 and out1[0].action == "dispatched", out1
        train = out1[0].train
        assert {m["id"] for m in train.members} == {id_a, id_b}
        assert train.base == m1                      # built on CURRENT main
        _wait_done(gate_state_path(loops_root, train))
        # tick 2: read PASS -> ff-land both members
        loop2 = _make_loop(loops_root, repo, offload)
        out2 = loop2.run_once()
        assert out2[0].action == "landed", (out2[0].action, out2[0].detail, loop2.lines)
    finally:
        os.environ.pop("FAKE_GATE_RC", None)

    new_main = _git(repo, "rev-parse", "main")
    assert new_main == train.tip                     # main fast-forwarded to the train tip
    # both files are on main
    assert (repo / "a.txt").exists() and (repo / "b.txt").exists()
    merged = [e for e in _ledger_events(loops_root) if e["event"] == "merged"]
    assert {e["id"] for e in merged} == {id_a, id_b}
    assert all(e["detail"]["merge_sha"] == new_main for e in merged)
    # tick 3: nothing left to land (members are terminal), no double-merge
    loop3 = _make_loop(loops_root, repo, offload)
    out3 = loop3.run_once()
    assert all(o.action != "landed" for o in out3)
    assert _git(repo, "rev-parse", "main") == new_main
    assert _git(repo, "branch", "--list", train.branch) == ""


# ----------------------------- instrument error -> re-gate, NEVER reject ------


def test_instrument_error_regates_not_rejects(tmp_path):
    repo = _init_repo(tmp_path / "repo")
    m0 = _git(repo, "rev-parse", "HEAD")
    _commit_on(repo, "cand-i", m0, "i.txt", "III\n")
    loops_root = tmp_path / "loops"
    id_i = _write_candidate(loops_root, "c" * 64, "cand-i", m0, ["i.txt"])

    offload = _fake_offload(tmp_path)
    os.environ["FAKE_GATE_RC"] = "127"               # could-not-run -> instrument error
    os.environ["FAKE_GATE_NO_RECEIPT"] = "1"
    os.environ.pop("FAKE_GATE_SLUG", None)
    try:
        loop = _make_loop(loops_root, repo, offload)
        out1 = loop.run_once()
        train = out1[0].train
        sf = gate_state_path(loops_root, train)
        _wait_done(sf)
        loop2 = _make_loop(loops_root, repo, offload)
        out2 = loop2.run_once()
        assert out2[0].action == "instrument", out2[0]
    finally:
        os.environ.pop("FAKE_GATE_RC", None)
        os.environ.pop("FAKE_GATE_NO_RECEIPT", None)

    events = _ledger_events(loops_root)
    assert any(e["event"] == "instrument_error" for e in events)
    # NEVER a rejection for an instrument fault, and the candidate stays landable
    assert not any(e["event"] == "rejected" and e.get("id") == id_i for e in events)
    assert id_i not in {e["id"] for e in events if e["event"] in ("merged", "rejected")}
    # an inquiry (area: tooling) was raised
    inq = list((loops_root / "inquiries").glob("*.json"))
    assert inq, "instrument error must raise an inquiry"
    assert json.loads(inq[0].read_text())["payload"]["area"] == "tooling"
    # re-gate ONCE: the running-state file was cleared so the next tick re-dispatches
    assert not sf.exists(), "instrument error must clear state to re-gate once"


# ----------------------- unknown slug -> instrument-error, not candidate-defect


def test_unknown_slug_is_instrument_error_not_candidate_defect(tmp_path):
    sf = tmp_path / "gate.json"
    sf.write_text(json.dumps({
        "state": "done", "rc": 2,
        "stdout": "running suites...\nrefusing: totally-unknown-slug\n",
        "stderr": "", "receipt": None, "duration_s": 1.0}), encoding="utf-8")
    v = read_gate_verdict(sf)
    assert v is not None
    assert v.result == "instrument-error", v
    assert v.result != "candidate-defect"


# --------------------------- missing status file -> instrument-error, not pass


def test_missing_status_file_is_instrument_error_never_pass(tmp_path):
    v = read_gate_verdict(tmp_path / "does-not-exist.json")
    assert v is not None
    assert v.result == "instrument-error", v
    assert v.result != "pass"      # favourable-absence guard: absence is NEVER a pass


def test_running_past_deadline_is_instrument_error_never_pass(tmp_path):
    sf = tmp_path / "gate.json"
    sf.write_text(json.dumps({"state": "running", "deadline": 1000.0}), encoding="utf-8")
    v = read_gate_verdict(sf, now=99999.0)           # well past the deadline
    assert v is not None and v.result == "instrument-error"
    # still within the deadline -> genuinely running, not a verdict
    assert read_gate_verdict(sf, now=500.0) is None


def test_running_without_a_deadline_is_never_a_deadline_expiry_verdict(tmp_path):
    """A MISSING deadline is not an EXPIRED deadline.

    `gate-deadline-expired` is the one instrument verdict whose handler reaps
    (SIGTERM/SIGKILL) a child that may still be alive. Deriving it from a state
    that never recorded a deadline would kill a live gate on no timing evidence
    at all — the deadline-less lease is the watchdog's business (it pages), and
    the lander's job is only to keep holding the slot. Both directions are
    pinned here: absent, and non-numeric.
    """
    for i, shape in enumerate(({"state": "running"},
                               {"state": "running", "deadline": None},
                               {"state": "running", "deadline": "3600"})):
        sf = tmp_path / f"deadlineless-{i}.json"
        sf.write_text(json.dumps(shape), encoding="utf-8")
        v = read_gate_verdict(sf, now=99999999.0)   # any clock, however late
        assert v is None, (
            f"deadline-less running state {shape} classified as {v!r}; a state with "
            "no deadline can never be PAST one, and this verdict reaps the child")


# ----------------------- two trains never ff-merge concurrently (lock held) ---


def test_ff_merge_serialised_behind_single_lock(tmp_path):
    repo = _init_repo(tmp_path / "repo")
    m0 = _git(repo, "rev-parse", "HEAD")
    _commit_on(repo, "cand-x", m0, "x.txt", "XXX\n")
    loops_root = tmp_path / "loops"
    (loops_root / "state").mkdir(parents=True, exist_ok=True)
    _write_candidate(loops_root, "d" * 64, "cand-x", m0, ["x.txt"])

    # Another gate-loop instance is mid-land: it holds the lock (a LIVE pid).
    lock_path = loops_root / "locks" / "gate-loop.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path.write_text(json.dumps({"pid": os.getpid(), "at": "now"}), encoding="utf-8")

    main_before = _git(repo, "rev-parse", "main")
    offload = _fake_offload(tmp_path)
    # main() must REFUSE to run a second lander while the lock is held, rather
    # than proceeding "just to check" and risking a second writer on main.
    with pytest.raises(SystemExit):
        gl.main(["--loops-root", str(loops_root), "--repo", str(repo),
                 "--offload", offload, "--no-push", "--once"])
    # nothing landed: main is untouched and no merged/rejected event was written
    assert _git(repo, "rev-parse", "main") == main_before
    assert _ledger_events(loops_root) == []


def test_lock_is_exclusive_and_steals_stale(tmp_path):
    lock_path = tmp_path / "gate-loop.lock"
    with Lock(lock_path):
        assert lock_path.exists()
        # a second acquisition while held must refuse, not proceed
        with pytest.raises(SystemExit), Lock(lock_path):
            pass
    assert not lock_path.exists()                    # released on exit

    # a stale lock (dead pid) is stolen, not honoured
    lock_path.write_text(json.dumps({"pid": 2 ** 31 - 1, "at": "old"}), encoding="utf-8")
    with Lock(lock_path):
        assert json.loads(lock_path.read_text())["pid"] == os.getpid()


# --------------------------- candidate-defect -> reject with TTL + file --------


def test_candidate_defect_rejects_with_ttl(tmp_path):
    repo = _init_repo(tmp_path / "repo")
    m0 = _git(repo, "rev-parse", "HEAD")
    _commit_on(repo, "cand-d", m0, "d.txt", "DDD\n")
    loops_root = tmp_path / "loops"
    id_d = _write_candidate(loops_root, "e" * 64, "cand-d", m0, ["d.txt"])

    offload = _fake_offload(tmp_path)
    os.environ["FAKE_GATE_RC"] = "2"
    os.environ["FAKE_GATE_SLUG"] = "secrets"          # a real candidate-defect slug
    try:
        loop = _make_loop(loops_root, repo, offload)
        out1 = loop.run_once()
        train = out1[0].train
        _wait_done(gate_state_path(loops_root, train))
        loop2 = _make_loop(loops_root, repo, offload)
        out2 = loop2.run_once()
        assert out2[0].action == "rejected", out2[0]
    finally:
        os.environ.pop("FAKE_GATE_RC", None)
        os.environ.pop("FAKE_GATE_SLUG", None)

    rej = [e for e in _ledger_events(loops_root)
           if e["event"] == "rejected" and e["id"] == id_d]
    assert rej, "candidate-defect must write a rejected event"
    assert rej[0]["detail"]["class"] == "candidate-defect"
    assert rej[0]["detail"]["expires_at"], "a rejection without a TTL is a permanent ban"
    assert (loops_root / "rejected" / f"sha256_{'e' * 64}.json").exists()
    # nothing landed
    assert _git(repo, "rev-parse", "main") == m0


# ================================================ MEMBER-AWARE FAILURE ISOLATION
# A candidate-defect verdict on a MULTI-member train is not member-specific
# evidence. Instead of terminalizing every member (the measured 15-bans-from-5-
# verdicts blast radius), the train is stamped isolation-pending and its members
# re-gate SOLO so a terminal claim can only ever be candidate-specific. These
# four tests are written RED-FIRST against the pre-fix mass-rejection behaviour.


def _write_isolation_state(loops_root: Path, member_ids: list[str], base: str,
                           *, filename: str, members_override: object = None,
                           source_override: object = None,
                           with_ledger: bool = True) -> Path:
    """Write an `iso-<...>` carrier in the isolation-pending shape a prior tick
    would have left behind, PLUS (by default) the durable `isolated` ledger events
    that production always writes alongside it — a carrier with no ledger at all is
    the data-loss case a test must opt into with `with_ledger=False`.
    `members_override`/`source_override` let a test forge a MALFORMED shape."""
    gdir = loops_root / "state" / "gates"
    gdir.mkdir(parents=True, exist_ok=True)
    members = sorted(member_ids) if members_override is None else members_override
    source = ({"train": "train/composite", "base": base, "tip": "f" * 40}
              if source_override is None else source_override)
    # Carriers are namespaced `iso-*` so they can never be counted as a lease.
    name = filename if filename.startswith("iso-") else f"iso-{filename}"
    sf = gdir / name
    sf.write_text(json.dumps({
        "state": "closed", "disposition": "isolation-pending",
        "isolation_members": members, "isolation_source": source,
        "classifier": "secrets", "receipt": "",
        "isolated_at": "2026-08-11T00:00:00Z", "closed_at": "2026-08-11T00:00:00Z",
    }), encoding="utf-8")
    if with_ledger:
        loops_root.mkdir(parents=True, exist_ok=True)
        group = members if isinstance(members, list) else []
        with open(loops_root / "ledger.jsonl", "a", encoding="utf-8") as fh:
            for mid in group:
                # each event carries the COMPLETE group, mirroring _isolate_train
                fh.write(json.dumps({
                    "ts": "2026-08-11T00:00:00Z", "role": "implementer",
                    "event": "isolated", "id": mid, "actor": "gate-loop-daemon",
                    "detail": {"kind": "isolation", "train": "train/composite",
                               "isolation_members": group}}) + "\n")
    return sf


def test_multi_defect_enters_isolation_not_rejection(tmp_path):
    """A 2-member red train writes ZERO rejected events and an isolation-pending
    state carrying BOTH exact member IDs — not two tombstones."""
    repo = _init_repo(tmp_path / "repo")
    m0 = _git(repo, "rev-parse", "HEAD")
    _commit_on(repo, "cand-x", m0, "x.txt", "XXX\n")
    _commit_on(repo, "cand-y", m0, "y.txt", "YYY\n")
    loops_root = tmp_path / "loops"
    id_x = _write_candidate(loops_root, "a1" * 32, "cand-x", m0, ["x.txt"])
    id_y = _write_candidate(loops_root, "b2" * 32, "cand-y", m0, ["y.txt"])

    offload = _fake_offload(tmp_path)
    os.environ["FAKE_GATE_RC"] = "2"
    os.environ["FAKE_GATE_SLUG"] = "secrets"          # a real candidate-defect slug
    try:
        loop = _make_loop(loops_root, repo, offload)
        out1 = loop.run_once()
        train = out1[0].train
        assert len(train.members) == 2, train.members
        _wait_done(gate_state_path(loops_root, train))
        loop2 = _make_loop(loops_root, repo, offload)
        out2 = loop2.run_once()
        assert out2[0].action == "isolated", out2[0]
    finally:
        os.environ.pop("FAKE_GATE_RC", None)
        os.environ.pop("FAKE_GATE_SLUG", None)

    events = _ledger_events(loops_root)
    assert not any(e["event"] == "rejected" for e in events), \
        "a multi-member red train must NOT mass-reject its members"
    rej_dir = loops_root / "rejected"
    assert not rej_dir.exists() or not list(rej_dir.glob("*.json")), \
        "no tombstone may be written for a composite verdict"
    # the namespaced iso- carrier holds the audit mirror
    sf = iso_state_path(loops_root, train)
    st = json.loads(sf.read_text())
    assert st["state"] == "closed" and st["disposition"] == "isolation-pending"
    assert st["isolation_members"] == sorted([id_x, id_y]), st
    src = st["isolation_source"]
    assert len(src["base"]) == 40 and len(src["tip"]) == 40 and src["train"], src
    # the DURABLE ledger backstop recorded one non-terminal `isolated` event/member
    iso_ev = {e["id"] for e in events if e["event"] == "isolated"}
    assert iso_ev == {id_x, id_y}, iso_ev
    # members remain non-terminal — re-gate, never a ban (isolated is NOT terminal)
    assert id_x not in {e["id"] for e in events if e["event"] in ("merged", "rejected")}
    assert id_y not in {e["id"] for e in events if e["event"] in ("merged", "rejected")}


def test_isolation_survives_restart_and_forces_solo(tmp_path):
    """A FRESH GateLoop reads the persisted isolation state and forces both
    members into ONE-member trains — never re-batches the disjoint pair."""
    repo = _init_repo(tmp_path / "repo")
    m0 = _git(repo, "rev-parse", "HEAD")
    _commit_on(repo, "cand-x", m0, "x.txt", "XXX\n")
    _commit_on(repo, "cand-y", m0, "y.txt", "YYY\n")
    loops_root = tmp_path / "loops"
    id_x = _write_candidate(loops_root, "a1" * 32, "cand-x", m0, ["x.txt"])
    id_y = _write_candidate(loops_root, "b2" * 32, "cand-y", m0, ["y.txt"])
    _write_isolation_state(loops_root, [id_x, id_y], m0,
                           filename="comp@" + "f" * 40 + ".json")

    offload = _fake_offload(tmp_path)
    os.environ["FAKE_GATE_RC"] = "0"
    os.environ.pop("FAKE_GATE_SLUG", None)
    try:
        loop = _make_loop(loops_root, repo, offload)  # a brand-new process
        out = loop.run_once()
    finally:
        os.environ.pop("FAKE_GATE_RC", None)

    # every assembled train is one-member, and BOTH isolated IDs were assembled
    seen: dict[str, str] = {}
    for o in out:
        if o.train and o.train.members:
            assert len(o.train.members) == 1, \
                ("an isolated member must gate solo", o.train.members)
            for m in o.train.members:
                seen[m["id"]] = o.action
    assert {id_x, id_y} <= set(seen), (seen, [o.action for o in out])


def test_malformed_isolation_fails_closed(tmp_path):
    """A broken isolation record (no member IDs) is a fail-closed tick veto: no
    dispatch, no rejection, a degraded tick, and exactly ONE alert — never an
    empty set that would let the protected members re-batch."""
    repo = _init_repo(tmp_path / "repo")
    m0 = _git(repo, "rev-parse", "HEAD")
    _commit_on(repo, "cand-x", m0, "x.txt", "XXX\n")
    loops_root = tmp_path / "loops"
    _write_candidate(loops_root, "a1" * 32, "cand-x", m0, ["x.txt"])
    _write_isolation_state(loops_root, [], m0,
                           filename="z@" + "f" * 40 + ".json",
                           members_override=[])       # the malformed shape

    offload = _fake_offload(tmp_path)
    os.environ["FAKE_GATE_RC"] = "0"
    os.environ.pop("FAKE_GATE_SLUG", None)
    try:
        loop = _make_loop(loops_root, repo, offload)
        out = loop.run_once()
    finally:
        os.environ.pop("FAKE_GATE_RC", None)

    assert all(o.action != "dispatched" for o in out), \
        "a malformed isolation must dispatch nothing this tick"
    assert not any(e["event"] == "rejected" for e in _ledger_events(loops_root))
    assert loop._tick_degraded_reason, "malformed isolation must degrade the tick"
    assert len(loop.alerts) == 1, loop.alerts


def test_isolated_solo_failure_rejects_only_one(tmp_path):
    """A solo re-gate that goes RED terminalizes ONLY that member; the sibling
    stays live (the one-member evidence boundary)."""
    repo = _init_repo(tmp_path / "repo")
    m0 = _git(repo, "rev-parse", "HEAD")
    _commit_on(repo, "cand-x", m0, "x.txt", "XXX\n")
    _commit_on(repo, "cand-y", m0, "y.txt", "YYY\n")
    loops_root = tmp_path / "loops"
    id_x = _write_candidate(loops_root, "a1" * 32, "cand-x", m0, ["x.txt"])
    id_y = _write_candidate(loops_root, "b2" * 32, "cand-y", m0, ["y.txt"])
    _write_isolation_state(loops_root, [id_x, id_y], m0,
                           filename="comp@" + "f" * 40 + ".json")

    offload = _fake_offload(tmp_path)
    os.environ["FAKE_GATE_RC"] = "2"
    os.environ["FAKE_GATE_SLUG"] = "secrets"
    try:
        loop = _make_loop(loops_root, repo, offload)
        out1 = loop.run_once()
        solo = [o.train for o in out1 if o.action == "dispatched"]
        assert len(solo) == 1 and len(solo[0].members) == 1, out1
        culprit = solo[0].members[0]["id"]
        _wait_done(gate_state_path(loops_root, solo[0]))
        loop2 = _make_loop(loops_root, repo, offload)
        out2 = loop2.run_once()
        assert any(o.action == "rejected" for o in out2), out2
    finally:
        os.environ.pop("FAKE_GATE_RC", None)
        os.environ.pop("FAKE_GATE_SLUG", None)

    rej = [e for e in _ledger_events(loops_root) if e["event"] == "rejected"]
    assert {e["id"] for e in rej} == {culprit}, "ONLY the solo-red member is rejected"
    other = ({id_x, id_y} - {culprit}).pop()
    assert (loops_root / "rejected" / f"{gl._stem(culprit)}.json").exists()
    assert not (loops_root / "rejected" / f"{gl._stem(other)}.json").exists()


def test_isolated_solo_pass_keeps_sibling_live(tmp_path):
    """A solo re-gate that PASSES lands that member on the existing serial path;
    the sibling remains non-terminal until its own verdict."""
    repo = _init_repo(tmp_path / "repo")
    m0 = _git(repo, "rev-parse", "HEAD")
    _commit_on(repo, "cand-x", m0, "x.txt", "XXX\n")
    _commit_on(repo, "cand-y", m0, "y.txt", "YYY\n")
    loops_root = tmp_path / "loops"
    id_x = _write_candidate(loops_root, "a1" * 32, "cand-x", m0, ["x.txt"])
    id_y = _write_candidate(loops_root, "b2" * 32, "cand-y", m0, ["y.txt"])
    _write_isolation_state(loops_root, [id_x, id_y], m0,
                           filename="comp@" + "f" * 40 + ".json")

    offload = _fake_offload(tmp_path)
    os.environ["FAKE_GATE_RC"] = "0"
    os.environ.pop("FAKE_GATE_SLUG", None)
    try:
        loop = _make_loop(loops_root, repo, offload)
        out1 = loop.run_once()
        solo = [o.train for o in out1 if o.action == "dispatched"]
        assert len(solo) == 1 and len(solo[0].members) == 1, out1
        lander = solo[0].members[0]["id"]
        _wait_done(gate_state_path(loops_root, solo[0]))
        loop2 = _make_loop(loops_root, repo, offload)
        out2 = loop2.run_once()
        assert any(o.action == "landed" for o in out2), out2
    finally:
        os.environ.pop("FAKE_GATE_RC", None)

    events = _ledger_events(loops_root)
    merged = {e["id"] for e in events if e["event"] == "merged"}
    assert lander in merged, merged
    other = ({id_x, id_y} - {lander}).pop()
    assert other not in {e["id"] for e in events if e["event"] in ("merged", "rejected")}, \
        "the sibling must stay live until its own solo verdict"


# --- CROSS-LINEAGE ROUND-1 FINDINGS (F1 BLOCKER, F2/F3 MAJOR) ------------------
# Each is written RED-FIRST against the first-round isolation implementation.


def test_unreadable_isolation_carrier_survives_on_the_ledger(tmp_path):
    """F1 (round 3): an UNREADABLE `iso-` carrier no longer aborts the tick — the
    durable ledger backstop still forces its members solo, and the file is
    short-quarantined rather than wedging every landing for the 2h gate deadline."""
    repo = _init_repo(tmp_path / "repo")
    m0 = _git(repo, "rev-parse", "HEAD")
    _commit_on(repo, "cand-x", m0, "x.txt", "XXX\n")
    _commit_on(repo, "cand-y", m0, "y.txt", "YYY\n")
    loops_root = tmp_path / "loops"
    id_x = _write_candidate(loops_root, "a1" * 32, "cand-x", m0, ["x.txt"])
    id_y = _write_candidate(loops_root, "b2" * 32, "cand-y", m0, ["y.txt"])
    # a real two-carrier isolation, THEN the readable mirror is corrupted
    sf = _write_isolation_state(loops_root, [id_x, id_y], m0, with_ledger=True,
                                filename="comp@" + "f" * 40 + ".json")
    sf.write_text("{ truncated mid-json — never valid", encoding="utf-8")

    offload = _fake_offload(tmp_path)
    loop = _make_loop(loops_root, repo, offload)
    # NO whole-tick veto: membership comes from the durable ledger backstop
    assert loop._pending_isolation_ids({id_x, id_y}) == {id_x, id_y}
    # and no member is ever terminalised off an unreadable carrier
    assert not any(e["event"] == "rejected" for e in _ledger_events(loops_root))


# --- CROSS-LINEAGE ROUND-1 FINDINGS (F1 BLOCKER, F2/F3 MAJOR) ------------------
# Each is written RED-FIRST against the first-round isolation implementation.


def test_two_distinct_malformed_records_each_alert(tmp_path):
    """F2: a second, independently-malformed record must raise its OWN alert even
    after the first is fixed — the alert dedup is keyed per file, not global."""
    repo = _init_repo(tmp_path / "repo")
    m0 = _git(repo, "rev-parse", "HEAD")
    _commit_on(repo, "cand-x", m0, "x.txt", "XXX\n")
    loops_root = tmp_path / "loops"
    _write_candidate(loops_root, "a1" * 32, "cand-x", m0, ["x.txt"])
    bad1 = _write_isolation_state(loops_root, [], m0, members_override=[],
                                  filename="a-bad@" + "f" * 40 + ".json")
    _write_isolation_state(loops_root, [], m0, members_override=[],
                           filename="z-bad@" + "f" * 40 + ".json")

    offload = _fake_offload(tmp_path)
    os.environ["FAKE_GATE_RC"] = "0"
    try:
        loop = _make_loop(loops_root, repo, offload)
        loop.run_once()
        assert len(loop.alerts) == 1, ("the first malformed record alerts", loop.alerts)
        loop.alerts.clear()
        bad1.unlink()                                 # operator fixes the first
        loop.run_once()                               # the SECOND now surfaces
        assert len(loop.alerts) == 1, \
            ("a newly-surfaced malformed record must raise its own alert", loop.alerts)
    finally:
        os.environ.pop("FAKE_GATE_RC", None)


def test_isolation_record_goes_dormant_after_members_resolve(tmp_path):
    """F3 (round-4 reconciled): once every member is terminal/absent, the CARRIER
    is stamped dormant (audit preserved). Membership itself is governed by the
    DURABLE ledger filtered by liveness, not by carrier disposition — so a member
    that is terminal/absent drops out, while a re-activated member is still forced
    solo by the ledger backstop (which errs toward solo — safe)."""
    repo = _init_repo(tmp_path / "repo")
    m0 = _git(repo, "rev-parse", "HEAD")
    _commit_on(repo, "cand-x", m0, "x.txt", "XXX\n")
    loops_root = tmp_path / "loops"
    id_x = _write_candidate(loops_root, "a1" * 32, "cand-x", m0, ["x.txt"])
    # a REAL two-carrier isolation (ledger + carrier), as production writes it
    sf = _write_isolation_state(loops_root, [id_x], m0, with_ledger=True,
                                filename="comp@" + "f" * 40 + ".json")

    offload = _fake_offload(tmp_path)
    loop = _make_loop(loops_root, repo, offload)
    # while the member is ACTIVE it is forced solo and the carrier stays pending
    assert loop._pending_isolation_ids({id_x}) == {id_x}
    assert json.loads(sf.read_text())["disposition"] == "isolation-pending"
    # once the member is terminal/absent, the carrier retires (audit kept) and the
    # ledger backstop no longer forces it (filtered out by liveness)
    assert loop._pending_isolation_ids(set()) == set()
    retired = json.loads(sf.read_text())
    assert retired["disposition"] == "isolation-resolved", retired
    assert retired["isolation_members"] == [id_x], "audit content must be preserved"
    assert retired["isolation_source"]["base"] == m0
    # reconciliation: a re-activated member is STILL forced solo via the durable
    # ledger backstop even though the carrier is dormant — errs toward solo (safe)
    assert loop._pending_isolation_ids({id_x}) == {id_x}


@pytest.mark.parametrize("bad_field", ["base", "tip"])
def test_non_hex_source_sha_fails_closed(tmp_path, bad_field):
    """FISO-002: a 40-CHARACTER but NON-hex base/tip is a corrupt source and must
    veto — a length-only check (len == 40) wrongly accepts a value like 'z'*40."""
    repo = _init_repo(tmp_path / "repo")
    m0 = _git(repo, "rev-parse", "HEAD")
    _commit_on(repo, "cand-x", m0, "x.txt", "XXX\n")
    loops_root = tmp_path / "loops"
    id_x = _write_candidate(loops_root, "a1" * 32, "cand-x", m0, ["x.txt"])
    source = {"train": "train/comp", "base": m0, "tip": "f" * 40}
    source[bad_field] = "z" * 40                       # 40 chars, NOT hex
    _write_isolation_state(loops_root, [id_x], m0,
                           filename="comp@" + "f" * 40 + ".json",
                           source_override=source)

    offload = _fake_offload(tmp_path)
    loop = _make_loop(loops_root, repo, offload)
    with pytest.raises(gl.MalformedIsolation):
        loop._pending_isolation_ids({id_x})

    os.environ["FAKE_GATE_RC"] = "0"
    try:
        dispatched: list = []
        loop.dispatch_gate = lambda train, *, allow_remote, twin=None: dispatched.append(train)  # type: ignore[assignment]
        out = loop.run_once()
    finally:
        os.environ.pop("FAKE_GATE_RC", None)
    assert dispatched == [], "a non-hex source sha must dispatch nothing"
    assert all(o.action != "dispatched" for o in out), out
    assert loop._tick_degraded_reason, "a corrupt source sha must degrade the tick"
    assert not any(e["event"] == "rejected" for e in _ledger_events(loops_root))


# --- CROSS-LINEAGE ROUND-2 FINDINGS (durability: ledger backstop + namespacing) --
# The doom-loop only dies if isolation membership cannot evaporate with a fragile
# file. Each is written RED-FIRST against the round-2 (single-file-carrier) code.


def test_ledger_backstop_survives_carrier_deletion(tmp_path):
    """Round-2 F2: after a real isolation, DELETE the carrier file — the durable
    ledger backstop must still force BOTH members solo, not re-batch them."""
    repo = _init_repo(tmp_path / "repo")
    m0 = _git(repo, "rev-parse", "HEAD")
    _commit_on(repo, "cand-x", m0, "x.txt", "XXX\n")
    _commit_on(repo, "cand-y", m0, "y.txt", "YYY\n")
    loops_root = tmp_path / "loops"
    id_x = _write_candidate(loops_root, "a1" * 32, "cand-x", m0, ["x.txt"])
    id_y = _write_candidate(loops_root, "b2" * 32, "cand-y", m0, ["y.txt"])

    offload = _fake_offload(tmp_path)
    os.environ["FAKE_GATE_RC"] = "2"
    os.environ["FAKE_GATE_SLUG"] = "secrets"
    try:
        loop = _make_loop(loops_root, repo, offload)
        train = loop.run_once()[0].train
        assert len(train.members) == 2, train.members
        _wait_done(gate_state_path(loops_root, train))
        loop2 = _make_loop(loops_root, repo, offload)
        assert loop2.run_once()[0].action == "isolated"
    finally:
        os.environ.pop("FAKE_GATE_RC", None)
        os.environ.pop("FAKE_GATE_SLUG", None)

    carrier = iso_state_path(loops_root, train)
    assert carrier.exists(), "isolation must have written the audit carrier"
    carrier.unlink()                                   # the fragile file is gone
    loop3 = _make_loop(loops_root, repo, offload)
    assert loop3._pending_isolation_ids({id_x, id_y}) == {id_x, id_y}, \
        "the ledger backstop must force both members solo with the carrier deleted"


def test_unreadable_iso_does_not_stall_tick_and_short_quarantines(tmp_path):
    """Round-2 F1: an unreadable `iso-` carrier does NOT abort the tick (a disjoint
    landing proceeds the same tick) and quarantines on the SHORT bound, not 2h."""
    repo = _init_repo(tmp_path / "repo")
    m0 = _git(repo, "rev-parse", "HEAD")
    _commit_on(repo, "cand-z", m0, "z.txt", "ZZZ\n")
    loops_root = tmp_path / "loops"
    id_z = _write_candidate(loops_root, "c3" * 32, "cand-z", m0, ["z.txt"])
    gdir = loops_root / "state" / "gates"
    gdir.mkdir(parents=True, exist_ok=True)
    isof = gdir / ("iso-train__old@" + "f" * 40 + ".json")
    isof.write_text("{ broken iso carrier", encoding="utf-8")
    # PRODUCTION always writes the ledger backstop alongside the carrier, so the
    # ledger is present and TRUSTWORTHY — the unreadable carrier may soften.
    id_old = "sha256:" + "d4" * 32
    _append_line(loops_root, {"ts": "t0", "role": "implementer", "event": "isolated",
                              "id": id_old, "detail": {"isolation_members": [id_old]}})

    offload = _fake_offload(tmp_path)
    os.environ["FAKE_GATE_RC"] = "0"
    try:
        loop = _make_loop(loops_root, repo, offload)
        out = loop.run_once()
    finally:
        os.environ.pop("FAKE_GATE_RC", None)
    # NO global stall: the disjoint candidate dispatches the same tick
    assert any(o.action == "dispatched" for o in out), out
    assert id_z in {m["id"] for o in out if o.train for m in o.train.members}
    # within the short bound the file is left in place (transient repair window)
    assert isof.exists()

    # past the SHORT bound it is quarantined (renamed out of the scan), NOT held
    # for the 2h gate deadline
    marker = loop._alert_marker_path(f"gate-veto:{isof}")
    ms = json.loads(marker.read_text())
    ms["veto_started_at"] = time.time() - (gl.ISO_UNREADABLE_QUARANTINE_S + 5)
    marker.write_text(json.dumps(ms), encoding="utf-8")
    loop._pending_isolation_ids(set())
    assert not isof.exists(), "an unreadable iso- carrier must short-quarantine"
    assert list(gdir.glob("iso-train__old@*.corrupt-*")), "quarantined, not deleted"


def test_read_gate_leases_ignores_iso_carriers(tmp_path):
    """Round-2 F3: neither a valid nor an UNREADABLE `iso-` carrier may count as a
    running gate — that mis-count is what wedged dispatch for 2h."""
    repo = _init_repo(tmp_path / "repo")
    loops_root = tmp_path / "loops"
    (loops_root / "state").mkdir(parents=True, exist_ok=True)
    loop = _make_loop(loops_root, repo, _fake_offload(tmp_path))
    gdir = loops_root / "state" / "gates"
    gdir.mkdir(parents=True, exist_ok=True)
    (gdir / ("iso-train__a@" + "f" * 40 + ".json")).write_text(json.dumps({
        "state": "closed", "disposition": "isolation-pending"}), encoding="utf-8")
    (gdir / ("iso-train__b@" + "e" * 40 + ".json")).write_text("{ broken", encoding="utf-8")

    leases = loop._read_gate_leases()
    assert leases.running == 0, "iso- carriers must not count as running gates"
    assert leases.corrupt is False, "an unreadable iso- carrier must not inflate occupancy"


def test_isolated_event_is_schema_valid_and_non_terminal(tmp_path):
    """`isolated` is a first-class NON-terminal enum event: it validates against
    the ledger schema, is NOT in the terminal set, and a submitted->gated->
    isolated->merged member has EXACTLY ONE terminal event and correct replay."""
    import jsonschema

    schema = json.loads((ROOT / "schema" / "ledger-event.schema.json").read_text())
    assert "isolated" in schema["properties"]["event"]["enum"], \
        "isolated must be a first-class schema enum value"

    repo = _init_repo(tmp_path / "repo")
    loops_root = tmp_path / "loops"
    (loops_root / "state").mkdir(parents=True, exist_ok=True)
    loop = _make_loop(loops_root, repo, _fake_offload(tmp_path))
    mid = "sha256:" + "a1" * 32

    def _append(ev: dict) -> None:
        with open(loops_root / "ledger.jsonl", "a", encoding="utf-8") as fh:
            fh.write(json.dumps(ev) + "\n")

    # a real-shaped isolated event VALIDATES against the schema (base fields only)
    iso_ev = {"ts": "2026-08-11T00:00:00Z", "role": "implementer",
              "event": "isolated", "id": mid, "actor": "gate-loop-daemon",
              "detail": {"kind": "isolation", "isolation_members": [mid]}}
    jsonschema.validate(iso_ev, schema)                # raises on any violation

    # full lifecycle: submitted -> gated -> isolated -> merged
    _append({"ts": "t0", "role": "implementer", "event": "submitted", "id": mid,
             "actor": "impl@x"})
    _append({"ts": "t1", "role": "implementer", "event": "gated", "id": mid,
             "actor": gl.ACTOR})
    _append(iso_ev)
    assert mid not in loop._terminal_ids(), "isolated must NOT be terminal"
    assert loop._ledger_isolated_active({mid}) == {mid}

    _append({"ts": "t2", "role": "implementer", "event": "merged", "id": mid,
             "actor": gl.ACTOR, "detail": {"merge_sha": "deadbeef"}})
    assert mid in loop._terminal_ids()
    terminal = [e for e in _ledger_events(loops_root)
                if e["event"] in ("merged", "completed", "rejected", "closed")
                and e["id"] == mid]
    assert len(terminal) == 1, "isolated must not double-count as a terminal event"
    # a terminal member is no longer active, so it no longer forces a solo
    assert loop._ledger_isolated_active(set()) == set()


# --- CROSS-LINEAGE ROUND-3 FINDINGS (fail-closed the ledger-read/durability path) --
# Both lenses: the ledger backstop must be TRUSTWORTHY to justify softening the
# carrier veto. Any undecodable/uncertain durable evidence VETOES, never empty.


def _append_line(loops_root: Path, obj) -> None:
    loops_root.mkdir(parents=True, exist_ok=True)
    with open(loops_root / "ledger.jsonl", "a", encoding="utf-8") as fh:
        fh.write((obj if isinstance(obj, str) else json.dumps(obj)) + "\n")


# --- CROSS-LINEAGE ROUND-4 FINDING (availability: skip a bad line, never HALT) --
# The ledger is append-only and never rewritten, so raising on ONE torn line or
# malformed event would re-veto every tick FOREVER — a permanent total-dispatch
# halt from a benign crash artifact, strictly worse than the TTL-recoverable
# re-shred it guards against. Skip the bad record (edge-alert once) and build
# membership from the good ones; VETO only when there is NO usable source at all.


def test_torn_non_tail_ledger_line_is_skipped_not_halted(tmp_path):
    """ROUND-5: a torn NON-final ledger line is SKIPPED (edge-alerted once);
    membership is built from the decodable events and landings PROCEED."""
    repo = _init_repo(tmp_path / "repo")
    loops_root = tmp_path / "loops"
    (loops_root / "state").mkdir(parents=True, exist_ok=True)
    id_x = "sha256:" + "a1" * 32
    id_y = "sha256:" + "b2" * 32
    # a WELL-FORMED isolated event, then a torn NON-final line, then a valid tail
    _append_line(loops_root, {"ts": "t0", "role": "implementer", "event": "isolated",
                              "id": id_x, "detail": {"isolation_members": [id_x]}})
    _append_line(loops_root, '{"ts": "t1", "event": "isolated", "id": "sha256:')  # torn
    _append_line(loops_root, {"ts": "t2", "role": "implementer", "event": "gated",
                              "id": id_y})                             # final, valid

    loop = _make_loop(loops_root, repo, _fake_offload(tmp_path))
    # the decodable isolated event is still honoured; the torn line does NOT halt
    assert loop._ledger_isolated_active({id_x, id_y}) == {id_x}
    assert len(loop.alerts) == 1, ("exactly one skip alert", loop.alerts)
    # EDGE-TRIGGERED: a second read of the same persistent torn line does NOT re-alert
    loop.alerts.clear()
    assert loop._ledger_isolated_active({id_x, id_y}) == {id_x}
    assert loop.alerts == [], ("a persistent torn line must not re-alert", loop.alerts)


def test_malformed_isolated_event_is_skipped_others_enforced(tmp_path):
    """ROUND-5: a malformed `isolated` event is SKIPPED (edge-alerted once); the
    OTHER, well-formed isolations are still enforced. The gate still runs for the
    skipped record's members, so no bad code can merge."""
    repo = _init_repo(tmp_path / "repo")
    loops_root = tmp_path / "loops"
    (loops_root / "state").mkdir(parents=True, exist_ok=True)
    id_bad = "sha256:" + "a1" * 32
    id_good = "sha256:" + "b2" * 32
    _append_line(loops_root, {"ts": "t0", "role": "implementer", "event": "isolated",
                              "id": id_bad, "detail": {"isolation_members": "not-a-list"}})
    _append_line(loops_root, {"ts": "t1", "role": "implementer", "event": "isolated",
                              "id": id_good, "detail": {"isolation_members": [id_good]}})

    loop = _make_loop(loops_root, repo, _fake_offload(tmp_path))
    # the well-formed isolation is enforced; the malformed one is skipped, not fatal
    assert loop._ledger_isolated_active({id_bad, id_good}) == {id_good}
    assert len(loop.alerts) == 1, ("exactly one skip alert", loop.alerts)


def test_ledger_oserror_with_no_carrier_vetoes_but_carrier_readable_uses_it(tmp_path):
    """ROUND-5: VETO only when there is NO usable isolation source. Entire ledger
    unreadable (OSError) AND no carrier -> veto. Ledger unreadable but a readable
    carrier exists -> use the carrier, no veto."""
    repo = _init_repo(tmp_path / "repo")
    m0 = _git(repo, "rev-parse", "HEAD")
    id_x = "sha256:" + "a1" * 32

    # Case 1: OSError ledger + NO carrier -> hard veto (no usable source at all).
    root1 = tmp_path / "loops1"
    (root1 / "state" / "gates").mkdir(parents=True, exist_ok=True)
    (root1 / "ledger.jsonl").mkdir()          # a DIRECTORY at the ledger path -> OSError on read
    loop1 = _make_loop(root1, repo, _fake_offload(tmp_path))
    with pytest.raises(gl.LedgerUnreadable):
        loop1._ledger_isolated_active({id_x})
    with pytest.raises(gl.IsolationVeto):
        loop1._pending_isolation_ids({id_x})

    # Case 2: OSError ledger + a READABLE carrier -> use the carrier, no veto.
    root2 = tmp_path / "loops2"
    _write_isolation_state(root2, [id_x], m0, with_ledger=False,
                           filename="comp@" + "f" * 40 + ".json")
    (root2 / "ledger.jsonl").mkdir()          # OSError on read
    loop2 = _make_loop(root2, repo, _fake_offload(tmp_path))
    assert loop2._pending_isolation_ids({id_x}) == {id_x}   # carrier is the source


def test_genuinely_fresh_system_has_no_isolation(tmp_path):
    """ROUND-5 regression: no ledger AND no carriers is a fresh system, not data
    loss — it must return empty, never wedge."""
    repo = _init_repo(tmp_path / "repo")
    (tmp_path / "loops" / "state").mkdir(parents=True, exist_ok=True)
    loop = _make_loop(tmp_path / "loops", repo, _fake_offload(tmp_path))
    assert loop._ledger_isolated_active({"sha256:" + "a1" * 32}) == set()
    assert loop._pending_isolation_ids({"sha256:" + "a1" * 32}) == set()


def test_ledger_append_failure_is_not_durable_isolation(tmp_path, monkeypatch):
    """BLOCKER B: a FAILED ledger append + a successful carrier write must NOT be
    reported as durable `isolated` — degrade and retry, and no fragile carrier is
    left standing. The members are recoverable on a subsequent healthy tick."""
    repo = _init_repo(tmp_path / "repo")
    m0 = _git(repo, "rev-parse", "HEAD")
    _commit_on(repo, "cand-x", m0, "x.txt", "XXX\n")
    _commit_on(repo, "cand-y", m0, "y.txt", "YYY\n")
    loops_root = tmp_path / "loops"
    _write_candidate(loops_root, "a1" * 32, "cand-x", m0, ["x.txt"])
    _write_candidate(loops_root, "b2" * 32, "cand-y", m0, ["y.txt"])

    offload = _fake_offload(tmp_path)
    os.environ["FAKE_GATE_RC"] = "2"
    os.environ["FAKE_GATE_SLUG"] = "secrets"
    try:
        loop = _make_loop(loops_root, repo, offload)
        train = loop.run_once()[0].train
        assert len(train.members) == 2
        _wait_done(gate_state_path(loops_root, train))

        loop2 = _make_loop(loops_root, repo, offload)
        real_append = loop2._append_ledger

        def failing_append(ev):
            if ev.get("event") == "isolated":
                raise OSError("simulated ledger lock contention")
            return real_append(ev)

        monkeypatch.setattr(loop2, "_append_ledger", failing_append)
        out2 = loop2.run_once()
        iso = [o for o in out2 if o.train and o.train.branch == train.branch]
        assert iso and iso[0].action == "degraded", out2
        # NO fragile carrier written, NO durable event, and NOT a rejection
        assert not iso_state_path(loops_root, train).exists(), \
            "no carrier may stand in for a missing durable backstop"
        assert not any(e["event"] == "isolated" for e in _ledger_events(loops_root))
        assert not any(e["event"] == "rejected" for e in _ledger_events(loops_root))

        # RECOVERABLE: a subsequent healthy tick isolates durably
        loop3 = _make_loop(loops_root, repo, offload)
        out3 = loop3.run_once()
        iso3 = [o for o in out3 if o.train and o.train.branch == train.branch]
        assert iso3 and iso3[0].action == "isolated", out3
        assert any(e["event"] == "isolated" for e in _ledger_events(loops_root))
    finally:
        os.environ.pop("FAKE_GATE_RC", None)
        os.environ.pop("FAKE_GATE_SLUG", None)


# ================================================================= REVIEW FIXES
# Regression tests for the 4 cross-lineage-review (Gemini) findings. Each is
# written to FAIL against the pre-fix code.


# --- FINDING 1: pass-without-receipt must be instrument-error, never a land ----


def test_pass_without_receipt_is_instrument_error_not_land(tmp_path):
    # Direct: rc==0 but the receipt file does not exist -> instrument-error.
    # Pre-fix this returned classify_gate's unconditional rc==0 "pass".
    sf = tmp_path / "gate.json"
    sf.write_text(json.dumps({
        "state": "done", "rc": 0, "stdout": "gate passed\n", "stderr": "",
        "receipt": str(tmp_path / "no-such-receipt.json"), "duration_s": 1.0}),
        encoding="utf-8")
    v = read_gate_verdict(sf)
    assert v is not None and v.result == "instrument-error", v
    assert v.result != "pass"

    # receipt None is equally unverifiable
    sf.write_text(json.dumps({"state": "done", "rc": 0, "stdout": "gate passed\n",
                              "stderr": "", "receipt": None, "duration_s": 1.0}),
                  encoding="utf-8")
    assert read_gate_verdict(sf).result == "instrument-error"


def test_gate_exit0_no_receipt_does_not_land_main(tmp_path):
    repo = _init_repo(tmp_path / "repo")
    m0 = _git(repo, "rev-parse", "HEAD")
    _commit_on(repo, "cand-p", m0, "p.txt", "PPP\n")
    loops_root = tmp_path / "loops"
    id_p = _write_candidate(loops_root, "f" * 64, "cand-p", m0, ["p.txt"])

    offload = _fake_offload(tmp_path)
    os.environ["FAKE_GATE_RC"] = "0"                 # gate "passes"...
    os.environ["FAKE_GATE_NO_RECEIPT"] = "1"         # ...but writes NO receipt
    os.environ.pop("FAKE_GATE_SLUG", None)
    try:
        loop = _make_loop(loops_root, repo, offload)
        train = loop.run_once()[0].train
        _wait_done(gate_state_path(loops_root, train))
        loop2 = _make_loop(loops_root, repo, offload)
        out2 = loop2.run_once()
    finally:
        os.environ.pop("FAKE_GATE_RC", None)
        os.environ.pop("FAKE_GATE_NO_RECEIPT", None)

    assert out2[0].action == "instrument", out2[0]
    assert _git(repo, "rev-parse", "main") == m0, "UNGATED code must NOT land"
    events = _ledger_events(loops_root)
    assert not any(e["event"] == "merged" for e in events)
    assert id_p not in {e["id"] for e in events if e["event"] in ("merged", "rejected")}


# --- FINDING 2: deterministic train tip even with commit.gpgsign=true ----------


def test_train_tip_deterministic_under_gpgsign(tmp_path):
    repo = _init_repo(tmp_path / "repo")
    m0 = _git(repo, "rev-parse", "HEAD")
    _commit_on(repo, "cand-g", m0, "g.txt", "GGG\n")
    # A host that force-signs every commit: without --no-gpg-sign the cherry-pick
    # embeds a fresh signature nonce (non-deterministic tip) or fails outright.
    _git(repo, "config", "commit.gpgsign", "true")
    _git(repo, "config", "user.signingkey", "DOES-NOT-EXIST")

    cands = [_cand("1" * 64, "cand-g", m0)]
    trains1, _ = _assemble(repo, [_cand("1" * 64, "cand-g", m0)], m0, tmp_path)
    trains2, _ = _assemble(repo, [_cand("1" * 64, "cand-g", m0)], m0, tmp_path)
    assert len(trains1) == 1 and len(trains2) == 1, (trains1, trains2)
    assert trains1[0].tip == trains2[0].tip, "train tip must be identical across ticks"
    # and the member actually made it in (proves the cherry-pick did not fail)
    assert {m["id"] for m in trains1[0].members} == {c.ident for c in cands}


# --- FINDING 3: a CHAIN of trains, every train after the first on a twin -------


def test_two_trains_second_goes_remote(tmp_path, monkeypatch):
    repo = _init_repo(tmp_path / "repo")
    m0 = _git(repo, "rev-parse", "HEAD")
    loops_root = tmp_path / "loops"
    # Enough disjoint candidates to fill EVERY box: trains cap at 10 members, so
    # this must exceed 10 * MAX_CONCURRENT_GATES for the last slot to be exercised
    # at all. At 14 the fixture only ever produced 2 trains, which silently could
    # not tell a 2-box scheduler from a 3-box one.
    n = 10 * gl.MAX_CONCURRENT_GATES + 4
    for i in range(n):
        _commit_on(repo, f"cand-{i}", m0, f"f{i}.txt", f"F{i}\n")
        _write_candidate(loops_root, f"{i:064x}", f"cand-{i}", m0, [f"f{i}.txt"])

    # Direct: assembly emits exactly one train per BOX, capped at 10 members, and
    # the trains form a CHAIN — chunk k rooted on chunk k-1's tip. The chunks used
    # to be built on `main` in parallel and raced; the depth bound is now the slot
    # count itself, so a train that no box could grade is never built at all.
    cands = [_cand(f"{i:064x}", f"cand-{i}", m0) for i in range(n)]
    trains, excluded = _assemble(repo, cands, m0, tmp_path,
                                 chain_depth=gl.MAX_CONCURRENT_GATES)
    assert len(trains) == gl.MAX_CONCURRENT_GATES, (
        f"expected one train per box from {n} disjoint candidates, got {len(trains)}")
    assert all(len(t.members) <= 10 for t in trains)
    assert trains[0].base == m0 and trains[0].parent is None
    for earlier, later in zip(trains, trains[1:], strict=False):
        assert later.base == earlier.tip, "chunk k must be ROOTED at chunk k-1's tip"
        assert later.parent == earlier.branch
        assert later.root == m0, "the whole chain stays anchored to one main"
    assert any("beyond the chain depth bound" in e.get("why", "") for e in excluded), \
        "candidates past the depth bound must be recorded, never silently dropped"

    # M1: claim goes through pick_twin (real probes). In this no-network suite
    # admit every non-excluded twin in preference order so the dispatch arithmetic
    # is what is under test, not SSH reachability.
    def _admit_all(exclude=frozenset(), probe=None, readings=None):
        free = [s for s in gl.TWIN_SPECS if s.host not in exclude]
        return (free[0] if free else None,
                [{"host": s.host, "admitted": True, "reason": ""} for s in free])

    monkeypatch.setattr(gl, "pick_twin", _admit_all)

    # run_once must dispatch two gates, the SECOND twin-eligible (allow_remote).
    offload = _fake_offload(tmp_path)
    loop = _make_loop(loops_root, repo, offload, allow_remote_gate=True)
    dispatched: list[tuple] = []
    loop.dispatch_gate = lambda train, *, allow_remote, twin=None: dispatched.append(  # type: ignore[assignment]
        (train.branch, allow_remote, twin))
    loop.run_once()
    # One slot per box: local + every twin in the pool.
    assert len(dispatched) == gl.MAX_CONCURRENT_GATES, dispatched
    assert len(dispatched) == 1 + len(gl.TWIN_SPECS)
    assert dispatched[0][1] is False, "first gate stays local"
    assert dispatched[0][2] is None, "the local gate claims no twin"
    assert all(d[1] is True for d in dispatched[1:]), \
        "every later disjoint train is dispatched to a twin"
    claimed = [d[2] for d in dispatched[1:]]
    assert all(claimed), "a remote dispatch must name the box it claimed"
    assert len(claimed) == len(set(claimed)), \
        "two trains must never be sent to the SAME box — that is the overload " \
        "this two-slot scheduler existed to prevent, now generalised to N boxes"
    assert set(claimed) <= {s.host for s in gl.TWIN_SPECS}


def test_remote_disabled_never_opens_a_second_local_gate(tmp_path):
    repo = _init_repo(tmp_path / "repo")
    m0 = _git(repo, "rev-parse", "HEAD")
    loops_root = tmp_path / "loops"
    for i in range(14):
        _commit_on(repo, f"cand-{i}", m0, f"f{i}.txt", f"F{i}\n")
        _write_candidate(loops_root, f"{i:064x}", f"cand-{i}", m0, [f"f{i}.txt"])

    loop = _make_loop(loops_root, repo, _fake_offload(tmp_path), allow_remote_gate=False)
    dispatched: list[tuple] = []
    loop.dispatch_gate = lambda train, *, allow_remote, twin=None: dispatched.append(  # type: ignore[assignment]
        (train.branch, allow_remote, twin))

    outcomes = loop.run_once()

    # With remote off there is exactly ONE box, whatever the pool size says.
    assert len(dispatched) == 1, dispatched
    assert dispatched[0][1] is False
    assert dispatched[0][2] is None
    assert [o.action for o in outcomes] == ["dispatched"], outcomes
    # ONE box means the chain is one train deep: the surplus candidates are not
    # built into a train nobody could grade this tick — but they are RECORDED,
    # because a candidate that silently disappears reads as "nothing to land".
    assert any("beyond the chain depth bound" in line for line in loop.lines), loop.lines


def test_remote_gate_mints_forwards_and_syncs_evidence(tmp_path, monkeypatch):
    """The twin slot has the section-0 receipt before its gate starts."""
    gate_ws = tmp_path / "gate-ws"
    gate_ws.mkdir()
    local_repo = tmp_path / "repo"
    local_repo.mkdir()
    evidence_root = tmp_path / "evidence"
    candidate_receipt = evidence_root / "records" / "merge-gate" / ("a" * 40 + ".json")
    candidate_receipt.parent.mkdir(parents=True)
    candidate_receipt.write_text('{"signed":true}')
    run_receipt = tmp_path / "run-receipt.json"
    state_file = tmp_path / "state.json"
    calls: list[str] = []

    minted_base = "f" * 40
    monkeypatch.setattr(
        gl, "_mint_candidate_receipt",
        lambda *_a, **_kw: gl.MintOutcome(True, str(candidate_receipt),
                                          str(evidence_root), "minted", minted_base))
    pins: list[dict] = []

    def _pin(host, workspace, branch, sha, *, local_repo, checkout=False, **_kw):
        pins.append({"branch": branch, "sha": sha, "checkout": checkout})
        calls.append("pin-base" if branch == "gate-pinned-main" else "pin")
        return {"ok": True, "why": "pinned"}

    monkeypatch.setattr(gl, "pin_remote_candidate", _pin)
    preflights: list[dict] = []

    def _preflight(*_a, **kw):
        calls.append("preflight")
        preflights.append(kw)
        return {"ready": True, "failed": []}

    monkeypatch.setattr(gl, "preflight_remote", _preflight)
    monkeypatch.setattr(
        gl, "sync_forward_candidate_receipt",
        lambda *_a, **_kw: calls.append("forward") or {"ok": True, "why": "synced"})
    monkeypatch.setattr(
        gl, "remote_gate_command",
        lambda *_a, **_kw: calls.append("gate") or [sys.executable, "-c", "pass"])

    def _sync_back(*_a, **kw):
        calls.append("sync-back")
        Path(kw["local_receipt"]).write_text('{"signed":true}')
        return {"ok": True, "why": "home"}

    monkeypatch.setattr(gl, "sync_back_evidence", _sync_back)
    run_gate_child([
        "--mode", "remote", "--state-file", str(state_file),
        "--gate-workspace", str(gate_ws), "--local-repo", str(local_repo),
        "--candidate", "train/test", "--tip", "a" * 40,
        "--receipt", str(run_receipt),
    ])

    st = json.loads(state_file.read_text())
    assert st["rc"] == 0 and st["receipt"] == str(run_receipt), st
    assert calls == ["pin-base", "pin", "preflight", "forward", "gate", "sync-back"]
    # The twin's HEAD is detached onto the EXACT merge-base the receipt was
    # minted against — the invariant whose absence produced the live
    # "bound to a different merge-base SHA" + false oracle-path refusals.
    assert pins[0] == {"branch": "gate-pinned-main", "sha": minted_base,
                       "checkout": True}
    assert pins[1]["checkout"] is False
    assert preflights[0]["expected_base"] == minted_base


def test_remote_gate_forward_failure_is_instrument_not_a_gate(tmp_path, monkeypatch):
    gate_ws = tmp_path / "gate-ws"
    gate_ws.mkdir()
    evidence_root = tmp_path / "evidence"
    candidate_receipt = evidence_root / "candidate.json"
    candidate_receipt.parent.mkdir(parents=True)
    candidate_receipt.write_text('{"signed":true}')
    state_file = tmp_path / "state.json"
    monkeypatch.setattr(
        gl, "_mint_candidate_receipt",
        lambda *_a, **_kw: gl.MintOutcome(True, str(candidate_receipt),
                                          str(evidence_root), "minted", "e" * 40))
    monkeypatch.setattr(gl, "pin_remote_candidate",
                        lambda *_a, **_kw: {"ok": True, "why": "pinned"})
    monkeypatch.setattr(gl, "preflight_remote",
                        lambda *_a, **_kw: {"ready": True, "failed": []})
    monkeypatch.setattr(gl, "sync_forward_candidate_receipt",
                        lambda *_a, **_kw: {"ok": False, "why": "no route"})
    monkeypatch.setattr(
        gl, "remote_gate_command",
        lambda *_a, **_kw: pytest.fail("gate must not run without its signed prerequisite"))

    run_gate_child([
        "--mode", "remote", "--state-file", str(state_file),
        "--gate-workspace", str(gate_ws), "--local-repo", str(tmp_path / "repo"),
        "--candidate", "train/test", "--tip", "b" * 40,
        "--receipt", str(tmp_path / "run-receipt.json"),
    ])
    st = json.loads(state_file.read_text())
    assert st["rc"] == 75
    assert "instrument" in st["stderr"]


def test_remote_gate_base_pin_failure_is_instrument_and_gate_never_runs(tmp_path, monkeypatch):
    """A twin whose HEAD cannot be pinned to the receipt's merge-base must not
    grade anything: it would recompute a different merge-base, refuse the
    signed receipt, and charge stale-main commits to the candidate."""
    gate_ws = tmp_path / "gate-ws"
    gate_ws.mkdir()
    evidence_root = tmp_path / "evidence"
    candidate_receipt = evidence_root / "candidate.json"
    candidate_receipt.parent.mkdir(parents=True)
    candidate_receipt.write_text('{"signed":true}')
    state_file = tmp_path / "state.json"
    monkeypatch.setattr(
        gl, "_mint_candidate_receipt",
        lambda *_a, **_kw: gl.MintOutcome(True, str(candidate_receipt),
                                          str(evidence_root), "minted", "c" * 40))

    def _pin(host, workspace, branch, sha, *, local_repo, checkout=False, **_kw):
        if branch == "gate-pinned-main":
            return {"ok": False, "why": "twin unreachable for base pin"}
        pytest.fail("candidate pin must not run after a failed base pin")

    monkeypatch.setattr(gl, "pin_remote_candidate", _pin)
    monkeypatch.setattr(
        gl, "preflight_remote",
        lambda *_a, **_kw: pytest.fail("preflight must not run after a failed base pin"))
    monkeypatch.setattr(
        gl, "remote_gate_command",
        lambda *_a, **_kw: pytest.fail("gate must not run on an unpinned twin"))

    run_gate_child([
        "--mode", "remote", "--state-file", str(state_file),
        "--gate-workspace", str(gate_ws), "--local-repo", str(tmp_path / "repo"),
        "--candidate", "train/test", "--tip", "c" * 40,
        "--receipt", str(tmp_path / "run-receipt.json"),
    ])
    st = json.loads(state_file.read_text())
    assert st["rc"] == 75
    assert "instrument" in st["stderr"] and "base pin" in st["stderr"]


# --- FINDING 4: push failure rolls local main back; no merged; re-landable -----


def test_push_failure_rolls_back_and_does_not_record_merged(tmp_path):
    repo = _init_repo(tmp_path / "repo")
    m0 = _git(repo, "rev-parse", "HEAD")
    _commit_on(repo, "cand-r", m0, "r.txt", "RRR\n")
    # A remote that cannot be pushed to -> every push fails.
    _git(repo, "remote", "add", "origin", str(tmp_path / "nonexistent-remote.git"))
    loops_root = tmp_path / "loops"
    id_r = _write_candidate(loops_root, "9" * 64, "cand-r", m0, ["r.txt"])

    offload = _fake_offload(tmp_path)
    os.environ["FAKE_GATE_RC"] = "0"
    os.environ.pop("FAKE_GATE_SLUG", None)
    os.environ.pop("FAKE_GATE_NO_RECEIPT", None)
    try:
        loop = _make_loop(loops_root, repo, offload, remote="origin", push=True)
        train = loop.run_once()[0].train
        _wait_done(gate_state_path(loops_root, train))
        loop2 = _make_loop(loops_root, repo, offload, remote="origin", push=True)
        out2 = loop2.run_once()
    finally:
        os.environ.pop("FAKE_GATE_RC", None)

    assert out2[0].action != "landed", out2[0]
    # local main was rolled back to the pre-merge sha (no split-brain with origin)
    assert _git(repo, "rev-parse", "main") == m0, "local main must roll back on push failure"
    # NO merged event was recorded for a merge that never reached origin
    events = _ledger_events(loops_root)
    assert not any(e["event"] == "merged" for e in events)
    # and the candidate is still landable (not terminal), so it re-lands next tick
    assert id_r not in {e["id"] for e in events if e["event"] in ("merged", "rejected")}


# ================================================= CLOSING-REVIEW (Grok) FIXES
# 3 push-failure edge-path findings. Each regression is written to FAIL against
# the current source (e12e31c) and pass only after its fix.


def _fake_git_failing(*, fail_push=False, fail_reset=False):
    """A gl.git wrapper that fails `push`/`reset` on demand and delegates
    everything else (merge, rev-parse, symbolic-ref, ...) to the real git."""
    real = gl.git

    def fake(repo, *args, **kw):
        if args and args[0] == "push" and fail_push:
            return (1, "", "push denied (test)")
        if args and args[0] == "reset" and fail_reset:
            return (128, "", "fatal: Unable to create '.git/index.lock': File exists (test)")
        return real(repo, *args, **kw)

    return fake


def _manual_train(repo: Path, base: str) -> Train:
    """A one-commit train branch ahead of `base` that ff-merges cleanly."""
    _git(repo, "checkout", "-q", "-B", "train/manual", base)
    (repo / "t.txt").write_text("TTT\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "train/manual: t.txt")
    tip = _git(repo, "rev-parse", "HEAD")
    _git(repo, "checkout", "-q", "main")
    return Train(branch="train/manual", base=base, tip=tip,
                 members=[{"id": f"sha256:{'a' * 64}", "branch": "train/manual",
                           "base": base, "paths": ["t.txt"]}], paths=["t.txt"])


# --- MAJOR-2: push + rollback DOUBLE failure -> poison & halt, no more landing -


def test_double_failure_poisons_and_halts(tmp_path, monkeypatch):
    repo = _init_repo(tmp_path / "repo")
    m0 = _git(repo, "rev-parse", "HEAD")
    loops_root = tmp_path / "loops"
    (loops_root / "state").mkdir(parents=True, exist_ok=True)
    train = _manual_train(repo, m0)

    loop = _make_loop(loops_root, repo, _fake_offload(tmp_path), remote="origin", push=True)
    # push fails AND the rollback reset ALSO fails (e.g. index.lock held).
    monkeypatch.setattr(gl, "git", _fake_git_failing(fail_push=True, fail_reset=True))

    with pytest.raises(gl.DaemonPoisoned):
        loop.land_train(train)

    # local main is DIVERGED (ff-merge happened, rollback did not) ...
    assert _git(repo, "rev-parse", "main") == train.tip
    assert _git(repo, "rev-parse", "main") != m0
    # ... a durable poison marker was written with the divergence details ...
    assert loop.poisoned()
    poison = json.loads(loop._poison_path().read_text())
    assert poison["poisoned"] is True
    assert poison["local_diverged_sha"] == train.tip
    assert poison["origin_expected_sha"] == m0
    assert any("CRITICAL" in a for a in loop.alerts)

    # ... and every subsequent tick REFUSES to land anything while it stands.
    diverged = _git(repo, "rev-parse", "main")
    loop2 = _make_loop(loops_root, repo, _fake_offload(tmp_path), remote="origin", push=True)
    with pytest.raises(gl.DaemonPoisoned):
        loop2.run_once()
    assert _git(repo, "rev-parse", "main") == diverged, "poisoned daemon must not land further"

    # a human clears the marker -> the daemon may run again
    loop._poison_path().unlink()
    loop3 = _make_loop(loops_root, repo, _fake_offload(tmp_path))
    loop3.run_once()                                  # no raise once the marker is gone


# --- MAJOR-1: push fails, rollback OK -> a DISTINCT instrument_error event ------


def test_push_failure_reset_ok_emits_instrument_error_event(tmp_path, monkeypatch):
    repo = _init_repo(tmp_path / "repo")
    m0 = _git(repo, "rev-parse", "HEAD")
    loops_root = tmp_path / "loops"
    (loops_root / "state").mkdir(parents=True, exist_ok=True)
    train = _manual_train(repo, m0)

    loop = _make_loop(loops_root, repo, _fake_offload(tmp_path), remote="origin", push=True)
    monkeypatch.setattr(gl, "git", _fake_git_failing(fail_push=True, fail_reset=False))

    # drive on_pass directly with a synthetic PASS verdict
    from bridge.integration import GateVerdict
    v = GateVerdict("pass", 0, "", "gate passed", None, "", 1.0)
    out = loop.on_pass(train, v)

    # rolled back cleanly, so it is NOT a divergence/poison ...
    assert not loop.poisoned()
    assert _git(repo, "rev-parse", "main") == m0
    # ... but it IS distinguishable from a routine base-move: action 'instrument'
    # and a real instrument_error event carrying the push signature.
    assert out.action == "instrument", out
    assert "push" in out.detail.lower()
    events = _ledger_events(loops_root)
    ie = [e for e in events if e.get("event") == "instrument_error"]
    assert ie, "push failure must emit an instrument_error event, not just an alert"
    assert any(e["detail"].get("kind") == "push_failed"
               or "push" in str(e.get("detail", "")).lower() for e in ie)
    # NOT a merged, NOT a rejection
    assert not any(e["event"] == "merged" for e in events)


# --- MINOR-1: rc==0 with an empty/garbage receipt is NOT a pass ----------------


def test_zerobyte_receipt_is_instrument_error_not_pass(tmp_path):
    rec = tmp_path / "r.json"
    rec.write_text("")                                # exists, but zero-byte
    sf = tmp_path / "s.json"
    sf.write_text(json.dumps({"state": "done", "rc": 0, "stdout": "ok", "stderr": "",
                              "receipt": str(rec), "duration_s": 1.0}), encoding="utf-8")
    v = read_gate_verdict(sf)
    assert v is not None and v.result == "instrument-error", v
    assert v.result != "pass"


def test_garbage_receipt_is_instrument_error_not_pass(tmp_path):
    rec = tmp_path / "r.json"
    rec.write_text("this is not json {{{")            # exists, non-empty, unparseable
    sf = tmp_path / "s.json"
    sf.write_text(json.dumps({"state": "done", "rc": 0, "stdout": "ok", "stderr": "",
                              "receipt": str(rec), "duration_s": 1.0}), encoding="utf-8")
    v = read_gate_verdict(sf)
    assert v is not None and v.result == "instrument-error", v
    # a valid, non-empty JSON object receipt still passes (regression floor)
    rec.write_text(json.dumps({"signed": True, "rc": 0}), encoding="utf-8")
    assert read_gate_verdict(sf).result == "pass"


# ============================================ LAST-MILE FIX: stale-gate-script
# The daemon must invoke merge-gate.sh FROM THE PINNED WORKSPACE, so the gate's
# self-identity guard sees a matching judge instead of self-refusing rc=2.


def test_local_gate_command_references_pinned_workspace_copy():
    cmd = local_gate_command(Path("/Users/youruser/OmniAgentOS-gate"),
                             "cand-x", "/tmp/r.json")
    assert cmd[0] == "bash"
    # the SCRIPT is the pinned workspace's own copy, not a repo-relative/hardcoded one
    assert cmd[1] == "/Users/youruser/OmniAgentOS-gate/scripts/merge-gate.sh"
    assert "--candidate" in cmd and "cand-x" in cmd
    assert "--emit-receipt" in cmd


def test_dispatch_local_gate_uses_direct_mode_from_pinned_workspace(tmp_path, monkeypatch):
    repo = _init_repo(tmp_path / "repo")
    m0 = _git(repo, "rev-parse", "HEAD")
    _commit_on(repo, "cand-l", m0, "l.txt", "LLL\n")
    loops_root = tmp_path / "loops"
    _write_candidate(loops_root, "7" * 64, "cand-l", m0, ["l.txt"])
    gate_ws = _fake_gate_workspace(tmp_path)

    captured: list[list[str]] = []
    real_popen = gl.subprocess.Popen

    class _FakeProc:
        pid = 4242

    def _fake_popen(argv, *a, **kw):
        # Only intercept the detached GATE CHILD; delegate every other Popen
        # (git worktree, git ...) to the real one so run_once still works.
        if isinstance(argv, list) and "run-gate" in argv:
            captured.append(argv)
            return _FakeProc()
        return real_popen(argv, *a, **kw)

    monkeypatch.setattr(gl.subprocess, "Popen", _fake_popen)
    loop = _make_loop(loops_root, repo, _fake_offload(tmp_path), gate_ws=gate_ws)
    loop.run_once()

    assert captured, "a gate child should have been dispatched"
    argv = captured[0]
    assert "--mode" in argv and argv[argv.index("--mode") + 1] == "direct"
    assert "--gate-workspace" in argv
    assert argv[argv.index("--gate-workspace") + 1] == str(gate_ws)
    # a local gate must NOT be handed to offload
    assert "offload" not in " ".join(argv).lower() or "--offload" not in argv
    # and the actual gate command the child will run points at the pinned copy
    gate_cmd = local_gate_command(gate_ws, "cand-l", "r.json")
    assert gate_cmd[1] == str(gate_ws / "scripts" / "merge-gate.sh")
    # the state file records the mode + pinned workspace, too
    sfs = list((loops_root / "state" / "gates").glob("*.json"))
    assert sfs
    st = json.loads(sfs[0].read_text())
    assert st["mode"] == "direct" and st["gate_workspace"] == str(gate_ws)


def _self_identity_gate(path: Path) -> None:
    """A merge-gate.sh mimic implementing ONLY the self-identity guard: hash the
    executing $0 and compare against the blob the pinned workspace carries at
    HEAD; refuse rc=2 stale-gate-script on mismatch, else write the receipt and
    pass. This is the exact guard that stuck the live daemon."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "#!/bin/bash\n"
        "SELF_DIR=$(CDPATH= cd -- \"$(dirname -- \"$0\")\" && pwd)\n"
        "GATE_SCRIPT_PATH=\"$SELF_DIR/$(basename -- \"$0\")\"\n"
        "sha() { { sha256sum 2>/dev/null || shasum -a 256 2>/dev/null; } | awk '{print $1}'; }\n"
        "SELF_SHA=$(sha < \"$GATE_SCRIPT_PATH\")\n"
        "GATE_WS=\"${OMNIAGENTOS_GATE_WORKSPACE}\"\n"
        "PIN_SHA=$(git -C \"$GATE_WS\" cat-file blob HEAD:scripts/merge-gate.sh 2>/dev/null | sha)\n"
        "receipt=\"\"\n"
        "while [ $# -gt 0 ]; do case \"$1\" in --emit-receipt) receipt=\"$2\"; shift 2;; *) shift;; esac; done\n"
        "if [ \"$SELF_SHA\" != \"$PIN_SHA\" ]; then echo 'refusing: stale-gate-script' >&2; exit 2; fi\n"
        "[ -n \"$receipt\" ] && printf '{\"signed\":true}\\n' > \"$receipt\"\n"
        "exit 0\n",
        encoding="utf-8")
    path.chmod(0o755)


def test_direct_gate_from_pinned_workspace_avoids_stale_gate_script(tmp_path, monkeypatch):
    # A CURRENT, clean gate workspace whose committed merge-gate.sh == on disk.
    gate_ws = tmp_path / "gate-ws"
    gate_ws.mkdir()
    _git(gate_ws, "init", "-q", "-b", "main")
    _git(gate_ws, "config", "user.email", "g@x")
    _git(gate_ws, "config", "user.name", "g")
    _self_identity_gate(gate_ws / "scripts" / "merge-gate.sh")
    _git(gate_ws, "add", "-A")
    _git(gate_ws, "commit", "-qm", "gate")

    # A serving repo carrying a DIFFERENT (stale) copy of the gate script.
    serving = tmp_path / "serving"
    serving.mkdir()
    _git(serving, "init", "-q", "-b", "main")
    _self_identity_gate(serving / "scripts" / "merge-gate.sh")
    (serving / "scripts" / "merge-gate.sh").write_text(
        (serving / "scripts" / "merge-gate.sh").read_text() + "# STALE DIVERGENCE\n",
        encoding="utf-8")
    (serving / "scripts" / "merge-gate.sh").chmod(0o755)

    receipt = tmp_path / "r.json"
    sf = tmp_path / "s.json"
    # Receipt minting has its own tests/integration path; this test isolates the
    # stale-judge identity invariant and uses a successful mint result.
    evidence = tmp_path / "evidence"
    monkeypatch.setattr(gl, "_mint_candidate_receipt",
                        lambda *_a, **_kw: gl.MintOutcome(True, "minted", str(evidence), "minted"))

    # FIX: running the PINNED workspace's own copy -> identity matches -> PASS.
    run_gate_child(["--mode", "direct", "--state-file", str(sf),
                    "--gate-workspace", str(gate_ws), "--candidate", "cand",
                    "--tip", "deadbeef", "--receipt", str(receipt)])
    st = json.loads(sf.read_text())
    assert st["rc"] == 0, st
    assert "stale-gate-script" not in (st["stdout"] + st["stderr"])
    assert read_gate_verdict(sf).result == "pass"

    # CONTRAST: the OLD behaviour — running the serving repo's copy against the
    # pinned workspace — is exactly the stale-gate-script self-refusal (rc 2).
    env = dict(os.environ)
    env["OMNIAGENTOS_GATE_WORKSPACE"] = str(gate_ws)
    proc = subprocess.run(
        ["bash", str(serving / "scripts" / "merge-gate.sh"),
         "--candidate", "cand", "--emit-receipt", str(tmp_path / "r2.json")],
        cwd=str(gate_ws), env=env, capture_output=True, text=True, check=False)
    assert proc.returncode == 2
    assert "stale-gate-script" in proc.stderr


# ============================ §0 RECEIPT MINTING IS IDEMPOTENT PER TRAIN TIP ===
# The evidence store is append-once per run id, and a merge candidate's run id IS
# the train tip SHA. Minting unconditionally on every dispatch therefore made the
# SECOND gate of a tip — which is exactly what "re-gate once on an instrument
# error" asks for — refuse with `gate evidence already recorded for run <tip>`,
# reported as rc 127, so every re-gated train parked forever. The remedy is NOT to
# make the store rewritable (it is write-once on purpose); it is to REUSE the
# receipt already on disk once the gate's own verifier confirms it binds this
# exact candidate.

_FAKE_MINTER = '''#!/usr/bin/env python3
"""Stand-in for scripts/mint-merge-candidate.py.

Reproduces the ONE behaviour under test: the real minter drives
GateEvidenceStore.record(), which publishes with os.link and raises
GateEvidenceExists for a run id that already has a record, which the real CLI
prints as "REFUSED: <message>" with exit 1. The message text is pinned against
the REAL store by test_daemon_detects_the_real_evidence_store_refusal.
"""
import argparse
import json
import os
import sys
from pathlib import Path

ap = argparse.ArgumentParser()
ap.add_argument("--candidate-sha", required=True)
ap.add_argument("--merge-base-sha", required=True)
ap.add_argument("--evidence-root", required=True)
ap.add_argument("--workspace", required=True)
ap.add_argument("--command", default="")
a = ap.parse_args()

root = Path(a.evidence_root)
root.mkdir(parents=True, exist_ok=True)
with (root / "calls-mint").open("a", encoding="utf-8") as fh:
    fh.write(a.candidate_sha + "\\n")

other = os.environ.get("FAKE_MINT_REFUSE_OTHER_RUN")
if other:
    print("REFUSED: gate evidence already recorded for run " + other, file=sys.stderr)
    raise SystemExit(1)

record = root / "records" / "merge-gate" / (a.candidate_sha + ".json")
if record.exists():
    print("REFUSED: gate evidence already recorded for run " + a.candidate_sha,
          file=sys.stderr)
    raise SystemExit(1)
record.parent.mkdir(parents=True, exist_ok=True)
record.write_text(json.dumps({
    "run_id": a.candidate_sha,
    "candidate_sha": a.candidate_sha,
    "merge_base_sha": a.merge_base_sha,
    "workspace": a.workspace,
    "signed": True,
}), encoding="utf-8")
print(record)
'''

_FAKE_VERIFIER = '''"""Stand-in for `omniagentos.scheduler.gate_evidence verify-candidate`.

Answers the same question the real verifier answers — is this signed receipt
bound to THIS candidate and merge base — with the same CLI surface, which is
pinned against the real module by
test_verify_candidate_cli_contract_is_what_the_daemon_calls.
"""
import argparse
import json
import sys
from pathlib import Path

ap = argparse.ArgumentParser()
sub = ap.add_subparsers(dest="action", required=True)
v = sub.add_parser("verify-candidate")
v.add_argument("--receipt", required=True)
v.add_argument("--evidence-root", required=True)
v.add_argument("--candidate-sha", required=True)
v.add_argument("--merge-base-sha", required=True)
a = ap.parse_args()

root = Path(a.evidence_root)
root.mkdir(parents=True, exist_ok=True)
with (root / "calls-verify").open("a", encoding="utf-8") as fh:
    fh.write(a.candidate_sha + "\\n")

try:
    payload = json.loads(Path(a.receipt).read_text(encoding="utf-8"))
except (OSError, ValueError) as exc:
    print("REFUSED: no signed receipt at %s (%s)" % (a.receipt, exc), file=sys.stderr)
    raise SystemExit(1)

errors = []
if not payload.get("signed"):
    errors.append("invalid evidence signature at " + a.receipt)
if payload.get("candidate_sha") != a.candidate_sha:
    errors.append("signed receipt is bound to a different candidate SHA")
if payload.get("merge_base_sha") != a.merge_base_sha:
    errors.append("signed receipt is bound to a different merge-base SHA")
if errors:
    print("REFUSED: " + "; ".join(errors), file=sys.stderr)
    raise SystemExit(1)
print("verified pytest receipt for candidate " + a.candidate_sha[:12])
'''


def _mint_workspace(tmp_path: Path) -> tuple[Path, str, Path]:
    """A gate workspace with a stand-in minter + verifier, and ONE train tip.

    The directory deliberately does NOT end in `-gate`, so the evidence root the
    daemon computes is `<gate_ws>/var/gate-evidence` — the same derivation
    merge-gate.sh performs in pinned mode.
    """
    gate_ws = _init_repo(tmp_path / "gate-ws")
    base = _git(gate_ws, "rev-parse", "HEAD")
    tip = _commit_on(gate_ws, "train/mint", base, "t.txt", "T\n")

    minter = gate_ws / "scripts" / "mint-merge-candidate.py"
    minter.parent.mkdir(parents=True, exist_ok=True)
    minter.write_text(_FAKE_MINTER, encoding="utf-8")

    verifier = gate_ws / "omniagentos" / "scheduler" / "gate_evidence.py"
    verifier.parent.mkdir(parents=True, exist_ok=True)
    verifier.write_text(_FAKE_VERIFIER, encoding="utf-8")
    (gate_ws / "omniagentos" / "__init__.py").write_text("", encoding="utf-8")
    (gate_ws / "omniagentos" / "scheduler" / "__init__.py").write_text("", encoding="utf-8")

    return gate_ws, tip, gate_ws / "var" / "gate-evidence"


def test_second_mint_of_the_same_tip_reuses_the_verified_receipt(tmp_path):
    """RED before the fix: the second mint returned an rc-127 instrument fault."""
    gate_ws, tip, evidence_root = _mint_workspace(tmp_path)

    first = gl._mint_candidate_receipt(gate_ws, tip)
    assert first.ok, first
    assert first.provenance == "minted", first
    assert first.evidence_root == str(evidence_root)
    receipt = Path(first.detail)
    assert receipt.exists(), first
    minted_bytes = receipt.read_text(encoding="utf-8")

    second = gl._mint_candidate_receipt(gate_ws, tip)

    assert second.ok, second                      # <- the bricked-train defect
    assert second.provenance == "reused", second
    assert second.detail == first.detail
    assert second.evidence_root == first.evidence_root
    # Reuse went through the gate's OWN verifier — not a bare .exists() check.
    assert (evidence_root / "calls-verify").read_text(encoding="utf-8").split() == [tip]
    # And it did not rewrite the append-once record.
    assert receipt.read_text(encoding="utf-8") == minted_bytes


def test_reuse_is_refused_when_the_receipt_binds_another_candidate(tmp_path):
    """A receipt for a DIFFERENT candidate is still an instrument failure."""
    gate_ws, tip, evidence_root = _mint_workspace(tmp_path)
    record = evidence_root / "records" / "merge-gate" / f"{tip}.json"
    record.parent.mkdir(parents=True, exist_ok=True)
    record.write_text(json.dumps({
        "run_id": tip, "candidate_sha": "b" * 40,
        "merge_base_sha": "c" * 40, "signed": True,
    }), encoding="utf-8")

    out = gl._mint_candidate_receipt(gate_ws, tip)

    assert out.ok is False, out
    assert out.provenance == "reuse-refused", out
    assert "different candidate SHA" in out.detail, out


def test_reuse_is_refused_when_the_existing_receipt_does_not_verify(tmp_path):
    gate_ws, tip, evidence_root = _mint_workspace(tmp_path)
    record = evidence_root / "records" / "merge-gate" / f"{tip}.json"
    record.parent.mkdir(parents=True, exist_ok=True)
    record.write_text("{ not json", encoding="utf-8")

    out = gl._mint_candidate_receipt(gate_ws, tip)

    assert out.ok is False, out
    assert out.provenance == "reuse-refused", out
    assert "no signed receipt" in out.detail, out


def test_a_refusal_naming_another_run_is_not_reused(tmp_path):
    """Only THIS tip's own recorded evidence licenses a reuse."""
    gate_ws, tip, _root = _mint_workspace(tmp_path)
    os.environ["FAKE_MINT_REFUSE_OTHER_RUN"] = "d" * 40
    try:
        out = gl._mint_candidate_receipt(gate_ws, tip)
    finally:
        os.environ.pop("FAKE_MINT_REFUSE_OTHER_RUN", None)

    assert out.ok is False, out
    assert out.provenance == "mint-failed", out


def test_an_ordinary_mint_failure_stays_a_mint_failure(tmp_path):
    gate_ws, tip, _root = _mint_workspace(tmp_path)
    (gate_ws / "scripts" / "mint-merge-candidate.py").unlink()

    out = gl._mint_candidate_receipt(gate_ws, tip)

    assert out.ok is False, out
    assert out.provenance == "mint-failed", out


def test_direct_gate_state_records_a_refused_reuse_distinctly(tmp_path):
    """The state file must say WHICH instrument fault this was."""
    gate_ws, tip, evidence_root = _mint_workspace(tmp_path)
    record = evidence_root / "records" / "merge-gate" / f"{tip}.json"
    record.parent.mkdir(parents=True, exist_ok=True)
    record.write_text(json.dumps({
        "run_id": tip, "candidate_sha": "b" * 40,
        "merge_base_sha": "c" * 40, "signed": True,
    }), encoding="utf-8")
    sf = tmp_path / "state.json"

    run_gate_child(["--mode", "direct", "--state-file", str(sf),
                    "--gate-workspace", str(gate_ws), "--candidate", "train/mint",
                    "--tip", tip, "--receipt", str(tmp_path / "r.json")])

    st = json.loads(sf.read_text())
    assert st["rc"] == 127, st
    assert st["mint"] == "reuse-refused", st
    assert "cannot be reused" in st["stderr"], st
    assert read_gate_verdict(sf).result == "instrument-error"


def test_direct_gate_state_records_a_reused_receipt(tmp_path, monkeypatch):
    gate_ws = tmp_path / "gate-ws"
    gate_ws.mkdir()
    _git(gate_ws, "init", "-q", "-b", "main")
    _git(gate_ws, "config", "user.email", "g@x")
    _git(gate_ws, "config", "user.name", "g")
    _self_identity_gate(gate_ws / "scripts" / "merge-gate.sh")
    _git(gate_ws, "add", "-A")
    _git(gate_ws, "commit", "-qm", "gate")
    sf = tmp_path / "state.json"
    monkeypatch.setattr(
        gl, "_mint_candidate_receipt",
        lambda *_a, **_kw: gl.MintOutcome(True, "receipt", str(tmp_path / "ev"), "reused"))

    run_gate_child(["--mode", "direct", "--state-file", str(sf),
                    "--gate-workspace", str(gate_ws), "--candidate", "cand",
                    "--tip", "deadbeef", "--receipt", str(tmp_path / "r.json")])

    st = json.loads(sf.read_text())
    assert st["rc"] == 0, st
    assert st["mint"] == "reused", st
    assert read_gate_verdict(sf).result == "pass"


def test_remote_gate_reuses_and_forwards_the_existing_receipt(tmp_path, monkeypatch):
    """The twin call site carries the SAME idempotence, not a second policy."""
    gate_ws = tmp_path / "gate-ws"
    gate_ws.mkdir()
    (tmp_path / "repo").mkdir()
    evidence_root = tmp_path / "evidence"
    reused_receipt = evidence_root / "records" / "merge-gate" / ("a" * 40 + ".json")
    reused_receipt.parent.mkdir(parents=True)
    reused_receipt.write_text('{"signed":true}')
    forwarded: list[str] = []
    monkeypatch.setattr(
        gl, "_mint_candidate_receipt",
        lambda *_a, **_kw: gl.MintOutcome(True, str(reused_receipt),
                                          str(evidence_root), "reused", "d" * 40))
    monkeypatch.setattr(gl, "pin_remote_candidate",
                        lambda *_a, **_kw: {"ok": True, "why": "pinned"})
    monkeypatch.setattr(gl, "preflight_remote",
                        lambda *_a, **_kw: {"ready": True, "failed": []})
    monkeypatch.setattr(
        gl, "sync_forward_candidate_receipt",
        lambda *_a, **kw: forwarded.append(kw["local_receipt"]) or {"ok": True, "why": "s"})
    monkeypatch.setattr(gl, "remote_gate_command",
                        lambda *_a, **_kw: [sys.executable, "-c", "pass"])

    def _sync_back(*_a, **kw):
        Path(kw["local_receipt"]).write_text('{"signed":true}')
        return {"ok": True, "why": "home"}

    monkeypatch.setattr(gl, "sync_back_evidence", _sync_back)
    sf = tmp_path / "state.json"
    run_gate_child([
        "--mode", "remote", "--state-file", str(sf),
        "--gate-workspace", str(gate_ws), "--local-repo", str(tmp_path / "repo"),
        "--candidate", "train/test", "--tip", "a" * 40,
        "--receipt", str(tmp_path / "run-receipt.json"),
    ])

    st = json.loads(sf.read_text())
    assert st["rc"] == 0, st
    assert st["mint"] == "reused", st
    # The receipt forwarded to the twin is the REUSED one, not an absent re-mint.
    assert forwarded == [str(reused_receipt)]


def test_offload_gate_records_no_mint_provenance(tmp_path):
    sf = tmp_path / "state.json"
    run_gate_child(["--mode", "offload", "--state-file", str(sf),
                    "--offload", sys.executable, "--candidate", "c", "--tip", "t",
                    "--receipt", str(tmp_path / "r.json")])
    assert json.loads(sf.read_text())["mint"] is None


# --- the two REAL contracts the stand-ins above stand in for -------------------


def test_daemon_detects_the_real_evidence_store_refusal(tmp_path):
    """Pin `_ALREADY_RECORDED_RE` to the message the REAL store actually raises."""
    sys.path.insert(0, str(ROOT.parent))
    from omniagentos.scheduler import gate_evidence as ge  # noqa: PLC0415

    store = ge.GateEvidenceStore(tmp_path / "ev", create_key=True)
    run_id = "a" * 40
    evidence = ge.GateEvidence(
        schema=ge.SCHEMA, routine_id=ge.MERGE_GATE_ROUTINE_ID, run_id=run_id,
        iteration=1, gate_type=ge.MERGE_GATE_TYPE, command="pytest -q",
        targets=("tests/",), workspace_digest="0" * 64, binding_digest="1" * 64,
        tool="pytest", tool_version="9.0", exit_code=0, checks_collected=1,
        checks_passed=1, checks_skipped=0, checks_failed=0,
        started_at="2026-08-09T00:00:00Z", finished_at="2026-08-09T00:00:01Z",
        nonce="n" * 32, candidate_sha=run_id, merge_base_sha="b" * 40,
    )
    store.record(evidence)
    with pytest.raises(ge.GateEvidenceExists) as caught:
        store.record(evidence)

    match = gl._ALREADY_RECORDED_RE.search(f"REFUSED: {caught.value}")
    assert match is not None, str(caught.value)
    assert match.group(1) == run_id


def test_verify_candidate_cli_contract_is_what_the_daemon_calls(tmp_path):
    """Pin the REAL verifier's subcommand and flag names the reuse path shells to."""
    repo_root = ROOT.parent
    env = dict(os.environ)
    env["PYTHONPATH"] = str(repo_root)
    proc = subprocess.run(
        [sys.executable, "-m", "omniagentos.scheduler.gate_evidence", "verify-candidate",
         "--receipt", str(tmp_path / "absent.json"),
         "--evidence-root", str(tmp_path / "ev"),
         "--candidate-sha", "a" * 40, "--merge-base-sha", "b" * 40],
        cwd=str(repo_root), env=env, capture_output=True, text=True, check=False)

    combined = proc.stdout + proc.stderr
    assert proc.returncode != 0, combined
    # argparse rejections (a renamed flag or subcommand) print a usage block; the
    # daemon's reuse path would then read a REAL receipt as unverifiable.
    assert "usage:" not in combined, combined
    assert "invalid choice" not in combined and "unrecognized arguments" not in combined


# ============================================== CLOSURE CONTRACT §5.5 (emit half)
# The self-closing loop's last mile: the daemon resolves candidate -> proposal ->
# (finding, bound test), hands the binding to the gate, and — only on PROOF that
# the bound test ran green on the merged tree — emits the finding-side terminal
# `closed` event. Every test below is written so that it fails against a daemon
# that closes a finding on anything less than that proof.


FINDING = "sha256:" + "f" * 64
PROPOSAL = "sha256:" + "9" * 64
NODE = "pipeline/tests/test_publish_queue.py::test_F6_read_wip_rejects_non_int_wip"


def _write_proposal(loops_root: Path, ident: str = PROPOSAL, *,
                    answers_finding: object = FINDING,
                    node_id: object = NODE,
                    failing_test: object = "__default__") -> str:
    payload: dict = {"direction": 1, "problem": "p"}
    if answers_finding is not None:
        payload["answers_finding"] = answers_finding
    if failing_test == "__default__":
        if node_id is not None:
            payload["failing_test"] = {"node_id": node_id, "kind": "regression"}
    elif failing_test is not None:
        payload["failing_test"] = failing_test
    pdir = loops_root / "proposals"
    pdir.mkdir(parents=True, exist_ok=True)
    (pdir / f"{ident.replace(':', '_', 1)}.json").write_text(
        json.dumps({"contract": "v1.1", "id": ident, "kind": "proposal",
                    "title": "p", "created_at": "2026-08-10T00:00:00Z",
                    "producer": {"role": "planner", "actor": "planner@x"},
                    "payload": payload}),
        encoding="utf-8")
    return ident


def _binding_gate_workspace(base: Path, repo: Path | None = None) -> Path:
    """A pinned gate workspace whose merge-gate.sh ACCEPTS `--bound-test` and
    reports the binding in its emitted run receipt exactly as the real script
    does: `bound_test` a LIST (null when none was passed) and `bound_test_result`
    the worst-wins verdict, ABSENT entirely when FAKE_BOUND_RESULT is unset (the
    older-gate case the daemon must refuse to close on)."""
    gw = _fake_gate_workspace(base, repo)
    script = gw / "scripts" / "merge-gate.sh"
    script.write_text(
        '#!/bin/bash\n'
        'receipt=""\n'
        'bound=()\n'
        'while [ $# -gt 0 ]; do\n'
        '  case "$1" in\n'
        '    --emit-receipt) receipt="$2"; shift 2;;\n'
        '    --bound-test) bound+=("$2"); shift 2;;\n'
        '    *) shift;;\n'
        '  esac\n'
        'done\n'
        'rc="${FAKE_GATE_RC:-0}"\n'
        'slug="${FAKE_GATE_SLUG:-}"\n'
        'if [ -n "$receipt" ] && [ "${FAKE_GATE_NO_RECEIPT:-}" != "1" ]; then\n'
        '  python3 - "$receipt" "${FAKE_BOUND_RESULT:-}" "${bound[@]}" <<\'PY\'\n'
        'import json, sys\n'
        'path, result, nodes = sys.argv[1], sys.argv[2], sys.argv[3:]\n'
        'doc = {"signed": True, "bound_test": nodes or None}\n'
        'if result:\n'
        '    doc["bound_test_result"] = result\n'
        'with open(path, "w") as fh:\n'
        '    json.dump(doc, fh)\n'
        'PY\n'
        'fi\n'
        'if [ -n "$slug" ]; then echo "refusing: $slug" >&2; fi\n'
        'exit "$rc"\n',
        encoding="utf-8")
    script.chmod(0o755)
    return gw


def _land_with_binding(tmp_path, *, bound_result: str | None, gate_ws_binds: bool = True,
                       node_id: object = NODE, resolves: object = PROPOSAL):
    """Drive a full two-tick landing of one candidate carrying a closure chain.
    Returns (loops_root, ledger events, the daemon that landed it)."""
    repo = _init_repo(tmp_path / "repo")
    m0 = _git(repo, "rev-parse", "HEAD")
    _commit_on(repo, "cand-c", m0, "c.txt", "CCC\n")
    loops_root = tmp_path / "loops"
    _write_proposal(loops_root, node_id=node_id)
    cand_id = _write_candidate(loops_root, "c" * 64, "cand-c", m0, ["c.txt"],
                               resolves=resolves)
    gate_ws = (_binding_gate_workspace(tmp_path, repo) if gate_ws_binds
               else _fake_gate_workspace(tmp_path, repo))
    offload = _fake_offload(tmp_path)
    os.environ["FAKE_GATE_RC"] = "0"
    os.environ.pop("FAKE_GATE_SLUG", None)
    if bound_result is None:
        os.environ.pop("FAKE_BOUND_RESULT", None)
    else:
        os.environ["FAKE_BOUND_RESULT"] = bound_result
    try:
        loop = _make_loop(loops_root, repo, offload, gate_ws=gate_ws)
        out1 = loop.run_once()
        assert out1[0].action == "dispatched", out1
        _wait_done(gate_state_path(loops_root, out1[0].train))
        loop2 = _make_loop(loops_root, repo, offload, gate_ws=gate_ws)
        out2 = loop2.run_once()
        assert out2[0].action == "landed", (out2[0].action, out2[0].detail, loop2.lines)
    finally:
        os.environ.pop("FAKE_GATE_RC", None)
        os.environ.pop("FAKE_BOUND_RESULT", None)
    return loops_root, _ledger_events(loops_root), loop2, cand_id


# ------------------------------------------------ 1. binding resolution (§5.5.2)


def _loaded(loops_root: Path, repo: Path, tmp_path: Path):
    loop = _make_loop(loops_root, repo, _fake_offload(tmp_path))
    return loop, loop.load_candidates(set())


def test_full_chain_resolves_the_finding_and_its_bound_test(tmp_path):
    repo = _init_repo(tmp_path / "repo")
    m0 = _git(repo, "rev-parse", "HEAD")
    _commit_on(repo, "cand-c", m0, "c.txt", "CCC\n")
    loops_root = tmp_path / "loops"
    _write_proposal(loops_root)
    ident = _write_candidate(loops_root, "c" * 64, "cand-c", m0, ["c.txt"],
                             resolves=PROPOSAL)

    loop, cands = _loaded(loops_root, repo, tmp_path)
    assert [c.ident for c in cands] == [ident]
    assert loop.bindings[ident] == [gl.Binding(ident, FINDING, NODE)]


def test_a_list_of_resolves_yields_one_binding_per_proposal(tmp_path):
    """38 of the live candidates carry `resolves` as a LIST; each named proposal
    is its own closure chain and must contribute its own bound test."""
    repo = _init_repo(tmp_path / "repo")
    m0 = _git(repo, "rev-parse", "HEAD")
    _commit_on(repo, "cand-c", m0, "c.txt", "CCC\n")
    loops_root = tmp_path / "loops"
    second_prop = "sha256:" + "8" * 64
    second_find = "sha256:" + "7" * 64
    _write_proposal(loops_root)
    _write_proposal(loops_root, second_prop, answers_finding=second_find,
                    node_id="tests/test_other.py::test_two")
    ident = _write_candidate(loops_root, "c" * 64, "cand-c", m0, ["c.txt"],
                             resolves=[PROPOSAL, second_prop])

    loop, cands = _loaded(loops_root, repo, tmp_path)
    assert len(cands) == 1
    assert loop.bindings[ident] == [
        gl.Binding(ident, FINDING, NODE),
        gl.Binding(ident, second_find, "tests/test_other.py::test_two"),
    ]


@pytest.mark.parametrize("mutate", [
    "no-resolves",            # today's ordinary candidate
    "resolves-not-an-id",     # the existing fixture value "x"
    "proposal-absent",        # names a proposal that was never written
    "proposal-garbage",       # the file exists but is not JSON
    "no-answers-finding",     # a proposal that predates the closure contract
    "no-failing-test",        # half a chain
    "failing-test-not-a-dict",
    "node-id-not-a-node",     # a bare file, not <file>::<test>
    "node-id-with-a-newline",  # would smuggle a second binding into BOUND_TESTS
    "node-id-leading-dash",   # the shell reads it as a missing value
    "finding-not-an-id",
])
def test_a_partial_or_malformed_chain_is_no_binding_and_still_eligible(tmp_path, mutate):
    """Closure is OPT-IN. Nothing about a broken chain may make a candidate
    ineligible — a routine fix must never fail to LAND because someone wrote a
    bad failing_test block."""
    repo = _init_repo(tmp_path / "repo")
    m0 = _git(repo, "rev-parse", "HEAD")
    _commit_on(repo, "cand-c", m0, "c.txt", "CCC\n")
    loops_root = tmp_path / "loops"
    resolves: object = PROPOSAL

    if mutate == "no-resolves":
        resolves = None
    elif mutate == "resolves-not-an-id":
        resolves = "x"
    elif mutate == "proposal-absent":
        pass                                   # simply never write it
    elif mutate == "proposal-garbage":
        (loops_root / "proposals").mkdir(parents=True, exist_ok=True)
        (loops_root / "proposals" / f"{PROPOSAL.replace(':', '_', 1)}.json").write_text(
            "{not json", encoding="utf-8")
    elif mutate == "no-answers-finding":
        _write_proposal(loops_root, answers_finding=None)
    elif mutate == "no-failing-test":
        _write_proposal(loops_root, node_id=None)
    elif mutate == "failing-test-not-a-dict":
        _write_proposal(loops_root, failing_test="tests/test_x.py::test_y")
    elif mutate == "node-id-not-a-node":
        _write_proposal(loops_root, node_id="pipeline/tests/test_publish_queue.py")
    elif mutate == "node-id-with-a-newline":
        _write_proposal(loops_root, node_id=f"{NODE}\ntests/other.py::test_z")
    elif mutate == "node-id-leading-dash":
        _write_proposal(loops_root, node_id="-m")
    elif mutate == "finding-not-an-id":
        _write_proposal(loops_root, answers_finding="the flaky one")

    ident = _write_candidate(loops_root, "c" * 64, "cand-c", m0, ["c.txt"],
                             resolves=resolves)
    loop, cands = _loaded(loops_root, repo, tmp_path)
    assert [c.ident for c in cands] == [ident], "a broken chain must not cost eligibility"
    assert loop.bindings.get(ident, []) == []


def test_bindings_are_re_resolved_from_the_queue_on_every_load(tmp_path):
    """Bindings are held per-TICK, not accumulated. `run_once` re-loads
    candidates before it reads any gate result, so a train judged this tick has
    fresh bindings — and a chain that has since been withdrawn cannot be spent on
    a later merge. A daemon that cached them would close findings on the strength
    of a proposal that no longer says so."""
    repo = _init_repo(tmp_path / "repo")
    m0 = _git(repo, "rev-parse", "HEAD")
    _commit_on(repo, "cand-c", m0, "c.txt", "CCC\n")
    loops_root = tmp_path / "loops"
    _write_proposal(loops_root)
    ident = _write_candidate(loops_root, "c" * 64, "cand-c", m0, ["c.txt"],
                             resolves=PROPOSAL)
    loop = _make_loop(loops_root, repo, _fake_offload(tmp_path))
    loop.load_candidates(set())
    assert loop.bindings[ident]

    (loops_root / "proposals" / f"{PROPOSAL.replace(':', '_', 1)}.json").unlink()
    loop.load_candidates(set())
    assert loop.bindings == {}


def test_resolves_can_never_walk_out_of_the_proposals_directory(tmp_path):
    """`_stem` alone would happily build `proposals/../candidates/x.json`."""
    repo = _init_repo(tmp_path / "repo")
    m0 = _git(repo, "rev-parse", "HEAD")
    _commit_on(repo, "cand-c", m0, "c.txt", "CCC\n")
    loops_root = tmp_path / "loops"
    _write_proposal(loops_root)
    (loops_root / "elsewhere").mkdir(parents=True, exist_ok=True)
    ident = _write_candidate(loops_root, "c" * 64, "cand-c", m0, ["c.txt"],
                             resolves="../elsewhere/evil")
    loop, cands = _loaded(loops_root, repo, tmp_path)
    assert len(cands) == 1 and loop.bindings.get(ident, []) == []


@pytest.mark.parametrize("bad", ["", "   ", "tests/test_x.py", "-m", "a::b\nc::d",
                                 "::test_x", "tests/test_x.py::", 42, None])
def test_valid_node_id_refuses_everything_the_gate_would_refuse(bad):
    assert gl._valid_node_id(bad) is None


def test_valid_node_id_accepts_a_parametrised_node():
    node = "pipeline/tests/test_x.py::test_y[param with spaces]"
    assert gl._valid_node_id(f"  {node}  ") == node


# ------------------------------------------------- 2. flag threading (§5.5.1)


def test_local_gate_command_repeats_the_flag_once_per_binding():
    cmd = local_gate_command(Path("/ws"), "cand-x", "/tmp/r.json",
                             ["a.py::t1", "b.py::t2"])
    assert cmd.count("--bound-test") == 2, "store-not-append grades N-1 members blind"
    assert cmd[cmd.index("--bound-test") + 1] == "a.py::t1"
    assert cmd[-1] == "b.py::t2"
    # absent by default: an unbound train's argv is byte-identical to today's
    assert "--bound-test" not in local_gate_command(Path("/ws"), "c", "/r.json")


def test_remote_gate_command_mirrors_the_bound_test_flag():
    from bridge.gate_host import remote_gate_command

    inner = remote_gate_command(
        "twin", workspace="/ws", candidate="cand-x", receipt="/r.json",
        evidence_root="/ev", bound_tests=["a.py::t1", "b.py::t2 [x]"])[-1]
    assert inner.count("--bound-test") == 2
    assert "'b.py::t2 [x]'" in inner, "a node id with spaces must survive the shell"
    plain = remote_gate_command("twin", workspace="/ws", candidate="c",
                                receipt="/r.json", evidence_root="/ev")[-1]
    assert "--bound-test" not in plain


def _dispatch_argv(tmp_path, monkeypatch, *, gate_ws: Path, resolves: object = PROPOSAL,
                   write_proposal: bool = True) -> tuple[list[str], GateLoop]:
    repo = _init_repo(tmp_path / "repo")
    m0 = _git(repo, "rev-parse", "HEAD")
    _commit_on(repo, "cand-c", m0, "c.txt", "CCC\n")
    loops_root = tmp_path / "loops"
    if write_proposal:
        _write_proposal(loops_root)
    _write_candidate(loops_root, "c" * 64, "cand-c", m0, ["c.txt"], resolves=resolves)

    captured: list[list[str]] = []
    real_popen = gl.subprocess.Popen

    class _FakeProc:
        pid = 909

    def _fake_popen(argv, *a, **kw):
        if isinstance(argv, list) and "run-gate" in argv:
            captured.append(argv)
            return _FakeProc()
        return real_popen(argv, *a, **kw)

    monkeypatch.setattr(gl.subprocess, "Popen", _fake_popen)
    loop = _make_loop(loops_root, repo, _fake_offload(tmp_path), gate_ws=gate_ws)
    loop.run_once()
    assert captured, "a gate child should have been dispatched"
    return captured[0], loop


def test_bound_tests_reach_the_gate_child_when_the_script_supports_the_flag(
        tmp_path, monkeypatch):
    gate_ws = _binding_gate_workspace(tmp_path)
    argv, loop = _dispatch_argv(tmp_path, monkeypatch, gate_ws=gate_ws)
    assert argv.count("--bound-test") == 1
    assert argv[argv.index("--bound-test") + 1] == NODE
    st = json.loads(next((tmp_path / "loops" / "state" / "gates").glob("*.json")).read_text())
    assert st["bound_tests"] == [NODE]


def test_bound_tests_are_withheld_when_the_pinned_script_lacks_the_flag(
        tmp_path, monkeypatch):
    """THE safety gate. The shell half is a separate candidate; handing an old
    script an unknown flag refuses `unknown-flag` at exit 2 — an instrument error
    that costs the LANDING, not just the closure."""
    gate_ws = _fake_gate_workspace(tmp_path)          # no --bound-test in its text
    argv, loop = _dispatch_argv(tmp_path, monkeypatch, gate_ws=gate_ws)
    assert "--bound-test" not in argv
    st = json.loads(next((tmp_path / "loops" / "state" / "gates").glob("*.json")).read_text())
    assert st["bound_tests"] == []
    assert any("bound-test flags WITHHELD" in ln for ln in loop.lines), loop.lines


def test_an_unbound_train_dispatches_exactly_as_before(tmp_path, monkeypatch):
    gate_ws = _binding_gate_workspace(tmp_path)
    argv, loop = _dispatch_argv(tmp_path, monkeypatch, gate_ws=gate_ws,
                                resolves="x", write_proposal=False)
    assert "--bound-test" not in argv
    assert not any("WITHHELD" in ln for ln in loop.lines)


def test_gate_supports_bound_test_reads_the_blob_at_a_ref(tmp_path):
    """The twin runs the script at the MERGE BASE, which can be older than the
    local pinned workspace — so the remote probe must ask the commit, not the
    working file."""
    repo = _init_repo(tmp_path / "repo")
    (repo / "scripts").mkdir()
    (repo / "scripts" / "merge-gate.sh").write_text("echo old\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "old gate")
    old = _git(repo, "rev-parse", "HEAD")
    (repo / "scripts" / "merge-gate.sh").write_text(
        "case $1 in --bound-test) :;; esac\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "new gate")
    new = _git(repo, "rev-parse", "HEAD")

    assert gl.gate_supports_bound_test(repo) is True          # working tree
    assert gl.gate_supports_bound_test(repo, ref=new) is True
    assert gl.gate_supports_bound_test(repo, ref=old) is False
    assert gl.gate_supports_bound_test(repo, ref="deadbeef" * 5) is False
    assert gl.gate_supports_bound_test(tmp_path / "nope") is False


def test_the_child_drops_a_malformed_bound_test_before_it_reaches_the_gate(tmp_path):
    """argv is a boundary too: a value that would make merge-gate.sh `refuse()`
    is dropped, because a bad binding must cost the closure, never the gate."""
    sf = tmp_path / "state.json"
    receipt = tmp_path / "r.json"
    rc = run_gate_child([
        "--mode", "offload", "--state-file", str(sf), "--candidate", "c",
        "--tip", "a" * 40, "--receipt", str(receipt),
        "--offload", _fake_offload(tmp_path),
        "--bound-test", "not-a-node", "--bound-test", NODE])
    assert rc == 0
    assert json.loads(sf.read_text())["bound_tests"] == [NODE]


# --------------------------------------------- 3. the `closed` emit (§5.5.4)


def _closed_events(events: list[dict]) -> list[dict]:
    return [e for e in events if e["event"] == "closed"]


def test_closed_is_emitted_on_the_finding_when_the_bound_test_is_green(tmp_path):
    loops_root, events, loop, cand_id = _land_with_binding(tmp_path, bound_result="green")
    closed = _closed_events(events)
    assert len(closed) == 1, [e["event"] for e in events]
    ev = closed[0]
    assert ev["id"] == FINDING, "closed is emitted on the FINDING, never the candidate"
    assert ev["actor"] == "gate-loop-daemon"
    assert ev["detail"]["closed_by"] == cand_id
    assert ev["detail"]["bound_test"] == NODE
    assert ev["detail"]["merge_sha"] == _git(tmp_path / "repo", "rev-parse", "main")
    assert ev["detail"]["receipt"]
    # the candidate still terminalises the ordinary way, on its own id
    merged = [e for e in events if e["event"] == "merged"]
    assert [e["id"] for e in merged] == [cand_id]


def test_the_emitted_closed_event_validates_against_the_ledger_schema(tmp_path):
    import jsonschema

    _, events, _loop, _cand = _land_with_binding(tmp_path, bound_result="green")
    schema = json.loads((ROOT / "schema" / "ledger-event.schema.json").read_text())
    ev = _closed_events(events)[0]
    jsonschema.validate(ev, schema)                     # must not raise


def test_closed_is_withheld_when_the_receipt_lacks_bound_test_result(tmp_path):
    """An older gate reports nothing about the binding. A `closed` minted on an
    unmeasured test is the worst outcome of the whole closure plan."""
    _, events, loop, _cand = _land_with_binding(tmp_path, bound_result=None)
    assert _closed_events(events) == []
    assert any("closed-withheld: gate did not report bound_test_result" in ln
               for ln in loop.lines), loop.lines


@pytest.mark.parametrize("result", ["red", "weakened"])
def test_closed_is_withheld_when_the_bound_test_did_not_pass(tmp_path, result):
    _, events, loop, _cand = _land_with_binding(tmp_path, bound_result=result)
    assert _closed_events(events) == []
    assert any(f"bound_test_result={result}" in ln for ln in loop.lines), loop.lines


def test_closed_is_withheld_when_the_gate_never_received_the_binding(tmp_path):
    """The pinned script does not take the flag, so the run receipt names no
    node. A green from a run that was never told what it was closing closes
    nothing."""
    _, events, loop, _cand = _land_with_binding(
        tmp_path, bound_result="green", gate_ws_binds=False)
    assert _closed_events(events) == []


def test_a_green_run_closes_only_the_findings_whose_nodes_it_ran(tmp_path):
    """Worst-wins green covers the nodes the receipt LISTS; a binding absent from
    that list borrows another member's proof."""
    repo = _init_repo(tmp_path / "repo")
    m0 = _git(repo, "rev-parse", "HEAD")
    loop = _make_loop(tmp_path / "loops", repo, _fake_offload(tmp_path))
    receipt = tmp_path / "run.json"
    receipt.write_text(json.dumps({"bound_test": [NODE], "bound_test_result": "green"}))
    other_find = "sha256:" + "e" * 64
    member = "sha256:" + "a" * 64
    loop.bindings = {member: [gl.Binding(member, FINDING, NODE),
                              gl.Binding(member, other_find, "tests/x.py::test_ghost")]}
    train = _manual_train(repo, m0)
    verdict = gl.GateVerdict("pass", 0, "", "gate passed", receipt, "", 1.0)
    loop._emit_closures(train, verdict, "d" * 40, "receipts/land.json")

    closed = _closed_events(_ledger_events(tmp_path / "loops"))
    assert [e["id"] for e in closed] == [FINDING]
    assert any("not in the run receipt's bound_test list" in ln for ln in loop.lines)


def test_a_finding_that_is_already_terminal_is_never_closed_twice(tmp_path):
    """`ledger.exactly_one_terminal_event` refuses a second terminal on one id."""
    repo = _init_repo(tmp_path / "repo")
    m0 = _git(repo, "rev-parse", "HEAD")
    loops_root = tmp_path / "loops"
    loops_root.mkdir(parents=True, exist_ok=True)
    (loops_root / "ledger.jsonl").write_text(json.dumps({
        "ts": "2026-08-10T00:00:00Z", "role": "implementer", "event": "closed",
        "id": FINDING, "actor": "gate-loop-daemon",
        "detail": {"closed_by": "sha256:" + "b" * 64, "merge_sha": "a" * 40,
                   "bound_test": NODE}}) + "\n", encoding="utf-8")
    loop = _make_loop(loops_root, repo, _fake_offload(tmp_path))
    receipt = tmp_path / "run.json"
    receipt.write_text(json.dumps({"bound_test": [NODE], "bound_test_result": "green"}))
    member = "sha256:" + "a" * 64
    loop.bindings = {member: [gl.Binding(member, FINDING, NODE)]}
    loop._emit_closures(_manual_train(repo, m0),
                        gl.GateVerdict("pass", 0, "", "ok", receipt, "", 1.0),
                        "d" * 40, "receipts/land.json")
    assert len(_closed_events(_ledger_events(loops_root))) == 1
    assert any("already has a terminal event" in ln for ln in loop.lines)


@pytest.mark.parametrize("receipt_body", ["", "{not json", '{"bound_test_result": "GREEN"}',
                                          '{"bound_test_result": true}', "[]"])
def test_an_unreadable_or_unrecognised_result_never_closes(tmp_path, receipt_body):
    repo = _init_repo(tmp_path / "repo")
    m0 = _git(repo, "rev-parse", "HEAD")
    loops_root = tmp_path / "loops"
    loop = _make_loop(loops_root, repo, _fake_offload(tmp_path))
    receipt = tmp_path / "run.json"
    receipt.write_text(receipt_body, encoding="utf-8")
    member = "sha256:" + "a" * 64
    loop.bindings = {member: [gl.Binding(member, FINDING, NODE)]}
    loop._emit_closures(_manual_train(repo, m0),
                        gl.GateVerdict("pass", 0, "", "ok", receipt, "", 1.0),
                        "d" * 40, "receipts/land.json")
    assert _closed_events(_ledger_events(loops_root)) == []


def test_a_nested_payload_receipt_is_read_too(tmp_path):
    """merge-gate.sh mints the run receipt FLAT; the plan's acceptance snippet
    reads a nested `payload`. Accept both, so a future wrapper cannot silently
    turn every green into a withheld closure."""
    receipt = tmp_path / "run.json"
    receipt.write_text(json.dumps({"payload": {"bound_test": [NODE],
                                               "bound_test_result": "green"}}))
    assert gl._receipt_fields(receipt)["bound_test_result"] == "green"
    assert gl._receipt_fields(tmp_path / "absent.json") is None


# ===================================== ON-MERGE PROPOSAL TERMINALIZATION
# A landed candidate's ``payload.resolves`` proposal must stop being offered
# to builders once the candidate is actually on `main` -- otherwise the
# governor keeps handing out already-landed work forever (the measured root
# cause of the proposal-queue bloat).


def test_a_merged_candidates_resolved_proposal_gets_a_terminal_event(tmp_path):
    repo = _init_repo(tmp_path / "repo")
    m0 = _git(repo, "rev-parse", "HEAD")
    _commit_on(repo, "cand-r", m0, "r.txt", "RRR\n")
    loops_root = tmp_path / "loops"
    prop_id = "sha256:" + "6" * 64
    _write_proposal(loops_root, prop_id, answers_finding=None, node_id=None,
                    failing_test=None)
    _write_candidate(loops_root, content_id({"resolves": prop_id})[7:],
                     "cand-r", m0, ["r.txt"], resolves=prop_id)

    offload = _fake_offload(tmp_path)
    loop = _make_loop(loops_root, repo, offload)
    out1 = loop.run_once()
    assert out1[0].action == "dispatched", out1
    _wait_done(gate_state_path(loops_root, out1[0].train))
    loop2 = _make_loop(loops_root, repo, offload)
    out2 = loop2.run_once()
    assert out2[0].action == "landed", (out2[0].action, out2[0].detail, loop2.lines)

    events = _ledger_events(loops_root)
    retirements = [ev for ev in events if ev.get("id") == prop_id]
    assert retirements, "no terminal event was ever written for the resolved proposal"
    assert retirements[-1]["event"] == "completed", retirements

    view = LedgerView.build(loops_root)
    assert prop_id in view.terminal, "the resolved proposal is still selectable"


def test_a_non_merged_candidates_resolved_proposal_is_not_retired(tmp_path):
    repo = _init_repo(tmp_path / "repo")
    m0 = _git(repo, "rev-parse", "HEAD")
    _commit_on(repo, "cand-r2", m0, "r2.txt", "RRR2\n")
    loops_root = tmp_path / "loops"
    prop_id = "sha256:" + "5" * 64
    _write_proposal(loops_root, prop_id, answers_finding=None, node_id=None,
                    failing_test=None)
    _write_candidate(loops_root, "e" * 64, "cand-r2", m0, ["r2.txt"],
                     resolves=prop_id)

    offload = _fake_offload(tmp_path)
    os.environ["FAKE_GATE_RC"] = "2"
    os.environ["FAKE_GATE_SLUG"] = "secrets"
    try:
        loop = _make_loop(loops_root, repo, offload)
        out1 = loop.run_once()
        assert out1[0].action == "dispatched", out1
        _wait_done(gate_state_path(loops_root, out1[0].train))
        loop2 = _make_loop(loops_root, repo, offload)
        out2 = loop2.run_once()
        assert out2[0].action == "rejected", (out2[0].action, out2[0].detail)
    finally:
        os.environ.pop("FAKE_GATE_RC", None)
        os.environ.pop("FAKE_GATE_SLUG", None)

    events = _ledger_events(loops_root)
    assert not any(ev.get("id") == prop_id for ev in events), (
        "a proposal was retired for a candidate that never merged")
    view = LedgerView.build(loops_root)
    assert prop_id not in view.terminal


def test_the_write_boundary_refuses_a_closed_without_full_provenance(tmp_path):
    repo = _init_repo(tmp_path / "repo")
    loop = _make_loop(tmp_path / "loops", repo, _fake_offload(tmp_path))
    good = {"ts": "2026-08-10T00:00:00Z", "role": "implementer", "event": "closed",
            "id": FINDING, "actor": "gate-loop-daemon",
            "detail": {"closed_by": "sha256:" + "b" * 64, "merge_sha": "a" * 40,
                       "bound_test": NODE}}
    loop._append_ledger(dict(good))                       # the complete event writes
    for strip in ("closed_by", "merge_sha", "bound_test"):
        bad = json.loads(json.dumps(good))
        del bad["detail"][strip]
        with pytest.raises(ValueError):
            loop._append_ledger(bad)
    bad = json.loads(json.dumps(good))
    del bad["id"]
    with pytest.raises(ValueError):
        loop._append_ledger(bad)
    assert len(_closed_events(_ledger_events(tmp_path / "loops"))) == 1


def test_landing_still_completes_when_a_closure_cannot_be_written(tmp_path, monkeypatch):
    """The closure is the last mile, not the landing's precondition."""
    repo = _init_repo(tmp_path / "repo")
    m0 = _git(repo, "rev-parse", "HEAD")
    loops_root = tmp_path / "loops"
    loop = _make_loop(loops_root, repo, _fake_offload(tmp_path))
    receipt = tmp_path / "run.json"
    receipt.write_text(json.dumps({"bound_test": [NODE], "bound_test_result": "green"}))
    member = "sha256:" + "a" * 64
    loop.bindings = {member: [gl.Binding(member, "not-an-id", NODE)]}
    # a finding id that is not id-shaped never resolves in practice; force the
    # boundary refusal directly to prove it degrades to a log, not an exception.
    loop.bindings = {member: [gl.Binding(member, FINDING, NODE)]}
    real = loop._append_ledger

    def _boom(event):
        if event.get("event") == "closed":
            raise ValueError("refused to write 'closed' missing ['bound_test']")
        return real(event)

    monkeypatch.setattr(loop, "_append_ledger", _boom)
    loop._emit_closures(_manual_train(repo, m0),
                        gl.GateVerdict("pass", 0, "", "ok", receipt, "", 1.0),
                        "d" * 40, "receipts/land.json")
    assert any("closed-withheld" in ln for ln in loop.lines), loop.lines


# ------------------------------------ 4. defect enrichment on a binding refusal


def _defect_verdict(reason: str, slug: str = "verdict", rc: int = 1,
                    receipt: Path | None = None):
    return gl.GateVerdict("candidate-defect", rc, slug, reason, receipt, "", 2.0)


def test_a_bound_test_refusal_names_the_test_and_the_remedy(tmp_path):
    repo = _init_repo(tmp_path / "repo")
    m0 = _git(repo, "rev-parse", "HEAD")
    loops_root = tmp_path / "loops"
    loop = _make_loop(loops_root, repo, _fake_offload(tmp_path))
    train = _manual_train(repo, m0)
    member = train.members[0]["id"]
    loop.bindings = {member: [gl.Binding(member, FINDING, NODE)]}
    loop.on_candidate_defect(train, _defect_verdict(
        f"gate verdict FAIL: bound-test: {NODE} did not execute: nothing ran"))

    rej = [e for e in _ledger_events(loops_root) if e["event"] == "rejected"]
    assert len(rej) == 1
    detail = rej[0]["detail"]
    assert detail["bound_test"] == [NODE]
    assert detail["bound_test_refusal"] == "bound-test"
    assert detail["remedy"] == "fix the named cause; do not edit the bound test"
    assert detail["class"] == "candidate-defect" and detail["expires_at"]
    on_disk = json.loads(
        (loops_root / "rejected" / f"{member.replace(':', '_', 1)}.json").read_text())
    assert on_disk["detail"]["bound_test"] == [NODE]
    assert on_disk["detail"]["remedy"].startswith("fix the named cause")


def test_bound_test_untouched_is_recognised_from_the_verdict_reason(tmp_path):
    """`fail` exits 1, so the NAME only ever survives inside the reason —
    matching on the slug alone would never fire on a real refusal."""
    repo = _init_repo(tmp_path / "repo")
    m0 = _git(repo, "rev-parse", "HEAD")
    loop = _make_loop(tmp_path / "loops", repo, _fake_offload(tmp_path))
    train = _manual_train(repo, m0)
    member = train.members[0]["id"]
    loop.bindings = {member: [gl.Binding(member, FINDING, NODE)]}
    loop.on_candidate_defect(train, _defect_verdict(
        "gate verdict FAIL: ruff-new: 2 new findings; bound-test-untouched: candidate "
        "edits its own bound test file pipeline/tests/test_publish_queue.py"))
    detail = [e for e in _ledger_events(tmp_path / "loops")
              if e["event"] == "rejected"][0]["detail"]
    assert detail["bound_test_refusal"] == "bound-test-untouched"
    assert detail["remedy"] == "fix the named cause; do not edit the bound test"


def test_an_ordinary_refusal_keeps_the_replan_remedy(tmp_path):
    repo = _init_repo(tmp_path / "repo")
    m0 = _git(repo, "rev-parse", "HEAD")
    loop = _make_loop(tmp_path / "loops", repo, _fake_offload(tmp_path))
    train = _manual_train(repo, m0)
    member = train.members[0]["id"]
    loop.bindings = {member: [gl.Binding(member, FINDING, NODE)]}
    loop.on_candidate_defect(train, _defect_verdict(
        "gate: secrets — a live key shape reached the diff", slug="secrets", rc=2))
    detail = [e for e in _ledger_events(tmp_path / "loops")
              if e["event"] == "rejected"][0]["detail"]
    assert detail["remedy"] == "replan"
    assert "bound_test" not in detail, "prose mentioning a test must not enrich"


def test_bound_test_refusal_detection_is_field_wise_not_substring():
    assert gl._bound_test_refusal(
        _defect_verdict("gate verdict FAIL: ladder: a bound-test helper is flaky")) is None
    assert gl._bound_test_refusal(
        _defect_verdict("gate verdict FAIL: bound-test: red")) == "bound-test"
    assert gl._bound_test_refusal(
        _defect_verdict("gate: bad-bound-test — names no test", slug="bad-bound-test",
                        rc=2)) == "bad-bound-test"
# --- cross-lineage review 2026-08-10, blocker 2 / ultra I4'+I8: lease parser + reap


def _running_state(gdir, name, *, mode="remote", twin=None, deadline_in=3600,
                   pid=None, pid_started=None, train=None, tip=None):
    gdir.mkdir(parents=True, exist_ok=True)
    st = {"state": "running", "mode": mode, "deadline": time.time() + deadline_in}
    if twin is not None:
        st["twin"] = twin
    if pid is not None:
        st["pid"] = pid
    if pid_started is not None:
        st["pid_started"] = pid_started
    if train is not None:
        st["train"] = train
    if tip is not None:
        st["tip"] = tip
    (gdir / f"{name}.json").write_text(json.dumps(st))
    return gdir / f"{name}.json"


def _spawn_session_sleep(seconds: int = 300) -> subprocess.Popen:
    """A throwaway child in its own session (pgid == pid), like a gate child."""
    return subprocess.Popen(
        ["sleep", str(seconds)],
        start_new_session=True,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        stdin=subprocess.DEVNULL,
    )


def _pid_alive(pid: int) -> bool:
    """True if *pid* is a live (non-zombie) process.

    ``os.kill(pid, 0)`` still succeeds for zombies — after killpg the child is
    often a zombie until waitpid, so a bare existence check would false-positive
    "still alive" and fail the reap assertions.
    """
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    # Reap our own zombie if present; if waitpid collects it, it is gone.
    try:
        wpid, _status = os.waitpid(pid, os.WNOHANG)
        if wpid == pid:
            return False
    except ChildProcessError:
        pass
    # Fall back: /bin/ps state (R/S/D = live; Z = zombie).
    try:
        proc = subprocess.run(
            ["ps", "-p", str(pid), "-o", "state="],
            capture_output=True, text=True, check=False, timeout=2)
        if proc.returncode != 0:
            return False
        state = (proc.stdout or "").strip()
        if not state:
            return False
        if state[0].upper() == "Z":
            return False
        return True
    except (OSError, subprocess.TimeoutExpired):
        # Probe blocked: treat kill(0) success as "maybe alive".
        return True


def test_a_legacy_running_remote_state_still_holds_its_box(tmp_path):
    """The rolling-upgrade bug: gates dispatched BEFORE the pool existed carry no
    "twin" field. Read as 'no box claimed', the first tick after deploy sends a
    second gate to MW0001 — the box already gating — which is precisely the
    overload this scheduler exists to prevent."""
    loop = _make_loop(tmp_path / "loops", _init_repo(tmp_path / "repo"),
                      _fake_offload(tmp_path), allow_remote_gate=True)
    _running_state(loop.root / "state" / "gates", "legacy", twin=None)
    assert loop._twins_in_flight() == {gl.TWIN_HOST}, \
        "a pre-pool remote gate always meant TWIN_HOST; absence is not freedom"
    # GateLeases unit surface for the same legacy fact (I4').
    leases = loop._read_gate_leases()
    assert isinstance(leases, gl.GateLeases)
    assert leases.running == 1
    assert leases.twins == {gl.TWIN_HOST}
    assert leases.corrupt is False


def test_gate_leases_legacy_remote_without_twin_maps_to_twin_host(tmp_path):
    """Unit: GateLeases maps pre-pool remote (no twin field) → TWIN_HOST."""
    loop = _make_loop(tmp_path / "loops", _init_repo(tmp_path / "repo"),
                      _fake_offload(tmp_path), allow_remote_gate=True)
    gdir = loop.root / "state" / "gates"
    # twin=None means the helper omits the field entirely (legacy shape).
    _running_state(gdir, "legacy-no-twin", mode="remote", twin=None)
    leases = loop._read_gate_leases()
    assert leases.running == 1
    assert leases.twins == {gl.TWIN_HOST}
    assert loop._running_gate_count() == 1
    assert loop._twins_in_flight() == {gl.TWIN_HOST}


def test_an_unreadable_state_file_is_not_read_as_a_free_box(tmp_path):
    """Favourable absence: a state file we cannot parse may be a running remote
    gate. Skipping it double-books a box; claiming the pool costs one tick.

    Frozen I4': corrupt is busy BOTH directions (twins + running ≥ 1) for the
    bound window. Prior semantics only asserted twins; running now also fails
    closed so run_once cannot dispatch a local gate beside a corrupt record
    that may BE the already-running local gate (F-B1).
    """
    loop = _make_loop(tmp_path / "loops", _init_repo(tmp_path / "repo"),
                      _fake_offload(tmp_path), allow_remote_gate=True)
    gdir = loop.root / "state" / "gates"
    gdir.mkdir(parents=True, exist_ok=True)
    (gdir / "corrupt.json").write_text("{not json")
    assert loop._twins_in_flight() == {s.host for s in gl.TWIN_SPECS}
    assert loop._running_gate_count() >= 1, \
        "corrupt state must fail-closed on the slot count too (F-B1)"
    leases = loop._read_gate_leases()
    assert leases.corrupt is True


def test_an_expired_remote_gate_does_not_silently_release_the_twin(tmp_path, monkeypatch):
    """Expiry detection and reap are exercised through run_once, not a helper."""
    repo = _init_repo(tmp_path / "repo")
    m0 = _git(repo, "rev-parse", "HEAD")
    loop = _make_loop(tmp_path / "loops", repo, _fake_offload(tmp_path),
                      allow_remote_gate=True)
    train = _manual_train(repo, m0)
    child = _spawn_session_sleep(300)
    try:
        # Environment detection probes THIS test's own PID.  Once it works,
        # the child probe is a hard assertion: a broken probe must turn red.
        if shutil.which("ps") is None:
            pytest.skip("ps is absent")
        if os.environ.get("CODEX_SANDBOX") == "seatbelt":
            pytest.skip("explicit seatbelt sandbox marker denies ps")
        direct_probe = subprocess.run(
            ["/bin/ps", "-p", str(os.getpid()), "-o", "lstart="],
            capture_output=True, text=True, check=False,
        )
        if direct_probe.returncode != 0 or not (direct_probe.stdout or "").strip():
            pytest.skip("sandbox did not expose real ps lstart for spawned child")
        pid_started = gl._pid_lstart(child.pid)
        assert pid_started is not None, "real lstart probe must identify spawned child"
        # Write under the real gate_state_path so on_instrument_error finds it.
        sf = gate_state_path(loop.root, train)
        sf.parent.mkdir(parents=True, exist_ok=True)
        sf.write_text(json.dumps({
            "state": "running", "mode": "remote", "twin": "mw0002",
            "deadline": time.time() - 10,  # already expired
            "pid": child.pid, "pid_started": pid_started,
            "train": train.branch, "tip": train.tip, "self_bounded": True,
        }))
        # (1) twin stays busy until reap
        assert "mw0002" in loop._twins_in_flight()
        assert loop._running_gate_count() >= 1

        # run_once must detect the expiry, classify it, and invoke the reap
        # path.  Removing expiry detection from run_once therefore breaks this.
        cand = _cand("e" * 64, "cand/e", m0)
        monkeypatch.setattr(loop, "load_candidates", lambda _terminal: [cand])
        monkeypatch.setattr(loop, "_reconcile_already_merged", lambda cands, _sha: cands)
        monkeypatch.setattr(loop, "_open_builder", lambda: tmp_path / "builder")
        monkeypatch.setattr(loop, "_close_builder", lambda: None)
        monkeypatch.setattr(gl, "assemble_trains",
                            lambda *_args, **_kw: ([train], []))
        monkeypatch.setattr(gl, "is_ancestor_of_main", lambda *_args: True)
        out = loop.run_once()
        assert out and out[0].action == "instrument"

        # (2) child process is GONE after reap (poll covers zombies).
        deadline = time.time() + 5
        while time.time() < deadline and child.poll() is None:
            time.sleep(0.05)
        assert child.poll() is not None, \
            f"reap must terminate the child process group (poll={child.poll()})"

        # (3) state file unlinked (re-gate once path)
        assert not sf.exists(), "instrument re-gate must unlink after reap"

        # (4) twin free only after reap
        assert "mw0002" not in loop._twins_in_flight()
        assert loop._running_gate_count() == 0

        events = _ledger_events(loop.root)
        inst = [e for e in events if e.get("event") == "instrument_error"]
        assert inst, "instrument_error ledger event required"
        assert inst[-1]["detail"].get("reap") == "reaped", inst[-1]["detail"]
    finally:
        if child.poll() is None:
            try:
                os.killpg(child.pid, 9)
            except (ProcessLookupError, PermissionError, OSError):
                pass
        try:
            child.wait(timeout=2)
        except Exception:
            pass


def test_second_instrument_error_reaps_still_running_child(tmp_path, monkeypatch):
    """A second instrument failure parks only after reaping its live child."""
    repo = _init_repo(tmp_path / "repo")
    m0 = _git(repo, "rev-parse", "HEAD")
    loop = _make_loop(tmp_path / "loops", repo, _fake_offload(tmp_path),
                      allow_remote_gate=True)
    train = _manual_train(repo, m0)
    sf = gate_state_path(loop.root, train)
    sf.parent.mkdir(parents=True, exist_ok=True)
    child = _spawn_session_sleep(300)
    fixed_lstart = "Mon Aug 10 12:00:00 2026"

    def _lstart(pid: int):
        return fixed_lstart if pid == child.pid and _pid_alive(pid) else None

    monkeypatch.setattr(gl, "_pid_lstart", _lstart)
    try:
        # A prior recorded instrument error consumes the one permitted re-gate.
        # A pid-less running state must now park, never be unlinked to manufacture
        # that history (R1/R2).
        loop._append_ledger({
            "ts": "2026-08-10T00:00:00Z", "role": "test", "event": "instrument_error",
            "id": None, "actor": "test",
            "detail": {"train_key": f"{train.branch}@{train.tip}"},
        })

        # The re-gated attempt has a live session-leader child when it fails.
        sf.write_text(json.dumps({
            "state": "running", "mode": "remote", "twin": "mw0002",
            "deadline": time.time() - 10, "pid": child.pid,
            "pid_started": fixed_lstart, "train": train.branch, "tip": train.tip,
            "self_bounded": True,
        }))
        verdict = read_gate_verdict(sf)
        assert verdict is not None and verdict.result == "instrument-error"
        out = loop.on_instrument_error(train, verdict)
        assert out.action == "instrument"

        deadline = time.time() + 5
        while time.time() < deadline and child.poll() is None:
            time.sleep(0.05)
        assert child.poll() is not None, "second instrument failure must reap before parking"
        parked = json.loads(sf.read_text())
        assert parked["disposition"] == "instrument-parked"
    finally:
        if child.poll() is None:
            try:
                os.killpg(child.pid, 9)
            except (ProcessLookupError, PermissionError, OSError):
                pass
        try:
            child.wait(timeout=2)
        except Exception:
            pass


def test_sigterm_ignoring_child_sigkill_escalation_is_reaped(tmp_path, monkeypatch):
    """A kernel-accepted SIGKILL is a clean reap, not a zero-settle refusal."""
    loop = _make_loop(tmp_path / "loops", _init_repo(tmp_path / "repo"),
                      _fake_offload(tmp_path), allow_remote_gate=True)
    child = subprocess.Popen(
        [sys.executable, "-c", (
            "import signal,time; "
            "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
            "print('ready', flush=True); time.sleep(300)"
        )],
        start_new_session=True, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        stdin=subprocess.DEVNULL, text=True,
    )
    try:
        assert child.stdout is not None and child.stdout.readline().strip() == "ready"
        pid_started = "real-sigterm-ignoring-child"
        monkeypatch.setattr(gl, "_pid_lstart", lambda pid: (
            pid_started if pid == child.pid and child.poll() is None else None))
        sf = loop.root / "state" / "gates" / "sigkill-escalation.json"
        sf.parent.mkdir(parents=True, exist_ok=True)
        state = {
            "state": "running", "mode": "remote", "twin": "mw0002",
            "pid": child.pid, "pid_started": pid_started,
        }
        sf.write_text(json.dumps(state))

        assert loop._reap_gate_child(state, sf, reason="sigkill-test") == "reaped"
        child.wait(timeout=5)
        recorded = json.loads(sf.read_text())
        assert recorded["reap_confirmed"] is True
        confidence = [
            e for e in _ledger_events(loop.root)
            if (e.get("detail") or {}).get("kind") == "reap-confidence-confirmed"
        ]
        assert len(confidence) == 1
        assert confidence[0]["detail"]["reap_confirmed"] is True
    finally:
        if child.poll() is None:
            try:
                os.killpg(child.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError, OSError):
                pass
        try:
            child.wait(timeout=2)
        except Exception:
            pass


def test_reap_permission_error_is_unverifiable(tmp_path, monkeypatch):
    """I8: a refused SIGTERM cannot be reported as a successful reap."""
    loop = _make_loop(tmp_path / "loops", _init_repo(tmp_path / "repo"),
                      _fake_offload(tmp_path), allow_remote_gate=True)
    monkeypatch.setattr(gl, "_pid_lstart", lambda _pid: "known-start")

    def _deny_killpg(_pid: int, _sig: int) -> None:
        raise PermissionError("EPERM")

    monkeypatch.setattr(gl.os, "killpg", _deny_killpg)
    outcome = loop._reap_gate_child(
        {"state": "running", "pid": 424242, "pid_started": "known-start"},
        tmp_path / "state.json", reason="permission-test")
    assert outcome == "unverifiable"
    assert any("unverifiable" in alert for alert in loop.alerts)


def test_pid_publication_failure_terminates_child_and_stamps_terminal_state(
        tmp_path, monkeypatch):
    """R1: failed PID write-back cannot leave a live untracked running lease."""
    repo = _init_repo(tmp_path / "repo")
    loop = _make_loop(tmp_path / "loops", repo, _fake_offload(tmp_path))
    train = _manual_train(repo, _git(repo, "rev-parse", "HEAD"))

    class Child:
        pid = 424242
        terminated = False
        killed = False

        def terminate(self):
            self.terminated = True

        def wait(self, timeout=None):
            if self.terminated and not self.killed:
                raise subprocess.TimeoutExpired("gate", timeout)

        def kill(self):
            self.killed = True

    child = Child()
    monkeypatch.setattr(gl.subprocess, "Popen", lambda *_a, **_kw: child)
    monkeypatch.setattr(gl, "_pid_lstart", lambda _pid: "known-start")
    real_write = loop._write_json_atomic
    calls = 0

    def fail_only_pid_write(path, payload):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("state disk failed")
        real_write(path, payload)

    monkeypatch.setattr(loop, "_write_json_atomic", fail_only_pid_write)
    loop.dispatch_gate(train, allow_remote=False)
    sf = gate_state_path(loop.root, train)
    stamped = json.loads(sf.read_text())
    assert child.terminated and child.killed
    assert stamped["state"] == "closed"
    assert stamped["disposition"] == "pid-publication-failed"
    assert any((e.get("detail") or {}).get("kind") == "pid-publication-failed"
               for e in _ledger_events(loop.root))


def test_remote_pid_publication_failure_park_keeps_its_twin(tmp_path, monkeypatch):
    """A live untracked remote child keeps the exact box it was sent to busy."""
    repo = _init_repo(tmp_path / "repo")
    loop = _make_loop(tmp_path / "loops", repo, _fake_offload(tmp_path),
                      allow_remote_gate=True)
    train = _manual_train(repo, _git(repo, "rev-parse", "HEAD"))
    host = gl.TWIN_SPECS[0].host if gl.TWIN_SPECS else gl.TWIN_HOST

    class UnkillableChild:
        pid = 424242

        def terminate(self):
            raise PermissionError("SIGTERM denied")

        def kill(self):
            raise PermissionError("SIGKILL denied")

        def wait(self, timeout=None):
            raise subprocess.TimeoutExpired("gate-child", timeout)

    child = UnkillableChild()
    monkeypatch.setattr(gl.subprocess, "Popen", lambda *_a, **_kw: child)
    monkeypatch.setattr(gl, "_pid_lstart", lambda _pid: "known-start")
    real_write = loop._write_json_atomic
    calls = 0

    def fail_only_pid_write(path, payload):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("state disk failed")
        real_write(path, payload)

    monkeypatch.setattr(loop, "_write_json_atomic", fail_only_pid_write)
    loop.dispatch_gate(train, allow_remote=True, twin=host)

    sf = gate_state_path(loop.root, train)
    parked = json.loads(sf.read_text())
    assert parked["state"] == "running"
    assert parked["disposition"] == "reap-unverifiable-parked"
    assert parked["mode"] == "remote"
    assert parked["twin"] == host
    assert loop._twins_in_flight() == {host}


def test_unreadable_state_is_held_not_unlinked_by_instrument_regate(tmp_path):
    """R17: corrupt state survives its same-tick status-unreadable verdict."""
    repo = _init_repo(tmp_path / "repo")
    loop = _make_loop(tmp_path / "loops", repo, _fake_offload(tmp_path),
                      allow_remote_gate=True)
    train = _manual_train(repo, _git(repo, "rev-parse", "HEAD"))
    sf = gate_state_path(loop.root, train)
    sf.parent.mkdir(parents=True, exist_ok=True)
    sf.write_text('{"state":"running"')
    verdict = read_gate_verdict(sf)
    assert verdict is not None and verdict.slug == "status-unreadable"
    out = loop.on_instrument_error(train, verdict)
    assert out.action == "instrument"
    assert sf.exists()
    leases = loop._read_gate_leases()
    assert leases.corrupt and leases.running >= 1


def test_legacy_unverifiable_reap_parks_twin_until_bounded_exit(tmp_path, monkeypatch):
    """R2: a pre-self-bound child remains an occupied remote lease."""
    repo = _init_repo(tmp_path / "repo")
    loop = _make_loop(tmp_path / "loops", repo, _fake_offload(tmp_path),
                      allow_remote_gate=True)
    train = _manual_train(repo, _git(repo, "rev-parse", "HEAD"))
    sf = gate_state_path(loop.root, train)
    sf.parent.mkdir(parents=True, exist_ok=True)
    sf.write_text(json.dumps({"state": "running", "mode": "remote", "twin": "mw0002",
                              "deadline": time.time() - 1, "pid": 4242,
                              "pid_started": "old", "train": train.branch, "tip": train.tip}))
    monkeypatch.setattr(gl, "_pid_lstart", lambda _pid: "new")
    out = loop.on_instrument_error(train, read_gate_verdict(sf))
    assert out.action == "instrument"
    stamped = json.loads(sf.read_text())
    assert stamped["state"] == "running"
    assert stamped["disposition"] == "reap-unverifiable-parked"
    assert "mw0002" in loop._twins_in_flight()


def test_deadlineless_running_state_quarantines_after_mtime_bound(tmp_path):
    """R16: malformed deadline has the same finite mtime-based veto exit."""
    loop = _make_loop(tmp_path / "loops", _init_repo(tmp_path / "repo"),
                      _fake_offload(tmp_path), allow_remote_gate=True)
    sf = loop.root / "state" / "gates" / "deadlineless.json"
    sf.parent.mkdir(parents=True, exist_ok=True)
    sf.write_text(json.dumps({"state": "running", "mode": "remote", "twin": "mw0002"}))
    past = time.time() - gl.GATE_DEADLINE_S - 1
    os.utime(sf, (past, past))
    loop._sweep_orphan_gates(())
    assert not sf.exists()
    assert list(sf.parent.glob("deadlineless.json.corrupt-*"))


def test_deadlineless_running_state_holds_slot_and_pool_before_its_bound(tmp_path):
    """The other half of R16: BEFORE the mtime bound, a deadline-less running
    state is occupancy, not expiry.

    A state that never recorded a deadline cannot be past one, so it is exactly
    as opaque as an unparseable one — it may BE the gate currently burning a
    box. Dropping it from the lease count is favourable absence with a CPU
    price: the daemon would dispatch beside a child it never proved dead. It
    is held (fail-closed on the slot AND the whole twin pool) until the veto
    clock in `test_deadlineless_running_state_quarantines_after_mtime_bound`
    expires — and it is never reaped on the way there, because there is no
    timing fact to reap on.
    """
    loop = _make_loop(tmp_path / "loops", _init_repo(tmp_path / "repo"),
                      _fake_offload(tmp_path), allow_remote_gate=True)
    sf = loop.root / "state" / "gates" / "deadlineless-fresh.json"
    sf.parent.mkdir(parents=True, exist_ok=True)
    sf.write_text(json.dumps({"state": "running", "mode": "remote",
                              "twin": "mw0002", "pid": os.getpid()}))
    leases = loop._read_gate_leases()
    assert leases.running == 1, \
        "a deadline-less running state must occupy its slot, not vanish from the count"
    assert leases.corrupt is True
    assert leases.twins == {s.host for s in gl.TWIN_SPECS}, \
        "an unreadable lease claims the whole pool for its bound window"
    assert loop._running_gate_count() == 1
    assert loop._twins_in_flight() == {s.host for s in gl.TWIN_SPECS}
    # Held, not reaped: the state survives observation with its lease intact.
    assert sf.exists()
    assert json.loads(sf.read_text())["state"] == "running"


def test_parked_lease_observations_do_not_rewrite_its_veto_clock(tmp_path):
    """A fresh launchd tick may observe a park, but never refresh its expiry."""
    loop = _make_loop(tmp_path / "loops", _init_repo(tmp_path / "repo"),
                      _fake_offload(tmp_path), allow_remote_gate=True)
    sf = loop.root / "state" / "gates" / "parked.json"
    sf.parent.mkdir(parents=True, exist_ok=True)
    sf.write_text(json.dumps({
        "state": "running", "mode": "remote", "twin": "mw0002",
        "deadline": 1.0, "disposition": "reap-unverifiable-parked",
        "reap": "unverifiable", "parked_at": time.time(),
    }))
    before = sf.read_bytes()
    loop._read_gate_leases()
    assert sf.read_bytes() == before
    # Simulate another fresh daemon process observing the exact same state.
    loop2 = _make_loop(loop.root, loop.repo, _fake_offload(tmp_path),
                       allow_remote_gate=True)
    loop2._read_gate_leases()
    assert sf.read_bytes() == before


def test_orphan_sweep_twice_does_not_rewrite_existing_park_clock(tmp_path):
    """The sweep drives an existing park twice without refreshing parked_at."""
    loop = _make_loop(tmp_path / "loops", _init_repo(tmp_path / "repo"),
                      _fake_offload(tmp_path), allow_remote_gate=True)
    sf = loop.root / "state" / "gates" / "already-parked.json"
    sf.parent.mkdir(parents=True, exist_ok=True)
    parked_at = time.time() - 60
    sf.write_text(json.dumps({
        "state": "running", "mode": "remote", "twin": "mw0002",
        "deadline": time.time() - gl.ORPHAN_SWEEP_GRACE_S - 1,
        "disposition": "reap-unverifiable-parked", "reap": "unverifiable",
        "parked_at": parked_at,
    }))

    loop._sweep_orphan_gates(())
    after_first = json.loads(sf.read_text())["parked_at"]
    loop._sweep_orphan_gates(())
    after_second = json.loads(sf.read_text())["parked_at"]

    assert after_first == parked_at
    assert after_second == parked_at


def test_backdated_park_clock_quarantines_despite_fresh_state_mtime(tmp_path):
    """The recorded parked_at, not the observer-visible mtime, owns expiry."""
    loop = _make_loop(tmp_path / "loops", _init_repo(tmp_path / "repo"),
                      _fake_offload(tmp_path), allow_remote_gate=True)
    sf = loop.root / "state" / "gates" / "old-park-fresh-file.json"
    sf.parent.mkdir(parents=True, exist_ok=True)
    sf.write_text(json.dumps({
        "state": "running", "mode": "remote", "twin": "mw0002",
        "deadline": 1.0, "disposition": "reap-unverifiable-parked",
        "reap": "unverifiable",
        "parked_at": time.time() - gl.GATE_DEADLINE_S - 1,
    }))
    assert time.time() - sf.stat().st_mtime < 5

    leases = loop._read_gate_leases()

    assert leases.running == 0
    assert leases.twins == set()
    assert not sf.exists()
    assert list(sf.parent.glob(f"{sf.name}.corrupt-*"))


def test_orphan_park_alert_and_ledger_are_edge_triggered(tmp_path, monkeypatch):
    loop = _make_loop(tmp_path / "loops", _init_repo(tmp_path / "repo"),
                      _fake_offload(tmp_path), allow_remote_gate=True)
    sf = _running_state(
        loop.root / "state" / "gates", "legacy-orphan",
        mode="remote", twin="mw0002",
        deadline_in=-(gl.ORPHAN_SWEEP_GRACE_S + 1),
        pid=4242, pid_started="dispatch-start", train="train/old", tip="a" * 40,
    )
    monkeypatch.setattr(gl, "_pid_lstart", lambda _pid: "current-start")

    loop._sweep_orphan_gates(())
    before = sf.read_bytes()
    alert_lines = (loop.root / "ALERTS.md").read_text().splitlines()
    ledger = _ledger_events(loop.root)
    assert sum((e.get("detail") or {}).get("kind") == "orphan-gate-parked"
               for e in ledger) == 1

    loop2 = _make_loop(loop.root, loop.repo, _fake_offload(tmp_path),
                       allow_remote_gate=True)
    monkeypatch.setattr(gl, "_pid_lstart", lambda _pid: "current-start")
    loop2._sweep_orphan_gates(())

    assert sf.read_bytes() == before
    assert (loop.root / "ALERTS.md").read_text().splitlines() == alert_lines
    assert _ledger_events(loop.root) == ledger


def test_direct_park_holds_only_the_local_slot(tmp_path):
    loop = _make_loop(tmp_path / "loops", _init_repo(tmp_path / "repo"),
                      _fake_offload(tmp_path), allow_remote_gate=True)
    sf = loop.root / "state" / "gates" / "direct-parked.json"
    sf.parent.mkdir(parents=True, exist_ok=True)
    sf.write_text(json.dumps({
        "state": "running", "mode": "direct", "deadline": 1.0,
        "disposition": "reap-unverifiable-parked", "reap": "unverifiable",
        "parked_at": time.time(),
    }))
    leases = loop._read_gate_leases()
    assert leases.running == 1
    assert leases.twins == set()
    assert leases.corrupt is False


def test_orphan_sweep_preserves_terminal_verdict_written_during_reap(tmp_path, monkeypatch):
    """R15: reap grace must not clobber a child verdict that just landed."""
    loop = _make_loop(tmp_path / "loops", _init_repo(tmp_path / "repo"),
                      _fake_offload(tmp_path), allow_remote_gate=True)
    sf = _running_state(loop.root / "state" / "gates", "mid-sweep",
                        deadline_in=-(gl.ORPHAN_SWEEP_GRACE_S + 1),
                        pid=4242, pid_started="known")

    def child_finishes(_st, path, *, reason):
        path.write_text(json.dumps({"state": "done", "rc": 0,
                                    "stdout": "gate passed during reap"}))
        return "reaped"

    monkeypatch.setattr(loop, "_reap_gate_child", child_finishes)
    loop._sweep_orphan_gates(())
    preserved = json.loads(sf.read_text())
    assert preserved == {"state": "done", "rc": 0,
                         "stdout": "gate passed during reap"}


def test_twin_deferrals_share_one_probe_round_and_one_ledger_event(tmp_path, monkeypatch):
    """R4/R5: a tick caches probe readings and records aggregate starvation."""
    repo = _init_repo(tmp_path / "repo")
    main = _git(repo, "rev-parse", "HEAD")
    loop = _make_loop(tmp_path / "loops", repo, _fake_offload(tmp_path),
                      allow_remote_gate=True)
    _running_state(loop.root / "state" / "gates", "busy", mode="direct")
    trains = [_manual_train(repo, main), _manual_train(repo, main)]
    # Distinct state paths avoid an accidental shared branch in the fixture.
    trains[1] = Train(branch="train/second", base=main, tip="f" * 40,
                      members=trains[1].members, paths=trains[1].paths)
    cand = _cand("d" * 64, "cand/d", main)
    monkeypatch.setattr(loop, "load_candidates", lambda _terminal: [cand])
    monkeypatch.setattr(loop, "_reconcile_already_merged", lambda cands, _sha: cands)
    monkeypatch.setattr(loop, "_open_builder", lambda: tmp_path / "builder")
    monkeypatch.setattr(loop, "_close_builder", lambda: None)
    monkeypatch.setattr(gl, "assemble_trains", lambda *_args, **_kw: (trains, []))
    monkeypatch.setattr(gl, "is_ancestor_of_main", lambda *_args: True)
    calls: list[str] = []

    def loaded(host):
        from bridge.gate_host import LoadReading
        calls.append(host)
        return LoadReading(host, 99.0, time.monotonic())

    monkeypatch.setattr(gl, "probe_remote_load", loaded)
    outcomes = loop.run_once()
    assert [o.action for o in outcomes] == ["deferred", "deferred"]
    assert len(calls) == len(gl.TWIN_SPECS)
    events = [e for e in _ledger_events(loop.root)
              if (e.get("detail") or {}).get("kind") == "twin-pool-inadmissible"]
    assert len(events) == 1
    assert len(events[0]["detail"]["deferrals"]) == 2


def test_darwin_narrowed_eperm_branches_remain_reaped(tmp_path, monkeypatch):
    """R19: only post-SIGTERM EPERM is the Darwin zombie-leader narrowing."""
    loop = _make_loop(tmp_path / "loops", _init_repo(tmp_path / "repo"),
                      _fake_offload(tmp_path), allow_remote_gate=True)
    monkeypatch.setattr(gl, "_pid_lstart", lambda _pid: "known-start")
    monkeypatch.setattr(gl.time, "sleep", lambda _seconds: None)
    state = tmp_path / "state.json"
    state.write_text(json.dumps({"state": "running"}))

    def alive_check_eperm(_pid: int, sig: int) -> None:
        if sig == 0:
            raise PermissionError("zombie leader")

    monkeypatch.setattr(gl.os, "killpg", alive_check_eperm)
    assert loop._reap_gate_child(
        {"state": "running", "pid": 4242, "pid_started": "known-start"},
        state, reason="darwin-alive-check") == "reaped"
    assert json.loads(state.read_text())["reap_confirmed"] is False

    def kill_never_sent_eperm(_pid: int, sig: int) -> None:
        if sig == signal.SIGKILL:
            raise PermissionError("zombie leader")

    monkeypatch.setattr(gl.os, "killpg", kill_never_sent_eperm)
    assert loop._reap_gate_child(
        {"state": "running", "pid": 4242, "pid_started": "known-start"},
        state, reason="darwin-sigkill") == "reaped"
    confidence = [e for e in _ledger_events(loop.root)
                  if (e.get("detail") or {}).get("kind") == "reap-confidence-narrowed"]
    assert len(confidence) == 2
    assert all(e["detail"].get("reap_confirmed") is False for e in confidence)


def test_identity_mismatch_never_signals_recycled_pid_and_parks_state(
        tmp_path, monkeypatch):
    """I8: a recycled pid's different lstart is never killpg'd or re-gated."""
    repo = _init_repo(tmp_path / "repo")
    m0 = _git(repo, "rev-parse", "HEAD")
    loop = _make_loop(tmp_path / "loops", repo, _fake_offload(tmp_path),
                      allow_remote_gate=True)
    train = _manual_train(repo, m0)
    child = _spawn_session_sleep(300)
    signals: list[tuple[int, int]] = []
    real_killpg = os.killpg
    monkeypatch.setattr(gl, "_pid_lstart", lambda _pid: "current-lstart")

    def _record_killpg(pid: int, sig: int) -> None:
        signals.append((pid, sig))

    monkeypatch.setattr(gl.os, "killpg", _record_killpg)
    try:
        sf = gate_state_path(loop.root, train)
        sf.parent.mkdir(parents=True, exist_ok=True)
        sf.write_text(json.dumps({
            "state": "running", "mode": "remote", "twin": "mw0002",
            "deadline": time.time() - 10, "pid": child.pid,
            "pid_started": "recorded-lstart", "train": train.branch, "tip": train.tip,
        }))
        verdict = read_gate_verdict(sf)
        assert verdict is not None and verdict.result == "instrument-error"

        out = loop.on_instrument_error(train, verdict)

        assert out.action == "instrument"
        assert signals == []
        assert child.poll() is None, "identity mismatch must leave the live process untouched"
        stamped = json.loads(sf.read_text())
        assert stamped["disposition"] == "reap-unverifiable-parked"
        assert stamped["reap"] == "identity-mismatch"
        assert any("identity-mismatch" in alert for alert in loop.alerts)
    finally:
        if child.poll() is None:
            try:
                real_killpg(child.pid, 9)
            except (ProcessLookupError, PermissionError, OSError):
                pass
        try:
            child.wait(timeout=2)
        except Exception:
            pass


def test_a_local_gate_claims_no_twin(tmp_path):
    loop = _make_loop(tmp_path / "loops", _init_repo(tmp_path / "repo"),
                      _fake_offload(tmp_path), allow_remote_gate=True)
    _running_state(loop.root / "state" / "gates", "localgate", mode="direct")
    assert loop._twins_in_flight() == set()
    # Expired local still holds the SLOT (I4' expired-present) even with no twin.
    _running_state(loop.root / "state" / "gates", "local-expired",
                   mode="direct", deadline_in=-1)
    assert loop._running_gate_count() == 2
    assert loop._twins_in_flight() == set()


def test_receipt_json_is_not_an_occupancy_fact(tmp_path):
    """I4' receipt filter (design B1): truncated receipt-*.json must never block."""
    loop = _make_loop(tmp_path / "loops", _init_repo(tmp_path / "repo"),
                      _fake_offload(tmp_path), allow_remote_gate=True)
    gdir = loop.root / "state" / "gates"
    gdir.mkdir(parents=True, exist_ok=True)
    (gdir / "receipt-train__x@deadbeef.json").write_text("{truncated")
    leases = loop._read_gate_leases()
    assert leases.running == 0
    assert leases.twins == set()
    assert leases.corrupt is False
    assert loop._running_gate_count() == 0
    assert loop._twins_in_flight() == set()


def test_corrupt_state_blocks_dispatch_now_and_quarantines_after_bound(
        tmp_path, monkeypatch):
    """I4' bounded corrupt veto: blocks dispatch NOW; after its recorded start+
    the file is quarantined and dispatch resumes with the artifact present."""
    repo = _init_repo(tmp_path / "repo")
    m0 = _git(repo, "rev-parse", "HEAD")
    loops_root = tmp_path / "loops"
    _commit_on(repo, "cand-c", m0, "c.txt", "C\n")
    _write_candidate(loops_root, "c" * 64, "cand-c", m0, ["c.txt"])

    loop = _make_loop(loops_root, repo, _fake_offload(tmp_path),
                      allow_remote_gate=True)
    gdir = loops_root / "state" / "gates"
    gdir.mkdir(parents=True, exist_ok=True)
    corrupt = gdir / "corrupt-gate.json"
    corrupt.write_text("{not json")

    # --- NOW: fail-closed, run_once dispatches NOTHING ---
    dispatched: list = []
    loop.dispatch_gate = lambda *a, **k: dispatched.append((a, k))  # type: ignore[assignment]
    out = loop.run_once()
    assert dispatched == [], f"corrupt state must block all dispatch, got {dispatched}"
    assert loop._running_gate_count() >= 1
    assert loop._twins_in_flight() == {s.host for s in gl.TWIN_SPECS}
    veto_events = [e for e in _ledger_events(loops_root)
                   if (e.get("detail") or {}).get("kind") == "gate-state-veto-start"]
    assert len(veto_events) == 1
    assert veto_events[0]["detail"]["path"].endswith("corrupt-gate.json")
    assert any("corrupt gate-state corrupt-gate.json" in a for a in loop.alerts)
    # Outcomes may be deferred (slots full) or empty if assembly saw full slots
    # before any train loop; either way no dispatch.
    assert all(o.action != "dispatched" for o in out)

    # --- PAST BOUND: quarantine + dispatch resumes ---
    past = time.time() - (gl.GATE_DEADLINE_S + 60)
    marker = loop._alert_marker_path(f"gate-veto:{corrupt}")
    marker_state = json.loads(marker.read_text())
    marker_state["veto_started_at"] = past
    marker.write_text(json.dumps(marker_state))
    # Re-read leases triggers quarantine side effect.
    leases = loop._read_gate_leases()
    assert leases.corrupt is False
    assert leases.running == 0
    assert not corrupt.exists(), "corrupt file must be renamed out of the glob"
    artifacts = list(gdir.glob("corrupt-gate.json.corrupt-*"))
    assert artifacts, "quarantine artifact must exist"

    # After quarantine a fresh tick can dispatch (pick_twin admitted in-suite).
    def _admit_all(exclude=frozenset(), probe=None, readings=None):
        for s in gl.TWIN_SPECS:
            if s.host not in exclude:
                return s, [{"host": s.host, "admitted": True, "reason": ""}]
        return None, []

    monkeypatch.setattr(gl, "pick_twin", _admit_all)
    dispatched2: list = []
    loop2 = _make_loop(loops_root, repo, _fake_offload(tmp_path),
                       allow_remote_gate=True)
    loop2.dispatch_gate = lambda *a, **k: dispatched2.append((a, k))  # type: ignore[assignment]
    out2 = loop2.run_once()
    assert any(o.action == "dispatched" for o in out2) or dispatched2, \
        f"after quarantine dispatch must resume; out={out2} dispatched={dispatched2}"

    q_events = [e for e in _ledger_events(loops_root)
                if (e.get("detail") or {}).get("kind") == "gate-state-quarantined"]
    assert q_events, "quarantine must emit instrument_error ledger event"


def test_corrupt_veto_marker_is_gc_d_and_not_reused_by_new_file(tmp_path):
    loop = _make_loop(tmp_path / "loops", _init_repo(tmp_path / "repo"),
                      _fake_offload(tmp_path), allow_remote_gate=True)
    sf = loop.root / "state" / "gates" / "same-path.json"
    sf.parent.mkdir(parents=True, exist_ok=True)
    sf.write_text("")

    first = loop._read_gate_leases()
    marker = loop._alert_marker_path(f"gate-veto:{sf}")
    stale = json.loads(marker.read_text())
    assert first.corrupt and stale["inode"] == sf.stat().st_ino

    sf.unlink()
    loop._read_gate_leases()
    assert not marker.exists(), "missing state paths must garbage-collect veto markers"

    time.sleep(0.01)
    sf.write_text("")
    stale["veto_started_at"] = time.time() - gl.GATE_DEADLINE_S - 1
    marker.write_text(json.dumps(stale))
    fresh_identity = (sf.stat().st_ino, sf.stat().st_ctime_ns)
    assert fresh_identity != (stale["inode"], stale["ctime_ns"])

    second = loop._read_gate_leases()
    refreshed = json.loads(marker.read_text())
    assert second.corrupt and second.running == 1
    assert sf.exists()
    assert not list(sf.parent.glob(f"{sf.name}.corrupt-*"))
    assert (refreshed["inode"], refreshed["ctime_ns"]) == fresh_identity
    assert refreshed["veto_started_at"] > stale["veto_started_at"]


def test_quarantine_ledger_append_precedes_state_rename(tmp_path, monkeypatch):
    loop = _make_loop(tmp_path / "loops", _init_repo(tmp_path / "repo"),
                      _fake_offload(tmp_path), allow_remote_gate=True)
    sf = loop.root / "state" / "gates" / "quarantine-order.json"
    sf.parent.mkdir(parents=True, exist_ok=True)
    sf.write_text("corrupt")
    observed: list[str] = []
    real_rename = Path.rename

    def inspect_then_fail(path, target):
        if path == sf:
            observed.extend(
                (e.get("detail") or {}).get("kind")
                for e in _ledger_events(loop.root)
            )
            raise OSError("rename crash window")
        return real_rename(path, target)

    monkeypatch.setattr(Path, "rename", inspect_then_fail)
    loop._quarantine_gate_state(sf, kind="corrupt")

    assert observed == ["gate-state-quarantined"]
    assert sf.exists()
    kinds = [(e.get("detail") or {}).get("kind") for e in _ledger_events(loop.root)]
    assert kinds == ["gate-state-quarantined", "gate-state-quarantine-failed"]


def test_orphan_running_state_is_swept_with_verified_reap(tmp_path, monkeypatch):
    """I8 sweep (M3): orphan running state (no candidate/train) past deadline+grace
    with a real child → swept, child gone, terminal stamp, slot+twin free next tick.
    """
    repo = _init_repo(tmp_path / "repo")
    loop = _make_loop(tmp_path / "loops", repo, _fake_offload(tmp_path),
                      allow_remote_gate=True)
    child = _spawn_session_sleep(300)
    fixed_lstart = "Mon Aug 10 12:00:00 2026"

    def _lstart(pid: int):
        if pid == child.pid and _pid_alive(pid):
            return fixed_lstart
        return None

    monkeypatch.setattr(gl, "_pid_lstart", _lstart)
    try:
        gdir = loop.root / "state" / "gates"
        # Orphan: train/tip that will never assemble (no candidates at all).
        sf = _running_state(
            gdir, "orphan@deadbeef",
            mode="remote", twin="mw0002",
            deadline_in=-(gl.ORPHAN_SWEEP_GRACE_S + 30),
            pid=child.pid, pid_started=fixed_lstart,
            train="train/gone", tip="a" * 40,
        )
        assert "mw0002" in loop._twins_in_flight()

        # No candidates → early path still runs the orphan sweep.
        out = loop.run_once()
        assert out == []

        # Child gone (poll covers zombies left until waitpid).
        deadline = time.time() + 5
        while time.time() < deadline and child.poll() is None:
            time.sleep(0.05)
        assert child.poll() is not None, \
            f"sweep must terminate the orphan child (poll={child.poll()})"

        # Terminal stamp present (not unlinked — sweep stamps closed).
        assert sf.exists()
        st = json.loads(sf.read_text())
        assert st.get("state") == "closed"
        assert st.get("disposition") == "orphan-swept"
        assert st.get("reap") == "reaped"

        # Slot AND twin free next tick.
        assert loop._running_gate_count() == 0
        assert "mw0002" not in loop._twins_in_flight()

        events = _ledger_events(loop.root)
        assert any((e.get("detail") or {}).get("kind") == "orphan-gate-swept"
                   for e in events)
    finally:
        if child.poll() is None:
            try:
                os.killpg(child.pid, 9)
            except (ProcessLookupError, PermissionError, OSError):
                pass
        try:
            child.wait(timeout=2)
        except Exception:
            pass


def test_remote_dispatch_argv_carries_gtimeout_and_ladder_workers(
        tmp_path, monkeypatch):
    """I1+I6 consumption: remote run_gate_child argv carries gtimeout wrapper
    and MERGE_GATE_LADDER_WORKERS from GATE_LADDER_WORKERS (integration-shaped,
    monkeypatched ssh/subprocess)."""
    gate_ws = tmp_path / "gate-ws"
    gate_ws.mkdir()
    local_repo = tmp_path / "repo"
    local_repo.mkdir()
    evidence_root = tmp_path / "evidence"
    candidate_receipt = evidence_root / "records" / "merge-gate" / ("a" * 40 + ".json")
    candidate_receipt.parent.mkdir(parents=True)
    candidate_receipt.write_text('{"signed":true}')
    run_receipt = tmp_path / "run-receipt.json"
    state_file = tmp_path / "state.json"
    state_file.write_text(json.dumps({"state": "running", "deadline": 2000.0}))
    captured: list = []

    minted_base = "f" * 40
    monkeypatch.setattr(
        gl, "_mint_candidate_receipt",
        lambda *_a, **_kw: gl.MintOutcome(True, str(candidate_receipt),
                                          str(evidence_root), "minted", minted_base))
    monkeypatch.setattr(gl, "pin_remote_candidate",
                        lambda *_a, **_kw: {"ok": True})
    monkeypatch.setattr(gl, "preflight_remote",
                        lambda *_a, **_kw: {"ready": True, "failed": [], "checked": 1})
    monkeypatch.setattr(gl, "sync_forward_candidate_receipt",
                        lambda *_a, **_kw: {"ok": True})
    monkeypatch.setattr(gl, "sync_back_evidence",
                        lambda *_a, **_kw: {"ok": True})

    def _capture_run(cmd, **kw):
        captured.append(cmd)
        # Write a done-shaped outcome via the real path's returncode.
        class _P:
            returncode = 0
            stdout = "ok"
            stderr = ""
        return _P()

    monkeypatch.setattr(gl.subprocess, "run", _capture_run)
    monkeypatch.setattr(gl.time, "time", lambda: 1000.0)

    rc = run_gate_child([
        "--mode", "remote",
        "--state-file", str(state_file),
        "--gate-workspace", str(gate_ws),
        "--twin", gl.TWIN_HOST,
        "--local-repo", str(local_repo),
        "--candidate", "cand/x",
        "--tip", "a" * 40,
        "--receipt", str(run_receipt),
    ])
    assert rc == 0
    assert captured, "subprocess.run must have been invoked for the remote gate"
    # remote_gate_command returns [ssh, ..., host, inner]; gtimeout lives in inner.
    inner = captured[0][-1] if captured[0] else ""
    assert isinstance(inner, str)
    assert gl.GATE_REMOTE_TIMEOUT_S == 300
    assert "gtimeout -k 30 880" in inner, inner
    assert f"MERGE_GATE_LADDER_WORKERS={GATE_LADDER_WORKERS}" in inner \
        or f"MERGE_GATE_LADDER_WORKERS='{GATE_LADDER_WORKERS}'" in inner \
        or f'MERGE_GATE_LADDER_WORKERS="{GATE_LADDER_WORKERS}"' in inner, inner


def test_direct_gate_uses_gate_ladder_workers_env(tmp_path, monkeypatch):
    """I1: direct branch setdefault MERGE_GATE_LADDER_WORKERS from GATE_LADDER_WORKERS."""
    gate_ws = _fake_gate_workspace(tmp_path, None)
    # Ensure the fake gate script exists (no real repo worktree needed).
    state_file = tmp_path / "state.json"
    receipt = tmp_path / "receipt.json"
    seen_env: dict = {}

    monkeypatch.setattr(
        gl, "_mint_candidate_receipt",
        lambda *_a, **_kw: gl.MintOutcome(
            True, str(tmp_path / "signed.json"), str(tmp_path / "ev"), "minted", "b" * 40))

    def _capture_run(cmd, **kw):
        seen_env.update(kw.get("env") or {})
        class _P:
            returncode = 0
            stdout = ""
            stderr = ""
        return _P()

    monkeypatch.setattr(gl.subprocess, "run", _capture_run)
    # Ensure the env var is unset so setdefault actually fires.
    monkeypatch.delenv("MERGE_GATE_LADDER_WORKERS", raising=False)

    run_gate_child([
        "--mode", "direct",
        "--state-file", str(state_file),
        "--gate-workspace", str(gate_ws),
        "--candidate", "cand/x",
        "--tip", "a" * 40,
        "--receipt", str(receipt),
    ])
    assert seen_env.get("MERGE_GATE_LADDER_WORKERS") == str(GATE_LADDER_WORKERS)


def test_empty_active_pool_emits_ledger_and_alert(tmp_path, monkeypatch):
    """R7: unchanged empty pool is edge-triggered, then re-arms after recovery."""
    repo = _init_repo(tmp_path / "repo")
    m0 = _git(repo, "rev-parse", "HEAD")
    loops_root = tmp_path / "loops"
    _commit_on(repo, "cand-e", m0, "e.txt", "E\n")
    _write_candidate(loops_root, "e" * 64, "cand-e", m0, ["e.txt"])

    original_specs = gl.TWIN_SPECS
    monkeypatch.setattr(gl, "TWIN_SPECS", ())
    # MAX_CONCURRENT_GATES is module-level; empty pool ⇒ only local matters.
    loop = _make_loop(loops_root, repo, _fake_offload(tmp_path),
                      allow_remote_gate=True)
    loop.dispatch_gate = lambda *a, **k: None  # type: ignore[assignment]
    loop.run_once()
    loop.run_once()
    assert any("ACTIVE twin pool is empty" in a for a in loop.alerts)
    events = _ledger_events(loops_root)
    empty = [e for e in events if (e.get("detail") or {}).get("kind") == "empty-active-twin-pool"]
    assert len(empty) == 1
    monkeypatch.setattr(gl, "TWIN_SPECS", original_specs)
    # A non-empty tick clears the marker; use a harmless synthetic spec when
    # the module started under an explicitly empty active-pool environment.
    if not original_specs:
        from bridge.gate_host import KNOWN_TWIN_SPECS
        monkeypatch.setattr(gl, "TWIN_SPECS", (KNOWN_TWIN_SPECS[0],))
    loop.run_once()
    monkeypatch.setattr(gl, "TWIN_SPECS", ())
    loop.run_once()
    empty = [e for e in _ledger_events(loops_root)
             if (e.get("detail") or {}).get("kind") == "empty-active-twin-pool"]
    assert len(empty) == 2 and "suppressed_ticks" in empty[-1]["detail"]


# ============================== ORIGIN SYNC / PUSH CLASSIFICATION (0811) ======
# Finding sha256:444a8713. The daemon assembles trains on the serving checkout's
# LOCAL main and pushes them to origin, but never syncs main FROM origin. On
# 2026-08-11 two PRs landed on origin through GitHub, local main fell two behind,
# and EVERY train push was non-fast-forward: the daemon rebuilt the identical
# train on the identical stale base every tick and refused identically, three
# times in eight minutes, until a human ran `git pull --ff-only`. These tests are
# written to fail against the pre-fix source.


def _bare_origin(tmp_path: Path, repo: Path) -> Path:
    """A REAL bare `origin` the serving checkout pushes to and fetches from."""
    bare = tmp_path / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(bare)],
                   capture_output=True, text=True, check=True)
    _git(repo, "remote", "add", "origin", str(bare))
    _git(repo, "push", "-q", "origin", "main")
    return bare


def _advance_origin(tmp_path: Path, bare: Path, filename: str, body: str) -> str:
    """Land a commit on origin/main the way a merged GitHub PR does — from a
    SEPARATE clone, so the serving checkout knows nothing about it until it
    fetches. Returns the new origin/main sha."""
    clone = tmp_path / f"pr-clone-{filename.replace('/', '_')}"
    subprocess.run(["git", "clone", "-q", str(bare), str(clone)],
                   capture_output=True, text=True, check=True)
    _git(clone, "config", "user.email", "pr@example.invalid")
    _git(clone, "config", "user.name", "pr-bot")
    (clone / filename).parent.mkdir(parents=True, exist_ok=True)   # nested paths too
    (clone / filename).write_text(body, encoding="utf-8")
    _git(clone, "add", "-A")
    _git(clone, "commit", "-qm", f"PR: add {filename}")
    _git(clone, "push", "-q", "origin", "main")
    return _git(clone, "rev-parse", "HEAD")


def _recording_git(calls: list, *, push_error: str | None = None,
                   fail_fetch: bool = False, fail_stash: bool = False,
                   fail_reset: bool = False):
    """A gl.git wrapper that RECORDS every invocation and can force a push,
    fetch, stash or reset failure with verbatim git text. Everything else runs
    for real, so the classifier is exercised against git's own words."""
    real = gl.git

    def fake(repo, *args, **kw):
        calls.append(tuple(args))
        if args and args[0] == "push" and push_error is not None:
            return (1, "", push_error)
        if args and args[0] == "fetch" and fail_fetch:
            return (128, "", "fatal: unable to access 'https://github.test/': "
                             "Could not resolve host: github.test")
        if args and args[0] == "stash" and fail_stash:
            return (1, "", "fatal: Unable to create '.git/index.lock': File exists (test)")
        if args and args[0] == "reset" and fail_reset:
            return (128, "", "fatal: Unable to create '.git/index.lock': File exists (test)")
        return real(repo, *args, **kw)

    return fake


# --- (1) pre-assembly origin sync ---------------------------------------------


def test_origin_advanced_out_of_band_lands_next_tick_with_no_operator_action(tmp_path):
    """MUTATION GUARD (sync-disabled): deleting the pre-assembly origin sync
    fails HERE — the train is assembled on the stale base and its push is
    non-fast-forward, exactly the 2026-08-11 outage."""
    repo = _init_repo(tmp_path / "repo")
    m0 = _git(repo, "rev-parse", "HEAD")
    bare = _bare_origin(tmp_path, repo)
    _commit_on(repo, "cand-s", m0, "s.txt", "SSS\n")
    loops_root = tmp_path / "loops"
    id_s = _write_candidate(loops_root, "5" * 64, "cand-s", m0, ["s.txt"])
    # a PR lands on origin AFTER the candidate was built — the routine event
    # that blocked every landing before this fix.
    o1 = _advance_origin(tmp_path, bare, "pr.txt", "from a merged PR\n")
    assert _git(repo, "rev-parse", "main") == m0, "serving main starts BEHIND origin"

    offload = _fake_offload(tmp_path)
    os.environ["FAKE_GATE_RC"] = "0"
    os.environ.pop("FAKE_GATE_SLUG", None)
    os.environ.pop("FAKE_GATE_NO_RECEIPT", None)
    try:
        loop = _make_loop(loops_root, repo, offload, remote="origin", push=True)
        outs = loop.run_once()
        assert outs and outs[0].action == "dispatched", outs
        train = outs[0].train
        # the train was assembled on the FRESH base, not the stale one
        assert train.base == o1, f"train base {train.base[:12]} is not origin {o1[:12]}"
        _wait_done(gate_state_path(loops_root, train))
        loop2 = _make_loop(loops_root, repo, offload, remote="origin", push=True)
        outs2 = loop2.run_once()
    finally:
        os.environ.pop("FAKE_GATE_RC", None)

    assert outs2 and outs2[0].action == "landed", outs2
    new_main = _git(repo, "rev-parse", "main")
    assert _git(bare, "rev-parse", "main") == new_main, "the push reached origin"
    # the PR commit is an ancestor: nothing was forced over it
    assert _git(repo, "rev-list", "--max-count=1", o1, "--not", new_main) == ""
    events = _ledger_events(loops_root)
    assert any(e["event"] == "merged" and e["id"] == id_s for e in events), events


def test_burst_of_out_of_band_advances_does_not_re_gate_the_in_flight_train(
        tmp_path, monkeypatch):
    """CHURN GUARD (out-of-band-origin-advance re-gate storm). While a gate is in
    flight, a BURST of merged PRs on origin must NOT keep moving the base out from
    under it. Each advance would otherwise re-assemble the same members onto a new
    base, mint a new tip, change the `<train>@<tip>` gate key, and orphan the
    running ~25-min gate into a from-scratch re-gate — every tick, so the gate
    never finishes and nothing lands (the livelock). Assert: the running gate is
    held (base not advanced, gate not re-dispatched) through the burst, then a
    clean land still happens once it settles, with nothing forced and exactly ONE
    re-gate onto the settled origin. Deleting the `hold_base` guard fails HERE."""
    repo = _init_repo(tmp_path / "repo")
    m0 = _git(repo, "rev-parse", "HEAD")
    bare = _bare_origin(tmp_path, repo)
    _commit_on(repo, "cand-s", m0, "s.txt", "SSS\n")
    loops_root = tmp_path / "loops"
    id_s = _write_candidate(loops_root, "5" * 64, "cand-s", m0, ["s.txt"])

    # Hold the gate "running" (no detached child) so origin can advance under an
    # in-flight gate deterministically, and record every dispatch to prove the
    # running gate is not re-gated per advance.
    dispatched: list[str] = []
    # Patch the imported `GateLoop` name that `_make_loop` actually instantiates,
    # NOT `gl.GateLoop`: an earlier suite file (test_gate_host) does
    # `importlib.reload(gl)`, which rebinds `gl.GateLoop` to a NEW class object
    # while this module's `GateLoop` (and `_make_loop`) keep the original. Under
    # full-suite order, patching `gl.GateLoop` would miss and the real detached
    # gate child would spawn — the flake that failed the merge-gate full run.
    real_dispatch = GateLoop.dispatch_gate

    def stub_dispatch(self, train, *, allow_remote, twin=None):
        sf = gate_state_path(self.root, train)
        sf.parent.mkdir(parents=True, exist_ok=True)
        self._write_json_atomic(sf, {
            "state": "running", "train": train.branch, "tip": train.tip,
            "base": train.base, "members": [m["id"] for m in train.members],
            "deadline": time.time() + gl.GATE_DEADLINE_S,
            "started_at": gl._iso(gl._now()), "mode": "direct", "receipt": None,
        })
        dispatched.append(train.tip)

    monkeypatch.setattr(GateLoop, "dispatch_gate", stub_dispatch)

    offload = _fake_offload(tmp_path)
    os.environ["FAKE_GATE_RC"] = "0"
    os.environ.pop("FAKE_GATE_SLUG", None)
    os.environ.pop("FAKE_GATE_NO_RECEIPT", None)
    try:
        # tick 1: the gate is dispatched on the base the daemon starts at.
        loop = _make_loop(loops_root, repo, offload, remote="origin", push=True)
        outs = loop.run_once()
        assert outs and outs[0].action == "dispatched", outs
        first_train = outs[0].train
        running_tip = first_train.tip
        assert dispatched == [running_tip]
        assert _git(repo, "rev-parse", "main") == m0

        # A BURST of merged PRs on origin while that gate is still running.
        for i in range(3):
            _advance_origin(tmp_path, bare, f"pr{i}.txt", f"PR {i}\n")
            tick = _make_loop(loops_root, repo, offload, remote="origin", push=True)
            tick.run_once()
            # the base is NOT advanced out from under the running gate ...
            assert _git(repo, "rev-parse", "main") == m0, (
                f"base advanced under an in-flight gate on advance {i}")
            # ... and the running gate is NOT re-dispatched (no new tip minted).
            assert dispatched == [running_tip], (
                f"in-flight gate re-dispatched on advance {i}: {dispatched}")
            assert any("DEFERRING the advance" in ln for ln in tick.lines), tick.lines
    finally:
        os.environ.pop("FAKE_GATE_RC", None)

    # The gate finishes and frees its slot: the base may now advance and the train
    # re-gates exactly ONCE onto the settled origin, then lands clean.
    monkeypatch.setattr(GateLoop, "dispatch_gate", real_dispatch)
    gate_state_path(loops_root, first_train).unlink()      # the held gate completed
    origin_tip = _git(bare, "rev-parse", "main")

    os.environ["FAKE_GATE_RC"] = "0"
    os.environ.pop("FAKE_GATE_SLUG", None)
    os.environ.pop("FAKE_GATE_NO_RECEIPT", None)
    try:
        tick_a = _make_loop(loops_root, repo, offload, remote="origin", push=True)
        outs_a = tick_a.run_once()
        assert outs_a and outs_a[0].action == "dispatched", outs_a
        final_train = outs_a[0].train
        # the ONE re-gate binds the SETTLED origin base, not the stale one, and is
        # a fresh tip (a burst of 3 advances cost 1 re-gate, not 3).
        assert final_train.base == origin_tip, (final_train.base, origin_tip)
        assert final_train.tip != running_tip, "the re-gate is a fresh tip"
        _wait_done(gate_state_path(loops_root, final_train))

        tick_b = _make_loop(loops_root, repo, offload, remote="origin", push=True)
        outs_b = tick_b.run_once()
    finally:
        os.environ.pop("FAKE_GATE_RC", None)

    assert outs_b and outs_b[0].action == "landed", outs_b
    new_main = _git(repo, "rev-parse", "main")
    assert _git(bare, "rev-parse", "main") == new_main, "the land reached origin"
    # nothing was forced: the settled origin tip is an ancestor of what landed.
    assert _git(repo, "rev-list", "--max-count=1", origin_tip, "--not", new_main) == ""
    events = _ledger_events(loops_root)
    assert any(e["event"] == "merged" and e["id"] == id_s for e in events), events


def test_diverged_origin_is_refused_loudly_and_never_forced(tmp_path):
    """MUTATION GUARD (divergence-force): making the sync reset/force onto
    origin, or ff past a divergence, fails HERE."""
    repo = _init_repo(tmp_path / "repo")
    bare = _bare_origin(tmp_path, repo)
    o1 = _advance_origin(tmp_path, bare, "pr.txt", "from a merged PR\n")
    # ... and local main independently grows a commit origin does not have.
    (repo / "local.txt").write_text("local only\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "local-only commit")
    l1 = _git(repo, "rev-parse", "main")
    _commit_on(repo, "cand-d", l1, "d.txt", "DDD\n")
    loops_root = tmp_path / "loops"
    _write_candidate(loops_root, "d" * 64, "cand-d", l1, ["d.txt"])

    loop = _make_loop(loops_root, repo, _fake_offload(tmp_path),
                      remote="origin", push=False)
    outs = loop.run_once()

    # NEITHER side moved: no fast-forward, no reset, no force-push.
    assert _git(repo, "rev-parse", "main") == l1, "local main must not move on divergence"
    assert _git(bare, "rev-parse", "main") == o1, "origin must never be forced"
    # loud: an alert AND a ledger event naming the divergence
    assert any("DIVERGED" in a for a in loop.alerts), loop.alerts
    diverged = [e for e in _ledger_events(loops_root)
                if (e.get("detail") or {}).get("kind") == "main-diverged-from-origin"]
    assert diverged, "divergence must reach the ledger, not only an alert"
    assert diverged[0]["detail"]["class"] == "instrument-error"
    # ... and assembly still ran on the local view (a fetch outage or a
    # divergence must not halt the pipeline).
    assert outs and outs[0].action == "dispatched", outs


def test_diverged_origin_alert_is_edge_triggered_not_a_storm(tmp_path):
    repo = _init_repo(tmp_path / "repo")
    bare = _bare_origin(tmp_path, repo)
    _advance_origin(tmp_path, bare, "pr.txt", "from a merged PR\n")
    (repo / "local.txt").write_text("local only\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "local-only commit")
    loops_root = tmp_path / "loops"

    for _ in range(3):
        _make_loop(loops_root, repo, _fake_offload(tmp_path),
                   remote="origin", push=False).run_once()
    diverged = [e for e in _ledger_events(loops_root)
                if (e.get("detail") or {}).get("kind") == "main-diverged-from-origin"]
    assert len(diverged) == 1, "an unchanged divergence is alerted ONCE, never per tick"


def test_content_free_divergence_autoheals_to_origin(tmp_path):
    """The 2026-08-14 stall class: a divergence whose LOCAL-only commits are
    content-free (empty diff vs the merge base — their content already reached
    origin, e.g. via a merged PR) AUTO-RECONCILES onto origin: salvage branch
    kept, ledger evidence written, no degraded refusal, no human."""
    repo = _init_repo(tmp_path / "repo")
    bare = _bare_origin(tmp_path, repo)
    o1 = _advance_origin(tmp_path, bare, "pr.txt", "from a merged PR\n")
    # Local main re-merges a lane whose CONTENT is already on origin: the
    # commit object is new, the tree is not.
    _git(repo, "commit", "--allow-empty", "-qm",
         "re-merge of already-landed lane (content-free)")
    l1 = _git(repo, "rev-parse", "main")
    loops_root = tmp_path / "loops"

    loop = _make_loop(loops_root, repo, _fake_offload(tmp_path),
                      remote="origin", push=False)
    loop.run_once()

    assert _git(repo, "rev-parse", "main") == o1, "main must heal onto origin"
    assert _git(bare, "rev-parse", "main") == o1, "origin must never move"
    salvage = [ln.strip().lstrip("* ") for ln in
               _git(repo, "branch", "--list", "salvage/gl-contentfree-*").splitlines()]
    assert salvage, "the old local tip must be preserved on a salvage branch"
    assert _git(repo, "rev-parse", salvage[0]) == l1, "salvage must pin the old tip"
    healed = [e for e in _ledger_events(loops_root)
              if (e.get("detail") or {}).get("kind") == "main-reconciled-content-free"]
    assert healed, "the heal must reach the ledger with evidence"
    assert healed[0]["detail"]["salvage_ref"] == salvage[0]
    # Sol F3: the record must satisfy the repo's own ledger-event contract —
    # an append-only log must never contain a self-contradicting record.
    assert gl.ledger_event_problems(healed[0]) == []
    assert not any("DIVERGED" in a for a in loop.alerts), (
        "a healed content-free divergence must not raise the fail-closed alert")
    # Sol F4: the write-ahead intent marker is cleaned up on full success.
    # (Marker names are per-heal — Sol F8 — keyed on the healed local sha.)
    assert not loop._alert_marker_path(
        f"content-free-heal-intent-{l1}-{o1}").exists()


def test_content_free_autoheal_never_fires_on_real_content(tmp_path):
    """MUTATION GUARD (autoheal-overreach): widening the carve-out past the
    empty-diff proof fails HERE — a divergence with real local content must
    still be refused loudly and move nothing (see also
    test_diverged_origin_is_refused_loudly_and_never_forced)."""
    repo = _init_repo(tmp_path / "repo")
    bare = _bare_origin(tmp_path, repo)
    o1 = _advance_origin(tmp_path, bare, "pr.txt", "from a merged PR\n")
    (repo / "local.txt").write_text("real local content\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "local-only commit WITH content")
    l1 = _git(repo, "rev-parse", "main")
    loops_root = tmp_path / "loops"

    loop = _make_loop(loops_root, repo, _fake_offload(tmp_path),
                      remote="origin", push=False)
    loop.run_once()

    assert _git(repo, "rev-parse", "main") == l1, "real divergence must not move main"
    assert _git(bare, "rev-parse", "main") == o1
    assert any("DIVERGED" in a for a in loop.alerts), loop.alerts
    assert not _git(repo, "branch", "--list", "salvage/gl-contentfree-*").strip(), (
        "no salvage branch may be minted for a refused divergence")


def test_content_free_autoheal_refuses_dirty_tracked_worktree(tmp_path):
    """A peer session's uncommitted TRACKED edit in the serving checkout blocks
    the heal entirely (fail-closed to the divergence path) — the carve-out
    never risks another writer's state."""
    repo = _init_repo(tmp_path / "repo")
    bare = _bare_origin(tmp_path, repo)
    _advance_origin(tmp_path, bare, "pr.txt", "from a merged PR\n")
    _git(repo, "commit", "--allow-empty", "-qm", "content-free local")
    l1 = _git(repo, "rev-parse", "main")
    (repo / "README.md").write_text("peer's uncommitted edit\n", encoding="utf-8")
    loops_root = tmp_path / "loops"

    loop = _make_loop(loops_root, repo, _fake_offload(tmp_path),
                      remote="origin", push=False)
    loop.run_once()

    assert _git(repo, "rev-parse", "main") == l1, "no heal over a dirty tracked file"
    assert any("DIVERGED" in a for a in loop.alerts), loop.alerts
    assert (repo / "README.md").read_text(encoding="utf-8") == "peer's uncommitted edit\n"


def test_content_free_autoheal_refuses_when_a_commit_slips_in(tmp_path):
    """Grok FINDING-1 (the TOCTOU): a peer commit landing on serving main
    AFTER the content-free proof but BEFORE the ref move must survive. The
    swap is a compare-and-swap on the proved snapshot, so a moved tip refuses
    atomically: nothing moves, no salvage litter, the fail-closed path runs."""
    repo = _init_repo(tmp_path / "repo")
    bare = _bare_origin(tmp_path, repo)
    o1 = _advance_origin(tmp_path, bare, "pr.txt", "from a merged PR\n")
    _git(repo, "commit", "--allow-empty", "-qm", "content-free local")
    stale_snapshot = _git(repo, "rev-parse", "main")
    # ... and the peer's REAL commit lands after the snapshot was proved.
    (repo / "peer.txt").write_text("peer's real work\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "peer commit in the TOCTOU window")
    peer_tip = _git(repo, "rev-parse", "main")
    loops_root = tmp_path / "loops"

    loop = _make_loop(loops_root, repo, _fake_offload(tmp_path),
                      remote="origin", push=False)
    healed = loop._content_free_reconcile(stale_snapshot, o1, why="test-toctou")

    assert healed is False, "a moved tip must refuse the swap"
    assert _git(repo, "rev-parse", "main") == peer_tip, (
        "the peer's commit must remain exactly where it was")
    assert _git(bare, "rev-parse", "main") == o1
    assert not _git(repo, "branch", "--list", "salvage/gl-contentfree-*").strip(), (
        "a refused heal must not leave salvage litter")


def test_content_free_autoheal_refuses_ignored_file_collision(tmp_path):
    """Sol F2: porcelain never lists ignored files, so the tracked-clean probe
    cannot see an ignored file with real content at a path origin's tree
    begins to TRACK — materializing origin would overwrite it with no salvage
    ref and no git object to recover from. The added-path collision guard
    must refuse the heal and leave the file byte-identical."""
    repo = _init_repo(tmp_path / "repo")
    bare = _bare_origin(tmp_path, repo)
    # Origin begins tracking secrets.env (e.g. a merged PR adds a template)...
    _advance_origin(tmp_path, bare, "secrets.env", "origin version\n")
    # ...while locally that path is IGNORED (repo-local exclude, untracked)
    # and holds real, uncommitted work.
    (repo / ".git" / "info" / "exclude").write_text("secrets.env\n",
                                                    encoding="utf-8")
    (repo / "secrets.env").write_text("irreplaceable local ignored work\n",
                                      encoding="utf-8")
    _git(repo, "commit", "--allow-empty", "-qm", "content-free local")
    l1 = _git(repo, "rev-parse", "main")
    loops_root = tmp_path / "loops"

    loop = _make_loop(loops_root, repo, _fake_offload(tmp_path),
                      remote="origin", push=False)
    loop.run_once()

    assert _git(repo, "rev-parse", "main") == l1, (
        "a heal that would overwrite an ignored file must refuse")
    assert (repo / "secrets.env").read_text(encoding="utf-8") == (
        "irreplaceable local ignored work\n")
    assert any("DIVERGED" in a for a in loop.alerts), loop.alerts


def test_content_free_autoheal_refuses_broken_symlink_collision(tmp_path):
    """Sol F5 (round 2): a BROKEN symlink at a collided path is real local
    state; Path.exists() follows the link and lies about its presence. The
    guard must use lexists semantics and refuse."""
    repo = _init_repo(tmp_path / "repo")
    bare = _bare_origin(tmp_path, repo)
    _advance_origin(tmp_path, bare, "secrets.env", "origin version\n")
    (repo / ".git" / "info" / "exclude").write_text("secrets.env\n",
                                                    encoding="utf-8")
    (repo / "secrets.env").symlink_to(repo / "does-not-exist-target")
    _git(repo, "commit", "--allow-empty", "-qm", "content-free local")
    l1 = _git(repo, "rev-parse", "main")
    loops_root = tmp_path / "loops"

    loop = _make_loop(loops_root, repo, _fake_offload(tmp_path),
                      remote="origin", push=False)
    loop.run_once()

    assert _git(repo, "rev-parse", "main") == l1, (
        "a heal that would replace a broken symlink must refuse")
    assert (repo / "secrets.env").is_symlink(), "the symlink must survive"
    assert any("DIVERGED" in a for a in loop.alerts), loop.alerts


def test_content_free_heal_double_failure_poisons_the_daemon(
        tmp_path, monkeypatch):
    """Sol F7 (round 2): read-tree refused AND the compensating swap-back
    refused — ref at origin, worktree old. Continuing to land would compound
    the inconsistency; the daemon must freeze behind a durable poison marker,
    like the land path's push-and-rollback double failure."""
    repo = _init_repo(tmp_path / "repo")
    bare = _bare_origin(tmp_path, repo)
    o1 = _advance_origin(tmp_path, bare, "pr.txt", "from a merged PR\n")
    _git(repo, "commit", "--allow-empty", "-qm", "content-free local")
    l1 = _git(repo, "rev-parse", "main")
    _git(repo, "fetch", "-q", "origin")
    loops_root = tmp_path / "loops"
    loop = _make_loop(loops_root, repo, _fake_offload(tmp_path),
                      remote="origin", push=False)

    real_git = gl.git

    def failing_git(repo_arg, *args, **kwargs):
        if args and args[0] == "read-tree":
            return 128, "", "injected read-tree refusal"
        # the compensating swap-back is the SECOND update-ref call (old<-new)
        if args[:2] == ("update-ref", "refs/heads/main") and \
                args[2:] == (l1, o1):
            return 128, "", "injected swap-back refusal"
        return real_git(repo_arg, *args, **kwargs)

    monkeypatch.setattr(gl, "git", failing_git)
    with pytest.raises(gl.DaemonPoisoned):
        loop._content_free_reconcile(l1, o1, why="test-f7")

    assert loop.poisoned(), "double failure must freeze the daemon"
    body = json.loads(loop._poison_path().read_text(encoding="utf-8"))
    assert body["salvage_ref"].startswith("salvage/gl-contentfree-")
    assert "read_tree_error" in body and "rollback_error" in body
    assert any("POISONED" in a for a in loop.alerts), loop.alerts
    salvages = _git(repo, "branch", "--list", "salvage/gl-contentfree-*")
    assert salvages.strip(), "the salvage ref must survive as the remedy anchor"


def test_late_collision_double_failure_also_poisons(tmp_path, monkeypatch):
    """Grok delta finding D3-1: the late-collision refusal branch has the same
    CAS-succeeded/compensation-failed shape as the read-tree branch and must
    poison identically — salvage and intent marker surviving as remedy
    anchors, never a false 'compensated' log."""
    repo = _init_repo(tmp_path / "repo")
    bare = _bare_origin(tmp_path, repo)
    o1 = _advance_origin(tmp_path, bare, "secrets.env", "origin version\n")
    _git(repo, "commit", "--allow-empty", "-qm", "content-free local")
    l1 = _git(repo, "rev-parse", "main")
    _git(repo, "fetch", "-q", "origin")
    loops_root = tmp_path / "loops"
    loop = _make_loop(loops_root, repo, _fake_offload(tmp_path),
                      remote="origin", push=False)

    real_git = gl.git

    def tricky_git(repo_arg, *args, **kwargs):
        if args[:2] == ("update-ref", "refs/heads/main") and \
                args[2:] == (o1, l1):
            # The CAS itself: succeed, but a peer file lands at the collided
            # path in the same instant.
            (repo / "secrets.env").write_text("late arrival\n",
                                              encoding="utf-8")
            return real_git(repo_arg, *args, **kwargs)
        if args[:2] == ("update-ref", "refs/heads/main") and \
                args[2:] == (l1, o1):
            return 128, "", "injected compensation refusal"
        return real_git(repo_arg, *args, **kwargs)

    monkeypatch.setattr(gl, "git", tricky_git)
    with pytest.raises(gl.DaemonPoisoned):
        loop._content_free_reconcile(l1, o1, why="test-d3-1")

    assert loop.poisoned(), "late-collision double failure must freeze the daemon"
    body = json.loads(loop._poison_path().read_text(encoding="utf-8"))
    assert body["collision_path"] == "secrets.env"
    assert body["salvage_ref"].startswith("salvage/gl-contentfree-")
    assert _git(repo, "branch", "--list", "salvage/gl-contentfree-*").strip(), (
        "salvage must survive as the remedy anchor")
    marker = loop._alert_marker_path(
        f"content-free-heal-intent-{l1}-{o1}")
    assert marker.exists(), "the intent marker must survive as evidence"
    assert not any("compensated" in ln for ln in loop.lines if "test-d3-1" in ln), (
        "no false 'compensated' claim may be logged")


def test_content_free_autoheal_refuses_unicode_path_collision(tmp_path):
    """Sol F24 (round 4, race-free): git C-quotes non-ASCII names in
    line-oriented diff output; probing the quoted literal finds nothing and
    the real file gets overwritten. The guard must parse NUL-delimited raw
    bytes and refuse."""
    repo = _init_repo(tmp_path / "repo")
    bare = _bare_origin(tmp_path, repo)
    _advance_origin(tmp_path, bare, "caf\u00e9.env", "origin version\n")
    (repo / ".git" / "info" / "exclude").write_text("caf\u00e9.env\n",
                                                    encoding="utf-8")
    (repo / "caf\u00e9.env").write_text("irreplaceable local bytes\n",
                                        encoding="utf-8")
    _git(repo, "commit", "--allow-empty", "-qm", "content-free local")
    l1 = _git(repo, "rev-parse", "main")
    loops_root = tmp_path / "loops"

    loop = _make_loop(loops_root, repo, _fake_offload(tmp_path),
                      remote="origin", push=False)
    loop.run_once()

    assert _git(repo, "rev-parse", "main") == l1, (
        "a unicode-named collision must refuse the heal")
    assert (repo / "caf\u00e9.env").read_text(encoding="utf-8") == (
        "irreplaceable local bytes\n")
    assert any("DIVERGED" in a for a in loop.alerts), loop.alerts


def test_content_free_heal_evidence_survives_ledger_append_failure(
        tmp_path, monkeypatch):
    """Sol F4: once the ref has moved, nothing in the repository proves a heal
    happened — the write-ahead intent marker must survive a ledger-append
    failure so the evidence is durable and re-emittable."""
    repo = _init_repo(tmp_path / "repo")
    bare = _bare_origin(tmp_path, repo)
    o1 = _advance_origin(tmp_path, bare, "pr.txt", "from a merged PR\n")
    _git(repo, "commit", "--allow-empty", "-qm", "content-free local")
    l1 = _git(repo, "rev-parse", "main")
    _git(repo, "fetch", "-q", "origin")
    loops_root = tmp_path / "loops"

    loop = _make_loop(loops_root, repo, _fake_offload(tmp_path),
                      remote="origin", push=False)

    def boom(event: dict) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(loop, "_append_ledger", boom)
    healed = loop._content_free_reconcile(l1, o1, why="test-f4")

    assert healed is True, "the heal itself already happened"
    assert _git(repo, "rev-parse", "main") == o1
    marker = loop._alert_marker_path(
        f"content-free-heal-intent-{l1}-{o1}")
    assert marker.exists(), "durable evidence must survive the append failure"
    data = json.loads(marker.read_text(encoding="utf-8"))
    assert data["local_sha"] == l1 and data["origin_sha"] == o1
    assert data["salvage_ref"].startswith("salvage/gl-contentfree-")
    assert any("ledger append" in a for a in loop.alerts), loop.alerts


def test_content_free_heal_verify_failure_emits_its_own_ledger_kind(
        tmp_path, monkeypatch):
    """Grok FINDING-R2-3: the post-swap verify-failure branch must append its
    distinct `main-reconcile-verify-failed` instrument record — a regression
    that silently dropped it would otherwise pass CI."""
    repo = _init_repo(tmp_path / "repo")
    bare = _bare_origin(tmp_path, repo)
    o1 = _advance_origin(tmp_path, bare, "pr.txt", "from a merged PR\n")
    _git(repo, "commit", "--allow-empty", "-qm", "content-free local")
    l1 = _git(repo, "rev-parse", "main")
    _git(repo, "fetch", "-q", "origin")
    loops_root = tmp_path / "loops"
    loop = _make_loop(loops_root, repo, _fake_offload(tmp_path),
                      remote="origin", push=False)

    real_git = gl.git

    def lying_git(repo_arg, *args, **kwargs):
        # After the swap, the verify's rev-parse sees a THIRD sha — the state
        # the code must classify as verify-failure.
        if args[:2] == ("rev-parse", "--verify") and args[2:] == ("main",):
            return 0, "f" * 40, ""
        return real_git(repo_arg, *args, **kwargs)

    monkeypatch.setattr(gl, "git", lying_git)
    healed = loop._content_free_reconcile(l1, o1, why="test-verify-fail")

    assert healed is False
    bad = [e for e in _ledger_events(loops_root)
           if (e.get("detail") or {}).get("kind") == "main-reconcile-verify-failed"]
    assert bad, "verify failure must emit its own distinct ledger kind"
    assert bad[0]["detail"]["salvage_ref"].startswith("salvage/gl-contentfree-")
    assert any("verify FAILED" in a for a in loop.alerts), loop.alerts


def test_content_free_heal_escalates_after_three_in_24h(tmp_path):
    """Grok follow-up on #410: the heal is a safety net, not a fix. A 4th
    content-free heal inside 24h means a writer is repeatedly re-merging
    content that already landed on origin -- the ALERT text (not the heal
    itself, not the ledger record) must say so, so an operator finds the
    producer instead of watching the heal keep firing quietly."""
    repo = _init_repo(tmp_path / "repo")
    bare = _bare_origin(tmp_path, repo)
    o1 = _advance_origin(tmp_path, bare, "pr.txt", "from a merged PR\n")
    _git(repo, "commit", "--allow-empty", "-qm", "content-free local")
    l1 = _git(repo, "rev-parse", "main")
    _git(repo, "fetch", "-q", "origin")
    loops_root = tmp_path / "loops"
    loop = _make_loop(loops_root, repo, _fake_offload(tmp_path),
                      remote="origin", push=False)

    # Seed three PRIOR synthetic heal records via the real transport -- it
    # stamps `ts` to the current append clock regardless of what is passed,
    # which lands them well inside the 24h window (see the sibling
    # stale-events test for why that stamping matters).
    for i in range(3):
        loop._append_ledger({
            "ts": gl._iso(gl._now()), "role": gl.ROLE, "event": "instrument_error",
            "id": f"sha256:{'a' * 63}{i}", "actor": gl.ACTOR,
            "detail": {"kind": "main-reconciled-content-free", "area": "tooling",
                       "class": "instrument-error", "reason": "synthetic prior heal",
                       "remote": "origin", "local_sha": "b" * 40, "origin_sha": "c" * 40,
                       "merge_base": "d" * 40,
                       "salvage_ref": f"salvage/gl-contentfree-synthetic-{i}",
                       "falsifier": "synthetic"}})

    healed = loop._content_free_reconcile(l1, o1, why="test-heal-rate")

    assert healed is True
    escalated = [a for a in loop.alerts if "3+ content-free heals in 24h" in a]
    assert escalated, loop.alerts
    assert "find and stop the producer" in escalated[0]
    assert "heals are a safety net, not the fix" in escalated[0]
    # The escalation is alert-text only -- the ledger record's own `reason`
    # (and the heal's mechanics) are unaffected, per the no-behavior-change
    # contract.
    real_heal = [e for e in _ledger_events(loops_root)
                 if (e.get("detail") or {}).get("kind") == "main-reconciled-content-free"
                 and (e.get("detail") or {}).get("local_sha") == l1]
    assert real_heal, "the real heal must still reach the ledger"
    assert "3+ content-free heals" not in real_heal[0]["detail"]["reason"]


def test_content_free_heal_count_ignores_events_older_than_24h(tmp_path):
    """The escalation is a RATE, not a lifetime total: heals from more than
    24h ago must not count toward the 3-in-24h threshold.

    Written DIRECTLY to ledger.jsonl (not via `_append_ledger`): the
    transport itself stamps `ts` to the real append clock and moves any
    caller-supplied value to `ts_claims` (`ledger_write.stamp_ts` -- "the
    append clock always wins"), so a synthetic old `ts` can only be seeded
    by bypassing that transport, exactly like the isolation-ledger fixture
    at `_write_candidate(..., with_ledger=True)` above does."""
    repo = _init_repo(tmp_path / "repo")
    bare = _bare_origin(tmp_path, repo)
    o1 = _advance_origin(tmp_path, bare, "pr.txt", "from a merged PR\n")
    _git(repo, "commit", "--allow-empty", "-qm", "content-free local")
    l1 = _git(repo, "rev-parse", "main")
    _git(repo, "fetch", "-q", "origin")
    loops_root = tmp_path / "loops"
    loop = _make_loop(loops_root, repo, _fake_offload(tmp_path),
                      remote="origin", push=False)

    stale_ts = gl._iso(gl._now() - gl.timedelta(hours=25))
    loops_root.mkdir(parents=True, exist_ok=True)
    with open(loops_root / "ledger.jsonl", "a", encoding="utf-8") as fh:
        for i in range(3):
            fh.write(json.dumps({
                "ts": stale_ts, "role": gl.ROLE, "event": "instrument_error",
                "id": f"sha256:{'e' * 63}{i}", "actor": gl.ACTOR,
                "detail": {"kind": "main-reconciled-content-free", "area": "tooling",
                           "class": "instrument-error", "reason": "stale prior heal",
                           "remote": "origin", "local_sha": "b" * 40, "origin_sha": "c" * 40,
                           "merge_base": "d" * 40,
                           "salvage_ref": f"salvage/gl-contentfree-stale-{i}",
                           "falsifier": "synthetic"}}) + "\n")

    assert loop._content_free_heal_count_24h() == 0, (
        "seeded records are 25h old and must not count toward the 24h window")

    healed = loop._content_free_reconcile(l1, o1, why="test-heal-rate-stale")

    assert healed is True
    assert not any("3+ content-free heals in 24h" in a for a in loop.alerts), loop.alerts


def test_content_free_autoheal_kill_switch(tmp_path, monkeypatch):
    """GATE_LOOP_AUTOHEAL_CONTENT_FREE=0 restores the old always-refuse
    behavior exactly."""
    monkeypatch.setenv("GATE_LOOP_AUTOHEAL_CONTENT_FREE", "0")
    repo = _init_repo(tmp_path / "repo")
    bare = _bare_origin(tmp_path, repo)
    _advance_origin(tmp_path, bare, "pr.txt", "from a merged PR\n")
    _git(repo, "commit", "--allow-empty", "-qm", "content-free local")
    l1 = _git(repo, "rev-parse", "main")
    loops_root = tmp_path / "loops"

    loop = _make_loop(loops_root, repo, _fake_offload(tmp_path),
                      remote="origin", push=False)
    loop.run_once()

    assert _git(repo, "rev-parse", "main") == l1, "kill switch must disable the heal"
    assert any("DIVERGED" in a for a in loop.alerts), loop.alerts


def test_fetch_failure_proceeds_on_the_stale_view_and_says_so(tmp_path):
    repo = _init_repo(tmp_path / "repo")
    m0 = _git(repo, "rev-parse", "HEAD")
    _git(repo, "remote", "add", "origin", str(tmp_path / "nonexistent-remote.git"))
    _commit_on(repo, "cand-f", m0, "f.txt", "FFF\n")
    loops_root = tmp_path / "loops"
    _write_candidate(loops_root, "f" * 64, "cand-f", m0, ["f.txt"])

    loop = _make_loop(loops_root, repo, _fake_offload(tmp_path),
                      remote="origin", push=False)
    outs = loop.run_once()

    assert outs and outs[0].action == "dispatched", outs      # fail-OPEN on fetch
    assert _git(repo, "rev-parse", "main") == m0
    assert any("fetch" in ln and "stale" in ln for ln in loop.lines), loop.lines
    assert not any("DIVERGED" in a for a in loop.alerts), loop.alerts


# --- (2) non-ff push -> re-anchor, never an identical retry --------------------


def test_non_ff_push_reanchors_and_never_repeats_the_identical_push(tmp_path):
    """The push refusal is REAL git output from a REAL bare remote that is
    ahead, so the classifier is tested against git, not against a fixture."""
    repo = _init_repo(tmp_path / "repo")
    m0 = _git(repo, "rev-parse", "HEAD")
    bare = _bare_origin(tmp_path, repo)
    o1 = _advance_origin(tmp_path, bare, "pr.txt", "from a merged PR\n")
    loops_root = tmp_path / "loops"
    (loops_root / "state").mkdir(parents=True, exist_ok=True)
    train = _manual_train(repo, m0)

    loop = _make_loop(loops_root, repo, _fake_offload(tmp_path),
                      remote="origin", push=True)
    calls: list = []
    from bridge.integration import GateVerdict
    v = GateVerdict("pass", 0, "", "gate passed", None, "", 1.0)
    import unittest.mock as _mock
    with _mock.patch.object(gl, "git", _recording_git(calls)):
        out = loop.on_pass(train, v)
        out2 = loop.on_pass(train, v)

    pushes = [i for i, c in enumerate(calls) if c and c[0] == "push"]
    fetches = [i for i, c in enumerate(calls) if c and c[0] == "fetch"]
    assert len(pushes) == 1, f"the identical push must NOT be retried: {calls}"
    assert fetches and fetches[-1] > pushes[0], "the refusal must trigger a fetch"
    # re-anchored: local main now carries the PR commit, by fast-forward only
    assert _git(repo, "rev-parse", "main") == o1
    assert _git(bare, "rev-parse", "main") == o1, "origin must never be forced"
    assert not any("--force" in c or "-f" in c or "--force-with-lease" in c
                   for c in calls if c and c[0] == "push"), calls
    assert out.action == "instrument" and "anchor" in out.detail.lower(), out
    assert out2.action == "skipped", out2      # stale base now, so no second push
    non_ff = [e for e in _ledger_events(loops_root)
              if (e.get("detail") or {}).get("kind") == "push_non_ff"]
    assert non_ff, "a non-ff push must be distinguishable from a push instrument fault"


def test_non_ff_push_that_the_reanchor_cannot_change_still_parks(tmp_path):
    """A refusal that CLAIMS non-ff but whose re-anchor changes nothing is an
    unchanged input again — it must consume the bounded budget and park, never
    loop forever."""
    repo = _init_repo(tmp_path / "repo")
    m0 = _git(repo, "rev-parse", "HEAD")
    loops_root = tmp_path / "loops"
    (loops_root / "state").mkdir(parents=True, exist_ok=True)
    train = _manual_train(repo, m0)
    from bridge.integration import GateVerdict
    v = GateVerdict("pass", 0, "", "gate passed", None, "", 1.0)
    non_ff_text = (" ! [rejected]        main -> main (fetch first)\n"
                   "error: failed to push some refs to 'origin'\n"
                   "hint: Updates were rejected because the remote contains work "
                   "that you do not have locally.")
    import unittest.mock as _mock

    for _attempt in range(1, gl.PUSH_RETRY_LIMIT + 1):
        loop = _make_loop(loops_root, repo, _fake_offload(tmp_path),
                          remote="origin", push=True)
        with _mock.patch.object(gl, "git", _recording_git(
                [], push_error=non_ff_text, fail_fetch=True)):
            out = loop.on_pass(train, v)
        assert _git(repo, "rev-parse", "main") == m0, "always rolled back"
    assert "park" in out.detail.lower(), out
    st = json.loads(gate_state_path(loops_root, train).read_text())
    assert st["state"] == "closed" and st["disposition"] == "push-parked", st


def test_non_ff_reanchor_holds_base_for_a_running_sibling_gate(tmp_path):
    """CHURN-GUARD PROPAGATION (Grok MAJOR-1). Under MAX_CONCURRENT_GATES > 1, a
    passed train whose push is refused non-ff must NOT re-anchor the base while a
    SIBLING gate is still running — that advance would orphan the sibling into a
    from-scratch re-gate, the exact churn the tick-top guard prevents. The base is
    HELD; the passed train re-attempts next tick; it is NOT parked and spends no
    push-retry budget. Contrast: with no sibling running the identical refusal
    re-anchors and advances (test_non_ff_push_reanchors...). Removing the
    `hold_base` propagation in `_on_push_refused` fails HERE."""
    repo = _init_repo(tmp_path / "repo")
    m0 = _git(repo, "rev-parse", "HEAD")
    bare = _bare_origin(tmp_path, repo)
    o1 = _advance_origin(tmp_path, bare, "pr.txt", "from a merged PR\n")
    loops_root = tmp_path / "loops"
    (loops_root / "state" / "gates").mkdir(parents=True, exist_ok=True)
    train = _manual_train(repo, m0)

    # A SIBLING gate is genuinely in flight: state=running, deadline in the FUTURE
    # so the orphan sweep never reaps it. This is what makes _running_gate_count()
    # (which excludes this train — it has already PASSED, so its state is 'done')
    # report a live peer.
    sibling = loops_root / "state" / "gates" / ("train__gl-sibling@" + "c" * 40 + ".json")
    sibling.write_text(json.dumps({
        "state": "running", "train": "train/gl-sibling", "tip": "c" * 40,
        "base": m0, "members": ["sha256:" + "c" * 64],
        "deadline": time.time() + gl.GATE_DEADLINE_S, "mode": "direct",
    }), encoding="utf-8")

    loop = _make_loop(loops_root, repo, _fake_offload(tmp_path),
                      remote="origin", push=True)
    assert loop._running_gate_count() >= 1, "the sibling must read as in-flight"
    from bridge.integration import GateVerdict
    v = GateVerdict("pass", 0, "", "gate passed", None, "", 1.0)
    out = loop.on_pass(train, v)

    # the base was HELD at m0 — NOT advanced to o1, which would orphan the sibling.
    assert _git(repo, "rev-parse", "main") == m0, (
        "re-anchor advanced main under an in-flight sibling gate")
    assert _git(bare, "rev-parse", "main") == o1, "origin must never be forced"
    # non-terminal and explicitly a deferral; the passed train re-attempts later.
    assert out.action == "instrument" and "defer" in out.detail.lower(), out
    # NOT parked, and NO push-retry budget was consumed (no push_failed record).
    st_path = gate_state_path(loops_root, train)
    if st_path.exists():
        assert json.loads(st_path.read_text()).get("disposition") != "push-parked"
    events = _ledger_events(loops_root)
    assert not any((e.get("detail") or {}).get("kind") == "push_failed" for e in events), (
        "a deferred re-anchor must not record push_failed / park a GREEN train")


def test_on_push_refused_defers_then_releases_across_a_sibling_gate(tmp_path):
    """MINOR-1 release + mutation guard (Grok). Drive `_on_push_refused` through
    the full HOLD -> RELEASE transition, not just a static count check:

      HOLD    — while a sibling gate is running, the passed train's non-ff
                re-anchor is DEFERRED: the base is held at m0, the `push_deferred`
                row is EDGE-triggered (exactly one, not one per attempt), the edge
                marker is set, and NOTHING is parked or charged to the push budget.
      RELEASE — the sibling terminates; the SAME train's re-anchor now advances the
                base to origin and clears the edge marker.

    Removing the `_on_push_refused` hold_base propagation, the edge marker, or the
    `push_deferred` exclusion from the push-retry budget each fails HERE."""
    repo = _init_repo(tmp_path / "repo")
    m0 = _git(repo, "rev-parse", "HEAD")
    bare = _bare_origin(tmp_path, repo)
    o1 = _advance_origin(tmp_path, bare, "pr.txt", "from a merged PR\n")
    loops_root = tmp_path / "loops"
    (loops_root / "state" / "gates").mkdir(parents=True, exist_ok=True)
    train = _manual_train(repo, m0)

    # a sibling gate genuinely in flight (future deadline so it is never reaped).
    sib = loops_root / "state" / "gates" / ("train__gl-sib@" + "c" * 40 + ".json")
    sib.write_text(json.dumps({
        "state": "running", "train": "train/gl-sib", "tip": "c" * 40, "base": m0,
        "members": ["sha256:" + "c" * 64],
        "deadline": time.time() + gl.GATE_DEADLINE_S, "mode": "direct",
    }), encoding="utf-8")

    from bridge.integration import GateVerdict
    v = GateVerdict("pass", 0, "", "gate passed", None, "", 1.0)

    # HOLD: two deferral attempts (fresh --once processes) — base stays m0 and the
    # push_deferred row is emitted ONCE, not once per attempt.
    for _ in range(2):
        loop = _make_loop(loops_root, repo, _fake_offload(tmp_path),
                          remote="origin", push=True)
        out = loop.on_pass(train, v)
        assert out.action == "instrument" and "defer" in out.detail.lower(), out
        assert _git(repo, "rev-parse", "main") == m0, "base held while the sibling gates"
    deferred = [e for e in _ledger_events(loops_root)
                if (e.get("detail") or {}).get("kind") == "push_deferred"]
    assert len(deferred) == 1, f"push_deferred must be edge-triggered once: {deferred}"
    assert loop._push_deferred_marker().exists(), "the edge marker holds across ticks"
    assert not any((e.get("detail") or {}).get("kind") == "push_failed"
                   for e in _ledger_events(loops_root)), "a deferral spends no push budget"

    # RELEASE: the sibling finishes; the SAME train's re-anchor now advances.
    sib.unlink()
    loop2 = _make_loop(loops_root, repo, _fake_offload(tmp_path),
                       remote="origin", push=True)
    out2 = loop2.on_pass(train, v)
    assert _git(repo, "rev-parse", "main") == o1, "base advances once the sibling is gone"
    assert not loop2._push_deferred_marker().exists(), "the edge marker clears on release"
    assert out2.action == "instrument" and "anchor" in out2.detail.lower(), out2


def test_two_concurrently_deferred_trains_each_get_a_ledger_row(tmp_path):
    """FAVOURABLE-ABSENCE (Gemini F1). With MAX_CONCURRENT_GATES > 1 two different
    passed trains can BOTH be held for a running sibling in the same episode. The
    edge marker records a SET of deferred push_keys, so each train emits its OWN
    `push_deferred` row — a bare existence flag hid the 2nd+ train's deferral from
    the ledger (the exact favourable-absence class this estate refuses). Both
    trains get a row; a repeat of the SAME train still emits only once. Reverting
    the marker to bare existence fails HERE."""
    repo = _init_repo(tmp_path / "repo")
    m0 = _git(repo, "rev-parse", "HEAD")
    bare = _bare_origin(tmp_path, repo)
    _advance_origin(tmp_path, bare, "pr.txt", "from a merged PR\n")
    loops_root = tmp_path / "loops"
    (loops_root / "state" / "gates").mkdir(parents=True, exist_ok=True)

    def _real_train(name: str, filename: str) -> Train:
        # two DISTINCT real one-commit trains on m0 (distinct branch AND tip, so
        # distinct push_keys) that both ff-merge cleanly.
        _git(repo, "checkout", "-q", "-B", name, m0)
        (repo / filename).write_text("X\n", encoding="utf-8")
        _git(repo, "add", "-A")
        _git(repo, "commit", "-qm", f"{name}: {filename}")
        tip = _git(repo, "rev-parse", "HEAD")
        _git(repo, "checkout", "-q", "main")
        return Train(branch=name, base=m0, tip=tip,
                     members=[{"id": "sha256:" + filename[0] * 64, "branch": name,
                               "base": m0, "paths": [filename]}], paths=[filename])

    train_a = _real_train("train/gl-aaaa", "a.txt")
    train_b = _real_train("train/gl-bbbb", "b.txt")

    # a sibling gate genuinely in flight (future deadline so it is never reaped).
    sib = loops_root / "state" / "gates" / ("train__gl-sib@" + "c" * 40 + ".json")
    sib.write_text(json.dumps({
        "state": "running", "train": "train/gl-sib", "tip": "c" * 40, "base": m0,
        "members": ["sha256:" + "c" * 64],
        "deadline": time.time() + gl.GATE_DEADLINE_S, "mode": "direct",
    }), encoding="utf-8")

    from bridge.integration import GateVerdict
    v = GateVerdict("pass", 0, "", "gate passed", None, "", 1.0)

    # A defers, then B defers (same episode), then A defers AGAIN.
    for train in (train_a, train_b, train_a):
        loop = _make_loop(loops_root, repo, _fake_offload(tmp_path),
                          remote="origin", push=True)
        out = loop.on_pass(train, v)
        assert out.action == "instrument" and "defer" in out.detail.lower(), out
        assert _git(repo, "rev-parse", "main") == m0, "base held for every deferral"

    deferred = [e for e in _ledger_events(loops_root)
                if (e.get("detail") or {}).get("kind") == "push_deferred"]
    trains = {e["detail"]["train"] for e in deferred}
    assert train_a.branch in trains, "train A must have a push_deferred row"
    assert train_b.branch in trains, (
        "train B must NOT be suppressed by A's marker (favourable-absence)")
    # edge-triggered PER push_key: exactly two rows (A once, B once), not three.
    assert len(deferred) == 2, f"one row per distinct deferred train, no repeats: {deferred}"


def test_origin_sync_salvages_byte_identical_untracked_ff_blocker(tmp_path):
    """HARDENING (untracked-ff-collision deadlock, 2026-08-13). When the origin
    fast-forward is refused ONLY because an untracked working-tree file is
    byte-identical to a file the incoming commit adds, the sync SALVAGES that
    redundant copy into a stash and completes the ff — instead of pinning the land
    pipeline at zero (measured: a stray identical HANDOFF doc held the daemon at
    0/3 util for 26 sweeps until a human removed it). The salvaged copy is
    preserved, never destroyed."""
    repo = _init_repo(tmp_path / "repo")
    bare = _bare_origin(tmp_path, repo)
    body = "# identical plan\nwritten twice\n"
    origin_sha = _advance_origin(tmp_path, bare, "plan.md", body)
    # local holds a byte-IDENTICAL UNTRACKED copy of the file origin's PR adds.
    (repo / "plan.md").write_text(body, encoding="utf-8")
    assert "?? plan.md" in _git(repo, "status", "--porcelain"), "precondition: untracked collision"
    loops_root = tmp_path / "loops"
    (loops_root / "state").mkdir(parents=True, exist_ok=True)
    loop = _make_loop(loops_root, repo, _fake_offload(tmp_path), remote="origin", push=True)

    assert loop._sync_main_from_origin(why="test") == "synced"
    assert _git(repo, "rev-parse", "main") == origin_sha, "main advanced to origin"
    assert (repo / "plan.md").read_text() == body, "the ff installed the identical tracked copy"
    assert not _git(repo, "status", "--porcelain").strip(), "worktree clean after salvage+ff"
    assert "origin-sync-salvage" in _git(repo, "stash", "list"), (
        "the redundant copy must be PRESERVED in a stash, not destroyed")


def test_origin_sync_refuses_when_untracked_ff_blocker_has_distinct_content(tmp_path):
    """FAIL-CLOSED companion to the salvage hardening. A colliding untracked file
    whose content DIFFERS from what the ff would install carries unique work, so
    the sync must NOT touch it: the advance stays refused ('ff-refused'), main does
    not move, and the operator's copy is byte-for-byte intact."""
    repo = _init_repo(tmp_path / "repo")
    bare = _bare_origin(tmp_path, repo)
    _advance_origin(tmp_path, bare, "plan.md", "origin's committed content\n")
    local_body = "MY UNIQUE UNCOMMITTED WORK -- do not destroy\n"
    (repo / "plan.md").write_text(local_body, encoding="utf-8")
    local_sha = _git(repo, "rev-parse", "main")
    loops_root = tmp_path / "loops"
    (loops_root / "state").mkdir(parents=True, exist_ok=True)
    loop = _make_loop(loops_root, repo, _fake_offload(tmp_path), remote="origin", push=True)

    assert loop._sync_main_from_origin(why="test") == "ff-refused"
    assert _git(repo, "rev-parse", "main") == local_sha, "main did NOT move"
    assert (repo / "plan.md").read_text() == local_body, "the operator's distinct work is untouched"
    assert _git(repo, "stash", "list").strip() == "", "nothing salvaged on the fail-closed path"


# --- BEGIN untracked-ff-salvage safety regressions (r2) ---------------------
#
# Promoted from the three executable repros filed against the first cut of the
# salvage (crit-20260813T210430Z): a lossy clean filter making DIFFERENT bytes
# read as identical, a collision filename that is itself pathspec MAGIC widening
# the salvage over unrelated untracked work, and an operator edit landing between
# the identity check and the stash. All three returned the FAVOURABLE answer
# ("synced", main advances) on an input that could not be proven safe. Each test
# asserts the same five things: the sync REFUSES, main does not move, the RAW
# on-disk bytes survive, unrelated untracked files survive, and no stash entry is
# created (nothing was touched at all).


def _sync_env(tmp_path, repo):
    """The loop + loops_root a `_sync_main_from_origin` test drives."""
    loops_root = tmp_path / "loops"
    (loops_root / "state").mkdir(parents=True, exist_ok=True)
    return _make_loop(loops_root, repo, _fake_offload(tmp_path), remote="origin", push=True)


def _lossy_clean_filter(tmp_path, repo, pattern="plan.md"):
    """Configure a clean filter that COLLAPSES every input to one canonical blob —
    the shape under which `git hash-object` (filters applied) declares distinct
    working-tree bytes identical, and `git stash` stores only the collapsed form."""
    cleaner = tmp_path / "collapse-clean"
    cleaner.write_text("#!/bin/sh\ncat >/dev/null\nprintf 'CANONICAL\\n'\n", encoding="utf-8")
    cleaner.chmod(0o755)
    _git(repo, "config", "filter.collapse.clean", str(cleaner))
    _git(repo, "config", "filter.collapse.smudge", "cat")
    (repo / ".gitattributes").write_text(f"{pattern} filter=collapse\n", encoding="utf-8")
    _git(repo, "add", ".gitattributes")
    _git(repo, "commit", "-qm", "declare lossy clean filter")


def test_origin_sync_refuses_untracked_ff_salvage_under_a_lossy_clean_filter(tmp_path):
    """BLOCKER (lossy-clean-filter-data-loss). `git hash-object` WITHOUT
    `--no-filters` hashes what the CLEAN FILTER produces, not the bytes on disk,
    so under a lossy filter the operator's unique file hashes to the incoming
    blob and reads as "redundant" — and `git stash` applies that same filter, so
    the stash entry holds only the filtered content and the unique bytes exist
    NOWHERE. The identity must be proven on RAW bytes, and a refusal here costs
    one landing tick while the favourable answer costs an unrecoverable file."""
    repo = _init_repo(tmp_path / "repo")
    _lossy_clean_filter(tmp_path, repo)
    bare = _bare_origin(tmp_path, repo)
    _advance_origin(tmp_path, bare, "plan.md", "CANONICAL\n")
    unique = b"MY UNIQUE RAW BYTES -- MUST STAY HERE\n"
    (repo / "plan.md").write_bytes(unique)
    local_sha = _git(repo, "rev-parse", "main")
    _git(repo, "fetch", "-q", "origin")
    want = _git(repo, "rev-parse", "origin/main:plan.md")
    assert _git(repo, "hash-object", "--", "plan.md") == want, (
        "precondition: the FILTERED hash hides the difference")
    assert _git(repo, "hash-object", "--no-filters", "--", "plan.md") != want, (
        "precondition: the RAW bytes are genuinely different")
    loop = _sync_env(tmp_path, repo)

    assert loop._sync_main_from_origin(why="test") == "ff-refused"
    assert _git(repo, "rev-parse", "main") == local_sha, "main did NOT move"
    assert (repo / "plan.md").read_bytes() == unique, "the RAW operator bytes survive"
    assert _git(repo, "stash", "list").strip() == "", "nothing was stashed at all"


def test_origin_sync_refuses_untracked_ff_salvage_when_only_the_filtered_hash_matches(tmp_path):
    """The same BLOCKER without any custom filter driver: under a `text`
    attribute git's own eol conversion makes a CRLF working file hash (filtered)
    to the incoming LF blob while its raw bytes differ. Salvaging would replace
    the operator's CRLF file with the LF one and keep only the LF form in the
    stash. The raw-hash gate — not the attribute gate — is what must refuse."""
    repo = _init_repo(tmp_path / "repo")
    (repo / ".gitattributes").write_text("plan.md text\n", encoding="utf-8")
    _git(repo, "add", ".gitattributes")
    _git(repo, "commit", "-qm", "declare text attribute")
    bare = _bare_origin(tmp_path, repo)
    _advance_origin(tmp_path, bare, "plan.md", "line one\nline two\n")
    crlf = b"line one\r\nline two\r\n"
    (repo / "plan.md").write_bytes(crlf)
    local_sha = _git(repo, "rev-parse", "main")
    _git(repo, "fetch", "-q", "origin")
    want = _git(repo, "rev-parse", "origin/main:plan.md")
    assert _git(repo, "hash-object", "--", "plan.md") == want, "precondition: filtered match"
    assert _git(repo, "hash-object", "--no-filters", "--", "plan.md") != want, (
        "precondition: raw bytes differ")
    loop = _sync_env(tmp_path, repo)

    assert loop._sync_main_from_origin(why="test") == "ff-refused"
    assert _git(repo, "rev-parse", "main") == local_sha, "main did NOT move"
    assert (repo / "plan.md").read_bytes() == crlf, "the RAW operator bytes survive"
    assert _git(repo, "stash", "list").strip() == "", "nothing was stashed at all"


def test_origin_sync_salvage_never_sweeps_unrelated_untracked_paths(tmp_path):
    """BLOCKER (pathspec-scope-escape). `--` stops git parsing OPTIONS but NOT
    pathspec MAGIC, so a collision named `:(glob)**` expands to every untracked
    path in the tree and the salvage stashes unrelated operator artifacts. With
    `--literal-pathspecs` the salvage touches EXACTLY the file git named: the
    advance still completes (the collision really is redundant) and the unrelated
    file is byte-for-byte where the operator left it."""
    repo = _init_repo(tmp_path / "repo")
    bare = _bare_origin(tmp_path, repo)
    collision = ":(glob)**"
    incoming = b"literal collision\n"
    origin_sha = _advance_origin(tmp_path, bare, collision, incoming.decode())
    (repo / collision).write_bytes(incoming)
    unrelated = repo / "unrelated-operator-artifact.txt"
    unrelated_bytes = b"UNRELATED UNIQUE WORK -- MUST REMAIN IN PLACE\n"
    unrelated.write_bytes(unrelated_bytes)
    loop = _sync_env(tmp_path, repo)

    assert loop._sync_main_from_origin(why="test") == "synced"
    assert _git(repo, "rev-parse", "main") == origin_sha, "main advanced"
    assert unrelated.read_bytes() == unrelated_bytes, "the unrelated artifact is untouched"
    assert "?? unrelated-operator-artifact.txt" in _git(repo, "status", "--porcelain"), (
        "and is still untracked in the working tree, not swept into the stash")
    assert _git(repo, "ls-tree", "-r", "--name-only", "stash@{0}^3") == collision, (
        "the stash carries ONLY the file git itself named as a collision")


def test_origin_sync_salvages_a_redundant_collision_inside_an_untracked_directory(tmp_path):
    """Coverage for the tightened predicate itself (NOT a red-first regression:
    the pre-repair code salvaged this too). The hardened proof demands that git's
    own status record match the collision path EXACTLY — a much narrower test
    than the old `startswith("?? ")` — so this pins the very ordinary deadlock
    shape it must keep clearing: a merged PR adds `newdir/file.md` while an
    identical stray copy sits in a wholly-untracked local `newdir/`. If the record
    shape ever stops matching (a status mode that collapses the directory, a
    quoted path), the daemon pins at zero again and this test says so."""
    repo = _init_repo(tmp_path / "repo")
    bare = _bare_origin(tmp_path, repo)
    body = "# handoff\nidentical stray copy\n"
    origin_sha = _advance_origin(tmp_path, bare, "newdir/file.md", body)
    (repo / "newdir").mkdir()
    (repo / "newdir" / "file.md").write_text(body, encoding="utf-8")
    loop = _sync_env(tmp_path, repo)

    assert loop._sync_main_from_origin(why="test") == "synced"
    assert _git(repo, "rev-parse", "main") == origin_sha, "main advanced"
    assert (repo / "newdir" / "file.md").read_text() == body
    assert "origin-sync-salvage" in _git(repo, "stash", "list"), "the copy is preserved"


def test_origin_sync_refuses_untracked_ff_salvage_when_the_file_changes_after_the_hash(tmp_path):
    """HIGH (compare/action TOCTOU). An operator write that lands after the
    identity hash and before the stash makes the destructive step run on stale
    evidence. Re-proving the identity (raw hash AND stat identity) catches the
    change and refuses: main does not move and the NEW bytes stay on disk."""
    repo = _init_repo(tmp_path / "repo")
    bare = _bare_origin(tmp_path, repo)
    incoming = b"same at comparison time\n"
    _advance_origin(tmp_path, bare, "plan.md", incoming.decode())
    (repo / "plan.md").write_bytes(incoming)
    local_sha = _git(repo, "rev-parse", "main")
    unique = b"OPERATOR EDIT AFTER HASH -- DO NOT MOVE\n"
    loop = _sync_env(tmp_path, repo)

    real_git = gl.git
    changed = False

    def edit_after_the_first_hash(path, *args, **kwargs):
        nonlocal changed
        result = real_git(path, *args, **kwargs)
        if not changed and args[0] == "hash-object" and args[-1] == "plan.md" and result[0] == 0:
            (repo / "plan.md").write_bytes(unique)
            changed = True
        return result

    gl.git = edit_after_the_first_hash
    try:
        assert loop._sync_main_from_origin(why="test") == "ff-refused"
    finally:
        gl.git = real_git

    assert changed, "precondition: the edit really did land mid-check"
    assert _git(repo, "rev-parse", "main") == local_sha, "main did NOT move"
    assert (repo / "plan.md").read_bytes() == unique, "the operator's new bytes survive"
    assert _git(repo, "stash", "list").strip() == "", "nothing was stashed at all"


def test_origin_sync_refuses_untracked_ff_salvage_when_the_file_changes_after_the_proof(tmp_path):
    """The same race at its LAST possible instant: the file is edited after the
    whole set has been proven redundant and before the stash runs. The proof is
    re-run INSIDE the exclusion window for exactly this reason, so the salvage is
    abandoned with the working tree untouched."""
    repo = _init_repo(tmp_path / "repo")
    bare = _bare_origin(tmp_path, repo)
    incoming = b"identical at proof time\n"
    _advance_origin(tmp_path, bare, "plan.md", incoming.decode())
    (repo / "plan.md").write_bytes(incoming)
    local_sha = _git(repo, "rev-parse", "main")
    unique = b"OPERATOR EDIT AFTER THE PROOF -- DO NOT MOVE\n"
    loop = _sync_env(tmp_path, repo)

    real_prove = loop._redundant_untracked_ff_blockers

    def edit_after_the_proof(*args, **kwargs):
        proven = real_prove(*args, **kwargs)
        assert proven, "precondition: the collision proved redundant before the edit"
        (repo / "plan.md").write_bytes(unique)
        return proven

    loop._redundant_untracked_ff_blockers = edit_after_the_proof

    assert loop._sync_main_from_origin(why="test") == "ff-refused"
    assert _git(repo, "rev-parse", "main") == local_sha, "main did NOT move"
    assert (repo / "plan.md").read_bytes() == unique, "the operator's new bytes survive"
    assert _git(repo, "stash", "list").strip() == "", "nothing was stashed at all"


def test_origin_sync_refuses_when_the_salvage_stash_removes_more_than_it_proved(tmp_path):
    """Defence in depth for the scope escape. `--literal-pathspecs` is what stops
    a magic filename widening the stash, but the salvage ALSO proves after the
    fact that EXACTLY the paths it proved left the working tree. A wider sweep
    (here: an extra untracked file vanishing with the stash) must abandon the
    advance and alert loudly — main is never fast-forwarded over a working tree
    that changed in a way the daemon did not authorise."""
    repo = _init_repo(tmp_path / "repo")
    bare = _bare_origin(tmp_path, repo)
    body = b"identical\n"
    _advance_origin(tmp_path, bare, "plan.md", body.decode())
    (repo / "plan.md").write_bytes(body)
    extra = repo / "extra-untracked.txt"
    extra.write_bytes(b"swept by a hypothetical wider pathspec\n")
    local_sha = _git(repo, "rev-parse", "main")
    loop = _sync_env(tmp_path, repo)

    real_git = gl.git

    def sweep_wider_than_proved(path, *args, **kwargs):
        result = real_git(path, *args, **kwargs)
        if "stash" in args and "push" in args and result[0] == 0:
            extra.unlink(missing_ok=True)
        return result

    gl.git = sweep_wider_than_proved
    try:
        assert loop._sync_main_from_origin(why="test") == "ff-refused"
    finally:
        gl.git = real_git

    assert _git(repo, "rev-parse", "main") == local_sha, "main did NOT move"
    assert any("did not do exactly what it proved" in a for a in loop.alerts), (
        f"the unauthorised removal must be alerted, not swallowed: {loop.alerts}")


def test_salvage_exclusion_refuses_while_another_git_process_holds_index_lock(tmp_path):
    """Writer exclusion, part 1 (unit). A live `index.lock` means another git
    process is mid-write on the serving checkout, so any identity proof can be
    invalidated between check and use: the exclusion yields a REASON and the
    caller refuses. With no competing writer it yields None and the salvage may
    proceed."""
    repo = _init_repo(tmp_path / "repo")
    loop = _sync_env(tmp_path, repo)
    index_lock = repo / ".git" / "index.lock"
    index_lock.write_text("", encoding="utf-8")
    with loop._salvage_exclusion() as reason:
        assert reason and "index.lock" in reason, (
            "a concurrent git writer must block the salvage, not be raced")
    index_lock.unlink()
    with loop._salvage_exclusion() as reason:
        assert reason is None, "control: no competing writer -> exclusion is held"


def test_origin_sync_refuses_untracked_ff_salvage_when_the_salvage_lock_is_held(tmp_path):
    """Writer exclusion, part 2: the salvage takes an exclusive non-blocking
    `flock` for the whole check-then-act window, so a second holder (another
    gate-loop process) makes it refuse instead of acting on evidence that peer
    can invalidate."""
    repo = _init_repo(tmp_path / "repo")
    bare = _bare_origin(tmp_path, repo)
    body = b"identical\n"
    _advance_origin(tmp_path, bare, "plan.md", body.decode())
    (repo / "plan.md").write_bytes(body)
    local_sha = _git(repo, "rev-parse", "main")
    loop = _sync_env(tmp_path, repo)
    lock_path = loop.root / "locks" / "origin-sync-salvage.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        assert loop._sync_main_from_origin(why="test") == "ff-refused"
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)

    assert _git(repo, "rev-parse", "main") == local_sha, "main did NOT move"
    assert (repo / "plan.md").read_bytes() == body, "the untracked file is untouched"
    assert _git(repo, "stash", "list").strip() == "", "nothing was stashed at all"


# --- END untracked-ff-salvage safety regressions (r2) -----------------------


def test_hold_base_true_still_fail_closes_on_divergence(tmp_path):
    """MINOR-2 (Grok): `hold_base` must never short-circuit the fail-closed
    guards. A diverged origin returns 'diverged' even with hold_base=True and
    moves nothing — the deferral sits strictly AFTER divergence classification."""
    repo = _init_repo(tmp_path / "repo")
    bare = _bare_origin(tmp_path, repo)
    o1 = _advance_origin(tmp_path, bare, "pr.txt", "from a merged PR\n")
    # local main independently grows a commit origin does not have -> divergence.
    (repo / "local.txt").write_text("local only\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "local-only commit")
    l1 = _git(repo, "rev-parse", "main")
    loops_root = tmp_path / "loops"
    (loops_root / "state").mkdir(parents=True, exist_ok=True)
    loop = _make_loop(loops_root, repo, _fake_offload(tmp_path),
                      remote="origin", push=True)
    assert loop._sync_main_from_origin(why="test", hold_base=True) == "diverged"
    assert _git(repo, "rev-parse", "main") == l1, "divergence must not move main"
    assert _git(bare, "rev-parse", "main") == o1, "origin must never be forced"


def test_hold_base_true_still_fail_closes_on_local_ahead(tmp_path):
    """MINOR-2 (Grok): with push on, a local-ahead main fail-closes as
    'local-ahead' even under hold_base=True — the deferral never masks an ungated
    local commit (the classification precedes the hold)."""
    repo = _init_repo(tmp_path / "repo")
    m0 = _git(repo, "rev-parse", "HEAD")
    bare = _bare_origin(tmp_path, repo)      # origin == m0
    # local main grows a commit origin does NOT have; origin stays put.
    (repo / "local.txt").write_text("local only\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "local-only commit")
    l1 = _git(repo, "rev-parse", "main")
    loops_root = tmp_path / "loops"
    (loops_root / "state").mkdir(parents=True, exist_ok=True)
    loop = _make_loop(loops_root, repo, _fake_offload(tmp_path),
                      remote="origin", push=True)
    assert loop._sync_main_from_origin(why="test", hold_base=True) == "local-ahead"
    assert _git(repo, "rev-parse", "main") == l1, "local-ahead must not move main"
    assert _git(bare, "rev-parse", "main") == m0, "origin must never be forced"


def test_network_push_failure_retries_boundedly_then_parks_loudly(tmp_path):
    """MUTATION GUARD (infinite-retry-restored): removing the bounded budget
    fails HERE — a push that never succeeds must stop, loudly, not retry until
    morning."""
    repo = _init_repo(tmp_path / "repo")
    m0 = _git(repo, "rev-parse", "HEAD")
    loops_root = tmp_path / "loops"
    (loops_root / "state").mkdir(parents=True, exist_ok=True)
    train = _manual_train(repo, m0)
    from bridge.integration import GateVerdict
    v = GateVerdict("pass", 0, "", "gate passed", None, "", 1.0)
    network = ("fatal: unable to access 'https://github.com/x/y.git/': "
               "Could not resolve host: github.com")
    import unittest.mock as _mock

    outs = []
    for _attempt in range(1, gl.PUSH_RETRY_LIMIT + 1):
        loop = _make_loop(loops_root, repo, _fake_offload(tmp_path),
                          remote="origin", push=True)
        with _mock.patch.object(gl, "git", _recording_git(
                [], push_error=network, fail_fetch=True)):
            outs.append((loop, loop.on_pass(train, v)))
        assert _git(repo, "rev-parse", "main") == m0, "always rolled back"

    # a network fault is NOT classified as remote-ahead ...
    assert not [e for e in _ledger_events(loops_root)
                if (e.get("detail") or {}).get("kind") == "push_non_ff"]
    failed = [e for e in _ledger_events(loops_root)
              if (e.get("detail") or {}).get("kind") == "push_failed"]
    assert len(failed) == gl.PUSH_RETRY_LIMIT, failed
    assert [e["detail"]["attempt"] for e in failed] == list(
        range(1, gl.PUSH_RETRY_LIMIT + 1))
    # ... the first attempts retry ...
    for _loop, out in outs[:-1]:
        assert out.action == "instrument" and "park" not in out.detail.lower(), out
    # ... and the last one PARKS the train state loudly.
    last_loop, last_out = outs[-1]
    assert "park" in last_out.detail.lower(), last_out
    assert any("park" in a.lower() for a in last_loop.alerts), last_loop.alerts
    st = json.loads(gate_state_path(loops_root, train).read_text())
    assert st["state"] == "closed" and st["disposition"] == "push-parked", st


def test_push_refusal_classifier_reads_gits_own_words():
    assert gl.push_refusal_is_non_ff(
        " ! [rejected]  main -> main (non-fast-forward)")
    assert gl.push_refusal_is_non_ff(
        " ! [rejected]  main -> main (fetch first)\nhint: Updates were rejected "
        "because the remote contains work that you do not have locally.")
    # a server-side content refusal is NOT remote-ahead: fetching finds nothing
    assert not gl.push_refusal_is_non_ff(
        " ! [remote rejected] main -> main (pre-receive hook declined)")
    assert not gl.push_refusal_is_non_ff(
        "fatal: unable to access 'https://github.com/x/y.git/': "
        "Could not resolve host: github.com")
    assert not gl.push_refusal_is_non_ff(
        "fatal: Authentication failed for 'https://github.com/x/y.git/'")
    assert not gl.push_refusal_is_non_ff("")


def test_sync_never_moves_a_ref_that_is_not_main(tmp_path):
    repo = _init_repo(tmp_path / "repo")
    bare = _bare_origin(tmp_path, repo)
    _advance_origin(tmp_path, bare, "pr.txt", "from a merged PR\n")
    _git(repo, "checkout", "-q", "-B", "sidebranch")
    side = _git(repo, "rev-parse", "HEAD")
    loops_root = tmp_path / "loops"
    loop = _make_loop(loops_root, repo, _fake_offload(tmp_path),
                      remote="origin", push=False)
    assert loop._sync_main_from_origin(why="test") == "wrong-head"
    assert _git(repo, "rev-parse", "sidebranch") == side
    assert _git(repo, "rev-parse", "main") == side


# ===================== ROLLBACK SALVAGE (DATA_LOSS 2026-08-11T00:48:44Z) ======
# The push-divergence guard rolls local main back with `reset --hard` on the
# SERVING checkout, which is not guaranteed clean. During the 00:35-00:48
# push-failure storm it ran every ~60s and destroyed four uncommitted tracked
# operator edits. The guard's INTENT (never leave local main ahead of origin
# while pretending nothing happened) is right; its INSTRUMENT was a data-loss
# machine. A stuck train is recoverable. Destroyed work is not.


def _dirty_the_serving_checkout(repo: Path, text: str = "OPERATOR EDIT, uncommitted") -> str:
    """One uncommitted TRACKED edit plus one untracked file — the shape of a real
    operator mid-edit, and the shape `reset --hard` destroys half of."""
    (repo / "README.md").write_text(text + "\n", encoding="utf-8")
    (repo / "scratch-untracked.txt").write_text("untracked scratch\n", encoding="utf-8")
    return text


def test_rollback_salvages_uncommitted_operator_edits_into_a_stash(tmp_path):
    """MUTATION GUARD (bare `reset --hard` restored): this test fails on the
    destroyed-content assertion."""
    repo = _init_repo(tmp_path / "repo")
    m0 = _git(repo, "rev-parse", "HEAD")
    loops_root = tmp_path / "loops"
    (loops_root / "state").mkdir(parents=True, exist_ok=True)
    train = _manual_train(repo, m0)
    text = _dirty_the_serving_checkout(repo)

    loop = _make_loop(loops_root, repo, _fake_offload(tmp_path),
                      remote="origin", push=True)
    from bridge.integration import GateVerdict
    v = GateVerdict("pass", 0, "", "gate passed", None, "", 1.0)
    import unittest.mock as _mock
    calls: list = []
    with _mock.patch.object(gl, "git", _recording_git(
            calls, push_error="fatal: unable to access: Could not resolve host")):
        out = loop.on_pass(train, v)

    # the guard still did its job: local main is back level with origin
    assert _git(repo, "rev-parse", "main") == m0
    assert out.action == "instrument", out
    # ... and the operator's uncommitted edit SURVIVED, verbatim, in a stash
    assert _git(repo, "stash", "list"), "uncommitted tracked work must be salvaged"
    assert _git(repo, "show", "stash@{0}:README.md").strip() == text
    stash_sha = _git(repo, "rev-parse", "stash@{0}")
    # ... the operator is TOLD where it went, by ref, in the alert ...
    assert any(stash_sha[:12] in a for a in loop.alerts), loop.alerts
    # ... and in the ledger, so recovery survives the log rotating.
    salvaged = [e for e in _ledger_events(loops_root)
                if (e.get("detail") or {}).get("kind") == "rollback_salvage"]
    assert salvaged, "the salvage must be recorded, not only alerted"
    assert salvaged[0]["detail"]["stash_sha"] == stash_sha
    # untracked files are NOT swept into the stash (reset --hard never touched
    # them, so sweeping them would be a second, self-inflicted surprise)
    assert (repo / "scratch-untracked.txt").exists()
    assert "scratch-untracked.txt" not in _git(
        repo, "show", "--name-only", "--format=", "stash@{0}")
    # the salvage must not be counted as a push attempt (two independent budgets)
    assert "push_key" not in salvaged[0]["detail"]


def test_a_clean_serving_checkout_rolls_back_with_no_stash_at_all(tmp_path):
    repo = _init_repo(tmp_path / "repo")
    m0 = _git(repo, "rev-parse", "HEAD")
    loops_root = tmp_path / "loops"
    (loops_root / "state").mkdir(parents=True, exist_ok=True)
    train = _manual_train(repo, m0)

    loop = _make_loop(loops_root, repo, _fake_offload(tmp_path),
                      remote="origin", push=True)
    from bridge.integration import GateVerdict
    v = GateVerdict("pass", 0, "", "gate passed", None, "", 1.0)
    import unittest.mock as _mock
    with _mock.patch.object(gl, "git", _recording_git(
            [], push_error="fatal: unable to access: Could not resolve host")):
        out = loop.on_pass(train, v)

    assert _git(repo, "rev-parse", "main") == m0
    assert _git(repo, "stash", "list") == "", "a clean rollback creates no stash"
    assert out.action == "instrument", out
    assert not [e for e in _ledger_events(loops_root)
                if (e.get("detail") or {}).get("kind") == "rollback_salvage"]


def test_rollback_is_refused_when_the_salvage_itself_fails(tmp_path):
    """A stuck train is recoverable; destroyed work is not. If the work cannot be
    salvaged, the reset does NOT happen — the daemon halts loudly instead."""
    repo = _init_repo(tmp_path / "repo")
    m0 = _git(repo, "rev-parse", "HEAD")
    loops_root = tmp_path / "loops"
    (loops_root / "state").mkdir(parents=True, exist_ok=True)
    train = _manual_train(repo, m0)
    text = _dirty_the_serving_checkout(repo)

    loop = _make_loop(loops_root, repo, _fake_offload(tmp_path),
                      remote="origin", push=True)
    from bridge.integration import GateVerdict
    v = GateVerdict("pass", 0, "", "gate passed", None, "", 1.0)
    import unittest.mock as _mock
    calls: list = []
    with _mock.patch.object(gl, "git", _recording_git(
            calls, push_error="fatal: unable to access: Could not resolve host",
            fail_stash=True)):
        with pytest.raises(gl.DaemonPoisoned):
            loop.on_pass(train, v)

    # NO reset was attempted over work that could not be salvaged ...
    assert not any(c and c[0] == "reset" for c in calls), calls
    # ... the working tree is untouched ...
    assert (repo / "README.md").read_text().strip() == text
    assert _git(repo, "stash", "list") == ""
    # ... local main is left ahead, and the daemon HALTS rather than carry on
    # with a split brain (a resumed daemon would otherwise push an un-recorded
    # merge to origin on a later tick).
    assert _git(repo, "rev-parse", "main") == train.tip
    assert loop.poisoned()
    poison = json.loads(loop._poison_path().read_text())
    assert "salvage" in json.dumps(poison).lower()
    assert any("CRITICAL" in a for a in loop.alerts), loop.alerts
    assert any("uncommitted" in a.lower() for a in loop.alerts), loop.alerts


def test_double_failure_poison_still_names_the_salvaged_stash(tmp_path):
    """Salvage OK, reset then fails: the existing poison path stands, and it must
    tell the operator where their edits are."""
    repo = _init_repo(tmp_path / "repo")
    m0 = _git(repo, "rev-parse", "HEAD")
    loops_root = tmp_path / "loops"
    (loops_root / "state").mkdir(parents=True, exist_ok=True)
    train = _manual_train(repo, m0)
    _dirty_the_serving_checkout(repo)

    loop = _make_loop(loops_root, repo, _fake_offload(tmp_path),
                      remote="origin", push=True)
    from bridge.integration import GateVerdict
    v = GateVerdict("pass", 0, "", "gate passed", None, "", 1.0)
    import unittest.mock as _mock
    with _mock.patch.object(gl, "git", _recording_git(
            [], push_error="push denied (test)", fail_reset=True)):
        with pytest.raises(gl.DaemonPoisoned):
            loop.on_pass(train, v)

    stash_sha = _git(repo, "rev-parse", "stash@{0}")
    poison = json.loads(loop._poison_path().read_text())
    assert poison["salvage_stash_sha"] == stash_sha, poison
    assert any(stash_sha[:12] in a for a in loop.alerts), loop.alerts


# ===== CROSS-LINEAGE REWORK (GPT-5.6-Sol xhigh, PR #230) — 2 BLOCKERS + 1 MAJOR
# Each regression FAILS against the request-changes source and passes only after
# its fix. They cover the gaps the +tests missed: post-probe dirt, a concurrent
# commit in the rollback window, local-ahead-only provenance, and a peer stash
# moving refs/stash under the salvage.


def test_post_probe_tracked_edit_is_salvaged_not_destroyed(tmp_path):
    """BLOCKER-1 (rollback-dirty-toctou, edit half). A tracked edit that lands
    AFTER the cleanliness probe but BEFORE `reset --hard` must be captured, never
    destroyed. The salvage stashes UNCONDITIONALLY (a status probe is a TOCTOU
    point), so a stale 'clean' verdict cannot open a data-loss window."""
    repo = _init_repo(tmp_path / "repo")
    m0 = _git(repo, "rev-parse", "HEAD")
    loops_root = tmp_path / "loops"
    (loops_root / "state").mkdir(parents=True, exist_ok=True)
    train = _manual_train(repo, m0)

    loop = _make_loop(loops_root, repo, _fake_offload(tmp_path),
                      remote="origin", push=True)
    from bridge.integration import GateVerdict
    v = GateVerdict("pass", 0, "", "gate passed", None, "", 1.0)

    real = gl.git
    injected = "OPERATOR EDIT AFTER CLEAN PROBE\n"
    state = {"done": False}

    def racing(r, *args, **kw):
        if args and args[0] == "push":
            return (1, "", "fatal: simulated network outage")
        out = real(r, *args, **kw)
        if (Path(r) == repo and args[:2] == ("status", "--porcelain")
                and not state["done"]):
            # the probe truthfully saw clean; the human writes in the window
            (repo / "README.md").write_text(injected, encoding="utf-8")
            state["done"] = True
        return out

    import unittest.mock as _mock
    with _mock.patch.object(gl, "git", racing):
        out = loop.on_pass(train, v)

    assert out.action == "instrument", out
    assert state["done"], "the racing edit was never injected — test is inert"
    # local main rolled back level with origin (the guard still did its job) ...
    assert _git(repo, "rev-parse", "main") == m0
    # ... but the post-probe edit was CAPTURED into a labelled stash, verbatim ...
    rows = _git(repo, "stash", "list", "--format=%H%x09%gs").splitlines()
    ours = [row for row in rows if "gate-rollback-salvage" in row]
    assert ours, f"post-probe tracked edit was not salvaged: {rows}"
    sha = ours[0].split("\t", 1)[0]
    assert _git(repo, "show", f"{sha}:README.md") == injected.rstrip("\n")
    # ... and the operator is told where it went, by sha, in alert AND ledger.
    assert any(sha[:12] in a for a in loop.alerts), loop.alerts
    salvaged = [e for e in _ledger_events(loops_root)
                if (e.get("detail") or {}).get("kind") == "rollback_salvage"]
    assert salvaged and salvaged[0]["detail"]["stash_sha"] == sha


def test_concurrent_commit_in_rollback_window_is_pinned_to_a_durable_ref(tmp_path):
    """BLOCKER-1 (rollback-dirty-toctou, commit half). A commit that lands on the
    SHARED serving checkout between the ff-merge and the rollback is retained by
    no branch; `git stash` cannot save a commit, so it must be pinned into a
    durable ref BEFORE `reset --hard` orphans it — always recoverable."""
    repo = _init_repo(tmp_path / "repo")
    m0 = _git(repo, "rev-parse", "HEAD")
    loops_root = tmp_path / "loops"
    (loops_root / "state").mkdir(parents=True, exist_ok=True)
    train = _manual_train(repo, m0)

    loop = _make_loop(loops_root, repo, _fake_offload(tmp_path),
                      remote="origin", push=True)
    from bridge.integration import GateVerdict
    v = GateVerdict("pass", 0, "", "gate passed", None, "", 1.0)

    real = gl.git
    state = {"sha": None}

    def committing(r, *args, **kw):
        if args and args[0] == "push":
            return (1, "", "fatal: simulated network outage")
        out = real(r, *args, **kw)
        if (Path(r) == repo and args[:2] == ("status", "--porcelain")
                and state["sha"] is None):
            (repo / "OP.txt").write_text("committed in the rollback window\n",
                                         encoding="utf-8")
            _git(repo, "add", "OP.txt")
            _git(repo, "commit", "-qm", "operator concurrent commit")
            state["sha"] = _git(repo, "rev-parse", "HEAD")
        return out

    import unittest.mock as _mock
    with _mock.patch.object(gl, "git", committing):
        out = loop.on_pass(train, v)

    assert out.action == "instrument", out
    assert state["sha"] is not None, "the concurrent commit was never made"
    # local main rolled back (the reset DID happen — a stuck train is fine) ...
    assert _git(repo, "rev-parse", "main") == m0
    # ... but the concurrent commit survives in a durable ref, recoverable.
    refs = _git(repo, "for-each-ref", "--contains", state["sha"],
                "--format=%(refname)").splitlines()
    assert any(r.startswith("refs/gate-loop/rollback-salvage/") for r in refs), refs
    pinned = [e for e in _ledger_events(loops_root)
              if (e.get("detail") or {}).get("kind")
              == "rollback_concurrent_commit_pinned"]
    assert pinned and pinned[0]["detail"]["pinned_sha"] == state["sha"]
    assert any(state["sha"][:12] in a for a in loop.alerts), loop.alerts


def test_local_ahead_unreviewed_base_is_not_pushed_to_origin(tmp_path):
    """BLOCKER-2 (local-ahead-pushes-unreviewed). A manual commit on the serving
    checkout leaves local main AHEAD of origin with no gate/push provenance. The
    classifier must call that DISTINCTLY (not the healthy 'current'), and the
    land path must REFUSE to fast-forward a gated train over it — the ungated
    commit must never ride to origin/main."""
    repo = _init_repo(tmp_path / "repo")
    m0 = _git(repo, "rev-parse", "HEAD")
    bare = _bare_origin(tmp_path, repo)                 # origin/main == m0
    # a manual commit with no candidate envelope, review verdict or gate receipt
    (repo / "UNREVIEWED.txt").write_text("manual local-only commit\n", encoding="utf-8")
    _git(repo, "add", "UNREVIEWED.txt")
    _git(repo, "commit", "-qm", "manual unreviewed commit on serving main")
    u1 = _git(repo, "rev-parse", "main")
    train = _manual_train(repo, u1)                     # a gated train on the ungated base
    loops_root = tmp_path / "loops"

    loop = _make_loop(loops_root, repo, _fake_offload(tmp_path),
                      remote="origin", push=True)
    # (1) the classifier names local-ahead DISTINCTLY and fails closed
    assert loop._sync_main_from_origin(why="test") == "local-ahead"
    # (2) the land path refuses to carry the ungated base to origin
    merge_sha, reason = loop.land_train(train)
    assert merge_sha is None and reason == "local-ahead-unreviewed", (merge_sha, reason)
    # origin never advanced over the unreviewed commit ...
    assert _git(bare, "rev-parse", "main") == m0
    # ... and local main was NOT advanced either (refused BEFORE the ff-merge).
    assert _git(repo, "rev-parse", "main") == u1
    # the anomaly is surfaced for reconciliation, in alert AND ledger.
    assert any("AHEAD" in a for a in loop.alerts), loop.alerts
    ev = [e for e in _ledger_events(loops_root)
          if (e.get("detail") or {}).get("kind") == "main-local-ahead-unreviewed"]
    assert ev, "local-ahead must be recorded for reconciliation"


def test_peer_stash_does_not_hide_our_salvage_entry(tmp_path):
    """MAJOR (shared-stash-head-race). `refs/stash` is shared across worktrees; a
    peer worktree's concurrent `git stash push` moves the top of the stack off
    our salvage entry. The lander must still find ITS OWN entry by label anywhere
    in the stack and record it — not read `stash@{0}` and misreport 'clean'."""
    repo = _init_repo(tmp_path / "repo")
    m0 = _git(repo, "rev-parse", "HEAD")
    loops_root = tmp_path / "loops"
    (loops_root / "state").mkdir(parents=True, exist_ok=True)
    train = _manual_train(repo, m0)
    operator_text = "TARGET OPERATOR EDIT\n"
    (repo / "README.md").write_text(operator_text, encoding="utf-8")

    peer = tmp_path / "peer-worktree"
    _git(repo, "worktree", "add", "-q", "-b", "peer-stasher", str(peer), m0)
    (peer / "README.md").write_text("PEER EDIT\n", encoding="utf-8")

    loop = _make_loop(loops_root, repo, _fake_offload(tmp_path),
                      remote="origin", push=True)
    from bridge.integration import GateVerdict
    v = GateVerdict("pass", 0, "", "gate passed", None, "", 1.0)

    real = gl.git
    state = {"done": False}

    def racing(r, *args, **kw):
        if args and args[0] == "push":
            return (1, "", "fatal: simulated network outage")
        out = real(r, *args, **kw)
        if (Path(r) == repo and args[:2] == ("stash", "push")
                and out[0] == 0 and not state["done"]):
            # a legal peer update moves refs/stash off our just-created entry
            pr = real(peer, "stash", "push", "-m", "peer concurrent stash")
            assert pr[0] == 0, pr
            state["done"] = True
        return out

    import unittest.mock as _mock
    with _mock.patch.object(gl, "git", racing):
        out = loop.on_pass(train, v)

    assert out.action == "instrument", out
    assert state["done"], "the peer stash never moved refs/stash — test is inert"
    rows = _git(repo, "stash", "list", "--format=%H%x09%gs").splitlines()
    # the peer entry sits ABOVE ours — reading stash@{0} would find the wrong one
    assert "peer concurrent stash" in rows[0], rows
    ours = [row for row in rows if "gate-rollback-salvage" in row]
    assert ours, f"our salvage entry vanished from the stack: {rows}"
    sha = ours[0].split("\t", 1)[0]
    assert _git(repo, "show", f"{sha}:README.md") == operator_text.rstrip("\n")
    # found by label and recorded, in alert AND ledger, despite not being on top
    assert any(sha[:12] in a for a in loop.alerts), loop.alerts
    salvaged = [e for e in _ledger_events(loops_root)
                if (e.get("detail") or {}).get("kind") == "rollback_salvage"]
    assert salvaged and salvaged[0]["detail"]["stash_sha"] == sha


def test_commit_racing_the_merge_to_revparse_window_is_still_pinned(tmp_path):
    """BLOCKER-1 residue (found in the #235 landing review). A commit that lands
    in the window between `merge --ff-only` and the daemon's re-read of `main`
    is absorbed INTO `new_main`; a pin anchored on new_main compares the
    operator's commit to itself, pins nothing, and `reset --hard` orphans it to
    reflog-only with no alert and no ledger event. Anchoring on `train.tip` —
    the sha the ff-merge deterministically set main to — closes the window.
    Fails against the pre-fix source."""
    repo = _init_repo(tmp_path / "repo")
    m0 = _git(repo, "rev-parse", "HEAD")
    loops_root = tmp_path / "loops"
    (loops_root / "state").mkdir(parents=True, exist_ok=True)
    train = _manual_train(repo, m0)

    loop = _make_loop(loops_root, repo, _fake_offload(tmp_path),
                      remote="origin", push=True)
    from bridge.integration import GateVerdict
    v = GateVerdict("pass", 0, "", "gate passed", None, "", 1.0)

    real = gl.git
    state = {"sha": None}

    def racing(r, *args, **kw):
        if args and args[0] == "push":
            return (1, "", "fatal: simulated network outage")
        out = real(r, *args, **kw)
        if (Path(r) == repo and args and args[0] == "merge" and "--ff-only" in args
                and state["sha"] is None and out[0] == 0):
            # the operator's commit completes BETWEEN the daemon's ff-merge and
            # its immediately following rev-parse of main — the narrowest window
            (repo / "RACE.txt").write_text(
                "committed in the merge-to-revparse window\n", encoding="utf-8")
            _git(repo, "add", "RACE.txt")
            _git(repo, "commit", "-qm", "operator commit in the narrowest window")
            state["sha"] = _git(repo, "rev-parse", "HEAD")
        return out

    import unittest.mock as _mock
    with _mock.patch.object(gl, "git", racing):
        out = loop.on_pass(train, v)

    assert out.action == "instrument", out
    assert state["sha"] is not None, "the racing commit was never made — test is inert"
    # rolled back level with the pre-merge base, as ever ...
    assert _git(repo, "rev-parse", "main") == m0
    # ... but the racing commit survives in a durable ref, recoverable
    refs = _git(repo, "for-each-ref", "--contains", state["sha"],
                "--format=%(refname)").splitlines()
    assert any(r.startswith("refs/gate-loop/rollback-salvage/") for r in refs), (
        f"racing commit {state['sha'][:12]} was orphaned, not pinned: {refs}")
    pinned = [e for e in _ledger_events(loops_root)
              if (e.get("detail") or {}).get("kind")
              == "rollback_concurrent_commit_pinned"]
    assert pinned and pinned[0]["detail"]["pinned_sha"] == state["sha"]
    assert any(state["sha"][:12] in a for a in loop.alerts), loop.alerts


def test_push_publishes_the_gated_tip_never_the_live_ref(tmp_path):
    """BLOCKER-2 residue (found in the #235 landing review). `push <remote> main`
    resolves the MUTABLE ref at push time, so an operator commit racing into the
    merge-to-push window rides to origin ungated on the SUCCESS path — the
    line-level invariant ('nothing without gate provenance reaches origin')
    broken with no alert at all. Pushing the explicit `train.tip:refs/heads/main`
    refspec publishes what the gate graded and only that; the racing commit
    stays local, where the next tick's local-ahead classifier refuses it
    loudly. Fails against the pre-fix source."""
    repo = _init_repo(tmp_path / "repo")
    m0 = _git(repo, "rev-parse", "HEAD")
    bare = _bare_origin(tmp_path, repo)
    loops_root = tmp_path / "loops"
    (loops_root / "state").mkdir(parents=True, exist_ok=True)
    train = _manual_train(repo, m0)

    loop = _make_loop(loops_root, repo, _fake_offload(tmp_path),
                      remote="origin", push=True)

    real = gl.git
    state = {"sha": None}

    def racing(r, *args, **kw):
        out = real(r, *args, **kw)
        if (Path(r) == repo and args and args[0] == "merge" and "--ff-only" in args
                and state["sha"] is None and out[0] == 0):
            (repo / "RACE.txt").write_text(
                "committed in the merge-to-push window\n", encoding="utf-8")
            _git(repo, "add", "RACE.txt")
            _git(repo, "commit", "-qm", "operator commit before the push resolves main")
            state["sha"] = _git(repo, "rev-parse", "HEAD")
        return out

    import unittest.mock as _mock
    with _mock.patch.object(gl, "git", racing):
        merge_sha, reason = loop.land_train(train)

    assert state["sha"] is not None, "the racing commit was never made — test is inert"
    # the landing is recorded against the GATED sha, not the live ref
    assert reason == "landed" and merge_sha == train.tip, (merge_sha, reason)
    # origin received the gated tip; the ungated racing commit never left the box
    assert _git(bare, "rev-parse", "main") == train.tip
    # the racing commit stays local-only — exactly the state the tick-top
    # local-ahead classifier exists to refuse loudly on the next tick
    assert _git(repo, "rev-parse", "main") == state["sha"]


# ===== ROUND-2 REWORK (Opus xhigh, PR #235) — the 3 RESIDUAL defects the v2
# cross-lineage repros still reproduced on the round-1 fix. Each regression is
# RED on the pre-fix source (`reset --hard`, fail-open-on-unreadable, substring
# stash match) and passes only on the mechanism-level fix (`reset --keep` + a
# post-reset reflog/stash capture, fail-CLOSED provenance, EXACT-label match).


def test_reset_instant_edit_and_commit_survive_the_keep_rollback(tmp_path):
    """BLOCKER-1 v2 (rollback-dirty-toctou, reset-instant window). Round-1 stashes
    and pins BEFORE `reset --hard`, so a tracked edit or a commit that appears in
    the window right up to the reset ITSELF is still destroyed. RED on round-1;
    `reset --keep` plus post-reset reflog/stash capture leave BOTH recoverable."""
    import unittest.mock as _mock

    from bridge.integration import GateVerdict
    v = GateVerdict("pass", 0, "", "gate passed", None, "", 1.0)
    real = gl.git
    offload = _fake_offload(tmp_path)

    # -- edit half: a tracked edit lands as the reset begins --------------------
    repo = _init_repo(tmp_path / "edit" / "repo")
    m0 = _git(repo, "rev-parse", "HEAD")
    train = _manual_train(repo, m0)
    loop = _make_loop(tmp_path / "edit" / "loops", repo, offload,
                      remote="origin", push=True)
    injected = "LATE OPERATOR EDIT AT THE RESET INSTANT\n"
    done = {"edit": False}

    def edit_at_reset(r, *a, **k):
        if a and a[0] == "push":
            return (1, "", "fatal: simulated network outage")
        if Path(r) == repo and a and a[0] == "reset" and not done["edit"]:
            # every capture has run; the writer edits as reset is about to fire.
            (repo / "README.md").write_text(injected, encoding="utf-8")
            done["edit"] = True
        return real(r, *a, **k)

    with _mock.patch.object(gl, "git", edit_at_reset):
        out = loop.on_pass(train, v)

    assert done["edit"], "the reset-instant edit was never injected — test is inert"
    assert out.action == "instrument", out
    assert _git(repo, "rev-parse", "main") == m0        # guard still rolled main back
    worktree = (repo / "README.md").read_text()
    stashed = any(
        _git(repo, "show", f"{s}:README.md", check=False) == injected.rstrip("\n")
        for s in _git(repo, "stash", "list", "--format=%H").splitlines())
    assert worktree == injected or stashed, (
        "the rollback destroyed a tracked edit created at the reset instant")

    # -- commit half: a commit lands as the reset begins ------------------------
    repo2 = _init_repo(tmp_path / "commit" / "repo")
    n0 = _git(repo2, "rev-parse", "HEAD")
    train2 = _manual_train(repo2, n0)
    loop2 = _make_loop(tmp_path / "commit" / "loops", repo2, offload,
                       remote="origin", push=True)
    state = {"sha": None}

    def commit_at_reset(r, *a, **k):
        if a and a[0] == "push":
            return (1, "", "fatal: simulated network outage")
        if Path(r) == repo2 and a and a[0] == "reset" and state["sha"] is None:
            (repo2 / "OP.txt").write_text("committed at the reset instant\n",
                                          encoding="utf-8")
            _git(repo2, "add", "OP.txt")
            _git(repo2, "commit", "-qm", "operator commit at the reset instant")
            state["sha"] = _git(repo2, "rev-parse", "HEAD")
        return real(r, *a, **k)

    with _mock.patch.object(gl, "git", commit_at_reset):
        loop2.on_pass(train2, v)

    assert state["sha"] is not None, "the reset-instant commit was never made"
    assert _git(repo2, "rev-parse", "main") == n0        # rollback still happened
    refs = _git(repo2, "for-each-ref", "--contains", state["sha"],
                "--format=%(refname)").splitlines()
    assert any(r.startswith("refs/gate-loop/rollback-salvage/") for r in refs), refs
    pinned = [e for e in _ledger_events(tmp_path / "commit" / "loops")
              if (e.get("detail") or {}).get("kind")
              == "rollback_concurrent_commit_pinned"]
    assert pinned and pinned[0]["detail"]["pinned_sha"] == state["sha"]


def test_unreadable_origin_provenance_fails_closed_not_open(tmp_path):
    """BLOCKER-2 v2 (local-ahead-unreadable-probe-fails-open). Round-1 folds an
    UNREADABLE origin/main into the favourable path (`not _full_sha` skips the
    guard entirely), so a blocked/transient probe pushes an ungated local-ahead
    commit to origin. RED on round-1. The fix distinguishes 'genuinely no origin
    ref' (fail open) from 'could not read origin' (fail CLOSED) and refuses."""
    import unittest.mock as _mock
    repo = _init_repo(tmp_path / "repo")
    origin_before = _git(repo, "rev-parse", "HEAD")
    bare = _bare_origin(tmp_path, repo)                  # origin/main ref EXISTS
    # a manual commit with no candidate envelope, verdict, receipt or merged event
    (repo / "UNREVIEWED.txt").write_text("manual local-only commit\n", encoding="utf-8")
    _git(repo, "add", "UNREVIEWED.txt")
    _git(repo, "commit", "-qm", "manual unreviewed commit on serving main")
    local_only = _git(repo, "rev-parse", "main")
    train = _manual_train(repo, local_only)              # a gated train on the ungated base
    loops_root = tmp_path / "loops"
    loop = _make_loop(loops_root, repo, _fake_offload(tmp_path),
                      remote="origin", push=True)
    real = gl.git

    def unreadable_origin(r, *a, **k):
        if Path(r) == repo and a == ("rev-parse", "--verify", "origin/main"):
            # the ref EXISTS but the value-read is blocked/transient: UNKNOWN,
            # not "no unexplained commits".
            return (128, "", "fatal: simulated unreadable origin/main")
        return real(r, *a, **k)

    with _mock.patch.object(gl, "git", unreadable_origin):
        merge_sha, reason = loop.land_train(train)

    assert merge_sha is None and reason == "provenance-unknown", (merge_sha, reason)
    # neither origin nor local advanced over the unreviewed commit ...
    assert _git(bare, "rev-parse", "main") == origin_before, "origin advanced on an unreadable probe"
    assert _git(repo, "rev-parse", "main") == local_only, "local main advanced despite refusal"
    # ... and the refusal is surfaced DISTINCTLY, in alert AND ledger.
    assert any("could not read" in a.lower() and "refusing" in a.lower()
               for a in loop.alerts), loop.alerts
    ev = [e for e in _ledger_events(loops_root)
          if (e.get("detail") or {}).get("kind") == "main-provenance-unreadable"]
    assert ev, "an unreadable provenance probe must be recorded, not silently pushed"


def test_peer_stash_label_extension_is_not_mistaken_for_ours(tmp_path):
    """MAJOR v2 (shared-stash-head-race). `refs/stash` is repo-wide; a peer whose
    label EXTENDS ours (…LABEL-peer) is pushed ABOVE our entry in the shared
    stack. Round-1's substring test (`label in subject`) selects that peer as our
    salvage. RED on round-1. EXACT-label match records OUR entry, never the peer."""
    import unittest.mock as _mock

    from bridge.integration import GateVerdict
    repo = _init_repo(tmp_path / "repo")
    m0 = _git(repo, "rev-parse", "HEAD")
    train = _manual_train(repo, m0)
    operator_text = "TARGET OPERATOR EDIT\n"
    (repo / "README.md").write_text(operator_text, encoding="utf-8")
    peer = tmp_path / "peer-worktree"
    _git(repo, "worktree", "add", "-q", "-b", "peer-stasher", str(peer), m0)
    (peer / "README.md").write_text("PEER EDIT\n", encoding="utf-8")
    loops_root = tmp_path / "loops"
    loop = _make_loop(loops_root, repo, _fake_offload(tmp_path),
                      remote="origin", push=True)
    v = GateVerdict("pass", 0, "", "gate passed", None, "", 1.0)
    real = gl.git
    peer_stashed = {"done": False}

    def racing(r, *a, **k):
        if a and a[0] == "push":
            return (1, "", "fatal: simulated network outage")
        out = real(r, *a, **k)
        if (Path(r) == repo and a[:2] == ("stash", "push")
                and out[0] == 0 and not peer_stashed["done"]):
            label = a[a.index("-m") + 1]
            pr = real(peer, "stash", "push", "-m", f"{label}-peer")
            assert pr[0] == 0, pr
            peer_stashed["done"] = True
        return out

    with _mock.patch.object(gl, "git", racing):
        out = loop.on_pass(train, v)

    assert out.action == "instrument", out
    assert peer_stashed["done"], "the prefix-colliding peer stash was never made — test is inert"
    rows = _git(repo, "stash", "list", "--format=%H%x09%gs").splitlines()
    target_row = next(r for r in rows if "gate-rollback-salvage" in r and not r.endswith("-peer"))
    peer_row = next(r for r in rows if r.endswith("-peer"))
    target_sha = target_row.split("\t", 1)[0]
    peer_sha = peer_row.split("\t", 1)[0]
    assert _git(repo, "show", f"{target_sha}:README.md") == operator_text.rstrip("\n")
    salvaged = [e for e in _ledger_events(loops_root)
                if (e.get("detail") or {}).get("kind") == "rollback_salvage"]
    recorded = salvaged[0]["detail"]["stash_sha"] if salvaged else None
    assert recorded == target_sha, "the peer's prefix-colliding label was recorded as our salvage"
    assert recorded != peer_sha
    assert any(target_sha[:12] in a for a in loop.alerts), loop.alerts


# ---------------------------------------------------------------------------
# N-HOST POOL — the scheduler cap is a count of PHYSICAL BOXES.
#
# Adding gate boxes is a config edit now, so the cap is only as safe as the
# identity check behind it: two names for one Mac must buy ONE slot. These
# tests drive the real import-time path (a stub `ssh` earlier on PATH answers
# the `ssh -G` expansion) rather than a hand-set constant, because the constant
# is what `scripts/gate-watch` kills excess gates against.
# ---------------------------------------------------------------------------


def _stub_ssh(tmp_path, mapping):
    """A fake `ssh` whose -G expansion answers from `mapping` (host -> id)."""
    bindir = tmp_path / "stub-bin"
    bindir.mkdir(parents=True, exist_ok=True)
    cases = "\n".join(
        f'  {host}) echo "hostname {ident}" ;;' for host, ident in mapping.items())
    (bindir / "ssh").write_text(
        "#!/bin/sh\n"
        'if [ "$1" != "-G" ]; then exit 99; fi\n'
        'case "$2" in\n'
        f"{cases}\n"
        "  *) exit 255 ;;\n"
        "esac\n"
        "echo 'user youruser'\n"
    )
    (bindir / "ssh").chmod(0o755)
    return bindir


def _reload_pool(monkeypatch, *, config: str, tmp_path, ssh_ids):
    """Rebuild gate_host/gate_loop against a config file and a stub ssh."""
    import importlib

    from bridge import gate_host as gh
    cfg = tmp_path / "pool.yaml"
    cfg.write_text(config, encoding="utf-8")
    bindir = _stub_ssh(tmp_path, ssh_ids)
    monkeypatch.setenv("PATH", f"{bindir}:{os.environ['PATH']}")
    monkeypatch.setenv("GATE_HOSTS_CONFIG", str(cfg))
    monkeypatch.delenv("THREELOOPS_ACTIVE_TWINS", raising=False)
    importlib.reload(gh)
    return gh, importlib.reload(gl)


def _restore_pool(monkeypatch):
    """Undo the env FIRST, then reload.

    Reloading while the stub PATH/config are still installed would re-import the
    FIXTURE pool and leave it there for the rest of the file — a reload-based
    test that poisons node order is worse than no test at all.
    """
    import importlib

    from bridge import gate_host as gh
    monkeypatch.undo()
    importlib.reload(gh)
    importlib.reload(gl)
    assert [s.host for s in gh.TWIN_SPECS] == [s.host for s in gh.KNOWN_TWIN_SPECS], \
        "the fixture pool must not survive the test that installed it"


#: Deliberately NOT the shipped host names: a config-driven pool that silently
#: fell back to the built-in two twins would otherwise satisfy these assertions
#: for the wrong reason (favourable absence — the exact class this estate keeps
#: paying for).
_THREE_DISTINCT = """
twins:
  - host: gate-a
    workspace: /Users/youruser/OmniAgentOS-gate
    evidence_root: /Users/youruser/OmniAgentOS/var/gate-evidence
    perf_cores: 16
  - host: gate-b
    workspace: /Users/cloud/OmniAgentOS-gate
    evidence_root: /Users/cloud/OmniAgentOS/var/gate-evidence
    perf_cores: 12
  - host: gate-c
    workspace: /Users/cloud/OmniAgentOS-gate
    evidence_root: /Users/cloud/OmniAgentOS/var/gate-evidence
    perf_cores: 12
"""


def test_a_config_added_box_raises_the_cap_by_exactly_one(monkeypatch, tmp_path):
    try:
        gh, gl_reloaded = _reload_pool(
            monkeypatch, config=_THREE_DISTINCT, tmp_path=tmp_path,
            ssh_ids={"gate-a": "10.0.0.1", "gate-b": "10.0.0.2",
                     "gate-c": "10.0.0.3"})
        assert [s.host for s in gh.TWIN_SPECS] == ["gate-a", "gate-b", "gate-c"]
        assert gl_reloaded.MAX_CONCURRENT_GATES == 4, "local + three distinct boxes"
    finally:
        _restore_pool(monkeypatch)


def test_two_config_entries_on_one_physical_box_do_not_raise_the_cap(monkeypatch, tmp_path):
    """THE regression this lane exists for. `gate-c` is a second SSH name for
    `gate-b`'s Mac, so the cap must stay at local + 2 distinct boxes — never 4 —
    or the scheduler runs two full ~12-minute gates on one machine and grades
    the second under contention it did not cause."""
    try:
        gh, gl_reloaded = _reload_pool(
            monkeypatch, config=_THREE_DISTINCT, tmp_path=tmp_path,
            ssh_ids={"gate-a": "10.0.0.1", "gate-b": "10.0.0.2",
                     "gate-c": "10.0.0.2"})
        assert [s.host for s in gh.TWIN_SPECS] == ["gate-a", "gate-b"], \
            "the later alias for an already-listed box is collapsed away"
        assert gl_reloaded.MAX_CONCURRENT_GATES == 3
    finally:
        _restore_pool(monkeypatch)


def test_an_unreachable_configured_box_does_not_buy_a_slot(monkeypatch, tmp_path):
    try:
        gh, gl_reloaded = _reload_pool(
            monkeypatch, config=_THREE_DISTINCT, tmp_path=tmp_path,
            ssh_ids={"gate-a": "10.0.0.1", "gate-b": "10.0.0.2"})
        assert [s.host for s in gh.TWIN_SPECS] == ["gate-a", "gate-b"]
        assert gl_reloaded.MAX_CONCURRENT_GATES == 3
    finally:
        _restore_pool(monkeypatch)


def test_a_gate_in_flight_under_an_alias_blocks_its_pool_entry(tmp_path):
    """Occupancy is recorded as a NAME in the gate-state file. After a config
    rename the running gate's name may not be the pool's name for that box —
    the busy set must therefore be compared by physical identity."""
    loop = _make_loop(tmp_path / "loops", _init_repo(tmp_path / "repo"),
                      _fake_offload(tmp_path), allow_remote_gate=True)
    _running_state(loop.root / "state" / "gates", "aliased", twin="mw0001")
    ids = {"mw0001": "203.0.113.10", "mw0001-owner": "203.0.113.10",
           "mw0002": "203.0.113.11"}
    busy = loop._busy_boxes(resolve=lambda host, **_kw: ids.get(host))
    assert busy == {"mw0001-owner"}, \
        "the pool entry for that Mac is occupied even under its other name"


def test_an_unreadable_state_file_still_blocks_every_box(tmp_path):
    """Fail-closed must survive the generalization: a corrupt record may be a
    gate on ANY configured box."""
    loop = _make_loop(tmp_path / "loops", _init_repo(tmp_path / "repo"),
                      _fake_offload(tmp_path), allow_remote_gate=True)
    gdir = loop.root / "state" / "gates"
    gdir.mkdir(parents=True, exist_ok=True)
    (gdir / "corrupt.json").write_text("{not json")
    assert loop._busy_boxes() == {s.host for s in gl.TWIN_SPECS}
# ==================================================================== CHAINS
# Chain trains + conflict parking (2026-08-11). The waste each one removes is
# measured, not hypothetical:
#   * racing disjoint trains on one base: 20 landed vs 57 done-but-SUPERSEDED
#     across 116 gate runs — 3x the compute for 1x the landings, because landing
#     is single-writer and the first train to land staled every other one;
#   * retrying a cherry-pick conflict every tick: 687 consecutive ticks (~11h)
#     of "no trains assembled this tick" for the same two candidates.


class _Spec:
    """The only field the scheduler reads off a twin spec."""

    def __init__(self, host: str) -> None:
        self.host = host


def _two_fake_twins(monkeypatch):
    """A deterministic 3-box fleet, independent of THREELOOPS_ACTIVE_TWINS."""
    specs = [_Spec("twin-a"), _Spec("twin-b")]
    monkeypatch.setattr(gl, "TWIN_SPECS", specs)
    monkeypatch.setattr(gl, "MAX_CONCURRENT_GATES", 1 + len(specs))

    def _admit_all(exclude=frozenset(), probe=None, readings=None):
        free = [s for s in specs if s.host not in exclude]
        return (free[0] if free else None,
                [{"host": s.host, "admitted": True, "reason": ""} for s in free])

    monkeypatch.setattr(gl, "pick_twin", _admit_all)
    return specs


def _gate_result(loops_root: Path, train: Train, *, rc: int, slug: str = "") -> Path:
    """Stamp a FINISHED gate result onto a train's real state-file key."""
    sf = gate_state_path(loops_root, train)
    sf.parent.mkdir(parents=True, exist_ok=True)
    receipt = sf.parent / f"receipt-{train.branch.replace('/', '__')}@{train.tip[:12]}.json"
    receipt.write_text(json.dumps({"signed": True}), encoding="utf-8")
    sf.write_text(json.dumps({
        "state": "done", "rc": rc,
        "stdout": f"refusing: {slug}\n" if slug else "gate passed\n",
        "stderr": "", "receipt": str(receipt), "duration_s": 1.0,
        "train": train.branch, "tip": train.tip, "base": train.base}), encoding="utf-8")
    return sf


def _chain_fixture(tmp_path, monkeypatch, *, members: int = 21):
    """A real 3-train chain, dispatched by the daemon itself (gates stubbed out).

    Going through `run_once` rather than calling the assembler directly is the
    point: the state-file keys under test are the ones the daemon will actually
    read back next tick.
    """
    _two_fake_twins(monkeypatch)
    repo = _init_repo(tmp_path / "repo")
    m0 = _git(repo, "rev-parse", "HEAD")
    loops_root = tmp_path / "loops"
    for i in range(members):
        _commit_on(repo, f"cand-{i}", m0, f"f{i}.txt", f"F{i}\n")
        _write_candidate(loops_root, f"{i:064x}", f"cand-{i}", m0, [f"f{i}.txt"])
    loop = _make_loop(loops_root, repo, _fake_offload(tmp_path), allow_remote_gate=True)
    loop.dispatch_gate = lambda train, *, allow_remote, twin=None: None  # type: ignore[assignment]
    out = loop.run_once()
    chain = [o.train for o in out if o.action == "dispatched"]
    assert len(chain) == 3, [(o.action, o.detail) for o in out]
    assert chain[1].base == chain[0].tip and chain[2].base == chain[1].tip
    return repo, m0, loops_root, chain


# --- 1. assembly shape --------------------------------------------------------


def test_chain_roots_each_chunk_on_the_previous_tip(tmp_path):
    """>cap reps chain; <=cap reps are byte-for-byte the pre-chain single train."""
    from bridge.train_assembler import _train_branch_name

    repo = _init_repo(tmp_path / "repo")
    m0 = _git(repo, "rev-parse", "HEAD")
    for i in range(12):
        _commit_on(repo, f"cand-{i}", m0, f"f{i}.txt", f"F{i}\n")
    cands = [_cand(f"{i:064x}", f"cand-{i}", m0) for i in range(12)]

    trains, _ = _assemble(repo, cands, m0, tmp_path, chain_depth=3)
    assert len(trains) == 2, trains
    assert trains[0].base == m0 and trains[0].parent is None
    assert trains[0].chain_index == 0 and trains[0].root == m0
    # THE invariant: train 2 is built ON train 1's tip, so its gate grades the
    # exact tree that lands if the whole prefix lands.
    assert trains[1].base == trains[0].tip
    assert trains[1].parent == trains[0].branch
    assert trains[1].chain_index == 1
    assert trains[1].root == m0, "the chain stays anchored to ONE main"
    assert trains[0].tip in _git(repo, "rev-list", trains[1].tip).splitlines(), \
        "train 1's tip must be an ANCESTOR of train 2's tip, not a sibling"
    assert not (set(trains[0].paths) & set(trains[1].paths))

    # <= cap: exactly one train, on main, unchained — including the branch NAME,
    # which is what keeps every pre-chain gate key valid.
    small, _ = _assemble(repo, cands[:5], m0, tmp_path, chain_depth=3)
    assert len(small) == 1
    assert small[0].base == m0 and small[0].root == m0
    assert small[0].parent is None and small[0].chain_index == 0
    assert small[0].branch == _train_branch_name([c.ident for c in cands[:5]])


# --- 2. longest passed prefix lands, attribution, supersession ----------------


def test_pass_pass_fail_lands_the_two_and_rejects_only_the_third(tmp_path, monkeypatch):
    repo, m0, loops_root, chain = _chain_fixture(tmp_path, monkeypatch)
    _gate_result(loops_root, chain[0], rc=0)
    _gate_result(loops_root, chain[1], rc=0)
    _gate_result(loops_root, chain[2], rc=2, slug="secrets")

    _two_fake_twins(monkeypatch)
    loop = _make_loop(loops_root, repo, _fake_offload(tmp_path), allow_remote_gate=True)
    lands: list[str] = []
    real_land = loop.land_train
    loop.land_train = lambda train: (lands.append(train.branch), real_land(train))[1]  # type: ignore[assignment]
    out = loop.run_once()

    # ONE main advance, to the DEEPEST passed train's tip: the prefix landed whole.
    assert lands == [chain[1].branch], lands
    assert _git(repo, "rev-parse", "main") == chain[1].tip
    actions = [(o.train.branch, o.action) for o in out]
    assert (chain[0].branch, "landed") in actions, actions
    assert (chain[1].branch, "landed") in actions, actions
    assert (chain[2].branch, "rejected") in actions, actions

    events = _ledger_events(loops_root)
    merged = {e["id"] for e in events if e["event"] == "merged"}
    landed_ids = {m["id"] for t in chain[:2] for m in t.members}
    assert merged == landed_ids, "every member of every landed train owes a merged event"
    assert all(e["detail"]["merge_sha"] == chain[1].tip
               for e in events if e["event"] == "merged")
    # train 1's members are recorded against the sha the GATE graded, not the
    # prefix tip, so the two facts stay separable in the ledger.
    per_train = {e["id"]: e["detail"]["candidate_sha"]
                 for e in events if e["event"] == "merged"}
    assert {per_train[m["id"]] for m in chain[0].members} == {chain[0].tip}
    assert {per_train[m["id"]] for m in chain[1].members} == {chain[1].tip}
    # ONLY the boundary train is rejected, and only because every ancestor passed.
    rejected_events = [e for e in events if e["event"] == "rejected"]
    rejected = {e["id"] for e in rejected_events}
    assert rejected == {m["id"] for m in chain[2].members}, rejected
    assert not (rejected & landed_ids)
    # The rejection NAMES the ancestors whose commits were in the graded tree:
    # the attribution argument is sound, but an interaction defect with an
    # ancestor would surface here too, and the next agent must be able to see it.
    for ev in rejected_events:
        assert ev["detail"]["chain_ancestors"] == [chain[0].branch, chain[1].branch]
        assert "PASSED their own gate" in ev["detail"]["reason"]
    for m in chain[2].members:
        body = json.loads((loops_root / "rejected" / f"sha256_{m['id'].split(':')[1]}.json")
                          .read_text())
        assert body["detail"]["chain_ancestors"] == [chain[0].branch, chain[1].branch]


def test_pass_fail_pass_discards_the_third_verdict_instead_of_rejecting_it(
        tmp_path, monkeypatch):
    repo, m0, loops_root, chain = _chain_fixture(tmp_path, monkeypatch)
    _gate_result(loops_root, chain[0], rc=0)
    _gate_result(loops_root, chain[1], rc=2, slug="secrets")
    _gate_result(loops_root, chain[2], rc=0)          # a PASS that means nothing

    _two_fake_twins(monkeypatch)
    loop = _make_loop(loops_root, repo, _fake_offload(tmp_path), allow_remote_gate=True)
    out = loop.run_once()

    assert _git(repo, "rev-parse", "main") == chain[0].tip, \
        "only the passed PREFIX may land; train 3 sits behind a failed ancestor"
    by_branch = {o.train.branch: o.action for o in out}
    assert by_branch[chain[0].branch] == "landed"
    # The boundary train is MULTI-member, so its composite candidate-defect
    # verdict is NOT member-specific: it ISOLATES (each member re-gates solo)
    # rather than mass-rejecting every member. This is the member-aware failure-
    # isolation fix — a 10-member boundary red used to terminalise 10 candidates.
    assert by_branch[chain[1].branch] == "isolated", by_branch
    assert by_branch[chain[2].branch] == "superseded", by_branch

    events = _ledger_events(loops_root)
    second = {m["id"] for m in chain[1].members}
    assert not (second & {e["id"] for e in events if e["event"] == "rejected"}), \
        "a multi-member boundary train isolates its members, never mass-rejects them"
    isf = iso_state_path(loops_root, chain[1])
    ist = json.loads(isf.read_text())
    assert ist["disposition"] == "isolation-pending"
    assert set(ist["isolation_members"]) == second
    # the durable ledger backstop carries every isolated member (non-terminal)
    assert {e["id"] for e in events if e["event"] == "isolated"} == second
    third = {m["id"] for m in chain[2].members}
    assert not (third & {e["id"] for e in events if e["event"] == "rejected"}), \
        "a train behind a FAILED ancestor is not attributable — never rejected"
    assert not (third & {e["id"] for e in events if e["event"] == "merged"})
    assert any("superseded-by-chain-ancestor" in line for line in loop.lines), loop.lines
    # The discarded run is KEPT on disk: a deterministic tip means the same
    # `<train>@<tip>` key next tick, so the ~618s gate is reused, not repeated.
    sf = gate_state_path(loops_root, chain[2])
    assert sf.exists()
    assert json.loads(sf.read_text())["state"] == "done"


def test_a_train_behind_an_UNRESOLVED_ancestor_is_also_discarded(tmp_path, monkeypatch):
    """Unresolved is not green: a descendant's pass is not landable either."""
    repo, m0, loops_root, chain = _chain_fixture(tmp_path, monkeypatch)
    # train 1 never reports (no state file at all -> re-dispatched this tick),
    # train 2 passed. Nothing may land: train 2's tree contains train 1.
    _gate_result(loops_root, chain[1], rc=0)

    _two_fake_twins(monkeypatch)
    loop = _make_loop(loops_root, repo, _fake_offload(tmp_path), allow_remote_gate=True)
    loop.dispatch_gate = lambda train, *, allow_remote, twin=None: None  # type: ignore[assignment]
    out = loop.run_once()

    assert _git(repo, "rev-parse", "main") == m0, "nothing may land under an ungraded ancestor"
    by_branch = {o.train.branch: o.action for o in out}
    assert by_branch[chain[1].branch] == "superseded", by_branch
    events = _ledger_events(loops_root)
    assert not [e for e in events if e["event"] in ("merged", "rejected")]


# --- 3. single writer ---------------------------------------------------------


def test_a_landed_prefix_is_exactly_one_push(tmp_path, monkeypatch):
    """Prefix length is irrelevant: main advances once and origin is written once."""
    repo, m0, loops_root, chain = _chain_fixture(tmp_path, monkeypatch)
    _gate_result(loops_root, chain[0], rc=0)
    _gate_result(loops_root, chain[1], rc=0)
    _gate_result(loops_root, chain[2], rc=0)          # the WHOLE chain passed

    _two_fake_twins(monkeypatch)
    loop = _make_loop(loops_root, repo, _fake_offload(tmp_path),
                      allow_remote_gate=True, remote="origin", push=True)
    pushes: list[tuple] = []
    real = gl.git

    def spy(r, *args, **kw):
        if args and args[0] == "push":
            pushes.append(args)
            return (0, "", "")
        return real(r, *args, **kw)

    monkeypatch.setattr(gl, "git", spy)
    out = loop.run_once()

    assert len(pushes) == 1, pushes
    assert pushes[0] == ("push", "origin", f"{chain[2].tip}:refs/heads/main"), pushes
    assert _git(repo, "rev-parse", "main") == chain[2].tip
    assert [o.action for o in out].count("landed") == 3
    merged = {e["id"] for e in _ledger_events(loops_root) if e["event"] == "merged"}
    assert merged == {m["id"] for t in chain for m in t.members}


# --- 4. conflict parking ------------------------------------------------------


def _conflicting_candidate(repo: Path, m0: str) -> tuple[str, str]:
    """A candidate whose diff no longer applies to main. Returns (branch, main)."""
    (repo / "x.txt").write_text("A\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "x.txt: A")
    base = _git(repo, "rev-parse", "HEAD")
    _commit_on(repo, "cand-x", base, "x.txt", "B\n")      # candidate: A -> B
    (repo / "x.txt").write_text("C\n", encoding="utf-8")  # main:      A -> C
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "x.txt: C")
    return base, _git(repo, "rev-parse", "HEAD")


def test_three_content_conflicts_park_the_candidate_and_stop_the_thrash(tmp_path):
    from bridge import train_assembler as ta

    repo = _init_repo(tmp_path / "repo")
    m0 = _git(repo, "rev-parse", "HEAD")
    base, main = _conflicting_candidate(repo, m0)
    loops_root = tmp_path / "loops"
    (loops_root / "state").mkdir(parents=True, exist_ok=True)
    ident = f"sha256:{'7' * 64}"

    def one_tick():
        return _assemble(repo, [_cand("7" * 64, "cand-x", base)], main, tmp_path,
                         root=loops_root)

    for tick in (1, 2):
        trains, excluded = one_tick()
        assert trains == []
        assert any("cherry-pick conflict" in e["why"] for e in excluded), excluded
        strikes = json.loads((loops_root / "state" / "assembly_strikes.json").read_text())
        assert strikes["candidates"][ident]["strikes"] == tick
        assert not (loops_root / "parked" / f"sha256_{'7' * 64}.json").exists()

    trains, excluded = one_tick()                     # the third strike
    assert trains == []
    marker = loops_root / "parked" / f"sha256_{'7' * 64}.json"
    assert marker.exists(), "the third consecutive conflict must PARK"
    body = json.loads(marker.read_text())
    assert body["id"] == ident and body["needs"] == "human"
    assert "re-anchor onto current main" in body["remedy"]
    parked_events = [e for e in _ledger_events(loops_root) if e["event"] == "parked"]
    assert len(parked_events) == 1, parked_events
    assert parked_events[0]["id"] == ident
    assert parked_events[0]["detail"]["alerted"] is True
    assert parked_events[0]["detail"]["class"] == "blocked-on-human"
    assert "re-anchor onto current main" in parked_events[0]["detail"]["remedy"]
    assert (loops_root / "ALERTS.md").read_text().count(ident) == 1

    # THE POINT: the 4th tick does not even try. The thrash stops.
    trains, excluded = one_tick()
    assert trains == []
    assert any("PARKED" in e["why"] for e in excluded), excluded
    assert not any("cherry-pick conflict" in e["why"] for e in excluded), \
        "a parked candidate must never reach the cherry-pick again"
    assert len([e for e in _ledger_events(loops_root) if e["event"] == "parked"]) == 1, \
        "one alert per parked item, ever"
    # …and a park is a suspension, never a terminal event.
    assert not [e for e in _ledger_events(loops_root)
                if e["event"] in ("merged", "rejected", "closed")]
    assert ta.parked_ids(loops_root) >= {ident}

    # AN AUTHENTICATED UN-PARK MUST ACTUALLY RELEASE IT. The marker is the only
    # authority: when a human's release removes it, this module's own strike
    # record must not keep the candidate excluded behind the operator's back —
    # the episode is over and the counter starts again from zero.
    marker.unlink()
    trains, excluded = one_tick()
    assert not any("PARKED" in e["why"] for e in excluded), excluded
    assert any("cherry-pick conflict" in e["why"] for e in excluded), excluded
    strikes = json.loads((loops_root / "state" / "assembly_strikes.json").read_text())
    assert strikes["candidates"][ident]["strikes"] == 1, strikes
    assert not strikes["candidates"][ident].get("parked")


def test_an_instrument_shaped_cherry_pick_failure_never_strikes(tmp_path, monkeypatch):
    from bridge import train_assembler as ta

    repo = _init_repo(tmp_path / "repo")
    m0 = _git(repo, "rev-parse", "HEAD")
    _commit_on(repo, "cand-i", m0, "i.txt", "III\n")
    loops_root = tmp_path / "loops"
    (loops_root / "state").mkdir(parents=True, exist_ok=True)

    monkeypatch.setattr(
        ta, "_cherry_pick_onto",
        lambda builder, commit: (False, "TimeoutExpired: git took too long", True))
    for _ in range(ta.ASSEMBLY_STRIKE_LIMIT + 2):
        trains, excluded = _assemble(repo, [_cand("8" * 64, "cand-i", m0)], m0,
                                     tmp_path, root=loops_root)
        assert trains == []
        assert any("could not RUN" in e["why"] for e in excluded), excluded

    assert not (loops_root / "state" / "assembly_strikes.json").exists(), \
        "a git timeout is a fact about the HOST; it must never earn a strike"
    assert not (loops_root / "parked").exists()
    assert not [e for e in _ledger_events(loops_root) if e["event"] == "parked"]


def test_a_successful_cherry_pick_resets_the_strikes(tmp_path):
    repo = _init_repo(tmp_path / "repo")
    m0 = _git(repo, "rev-parse", "HEAD")
    base, main = _conflicting_candidate(repo, m0)
    loops_root = tmp_path / "loops"
    (loops_root / "state").mkdir(parents=True, exist_ok=True)
    ident = f"sha256:{'7' * 64}"
    strikes_file = loops_root / "state" / "assembly_strikes.json"

    for _ in range(2):
        _assemble(repo, [_cand("7" * 64, "cand-x", base)], main, tmp_path,
                  root=loops_root)
    assert json.loads(strikes_file.read_text())["candidates"][ident]["strikes"] == 2

    # main goes back to the content the candidate was written against, so the
    # identical diff now applies: the INPUT changed, and the streak is over.
    (repo / "x.txt").write_text("A\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "x.txt: back to A")
    reapplied = _git(repo, "rev-parse", "HEAD")
    trains, _ = _assemble(repo, [_cand("7" * 64, "cand-x", base)], reapplied,
                          tmp_path, root=loops_root)
    assert len(trains) == 1 and trains[0].members[0]["id"] == ident
    assert ident not in json.loads(strikes_file.read_text())["candidates"]
    assert not (loops_root / "parked" / f"sha256_{'7' * 64}.json").exists()


def test_a_human_parked_candidate_is_never_assembled(tmp_path):
    """CONTRACT §9 drop-at-source: the marker is what a producer must obey.

    Measured 2026-08-11: `sha256:e1c1fac6806d` was parked by a human at 22:55 and
    was still being cherry-picked (and conflicting) by the assembler every 60s
    hours later, because the daemon's selector does not read `parked/`.
    """
    repo = _init_repo(tmp_path / "repo")
    m0 = _git(repo, "rev-parse", "HEAD")
    _commit_on(repo, "cand-h", m0, "h.txt", "HHH\n")
    loops_root = tmp_path / "loops"
    (loops_root / "parked").mkdir(parents=True, exist_ok=True)
    ident = f"sha256:{'4' * 64}"
    (loops_root / "parked" / f"sha256_{'4' * 64}.json").write_text(
        json.dumps({"id": ident, "kind": "candidate", "reason": "human park",
                    "at": "2026-08-10T22:55:38Z", "needs": "human"}),
        encoding="utf-8")

    trains, excluded = _assemble(repo, [_cand("4" * 64, "cand-h", m0)], m0, tmp_path,
                                 root=loops_root)
    assert trains == []
    assert any("PARKED" in e["why"] for e in excluded), excluded
    # Without a root the assembler has no queue to read, and behaves as before.
    trains, _ = _assemble(repo, [_cand("4" * 64, "cand-h", m0)], m0, tmp_path)
    assert len(trains) == 1


# --- 5. determinism -----------------------------------------------------------


def test_same_members_and_root_give_the_same_branch_and_tip(tmp_path):
    """And a DIFFERENT root gives the same branch with a different gate KEY."""
    repo = _init_repo(tmp_path / "repo")
    m0 = _git(repo, "rev-parse", "HEAD")
    for i in range(12):
        _commit_on(repo, f"cand-{i}", m0, f"f{i}.txt", f"F{i}\n")
    cands = [_cand(f"{i:064x}", f"cand-{i}", m0) for i in range(12)]

    first, _ = _assemble(repo, cands, m0, tmp_path, chain_depth=3)
    second, _ = _assemble(repo, cands, m0, tmp_path, chain_depth=3)
    assert [t.branch for t in first] == [t.branch for t in second]
    assert [t.tip for t in first] == [t.tip for t in second], \
        "an unchanged chain must mint the SAME tips, or every tick is a new gate key"
    assert [t.base for t in first] == [t.base for t in second]

    # Move main. The tail chunk keeps its member set, so it keeps its branch NAME
    # (which is what lets a running gate be picked up when its ancestor lands) —
    # but a new root is a new parent commit, so the tip, and therefore the
    # `<train>@<tip>` gate key, is necessarily different. No stale result can be
    # read against a tree that changed.
    (repo / "moved.txt").write_text("moved\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "main moves")
    m1 = _git(repo, "rev-parse", "HEAD")
    moved, _ = _assemble(repo, cands, m1, tmp_path, chain_depth=3)
    assert [t.branch for t in moved] == [t.branch for t in first]
    assert all(a.tip != b.tip for a, b in zip(first, moved, strict=True))
    root = tmp_path / "loops-keys"
    assert (gate_state_path(root, first[1]).name
            != gate_state_path(root, moved[1]).name), \
        "a train on a new root must never reuse the old gate-state key"


def test_a_chained_train_keeps_its_key_when_its_ancestor_lands(tmp_path):
    """The reuse that makes chaining pay for itself.

    When train 1 lands, train 2's members become chunk 0 on the NEW main — which
    is train 1's tip, exactly what train 2 was already built on. Same members,
    same base, so the same branch, the same tip and the same `<train>@<tip>` key:
    a gate already running (or already passed) for train 2 is picked up next tick
    instead of being thrown away and re-run.
    """
    repo = _init_repo(tmp_path / "repo")
    m0 = _git(repo, "rev-parse", "HEAD")
    for i in range(12):
        _commit_on(repo, f"cand-{i}", m0, f"f{i}.txt", f"F{i}\n")
    cands = [_cand(f"{i:064x}", f"cand-{i}", m0) for i in range(12)]
    chain, _ = _assemble(repo, cands, m0, tmp_path, chain_depth=3)
    assert len(chain) == 2

    # train 1 lands: main is now its tip, and its members are terminal.
    landed = {m["id"] for m in chain[0].members}
    survivors = [c for c in cands if c.ident not in landed]
    after, _ = _assemble(repo, survivors, chain[0].tip, tmp_path, chain_depth=3)
    assert len(after) == 1
    assert after[0].branch == chain[1].branch
    assert after[0].tip == chain[1].tip
    assert after[0].parent is None and after[0].chain_index == 0
    root = tmp_path / "loops-keys"
    assert gate_state_path(root, after[0]) == gate_state_path(root, chain[1]), \
        "promoting a chained train to chunk 0 must NOT mint a new gate key"


def test_landing_refuses_a_prefix_that_is_not_a_contiguous_chain(tmp_path):
    """The last guard before ungated code could ride a fast-forward onto main.

    `land_train` moves main to the DEEPEST tip, which carries every commit
    beneath it. If the list handed to `_land_prefix` is not the chain's own
    prefix from index 0, those commits include trains whose verdicts this call
    never read — so the merge is refused outright, loudly, as an instrument fault.
    """
    repo = _init_repo(tmp_path / "repo")
    m0 = _git(repo, "rev-parse", "HEAD")
    loops_root = tmp_path / "loops"
    loop = _make_loop(loops_root, repo, _fake_offload(tmp_path))
    train = _manual_train(repo, m0)
    # A train that CLAIMS to be chained behind something this call never graded.
    orphan = Train(branch=train.branch, base=train.base, tip=train.tip,
                   members=train.members, paths=train.paths,
                   parent="train/never-graded", chain_root=m0, chain_index=1)
    v = read_gate_verdict(_gate_result(loops_root, orphan, rc=0))
    assert v is not None and v.result == "pass"

    out = loop.on_pass(orphan, v)

    assert out.action == "skipped", out
    assert _git(repo, "rev-parse", "main") == m0, "UNGRADED ancestors must never land"
    assert not [e for e in _ledger_events(loops_root) if e["event"] == "merged"]
    assert any("REFUSING to land" in a for a in loop.alerts), loop.alerts


# ======================================================== ROUND-2 REVIEW FIXES
# Three findings from the second (gemini) lens, each written to FAIL against
# a9945e854, plus the adversarial-strike-file vector the lens did not cover.


def _mock_cherry_pick_exit(monkeypatch, *, rc: int, stderr: str, stdout: str = ""):
    """Make the cherry-pick itself exit `rc`, leaving every other git call real.

    Patches `subprocess.run` at module scope the way the reviewer's repro does,
    so the CLASSIFIER — not a convenient stub of it — is what is under test.
    """
    real_run = subprocess.run

    def fake(cmd, *args, **kwargs):
        if (isinstance(cmd, (list, tuple)) and "cherry-pick" in cmd
                and "--abort" not in cmd):
            return subprocess.CompletedProcess(cmd, rc, stdout=stdout, stderr=stderr)
        return real_run(cmd, *args, **kwargs)

    monkeypatch.setattr(subprocess, "run", fake)


# --- B1 (BLOCKER): a host-level git failure must never earn a park strike -----


@pytest.mark.parametrize(("rc", "stderr"), [
    # A held index.lock. NOTE its stderr ends "fatal: cherry-pick failed" — the
    # exact reason a substring match on that phrase is a trap, not a signature.
    (128, "error: Unable to create '.git/index.lock': File exists.\n"
          "fatal: cherry-pick failed\n"),
    (128, "fatal: bad object 0123456789abcdef0123456789abcdef01234567\n"),
    (128, "fatal: Unable to write new index file\n"),          # ENOSPC shape
    (137, ""),                                                 # OOM-killed
])
def test_a_host_level_git_failure_never_earns_a_strike(tmp_path, monkeypatch, rc, stderr):
    """rc=128/137 with no conflict evidence is an INSTRUMENT fault.

    Pre-fix, ANY non-zero cherry-pick exit was classified as a content conflict,
    so three transient host errors in three ticks PARKED an innocent candidate —
    terminalising an instrument condition, which CONTRACT §1 forbids.
    """
    from bridge import train_assembler as ta

    repo = _init_repo(tmp_path / "repo")
    m0 = _git(repo, "rev-parse", "HEAD")
    _commit_on(repo, "cand-a", m0, "a.txt", "A\n")
    loops_root = tmp_path / "loops"
    (loops_root / "state").mkdir(parents=True, exist_ok=True)
    ident = f"sha256:{'1' * 64}"

    _mock_cherry_pick_exit(monkeypatch, rc=rc, stderr=stderr)
    for _ in range(ta.ASSEMBLY_STRIKE_LIMIT + 2):
        trains, excluded = _assemble(repo, [_cand("1" * 64, "cand-a", m0)], m0,
                                     tmp_path, root=loops_root)
        assert trains == []
        assert any("no conflict evidence" in e["why"] for e in excluded), excluded

    assert ident not in ta.parked_ids(loops_root), \
        "an innocent candidate was PARKED for a host-level git failure"
    assert not (loops_root / "parked").exists()
    assert not [e for e in _ledger_events(loops_root) if e["event"] == "parked"]
    strikes_file = loops_root / "state" / "assembly_strikes.json"
    if strikes_file.exists():
        assert ident not in json.loads(strikes_file.read_text())["candidates"]


def test_a_real_content_conflict_is_still_recognised_through_the_classifier(tmp_path):
    """The other half of B1: tightening the test must not stop parking real ones."""
    from bridge import train_assembler as ta

    repo = _init_repo(tmp_path / "repo")
    m0 = _git(repo, "rev-parse", "HEAD")
    base, main = _conflicting_candidate(repo, m0)
    loops_root = tmp_path / "loops"
    (loops_root / "state").mkdir(parents=True, exist_ok=True)
    ident = f"sha256:{'7' * 64}"
    for _ in range(ta.ASSEMBLY_STRIKE_LIMIT):
        _assemble(repo, [_cand("7" * 64, "cand-x", base)], main, tmp_path,
                  root=loops_root)
    assert ident in ta.parked_ids(loops_root)


def test_the_conflict_classifier_reads_real_git_output(tmp_path):
    """Unit-level: rc and evidence, judged exactly as git reports them."""
    from bridge.train_assembler import _cherry_pick_conflicted

    repo = _init_repo(tmp_path / "repo")
    conflict_blob = ("Auto-merging x.txt\nCONFLICT (content): Merge conflict in x.txt\n"
                     "error: could not apply 7034879... feat\n")
    lock_blob = ("error: Unable to create '.git/index.lock': File exists.\n"
                 "fatal: cherry-pick failed\n")
    assert _cherry_pick_conflicted(repo, 1, conflict_blob) is True
    # Same evidence, a FATAL exit code: not a conflict.
    assert _cherry_pick_conflicted(repo, 128, conflict_blob) is False
    # "fatal: cherry-pick failed" is the host's words, not a conflict signature.
    assert _cherry_pick_conflicted(repo, 128, lock_blob) is False
    assert _cherry_pick_conflicted(repo, 1, lock_blob) is False
    # rc 1 with no evidence at all and no CHERRY_PICK_HEAD -> not proven.
    assert _cherry_pick_conflicted(repo, 1, "") is False


# --- M1 (MAJOR): an unwritable park marker must not become a park/event thrash -


def test_an_unwritable_park_marker_parks_nothing_and_does_not_thrash(tmp_path, monkeypatch):
    """Marker write fails (ENOSPC): NOTHING is claimed, and the next tick is sane.

    Pre-fix the ledger event went first, so a failed marker write left
    `parked: true` in the strike record with no marker on disk — and the next
    tick's drop-at-source read the absent marker as "the human released it",
    wiped the record, and let the candidate earn three more strikes and ANOTHER
    `parked` event, forever.
    """
    from bridge import train_assembler as ta

    repo = _init_repo(tmp_path / "repo")
    m0 = _git(repo, "rev-parse", "HEAD")
    base, main = _conflicting_candidate(repo, m0)
    loops_root = tmp_path / "loops"
    (loops_root / "state").mkdir(parents=True, exist_ok=True)
    ident = f"sha256:{'7' * 64}"

    real_write = ta._write_json_atomic

    def no_space(path, obj):
        if path.parent.name == "parked":
            raise OSError(28, "No space left on device")
        return real_write(path, obj)

    monkeypatch.setattr(ta, "_write_json_atomic", no_space)

    for tick in range(1, ta.ASSEMBLY_STRIKE_LIMIT + 3):
        trains, excluded = _assemble(repo, [_cand("7" * 64, "cand-x", base)], main,
                                     tmp_path, root=loops_root)
        assert trains == []
        rec = json.loads((loops_root / "state" / "assembly_strikes.json").read_text()
                         )["candidates"][ident]
        # The strikes keep CLIMBING — they are never wiped and never reset.
        assert rec["strikes"] == tick, (tick, rec)
        assert not rec.get("parked"), "a park that could not be written is not a park"
        if tick >= ta.ASSEMBLY_STRIKE_LIMIT:      # a park was attempted and refused
            assert rec["park_write_failed"].startswith("OSError"), rec
        else:                                     # below the limit nothing is tried
            assert "park_write_failed" not in rec, rec

    # No park was ever claimed: no marker, no event, and exactly ONE alert about
    # the instrument fault (not one per tick).
    assert ta.parked_ids(loops_root) == set()
    assert not [e for e in _ledger_events(loops_root) if e["event"] == "parked"]
    alerts = (loops_root / "ALERTS.md").read_text()
    assert alerts.count("could NOT write the park marker") == 1, alerts

    # Disk comes back: the very next conflicting tick parks properly, once.
    monkeypatch.setattr(ta, "_write_json_atomic", real_write)
    _assemble(repo, [_cand("7" * 64, "cand-x", base)], main, tmp_path, root=loops_root)
    assert ident in ta.parked_ids(loops_root)
    assert len([e for e in _ledger_events(loops_root) if e["event"] == "parked"]) == 1
    rec = json.loads((loops_root / "state" / "assembly_strikes.json").read_text()
                     )["candidates"][ident]
    assert rec["parked"] is True and rec["event_written"] is True
    assert "park_write_failed" not in rec


def test_a_park_whose_event_failed_is_healed_from_the_marker_next_tick(tmp_path, monkeypatch):
    """Marker ok, ledger append fails: the candidate IS parked, and the missing
    event is appended next tick rather than being lost forever."""
    from bridge import train_assembler as ta

    repo = _init_repo(tmp_path / "repo")
    m0 = _git(repo, "rev-parse", "HEAD")
    base, main = _conflicting_candidate(repo, m0)
    loops_root = tmp_path / "loops"
    (loops_root / "state").mkdir(parents=True, exist_ok=True)
    ident = f"sha256:{'7' * 64}"

    def refuse(root, event):
        raise ta.LedgerAppendError("ledger unwritable (test)", phase="append")

    monkeypatch.setattr(ta, "append_event", refuse)
    for _ in range(ta.ASSEMBLY_STRIKE_LIMIT):
        trains, excluded = _assemble(repo, [_cand("7" * 64, "cand-x", base)], main,
                                     tmp_path, root=loops_root)
    # Parked on disk (so every producer skips it) but the event is owed.
    assert ident in ta.parked_ids(loops_root)
    assert any("healed" in e["why"] or "NOT recorded" in e["why"] for e in excluded)
    rec = json.loads((loops_root / "state" / "assembly_strikes.json").read_text()
                     )["candidates"][ident]
    assert rec["parked"] is True and rec["event_written"] is False

    monkeypatch.undo()
    _assemble(repo, [_cand("7" * 64, "cand-x", base)], main, tmp_path, root=loops_root)
    events = [e for e in _ledger_events(loops_root) if e["event"] == "parked"]
    assert len(events) == 1, "the owed event is healed exactly once"
    assert events[0]["id"] == ident
    rec = json.loads((loops_root / "state" / "assembly_strikes.json").read_text()
                     )["candidates"][ident]
    assert rec["event_written"] is True


# --- m1 (MINOR): every train in a failed prefix landing is accounted for ------


def test_a_failed_prefix_push_still_reports_every_train_it_attempted(
        tmp_path, monkeypatch):
    repo, m0, loops_root, chain = _chain_fixture(tmp_path, monkeypatch)
    _gate_result(loops_root, chain[0], rc=0)
    _gate_result(loops_root, chain[1], rc=0)

    _two_fake_twins(monkeypatch)
    loop = _make_loop(loops_root, repo, _fake_offload(tmp_path), allow_remote_gate=True)
    loop.land_train = lambda train: (None, "push-non-ff")  # type: ignore[assignment]
    out = loop.run_once()

    actions = {o.train.branch: o.action for o in out}
    assert actions[chain[1].branch] == "instrument", actions
    assert chain[0].branch in actions, \
        "the ancestor train vanished from the tick's outcomes after a failed push"
    assert actions[chain[0].branch] == "skipped"
    detail = next(o.detail for o in out if o.train.branch == chain[0].branch)
    assert "prefix landing failed" in detail and chain[1].branch in detail
    # Nothing landed and nothing was terminalised.
    assert _git(repo, "rev-parse", "main") == m0
    assert not [e for e in _ledger_events(loops_root)
                if e["event"] in ("merged", "rejected")]


# --- unaddressed vector: an adversarial strike file ---------------------------


def test_a_corrupt_strike_file_never_parks_without_a_conflict_this_tick(tmp_path):
    """Parking requires a LIVE conflict observation this tick AND strikes>=limit.

    The strike file is on disk, so it is untrusted input: wrong types, unknown
    ids and a pre-set 999 must neither crash assembly nor park a candidate that
    assembled cleanly.
    """
    from bridge import train_assembler as ta

    repo = _init_repo(tmp_path / "repo")
    m0 = _git(repo, "rev-parse", "HEAD")
    _commit_on(repo, "cand-ok", m0, "ok.txt", "OK\n")
    loops_root = tmp_path / "loops"
    (loops_root / "state").mkdir(parents=True, exist_ok=True)
    clean = f"sha256:{'2' * 64}"
    (loops_root / "state" / "assembly_strikes.json").write_text(json.dumps({
        "version": "not-an-int",
        "candidates": {
            clean: {"strikes": 999, "bases": "not-a-list", "parked": "yes-ish"},
            "sha256:" + "9" * 64: {"strikes": -5},            # unknown id
            "not-an-id": ["not", "a", "dict"],                # wrong shape
            "sha256:" + "8" * 64: "also-not-a-dict",
        },
        "extra": {"unexpected": True},
    }), encoding="utf-8")

    trains, excluded = _assemble(repo, [_cand("2" * 64, "cand-ok", m0)], m0,
                                 tmp_path, root=loops_root)

    # It ASSEMBLED — a pre-set 999 is not an observation, and no conflict happened.
    assert len(trains) == 1 and trains[0].members[0]["id"] == clean
    assert ta.parked_ids(loops_root) == set()
    assert not [e for e in _ledger_events(loops_root) if e["event"] == "parked"]
    # The successful pick cleared the junk record outright.
    body = json.loads((loops_root / "state" / "assembly_strikes.json").read_text())
    assert clean not in body["candidates"]


def test_a_garbage_strike_file_is_read_as_no_strikes_at_all(tmp_path):
    from bridge.train_assembler import _load_strikes

    root = tmp_path / "loops"
    (root / "state").mkdir(parents=True)
    for junk in ("", "{", "[]", '"a string"', '{"candidates": []}', "\x00\x01"):
        (root / "state" / "assembly_strikes.json").write_text(junk, encoding="utf-8")
        assert _load_strikes(root) == {}, junk


def test_a_preset_strike_count_still_parks_when_a_conflict_is_observed(tmp_path):
    """The other direction: a legitimate record IS honoured on a live conflict."""
    from bridge import train_assembler as ta

    repo = _init_repo(tmp_path / "repo")
    m0 = _git(repo, "rev-parse", "HEAD")
    base, main = _conflicting_candidate(repo, m0)
    loops_root = tmp_path / "loops"
    (loops_root / "state").mkdir(parents=True, exist_ok=True)
    ident = f"sha256:{'7' * 64}"
    (loops_root / "state" / "assembly_strikes.json").write_text(json.dumps({
        "version": 1, "candidates": {ident: {"strikes": ta.ASSEMBLY_STRIKE_LIMIT - 1}},
    }), encoding="utf-8")

    _assemble(repo, [_cand("7" * 64, "cand-x", base)], main, tmp_path, root=loops_root)
    assert ident in ta.parked_ids(loops_root), \
        "one live conflict on top of limit-1 recorded strikes must park"


def test_a_boolean_strike_count_is_not_silently_an_integer(tmp_path):
    """`True` is an int in Python, so `{"strikes": true}` would otherwise count
    as 1 and bring a candidate one tick closer to a park it never earned."""
    from bridge import train_assembler as ta

    repo = _init_repo(tmp_path / "repo")
    m0 = _git(repo, "rev-parse", "HEAD")
    base, main = _conflicting_candidate(repo, m0)
    loops_root = tmp_path / "loops"
    (loops_root / "state").mkdir(parents=True, exist_ok=True)
    ident = f"sha256:{'7' * 64}"
    (loops_root / "state" / "assembly_strikes.json").write_text(
        json.dumps({"version": 1, "candidates": {ident: {"strikes": True}}}),
        encoding="utf-8")

    for expected in (1, 2):                 # never 2, 3 — which would park here
        _assemble(repo, [_cand("7" * 64, "cand-x", base)], main, tmp_path,
                  root=loops_root)
        rec = json.loads((loops_root / "state" / "assembly_strikes.json").read_text()
                         )["candidates"][ident]
        assert rec["strikes"] == expected, rec
    assert ta.parked_ids(loops_root) == set()


# ============================================ ROUND-3 REVIEW FIX (M2, MAJOR)
# One malformed field in one on-disk JSON file must never take assembly down for
# the whole queue. `rec.get("bases") or []` looks defensive but `or` only
# replaces FALSY values, so `"bases": true` sails past it into a comprehension
# that raises TypeError. The same shape was on six further lines.


def test_park_reason_survives_every_adversarial_strike_record(tmp_path):
    """The M2 repro, adapted to assert SURVIVAL rather than the crash.

    `"abc"` is the subtle one: a string IS iterable, so a reader that only
    checks "can I loop over it" would happily record ['a','b','c'] as base shas.
    """
    from bridge.train_assembler import _park_reason

    for rec in (
        {"strikes": 3, "bases": True},          # the reported crash
        {"strikes": 3, "bases": 5},
        {"strikes": 3, "bases": {"a": 1}},
        {"strikes": 3, "bases": "abc"},         # iterable, but not a list of shas
        {"strikes": "9", "bases": None},
        {"strikes": None, "bases": [1, None, "cafebabecafe"]},
        {"strikes": True},
        {},
    ):
        text = _park_reason(rec)
        assert isinstance(text, str)
        assert "re-anchor onto current main" in text
        assert "abc" not in text, rec       # a string was iterated into chars
    assert "cafebabecafe" in _park_reason({"strikes": 3, "bases": [1, "cafebabecafe"]})


def test_typed_readers_never_raise_on_untrusted_json(tmp_path):
    from bridge.train_assembler import _as_count, _as_str_list, _as_text

    for junk in (True, False, None, 5, "abc", {"a": 1}, [1, "b", None], (), object()):
        assert isinstance(_as_str_list(junk), list)
        assert all(isinstance(v, str) for v in _as_str_list(junk))
        assert isinstance(_as_count(junk), int) and _as_count(junk) >= 0
        assert isinstance(_as_text(junk), str)
    assert _as_str_list(["a", 1, "b"]) == ["a", "b"]
    assert _as_str_list("abc") == [], "a string must not be iterated into characters"
    assert _as_count(True) == 0 and _as_count(-3) == 0 and _as_count(4) == 4
    assert _as_text("x" * 999, limit=10) == "x" * 10


def test_an_adversarial_strike_file_neither_crashes_nor_parks_a_clean_candidate(tmp_path):
    """Wrong types everywhere; assembly still schedules, and nothing is parked."""
    from bridge import train_assembler as ta

    repo = _init_repo(tmp_path / "repo")
    m0 = _git(repo, "rev-parse", "HEAD")
    _commit_on(repo, "cand-ok", m0, "ok.txt", "OK\n")
    loops_root = tmp_path / "loops"
    (loops_root / "state").mkdir(parents=True, exist_ok=True)
    clean = f"sha256:{'2' * 64}"
    (loops_root / "state" / "assembly_strikes.json").write_text(json.dumps({
        "version": 1,
        "candidates": {clean: {"strikes": "9", "bases": True,
                               "last_reason": {"not": "a string"},
                               "first_at": 17, "parked": None}},
    }), encoding="utf-8")

    trains, _ = _assemble(repo, [_cand("2" * 64, "cand-ok", m0)], m0, tmp_path,
                          root=loops_root)

    assert len(trains) == 1 and trains[0].members[0]["id"] == clean
    assert ta.parked_ids(loops_root) == set()
    assert not [e for e in _ledger_events(loops_root) if e["event"] == "parked"]


def test_an_adversarial_strike_file_still_parks_a_live_conflict_with_clean_fields(
        tmp_path):
    """The path M2 actually crashes on: a park driven by a poisoned record.

    Pre-fix this raises TypeError out of assemble_trains — a DoS on the landing
    pipeline. Post-fix the park happens and every field it publishes is typed.
    """
    from bridge import train_assembler as ta

    repo = _init_repo(tmp_path / "repo")
    m0 = _git(repo, "rev-parse", "HEAD")
    base, main = _conflicting_candidate(repo, m0)
    loops_root = tmp_path / "loops"
    (loops_root / "state").mkdir(parents=True, exist_ok=True)
    ident = f"sha256:{'7' * 64}"
    (loops_root / "state" / "assembly_strikes.json").write_text(json.dumps({
        "version": 1,
        "candidates": {ident: {"strikes": ta.ASSEMBLY_STRIKE_LIMIT + 2,
                               "bases": True,
                               "last_reason": ["not", "a", "string"]}},
    }), encoding="utf-8")

    trains, excluded = _assemble(repo, [_cand("7" * 64, "cand-x", base)], main,
                                 tmp_path, root=loops_root)

    assert trains == []
    assert ident in ta.parked_ids(loops_root)
    marker = json.loads((loops_root / "parked" / f"sha256_{'7' * 64}.json").read_text())
    # The poisoned `bases: true` is dropped and replaced by the base actually
    # observed; the poisoned `last_reason` list is replaced by THIS tick's real
    # conflict text, which is the evidence an operator needs — sanitised in type,
    # never blanked of content.
    assert marker["detail"]["bases"] == [main], marker["detail"]
    assert isinstance(marker["detail"]["last_conflict"], str)
    assert "could not apply" in marker["detail"]["last_conflict"]
    assert isinstance(marker["detail"]["strikes"], int)
    assert isinstance(marker["reason"], str)
    events = [e for e in _ledger_events(loops_root) if e["event"] == "parked"]
    assert len(events) == 1
    assert events[0]["detail"]["bases"] == [main]
    assert isinstance(events[0]["detail"]["last_conflict"], str)


@pytest.mark.parametrize("poison", [
    True,          # non-iterable: pre-fix this is TypeError out of assemble_trains
    5,             # non-iterable
    {"x": 1},      # iterable, so no crash — it publishes the KEYS as base shas
    "abc",         # iterable, so no crash — it publishes the CHARACTERS
])
def test_a_poisoned_record_cannot_crash_the_next_tick_heal_path(tmp_path, poison):
    """The live route to M2: `_park_reason` runs again on the HEAL path, where
    the record comes straight off disk untouched.

    On the strike-and-park path `_settle_strikes` happens to rewrite `bases`
    with a clean list first, which is why the crash needs this path (or the
    reviewer's direct call) to reach. Both failure modes are covered: the
    non-iterable poisons take assembly DOWN for the whole queue, and the
    iterable ones silently publish characters or dict keys as base shas.
    """
    repo = _init_repo(tmp_path / "repo")
    m0 = _git(repo, "rev-parse", "HEAD")
    base, main = _conflicting_candidate(repo, m0)
    loops_root = tmp_path / "loops"
    (loops_root / "state").mkdir(parents=True, exist_ok=True)
    ident = f"sha256:{'7' * 64}"
    (loops_root / "parked").mkdir(parents=True, exist_ok=True)
    (loops_root / "parked" / f"sha256_{'7' * 64}.json").write_text(
        json.dumps({"id": ident, "kind": "candidate", "reason": "r",
                    "at": "2026-08-11T00:00:00Z", "needs": "human"}), encoding="utf-8")
    (loops_root / "state" / "assembly_strikes.json").write_text(json.dumps({
        "version": 1,
        "candidates": {ident: {"strikes": True, "bases": poison, "parked": True,
                               "event_written": False, "last_reason": 42}},
    }), encoding="utf-8")

    trains, excluded = _assemble(repo, [_cand("7" * 64, "cand-x", base)], main,
                                 tmp_path, root=loops_root)

    assert trains == []
    assert any("PARKED" in e["why"] for e in excluded), excluded
    healed = [e for e in _ledger_events(loops_root) if e["event"] == "parked"]
    assert len(healed) == 1, "the owed event is healed once, from a poisoned record"
    assert healed[0]["detail"]["bases"] == []
    assert healed[0]["detail"]["last_conflict"] == ""


def test_a_synthetic_mechanical_gate_verdict_is_not_an_independent_review(tmp_path):
    """Cross-commit interaction found at rebase (tiered-verify, 1bb016c3f).

    `load_candidates` now appends `{"lineage": "mechanical-gate"}` to a LOW-risk
    candidate to record that a signed gate PASS stood in for the cross-lineage
    LLM verdict. That label sits outside KNOWN_LINEAGES on purpose. The assembly
    ranking must honour the same vocabulary the approval gate does, or every
    LOW-tier candidate quietly outranks genuinely twice-reviewed work for the
    front of train #1.
    """
    from bridge.train_assembler import _cross_lineage_count

    def cand(verdicts):
        art = {"id": "sha256:z", "producer": {"lineage": "anthropic"},
               "verdicts": verdicts}
        return Candidate("sha256:z", Path("x"), art, branch="b", base_sha="0" * 40)

    assert _cross_lineage_count(cand([{"lineage": "mechanical-gate",
                                       "by": "merge-gate"}])) == 0
    assert _cross_lineage_count(cand([{"lineage": "openai", "model": "m"}])) == 1
    assert _cross_lineage_count(cand([{"lineage": "Anthropic"}])) == 0  # producer's own
    assert _cross_lineage_count(cand([{"lineage": "not-a-lab"}])) == 0
    assert _cross_lineage_count(cand([{"lineage": "openai"},
                                      {"lineage": "mechanical-gate"},
                                      {"lineage": "google"}])) == 2
    assert _cross_lineage_count(cand([])) == 0
    assert _cross_lineage_count(cand("not-a-list")) == 0


# ======================================================== ROUND-4 REVIEW FIXES
# The daemon's crash-recovery invariants are the point of this system, so these
# test the seams a landing has AFTER main has already moved, and the two places
# an "unreadable" answer was being read as a favourable one.


def _land_receipt_body(loop: GateLoop, train: Train) -> dict:
    verdict = GateVerdict("pass", 0, "", "passed", None, "", 1.0)
    rel = loop._write_land_receipt(train, verdict, train.tip)
    return json.loads((loop.root / rel).read_text())


# --- B3: the <=cap contract holds in the SERIALIZED shapes, not just in RAM ---


def test_an_unchained_train_serializes_exactly_the_pre_chain_shapes(tmp_path):
    """"<=cap behaves exactly as before" has to be true of the BYTES.

    The previous compat test compared in-memory dataclass fields, which is
    vacuous for this claim: the receipt and manifest are what other tools read
    back, and both had unconditionally gained chain keys.
    """
    loop = _make_loop(tmp_path / "loops", _init_repo(tmp_path / "repo"),
                      _fake_offload(tmp_path))
    member = {"id": f"sha256:{'1' * 64}", "branch": "cand/1",
              "base": "a" * 40, "paths": ["x.txt"]}
    unchained = Train(branch="train/gl-compat", base="a" * 40, tip="b" * 40,
                      members=[member], paths=["x.txt"])

    # The exact pre-chain key sets (origin/main: gate_loop._write_land_receipt
    # and train_assembler.Train.as_manifest).
    assert sorted(_land_receipt_body(loop, unchained)) == [
        "at", "base", "by", "gate", "kind", "members", "merge_sha", "paths",
        "tip", "train"]
    assert unchained.as_manifest() == {
        "branch": "train/gl-compat", "base": "a" * 40, "tip": "b" * 40,
        "members": [member], "paths": ["x.txt"]}
    assert unchained.chained is False

    # A CHAINED train is the only one that gains provenance — and it must, or an
    # auditor cannot tell which tree the gate graded from the receipt alone.
    chained = Train(branch="train/gl-chained", base="b" * 40, tip="c" * 40,
                    members=[member], paths=["x.txt"], parent="train/gl-compat",
                    chain_root="a" * 40, chain_index=1)
    body = _land_receipt_body(loop, chained)
    assert body["chain"] == {"root": "a" * 40, "parent": "train/gl-compat", "index": 1}
    assert chained.as_manifest()["chain_index"] == 1
    assert chained.chained is True


def test_a_single_train_landing_writes_the_pre_chain_receipt_end_to_end(tmp_path):
    """The same claim through a real tick, not a hand-built Train."""
    repo = _init_repo(tmp_path / "repo")
    m0 = _git(repo, "rev-parse", "HEAD")
    _commit_on(repo, "cand-s", m0, "s.txt", "SSS\n")
    loops_root = tmp_path / "loops"
    _write_candidate(loops_root, "5" * 64, "cand-s", m0, ["s.txt"])
    offload = _fake_offload(tmp_path)
    os.environ["FAKE_GATE_RC"] = "0"
    os.environ.pop("FAKE_GATE_SLUG", None)
    try:
        loop = _make_loop(loops_root, repo, offload)
        train = loop.run_once()[0].train
        _wait_done(gate_state_path(loops_root, train))
        out = _make_loop(loops_root, repo, offload).run_once()
    finally:
        os.environ.pop("FAKE_GATE_RC", None)
    assert out[0].action == "landed", out
    receipts = list((loops_root / "receipts").rglob("land-*.json"))
    assert len(receipts) == 1
    assert "chain" not in json.loads(receipts[0].read_text())


# --- B1a: a landing interrupted after the push is completed, never re-landed --


def _interrupted_landing(tmp_path, monkeypatch, *, members=21):
    """Land a chain prefix, then kill the bookkeeping at the first receipt."""
    repo, m0, loops_root, chain = _chain_fixture(tmp_path, monkeypatch, members=members)
    _gate_result(loops_root, chain[0], rc=0)
    _gate_result(loops_root, chain[1], rc=0)
    _gate_result(loops_root, chain[2], rc=2, slug="secrets")

    _two_fake_twins(monkeypatch)
    loop = _make_loop(loops_root, repo, _fake_offload(tmp_path), allow_remote_gate=True)

    def enospc(*_a, **_kw):
        raise OSError(28, "No space left on device")

    loop._write_land_receipt = enospc  # type: ignore[assignment]
    out = loop.run_once()
    return repo, m0, loops_root, chain, loop, out


def test_a_landing_interrupted_after_the_push_is_completed_next_tick(
        tmp_path, monkeypatch):
    """main advanced, every post-push write failed, the daemon restarts.

    The members were CHERRY-PICKED, so their original SHAs are not ancestors of
    main and `_reconcile_already_merged` cannot see them. Without the write-ahead
    intent they come back eligible and land a SECOND time.
    """
    repo, m0, loops_root, chain, loop, out = _interrupted_landing(tmp_path, monkeypatch)

    # The push happened: main IS advanced, and nothing was terminalised.
    assert _git(repo, "rev-parse", "main") == chain[1].tip
    assert [o.action for o in out].count("landed") == 2, [o.action for o in out]
    assert "records incomplete" in " ".join(o.detail for o in out)
    assert not [e for e in _ledger_events(loops_root) if e["event"] == "merged"]
    intent = loops_root / "state" / "landing-intent.json"
    assert intent.exists(), "the intent must survive to describe what landed"

    # The pre-existing reconciliation genuinely cannot see this landing …
    restart = _make_loop(loops_root, repo, _fake_offload(tmp_path))
    cands = restart.load_candidates(set())
    assert restart._reconcile_already_merged(cands, chain[1].tip), \
        "cherry-picked members are not ancestors of main — this is why the intent exists"

    # … so the NEXT TICK completes the bookkeeping from the intent.
    _two_fake_twins(monkeypatch)
    loop2 = _make_loop(loops_root, repo, _fake_offload(tmp_path), allow_remote_gate=True)
    loop2.dispatch_gate = lambda train, *, allow_remote, twin=None: None  # type: ignore[assignment]
    loop2.run_once()

    merged = {e["id"] for e in _ledger_events(loops_root) if e["event"] == "merged"}
    landed_ids = {m["id"] for t in chain[:2] for m in t.members}
    assert merged == landed_ids, "every member on main must end up terminal exactly once"
    assert all(e["detail"]["merge_sha"] == chain[1].tip
               for e in _ledger_events(loops_root) if e["event"] == "merged")
    assert not intent.exists(), "a completed recovery clears its intent"
    assert _git(repo, "rev-parse", "main") == chain[1].tip, "nothing landed twice"
    assert any("RECOVERING interrupted landing" in line for line in loop2.lines), loop2.lines


def test_the_recovery_is_idempotent_and_never_double_terminalises(tmp_path, monkeypatch):
    repo, m0, loops_root, chain, loop, out = _interrupted_landing(tmp_path, monkeypatch)
    _two_fake_twins(monkeypatch)
    for _ in range(3):                      # replay the recovery tick repeatedly
        again = _make_loop(loops_root, repo, _fake_offload(tmp_path),
                           allow_remote_gate=True)
        again.dispatch_gate = lambda train, *, allow_remote, twin=None: None  # type: ignore[assignment]
        again.run_once()
    merged = [e for e in _ledger_events(loops_root) if e["event"] == "merged"]
    assert len(merged) == len({e["id"] for e in merged}), \
        "exactly_one_terminal_event: a member may never receive a second `merged`"


def test_an_intent_whose_landing_never_reached_main_is_discarded(tmp_path, monkeypatch):
    """The push was rolled back: there is nothing on main to account for."""
    repo, m0, loops_root, chain = _chain_fixture(tmp_path, monkeypatch)
    intent = loops_root / "state" / "landing-intent.json"
    intent.write_text(json.dumps({
        "kind": "landing-intent", "merge_sha": "d" * 40, "root": m0,
        "trains": [{"branch": chain[0].branch, "base": chain[0].base,
                    "tip": chain[0].tip, "members": chain[0].members,
                    "paths": chain[0].paths, "parent": None, "chain_root": m0,
                    "chain_index": 0,
                    "verdict": {"result": "pass", "exit_code": 0, "slug": "",
                                "reason": "", "receipt": None, "stdout_tail": "",
                                "duration_s": 1.0}}],
    }), encoding="utf-8")
    _two_fake_twins(monkeypatch)
    loop = _make_loop(loops_root, repo, _fake_offload(tmp_path), allow_remote_gate=True)
    loop.dispatch_gate = lambda train, *, allow_remote, twin=None: None  # type: ignore[assignment]
    loop.run_once()
    assert not intent.exists()
    assert not [e for e in _ledger_events(loops_root) if e["event"] == "merged"]


def test_an_unwritable_landing_intent_refuses_to_advance_main(tmp_path, monkeypatch):
    """Fail-closed ordering: no push is issued that a restart cannot account for."""
    repo, m0, loops_root, chain = _chain_fixture(tmp_path, monkeypatch)
    _gate_result(loops_root, chain[0], rc=0)
    _two_fake_twins(monkeypatch)
    loop = _make_loop(loops_root, repo, _fake_offload(tmp_path), allow_remote_gate=True)
    loop.dispatch_gate = lambda train, *, allow_remote, twin=None: None  # type: ignore[assignment]
    landed: list[str] = []
    real_land = loop.land_train
    loop.land_train = lambda t: (landed.append(t.branch), real_land(t))[1]  # type: ignore[assignment]
    real_write = loop._write_json_atomic

    def refuse(path, obj):
        if path.name == "landing-intent.json":
            raise OSError(28, "No space left on device")
        return real_write(path, obj)

    loop._write_json_atomic = refuse  # type: ignore[assignment]
    out = loop.run_once()

    assert landed == [], "land_train must never run without a durable intent"
    assert _git(repo, "rev-parse", "main") == m0
    assert any(o.action == "skipped" and "landing intent" in o.detail for o in out), out
    assert any("REFUSING to land" in a for a in loop.alerts), loop.alerts


def test_a_corrupt_landing_intent_is_quarantined_and_alerted(tmp_path, monkeypatch):
    repo, m0, loops_root, chain = _chain_fixture(tmp_path, monkeypatch)
    intent = loops_root / "state" / "landing-intent.json"
    intent.write_text("{not json", encoding="utf-8")
    _two_fake_twins(monkeypatch)
    loop = _make_loop(loops_root, repo, _fake_offload(tmp_path), allow_remote_gate=True)
    loop.dispatch_gate = lambda train, *, allow_remote, twin=None: None  # type: ignore[assignment]
    loop.run_once()
    assert not intent.exists()
    assert list((loops_root / "state").glob("landing-intent.corrupt-*.json"))
    assert any("UNREADABLE landing intent" in a for a in loop.alerts), loop.alerts


# --- B1b: a REAL writer exception cannot truncate the rest of the prefix ------


def test_a_real_ledger_append_error_does_not_abandon_a_later_trains_records(tmp_path):
    """LedgerAppendError is an OSError, not a ValueError — the closure handler
    caught only ValueError, so a real transport failure escaped `_emit_closures`
    and every train after it in the prefix lost its `merged` event entirely."""
    from bridge.ledger_write import LedgerAppendError

    tmp = tmp_path
    root = tmp / "loops"
    (root / "state").mkdir(parents=True)
    receipt = tmp / "gate-receipt.json"
    node = "tests/test_bound.py::test_fixed"
    receipt.write_text(json.dumps({"bound_test": [node],
                                   "bound_test_result": "green"}), encoding="utf-8")
    root_sha, one_sha, two_sha = "a" * 40, "b" * 40, "c" * 40
    one_id, two_id = f"sha256:{'1' * 64}", f"sha256:{'2' * 64}"
    t0 = Train("train/one", root_sha, one_sha,
               members=[{"id": one_id, "branch": "cand/one", "base": root_sha,
                         "paths": ["one.txt"]}], paths=["one.txt"])
    t1 = Train("train/two", one_sha, two_sha,
               members=[{"id": two_id, "branch": "cand/two", "base": root_sha,
                         "paths": ["two.txt"]}], paths=["two.txt"],
               parent=t0.branch, chain_root=root_sha, chain_index=1)
    verdict = GateVerdict("pass", 0, "", "passed", receipt, "", 1.0)
    loop = GateLoop(root, tmp, remote=None, push=False)
    loop.bindings[one_id] = [gl.Binding(one_id, f"sha256:{'f' * 64}", node)]
    loop.land_train = lambda _t: (two_sha, "landed")  # type: ignore[assignment]
    loop._write_land_receipt = lambda *_a, **_kw: "receipt.json"  # type: ignore[assignment]
    events: list[dict] = []

    def writer(event: dict) -> None:
        if event.get("event") == "closed":
            raise LedgerAppendError("simulated zero-byte write refusal",
                                    phase="write", bytes_written=0)
        events.append(event)

    loop._append_ledger = writer  # type: ignore[assignment]
    loop._terminal_ids = lambda: set()  # type: ignore[assignment]

    out = loop._land_prefix([(t0, verdict), (t1, verdict)])

    assert [o.action for o in out] == ["landed", "landed"], out
    assert {e["id"] for e in events if e["event"] == "merged"} == {one_id, two_id}
    assert not [e for e in events if e["event"] == "closed"], "the closure is withheld"


# --- B2: an unreadable parked/ is never read as "nothing is parked" ----------


def test_an_unreadable_parked_directory_refuses_the_tick(tmp_path, monkeypatch):
    from bridge import train_assembler as ta

    repo = _init_repo(tmp_path / "repo")
    m0 = _git(repo, "rev-parse", "HEAD")
    _commit_on(repo, "cand-p", m0, "p.txt", "PPP\n")
    loops_root = tmp_path / "loops"
    (loops_root / "parked").mkdir(parents=True)
    ident = f"sha256:{'a' * 64}"
    (loops_root / "parked" / f"sha256_{'a' * 64}.json").write_text(
        json.dumps({"id": ident, "reason": "human park", "needs": "human"}),
        encoding="utf-8")
    real_glob = Path.glob

    def unreadable(self, pattern):
        if self.name == "parked":
            raise OSError("simulated EIO while enumerating parked/")
        return real_glob(self, pattern)

    monkeypatch.setattr(Path, "glob", unreadable)
    with pytest.raises(ta.ParkedIndexUnreadable):
        ta.parked_ids(loops_root)
    trains, excluded = _assemble(repo, [_cand("a" * 64, "cand-p", m0)], m0, tmp_path,
                                 root=loops_root)
    assert trains == [], "an unknown park set must not assemble anything"
    assert any(e.get("instrument") for e in excluded), excluded
    assert any("parked/ is UNREADABLE" in e["why"] for e in excluded), excluded


def test_an_absent_parked_directory_is_still_simply_empty(tmp_path):
    from bridge import train_assembler as ta

    repo = _init_repo(tmp_path / "repo")
    m0 = _git(repo, "rev-parse", "HEAD")
    _commit_on(repo, "cand-n", m0, "n.txt", "NNN\n")
    loops_root = tmp_path / "loops"
    (loops_root / "state").mkdir(parents=True)
    assert ta.parked_ids(loops_root) == set()
    trains, _ = _assemble(repo, [_cand("b" * 64, "cand-n", m0)], m0, tmp_path,
                          root=loops_root)
    assert len(trains) == 1, "absent is not unreadable"


def test_an_intent_for_a_real_but_unlanded_sha_is_also_discarded(tmp_path, monkeypatch):
    """The rolled-back case where the object still EXISTS (not yet collected)."""
    repo, m0, loops_root, chain = _chain_fixture(tmp_path, monkeypatch)
    orphan = _commit_on(repo, "side-branch", m0, "side.txt", "SIDE\n")
    assert orphan != _git(repo, "rev-parse", "main")
    (loops_root / "state" / "landing-intent.json").write_text(json.dumps({
        "kind": "landing-intent", "merge_sha": orphan, "root": m0, "trains": [],
    }), encoding="utf-8")
    _two_fake_twins(monkeypatch)
    loop = _make_loop(loops_root, repo, _fake_offload(tmp_path), allow_remote_gate=True)
    loop.dispatch_gate = lambda train, *, allow_remote, twin=None: None  # type: ignore[assignment]
    loop.run_once()
    assert not (loops_root / "state" / "landing-intent.json").exists()
    assert not [e for e in _ledger_events(loops_root) if e["event"] == "merged"]
    assert any("did not take effect" in line for line in loop.lines), loop.lines
