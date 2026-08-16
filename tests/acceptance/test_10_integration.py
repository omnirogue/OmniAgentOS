"""AT3 area 10 — INTEGRATION.

Acceptance claims under test:

  1. Completed work MERGES correctly.
  2. Integration verification runs and its verdict is honoured.
  3. Artifacts are PRESERVED (nothing a worker produced is silently destroyed).
  4. Final output matches the acceptance criteria (and is REJECTED when it does
     not, rather than being waved through).

Ground truth:
  * ``omniagentos/worktrees/git.py`` — ``SubprocessWorktrees.merge_branch``
    (sha-pinned ``merge --no-ff``, conflict -> ``merge --abort`` + branch kept),
    ``salvage_commit``, ``remove(salvage=...)``.
  * ``omniagentos/fanin/adjudicate.py`` — conflict-preserving synthesis, the
    merge budget and ``MergeEscalationExhausted`` escalation.
  * ``omniagentos/fanin/pipeline.py`` — ``run_fanin_pipeline`` verify stage.
  * ``omniagentos/swarm/summary_fanin.py`` — ``fanin_multi_attempt_tasks``.

Hermetic: real ``git`` against a ``tmp_path`` repo, no network, no LLM.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import pytest

from omniagentos.fanin import Candidate, FanInMode, run_fanin_pipeline
from omniagentos.fanin.adjudicate import (
    MergeEscalationExhausted,
    adjudicate,
    get_last_synthesis_evidence,
)
from omniagentos.fanin.pipeline import VerifyOutcome
from omniagentos.swarm.summary_fanin import fanin_multi_attempt_tasks
from omniagentos.worktrees.git import MergeOutcome, SubprocessWorktrees

OWNER = "swr_at3int"


# ---------------------------------------------------------------------------
# git helpers (real git, tmp repo)
# ---------------------------------------------------------------------------


def _git(cwd: Path | str, *args: str) -> str:
    proc = subprocess.run(
        ("git", "-C", str(cwd), "-c", "user.email=t@t", "-c", "user.name=t", *args),
        capture_output=True,
        text=True,
        check=True,
    )
    return proc.stdout.strip()


def _git_ok(cwd: Path | str, *args: str) -> bool:
    return (
        subprocess.run(
            ("git", "-C", str(cwd), "-c", "user.email=t@t", "-c", "user.name=t", *args),
            capture_output=True,
            text=True,
            check=False,
        ).returncode
        == 0
    )


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A one-commit git repo standing in for the shared workspace."""
    root = tmp_path / "main"
    root.mkdir()
    subprocess.run(("git", "init", "-q", "-b", "main", str(root)), check=True, capture_output=True)
    (root / "README.md").write_text("base\n", encoding="utf-8")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "initial")
    return root


@pytest.fixture
def wt(tmp_path: Path) -> SubprocessWorktrees:
    return SubprocessWorktrees(
        namespace="at3",
        var_root=tmp_path / "var",
        dep_link_dirs=(),
        lock_retry_attempts=2,
        lock_retry_sleep=0.01,
    )


def _work(wt: SubprocessWorktrees, repo: Path, unit: str, rel: str, body: str) -> tuple[str, str]:
    """Create a unit worktree, write + commit one file, return (branch, sha)."""
    base = _git(repo, "rev-parse", "HEAD")
    info = wt.create(str(repo), OWNER, unit, base)
    target = Path(info.path) / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(body, encoding="utf-8")
    _git(info.path, "add", "-A")
    _git(info.path, "commit", "-q", "-m", f"{unit}: work")
    sha = wt.head_sha(info.path)
    assert sha, "worktree must have a readable HEAD after committing"
    return info.branch, sha


# ---------------------------------------------------------------------------
# 1. Completed work merges correctly
# ---------------------------------------------------------------------------


