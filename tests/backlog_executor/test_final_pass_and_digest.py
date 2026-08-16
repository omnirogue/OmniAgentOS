"""Final merge pass (held tier by 05:00) + morning digest + bookkeeping."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from tests.backlog_executor.conftest import FakeApi, FakeGit, FakeNotify, make_runtime


def _held_result(executor, sandbox, safe_id: str = "todo-a5") -> object:
    clone = sandbox["backlog"] / f"2026-07-25-{safe_id}" / "repo"
    clone.mkdir(parents=True, exist_ok=True)
    return executor.ItemResult(
        pick=executor.Pick(id=safe_id, why="w", brief="b"),
        title="A5: sweep",
        safe_id=safe_id,
        outcome="held-for-morning",
        note="green but held: diff touches test files",
        branch=f"backlog/2026-07-25-{safe_id}",
        clone_dir=str(clone),
        held_reason="diff touches test files (tests/test_a.py)",
    )


def test_held_green_branch_merges_via_integration_reverify(executor, sandbox, monkeypatch):
    batch_wt = sandbox["root"] / "batch-wt"
    batch_wt.mkdir()
    monkeypatch.setattr(
        executor,
        "_resolve_batch_target",
        lambda root: executor.BatchTarget(branch="integration/test", worktree_path=batch_wt),
    )
    result = _held_result(executor, sandbox)
    git = FakeGit().on("rev-parse", "HEAD", result=(0, "mergesha77"))
    rt = make_runtime(
        executor,
        api=FakeApi(),
        git=git,
        suite=lambda cwd, t: (True, "green"),
        notify=FakeNotify(),
        now=datetime(2026, 7, 25, 1, 30),
    )
    executor.final_merge_pass([result], "2026-07-25", executor.Policy(), rt)

    assert result.outcome == "merged-held-tier"
    assert result.merge_sha == "mergesha77"
    merges = git.calls_for("merge")
    # 1: candidate merge in the CLONE (FETCH_HEAD), 2: batch merge with the tag
    assert len(merges) == 2
    assert "FETCH_HEAD" in merges[0][0]
    assert merges[0][1] == Path(result.clone_dir)
    batch_merge = merges[1]
    assert batch_merge[1] == batch_wt
    assert any("[held-tier]" in a for a in batch_merge[0])
    assert any("integration/test" in a for a in batch_merge[0])
    # improvement log got the second, merged-held-tier line
    lines = sandbox["improvement"].read_text(encoding="utf-8").strip().splitlines()
    entry = json.loads(lines[-1])
    assert entry["improver"] == "backlog-executor"
    assert entry["changes"][0]["outcome"] == "merged-held-tier"


def test_red_reverify_leaves_branch_unmerged(executor, sandbox):
    result = _held_result(executor, sandbox)
    clone = Path(result.clone_dir)
    git = FakeGit()
    notify = FakeNotify()

    def suite(cwd, timeout_min):
        return (False, "candidate merge red") if cwd == clone else (True, "green")

    rt = make_runtime(
        executor,
        api=FakeApi(),
        git=git,
        suite=suite,
        notify=notify,
        now=datetime(2026, 7, 25, 1, 30),
    )
    executor.final_merge_pass([result], "2026-07-25", executor.Policy(), rt)

    assert result.outcome == "held-for-morning"  # RED never merges
    live_merges = [c for c in git.calls_for("merge") if c[1] == sandbox["root"]]
    assert live_merges == []
    assert any(kind == "alert" for kind, _ in notify.calls)


def test_deadline_skip_path(executor, sandbox):
    result = _held_result(executor, sandbox)
    git = FakeGit()
    rt = make_runtime(
        executor,
        api=FakeApi(),
        git=git,
        suite=lambda cwd, t: (True, "green"),
        notify=FakeNotify(),
        now=datetime(2026, 7, 25, 4, 50),  # past the 04:45 cutoff
    )
    executor.final_merge_pass([result], "2026-07-25", executor.Policy(), rt)

    assert result.outcome == "held-for-morning"
    assert "skipped" in result.note
    assert git.calls == []  # nothing touched at all


def test_deadline_states(executor):
    policy_hour = 5
    assert executor.merge_pass_deadline_state(datetime(2026, 7, 25, 1, 0), policy_hour) == "open"
    assert executor.merge_pass_deadline_state(datetime(2026, 7, 25, 4, 46), policy_hour) == "skip"
    assert executor.merge_pass_deadline_state(datetime(2026, 7, 25, 5, 1), policy_hour) == "stop"
    # daytime manual runs are past the overnight deadline by definition
    assert executor.merge_pass_deadline_state(datetime(2026, 7, 25, 14, 0), policy_hour) == "stop"


# --- digest -------------------------------------------------------------------


def _mk_result(executor, outcome: str, **kw) -> object:
    base = dict(
        pick=executor.Pick(id="x", why="w", brief="b"),
        title="Item title",
        safe_id="x",
        outcome=outcome,
        note="note",
    )
    base.update(kw)
    return executor.ItemResult(**base)


def test_digest_generation_three_categories(executor, sandbox):
    results = [
        _mk_result(executor, "merged", merge_sha="cleansha123", note="merged cleansha1 (3 files)"),
        _mk_result(
            executor,
            "merged-held-tier",
            merge_sha="heldsha4567",
            held_reason="7 files changed (cap 6)",
        ),
        _mk_result(executor, "held-for-morning", branch="backlog/2026-07-25-y", note="deadline"),
        _mk_result(executor, "failed", branch="backlog/2026-07-25-z", note="suite red"),
        _mk_result(executor, "reverted", merge_sha="deadsha", note="post-merge red"),
    ]
    digest = executor.render_digest("2026-07-25", results)
    lines = digest.splitlines()
    assert lines[1] == "1 merged-clean, 1 merged-held-tier, 3 failed-unmerged"
    assert "## Merged (clean)" in digest
    assert "## Merged (held-tier — eyeball these first)" in digest
    assert "## Failed / unmerged" in digest
    assert "cleansha123"[:10] in digest
    assert "heldsha456" in digest
    # green-but-unmerged held branch carries its exact merge command
    assert f"merge: git -C {sandbox['root']} merge --no-ff backlog/2026-07-25-y" in digest
    assert len(lines) <= 30


def test_digest_caps_at_thirty_lines(executor, sandbox):
    results = [
        _mk_result(executor, "held-for-morning", branch=f"backlog/2026-07-25-i{i}", note="n")
        for i in range(20)
    ]
    digest = executor.render_digest("2026-07-25", results)
    assert len(digest.splitlines()) <= 30
    assert "truncated" in digest


def test_empty_night_digest(executor, sandbox):
    digest = executor.render_digest("2026-07-25", [])
    assert "No items ran tonight." in digest


# --- TODO row flip -------------------------------------------------------------


def test_mark_todo_row_done(executor, sandbox):
    row = "| A5 | Headless stale sweep | GPT-5.6 | ⬜ TODO (Phase 1 tail) |"
    sandbox["todo"].parent.mkdir(parents=True, exist_ok=True)
    sandbox["todo"].write_text(f"# t\n\n{row}\n| B | other | x | ⬜ QUEUED |\n", encoding="utf-8")
    assert executor.mark_todo_row_done(sandbox["todo"], row, "2026-07-25") is True
    text = sandbox["todo"].read_text(encoding="utf-8")
    assert (
        "| A5 | Headless stale sweep | GPT-5.6 | ✅ DONE (backlog-executor 2026-07-25) (Phase 1 tail) |"
        in text
    )
    assert "| B | other | x | ⬜ QUEUED |" in text  # untouched sibling


def test_mark_todo_row_missing_line_is_noop(executor, sandbox):
    sandbox["todo"].parent.mkdir(parents=True, exist_ok=True)
    sandbox["todo"].write_text("# nothing here\n", encoding="utf-8")
    assert executor.mark_todo_row_done(sandbox["todo"], "| gone |", "2026-07-25") is False
