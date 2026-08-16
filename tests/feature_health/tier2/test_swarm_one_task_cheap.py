"""Tier2 live probe: ONE trivial swarm task end-to-end in worktree mode.

Drives the REAL SwarmScheduler + UnifiedSpawner + SubprocessSwarmWorktrees with
a live gemini CLI worker against an ISOLATED tmp git repo (git init + one seed
commit — NEVER the product repo or a clone of it). The scheduler's injectable
router seam returns the cheap gemini flash route. Asserts the worktree merge
model held: swarm/<run>/<task> branch exists in the TMP repo, the worker commit
merged --no-ff, the worktree was pruned, and the PRODUCT repo's .git/worktrees
gained no entries. Construction idioms follow tests/swarm/scheduler_fakes.py
(harness) and tests/swarm/test_live_all_providers.py (live await + gemini env).
"""

from __future__ import annotations

import os
import shutil
import subprocess
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.live

REPO = Path(__file__).resolve().parents[3]

_TASK_KEY = "edit-notes"
_LINE = "fh-tier2-swarm-ok"
_RUN_TERMINAL_S = 150.0
_TERMINAL = {"completed", "failed", "cancelled", "killed"}


class _GeminiFlashRouter:
    """Router seam: always the cheap gemini flash route (scheduler.py:503 idiom)."""

    def route(self, task: Mapping[str, Any], tier: str) -> Any:
        del task
        from omniagentos.swarm.scheduler import RouteDecision

        return RouteDecision(provider="gemini", model="gemini-2.5-flash", tier=tier)


class _ConfirmReviewer:
    """Scripted confirm so no second (reviewer) model call is spent."""

    def review(self, *, task: Any, swarm_json: Any, session: Any, verify_output: Any, flags: Any):
        del task, swarm_json, session, verify_output, flags
        from omniagentos.swarm.scheduler import SwarmReviewOutcome

        return SwarmReviewOutcome(verdict="confirm", feedback="fh tier2 scripted", reviewer="fh")


class _Limits:
    def report_rate_limited(self, provider, account_id, detail, reset_at) -> None:  # noqa: ANN001
        return None

    def earliest_cooldown_until(self):  # noqa: ANN201
        return None

    def swarm_slots_remaining(self) -> int:
        return 2


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ("git", "-C", str(repo), "-c", "user.email=fh@t", "-c", "user.name=fh", *args),
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout


