"""Integration-batch merge target: preflight + tier-1 / held-tier / revert.

These tests use real scratch git repos (tmp_path) + a written
``current-batch.json``. FakeApi/FakeNotify still stub the swarm surface;
``run_git`` is the real executor helper so merges land on disk.
"""

from __future__ import annotations

import json
import subprocess
from datetime import datetime
from pathlib import Path

from tests.backlog_executor.conftest import FakeApi, FakeNotify, make_runtime

BATCH_BRANCH = "integration/batch-test"


def _git(args: list[str], cwd: Path) -> str:
    proc = subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        check=True,
    )
    return (proc.stdout + proc.stderr).strip()


def _init_repo(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    _git(["init", "-b", "main"], path)
    _git(["config", "user.email", "backlog-test@example.com"], path)
    _git(["config", "user.name", "Backlog Test"], path)
    (path / "README").write_text("root\n", encoding="utf-8")
    _git(["add", "README"], path)
    _git(["commit", "-m", "init"], path)


def _write_batch_json(root: Path, *, branch: str, worktree: Path, status: str = "open") -> None:
    meta = root / "var" / "integration"
    meta.mkdir(parents=True, exist_ok=True)
    (meta / "current-batch.json").write_text(
        json.dumps(
            {
                "branch": branch,
                "worktree": str(worktree),
                "status": status,
                "opened_at": "2026-07-29T00:00:00Z",
                "pinned_sha": _git(["rev-parse", "HEAD"], worktree if worktree.is_dir() else root),
            }
        ),
        encoding="utf-8",
    )


def _open_batch(root: Path, batch_parent: Path, branch: str = BATCH_BRANCH) -> Path:
    """Linked worktree on ``branch`` + open current-batch.json under root."""
    wt = batch_parent / "batch-wt"
    _git(["worktree", "add", "-b", branch, str(wt)], root)
    _git(["config", "user.email", "backlog-test@example.com"], wt)
    _git(["config", "user.name", "Backlog Test"], wt)
    _write_batch_json(root, branch=branch, worktree=wt, status="open")
    return wt


class _CommittingApi(FakeApi):
    """Dispatch that leaves a real commit on the clone's main (clean tier)."""

    def __init__(self, *, files: list[str] | None = None, **kw) -> None:
        super().__init__(**kw)
        self._files = files or ["omniagentos/feat.py"]

    def dispatch(self, brief: str, working_dir: str) -> str:
        clone = Path(working_dir)
        # Explicit identity: a fresh clone does not inherit the source repo's
        # local user.name/user.email, and GitHub-hosted runners have neither
        # a global identity nor a dotted hostname for git's auto-detect
        # fallback — the commit below fails closed there while silently
        # succeeding wherever an ambient identity exists. Premise-pinning,
        # the same convention as _GIT_IDENTITY_ENV in the gate's own tests.
        subprocess.run(
            ["git", "-C", str(clone), "config", "user.email", "batch-target-test@example.invalid"],
            check=True,
        )
        subprocess.run(
            ["git", "-C", str(clone), "config", "user.name", "Batch Target Test"], check=True
        )
        for rel in self._files:
            p = clone / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(f"# {rel}\n", encoding="utf-8")
        _git(["add", "-A"], clone)
        _git(["commit", "-m", "overnight feat"], clone)
        return super().dispatch(brief, working_dir)


def _pick(executor, pid: str = "todo-a5"):
    return executor.Pick(id=pid, why="small", brief="do the thing", verify_hint="pytest -k thing")


def _green_suite(cwd, timeout_min):
    return True, "all green"


# --- pure resolver ----------------------------------------------------------


def test_preflight_requires_batch_worktree(executor, sandbox, tmp_path):
    """Dirty (or missing) batch worktree => resolve returns None."""
    root = sandbox["root"]
    _init_repo(root)
    wt = _open_batch(root, tmp_path)

    ok = executor._resolve_batch_target(root)
    assert ok is not None
    assert ok.branch == BATCH_BRANCH
    assert ok.worktree_path == wt.resolve() or ok.worktree_path == wt

    (wt / "dirt.txt").write_text("dirty\n", encoding="utf-8")
    assert executor._resolve_batch_target(root) is None

    (wt / "dirt.txt").unlink()
    assert executor._resolve_batch_target(root) is not None

    _write_batch_json(root, branch=BATCH_BRANCH, worktree=wt, status="closed")
    assert executor._resolve_batch_target(root) is None

    # missing file
    (root / "var" / "integration" / "current-batch.json").unlink()
    assert executor._resolve_batch_target(root) is None


def test_no_open_batch_holds_never_merges_root(executor, sandbox, tmp_path):
    """No open batch: hold; never merge into root main."""
    root = sandbox["root"]
    _init_repo(root)
    main_before = _git(["rev-parse", "HEAD"], root)

    # No current-batch.json at all.
    rt = make_runtime(
        executor,
        api=_CommittingApi(),
        git=executor.run_git,
        suite=_green_suite,
        notify=FakeNotify(),
    )
    result = executor.execute_item(
        _pick(executor), "A5: sweep", "2026-07-25", executor.Policy(), rt
    )
    assert result.outcome == "held-for-morning"
    assert result.held_reason == executor.NO_BATCH_HELD_REASON
    assert _git(["rev-parse", "HEAD"], root) == main_before
    # no merge commit on main
    assert _git(["rev-list", "--count", "HEAD"], root) == "1"


def test_protected_main_worktree_root_refused(executor, sandbox, tmp_path):
    """DECISIVE (fail-closed): branch=main (and/or worktree=root) is refused.

    Real-git: root clean and on main. A state file naming branch "main" must
    not resolve — neither when worktree is the scratch root, nor when worktree
    is a separate clean repo also on main. Execute holds; root main HEAD is
    unchanged afterward.
    """
    root = sandbox["root"]
    _init_repo(root)
    main_before = _git(["rev-parse", "HEAD"], root)
    assert _git(["symbolic-ref", "--quiet", "--short", "HEAD"], root) == "main"
    assert _git(["status", "--porcelain"], root) == ""

    # Attack shape from the review: protected branch + root as worktree.
    _write_batch_json(root, branch="main", worktree=root, status="open")
    assert executor._resolve_batch_target(root) is None

    # Root alias (trailing "/.") must also fail closed.
    _write_batch_json(root, branch="main", worktree=root / ".", status="open")
    assert executor._resolve_batch_target(root) is None

    # Protected branch alone (worktree is a different clean repo on main) —
    # load-bearing for PROTECTED_BRANCHES without relying on root-alias.
    other = tmp_path / "other-main-repo"
    _init_repo(other)
    _write_batch_json(root, branch="main", worktree=other, status="open")
    assert executor._resolve_batch_target(root) is None

    # Put the root-as-worktree counterfeit back for the execute_item path.
    _write_batch_json(root, branch="main", worktree=root, status="open")
    rt = make_runtime(
        executor,
        api=_CommittingApi(),
        git=executor.run_git,
        suite=_green_suite,
        notify=FakeNotify(),
    )
    result = executor.execute_item(
        _pick(executor), "A5: sweep", "2026-07-25", executor.Policy(), rt
    )
    assert result.outcome == "held-for-morning"
    assert result.held_reason == executor.NO_BATCH_HELD_REASON
    assert _git(["rev-parse", "HEAD"], root) == main_before
    assert _git(["rev-parse", "main"], root) == main_before
    # still a single root commit (no merge onto main)
    assert _git(["rev-list", "--count", "HEAD"], root) == "1"


def test_held_tier_no_batch_sets_held_reason(executor, sandbox, tmp_path):
    """Held-tier final pass with no open batch records NO_BATCH in held_reason."""
    root = sandbox["root"]
    _init_repo(root)
    main_before = _git(["rev-parse", "HEAD"], root)
    # No current-batch.json — re-verify can still green in the clone.

    branch = "backlog/2026-07-25-todo-a5"
    clone = sandbox["backlog"] / "2026-07-25-todo-a5" / "repo"
    _git(["clone", str(root), str(clone)], tmp_path)
    _git(["config", "user.email", "backlog-test@example.com"], clone)
    _git(["config", "user.name", "Backlog Test"], clone)
    (clone / "held.txt").write_text("held work\n", encoding="utf-8")
    _git(["add", "held.txt"], clone)
    _git(["commit", "-m", "held feat"], clone)
    _git(["fetch", str(clone), f"main:refs/heads/{branch}"], root)

    earlier = "7 files changed (cap 6)"
    result = executor.ItemResult(
        pick=_pick(executor),
        title="A5: held",
        safe_id="todo-a5",
        outcome="held-for-morning",
        note=f"green but held: {earlier}",
        branch=branch,
        clone_dir=str(clone),
        held_reason=earlier,
    )
    rt = make_runtime(
        executor,
        api=FakeApi(),
        git=executor.run_git,
        suite=_green_suite,
        notify=FakeNotify(),
        now=datetime(2026, 7, 25, 1, 30),
    )
    executor.final_merge_pass([result], "2026-07-25", executor.Policy(), rt)

    assert result.outcome == "held-for-morning"
    assert executor.NO_BATCH_HELD_REASON in (result.held_reason or "")
    assert earlier in (result.held_reason or "") or earlier in (result.note or "")
    assert executor.NO_BATCH_HELD_REASON in (result.note or "")
    assert _git(["rev-parse", "main"], root) == main_before
    assert _git(["rev-parse", "HEAD"], root) == main_before


def test_tier1_merge_lands_on_batch_branch(executor, sandbox, tmp_path):
    """DECISIVE: merge parents live on the batch branch; root main unchanged."""
    root = sandbox["root"]
    _init_repo(root)
    main_before = _git(["rev-parse", "HEAD"], root)
    wt = _open_batch(root, tmp_path)
    batch_before = _git(["rev-parse", "HEAD"], wt)

    rt = make_runtime(
        executor,
        api=_CommittingApi(files=["omniagentos/a.py", "docs/c.md"]),
        git=executor.run_git,
        suite=_green_suite,
        notify=FakeNotify(),
    )
    result = executor.execute_item(
        _pick(executor), "A5: sweep", "2026-07-25", executor.Policy(), rt
    )
    assert result.outcome == "merged", result.note
    assert result.merge_sha

    # root main untouched
    assert _git(["rev-parse", "HEAD"], root) == main_before
    assert _git(["rev-parse", "main"], root) == main_before

    # merge is tip of batch branch
    assert _git(["symbolic-ref", "--quiet", "--short", "HEAD"], wt) == BATCH_BRANCH
    assert _git(["rev-parse", "HEAD"], wt) == result.merge_sha

    parents_line = _git(["rev-list", "--parents", "-n", "1", result.merge_sha], wt)
    parts = parents_line.split()
    assert len(parts) == 3, f"expected merge commit with 2 parents, got {parts!r}"
    _merge_sha, parent1, parent2 = parts
    assert parent1 == batch_before
    # both parents are ancestors of the batch branch tip
    for p in (parent1, parent2):
        _git(["merge-base", "--is-ancestor", p, result.merge_sha], wt)


def test_held_tier_pass_targets_batch(executor, sandbox, tmp_path):
    """final_merge_pass merges held branches into the batch worktree, not main."""
    root = sandbox["root"]
    _init_repo(root)
    main_before = _git(["rev-parse", "HEAD"], root)
    wt = _open_batch(root, tmp_path)

    # Simulate a held backlog branch already fetched into the live repo.
    branch = "backlog/2026-07-25-todo-a5"
    clone = sandbox["backlog"] / "2026-07-25-todo-a5" / "repo"
    _git(["clone", str(root), str(clone)], tmp_path)
    _git(["config", "user.email", "backlog-test@example.com"], clone)
    _git(["config", "user.name", "Backlog Test"], clone)
    (clone / "held.txt").write_text("held work\n", encoding="utf-8")
    _git(["add", "held.txt"], clone)
    _git(["commit", "-m", "held feat"], clone)
    _git(["fetch", str(clone), f"main:refs/heads/{branch}"], root)

    result = executor.ItemResult(
        pick=_pick(executor),
        title="A5: held",
        safe_id="todo-a5",
        outcome="held-for-morning",
        note="green but held: 7 files changed (cap 6)",
        branch=branch,
        clone_dir=str(clone),
        held_reason="7 files changed (cap 6)",
    )
    rt = make_runtime(
        executor,
        api=FakeApi(),
        git=executor.run_git,
        suite=_green_suite,
        notify=FakeNotify(),
        now=datetime(2026, 7, 25, 1, 30),
    )
    executor.final_merge_pass([result], "2026-07-25", executor.Policy(), rt)

    assert result.outcome == "merged-held-tier", result.note
    assert _git(["rev-parse", "main"], root) == main_before
    assert _git(["rev-parse", "HEAD"], wt) == result.merge_sha
    assert _git(["symbolic-ref", "--quiet", "--short", "HEAD"], wt) == BATCH_BRANCH
    msg = _git(["log", "-1", "--format=%B", result.merge_sha], wt)
    assert "[held-tier]" in msg
    assert BATCH_BRANCH in msg


def test_post_merge_red_reverts_on_batch(executor, sandbox, tmp_path):
    """Post-merge suite RED reverts on the batch worktree; root main stays put."""
    root = sandbox["root"]
    _init_repo(root)
    main_before = _git(["rev-parse", "HEAD"], root)
    wt = _open_batch(root, tmp_path)
    batch_before = _git(["rev-parse", "HEAD"], wt)

    def suite(cwd, timeout_min):
        # green in clone gate, RED once the suite runs in the batch worktree
        return (False, "batch suite red") if Path(cwd) == wt else (True, "green")

    rt = make_runtime(
        executor,
        api=_CommittingApi(files=["omniagentos/a.py"]),
        git=executor.run_git,
        suite=suite,
        notify=FakeNotify(),
    )
    result = executor.execute_item(
        _pick(executor), "A5: sweep", "2026-07-25", executor.Policy(), rt
    )
    assert result.outcome == "reverted", result.note
    assert result.stop_night is True
    assert _git(["rev-parse", "main"], root) == main_before
    # batch tip is a revert (or back to pre-merge tree content); main still alone
    batch_head = _git(["rev-parse", "HEAD"], wt)
    assert batch_head != batch_before  # merge + revert advanced history
    # tree matches pre-merge batch tip (revert -m 1 of the merge)
    assert _git(["rev-parse", f"{batch_head}^{{tree}}"], wt) == _git(
        ["rev-parse", f"{batch_before}^{{tree}}"], wt
    )
