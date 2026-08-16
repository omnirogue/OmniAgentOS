"""H-07: CAS/lease-guarded re-driver + durable worker exit evidence.

Covers restart re-drive, duplicate-reconciler exactly-once, deferred-then-release
retry, spawn-failure recovery, exit-evidence durability, and — critically — that the
REAL scheduled/recovery path (``watch`` → ``run_recovery_cycle``) re-drives a stranded
approval without any manual command. Uses temp SQLite + fake subprocesses — no live
providers, no live services.
"""

from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import pytest

from omniagentos.db.migrate import migrate_connection
from omniagentos.reliability.audit import watch
from omniagentos.reliability.pipeline import ApplyResult
from omniagentos.reliability.recovery import run_recovery_cycle
from omniagentos.reliability.store import SqliteReliabilityStore
from omniagentos.reliability.taxonomy import ImprovementStatus
from omniagentos.reliability.worker_drive import (
    DEFAULT_MAX_ATTEMPTS,
    OUTCOME_APPLIED,
    OUTCOME_DEFERRED,
    OUTCOME_ESCALATED,
    OUTCOME_EXITED,
    OUTCOME_SPAWN_FAILED,
    OUTCOME_SPAWNED,
    REDRIVE_LEASE_KEY,
    WORKER_MODULE,
    capture_exit_evidence,
    get_worker_state,
    make_worker_spawn_fn,
    product_root,
    put_worker_state,
    record_spawn_attempt,
    record_spawn_failure,
    record_spawn_success,
    record_worker_exit,
    redrive_stranded_approvals,
    summarize_log,
)


@pytest.fixture()
def store(tmp_path: Path) -> SqliteReliabilityStore:
    db = tmp_path / "rel.sqlite3"
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    migrate_connection(conn)
    conn.close()
    return SqliteReliabilityStore(str(db))


def _approved(store: SqliteReliabilityStore, *, title: str = "fix") -> str:
    imp_id = store.create_improvement("audit", "fix", title)
    store.transition_improvement(
        imp_id, ImprovementStatus.PROPOSED.value, ImprovementStatus.APPROVED.value, actor="test"
    )
    # Jump straight to approved for redrive tests (skip intermediate CAS path).
    # create starts at proposed; single hop is only valid if transitions allow it.
    # If intermediate states are required, drive through them.
    return imp_id


def _drive_to_approved(store: SqliteReliabilityStore, title: str = "fix") -> str:
    """Create an improvement already in approved status (direct SQL for test speed)."""
    imp_id = store.create_improvement("audit", "fix", title)
    now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    store._connection.execute(
        "UPDATE improvements SET status=?, version=version+1, updated_at=?, "
        "stage_started_at=? WHERE id=?",
        (ImprovementStatus.APPROVED.value, now, now, imp_id),
    )
    store._connection.commit()
    return imp_id


@dataclass
class _ApplyBox:
    results: list[ApplyResult]
    calls: list[str]

    def __call__(self, imp_id: str) -> ApplyResult:
        self.calls.append(imp_id)
        if not self.results:
            return ApplyResult(applied=True, reason="default")
        return self.results.pop(0)


class _Clock:
    def __init__(self, start: datetime) -> None:
        self.t = start

    def __call__(self) -> datetime:
        return self.t

    def advance(self, seconds: int) -> None:
        self.t = self.t + timedelta(seconds=seconds)