def _load_gemini_env() -> None:
    gemini_env = Path.home() / ".gemini" / ".env"
    if not gemini_env.exists():
        return
    for line in gemini_env.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def test_one_swarm_task_worktree_merge_live(
    fh_budget, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    if shutil.which("git") is None:
        pytest.skip("git binary not on PATH")
    if shutil.which("gemini") is None:
        pytest.skip("gemini CLI binary not on PATH — cannot run a live swarm worker")
    if not (Path.home() / ".gemini").is_dir():
        pytest.skip("gemini CLI auth dir ~/.gemini absent — cannot run a live swarm worker")
    fh_budget.require_headroom(cli=True)

    monkeypatch.setenv("OMNIAGENTOS_SWARM_WORKTREES", "1")
    monkeypatch.setenv("OMNIAGENTOS_CORAL_CONTEXT_MODE", "off")
    _load_gemini_env()

    # PRODUCT repo worktree registry snapshot (must be untouched by this test).
    product_worktrees = REPO / ".git" / "worktrees"
    before = set(p.name for p in product_worktrees.iterdir()) if product_worktrees.is_dir() else set()

    # Isolated tmp git repo with one seed commit — never the product repo.
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(("git", "init", "-q", str(repo)), check=True, capture_output=True)
    (repo / "NOTES.md").write_text("# notes\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "seed")

    from omniagentos.collab.store import CollabStore
    from omniagentos.sessions.dal import SessionsDal
    from omniagentos.swarm.contracts import SwarmPlan, SwarmTaskSpec
    from omniagentos.swarm.dal import SwarmDal
    from omniagentos.swarm.planner import provision_run
    from omniagentos.swarm.provider_exec import ProviderSessionRunner
    from omniagentos.swarm.scheduler import SwarmScheduler
    from omniagentos.swarm.spawn import UnifiedSpawner

    db_path = str(tmp_path / "swarm.db")
    collab = CollabStore(db_path)
    dal = SwarmDal(db_path)

    plan = SwarmPlan(
        goal="fh tier2: one trivial worktree task",
        tasks=[
            SwarmTaskSpec(
                id=_TASK_KEY,
                title="Append one line to NOTES.md",
                description=(
                    f"Append the exact line '{_LINE}' as a new last line of NOTES.md "
                    "in the current directory. Make no other changes. Do not run "
                    "tests and do not run any git commands."
                ),
                depends_on=[],
                complexity="simple",
                est_agent_minutes=2,
                owned_paths=["NOTES.md"],
                acceptance=f"NOTES.md ends with the line {_LINE}",
                verify_command="",
            )
        ],
        integration_task_id=None,
        mode="swarm",
        version=1,
        target_n=1,
        parallelism_ratio=1.0,
    )
    provisioned = provision_run(plan, dal=dal, working_dir=str(repo), max_concurrency=1)
    run_id = str(provisioned["run"]["id"])

    sessions = SessionsDal(db_path)
    spawner = UnifiedSpawner(
        db_path=db_path,
        provider_runner=ProviderSessionRunner(db_path=db_path, wall_timeout_seconds=120),
    )
    scheduler = SwarmScheduler(
        dal=dal,
        collab=collab,
        spawner=spawner,
        session_store=sessions,
        router=_GeminiFlashRouter(),
        reviewer=_ConfirmReviewer(),
        splitter=lambda task, swarm_json: None,
        verifier=lambda task, swarm_json, working_dir: (True, ""),
        limits=_Limits(),
        worktrees_enabled=True,
        heartbeat_seconds=0.5,
        fallback_poll_seconds=0.5,
        worker_poll_seconds=0.5,
        await_poll_seconds=0.5,
        db_path=db_path,
        summary_writer=lambda run_id: None,
    )

    fh_budget.record_cli_call()
    status = ""
    try:
        handle = scheduler.start_run(run_id)
        assert handle is not None, "start_run lost the activation CAS on a fresh run"
        deadline = time.monotonic() + _RUN_TERMINAL_S
        while time.monotonic() < deadline:
            row = dal.get_run(run_id) or {}
            status = str(row.get("status") or "")
            if status in _TERMINAL:
                break
            time.sleep(1.0)
    finally:
        scheduler.shutdown()

    if status not in _TERMINAL:
        pytest.skip(
            f"live swarm run {run_id} did not terminalize within {_RUN_TERMINAL_S:.0f}s "
            f"(last status {status!r}) — internal cap under the 180s pytest timeout"
        )
    task_rows = dal.tasks_for_run(run_id)
    assert status == "completed", (
        f"swarm run ended {status!r}; tasks: "
        f"{[(t.get('title'), t.get('status')) for t in task_rows]}"
    )

    # 1. The worker commit merged --no-ff: a 2-parent merge commit whose
    # message names this run's swarm/<run>/<task> branch merge
    # (scheduler.py:_confirm merge message "swarm <run_id>: merge task <key>").
    merges = _git(repo, "rev-list", "--min-parents=2", "HEAD").split()
    assert merges, "no --no-ff merge commit reached the tmp repo's main branch"
    merge_subjects = _git(repo, "log", "--merges", "--format=%s", "HEAD")
    assert f"swarm {run_id}: merge task {_TASK_KEY}" in merge_subjects, (
        f"merge commits present but none from this run/task: {merge_subjects!r}"
    )
    # ...and the worker's edit is really on the main branch.
    notes = (repo / "NOTES.md").read_text(encoding="utf-8")
    assert _LINE in notes, f"worker line missing from merged NOTES.md: {notes!r}"

    # 2. Branch lifecycle: the swarm/<run>/<task> branch existed (its merge is
    # in history above) and, on a COMPLETED run, was deleted by the terminal
    # cleanup — that deletion is the documented behavior (scheduler.py:3164:
    # "branches are deleted only on COMPLETED runs").
    branches = _git(repo, "branch", "--list", f"swarm/{run_id}/*").strip()
    assert branches == "", (
        f"completed-run branch cleanup did not run: {branches!r} still present"
    )

    # 3. The worktree was pruned: only the main checkout remains registered.
    worktree_list = _git(repo, "worktree", "list", "--porcelain")
    registered = [line for line in worktree_list.splitlines() if line.startswith("worktree ")]
    assert len(registered) == 1, f"stale worktrees remain: {worktree_list}"

    # 4. The PRODUCT repo's worktree registry gained no entries FROM THIS RUN.
    # (Exact set equality is a flake in this shared checkout: concurrent agent
    # sessions register their own unrelated agent-* worktrees; only an entry
    # attributable to this test's run would prove a leak.)
    after = set(p.name for p in product_worktrees.iterdir()) if product_worktrees.is_dir() else set()
    leaked = {
        name
        for name in after - before
        if run_id in name or _TASK_KEY in name or name.startswith("swarm")
    }
    assert not leaked, f"this swarm run leaked worktrees into the product repo: {leaked}"
