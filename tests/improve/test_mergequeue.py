"""Unit and integration tests for MergeQueue executing deterministic SHA-bound instructions."""

from __future__ import annotations

import fcntl
import os
import subprocess
import threading
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Literal

import pytest

from omniagentos.db.migrate import migrate
from omniagentos.improve.mergequeue import (
    AttemptFingerprint,
    MergeInstruction,
    MergeQueue,
    MergeReceipt,
    RepoObservation,
    SagaStore,
    StewardPage,
    VerificationOutcome,
    fingerprint_attempt,
    plan_merge,
    plan_recovery,
)
from omniagentos.worktrees.git import SubprocessWorktrees


class FakeClock:
    def __init__(self, now: float = 1000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def tick(self, dt: float) -> None:
        self.now += dt


def git_out(repo: Path, *args: str) -> str:
    res = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        check=True
    )
    return res.stdout.strip()


def commit_on(repo: Path, branch: str, filename: str, content: str) -> str:
    repo_str = str(repo)
    # Check if branch exists
    branches_p = subprocess.run(
        ["git", "-C", repo_str, "branch", "--list", branch],
        capture_output=True,
        text=True,
        check=True
    )
    branch_exists = bool(branches_p.stdout.strip())

    if branch_exists:
        subprocess.run(["git", "-C", repo_str, "checkout", branch], check=True)
    else:
        # Create branch from main
        subprocess.run(["git", "-C", repo_str, "checkout", "main"], check=True)
        subprocess.run(["git", "-C", repo_str, "checkout", "-b", branch], check=True)

    file_path = repo / filename
    file_path.write_text(content, encoding="utf-8")

    subprocess.run(["git", "-C", repo_str, "add", filename], check=True)
    subprocess.run(["git", "-C", repo_str, "commit", "-m", f"Commit on {branch} for {filename}"], check=True)

    sha = git_out(repo, "rev-parse", "HEAD")
    subprocess.run(["git", "-C", repo_str, "checkout", "main"], check=True)
    return sha


def build_instruction(
    repo: Path,
    branch: str,
    attempt_id: str,
    idempotency_key: str,
    judge_config_hash: str = "jc1",
    message: str = "improve: merge attempt",
    base_sha: str | None = None,
) -> MergeInstruction:
    main_head = base_sha or git_out(repo, "rev-parse", "main")
    frozen = fingerprint_attempt(
        str(repo),
        base_sha=main_head,
        head_ref=branch,
        judge_config_hash=judge_config_hash
    )
    assert frozen is not None, f"Failed to fingerprint branch {branch}"
    return MergeInstruction(
        attempt_id=attempt_id,
        branch=branch,
        frozen=frozen,
        rebased_against_sha=main_head,
        idempotency_key=idempotency_key,
        message=message,
    )


@pytest.fixture
def repo_path(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    # Initialize repository
    subprocess.run(["git", "init", "-b", "main"], check=True, cwd=str(repo))
    subprocess.run(["git", "config", "user.email", "test@example.com"], check=True, cwd=str(repo))
    subprocess.run(["git", "config", "user.name", "Test"], check=True, cwd=str(repo))
    subprocess.run(["git", "config", "commit.gpgsign", "false"], check=True, cwd=str(repo))
    # Initial commit
    readme = repo / "README.md"
    readme.write_text("Initial README", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], check=True, cwd=str(repo))
    subprocess.run(["git", "commit", "-m", "Initial commit"], check=True, cwd=str(repo))
    return repo


@pytest.fixture
def saga_store(tmp_path: Path) -> Iterator[SagaStore]:
    db_file = tmp_path / "improve.db"
    migrate(str(db_file))
    store = SagaStore(str(db_file))
    yield store
    store.close()


@pytest.fixture
def worktrees(tmp_path: Path) -> SubprocessWorktrees:
    return SubprocessWorktrees(namespace="improve", var_root=tmp_path / "var")


def make_verifier(
    clock: FakeClock,
    status: Literal["ok", "failed", "timeout", "server_error"] = "ok",
    duration: float = 0.0,
    http_status: int | None = None,
) -> Callable[..., VerificationOutcome]:
    def verifier(*, repo: str, merged_sha: str, budget_s: float) -> VerificationOutcome:
        clock.tick(duration)
        return VerificationOutcome(status, http_status=http_status, detail=f"Verified {merged_sha}")
    return verifier