class TestCompletedWorkMerges:
    def test_two_disjoint_units_both_land_in_the_shared_workspace(
        self, wt: SubprocessWorktrees, repo: Path
    ) -> None:
        branch_a, sha_a = _work(wt, repo, "unitA", "src/a.py", "A = 1\n")
        branch_b, sha_b = _work(wt, repo, "unitB", "src/b.py", "B = 2\n")

        out_a = wt.merge_branch(str(repo), branch_a, "merge A", sha=sha_a)
        out_b = wt.merge_branch(str(repo), branch_b, "merge B", sha=sha_b)

        assert out_a.status == "merged", out_a.detail
        assert out_b.status == "merged", out_b.detail
        # The claim is about CONTENT landing, not about a status string.
        assert (repo / "src" / "a.py").read_text(encoding="utf-8") == "A = 1\n"
        assert (repo / "src" / "b.py").read_text(encoding="utf-8") == "B = 2\n"
        assert wt.has_pending_merge(str(repo)) is False

    def test_merge_is_no_ff_so_each_unit_keeps_an_auditable_merge_commit(
        self, wt: SubprocessWorktrees, repo: Path
    ) -> None:
        branch, sha = _work(wt, repo, "unitA", "src/a.py", "A = 1\n")
        outcome = wt.merge_branch(str(repo), branch, "merge A", sha=sha)
        assert outcome.status == "merged"

        parents = _git(repo, "rev-list", "--parents", "-n", "1", "HEAD").split()
        # commit + 2 parents == a real merge commit. A fast-forward would give 2
        # fields (commit + 1 parent) and lose the unit boundary entirely.
        assert len(parents) == 3, f"expected --no-ff merge commit, got parents={parents}"
        assert sha in parents[1:], "the unit's own commit must be a parent of the merge"

    def test_merge_lands_the_verified_sha_not_whatever_the_branch_ref_says(
        self, wt: SubprocessWorktrees, repo: Path
    ) -> None:
        """M3: the gate verifies a sha; that exact sha is what may be merged.

        A branch ref advanced after verification (a sibling worker, or the unit's
        own still-running process) must NOT change what lands in the workspace.
        """
        base = _git(repo, "rev-parse", "HEAD")
        info = wt.create(str(repo), OWNER, "unitA", base)
        (Path(info.path) / "payload.py").write_text("VERIFIED = True\n", encoding="utf-8")
        _git(info.path, "add", "-A")
        _git(info.path, "commit", "-q", "-m", "verified work")
        verified_sha = wt.head_sha(info.path)
        assert verified_sha

        # ... branch ref moves AFTER the gate captured the sha.
        (Path(info.path) / "payload.py").write_text("TAMPERED = True\n", encoding="utf-8")
        _git(info.path, "add", "-A")
        _git(info.path, "commit", "-q", "-m", "post-verification tamper")
        assert wt.head_sha(info.path) != verified_sha

        outcome = wt.merge_branch(str(repo), info.branch, "merge A", sha=verified_sha)

        assert outcome.status == "merged", outcome.detail
        landed = (repo / "payload.py").read_text(encoding="utf-8")
        assert landed == "VERIFIED = True\n", (
            "merge_branch(sha=...) merged the branch ref instead of the pinned sha"
        )
        assert "TAMPERED" not in landed

    def test_merging_an_already_merged_unit_is_a_noop_not_a_duplicate(
        self, wt: SubprocessWorktrees, repo: Path
    ) -> None:
        branch, sha = _work(wt, repo, "unitA", "src/a.py", "A = 1\n")
        assert wt.merge_branch(str(repo), branch, "merge A", sha=sha).status == "merged"
        head_after_first = _git(repo, "rev-parse", "HEAD")

        second: MergeOutcome = wt.merge_branch(str(repo), branch, "merge A again", sha=sha)

        assert second.status == "noop"
        assert _git(repo, "rev-parse", "HEAD") == head_after_first


# ---------------------------------------------------------------------------
# 2 + 3. Conflicts do not destroy work; artifacts are preserved
# ---------------------------------------------------------------------------


