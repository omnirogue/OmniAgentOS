"""Saga-driven merge queue executing deterministic SHA-bound instructions.

The merge queue manages merge saga states to safely land approved branches on main.
Verdicts are strictly SHA-bound so that judges who approved commit A cannot have commit B
merged. Because LLMs hold no git credentials, Sol issues a merge instruction as data.
This deterministic module then executes the instruction against the frozen SHA only after
re-verifying the branch hashes.
"""

from __future__ import annotations

import collections
import fcntl
import hashlib
import itertools
import logging
import os
import sqlite3
import subprocess
import threading
import time
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Literal, Protocol

from omniagentos.worktrees.git import MergeOutcome, WorktreesProto

__all__ = ["MergeOutcome"]

LOG = logging.getLogger(__name__)

VERIFY_TIMEOUT_S: float = 600.0   # 10 minutes
PAUSE_S: float = 1800.0           # 30-minute circuit-breaker pause
CHEAP_TIER: str = "cheap"
SagaState = Literal[
    "CALL_RESERVED",
    "CALL_SENT",
    "APPROVED_SHA",
    "MERGE_INTENT",
    "MERGED_SHA",
    "VERIFYING",
    "VERIFIED",
    "REVERTED",
]
TERMINAL_STATES: frozenset[str] = frozenset({"VERIFIED", "REVERTED"})


@dataclass(frozen=True)
class AttemptFingerprint:
    base_sha: str
    head_sha: str
    tree_hash: str
    diff_hash: str
    judge_config_hash: str


@dataclass(frozen=True)
class MergeInstruction:
    """Issued by the LLM as DATA. Carries no credentials and no git handle."""

    attempt_id: str
    branch: str
    frozen: AttemptFingerprint      # frozen when judging BEGAN; never re-read after
    rebased_against_sha: str        # main HEAD the branch was last rebased onto
    idempotency_key: str
    message: str = "improve: merge attempt"


@dataclass(frozen=True)
class RepoObservation:
    main_head_sha: str
    branch_fingerprint: AttemptFingerprint | None   # None when the branch/sha vanished


MergeDecision = Literal["MERGE", "REJECT_MUTATED", "REJECT_MISSING", "ABORT_STALE_BASE"]


@dataclass(frozen=True)
class MergePlan:
    decision: MergeDecision
    reason: str
    merge_sha: str | None = None      # EXACTLY instruction.frozen.head_sha when decision == MERGE
    rejudge_tier: str | None = None   # CHEAP_TIER, only on ABORT_STALE_BASE


def plan_merge(instruction: MergeInstruction, observed: RepoObservation) -> MergePlan:
    """Pure planning function with strict check ordering."""
    if observed.branch_fingerprint is None:
        return MergePlan(
            decision="REJECT_MISSING",
            reason="Branch or head ref is missing in the repository.",
        )

    frozen = instruction.frozen
    obs = observed.branch_fingerprint

    # Mutation gate: Compares ALL FIVE fields in order
    if obs.base_sha != frozen.base_sha:
        return MergePlan(
            decision="REJECT_MUTATED",
            reason="base_sha mutated",
        )
    if obs.head_sha != frozen.head_sha:
        return MergePlan(
            decision="REJECT_MUTATED",
            reason="head_sha mutated",
        )
    if obs.tree_hash != frozen.tree_hash:
        return MergePlan(
            decision="REJECT_MUTATED",
            reason="tree_hash mutated",
        )
    if obs.diff_hash != frozen.diff_hash:
        return MergePlan(
            decision="REJECT_MUTATED",
            reason="diff_hash mutated",
        )
    if obs.judge_config_hash != frozen.judge_config_hash:
        return MergePlan(
            decision="REJECT_MUTATED",
            reason="judge_config_hash mutated",
        )

    # Freshness gate
    if observed.main_head_sha != instruction.rebased_against_sha:
        return MergePlan(
            decision="ABORT_STALE_BASE",
            reason="main HEAD moved since the branch was last rebased",
            rejudge_tier=CHEAP_TIER,
        )

    return MergePlan(
        decision="MERGE",
        reason="Fingerprint matches perfectly and base is fresh.",
        merge_sha=instruction.frozen.head_sha,
    )


