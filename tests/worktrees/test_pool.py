from __future__ import annotations

import os
import shutil
import statistics
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from omniagentos.worktrees.git import SubprocessWorktrees
from omniagentos.worktrees.pool import PooledWorktree, WorktreePool


def run_git(cwd: Path, *args: str) -> None:
    # check=True with captured-but-unprinted output made every CI failure here
    # blind ("exit status 128", no cause) — surface git's own words instead.
    proc = subprocess.run(
        ("git", "-C", str(cwd), "-c", "core.hooksPath=", *args),
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        raise AssertionError(
            f"git {' '.join(args)} failed ({proc.returncode}) in {cwd}: "
            f"stderr={proc.stderr.strip()!r} stdout={proc.stdout.strip()!r}"
        )


def git_rev_parse(cwd: str | Path, *args: str) -> str:
    """Read a git rev without touching WorktreePool private helpers."""
    return subprocess.run(
        ("git", "-C", str(cwd), "rev-parse", *args),
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


def assert_label_is_truthful(pw: PooledWorktree) -> None:
    """Whatever acquire() hands back, .branch must be the branch actually
    checked out at .path -- a detached or mislabelled tree silently drops the
    worker's commits at merge time."""
    actual = subprocess.run(
        ("git", "-C", pw.path, "rev-parse", "--abbrev-ref", "HEAD"),
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    assert actual == pw.branch, f"{pw.path} is on {actual!r}, not {pw.branch!r}"


@pytest.fixture
def test_repo(tmp_path: Path) -> tuple[Path, SubprocessWorktrees]:
    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()

    # git init -b main
    run_git(repo_dir, "init", "-b", "main")
    run_git(repo_dir, "config", "user.email", "test@example.com")
    run_git(repo_dir, "config", "user.name", "Test User")

    # write several commits and files to make checkout / worktree add do real work
    for i in range(15):
        test_file = repo_dir / f"file_{i}.txt"
        test_file.write_text("some dummy content " * 1000, encoding="utf-8")
        run_git(repo_dir, "add", f"file_{i}.txt")
        run_git(repo_dir, "commit", "-m", f"commit {i}")

    var_root = tmp_path / "var"
    var_root.mkdir(parents=True, exist_ok=True)

    worktrees = SubprocessWorktrees(namespace="pooltest", var_root=var_root)
    return repo_dir, worktrees


@pytest.fixture
def benchmark_repo(tmp_path: Path) -> tuple[Path, SubprocessWorktrees]:
    repo_dir = tmp_path / "benchmark_repo"
    repo_dir.mkdir()

    # git init -b main
    run_git(repo_dir, "init", "-b", "main")
    run_git(repo_dir, "config", "user.email", "bench@example.com")
    run_git(repo_dir, "config", "user.name", "Bench User")

    # Put files in a subdir
    subdir = repo_dir / "src"
    subdir.mkdir()

    # ~2000 files over ~12 commits; ONE `git add -A` per commit (not one
    # subprocess per file — that used to cost ~24s of setup). Cold worktree-add
    # cost scales with the tree; warm pool acquire is roughly constant, so the
    # 2x speed assertion stays honest while setup stays a couple of seconds.
    total_files = 2000
    commits_count = 12
    files_per_commit = total_files // commits_count

    for c in range(commits_count):
        for f in range(files_per_commit):
            file_idx = c * files_per_commit + f
            file_path = subdir / f"bench_{file_idx}.txt"
            file_path.write_text(f"dummy content for file {file_idx}\n" * 10, encoding="utf-8")
        run_git(repo_dir, "add", "-A")
        run_git(repo_dir, "commit", "-m", f"benchmark commit {c}")

    var_root = tmp_path / "var_bench"
    var_root.mkdir(parents=True, exist_ok=True)

    worktrees = SubprocessWorktrees(namespace="poolbench", var_root=var_root)
    return repo_dir, worktrees


def test_pooled_acquire_is_much_faster_than_cold_creation(
    benchmark_repo: tuple[Path, SubprocessWorktrees],
) -> None:
    repo_dir, worktrees = benchmark_repo

    # Measure cold creation times (>= 5 samples)
    cold_times = []
    for i in range(5):
        start = time.perf_counter()
        worktrees.create(str(repo_dir), "owner", f"cold_{i}", "HEAD")
        cold_times.append(time.perf_counter() - start)

    # Measure pooled acquire times (>= 5 samples) using branch reuse (relay)
    pool = WorktreePool(worktrees, str(repo_dir), "owner", size=1)
    pool.prewarm()

    # Prime the unit branch once untimed so every timed sample is tip-attach
    # (the first acquire would otherwise create the branch with -B).
    primed = pool.acquire("pool_unit_0", "HEAD")
    assert_label_is_truthful(primed)
    pool.release(primed.path, salvage=False)

    pooled_times = []
    for _i in range(5):
        start = time.perf_counter()
        # Reuse the same unit key so that branch_exists is True, testing the warm tip attachment
        pw = pool.acquire("pool_unit_0", "HEAD")
        pooled_times.append(time.perf_counter() - start)
        # Label check after the timer — must not inflate the warm median
        assert_label_is_truthful(pw)
        pool.release(pw.path, salvage=False)

    cold_median = statistics.median(cold_times)
    warm_median = statistics.median(pooled_times)
    ratio = warm_median / cold_median

    assert warm_median <= cold_median * 0.5, (
        f"Pooled acquire median ({warm_median * 1000:.2f} ms) is not at least "
        f"2x faster than cold median ({cold_median * 1000:.2f} ms). "
        f"Ratio: {ratio:.4f}"
    )


def test_untracked_file_does_not_leak_into_next_acquisition(
    test_repo: tuple[Path, SubprocessWorktrees],
) -> None:
    repo_dir, worktrees = test_repo
    pool = WorktreePool(worktrees, str(repo_dir), "owner", size=1)

    # Acquire unit A
    pw_a = pool.acquire("unit_a")
    assert_label_is_truthful(pw_a)
    path_a = Path(pw_a.path)

    # Write untracked file
    untracked_file = path_a / "stale_artifact.txt"
    untracked_file.write_text("stale", encoding="utf-8")

    # Write a stale file inside an untracked dir
    ignored_dir = path_a / "some_ignored_dir"
    ignored_dir.mkdir()
    ignored_file = ignored_dir / "ignored.txt"
    ignored_file.write_text("ignored content", encoding="utf-8")

    assert untracked_file.exists()
    assert ignored_file.exists()

    # Release A
    pool.release(pw_a.path, salvage=False)

    # Acquire unit B
    pw_b = pool.acquire("unit_b")
    assert_label_is_truthful(pw_b)

    # Assert BOTH are gone from the SAME directory
    assert pw_a.path == pw_b.path
    assert not untracked_file.exists()
    assert not ignored_file.exists()
    assert not ignored_dir.exists()


def test_concurrent_acquires_are_disjoint(
    test_repo: tuple[Path, SubprocessWorktrees],
) -> None:
    repo_dir, worktrees = test_repo
    pool = WorktreePool(worktrees, str(repo_dir), "owner", size=4)

    def acquire_one(i: int) -> PooledWorktree:
        return pool.acquire(f"thread_unit_{i}")

    with ThreadPoolExecutor(max_workers=8) as executor:
        results = list(executor.map(acquire_one, range(8)))

    paths = [pw.path for pw in results]
    assert len(paths) == 8
    assert len(set(paths)) == 8

    for pw in results:
        assert os.path.isdir(pw.path)
        assert_label_is_truthful(pw)


def test_exhaustion_falls_back_to_on_demand_creation(
    test_repo: tuple[Path, SubprocessWorktrees],
) -> None:
    repo_dir, worktrees = test_repo
    pool = WorktreePool(worktrees, str(repo_dir), "owner", size=1)

    pw1 = pool.acquire("unit_1")
    pw2 = pool.acquire("unit_2")
    pw3 = pool.acquire("unit_3")

    assert_label_is_truthful(pw1)
    assert_label_is_truthful(pw2)
    assert_label_is_truthful(pw3)

    assert pw1.pooled is True
    assert pw2.pooled is False
    assert pw3.pooled is False

    assert len({pw1.path, pw2.path, pw3.path}) == 3

    expected_path2 = str(worktrees.worktree_path("owner", "unit_2"))
    expected_path3 = str(worktrees.worktree_path("owner", "unit_3"))

    assert pw2.path == expected_path2
    assert pw3.path == expected_path3

    stats = pool.stats()
    assert stats["cold_fallbacks"] == 2


def test_release_salvages_uncommitted_work_and_it_relays_into_the_retry(
    test_repo: tuple[Path, SubprocessWorktrees],
) -> None:
    repo_dir, worktrees = test_repo
    pool = WorktreePool(worktrees, str(repo_dir), "owner", size=1)

    pw = pool.acquire("unit_a")
    assert_label_is_truthful(pw)
    path_a = Path(pw.path)

    # Write a file
    uncommitted_file = path_a / "dirty_file.txt"
    uncommitted_file.write_text("partial progress", encoding="utf-8")

    # Release with default salvage
    released = pool.release(pw.path, salvage=True)
    assert released is True

    # Check unit branch has a commit containing that file
    branch_name = worktrees.branch_name("owner", "unit_a")

    # git show <branch>:<file>
    proc = subprocess.run(
        ("git", "-C", str(repo_dir), "show", f"{branch_name}:dirty_file.txt"),
        capture_output=True,
        text=True,
        check=True,
    )
    assert proc.stdout.strip() == "partial progress"

    # Now acquire unit A again
    pw2 = pool.acquire("unit_a")
    assert_label_is_truthful(pw2)

    # Assert the file is STILL THERE (attached at its tip)
    file_on_retry = Path(pw2.path) / "dirty_file.txt"
    assert file_on_retry.exists()
    assert file_on_retry.read_text(encoding="utf-8") == "partial progress"


def test_pooled_branch_merges_by_pinned_sha(
    test_repo: tuple[Path, SubprocessWorktrees],
) -> None:
    repo_dir, worktrees = test_repo
    pool = WorktreePool(worktrees, str(repo_dir), "owner", size=1)

    pw = pool.acquire("unit_a")
    assert_label_is_truthful(pw)
    path_a = Path(pw.path)

    # Commit a file in the worktree
    new_file = path_a / "merged_file.txt"
    new_file.write_text("merged contents", encoding="utf-8")

    subprocess.run(
        ("git", "-C", str(path_a), "add", "merged_file.txt"),
        check=True,
    )
    subprocess.run(
        (
            "git",
            "-C",
            str(path_a),
            "-c",
            "user.email=test@example.com",
            "-c",
            "user.name=Test",
            "commit",
            "-m",
            "worktree commit",
        ),
        check=True,
    )

    # Capture head_sha
    captured_sha = worktrees.head_sha(pw.path)
    assert captured_sha is not None

    # Write a second file to simulate a tampered/advanced ref on the same branch
    second_file = path_a / "second_file.txt"
    second_file.write_text("tampered contents", encoding="utf-8")

    subprocess.run(
        ("git", "-C", str(path_a), "add", "second_file.txt"),
        check=True,
    )
    subprocess.run(
        (
            "git",
            "-C",
            str(path_a),
            "-c",
            "user.email=test@example.com",
            "-c",
            "user.name=Test",
            "commit",
            "-m",
            "second worktree commit",
        ),
        check=True,
    )

    # Release the unit
    pool.release(pw.path, salvage=False)

    # Merge the branch into the main repo using the captured pinned SHA
    outcome = worktrees.merge_branch(
        str(repo_dir),
        pw.branch,
        "merge msg",
        sha=captured_sha,
    )

    assert outcome.status == "merged"

    # Assert that the file contents in the main checkout match
    main_file = repo_dir / "merged_file.txt"
    assert main_file.exists()
    assert main_file.read_text(encoding="utf-8") == "merged contents"

    # Assert the second file is NOT in the main checkout
    second_main_file = repo_dir / "second_file.txt"
    assert not second_main_file.exists()


def test_release_of_unknown_path_is_a_noop(
    test_repo: tuple[Path, SubprocessWorktrees],
) -> None:
    repo_dir, worktrees = test_repo
    pool = WorktreePool(worktrees, str(repo_dir), "owner", size=1)

    pool.prewarm()
    initial_stats = pool.stats()
    assert initial_stats["free"] == 1

    result = pool.release("/some/foreign/path")
    assert result is False

    stats_after = pool.stats()
    assert stats_after["free"] == 1


def test_disappeared_slot_falls_back(
    test_repo: tuple[Path, SubprocessWorktrees],
) -> None:
    repo_dir, worktrees = test_repo
    pool = WorktreePool(worktrees, str(repo_dir), "owner", size=1)

    paths = pool.prewarm()
    stats_before = pool.stats()
    assert stats_before["free"] == 1

    slot_path = paths[0]
    shutil.rmtree(slot_path)

    # Acquire a unit
    pw = pool.acquire("unit_a")
    assert_label_is_truthful(pw)

    # Since the slot disappeared, it must have fallen back to on-demand creation (pooled=False)
    assert pw.pooled is False
    assert pw.path == str(worktrees.worktree_path("owner", "unit_a"))

    # Stats should show cold_fallback
    stats_after = pool.stats()
    assert stats_after["cold_fallbacks"] == 1


def test_shutdown_removes_worktrees_and_temp_branches(
    test_repo: tuple[Path, SubprocessWorktrees],
) -> None:
    repo_dir, worktrees = test_repo
    pool = WorktreePool(worktrees, str(repo_dir), "owner", size=2)

    paths = pool.prewarm()
    assert len(paths) == 2
    for p in paths:
        assert os.path.isdir(p)

    # Check temp branches exist
    for i in range(2):
        branch = worktrees.branch_name("owner", f"__pool{i}")
        proc = subprocess.run(
            ("git", "-C", str(repo_dir), "rev-parse", "--verify", f"refs/heads/{branch}"),
            capture_output=True,
            text=True,
        )
        assert proc.returncode == 0

    removed_paths = pool.shutdown()
    assert removed_paths == sorted(paths)

    # Worktrees are gone
    for p in removed_paths:
        assert not os.path.exists(p)

    # Branches are gone
    for i in range(2):
        branch = worktrees.branch_name("owner", f"__pool{i}")
        proc = subprocess.run(
            ("git", "-C", str(repo_dir), "rev-parse", "--verify", f"refs/heads/{branch}"),
            capture_output=True,
            text=True,
        )
        assert proc.returncode != 0


def test_new_unit_branch_is_based_on_main_head_not_the_previous_occupant(
    test_repo: tuple[Path, SubprocessWorktrees],
) -> None:
    repo_dir, worktrees = test_repo
    pool = WorktreePool(worktrees, str(repo_dir), "owner", size=1)

    # Acquire unit A
    pw_a = pool.acquire("unit_a")
    assert_label_is_truthful(pw_a)
    path_a = Path(pw_a.path)

    # Commit a file in A's worktree
    a_file = path_a / "a_work.txt"
    a_file.write_text("a work", encoding="utf-8")
    run_git(path_a, "add", "a_work.txt")
    run_git(path_a, "commit", "-m", "commit a")
    a_sha = git_rev_parse(path_a, "HEAD")

    # Get the HEAD sha of the main repo
    main_head_sha = git_rev_parse(repo_dir, "HEAD")

    # Release A
    pool.release(pw_a.path, salvage=False)

    # Acquire unit B in the same slot
    pw_b = pool.acquire("unit_b")
    assert_label_is_truthful(pw_b)

    # Assert pw_b.base_sha == <main repo HEAD sha>
    assert pw_b.base_sha == main_head_sha

    # A's commit is NOT an ancestor of B's HEAD
    proc = subprocess.run(
        ("git", "-C", pw_b.path, "merge-base", "--is-ancestor", a_sha, "HEAD"),
        capture_output=True,
    )
    assert proc.returncode != 0

    # A's file does not exist in B's worktree
    assert not (Path(pw_b.path) / "a_work.txt").exists()


def test_a_released_unit_relays_into_a_different_slot(
    test_repo: tuple[Path, SubprocessWorktrees],
) -> None:
    """Size-2 FIFO: release A, re-acquire A lands on a different slot with tip attached."""
    repo_dir, worktrees = test_repo
    pool = WorktreePool(worktrees, str(repo_dir), "owner", size=2)

    # Acquire unit A (lands slot 0)
    pw_a = pool.acquire("unit_a")
    assert_label_is_truthful(pw_a)
    path_a = Path(pw_a.path)

    # Write AND COMMIT a file on unit A's branch
    relay_file = path_a / "relayed.txt"
    relay_file.write_text("relayed content", encoding="utf-8")
    run_git(path_a, "add", "relayed.txt")
    run_git(path_a, "commit", "-m", "commit for relay")

    first_path = pw_a.path
    pool.release(pw_a.path, salvage=False)

    # After prewarm the free list is [s0, s1]; acquiring A takes s0; releasing A
    # appends it, giving [s1, s0]; so the re-acquire pops s1 — a DIFFERENT
    # directory. That is the case that used to die with CalledProcessError,
    # before release() started detaching the slot from the unit branch.
    pw_a2 = pool.acquire("unit_a")
    assert_label_is_truthful(pw_a2)

    assert pw_a2.path != first_path
    assert pw_a2.pooled is True
    assert (Path(pw_a2.path) / "relayed.txt").read_text(encoding="utf-8") == ("relayed content")


def test_double_acquire_of_a_live_unit_raises_instead_of_returning_a_detached_tree(
    test_repo: tuple[Path, SubprocessWorktrees],
) -> None:
    """Git cannot check one branch out in two worktrees; a unit is only ever
    acquired once at a time. Returning a detached tree here would silently
    drop the worker's commits at merge time, which is why raising is correct.
    """
    repo_dir, worktrees = test_repo
    pool = WorktreePool(worktrees, str(repo_dir), "owner", size=2)

    pw_a = pool.acquire("unit_a")
    assert_label_is_truthful(pw_a)

    # Second acquire of the same live unit must not invent a detached tree
    # with a lying .branch label — it must raise.
    with pytest.raises(subprocess.CalledProcessError):
        pool.acquire("unit_a")


def test_modified_tracked_file_does_not_leak_into_next_acquisition(
    test_repo: tuple[Path, SubprocessWorktrees],
) -> None:
    repo_dir, worktrees = test_repo
    pool = WorktreePool(worktrees, str(repo_dir), "owner", size=1)

    # Acquire unit A
    pw_a = pool.acquire("unit_a")
    assert_label_is_truthful(pw_a)
    path_a = Path(pw_a.path)

    # Overwrite a tracked file with junk
    tracked_file = path_a / "file_0.txt"
    original_content = tracked_file.read_text(encoding="utf-8")
    tracked_file.write_text("junk content", encoding="utf-8")

    # Release A with salvage=False
    pool.release(pw_a.path, salvage=False)

    # Acquire unit B
    pw_b = pool.acquire("unit_b")
    assert_label_is_truthful(pw_b)
    assert pw_b.pooled is True

    # Assert the tracked file is back to its committed content
    assert (Path(pw_b.path) / "file_0.txt").read_text(encoding="utf-8") == original_content


def test_acquire_recovers_a_slot_wedged_mid_merge(
    test_repo: tuple[Path, SubprocessWorktrees],
) -> None:
    repo_dir, worktrees = test_repo
    pool = WorktreePool(worktrees, str(repo_dir), "owner", size=1)

    # Acquire unit A
    pw_a = pool.acquire("unit_a")
    assert_label_is_truthful(pw_a)
    path_a = Path(pw_a.path)

    # Create a conflict base file and commit
    conflict_file = path_a / "conflict.txt"
    conflict_file.write_text("initial base content", encoding="utf-8")
    run_git(path_a, "add", "conflict.txt")
    run_git(path_a, "commit", "-m", "add conflict base file")

    # Create branch_c1 and commit a change to conflict.txt
    run_git(path_a, "checkout", "-b", "branch_c1")
    conflict_file.write_text("conflict version 1", encoding="utf-8")
    run_git(path_a, "add", "conflict.txt")
    run_git(path_a, "commit", "-m", "commit on branch_c1")

    # Go back to unit_a branch, then create branch_c2 and commit a different change
    unit_a_branch = pw_a.branch
    run_git(path_a, "checkout", unit_a_branch)
    run_git(path_a, "checkout", "-b", "branch_c2")
    conflict_file.write_text("conflict version 2", encoding="utf-8")
    run_git(path_a, "add", "conflict.txt")
    run_git(path_a, "commit", "-m", "commit on branch_c2")

    # Merge branch_c1 into branch_c2 to manufacture a REAL conflict merge state
    subprocess.run(
        ("git", "-C", str(path_a), "-c", "core.hooksPath=", "merge", "branch_c1"),
        capture_output=True,
    )

    # Assert MERGE_HEAD exists
    proc = subprocess.run(
        ("git", "-C", str(path_a), "rev-parse", "--verify", "MERGE_HEAD"),
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0

    # Release A with salvage=False
    pool.release(pw_a.path, salvage=False)

    # Acquire B
    pw_b = pool.acquire("unit_b")
    assert_label_is_truthful(pw_b)
    assert pw_b.pooled is True

    # Assert MERGE_HEAD is gone from the slot
    proc_b = subprocess.run(
        ("git", "-C", str(pw_b.path), "rev-parse", "--verify", "MERGE_HEAD"),
        capture_output=True,
        text=True,
    )
    assert proc_b.returncode != 0


def test_shutdown_salvages_dirty_slots_by_default(
    test_repo: tuple[Path, SubprocessWorktrees],
) -> None:
    repo_dir, worktrees = test_repo
    pool = WorktreePool(worktrees, str(repo_dir), "owner", size=1)

    pw = pool.acquire("unit_a")
    assert_label_is_truthful(pw)
    path_a = Path(pw.path)

    # Write an uncommitted file
    uncommitted_file = path_a / "dirty_file_shutdown.txt"
    uncommitted_file.write_text("shutdown progress", encoding="utf-8")

    # Call shutdown() with defaults (salvage=True)
    removed = pool.shutdown()
    assert pw.path in removed

    # Assert the file's content is retrievable from the unit branch
    branch_name = worktrees.branch_name("owner", "unit_a")
    proc = subprocess.run(
        ("git", "-C", str(repo_dir), "show", f"{branch_name}:dirty_file_shutdown.txt"),
        capture_output=True,
        text=True,
        check=True,
    )
    assert proc.stdout.strip() == "shutdown progress"
