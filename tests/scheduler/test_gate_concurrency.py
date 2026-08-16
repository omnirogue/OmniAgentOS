"""Tests for B2 single-flight claims, atomic publication, CAS settlement, and crash recovery."""

from __future__ import annotations

import hmac
import json
import os
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

import omniagentos.scheduler.gate_evidence as gate_evidence_module
from omniagentos.contracts import RunState
from omniagentos.db.store import SqliteStore
from omniagentos.policy import load_policy
from omniagentos.scheduler.gate_evidence import (
    ClaimToken,
    GateEvidence,
    GateEvidenceStore,
    GateExecutionInfraError,
)
from omniagentos.scheduler.gate_runner import (
    GateRunRequest,
    PytestGateRunner,
    produce_gate_evidence,
)
from omniagentos.scheduler.routines_settle import settle_pending
from omniagentos.scheduler.routines_tick import tick
from omniagentos.scheduler.store import RoutineRunAlreadySettled, RoutinesStore
from tests.routines.conftest import valid_routine_payload
from tests.support.db_template import make_store

NOW = datetime(2026, 1, 1, 9, 0, tzinfo=UTC)


def _git_workspace(tmp_path: Path) -> Path:
    root = tmp_path / "workspace"
    root.mkdir(parents=True, exist_ok=True)
    (root / "suite").mkdir(parents=True, exist_ok=True)
    (root / "suite" / "test_gate.py").write_text("def test_ok(): assert True\n", encoding="utf-8")
    (root / ".gitignore").write_text(".pytest_cache\n__pycache__\nvar\n", encoding="utf-8")

    subprocess.run(["git", "init"], cwd=str(root), check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.name", "Test"], cwd=str(root), check=True, capture_output=True
    )
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"],
        cwd=str(root),
        check=True,
        capture_output=True,
    )
    subprocess.run(["git", "add", "."], cwd=str(root), check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=str(root), check=True, capture_output=True)
    return root


def _fire_run(db: SqliteStore, **overrides: object) -> tuple[dict, dict]:
    routines = RoutinesStore(db)
    payload = valid_routine_payload(
        name="R1",
        trigger_config={"cron": "* * * * *"},
        task_template={"title": "test", "harness": "mock"},
        gate_type="test_command",
        gate_config={"command": "pytest suite", "expected_exit_code": 0},
    )
    payload.update(overrides)
    routine = routines.create_routine(payload)
    _fired = tick(db, load_policy(), now=NOW)["fired"][0]
    routine_run = routines.list_runs(routine["id"])[0]
    run_id = routine_run["run_id"]
    db.update_run(
        run_id,
        {"state": RunState.COMPLETED.value, "finished_at": "2026-01-01T09:00:00Z", "cost_usd": 1.0},
    )
    return routine, routine_run


class CountingRunner:
    def __init__(self, inner: PytestGateRunner) -> None:
        self.inner = inner
        self.run_count = 0
        import threading

        self._lock = threading.Lock()

    @property
    def timeout_seconds(self) -> float:
        return self.inner.timeout_seconds

    def run(self, request: GateRunRequest) -> GateEvidence:
        with self._lock:
            self.run_count += 1
        return self.inner.run(request)


def test_concurrent_settlers_execute_once_and_notify_once(tmp_path: Path) -> None:
    """N1 probe: two concurrent settle_pending calls execute gate once and notify once."""
    db = make_store(SqliteStore, tmp_path / "test.db")
    routine, routine_run = _fire_run(db)
    workspace = _git_workspace(tmp_path)
    store = GateEvidenceStore(tmp_path / "evidence")
    counting_runner = CountingRunner(PytestGateRunner(store))

    notifications: list[str] = []

    def mock_notify(routine: dict[str, Any], run: dict[str, Any], gate_passed: bool) -> None:
        notifications.append(run["id"])

    import omniagentos.scheduler.routines_settle as rs

    old_notify = rs._notify
    rs._notify = mock_notify

    try:

        def worker() -> dict:
            return settle_pending(
                db,
                now=NOW,
                evidence_store=store,
                gate_runner=counting_runner,
                workspace=workspace,
            )

        with ThreadPoolExecutor(max_workers=2) as ex:
            f1 = ex.submit(worker)
            f2 = ex.submit(worker)
            res1 = f1.result()
            res2 = f2.result()

        settled_count = len(res1["settled"]) + len(res2["settled"])
        assert counting_runner.run_count == 1
        assert settled_count == 1
        assert len(notifications) == 1
    finally:
        rs._notify = old_notify


