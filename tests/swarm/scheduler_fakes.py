"""Shared fakes + harness for the WP5a scheduler test suites.

Fake spawner/session world, fake clock, recording emitter, scripted
router/reviewer/limits/git — everything the scheduler's injectable seams need
so the engine runs end-to-end with zero live processes. Real git repos are
built per-test (``init_git_repo``) only where snapshot/commit behavior is the
thing under test.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from omniagentos.collab.store import CollabStore
from omniagentos.swarm.contracts import SwarmPlan, SwarmTaskSpec
from omniagentos.swarm.dal import SwarmDal
from omniagentos.swarm.planner import provision_run
from omniagentos.swarm.scheduler import (
    RouteDecision,
    SchedulerClock,
    SwarmReviewOutcome,
    SwarmScheduler,
)
from tests.support.db_template import migrated_db

_TERMINAL = frozenset({"completed", "failed", "cancelled", "killed"})


def wait_until(predicate, timeout: float = 10.0, interval: float = 0.01) -> bool:
    """Real-time poll helper for cross-thread assertions."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class FakeClock(SchedulerClock):
    """Virtual time: ``sleep`` advances instantly; ``advance`` is test-driven."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._t = 0.0
        self._base = datetime(2026, 1, 1, tzinfo=UTC)
        self._first_sleep = threading.Event()

    def now(self) -> datetime:
        with self._lock:
            return self._base + timedelta(seconds=self._t)

    def monotonic(self) -> float:
        with self._lock:
            return self._t

    def sleep(self, seconds: float) -> None:
        with self._lock:
            self._t += max(0.0, seconds)
        self._first_sleep.set()

    def wait_for_first_sleep(self, timeout: float) -> bool:
        """Wait until scheduler code has registered and entered a clock wait."""
        return self._first_sleep.wait(timeout=max(0.0, timeout))

    def advance(self, seconds: float) -> None:
        with self._lock:
            self._t += max(0.0, seconds)


class RecordingEmitter:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.events: list[tuple[str, dict[str, Any]]] = []

    def emit(self, run_id: str, action: str, payload: dict[str, Any] | None = None) -> None:
        del run_id
        with self._lock:
            self.events.append((action, dict(payload or {})))

    def actions(self) -> list[str]:
        with self._lock:
            return [action for action, _ in self.events]

    def of(self, action: str) -> list[dict[str, Any]]:
        with self._lock:
            return [payload for act, payload in self.events if act == action]


class FakeWorld:
    """Fake spawner + session store in one object.

    Behaviors are scripted per ``task_key``; a list of behaviors is consumed
    one per successive attempt (the last repeats). Behavior dict keys:

    - ``kind``: complete | fail | rate_limited | await_approval | hang | gated
    - ``polls``: session polls before the behavior lands (default 2)
    - ``output`` / ``error`` / ``cost`` / ``files`` (files_json list)
    - ``write_files``: {relpath: content} written into the workspace at spawn
    - ``gate``: threading.Event — a "gated" session completes only once set
    """

    def __init__(self) -> None:
        self.lock = threading.RLock()
        self.sessions: dict[str, dict[str, Any]] = {}
        self.scripts: dict[str, dict[str, Any]] = {}
        self.behaviors: dict[str, list[dict[str, Any]]] = {}
        self.spawn_order: list[str] = []
        self.spawn_requests: list[Any] = []
        self.active_at_spawn: list[int] = []
        self.kills: list[str] = []
        # H-19 ordering: (event, session_id_or_path, monotonic) for kill vs
        # worktree-remove sequencing assertions.
        self.order_events: list[tuple[str, str, float]] = []
        self.high_water = 0
        self._counter = 0
        self.default_behavior: dict[str, Any] = {
            "kind": "complete",
            "polls": 2,
            "output": "done",
            "cost": 0.0,
        }

    # -- scripting ----------------------------------------------------------

    def set_behavior(self, task_key: str, *behaviors: dict[str, Any]) -> None:
        self.behaviors[task_key] = [dict(b) for b in behaviors]

    def _next_behavior(self, task_key: str) -> dict[str, Any]:
        queue = self.behaviors.get(task_key)
        if not queue:
            return dict(self.default_behavior)
        if len(queue) > 1:
            return dict(queue.pop(0))
        return dict(queue[0])

    # -- bookkeeping ---------------------------------------------------------

    def _running_count(self) -> int:
        return sum(1 for row in self.sessions.values() if row["state"] == "running")

    def _recount(self) -> None:
        self.high_water = max(self.high_water, self._running_count())

    @property
    def active(self) -> int:
        with self.lock:
            return self._running_count()

    # -- spawner protocol ----------------------------------------------------

    def spawn(self, request: Any) -> str:
        with self.lock:
            self._counter += 1
            session_id = f"fses_{self._counter}"
            behavior = self._next_behavior(request.task_key)
            row = {
                "id": session_id,
                "state": "running",
                "output_text": "",
                "cost_usd": 0.0,
                # M-44: bind session project_dir to the spawn working_dir so
                # claim-vs-execution checks can exercise real sessions.
                "project_dir": str(getattr(request, "working_dir", "") or ""),
            }
            # Optional behavior override for M-44 divergence tests.
            if behavior.get("project_dir") is not None:
                row["project_dir"] = str(behavior["project_dir"])
            self.sessions[session_id] = row
            self.scripts[session_id] = {
                "behavior": behavior,
                "polls_left": int(behavior.get("polls", 2)),
            }
            self.spawn_order.append(request.task_key)
            self.spawn_requests.append(request)
            self.active_at_spawn.append(self._running_count())
            self._recount()
        for rel_path, content in (behavior.get("write_files") or {}).items():
            target = Path(request.working_dir) / rel_path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
        return session_id

    def add_session(self, *, state: str = "running", behavior: dict[str, Any] | None = None) -> str:
        """Manually add a session row (crash-resume tests)."""
        with self.lock:
            self._counter += 1
            session_id = f"fses_{self._counter}"
            self.sessions[session_id] = {
                "id": session_id,
                "state": state,
                "output_text": "",
                "cost_usd": 0.0,
            }
            if behavior is not None:
                self.scripts[session_id] = {
                    "behavior": dict(behavior),
                    "polls_left": int(behavior.get("polls", 1)),
                }
            self._recount()
            return session_id

    # -- session store protocol -----------------------------------------------

    def get_session(self, session_id: str) -> dict[str, Any] | None:
        with self.lock:
            row = self.sessions.get(session_id)
            if row is None:
                return None
            script = self.scripts.get(session_id)
            if script is not None and row["state"] == "running":
                behavior = script["behavior"]
                kind = behavior.get("kind", "complete")
                gate = behavior.get("gate")
                blocked = kind == "hang" or (
                    kind == "gated" and (gate is None or not gate.is_set())
                )
                if not blocked:
                    script["polls_left"] -= 1
                    if script["polls_left"] <= 0:
                        self._land(row, behavior)
                        self._recount()
            return dict(row)

    def _land(self, row: dict[str, Any], behavior: dict[str, Any]) -> None:
        kind = behavior.get("kind", "complete")
        row["cost_usd"] = float(behavior.get("cost", 0.0))
        if "files" in behavior:
            row["files_json"] = json.dumps(list(behavior["files"]))
        if kind in ("complete", "gated"):
            row["state"] = "completed"
            row["output_text"] = str(behavior.get("output", "done"))
        elif kind == "fail":
            row["state"] = "failed"
            row["error"] = str(behavior.get("error", "boom"))
        elif kind == "rate_limited":
            row["state"] = "failed"
            row["error"] = str(behavior.get("error", "rate limited"))
            row["swarm_outcome"] = "rate_limited"
        elif kind == "await_approval":
            row["state"] = "awaiting_approval"

    def request_kill(self, session_id: str, *, killed_by: str | None = None) -> bool:
        del killed_by
        with self.lock:
            self.kills.append(session_id)
            self.order_events.append(("kill", session_id, time.monotonic()))
            script = self.scripts.get(session_id)
            if script is not None and script["behavior"].get("ignore_kill"):
                return True  # a session that outruns its kill (late-completion tests)
            row = self.sessions.get(session_id)
            if row is not None and row["state"] not in _TERMINAL:
                row["state"] = "killed"
                self._recount()
            return True

    def resolve(
        self, session_id: str, state: str, *, output: str = "ok", cost: float = 0.0
    ) -> None:
        """Test-driven session transition (approval resolutions, late completions)."""
        with self.lock:
            row = self.sessions[session_id]
            row["state"] = state
            row["output_text"] = output
            row["cost_usd"] = cost
            self.scripts.pop(session_id, None)
            self._recount()


class FakeRouter:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.cooling = False
        self.calls = 0
        self.effort: str | None = None  # router-decided effort (048), if any

    def route(self, task: dict[str, Any], tier: str) -> RouteDecision | None:
        del task
        with self.lock:
            self.calls += 1
            if self.cooling:
                return None
        return RouteDecision(
            provider="claude",
            model="sonnet",
            tier=tier,
            account_id="acct_test",
            effort=self.effort,
        )


class ScriptedReviewer:
    """Per-task-key verdict scripts; records every call (incl. flags)."""

    def __init__(self, default: str = "confirm") -> None:
        self.lock = threading.Lock()
        self.default = default
        self.script: dict[str, list[str]] = {}
        self.calls: list[dict[str, Any]] = []

    def set_script(self, task_key: str, *verdicts: str) -> None:
        self.script[task_key] = list(verdicts)

    def review(
        self,
        *,
        task: dict[str, Any],
        swarm_json: dict[str, Any],
        session: dict[str, Any],
        verify_output: str,
        flags: Any,
    ) -> SwarmReviewOutcome:
        del session, verify_output
        key = str(swarm_json.get("task_key") or task["id"])
        with self.lock:
            self.calls.append({"key": key, "flags": list(flags)})
            queue = self.script.get(key)
            verdict = self.default
            if queue:
                verdict = queue.pop(0) if len(queue) > 1 else queue[0]
        return SwarmReviewOutcome(verdict=verdict, feedback=f"scripted {verdict}", reviewer="fake")


class FakeLimits:
    """Fleet + cooldown port. When bound to a world, the SIGNED remaining is
    ``capacity - live sessions`` (mimicking the real ledger's consumption —
    negative once the ceiling drops below the live count)."""

    def __init__(self, capacity: int = 100, world: FakeWorld | None = None) -> None:
        self.capacity = capacity
        self.world = world
        self.cooldown_until: datetime | None = None
        self.reports: list[dict[str, Any]] = []

    def report_rate_limited(
        self, provider: str, account_id: str | None, detail: str, reset_at: str | None
    ) -> None:
        self.reports.append(
            {"provider": provider, "account_id": account_id, "detail": detail, "reset_at": reset_at}
        )

    def earliest_cooldown_until(self) -> datetime | None:
        return self.cooldown_until

    def swarm_slots_remaining(self) -> int:
        live = self.world.active if self.world is not None else 0
        return self.capacity - live


class FakeWorktrees:
    """In-memory worktree seam backed by REAL tmp directories.

    Real dirs matter: the scheduler stats ``worktree_path`` at settle/resume,
    and ``FakeWorld`` write_files land inside ``request.working_dir``. Merge
    results are scripted per task_key (default ``merged``); every mutation is
    recorded for assertions."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.lock = threading.RLock()
        self.created: list[dict[str, Any]] = []
        self.removed: list[dict[str, Any]] = []
        self.merges: list[str] = []  # task_key, in merge order
        self.merge_shas: list[tuple[str, str | None]] = []  # (task_key, sha argument)
        self.merge_scripts: dict[str, list[str]] = {}  # task_key -> status queue
        self.changed: dict[str, list[str]] = {}  # task_key -> changed_paths_since
        self.dirty: dict[str, list[str]] = {}  # task_key -> dirty_paths
        self.salvage_fail: set[str] = set()  # task_keys whose salvage_commit fails
        self.salvages: list[tuple[str, str]] = []  # (task_key, message)
        self.branch_tips: dict[str, str] = {}
        self.deleted_branches: list[str] = []
        self.active: dict[str, str] = {}  # path -> task_key
        self.common_dir = str(root / "common.git")
        self.pending_merge = False  # scripted MERGE_HEAD wedge (M4c)
        self.aborts: list[str] = []  # abort_merge calls (working_dir)
        # H-04: when set, ``changed_paths_since`` raises (fail-closed ownership).
        self.changed_paths_error: BaseException | None = None
        # H-19 ordering: shared list can be pointed at FakeWorld.order_events.
        self.order_events: list[tuple[str, str, float]] = []
        self._counter = 0

    # -- scripting ----------------------------------------------------------

    def set_merge(self, task_key: str, *statuses: str) -> None:
        self.merge_scripts[task_key] = list(statuses)

    def set_changed(self, task_key: str, paths: list[str]) -> None:
        self.changed[task_key] = list(paths)

    def set_dirty(self, task_key: str, paths: list[str]) -> None:
        self.dirty[task_key] = list(paths)

    def set_salvage_fail(self, task_key: str) -> None:
        self.salvage_fail.add(task_key)

    # -- SwarmWorktreesProto -------------------------------------------------

    def supported(self, working_dir: str) -> bool:
        del working_dir
        return True

    def git_common_dir(self, working_dir: str) -> str | None:
        del working_dir
        return self.common_dir

    def worktree_git_dir(self, path: str) -> str | None:
        # R1: mirrors the real seam — each worktree's private gitdir lives
        # under <common>/worktrees/<task_key>.
        with self.lock:
            key = self.active.get(path, Path(path).name)
        return str(Path(self.common_dir) / "worktrees" / key)

    def create(self, working_dir: str, run_id: str, task_key: str, base_ref: str) -> Any:
        from omniagentos.swarm.worktrees import WorktreeInfo

        del working_dir
        with self.lock:
            branch = f"swarm/{run_id}/{task_key}"
            path = self.root / run_id / task_key
            reused = branch in self.branch_tips
            path.mkdir(parents=True, exist_ok=True)
            self.branch_tips.setdefault(branch, base_ref)
            self.active[str(path)] = task_key
            info = WorktreeInfo(
                path=str(path),
                branch=branch,
                base_sha=self.branch_tips[branch],
                reused=reused,
            )
            self.created.append(
                {"task_key": task_key, "path": str(path), "branch": branch, "reused": reused}
            )
            return info

    def remove(self, working_dir: str, path: str, *, salvage: bool, message: str = "") -> Any:
        from omniagentos.swarm.worktrees import RemoveOutcome

        del working_dir
        with self.lock:
            key = self.active.get(path, Path(path).name)
            self.order_events.append(("remove", path, time.monotonic()))
            if salvage and key in self.salvage_fail and self.dirty.get(key):
                # R2b contract: salvage requested, tree still dirty, salvage
                # failed — the worktree is LEFT IN PLACE.
                self.removed.append(
                    {"path": path, "salvage": salvage, "message": message, "kept": True}
                )
                return RemoveOutcome(status="salvage_failed")
            self.removed.append({"path": path, "salvage": salvage, "message": message})
            self.active.pop(path, None)
        shutil.rmtree(path, ignore_errors=True)
        return RemoveOutcome(status="removed", salvage_sha="salvage-sha" if salvage else None)

    def head_sha(self, path: str) -> str | None:
        with self.lock:
            key = self.active.get(path, Path(path).name)
            return f"head-{key}"

    def merge_branch(
        self, working_dir: str, branch: str, message: str, *, sha: str | None = None
    ) -> Any:
        from omniagentos.swarm.worktrees import MergeOutcome

        del working_dir, message
        task_key = branch.rsplit("/", 1)[-1]
        with self.lock:
            self.merges.append(task_key)
            self.merge_shas.append((task_key, sha))
            queue = self.merge_scripts.get(task_key)
            status = "merged"
            if queue:
                status = queue.pop(0) if len(queue) > 1 else queue[0]
            if status == "raise":
                raise RuntimeError("scripted merge explosion")
            if status == "conflict":
                return MergeOutcome(status="conflict", conflict_files=("shared.txt",))
            self._counter += 1
            return MergeOutcome(status=status, sha=f"merge{self._counter}")  # type: ignore[arg-type]

    def has_pending_merge(self, working_dir: str) -> bool:
        del working_dir
        with self.lock:
            return self.pending_merge

    def abort_merge(self, working_dir: str) -> bool:
        with self.lock:
            self.aborts.append(working_dir)
            self.pending_merge = False
        return True

    def changed_paths_since(self, path: str, base_sha: str) -> list[str]:
        del base_sha
        with self.lock:
            if self.changed_paths_error is not None:
                raise self.changed_paths_error
            key = self.active.get(path, Path(path).name)
            return list(self.changed.get(key, []))

    def dirty_paths(self, path: str) -> list[str]:
        with self.lock:
            key = self.active.get(path, Path(path).name)
            return list(self.dirty.get(key, []))

    def salvage_commit(self, path: str, message: str) -> str | None:
        with self.lock:
            key = self.active.get(path, Path(path).name)
            self.salvages.append((key, message))
            if key in self.salvage_fail:
                return None
            # Salvage persists the dirty state to the branch: clean afterwards.
            self.dirty.pop(key, None)
            return f"salvage-{key}"

    def list_run_worktrees(self, working_dir: str, run_id: str) -> list[tuple[str, str]]:
        del working_dir
        with self.lock:
            return [
                (path, key) for path, key in self.active.items() if Path(path).parent.name == run_id
            ]

    def prune_orphans(self, working_dir: str, run_id: str, live_task_keys: Any) -> list[str]:
        live = {str(k) for k in live_task_keys}
        removed = []
        for path, key in self.list_run_worktrees(working_dir, run_id):
            if key in live:
                continue
            outcome = self.remove(working_dir, path, salvage=True)
            if outcome.status == "salvage_failed":
                continue  # R2: left in place, mirrors the real seam
            removed.append(path)
        return removed

    def delete_run_branches(self, working_dir: str, run_id: str) -> list[str]:
        del working_dir
        with self.lock:
            prefix = f"swarm/{run_id}/"
            doomed = [b for b in self.branch_tips if b.startswith(prefix)]
            for branch in doomed:
                del self.branch_tips[branch]
                self.deleted_branches.append(branch)
            return doomed