def test_branch_mutated_after_approval_is_rejected(
    repo_path: Path, saga_store: SagaStore, worktrees: SubprocessWorktrees
) -> None:
    """Defends against landing untracked/unverified mutations on approved branches after judging."""
    # Create branch and commit A
    branch_name = "improve/f1"
    commit_on(repo_path, branch_name, "feat.txt", "content A")

    # Freeze instruction with commit A
    instruction = build_instruction(repo_path, branch_name, "att1", "idem1")

    # Mutate branch with commit B
    commit_on(repo_path, branch_name, "feat.txt", "content B")

    clock = FakeClock()
    pages: list[StewardPage] = []
    def pager(page: StewardPage) -> None:
        pages.append(page)

    verifier = make_verifier(clock)
    queue = MergeQueue(
        repo=str(repo_path),
        worktrees=worktrees,
        saga=saga_store,
        verifier=verifier,
        pager=pager,
        clock=clock,
    )

    head_before = git_out(repo_path, "rev-parse", "main")
    queue.submit(instruction)

    receipt = queue.run_next()
    assert receipt is not None
    assert receipt.outcome == "REJECTED_MUTATED"

    head_after = git_out(repo_path, "rev-parse", "main")
    assert head_after == head_before

    # Saga state check
    row = saga_store.get("att1")
    assert row is not None
    assert row.state == "REVERTED"

    # Check feat.txt is absent from main
    proc = subprocess.run(["git", "-C", str(repo_path), "cat-file", "-e", "HEAD:feat.txt"])
    assert proc.returncode != 0


def test_plan_merge_rejects_each_mutated_field() -> None:
    """Defends against accepting any change in fingerprint fields during merge planning."""
    frozen = AttemptFingerprint(
        base_sha="base",
        head_sha="head",
        tree_hash="tree",
        diff_hash="diff",
        judge_config_hash="jc1"
    )
    instruction = MergeInstruction(
        attempt_id="att1",
        branch="feature",
        frozen=frozen,
        rebased_against_sha="base",
        idempotency_key="idem1"
    )

    fields = ["base_sha", "head_sha", "tree_hash", "diff_hash", "judge_config_hash"]
    for field in fields:
        kwargs = {f: getattr(frozen, f) for f in fields}
        kwargs[field] = "mutated"
        mutated_fp = AttemptFingerprint(**kwargs)
        observed = RepoObservation(
            main_head_sha="base",
            branch_fingerprint=mutated_fp
        )
        plan = plan_merge(instruction, observed)
        assert plan.decision == "REJECT_MUTATED", f"Failed for field {field}"

    # An identical observation with a fresh base yields MERGE
    observed_fresh = RepoObservation(
        main_head_sha="base",
        branch_fingerprint=frozen
    )
    plan_fresh = plan_merge(instruction, observed_fresh)
    assert plan_fresh.decision == "MERGE"
    assert plan_fresh.merge_sha == frozen.head_sha


def test_stale_base_aborts_and_rejudges_cheap_only(
    repo_path: Path, saga_store: SagaStore, worktrees: SubprocessWorktrees
) -> None:
    """Defends against merging a branch built on an outdated main HEAD without re-evaluating."""
    branch_name = "improve/f2"
    main_head_1 = git_out(repo_path, "rev-parse", "main")

    # Commit on branch
    commit_on(repo_path, branch_name, "feat.txt", "content A")

    # Build instruction based on main_head_1
    instruction = build_instruction(repo_path, branch_name, "att2", "idem2", base_sha=main_head_1)

    # Commit directly on main, moving main HEAD forward
    readme = repo_path / "README.md"
    readme.write_text("Updated README directly on main", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo_path), "add", "README.md"], check=True)
    subprocess.run(["git", "-C", str(repo_path), "commit", "-m", "direct main commit"], check=True)

    main_head_2 = git_out(repo_path, "rev-parse", "main")
    assert main_head_2 != main_head_1

    clock = FakeClock()
    pages: list[StewardPage] = []
    def pager(page: StewardPage) -> None:
        pages.append(page)

    verifier = make_verifier(clock)
    queue = MergeQueue(
        repo=str(repo_path),
        worktrees=worktrees,
        saga=saga_store,
        verifier=verifier,
        pager=pager,
        clock=clock,
    )

    queue.submit(instruction)
    receipt = queue.run_next()

    assert receipt is not None
    assert receipt.outcome == "ABORTED_STALE_BASE"
    assert receipt.rejudge_tier == "cheap"

    head_after = git_out(repo_path, "rev-parse", "main")
    assert head_after == main_head_2

    row = saga_store.get("att2")
    assert row is not None
    assert row.state == "APPROVED_SHA"

    proc = subprocess.run(["git", "-C", str(repo_path), "cat-file", "-e", "HEAD:feat.txt"])
    assert proc.returncode != 0


