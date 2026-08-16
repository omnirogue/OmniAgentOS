"""Durable state machine for long-running board tasks.

The database is the journal.  In particular, an attempt is inserted before its
executor is launched, and terminal callbacks win a compare-and-swap close before
performing any follow-up work.  That makes both dispatch and terminal delivery
safe to retry after a process crash.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import secrets
import shutil
import signal
import subprocess
import sys
import threading
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from time import monotonic, sleep
from typing import Any, cast

from omniagentos.contracts import (
    AgentInput,
    BudgetSpec,
    HarnessType,
    ResultStatus,
    utc_now_iso,
)
from omniagentos.db.store import _next_event_sequence
from omniagentos.longhaul.limits import Classification, classify_terminal
from omniagentos.longhaul.prompts import continuation_prompt, initial_prompt
from omniagentos.longhaul.routing import rank_workers
from omniagentos.longhaul.store import (
    SCOPE_PARK_REASON,
    SCOPE_PARK_STATE,
    AttemptLimitReached,
    LonghaulStore,
    ScopeUnavailable,
    TaskSession,
)
from omniagentos.longhaul.terminal_evidence import (
    TERMINAL_EVIDENCE_VERSION,
    TerminalEvidenceError,
    launch_record_path,
    load_launch_ack,
    load_launch_record,
    load_terminal_record,
    prepare_evidence_root,
    publish_launch_ack,
    publish_tombstone,
    remove_terminal_records,
    terminal_record_path,
)
from omniagentos.longhaul.workbook import (
    append_checkpoint,
    init_workbook,
    read_workbook,
    workbook_status,
)
from omniagentos.scope.config import scope_locks_enabled, scope_ttl_s

LOG = logging.getLogger(__name__)

_MARKER_RE = re.compile(r"\[longhaul:(?P<attempt_id>[A-Za-z0-9_-]+)\]")
_TERMINAL_BOARD = frozenset({"done", "blocked", "cancelled"})
_TERMINAL_SESSIONS = frozenset({"completed", "failed", "cancelled", "killed"})
_DIRECT_CLI_HARNESSES = frozenset({"cli-codex", "cli-grok", "cli-gemini", "cli-kimi"})
_LEGACY_TASK_SESSION_HARNESSES = frozenset({"cli-claude", "cli-codex"})
_PROVIDER_CONFIG_ENV = {
    "codex": "CODEX_HOME",
    "grok": "GROK_HOME",
    "gemini": "GEMINI_CLI_HOME",
    "kimi": "KIMI_CODE_HOME",
}
_PROVIDER_DEFAULT_CONFIG_DIR = {
    "codex": "~/.codex",
    "grok": "~/.grok",
    "gemini": "~/.gemini",
    "kimi": "~/.kimi-code",
}
# Migration 043 is immutable. S14D/L14 migration 072 is the sole capability
# flag that makes the three newly persisted provider harnesses legal.
_PROVIDER_HARNESS_MIGRATION = 72
# Bounded poll while waiting for the threading dispatch mutex. ``sleep(0)`` only
# yields the event loop once and then tight-spins if the holder is a different
# OS thread (codex worker / foreign loop), burning a core until release.
_DISPATCH_LOCK_POLL_S = 0.01
_DEFAULT_ATTEMPT_WALL_MS = 1_800_000
_MIN_ATTEMPT_WALL_MS = 1
_TERMINAL_WRAPPER_START_GRACE_S = 5


def _as_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return dict(value)
    if isinstance(value, str):
        try:
            decoded = json.loads(value)
        except (TypeError, ValueError):
            return {}
        return decoded if isinstance(decoded, dict) else {}
    return {}


def _parse_target_location() -> Any:
    """Bind intake.fastlane.parse_target_location without the package init.

    ``import omniagentos.intake.*`` executes the intake package __init__, which
    pulls the API app and is circular in processes that haven't loaded it (the
    sessions supervisor). fastlane.py itself is pure stdlib, so fall back to a
    direct-file load there.
    """
    try:
        from omniagentos.intake.fastlane import parse_target_location

        return parse_target_location
    except ImportError:
        import importlib.util

        path = Path(__file__).resolve().parents[1] / "intake" / "fastlane.py"
        spec = importlib.util.spec_from_file_location("_longhaul_fastlane", path)
        assert spec is not None
        module = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        spec.loader.exec_module(module)
        return module.parse_target_location


def _utc_after(seconds: int) -> str:
    return (datetime.now(UTC) + timedelta(seconds=max(1, seconds))).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_time(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def _provider_for_harness(harness: Any) -> str:
    """Map a longhaul harness key to a limits.py provider table name (L-14).

    ``cli-claude`` → ``claude``, ``cli-grok`` → ``grok``, etc. Unknown / empty
    harnesses fall back to ``claude`` so the structured-first claude path stays
    the default.
    """
    text = str(harness or "").strip().lower()
    if not text:
        return "claude"
    if text.startswith("cli-"):
        text = text[4:]
    # Codex has no dedicated table; the generic patterns still run for it.
    if text in {"claude", "grok", "gemini", "kimi", "codex"}:
        return text
    return "claude"


def _events_from_session_error(session: dict[str, Any]) -> list[dict[str, Any]]:
    """Synthesize a terminal error event from the durable session error column.

    Tick reconciliation and some terminal delivery paths pass ``events=[]``
    (H-08 / F-18). Without this, a usage-limit or auth failure that was already
    persisted on ``sessions.error`` is misclassified as ``crashed`` and the
    engine immediately respawns on the same limited account.
    """
    error = str(session.get("error") or "").strip()
    if not error:
        return []
    return [
        {
            "type": "result",
            "subtype": "error",
            "is_error": True,
            "error": error,
            "result": error,
            "terminal_reason": error,
        }
    ]


def _positive_ms(value: Any, fallback: int) -> int:
    """A positive millisecond config value with a deterministic fallback."""

    if isinstance(value, bool):
        return fallback
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return fallback
    return parsed if parsed > 0 else fallback


def _bounded_wall_ms(value: Any, fallback_ms: int = _DEFAULT_ATTEMPT_WALL_MS) -> int:
    """Return an always-positive outer process wall.

    The outer process-group supervisor is the last containment boundary. A
    configured zero must therefore mean "time out immediately", never "wait
    forever"; malformed values use the positive shipped default.
    """

    if isinstance(value, bool):
        return fallback_ms
    try:
        milliseconds = int(value)
    except (TypeError, ValueError):
        return fallback_ms
    return max(_MIN_ATTEMPT_WALL_MS, milliseconds)


def _bounded_wall_seconds(value: Any) -> float:
    """The positive communicate() timeout derived from ``attempt_wall_ms``."""

    return _bounded_wall_ms(value) / 1000


def _pgid_alive(pgid: int) -> bool:
    if pgid <= 0:
        return False
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _signal_group(pgid: int, sig: int) -> None:
    """Idempotently signal the launch-time process group.

    Callers pass the PGID captured immediately after ``Popen``. Re-resolving it
    from a dead leader after timeout creates a PID-reuse race and also fails
    when a pipe-holding descendant outlives the leader.
    """

    if pgid <= 0:
        return
    try:
        os.killpg(pgid, sig)
    except (ProcessLookupError, PermissionError, OSError):
        pass


def _wait_group_gone(pgid: int, timeout_s: float) -> bool:
    """Poll group liveness for at most *timeout_s* seconds."""

    deadline = monotonic() + max(0.0, timeout_s)
    while _pgid_alive(pgid):
        remaining = deadline - monotonic()
        if remaining <= 0:
            return False
        sleep(min(0.02, remaining))
    return True


def _timeout_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)


class LonghaulEngine:
    """Own longhaul task status, executor attempts, parking, and handoffs."""

    def __init__(self, store: LonghaulStore, cfg: dict, db_path: str) -> None:
        import warnings

        warnings.warn(
            "DEPRECATION WARNING: The longhaul engine is frozen and deprecated. It will be removed in a future release.",
            DeprecationWarning,
            stacklevel=2,
        )
        logger = logging.getLogger(__name__)
        logger.warning(
            "DEPRECATION WARNING: The longhaul engine is frozen and deprecated. It will be removed in a future release."
        )
        self.store = store
        self.cfg = dict(cfg.get("longhaul", cfg))
        # H-29 containment cannot be disabled by a zero/negative YAML value.
        # Normalize once so BudgetSpec and the outer communicate() wall see the
        # exact same positive value.
        self.cfg["attempt_wall_ms"] = _bounded_wall_ms(self.cfg.get("attempt_wall_ms"))
        self.db_path = db_path
        # Test seams are deliberately private config entries.  Production callers
        # omit them and get the real implementations lazily.
        self._supervisor = self.cfg.get("_supervisor")
        self._reviewer = self.cfg.get("_reviewer")
        self._prep = self.cfg.get("_prep")
        self._codex_runner = self.cfg.get("_codex_runner")
        # Threading mutex, NOT asyncio.Lock: dispatch is entered from the
        # sessions event loop, from codex worker threads via asyncio.run, and
        # from tests that spin independent loops. asyncio.Lock is loop-bound
        # and not safe across those callers (C-06).
        self._dispatch_lock = threading.Lock()
        self._codex_threads: dict[str, threading.Thread] = {}
        # Terminal callbacks can arrive from the supervisor loop or a direct-CLI
        # worker thread. Shutdown must fence new callbacks before closing their
        # shared store, while allowing callbacks already inside the boundary to
        # finish. A threading condition works across all of those event loops.
        self._terminal_condition = threading.Condition()
        self._terminal_callbacks = 0
        self._terminal_closing = False
        self._terminal_closed = False
        self._rejected_terminal_callbacks = 0
        # attempt_id -> monotonic stamp of its last scope renewal. In memory on
        # purpose: it is a rate limiter, not state. Losing it on restart costs
        # one extra renewal, and it is pruned to the live set every tick so it
        # cannot grow with the number of attempts this process has ever seen.
        self._scope_renewed_at: dict[str, float] = {}

    @asynccontextmanager
    async def _hold_dispatch_lock(self) -> AsyncIterator[None]:
        """Serialize dispatch for every thread and every event loop.

        Polls with a small bounded ``asyncio.sleep`` so a lock held by another
        OS thread does not freeze (or tight-spin) the caller's loop, and so
        acquisition is cancellation-safe (no orphaned executor stuck inside
        blocking ``Lock.acquire()``).
        """
        while not self._dispatch_lock.acquire(blocking=False):
            await asyncio.sleep(_DISPATCH_LOCK_POLL_S)
        try:
            yield
        finally:
            self._dispatch_lock.release()

    # ------------------------------------------------------------------
    # Small durable helpers
    # ------------------------------------------------------------------

    def _task(self, task_id: str) -> dict[str, Any] | None:
        row = self.store._connection.execute(
            "SELECT * FROM board_tasks WHERE id = ?", (task_id,)
        ).fetchone()
        return dict(row) if row is not None else None

    def _attempt_by_id(self, attempt_id: str) -> TaskSession | None:
        row = self.store._connection.execute(
            "SELECT * FROM task_sessions WHERE id = ?", (attempt_id,)
        ).fetchone()
        return dict(row) if row is not None else None  # type: ignore[return-value]

    def _session(self, session_id: str) -> dict[str, Any] | None:
        row = self.store._connection.execute(
            "SELECT * FROM sessions WHERE id = ?", (session_id,)
        ).fetchone()
        return dict(row) if row is not None else None

    def _event_in_tx(
        self,
        task_id: str,
        action: str,
        payload: dict[str, Any] | None = None,
    ) -> None:
        # W2.6: a longhaul board_task IS the execution unit for this lane, so
        # execution_id = task_id. The sole caller (_transition) always holds
        # self.store._lock with a BEGIN IMMEDIATE already open, so this
        # read-then-insert is atomic -- see _next_event_sequence.
        sequence = _next_event_sequence(self.store._connection, task_id)
        self.store._connection.execute(
            "INSERT INTO events "
            "(ts, type, actor, action, target_type, target_id, payload_json, trace_id, "
            "execution_id, sequence) "
            "VALUES (?, ?, ?, ?, 'board_task', ?, ?, '', ?, ?)",
            (
                utc_now_iso(),
                "task.longhaul",
                "longhaul",
                action,
                task_id,
                json.dumps(payload or {}, separators=(",", ":"), sort_keys=True, default=str),
                task_id,
                sequence,
            ),
        )

    def _transition(
        self,
        task_id: str,
        *,
        phase: str,
        status: str | None = None,
        park_state: str | None | object = ...,
        result_ref: str | None | object = ...,
        updates: dict[str, Any] | None = None,
        action: str | None = None,
    ) -> dict[str, Any]:
        """Atomically update board ownership fields, journal JSON, and event."""

        with self.store._lock:
            self.store._begin()
            try:
                row = self.store._connection.execute(
                    "SELECT longhaul_json FROM board_tasks WHERE id = ?", (task_id,)
                ).fetchone()
                state = _as_dict(row["longhaul_json"]) if row is not None else {}
                state.update(updates or {})
                state["phase"] = phase
                assignments = ["longhaul_json = ?", "updated_at = ?"]
                values: list[Any] = [
                    json.dumps(state, separators=(",", ":"), sort_keys=True, default=str),
                    utc_now_iso(),
                ]
                if status is not None:
                    assignments.append("status = ?")
                    values.append(status)
                if park_state is not ...:
                    assignments.append("park_state = ?")
                    values.append(park_state)
                if result_ref is not ...:
                    assignments.append("result_ref = ?")
                    values.append(result_ref)
                values.append(task_id)
                self.store._connection.execute(
                    f"UPDATE board_tasks SET {', '.join(assignments)} WHERE id = ?", values
                )
                self._event_in_tx(
                    task_id,
                    action or f"longhaul.{phase}",
                    {
                        "phase": phase,
                        "status": status,
                        "park_state": park_state if park_state is not ... else None,
                    },
                )
                self.store._commit()
                return state
            except BaseException:
                self.store._rollback()
                raise

    def _update_attempt(
        self,
        attempt_id: str,
        *,
        session_id: str | None | object = ...,
        detail: dict[str, Any] | str | None = None,
    ) -> None:
        with self.store._lock:
            self.store._begin()
            try:
                assignments: list[str] = []
                values: list[Any] = []
                if session_id is not ...:
                    assignments.append("session_id = ?")
                    values.append(session_id)
                if detail is not None:
                    assignments.append("detail = ?")
                    values.append(
                        detail
                        if isinstance(detail, str)
                        else json.dumps(detail, separators=(",", ":"), sort_keys=True)
                    )
                if assignments:
                    values.append(attempt_id)
                    self.store._connection.execute(
                        f"UPDATE task_sessions SET {', '.join(assignments)} WHERE id = ?",
                        values,
                    )
                self.store._commit()
            except BaseException:
                self.store._rollback()
                raise

    def _notify(self, task_id: str, kind: str, title: str, body: str) -> None:
        try:
            from omniagentos.notifications.service import record_notification

            record_notification(
                kind=kind,
                title=title,
                body=body,
                severity="warning" if kind in {"blocked", "escalation"} else "info",
                ref_type="board_task",
                ref_id=task_id,
                payload={"task_id": task_id, "source": "longhaul"},
                db_path=self.db_path,
            )
        except Exception:  # noqa: BLE001 - notifications never own the transition.
            LOG.debug("longhaul notification failed for %s", task_id, exc_info=True)

    # ------------------------------------------------------------------
    # Dispatch
    # ------------------------------------------------------------------

    def _available_accounts(self, provider: str = "claude") -> list[dict[str, Any]]:
        # Routed through the single durable limit-state authority (WP2):
        # same enabled + cooldown filter and LRU ordering as before, now
        # provider-scoped over the generalized accounts table.
        from omniagentos.routing.limit_state import list_available_accounts

        return list_available_accounts(provider, db_path=self.db_path)

    def _provider_has_managed_accounts(self, provider: str) -> bool:
        row = self.store._connection.execute(
            "SELECT 1 FROM claude_accounts WHERE provider = ? LIMIT 1",
            (provider,),
        ).fetchone()
        return row is not None

    def _harness_persistable(self, harness: str) -> bool:
        """Fail closed until the immutable schema can persist this harness.

        Parsing sqlite_master CHECK text is brittle and lets an ad-hoc private
        schema masquerade as production. Migration 072 is the explicit,
        forward-only capability boundary agreed with L14.
        """

        if harness in _LEGACY_TASK_SESSION_HARNESSES:
            return True
        if harness not in _DIRECT_CLI_HARNESSES:
            return False
        row = self.store._connection.execute(
            "SELECT 1 FROM schema_migrations WHERE version = ?",
            (_PROVIDER_HARNESS_MIGRATION,),
        ).fetchone()
        return row is not None

    def _acceptance(self, task: dict[str, Any], state: dict[str, Any]) -> str:
        existing = state.get("acceptance")
        if isinstance(existing, str) and existing.strip():
            return existing.strip()
        if callable(self._prep):
            try:
                value = self._prep(task)
                if isinstance(value, str) and value.strip():
                    return value.strip()
            except Exception:  # noqa: BLE001 - prep has a deterministic fallback.
                LOG.debug("longhaul prep failed; using heuristic", exc_info=True)
        description = str(task.get("description") or "").strip()
        match = re.search(
            r"(?:acceptance criteria|acceptance)\s*:?\s*(.+)$",
            description,
            flags=re.IGNORECASE | re.DOTALL,
        )
        if match and match.group(1).strip():
            return match.group(1).strip()
        return (
            f"Complete the requested work and verify the result: {task.get('title') or task['id']}."
        )

    def _project_dir(self, task: dict[str, Any], state: dict[str, Any]) -> str:
        """Resolve the executor working directory.

        A *recorded* working_dir that is missing must fail closed (M-45) — never
        silently fall back to the daemon's cwd, which would let a longhaul
        executor write into the sessions process directory. When no working_dir
        was recorded, cwd remains the intentional default.
        """
        del task  # reserved for future task-level project resolution
        candidate = state.get("working_dir") or self.cfg.get("working_dir")
        if isinstance(candidate, str) and candidate.strip():
            path = Path(candidate).expanduser()
            if path.is_dir():
                return str(path.resolve())
            raise FileNotFoundError(f"longhaul working_dir missing or not a directory: {path}")
        return str(Path.cwd())

    def _dispatch_backoff_active(self, state: dict[str, Any]) -> bool:
        """True while a fast-crash (or similar) dispatch backoff is still running."""
        when = _parse_time(state.get("next_dispatch_at"))
        return when is not None and when > datetime.now(UTC)

    def _fast_crash_updates(
        self,
        state: dict[str, Any],
        attempt: TaskSession,
        *,
        kind: str,
    ) -> dict[str, Any]:
        """Compute longhaul_json updates for the L-13 fast-crash backoff.

        Attempts that die within ``fast_crash_s`` of start accumulate a
        consecutive counter and exponential ``next_dispatch_at``. Longer-lived
        attempts (or non-crash kinds) clear the counter so a healthy run resets
        the loop guard. ``fast_crash_s <= 0`` disables the feature.
        """
        threshold = self.cfg.get("fast_crash_s", 30)
        try:
            threshold_s = float(threshold)
        except (TypeError, ValueError):
            threshold_s = 30.0
        pacing_kinds = {
            "crashed",
            "killed",
            "unfinished_exit",
        }
        if threshold_s <= 0 or kind not in pacing_kinds:
            return {
                "fast_crash_count": 0,
                "next_dispatch_at": None,
            }

        started = _parse_time(attempt.get("started_at"))
        if started is None:
            return {}
        duration = (datetime.now(UTC) - started).total_seconds()
        if duration >= threshold_s:
            return {
                "fast_crash_count": 0,
                "next_dispatch_at": None,
            }

        count = int(state.get("fast_crash_count") or 0) + 1
        base = int(self.cfg.get("fast_crash_backoff_s", 5) or 5)
        cap = int(self.cfg.get("fast_crash_max_backoff_s", 300) or 300)
        backoff = min(cap, max(1, base) * (2 ** max(0, count - 1)))
        return {
            "fast_crash_count": count,
            "next_dispatch_at": _utc_after(backoff),
            "parked_detail": (
                f"fast-crash backoff {backoff}s after {count} consecutive "
                f"sub-{int(threshold_s)}s non-success attempt(s); last={kind}"
            ),
        }

    def _close_crash_with_backoff(
        self,
        task_id: str,
        attempt: TaskSession,
        *,
        action: str,
        detail: str,
        extra_updates: dict[str, Any] | None = None,
    ) -> tuple[bool, dict[str, Any]]:
        """Atomically close a reconcile crash and persist its pacing horizon.

        Used by the spawn_incomplete / direct-CLI orphan paths so every
        crash-class close stamps ``next_dispatch_at`` consistently instead of
        immediately reopening an attempt.
        """
        task = self._task(task_id)
        state = _as_dict((task or {}).get("longhaul_json"))
        attempt_detail = _as_dict(attempt.get("detail"))
        nonce = attempt_detail.get("terminal_record_nonce")
        publish_tombstone(
            self.db_path,
            attempt_id=str(attempt["id"]),
            harness=str(attempt.get("harness")) if attempt.get("harness") else None,
            provider=_provider_for_harness(attempt.get("harness"))
            if attempt.get("harness")
            else None,
            launch_nonce=str(nonce) if isinstance(nonce, str) else None,
            reason=f"attempt closed crash: {action}",
        )
        crash_updates = self._fast_crash_updates(state, attempt, kind="crashed")
        limit_reached = self._sessions_exhausted(task_id, int(attempt["seq"]))
        updates = {
            "prior_end_reason": "crashed",
            "last_attempt_id": attempt["id"],
            "active_attempt_id": None,
            # Every reconcile close owns the same restart fence as an ordinary
            # terminal callback. A dead-PID fallback must not be the one path
            # capable of opening attempt max+1.
            "max_sessions_reached": limit_reached,
            **(extra_updates or {}),
            **crash_updates,
        }
        closed, durable_state, _ = self.store.close_attempt_with_task_state(
            str(attempt["id"]),
            "crashed",
            detail,
            board_task_id=task_id,
            phase="running",
            updates=updates,
            action=action,
        )
        if closed and limit_reached:
            # The marker above is committed with the close, so a crash in this
            # follow-up is safe: dispatch/tick will complete the block later.
            self._block_for_limit(task_id)
        return closed, durable_state or state

    def _prompt(
        self,
        task: dict[str, Any],
        state: dict[str, Any],
        attempt_seq: int,
        acceptance: str,
        workbook_path: str,
    ) -> str:
        turns = list(reversed(self.store.task_turns(str(task["id"]), limit=10)))
        if attempt_seq == 0:
            category = (
                self.store.get_category(str(task["category_id"]))
                if task.get("category_id")
                else None
            )
            return initial_prompt(task, workbook_path, acceptance, category, turns)
        workbook = read_workbook(str(task["id"])) or ""
        prior = str(state.get("prior_end_reason") or "the prior executor exited")
        findings = state.get("review_findings")
        if findings:
            prior += f"\nReviewer findings: {findings}"
        return continuation_prompt(task, workbook, None, turns, prior)

    def _supervisor_instance(self) -> Any:
        if self._supervisor is None:
            from omniagentos.sessions.supervisor import SessionSupervisor

            self._supervisor = SessionSupervisor(db_path=self.db_path)
        return self._supervisor

    async def dispatch(self, task_id: str) -> None:
        """Start one durable executor attempt when the task is dispatchable."""

        async with self._hold_dispatch_lock():
            task = self._task(task_id)
            if (
                task is None
                or task.get("lane") != "longhaul"
                or str(task.get("status")) in _TERMINAL_BOARD
                or task.get("archived_at") is not None
            ):
                return
            if self.store.current_attempt(task_id) is not None:
                return

            state = _as_dict(task.get("longhaul_json"))
            if state.get("max_sessions_reached"):
                attempts = self.store.list_attempts(task_id)
                if attempts and self._sessions_exhausted(task_id, int(attempts[-1]["seq"])):
                    # A terminal callback committed its attempt/account boundary
                    # and the process died before the follow-up block. The marker
                    # is in that same state transaction, so restart cannot open
                    # attempt max+1.
                    self._block_for_limit(task_id)
                    return
            # L-13: honor fast-crash backoff before opening another attempt.
            if self._dispatch_backoff_active(state):
                return
            # A task already in_progress owns its category slot.  Only pending/open
            # tasks race the category CAS.
            if task.get("category_id") and str(task.get("status")) in {"pending", "open"}:
                if not self.store.claim_category_slot(str(task["category_id"]), task_id):
                    self._transition(
                        task_id,
                        phase="parked",
                        status="pending",
                        park_state="waiting_category",
                        updates={"parked_detail": "category capacity is full"},
                        action="longhaul.waiting_category",
                    )
                    return
                task = self._task(task_id) or task

            accounts = self._available_accounts("claude")
            workers = rank_workers(
                str(self.cfg.get("registry_path") or "var/modelintel/registry.json"),
                self.cfg,
                len(accounts),
            )
            provider_accounts: dict[str, list[dict[str, Any]]] = {"claude": accounts}
            runnable_workers = []
            for worker in workers:
                harness = str(worker["harness"])
                if not self._harness_persistable(harness):
                    # Before migration 072 a provider-only config truthfully
                    # parks for capacity; it never crashes on migration 043's
                    # CHECK and never fabricates an in-memory harness value.
                    continue
                provider = _provider_for_harness(harness)
                available = provider_accounts.setdefault(
                    provider, self._available_accounts(provider)
                )
                # Claude has always required a registered account. Other CLIs
                # retain their ambient-login fallback only when the provider has
                # no managed rows at all. Once managed, cooldown/disable state is
                # authoritative and routing must not silently bypass it.
                if harness == "cli-claude" and not available:
                    continue
                if (
                    harness != "cli-claude"
                    and self._provider_has_managed_accounts(provider)
                    and not available
                ):
                    continue
                runnable_workers.append(worker)
            workers = runnable_workers
            if not workers:
                self._transition(
                    task_id,
                    phase="parked",
                    status="in_progress",
                    park_state="waiting_capacity",
                    updates={"parked_detail": "no enabled worker capacity"},
                    action="longhaul.waiting_capacity",
                )
                self._notify(
                    task_id,
                    "blocked",
                    "Longhaul task waiting for capacity",
                    f"{task.get('title') or task_id} has no available worker.",
                )
                return

            acceptance = self._acceptance(task, state)
            if not state.get("workbook_path"):
                self._transition(
                    task_id,
                    phase="prep",
                    status="in_progress",
                    park_state=None,
                    updates={"acceptance": acceptance},
                    action="longhaul.prep",
                )
                workbook_path = init_workbook(
                    task_id,
                    str(task.get("title") or task_id),
                    str(task.get("description") or ""),
                    acceptance,
                )
                state = self._transition(
                    task_id,
                    phase="prep",
                    status="in_progress",
                    park_state=None,
                    updates={"acceptance": acceptance, "workbook_path": workbook_path},
                    action="longhaul.workbook_ready",
                )
            else:
                workbook_path = str(state["workbook_path"])

            worker = workers[0]
            worker_provider = _provider_for_harness(worker["harness"])
            worker_accounts = provider_accounts.get(worker_provider, [])
            account_id = str(worker_accounts[0]["id"]) if worker_accounts else None
            # Resolved once and reused by the claude branch below (which used to
            # recompute it) — the realm the attempt is about to claim and the
            # directory it is about to run in have to be the same directory or
            # the claim is a lie.
            try:
                project_dir = self._project_dir(task, state)
            except FileNotFoundError as exc:
                # M-45: recorded working_dir is gone — fail closed. Status
                # 'blocked' frees any category WIP this task already claimed;
                # tick re-drives waiting_category peers on the next pass. Do
                # not await dispatch here: we still hold the dispatch lock.
                self._transition(
                    task_id,
                    phase="blocked",
                    status="blocked",
                    park_state=None,
                    updates={
                        "parked_detail": str(exc)[:500],
                        "missing_working_dir": True,
                    },
                    action="longhaul.missing_working_dir",
                )
                self._notify(
                    task_id,
                    "escalation",
                    "Longhaul working directory missing",
                    str(exc)[:500],
                )
                return
            # Durable dispatch boundary: this row exists before any process/session.
            try:
                attempt = self.store.open_attempt(
                    task_id,
                    worker["harness"],
                    worker["model"],
                    account_id=account_id,
                    working_dir=project_dir,
                    max_sessions=self._max_sessions(state),
                )
            except ScopeUnavailable as exc:
                # The same shape as the category park above, for the same
                # reason: a lane that cannot get a resource parks DURABLY and
                # returns. It does not spin, and — the load-bearing half — it
                # does not hold anything while it waits, which is what keeps
                # hold-and-wait (and therefore deadlock) structurally absent.
                #
                # status stays in_progress because SCOPE_PARK_STATE is
                # 'waiting_capacity', whose documented contract in
                # claim_category_slot is "a parked task HOLDS its slot
                # (status='in_progress')". Surrendering the category slot here
                # would also be safe — this lane always takes the category slot
                # BEFORE the scope lock and never the reverse, so the two
                # resources have a total order and cannot cycle — but it would
                # make one park_state value carry two different status
                # conventions, and re-run the category CAS on every tick.
                #
                # Recovery needs no new machinery: tick() re-dispatches every
                # non-terminal longhaul task that has no live attempt, and
                # _dispatch_waiting_scope wakes this queue FIFO the moment a
                # realm is given back.
                self._transition(
                    task_id,
                    phase="parked",
                    status="in_progress",
                    park_state=SCOPE_PARK_STATE,
                    updates={
                        "parked_detail": str(exc)[:500],
                        "park_reason": SCOPE_PARK_REASON,
                        "scope_realm": exc.realm,
                        "scope_blocked_on": exc.blocked_on,
                        "scope_status": exc.status,
                    },
                    action="longhaul.waiting_scope",
                )
                return
            except AttemptLimitReached:
                # This transactional guard is the final defense when a legacy
                # row lacks max_sessions_reached or a prior daemon died before
                # it could publish the marker.
                self._block_for_limit(task_id)
                return
            marker = f"[longhaul:{attempt['id']}]"
            prompt = self._prompt(
                task,
                state,
                int(attempt["seq"]),
                acceptance,
                workbook_path,
            )
            self._transition(
                task_id,
                phase="running",
                status="in_progress",
                park_state=None,
                result_ref=attempt["id"],
                updates={
                    "active_attempt_id": attempt["id"],
                    "spawn_logged_at": utc_now_iso(),
                    "worker": {
                        "harness": worker["harness"],
                        "model": worker["model"],
                        "account_id": account_id,
                    },
                },
                action="longhaul.attempt_opened",
            )

            try:
                if worker["harness"] == "cli-claude":
                    resume_ref = state.pop("_resume_session_ref", None)
                    if isinstance(resume_ref, str) and resume_ref:
                        self._transition(
                            task_id,
                            phase="running",
                            status="in_progress",
                            updates={"_resume_session_ref": None},
                            action="longhaul.native_resume_claimed",
                        )
                    supervisor = self._supervisor_instance()
                    # Grant the goal's target location like the fast lane does
                    # (same vetted parser) — without it the session can only
                    # write inside the workbook dir and EPERMs on the actual
                    # deliverable path.
                    parse_target_location = _parse_target_location()
                    goal_text = "\n".join(
                        str(task.get(key) or "") for key in ("title", "description")
                    )
                    target = parse_target_location(goal_text)
                    write_roots = [str(Path(workbook_path).parent)]
                    if target:
                        write_roots.append(target)
                    session_id = supervisor.spawn(
                        project_dir=target or project_dir,
                        model=worker["model"],
                        prompt=prompt,
                        title=str(task.get("title") or task_id),
                        title_prefix=marker,
                        extra_write_roots=write_roots,
                        granted_roots=write_roots,
                        orchestrator_owned=True,
                        resume_session_ref=resume_ref if isinstance(resume_ref, str) else None,
                        # D8: longhaul's idle threshold rides the session row so
                        # the A2 reaper enforces lane policy — written at row
                        # creation (spawn kwarg), no post-spawn race window.
                        idle_minutes=self._idle_minutes(),
                    )
                    self._update_attempt(attempt["id"], session_id=session_id)
                    self._transition(
                        task_id,
                        phase="running",
                        status="in_progress",
                        park_state=None,
                        result_ref=session_id,
                        updates={"session_id": session_id},
                        action="longhaul.spawned",
                    )
                else:
                    self._start_cli(attempt, task, state, prompt, workbook_path, marker)
                # The spawn prompt embeds the last steering turns — receipt them
                # so a delivered-in-prompt turn cannot re-trigger the pending
                # check at completion forever.
                for turn in self.store.pending_steering(task_id):
                    ref = turn.get("id") or (task_id, int(turn["seq"]))
                    self.store.mark_turn_delivered(ref, f"prompt:{attempt['id']}")
            except Exception as exc:
                # Leave the open/no-session attempt intact.  tick() is the single
                # crash-window reconciler and will close+redispatch it.
                self._transition(
                    task_id,
                    phase="running",
                    status="in_progress",
                    updates={"spawn_error": str(exc)[:500]},
                    action="longhaul.spawn_failed",
                )
                LOG.warning("longhaul spawn failed for %s: %s", task_id, exc)

    def _start_cli(
        self,
        attempt: TaskSession,
        task: dict[str, Any],
        state: dict[str, Any],
        prompt: str,
        workbook_path: str,
        marker: str,
    ) -> None:
        # Preserve the existing Codex test/integration seam. Other providers
        # intentionally have no runner shortcut: their acceptance coverage must
        # exercise adapter resolution, process launch, persistence, and terminal
        # classification through the public dispatch path.
        if attempt["harness"] == "cli-codex" and callable(self._codex_runner):
            result = self._codex_runner(attempt, task, prompt, workbook_path)
            detail = result if isinstance(result, dict) else {"started": utc_now_iso()}
            self._update_attempt(attempt["id"], detail=detail)
            return

        provider = _provider_for_harness(attempt["harness"])
        thread = threading.Thread(
            target=self._run_cli_process,
            args=(attempt, task, state, prompt, workbook_path, marker),
            name=f"longhaul-{provider}-{attempt['id']}",
            daemon=True,
        )
        self._codex_threads[attempt["id"]] = thread
        self._update_attempt(
            attempt["id"], detail={"started": utc_now_iso(), "thread": thread.name}
        )
        thread.start()

    def _provider_config_dir(self, provider: str, account_id: Any) -> tuple[str, str]:
        """Return (environment home, concrete writable state directory)."""

        configured: str | None = None
        if account_id:
            row = self.store._connection.execute(
                "SELECT config_dir FROM claude_accounts WHERE id = ? AND provider = ? LIMIT 1",
                (str(account_id), provider),
            ).fetchone()
            if row is not None and row["config_dir"]:
                configured = str(row["config_dir"])
        root = Path(
            os.path.expanduser(configured or _PROVIDER_DEFAULT_CONFIG_DIR[provider])
        ).resolve(strict=False)
        # GEMINI_CLI_HOME replaces HOME and Gemini appends ".gemini".
        if provider == "gemini":
            if root.name == ".gemini":
                return str(root.parent), str(root)
            return str(root), str(root / ".gemini")
        return str(root), str(root)

    def _bounded_process_group_shutdown(
        self,
        proc: subprocess.Popen[str],
        pgid: int,
        timeout: subprocess.TimeoutExpired,
        *,
        terminal_detail: str = "attempt wall timeout",
        terminal_rc: int = 124,
    ) -> tuple[str, str, int]:
        """TERM → grace → KILL → bounded pipe drain/reap for one process group."""

        stdout = _timeout_text(timeout.output)
        stderr = _timeout_text(timeout.stderr)
        term_s = _positive_ms(self.cfg.get("attempt_term_grace_ms"), 3_000) / 1000
        reap_s = _positive_ms(self.cfg.get("attempt_kill_reap_ms"), 3_000) / 1000

        _signal_group(pgid, signal.SIGTERM)
        term_deadline = monotonic() + term_s
        try:
            # This is also the pipe-holding-descendant probe: communicate does
            # not return until every inherited writer closes.
            stdout, stderr = proc.communicate(timeout=term_s)
        except subprocess.TimeoutExpired as exc:
            stdout = _timeout_text(exc.output) or stdout
            stderr = _timeout_text(exc.stderr) or stderr
        else:
            # The leader may exit while a descendant that did not inherit the
            # pipes keeps running. Give the full group the remainder of TERM
            # grace before escalating.
            _wait_group_gone(pgid, max(0.0, term_deadline - monotonic()))

        if _pgid_alive(pgid):
            _signal_group(pgid, signal.SIGKILL)

        reap_deadline = monotonic() + reap_s
        if proc.poll() is None:
            try:
                stdout, stderr = proc.communicate(timeout=reap_s)
            except subprocess.TimeoutExpired as exc:
                stdout = _timeout_text(exc.output) or stdout
                stderr = _timeout_text(exc.stderr) or stderr
        # If communicate already reaped the leader, this wait still bounds the
        # non-pipe-holding descendant case. There is no final unbounded wait.
        _wait_group_gone(pgid, max(0.0, reap_deadline - monotonic()))
        # Crossing the wall budget is terminal failure even when the leader
        # cooperates with TERM and exits 0. Otherwise a timed-out CLI could be
        # misreported as a clean unfinished_exit.
        rc = terminal_rc
        shutdown_detail = f"{terminal_detail}; process group received TERM then bounded KILL/reap"
        stderr = f"{stderr.rstrip()}\n{shutdown_detail}".strip()
        return stdout, stderr, rc

    def _load_attempt_terminal_record(
        self,
        attempt: TaskSession,
        detail: dict[str, Any],
    ) -> dict[str, Any] | None:
        """Load only the record bound to this attempt's durable launch nonce."""

        launch_nonce = detail.get("terminal_record_nonce")
        if not isinstance(launch_nonce, str) or not launch_nonce:
            return None
        pid = detail.get("pid")
        expected_pid = pid if isinstance(pid, int) and pid > 0 else None
        try:
            return load_terminal_record(
                self.db_path,
                attempt_id=str(attempt["id"]),
                harness=str(attempt["harness"]),
                provider=_provider_for_harness(attempt.get("harness")),
                launch_nonce=launch_nonce,
                expected_wrapper_pid=expected_pid,
            )
        except TerminalEvidenceError as exc:
            if "missing" not in str(exc):
                LOG.warning(
                    "refusing terminal evidence for attempt %s: %s",
                    attempt["id"],
                    exc,
                )
            return None

    def _load_attempt_wrapper_pid(
        self,
        attempt: TaskSession,
        detail: dict[str, Any],
    ) -> int | None:
        """Recover the wrapper PID when the original daemon died after Popen."""

        launch_nonce = detail.get("terminal_record_nonce")
        if not isinstance(launch_nonce, str) or not launch_nonce:
            return None
        try:
            record = load_launch_record(
                self.db_path,
                attempt_id=str(attempt["id"]),
                harness=str(attempt["harness"]),
                provider=_provider_for_harness(attempt.get("harness")),
                launch_nonce=launch_nonce,
            )
        except TerminalEvidenceError as exc:
            if "missing" not in str(exc):
                LOG.warning(
                    "refusing terminal launch identity for attempt %s: %s",
                    attempt["id"],
                    exc,
                )
            return None
        return int(record["wrapper_pid"])

    def _terminal_payload_from_output(
        self,
        attempt: TaskSession,
        task: dict[str, Any],
        marker: str,
        stdout: str,
        stderr: str,
        rc: int,
    ) -> tuple[dict[str, Any], list[dict[str, Any]], int]:
        """Parse one trusted bounded output record into the public terminal shape."""

        from omniagentos.adapters.common import CliAdapter
        from omniagentos.adapters.registry import resolve_adapter

        provider = _provider_for_harness(attempt["harness"])
        # Longhaul attempts are schema-constrained (072 CHECK on
        # task_sessions.harness) to cli-* harnesses, and every cli-* registry
        # entry is a CliAdapter — the class that owns the private _parse /
        # _command / _sandboxed_launch seams these terminal paths drive.
        adapter = cast(CliAdapter, resolve_adapter(HarnessType(str(attempt["harness"]))))
        events: list[dict[str, Any]] = []
        session_ref: str | None = None
        if rc == 0:
            try:
                parsed = adapter._parse(stdout)
                session_ref = parsed.session_ref
                events.append({"type": "result", "result": parsed.text, "is_error": False})
            except Exception as exc:  # noqa: BLE001
                rc = 1
                stderr = str(exc)
        if rc != 0:
            error_text = "\n".join(value.strip() for value in (stdout, stderr) if value.strip())
            events.append(
                {
                    "type": "result",
                    "subtype": "error",
                    "is_error": True,
                    "error": error_text[-2000:] or f"{provider} exited {rc}",
                    "result": error_text[-2000:],
                }
            )
        synthetic = {
            "id": f"{provider}-{attempt['id']}",
            "title": f"{marker} {task.get('title') or task['id']}",
            "state": "completed" if rc == 0 else "failed",
            "session_ref": session_ref,
            "cost_usd": 0,
            "todos_json": "[]",
            "files_json": "[]",
        }
        return synthetic, events, rc

    def _persist_terminal_evidence(
        self,
        attempt: TaskSession,
        launch_detail: dict[str, Any],
        synthetic: dict[str, Any],
        events: list[dict[str, Any]],
        rc: int,
    ) -> None:
        """Journal a compact DB copy; the fsynced record remains restart authority."""

        durable_events: list[dict[str, Any]] = []
        for event in events:
            durable_event: dict[str, Any] = {}
            for key in (
                "type",
                "subtype",
                "is_error",
                "error",
                "result",
                "terminal_reason",
            ):
                value = event.get(key)
                if value is None:
                    continue
                durable_event[key] = value[-4000:] if isinstance(value, str) else value
            durable_events.append(durable_event)
        self._update_attempt(
            attempt["id"],
            detail={
                **launch_detail,
                "terminal_evidence": {
                    "recorded_at": utc_now_iso(),
                    "state": synthetic["state"],
                    "session_ref": synthetic.get("session_ref"),
                    "rc": rc,
                    "events": durable_events,
                },
            },
        )

    async def _replay_attempt_terminal_record(
        self,
        attempt: TaskSession,
        task: dict[str, Any],
        detail: dict[str, Any],
    ) -> bool:
        """Consume one verified wrapper record through the ordinary close CAS."""

        terminal_record = self._load_attempt_terminal_record(attempt, detail)
        if terminal_record is None:
            return False
        marker = f"[longhaul:{attempt['id']}]"
        synthetic, record_events, record_rc = self._terminal_payload_from_output(
            attempt,
            task,
            marker,
            str(terminal_record["stdout"]),
            str(terminal_record["stderr"]),
            int(terminal_record["returncode"]),
        )
        self._persist_terminal_evidence(
            attempt,
            detail,
            synthetic,
            record_events,
            record_rc,
        )
        await self.on_session_terminal(synthetic, record_events, record_rc)
        ended = self._attempt_by_id(str(attempt["id"]))
        nonce = detail.get("terminal_record_nonce")
        if ended is not None and ended.get("ended_at") is not None and isinstance(nonce, str):
            remove_terminal_records(self.db_path, str(attempt["id"]), nonce)
        return True

    def _attempt_wall_expired(self, attempt: TaskSession) -> bool:
        started = _parse_time(attempt.get("started_at"))
        if started is None:
            return True
        wall = timedelta(milliseconds=_bounded_wall_ms(self.cfg.get("attempt_wall_ms")))
        return datetime.now(UTC) >= started + wall

    def _shutdown_recovered_process_group(self, pgid: int) -> None:
        """Bound a direct-CLI group whose original supervising daemon vanished."""

        term_s = _positive_ms(self.cfg.get("attempt_term_grace_ms"), 3_000) / 1000
        reap_s = _positive_ms(self.cfg.get("attempt_kill_reap_ms"), 3_000) / 1000
        _signal_group(pgid, signal.SIGTERM)
        if not _wait_group_gone(pgid, term_s):
            _signal_group(pgid, signal.SIGKILL)
            _wait_group_gone(pgid, reap_s)

    def _run_cli_process(
        self,
        attempt: TaskSession,
        task: dict[str, Any],
        state: dict[str, Any],
        prompt: str,
        workbook_path: str,
        marker: str,
    ) -> None:
        """Run a non-Claude CLI in a bounded dedicated process group."""

        from omniagentos.adapters.common import (
            CliAdapter,
            _scrubbed_env,
            disable_inner_sandbox,
        )
        from omniagentos.adapters.registry import resolve_adapter
        from omniagentos.runner import sandbox

        provider = _provider_for_harness(attempt["harness"])
        # Same 072-CHECK narrowing as _terminal_payload_from_output: longhaul
        # harnesses always resolve to CliAdapter subclasses.
        adapter = cast(CliAdapter, resolve_adapter(HarnessType(str(attempt["harness"]))))
        proc: subprocess.Popen[str] | None = None
        stdout = ""
        stderr = ""
        rc = 1
        pgid = 0
        launch_detail: dict[str, Any] = {}
        try:
            working_dir = self._project_dir(task, state)
            elevated = str(attempt["harness"]) in {
                str(value) for value in (self.cfg.get("unattended_elevated_harnesses") or [])
            }
            agent_input = AgentInput(
                run_id=f"longhaul-{attempt['id']}",
                task_id=str(task["id"]),
                prompt=prompt,
                working_dir=working_dir,
                model=str(attempt["model"]),
                budget=BudgetSpec(wall_ms_max=self.cfg.get("attempt_wall_ms")),
                metadata={
                    "sandbox": {"level": "workspace_write"},
                    "extra_dirs": [str(Path(workbook_path).parent)],
                    # Empty by default. This is the same explicit operator
                    # elevation understood by CliAdapter; it is needed for
                    # force-auto Kimi and no-sandbox hosts.
                    "cli_unattended_elevated": elevated,
                },
            )
            refusal = adapter._refuse_unattended_launch(agent_input)
            if refusal is not None:
                raise RuntimeError(str(refusal.error or "unattended CLI launch refused"))

            env = _scrubbed_env()
            command = adapter._command(agent_input, prompt, None)
            resolved_cli = shutil.which(command[0], path=env.get("PATH"))
            if resolved_cli is not None:
                # Resolve once before the launch/sandbox boundary. This removes
                # a background-thread PATH race and makes the PID identity
                # correspond to the executable routing actually selected.
                command[0] = resolved_cli
            if sandbox.wrap_available(command, working_dir):
                command = disable_inner_sandbox(command, provider)
            config_env_dir, config_state_dir = self._provider_config_dir(
                provider, attempt.get("account_id")
            )
            launch = adapter._sandboxed_launch(
                command,
                working_dir,
                [str(Path(workbook_path).parent), config_state_dir],
                provider=provider,
                provider_config_dir=config_state_dir,
            )
            env[_PROVIDER_CONFIG_ENV[provider]] = config_env_dir
            if provider == "gemini":
                env["GEMINI_CLI_TRUST_WORKSPACE"] = "true"
            prepare_evidence_root(self.db_path)
            launch_nonce = secrets.token_hex(16)
            record_path = terminal_record_path(self.db_path, str(attempt["id"]), launch_nonce)
            started_record_path = launch_record_path(self.db_path, str(attempt["id"]), launch_nonce)
            # Persist the unpredictable launch identity before Popen. If this
            # daemon dies before it can store a PID, a completed wrapper record
            # is still bound to the exact attempt by this nonce.
            launch_detail = {
                "provider": provider,
                "executable": command[0],
                "provider_launcher": launch[0],
                "terminal_record_version": TERMINAL_EVIDENCE_VERSION,
                "terminal_record_nonce": launch_nonce,
                "started": utc_now_iso(),
            }
            self._update_attempt(attempt["id"], detail=launch_detail)
            evidence_launch = [
                sys.executable,
                str(Path(__file__).with_name("terminal_evidence.py")),
                "--record-path",
                str(record_path),
                "--launch-record-path",
                str(started_record_path),
                "--attempt-id",
                str(attempt["id"]),
                "--harness",
                str(attempt["harness"]),
                "--provider",
                provider,
                "--launch-nonce",
                launch_nonce,
                "--",
                *launch,
            ]
            proc = subprocess.Popen(
                evidence_launch,
                stdin=subprocess.PIPE if provider == "codex" else subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                cwd=working_dir,
                start_new_session=True,
                env=env,
            )
            # start_new_session makes the leader's PID its PGID. Store that
            # identity immediately; getpgid can legitimately race a very fast
            # exit, but the launch-time group id is still known.
            pgid = proc.pid
            try:
                observed_pgid = os.getpgid(proc.pid)
            except ProcessLookupError:
                observed_pgid = pgid
            if observed_pgid > 0:
                pgid = observed_pgid

            start_wait = time.monotonic()
            started_record = None
            wrapper_timeout = max(1.0, float(self.cfg.get("spawn_grace_s") or 5.0))
            while time.monotonic() - start_wait < wrapper_timeout:
                try:
                    started_record = load_launch_record(
                        self.db_path,
                        attempt_id=str(attempt["id"]),
                        harness=str(attempt["harness"]),
                        provider=provider,
                        launch_nonce=launch_nonce,
                    )
                    if started_record is not None:
                        break
                except TerminalEvidenceError as exc:
                    LOG.debug("load_launch_record error: %s", exc)
                if proc.poll() is not None:
                    LOG.debug("proc.poll() was not None: %s", proc.poll())
                    break
                time.sleep(0.01)

            current = self.store.current_attempt(str(task["id"]))
            if (
                started_record is not None
                and current is not None
                and str(current["id"]) == str(attempt["id"])
            ):
                wrapper_pid = int(started_record.get("wrapper_pid") or proc.pid)
                publish_launch_ack(
                    self.db_path,
                    attempt_id=str(attempt["id"]),
                    harness=str(attempt["harness"]),
                    provider=provider,
                    launch_nonce=launch_nonce,
                )
                launch_detail = {
                    **launch_detail,
                    "pid": proc.pid,
                    "wrapper_pid": wrapper_pid,
                    "pgid": pgid,
                    "launcher": evidence_launch[0],
                    "ack": True,
                }
                self._update_attempt(attempt["id"], detail=launch_detail)
            else:
                publish_tombstone(
                    self.db_path,
                    attempt_id=str(attempt["id"]),
                    harness=str(attempt["harness"]),
                    provider=provider,
                    launch_nonce=launch_nonce,
                    reason="launch unacknowledged or attempt replaced",
                )
                if proc.poll() is None:
                    try:
                        proc.kill()
                    except (OSError, ProcessLookupError):
                        pass
                raise RuntimeError("direct CLI launch unacknowledged or attempt replaced")

            timeout_s = _bounded_wall_seconds(self.cfg.get("attempt_wall_ms"))
            wrapper_stdout, wrapper_stderr = proc.communicate(
                prompt if provider == "codex" else None,
                timeout=timeout_s,
            )
            wrapper_rc = int(proc.returncode or 0)
            record = load_terminal_record(
                self.db_path,
                attempt_id=str(attempt["id"]),
                harness=str(attempt["harness"]),
                provider=provider,
                launch_nonce=launch_nonce,
                expected_wrapper_pid=proc.pid,
            )
            stdout = str(record["stdout"])
            stderr = str(record["stderr"])
            rc = int(record["returncode"])
            print(
                f"\n[DEBUG _run_cli_process] wrapper_rc={wrapper_rc} rc={rc} stdout={stdout!r} stderr={stderr!r}\n"
            )
            if wrapper_rc == 0 and rc != 0:
                raise RuntimeError("terminal evidence wrapper hid provider failure")
            if wrapper_stdout.strip() or wrapper_stderr.strip():
                LOG.debug(
                    "terminal evidence wrapper emitted diagnostics for %s: %s",
                    attempt["id"],
                    (wrapper_stdout + "\n" + wrapper_stderr)[-1000:],
                )
        except subprocess.TimeoutExpired as exc:
            if proc is not None and pgid > 0:
                stdout, stderr, rc = self._bounded_process_group_shutdown(proc, pgid, exc)
            else:
                stderr = "attempt wall timeout before process-group supervision"
        except Exception as exc:  # noqa: BLE001 - converted to a terminal callback.
            failure_detail = str(exc)
            if proc is not None:
                if pgid > 0 and _pgid_alive(pgid):
                    synthetic_timeout = subprocess.TimeoutExpired(
                        getattr(proc, "args", provider), 0
                    )
                    stdout, bounded_stderr, rc = self._bounded_process_group_shutdown(
                        proc,
                        pgid,
                        synthetic_timeout,
                        terminal_detail="CLI launch/runtime failure",
                        terminal_rc=1,
                    )
                    failure_detail = (f"{bounded_stderr}\nlaunch/runtime failure: {exc}").strip()
                elif proc.poll() is None:
                    try:
                        stdout, bounded_stderr = proc.communicate(
                            timeout=_positive_ms(self.cfg.get("attempt_kill_reap_ms"), 3_000) / 1000
                        )
                        rc = int(proc.returncode or 1)
                        failure_detail = f"{bounded_stderr}\n{exc}".strip()
                    except subprocess.TimeoutExpired:
                        try:
                            proc.kill()
                        except (OSError, ProcessLookupError):
                            pass
            stderr = failure_detail
        synthetic, events, rc = self._terminal_payload_from_output(
            attempt,
            task,
            marker,
            stdout,
            stderr,
            rc,
        )
        # Journal direct-CLI terminal evidence before applying the close. If the
        # daemon dies between those two commits, restart reconciliation can
        # replay the same public classifier and atomically close+cool/disable;
        # it must not downgrade a known provider terminal to an orphan crash.
        try:
            self._persist_terminal_evidence(
                attempt,
                launch_detail,
                synthetic,
                events,
                rc,
            )
        except Exception:  # noqa: BLE001 - callback may still durably finish.
            LOG.exception(
                "%s terminal evidence persistence failed for %s",
                provider,
                attempt["id"],
            )
        try:
            asyncio.run(self.on_session_terminal(synthetic, events, rc))
        except Exception:  # noqa: BLE001 - tick will reconcile the open attempt.
            LOG.exception("%s terminal callback failed for %s", provider, attempt["id"])
        finally:
            try:
                ended = self._attempt_by_id(str(attempt["id"]))
            except RuntimeError:
                ended = None
            nonce = launch_detail.get("terminal_record_nonce")
            if ended is not None and ended.get("ended_at") is not None and isinstance(nonce, str):
                remove_terminal_records(self.db_path, str(attempt["id"]), nonce)
            self._codex_threads.pop(attempt["id"], None)

    # ------------------------------------------------------------------
    # Terminal state machine
    # ------------------------------------------------------------------

    @staticmethod
    def attempt_id_from_title(title: Any) -> str | None:
        match = _MARKER_RE.search(str(title or ""))
        return match.group("attempt_id") if match else None

    async def on_session_terminal(self, session: dict, events: list[dict], rc: int) -> None:
        """Admit one terminal callback unless ordered shutdown has started."""
        with self._terminal_condition:
            if self._terminal_closing:
                self._rejected_terminal_callbacks += 1
                LOG.warning(
                    "longhaul terminal callback rejected during shutdown: %s",
                    session.get("title"),
                )
                return
            self._terminal_callbacks += 1
        try:
            await self._on_session_terminal_admitted(session, events, rc)
        finally:
            with self._terminal_condition:
                self._terminal_callbacks -= 1
                if self._terminal_callbacks == 0:
                    self._terminal_condition.notify_all()

    async def _on_session_terminal_admitted(
        self, session: dict, events: list[dict], rc: int
    ) -> None:
        """Close one attempt exactly once, checkpoint it, and choose a successor."""

        attempt_id = self.attempt_id_from_title(session.get("title"))
        if attempt_id is None:
            return
        attempt = self._attempt_by_id(attempt_id)
        if attempt is None:
            return
        task_id = str(attempt["board_task_id"])
        task = self._task(task_id)
        if task is None:
            return

        session_state = str(session.get("state") or "").lower()
        killed_by = str(session.get("killed_by") or "").strip().lower()
        # D7 killed_by discrimination: only an OPERATOR's cancel supersedes the
        # task. `kill_requested` alone is NOT operator intent — the A2 reaper
        # sets it too (killed_by='idle-reaper'/'budget'), and those kills must
        # route through the killed branch below so the engine (sole respawn
        # owner) spawns a successor. Blocklist on purpose: any unknown killer
        # respawns rather than silently cancelling the task.
        killed_by_operator = session_state == "cancelled" or killed_by in {
            "operator",
            "cancel_requested",
        }
        # The operator/killed branches build partial shapes (no "kind" key —
        # their kind is decided right here, not classified), so the variable is
        # a union rather than a plain Classification.
        classification: Classification | dict[str, str | None]
        if killed_by_operator:
            kind = "superseded"
            classification = {"detail": "operator cancelled the longhaul task", "reset_at": None}
        elif session_state == "killed":
            kind = "killed"
            classification = {"detail": "session process was killed", "reset_at": None}
        else:
            # H-08: tick reconcile and some delivery paths pass events=[]. Use
            # the durable session.error text so usage/auth terminals are not
            # misread as crashes. L-14: pass the harness→provider so Grok /
            # Gemini / Kimi pattern tables are reachable.
            classify_events = list(events) if events else _events_from_session_error(session)
            classification = classify_terminal(
                classify_events,
                rc,
                float(session.get("cost_usd") or 0),
                provider=_provider_for_harness(attempt.get("harness")),
            )
            kind = str(classification["kind"])
            if kind == "completed" and workbook_status(task_id) != "DONE":
                kind = "unfinished_exit"

        state = _as_dict(task.get("longhaul_json"))
        account_effect: dict[str, str] | None = None
        if kind == "usage_limited" and attempt.get("account_id"):
            account_effect = {
                "kind": "usage_limited",
                "cooldown_until": str(
                    classification.get("reset_at")
                    or _utc_after(int(self.cfg.get("default_cooldown_s", 3600)))
                ),
                "detail": str(classification.get("detail") or "usage limited")[:1000],
            }
        elif kind == "auth_failed" and attempt.get("account_id"):
            account_effect = {
                "kind": "auth_failed",
                "detail": str(classification.get("detail") or "authentication failed")[:1000],
            }
        # L-13: compute the pacing update before the close, then publish it in
        # the SAME transaction as ended_at. No tick/process can observe the old
        # "no attempt, no horizon" gap and open an early successor.
        crash_updates = self._fast_crash_updates(state, attempt, kind=kind)
        terminal_updates = {
            "prior_end_reason": kind,
            "last_attempt_id": attempt_id,
            "active_attempt_id": None,
            "session_ref": session.get("session_ref"),
            **crash_updates,
        }
        limit_reached = kind not in {"completed", "superseded"} and self._sessions_exhausted(
            task_id, int(attempt["seq"])
        )
        if kind != "completed":
            # Durable restart fence for the small window between terminal close
            # and the follow-up blocked transition.
            terminal_updates["max_sessions_reached"] = limit_reached
        if kind == "completed":
            terminal_updates["review_started_at"] = utc_now_iso()
        closed, durable_state, account_effect_applied = self.store.close_attempt_with_task_state(
            attempt_id,
            kind,
            str(classification.get("detail") or "")[:1000],
            board_task_id=task_id,
            phase="reviewing" if kind == "completed" else "running",
            action=f"longhaul.terminal.{kind}",
            updates=terminal_updates,
            account_effect=account_effect,
        )
        # The close CAS is the idempotency boundary. Duplicate terminal
        # delivery performs no checkpoint, cooldown, notification, or spawn.
        if not closed or durable_state is None:
            return

        append_checkpoint(
            task_id,
            int(attempt["seq"]),
            str(session.get("todos_json") or "[]"),
            str(session.get("files_json") or "[]"),
            kind,
        )

        if kind == "auth_failed" and account_effect_applied and attempt.get("account_id"):
            # Preserve the existing stop-the-line operator notification even
            # when this was the final allowed attempt. Duplicate delivery loses
            # the close CAS above and therefore cannot emit it twice.
            from omniagentos.routing.limit_state import _notify_auth_failure

            row = self.store._connection.execute(
                "SELECT label FROM claude_accounts WHERE id = ?",
                (str(attempt["account_id"]),),
            ).fetchone()
            label = str(row["label"]) if row is not None else None
            _notify_auth_failure(
                _provider_for_harness(attempt.get("harness")),
                str(attempt["account_id"]),
                label,
                str(classification.get("detail") or "authentication failed"),
                self.db_path,
            )

        if kind == "superseded":
            self._transition(
                task_id,
                phase="blocked",
                status="cancelled",
                park_state=None,
                action="longhaul.cancelled",
            )
            return

        if kind == "completed":
            self._report_provider_outcome(attempt, "ok", "")
            await self._review_completed(task_id)
            return

        if limit_reached:
            self._block_for_limit(task_id)
            return

        if kind == "usage_limited":
            # The cooldown was committed in the same transaction as the close
            # CAS above. A crash can expose both or neither, and a duplicate
            # callback cannot refresh/re-apply it.
            await self.dispatch(task_id)
            return

        if kind == "auth_failed":
            # Stop-the-line disable was atomic with the attempt close.
            await self.dispatch(task_id)
            return

        if kind in {"crashed", "killed"}:
            current = self._task(task_id) or task
            state = _as_dict(current.get("longhaul_json"))
            # L-13: while fast-crash backoff is active, do not open a successor
            # immediately — tick will re-enter dispatch after next_dispatch_at.
            if self._dispatch_backoff_active(state):
                return
            ref = session.get("session_ref")
            healthy = self._account_healthy(attempt.get("account_id"))
            if (
                attempt["harness"] == "cli-claude"
                and isinstance(ref, str)
                and ref
                and healthy
                and not bool(state.get("native_resume_used"))
            ):
                self._transition(
                    task_id,
                    phase="running",
                    status="in_progress",
                    updates={
                        "native_resume_used": True,
                        "_resume_session_ref": ref,
                        "prior_end_reason": kind,
                    },
                    action="longhaul.native_resume_scheduled",
                )
            await self.dispatch(task_id)
            return

        if kind == "unfinished_exit":
            # A clean CLI turn still proves account health even though the
            # workbook says the task is unfinished. The durable pacing horizon
            # above prevents a zero-exit loop from burning max_sessions.
            self._report_provider_outcome(attempt, "ok", "")

        # unfinished_exit and any future nonterminal classifier outcome try a
        # fresh executor; dispatch itself refuses until next_dispatch_at.
        await self.dispatch(task_id)

    def close(self) -> None:
        """Fence terminal delivery, drain admitted callbacks, then close the store."""
        with self._terminal_condition:
            if self._terminal_closed:
                return
            if self._terminal_closing:
                self._terminal_condition.wait_for(
                    lambda: self._terminal_closed or not self._terminal_closing
                )
                if self._terminal_closed:
                    return
            self._terminal_closing = True
            while self._terminal_callbacks:
                self._terminal_condition.wait()
            try:
                self.store.close()
            except BaseException:
                self._terminal_closing = False
                self._terminal_condition.notify_all()
                raise
            self._terminal_closed = True
            self._terminal_condition.notify_all()

    def _report_provider_outcome(self, attempt: TaskSession, outcome: str, detail: str) -> None:
        account_id = attempt.get("account_id")
        if not account_id:
            return
        try:
            from omniagentos.routing.limit_state import report_outcome

            report_outcome(
                _provider_for_harness(attempt.get("harness")),
                str(account_id),
                outcome,
                detail,
                db_path=self.db_path,
            )
        except Exception:  # noqa: BLE001 - terminal state already committed.
            LOG.exception(
                "longhaul could not report %s for attempt %s",
                outcome,
                attempt.get("id"),
            )

    def _idle_minutes(self) -> float:
        """Longhaul's per-session idle threshold for the A2 reaper (D8).

        Longhaul builds legitimately run long quiet stretches; 45 minutes
        (config ``idle_minutes``) overrides the reaper's 15-minute global
        default on every longhaul session row — fresh spawns AND native
        resumes alike, since both go through the single spawn call."""
        raw = self.cfg.get("idle_minutes", 45)
        # bool is an int subclass: a YAML `idle_minutes: true` would otherwise
        # coerce to a 1.0-minute reap threshold. Treat it as invalid config.
        if isinstance(raw, bool):
            return 45.0
        try:
            value = float(raw)
        except (TypeError, ValueError):
            value = 45.0
        return value if value > 0 else 45.0

    def _max_sessions(self, state: dict[str, Any]) -> int:
        raw = state.get("max_sessions")
        if raw is None:
            raw = self.cfg.get("max_sessions", 8)
        if isinstance(raw, bool):
            return 1
        try:
            maximum = int(raw)
        except (TypeError, ValueError):
            maximum = 8
        return max(1, maximum)

    def _sessions_exhausted(self, task_id: str, seq: int) -> bool:
        state = self.store.get_longhaul_json(task_id) or {}
        return seq + 1 >= self._max_sessions(state)

    def _block_for_limit(self, task_id: str) -> None:
        attempts = self.store.list_attempts(task_id)
        summary = ", ".join(
            f"#{int(item['seq']) + 1}:{item.get('end_reason') or 'open'}" for item in attempts
        )
        self._transition(
            task_id,
            phase="blocked",
            status="blocked",
            park_state=None,
            updates={"parked_detail": f"maximum sessions reached ({summary})"},
            action="longhaul.max_sessions",
        )
        self._notify(
            task_id,
            "escalation",
            "Longhaul task needs intervention",
            f"Maximum executor sessions reached. {summary}",
        )

    def _account_healthy(self, account_id: Any) -> bool:
        if not account_id:
            return False
        row = self.store._connection.execute(
            "SELECT enabled, cooldown_until FROM claude_accounts WHERE id = ?",
            (str(account_id),),
        ).fetchone()
        if row is None or not bool(row["enabled"]):
            return False
        cooldown = _parse_time(row["cooldown_until"])
        return cooldown is None or cooldown <= datetime.now(UTC)

    async def _review_completed(self, task_id: str) -> None:
        task = self._task(task_id)
        if task is None:
            return
        state = _as_dict(task.get("longhaul_json"))
        # Claim the review window BEFORE the (slow) reviewer runs so peer
        # engine processes' ticks leave this task alone (see tick's gate).
        self._transition(
            task_id,
            phase="reviewing",
            status="in_progress",
            park_state=None,
            updates={"review_started_at": utc_now_iso()},
            action="longhaul.reviewing",
        )
        review_cfg = _as_dict(self.cfg.get("review"))
        verdict: dict[str, str] | None
        if not review_cfg.get("enabled", True):
            verdict = {"verdict": "confirm", "feedback": "review disabled"}
        else:
            verdict = await asyncio.to_thread(self._run_review, task, state)

        value = str(verdict.get("verdict") or "").lower() if verdict else ""
        feedback = str(verdict.get("feedback") or "") if verdict else ""
        if value == "confirm":
            # Defense in depth against dispatch races: a stray attempt opened
            # during the review window must not survive the task going done.
            stray = self.store.current_attempt(task_id)
            if stray is not None:
                self.store.close_attempt(
                    str(stray["id"]), "superseded", "task finished during review"
                )
                stray_session = (
                    self._session(str(stray["session_id"])) if stray.get("session_id") else None
                )
                if (
                    stray_session is not None
                    and str(stray_session.get("state")) not in _TERMINAL_SESSIONS
                ):
                    try:
                        from omniagentos.sessions.dal import SessionsDal

                        dal = SessionsDal(self.db_path)
                        try:
                            dal.request_kill(str(stray_session["id"]))
                        finally:
                            dal.close()
                    except Exception:  # noqa: BLE001 - the A2 reaper will reap it
                        LOG.warning("could not kill stray session for %s", task_id)
            # Late operator steering must never be silently dropped: if any
            # turn is still undelivered, finish is refused and a continuation
            # attempt (whose prompt injects the pending turns) runs instead.
            if self.store.pending_steering(task_id):
                self._transition(
                    task_id,
                    phase="running",
                    status="in_progress",
                    park_state=None,
                    updates={
                        "prior_end_reason": (
                            "work was completed, but operator steering arrived that "
                            "was never delivered — apply it, then finish again"
                        )
                    },
                    action="longhaul.steering_respawn",
                )
                await self.dispatch(task_id)
                return
            self._transition(
                task_id,
                phase="done",
                status="done",
                park_state=None,
                updates={
                    "review": {
                        "verdict": "confirm",
                        "notes": feedback,
                        "at": utc_now_iso(),
                    }
                },
                action="longhaul.done",
            )
            self._notify(
                task_id,
                "done",
                "Longhaul task completed",
                str(task.get("title") or task_id),
            )
            await self._dispatch_waiting_category(task.get("category_id"))
            await self._dispatch_waiting_scope()
            return

        if value == "deny":
            denied = int(state.get("review_denials") or 0) + 1
            limit = int(review_cfg.get("deny_respawns", 2))
            with self.store._lock:
                self.store._begin()
                try:
                    self.store._connection.execute(
                        "UPDATE task_sessions SET end_reason = 'review_denied', detail = ? "
                        "WHERE id = ? AND end_reason = 'completed'",
                        (feedback[:1000], state.get("last_attempt_id")),
                    )
                    self.store._commit()
                except BaseException:
                    self.store._rollback()
                    raise
            self.store.append_task_turn(
                task_id,
                "system",
                f"Completion review denied. Address these findings:\n{feedback}",
                meta={"kind": "handoff", "delivery": {"pending": True}},
            )
            self._transition(
                task_id,
                phase="running" if denied <= limit else "blocked",
                status="in_progress" if denied <= limit else "blocked",
                park_state=None,
                updates={"review_denials": denied, "review_findings": feedback},
                action="longhaul.review_denied",
            )
            if denied > limit:
                self._notify(
                    task_id,
                    "escalation",
                    "Longhaul review repeatedly denied",
                    feedback or "The completion review denied the task.",
                )
                return
            await self.dispatch(task_id)
            return

        retries = int(state.get("review_unavailable_retries") or 0) + 1
        maximum = int(review_cfg.get("unavailable_retries", 3))
        detail = feedback or "reviewer unavailable or unparseable"
        # H-28: once the unavailable-retry budget is exhausted, escalate and
        # RELEASE the category WIP slot. Staying in_progress + waiting_review
        # forever wedges the category FIFO for every peer task.
        if retries >= maximum:
            self._transition(
                task_id,
                phase="blocked",
                status="blocked",
                park_state=None,
                updates={
                    "review_unavailable_retries": retries,
                    "next_review_at": None,
                    "parked_detail": (
                        f"completion review unavailable after {retries} retries: {detail}"
                    )[:500],
                    "review_escalated": True,
                },
                action="longhaul.review_unavailable_escalated",
            )
            self._notify(
                task_id,
                "escalation",
                "Longhaul completion review unavailable",
                (
                    "Reviewer retries exhausted; task is blocked and the category "
                    "WIP slot has been released. An operator must re-open or finish "
                    f"the task. Last detail: {detail}"
                )[:1000],
            )
            await self._dispatch_waiting_category(task.get("category_id"))
            await self._dispatch_waiting_scope()
            return

        backoff = min(
            int(review_cfg.get("max_backoff_s", 900)),
            int(review_cfg.get("backoff_s", 30)) * (2 ** max(0, retries - 1)),
        )
        self._transition(
            task_id,
            phase="parked",
            status="in_progress",
            park_state="waiting_review",
            updates={
                "review_unavailable_retries": retries,
                "next_review_at": _utc_after(backoff),
                "parked_detail": detail,
            },
            action="longhaul.waiting_review",
        )

    def _run_review(self, task: dict[str, Any], state: dict[str, Any]) -> dict[str, str] | None:
        """Return only an explicit confirm/deny; infrastructure faults are unknown."""

        if callable(self._reviewer):
            try:
                raw = self._reviewer(task, state, read_workbook(str(task["id"])) or "")
            except Exception as exc:  # noqa: BLE001
                return {"verdict": "", "feedback": f"reviewer unavailable: {exc}"}
            if isinstance(raw, dict):
                value = str(raw.get("verdict") or "").lower()
                if value in {"confirm", "deny"}:
                    return {
                        "verdict": value,
                        "feedback": str(raw.get("feedback") or raw.get("notes") or ""),
                    }
            value = str(getattr(raw, "verdict", "") or "").lower()
            if value in {"confirm", "deny"}:
                return {
                    "verdict": value,
                    "feedback": str(getattr(raw, "feedback", "") or ""),
                }
            return {"verdict": "", "feedback": "reviewer returned no valid verdict"}

        try:
            from omniagentos.adapters.registry import resolve_adapter
            from omniagentos.contracts import HarnessType

            adapter = resolve_adapter(HarnessType.CLI_CODEX)
            acceptance = str(state.get("acceptance") or "")
            workbook = read_workbook(str(task["id"])) or ""
            result = adapter.run(
                AgentInput(
                    run_id=f"longhaul-review-{task['id']}",
                    task_id=str(task["id"]),
                    prompt=(
                        "Review this completed task. Return only an explicit verdict.\n\n"
                        f"Acceptance:\n{acceptance}\n\nWorkbook:\n{workbook}"
                    ),
                    working_dir=self._project_dir(task, state),
                    output_schema={
                        "type": "object",
                        "required": ["verdict"],
                        "properties": {
                            "verdict": {"type": "string", "enum": ["confirm", "deny"]},
                            "feedback": {"type": "string"},
                        },
                    },
                )
            )
        except Exception as exc:  # noqa: BLE001
            return {"verdict": "", "feedback": f"reviewer unavailable: {exc}"}
        payload = result.output_json if result.status == ResultStatus.OK else None
        if not isinstance(payload, dict):
            return {"verdict": "", "feedback": result.error or "unparseable review"}
        value = str(payload.get("verdict") or "").lower()
        if value not in {"confirm", "deny"}:
            return {"verdict": "", "feedback": result.error or "unparseable review"}
        return {"verdict": value, "feedback": str(payload.get("feedback") or "")}

    # ------------------------------------------------------------------
    # Reconciliation
    # ------------------------------------------------------------------

    async def tick(self) -> None:
        """Reconcile attempts, capacity, category FIFO, reviews, and stale workers."""

        now = utc_now_iso()
        freed = self.store.clear_expired_cooldowns(now)
        if freed:
            for task_id in self.store.list_parked("waiting_capacity"):
                await self.dispatch(task_id)

        rows = self.store._connection.execute(
            "SELECT * FROM board_tasks WHERE lane = 'longhaul' "
            "AND status NOT IN ('done', 'blocked', 'cancelled') "
            "ORDER BY created_at ASC, id ASC"
        ).fetchall()
        live_attempts: set[str] = set()
        # Resolved at most ONCE per tick, and only if something is actually
        # live: scope_locks_enabled()/scope_ttl_s() re-read configs/parallelism.
        # yaml on every call, and tick rides a sub-second poll loop. None == the
        # feature is off, which is also the state a tick with nothing live
        # leaves untouched. Mutable list so _tick_one_task can fill it once.
        renew_state: list[Any] = [None, False]  # [window, resolved]
        for raw in rows:
            task = dict(raw)
            task_id = str(task["id"])
            # M-45: one task's exception must not abort the rest of the pass.
            try:
                await self._tick_one_task(task, live_attempts, renew_state)
            except Exception:  # noqa: BLE001 - per-task isolation is the contract.
                LOG.exception("longhaul tick failed for task %s", task_id)

        # Bound the renewal throttle to what is actually live, so a supervisor
        # that runs for weeks does not accumulate one entry per attempt it has
        # ever seen. Dropping an entry only costs one extra renewal.
        if self._scope_renewed_at:
            self._scope_renewed_at = {
                attempt_id: stamp
                for attempt_id, stamp in self._scope_renewed_at.items()
                if attempt_id in live_attempts
            }

    async def _tick_one_task(
        self,
        task: dict[str, Any],
        live_attempts: set[str],
        renew_state: list[Any],
    ) -> None:
        """Reconcile a single longhaul board task (M-45 isolation unit)."""
        task_id = str(task["id"])
        if task.get("archived_at") is not None:
            self._cancel_task(task)
            return
        state = _as_dict(task.get("longhaul_json"))
        # A review in flight (30-90s of reviewer wall time) closes the
        # attempt first — without this gate a SECOND engine process reads
        # "in_progress, no attempt" as a crash window and redispatches,
        # producing an attempt-churn loop. Stale reviews (crashed reviewer
        # process) fall through after 15 minutes.
        if str(state.get("phase") or "") == "reviewing":
            started = _parse_time(state.get("review_started_at"))
            if started is not None and (datetime.now(UTC) - started).total_seconds() < 900:
                return
            self._transition(
                task_id,
                phase="running",
                status="in_progress",
                updates={"review_started_at": None},
                action="longhaul.review_stale_reset",
            )
        if task.get("park_state") == "waiting_review":
            when = _parse_time(state.get("next_review_at"))
            review_cfg = _as_dict(self.cfg.get("review"))
            retries = int(state.get("review_unavailable_retries") or 0)
            maximum = int(review_cfg.get("unavailable_retries", 3))
            # H-28: exhausted review retries escalate out of the wedge. The
            # review path itself blocks + releases WIP when the counter hits
            # the cap; this branch is the safety net if state was left parked
            # at/above the cap by an older process.
            if retries >= maximum:
                self._transition(
                    task_id,
                    phase="blocked",
                    status="blocked",
                    park_state=None,
                    updates={
                        "review_escalated": True,
                        "parked_detail": (f"completion review unavailable after {retries} retries"),
                        "next_review_at": None,
                    },
                    action="longhaul.review_unavailable_escalated",
                )
                self._notify(
                    task_id,
                    "escalation",
                    "Longhaul completion review unavailable",
                    "Reviewer retries exhausted; category WIP slot released.",
                )
                await self._dispatch_waiting_category(task.get("category_id"))
                await self._dispatch_waiting_scope()
                return
            if when is None or when <= datetime.now(UTC):
                await self._review_completed(task_id)
            return

        attempt = self.store.current_attempt(task_id)
        if attempt is None:
            # L-13: honor next_dispatch_at before re-opening an attempt.
            if not self._dispatch_backoff_active(state):
                await self.dispatch(task_id)
            return
        # This attempt is live, so its lease must not lapse under it. Done
        # here, before any of the branches below can return, because every
        # one of them leaves the attempt live.
        live_attempts.add(str(attempt["id"]))
        if not renew_state[1]:
            renew_state[1] = True
            renew_state[0] = max(1.0, scope_ttl_s() / 3.0) if scope_locks_enabled() else None
        self._renew_scope(str(attempt["id"]), renew_state[0])
        if attempt.get("session_id"):
            session = self._session(str(attempt["session_id"]))
            if session is not None and str(session.get("state")) in _TERMINAL_SESSIONS:
                rc = 0 if session.get("state") == "completed" else 1
                # events=[] on purpose: on_session_terminal synthesizes from
                # sessions.error when the stream is unavailable (H-08).
                await self.on_session_terminal(session, [], rc)
                return
            # Idle/hang supervision for live longhaul sessions is the A2
            # reaper's job (sessions/supervisor.py), enforced under the
            # per-session idle_minutes override persisted at spawn.
            return

        detail = _as_dict(attempt.get("detail"))
        pid = detail.get("pid")
        if await self._replay_attempt_terminal_record(attempt, task, detail):
            # The wrapper publishes only after the provider child is terminal.
            # Replay this before consulting PID liveness: the wrapper may still
            # be in its final exit instructions, and a reused PID must not turn
            # authoritative terminal truth into a live-attempt false positive.
            return
        if attempt.get("harness") in _DIRECT_CLI_HARNESSES and not (
            isinstance(pid, int) and pid > 0
        ):
            recovered_pid = self._load_attempt_wrapper_pid(attempt, detail)
            if recovered_pid is not None:
                nonce = detail.get("terminal_record_nonce")
                ack = (
                    load_launch_ack(
                        self.db_path,
                        attempt_id=str(attempt["id"]),
                        harness=str(attempt["harness"]),
                        provider=_provider_for_harness(attempt.get("harness")),
                        launch_nonce=str(nonce),
                    )
                    if isinstance(nonce, str)
                    else None
                )
                if ack is not None:
                    pid = recovered_pid
                    detail = {**detail, "pid": recovered_pid, "pgid": recovered_pid}
                    self._update_attempt(str(attempt["id"]), detail=detail)
                else:
                    if isinstance(nonce, str):
                        publish_tombstone(
                            self.db_path,
                            attempt_id=str(attempt["id"]),
                            harness=str(attempt["harness"]),
                            provider=_provider_for_harness(attempt.get("harness")),
                            launch_nonce=nonce,
                            reason="unacknowledged wrapper on restart",
                        )
            elif isinstance(detail.get("terminal_record_nonce"), str):
                opened = _parse_time(detail.get("started")) or _parse_time(
                    attempt.get("started_at")
                )
                if opened is not None and datetime.now(UTC) < opened + timedelta(
                    seconds=_TERMINAL_WRAPPER_START_GRACE_S
                ):
                    return
        terminal_evidence = _as_dict(detail.get("terminal_evidence"))
        terminal_events = terminal_evidence.get("events")
        terminal_rc = terminal_evidence.get("rc")
        if (
            attempt.get("harness") in _DIRECT_CLI_HARNESSES
            and terminal_evidence
            and isinstance(terminal_events, list)
            and all(isinstance(event, dict) for event in terminal_events)
            and isinstance(terminal_rc, int)
        ):
            # The executor durably journaled its result but the daemon died
            # before close delivery committed. Evidence is authoritative even
            # if the old PID has already been reused by an unrelated process.
            await self.on_session_terminal(
                {
                    "title": f"[longhaul:{attempt['id']}]",
                    "state": str(terminal_evidence.get("state") or "failed"),
                    "session_ref": terminal_evidence.get("session_ref"),
                    "todos_json": "[]",
                    "files_json": "[]",
                },
                [dict(event) for event in terminal_events],
                terminal_rc,
            )
            return
        if attempt.get("harness") in _DIRECT_CLI_HARNESSES and isinstance(pid, int) and pid > 0:
            if self._pid_alive(pid):
                if not self._attempt_wall_expired(attempt):
                    return
                pgid = detail.get("pgid")
                self._shutdown_recovered_process_group(
                    pgid if isinstance(pgid, int) and pgid > 0 else pid
                )
                # A provider that completed during TERM may have atomically
                # published truthful evidence. Give it precedence over the
                # conservative timeout fallback.
                if await self._replay_attempt_terminal_record(attempt, task, detail):
                    return
            # L-13: stamp bounded backoff on direct-CLI orphan reconcile (same as the
            # on_session_terminal crash path). Immediate redispatch burned the
            # attempt budget when the process was already gone at restart.
            closed, state = self._close_crash_with_backoff(
                task_id,
                attempt,
                action="longhaul.cli_orphan",
                detail=f"{attempt.get('harness')} process missing after restart",
            )
            if not closed:
                return
            if self._dispatch_backoff_active(state):
                return
            await self.dispatch(task_id)
            return

        marker = f"[longhaul:{attempt['id']}]"
        session = self.store._connection.execute(
            "SELECT * FROM sessions WHERE title LIKE ? ORDER BY created_at DESC LIMIT 1",
            (f"%{marker}%",),
        ).fetchone()
        if session is not None:
            found = dict(session)
            self._update_attempt(str(attempt["id"]), session_id=str(found["id"]))
            self._transition(
                task_id,
                phase="running",
                status="in_progress",
                result_ref=str(found["id"]),
                updates={"session_id": str(found["id"])},
                action="longhaul.spawn_recovered",
            )
            if str(found.get("state")) in _TERMINAL_SESSIONS:
                await self.on_session_terminal(
                    found, [], 0 if found.get("state") == "completed" else 1
                )
            return

        opened = _parse_time(attempt.get("started_at"))
        grace = int(self.cfg.get("spawn_grace_s", 30))
        if opened is not None and datetime.now(UTC) < opened + timedelta(seconds=grace):
            return
        # L-13: stamp bounded backoff on spawn_incomplete reconcile so a tight
        # spawn-fail loop cannot burn max_sessions in under a second.
        closed, state = self._close_crash_with_backoff(
            task_id,
            attempt,
            action="longhaul.spawn_incomplete",
            detail="spawn_incomplete",
            extra_updates={"spawn_incomplete": True},
        )
        if not closed:
            return
        if self._dispatch_backoff_active(state):
            return
        await self.dispatch(task_id)

    async def _dispatch_waiting_category(self, category_id: Any) -> None:
        if not category_id:
            return
        next_id = self.store.next_waiting_in_category(str(category_id))
        if next_id:
            await self.dispatch(next_id)

    def _renew_scope(self, attempt_id: str, window: float | None) -> None:
        """Heartbeat one live attempt's lease. ``window is None`` == locks off.

        THROTTLED BECAUSE tick() RIDES A SUB-SECOND POLL LOOP (the supervisor
        calls it from the same pass that services every session). An unthrottled
        renew would push one UPDATE per live attempt per poll through the single
        process-wide writer lock, which is a real cost paid for nothing: the
        caller's ``window`` is a third of the TTL, the standard heartbeat
        margin, which leaves room for two consecutive missed renewals before
        anything lapses.

        A failed renew is logged, not acted on — see
        ``LonghaulStore.renew_attempt_scope`` for why the recovery policy is a
        separate decision. The stamp is recorded either way so a persistent
        failure warns every window rather than every poll.
        """
        if window is None:
            return
        now = monotonic()
        last = self._scope_renewed_at.get(attempt_id)
        if last is not None and (now - last) < window:
            return
        self._scope_renewed_at[attempt_id] = now
        try:
            renewed = self.store.renew_attempt_scope(attempt_id)
        except Exception:  # noqa: BLE001 - a heartbeat must never starve the tick.
            LOG.warning("longhaul scope renew failed for %s", attempt_id, exc_info=True)
            return
        if not renewed:
            LOG.warning(
                "longhaul attempt %s no longer holds its scope lease (lapsed or "
                "fenced); its executor is running unclaimed",
                attempt_id,
            )

    async def _dispatch_waiting_scope(self) -> None:
        """Wake the oldest scope-parked longhaul task, FIFO. No-op when off.

        Called only where a task is FINISHED and will not redispatch itself — a
        done task's realm is genuinely free. It is deliberately NOT called after
        every close_attempt: most terminal kinds immediately open a successor
        attempt for the SAME task, and waking a parked rival there would hand the
        realm to a task with no workbook and no context, evicting one that has
        both. Continuity beats fairness for a lane whose unit of work is hours
        long. The other terminal paths (cancelled, blocked-on-max-sessions) leak
        no liveness either: tick() re-dispatches every non-terminal longhaul task
        that has no live attempt, so a parked task retries there at worst one
        tick later. This is a promptness optimisation, not the recovery path.

        The gate is first so the dark path does not even run the query.
        """
        if not scope_locks_enabled():
            return
        next_id = self.store.next_waiting_scope()
        if next_id:
            await self.dispatch(next_id)

    def _cancel_task(self, task: dict[str, Any]) -> None:
        attempt = self.store.current_attempt(str(task["id"]))
        if attempt is not None:
            publish_tombstone(
                self.db_path,
                attempt_id=str(attempt["id"]),
                reason="task cancelled",
            )
            if attempt.get("session_id"):
                # H-27: never call Connection.commit() outside the store lock /
                # transaction helpers. A raw commit on a shared-handle path can
                # land another thread's still-open transaction after that thread
                # intended to roll it back.
                with self.store._lock:
                    self.store._begin()
                    try:
                        self.store._connection.execute(
                            "UPDATE sessions SET kill_requested = 1, "
                            "killed_by = 'cancel_requested', updated_at = ? "
                            "WHERE id = ?",
                            (utc_now_iso(), str(attempt["session_id"])),
                        )
                        self.store._commit()
                    except BaseException:
                        self.store._rollback()
                        raise
            else:
                detail = _as_dict(attempt.get("detail"))
                pgid = detail.get("pgid")
                if isinstance(pgid, int) and pgid > 0:
                    try:
                        os.killpg(pgid, signal.SIGTERM)
                    except (OSError, ProcessLookupError):
                        pass
            self.store.close_attempt(str(attempt["id"]), "superseded", "task archived")
        self._transition(
            str(task["id"]),
            phase="blocked",
            status="cancelled",
            park_state=None,
            action="longhaul.cancelled",
        )

    @staticmethod
    def _pid_alive(pid: int) -> bool:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        return True


__all__ = ["LonghaulEngine"]