class TestArtifactsPreserved:
    def test_conflict_leaves_workspace_pristine_and_keeps_the_branch_alive(
        self, wt: SubprocessWorktrees, repo: Path
    ) -> None:
        branch_a, sha_a = _work(wt, repo, "unitA", "shared.py", "OWNER = 'A'\n")
        branch_b, sha_b = _work(wt, repo, "unitB", "shared.py", "OWNER = 'B'\n")

        assert wt.merge_branch(str(repo), branch_a, "merge A", sha=sha_a).status == "merged"
        outcome = wt.merge_branch(str(repo), branch_b, "merge B", sha=sha_b)

        assert outcome.status == "conflict"
        assert "shared.py" in outcome.conflict_files
        # Pristine: no wedged MERGE_HEAD, no conflict markers left on disk.
        assert wt.has_pending_merge(str(repo)) is False
        assert "<<<<<<<" not in (repo / "shared.py").read_text(encoding="utf-8")
        assert (repo / "shared.py").read_text(encoding="utf-8") == "OWNER = 'A'\n"
        # The losing unit's WORK still exists — a conflicting branch is unmerged
        # work and deleting it would be data loss.
        assert _git_ok(repo, "rev-parse", "--verify", f"refs/heads/{branch_b}")
        assert _git(repo, "show", f"{sha_b}:shared.py") == "OWNER = 'B'"

    def test_uncommitted_work_is_salvaged_onto_the_branch_before_teardown(
        self, wt: SubprocessWorktrees, repo: Path
    ) -> None:
        base = _git(repo, "rev-parse", "HEAD")
        info = wt.create(str(repo), OWNER, "unitA", base)
        (Path(info.path) / "in_progress.txt").write_text("half-done\n", encoding="utf-8")

        outcome = wt.remove(str(repo), info.path, salvage=True, message="salvage unitA")

        assert outcome.status == "removed"
        assert outcome.salvage_sha, "dirty worktree must be salvaged, not discarded"
        assert _git(repo, "show", f"{outcome.salvage_sha}:in_progress.txt") == "half-done"

    def test_salvaged_work_is_mergeable_afterwards(
        self, wt: SubprocessWorktrees, repo: Path
    ) -> None:
        base = _git(repo, "rev-parse", "HEAD")
        info = wt.create(str(repo), OWNER, "unitA", base)
        (Path(info.path) / "late.txt").write_text("late work\n", encoding="utf-8")
        outcome = wt.remove(str(repo), info.path, salvage=True, message="salvage unitA")
        assert outcome.salvage_sha

        merged = wt.merge_branch(str(repo), info.branch, "merge salvage", sha=outcome.salvage_sha)

        assert merged.status == "merged", merged.detail
        assert (repo / "late.txt").read_text(encoding="utf-8") == "late work\n"

    def test_clean_worktree_removal_reports_no_salvage(
        self, wt: SubprocessWorktrees, repo: Path
    ) -> None:
        base = _git(repo, "rev-parse", "HEAD")
        info = wt.create(str(repo), OWNER, "unitClean", base)
        outcome = wt.remove(str(repo), info.path, salvage=True, message="salvage clean")
        assert outcome.status == "removed"
        assert outcome.salvage_sha is None

    def test_changed_paths_since_reports_committed_and_uncommitted_output(
        self, wt: SubprocessWorktrees, repo: Path
    ) -> None:
        """The unit's artifact manifest must not under-report work in flight."""
        base = _git(repo, "rev-parse", "HEAD")
        info = wt.create(str(repo), OWNER, "unitA", base)
        (Path(info.path) / "committed.py").write_text("x = 1\n", encoding="utf-8")
        _git(info.path, "add", "-A")
        _git(info.path, "commit", "-q", "-m", "committed")
        (Path(info.path) / "untracked.py").write_text("y = 2\n", encoding="utf-8")

        changed = wt.changed_paths_since(info.path, info.base_sha)

        assert "committed.py" in changed
        assert "untracked.py" in changed


# ---------------------------------------------------------------------------
# 4. Fan-in: integration output preserves conflicts and honours its budget
# ---------------------------------------------------------------------------


def _cand(cid: str, content: dict[str, Any], score: float) -> Candidate:
    return Candidate(id=cid, content=content, score=score, confidence=score)