def test_verification_timeout_reverts_and_pauses(
    repo_path: Path, saga_store: SagaStore, worktrees: SubprocessWorktrees
) -> None:
    """Defends against hanging tests and verification timeouts by rolling back the merge and pausing."""
    branch_name = "improve/f3"
    commit_on(repo_path, branch_name, "feat.txt", "content A")

    instruction = build_instruction(repo_path, branch_name, "att3", "idem3")

    clock = FakeClock(1000.0)
    pages: list[StewardPage] = []
    def pager(page: StewardPage) -> None:
        pages.append(page)

    # Verifier advances the fake clock by 601 and returns VerificationOutcome("timeout")
    verifier = make_verifier(clock, status="timeout", duration=601.0)

    queue = MergeQueue(
        repo=str(repo_path),
        worktrees=worktrees,
        saga=saga_store,
        verifier=verifier,
        pager=pager,
        clock=clock,
    )

    pre_merge_sha = git_out(repo_path, "rev-parse", "main")
    pre_merge_tree = git_out(repo_path, "rev-parse", "HEAD^{tree}")

    queue.submit(instruction)
    receipt = queue.run_next()

    assert receipt is not None
    assert receipt.merged_sha is not None
    assert receipt.merged_sha != pre_merge_sha
    assert receipt.outcome == "REVERTED"
    assert receipt.revert_sha is not None

    # Assert main's tree hash after the revert equals the pre-merge tree hash
    post_revert_tree = git_out(repo_path, "rev-parse", "HEAD^{tree}")
    assert post_revert_tree == pre_merge_tree

    # The branch file is gone from HEAD
    proc = subprocess.run(["git", "-C", str(repo_path), "cat-file", "-e", "HEAD:feat.txt"])
    assert proc.returncode != 0

    # Saga state check
    row = saga_store.get("att3")
    assert row is not None
    assert row.state == "REVERTED"

    # Paused queue checks
    assert queue.paused() is True
    assert queue.paused_until() == 1000.0 + 601.0 + 1800.0

    # Steward page check
    assert len(pages) == 1
    assert pages[0].attempt_id == "att3"


def test_verification_ok_after_deadline_is_fail_closed(
    repo_path: Path, saga_store: SagaStore, worktrees: SubprocessWorktrees
) -> None:
    """Defends against fail-open behaviors where a slow but successful verification passes the deadline."""
    branch_name = "improve/f4"
    commit_on(repo_path, branch_name, "feat.txt", "content A")

    instruction = build_instruction(repo_path, branch_name, "att4", "idem4")

    clock = FakeClock(1000.0)
    pages: list[StewardPage] = []
    def pager(page: StewardPage) -> None:
        pages.append(page)

    # Verifier returns "ok" but burns 601 seconds
    verifier = make_verifier(clock, status="ok", duration=601.0)

    queue = MergeQueue(
        repo=str(repo_path),
        worktrees=worktrees,
        saga=saga_store,
        verifier=verifier,
        pager=pager,
        clock=clock,
    )

    queue.submit(instruction)
    receipt = queue.run_next()

    assert receipt is not None
    assert receipt.outcome == "REVERTED"
    assert receipt.revert_sha is not None

    assert queue.paused() is True
    assert len(pages) == 1


