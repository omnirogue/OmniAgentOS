"""Merge gate, two-tier classification, auto-revert rail, dry-run."""

from __future__ import annotations

from tests.backlog_executor.conftest import FakeApi, FakeGit, FakeNotify, make_runtime


def _pick(executor, pid: str = "todo-a5"):
    return executor.Pick(id=pid, why="small", brief="do the thing", verify_hint="pytest -k thing")


def _green_suite(cwd, timeout_min):
    return True, "all green"


def _red_suite(cwd, timeout_min):
    return False, "1 failed"


# --- two-tier classification (pure) ----------------------------------------


def test_clean_tier_single_attempt_three_files_merges(executor):
    tier, reason = executor.classify_merge_tier(
        risk_classes=["none", "none"],
        max_attempts_per_task=1,
        changed_files=["omniagentos/a.py", "omniagentos/b.py", "docs/c.md"],
        policy=executor.Policy(),
    )
    assert (tier, reason) == ("merge", None)


def test_test_touching_diff_is_held_despite_green(executor):
    tier, reason = executor.classify_merge_tier(
        risk_classes=["none"],
        max_attempts_per_task=1,
        changed_files=["omniagentos/a.py", "tests/foo/test_a.py"],
        policy=executor.Policy(),
    )
    assert tier == "hold"
    assert "test files" in reason


def test_seven_file_diff_is_held(executor):
    tier, reason = executor.classify_merge_tier(
        risk_classes=["none"],
        max_attempts_per_task=1,
        changed_files=[f"omniagentos/m{i}.py" for i in range(7)],
        policy=executor.Policy(),
    )
    assert tier == "hold"
    assert "7 files" in reason


def test_multi_attempt_and_risky_items_are_held(executor):
    tier, _ = executor.classify_merge_tier(
        risk_classes=["none"],
        max_attempts_per_task=2,
        changed_files=["a.py"],
        policy=executor.Policy(),
    )
    assert tier == "hold"
    tier, reason = executor.classify_merge_tier(
        risk_classes=["none", "external"],
        max_attempts_per_task=1,
        changed_files=["a.py"],
        policy=executor.Policy(),
    )
    assert tier == "hold"
    assert "external" in reason


# --- merge gate red path: branch left, NEVER merged ------------------------


def test_red_gate_never_merges_and_alerts(executor, sandbox):
    git = FakeGit().on("rev-parse", "main", result=(0, "newsha111"))
    notify = FakeNotify()
    rt = make_runtime(executor, api=FakeApi(), git=git, suite=_red_suite, notify=notify)
    result = executor.execute_item(
        _pick(executor), "A5: sweep", "2026-07-25", executor.Policy(), rt
    )
    assert result.outcome == "failed"
    assert "merge gate RED" in result.note
    assert git.calls_for("merge") == []  # never merged
    assert git.calls_for("fetch"), "branch must be left for review"
    assert result.branch.startswith("backlog/2026-07-25-")
    assert any(kind == "alert" for kind, _ in notify.calls)


def test_failed_run_status_never_merges(executor, sandbox):
    git = FakeGit().on("rev-parse", "main", result=(0, "newsha111"))
    notify = FakeNotify()
    rt = make_runtime(
        executor, api=FakeApi(status="failed"), git=git, suite=_green_suite, notify=notify
    )
    result = executor.execute_item(_pick(executor), "A5", "2026-07-25", executor.Policy(), rt)
    assert result.outcome == "failed"
    assert git.calls_for("merge") == []
    assert any(kind == "alert" for kind, _ in notify.calls)


# --- green clean tier merges; held tier holds -------------------------------


