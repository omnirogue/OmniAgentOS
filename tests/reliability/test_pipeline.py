"""Tests for the improvement pipeline (§5, §5b, §6 — safety-critical core).

Acceptance backbone (§13):
  * sandbox fail ⇒ terminal 'failed', never judged (B1)
  * panel_blocked (< 3 distinct families) ⇒ panel_blocked + notification; human pull ⇒ awaiting_human
  * approve mode ⇒ queue for human; auto mode L1 + quorum ⇒ end-to-end apply
  * L4 always queues regardless of mode
  * single-flight lease held ⇒ apply deferred; overlap with monitoring ⇒ deferred
  * crash mid-applying resume BOTH directions (real tmp git repos, M2)
  * revert-conflict clean abort ⇒ failed + escalation (M2)
  * monitoring divergence (raw up, events flat) ⇒ critical alert (M1)
  * monitoring regression ⇒ auto-rollback + escalation; clean at window end ⇒ confirmed
  * apply performs NO proposal-declared shell (B1)
  * pipeline never writes autonomy_settings
"""

from __future__ import annotations

import sqlite3
import subprocess
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from omniagentos.contracts import utc_now_iso
from omniagentos.db.migrate import migrate_connection
from omniagentos.reliability.governance import DiffEntry, GovernanceConfig
from omniagentos.reliability.pipeline import (
    ImprovementPipeline,
    JudgeOutcome,
    SandboxOutcome,
    _Signals,
)
from omniagentos.reliability.store import SqliteReliabilityStore
from omniagentos.reliability.taxonomy import ChangeRisk, ImprovementStatus

# --- Test doubles


class RecordingNotifier:
    """Captures every pipeline notification call."""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    def __call__(self, *, kind, title, severity, imp_id, dedupe, payload):
        self.calls.append(
            {
                "kind": kind,
                "title": title,
                "severity": severity,
                "imp_id": imp_id,
                "dedupe": dedupe,
                "payload": payload,
            }
        )

    def kinds(self) -> list[str]:
        return [c["kind"] for c in self.calls]

    def with_kind(self, kind: str) -> list[dict]:
        return [c for c in self.calls if c["kind"] == kind]


@dataclass
class FakeSandbox:
    outcome: SandboxOutcome
    calls: int = 0

    def run(self, improvement, repo_root):
        self.calls += 1
        return self.outcome


@dataclass
class FakeJudge:
    outcome: JudgeOutcome
    calls: int = 0

    def evaluate(self, improvement, sandbox):
        self.calls += 1
        return self.outcome


class ExplodingJudge:
    """A judge that must never be called (asserts the pipeline short-circuited)."""

    def evaluate(self, improvement, sandbox):  # pragma: no cover - defensive
        raise AssertionError("judge should not be invoked")


class Clock:
    def __init__(self, start: datetime) -> None:
        self.t = start

    def __call__(self) -> datetime:
        return self.t

    def advance(self, **kw) -> None:
        self.t = self.t + timedelta(**kw)


# --- Fixtures


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(cwd), *args], capture_output=True, text=True, check=True
    ).stdout.strip()


@dataclass
class Env:
    repo: Path
    store: SqliteReliabilityStore
    notifier: RecordingNotifier
    clock: Clock
    pipeline: ImprovementPipeline