class FakeGit:
    """No-op git for tests where snapshots do not matter."""

    def __init__(self, checkout: bool = True) -> None:
        self.checkout = checkout
        self.lock = threading.Lock()
        self.snapshots: list[str] = []
        self.snapshot_paths: list[list[str]] = []
        self.snapshot_digests: list[dict[str, str] | None] = []
        self.snapshot_modes: list[dict[str, str] | None] = []
        self.commits: list[tuple[list[str], str]] = []
        self.reverts: list[tuple[str, list[str]]] = []
        # H-04: when set, ``changed_paths`` raises — ownership observation
        # failure surface for fail-closed confirmation tests.
        self.changed_paths_error: BaseException | None = None
        self.changed_paths_calls: int = 0
        # M-35: pathspec confirm commit failure surface.
        self.commit_paths_error: BaseException | None = None
        self.commit_paths_calls: int = 0

    def is_checkout(self, working_dir: str) -> bool:
        del working_dir
        return self.checkout

    def pre_attempt_dirty_paths(self, working_dir: str) -> list[str]:
        del working_dir
        return []

    def snapshot(
        self,
        working_dir: str,
        message: str,
        coordinator_paths: Any,
        expected_digests: Any = None,
        expected_modes: Any = None,
    ) -> str:
        del working_dir
        with self.lock:
            self.snapshots.append(message)
            self.snapshot_paths.append(list(coordinator_paths))
            # Record digests/modes when supplied so tests can prove the call
            # site binds approved content + mode, not pathnames alone.
            self.snapshot_digests.append(
                dict(expected_digests) if expected_digests is not None else None
            )
            self.snapshot_modes.append(dict(expected_modes) if expected_modes is not None else None)
            return f"snap{len(self.snapshots)}"

    def changed_paths(self, working_dir: str) -> list[str]:
        del working_dir
        with self.lock:
            self.changed_paths_calls += 1
            if self.changed_paths_error is not None:
                raise self.changed_paths_error
        return []

    def revert_paths(self, working_dir: str, snapshot_sha: str, paths: Any) -> None:
        del working_dir
        with self.lock:
            self.reverts.append((snapshot_sha, list(paths)))

    def commit_paths(self, working_dir: str, paths: Any, message: str) -> str | None:
        del working_dir
        with self.lock:
            self.commit_paths_calls += 1
            if self.commit_paths_error is not None:
                raise self.commit_paths_error
            self.commits.append((list(paths), message))
            return f"commit{len(self.commits)}"