def test_direct_settle_run_race_raises_routine_run_already_settled(tmp_path: Path) -> None:
    """N2 probe: second call to settle_run raises RoutineRunAlreadySettled."""
    db = make_store(SqliteStore, tmp_path / "test.db")
    routines = RoutinesStore(db)
    routine, routine_run = _fire_run(db)

    routines.settle_run(
        routine_run["id"],
        gate_passed=True,
        accepted=True,
        finished_at="2026-01-01T09:00:00Z",
        notes="first",
    )

    with pytest.raises(RoutineRunAlreadySettled):
        routines.settle_run(
            routine_run["id"],
            gate_passed=True,
            accepted=True,
            finished_at="2026-01-01T09:00:00Z",
            notes="second",
        )


def test_expired_claim_reclaim_by_fresh_process(tmp_path: Path) -> None:
    """N3 probe: expired claim is reclaimed via atomic rename."""
    store = GateEvidenceStore(tmp_path / "evidence")
    token1 = store.claim("rt-1", "run-1", ttl_seconds=0.01)
    assert token1 is not None
    time.sleep(0.05)

    token2 = store.claim("rt-1", "run-1", ttl_seconds=60.0)
    assert token2 is not None
    assert token2.holder_uuid != token1.holder_uuid


def test_concurrent_expired_claim_reclaim_single_winner(tmp_path: Path) -> None:
    """Requirement 3: concurrent reclaim of an expired claim yields exactly one token and one runner execution."""
    store = GateEvidenceStore(tmp_path / "evidence")
    workspace = _git_workspace(tmp_path)

    # 1. Test direct store.claim concurrency on an expired claim
    expired_token = store.claim("rt-1", "run-expired-1", ttl_seconds=0.01)
    assert expired_token is not None
    time.sleep(0.05)

    def claim_worker() -> ClaimToken | None:
        return store.claim("rt-1", "run-expired-1", ttl_seconds=60.0)

    with ThreadPoolExecutor(max_workers=32) as ex:
        tokens = list(ex.map(lambda _: claim_worker(), range(32)))

    non_none_tokens = [t for t in tokens if t is not None]
    assert len(non_none_tokens) == 1, f"Expected exactly 1 claim winner, got {len(non_none_tokens)}"

    # 2. Test produce_gate_evidence concurrency on an expired claim
    expired_token2 = store.claim("rt-1", "run-expired-2", ttl_seconds=0.01)
    assert expired_token2 is not None
    time.sleep(0.05)

    counting_runner = CountingRunner(PytestGateRunner(store))
    req = GateRunRequest(
        routine_id="rt-1",
        run_id="run-expired-2",
        iteration=1,
        gate_type="test_command",
        gate_config={"command": "pytest suite", "expected_exit_code": 0},
        workspace=workspace,
    )

    def produce_worker() -> object:
        return produce_gate_evidence(counting_runner, store, req)

    with ThreadPoolExecutor(max_workers=32) as ex:
        outcomes = list(ex.map(lambda _: produce_worker(), range(32)))

    assert counting_runner.run_count == 1, (
        f"Expected 1 runner execution, got {counting_runner.run_count}"
    )
    recorded = [o for o in outcomes if getattr(o, "detail", "") == "recorded new evidence"]
    assert len(recorded) == 1