def _git(repo: str, *args: str) -> subprocess.CompletedProcess[str]:
    """Fixed-argv, hooks-free git subprocess invocation."""
    identity = (
        "-c",
        "user.email=00000000+omniagentos-bot[bot]@users.noreply.github.com",
        "-c",
        "user.name=OmniAgentOS Improve",
    )
    no_hooks = ("-c", "core.hooksPath=")
    return subprocess.run(  # noqa: S603
        ("git", "-C", repo, *identity, *no_hooks, *args),
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )


def fingerprint_attempt(
    repo: str, *, base_sha: str, head_ref: str, judge_config_hash: str
) -> AttemptFingerprint | None:
    """Read narrow git information to assemble an AttemptFingerprint."""
    p_head = _git(repo, "rev-parse", f"{head_ref}^{{commit}}")
    if p_head.returncode != 0:
        return None
    head_sha = p_head.stdout.strip()
    if not head_sha:
        return None

    p_tree = _git(repo, "rev-parse", f"{head_sha}^{{tree}}")
    if p_tree.returncode != 0:
        return None
    tree_hash = p_tree.stdout.strip()
    if not tree_hash:
        return None

    p_diff = _git(repo, "diff", "--binary", base_sha, head_sha)
    if p_diff.returncode != 0:
        return None
    diff_hash = hashlib.sha256(p_diff.stdout.encode("utf-8")).hexdigest()

    return AttemptFingerprint(
        base_sha=base_sha,
        head_sha=head_sha,
        tree_hash=tree_hash,
        diff_hash=diff_hash,
        judge_config_hash=judge_config_hash,
    )


def revert_head(repo: str, merged_sha: str) -> str | None:
    """Best-effort git revert of the merged commit."""
    p = _git(repo, "revert", "-m", "1", "--no-edit", merged_sha)
    if p.returncode != 0:
        _git(repo, "revert", "--abort")
        return None
    p_head = _git(repo, "rev-parse", "HEAD")
    if p_head.returncode == 0:
        return p_head.stdout.strip()
    return None


@dataclass(frozen=True)
class VerificationOutcome:
    status: Literal["ok", "failed", "timeout", "server_error"]
    http_status: int | None = None
    detail: str = ""


class VerifierProto(Protocol):

    def __call__(self, *, repo: str, merged_sha: str, budget_s: float) -> VerificationOutcome: ...


@dataclass(frozen=True)
class StewardPage:
    attempt_id: str
    reason: str
    merged_sha: str | None
    paused_until: float


PagerProto = Callable[[StewardPage], None]


@dataclass(frozen=True)
class SagaRow:
    attempt_id: str
    state: SagaState
    approved_sha: str | None
    merged_sha: str | None
    idempotency_key: str
    updated_at: str