# ---------------------------------------------------------------------------
# Plan/harness builders
# ---------------------------------------------------------------------------


def make_plan(
    specs: list[dict[str, Any]], *, integration: bool = True, target_n: int = 2
) -> SwarmPlan:
    tasks = [
        SwarmTaskSpec(
            id=spec["id"],
            title=spec.get("title", spec["id"]),
            description=spec.get("description", f"work on {spec['id']}"),
            depends_on=list(spec.get("depends_on", [])),
            complexity=spec.get("complexity", "simple"),
            est_agent_minutes=int(spec.get("est", 10)),
            owned_paths=list(spec.get("owned_paths", [f"src/{spec['id']}.txt"])),
            tier_hint=spec.get("tier_hint"),
            acceptance=spec.get("acceptance", "delivered"),
            verify_command=spec.get("verify_command", ""),
        )
        for spec in specs
    ]
    integration_id = None
    if integration:
        depended_on = {dep for task in tasks for dep in task.depends_on}
        leaves = [task.id for task in tasks if task.id not in depended_on]
        tasks.append(
            SwarmTaskSpec(
                id="integration",
                title="Integration",
                description="merge + verify",
                depends_on=leaves,
                complexity="simple",
                est_agent_minutes=5,
                owned_paths=["."],
                acceptance="suite green",
            )
        )
        integration_id = "integration"
    return SwarmPlan(
        goal="test goal",
        tasks=tasks,
        integration_task_id=integration_id,
        mode="swarm",
        version=1,
        target_n=target_n,
        parallelism_ratio=2.0,
    )


