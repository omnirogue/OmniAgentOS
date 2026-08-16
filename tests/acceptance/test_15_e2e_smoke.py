"""AT4 area 15 — End-to-end smoke: ONE complete simulated run.

    planning -> decomposition -> implementation -> verification
             -> merge -> learning -> promotion

This is the keystone test, so the rule it is written to is: **mock the model,
not the machinery.** Exactly three seams are faked, and all three are places
where a token would otherwise be spent:

  1. ``planner_llm``      -- the planning model call.
  2. ``reviewer``         -- the verification model call.
  3. ``executor_runner``  -- the implementation agent. This one is NOT a
     no-op stub: it writes real files into a real git worktree, makes a real
     commit, and drives the REAL ``ApprovalGateway`` with real proposed
     actions. Only the "an LLM decided what to write" part is replaced.

Everything between those seams is production code, in particular:

  * ``estimate_complexity`` + ``resolve_execution``  (tier/effort resolution)
  * ``plan_goal``                                     (decomposition)
  * ``render_spec_markdown`` / ``write_spec_doc``     (the spec artifact)
  * ``ApprovalGateway`` / ``classify_hard_stop``      (money/delete/secret)
  * the corrective-retry loop and ``_aggregate_status``
  * ``SubprocessWorktrees.merge_branch``              (a real ``git merge --no-ff``)
  * ``append_manifest`` / ``read_manifests``          (the append-only ledger)
  * ``selfimprove.curator.curate``                    (learning capture)
  * ``lab.campaign.run_experiment``                   (the promotion gate)

Fully offline: no network, no provider CLI, no real LLM. ``git`` runs as a
local subprocess inside ``tmp_path``. Every write is under ``tmp_path``.
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

import omniagentos.lab.campaign as campaign
from omniagentos.contracts import (
    AgentUsage,
    HarnessProfile,
    HarnessType,
    RunManifest,
    RunState,
)
from omniagentos.lab.contracts import Disposition
from omniagentos.ledger import append_manifest, read_manifests
from omniagentos.orchestrator import run_orchestration
from omniagentos.orchestrator.contracts import (
    ApprovalRequest,
    ExecutorRequest,
    ExecutorResult,
    ExecutorTier,
    HardStop,
    LearnEvent,
    ReviewVerdict,
)
from omniagentos.selfimprove.curator import curate
from omniagentos.worktrees.git import SubprocessWorktrees

from .conftest import make_experiment

# A goal whose cheap complexity estimate is stable and lands on the
# multi-task planning path (so decomposition is actually exercised).
_GOAL = (
    "Build an end-to-end reporting platform: integrate the metrics service, "
    "add a pipeline, wire a dashboard and ship a migration workflow"
)


# ---------------------------------------------------------------------------
# The three faked seams
# ---------------------------------------------------------------------------


def _planner_llm(tasks: list[dict[str, Any]]) -> Any:
    """FAKE #1: the planning model. Records the effort it was invoked at."""
    calls: list[str] = []

    def _llm(prompt: str, schema: dict[str, Any], effort: str) -> dict[str, Any]:
        calls.append(effort)
        assert "schema" not in prompt.lower() or schema, "the planner is given a schema"
        return {
            "project_name": "Reporting platform",
            "description": "Ship the reporting platform",
            "complexity": "complex",
            "tasks": tasks,
        }

    _llm.calls = calls  # type: ignore[attr-defined]
    return _llm


@dataclass
class _RecordingReviewer:
    """FAKE #2: the verification model. Verdicts are scripted per call."""

    verdicts: list[str]
    calls: list[str] = field(default_factory=list)

    def review(self, *, task: Any, spec_markdown: str, result: ExecutorResult) -> ReviewVerdict:
        # The reviewer must be handed the SPEC, not just the output -- that is
        # what makes it a review against acceptance criteria rather than a
        # vibe check. Assert it here so a spec-less review would fail the run.
        assert task.title in spec_markdown, "the reviewer must see the spec for this task"
        assert result.output_text, "the reviewer must see the executor's output"
        verdict = self.verdicts[min(len(self.calls), len(self.verdicts) - 1)]
        self.calls.append(verdict)
        return ReviewVerdict(
            verdict=verdict,  # type: ignore[arg-type]
            feedback="acceptance criterion not evidenced",
            reviewer="acceptance-fake-critic",
        )


