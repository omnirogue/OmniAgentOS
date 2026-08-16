"""The durable, fenced run state machine."""

from __future__ import annotations

import hashlib
import importlib
import json
import logging
import os
import shlex
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Iterable, Iterator
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast

from omniagentos.adapters.common import _scrubbed_env
from omniagentos.budget.policy import blocks as budget_blocks
from omniagentos.contracts import (
    TASK_TRANSITIONS,
    TERMINAL_RUN_STATES,
    ActionClass,
    AgentAdapter,
    AgentInput,
    AgentUsage,
    ApprovalState,
    BudgetDecision,
    BudgetSpec,
    Events,
    HarnessProfile,
    HarnessType,
    IdempotencyReceipt,
    PolicyDecision,
    ResultStatus,
    RunManifest,
    RunState,
    SandboxSpec,
    StepStatus,
    Store,
    TaskState,
    can_transition_run,
    default_db_path,
    default_ledger_dir,
    default_vault_dir,
    digest,
    new_id,
    utc_now_iso,
)
from omniagentos.path_containment import inode_relative_parts_anchored
from omniagentos.policy import approval_satisfies_gate
from omniagentos.policy.shell import classify_shell
from omniagentos.runner import sandbox
from omniagentos.runner.scope_wiring import RunnerScope, ScopeDecision
from omniagentos.toolplane.scrub import scrub_agent_result

LOG = logging.getLogger(__name__)

_ACTION_CLASS_RANK = {
    ActionClass.READ_ONLY: 0,
    ActionClass.SANDBOXED_CREATION: 1,
    ActionClass.INTERNAL_REVERSIBLE: 2,
    ActionClass.EXTERNAL_REVERSIBLE: 3,
    ActionClass.CONSEQUENTIAL: 4,
    # Highest risk: the ONLY class that hard-stops in AUTO mode. Ranked above
    # consequential so `max(...)` always lets a delete/destroy dominate.
    ActionClass.IRREVERSIBLE: 5,
}
_DEFAULT_FINALIZE_ATTEMPT_LIMIT = 5
_FINALIZE_QUARANTINE_SENTINEL = "quarantined:finalization_failed"
# Transient pulse errors are retried; this many consecutive failures means the
# store is unusable and the active effect must stop/fence before a peer can
# adopt the still-running step (H-12).
_HEARTBEAT_PERSISTENT_FAILURES = 3
# Bound the post-SIGKILL wait so a wedged child cannot pin a runner slot (L-18).
_COMMAND_CLEANUP_TIMEOUT_S = 5.0
# Post-run queue retention: keep at most this many completed offsets' markers and
# compact the durable queue when it grows past this many drained lines (M-43).
_POSTRUN_RETENTION = 256


def _repo_root() -> str:
    """Return the repository root without importing a private contracts helper."""
    return os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _default_workspace_base() -> str:
    """Return the trusted runner-assigned base for per-run workspaces."""
    return os.environ.get("OMNIAGENTOS_WORKSPACE_DIR") or os.path.join(_repo_root(), "var", "runs")


# The real-harness arming switch, re-checked HERE at execution time.
#
# Intake stamps ``params["real_harness"]`` on a step only when the operator had
# armed real-harness execution at dispatch (omniagentos/intake/service.py, which
# owns the same env name). A run can sit queued long after that, so the runner
# must not take the dispatch's word for it: if the operator has since disarmed,
# an armed step fails CLOSED before the adapter is resolved or invoked -- nothing
# is spent. The name is duplicated rather than imported because the runner must
# never import the intake package (import cycle: intake -> api -> ... -> runner).
REAL_HARNESS_ENV = "OMNIAGENTOS_REAL_HARNESS"
_REAL_HARNESS_TRUTHY = frozenset({"1", "true", "yes", "on"})


def real_harness_enabled() -> bool:
    """True only when the operator has explicitly armed real-harness execution."""
    return os.environ.get(REAL_HARNESS_ENV, "").strip().lower() in _REAL_HARNESS_TRUTHY


class LostFence(RuntimeError):
    """The run is no longer owned by this worker."""


class StepFailure(RuntimeError):
    """A step failed after its allowed retries."""


def _noop_escalate(kind: str, run_id: str, detail: str) -> None:
    """Default escalation sink: do nothing (tests inject their own spy)."""
    return None


def _notify_escalation(kind: str, run_id: str, detail: str) -> None:
    """Production escalation: ping the human, best-effort, never raising.

    This is the ONLY channel that reaches the operator in AUTO mode. It fires on
    a hard-stop (approval requested), a cap-hit (budget/iteration), completion, or
    a genuine blocker -- never per-action. Delivery failures are swallowed so a
    down notifier can never wedge or fail a run."""
    try:
        from omniagentos.sessions.notify import push

        push(f"OmniAgentOS [{kind}]", f"run {run_id}: {detail}")
    except Exception:  # noqa: BLE001 - notification must never affect the runner
        pass


@dataclass(slots=True)
class RunnerDependencies:
    """Injectable leaf-library seams; the durable Store is intentionally separate."""

    evaluate_policy: Callable[[ActionClass], PolicyDecision]
    sandbox_for_tools: Callable[[HarnessType, list[str]], SandboxSpec]
    check_budget: Callable[[BudgetSpec, int, int, float], BudgetDecision]
    resolve_adapter: Callable[[HarnessType], AgentAdapter]
    append_manifest: Callable[[str, RunManifest], str]
    render_run_note: Callable[..., tuple[str, str]]
    write_note: Callable[[str, str, str], str]
    approval_expiry_hours: int = 24
    # NT-notify: optional seam invoked when a run escalates to needing approval.
    # Receives the freshly-created approval row and persists+pushes a linked
    # notification. Defaults to None (no-op) so tests that build deps directly
    # stay fully isolated; production wires it in ``load`` (see below).
    notify_approval: Callable[[dict[str, Any]], None] | None = None
    # escalate(kind, run_id, detail) -- the human-ping seam. Defaults to a no-op so
    # existing test-constructed dependencies keep working; production uses
    # _notify_escalation. Fired ONLY on hard-stop / cap-hit / done / blocker.
    escalate: Callable[[str, str, str], None] = _noop_escalate

    @classmethod
    def load(cls) -> RunnerDependencies:
        """Import parallel leaf packages only when constructing a production runner."""
        budget = importlib.import_module("omniagentos.budget")
        ledger = importlib.import_module("omniagentos.ledger")
        policy = importlib.import_module("omniagentos.policy")
        registry = importlib.import_module("omniagentos.adapters.registry")
        vault = importlib.import_module("omniagentos.vault")
        load_policy = cast(Callable[[], Any], policy.load_policy)
        evaluate = cast(Callable[[ActionClass, Any], PolicyDecision], policy.evaluate_action)
        sandbox = cast(
            Callable[[HarnessType, list[str], Any], SandboxSpec],
            policy.sandbox_for_tools,
        )
        cfg = load_policy()
        return cls(
            evaluate_policy=lambda action: evaluate(action, cfg),
            sandbox_for_tools=lambda harness, tools: sandbox(harness, tools, cfg),
            check_budget=cast(
                Callable[[BudgetSpec, int, int, float], BudgetDecision],
                budget.check,
            ),
            resolve_adapter=cast(Callable[[HarnessType], AgentAdapter], registry.resolve_adapter),
            append_manifest=cast(Callable[[str, RunManifest], str], ledger.append_manifest),
            render_run_note=cast(
                Callable[
                    [dict[str, Any], list[dict[str, Any]], str, list[IdempotencyReceipt]],
                    tuple[str, str],
                ],
                vault.render_run_note,
            ),
            write_note=cast(Callable[[str, str, str], str], vault.write_note),
            approval_expiry_hours=int(cfg.approval_expiry_hours),
            notify_approval=_default_approval_notifier,
            escalate=_notify_escalation,
        )


def _default_approval_notifier(approval: dict[str, Any]) -> None:
    """Production seam: persist+push a notification for a run-escalated approval.

    Bound to ``default_db_path`` because the production runner's store IS that
    database (see ``runner/__main__.py``), so the notification lands in the same
    db the approval was written to. Best-effort inside the service, so this never
    perturbs runner supervision.
    """
    from omniagentos.notifications.service import notify_approval_requested

    notify_approval_requested(
        approval_id=str(approval.get("id")),
        proposed_action=str(approval.get("proposed_action") or ""),
        action_class=str(approval.get("action_class") or ""),
        source="runner",
        severity="high" if approval.get("risk") in {"high", "critical"} else "warning",
        risk=str(approval.get("risk") or ""),
        run_id=str(approval.get("run_id")) if approval.get("run_id") else None,
        db_path=default_db_path(),
    )


@dataclass(slots=True)
class StepOutcome:
    result: dict[str, Any]
    skipped: bool = False
    usage: AgentUsage | None = None


def _json(value: Any) -> str:
    return json.dumps(value, separators=(",", ":"), sort_keys=True)


def _object(raw: Any, default: dict[str, Any] | None = None) -> dict[str, Any]:
    if raw is None or raw == "":
        return {} if default is None else dict(default)
    if isinstance(raw, dict):
        return dict(raw)
    value = json.loads(str(raw))
    if not isinstance(value, dict):
        raise ValueError("expected a JSON object")
    return value


def _strip_elevation_metadata(metadata: dict[str, Any]) -> dict[str, Any]:
    """Strip elevation flags from model/plan-authored metadata.

    Step params originate in ``plan_json``, which a planning model may author.
    Keys consumed as current or future elevation grants must therefore never flow
    from that channel into an adapter. Operator-authenticated elevation has no
    runner plan channel today, so stripping every ``*elevated*`` key is the
    fail-closed behavior.
    """
    return {key: value for key, value in metadata.items() if "elevated" not in key.lower()}


def _array(raw: Any) -> list[dict[str, Any]]:
    if isinstance(raw, list):
        value = raw
    else:
        value = json.loads(str(raw or "[]"))
    if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
        raise ValueError("plan_json must be an array of objects")
    return [dict(item) for item in value]


def _casefold_path(path: str) -> str:
    """Case-fold a path for containment on a case-insensitive filesystem.

    Mirrors ``omniagentos.policy.secrets._casefold_path`` so a case-variant
    ``working_dir`` (e.g. ``~/.SSH``) cannot slip a granted-root containment
    check on macOS's default case-insensitive volume. Linux stays case-sensitive.
    """
    normalized = os.path.normcase(path)
    if sys.platform == "darwin":
        normalized = normalized.lower()
    return normalized


def _resolve_realpath(path: str) -> str | None:
    text = path.strip().strip("\"'") if isinstance(path, str) else ""
    if not text:
        return None
    try:
        return os.path.realpath(os.path.expanduser(text))
    except (OSError, ValueError):
        return None


def _path_within_granted(candidate: str, granted_roots: Iterable[str]) -> bool:
    """True when ``candidate`` provably resolves INSIDE one of ``granted_roots``.

    Mirrors ``omniagentos.policy.shell._path_in_project`` (``os.path.realpath``
    resolves ``..`` and symlinks, so an escape lands OUT of scope) and adds the
    secret registry's case-fold, so the check is ``..``/symlink/case safe. A path
    that cannot be resolved -- or that resolves outside every granted root --
    fails closed (not in scope).
    """
    resolved = _resolve_realpath(candidate)
    if resolved is None:
        return False
    for root in granted_roots:
        root_resolved = _resolve_realpath(root)
        if root_resolved is None:
            continue
        if inode_relative_parts_anchored(resolved, root_resolved) is not None:
            return True
    return False