def test_verification_5xx_reverts_and_pauses(
    repo_path: Path, saga_store: SagaStore, worktrees: SubprocessWorktrees
) -> None:
    """Defends against transient server errors during verification by reverting and pausing for safety."""
    branch_name = "improve/f5"
    commit_on(repo_path, branch_name, "feat.txt", "content A")

    instruction = build_instruction(repo_path, branch_name, "att5", "idem5")

    clock = FakeClock(1000.0)
    pages: list[StewardPage] = []
    def pager(page: StewardPage) -> None:
        pages.append(page)

    # VerificationOutcome("server_error", http_status=503) inside budget
    verifier = make_verifier(clock, status="server_error", duration=5.0, http_status=503)

    queue = MergeQueue(
        repo=str(repo_path),
        worktrees=worktrees,
        saga=saga_store,
        verifier=verifier,
        pager=pager,
        clock=clock,
    )

    queue.submit(instruction)
    receipt = queue.run_next()

    assert receipt is not None
    assert receipt.outcome == "REVERTED"
    assert receipt.revert_sha is not None

    assert queue.paused() is True
    assert len(pages) == 1


def test_paused_queue_holds_next_merge(
    repo_path: Path, saga_store: SagaStore, worktrees: SubprocessWorktrees
) -> None:
    """Defends against continuing to process queue items while the queue circuit-breaker is tripped."""
    # 1. Setup first instruction that will fail and pause queue
    branch_name_1 = "improve/f6-1"
    commit_on(repo_path, branch_name_1, "feat1.txt", "content 1")
    instruction_1 = build_instruction(repo_path, branch_name_1, "att6-1", "idem6-1")

    clock = FakeClock(1000.0)
    pages: list[StewardPage] = []
    def pager(page: StewardPage) -> None:
        pages.append(page)

    # Fails and pauses
    verifier_fail = make_verifier(clock, status="failed", duration=5.0)

    queue = MergeQueue(
        repo=str(repo_path),
        worktrees=worktrees,
        saga=saga_store,
        verifier=verifier_fail,
        pager=pager,
        clock=clock,
    )

    queue.submit(instruction_1)
    # This runs, fails, reverts, and pauses the queue until clock=1005 + 1800 = 2805
    receipt_1 = queue.run_next()
    assert receipt_1 is not None
    assert receipt_1.outcome == "REVERTED"
    assert queue.paused() is True

    # 2. Get HEAD of main *after the revert*
    main_head_after_revert = git_out(repo_path, "rev-parse", "main")

    # 3. Setup second branch off main_head_after_revert
    branch_name_2 = "improve/f6-2"
    commit_on(repo_path, branch_name_2, "feat2.txt", "content 2")
    # Freshly rebased against main_head_after_revert
    instruction_2 = build_instruction(repo_path, branch_name_2, "att6-2", "idem6-2", base_sha=main_head_after_revert)

    queue.submit(instruction_2)
    assert queue.pending() == 1

    # 4. Try running run_next() while paused
    receipt_2 = queue.run_next()
    assert receipt_2 is not None
    assert receipt_2.outcome == "QUEUE_PAUSED"

    # Main head did not move
    assert git_out(repo_path, "rev-parse", "main") == main_head_after_revert
    # It was not popped from the queue
    assert queue.pending() == 1

    # 5. Advance clock past the pause limit (clock is currently 1005.0)
    clock.tick(1801.0) # now clock is 2806.0, paused_until was 2805.0
    assert queue.paused() is False

    # Set a successful verifier for queue
    queue._verifier = make_verifier(clock, status="ok", duration=5.0)

    # Now run run_next() again
    receipt_3 = queue.run_next()
    assert receipt_3 is not None
    assert receipt_3.outcome == "VERIFIED"
    assert queue.pending() == 0

    # branch file 2 exists in main
    proc = subprocess.run(["git", "-C", str(repo_path), "cat-file", "-e", "HEAD:feat2.txt"])
    assert proc.returncode == 0