def test_restart_redrives_stranded_approved_after_grace(store: SqliteReliabilityStore) -> None:
    """After restart, an approved row past grace is re-driven exactly once (apply path)."""
    imp_id = _drive_to_approved(store)
    put_worker_state(
        store,
        imp_id,
        command="apply",
        spawn_attempts=1,
        last_spawn_at=(datetime.now(UTC) - timedelta(seconds=120)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        outcome="spawned",
        running=False,
    )
    box = _ApplyBox([ApplyResult(applied=True, applied_sha="abc", reason="ok")], [])
    summary = redrive_stranded_approvals(
        store,
        apply_fn=box,
        grace_seconds=30,
        max_attempts=3,
        owner="t1",
    )
    assert summary["redriven"] == 1
    assert summary["applied"] == 1
    assert box.calls == [imp_id]
    state = get_worker_state(store, imp_id)
    assert state["outcome"] == OUTCOME_APPLIED
    assert state.get("applied_sha") == "abc"


def test_duplicate_reconciler_exactly_once(store: SqliteReliabilityStore) -> None:
    """Two concurrent reconcilers: lease ensures only one redrives."""
    imp_id = _drive_to_approved(store)
    put_worker_state(
        store,
        imp_id,
        last_spawn_at=(datetime.now(UTC) - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        spawn_attempts=1,
        running=False,
    )
    # Hold the redrive lease as another owner.
    store.acquire_lease(REDRIVE_LEASE_KEY, owner="other", duration_seconds=600)
    box = _ApplyBox([ApplyResult(applied=True)], [])
    summary = redrive_stranded_approvals(
        store, apply_fn=box, grace_seconds=0, max_attempts=3, owner="me"
    )
    assert summary["lease_held"] is True
    assert summary["redriven"] == 0
    assert box.calls == []


def test_deferred_then_release_retry(store: SqliteReliabilityStore) -> None:
    """apply_lease_held deferral stays approved; next redrive after release applies."""
    imp_id = _drive_to_approved(store)
    put_worker_state(
        store,
        imp_id,
        last_spawn_at=(datetime.now(UTC) - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        spawn_attempts=1,
        running=False,
    )
    box = _ApplyBox(
        [
            ApplyResult(deferred=True, reason="apply_lease_held"),
            ApplyResult(applied=True, applied_sha="sha2", reason="ok"),
        ],
        [],
    )
    s1 = redrive_stranded_approvals(
        store, apply_fn=box, grace_seconds=0, max_attempts=5, owner="r1"
    )
    assert s1["deferred"] == 1
    assert store.get_improvement(imp_id).status == ImprovementStatus.APPROVED.value
    state = get_worker_state(store, imp_id)
    assert state["outcome"] == OUTCOME_DEFERRED
    assert state.get("last_deferred_reason") == "apply_lease_held"

    s2 = redrive_stranded_approvals(
        store, apply_fn=box, grace_seconds=0, max_attempts=5, owner="r2"
    )
    assert s2["applied"] == 1
    assert get_worker_state(store, imp_id)["outcome"] == OUTCOME_APPLIED


def test_spawn_failure_recovery_records_durable_state(
    store: SqliteReliabilityStore, tmp_path: Path
) -> None:
    """Route-level spawn failure leaves durable evidence so redrive can recover."""
    imp_id = _drive_to_approved(store)
    log = tmp_path / "spawn.log"
    log.write_text("spawn planned\n", encoding="utf-8")
    record_spawn_attempt(
        store,
        imp_id,
        command="apply",
        python="/no/such/python",
        cwd=str(tmp_path),
        log_path=str(log),
        argv=["/no/such/python", "-m", "omniagentos.reliability", "apply"],
    )
    record_spawn_failure(store, imp_id, error="No such file or directory", log_path=str(log))
    state = get_worker_state(store, imp_id)
    assert state["outcome"] == OUTCOME_SPAWN_FAILED
    assert state["spawn_attempts"] == 1
    assert "No such file" in (state.get("last_spawn_error") or "")
    imp = store.get_improvement(imp_id)
    assert imp.status == ImprovementStatus.APPROVED.value
    err = json.loads(
        imp.last_error_json
        if isinstance(imp.last_error_json, str)
        else json.dumps(imp.last_error_json)
    )
    assert err["kind"] == "spawn_failed"

    # Redrive after grace re-spawns (or applies inline).
    box = _ApplyBox([ApplyResult(applied=True, applied_sha="recovered")], [])
    # Force past grace via last_spawn_at already set inside record_spawn_attempt (now).
    put_worker_state(
        store,
        imp_id,
        last_spawn_at=(datetime.now(UTC) - timedelta(minutes=5)).strftime("%Y-%m-%dT%H:%M:%SZ"),
    )
    summary = redrive_stranded_approvals(
        store, apply_fn=box, grace_seconds=0, max_attempts=3, owner="recover"
    )
    assert summary["applied"] == 1
    assert get_worker_state(store, imp_id)["outcome"] == OUTCOME_APPLIED


def test_exit_evidence_captures_log_summary_and_exit_code(
    store: SqliteReliabilityStore, tmp_path: Path
) -> None:
    """Worker exit records durable exit_code + bounded stdout/stderr summary."""
    imp_id = _drive_to_approved(store)
    log = tmp_path / "worker.log"
    body = "line-start\n" + ("x" * 3000) + "\nTRACEBACK: boom\n"
    log.write_text(body, encoding="utf-8")
    state = record_worker_exit(
        store,
        imp_id,
        exit_code=2,
        outcome="exited",
        reason="nonzero",
        log_path=str(log),
    )
    assert state["exit_code"] == 2
    assert state["log_summary"]
    assert len(state["log_summary"]) <= 2000
    assert "TRACEBACK: boom" in state["log_summary"]
    # summarize_log standalone
    assert "TRACEBACK" in summarize_log(log)


def test_capture_exit_evidence_when_pid_dead(store: SqliteReliabilityStore, tmp_path: Path) -> None:
    imp_id = _drive_to_approved(store)
    log = tmp_path / "w.log"
    log.write_text("worker died mid-apply\n", encoding="utf-8")
    put_worker_state(
        store,
        imp_id,
        pid=999999,  # almost certainly not alive
        log=str(log),
        outcome="spawned",
        running=True,
    )
    state = capture_exit_evidence(store, imp_id, pid_alive_fn=lambda _pid: False)
    assert state.get("running") is False
    assert state["outcome"] == OUTCOME_EXITED
    assert "worker died" in (state.get("log_summary") or "")


def test_bounded_attempt_escalation_to_failed_plus_critical(
    store: SqliteReliabilityStore,
) -> None:
    """Exhausted attempts ⇒ approved→failed + critical notification."""
    imp_id = _drive_to_approved(store)
    put_worker_state(
        store,
        imp_id,
        spawn_attempts=DEFAULT_MAX_ATTEMPTS,
        apply_attempts=DEFAULT_MAX_ATTEMPTS,
        last_spawn_at=(datetime.now(UTC) - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        last_deferred_reason="apply_lease_held",
        running=False,
    )
    store.update_improvement_fields(imp_id, attempt=DEFAULT_MAX_ATTEMPTS)

    notes: list[dict] = []

    def notify(**kwargs):
        notes.append(kwargs)

    box = _ApplyBox([], [])  # must not be called when already exhausted
    summary = redrive_stranded_approvals(
        store,
        apply_fn=box,
        grace_seconds=0,
        max_attempts=DEFAULT_MAX_ATTEMPTS,
        owner="esc",
        notify_fn=notify,
    )
    assert summary["escalated"] == 1
    assert box.calls == []
    imp = store.get_improvement(imp_id)
    assert imp.status == ImprovementStatus.FAILED.value
    assert notes and notes[0]["severity"] == "critical"
    assert notes[0]["kind"] == "escalation"
    assert get_worker_state(store, imp_id)["outcome"] == OUTCOME_ESCALATED


def test_live_pid_skips_redrive(store: SqliteReliabilityStore) -> None:
    imp_id = _drive_to_approved(store)
    put_worker_state(
        store,
        imp_id,
        pid=1,
        running=True,
        last_spawn_at=(datetime.now(UTC) - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        spawn_attempts=1,
    )
    box = _ApplyBox([ApplyResult(applied=True)], [])
    summary = redrive_stranded_approvals(
        store,
        apply_fn=box,
        grace_seconds=0,
        max_attempts=3,
        owner="live",
        pid_alive_fn=lambda _pid: True,
    )
    assert summary["skipped_live"] == 1
    assert box.calls == []


def test_grace_period_skips_fresh_approval(store: SqliteReliabilityStore) -> None:
    imp_id = _drive_to_approved(store)
    put_worker_state(
        store,
        imp_id,
        last_spawn_at=datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        spawn_attempts=1,
        running=False,
    )
    box = _ApplyBox([ApplyResult(applied=True)], [])
    summary = redrive_stranded_approvals(
        store, apply_fn=box, grace_seconds=3600, max_attempts=3, owner="grace"
    )
    assert summary["skipped_grace"] == 1
    assert box.calls == []


def test_overlap_deferral_reason_recorded(store: SqliteReliabilityStore) -> None:
    imp_id = _drive_to_approved(store)
    put_worker_state(
        store,
        imp_id,
        last_spawn_at=(datetime.now(UTC) - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        running=False,
    )
    box = _ApplyBox(
        [ApplyResult(deferred=True, reason="overlap_with_monitoring")],
        [],
    )
    redrive_stranded_approvals(store, apply_fn=box, grace_seconds=0, max_attempts=5, owner="ov")
    state = get_worker_state(store, imp_id)
    assert state["outcome"] == OUTCOME_DEFERRED
    assert state["last_deferred_reason"] == "overlap_with_monitoring"
    assert store.get_improvement(imp_id).status == ImprovementStatus.APPROVED.value


# --- H-07 production wiring: the scheduled/recovery path re-drives, unaided --------


class _FakePopen:
    """Fake detached subprocess — records the spawn without starting a process."""

    def __init__(self, calls: list[dict], pid: int = 31337) -> None:
        self.calls = calls
        self.pid = pid

    def __call__(self, argv, **kwargs):
        self.calls.append({"argv": list(argv), "kwargs": kwargs})
        stdout = kwargs.get("stdout")
        if hasattr(stdout, "close"):
            stdout.close()
        return type("_Proc", (), {"pid": self.pid})()


@pytest.fixture()
def isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Worker logs land in a disposable runtime home, never the checkout."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("OMNIAGENTOS_HOME", str(home))
    monkeypatch.setenv("OMNIAGENTOS_RELIABILITY_LLM", "0")
    return home


def _strand_after_spawn_failure(
    store: SqliteReliabilityStore, tmp_path: Path, *, age_seconds: int = 300
) -> str:
    """An approved row whose worker never started: durable spawn failure, past grace."""
    imp_id = _drive_to_approved(store, title="stranded")
    log = tmp_path / "spawn-failed.log"
    log.write_text("spawn planned\n", encoding="utf-8")
    record_spawn_attempt(
        store,
        imp_id,
        command="apply",
        python="/no/such/python",
        cwd=str(tmp_path),
        log_path=str(log),
        argv=["/no/such/python", "-m", WORKER_MODULE, "apply"],
    )
    record_spawn_failure(store, imp_id, error="No such file or directory", log_path=str(log))
    put_worker_state(
        store,
        imp_id,
        last_spawn_at=(datetime.now(UTC) - timedelta(seconds=age_seconds)).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        ),
    )
    return imp_id


def _assert_h06_worker_argv(call: dict, imp_id: str, db_path: str, home: Path) -> None:
    argv = call["argv"]
    kwargs = call["kwargs"]
    assert argv[0] == sys.executable and argv[0] != "python"
    assert argv[1:4] == ["-m", WORKER_MODULE, "apply"]
    assert argv[argv.index("--improvement") + 1] == imp_id
    assert argv[argv.index("--db") + 1] == db_path
    assert argv[argv.index("--repo-root") + 1] == str(product_root())
    assert kwargs["cwd"] == str(product_root())
    assert kwargs["start_new_session"] is True
    assert kwargs["stderr"] is subprocess.STDOUT
    assert kwargs["stdout"] is not subprocess.DEVNULL
    assert (home / "var" / "log" / f"reliability-apply-{imp_id}.log").is_file()


def test_scheduled_watch_cycle_redrives_stranded_approval(
    store: SqliteReliabilityStore, tmp_path: Path, isolated_home: Path
) -> None:
    """H-07 production wiring: the launchd ``watch`` cycle re-drives a stranded row.

    No manual ``redrive`` command, no injected re-driver — this is the same call the
    scheduled job makes after a restart or a failed spawn.
    """
    imp_id = _strand_after_spawn_failure(store, tmp_path)
    calls: list[dict] = []

    with patch(
        "omniagentos.reliability.worker_drive.subprocess.Popen",
        new=_FakePopen(calls),
    ):
        result = watch(store, once=True, vault_dir=str(tmp_path / "vault"))

    redrive = result["stats_json"]["recover"]["redrive"]
    assert redrive["redriven"] == 1, redrive
    assert redrive["checked"] == 1
    assert len(calls) == 1, "the watch cycle must actually spawn the apply worker"
    _assert_h06_worker_argv(calls[0], imp_id, str(tmp_path / "rel.sqlite3"), isolated_home)

    state = get_worker_state(store, imp_id)
    assert state["outcome"] == OUTCOME_SPAWNED
    assert state["pid"] == 31337
    assert state["running"] is True
    assert state["spawn_attempts"] == 2  # original failure + scheduled re-drive
    assert store.get_improvement(imp_id).status == ImprovementStatus.APPROVED.value


def test_recovery_cycle_redrives_stranded_approval(
    store: SqliteReliabilityStore, tmp_path: Path, isolated_home: Path
) -> None:
    """The ``recover`` command's cycle re-drives too — same hook, no manual redrive."""
    imp_id = _strand_after_spawn_failure(store, tmp_path)
    calls: list[dict] = []

    with patch(
        "omniagentos.reliability.worker_drive.subprocess.Popen",
        new=_FakePopen(calls, pid=4242),
    ):
        summary = run_recovery_cycle(store)

    assert summary["redrive"]["redriven"] == 1
    assert len(calls) == 1
    _assert_h06_worker_argv(calls[0], imp_id, str(tmp_path / "rel.sqlite3"), isolated_home)
    assert get_worker_state(store, imp_id)["pid"] == 4242


def test_recovery_cycle_leaves_fresh_approval_alone(
    store: SqliteReliabilityStore, tmp_path: Path, isolated_home: Path
) -> None:
    """A just-approved row is inside the grace window: the live worker is not raced."""
    _strand_after_spawn_failure(store, tmp_path, age_seconds=0)
    calls: list[dict] = []

    with patch(
        "omniagentos.reliability.worker_drive.subprocess.Popen",
        new=_FakePopen(calls),
    ):
        summary = run_recovery_cycle(store)

    assert summary["redrive"]["skipped_grace"] == 1
    assert summary["redrive"]["redriven"] == 0
    assert calls == []


def test_recovery_cycle_survives_broken_redrive(store: SqliteReliabilityStore) -> None:
    """A failed re-drive is recorded as evidence and never aborts recovery."""

    def _boom(_store):
        raise RuntimeError("redrive exploded")

    summary = run_recovery_cycle(store, redrive_fn=_boom)
    assert summary["error_count"] == 0
    assert "redrive exploded" in summary["redrive"]["error"]


def test_worker_module_entrypoint_imports_under_spawned_interpreter(
    store: SqliteReliabilityStore, tmp_path: Path
) -> None:
    """H-06 guard: a REAL ``-m omniagentos.reliability`` child imports and exits 0."""
    proc = subprocess.run(
        [sys.executable, "-m", WORKER_MODULE, "status", "--db", str(tmp_path / "rel.sqlite3")],
        cwd=str(product_root()),
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout)["audits"] == []


def test_spawn_failure_in_redrive_records_durable_evidence(
    store: SqliteReliabilityStore, tmp_path: Path, isolated_home: Path
) -> None:
    """A re-drive whose spawn fails leaves evidence and keeps the row re-drivable."""
    imp_id = _strand_after_spawn_failure(store, tmp_path)
    spawn = make_worker_spawn_fn(store, db_path=str(tmp_path / "rel.sqlite3"))

    with patch(
        "omniagentos.reliability.worker_drive.subprocess.Popen",
        side_effect=OSError("No such file or directory: broken-python"),
    ) as popen:
        summary = redrive_stranded_approvals(
            store, spawn_fn=spawn, grace_seconds=0, max_attempts=5, owner="t"
        )

    assert summary["spawn_failed"] == 1
    assert summary["escalated"] == 0
    assert popen.call_args.kwargs["stdout"].closed is True
    state = get_worker_state(store, imp_id)
    assert state["outcome"] == OUTCOME_SPAWN_FAILED
    assert "broken-python" in state["last_spawn_error"]
    assert store.get_improvement(imp_id).status == ImprovementStatus.APPROVED.value


def test_make_worker_spawn_fn_closes_log_descriptor_on_success_and_failure(
    store: SqliteReliabilityStore, tmp_path: Path
) -> None:
    imp_id = _drive_to_approved(store)
    spawn = make_worker_spawn_fn(store, db_path=str(tmp_path / "rel.sqlite3"))

    # Success path
    with patch("omniagentos.reliability.worker_drive.subprocess.Popen") as popen:
        popen.return_value.pid = 9999
        meta = spawn("apply", imp_id)
        assert meta["pid"] == 9999
        assert popen.call_args.kwargs["stdout"].closed is True

    # Failure path
    with patch(
        "omniagentos.reliability.worker_drive.subprocess.Popen",
        side_effect=OSError("spawn failed"),
    ) as popen:
        try:
            spawn("apply", imp_id)
        except OSError:
            pass
        else:
            pytest.fail("expected OSError")
        assert popen.call_args.kwargs["stdout"].closed is True


def test_make_worker_spawn_fn_open_failure_persists_evidence(
    store: SqliteReliabilityStore, tmp_path: Path
) -> None:
    imp_id = _drive_to_approved(store)
    spawn = make_worker_spawn_fn(store, db_path=str(tmp_path / "rel.sqlite3"))

    with patch(
        "omniagentos.reliability.worker_drive.open",
        side_effect=PermissionError("Permission denied: log file"),
    ):
        try:
            spawn("apply", imp_id)
        except PermissionError:
            pass
        else:
            pytest.fail("expected PermissionError")

        summary = redrive_stranded_approvals(
            store, spawn_fn=spawn, grace_seconds=0, max_attempts=5, owner="t"
        )

    assert summary["spawn_failed"] == 1
    state = get_worker_state(store, imp_id)
    assert state["outcome"] == OUTCOME_SPAWN_FAILED
    assert "Permission denied" in state["last_spawn_error"]


def test_record_spawn_success_sets_stage_started(
    store: SqliteReliabilityStore, tmp_path: Path
) -> None:
    imp_id = _drive_to_approved(store)
    record_spawn_attempt(
        store,
        imp_id,
        command="apply",
        python="py",
        cwd=str(tmp_path),
        log_path=str(tmp_path / "l.log"),
        argv=["py"],
    )
    record_spawn_success(store, imp_id, pid=4242, log_path=str(tmp_path / "l.log"))
    imp = store.get_improvement(imp_id)
    assert imp.stage_started_at
    state = get_worker_state(store, imp_id)
    assert state["pid"] == 4242
    assert state["running"] is True