class Runner:
    """Executes persisted plans one fenced step boundary at a time."""

    def __init__(
        self,
        store: Store,
        worker_id: str,
        *,
        dependencies: RunnerDependencies | None = None,
        stale_s: int | None = None,
        ledger_dir: str | None = None,
        vault_dir: str | None = None,
        workspace_base: str | None = None,
        finalize_attempt_limit: int | None = None,
        pid: int | None = None,
        concurrency: int | None = None,
    ) -> None:
        self.store = store
        self.worker_id = worker_id
        self.dependencies = dependencies or RunnerDependencies.load()
        self.stale_s = (
            stale_s if stale_s is not None else int(os.environ.get("OMNIAGENTOS_STALE_S", "30"))
        )
        self.ledger_dir = ledger_dir or default_ledger_dir()
        self.vault_dir = vault_dir or default_vault_dir()
        self.workspace_base = workspace_base or _default_workspace_base()
        self.finalize_attempt_limit = (
            finalize_attempt_limit
            if finalize_attempt_limit is not None
            else int(
                os.environ.get(
                    "OMNIAGENTOS_FINALIZE_ATTEMPT_LIMIT",
                    str(_DEFAULT_FINALIZE_ATTEMPT_LIMIT),
                )
            )
        )
        self._finalize_attempts: dict[str, int] = {}
        self._finalize_backoff = False
        # Per-instance state mirrors _finalize_attempts: it makes recall a first-executed-
        # step operation without leaking a sentinel through AgentInput.metadata["context"].
        # A process restart can forget this guard, but completed steps are skipped on
        # resume and Hebbian pair updates have their own per-run deduplication.
        self._recall_state: dict[str, bool] = {}
        # Per-run guard for the memory / context-assembly layer: prepend prior context +
        # persist the operator's brief exactly once (on the first agent step) per run.
        self._memory_state: dict[str, bool] = {}
        self.pid = pid if pid is not None else os.getpid()
        # Per-worker concurrency cap. K in-flight runs execute at once; default 1
        # keeps single-slot behavior byte-for-byte (the launchd pool sets it higher
        # via --concurrency / OMNIAGENTOS_RUNNER_CONCURRENCY).
        self.concurrency = max(
            1,
            concurrency
            if concurrency is not None
            else int(os.environ.get("OMNIAGENTOS_RUNNER_CONCURRENCY", "1")),
        )
        # Generalizes the old single-valued ``current_run_id`` to the set of runs a
        # worker is executing RIGHT NOW across its K slots. It is the sole guard that
        # keeps every claim/reclaim/finalize/parked selection from handing an
        # already-executing run to a second slot -- the exactly-once invariant. Only
        # the (single) scheduler thread selects, so this set is mutated by the
        # scheduler on dispatch and by each slot on completion; the lock makes those
        # cross-thread reads/writes safe.
        self._in_flight: set[str] = set()
        self._in_flight_lock = threading.Lock()
        # Phase 3 path ownership (omniagentos/runner/scope_wiring.py). Constructing
        # it touches nothing: with OMNIAGENTOS_SCOPE_LOCKS off — the default — every
        # entry point returns before reading config or the database, so a worker is
        # byte-for-byte the pre-Phase-3 worker.
        self._scope = RunnerScope(store, worker_id=worker_id, workspace_base=self.workspace_base)
        # Post-run jobs are durable on disk, scoped to this store's database, and
        # drained under a cross-process claim so concurrent runners cannot double-
        # execute or drain another database's work (M-43). The worker is lazy for
        # new queues, but an existing queue is noticed here so jobs survive restarts.
        self._postrun_file_lock = threading.Lock()
        self._postrun_thread_lock = threading.Lock()
        self._postrun_wake = threading.Event()
        self._postrun_stop = threading.Event()
        self._postrun_thread: threading.Thread | None = None
        # Per-step abort signals from the heartbeat pulse thread: persistent
        # liveness failure must stop the active effect before peer adoption (H-12).
        self._step_aborts: dict[str, str] = {}
        self._step_aborts_lock = threading.Lock()
        self._active_processes: dict[str, subprocess.Popen[str]] = {}
        self._active_processes_lock = threading.Lock()
        queue_path = self._postrun_queue_path()
        if queue_path.is_file() and queue_path.stat().st_size:
            self._postrun_wake.set()
            self._ensure_postrun_daemon()

    @property
    def actor(self) -> str:
        return f"runner:{self.worker_id}"

    @property
    def current_run_id(self) -> str | None:
        """A representative in-flight run, for observers that expect one value.

        Retained for backward compatibility (heartbeat rows, dashboards) now that a
        worker can hold up to K runs at once; ``None`` when idle.
        """
        return self._heartbeat_run_id()

    def _mark_in_flight(self, run_id: str) -> None:
        with self._in_flight_lock:
            self._in_flight.add(run_id)

    def _clear_in_flight(self, run_id: str) -> None:
        with self._in_flight_lock:
            self._in_flight.discard(run_id)

    def _in_flight_snapshot(self) -> set[str]:
        with self._in_flight_lock:
            return set(self._in_flight)

    def _heartbeat_run_id(self) -> str | None:
        with self._in_flight_lock:
            return next(iter(self._in_flight), None)

    def _heartbeat(self) -> None:
        self.store.upsert_heartbeat(self.worker_id, self.pid, self._heartbeat_run_id())

    def _store_identity(self) -> str:
        """Stable fingerprint for the durable store this runner owns work for.

        Post-run jobs are scoped by this identity so two runners pointed at
        different databases never share a queue or drain each other's work.
        """
        raw = getattr(self.store, "_db_path", None)
        if raw is None or raw == ":memory:":
            # In-memory and protocol-only stores still need isolation between
            # concurrent runners; fall back to a process-local token.
            return f"mem-{abs(hash((id(self.store), self.worker_id))) % (16**8):08x}"
        resolved = os.path.abspath(os.path.expanduser(str(raw)))
        return hashlib.sha256(resolved.encode("utf-8")).hexdigest()[:16]

    def _postrun_queue_dir(self) -> Path:
        base = os.environ.get("OMNIAGENTOS_VAR_DIR") or os.environ.get("OMNIAGENTOS_VAR")
        if not base:
            base = os.path.join(_repo_root(), "var")
        return Path(base) / "runner" / "postrun" / self._store_identity()

    def _postrun_queue_path(self) -> Path:
        return self._postrun_queue_dir() / "queue.jsonl"

    def _postrun_done_path(self) -> Path:
        return self._postrun_queue_dir() / "queue.jsonl.done"

    def _postrun_claim_path(self) -> Path:
        return self._postrun_queue_dir() / "queue.jsonl.claims"

    def _postrun_lock_path(self) -> Path:
        return self._postrun_queue_dir() / "queue.lock"

    @contextmanager
    def _postrun_cross_process_lock(self) -> Iterator[None]:
        """Exclusive lock across runner processes for claim/drain critical sections."""
        lock_path = self._postrun_lock_path()
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        # Thread lock first so one process's daemon and main thread serialize too.
        with self._postrun_file_lock:
            handle = lock_path.open("a+", encoding="utf-8")
            try:
                try:
                    import fcntl

                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
                except ImportError:
                    pass
                yield
            finally:
                try:
                    import fcntl

                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
                except (ImportError, OSError):
                    pass
                handle.close()

    def _append_postrun_line(self, path: Path, value: dict[str, Any]) -> None:
        """Durably append one compact JSON object without rewriting queue state."""
        path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(value, separators=(",", ":")) + "\n"
        with path.open("a", encoding="utf-8") as handle:
            handle.write(line)
            handle.flush()
            os.fsync(handle.fileno())

    def _enqueue_postrun_job(self, run_id: str, kind: str) -> None:
        job = {
            "run_id": run_id,
            "kind": kind,
            "db": self._store_identity(),
            "enqueued_by": self.worker_id,
            "enqueued_at": utc_now_iso(),
        }
        with self._postrun_cross_process_lock():
            self._append_postrun_line(self._postrun_queue_path(), job)
        self._postrun_wake.set()
        self._ensure_postrun_daemon()

    def _ensure_postrun_daemon(self) -> None:
        if self._postrun_stop.is_set():
            return
        with self._postrun_thread_lock:
            if self._postrun_thread is not None and self._postrun_thread.is_alive():
                return
            thread = threading.Thread(
                target=self._postrun_daemon_main,
                name=f"runner-{self.worker_id}-postrun",
                daemon=True,
            )
            self._postrun_thread = thread
            thread.start()

    def _postrun_daemon_main(self) -> None:
        """Drain all visible work, then exit until the next enqueue wakes a worker."""
        while not self._postrun_stop.is_set():
            self._postrun_wake.clear()
            self._drain_postrun_queue()
            with self._postrun_thread_lock:
                if self._postrun_wake.is_set() and not self._postrun_stop.is_set():
                    continue
                self._postrun_thread = None
                return
        with self._postrun_thread_lock:
            self._postrun_thread = None

    def _read_postrun_marker_offsets(self, path: Path, *, claimed: bool = False) -> set[int]:
        if not path.is_file():
            return set()
        offsets: set[int] = set()
        try:
            with path.open("r", encoding="utf-8") as handle:
                for line in handle:
                    try:
                        marker = json.loads(line)
                        if claimed:
                            if marker.get("claimed") is True:
                                offsets.add(int(marker["offset"]))
                        elif marker.get("processed") is True:
                            offsets.add(int(marker["offset"]))
                    except (AttributeError, KeyError, TypeError, ValueError, json.JSONDecodeError):
                        LOG.warning(
                            "runner %s skipped malformed post-run marker in %s",
                            self.worker_id,
                            path,
                        )
        except OSError:
            LOG.exception(
                "runner %s could not read post-run markers from %s",
                self.worker_id,
                path,
            )
        return offsets

    def _completed_postrun_offsets(self) -> set[int]:
        return self._read_postrun_marker_offsets(self._postrun_done_path())

    def _claimed_postrun_offsets(self) -> set[int]:
        return self._read_postrun_marker_offsets(self._postrun_claim_path(), claimed=True)

    def _claim_file_for_offset(self, offset: int) -> Path:
        return self._postrun_queue_dir() / f"claim-{offset}.lock"

    def _try_claim_offset(self, offset: int) -> bool:
        """Atomically claim one queue offset across threads and processes.

        ``fcntl.flock`` is process-scoped, so two Runner instances in one process
        (or two threads) can both hold an exclusive flock. ``O_EXCL`` create is
        atomic at the filesystem and is the real cross-worker claim fence.
        """
        claim_file = self._claim_file_for_offset(offset)
        claim_file.parent.mkdir(parents=True, exist_ok=True)
        try:
            fd = os.open(str(claim_file), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            return False
        try:
            os.write(fd, f"{self.worker_id}\n{utc_now_iso()}\n".encode())
        finally:
            os.close(fd)
        self._append_postrun_line(
            self._postrun_claim_path(),
            {
                "offset": offset,
                "claimed": True,
                "worker_id": self.worker_id,
                "claimed_at": utc_now_iso(),
            },
        )
        return True

    def _mark_postrun_done(self, offset: int, status: str) -> None:
        self._append_postrun_line(
            self._postrun_done_path(),
            {
                "offset": offset,
                "processed": True,
                "status": status,
                "worker_id": self.worker_id,
                "completed_at": utc_now_iso(),
            },
        )
        claim_file = self._claim_file_for_offset(offset)
        try:
            claim_file.unlink(missing_ok=True)
        except OSError:
            pass

    def _read_postrun_claims_detail(self) -> dict[int, dict[str, Any]]:
        path = self._postrun_claim_path()
        if not path.is_file():
            return {}
        claims: dict[int, dict[str, Any]] = {}
        try:
            with path.open("r", encoding="utf-8") as handle:
                for line in handle:
                    try:
                        marker = json.loads(line)
                        if marker.get("claimed") is True and "offset" in marker:
                            claims[int(marker["offset"])] = marker
                    except (AttributeError, KeyError, TypeError, ValueError, json.JSONDecodeError):
                        continue
        except OSError:
            pass
        return claims

    def _recover_crashed_claims(self) -> None:
        """Recover claimed offsets that were never completed and passed claim TTL (M-43)."""
        completed = self._completed_postrun_offsets()
        claims_detail = self._read_postrun_claims_detail()
        if not claims_detail:
            return
        now_dt = datetime.now(UTC)
        claim_ttl_s = float(self.stale_s)

        for offset, detail in claims_detail.items():
            if offset in completed:
                continue
            claimed_at_str = detail.get("claimed_at")
            is_expired = False
            if claimed_at_str:
                try:
                    claimed_dt = datetime.fromisoformat(str(claimed_at_str).replace("Z", "+00:00"))
                    if (now_dt - claimed_dt).total_seconds() >= claim_ttl_s:
                        is_expired = True
                except ValueError:
                    is_expired = True
            else:
                is_expired = True

            claim_file = self._claim_file_for_offset(offset)
            if not is_expired and claim_file.is_file():
                try:
                    file_age = time.time() - claim_file.stat().st_mtime
                    if file_age >= claim_ttl_s:
                        is_expired = True
                except OSError:
                    pass

            if is_expired:
                LOG.warning(
                    "runner %s recovering crashed claim for offset %s (status=abandoned)",
                    self.worker_id,
                    offset,
                )
                self._mark_postrun_done(offset, "abandoned")
                completed.add(offset)

    def _claim_next_postrun_job(self) -> tuple[int, str, str] | None:
        """Atomically claim one unprocessed job owned by this database.

        Returns ``(offset, run_id, kind)`` or ``None`` when the queue is empty
        for this store. The claim is durable so a crash after claim can still
        mark the offset done without a second worker re-executing it.
        """
        queue_path = self._postrun_queue_path()
        if not queue_path.is_file():
            return None
        db_id = self._store_identity()
        with self._postrun_cross_process_lock():
            self._recover_crashed_claims()
            completed = self._completed_postrun_offsets()
            claims_detail = self._read_postrun_claims_detail()
            claimed_offsets = set(claims_detail.keys())
            try:
                with queue_path.open("rb") as handle:
                    while True:
                        offset = handle.tell()
                        raw_line = handle.readline()
                        if not raw_line:
                            self._maybe_compact_postrun_queue(completed)
                            return None
                        if offset in completed or offset in claimed_offsets:
                            continue
                        # Filesystem claim fence (thread- and process-safe).
                        claim_file = self._claim_file_for_offset(offset)
                        if claim_file.exists():
                            # Recover orphan or stale claim files without silent work loss
                            is_orphan = offset not in claimed_offsets
                            is_stale_lock = False
                            try:
                                if (time.time() - claim_file.stat().st_mtime) >= float(
                                    self.stale_s
                                ):
                                    is_stale_lock = True
                            except OSError:
                                pass
                            if is_orphan or is_stale_lock:
                                LOG.warning(
                                    "runner %s unlinking orphan/stale claim file for offset %s",
                                    self.worker_id,
                                    offset,
                                )
                                try:
                                    claim_file.unlink(missing_ok=True)
                                except OSError:
                                    pass
                            else:
                                continue
                        try:
                            job = json.loads(raw_line)
                            if not isinstance(job, dict):
                                raise ValueError("job must be a JSON object")
                            run_id = str(job["run_id"])
                            kind = str(job["kind"])
                            if not run_id or not kind:
                                raise ValueError("job must have non-empty run_id and kind")
                            job_db = job.get("db")
                            # Jobs stamped for another store must not be claimed.
                            if job_db is not None and str(job_db) != db_id:
                                self._mark_postrun_done(offset, "foreign_db")
                                completed.add(offset)
                                continue
                        except (
                            KeyError,
                            TypeError,
                            ValueError,
                            UnicodeDecodeError,
                            json.JSONDecodeError,
                        ) as exc:
                            LOG.warning(
                                "runner %s skipped malformed post-run queue line at "
                                "offset %s in %s: %s",
                                self.worker_id,
                                offset,
                                queue_path,
                                exc,
                            )
                            self._mark_postrun_done(offset, "skipped")
                            completed.add(offset)
                            continue
                        if not self._try_claim_offset(offset):
                            continue
                        return offset, run_id, kind
            except OSError:
                LOG.exception(
                    "runner %s could not claim from post-run queue %s",
                    self.worker_id,
                    queue_path,
                )
                return None

    def _outstanding_claims(self, completed: set[int]) -> set[int]:
        """Return offsets that are claimed but not yet marked done."""
        claimed = set(self._read_postrun_claims_detail().keys())
        return claimed - completed

    def _maybe_compact_postrun_queue(self, completed: set[int]) -> None:
        """Drop drained prefix lines and bound done/claim marker retention.

        Compaction is deferred while any claims are outstanding to avoid remapping
        offsets that a concurrent worker still references (M-43). This is safe
        because the daemon always marks done after executing, so outstanding claims
        eventually clear themselves.
        """
        if len(completed) < _POSTRUN_RETENTION:
            return
        # Never compact while claims are in-flight: the claimant uses the original
        # offset to mark done, and remapping would orphan that marker.
        outstanding = self._outstanding_claims(completed)
        if outstanding:
            LOG.debug(
                "runner %s deferring compaction: %d outstanding claims",
                self.worker_id,
                len(outstanding),
            )
            return
        queue_path = self._postrun_queue_path()
        if not queue_path.is_file():
            return
        try:
            kept_lines: list[bytes] = []
            kept_offsets: dict[int, int] = {}  # old_offset -> new_offset
            removed_offsets: set[int] = set()
            with queue_path.open("rb") as handle:
                while True:
                    old_offset = handle.tell()
                    raw = handle.readline()
                    if not raw:
                        break
                    if old_offset in completed:
                        removed_offsets.add(old_offset)
                        continue
                    new_offset = sum(len(line) for line in kept_lines)
                    kept_offsets[old_offset] = new_offset
                    kept_lines.append(raw)
            tmp_path = queue_path.with_suffix(".jsonl.compact")
            with tmp_path.open("wb") as handle:
                for line in kept_lines:
                    handle.write(line)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp_path, queue_path)
            # Rewrite markers for surviving offsets only; drop the rest.
            for path, _key in (
                (self._postrun_done_path(), "processed"),
                (self._postrun_claim_path(), "claimed"),
            ):
                if not path.is_file():
                    continue
                rewritten: list[dict[str, Any]] = []
                with path.open("r", encoding="utf-8") as handle:
                    for marker_line in handle:
                        try:
                            marker = json.loads(marker_line)
                            old = int(marker["offset"])
                        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
                            continue
                        if old not in kept_offsets:
                            continue
                        marker["offset"] = kept_offsets[old]
                        rewritten.append(marker)
                # Bound retention even among survivors.
                if len(rewritten) > _POSTRUN_RETENTION:
                    rewritten = rewritten[-_POSTRUN_RETENTION:]
                with path.open("w", encoding="utf-8") as handle:
                    for marker in rewritten:
                        handle.write(json.dumps(marker, separators=(",", ":")) + "\n")
                    handle.flush()
                    os.fsync(handle.fileno())
            # Clean up claim lock files for compacted offsets (bounded cleanup).
            self._cleanup_claim_files(removed_offsets)
        except OSError:
            LOG.exception(
                "runner %s could not compact post-run queue %s",
                self.worker_id,
                queue_path,
            )

    def _cleanup_claim_files(self, offsets: set[int]) -> None:
        """Remove claim-{offset}.lock files for the given offsets."""
        for offset in offsets:
            claim_file = self._claim_file_for_offset(offset)
            try:
                claim_file.unlink(missing_ok=True)
            except OSError:
                # Best effort; a leftover claim file is harmless (offset is gone).
                pass

    def _drain_postrun_queue(self) -> None:
        while not self._postrun_stop.is_set():
            claimed = self._claim_next_postrun_job()
            if claimed is None:
                return
            offset, run_id, kind = claimed
            status = "processed"
            try:
                self._execute_postrun_job(run_id, kind)
            except Exception:
                # Post-run hooks are best-effort and intentionally have no retry
                # policy. A crash after claim but before this marker can leave the
                # offset claimed-and-undone; that is preferred to double-execution.
                status = "failed"
                LOG.exception(
                    "runner %s post-run job %s for run %s failed",
                    self.worker_id,
                    kind,
                    run_id,
                )
            with self._postrun_cross_process_lock():
                self._mark_postrun_done(offset, status)

    def _execute_postrun_job(self, run_id: str, kind: str) -> None:
        if kind == "run_analysis":
            # Mirror knowledge/runner_hook.py: any failure degrades to no-op so
            # the durable drain never retries or poisons finalization.
            try:
                from omniagentos.reflection.perrun import analyze_run

                raw_db = getattr(self.store, "_db_path", None)
                analyze_run(run_id, db_path=str(raw_db) if raw_db else None)
            except Exception:
                LOG.exception(
                    "runner %s post-run analysis for run %s failed",
                    self.worker_id,
                    run_id,
                )
            return
        if kind != "wiki_update":
            LOG.warning(
                "runner %s skipped unknown post-run job kind %s for run %s",
                self.worker_id,
                kind,
                run_id,
            )
            return
        run = self.store.get_run(run_id)
        if run is None:
            # Ownership: a job whose run is not in THIS database is not ours.
            # Either the run was deleted, or a foreign queue entry slipped through.
            LOG.warning(
                "runner %s skipped post-run wiki update for missing run %s (not in this database)",
                self.worker_id,
                run_id,
            )
            return
        from omniagentos.vault_wiki import maybe_update_wiki

        maybe_update_wiki(
            run,
            self.vault_dir,
            # vault_wiki annotates ``artifacts`` as list[str] but documents (and
            # uses) the raw ``store.get_artifacts`` rows — truthiness only.
            artifacts=self.store.get_artifacts(run_id),  # type: ignore[arg-type]
        )

    def shutdown(self, timeout: float = 5.0) -> None:
        """Best-effort wait for post-run work before allowing worker shutdown."""
        self._postrun_wake.set()
        self._ensure_postrun_daemon()
        with self._postrun_thread_lock:
            thread = self._postrun_thread
        if thread is not None:
            thread.join(timeout=max(0.0, timeout))
        self._postrun_stop.set()
        self._postrun_wake.set()

    def run_forever(self, poll_ms: int = 500, *, once: bool = False) -> None:
        if self.concurrency <= 1:
            # Single-slot loop, unchanged: one synchronous ``tick`` per pass.
            while True:
                did_work = self.tick()
                if once:
                    return
                if not did_work:
                    time.sleep(max(0, poll_ms) / 1000)
            return
        self._run_concurrent(poll_ms, once=once)

    def _run_concurrent(self, poll_ms: int, *, once: bool) -> None:
        """Keep up to ``self.concurrency`` runs executing at once.

        A SINGLE scheduler thread (this one) owns every claim/reclaim/finalize/
        parked-service selection; the pool threads only ever run ``execute_run`` on
        a DISTINCT, already-selected run. Because selection happens on one thread and
        excludes the in-flight set, no run is handed to two slots -- the same
        exactly-once guarantee the single-slot loop had, widened to K slots. Store
        access from the K slots + scheduler is already serialized by the store's
        connection lock (WAL + busy_timeout), and ``claim_next_run``'s BEGIN
        IMMEDIATE still makes a claim atomic across workers AND slots.

        ``once`` fills up to K runnable slots, drains them, and returns.
        """
        poll_s = max(0.0, poll_ms / 1000)
        pool = ThreadPoolExecutor(
            max_workers=self.concurrency,
            thread_name_prefix=f"runner-{self.worker_id}",
        )
        futures: dict[Future[None], str] = {}
        try:
            while True:
                self._reap(futures)
                self._finalize_backoff = False
                self._heartbeat()
                self._requeue_paused()
                # Fill every free slot with a distinct runnable unit.
                while len(futures) < self.concurrency:
                    exclude = self._in_flight_snapshot() | set(futures.values())
                    unit = self._select(exclude)
                    if unit is None:
                        break
                    kind, run_id = unit
                    if kind == "execute" and run_id is not None:
                        self._mark_in_flight(run_id)
                        futures[pool.submit(self.execute_run, run_id)] = run_id
                    # A "done" unit was terminal/park work already applied by _select;
                    # loop again (it changed state) to keep filling slots.
                if once:
                    self._drain(futures)
                    return
                if futures:
                    # Block until a slot frees (or a short poll for housekeeping),
                    # rather than busy-spinning while all K slots are occupied.
                    wait(set(futures), timeout=poll_s or None, return_when=FIRST_COMPLETED)
                else:
                    time.sleep(poll_s)
        finally:
            pool.shutdown(wait=True)

    def _reap(self, futures: dict[Future[None], str]) -> None:
        for future in [f for f in futures if f.done()]:
            futures.pop(future, None)
            exc = future.exception()
            if exc is not None:  # execute_run already isolates run faults; this is belt-and-braces
                LOG.error("worker %s run slot raised %r", self.worker_id, exc)

    def _drain(self, futures: dict[Future[None], str]) -> None:
        if futures:
            wait(set(futures))
        self._reap(futures)

    def tick(self) -> bool:
        """Perform one claim-or-parked-service pass."""
        try:
            return self._tick()
        except (KeyboardInterrupt, SystemExit):
            raise
        except Exception:
            LOG.exception("runner %s isolated an unscoped tick fault", self.worker_id)
            return False

    def _tick(self) -> bool:
        """Perform one synchronous pass: select one unit and execute it inline.

        This is the single-slot path every test drives. ``_select`` does the
        durable selection; here nothing is ever in-flight during selection (the
        unit executes to completion before we return), so the exclusion set is
        empty and behavior is byte-for-byte the pre-concurrency loop.
        """
        self._finalize_backoff = False
        self._heartbeat()
        self._requeue_paused()
        unit = self._select(frozenset())
        if unit is None:
            return False
        kind, run_id = unit
        if kind == "execute" and run_id is not None:
            self.execute_run(run_id)
        return not self._finalize_backoff

    def _select(self, exclude: frozenset[str] | set[str]) -> tuple[str, str | None] | None:
        """Choose the next unit of work, never returning a run in ``exclude``.

        Returns ``("execute", run_id)`` for a run the caller must execute (already
        claimed/owned/transitioned to a runnable state), ``("done", None)`` when a
        terminal/park decision was applied inline (no run to execute), or ``None``
        when there is nothing to do. Runs ONLY on the scheduler thread, so excluding
        the in-flight set is sufficient to keep any run from reaching two slots.

        The six-branch structure below is UNCHANGED; Phase 3 adds one gate
        (``_scope_admit``) at each point a branch hands a run back. ``scope_on`` is
        resolved ONCE per pass so that with the feature off — the default — the
        whole addition costs one env/config read and every gate is a single
        boolean test that returns the same run this method has always returned.
        """
        scope_on = self._scope.enabled()
        parked = self.store.list_runs(
            {"state": RunState.AWAITING_APPROVAL.value, "worker_id": self.worker_id}, 100
        )
        for run in reversed(parked):
            if str(run["id"]) in exclude:
                continue
            outcome = self._service_parked(run)
            if isinstance(outcome, str):
                # An approval cleared, so this run is RUNNING again. Its lease very
                # probably lapsed while a human was deciding (nobody renews a parked
                # run), so it must re-acquire before it executes, not assume.
                verdict = self._scope_admit(run, scope_on, resume=True)
                if verdict == "halt":
                    return None
                if verdict == "skip":
                    continue
                return ("execute", outcome)
            if outcome:
                return ("done", None)

        # A stable worker id must resume work it owned when the process restarted --
        # skipping runs already executing in another slot.
        owned = self.store.list_runs(
            {"state": RunState.RUNNING.value, "worker_id": self.worker_id}, 100
        ) + self.store.list_runs(
            {"state": RunState.VALIDATING.value, "worker_id": self.worker_id}, 100
        )
        owned = [run for run in owned if str(run["id"]) not in exclude]
        # These are runs this worker BELIEVES it owns. After a process restart that
        # belief covers the run row but not the locks: a lease lapses after
        # ``scope_ttl_s`` with nobody renewing it, and somebody else may legitimately
        # hold those paths now. Re-acquire before executing, and skip rather than
        # execute unarbitrated. ``reversed`` + return-on-first preserves the old
        # ``owned[-1]`` pick exactly when the feature is off.
        for run in reversed(owned):
            verdict = self._scope_admit(run, scope_on, resume=True)
            if verdict == "halt":
                return None
            if verdict == "skip":
                continue
            return ("execute", str(run["id"]))

        # Complete finalization only for owners that are no longer heartbeating.
        heartbeat_by_worker: dict[str, dict[str, Any]] = {}
        liveness_available = False
        try:
            heartbeat_by_worker = {
                str(row["worker_id"]): row for row in self.store.get_heartbeats()
            }
            liveness_available = True
        except Exception:
            LOG.exception("runner %s could not inspect finalization liveness", self.worker_id)
        cutoff = (datetime.now(UTC) - timedelta(seconds=max(0, self.stale_s))).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
        for state in TERMINAL_RUN_STATES:
            try:
                # TODO(store): a dedicated unfinished-terminal query could avoid
                # relying on the generic nullable filter and descending run order.
                unfinished = self.store.list_runs(
                    {"state": state.value, "vault_note_path": None}, 100
                )
                for candidate in reversed(unfinished):
                    run_id = str(candidate["id"])
                    # A run transiently terminal-but-unfinalized inside its own slot
                    # (between _transition and _finalize) must not be re-dispatched.
                    if run_id in exclude:
                        continue
                    owner_raw = candidate.get("worker_id")
                    owner = str(owner_raw) if owner_raw is not None else None
                    heartbeat = heartbeat_by_worker.get(owner) if owner is not None else None
                    eligible = owner == self.worker_id or (
                        liveness_available
                        and (heartbeat is None or str(heartbeat["last_beat_at"]) < cutoff)
                    )
                    if not eligible:
                        continue
                    if self.store.update_run(
                        run_id,
                        {"worker_id": self.worker_id},
                        expect_worker=owner,
                    ):
                        # Adoption keeps the HOLDER identity (the run), so this is a
                        # resume: replace-semantics hand the dead owner's scope to
                        # this worker under a bumped generation, which is also what
                        # fences the dead owner if it turns out to be merely wedged.
                        verdict = self._scope_admit(candidate, scope_on, resume=True)
                        if verdict == "halt":
                            return None
                        if verdict == "skip":
                            continue
                        return ("execute", run_id)
            except (KeyboardInterrupt, SystemExit):
                raise
            except Exception:
                LOG.exception(
                    "runner %s isolated the %s finalization scan",
                    self.worker_id,
                    state.value,
                )
                continue

        reclaimed = self.store.reclaim_stale_runs(self.worker_id, self.stale_s)
        for run in reclaimed:
            self._event(Events.AUDIT, "reclaimed", run, {"worker_id": self.worker_id})
        reclaimed = [run for run in reclaimed if str(run["id"]) not in exclude]
        # Same shape as the adoption branch above: reclaiming a stale run takes over
        # the run's identity, so the scope comes with it under a bumped generation.
        for run in reclaimed:
            verdict = self._scope_admit(run, scope_on, resume=True)
            if verdict == "halt":
                return None
            if verdict == "skip":
                continue
            if run["state"] == RunState.AWAITING_APPROVAL.value:
                outcome = self._service_parked(run)
                if isinstance(outcome, str):
                    return ("execute", outcome)
                return ("done", None)
            return ("execute", str(run["id"]))

        if bool(self.store.get_pause()["paused"]):
            return None
        claimed = self.store.claim_next_run(self.worker_id)
        if claimed is None:
            return None
        run_id = str(claimed["id"])
        self._project(claimed, RunState.RUNNING)
        self._event(Events.RUN_UPDATED, "running", claimed)
        # THE CLAIM PATH. A fresh claim, so try_acquire (not reacquire).
        #
        # A claimed-but-refused run stays RUNNING and owned by this worker, and this
        # method returns None for the pass. That is deliberate: RUNNING -> QUEUED is
        # not a legal run transition (contracts.RUN_TRANSITIONS), so there is no way
        # to hand it back, and inventing one would race every other worker for it.
        # The owned branch above retries it — oldest first, so the FIFO position it
        # was claimed in is exactly the order it is retried in — and skips past it to
        # other realms' work in the meantime. The cost is that under enforce mode with
        # real contention, `state='running'` over-reports for as long as a run waits;
        # a first-class blocked projection is the honest fix and needs a state-machine
        # change this work package is not allowed to make.
        verdict = self._scope_admit(claimed, scope_on, resume=False)
        if verdict != "go":
            return None
        return ("execute", run_id)

    def _scope_admit(self, run: dict[str, Any], scope_on: bool, *, resume: bool) -> str:
        """Path-ownership gate for one selected run: ``go`` / ``skip`` / ``halt``.

        ``halt`` is the anti-starvation break. Skipping a blocked run is what keeps
        one contended realm from idling a whole worker, but it also turns strict
        FIFO into skip-the-blocked-head — so once a run's DURABLE waiter row is
        older than ``OMNIAGENTOS_SCOPE_STARVATION_S`` (default 300) the lane stops
        taking other work entirely, and the starving head gets its scope the moment
        the blocker releases.
        """
        if not scope_on:
            return "go"
        decision = self._scope.take(run, resume=resume)
        if decision.report:
            self._scope_event(run, decision)
        if decision.proceed:
            return "go"
        return "halt" if decision.starved else "skip"

    def _scope_event(self, run: dict[str, Any], decision: ScopeDecision) -> None:
        """Record one scope decision. Best-effort: telemetry never fails selection.

        Emitted only when :class:`ScopeDecision` reports NEW information — a run
        that stays blocked for a thousand passes produces one event, not a thousand.
        """
        try:
            self._event(
                Events.AUDIT,
                f"scope_{decision.status}",
                run,
                {
                    "realm": decision.realm,
                    "generation": decision.generation,
                    "detail": decision.detail,
                    "blocked_on": decision.blocked_on,
                    "blocked_path": decision.blocked_path,
                    "waited_s": round(decision.waited_s, 3),
                    "starved": decision.starved,
                    "shadowed": decision.shadowed,
                },
            )
        except Exception:  # noqa: BLE001 -- an unwritable event must not wedge the lane
            LOG.exception(
                "runner %s could not record a scope decision for %s",
                self.worker_id,
                run.get("id"),
            )

    def execute_run(self, run_id: str) -> None:
        # The unit of concurrent work: under K>1 this runs on a pool thread, one
        # DISTINCT run per slot. Marking the run in-flight for its whole lifetime is
        # what keeps the scheduler's next selection from handing it to another slot.
        self._mark_in_flight(run_id)
        try:
            self._execute_run(run_id)
        except LostFence:
            LOG.info("worker %s lost fence for run %s", self.worker_id, run_id)
        except (KeyboardInterrupt, SystemExit):
            raise
        except Exception as exc:
            LOG.exception("worker %s isolated a fault in run %s", self.worker_id, run_id)
            self._isolate_fault(run_id, exc)
        finally:
            self._clear_in_flight(run_id)
            # The crash net for path ownership. ``_transition`` releases on the
            # ordinary terminal path; this catches every other way a run can end --
            # an isolated fault, a quarantined finalization, a raise out of
            # ``_transition`` itself, a lost fence. It releases ONLY when the run is
            # actually finished (or no longer ours): a run that merely parked for an
            # approval keeps its claim until the lease lapses, and the resume path
            # re-acquires. No-op when no lease is held.
            self._scope.release_if_terminal(run_id)
            try:
                # Report a still-running slot (or None) so the heartbeat's
                # current_run_id stays meaningful while other slots keep working.
                self.store.upsert_heartbeat(self.worker_id, self.pid, self._heartbeat_run_id())
            except Exception:
                LOG.exception("worker %s could not clear its active heartbeat", self.worker_id)

    def _isolate_fault(self, run_id: str, exc: Exception) -> None:
        """Record a run fault without recursively invoking transition/finalization code."""
        error = f"runner_fault:{type(exc).__name__}:{exc}"
        try:
            run = self.store.get_run(run_id)
            if run is None or run.get("worker_id") != self.worker_id:
                return
            state = RunState(str(run["state"]))
            if state not in TERMINAL_RUN_STATES:
                now = utc_now_iso()
                if not self.store.update_run(
                    run_id,
                    {
                        "state": RunState.FAILED.value,
                        "error": error,
                        "finished_at": now,
                        "updated_at": now,
                    },
                    expect_worker=self.worker_id,
                ):
                    return
                run = self.store.get_run(run_id) or run
            self._event(
                Events.AUDIT,
                "runner_fault_isolated",
                run,
                {"error": error, "terminal": state in TERMINAL_RUN_STATES},
            )
        except Exception:
            LOG.exception(
                "worker %s could not persist isolated fault for %s", self.worker_id, run_id
            )

    def _execute_run(self, run_id: str) -> None:
        run = self._owned_run(run_id)
        if RunState(str(run["state"])) in TERMINAL_RUN_STATES:
            self._finalize(run_id)
            return
        plan = _array(run["plan_json"])
        saved = {int(row["seq"]): row for row in self.store.get_steps(run_id)}
        context: dict[str, Any] = {}

        for seq, step in enumerate(plan):
            self.store.upsert_heartbeat(self.worker_id, self.pid, run_id)
            previous = saved.get(seq)
            if previous and previous["status"] in {
                StepStatus.COMPLETED.value,
                StepStatus.SKIPPED.value,
            }:
                if previous.get("result_json"):
                    context[str(step.get("name", seq))] = json.loads(previous["result_json"])
                continue

            run = self._owned_run(run_id)
            state = RunState(str(run["state"]))
            if state not in {RunState.RUNNING, RunState.VALIDATING}:
                return
            if bool(self.store.get_pause()["paused"]):
                if can_transition_run(state, RunState.PAUSED):
                    self._transition(run, RunState.PAUSED)
                    return
                # VALIDATING cannot park; finish its validate group and stop at
                # its legal terminal boundary rather than violate RUN_TRANSITIONS.
            if bool(run["cancel_requested"]):
                if can_transition_run(state, RunState.CANCELLED):
                    self._cancel(run)
                    return

            kind = str(step.get("kind", ""))
            if kind not in {"agent", "effect", "validate"}:
                self._fail(run, f"unknown_step_kind:{kind}")
                return
            effective_action = self._effective_action_class(step, self._workspace_path(run_id))
            decision = self.dependencies.evaluate_policy(effective_action)
            if (decision.requires_approval or decision.always_human) and not self._approval_allows(
                run, seq, step, effective_action
            ):
                return

            if not self._budget_allows(run):
                # Advisory by default: record + surface the overshoot, keep going.
                # Failing a run mid-plan on a cost line strands completed steps and
                # leaves the work half-done (omniagentos.budget.policy).
                self._escalate("cap_hit", run, "budget_exceeded")
                if budget_blocks():
                    self._fail(run, "budget_exceeded")
                    return

            # Once a validate group has finished, return to RUNNING before the next
            # non-validate step so later approval parks and agent work use legal
            # transitions (VALIDATING may no longer be a terminal-only phase).
            if kind != "validate" and state == RunState.VALIDATING:
                self._transition(run, RunState.RUNNING)
                run = self._owned_run(run_id)
                state = RunState.RUNNING

            if kind == "validate" and state == RunState.RUNNING:
                self._transition(run, RunState.VALIDATING)
                run = self._owned_run(run_id)

            params = _object(step.get("params", {}))
            retries = max(0, int(params.get("retries", 0)))
            original_failure: str | None = None
            for attempt in range(retries + 1):
                if attempt:
                    run = self._owned_run(run_id)
                    if not self._budget_allows(run):
                        self._escalate("cap_hit", run, "budget_exceeded")
                        if budget_blocks():
                            self._fail(run, "budget_exceeded")
                            return
                self._checkpoint_before(run, seq, step, context, attempt)
                try:
                    with self._heartbeat_during_step(run_id):
                        outcome = self._execute_step(run, seq, step, context, attempt=attempt)
                    self._record_usage(run_id, outcome.usage)
                    status = StepStatus.SKIPPED if outcome.skipped else StepStatus.COMPLETED
                    self._checkpoint_after(run_id, seq, step, status, outcome.result)
                    context[str(step.get("name", seq))] = outcome.result
                    break
                except LostFence:
                    raise
                except (KeyboardInterrupt, SystemExit):
                    raise
                except Exception as exc:
                    error = str(exc)
                    if original_failure is None:
                        original_failure = error
                    self._checkpoint_failure(run_id, seq, step, error)
                    if attempt == retries:
                        current = self._owned_run(run_id)
                        # Preserve the original failure when later retries only hit
                        # secondary integrity errors (e.g. incomplete idempotency).
                        self._fail(current, original_failure or error)
                        return

        run = self._owned_run(run_id)
        if bool(run["cancel_requested"]):
            self._cancel(run)
        else:
            self._transition(run, RunState.COMPLETED)

    def _owned_run(self, run_id: str) -> dict[str, Any]:
        run = self.store.get_run(run_id)
        if run is None or run.get("worker_id") != self.worker_id:
            raise LostFence(run_id)
        return run

    def _effective_action_class(
        self, step: dict[str, Any], project_dir: str | None = None
    ) -> ActionClass:
        """Raise a plan's declaration to the minimum class implied by its behavior."""
        try:
            declared = ActionClass(
                str(step.get("action_class", ActionClass.SANDBOXED_CREATION.value))
            )
        except ValueError:
            declared = ActionClass.CONSEQUENTIAL
        derived = declared
        kind = str(step.get("kind", ""))
        params = _object(step.get("params", {}))
        if kind == "validate":
            derived = self._command_action_class(params.get("command"), project_dir)
        elif kind == "effect":
            effect = str(params.get("effect", ""))
            if effect == "noop":
                derived = ActionClass.SANDBOXED_CREATION
            elif effect == "append_file":
                # append_file is ALWAYS confined to the trusted per-run workspace
                # (params.working_dir is no longer honored), so a within-sandbox
                # write is internal_reversible (auto); confinement — not an approval
                # prompt — is the control against escape (council R2 governance).
                derived = ActionClass.INTERNAL_REVERSIBLE
            else:
                derived = ActionClass.CONSEQUENTIAL
            if params.get("probe"):
                probe_class = self._command_action_class(params.get("probe"), project_dir)
                if _ACTION_CLASS_RANK[probe_class] > _ACTION_CLASS_RANK[derived]:
                    derived = probe_class
        return max((declared, derived), key=_ACTION_CLASS_RANK.__getitem__)

    @staticmethod
    def _command_action_class(command: Any, project_dir: str | None = None) -> ActionClass:
        """Effective action class for a runner-executed command.

        Delegates to the SINGLE shared shell classifier (``classify_shell``) that
        the Session Bridge also uses, so the two gates can never disagree. It is
        deny-by-default: interpreters, deletes, out-of-scope writes and anything
        not provably read-only classify IRREVERSIBLE (hard-stop in AUTO mode). The
        OS sandbox in ``_run_command`` is the physical backstop underneath it."""
        return classify_shell(command, project_dir)

    def _abort_active_step_processes(self, run_id: str) -> None:
        with self._active_processes_lock:
            proc = self._active_processes.get(run_id)
        if proc is not None:
            self._kill_process_group(proc)

    def _mark_step_abort(self, run_id: str, reason: str) -> None:
        with self._step_aborts_lock:
            self._step_aborts.setdefault(run_id, reason)

    def _step_abort_reason(self, run_id: str) -> str | None:
        with self._step_aborts_lock:
            return self._step_aborts.get(run_id)

    def _clear_step_abort(self, run_id: str) -> None:
        with self._step_aborts_lock:
            self._step_aborts.pop(run_id, None)

    @contextmanager
    def _heartbeat_during_step(self, run_id: str) -> Iterator[None]:
        """Pulse worker liveness on a real clock until the in-flight step exits.

        The scope lease renews on the SAME pulse. That is the right clock for it:
        the pulse already runs at ``stale_s/4`` (with safety margin) which sits well
        inside the 90s default lease, and tying the two together means a worker
        cannot be liveness-alive while its path claims silently lapse. ``renew`` is
        a no-op returning True when no lease is held (the dark path).

        Transient store errors must NOT kill the pulse (H-12): a single
        ``database is locked`` after busy_timeout used to exit forever, after which
        a peer's stale reclaim adopted a step still running in this process.
        Persistent consecutive failures mark the step for abort and durably fence in DB
        so the owner stops and fences the effect before adoption can duplicate it.
        """
        stop = threading.Event()
        interval_s = max(0.5, self.stale_s / 4.0)
        self._clear_step_abort(run_id)

        def pulse() -> None:
            consecutive_failures = 0
            while not stop.wait(interval_s):
                # Keep renewing until the step context exits. Abort marks the
                # owner to fence, but MUST NOT drop liveness while the effect is
                # still running — otherwise a peer can adopt mid-step (H-12).
                try:
                    self.store.upsert_heartbeat(self.worker_id, self.pid, run_id)
                    self._scope.renew(run_id)
                    consecutive_failures = 0
                except Exception:
                    consecutive_failures += 1
                    LOG.exception(
                        "worker %s could not refresh heartbeat during run %s (failure %s/%s)",
                        self.worker_id,
                        run_id,
                        consecutive_failures,
                        _HEARTBEAT_PERSISTENT_FAILURES,
                    )
                    if consecutive_failures >= _HEARTBEAT_PERSISTENT_FAILURES:
                        # Fence the active effect; keep looping so a recovered
                        # store can hold the lease until the owner stops.
                        self._mark_step_abort(run_id, "heartbeat_persistent_failure")
                        self._abort_active_step_processes(run_id)
                        try:
                            self.store.update_run(
                                run_id,
                                {
                                    "state": RunState.FAILED.value,
                                    "error": "heartbeat_persistent_failure",
                                    "finished_at": utc_now_iso(),
                                    "updated_at": utc_now_iso(),
                                },
                                expect_worker=self.worker_id,
                            )
                        except Exception:
                            LOG.exception(
                                "worker %s could not durably fence aborted run %s",
                                self.worker_id,
                                run_id,
                            )
                    # Transient or persistent: never exit the pulse early.
                    # stop is the only legitimate exit (step body finished).

        thread = threading.Thread(
            target=pulse,
            name=f"heartbeat-{self.worker_id}-{run_id}",
            daemon=True,
        )
        thread.start()
        try:
            yield
            # If the pulse marked a persistent failure while we were inside the
            # step body, fence here before the caller can checkpoint success.
            reason = self._step_abort_reason(run_id)
            if reason is not None:
                raise StepFailure(reason)
        finally:
            stop.set()
            thread.join(timeout=max(1.0, interval_s + 1.0))
            self._clear_step_abort(run_id)

    def _assert_fence(self, run_id: str) -> None:
        """Two predicates now: still MY run, and still MY lease generation.

        ``worker_id`` alone cannot see a displacement that leaves the worker id
        intact — an adopter that is this same worker restarted, or a wedged worker
        whose run was adopted and handed back. ``runs.lease_generation`` (migration
        059, the shape 052 gave ``swarm_runs``) is bumped by whoever takes the run,
        so a displaced-but-alive worker fails here exactly like a displaced swarm
        coordinator does, instead of writing next to its adopter.

        With scope locks off the second predicate costs one dict lookup on an empty
        dict: no generation is ever recorded, so ``fence_ok`` is True immediately.

        A persistent heartbeat abort is also a fence failure: the owner must stop
        the active effect before a peer can adopt it (H-12).
        """
        reason = self._step_abort_reason(run_id)
        if reason is not None:
            raise StepFailure(reason)
        if not self.store.update_run(run_id, {}, expect_worker=self.worker_id):
            raise LostFence(run_id)
        if not self._scope.fence_ok(run_id):
            raise LostFence(run_id)

    def _checkpoint_before(
        self,
        run: dict[str, Any],
        seq: int,
        step: dict[str, Any],
        context: dict[str, Any],
        attempt: int,
    ) -> None:
        fields = {
            "name": str(step.get("name", f"step-{seq}")),
            "action_class": str(step.get("action_class", ActionClass.SANDBOXED_CREATION.value)),
            "status": StepStatus.STARTED.value,
            "checkpoint_json": _json({"context": context, "attempt": attempt}),
            "error": None,
            "started_at": utc_now_iso(),
            "finished_at": None,
        }
        if str(step.get("kind")) == "effect":
            fields["idempotency_key"] = self._idempotency_key(run, seq, step)
        if not self.store.upsert_step(str(run["id"]), seq, fields, expect_worker=self.worker_id):
            raise LostFence(str(run["id"]))
        self._event(Events.STEP_UPDATED, "started", run, {"seq": seq})

    def _checkpoint_after(
        self,
        run_id: str,
        seq: int,
        step: dict[str, Any],
        status: StepStatus,
        result: dict[str, Any],
    ) -> None:
        if not self.store.upsert_step(
            run_id,
            seq,
            {
                "name": str(step.get("name", f"step-{seq}")),
                "status": status.value,
                "result_json": _json(result),
                "error": None,
                "finished_at": utc_now_iso(),
            },
            expect_worker=self.worker_id,
        ):
            raise LostFence(run_id)
        self._event(
            Events.STEP_UPDATED,
            status.value,
            self._owned_run(run_id),
            {"seq": seq},
        )

    def _checkpoint_failure(self, run_id: str, seq: int, step: dict[str, Any], error: str) -> None:
        if not self.store.upsert_step(
            run_id,
            seq,
            {
                "name": str(step.get("name", f"step-{seq}")),
                "status": StepStatus.FAILED.value,
                "error": error,
                "finished_at": utc_now_iso(),
            },
            expect_worker=self.worker_id,
        ):
            raise LostFence(run_id)
        self._event(Events.STEP_UPDATED, "failed", self._owned_run(run_id), {"seq": seq})

    def _execute_step(
        self,
        run: dict[str, Any],
        seq: int,
        step: dict[str, Any],
        context: dict[str, Any],
        *,
        attempt: int = 0,
    ) -> StepOutcome:
        kind = str(step["kind"])
        if kind == "effect":
            return self._execute_effect(run, seq, step, attempt=attempt)
        if kind == "validate":
            return self._execute_validate(run, step)
        return self._execute_agent(run, step, context)

    def _execute_agent(
        self, run: dict[str, Any], step: dict[str, Any], context: dict[str, Any]
    ) -> StepOutcome:
        params = _object(step.get("params", {}))
        # FAIL-CLOSED ARMING RE-CHECK, before any other work in this method: a
        # step dispatched while real-harness execution was armed must not run in
        # a runner whose operator has since disarmed it. Only steps intake armed
        # carry this marker, so with the switch off everywhere no step reaches
        # this branch and behavior is unchanged.
        if params.get("real_harness") and not real_harness_enabled():
            raise StepFailure(
                "real_harness_disabled: this step was dispatched with real-harness "
                f"execution armed, but {REAL_HARNESS_ENV} is not set for this runner; "
                "refusing to execute"
            )
        task, task_input, tools = self._task_context(run, params)
        harness = HarnessType(str(params.get("adapter", run["harness"])))
        # Drive Access for projects (W4) seam: `sandbox` (below) is the CLI-level
        # read_only/workspace_write mode a future filesystem/shell guardrail
        # (runner/sandbox.py, policy/** -- under review elsewhere as of this
        # writing, intentionally not edited here) will enforce against. That
        # guardrail's write-scope + shell-scope MUST derive from the SAME
        # project root_dirs/allowed_dirs this method reads via
        # `_project_extra_dirs` below (also surfaced on
        # AgentInput.metadata["extra_dirs"]) -- one source of truth for "what
        # this run may touch," including any granted Drive subfolder.
        sandbox = self.dependencies.sandbox_for_tools(harness, tools)
        working_dir = params.get("working_dir", task_input.get("working_dir"))
        # AC-policy fix7: a None/empty working_dir defaults to the runner-assigned
        # per-run workspace, NEVER the adapter's os.getcwd() fallback (the repo
        # root). Resolve it HERE so the same in-scope value flows into both the
        # scope assertion and AgentInput.working_dir — otherwise an unscoped step
        # would run with the repository as its writable dir (and could overwrite
        # configs/policy.yaml).
        if working_dir is None or not str(working_dir).strip():
            working_dir = self._workspace_path(str(run["id"]))
        # SECURITY (control-plane create_run escape): validate working_dir scope
        # BEFORE it becomes AgentInput.working_dir -> a sandbox write-root
        # (runner/sandbox.py). Without this, a plan/task-authored working_dir of
        # e.g. ~/.ssh made that dir writable and a sandboxed_creation step (auto in
        # AUTO mode) could write outside every grant. Reject an out-of-scope dir
        # here so the step fails rather than silently running.
        self._assert_working_dir_in_scope(run, task, working_dir)
        # Drive-under-full-auto wiring: the granted external dirs (project
        # root_dirs/allowed_dirs, incl. any granted Drive subfolder) computed below
        # via ``_project_extra_dirs`` are the SAME source of truth the OS sandbox
        # must widen its write-scope to. They are surfaced on
        # ``metadata["extra_dirs"]`` (for the adapter ``--add-dir`` flags) AND read
        # by the adapter's ``_sandboxed_launch`` to add them as sandbox write roots,
        # so under full-auto a Drive-scoped sub-CLI can write its granted folder
        # while every out-of-scope path still hard-stops. Elevation flags are
        # stripped from model/plan-authored metadata (fail-closed) before it flows
        # to the adapter.
        metadata = _strip_elevation_metadata(_object(params.get("metadata", {})))
        if "mock" in params:
            metadata["mock"] = params["mock"]
        metadata["sandbox"] = sandbox.model_dump(mode="json")
        # Keep metadata["context"] for compatibility with adapters/tests that read it,
        # and also materialize prior-step results into the prompt so production CLI
        # adapters (which only forward the prompt text) actually receive step chaining
        # (L-12). An empty context is a no-op.
        metadata["context"] = context
        metadata["extra_dirs"] = self._project_extra_dirs(task.get("project_id"), working_dir)
        prompt = str(params.get("prompt", task_input.get("prompt", task.get("title", ""))))
        # The operator's raw brief, captured before any recall/memory/context injection
        # so the persisted conversation turn is the clean prompt, not an enriched one.
        base_prompt = prompt
        # Materialize prior-step results into the prompt so production CLI adapters
        # (which only forward prompt text) receive step chaining (L-12).
        prompt = self._inject_plan_step_context(prompt, context)
        run_id = str(run["id"])
        from omniagentos.knowledge import config as knowledge_config

        if knowledge_config.knowledge_enabled() and run_id not in self._recall_state:
            # Mark attempted before entering the never-raising wrapper so retries and
            # later plan steps cannot re-embed or reinforce this run.
            self._recall_state[run_id] = False
            from omniagentos.knowledge.recall import (
                last_recall_metadata,
                safe_recall_block,
            )

            block = safe_recall_block(
                prompt=prompt,
                discipline=run.get("discipline_id"),
                agent_id=run.get("agent"),
                run_id=run_id,
            )
            if block:
                prompt = f"{block}\n\n{prompt}"
                self._recall_state[run_id] = True
                metadata["knowledge_recall"] = last_recall_metadata(run_id)
            else:
                # A pathological over-budget fact can produce a recall_log row without
                # a renderable block. Preserve that distinction for record_helped().
                self._recall_state[run_id] = (
                    last_recall_metadata(run_id).get("recall_id") is not None
                )
                metadata["knowledge_recall"] = {"status": "unavailable_or_empty"}

        # Mid-run file-search capability hint: stateless and side-effect-free, so
        # unlike recall/memory it needs no per-run guard — every step's prompt gets
        # it (each step launches a fresh CLI process that only sees its own prompt).
        from omniagentos.filesearch.hint import brief_hint, hint_enabled

        # Health digest (U-L2) is prepended FIRST so it ends up LAST among the
        # injected blocks, immediately above the task itself. PLAN.md §4 ranks
        # HEALTH_DIGEST 12th of 14: it is a <=120-token advisory about degraded
        # subsystems, not context the step reasons from, and every later
        # prepend correctly stacks above it. (Phase-2 integration ordering: U-L2
        # and U-C12 both prepend here, and prepend order is inverse render
        # order, so this is the only place the two can be sequenced.)
        try:
            from omniagentos.health.digest import build_health_digest, read_health_snapshot

            health_digest = build_health_digest(read_health_snapshot())
        except Exception:  # noqa: BLE001 - optional health context must not block a run.
            LOG.exception("health digest omitted from runner prompt")
            health_digest = ""
        if health_digest:
            prompt = f"{health_digest}\n\n{prompt}"

        if hint_enabled():
            prompt = f"{brief_hint()}\n\n{prompt}"

        # Skill bodies (U-C12). The runner lane injected ZERO skills before
        # this: `runner/core.py` had no reference to the skill library at all,
        # so a step never saw a playbook the swarm's workers were selecting
        # from. It goes through the SAME resolver as the swarm — one path, one
        # verify-at-read — and differs only in taking a smaller slice of the
        # budget, because a run has many short steps rather than one long
        # worker brief.
        skill_block = self._resolved_skill_block(run, task)
        if skill_block:
            prompt = f"{skill_block}\n\n{prompt}"

        # Memory / "never re-brief" layer: prepend the node's prior conversation +
        # rolling summaries (from the frozen conversations table), then record THIS
        # brief as a user turn so history accrues. Both are never-raising no-ops when
        # the conversations table is absent (pre-migration-031) or memory is disabled.
        from omniagentos.memory import config as memory_config

        if memory_config.memory_enabled() and run_id not in self._memory_state:
            self._memory_state[run_id] = True
            from omniagentos.memory.runner_hook import (
                safe_memory_block,
                safe_persist_user_turn,
            )

            task_id = str(run["task_id"])
            model = params.get("model", run.get("model"))
            # U-C1 made this a (block, AssembledContext) pair. Binding the pair to a
            # single name left `mem_block` an always-truthy tuple, so EVERY prompt was
            # prefixed with a Python tuple repr and the accounting was thrown away.
            # U-L1 additionally threads ``run_id`` so each recalled lesson can be
            # attributed to the precise run that consumed it.
            mem_block, memory_context = safe_memory_block(
                self.store,
                task_id=task_id,
                budget_tokens=memory_config.budget_tokens(),
                task_text=base_prompt,
                run_id=run_id,
                project_id=(str(task.get("project_id")) if task.get("project_id") else None),
            )
            if mem_block:
                prompt = f"{mem_block}\n\n{prompt}"
                metadata["memory_context"] = {
                    "status": "injected",
                    "estimated_tokens": memory_context.estimated_tokens,
                    "truncated": memory_context.truncated,
                    "node_turns": memory_context.node_turns,
                    "ancestor_summaries": memory_context.ancestor_summaries,
                    "recalls": memory_context.recalls,
                    "history_hits": memory_context.history_hits,
                    "has_summary": memory_context.has_summary,
                }
            # Persist AFTER assembling so the current brief is recorded for next time
            # without being duplicated in this run's own prior-context block.
            safe_persist_user_turn(self.store, task_id=task_id, content=base_prompt, model=model)
        adapter = self.dependencies.resolve_adapter(harness)
        self._assert_fence(run_id)
        # SEAM 1 (agent-output redaction). The six adapters build an AgentResult
        # independently, so there is no single CONSTRUCTION site to scrub -- but
        # there is a single RECEIPT site, and this is it. Redacting here is what
        # lets runs.output_text/output_json, runs.error (via the StepFailure
        # below), steps.result_json, the manifest, the ledger line, the vault
        # note, the memory turn and every reviewer prompt inherit redaction from
        # ONE place instead of N per-writer scrubs that review proved incomplete.
        result = scrub_agent_result(
            adapter.run(
                AgentInput(
                    run_id=run_id,
                    task_id=str(run["task_id"]),
                    prompt=prompt,
                    working_dir=working_dir,
                    model=params.get("model", run.get("model")),
                    output_schema=params.get("output_schema"),
                    tools_allowed=tools,
                    budget=BudgetSpec.model_validate(_object(run.get("budget_json", {}))),
                    metadata=metadata,
                )
            )
        )
        self._assert_fence(str(run["id"]))
        if result.status != ResultStatus.OK:
            self._record_usage(str(run["id"]), result.usage)
            raise StepFailure(result.error or result.status.value)
        payload = result.model_dump(mode="json")
        if not self.store.update_run(
            str(run["id"]),
            {
                "output_text": result.output_text,
                "output_json": _json(result.output_json)
                if result.output_json is not None
                else None,
                "session_ref": result.session_ref,
                "updated_at": utc_now_iso(),
            },
            expect_worker=self.worker_id,
        ):
            raise LostFence(str(run["id"]))
        return StepOutcome(payload, usage=result.usage)

    def _resolved_skill_block(self, run: dict[str, Any], task: dict[str, Any]) -> str:
        """Verified skill bodies for this step, or "" (U-C12).

        Selection uses the run's discipline the same way the swarm spawner uses
        the task's, and the SAME resolver enforces verify-at-read; only the byte
        budget differs. Never raises: any fault costs this step its skills, not
        the run.
        """
        try:
            from omniagentos.skills import list_skills
            from omniagentos.skills.resolve import (
                RUNNER_PER_SKILL_BYTE_CAP,
                RUNNER_TOTAL_BYTE_CAP,
                render_skill_block,
                resolve_approved_skill_content,
            )
            from omniagentos.skills.select import select_skills

            raw_db = getattr(self.store, "_db_path", None)
            if raw_db is None or str(raw_db) == ":memory:":
                # An in-memory store has no path a second connection can open;
                # opening the default DB instead would inject a DIFFERENT
                # database's skills into this run.
                return ""
            db_path = str(raw_db)
            domain = str(run.get("discipline_id") or task.get("discipline") or "").strip()
            if not domain:
                return ""
            registry = list_skills(database=db_path)
            if not registry:
                return ""
            hits = select_skills(registry, domain=domain, max_skills=8)
            if not hits:
                return ""
            resolved = resolve_approved_skill_content(hits[:4], registry, database=db_path)
            block = render_skill_block(
                resolved,
                total_cap=RUNNER_TOTAL_BYTE_CAP,
                per_skill_cap=RUNNER_PER_SKILL_BYTE_CAP,
            )
            if block:
                # Recorded only after the block is confirmed non-empty (2026-08-14
                # xcrit F2), so a budget-drop never leaves a usage row for a skill
                # that did not reach the brief. The import + call are wrapped
                # locally (xcrit F3) so a fault resolving this telemetry module
                # itself can never strip an already-rendered skill block.
                try:
                    from omniagentos.skills.usage import record_skill_usage

                    record_skill_usage(
                        db_path,
                        str(run.get("id") or ""),
                        [skill.name for skill in resolved],
                        "runner",
                        skill_versions=[skill.version for skill in resolved],
                    )
                except Exception:  # noqa: BLE001 -- telemetry must never cost the skill block
                    LOG.warning("skill usage telemetry failed; continuing", exc_info=True)
            return block
        except Exception:  # noqa: BLE001 -- skills never block a step
            LOG.warning("runner skill injection failed; continuing without skills", exc_info=True)
            return ""

    @staticmethod
    def _inject_plan_step_context(prompt: str, context: dict[str, Any]) -> str:
        """Embed prior plan-step results into the prompt for production CLI adapters.

        Production adapters forward ``AgentInput.prompt`` to the CLI and do not read
        ``metadata["context"]``. Without this injection, step chaining was a false
        contract: context was written into metadata and then ignored (L-12).
        """
        if not context:
            return prompt
        try:
            rendered = json.dumps(context, indent=2, default=str, sort_keys=True)
        except (TypeError, ValueError):
            rendered = str(context)
        block = (
            "## Prior plan-step context\n"
            "The following JSON is the durable result of earlier steps in this run. "
            "Use it; do not re-derive it.\n"
            f"```json\n{rendered}\n```"
        )
        if not prompt:
            return block
        return f"{block}\n\n{prompt}"

    def _agent_grant(self, agent_id: str) -> set[str]:
        """Connector capabilities held by the run's agent, or an empty set."""
        from omniagentos.connectors.store import CapabilityStore
        from omniagentos.db.store import SqliteStore

        try:
            return set(CapabilityStore(cast(SqliteStore, self.store)).get_grant(agent_id))
        except Exception:  # noqa: BLE001 -- an unreadable grant must deny, not crash.
            return set()

    def _granted_scope_roots(self, run: dict[str, Any], task: dict[str, Any]) -> list[str]:
        """The directory roots this run may write to: the per-run workspace UNION
        the project's ``root_dirs`` + ``allowed_dirs``.

        This is the SAME server-derived set ``_project_extra_dirs`` threads onto
        ``AgentInput.metadata["extra_dirs"]`` (and the OS sandbox widens its
        write-roots to), just WITHOUT excluding ``working_dir`` -- so it is the one
        source of truth for "what this run may touch" that ``working_dir`` is
        validated against. An unscoped run (no project) is confined to its
        workspace alone PLUS the intake scratch-workspace base (below).
        """
        roots = [self._workspace_path(str(run["id"]))]
        roots.extend(self._project_extra_dirs(task.get("project_id"), None))
        # AC-policy fix7 (intake no-project flow): a tools-mode dispatch with no
        # project scopes its run to <var>/intake-workspace/<task_id> (see
        # intake.service._resolve_working_dir). That dir is NOT the per-run
        # workspace, so without this it would fail the working_dir scope check and
        # the legit post-approval flow would break. The intake base is a
        # server-derived, per-task scratch root (never an agent-authored path), so
        # admitting it as a granted root is safe.
        roots.append(self._intake_workspace_base())
        return roots

    @staticmethod
    def _intake_workspace_base() -> str:
        """The <var>/intake-workspace base that scopes no-project intake runs."""
        base = os.environ.get("OMNIAGENTOS_VAR_DIR")
        if not base:
            base = os.path.join(_repo_root(), "var")
        return os.path.join(base, "intake-workspace")

    def _assert_working_dir_in_scope(
        self, run: dict[str, Any], task: dict[str, Any], working_dir: str | None
    ) -> None:
        """Reject an agent step whose ``working_dir`` escapes the run's granted scope.

        Closes the control-plane ``create_run`` escape: a step's ``working_dir``
        became a sandbox WRITE-ROOT (``runner/sandbox.py``) with NO scope check, so
        a plan/task-authored ``working_dir`` of ``~/.ssh`` let a
        ``sandboxed_creation`` step (auto in AUTO mode) write outside every grant.
        The granted scope is the per-run workspace UNION the project's
        ``root_dirs``+``allowed_dirs`` (see ``_granted_scope_roots``). A
        ``working_dir`` that resolves OUT of that set -- an absolute out-of-scope
        path, or a ``..``/symlink escape -- fails the step, never runs it. This
        makes ``working_dir`` as trustworthy as ``extra_dirs`` already is,
        regardless of who authored the run. ``None``/empty resolves to the per-run
        workspace (AC-policy fix7 — no longer merely exempted) and is validated
        like any other value; the workspace is always one of the granted roots.
        """
        if working_dir is None or not str(working_dir).strip():
            working_dir = self._workspace_path(str(run["id"]))
        granted = self._granted_scope_roots(run, task)
        if _path_within_granted(str(working_dir), granted):
            return
        self._event(
            Events.AUDIT,
            "working_dir_out_of_scope",
            run,
            {"working_dir": str(working_dir), "granted_roots": granted},
        )
        raise StepFailure(f"working_dir_out_of_scope:{working_dir}")

    def _project_extra_dirs(self, project_id: Any, working_dir: str | None) -> list[str]:
        """Directories, beyond ``working_dir``, this run's project has been granted.

        Drive Access for projects (W4): a project's ``root_dirs`` + ``allowed_dirs``
        (``omniagentos/projects/store.py``) are the single source of truth for what
        it may read/write beyond its primary working_dir -- including a Drive
        subfolder granted via ``omniagentos/provision/drive.py::grant_drive_dir``
        (which validates the path before ever calling the same
        ``ProvisionStore.grant_dir`` used for any other directory grant). The
        result is threaded onto ``AgentInput.metadata["extra_dirs"]`` so an
        adapter can ``--add-dir`` every one of them (see adapters/claude.py and
        friends), and is the seam a future filesystem/shell guardrail should read
        from too (see the comment at the ``sandbox_for_tools`` call above).

        No ``project_id`` (an unscoped run, the pre-W4 default) or an unreadable
        project both degrade to ``[]`` -- byte-for-byte unchanged behavior for
        every run that isn't attached to a project.
        """
        if not project_id:
            return []
        from omniagentos.db.store import SqliteStore
        from omniagentos.projects.store import ProjectStore

        try:
            project = ProjectStore(cast(SqliteStore, self.store)).get_project(str(project_id))
        except Exception:  # noqa: BLE001 -- an unreadable project must degrade, not crash the run.
            LOG.exception("could not resolve project %s for drive-access dir threading", project_id)
            return []
        if project is None:
            return []
        dirs: list[str] = []
        seen: set[str] = {working_dir} if working_dir else set()
        for raw in (*project.get("root_dirs", []), *project.get("allowed_dirs", [])):
            value = str(raw)
            if value and value not in seen:
                seen.add(value)
                dirs.append(value)
        return dirs

    def _task_context(
        self, run: dict[str, Any], params: dict[str, Any]
    ) -> tuple[dict[str, Any], dict[str, Any], list[str]]:
        task = self.store.get_task(str(run["task_id"])) or {}
        task_input = _object(task.get("input_json", {}))
        task_tools_raw = task_input.get("tools_allowed", [])
        if not isinstance(task_tools_raw, list):
            raise StepFailure("tools_allowed must be a list")
        task_tools = [str(tool) for tool in task_tools_raw]

        # The AGENT grant is the outermost ceiling. When a run is bound to a
        # registered agent, that agent's standing grant caps what the task may
        # reach: a task template asking for stripe_acmeuni.read is narrowed away to
        # nothing if the agent it runs as was never granted it. Primitives
        # ('shell', 'file_write') stay task-scoped -- they describe the harness's
        # own workspace, not the outside world, and are unchanged by this.
        #
        #     agent grant  >=  task grant  >=  step grant
        #
        agent_id = run.get("agent")
        if agent_id:
            held = self._agent_grant(str(agent_id))
            task_tools = [t for t in task_tools if "." not in t or t in held]

        if "tools_allowed" not in params:
            return task, task_input, task_tools
        step_tools_raw = params.get("tools_allowed")
        if not isinstance(step_tools_raw, list):
            raise StepFailure("tools_allowed must be a list")
        # The persisted task grant is authoritative. A plan step may narrow it,
        # but cannot self-grant capabilities (council PROD-001/NEW-1).
        effective = [str(tool) for tool in step_tools_raw if str(tool) in task_tools]
        return task, task_input, effective

    def _workspace_path(self, run_id: str) -> str:
        """The per-run workspace path WITHOUT creating it (for scope classification)."""
        return str(Path(self.workspace_base) / run_id)

    def _run_workspace(self, run_id: str) -> Path:
        """Create and return the runner-assigned confinement root for one run."""
        workspace = Path(self.workspace_base) / run_id
        workspace.mkdir(parents=True, exist_ok=True)
        return workspace.resolve()

    def _require_tool(
        self,
        run: dict[str, Any],
        params: dict[str, Any],
        tool: str,
        operation: str,
    ) -> None:
        _, _, tools = self._task_context(run, params)
        if tool in tools:
            return
        self._event(
            Events.AUDIT,
            "tool_capability_denied",
            run,
            {"operation": operation, "required_tool": tool, "tools_allowed": tools},
        )
        raise StepFailure(f"tool_not_allowed:{tool}:{operation}")

    @staticmethod
    def _command_argv(command: Any) -> list[str]:
        if isinstance(command, str):
            argv = shlex.split(command)
        elif isinstance(command, list) and all(isinstance(part, str) for part in command):
            argv = list(command)
        else:
            raise StepFailure("command must be a string or list of strings")
        if not argv:
            raise StepFailure("command must not be empty")
        return argv

    @classmethod
    def _run_command(
        cls,
        command: Any,
        *,
        cwd: str | None,
        timeout_s: float,
        timeout_label: str,
    ) -> subprocess.CompletedProcess[str]:
        argv = cls._command_argv(command)
        # AC-policy: (1) money/bank/infra credentials are stripped from the
        # subprocess env (broker is the sole money path); (2) the command is wrapped
        # in an OS sandbox that confines file writes+deletes to the per-run
        # workspace, so an out-of-scope write is physically impossible even if the
        # command tries. wrap_command is a no-op when the OS sandbox is unavailable,
        # in which case the deny-by-default classifier is the guarantee.
        env = _scrubbed_env()
        if cwd:
            # Keep tool scratch inside the confined workspace (the strict sandbox
            # profile does NOT allow the system temp dir).
            workspace_tmp = os.path.join(cwd, ".tmp")
            try:
                os.makedirs(workspace_tmp, exist_ok=True)
                env["TMPDIR"] = workspace_tmp
            except OSError:
                pass
        launch = sandbox.wrap_command(argv, cwd)
        proc = subprocess.Popen(
            launch,
            cwd=cwd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
            env=env,
        )
        try:
            stdout, stderr = proc.communicate(timeout=timeout_s)
        except subprocess.TimeoutExpired:
            # Kill the process group, then wait a BOUNDED time for reaping. An
            # unbounded second communicate() after SIGKILL can wedge a runner slot
            # when the child is uninterruptible or already reaped (L-18).
            cls._kill_process_group(proc)
            stdout, stderr = cls._bounded_communicate(proc, _COMMAND_CLEANUP_TIMEOUT_S)
            raise StepFailure(f"{timeout_label}:{timeout_s:g}") from None
        return subprocess.CompletedProcess(argv, proc.returncode, stdout, stderr)

    def _exec_command_for_run(
        self,
        run_id: str,
        command: Any,
        *,
        cwd: str | None,
        timeout_s: float,
        timeout_label: str,
    ) -> subprocess.CompletedProcess[str]:
        argv = self._command_argv(command)
        env = _scrubbed_env()
        if cwd:
            workspace_tmp = os.path.join(cwd, ".tmp")
            try:
                os.makedirs(workspace_tmp, exist_ok=True)
                env["TMPDIR"] = workspace_tmp
            except OSError:
                pass
        launch = sandbox.wrap_command(argv, cwd)
        proc = subprocess.Popen(
            launch,
            cwd=cwd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
            env=env,
        )
        with self._active_processes_lock:
            self._active_processes[run_id] = proc
        try:
            stdout, stderr = proc.communicate(timeout=timeout_s)
        except subprocess.TimeoutExpired:
            self._kill_process_group(proc)
            stdout, stderr = self._bounded_communicate(proc, _COMMAND_CLEANUP_TIMEOUT_S)
            raise StepFailure(f"{timeout_label}:{timeout_s:g}") from None
        finally:
            with self._active_processes_lock:
                self._active_processes.pop(run_id, None)
        return subprocess.CompletedProcess(argv, proc.returncode, stdout, stderr)

    @staticmethod
    def _kill_process_group(proc: subprocess.Popen[str]) -> None:
        """Best-effort SIGKILL of the child's process group; tolerate disappearance."""
        pid = proc.pid
        if pid is None:
            return
        try:
            os.killpg(os.getpgid(pid), signal.SIGKILL)
        except ProcessLookupError:
            pass
        except OSError:
            # ESRCH/EPERM/EINVAL: process already gone, not a group leader, etc.
            try:
                proc.kill()
            except (ProcessLookupError, OSError):
                pass

    @staticmethod
    def _bounded_communicate(proc: subprocess.Popen[str], timeout_s: float) -> tuple[str, str]:
        """Reap a killed child without blocking the runner slot indefinitely."""
        try:
            stdout, stderr = proc.communicate(timeout=max(0.1, timeout_s))
            return stdout or "", stderr or ""
        except subprocess.TimeoutExpired:
            try:
                proc.kill()
            except (ProcessLookupError, OSError):
                pass
            try:
                stdout, stderr = proc.communicate(timeout=1.0)
                return stdout or "", stderr or ""
            except (subprocess.TimeoutExpired, ProcessLookupError, OSError):
                # Child is gone or still wedged; free the slot with empty output.
                return "", ""
        except (ProcessLookupError, OSError):
            return "", ""

    def _execute_validate(self, run: dict[str, Any], step: dict[str, Any]) -> StepOutcome:
        params = _object(step.get("params", {}))
        command = params.get("command")
        if not isinstance(command, (str, list)) or not command:
            raise StepFailure("validate step requires params.command")
        self._require_tool(run, params, "shell", "validate")
        self._assert_fence(str(run["id"]))
        started = time.monotonic()
        completed = self._exec_command_for_run(
            str(run["id"]),
            command,
            cwd=str(self._run_workspace(str(run["id"]))),
            timeout_s=float(params.get("timeout_s", 300)),
            timeout_label="validation_timeout",
        )
        self._assert_fence(str(run["id"]))
        result = {
            "returncode": completed.returncode,
            "stdout": completed.stdout,
            "stderr": completed.stderr,
        }
        if completed.returncode != 0:
            self._record_usage(
                str(run["id"]),
                AgentUsage(
                    wall_ms=max(1, int((time.monotonic() - started) * 1000)),
                    source="runner",
                ),
            )
            raise StepFailure(f"validation_failed:{completed.returncode}")
        usage = AgentUsage(
            wall_ms=max(1, int((time.monotonic() - started) * 1000)), source="runner"
        )
        return StepOutcome(result, usage=usage)

    def _idempotency_key(self, run: dict[str, Any], seq: int, step: dict[str, Any]) -> str:
        params = _object(step.get("params", {}))
        if params.get("key"):
            return str(params["key"])
        material = f"{run['id']}|{seq}|{step.get('name', f'step-{seq}')}|{digest(_json(params))}"
        return hashlib.sha256(material.encode()).hexdigest()

    def _execute_effect(
        self,
        run: dict[str, Any],
        seq: int,
        step: dict[str, Any],
        *,
        attempt: int = 0,
    ) -> StepOutcome:
        params = _object(step.get("params", {}))
        if params.get("probe"):
            self._require_tool(run, params, "shell", "effect_probe")
        key = self._idempotency_key(run, seq, step)
        self._assert_fence(str(run["id"]))
        inserted = self.store.idem_insert(key, str(run["id"]), str(step.get("name", seq)))
        if not inserted:
            receipt = self.store.idem_get(key)
            if receipt is None:
                raise StepFailure("idempotency_unresolved")
            if receipt.get("result_json") is not None:
                return StepOutcome(json.loads(receipt["result_json"]), skipped=True)
            probe = params.get("probe")
            if probe:
                landed = (
                    self._exec_command_for_run(
                        str(run["id"]),
                        probe,
                        cwd=str(self._run_workspace(str(run["id"]))),
                        timeout_s=float(params.get("timeout_s", 300)),
                        timeout_label="effect_probe_timeout",
                    ).returncode
                    == 0
                )
                self._assert_fence(str(run["id"]))
                if landed:
                    result = {"effect": str(params.get("effect")), "probed": True}
                    self.store.idem_complete(key, _json(result))
                    return StepOutcome(result, skipped=True)
            # Incomplete prior attempt: reapply only when the step author opted in
            # via unsafe_retry, OR this is an intentional in-process retry
            # (params.retries / attempt > 0). Crash-recovery without either signal
            # still fails closed to avoid silent double-apply (M-42).
            allow_reapply = bool(params.get("unsafe_retry", False)) or attempt > 0
            if not allow_reapply:
                raise StepFailure("idempotency_unresolved")

        self._assert_fence(str(run["id"]))
        result = self._apply_effect(run, params)
        self._assert_fence(str(run["id"]))
        self.store.idem_complete(key, _json(result))
        return StepOutcome(result)

    def _apply_effect(self, run: dict[str, Any], params: dict[str, Any]) -> dict[str, Any]:
        effect = str(params.get("effect", ""))
        if effect == "noop":
            return {"effect": "noop", "ok": True}
        if effect != "append_file":
            raise StepFailure(f"unknown_effect:{effect}")
        self._require_tool(run, params, "file_write", "append_file")
        raw_path = params.get("path")
        if not raw_path:
            raise StepFailure("append_file requires params.path")
        relative_path = Path(str(raw_path))
        if relative_path.is_absolute():
            raise StepFailure("append_file_absolute_path_denied")
        root = self._run_workspace(str(run["id"]))
        path = (root / relative_path).resolve()
        if inode_relative_parts_anchored(path, root) is None:
            raise StepFailure("append_file_path_escape_denied")
        path.parent.mkdir(parents=True, exist_ok=True)
        line = str(params.get("line", ""))
        with path.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        return {"effect": effect, "path": str(path), "line": line}

    def _approval_allows(
        self,
        run: dict[str, Any],
        seq: int,
        step: dict[str, Any],
        effective_action: ActionClass,
    ) -> bool:
        approval = self.store.get_approval_for(str(run["id"]), seq)
        if approval and approval["state"] == ApprovalState.APPROVED.value:
            if not self._approval_has_required_human(approval, effective_action):
                self._fail(run, "approval_not_human")
                return False
            return True
        if approval is None:
            self._assert_fence(str(run["id"]))
            params = _object(step.get("params", {}))
            expires_at = params.get("approval_expires_at")
            if not expires_at:
                expires_at = (
                    datetime.now(UTC)
                    + timedelta(hours=max(0, self.dependencies.approval_expiry_hours))
                ).strftime("%Y-%m-%dT%H:%M:%SZ")
            # F1: proposed_action must be evaluable — include command/target, not just tool name.
            proposed = str(
                params.get("command")
                or params.get("path")
                or params.get("file_path")
                or step.get("name")
                or f"step-{seq}"
            )
            if len(proposed) > 500:
                proposed = proposed[:497] + "..."
            self.store.create_approval(
                {
                    "id": new_id("apr"),
                    "run_id": run["id"],
                    "task_id": run["task_id"],
                    "step_seq": seq,
                    "action_class": effective_action.value,
                    "proposed_action": proposed,
                    "params_json": _json(params),
                    "risk": str(params.get("risk", "")),
                    "evidence": str(params.get("evidence", "")),
                    "state": ApprovalState.PENDING.value,
                    "expires_at": expires_at,
                    "created_at": utc_now_iso(),
                }
            )
            approval = self.store.get_approval_for(str(run["id"]), seq)
            self._event(Events.APPROVAL_REQUESTED, "requested", run, approval or {"seq": seq})
            # NT-notify: persist+push a notification linked to this approval, so a
            # run escalating for a human decision is a durable, actionable feed
            # entry — never a phantom. Best-effort; supervision must not depend on it.
            notifier = self.dependencies.notify_approval
            if notifier is not None and approval is not None:
                try:
                    notifier(approval)
                except Exception:  # noqa: BLE001 - notification must never break a run
                    LOG.exception("approval notification failed for run %s", run.get("id"))
            # HARD-STOP: a hard-stop park (irreversible in AUTO, or a gated class in
            # SUPERVISED) is exactly when the operator must be pinged.
            self._escalate(
                "hard_stop",
                run,
                f"{effective_action.value}: {step.get('name', f'step-{seq}')}",
            )
        self._transition(run, RunState.AWAITING_APPROVAL)
        return False

    def _approval_has_required_human(
        self, approval: dict[str, Any], action_class: ActionClass
    ) -> bool:
        decision = self.dependencies.evaluate_policy(action_class)
        return approval_satisfies_gate(
            approval, decision, actor=self.actor, now_iso=utc_now_iso()
        ).human_ok

    def _requeue_expired_run_approval(
        self,
        run: dict[str, Any],
        seq: int,
        expired: dict[str, Any],
        step: dict[str, Any],
        action_class: ActionClass,
    ) -> None:
        """Create a fresh pending approval after expiry; keep run parked (F1/5.2)."""
        params = _object(step.get("params", {}))
        try:
            import json as _json_mod

            raw = expired.get("params_json")
            if isinstance(raw, str) and raw.strip():
                decoded = _json_mod.loads(raw)
                if isinstance(decoded, dict):
                    params = {**params, **decoded}
        except Exception:  # noqa: BLE001
            pass
        expires_at = (
            datetime.now(UTC) + timedelta(hours=max(1, self.dependencies.approval_expiry_hours))
        ).strftime("%Y-%m-%dT%H:%M:%SZ")
        proposed = str(
            expired.get("proposed_action")
            or step.get("name")
            or params.get("command")
            or f"step-{seq}"
        )
        new_approval_id = new_id("apr")
        self.store.create_approval(
            {
                "id": new_approval_id,
                "run_id": run["id"],
                "task_id": run["task_id"],
                "step_seq": seq,
                "action_class": action_class.value
                if hasattr(action_class, "value")
                else str(action_class),
                "proposed_action": proposed,
                "params_json": _json(params),
                "risk": str(expired.get("risk") or params.get("risk") or ""),
                "evidence": str(
                    expired.get("evidence")
                    or params.get("evidence")
                    or "requeued after approval_expired"
                ),
                "state": ApprovalState.PENDING.value,
                "expires_at": expires_at,
                "created_at": utc_now_iso(),
            }
        )
        self._event(
            Events.APPROVAL_REQUESTED,
            "requeued_after_expiry",
            run,
            {"id": new_approval_id, "prior": expired.get("id"), "seq": seq},
        )
        self._transition(run, RunState.AWAITING_APPROVAL)
        notifier = self.dependencies.notify_approval
        if notifier is not None:
            try:
                requeued = self.store.get_approval_for(str(run["id"]), seq)
                if requeued is not None:
                    notifier(requeued)
            except Exception:  # noqa: BLE001
                LOG.exception("approval requeue notification failed for run %s", run.get("id"))
        try:
            from omniagentos.notifications.service import record_notification

            record_notification(
                kind="approval",
                title="Approval expired — requeued",
                body=f"Run kept parked; new approval {new_approval_id}: {proposed[:200]}",
                severity="warning",
                ref_type="approval",
                ref_id=new_approval_id,
                payload={
                    "prior_approval_id": expired.get("id"),
                    "run_id": run.get("id"),
                    "reason": "approval_expired_requeued",
                    "proposed_action": proposed,
                },
                push=True,
            )
        except Exception:  # noqa: BLE001
            LOG.debug("expiry requeue notify failed", exc_info=True)

    def _service_parked(self, run: dict[str, Any]) -> str | bool:
        """Advance one AWAITING_APPROVAL run.

        Returns the run_id (str) when the approval cleared and the run is now
        RUNNING and must be executed by the caller, ``True`` when a terminal
        decision (cancel/reject/expire/missing) was applied inline, or ``False``
        when the approval is still pending. Splitting the execute out of this method
        lets the caller run it inline (single slot) or dispatch it to a pool slot.
        """
        try:
            run = self._owned_run(str(run["id"]))
            if bool(run["cancel_requested"]):
                self._cancel(run)
                return True
            plan = _array(run["plan_json"])
            completed = {
                int(row["seq"])
                for row in self.store.get_steps(str(run["id"]))
                if row["status"] in {StepStatus.COMPLETED.value, StepStatus.SKIPPED.value}
            }
            seq = next((index for index in range(len(plan)) if index not in completed), None)
            approval = self.store.get_approval_for(str(run["id"]), seq)
            if approval is None:
                self._fail(run, "approval_missing")
                return True
            state = ApprovalState(str(approval["state"]))
            # Determine action_class for gate evaluation
            try:
                action_class = ActionClass(str(approval["action_class"]))
            except ValueError:
                action_class = ActionClass.CONSEQUENTIAL
            decision = self.dependencies.evaluate_policy(action_class)
            # Check both human approval and expiry gates
            gate_result = approval_satisfies_gate(
                approval, decision, actor=self.actor, now_iso=utc_now_iso()
            )
            if gate_result.expired:
                self.store.decide_approval(
                    str(approval["id"]), ApprovalState.EXPIRED.value, self.actor, "expired"
                )
                state = ApprovalState.EXPIRED
            if state == ApprovalState.APPROVED:
                if not gate_result.human_ok:
                    self._fail(run, "approval_not_human")
                    return True
                self._transition(run, RunState.RUNNING)
                return str(run["id"])
            elif state == ApprovalState.REJECTED:
                self._fail(run, "approval_rejected")
                return True
            elif state == ApprovalState.EXPIRED:
                # AUTO-APPROVE Phase 5.2 / F1: re-queue with a fresh TTL instead of
                # failing the run. Unanswered parking costs latency, not the work.
                step = plan[seq] if isinstance(seq, int) and 0 <= seq < len(plan) else {}
                if not isinstance(step, dict):
                    step = {}
                self._requeue_expired_run_approval(
                    run, int(seq if seq is not None else 0), approval, step, action_class
                )
                return False
            return False
        except LostFence:
            LOG.info("worker %s lost parked-run fence for %s", self.worker_id, run["id"])
            return False

    def _budget_allows(self, run: dict[str, Any]) -> bool:
        spec = BudgetSpec.model_validate(_object(run.get("budget_json", {})))
        tokens = int(run.get("input_tokens") or 0) + int(run.get("output_tokens") or 0)
        raw_cost = run.get("cost_usd")
        # Unknown cost (None after real token usage) must not render as free.
        # Fail closed when a cost ceiling is set and cost is explicitly unknown.
        if raw_cost is None and tokens > 0 and spec.cost_usd_max is not None:
            return False
        used_cost = 0.0 if raw_cost is None else float(raw_cost)
        return self.dependencies.check_budget(
            spec,
            int(run.get("wall_ms") or 0),
            tokens,
            used_cost,
        ).allowed

    def _record_usage(self, run_id: str, usage: AgentUsage | None) -> None:
        if usage is None:
            return
        run = self._owned_run(run_id)
        wall = int(usage.wall_ms)
        turns = int(usage.turns or 0)
        input_tokens = int(usage.input_tokens or 0)
        output_tokens = int(usage.output_tokens or 0)
        # Three-valued cost: None means unknown — never coerce via `or 0.0`.
        if usage.cost_usd is None:
            # Preserve any previously known run cost; do not invent zero.
            new_cost: float | None = run.get("cost_usd")
            if new_cost is not None:
                new_cost = float(new_cost)
            budget_cost = 0.0
        else:
            prev = run.get("cost_usd")
            prev_f = float(prev) if prev is not None else 0.0
            new_cost = prev_f + float(usage.cost_usd)
            budget_cost = float(usage.cost_usd)
        if not self.store.update_run(
            run_id,
            {
                "wall_ms": int(run.get("wall_ms") or 0) + wall,
                "turns": int(run.get("turns") or 0) + turns,
                "input_tokens": int(run.get("input_tokens") or 0) + input_tokens,
                "output_tokens": int(run.get("output_tokens") or 0) + output_tokens,
                "cost_usd": new_cost,
                "usage_estimated": int(bool(run.get("usage_estimated")) or usage.estimated),
                "usage_source": usage.source,
                "updated_at": utc_now_iso(),
            },
            expect_worker=self.worker_id,
        ):
            raise LostFence(run_id)
        tokens = input_tokens + output_tokens
        for budget_id in {
            "global",
            f"run:{run_id}",
            f"task:{run['task_id']}",
            *([f"discipline:{run['discipline_id']}"] if run.get("discipline_id") else []),
        }:
            self._assert_fence(run_id)
            self.store.upsert_budget_usage(budget_id, wall, tokens, budget_cost)

    def _cancel(self, run: dict[str, Any]) -> None:
        session_ref = run.get("session_ref")
        if session_ref:
            try:
                harness = HarnessType(str(run["harness"]))
                self.dependencies.resolve_adapter(harness).cancel(str(session_ref))
            except Exception:
                LOG.exception("adapter cancellation failed for %s", run["id"])
        self._compensate(run)
        self.store.void_pending_approvals(str(run["id"]), "voided: run cancelled")
        self._transition(run, RunState.CANCELLED)

    def _escalate(self, kind: str, run: dict[str, Any], detail: str) -> None:
        """Ping the human on a hard-stop / cap-hit / done / blocker. Best-effort.

        Escalation is a pure side-effect: it never changes run state and never
        raises, so a broken notifier cannot wedge or fail a run. This is the only
        operator-facing channel in AUTO mode -- there is no per-action prompt."""
        try:
            self.dependencies.escalate(kind, str(run.get("id", "")), detail)
        except Exception:  # noqa: BLE001 - escalation must never affect the runner
            LOG.exception("escalation notification failed")

    def _fail(self, run: dict[str, Any], error: str) -> None:
        self._compensate(run)
        self.store.void_pending_approvals(str(run["id"]), "voided: run terminal")
        self._transition(run, RunState.FAILED, error=error)

    def _compensate(self, run: dict[str, Any]) -> None:
        plan = _array(run["plan_json"])
        rows = {int(row["seq"]): row for row in self.store.get_steps(str(run["id"]))}
        for seq in reversed(range(len(plan))):
            row = rows.get(seq)
            if not row or row["status"] not in {
                StepStatus.COMPLETED.value,
                StepStatus.SKIPPED.value,
            }:
                continue
            compensate = _object(plan[seq].get("params", {})).get("compensate")
            if not isinstance(compensate, dict):
                continue
            try:
                self._assert_fence(str(run["id"]))
                self._apply_effect(run, dict(compensate))
                if not self.store.upsert_step(
                    str(run["id"]),
                    seq,
                    {"status": StepStatus.COMPENSATED.value, "finished_at": utc_now_iso()},
                    expect_worker=self.worker_id,
                ):
                    raise LostFence(str(run["id"]))
            except LostFence:
                raise
            except Exception as exc:
                self._event(
                    Events.AUDIT,
                    "compensation_failed",
                    run,
                    {"seq": seq, "error": str(exc)},
                )

    def _transition(
        self, run: dict[str, Any], target: RunState, *, error: str | None = None
    ) -> None:
        current_state = RunState(str(run["state"]))
        # No-op same-state transitions are allowed (e.g. requeue while already
        # parked). Any other illegal edge is fail-closed rather than written
        # into durable state (M-42: VALIDATING → AWAITING_APPROVAL was one such).
        if current_state != target and not can_transition_run(current_state, target):
            raise StepFailure(f"illegal_run_transition:{current_state.value}->{target.value}")
        now = utc_now_iso()
        fields: dict[str, Any] = {"state": target.value, "updated_at": now}
        if error is not None:
            fields["error"] = error
        if target in TERMINAL_RUN_STATES:
            fields["finished_at"] = now
        if not self.store.update_run(str(run["id"]), fields, expect_worker=self.worker_id):
            raise LostFence(str(run["id"]))
        if target in TERMINAL_RUN_STATES:
            # RELEASE, generation-fenced, as early as the terminal state is durable:
            # the run will not touch those paths again, and anything queued behind it
            # should not wait for finalization (which writes the ledger and the vault,
            # not the workspace). A stale generation here matches zero rows, so a
            # displaced worker's cleanup cannot free its adopter's locks.
            self._scope.release(str(run["id"]))
        current = self._owned_run(str(run["id"]))
        self._project(current, target)
        if target in TERMINAL_RUN_STATES:
            self.store.void_pending_approvals(str(run["id"]), "voided: run terminal")
            self._finalize(str(run["id"]))
        else:
            self._event(Events.RUN_UPDATED, target.value, current)

    def _project(self, run: dict[str, Any], target: RunState) -> None:
        latest = self.store.list_runs({"task_id": run["task_id"]}, 1)
        if not latest or latest[0]["id"] != run["id"]:
            return
        task = self.store.get_task(str(run["task_id"]))
        if task is None:
            return
        current = TaskState(str(task["state"]))
        wanted = TaskState(target.value)
        if current == wanted:
            return
        if wanted not in TASK_TRANSITIONS[current] or not self.store.update_task_state(
            str(run["task_id"]), wanted.value, expect=[current.value]
        ):
            self._event(
                Events.AUDIT,
                "task_projection_guard_failed",
                run,
                {"from": current.value, "to": wanted.value},
            )
            return
        self._event(Events.TASK_UPDATED, wanted.value, run)

    def _requeue_paused(self) -> None:
        for run_id in self.store.requeue_paused_runs():
            run = self.store.get_run(run_id)
            if run is not None:
                self._project(run, RunState.QUEUED)
                self._event(Events.RUN_UPDATED, "queued", run)

    def _finalize(self, run_id: str) -> None:
        run = self._owned_run(run_id)
        state = RunState(str(run["state"]))
        if state not in TERMINAL_RUN_STATES:
            return
        try:
            self._finalize_body(run_id, run, state)
        except LostFence:
            raise
        except (KeyboardInterrupt, SystemExit):
            raise
        except Exception as exc:
            self._record_finalize_failure(run_id, exc)
            return
        self._finalize_attempts.pop(run_id, None)

    def _finalize_body(self, run_id: str, run: dict[str, Any], state: RunState) -> None:
        receipts = [
            IdempotencyReceipt.model_validate(row) for row in self.store.idem_for_run(run_id)
        ]
        manifest = self._manifest(run, receipts)
        manifest_path = run.get("manifest_path")
        if not manifest_path:
            self._assert_fence(run_id)
            manifest_path = self.dependencies.append_manifest(self.ledger_dir, manifest)
            if not self.store.update_run(
                run_id,
                {"manifest_path": manifest_path, "updated_at": utc_now_iso()},
                expect_worker=self.worker_id,
            ):
                raise LostFence(run_id)
            run = self._owned_run(run_id)
        if not run.get("vault_note_path"):
            steps = self.store.get_steps(run_id)
            # Wire the real task + artifacts into the note so the vault renders the
            # actual work product (title/discipline/artifacts/input), not a raw id
            # with empty sections (council DSGN-001/TEST-001; R-vault escalation).
            task = self.store.get_task(str(run["task_id"])) or {}
            relpath, content = self.dependencies.render_run_note(
                run,
                steps,
                str(manifest_path),
                receipts,
                artifacts_list=self.store.get_artifacts(run_id),
                task_title=task.get("title"),
                task_input=task.get("input_json"),
            )
            self._assert_fence(run_id)
            note_path = self.dependencies.write_note(self.vault_dir, relpath, content)
            if not self.store.update_run(
                run_id,
                {"vault_note_path": note_path, "updated_at": utc_now_iso()},
                expect_worker=self.worker_id,
            ):
                raise LostFence(run_id)
            run = self._owned_run(run_id)

        if state == RunState.COMPLETED:
            from omniagentos.knowledge import config as knowledge_config

            if knowledge_config.knowledge_enabled():
                from omniagentos.knowledge.recall import (
                    clear_run_state,
                    safe_record_helped,
                )
                from omniagentos.knowledge.runner_hook import safe_ingest_run_reflection

                # _recall_state records whether recall() wrote a non-empty recall_log
                # row, so no extra PostgreSQL existence query is needed.
                if self._recall_state.get(run_id, False):
                    safe_record_helped(run_id)
                safe_ingest_run_reflection(run, self.store.get_steps(run_id))
                clear_run_state(run_id)

        # Memory layer: persist the agent's result as a conversation turn on the task so
        # the history accrues for the next run. Never-raising; a no-op pre-migration-031.
        from omniagentos.memory import config as memory_config

        if state == RunState.COMPLETED and memory_config.memory_enabled():
            from omniagentos.memory.runner_hook import safe_persist_agent_turn

            safe_persist_agent_turn(
                self.store,
                task_id=str(run["task_id"]),
                content=str(run.get("output_text") or ""),
                model=run.get("model"),
            )
        # Unconditionally drop the per-run recall guard for EVERY terminal state (not just
        # COMPLETED+enabled): a FAILED/CANCELLED run, or the flag flipping off between
        # recall and finalize, would otherwise leak _recall_state entries unboundedly on a
        # long-lived worker (council finding). Best-effort clear the recall-module state too.
        if self._recall_state.pop(run_id, None) is not None:
            try:
                from omniagentos.knowledge.recall import clear_run_state as _clear

                _clear(run_id)
            except Exception:
                pass
        # Drop the per-run memory guard on EVERY terminal state so a long-lived worker
        # never leaks _memory_state entries (mirrors the recall-guard cleanup above).
        self._memory_state.pop(run_id, None)
        self.store.void_pending_approvals(run_id, "voided: run terminal")
        self._event(Events.RUN_UPDATED, state.value, run)
        # Post-run wiki work deliberately accepts one-run reflection staleness:
        # queued hooks have no ordering guarantees, so the wiki may reflect a run
        # one or more cycles after that run completes.
        if state == RunState.COMPLETED:
            try:
                self._enqueue_postrun_job(run_id, "wiki_update")
            except Exception:
                # Queueing a best-effort wiki update must never fail the run.
                LOG.exception(
                    "runner %s could not enqueue wiki update for %s", self.worker_id, run_id
                )
            # Per-run analysis (S1): same durable queue idiom; any failure degrades
            # to a no-op so finalization is never blocked (mirrors knowledge/runner_hook).
            try:
                self._enqueue_postrun_job(run_id, "run_analysis")
            except Exception:
                pass

    def _record_finalize_failure(self, run_id: str, exc: Exception) -> None:
        """Back off and durably quarantine a repeatedly unfinalizable run."""
        self._finalize_backoff = True
        attempts = self._finalize_attempts.get(run_id, 0) + 1
        self._finalize_attempts[run_id] = attempts
        error = f"finalize_fault:{type(exc).__name__}:{exc}"
        LOG.exception(
            "runner %s finalize attempt %s/%s failed for run %s",
            self.worker_id,
            attempts,
            self.finalize_attempt_limit,
            run_id,
        )
        run = self.store.get_run(run_id)
        if run is None or run.get("worker_id") != self.worker_id:
            return
        if attempts < self.finalize_attempt_limit:
            self._event(
                Events.AUDIT,
                "finalize_attempt_failed",
                run,
                {
                    "error": error,
                    "attempt": attempts,
                    "limit": self.finalize_attempt_limit,
                },
            )
            return
        now = utc_now_iso()
        # Avoid _transition/_fail: both recursively invoke finalization for a
        # terminal run, which is the failing operation being quarantined.
        if self.store.update_run(
            run_id,
            {
                "state": RunState.FAILED.value,
                "error": "finalization_failed",
                "vault_note_path": _FINALIZE_QUARANTINE_SENTINEL,
                "finished_at": run.get("finished_at") or now,
                "updated_at": now,
            },
            expect_worker=self.worker_id,
        ):
            quarantined = self.store.get_run(run_id) or run
            self._event(
                Events.AUDIT,
                "finalize_quarantined",
                quarantined,
                {"error": error, "attempts": attempts},
            )
        self._finalize_attempts.pop(run_id, None)

    def _manifest(self, run: dict[str, Any], receipts: list[IdempotencyReceipt]) -> RunManifest:
        tokens_in = int(run.get("input_tokens") or 0)
        tokens_out = int(run.get("output_tokens") or 0)
        raw_cost = run.get("cost_usd")
        usage = AgentUsage(
            wall_ms=int(run.get("wall_ms") or 0),
            turns=run.get("turns"),
            input_tokens=tokens_in,
            output_tokens=tokens_out,
            # Preserve unknown: None must not become 0.0 on the manifest.
            cost_usd=None if raw_cost is None else float(raw_cost),
            estimated=bool(run.get("usage_estimated")),
            source=str(run.get("usage_source") or "estimator"),
        )
        artifacts = [str(item["uri"]) for item in self.store.get_artifacts(str(run["id"]))]
        output = str(run.get("output_text") or "") + str(run.get("output_json") or "")
        return RunManifest(
            run_id=str(run["id"]),
            task_id=str(run["task_id"]),
            discipline=run.get("discipline_id"),
            arm=run.get("arm"),
            harness=HarnessProfile(
                harness=HarnessType(str(run["harness"])),
                version=str(run.get("harness_version") or ""),
                env_hash=str(run.get("env_hash") or ""),
                params=_object(run.get("harness_params", {})),
            ),
            agent=run.get("agent"),
            model=run.get("model"),
            state=RunState(str(run["state"])),
            started_at=run.get("started_at"),
            finished_at=run.get("finished_at"),
            usage=usage,
            receipts=receipts,
            output_digest=digest(output) if output else None,
            artifacts=artifacts,
            trace_id=str(run.get("trace_id") or ""),
        )

    def _event(
        self,
        event_type: str,
        action: str,
        run: dict[str, Any],
        payload: dict[str, Any] | None = None,
    ) -> None:
        self.store.insert_event(
            event_type,
            self.actor,
            action,
            target_type="run",
            target_id=str(run["id"]),
            payload=payload or {"state": run.get("state")},
            trace_id=str(run.get("trace_id") or ""),
        )