@pytest.fixture
def env(tmp_path: Path) -> Env:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@t.io")
    _git(repo, "config", "user.name", "t")
    (repo / "notes").mkdir()
    (repo / "notes" / "seed.txt").write_text("seed\n", encoding="utf-8")
    (repo / "configs").mkdir()
    (repo / "configs" / "governance.yaml").write_text("quorum: {}\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "seed")

    db_path = tmp_path / "test.db"
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    migrate_connection(conn)
    conn.close()
    store = SqliteReliabilityStore(str(db_path))

    notifier = RecordingNotifier()
    clock = Clock(datetime(2026, 7, 23, 12, 0, 0, tzinfo=UTC))
    pipeline = ImprovementPipeline(
        store,
        repo_root=repo,
        force_activation=True,  # L09/L03 gates; tests inject collaborators directly
        governance_config=GovernanceConfig(),
        notifier=notifier,
        clock=clock,
    )
    return Env(repo, store, notifier, clock, pipeline)


# --- helpers


def _make_imp(store, *, kind="fix", files=None, restart=False, extra=None):
    proposal = {
        "change_type": "files",
        "files": files or [],
        "plan": ["THIS PLAN MUST NEVER RUN"],
        "restart_required": restart,
    }
    if extra:
        proposal.update(extra)
    return store.create_improvement(origin="realtime", kind=kind, title="t", proposal_json=proposal)


def _sandbox_pass(files, declared=None, risk=1):
    entries = [
        DiffEntry(
            "A" if (f.get("action") == "create") else "M",
            f["path"],
            content=f.get("content", ""),
        )
        for f in files
    ]
    return SandboxOutcome(
        passed=True,
        diff_entries=entries,
        declared_paths=declared if declared is not None else [f["path"] for f in files],
        suggested_risk=risk,
    )


def _judge_all(verdict="approve", families=("anthropic", "openai", "xai")):
    votes = [
        {"model_family": f, "judge_agent": f"j_{f}", "verdict": verdict, "scores": {}}
        for f in families
    ]
    return JudgeOutcome(
        panel_attempt_id="pan1",
        families_seated=len(families),
        votes=votes,
        complete=len(families) >= 3,
    )


def _set_auto(store, kind, max_risk):
    conn = store._connection
    conn.execute(
        "INSERT OR REPLACE INTO autonomy_settings "
        "(id, scope_type, scope_id, mode, max_auto_risk, updated_by, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (f"aut_kind_{kind}", "kind", kind, "auto", max_risk, "test", utc_now_iso()),
    )
    conn.commit()


def _drive_to(store, imp_id, target):
    chain = ["proposed", "testing", "judging", "approved", "applying", "applied", "monitoring"]
    idx = chain.index(target)
    for a, b in zip(chain[:idx], chain[1 : idx + 1], strict=False):
        store.transition_improvement(imp_id, a, b, actor="test")


# --- Decision phase


def test_sandbox_fail_is_terminal_and_not_judged(env: Env):
    imp_id = _make_imp(env.store, files=[{"path": "notes/x.txt", "content": "y"}])
    env.pipeline.sandbox_runner = FakeSandbox(
        SandboxOutcome(passed=False, report={"why": "pytest red"})
    )
    env.pipeline.judge_panel = ExplodingJudge()

    result = env.pipeline.sandbox_and_judge(imp_id)
    assert result.status == ImprovementStatus.FAILED.value
    assert env.store.get_improvement(imp_id).status == ImprovementStatus.FAILED.value
    assert "escalation" in env.notifier.kinds()


def test_panel_blocked_then_human_pull(env: Env):
    imp_id = _make_imp(env.store, files=[{"path": "notes/x.txt", "content": "y"}])
    env.pipeline.sandbox_runner = FakeSandbox(
        _sandbox_pass([{"path": "notes/x.txt", "content": "y"}])
    )
    # Only 2 distinct families seated ⇒ fail-closed.
    env.pipeline.judge_panel = FakeJudge(
        JudgeOutcome("pan1", families_seated=2, votes=[], complete=False)
    )

    result = env.pipeline.sandbox_and_judge(imp_id)
    assert result.status == ImprovementStatus.PANEL_BLOCKED.value
    assert "approval" in env.notifier.kinds()

    env.pipeline.human_pull_panel_blocked(imp_id, decided_by="owner")
    assert env.store.get_improvement(imp_id).status == ImprovementStatus.AWAITING_HUMAN.value


def test_approve_mode_queues_for_human(env: Env):
    # Global default is approve/0 (seeded) — even a clean L1 3/3 queues.
    imp_id = _make_imp(env.store, files=[{"path": "notes/x.txt", "content": "y"}])
    env.pipeline.sandbox_runner = FakeSandbox(
        _sandbox_pass([{"path": "notes/x.txt", "content": "y"}])
    )
    env.pipeline.judge_panel = FakeJudge(_judge_all("approve"))

    result = env.pipeline.sandbox_and_judge(imp_id)
    assert result.status == ImprovementStatus.AWAITING_HUMAN.value
    assert env.store.get_improvement(imp_id).risk_level == ChangeRisk.L1.value
    assert "approval" in env.notifier.kinds()


def test_auto_apply_l1_end_to_end(env: Env):
    _set_auto(env.store, "fix", max_risk=1)
    files = [{"path": "notes/change.txt", "content": "improved\n"}]
    imp_id = _make_imp(env.store, files=files)
    env.pipeline.sandbox_runner = FakeSandbox(_sandbox_pass(files))
    env.pipeline.judge_panel = FakeJudge(_judge_all("approve"))

    env.pipeline.sandbox_and_judge(imp_id)
    imp = env.store.get_improvement(imp_id)

    assert imp.status == ImprovementStatus.MONITORING.value
    assert imp.applied_sha  # authoritative SHA recorded
    assert imp.rollback_point_id  # rollback point created first
    # File written + committed in the main tree.
    assert (env.repo / "notes" / "change.txt").read_text() == "improved\n"
    # Idempotent tag exists.
    assert _git(env.repo, "tag", "-l", f"imp/{imp_id}") == f"imp/{imp_id}"
    # Rollback point row exists with the pre-apply SHA.
    rp = env.store._connection.execute(
        "SELECT * FROM rollback_points WHERE improvement_id = ?", (imp_id,)
    ).fetchone()
    assert rp is not None and rp["git_ref"]
    assert "done" in env.notifier.kinds()


def test_l4_always_queues_even_in_auto_mode(env: Env):
    _set_auto(env.store, "config", max_risk=2)  # highest allowed
    files = [{"path": "configs/governance.yaml", "content": "quorum: {allow: true}\n"}]
    imp_id = _make_imp(env.store, kind="config", files=files)
    env.pipeline.sandbox_runner = FakeSandbox(_sandbox_pass(files, risk=1))
    env.pipeline.judge_panel = FakeJudge(_judge_all("approve"))

    result = env.pipeline.sandbox_and_judge(imp_id)
    assert result.risk_level == ChangeRisk.L4.value
    assert result.status == ImprovementStatus.AWAITING_HUMAN.value  # never auto at L4


# --- Single-flight + overlap (M2)


def test_single_flight_defers_when_lease_held(env: Env):
    imp_id = _make_imp(env.store, files=[{"path": "notes/x.txt", "content": "y"}])
    _drive_to(env.store, imp_id, "approved")
    # A different owner holds the apply lease.
    env.store.acquire_lease("reliability:apply", owner="other-worker", duration_seconds=600)

    result = env.pipeline.apply(imp_id)
    assert result.deferred and result.reason == "apply_lease_held"
    assert env.store.get_improvement(imp_id).status == ImprovementStatus.APPROVED.value


def test_overlap_with_monitoring_defers(env: Env):
    # A improvement in monitoring touching notes/shared.txt.
    a_id = _make_imp(env.store)
    conn = env.store._connection
    import json as _json

    conn.execute(
        "UPDATE improvements SET status='monitoring', sandbox_json=? WHERE id=?",
        (_json.dumps({"diff_paths": ["notes/shared.txt"]}), a_id),
    )
    conn.commit()

    b_id = _make_imp(env.store)
    conn.execute(
        "UPDATE improvements SET sandbox_json=? WHERE id=?",
        (_json.dumps({"diff_paths": ["notes/shared.txt"]}), b_id),
    )
    conn.commit()
    _drive_to(env.store, b_id, "approved")

    result = env.pipeline.apply(b_id)
    assert result.deferred and result.reason == "overlap_with_monitoring"
    assert env.store.get_improvement(b_id).status == ImprovementStatus.APPROVED.value


# --- Crash recovery (M2), both directions


def test_crash_recovery_advance_when_commit_landed(env: Env):
    imp_id = _make_imp(env.store, files=[{"path": "notes/change.txt", "content": "z"}])
    _drive_to(env.store, imp_id, "applying")
    pre_sha = _git(env.repo, "rev-parse", "HEAD")

    # Simulate: files written + committed with the trailer, but crashed before recording.
    (env.repo / "notes" / "change.txt").write_text("z", encoding="utf-8")
    _git(env.repo, "add", "-A")
    _git(
        env.repo,
        "commit",
        "-qm",
        f"reliability: apply {imp_id}\n\nImp-Id: {imp_id} Attempt: 1",
    )
    landed = _git(env.repo, "rev-parse", "HEAD")
    env.pipeline._journal_put(imp_id, {"phase": "committed", "pre_sha": pre_sha, "attempt": 1})

    result = env.pipeline.reconcile_apply(imp_id)
    assert result.applied and result.applied_sha == landed
    imp = env.store.get_improvement(imp_id)
    assert imp.status == ImprovementStatus.MONITORING.value
    assert imp.applied_sha == landed


def test_crash_recovery_retreat_when_commit_missing(env: Env):
    imp_id = _make_imp(env.store, files=[{"path": "notes/change.txt", "content": "z"}])
    _drive_to(env.store, imp_id, "applying")
    pre_sha = _git(env.repo, "rev-parse", "HEAD")

    # Simulate a crash after files_written but BEFORE commit — dirty worktree, no trailer.
    (env.repo / "notes" / "change.txt").write_text("half-written", encoding="utf-8")
    (env.repo / "notes" / "stray.txt").write_text("stray", encoding="utf-8")
    env.pipeline._journal_put(imp_id, {"phase": "files_written", "pre_sha": pre_sha, "attempt": 1})

    result = env.pipeline.reconcile_apply(imp_id)
    assert result.reason == "recovered_retreat"
    # Worktree restored clean from pre_sha.
    assert _git(env.repo, "status", "--porcelain") == ""
    assert not (env.repo / "notes" / "stray.txt").exists()
    assert not (env.repo / "notes" / "change.txt").exists()
    assert env.store.get_improvement(imp_id).status == ImprovementStatus.APPROVED.value


# --- Rollback conflict clean-abort (M2)


def test_rollback_revert_conflict_clean_abort(env: Env):
    # base A -> applied B -> later C; reverting B now conflicts (file is C, not B).
    f = env.repo / "notes" / "f.txt"
    f.write_text("A\n", encoding="utf-8")
    _git(env.repo, "add", "-A")
    _git(env.repo, "commit", "-qm", "A")

    f.write_text("B\n", encoding="utf-8")
    _git(env.repo, "add", "-A")
    _git(env.repo, "commit", "-qm", "applied B")
    applied_sha = _git(env.repo, "rev-parse", "HEAD")

    f.write_text("C\n", encoding="utf-8")
    _git(env.repo, "add", "-A")
    _git(env.repo, "commit", "-qm", "C")

    imp_id = _make_imp(env.store)
    conn = env.store._connection
    conn.execute(
        "UPDATE improvements SET status='monitoring', applied_sha=?, risk_level=2 WHERE id=?",
        (applied_sha, imp_id),
    )
    conn.commit()

    result = env.pipeline.rollback(imp_id, reason="test")
    assert result.conflict and not result.success
    # Clean abort: no partial revert, worktree clean, file still C.
    assert _git(env.repo, "status", "--porcelain") == ""
    assert f.read_text() == "C\n"
    assert env.store.get_improvement(imp_id).status == ImprovementStatus.FAILED.value
    assert any(c["severity"] == "critical" for c in env.notifier.with_kind("escalation"))


# --- Monitoring (M1)


def _auto_apply_to_monitoring(env: Env, path="notes/change.txt"):
    _set_auto(env.store, "fix", max_risk=1)
    files = [{"path": path, "content": "v1\n"}]
    imp_id = _make_imp(env.store, files=files)
    env.pipeline.sandbox_runner = FakeSandbox(_sandbox_pass(files))
    env.pipeline.judge_panel = FakeJudge(_judge_all("approve"))
    env.pipeline.sandbox_and_judge(imp_id)
    assert env.store.get_improvement(imp_id).status == ImprovementStatus.MONITORING.value
    return imp_id


def test_monitor_divergence_alert(env: Env):
    imp_id = _auto_apply_to_monitoring(env)
    # Baseline: 100 raw failures, 5 events. After: raw up past divergence floor but
    # under regression floor, events flat ⇒ divergence alert, no rollback.
    env.pipeline._monitor_baseline_put(imp_id, {"events": 5, "raw": 100})
    env.pipeline._measure_window = lambda start, end: _Signals(events=0, raw=112)

    status = env.pipeline.monitor_tick(imp_id)
    assert status == ImprovementStatus.MONITORING.value
    alerts = env.notifier.with_kind("alert")
    assert alerts and alerts[0]["severity"] == "critical"


def test_monitor_regression_triggers_rollback(env: Env):
    imp_id = _auto_apply_to_monitoring(env)
    # Baseline 0/0; a burst of raw failures after ⇒ regression ⇒ auto-rollback.
    env.pipeline._monitor_baseline_put(imp_id, {"events": 0, "raw": 0})
    env.pipeline._measure_window = lambda start, end: _Signals(events=0, raw=10)

    env.pipeline.monitor_tick(imp_id)
    imp = env.store.get_improvement(imp_id)
    assert imp.status == ImprovementStatus.ROLLED_BACK.value
    assert any(c["severity"] == "critical" for c in env.notifier.with_kind("escalation"))


def test_monitor_confirms_when_clean_at_window_end(env: Env):
    imp_id = _auto_apply_to_monitoring(env)
    env.pipeline._monitor_baseline_put(imp_id, {"events": 0, "raw": 0})
    env.pipeline._measure_window = lambda start, end: _Signals(events=0, raw=0)
    # Advance the simulated clock past the observation window.
    env.clock.advance(hours=25)

    status = env.pipeline.monitor_tick(imp_id)
    assert status == ImprovementStatus.CONFIRMED.value
    assert env.store.get_improvement(imp_id).status == ImprovementStatus.CONFIRMED.value
    assert "done" in env.notifier.kinds()


def test_monitor_reads_raw_runs_signal_from_db(env: Env):
    # Exercise the real detector-independent SQL path against runs/steps.
    conn = env.store._connection
    now = utc_now_iso()
    conn.execute(
        "INSERT INTO tasks (id, title, created_at, updated_at) VALUES (?,?,?,?)",
        ("tsk_1", "t", now, now),
    )
    conn.execute(
        "INSERT INTO runs (id, task_id, harness, trace_id, state, error, "
        "queued_at, finished_at, created_at, updated_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?)",
        ("run_1", "tsk_1", "cli-claude", "tr", "failed", "boom", now, now, now, now),
    )
    conn.commit()

    sig = env.pipeline._measure_window("2000-01-01T00:00:00Z", "2100-01-01T00:00:00Z")
    assert sig.raw >= 1


# --- Injection / capability invariants (B1, §6)


def test_apply_never_executes_proposal_plan(env: Env):
    _set_auto(env.store, "fix", max_risk=1)
    files = [
        {"path": "tools/hook.sh", "action": "create", "content": "#!/bin/sh\ntouch pwned\n"},
    ]
    # plan[] contains a shell command that would create MARKER if ever executed.
    imp_id = _make_imp(env.store, files=files, extra={"plan": ["touch MARKER", "rm -rf x"]})
    env.pipeline.sandbox_runner = FakeSandbox(_sandbox_pass(files))
    env.pipeline.judge_panel = FakeJudge(_judge_all("approve"))

    env.pipeline.sandbox_and_judge(imp_id)

    # The hook is written as inert file CONTENT only.
    assert (env.repo / "tools" / "hook.sh").read_text() == "#!/bin/sh\ntouch pwned\n"
    # No proposal-declared command ever ran.
    assert not (env.repo / "MARKER").exists()
    assert not (env.repo / "pwned").exists()


def test_pipeline_never_writes_autonomy_settings(env: Env):
    before = env.store.list_autonomy_settings()
    _auto_apply_to_monitoring(env)
    after = env.store.list_autonomy_settings()
    # The auto scope we seeded in the helper is the only change; the pipeline itself
    # added nothing and mutated nothing during the full flow.
    assert {s.id for s in after} == {s.id for s in before} | {"aut_kind_fix"}
    # And the global row is untouched (approve/0).
    glob = env.store.get_autonomy_setting("global", "")
    assert glob.mode == "approve" and glob.max_auto_risk == 0


def test_reliability_log_chain_extends_across_transitions(env: Env):
    _auto_apply_to_monitoring(env)
    rows = env.store._connection.execute(
        "SELECT prev_hash, hash FROM reliability_log ORDER BY id"
    ).fetchall()
    assert len(rows) >= 5  # proposed→testing→judging→approved→applying→applied→monitoring
    for prev, cur in zip(rows, rows[1:], strict=False):
        assert cur["prev_hash"] == prev["hash"]