class TestConflictPreservingSynthesis:
    def test_enforce_mode_preserves_both_sides_of_a_disagreement(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("OMNIAGENTOS_FANIN_SYNTHESIS_MODE", "enforce")
        result = adjudicate(
            [
                _cand("w1", {"api": "v1", "shared": "same"}, 0.9),
                _cand("w2", {"api": "v2", "shared": "same"}, 0.5),
            ],
            mode=FanInMode.SYNTHESIZE,
        )

        merged = result.selected[0].content
        assert result.evidence["conflicting_keys"] == ["api"]
        assert merged["shared"] == "same"  # agreement collapses
        api = merged["api"]
        assert api["conflict"] is True
        assert api["primary"] == "v1"  # higher score wins the primary slot ...
        # ... but the loser's value survives verbatim, attributed to its worker.
        assert {c["value"] for c in api["candidates"]} == {"v1", "v2"}
        assert {c["worker_id"] for c in api["candidates"]} == {"w1", "w2"}

    def test_legacy_shallow_merge_would_silently_drop_the_loser(self) -> None:
        """Contrast case: with synthesis off, the losing value is gone.

        This is the behaviour conflict-preserving synthesis exists to replace;
        asserting it keeps the two paths from quietly converging.
        """
        result = adjudicate(
            [
                _cand("w1", {"api": "v1"}, 0.9),
                _cand("w2", {"api": "v2"}, 0.5),
            ],
            mode=FanInMode.SYNTHESIZE,
        )
        assert result.selected[0].content == {"api": "v1"}

    def test_merge_budget_exhaustion_escalates_instead_of_guessing(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("OMNIAGENTOS_FANIN_SYNTHESIS_MODE", "enforce")
        left = {f"k{i}": f"L{i}" for i in range(4)}
        right = {f"k{i}": f"R{i}" for i in range(4)}

        with pytest.raises(MergeEscalationExhausted) as excinfo:
            adjudicate(
                [_cand("w1", left, 0.9), _cand("w2", right, 0.5)],
                mode=FanInMode.SYNTHESIZE,
                merge_budget=3,
            )

        assert excinfo.value.attempts == 4
        assert "exceeds budget (3)" in excinfo.value.reason

    def test_budget_exhaustion_in_shadow_mode_records_but_does_not_raise(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("OMNIAGENTOS_FANIN_SYNTHESIS_MODE", "shadow")
        left = {f"k{i}": f"L{i}" for i in range(4)}
        right = {f"k{i}": f"R{i}" for i in range(4)}
        result = adjudicate(
            [_cand("w1", left, 0.9), _cand("w2", right, 0.5)],
            mode=FanInMode.SYNTHESIZE,
            merge_budget=3,
        )

        assert result.decision == "synthesize"  # legacy merge still returned
        evidence = get_last_synthesis_evidence()
        assert evidence is not None
        assert evidence["evidence"]["escalated"] is True
        assert evidence["evidence"]["attempts"] == 4

    def test_merge_budget_reads_the_environment_when_not_passed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("OMNIAGENTOS_FANIN_SYNTHESIS_MODE", "enforce")
        monkeypatch.setenv("OMNIAGENTOS_FANIN_MERGE_BUDGET", "1")
        with pytest.raises(MergeEscalationExhausted):
            adjudicate(
                [
                    _cand("w1", {"a": 1, "b": 1}, 0.9),
                    _cand("w2", {"a": 2, "b": 2}, 0.5),
                ],
                mode=FanInMode.SYNTHESIZE,
            )


# ---------------------------------------------------------------------------
# 5. Final output must match acceptance criteria — and be rejected when it does not
# ---------------------------------------------------------------------------


class TestFinalOutputMatchesAcceptanceCriteria:
    def test_output_failing_the_acceptance_threshold_is_rejected_for_replan(
        self, tmp_path: Path
    ) -> None:
        result = run_fanin_pipeline(
            [_cand("w1", {"answer": "weak"}, 0.10), _cand("w2", {"answer": "weaker"}, 0.05)],
            mode=FanInMode.ADJUDICATE,
            criteria={"min_score": 0.80},
            scope_root=tmp_path,
            learning_path=tmp_path / "learning.jsonl",
            decision_id="at3_reject",
        )

        assert result.fanin.decision == "reject_replan"
        assert result.needs_replan is True
        assert result.fanin.selected == ()

    def test_output_clearing_the_threshold_is_accepted_and_verified(
        self, tmp_path: Path
    ) -> None:
        result = run_fanin_pipeline(
            [_cand("w1", {"answer": "strong"}, 0.95), _cand("w2", {"answer": "weak"}, 0.10)],
            mode=FanInMode.ADJUDICATE,
            criteria={"min_score": 0.80},
            scope_root=tmp_path,
            learning_path=tmp_path / "learning.jsonl",
            decision_id="at3_accept",
        )

        assert result.fanin.decision == "adjudicated"
        assert result.needs_replan is False
        assert result.fanin.selected[0].id == "w1"
        assert "verify" in result.stages

    def test_a_failing_verify_stage_forces_a_replan(self, tmp_path: Path) -> None:
        """The integration verdict is honoured: a failed verify cannot ship."""

        def _always_fail(selected: Any, evidence: Any) -> VerifyOutcome:
            return VerifyOutcome(ok=False, decision="g5_fail", rationale="integration suite red")

        result = run_fanin_pipeline(
            [_cand("w1", {"answer": "strong"}, 0.95)],
            mode=FanInMode.ADJUDICATE,
            verify=_always_fail,
            scope_root=tmp_path,
            learning_path=tmp_path / "learning.jsonl",
            decision_id="at3_verify_fail",
        )

        assert result.verify is not None and result.verify.ok is False
        assert result.needs_replan is True, (
            "a red integration verify must not be allowed to ship as a pass"
        )

    def test_disagreement_without_a_clear_winner_is_rejected(self, tmp_path: Path) -> None:
        result = run_fanin_pipeline(
            [_cand("w1", {"answer": "left"}, 0.5), _cand("w2", {"answer": "right"}, 0.5)],
            mode=FanInMode.ADJUDICATE,
            criteria={"require_agreement": True},
            scope_root=tmp_path,
            learning_path=tmp_path / "learning.jsonl",
            decision_id="at3_disagree",
        )
        assert result.fanin.decision == "reject_replan"
        assert result.needs_replan is True


# ---------------------------------------------------------------------------
# 6. The production fan-in caller on the swarm summary path
# ---------------------------------------------------------------------------


class _FakeDal:
    """Minimal stand-in for ``SwarmDal`` — only the two methods fan-in calls."""

    def __init__(self, tasks: list[dict[str, Any]], attempts: dict[str, list[dict[str, Any]]]):
        self._tasks = tasks
        self._attempts = attempts

    def tasks_for_run(self, run_id: str) -> list[dict[str, Any]]:
        return list(self._tasks)

    def list_attempts(self, task_id: str) -> list[dict[str, Any]]:
        return list(self._attempts.get(task_id, []))


class TestSummaryFanIn:
    def test_multi_attempt_task_is_adjudicated_and_stamped(self) -> None:
        dal = _FakeDal(
            tasks=[{"id": "t1"}, {"id": "root"}],
            attempts={
                "t1": [
                    {"id": "a1", "ended_at": "2026-01-01T00:00:00Z", "end_reason": "completed"},
                    {"id": "a2", "ended_at": "2026-01-01T00:01:00Z", "end_reason": "crashed"},
                ]
            },
        )
        stamps = fanin_multi_attempt_tasks(dal=dal, run={"id": "run1", "board_task_id": "root"})

        assert len(stamps) == 1
        assert stamps[0]["task_id"] == "t1"
        # The COMPLETED attempt wins over the crashed one (0.9 vs 0.1).
        assert stamps[0]["fanin"]["evidence"]["winner_id"] == "a1"

    def test_single_attempt_task_is_not_fanned_in(self) -> None:
        dal = _FakeDal(
            tasks=[{"id": "t1"}],
            attempts={"t1": [{"id": "a1", "ended_at": "2026-01-01T00:00:00Z"}]},
        )
        assert fanin_multi_attempt_tasks(dal=dal, run={"id": "run1"}) == []

    def test_root_card_is_excluded_from_fan_in(self) -> None:
        dal = _FakeDal(
            tasks=[{"id": "root"}],
            attempts={
                "root": [
                    {"id": "a1", "ended_at": "2026-01-01T00:00:00Z", "end_reason": "completed"},
                    {"id": "a2", "ended_at": "2026-01-01T00:01:00Z", "end_reason": "completed"},
                ]
            },
        )
        assert fanin_multi_attempt_tasks(dal=dal, run={"id": "r", "board_task_id": "root"}) == []
