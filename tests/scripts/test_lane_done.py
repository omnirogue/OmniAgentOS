"""Red-first tests for lane_done.py: the machine-checked lane admissibility guard.

Each test proves that violations are caught; green tests prove a valid lane passes.

Every scenario below reproduces a specific probe from the pre-merge review that found
lane_done.py FAILED OPEN in exactly the class of case it exists to kill: a non-empty
`git commit --allow-empty` and a merge-only branch both used to certify clean, any
ancestor of HEAD (including main's own tip) counted as a "claim SHA", claim-SHA /
owned-path / DB checks silently no-op'd when their CLI flags were omitted, a git ERROR
(corrupted index) read as an empty-but-successful result, stale local `main` produced a
false GREEN, and a detached HEAD certified admissible with branch:"HEAD".
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from scripts.lane_done import fnmatch_patterns, owned_paths_outside_required, path_matches_pattern

REPO_ROOT = Path(__file__).resolve().parents[2]
LANE_DONE_SCRIPT = REPO_ROOT / "scripts" / "lane_done.py"
SERVING_ROOT = "/Users/youruser/OmniAgentOS"


def _git(repo: Path, *args: str, check: bool = True) -> str:
    """Run git command, return stdout stripped."""
    result = subprocess.run(
        ["git", *args],
        cwd=repo,
        capture_output=True,
        text=True,
        check=False,
    )
    if check and result.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {result.stderr}\n{result.stdout}")
    return result.stdout.strip()


def _write_done_json(
    repo: Path,
    *,
    head_sha: str,
    owned_paths: list[str],
    **extra_fields: Any,
) -> Path:
    """Write <repo>/DONE.json — the lane's own (untracked) self-report manifest that
    lane_done.py verify now mandatorily reads for claim SHAs and owned paths (see
    lane_done.py's module docstring for the field-name convention). `**extra_fields`
    covers the optional, self-describing `base` / `lane` / `db_path` fields."""
    manifest_path = repo / "DONE.json"
    manifest_path.write_text(
        json.dumps({"head_sha": head_sha, "owned_paths": owned_paths, **extra_fields}, indent=2)
    )
    return manifest_path


def _run_lane_done(
    worktree_path: Path,
    *,
    base: str = "main",
    claim_shas: list[str] | None = None,
    owned_paths: list[str] | None = None,
    require_owned_paths: list[str] | None = None,
    db_path: str | None = None,
    serving_root: str = SERVING_ROOT,
    extra_env: dict[str, str] | None = None,
) -> tuple[int, dict[str, Any], list[str]]:
    """Run lane_done.py verify, return (exit_code, receipt_dict, stderr_lines)."""

    cmd = [
        sys.executable,
        str(LANE_DONE_SCRIPT),
        "verify",
        "--worktree",
        str(worktree_path),
        "--base",
        base,
        "--serving-root",
        serving_root,
    ]

    if claim_shas:
        for sha in claim_shas:
            cmd.extend(["--claim-sha", sha])

    if owned_paths:
        for path in owned_paths:
            cmd.extend(["--owned-path", path])

    if require_owned_paths:
        for path in require_owned_paths:
            cmd.extend(["--require-owned-path", path])

    if db_path:
        cmd.extend(["--db-path", db_path])

    # Isolate OMNIAGENTOS_DB so no test can silently inherit whatever the CALLING
    # session happens to have set (the CLAUDE.md rule "scratch OMNIAGENTOS_DB" governs
    # THIS process, not the subprocess under test) — a test that wants the now-mandatory
    # DB check to pass must say so explicitly via db_path. This dict is genuinely passed
    # to subprocess.run below (env=env) — previously it was built and then silently
    # discarded, which is a fixed bug, not incidental.
    env = os.environ.copy()
    env["OMNIAGENTOS_DB"] = db_path or ""
    if extra_env:
        env.update(extra_env)

    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )

    stderr_lines = [line for line in result.stderr.strip().split("\n") if line]

    # Parse JSON receipt from stdout
    try:
        receipt = json.loads(result.stdout)
    except json.JSONDecodeError:
        receipt = {"error": "failed to parse receipt", "stdout": result.stdout}

    return result.returncode, receipt, stderr_lines


@pytest.fixture
def valid_lane(tmp_path: Path) -> Path:
    """Create a valid lane: clean tree, non-empty commits, proper merge base, and a
    DONE.json manifest matching its own commit (claim SHAs / owned paths are now
    mandatorily sourced from this file, not merely optional CLI flags)."""

    repo = tmp_path / "valid_repo"
    repo.mkdir()

    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.name", "Test")
    _git(repo, "config", "user.email", "test@example.com")

    # Create base commit
    (repo / "base.txt").write_text("base\n")
    _git(repo, "add", "base.txt")
    _git(repo, "commit", "-m", "base")

    # Create a feature branch with a commit
    _git(repo, "checkout", "-b", "feat/test-lane")
    (repo / "feature.txt").write_text("feature\n")
    _git(repo, "add", "feature.txt")
    _git(repo, "commit", "-m", "add feature")

    head_sha = _git(repo, "rev-parse", "HEAD")
    _write_done_json(repo, head_sha=head_sha, owned_paths=["feature.txt"])

    _git(repo, "checkout", "main")

    return repo


@pytest.fixture
def orphan_worktree(tmp_path: Path) -> Path:
    """A single repo where the feature branch is a REAL git-history orphan
    (`git checkout --orphan`) — no merge base with main at all. Distinct from merely
    passing a nonexistent ref as --base (a different, already-covered failure mode)."""

    repo = tmp_path / "orphan_repo"
    repo.mkdir()

    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.name", "Test")
    _git(repo, "config", "user.email", "test@example.com")

    (repo / "base.txt").write_text("base\n")
    _git(repo, "add", "base.txt")
    _git(repo, "commit", "-m", "base")

    _git(repo, "checkout", "--orphan", "feat/orphan")
    (repo / "orphan.txt").write_text("orphan\n")
    _git(repo, "add", "orphan.txt")
    _git(repo, "commit", "-m", "orphan work")

    head_sha = _git(repo, "rev-parse", "HEAD")
    _write_done_json(repo, head_sha=head_sha, owned_paths=["orphan.txt"])

    return repo


@pytest.fixture
def deep_base_lane(tmp_path: Path) -> tuple[Path, str, str]:
    """A lane whose base has TWO commits — a root, then a later tip — so "main's tip"
    and "the root commit" are distinct SHAs. Both are ancestors of HEAD; neither is a
    member of base..HEAD. Returns (repo, root_sha, base_tip_sha)."""

    repo = tmp_path / "deep_base_repo"
    repo.mkdir()

    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.name", "Test")
    _git(repo, "config", "user.email", "test@example.com")

    (repo / "root.txt").write_text("root\n")
    _git(repo, "add", "root.txt")
    _git(repo, "commit", "-m", "root commit")
    root_sha = _git(repo, "rev-parse", "HEAD")

    (repo / "base.txt").write_text("base\n")
    _git(repo, "add", "base.txt")
    _git(repo, "commit", "-m", "base tip commit")
    base_tip_sha = _git(repo, "rev-parse", "HEAD")

    _git(repo, "checkout", "-b", "feat/deep")
    (repo / "feature.txt").write_text("feature\n")
    _git(repo, "add", "feature.txt")
    _git(repo, "commit", "-m", "own work")
    head_sha = _git(repo, "rev-parse", "HEAD")

    _write_done_json(repo, head_sha=head_sha, owned_paths=["feature.txt"])

    return repo, root_sha, base_tip_sha


@pytest.fixture
def stale_base_lane(tmp_path: Path) -> Path:
    """A local clone whose `main` has fallen behind its fetched `origin/main` — the
    false-GREEN scenario base freshness must catch."""

    remote = tmp_path / "remote"
    remote.mkdir()
    _git(remote, "init", "-b", "main")
    _git(remote, "config", "user.name", "Test")
    _git(remote, "config", "user.email", "test@example.com")
    (remote / "base.txt").write_text("base\n")
    _git(remote, "add", "base.txt")
    _git(remote, "commit", "-m", "base")

    local = tmp_path / "local"
    _git(tmp_path, "clone", str(remote), str(local))
    _git(local, "config", "user.name", "Test")
    _git(local, "config", "user.email", "test@example.com")

    _git(local, "checkout", "-b", "feat/stale")
    (local / "feature.txt").write_text("feature\n")
    _git(local, "add", "feature.txt")
    _git(local, "commit", "-m", "feature")
    head_sha = _git(local, "rev-parse", "HEAD")
    _write_done_json(local, head_sha=head_sha, owned_paths=["feature.txt"])

    # The remote advances AFTER the lane branched off it.
    (remote / "more.txt").write_text("more\n")
    _git(remote, "add", "more.txt")
    _git(remote, "commit", "-m", "remote moves on")

    _git(local, "fetch", "origin")

    return local


@pytest.fixture
def diverged_base_lane(tmp_path: Path) -> Path:
    """A local clone whose `main` and its fetched `origin/main` have DIVERGED — neither
    is an ancestor of the other (e.g. a local-only commit plus an independent remote
    advance) — round-2 finding 22's scenario, distinct from stale_base_lane's pure
    "purely behind" case."""

    remote = tmp_path / "d_remote"
    remote.mkdir()
    _git(remote, "init", "-b", "main")
    _git(remote, "config", "user.name", "Test")
    _git(remote, "config", "user.email", "test@example.com")
    (remote / "base.txt").write_text("base\n")
    _git(remote, "add", "base.txt")
    _git(remote, "commit", "-m", "base")

    local = tmp_path / "d_local"
    _git(tmp_path, "clone", str(remote), str(local))
    _git(local, "config", "user.name", "Test")
    _git(local, "config", "user.email", "test@example.com")

    _git(local, "checkout", "-b", "feat/diverge")
    (local / "feature.txt").write_text("feature\n")
    _git(local, "add", "feature.txt")
    _git(local, "commit", "-m", "feature")
    head_sha = _git(local, "rev-parse", "HEAD")
    _write_done_json(local, head_sha=head_sha, owned_paths=["feature.txt"])

    # Diverge: local main gets its OWN commit, while remote main independently advances.
    _git(local, "checkout", "main")
    _git(local, "commit", "--allow-empty", "-m", "local-only divergent commit")
    (remote / "more.txt").write_text("more\n")
    _git(remote, "add", "more.txt")
    _git(remote, "commit", "-m", "remote moves on independently")
    _git(local, "fetch", "origin")
    _git(local, "checkout", "feat/diverge")

    return local


def test_lane_done_refuses_zero_commits(valid_lane: Path) -> None:
    """RED: lane with zero commits should be refused."""

    # Check out main (no commits ahead)
    _git(valid_lane, "checkout", "main")

    exit_code, receipt, errors = _run_lane_done(valid_lane)

    assert exit_code != 0, "Expected non-zero exit for zero commits"
    assert receipt["verdict"] == "refused"
    assert receipt["reason"] == "empty_lane"
    assert any("no commits" in str(e).lower() for e in errors), (
        f"Expected 'no commits' error, got {errors}"
    )


def test_lane_done_refuses_dirty_tree(valid_lane: Path) -> None:
    """RED: lane with uncommitted changes should be refused. DONE.json itself (also
    untracked) must NOT be what trips this — only genuine uncommitted lane content."""

    _git(valid_lane, "checkout", "feat/test-lane")

    # Add uncommitted file
    (valid_lane / "uncommitted.txt").write_text("uncommitted\n")

    exit_code, receipt, errors = _run_lane_done(valid_lane)

    assert exit_code != 0, "Expected non-zero exit for dirty tree"
    assert receipt["verdict"] == "refused"
    assert receipt["reason"] == "tree_dirty"
    assert any("not clean" in str(e).lower() for e in errors), (
        f"Expected 'not clean' error, got {errors}"
    )


def test_lane_done_passes_with_only_done_json_untracked(valid_lane: Path, tmp_path: Path) -> None:
    """GREEN: DONE.json being untracked must not, by itself, ever read as a dirty tree
    — it is the lane's own status file, not lane content."""

    _git(valid_lane, "checkout", "feat/test-lane")
    status = _git(valid_lane, "status", "--porcelain")
    assert status == "?? DONE.json", f"fixture invariant broken, got: {status!r}"

    scratch_db = tmp_path / "scratch.sqlite3"
    exit_code, receipt, errors = _run_lane_done(valid_lane, db_path=str(scratch_db))

    assert exit_code == 0, f"expected DONE.json-only untracked state to be clean, got: {errors}"
    assert receipt["checks"]["tree_clean"] is True


def test_lane_done_refuses_fabricated_claim_sha(valid_lane: Path) -> None:
    """RED: fabricated (non-existent) claim SHA should be refused."""

    _git(valid_lane, "checkout", "feat/test-lane")

    fake_sha = "0000000000000000000000000000000000000000"

    exit_code, receipt, errors = _run_lane_done(
        valid_lane,
        claim_shas=[fake_sha],
    )

    assert exit_code != 0, "Expected non-zero exit for fabricated claim SHA"
    assert receipt["verdict"] == "refused"
    assert receipt["reason"] == "claim_sha_unresolvable"
    # Accept either "does not resolve" or "not an ancestor" — both refuse the fabricated SHA
    assert any(
        ("does not resolve" in str(e).lower() or "not an ancestor" in str(e).lower())
        for e in errors
    ), f"Expected claim SHA error, got {errors}"


def test_lane_done_refuses_borrowed_claim_sha_main_tip(
    deep_base_lane: tuple[Path, str, str],
) -> None:
    """RED (major #4, replaces the old decorative/zero-assert test): ANY ancestor of
    HEAD used to pass as a claim SHA, including main's own tip — proving nothing about
    this lane's own work, since main's tip is (trivially) also an ancestor of itself.
    Claim SHAs must be members of base..HEAD, not merely ancestors of HEAD."""

    repo, _root_sha, base_tip_sha = deep_base_lane
    _git(repo, "checkout", "feat/deep")

    exit_code, receipt, errors = _run_lane_done(
        repo,
        base="main",
        claim_shas=[base_tip_sha],
    )

    assert exit_code != 0, "Expected refusal for a claim SHA borrowed from base's own tip"
    assert receipt["verdict"] == "refused"
    assert receipt["reason"] == "claim_sha_not_in_range"
    assert any("not an ancestor" in str(e).lower() for e in errors), errors


def test_lane_done_refuses_borrowed_claim_sha_root_commit(
    deep_base_lane: tuple[Path, str, str],
) -> None:
    """RED (major #4): the root commit is also an ancestor of HEAD (transitively, via
    main's tip) but is even further from being this lane's own work than main's tip
    is — the review named this explicitly as a second case that used to pass."""

    repo, root_sha, _base_tip_sha = deep_base_lane
    _git(repo, "checkout", "feat/deep")

    exit_code, receipt, errors = _run_lane_done(
        repo,
        base="main",
        claim_shas=[root_sha],
    )

    assert exit_code != 0, "Expected refusal for a claim SHA borrowed from the root commit"
    assert receipt["verdict"] == "refused"
    assert receipt["reason"] == "claim_sha_not_in_range"
    assert any("not an ancestor" in str(e).lower() for e in errors), errors


def test_lane_done_passes_claim_sha_that_is_genuinely_the_lanes_own(
    deep_base_lane: tuple[Path, str, str], tmp_path: Path
) -> None:
    """GREEN companion to the two borrowed-SHA tests: a claim SHA that IS the lane's
    own commit (not borrowed from base) must still pass, proving the fix didn't just
    start refusing everything."""

    repo, _root_sha, _base_tip_sha = deep_base_lane
    _git(repo, "checkout", "feat/deep")
    own_sha = _git(repo, "rev-parse", "HEAD")
    scratch_db = tmp_path / "scratch.sqlite3"

    exit_code, receipt, errors = _run_lane_done(
        repo,
        base="main",
        claim_shas=[own_sha],
        db_path=str(scratch_db),
    )

    assert exit_code == 0, f"Expected the lane's own commit to pass as a claim SHA: {errors}"
    assert receipt["verdict"] == "admissible"


def test_lane_done_refuses_nonexistent_base_ref(valid_lane: Path) -> None:
    """RED: an unresolvable --base is refused before any merge-base/diff logic runs."""

    _git(valid_lane, "checkout", "feat/test-lane")

    exit_code, receipt, errors = _run_lane_done(valid_lane, base="nonexistent-ref")

    assert exit_code != 0
    assert receipt["verdict"] == "refused"
    assert receipt["reason"] == "base_unresolved"


def test_lane_done_refuses_no_merge_base(orphan_worktree: Path) -> None:
    """RED (major #8, replaces the old mislabeled test that actually exercised a
    nonexistent --base ref instead): a REAL orphan branch (git checkout --orphan) has
    no merge base with main whatsoever and must be refused as such."""

    exit_code, receipt, errors = _run_lane_done(orphan_worktree, base="main")

    assert exit_code != 0, "Expected non-zero exit for a true history orphan"
    assert receipt["verdict"] == "refused"
    assert receipt["reason"] == "no_merge_base"
    assert any("no merge base" in str(e).lower() for e in errors), errors


def test_lane_done_refuses_owned_path_violation(valid_lane: Path, tmp_path: Path) -> None:
    """RED: files outside ownership boundaries should be refused."""

    _git(valid_lane, "checkout", "feat/test-lane")

    # Modify feature.txt so it's part of changed files
    (valid_lane / "feature.txt").write_text("modified\n")
    (valid_lane / "outside.txt").write_text("not owned\n")
    _git(valid_lane, "add", "feature.txt", "outside.txt")
    _git(valid_lane, "commit", "-m", "modify and add outside")

    # Only allow "feature.txt"
    exit_code, receipt, errors = _run_lane_done(
        valid_lane,
        owned_paths=["feature.txt"],
    )

    assert exit_code != 0, "Expected non-zero exit for owned-path violation"
    assert receipt["verdict"] == "refused"
    assert receipt["reason"] == "owned_path_violation"
    assert any("outside ownership" in str(e).lower() for e in errors), (
        f"Expected 'outside ownership' error, got {errors}"
    )


def test_lane_done_refuses_owned_paths_not_subset_of_required(
    valid_lane: Path, tmp_path: Path
) -> None:
    """RED (round-2 finding 20): --require-owned-path is a coordinator-supplied
    ENFORCEMENT ceiling, distinct from self-declared owned_paths (CONFORMANCE only). A
    lane that self-declares a pattern broader than the coordinator's ceiling must be
    refused, even though its self-declared pattern alone would otherwise satisfy the
    ordinary per-file owned_paths check."""

    _git(valid_lane, "checkout", "feat/test-lane")
    scratch_db = tmp_path / "scratch.sqlite3"

    exit_code, receipt, errors = _run_lane_done(
        valid_lane,
        db_path=str(scratch_db),
        owned_paths=["*"],  # self-declared: broader than what the coordinator allows
        require_owned_paths=["feature.*"],
    )

    assert exit_code != 0, "self-declared '*' must not escape a narrower --require-owned-path"
    assert receipt["verdict"] == "refused"
    assert receipt["reason"] == "owned_paths_not_subset_of_required"


def test_lane_done_passes_owned_paths_within_required(valid_lane: Path, tmp_path: Path) -> None:
    """GREEN companion: a self-declared owned_paths pattern that IS covered by
    --require-owned-path passes normally — the enforcement ceiling doesn't block a
    lane that stays within it."""

    _git(valid_lane, "checkout", "feat/test-lane")
    scratch_db = tmp_path / "scratch.sqlite3"

    exit_code, receipt, errors = _run_lane_done(
        valid_lane,
        db_path=str(scratch_db),
        require_owned_paths=["feature.*"],
    )

    assert exit_code == 0, (
        f"Expected pass when owned_paths is within the required ceiling: {errors}"
    )
    assert receipt["verdict"] == "admissible"
    assert receipt["checks"]["owned_paths_within_required"] is True


def test_lane_done_refuses_live_db_path(valid_lane: Path, tmp_path: Path) -> None:
    """RED: DB path pointing at the exact legacy live-DB location must still be
    refused (now via the broadened "anywhere under serving-root's var/" rule)."""

    _git(valid_lane, "checkout", "feat/test-lane")

    # Point to the (legacy, exact) live DB location.
    live_db = Path(SERVING_ROOT) / "var" / "runtime" / "state.sqlite3"

    exit_code, receipt, errors = _run_lane_done(
        valid_lane,
        db_path=str(live_db),
    )

    assert exit_code != 0, "Expected non-zero exit for live DB path"
    assert receipt["verdict"] == "refused"
    assert receipt["reason"] == "db_path_under_serving_root"
    assert any("serving root" in str(e).lower() and "var" in str(e).lower() for e in errors), (
        f"Expected a serving-root var/ error, got {errors}"
    )


def test_lane_done_refuses_db_path_anywhere_under_serving_var(valid_lane: Path) -> None:
    """RED (minor: broadened var/ check): a DB path elsewhere under serving-root's
    var/ — NOT the one specific legacy file — must also be refused. The old code only
    matched the single hardcoded var/runtime/state.sqlite3 path exactly."""

    _git(valid_lane, "checkout", "feat/test-lane")

    other_db_under_var = Path(SERVING_ROOT) / "var" / "some" / "other" / "place.sqlite3"

    exit_code, receipt, errors = _run_lane_done(
        valid_lane,
        db_path=str(other_db_under_var),
    )

    assert exit_code != 0
    assert receipt["verdict"] == "refused"
    assert receipt["reason"] == "db_path_under_serving_root"


def test_lane_done_refuses_db_env_unset(valid_lane: Path) -> None:
    """RED (blocker #3): OMNIAGENTOS_DB unset/empty and no --db-path must now REFUSE,
    not silently skip the check — a bare verify used to check only 4 of 7."""

    _git(valid_lane, "checkout", "feat/test-lane")

    exit_code, receipt, errors = _run_lane_done(valid_lane)  # no db_path -> env forced to ""

    assert exit_code != 0, "Expected non-zero exit for an unset OMNIAGENTOS_DB"
    assert receipt["verdict"] == "refused"
    assert receipt["reason"] == "db_env_unset"


def test_lane_done_bare_verify_checks_all_mandatory_checks(
    valid_lane: Path, tmp_path: Path
) -> None:
    """GREEN (blocker #3): a bare `verify --worktree .` with NO --claim-sha/--owned-path
    flags must still run the claim-SHA and owned-path checks (sourced from DONE.json)
    and pass — mandatory does not mean "always fails without flags", it means "always
    runs, flags or not"."""

    _git(valid_lane, "checkout", "feat/test-lane")
    scratch_db = tmp_path / "scratch.sqlite3"

    exit_code, receipt, errors = _run_lane_done(valid_lane, db_path=str(scratch_db))

    assert exit_code == 0, f"Expected a bare verify (manifest-only) to pass: {errors}"
    assert receipt["verdict"] == "admissible"
    checks = receipt["checks"]
    assert any(k.startswith("claim_sha_") for k in checks), "claim-SHA check did not run"
    assert checks["owned_paths"] is True
    assert checks["db_path_not_live"] is True


@pytest.mark.skipif(
    not Path(SERVING_ROOT).is_dir(),
    reason="the estate serving checkout is not present on this host",
)
def test_lane_done_refuses_serving_root_as_worktree(valid_lane: Path) -> None:
    """RED: using serving root as worktree should be refused."""

    # This is an estate integration check. The hermetic override test below
    # proves the same refusal on hosts (including GitHub runners) where the
    # operator's absolute serving path does not exist.
    exit_code, receipt, errors = _run_lane_done(
        Path(SERVING_ROOT),
    )

    assert exit_code != 0, "Expected non-zero exit for serving root as worktree"
    assert receipt["verdict"] == "refused"
    assert receipt["reason"] == "serving_root_worktree"


def test_lane_done_refuses_fake_serving_root_override(tmp_path: Path) -> None:
    """RED (minor: --serving-root read from env/CLI, not hardcoded): overriding
    --serving-root to a FAKE, hermetic path (never touching the real production
    checkout) must still refuse a worktree located there — proving the flag is wired
    all the way through, not dead code."""

    fake_serving_root = tmp_path / "fake-serving-root"
    fake_serving_root.mkdir()
    _git(fake_serving_root, "init", "-b", "main")
    _git(fake_serving_root, "config", "user.name", "Test")
    _git(fake_serving_root, "config", "user.email", "test@example.com")
    (fake_serving_root / "base.txt").write_text("base\n")
    _git(fake_serving_root, "add", "base.txt")
    _git(fake_serving_root, "commit", "-m", "base")

    exit_code, receipt, errors = _run_lane_done(
        fake_serving_root,
        serving_root=str(fake_serving_root),
    )

    assert exit_code != 0
    assert receipt["verdict"] == "refused"
    assert receipt["reason"] == "serving_root_worktree"


def test_lane_done_refuses_invalid_serving_root_override(valid_lane: Path, tmp_path: Path) -> None:
    """RED (round-2 finding 18): a --serving-root override that does not resolve to an
    existing directory used to be silently accepted with no validation at all — the
    check it feeds (not_serving_root) trivially "passed" for any worktree, since it
    could never equal a nonexistent path. A bad override must itself be refused."""

    _git(valid_lane, "checkout", "feat/test-lane")
    scratch_db = tmp_path / "scratch.sqlite3"

    exit_code, receipt, errors = _run_lane_done(
        valid_lane,
        db_path=str(scratch_db),
        serving_root="/this/path/does/not/exist/at/all/probably",
    )

    assert exit_code != 0
    assert receipt["verdict"] == "refused"
    assert receipt["reason"] == "invalid_serving_root_override"


def test_lane_done_serving_root_override_adds_not_replaces_default_protection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """RED (round-2 finding 18): supplying a --serving-root override must never REMOVE
    protection for the built-in default — it may only ADD a second protected root. Since
    the real default is the live production checkout (never touched by tests), this
    exercises the mechanism directly: monkeypatch DEFAULT_SERVING_ROOT to a fake
    fixture-only path and prove a worktree AT that fake default is STILL refused even
    when a totally unrelated --serving-root override is also supplied — the override
    must not silently supersede the default check target."""
    from scripts import lane_done

    fake_default = tmp_path / "fake-default-serving-root"
    fake_default.mkdir()
    _git(fake_default, "init", "-b", "main")
    _git(fake_default, "config", "user.name", "Test")
    _git(fake_default, "config", "user.email", "test@example.com")
    (fake_default / "base.txt").write_text("base\n")
    _git(fake_default, "add", "base.txt")
    _git(fake_default, "commit", "-m", "base")

    unrelated_override = tmp_path / "unrelated-override-dir"
    unrelated_override.mkdir()

    monkeypatch.setattr(lane_done, "DEFAULT_SERVING_ROOT", str(fake_default))

    success, receipt, errors = lane_done.verify_lane(
        worktree=str(fake_default),
        base="main",
        serving_root=str(unrelated_override),
        db_path=str(tmp_path / "scratch.sqlite3"),
    )

    assert not success, "the (fake) default serving root must stay protected regardless of override"
    assert receipt.verdict == "refused"
    assert receipt.reason == "serving_root_worktree"


def test_lane_done_passes_valid_lane(valid_lane: Path, tmp_path: Path) -> None:
    """GREEN: a valid lane should pass all checks."""

    _git(valid_lane, "checkout", "feat/test-lane")

    # Use a scratch DB path
    scratch_db = tmp_path / "scratch.sqlite3"

    exit_code, receipt, errors = _run_lane_done(
        valid_lane,
        base="main",
        db_path=str(scratch_db),
    )

    assert exit_code == 0, f"Expected exit 0 for valid lane, got {exit_code}\nErrors: {errors}"
    assert receipt["verdict"] == "admissible"
    assert receipt["reason"] is None
    assert receipt["commit_count"] > 0
    assert not errors


def test_lane_done_passes_with_claim_sha(valid_lane: Path, tmp_path: Path) -> None:
    """GREEN: valid lane with resolved, ancestral claim SHA should pass."""

    _git(valid_lane, "checkout", "feat/test-lane")

    # Get the HEAD SHA (which is definitely an ancestor of HEAD)
    head_sha = _git(valid_lane, "rev-parse", "HEAD")

    scratch_db = tmp_path / "scratch.sqlite3"

    exit_code, receipt, errors = _run_lane_done(
        valid_lane,
        base="main",
        claim_shas=[head_sha],
        db_path=str(scratch_db),
    )

    assert exit_code == 0, (
        f"Expected exit 0 for valid lane with claim SHA, got {exit_code}\nErrors: {errors}"
    )
    assert receipt["verdict"] == "admissible"


def test_lane_done_passes_with_owned_path_match(valid_lane: Path, tmp_path: Path) -> None:
    """GREEN: lane with files matching owned paths should pass."""

    _git(valid_lane, "checkout", "feat/test-lane")

    scratch_db = tmp_path / "scratch.sqlite3"

    # "feature.txt" should match the glob "feature*"
    exit_code, receipt, errors = _run_lane_done(
        valid_lane,
        base="main",
        owned_paths=["feature*"],
        db_path=str(scratch_db),
    )

    assert exit_code == 0, (
        f"Expected exit 0 for owned paths match, got {exit_code}\nErrors: {errors}"
    )
    assert receipt["verdict"] == "admissible"


def test_lane_done_receipt_structure(valid_lane: Path, tmp_path: Path) -> None:
    """GREEN: receipt should contain all required fields, including the new
    machine-readable `reason` and non-blocking `warnings` fields."""

    _git(valid_lane, "checkout", "feat/test-lane")

    scratch_db = tmp_path / "scratch.sqlite3"

    exit_code, receipt, _ = _run_lane_done(
        valid_lane,
        db_path=str(scratch_db),
    )

    assert exit_code == 0

    # Verify receipt structure
    assert "worktree" in receipt
    assert "branch" in receipt
    assert "head_sha" in receipt
    assert "base_sha" in receipt
    assert "merge_base_ok" in receipt
    assert "commit_count" in receipt
    assert "changed_files" in receipt
    assert isinstance(receipt["changed_files"], list)
    assert "checks" in receipt
    assert isinstance(receipt["checks"], dict)
    assert "verdict" in receipt
    assert "reason" in receipt
    assert receipt["reason"] is None
    assert "warnings" in receipt
    assert receipt["warnings"] == []


def test_lane_done_detects_not_git_worktree(tmp_path: Path) -> None:
    """RED: non-git directory should be refused."""

    non_git = tmp_path / "not_git"
    non_git.mkdir()

    exit_code, receipt, errors = _run_lane_done(non_git)

    assert exit_code != 0
    assert receipt["verdict"] == "refused"
    assert receipt["reason"] == "not_a_git_worktree"
    assert any("not inside a git worktree" in str(e).lower() for e in errors)


def test_lane_done_refuses_missing_worktree_without_traceback(tmp_path: Path) -> None:
    """RED (minor: FileNotFoundError -> clean refusal receipt, not a traceback): a
    worktree path that does not exist at all used to crash subprocess.run's cwd
    resolution with an uncaught FileNotFoundError."""

    missing = tmp_path / "does-not-exist-at-all"

    exit_code, receipt, errors = _run_lane_done(missing)

    assert exit_code != 0
    assert "error" not in receipt or receipt.get("verdict") == "refused", (
        f"Expected a clean JSON refusal receipt, not a parse failure: {receipt}"
    )
    assert receipt["verdict"] == "refused"
    assert receipt["reason"] == "worktree_not_found"
    assert any("unreadable" in str(e).lower() for e in errors), errors
    # No Python traceback should have reached stderr.
    assert not any("Traceback" in e for e in errors), errors


def test_lane_done_refuses_git_command_failure_not_silent_success(
    valid_lane: Path, tmp_path: Path
) -> None:
    """RED (blocker #2): a git ERROR (corrupted .git/index) must refuse with a named
    reason, never silently read as an empty-but-successful result. Before the fix, a
    corrupted index made `git status --porcelain` fail, and the unchecked stdout (empty)
    made tree_clean read True."""

    _git(valid_lane, "checkout", "feat/test-lane")
    scratch_db = tmp_path / "scratch.sqlite3"

    index_path = valid_lane / ".git" / "index"
    index_path.write_bytes(b"not a real git index\x00\x01\x02")

    exit_code, receipt, errors = _run_lane_done(valid_lane, db_path=str(scratch_db))

    assert exit_code != 0, "A corrupted .git/index must never read as admissible"
    assert receipt["verdict"] == "refused"
    assert receipt["reason"] == "git_command_failed"
    assert receipt["checks"].get("tree_clean") is not True, (
        "tree_clean must not read True when git itself failed to answer"
    )
    assert any(
        "git status" in str(e).lower() or "git command failed" in str(e).lower() for e in errors
    ), errors


def test_lane_done_refuses_empty_diff_allow_empty_commit(tmp_path: Path) -> None:
    """RED (blocker #1, case A): `git commit --allow-empty` has commit_count > 0 but
    contributes NO content change and must be refused — the pre-fix code only checked
    commit_count, which this satisfies trivially."""

    repo = tmp_path / "allow_empty_repo"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.name", "Test")
    _git(repo, "config", "user.email", "test@example.com")
    (repo / "base.txt").write_text("base\n")
    _git(repo, "add", "base.txt")
    _git(repo, "commit", "-m", "base")

    _git(repo, "checkout", "-b", "feat/allow-empty")
    _git(repo, "commit", "--allow-empty", "-m", "empty commit, zero content")

    head_sha = _git(repo, "rev-parse", "HEAD")
    _write_done_json(repo, head_sha=head_sha, owned_paths=["*"])

    scratch_db = tmp_path / "scratch.sqlite3"
    exit_code, receipt, errors = _run_lane_done(repo, db_path=str(scratch_db))

    assert exit_code != 0, "An allow-empty commit must never certify as admissible"
    assert receipt["verdict"] == "refused"
    assert receipt["reason"] == "empty_diff"
    assert receipt["commit_count"] > 0, "fixture invariant: commit_count must be nonzero"
    assert any("empty" in str(e).lower() for e in errors), errors


def test_lane_done_refuses_empty_diff_merge_only_branch(tmp_path: Path) -> None:
    """RED (blocker #1, case B): a merge-only branch (git merge --no-ff main with zero
    own work) also has commit_count > 0 (the merge commit itself) but contributes no
    content beyond what main already has, and must be refused separately from the
    allow-empty case — same check, different construction, tested independently per
    the review's explicit "test each separately" instruction."""

    repo = tmp_path / "merge_only_repo"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.name", "Test")
    _git(repo, "config", "user.email", "test@example.com")
    (repo / "base.txt").write_text("base\n")
    _git(repo, "add", "base.txt")
    _git(repo, "commit", "-m", "base")

    _git(repo, "checkout", "-b", "feat/merge-only")
    _git(repo, "checkout", "main")
    (repo / "extra.txt").write_text("extra\n")
    _git(repo, "add", "extra.txt")
    _git(repo, "commit", "-m", "main moves on")
    _git(repo, "checkout", "feat/merge-only")
    _git(repo, "merge", "--no-ff", "main", "-m", "merge main into feat (no own work)")

    head_sha = _git(repo, "rev-parse", "HEAD")
    _write_done_json(repo, head_sha=head_sha, owned_paths=["*"])

    scratch_db = tmp_path / "scratch.sqlite3"
    exit_code, receipt, errors = _run_lane_done(repo, db_path=str(scratch_db))

    assert exit_code != 0, "A merge-only branch with zero own work must never certify as admissible"
    assert receipt["verdict"] == "refused"
    assert receipt["reason"] == "empty_diff"
    assert receipt["commit_count"] > 0, (
        "fixture invariant: the merge commit itself is nonzero count"
    )
    assert any("empty" in str(e).lower() for e in errors), errors


def test_lane_done_refuses_missing_done_manifest(valid_lane: Path, tmp_path: Path) -> None:
    """RED (blocker #3): an otherwise-perfectly-valid lane with NO DONE.json at all
    must be refused — claim SHAs and owned paths are no longer opt-in."""

    _git(valid_lane, "checkout", "feat/test-lane")
    (valid_lane / "DONE.json").unlink()
    scratch_db = tmp_path / "scratch.sqlite3"

    exit_code, receipt, errors = _run_lane_done(valid_lane, db_path=str(scratch_db))

    assert exit_code != 0
    assert receipt["verdict"] == "refused"
    assert receipt["reason"] == "missing_done_manifest"
    assert any("done.json" in str(e).lower() for e in errors), errors


@pytest.mark.parametrize(
    "manifest_content",
    [
        # NOTE: "abc123" is deliberately not a real 40-hex SHA — it exercises the
        # missing/wrong-type field cases below independent of the head_sha FORMAT
        # check (round-2 finding 17), which has its own dedicated tests.
        pytest.param({"head_sha": "a" * 40}, id="missing_owned_paths"),
        pytest.param({"owned_paths": ["feature.txt"]}, id="missing_head_sha"),
        pytest.param({"head_sha": "a" * 40, "owned_paths": []}, id="empty_owned_paths"),
        pytest.param({"head_sha": "", "owned_paths": ["feature.txt"]}, id="empty_head_sha"),
        pytest.param(
            {"head_sha": "a" * 40, "owned_paths": "not-a-list"}, id="owned_paths_wrong_type"
        ),
        pytest.param("not-an-object", id="not_a_json_object"),
    ],
)
def test_lane_done_refuses_incomplete_done_manifest(
    valid_lane: Path, tmp_path: Path, manifest_content: object
) -> None:
    """RED (blocker #3 / round-2 finding 21): a DONE.json that exists but is
    missing/empty/malformed on a required field must be refused with the DISTINCT
    `malformed_done_manifest` reason — "present but broken" is a different, more
    precise diagnosis than "absent" (`missing_done_manifest`, tested separately)."""

    _git(valid_lane, "checkout", "feat/test-lane")
    (valid_lane / "DONE.json").write_text(json.dumps(manifest_content))
    scratch_db = tmp_path / "scratch.sqlite3"

    exit_code, receipt, errors = _run_lane_done(valid_lane, db_path=str(scratch_db))

    assert exit_code != 0
    assert receipt["verdict"] == "refused"
    assert receipt["reason"] == "malformed_done_manifest"


def test_lane_done_refuses_done_json_that_is_a_directory(valid_lane: Path, tmp_path: Path) -> None:
    """RED (round-2 finding 21, dir-as-file): a directory sitting at DONE.json's path
    must be refused as malformed, not crash or read as absent."""

    _git(valid_lane, "checkout", "feat/test-lane")
    (valid_lane / "DONE.json").unlink()
    (valid_lane / "DONE.json").mkdir()
    scratch_db = tmp_path / "scratch.sqlite3"

    exit_code, receipt, errors = _run_lane_done(valid_lane, db_path=str(scratch_db))

    assert exit_code != 0
    assert receipt["verdict"] == "refused"
    assert receipt["reason"] == "malformed_done_manifest"


@pytest.mark.parametrize(
    # Each value is syntactically NOT a 40-lowercase-hex string, so the manifest-format
    # check itself must catch it (a well-formed-but-nonexistent 40-hex SHA is a
    # DIFFERENT case — claim_sha_unresolvable — covered by
    # test_lane_done_refuses_fabricated_claim_sha instead).
    "tautological_head_sha",
    ["HEAD", "feat/test-lane", "main", "abc123", "a" * 39, "A" * 40],
)
def test_lane_done_refuses_tautological_head_sha(
    valid_lane: Path, tmp_path: Path, tautological_head_sha: str
) -> None:
    """RED (round-2 finding 17): DONE.json {"head_sha": "HEAD"} (or a branch name, or
    any non-40-hex value) used to satisfy the manifest tautologically — it always
    equals whatever HEAD currently resolves to, proving nothing about a SPECIFIC
    commit. head_sha must be a real, full, resolved, lowercase 40-hex commit id."""

    _git(valid_lane, "checkout", "feat/test-lane")
    (valid_lane / "DONE.json").write_text(
        json.dumps({"head_sha": tautological_head_sha, "owned_paths": ["feature.txt"]})
    )
    scratch_db = tmp_path / "scratch.sqlite3"

    exit_code, receipt, errors = _run_lane_done(valid_lane, db_path=str(scratch_db))

    assert exit_code != 0, f"tautological head_sha {tautological_head_sha!r} must be refused"
    assert receipt["verdict"] == "refused"
    assert receipt["reason"] == "malformed_done_manifest"


def test_lane_done_warns_when_head_sha_is_stale_but_genuine(
    deep_base_lane: tuple[Path, str, str], tmp_path: Path
) -> None:
    """GREEN + advisory (round-2 finding 17, second half): a manifest head_sha that IS
    a real, resolved, in-range commit — just not the LITERAL current tip (e.g. written
    a commit or two before the latest push) — must still PASS, with a non-blocking
    warning, not a refusal. Requiring exact tip equality would be too strict."""

    repo, _root_sha, _base_tip_sha = deep_base_lane
    _git(repo, "checkout", "feat/deep")
    first_own_sha = _git(repo, "rev-parse", "HEAD")

    # Add a SECOND commit on top so HEAD moves past what the manifest declared.
    (repo / "more.txt").write_text("more\n")
    _git(repo, "add", "more.txt")
    _git(repo, "commit", "-m", "more work after the manifest was written")
    current_head = _git(repo, "rev-parse", "HEAD")
    assert current_head != first_own_sha

    _write_done_json(repo, head_sha=first_own_sha, owned_paths=["feature.txt", "more.txt"])
    scratch_db = tmp_path / "scratch.sqlite3"

    exit_code, receipt, errors = _run_lane_done(repo, base="main", db_path=str(scratch_db))

    assert exit_code == 0, f"a stale-but-genuine head_sha must still pass: {errors}"
    assert receipt["verdict"] == "admissible"
    assert any("head_sha" in w and "stale" in w.lower() for w in receipt["warnings"]), receipt[
        "warnings"
    ]


def test_lane_done_warns_on_declared_base_mismatch(valid_lane: Path, tmp_path: Path) -> None:
    """GREEN + advisory (round-2 review addendum): DONE.json MAY carry an optional,
    self-describing "base" field. When present and it disagrees with the --base
    actually used, that's worth a warning — never a refusal, since --base remains
    authoritative either way and the manifest may simply be stale."""

    _git(valid_lane, "checkout", "feat/test-lane")
    head_sha = _git(valid_lane, "rev-parse", "HEAD")
    _write_done_json(
        valid_lane, head_sha=head_sha, owned_paths=["feature.txt"], base="not-actually-main"
    )
    scratch_db = tmp_path / "scratch.sqlite3"

    exit_code, receipt, errors = _run_lane_done(valid_lane, base="main", db_path=str(scratch_db))

    assert exit_code == 0, f"a declared-base mismatch is advisory, not a refusal: {errors}"
    assert receipt["verdict"] == "admissible"
    assert any("base=" in w for w in receipt["warnings"]), receipt["warnings"]


def test_lane_done_warns_on_declared_lane_mismatch(valid_lane: Path, tmp_path: Path) -> None:
    """GREEN + advisory (round-2 review addendum): DONE.json MAY carry an optional,
    self-describing "lane" field, cross-checked against the worktree's ACTUAL branch."""

    _git(valid_lane, "checkout", "feat/test-lane")
    head_sha = _git(valid_lane, "rev-parse", "HEAD")
    _write_done_json(
        valid_lane,
        head_sha=head_sha,
        owned_paths=["feature.txt"],
        lane="totally-different-lane-name",
    )
    scratch_db = tmp_path / "scratch.sqlite3"

    exit_code, receipt, errors = _run_lane_done(valid_lane, base="main", db_path=str(scratch_db))

    assert exit_code == 0, f"a declared-lane mismatch is advisory, not a refusal: {errors}"
    assert receipt["verdict"] == "admissible"
    assert any("lane=" in w for w in receipt["warnings"]), receipt["warnings"]


def test_lane_done_passes_with_matching_declared_base_and_lane(
    valid_lane: Path, tmp_path: Path
) -> None:
    """GREEN: when DONE.json's optional "base"/"lane" fields MATCH reality, no
    warning is raised — the cross-check is silent when there's nothing to flag."""

    _git(valid_lane, "checkout", "feat/test-lane")
    head_sha = _git(valid_lane, "rev-parse", "HEAD")
    _write_done_json(
        valid_lane,
        head_sha=head_sha,
        owned_paths=["feature.txt"],
        base="main",
        lane="feat/test-lane",
    )
    scratch_db = tmp_path / "scratch.sqlite3"

    exit_code, receipt, errors = _run_lane_done(valid_lane, base="main", db_path=str(scratch_db))

    assert exit_code == 0, errors
    assert receipt["verdict"] == "admissible"
    assert receipt["warnings"] == []


def test_lane_done_refuses_detached_head(valid_lane: Path, tmp_path: Path) -> None:
    """RED (major #6): a detached HEAD used to certify admissible with branch:"HEAD"
    in the receipt — `--abbrev-ref HEAD` literally returns the string "HEAD" when
    detached, and nothing refused it."""

    _git(valid_lane, "checkout", "feat/test-lane")
    head_sha = _git(valid_lane, "rev-parse", "HEAD")
    _git(valid_lane, "checkout", head_sha)  # detaches HEAD

    scratch_db = tmp_path / "scratch.sqlite3"
    exit_code, receipt, errors = _run_lane_done(valid_lane, db_path=str(scratch_db))

    assert exit_code != 0, "A detached HEAD must never certify as admissible"
    assert receipt["verdict"] == "refused"
    assert receipt["reason"] == "detached_head"
    assert receipt["branch"] == "HEAD", "fixture invariant: git itself reports 'HEAD' when detached"
    assert any("detached" in str(e).lower() for e in errors), errors


def test_lane_done_refuses_stale_base(stale_base_lane: Path) -> None:
    """RED (major #5): a local `main` that has fallen behind its fetched origin/main
    must be refused as base_stale — certifying against a stale base is a false GREEN,
    since the real merge target has moved on."""

    _git(stale_base_lane, "checkout", "feat/stale")
    scratch_db = stale_base_lane.parent / "scratch.sqlite3"

    exit_code, receipt, errors = _run_lane_done(stale_base_lane, db_path=str(scratch_db))

    assert exit_code != 0, "A stale local base must never certify as admissible"
    assert receipt["verdict"] == "refused"
    assert receipt["reason"] == "base_stale"
    assert any("behind" in str(e).lower() for e in errors), errors


def test_lane_done_passes_when_no_origin_remote_configured(
    valid_lane: Path, tmp_path: Path
) -> None:
    """GREEN companion to the stale-base test: base freshness is conditional on
    origin/<base> existing at all — a lane with no remote configured (every test fixture
    in this file, and every throwaway local repo) must not be penalized for a check it
    has no way to satisfy."""

    _git(valid_lane, "checkout", "feat/test-lane")
    scratch_db = tmp_path / "scratch.sqlite3"

    exit_code, receipt, errors = _run_lane_done(valid_lane, db_path=str(scratch_db))

    assert exit_code == 0, f"Expected a lane with no origin remote to pass freshness: {errors}"
    assert receipt["checks"]["base_fresh"] == "skipped (no origin/main)"


def test_lane_done_warns_on_base_diverged(diverged_base_lane: Path) -> None:
    """RED (round-2 finding 22): local base and its fetched origin/<base> having
    DIVERGED (neither is an ancestor of the other — e.g. a force-push/rebase elsewhere)
    used to pass completely silently: base_fresh read True with an empty warnings list,
    even though the merge target is genuinely ambiguous. Must WARN (not refuse — a
    locally-rebased base is legitimate), naming base_diverged."""

    scratch_db = diverged_base_lane.parent / "scratch.sqlite3"

    exit_code, receipt, errors = _run_lane_done(diverged_base_lane, db_path=str(scratch_db))

    assert exit_code == 0, f"Divergence is advisory, not a refusal: {errors}"
    assert receipt["verdict"] == "admissible"
    assert receipt["checks"]["base_fresh"] is True
    assert any("base_diverged" in w for w in receipt["warnings"]), receipt["warnings"]


def test_lane_done_warns_on_gitignore_additions_without_blocking(tmp_path: Path) -> None:
    """GREEN + advisory (minor: .gitignore-same-commit note): a lane that adds
    .gitignore entries alongside its own work is still admissible, but the receipt
    carries a visible, non-blocking warning."""

    repo = tmp_path / "gitignore_repo"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.name", "Test")
    _git(repo, "config", "user.email", "test@example.com")
    (repo / "base.txt").write_text("base\n")
    _git(repo, "add", "base.txt")
    _git(repo, "commit", "-m", "base")

    _git(repo, "checkout", "-b", "feat/gitignore")
    (repo / "feature.txt").write_text("feature\n")
    (repo / ".gitignore").write_text("*.log\n")
    _git(repo, "add", "feature.txt", ".gitignore")
    _git(repo, "commit", "-m", "add feature and widen gitignore")

    head_sha = _git(repo, "rev-parse", "HEAD")
    _write_done_json(repo, head_sha=head_sha, owned_paths=["feature.txt", ".gitignore"])

    scratch_db = tmp_path / "scratch.sqlite3"
    exit_code, receipt, errors = _run_lane_done(repo, db_path=str(scratch_db))

    assert exit_code == 0, f"A .gitignore addition must warn, not block: {errors}"
    assert receipt["verdict"] == "admissible"
    assert any("gitignore" in w.lower() for w in receipt["warnings"]), receipt["warnings"]


def test_path_matches_pattern_does_not_cross_segment_boundary() -> None:
    """RED (minor: fnmatch crosses '/'): 'src/*' must NOT match a nested path two (or
    more) segments deep — fnmatch.fnmatch's '*' has no path-separator awareness and
    used to treat 'src/*' as "anything under src/ recursively"."""

    assert path_matches_pattern("src/deep/x.py", "src/*") is False
    assert path_matches_pattern("src/a/b/c.py", "src/*") is False


def test_path_matches_pattern_still_matches_direct_children() -> None:
    """GREEN companion (both directions, per the review): the segment-aware fix must
    not over-correct — 'src/*' still matches src/'s DIRECT children."""

    assert path_matches_pattern("src/foo.py", "src/*") is True
    assert path_matches_pattern("foo.py", "*.py") is True
    assert path_matches_pattern("src/foo.py", "*.py") is False  # '*.py' is one segment


def test_fnmatch_patterns_unions_multiple_globs() -> None:
    """GREEN: fnmatch_patterns (the multi-pattern OR) still works with the
    segment-aware matcher underneath."""

    assert fnmatch_patterns("src/foo.py", ["docs/*", "src/*"]) is True
    assert fnmatch_patterns("src/deep/foo.py", ["docs/*", "src/*"]) is False


def test_path_matches_pattern_double_star_matches_recursively() -> None:
    """RED->GREEN (round-2 finding 19): 'src/**' must match BOTH direct children AND
    arbitrarily nested descendants — the recursive escape hatch a lone '*' deliberately
    does not provide."""

    assert path_matches_pattern("src/foo.py", "src/**") is True
    assert path_matches_pattern("src/deep/x.py", "src/**") is True
    assert path_matches_pattern("src/a/b/c/d.py", "src/**") is True
    assert path_matches_pattern("other/foo.py", "src/**") is False


def test_path_matches_pattern_single_star_still_does_not_cross_after_double_star_added() -> None:
    """GREEN companion: adding '**' support must not weaken the plain '*' behavior
    fixed in round 1 — 'src/*' still must NOT match 'src/deep/x.py'."""

    assert path_matches_pattern("src/deep/x.py", "src/*") is False
    assert path_matches_pattern("src/foo.py", "src/*") is True


def test_owned_paths_outside_required_subset_semantics() -> None:
    """RED->GREEN (round-2 finding 20): owned_paths_outside_required is the pure
    function behind --require-owned-path's subset enforcement. An overly-broad
    self-declaration ("**") is correctly judged NOT covered by a narrower requirement
    ("src/**"); a narrower self-declaration ("src/*") is correctly judged covered."""

    assert owned_paths_outside_required(["**"], ["src/**"]) == ["**"]
    assert owned_paths_outside_required(["src/*"], ["src/**"]) == []
    assert owned_paths_outside_required(["src/*", "docs/*"], ["src/**"]) == ["docs/*"]
    assert owned_paths_outside_required(["src/foo.py"], ["src/**"]) == []