@dataclass
class _GitWritingRunner:
    """FAKE #3 (partial): the implementation agent.

    The *decision* about what to write is scripted; the *effects* are real --
    a real file write and a real ``git commit`` in a real worktree. It also
    drives the REAL approval gateway with a safe action, a money action and a
    delete action, so gateway classification is genuinely exercised.
    """

    worktree: Path
    requests: list[ExecutorRequest] = field(default_factory=list)
    commits: list[str] = field(default_factory=list)

    _ACTIONS = (
        ApprovalRequest(
            proposed_action="write the report module",
            action_class="consequential",
            tool_name="Write",
            tool_input={"file_path": "report.py"},
        ),
        ApprovalRequest(
            proposed_action="pay the data vendor invoice",
            action_class="consequential",
            tool_name="Bash",
            tool_input={"command": "stripe charge --amount 4200"},
        ),
        ApprovalRequest(
            proposed_action="clean the workspace",
            action_class="consequential",
            tool_name="Bash",
            tool_input={"command": "rm -rf ./build"},
        ),
    )

    def run(self, request: ExecutorRequest, gateway: Any) -> ExecutorResult:
        self.requests.append(request)
        # Every proposed action goes through the REAL gateway; an escalated one
        # is simply not performed, exactly as a live executor would behave.
        performed = [action for action in self._ACTIONS if gateway.resolve(action).approved]
        assert performed, "at least the ordinary file write must be approved"
        index = len(self.requests)
        target = self.worktree / f"module_{index}.py"
        target.write_text(f'"""Implements: {request.task.title}"""\n', encoding="utf-8")
        _git(self.worktree, "add", "-A")
        _git(self.worktree, "commit", "-m", f"feat: {request.task.title}")
        sha = _git(self.worktree, "rev-parse", "HEAD").stdout.strip()
        self.commits.append(sha)
        session_id = f"ses-{index}"
        if request.on_spawn is not None:
            request.on_spawn(session_id)
        return ExecutorResult(
            status="ok",
            output_text=f"wrote {target.name} at {sha[:8]}",
            session_id=session_id,
            commits=[sha],
            working_dir=str(self.worktree),
        )


@dataclass
class _RecordingNotifier:
    escalations: list[HardStop] = field(default_factory=list)

    def escalate(self, request: ApprovalRequest, category: HardStop) -> str | None:
        self.escalations.append(category)
        return f"notif-{len(self.escalations)}"


@dataclass
class _RecordingLearnHook:
    events: list[LearnEvent] = field(default_factory=list)

    def __call__(self, event: LearnEvent) -> None:
        self.events.append(event)


# ---------------------------------------------------------------------------
# Real git plumbing (local subprocess, inside tmp_path -- still fully offline)
# ---------------------------------------------------------------------------

_GIT_IDENTITY = (
    "-c",
    "user.email=acceptance@omniagentos.local",
    "-c",
    "user.name=AT4 Acceptance",
    "-c",
    "core.hooksPath=",
    "-c",
    "commit.gpgsign=false",
)


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 -- fixed argv, never shell
        ("git", "-C", str(cwd), *_GIT_IDENTITY, *args),
        capture_output=True,
        text=True,
        timeout=60,
        check=True,
    )