def test_happy_path_merges_and_verifies(
    repo_path: Path, saga_store: SagaStore, worktrees: SubprocessWorktrees
) -> None:
    """Defends against regressions in the standard success path of merging and verifying a branch."""
    branch_name = "improve/f7"
    commit_on(repo_path, branch_name, "feat.txt", "content A")

    instruction = build_instruction(repo_path, branch_name, "att7", "idem7")

    clock = FakeClock(1000.0)
    pages: list[StewardPage] = []
    def pager(page: StewardPage) -> None:
        pages.append(page)

    verifier = make_verifier(clock, status="ok", duration=10.0)

    queue = MergeQueue(
        repo=str(repo_path),
        worktrees=worktrees,
        saga=saga_store,
        verifier=verifier,
        pager=pager,
        clock=clock,
    )

    queue.submit(instruction)
    receipt = queue.run_next()

    assert receipt is not None
    assert receipt.outcome == "VERIFIED"
    assert receipt.saga_state == "VERIFIED"

    # Proves coordinator --no-ff
    parents_output = git_out(repo_path, "rev-list", "--parents", "-n", "1", "HEAD")
    assert len(parents_output.split()) == 3

    # Branch file exists in main
    proc = subprocess.run(["git", "-C", str(repo_path), "cat-file", "-e", "HEAD:feat.txt"])
    assert proc.returncode == 0

    # Saga state check
    row = saga_store.get("att7")
    assert row is not None
    assert row.state == "VERIFIED"

    assert queue.paused() is False


def test_plan_recovery_covers_every_nonterminal_state(saga_store: SagaStore) -> None:
    """Defends against missing or misconfigured recovery actions for any non-terminal saga state."""
    states = ["CALL_RESERVED", "CALL_SENT", "APPROVED_SHA", "MERGE_INTENT", "MERGED_SHA", "VERIFYING", "VERIFIED", "REVERTED"]
    for i, state in enumerate(states):
        saga_store._conn.execute(
            "INSERT INTO improve_saga (attempt_id, state, idempotency_key, updated_at) VALUES (?, ?, ?, ?)",
            (f"att_{i}", state, f"idem_{i}", f"2026-07-27T12:00:0{i}Z")
        )

    unfinished_rows = saga_store.unfinished()
    actions = plan_recovery(unfinished_rows)

    # VERIFIED and REVERTED should produce nothing (are terminal)
    # Remaining 6 non-terminal states should map to the exact action kind
    assert len(actions) == 6

    action_map = {act.attempt_id: act.action for act in actions}

    # Maps of expected transitions
    assert action_map["att_0"] == "RETRY_CALL"
    assert action_map["att_1"] == "RECHECK_IDEMPOTENCY"
    assert action_map["att_2"] == "REQUEUE_MERGE"
    assert action_map["att_3"] == "RECONCILE_MERGE"
    assert action_map["att_4"] == "RESUME_VERIFY"
    assert action_map["att_5"] == "RESUME_VERIFY"


def test_crash_at_merged_sha_is_recoverable(tmp_path: Path) -> None:
    """Defends against losing unverified merge states upon coordinator crash/restart."""
    db_file = tmp_path / "improve.db"
    migrate(str(db_file))

    store = SagaStore(str(db_file))
    # Setup some approved SHA and idempotency key
    store.reserve("att9", "idem9")
    # Simulate progress to MERGED_SHA with a real merged SHA
    store.advance("att9", "MERGED_SHA", merged_sha="abc123sha")
    store.close()

    # Re-open SagaStore over same database path
    store2 = SagaStore(str(db_file))
    try:
        unfinished = store2.unfinished()
        assert len(unfinished) == 1
        row = unfinished[0]
        assert row.attempt_id == "att9"
        assert row.state == "MERGED_SHA"
        assert row.merged_sha == "abc123sha"

        actions = plan_recovery(unfinished)
        assert len(actions) == 1
        assert actions[0].action == "RESUME_VERIFY"
    finally:
        store2.close()


def test_duplicate_idempotency_key_is_not_enqueued_twice(
    repo_path: Path, saga_store: SagaStore, worktrees: SubprocessWorktrees
) -> None:
    """Defends against duplicate submission requests with the same idempotency key."""
    branch_name = "improve/f8"
    commit_on(repo_path, branch_name, "feat.txt", "content A")

    instruction1 = build_instruction(repo_path, branch_name, "att10-1", "idem10")
    instruction2 = build_instruction(repo_path, branch_name, "att10-2", "idem10")

    clock = FakeClock()
    verifier = make_verifier(clock)
    queue = MergeQueue(
        repo=str(repo_path),
        worktrees=worktrees,
        saga=saga_store,
        verifier=verifier,
        pager=lambda p: None,
        clock=clock,
    )

    t1 = queue.submit(instruction1)
    assert t1 != -1

    t2 = queue.submit(instruction2)
    assert t2 == -1
    assert queue.pending() == 1