def test_expired_reclaim_does_not_remove_newer_live_claimant(tmp_path: Path) -> None:
    """Requirement 2: Reclaim never removes a newer live claimant published after the stale read."""
    store = GateEvidenceStore(tmp_path / "evidence")

    # 1. Create an expired claim with holder_uuid "stale_uuid"
    claim_path = store._claim_path("rt-1", "run-stale")
    claim_path.parent.mkdir(parents=True, exist_ok=True)
    stale_body = json.dumps({"holder_uuid": "stale_uuid", "expires_at": time.time() - 10})
    claim_path.write_text(stale_body, encoding="utf-8")

    # 2. Reclaimer A reclaims the stale claim
    token_a = store.claim("rt-1", "run-stale", ttl_seconds=60.0)
    assert token_a is not None
    assert token_a.holder_uuid != "stale_uuid"

    # Verify claim_path now holds token_a's live claim
    live_data = json.loads(claim_path.read_text(encoding="utf-8"))
    assert live_data["holder_uuid"] == token_a.holder_uuid

    # 3. Reclaimer B attempts to claim "run-stale" while token_a is live
    token_b = store.claim("rt-1", "run-stale", ttl_seconds=60.0)
    assert token_b is None

    # Verify token_a's live claim is STILL present on disk
    final_data = json.loads(claim_path.read_text(encoding="utf-8"))
    assert final_data["holder_uuid"] == token_a.holder_uuid