@pytest.fixture
def git_repo(tmp_path: Path) -> Path:
    """A real, empty git repository with one commit on ``main``."""
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(  # noqa: S603
        ("git", "-C", str(repo), "init", "-q", "-b", "main"),
        capture_output=True,
        text=True,
        timeout=60,
        check=True,
    )
    (repo / "README.md").write_text("# acceptance repo\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "chore: init")
    return repo


# ---------------------------------------------------------------------------
# THE end-to-end run
# ---------------------------------------------------------------------------


@pytest.mark.acceptance_smoke
def test_one_complete_run_from_planning_through_promotion(
    tmp_path: Path,
    git_repo: Path,
    isolated_roots: dict[str, Path],
    offline_lab: tuple[Any, Any, str],
) -> None:
    """One goal, all seven stages, no model call and no network.

    Each stage asserts something the NEXT stage depends on, so a break
    anywhere in the chain surfaces at the stage that broke rather than as a
    single opaque failure at the end.
    """
    lab_store, evaluator, suite_id = offline_lab
    vault_dir = isolated_roots["vault"]
    ledger_dir = isolated_roots["ledger"]

    # ---- Stage 1+2: planning and decomposition ---------------------------
    planner = _planner_llm(
        [
            {
                "title": "Add the metrics ingest module",
                "description": "ingest metrics from the service",
                "acceptance_criteria": ["ingests metrics", "has a test"],
            },
            {
                "title": "Wire the dashboard panel",
                "description": "render the ingested metrics",
                "acceptance_criteria": ["panel renders"],
            },
        ]
    )
    reviewer = _RecordingReviewer(verdicts=["deny", "confirm", "confirm"])
    notifier = _RecordingNotifier()
    learn_hook = _RecordingLearnHook()

    # A real worktree off the real repo -- this is where "implementation" lands.
    worktrees = SubprocessWorktrees(namespace="at4", var_root=tmp_path / "var")
    assert worktrees.supported(str(git_repo)), "the acceptance repo must be a real git repo"
    info = worktrees.create(str(git_repo), "run-at4", "unit-1", "main")
    runner = _GitWritingRunner(worktree=Path(info.path))

    result = run_orchestration(
        _GOAL,
        priority="balanced",
        working_dir=str(git_repo),
        planner_llm=planner,
        reviewer=reviewer,
        executor_runner=runner,
        approval_notifier=notifier,
        learn_hook=learn_hook,
        vault_dir=str(vault_dir),
        reflexion_store_dir=str(isolated_roots["reflexion"]),
        cascade_trace_path=str(tmp_path / "cascade.jsonl"),
    )

    # Planning happened at a real, resolved effort (not a hard-coded default).
    assert planner.calls, "the planner seam must actually be invoked"  # type: ignore[attr-defined]
    assert planner.calls[0] in {"low", "medium", "high", "max"}  # type: ignore[attr-defined]
    # Decomposition produced the two planned tasks as separate execution units.
    assert [outcome.task_title for outcome in result.tasks] == [
        "Add the metrics ingest module",
        "Wire the dashboard panel",
    ]
    # The tier came from the REAL resolver, driven by the real complexity estimate.
    assert result.resolved.executor_tier is ExecutorTier.FUSION
    assert result.resolved.run_quality_gate is True
    # The spec artifact exists on disk and names every task.
    assert result.spec_note_path is not None
    spec_text = Path(result.spec_note_path).read_text(encoding="utf-8")
    for outcome in result.tasks:
        assert outcome.task_title in spec_text

    # ---- Stage 3: implementation (real files, real commits) ---------------
    assert len(runner.commits) == 3, "two tasks + one corrective retry"
    assert len(set(runner.commits)) == 3, "each attempt must be a distinct commit"
    log = _git(Path(info.path), "log", "--format=%s", "-3").stdout.split("\n")
    assert any("Add the metrics ingest module" in line for line in log)

    # Approvals were classified by the REAL gateway, not waved through.
    escalated = [decision.category for decision in result.escalations]
    assert set(escalated) == {"money", "delete"}, (
        f"a payment and a recursive delete must both escalate; got {set(escalated)}"
    )
    # The notifier saw EVERY escalation, in order -- an escalation that never
    # reaches a human is not an escalation.
    assert notifier.escalations == escalated
    assert len(escalated) == 2 * len(runner.requests), "both hard stops park on every attempt"
    # ...and the ordinary file write auto-approved rather than parking too.
    approved = [d for d in result.approvals if d.approved]
    assert len(approved) == len(runner.requests)
    assert all(not d.escalated for d in approved)

    # ---- Stage 4: verification ------------------------------------------
    # The first review DENIED, so the loop must have driven a corrective retry
    # and only then accepted. A gate that ignored the verdict would show 1.
    assert reviewer.calls == ["deny", "confirm", "confirm"]
    assert result.tasks[0].attempts == 2, "a denied review must trigger exactly one corrective"
    assert result.tasks[0].status == "done"
    assert result.tasks[0].review is not None
    assert result.tasks[0].review.verdict == "confirm"
    assert result.status == "done"

    # ---- Stage 5: merge (a real git merge --no-ff) -----------------------
    head_before = _git(git_repo, "rev-parse", "HEAD").stdout.strip()
    merged_sha = _git(Path(info.path), "rev-parse", "HEAD").stdout.strip()
    merge = worktrees.merge_branch(
        str(git_repo), info.branch, "merge: at4 acceptance unit", sha=merged_sha
    )

    assert merge.status == "merged", f"merge must succeed cleanly: {merge}"
    assert merge.sha is not None and merge.sha != head_before
    # The implementation is actually present on main now.
    merged_tree = _git(git_repo, "ls-tree", "--name-only", "HEAD").stdout.split()
    assert "module_1.py" in merged_tree and "module_3.py" in merged_tree

    # ---- Stage 6: learning ----------------------------------------------
    # The orchestrator fired its learners for every completed executor session.
    assert [event.task_title for event in learn_hook.events] == [
        "Add the metrics ingest module",
        "Add the metrics ingest module",
        "Wire the dashboard panel",
    ]
    assert all(event.session_id for event in learn_hook.events)

    # The run is recorded in the append-only ledger, then mined into a skill.
    manifest = RunManifest(
        run_id=result.run_id,
        task_id="at4-acceptance",
        discipline="at4",
        harness=HarnessProfile(
            harness=HarnessType.FUSION, version="2026.07", env_hash="sha256:env-at4"
        ),
        model="mock-model",
        state=RunState.COMPLETED,
        started_at="2026-07-27T09:00:00Z",
        finished_at="2026-07-27T09:30:00Z",
        usage=AgentUsage(wall_ms=1_800_000, input_tokens=5_000, output_tokens=2_000),
        output_digest=f"sha256:{merge.sha}",
        artifacts=["module_1.py", "module_3.py"],
    )
    append_manifest(str(ledger_dir), manifest)
    assert [m.run_id for m in read_manifests(str(ledger_dir), limit=10)] == [result.run_id]

    curated = curate(
        ledger_dir=str(ledger_dir),
        vault_dir=str(vault_dir),
        skills_dir=str(isolated_roots["skills"]),
        autocommit=False,
        skills_api=None,
    )
    assert curated.captured == [result.run_id], f"the completed run must yield a skill: {curated}"
    assert curated.unverified == []
    skill_notes = list(vault_dir.rglob("*.md"))
    assert skill_notes, "learning must leave a durable, re-readable note"
    note_text = "\n".join(path.read_text(encoding="utf-8") for path in skill_notes)
    assert result.run_id in note_text, "the learning must cite the run it came from"

    # ---- Stage 7: promotion ----------------------------------------------
    # A replicated, genuinely-better challenger promotes...
    promoted = make_experiment(
        lab_store,
        suite_id,
        challenger_prompt="WINNING",
        exp_id="exp_e2e_promote",
        replicates=2,
        hypothesis=f"the change merged as {merge.sha} improves accuracy",
    )
    assert (
        campaign.run_experiment(lab_store, evaluator, promoted, dry_run=True) is Disposition.PROMOTE
    )
    champion = lab_store.get_champion("at4", "prompt")
    assert champion is not None

    # ...and the SAME improvement observed only once does not.
    unreplicated = make_experiment(
        lab_store,
        suite_id,
        challenger_prompt="WINNING",
        exp_id="exp_e2e_single",
        replicates=1,
    )
    assert (
        campaign.run_experiment(lab_store, evaluator, unreplicated, dry_run=True)
        is Disposition.REJECT
    )


# ---------------------------------------------------------------------------
# Seam probes: prove the stubs above are not hiding a broken machine
# ---------------------------------------------------------------------------


@pytest.mark.acceptance_daily
def test_a_persistently_denied_task_fails_the_run_instead_of_passing_it(
    tmp_path: Path, git_repo: Path, isolated_roots: dict[str, Path]
) -> None:
    """The quality gate must be able to FAIL a run, or it is decoration.

    Same harness as the full loop, only the reviewer's verdicts change. If the
    orchestration reported ``done`` here, the confirm in the main test would
    prove nothing.
    """
    worktrees = SubprocessWorktrees(namespace="at4d", var_root=tmp_path / "var")
    info = worktrees.create(str(git_repo), "run-deny", "unit-1", "main")
    runner = _GitWritingRunner(worktree=Path(info.path))

    result = run_orchestration(
        _GOAL,
        priority="balanced",
        working_dir=str(git_repo),
        planner_llm=_planner_llm(
            [
                {
                    "title": "Add the metrics ingest module",
                    "description": "ingest metrics",
                    "acceptance_criteria": ["ingests metrics"],
                }
            ]
        ),
        reviewer=_RecordingReviewer(verdicts=["deny"]),
        executor_runner=runner,
        approval_notifier=_RecordingNotifier(),
        learn_hook=_RecordingLearnHook(),
        vault_dir=str(isolated_roots["vault"]),
        reflexion_store_dir=str(isolated_roots["reflexion"]),
        cascade_trace_path=str(tmp_path / "cascade.jsonl"),
    )

    assert result.status == "failed"
    assert [outcome.status for outcome in result.tasks] == ["denied"]
    assert result.tasks[0].attempts > 1, "the loop must retry before giving up"


@pytest.mark.acceptance_daily
def test_a_conflicting_merge_is_refused_and_leaves_main_pristine(
    tmp_path: Path, git_repo: Path
) -> None:
    """Stage 5 is real: a genuine conflict must abort, not half-apply.

    Without this, the successful merge in the main test could be passing on a
    seam that silently swallows every failure.
    """
    worktrees = SubprocessWorktrees(namespace="at4c", var_root=tmp_path / "var")
    info = worktrees.create(str(git_repo), "run-conflict", "unit-1", "main")
    worktree = Path(info.path)

    (worktree / "shared.txt").write_text("from the branch\n", encoding="utf-8")
    _git(worktree, "add", "-A")
    _git(worktree, "commit", "-m", "feat: branch side")
    (git_repo / "shared.txt").write_text("from main\n", encoding="utf-8")
    _git(git_repo, "add", "-A")
    _git(git_repo, "commit", "-m", "feat: main side")
    head_before = _git(git_repo, "rev-parse", "HEAD").stdout.strip()

    merge = worktrees.merge_branch(str(git_repo), info.branch, "merge: conflicting")

    assert merge.status == "conflict"
    assert "shared.txt" in merge.conflict_files
    assert _git(git_repo, "rev-parse", "HEAD").stdout.strip() == head_before
    assert worktrees.has_pending_merge(str(git_repo)) is False, "the merge must be aborted"
    assert (git_repo / "shared.txt").read_text(encoding="utf-8") == "from main\n"


@pytest.mark.acceptance_daily
def test_an_unverified_run_produces_no_learning(
    tmp_path: Path, isolated_roots: dict[str, Path]
) -> None:
    """Stage 6 is real: a FAILED run must not be mined into a skill.

    This is the guard that makes the ``curated.captured`` assertion in the main
    test meaningful -- delete the gate check and this goes red.
    """
    ledger_dir = isolated_roots["ledger"]
    append_manifest(
        str(ledger_dir),
        RunManifest(
            run_id="run-failed",
            task_id="at4-acceptance",
            discipline="at4",
            harness=HarnessProfile(harness=HarnessType.FUSION, env_hash="sha256:env"),
            state=RunState.FAILED,
            finished_at="2026-07-27T09:30:00Z",
        ),
    )

    curated = curate(
        ledger_dir=str(ledger_dir),
        vault_dir=str(isolated_roots["vault"]),
        skills_dir=str(isolated_roots["skills"]),
        autocommit=False,
        skills_api=None,
    )

    assert curated.captured == []
    assert curated.unverified == ["run-failed"]
    assert list(Path(isolated_roots["vault"]).rglob("*.md")) == []


@pytest.mark.acceptance_daily
def test_the_orchestration_result_is_serialisable_for_the_trace(
    tmp_path: Path, git_repo: Path, isolated_roots: dict[str, Path]
) -> None:
    """Every stage's decision must survive as inspectable JSON (area 11 tie-in)."""
    worktrees = SubprocessWorktrees(namespace="at4s", var_root=tmp_path / "var")
    info = worktrees.create(str(git_repo), "run-trace", "unit-1", "main")

    result = run_orchestration(
        _GOAL,
        priority="quality",
        working_dir=str(git_repo),
        planner_llm=_planner_llm(
            [
                {
                    "title": "Add the metrics ingest module",
                    "description": "ingest metrics",
                    "acceptance_criteria": ["ingests metrics"],
                }
            ]
        ),
        reviewer=_RecordingReviewer(verdicts=["confirm"]),
        executor_runner=_GitWritingRunner(worktree=Path(info.path)),
        approval_notifier=_RecordingNotifier(),
        learn_hook=_RecordingLearnHook(),
        vault_dir=str(isolated_roots["vault"]),
        reflexion_store_dir=str(isolated_roots["reflexion"]),
        cascade_trace_path=str(tmp_path / "cascade.jsonl"),
    )

    trace = {
        "run_id": result.run_id,
        "status": result.status,
        "plan": json.loads(result.plan.model_dump_json()),
        "resolved_tier": result.resolved.executor_tier.value,
        "tasks": [
            {
                "title": t.task_title,
                "status": t.status,
                "attempts": t.attempts,
                "session": t.session_id,
            }
            for t in result.tasks
        ],
        "escalations": [d.category for d in result.escalations],
    }
    encoded = json.dumps(trace)

    assert json.loads(encoded)["resolved_tier"] == "fusion_ultra", (
        "priority=quality must resolve the ultra tier"
    )
    assert json.loads(encoded)["tasks"][0]["session"] == "ses-1"