def init_git_repo(path: Path) -> None:
    def run(*args: str) -> None:
        subprocess.run(
            ("git", "-C", str(path), "-c", "user.email=t@t", "-c", "user.name=t", *args),
            check=True,
            capture_output=True,
        )

    subprocess.run(("git", "init", "-q", str(path)), check=True, capture_output=True)
    (path / ".gitkeep").write_text("", encoding="utf-8")
    run("add", "-A")
    run("commit", "-q", "-m", "initial")


@dataclass
class Harness:
    db_path: str
    collab: CollabStore
    dal: SwarmDal
    workdir: Path
    run_id: str
    root_card_id: str
    card_ids: dict[str, str]
    world: FakeWorld
    emitter: RecordingEmitter
    router: FakeRouter
    reviewer: ScriptedReviewer
    limits: FakeLimits
    git: Any
    clock: SchedulerClock
    scheduler: SwarmScheduler | None = None
    extras: dict[str, Any] = field(default_factory=dict)

    def task_id(self, task_key: str) -> str:
        return self.card_ids[task_key]

    def task_row(self, task_key: str) -> dict[str, Any]:
        rows = [
            t for t in self.dal.tasks_for_run(self.run_id) if str(t["id"]) == self.task_id(task_key)
        ]
        return rows[0]

    def status_of(self, task_key: str) -> str:
        return str(self.task_row(task_key)["status"])

    def attempts_of(self, task_key: str) -> list[dict[str, Any]]:
        return self.dal.list_attempts(self.task_id(task_key))

    def swarm_json_of(self, task_key: str) -> dict[str, Any]:
        return self.dal.get_swarm_json(self.task_id(task_key)) or {}

    def close(self) -> None:
        if self.scheduler is not None:
            self.scheduler.shutdown()
        self.dal.close()