def test_green_clean_tier_merges_no_ff(executor, sandbox, monkeypatch):
    batch_wt = sandbox["root"] / "batch-wt"
    batch_wt.mkdir()
    monkeypatch.setattr(
        executor,
        "_resolve_batch_target",
        lambda root: executor.BatchTarget(branch="integration/test", worktree_path=batch_wt),
    )
    git = (
        FakeGit()
        .on("rev-parse", "main", result=(0, "newsha111"))
        .on("diff", result=(0, "omniagentos/a.py\nomniagentos/b.py\ndocs/c.md"))
    )
    rt = make_runtime(executor, api=FakeApi(), git=git, suite=_green_suite, notify=FakeNotify())
    result = executor.execute_item(_pick(executor), "A5", "2026-07-25", executor.Policy(), rt)
    assert result.outcome == "merged"
    merges = git.calls_for("merge")
    assert len(merges) == 1
    assert "--no-ff" in merges[0][0]
    assert merges[0][1] == batch_wt  # merged into the open batch worktree, never root main
    assert any("integration/test" in a for a in merges[0][0])


def test_green_but_test_touching_diff_is_held_with_hold_commit(executor, sandbox):
    git = (
        FakeGit()
        .on("rev-parse", "main", result=(0, "newsha111"))
        .on("diff", result=(0, "omniagentos/a.py\ntests/test_a.py"))
    )
    rt = make_runtime(executor, api=FakeApi(), git=git, suite=_green_suite, notify=FakeNotify())
    result = executor.execute_item(_pick(executor), "A5", "2026-07-25", executor.Policy(), rt)
    assert result.outcome == "held-for-morning"
    assert result.held_reason
    assert git.calls_for("merge") == []
    hold_commits = [
        args for args, _ in git.calls_for("commit") if any(a.startswith("HOLD:") for a in args)
    ]
    assert hold_commits, "held branch must carry the HOLD: note commit"


# --- auto-revert rail --------------------------------------------------------


def test_post_merge_red_auto_reverts_and_stops_night(executor, sandbox, monkeypatch):
    batch_wt = sandbox["root"] / "batch-wt"
    batch_wt.mkdir()
    monkeypatch.setattr(
        executor,
        "_resolve_batch_target",
        lambda root: executor.BatchTarget(branch="integration/test", worktree_path=batch_wt),
    )

    def suite(cwd, timeout_min):
        # green in the clone gate, RED in the batch worktree after the merge
        return (False, "batch broke") if cwd == batch_wt else (True, "green")

    git = (
        FakeGit()
        .on("rev-parse", "main", result=(0, "newsha111"))
        .on("rev-parse", "HEAD", result=(0, "mergesha99"))
        .on("diff", result=(0, "omniagentos/a.py"))
    )
    notify = FakeNotify()
    rt = make_runtime(executor, api=FakeApi(), git=git, suite=suite, notify=notify)
    result = executor.execute_item(_pick(executor), "A5", "2026-07-25", executor.Policy(), rt)
    assert result.outcome == "reverted"
    assert result.stop_night is True
    reverts = git.calls_for("revert")
    assert len(reverts) == 1
    assert reverts[0][0][:3] == ["revert", "-m", "1"]
    assert reverts[0][1] == batch_wt
    assert any(kind == "alert" for kind, _ in notify.calls)


# --- dry-run: zero dispatches -------------------------------------------------


def test_dry_run_dispatches_nothing(executor, sandbox, monkeypatch):
    sandbox["prompt"].write_text(
        "criteria\n\n```yaml\npolicy:\n  max_items: 3\n```\n", encoding="utf-8"
    )
    sandbox["todo"].parent.mkdir(parents=True, exist_ok=True)
    sandbox["todo"].write_text("| A5 | thing | x | ⬜ TODO |\n", encoding="utf-8")

    picks = [executor.Pick(id="todo-a5", why="w", brief="b")]
    monkeypatch.setattr(executor, "select_picks", lambda *a, **k: picks)
    notify = FakeNotify()
    monkeypatch.setattr(executor, "notify", notify)

    api = FakeApi()
    rt = make_runtime(executor, api=api, git=FakeGit(), suite=_green_suite, notify=notify)
    results = executor.run_night(rt=rt, dry_run=True)

    assert results == []
    assert api.dispatched == []  # ZERO dispatch calls
    digest = (
        sandbox["backlog"]
        / f"digest-{__import__('datetime').datetime.now().strftime('%Y-%m-%d')}.md"
    )
    assert digest.is_file()
    assert "DRY-RUN" in digest.read_text(encoding="utf-8")
    assert any(kind == "info" for kind, _ in notify.calls)