def test_post_revalidation_path_replacement_is_restored_without_overwrite(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A live inode published after exact revalidation survives takeover."""
    store = GateEvidenceStore(tmp_path / "evidence")
    stale = store.claim("rt-1", "run-race", ttl_seconds=0.01)
    assert stale is not None
    time.sleep(0.05)
    claim_path = store._claim_path("rt-1", "run-race")

    fresh_body = (
        json.dumps(
            {
                "holder_uuid": "fresh-live-holder",
                "pid": os.getpid(),
                "hostname": "test-host",
                "created_at": time.time(),
                "expires_at": time.time() + 600,
            }
        )
        + "\n"
    )
    real_rename = os.rename
    real_replace = os.replace
    injected: dict[str, int] = {}

    def replace_after_revalidation(source: str | Path, destination: str | Path) -> None:
        if Path(source) == claim_path and "inode" not in injected:
            fresh_tmp = claim_path.with_name(f".{claim_path.name}.fresh")
            fresh_tmp.write_text(fresh_body, encoding="utf-8")
            with fresh_tmp.open("rb") as stream:
                os.fsync(stream.fileno())
            real_replace(fresh_tmp, claim_path)
            injected["inode"] = claim_path.stat().st_ino
        real_rename(source, destination)

    monkeypatch.setattr(gate_evidence_module.os, "rename", replace_after_revalidation)
    reclaimer = store.claim("rt-1", "run-race", ttl_seconds=60.0)

    assert reclaimer is None
    assert claim_path.read_text(encoding="utf-8") == fresh_body
    assert claim_path.stat().st_ino == injected["inode"]
    assert not list(claim_path.parent.glob(f".{claim_path.name}.reclaim-*"))


def test_live_claim_published_after_isolation_wins_no_replace_publication(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A fresh claimant in the final publication gap is never overwritten."""
    store = GateEvidenceStore(tmp_path / "evidence")
    stale = store.claim("rt-1", "run-publish-gap", ttl_seconds=0.01)
    assert stale is not None
    time.sleep(0.05)
    claim_path = store._claim_path("rt-1", "run-publish-gap")
    fresh_body = (
        json.dumps(
            {
                "holder_uuid": "publication-gap-winner",
                "pid": os.getpid(),
                "hostname": "test-host",
                "created_at": time.time(),
                "expires_at": time.time() + 600,
            }
        )
        + "\n"
    ).encode()
    real_link = os.link
    publication_attempts = 0
    winner_inode = 0

    def publish_in_final_gap(
        source: str | Path,
        destination: str | Path,
        *,
        follow_symlinks: bool = True,
    ) -> None:
        nonlocal publication_attempts, winner_inode
        if Path(destination) == claim_path:
            publication_attempts += 1
            if publication_attempts == 2:
                winner = claim_path.with_name(f".{claim_path.name}.winner")
                winner.write_bytes(fresh_body)
                real_link(winner, claim_path)
                winner_inode = claim_path.stat().st_ino
                winner.unlink()
        real_link(source, destination, follow_symlinks=follow_symlinks)

    monkeypatch.setattr(gate_evidence_module.os, "link", publish_in_final_gap)
    reclaimer = store.claim("rt-1", "run-publish-gap", ttl_seconds=60.0)

    assert reclaimer is None
    assert publication_attempts == 2
    assert claim_path.read_bytes() == fresh_body
    assert claim_path.stat().st_ino == winner_inode


def test_post_publication_same_uuid_inode_aba_does_not_grant_token(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Token issuance remains bound to the exact inode that was published."""
    store = GateEvidenceStore(tmp_path / "evidence")
    claim_path = store._claim_path("rt-1", "run-token-aba")
    real_link = os.link
    published_inode = 0
    replacement_inode = 0
    expected_body = b""

    def replace_after_publication(
        source: str | Path,
        destination: str | Path,
        *,
        follow_symlinks: bool = True,
    ) -> None:
        nonlocal published_inode, replacement_inode, expected_body
        real_link(source, destination, follow_symlinks=follow_symlinks)
        if Path(destination) == claim_path and replacement_inode == 0:
            published_inode = claim_path.stat().st_ino
            expected_body = Path(source).read_bytes()
            replacement = claim_path.with_name(f".{claim_path.name}.same-uuid-aba")
            replacement.write_bytes(expected_body)
            os.replace(replacement, claim_path)
            replacement_inode = claim_path.stat().st_ino

    monkeypatch.setattr(gate_evidence_module.os, "link", replace_after_publication)
    token = store.claim("rt-1", "run-token-aba", ttl_seconds=60.0)

    assert token is None
    assert published_inode != replacement_inode
    assert claim_path.stat().st_ino == replacement_inode
    assert claim_path.read_bytes() == expected_body


def test_post_revalidation_inode_aba_with_identical_bytes_gets_no_token(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Replacing stale bytes with a new inode is detected even with the same UUID."""
    store = GateEvidenceStore(tmp_path / "evidence")
    stale = store.claim("rt-1", "run-aba", ttl_seconds=0.01)
    assert stale is not None
    time.sleep(0.05)
    claim_path = store._claim_path("rt-1", "run-aba")
    identical_body = claim_path.read_bytes()
    original_inode = claim_path.stat().st_ino
    real_rename = os.rename
    real_replace = os.replace
    replacement_inode = 0

    def inode_aba(source: str | Path, destination: str | Path) -> None:
        nonlocal replacement_inode
        if Path(source) == claim_path and replacement_inode == 0:
            replacement = claim_path.with_name(f".{claim_path.name}.aba")
            replacement.write_bytes(identical_body)
            real_replace(replacement, claim_path)
            replacement_inode = claim_path.stat().st_ino
            assert replacement_inode != original_inode
        real_rename(source, destination)

    monkeypatch.setattr(gate_evidence_module.os, "rename", inode_aba)
    assert store.claim("rt-1", "run-aba", ttl_seconds=60.0) is None
    assert claim_path.read_bytes() == identical_body
    assert claim_path.stat().st_ino == replacement_inode


def test_post_revalidation_same_inode_byte_change_gets_no_token(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """In-place UUID/expiry mutation cannot pass exact-byte revalidation."""
    store = GateEvidenceStore(tmp_path / "evidence")
    stale = store.claim("rt-1", "run-bytes", ttl_seconds=0.01)
    assert stale is not None
    time.sleep(0.05)
    claim_path = store._claim_path("rt-1", "run-bytes")
    original_inode = claim_path.stat().st_ino
    live_body = (
        json.dumps(
            {
                "holder_uuid": "in-place-live-holder",
                "pid": os.getpid(),
                "hostname": "test-host",
                "created_at": time.time(),
                "expires_at": time.time() + 600,
            }
        )
        + "\n"
    )
    real_rename = os.rename
    injected = False

    def byte_aba(source: str | Path, destination: str | Path) -> None:
        nonlocal injected
        if Path(source) == claim_path and not injected:
            claim_path.write_text(live_body, encoding="utf-8")
            injected = True
            assert claim_path.stat().st_ino == original_inode
        real_rename(source, destination)

    monkeypatch.setattr(gate_evidence_module.os, "rename", byte_aba)
    assert store.claim("rt-1", "run-bytes", ttl_seconds=60.0) is None
    assert claim_path.read_text(encoding="utf-8") == live_body
    assert claim_path.stat().st_ino == original_inode


def test_post_revalidation_contenders_execute_runner_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A contender arriving in the final takeover window cannot also execute."""
    store = GateEvidenceStore(tmp_path / "evidence")
    workspace = _git_workspace(tmp_path)
    stale = store.claim("rt-1", "run-window", ttl_seconds=0.01)
    assert stale is not None
    time.sleep(0.05)

    revalidated = threading.Event()
    continue_takeover = threading.Event()
    original_same = gate_evidence_module._same_claim_object
    paused = False

    def pause_after_revalidation(
        left: object,
        right: object,
    ) -> bool:
        nonlocal paused
        result = original_same(left, right)  # type: ignore[arg-type]
        if result and not paused:
            paused = True
            revalidated.set()
            assert continue_takeover.wait(timeout=5)
        return result

    monkeypatch.setattr(gate_evidence_module, "_same_claim_object", pause_after_revalidation)
    runner = CountingRunner(PytestGateRunner(store))
    request = GateRunRequest(
        routine_id="rt-1",
        run_id="run-window",
        iteration=1,
        gate_type="test_command",
        gate_config={"command": "pytest suite", "expected_exit_code": 0},
        workspace=workspace,
    )

    with ThreadPoolExecutor(max_workers=2) as executor:
        reclaimer_future = executor.submit(produce_gate_evidence, runner, store, request)
        assert revalidated.wait(timeout=5)
        contender_future = executor.submit(produce_gate_evidence, runner, store, request)
        contender = contender_future.result(timeout=5)
        continue_takeover.set()
        reclaimer = reclaimer_future.result(timeout=30)

    assert contender.status == "in_progress"
    assert reclaimer.status == "evidence"
    assert runner.run_count == 1


def test_release_is_inode_bound_against_same_uuid_aba(tmp_path: Path) -> None:
    """An old token cannot unlink a replacement inode reusing its UUID bytes."""
    store = GateEvidenceStore(tmp_path / "evidence")
    token = store.claim("rt-1", "run-release", ttl_seconds=60.0)
    assert token is not None
    claim_path = token.claim_path
    body = claim_path.read_bytes()
    replacement = claim_path.with_name(f".{claim_path.name}.replacement")
    replacement.write_bytes(body)
    os.replace(replacement, claim_path)
    assert claim_path.stat().st_ino != token.inode

    store.release_claim(token)

    assert claim_path.exists()
    assert claim_path.read_bytes() == body


def test_claim_refuses_symlinked_canonical_path(tmp_path: Path) -> None:
    """Canonical claim symlinks are unreadable, never stale takeover inputs."""
    store = GateEvidenceStore(tmp_path / "evidence")
    claim_path = store._claim_path("rt-1", "run-symlink")
    claim_path.parent.mkdir(parents=True, exist_ok=True)
    external = tmp_path / "external-claim"
    external.write_text(
        json.dumps({"holder_uuid": "external", "expires_at": time.time() - 100}),
        encoding="utf-8",
    )
    claim_path.symlink_to(external)

    assert store.claim("rt-1", "run-symlink", ttl_seconds=60.0) is None
    assert claim_path.is_symlink()
    assert external.exists()


def test_crash_artifacts_are_cleaned_after_bounded_stale_window(tmp_path: Path) -> None:
    """Expired takeover and tmp artifacts are reclaimed after the safety grace."""
    store = GateEvidenceStore(tmp_path / "evidence")
    claim_path = store._claim_path("rt-1", "run-cleanup")
    claim_path.parent.mkdir(parents=True, exist_ok=True)
    expired_body = json.dumps({"holder_uuid": "crashed", "expires_at": time.time() - 600}).encode()
    artifacts = [
        claim_path.parent / f".{claim_path.name}.claim.tmp-crash",
        claim_path.parent / f".{claim_path.name}.reclaim-crash",
    ]
    for artifact in artifacts:
        artifact.write_bytes(expired_body)
        old = time.time() - 300
        os.utime(artifact, (old, old))

    token = store.claim("rt-1", "run-cleanup", ttl_seconds=60.0)

    assert token is not None
    assert all(not artifact.exists() for artifact in artifacts)


def test_process_crash_after_stale_isolation_allows_immediate_recovery(
    tmp_path: Path,
) -> None:
    """Kernel lock release makes an abruptly interrupted takeover recoverable."""
    root = tmp_path / "evidence"
    store = GateEvidenceStore(root)
    stale = store.claim("rt-1", "run-isolation-crash", ttl_seconds=0.01)
    assert stale is not None
    time.sleep(0.05)
    claim_path = store._claim_path("rt-1", "run-isolation-crash")
    child = """
import os
import sys
from pathlib import Path
import omniagentos.scheduler.gate_evidence as module
from omniagentos.scheduler.gate_evidence import GateEvidenceStore

real_rename = os.rename
def crash_after_isolation(source, destination):
    real_rename(source, destination)
    os._exit(23)

module.os.rename = crash_after_isolation
GateEvidenceStore(Path(sys.argv[1])).claim(
    "rt-1", "run-isolation-crash", ttl_seconds=60.0
)
raise SystemExit(99)
"""
    completed = subprocess.run(
        [sys.executable, "-W", "error", "-c", child, str(root)],
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 23, completed.stderr
    assert not claim_path.exists()
    assert len(list(claim_path.parent.glob(f".{claim_path.name}.reclaim-*"))) == 1
    recovered = store.claim("rt-1", "run-isolation-crash", ttl_seconds=60.0)
    assert recovered is not None


def test_mutation_lock_is_kernel_released_on_descriptor_close(tmp_path: Path) -> None:
    """A crashed lock owner cannot wedge bounded stale-claim recovery."""
    store = GateEvidenceStore(tmp_path / "evidence")
    claim_path = store._claim_path("rt-1", "run-lock-crash")
    claim_path.parent.mkdir(parents=True, exist_ok=True)
    lock_fd = store._open_claim_lock(claim_path, blocking=False)
    assert lock_fd is not None
    os.close(lock_fd)  # Process death closes descriptors and releases flock.

    token = store.claim("rt-1", "run-lock-crash", ttl_seconds=60.0)
    assert token is not None


def test_unparseable_claim_file_is_not_reclaimed(tmp_path: Path) -> None:
    """Never interpret an empty, partial, or unreadable in-progress claim as expired."""
    store = GateEvidenceStore(tmp_path / "evidence")
    claim_file = store._claim_path("rt-1", "run-1")
    claim_file.parent.mkdir(parents=True, exist_ok=True)
    claim_file.write_text("corrupt json{", encoding="utf-8")

    token = store.claim("rt-1", "run-1", ttl_seconds=60.0)
    assert token is None


def test_tampered_record_causes_produce_gate_evidence_unavailable_and_settles_durable_stop_reason(
    tmp_path: Path,
) -> None:
    """Tampered final evidence on disk yields status='unavailable' and settles gate_evidence_unavailable."""
    db = make_store(SqliteStore, tmp_path / "test.db")
    routine, routine_run = _fire_run(db)
    workspace = _git_workspace(tmp_path)
    store = GateEvidenceStore(tmp_path / "evidence")

    rec_file = store._record_path(routine["id"], routine_run["run_id"])
    rec_file.parent.mkdir(parents=True, exist_ok=True)
    rec_file.write_text("tampered record data", encoding="utf-8")

    runner = PytestGateRunner(store)
    outcome = produce_gate_evidence(
        runner,
        store,
        GateRunRequest(
            routine_id=routine["id"],
            run_id=routine_run["run_id"],
            iteration=1,
            gate_type="test_command",
            gate_config={"command": "pytest suite", "expected_exit_code": 0},
            workspace=workspace,
        ),
    )
    assert outcome.status == "unavailable"

    res = settle_pending(
        db,
        now=NOW,
        evidence_store=store,
        gate_runner=runner,
        workspace=workspace,
    )
    assert len(res["settled"]) == 1
    assert res["settled"][0]["stop_reason"] == "gate_evidence_unavailable"


def test_crash_mid_write_leaves_no_torn_record(tmp_path: Path) -> None:
    """P3 probe: fault injected between tmp write and link leaves no torn record."""
    store = GateEvidenceStore(tmp_path / "evidence")
    workspace = _git_workspace(tmp_path)
    req = GateRunRequest(
        routine_id="rt-1",
        run_id="run-1",
        iteration=1,
        gate_type="test_command",
        gate_config={"command": "pytest suite", "expected_exit_code": 0},
        workspace=workspace,
    )

    rec_dir = store.root / "records" / "rt-1"
    rec_dir.mkdir(parents=True, exist_ok=True)
    (rec_dir / ".run-1.json.tmp-999-abc").write_text("partial data", encoding="utf-8")

    runner = PytestGateRunner(store)
    outcome = produce_gate_evidence(runner, store, req)
    assert outcome.status == "evidence"
    assert outcome.evidence is not None


def test_tampered_record_raises_gate_execution_infra_error(tmp_path: Path) -> None:
    """P3 probe: hand-corrupted final record raises GateExecutionInfraError on load."""
    store = GateEvidenceStore(tmp_path / "evidence")
    rec_file = store._record_path("rt-1", "run-1")
    rec_file.parent.mkdir(parents=True, exist_ok=True)
    rec_file.write_text("tampered garbage", encoding="utf-8")

    with pytest.raises(GateExecutionInfraError):
        store.load("rt-1", "run-1")


def test_v1_record_quarantined_and_reexecuted_once(tmp_path: Path) -> None:
    """P3 probe: valid v1 record is quarantined and re-executed."""
    store = GateEvidenceStore(tmp_path / "evidence")
    rec_file = store._record_path("rt-1", "run-1")
    rec_file.parent.mkdir(parents=True, exist_ok=True)

    v1_payload = {
        "schema": "omniagentos.gate-evidence.v1",
        "routine_id": "rt-1",
        "run_id": "run-1",
        "iteration": 1,
        "gate_type": "test_command",
        "command": "pytest suite",
        "targets": ["suite"],
        "workspace_digest": "ws",
        "binding_digest": "bd",
        "tool": "pytest",
        "tool_version": "8.0",
        "exit_code": 0,
        "checks_collected": 1,
        "checks_passed": 1,
        "checks_skipped": 0,
        "checks_failed": 0,
        "started_at": "2026-01-01T09:00:00Z",
        "finished_at": "2026-01-01T09:00:00Z",
        "nonce": "1234567890123456",
    }
    canonical = json.dumps(v1_payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    sig = hmac.new(store._key, canonical, "sha256").hexdigest()
    v1_payload["signature"] = sig
    rec_file.write_text(json.dumps(v1_payload, indent=2), encoding="utf-8")

    assert store.load("rt-1", "run-1") is None
    assert not rec_file.exists()
    assert rec_file.with_name("run-1.json.superseded-v1").exists()