def make_harness(
    tmp_path: Path,
    specs: list[dict[str, Any]],
    *,
    integration: bool = True,
    target_n: int = 2,
    max_concurrency: int = 10,
    budget: float | None = None,
    real_git: bool = False,
    fake_clock: bool = False,
) -> Harness:
    # Pre-migrated template copy instead of a fresh 86-migration apply per test.
    db_path = migrated_db(CollabStore, tmp_path / "swarm.db")
    collab = CollabStore(db_path)
    dal = SwarmDal(db_path)
    workdir = tmp_path / "ws"
    workdir.mkdir(exist_ok=True)
    if real_git:
        init_git_repo(workdir)
    plan = make_plan(specs, integration=integration, target_n=target_n)
    if specs:
        provisioned = provision_run(
            plan,
            dal=dal,
            working_dir=str(workdir),
            budget_usd_max=budget,
            max_concurrency=max_concurrency,
        )
        run_id = str(provisioned["run"]["id"])
        root_card_id = str(provisioned["root_card_id"])
        card_ids = dict(provisioned["card_ids"])
    else:
        # plan-safety refuses to provision empty plans; a planning-stage run is
        # a bare run row with no cards yet — fabricate it the way intake does.
        row = dal.create_run(working_dir=str(workdir), source="test", goal=plan.goal, status="planning")
        run_id = str(row["id"])
        root_card_id = ""
        card_ids = {}
    world = FakeWorld()
    harness = Harness(
        db_path=db_path,
        collab=collab,
        dal=dal,
        workdir=workdir,
        run_id=run_id,
        root_card_id=root_card_id,
        card_ids=card_ids,
        world=world,
        emitter=RecordingEmitter(),
        router=FakeRouter(),
        reviewer=ScriptedReviewer(),
        limits=FakeLimits(capacity=100, world=world),
        git=None,
        clock=FakeClock() if fake_clock else SchedulerClock(),
    )
    harness.git = None  # set below (real vs fake)
    if real_git:
        from omniagentos.swarm.scheduler import SubprocessSwarmGit

        harness.git = SubprocessSwarmGit()
    else:
        harness.git = FakeGit()
    return harness