class SagaStore:

    def __init__(self, db_path: str) -> None:
        self._conn = sqlite3.connect(db_path, isolation_level=None, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._lock = threading.RLock()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def reserve(self, attempt_id: str, idempotency_key: str) -> bool:
        now_iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        with self._lock:
            try:
                self._conn.execute(
                    "INSERT INTO improve_saga (attempt_id, state, idempotency_key, updated_at) "
                    "VALUES (?, 'CALL_RESERVED', ?, ?)",
                    (attempt_id, idempotency_key, now_iso),
                )
                return True
            except sqlite3.IntegrityError:
                return False

    def get(self, attempt_id: str) -> SagaRow | None:
        with self._lock:
            cur = self._conn.execute(
                "SELECT attempt_id, state, approved_sha, merged_sha, idempotency_key, updated_at "
                "FROM improve_saga WHERE attempt_id = ?",
                (attempt_id,),
            )
            row = cur.fetchone()
            if row is None:
                return None
            return SagaRow(
                attempt_id=row["attempt_id"],
                state=row["state"],
                approved_sha=row["approved_sha"],
                merged_sha=row["merged_sha"],
                idempotency_key=row["idempotency_key"],
                updated_at=row["updated_at"],
            )

    def advance(
        self,
        attempt_id: str,
        state: SagaState,
        *,
        approved_sha: str | None = None,
        merged_sha: str | None = None,
    ) -> None:
        now_iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        with self._lock:
            if approved_sha is not None and merged_sha is not None:
                self._conn.execute(
                    "UPDATE improve_saga SET state = ?, approved_sha = ?, merged_sha = ?, "
                    "updated_at = ? WHERE attempt_id = ?",
                    (state, approved_sha, merged_sha, now_iso, attempt_id),
                )
            elif approved_sha is not None:
                self._conn.execute(
                    "UPDATE improve_saga SET state = ?, approved_sha = ?, updated_at = ? "
                    "WHERE attempt_id = ?",
                    (state, approved_sha, now_iso, attempt_id),
                )
            elif merged_sha is not None:
                self._conn.execute(
                    "UPDATE improve_saga SET state = ?, merged_sha = ?, updated_at = ? "
                    "WHERE attempt_id = ?",
                    (state, merged_sha, now_iso, attempt_id),
                )
            else:
                self._conn.execute(
                    "UPDATE improve_saga SET state = ?, updated_at = ? WHERE attempt_id = ?",
                    (state, now_iso, attempt_id),
                )

    def unfinished(self) -> list[SagaRow]:
        with self._lock:
            cur = self._conn.execute(
                "SELECT attempt_id, state, approved_sha, merged_sha, idempotency_key, updated_at "
                "FROM improve_saga WHERE state NOT IN ('VERIFIED', 'REVERTED') "
                "ORDER BY updated_at, attempt_id"
            )
            rows = cur.fetchall()
            return [
                SagaRow(
                    attempt_id=r["attempt_id"],
                    state=r["state"],
                    approved_sha=r["approved_sha"],
                    merged_sha=r["merged_sha"],
                    idempotency_key=r["idempotency_key"],
                    updated_at=r["updated_at"],
                )
                for r in rows
            ]


RecoveryActionKind = Literal[
    "RETRY_CALL", "RECHECK_IDEMPOTENCY", "REQUEUE_MERGE", "RECONCILE_MERGE", "RESUME_VERIFY"
]


@dataclass(frozen=True)
class RecoveryAction:
    attempt_id: str
    state: SagaState
    action: RecoveryActionKind
    detail: str


def plan_recovery(rows: Sequence[SagaRow]) -> list[RecoveryAction]:
    """Pure recovery actions mapping for non-terminal saga states."""
    actions: list[RecoveryAction] = []
    for r in rows:
        if r.state == "CALL_RESERVED":
            actions.append(
                RecoveryAction(
                    r.attempt_id,
                    r.state,
                    "RETRY_CALL",
                    "The API call has been reserved but not sent yet; safe to retry call.",
                )
            )
        elif r.state == "CALL_SENT":
            actions.append(
                RecoveryAction(
                    r.attempt_id,
                    r.state,
                    "RECHECK_IDEMPOTENCY",
                    "The API call was sent but outcome is unknown; recheck idempotency.",
                )
            )
        elif r.state == "APPROVED_SHA":
            actions.append(
                RecoveryAction(
                    r.attempt_id,
                    r.state,
                    "REQUEUE_MERGE",
                    "Saga has an approved SHA; safe to re-queue the merge attempt.",
                )
            )
        elif r.state == "MERGE_INTENT":
            actions.append(
                RecoveryAction(
                    r.attempt_id,
                    r.state,
                    "RECONCILE_MERGE",
                    "Merge intent was registered; check if the merge landed on main.",
                )
            )
        elif r.state in ("MERGED_SHA", "VERIFYING"):
            actions.append(
                RecoveryAction(
                    r.attempt_id,
                    r.state,
                    "RESUME_VERIFY",
                    "Branch was merged; resume verification of the current HEAD.",
                )
            )
    return actions


ReceiptOutcome = Literal[
    "VERIFIED",
    "REJECTED_MUTATED",
    "REJECTED_MISSING",
    "ABORTED_STALE_BASE",
    "REVERTED",
    "CONFLICT",
    "QUEUE_PAUSED",
]


@dataclass(frozen=True)
class MergeReceipt:
    attempt_id: str
    outcome: ReceiptOutcome
    saga_state: SagaState
    merged_sha: str | None = None
    revert_sha: str | None = None
    rejudge_tier: str | None = None
    detail: str = ""


class MergeQueue:

    def __init__(
        self,
        *,
        repo: str,
        worktrees: WorktreesProto,
        saga: SagaStore,
        verifier: VerifierProto,
        pager: PagerProto,
        clock: Callable[[], float] = time.monotonic,
        lock_path: str | None = None,
        verify_timeout_s: float = VERIFY_TIMEOUT_S,
        pause_s: float = PAUSE_S,
    ) -> None:
        self._repo = repo
        self._worktrees = worktrees
        self._saga = saga
        self._verifier = verifier
        self._pager = pager
        self._clock = clock
        self._verify_timeout_s = verify_timeout_s
        self._pause_s = pause_s

        if lock_path is None:
            self._lock_path = os.path.join(repo, ".git", "improve-merge.lock")
        else:
            self._lock_path = lock_path

        self._queue_lock = threading.Lock()
        self._git_lock = threading.RLock()
        self._queue: collections.deque[tuple[int, MergeInstruction]] = collections.deque()
        self._tickets = itertools.count(1)
        self._paused_until: float | None = None

    def submit(self, instruction: MergeInstruction) -> int:
        """Enqueues a merge instruction under a thread lock and reserves the saga."""
        with self._queue_lock:
            reserved = self._saga.reserve(instruction.attempt_id, instruction.idempotency_key)
            if not reserved:
                return -1
            ticket = next(self._tickets)
            self._queue.append((ticket, instruction))
            self._saga.advance(
                instruction.attempt_id,
                "APPROVED_SHA",
                approved_sha=instruction.frozen.head_sha,
            )
            return ticket

    def pending(self) -> int:
        with self._queue_lock:
            return len(self._queue)

    def paused_until(self) -> float | None:
        return self._paused_until

    def paused(self) -> bool:
        if self._paused_until is None:
            return False
        return self._clock() < self._paused_until

    @contextmanager
    def _serialized(self) -> Iterator[None]:
        """Process-wide and thread-wide serialization lock via fcntl.flock."""
        with self._git_lock:
            lock_dir = os.path.dirname(self._lock_path)
            if lock_dir:
                os.makedirs(lock_dir, exist_ok=True)
            fd = os.open(self._lock_path, os.O_RDWR | os.O_CREAT, 0o666)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX)
                yield
            finally:
                try:
                    fcntl.flock(fd, fcntl.LOCK_UN)
                except OSError:
                    pass
                os.close(fd)

    def run_next(self) -> MergeReceipt | None:
        """Runs the next instruction in the queue, ensuring FIFO pop happens inside lock."""
        with self._serialized():
            if self.paused():
                with self._queue_lock:
                    if not self._queue:
                        return None
                    _, instruction = self._queue[0]
                return MergeReceipt(
                    attempt_id=instruction.attempt_id,
                    outcome="QUEUE_PAUSED",
                    saga_state="APPROVED_SHA",
                    detail="The queue is paused due to a prior verification failure.",
                )

            with self._queue_lock:
                if not self._queue:
                    return None
                ticket, instruction = self._queue.popleft()

            main_head = self._worktrees.head_sha(self._repo) or ""
            observed = fingerprint_attempt(
                self._repo,
                base_sha=instruction.frozen.base_sha,
                head_ref=instruction.branch,
                judge_config_hash=instruction.frozen.judge_config_hash,
            )

            plan = plan_merge(instruction, RepoObservation(main_head, observed))

            if plan.decision == "REJECT_MUTATED":
                self._saga.advance(instruction.attempt_id, "REVERTED")
                return MergeReceipt(
                    attempt_id=instruction.attempt_id,
                    outcome="REJECTED_MUTATED",
                    saga_state="REVERTED",
                    detail=plan.reason,
                )
            elif plan.decision == "REJECT_MISSING":
                self._saga.advance(instruction.attempt_id, "REVERTED")
                return MergeReceipt(
                    attempt_id=instruction.attempt_id,
                    outcome="REJECTED_MISSING",
                    saga_state="REVERTED",
                    detail=plan.reason,
                )
            elif plan.decision == "ABORT_STALE_BASE":
                self._saga.advance(instruction.attempt_id, "APPROVED_SHA")
                return MergeReceipt(
                    attempt_id=instruction.attempt_id,
                    outcome="ABORTED_STALE_BASE",
                    saga_state="APPROVED_SHA",
                    rejudge_tier=CHEAP_TIER,
                    detail=plan.reason,
                )

            self._saga.advance(instruction.attempt_id, "MERGE_INTENT")
            outcome = self._worktrees.merge_branch(
                self._repo,
                instruction.branch,
                instruction.message,
                sha=plan.merge_sha,
            )

            if outcome.status in ("conflict", "noop"):
                self._saga.advance(instruction.attempt_id, "APPROVED_SHA")
                return MergeReceipt(
                    attempt_id=instruction.attempt_id,
                    outcome="CONFLICT",
                    saga_state="APPROVED_SHA",
                    detail=f"Merge {outcome.status}: {outcome.detail}",
                )

            merged_sha = outcome.sha
            assert merged_sha is not None
            self._saga.advance(instruction.attempt_id, "MERGED_SHA", merged_sha=merged_sha)

            self._saga.advance(instruction.attempt_id, "VERIFYING")
            started = self._clock()
            try:
                result = self._verifier(
                    repo=self._repo,
                    merged_sha=merged_sha,
                    budget_s=self._verify_timeout_s,
                )
            except Exception as exc:
                result = VerificationOutcome("failed", detail=repr(exc))
            elapsed = self._clock() - started

            is_good = (elapsed < self._verify_timeout_s) and (result.status == "ok")

            if is_good:
                self._saga.advance(instruction.attempt_id, "VERIFIED", merged_sha=merged_sha)
                return MergeReceipt(
                    attempt_id=instruction.attempt_id,
                    outcome="VERIFIED",
                    saga_state="VERIFIED",
                    merged_sha=merged_sha,
                    detail=result.detail,
                )

            # Verification failed, or verification timed out -> auto-revert
            revert_sha = revert_head(self._repo, merged_sha)
            self._saga.advance(instruction.attempt_id, "REVERTED", merged_sha=merged_sha)

            self._paused_until = self._clock() + self._pause_s

            reason = (
                f"Verification failed. Status: {result.status}, "
                f"HTTP status: {result.http_status}, elapsed: {elapsed:.2f}s, "
                f"detail: {result.detail}"
            )
            self._pager(
                StewardPage(
                    attempt_id=instruction.attempt_id,
                    reason=reason,
                    merged_sha=merged_sha,
                    paused_until=self._paused_until,
                )
            )

            detail = reason
            if revert_sha is None:
                detail += " (Auto-revert failed!)"

            return MergeReceipt(
                attempt_id=instruction.attempt_id,
                outcome="REVERTED",
                saga_state="REVERTED",
                merged_sha=merged_sha,
                revert_sha=revert_sha,
                detail=detail,
            )

    def drain(self) -> list[MergeReceipt]:
        receipts: list[MergeReceipt] = []
        while True:
            r = self.run_next()
            if r is None:
                break
            receipts.append(r)
            if r.outcome == "QUEUE_PAUSED":
                break
        return receipts