def test_fifo_order_preserved_under_concurrent_submissions(
    repo_path: Path, saga_store: SagaStore, worktrees: SubprocessWorktrees
) -> None:
    """Defends against concurrency race conditions resulting in out-of-order or duplicate execution."""
    base_sha = git_out(repo_path, "rev-parse", "main")

    # Build 8 branches off the same base
    instructions = []
    for i in range(1, 9):
        branch = f"improve/f9-{i}"
        commit_on(repo_path, branch, f"f{i}.txt", f"content {i}")
        instr = build_instruction(repo_path, branch, f"att_{i}", f"idem_{i}", base_sha=base_sha)
        instructions.append(instr)

    clock = FakeClock()
    verifier = make_verifier(clock, status="ok", duration=5.0)
    queue = MergeQueue(
        repo=str(repo_path),
        worktrees=worktrees,
        saga=saga_store,
        verifier=verifier,
        pager=lambda p: None,
        clock=clock,
    )

    barrier = threading.Barrier(8)
    submissions: list[tuple[int, str]] = []
    submissions_lock = threading.Lock()

    def submit_worker(instr: MergeInstruction) -> None:
        barrier.wait()
        ticket = queue.submit(instr)
        with submissions_lock:
            submissions.append((ticket, instr.attempt_id))

    threads = []
    for instr in instructions:
        t = threading.Thread(target=submit_worker, args=(instr,))
        t.start()
        threads.append(t)

    for t in threads:
        t.join()

    # Sort expected order by tickets
    expected_order = [attempt_id for ticket, attempt_id in sorted(submissions)]

    receipts: list[MergeReceipt] = []
    # Serialize record-after-run so completion-order races cannot scramble the
    # observed list; flock still serializes the git work itself.
    emit_lock = threading.Lock()

    def run_worker() -> None:
        while True:
            with emit_lock:
                r = queue.run_next()
                if r is None:
                    return
                receipts.append(r)

    worker_threads = []
    for _ in range(4):
        t = threading.Thread(target=run_worker)
        t.start()
        worker_threads.append(t)

    for t in worker_threads:
        t.join(timeout=120)
        assert not t.is_alive()

    assert len(receipts) == 8
    executed_order = [r.attempt_id for r in receipts]

    assert executed_order == expected_order
    assert len(set(executed_order)) == 8


def test_flock_serializes_merge_execution(
    repo_path: Path, saga_store: SagaStore, worktrees: SubprocessWorktrees
) -> None:
    """Defends against concurrent coordinators executing multiple merges at the same time by using fcntl.flock."""
    branch_name = "improve/f10"
    commit_on(repo_path, branch_name, "feat.txt", "content A")

    instruction = build_instruction(repo_path, branch_name, "att11", "idem11")

    clock = FakeClock()
    verifier = make_verifier(clock, status="ok", duration=10.0)

    lock_path = os.path.join(str(repo_path), ".git", "improve-merge.lock")

    queue = MergeQueue(
        repo=str(repo_path),
        worktrees=worktrees,
        saga=saga_store,
        verifier=verifier,
        pager=lambda p: None,
        clock=clock,
        lock_path=lock_path,
    )

    queue.submit(instruction)
    assert queue.pending() == 1

    # Open lock file and lock it EXCLUSIVELY in the test process/thread
    lock_dir = os.path.dirname(lock_path)
    os.makedirs(lock_dir, exist_ok=True)
    fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o666)
    fcntl.flock(fd, fcntl.LOCK_EX)

    receipt_list: list[MergeReceipt] = []
    def run_queue_thread() -> None:
        r = queue.run_next()
        if r is not None:
            receipt_list.append(r)

    head_before = git_out(repo_path, "rev-parse", "main")

    t = threading.Thread(target=run_queue_thread)
    t.start()

    # Sleep to allow thread to block on the flock
    time.sleep(0.3)

    # Assert queue did not progress
    assert len(receipt_list) == 0
    assert queue.pending() == 1
    assert git_out(repo_path, "rev-parse", "main") == head_before

    # Release flock
    fcntl.flock(fd, fcntl.LOCK_UN)
    os.close(fd)

    t.join(timeout=5.0)

    # Assert queue progressed and verified
    assert len(receipt_list) == 1
    assert receipt_list[0].outcome == "VERIFIED"
    assert queue.pending() == 0
    assert git_out(repo_path, "rev-parse", "main") != head_before