def make_scheduler(harness: Harness, **overrides: Any) -> SwarmScheduler:
    kwargs: dict[str, Any] = dict(
        dal=harness.dal,
        collab=harness.collab,
        emitter=harness.emitter,
        spawner=harness.world,
        session_store=harness.world,
        router=harness.router,
        reviewer=harness.reviewer,
        splitter=lambda task, swarm_json: None,
        verifier=lambda task, swarm_json, working_dir: (True, ""),
        git=harness.git,
        limits=harness.limits,
        clock=harness.clock,
        # Phase-1 by default (deterministic regardless of ambient config/env);
        # worktree tests pass worktrees=FakeWorktrees(...) +
        # worktrees_enabled=True explicitly.
        worktrees_enabled=False,
        heartbeat_seconds=0.05,
        fallback_poll_seconds=0.05,
        worker_poll_seconds=0.01,
        await_poll_seconds=0.01,
        # WP7: fake by default like every other collaborator here — a test
        # that wants the real omniagentos.swarm.summary.write_summary (or its
        # own recorder) passes summary_writer= explicitly (see
        # test_summary_scheduler_hook.py). Without this, every scheduler test
        # that completes a run would shell out to a real Fable CLI call and
        # write a real vault note under the repo's vault/ directory.
        summary_writer=lambda run_id: None,
    )
    kwargs.update(overrides)
    scheduler = SwarmScheduler(**kwargs)
    harness.scheduler = scheduler
    return scheduler
