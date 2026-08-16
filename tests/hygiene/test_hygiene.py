"""scripts/hygiene/hygiene.py -- ESTATE HYGIENE nightly sweep.

Covers: age-filter math, merged-branch detection against a tmp repo
fixture, dirty-worktree skip, log rotation naming, and prompt.md policy
loading (override honored / malformed falls back to defaults).
"""

from __future__ import annotations

import os
import subprocess
import time
from datetime import UTC, datetime
from pathlib import Path

import pytest

from scripts.hygiene import hygiene


def _git(args: list[str], cwd: Path, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(cwd), *args], capture_output=True, text=True, check=check
    )


def _init_repo(root: Path) -> None:
    _git(["init", "-q", "-b", "main"], root)
    _git(["config", "user.email", "test@example.com"], root)
    _git(["config", "user.name", "Test"], root)
    (root / "README.md").write_text("hello\n", encoding="utf-8")
    _git(["add", "-A"], root)
    _git(["commit", "-q", "-m", "initial"], root)


def _commit_file(root: Path, name: str, content: str = "x\n") -> None:
    (root / name).write_text(content, encoding="utf-8")
    _git(["add", "-A"], root)
    _git(["commit", "-q", "-m", f"add {name}"], root)


# --------------------------------------------------------------------------
# age-filter math
# --------------------------------------------------------------------------


def test_cutoff_epoch_is_exactly_n_days_before_now() -> None:
    now = datetime(2026, 7, 24, 12, 0, 0, tzinfo=UTC)
    cutoff = hygiene.cutoff_epoch(14, now=now)
    expected = datetime(2026, 7, 10, 12, 0, 0, tzinfo=UTC).timestamp()
    assert cutoff == pytest.approx(expected)


def test_is_older_than_boundary() -> None:
    now = datetime(2026, 7, 24, 12, 0, 0, tzinfo=UTC)
    cutoff = hygiene.cutoff_epoch(14, now=now)
    assert hygiene.is_older_than(cutoff - 1, 14, now=now) is True
    assert hygiene.is_older_than(cutoff + 1, 14, now=now) is False


def test_iter_old_jsonl_filters_by_mtime(tmp_path: Path) -> None:
    root = tmp_path / "projects"
    root.mkdir()
    old = root / "sub" / "old.jsonl"
    old.parent.mkdir(parents=True)
    old.write_text("{}\n", encoding="utf-8")
    new = root / "new.jsonl"
    new.write_text("{}\n", encoding="utf-8")
    not_jsonl = root / "notes.txt"
    not_jsonl.write_text("skip me\n", encoding="utf-8")

    now = time.time()
    os.utime(old, (now - 20 * 86400, now - 20 * 86400))
    os.utime(new, (now - 1 * 86400, now - 1 * 86400))

    cutoff = now - 14 * 86400
    found = sorted(hygiene.iter_old_jsonl(root, cutoff))
    assert found == [old]


def test_old_ledger_sessions_filters_by_mtime(tmp_path: Path) -> None:
    sessions = tmp_path / "ledger" / "sessions"
    sessions.mkdir(parents=True)
    old = sessions / "ses_old.jsonl"
    old.write_text("{}\n", encoding="utf-8")
    new = sessions / "ses_new.jsonl"
    new.write_text("{}\n", encoding="utf-8")

    now = time.time()
    os.utime(old, (now - 31 * 86400, now - 31 * 86400))
    os.utime(new, (now - 1 * 86400, now - 1 * 86400))

    cutoff = now - 30 * 86400
    assert hygiene.old_ledger_sessions(sessions, cutoff) == [old]


def test_old_ledger_sessions_absent_dir_returns_empty(tmp_path: Path) -> None:
    assert hygiene.old_ledger_sessions(tmp_path / "nope", time.time()) == []


# --------------------------------------------------------------------------
# merged-branch detection (tmp repo fixture)
# --------------------------------------------------------------------------


def test_merged_swarm_branches_only_returns_merged_swarm_branches(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)

    # Merged: branched off main, no new commits -> trivially merged.
    _git(["branch", "swarm/merged-one"], repo)

    # Not merged: a branch with a commit main doesn't have.
    _git(["checkout", "-q", "-b", "swarm/not-merged"], repo)
    _commit_file(repo, "unmerged.txt")
    _git(["checkout", "-q", "main"], repo)

    # Not a swarm branch at all -- must never be touched by this sweep.
    _git(["branch", "fusion/other"], repo)

    result = hygiene.merged_swarm_branches(repo)
    assert result == ["swarm/merged-one"]


def test_sweep_merged_branches_deletes_and_logs(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    _git(["branch", "swarm/merged-one"], repo)
    _git(["checkout", "-q", "-b", "swarm/not-merged"], repo)
    _commit_file(repo, "unmerged.txt")
    _git(["checkout", "-q", "main"], repo)

    lines: list[str] = []
    report = hygiene.sweep_merged_branches(repo, lines.append)

    assert report["deleted"] == ["swarm/merged-one"]
    assert report["skipped"] == []
    remaining = _git(["branch", "--format=%(refname:short)"], repo).stdout.split()
    assert "swarm/merged-one" not in remaining
    assert "swarm/not-merged" in remaining  # never touched: not merged
    assert any("deleted merged swarm/merged-one" in line for line in lines)


def test_sweep_merged_branches_no_matches_still_logs(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    lines: list[str] = []
    report = hygiene.sweep_merged_branches(repo, lines.append)
    assert report["deleted"] == []
    assert any("no merged" in line for line in lines)


# --------------------------------------------------------------------------
# dirty-worktree skip
# --------------------------------------------------------------------------


def test_sweep_worktrees_skips_dirty_worktree_and_never_forces(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    _git(["branch", "swarm/dirty-one"], repo)

    worktrees_root = tmp_path / "worktrees"
    worktrees_root.mkdir()
    wt_path = worktrees_root / "dirty-one"
    _git(["worktree", "add", str(wt_path), "swarm/dirty-one"], repo)
    # Make it dirty: an uncommitted change in the working tree.
    (wt_path / "README.md").write_text("dirty change\n", encoding="utf-8")

    lines: list[str] = []
    report = hygiene.sweep_worktrees(repo, worktrees_root, lines.append, min_age_hours=0.0)

    assert report["removed"] == []
    assert len(report["skipped"]) == 1
    assert report["skipped"][0][0] == str(wt_path)
    assert "dirty" in report["skipped"][0][1]
    assert wt_path.is_dir(), "a dirty worktree must never be removed, even unforced"
    assert any("SKIPPED" in line and "never forced" in line for line in lines)

    # The worktree is still registered with git -- removal truly never happened.
    listed = _git(["worktree", "list"], repo).stdout
    assert str(wt_path) in listed


def test_sweep_worktrees_skips_unmerged_branch(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    _git(["checkout", "-q", "-b", "swarm/unmerged"], repo)
    _commit_file(repo, "unmerged.txt")
    _git(["checkout", "-q", "main"], repo)

    worktrees_root = tmp_path / "worktrees"
    worktrees_root.mkdir()
    wt_path = worktrees_root / "unmerged"
    _git(["worktree", "add", str(wt_path), "swarm/unmerged"], repo)

    lines: list[str] = []
    report = hygiene.sweep_worktrees(repo, worktrees_root, lines.append, min_age_hours=0.0)
    assert report["removed"] == []
    assert "not merged" in report["skipped"][0][1]
    assert wt_path.is_dir()


def test_sweep_worktrees_removes_clean_merged_worktree(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    _git(["branch", "swarm/clean-one"], repo)

    worktrees_root = tmp_path / "worktrees"
    worktrees_root.mkdir()
    wt_path = worktrees_root / "clean-one"
    _git(["worktree", "add", str(wt_path), "swarm/clean-one"], repo)

    lines: list[str] = []
    report = hygiene.sweep_worktrees(repo, worktrees_root, lines.append, min_age_hours=0.0)

    assert report["removed"] == [str(wt_path)]
    assert report["skipped"] == []
    assert not wt_path.exists()


def test_sweep_worktrees_refuses_fusion_wt_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_home = tmp_path / "home"
    fusion_wt = fake_home / ".fusion-wt"
    fusion_wt.mkdir(parents=True)
    monkeypatch.setattr(hygiene, "FUSION_WT_ROOT", fusion_wt)

    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)

    lines: list[str] = []
    report = hygiene.sweep_worktrees(repo, fusion_wt, lines.append)
    assert report == {"checked": 0, "removed": [], "skipped": []}
    assert any("REFUSING" in line and ".fusion-wt" in line for line in lines)


# --------------------------------------------------------------------------
# log rotation naming
# --------------------------------------------------------------------------


def test_rotated_path_naming() -> None:
    path = Path("/x/var/log/api.launchd.log")
    assert hygiene.rotated_path(path, 1) == Path("/x/var/log/api.launchd.log.1.gz")
    assert hygiene.rotated_path(path, 2) == Path("/x/var/log/api.launchd.log.2.gz")
    assert hygiene.rotated_path(path, 3) == Path("/x/var/log/api.launchd.log.3.gz")


def test_rotate_log_shifts_generations_and_drops_oldest(tmp_path: Path) -> None:
    log_dir = tmp_path / "log"
    log_dir.mkdir()
    path = log_dir / "api.log"
    path.write_text("current content\n", encoding="utf-8")
    hygiene.rotated_path(path, 1).write_bytes(b"gen1-bytes")
    hygiene.rotated_path(path, 2).write_bytes(b"gen2-bytes")
    hygiene.rotated_path(path, 3).write_bytes(b"gen3-bytes-should-be-dropped")

    size = hygiene.rotate_log(path, keep=3)

    assert size == len("current content\n")
    assert hygiene.rotated_path(path, 2).read_bytes() == b"gen1-bytes"  # shifted 1 -> 2
    assert hygiene.rotated_path(path, 3).read_bytes() == b"gen2-bytes"  # shifted 2 -> 3
    # oldest (former gen3) is gone -- 3 generations kept, no more.
    assert path.read_bytes() == b""  # truncated for the next append
    import gzip

    with gzip.open(hygiene.rotated_path(path, 1), "rb") as fh:
        assert fh.read() == b"current content\n"


def test_logs_over_limit_excludes_hygiene_log_and_respects_size(tmp_path: Path) -> None:
    log_dir = tmp_path / "log"
    log_dir.mkdir()
    big = log_dir / "big.log"
    big.write_bytes(b"x" * 100)
    small = log_dir / "small.log"
    small.write_bytes(b"x" * 10)
    hygiene_log = log_dir / "hygiene.log"
    hygiene_log.write_bytes(b"x" * 1000)

    found = hygiene.logs_over_limit(log_dir, limit_bytes=50)
    assert found == [big]  # small.log under limit, hygiene.log excluded regardless of size


def test_sweep_logs_rotates_only_over_limit(tmp_path: Path) -> None:
    log_dir = tmp_path / "log"
    log_dir.mkdir()
    big = log_dir / "big.log"
    big.write_bytes(b"x" * 200)
    small = log_dir / "small.log"
    small.write_bytes(b"x" * 10)

    lines: list[str] = []
    report = hygiene.sweep_logs(log_dir, limit_mb=0, log=lines.append)
    # limit_mb=0 -> limit_bytes=0, both files are "over the limit"
    assert str(big) in report["rotated"]
    assert str(small) in report["rotated"]


# --------------------------------------------------------------------------
# policy: override honored; malformed -> defaults
# --------------------------------------------------------------------------


def test_load_policy_missing_file_falls_back_to_defaults(tmp_path: Path) -> None:
    lines: list[str] = []
    policy = hygiene.load_policy(tmp_path / "does-not-exist.md", lines.append)
    assert policy == hygiene.Policy()
    assert any("not found" in line for line in lines)


def test_load_policy_override_is_honored(tmp_path: Path) -> None:
    prompt = tmp_path / "prompt.md"
    prompt.write_text(
        "# Agent\n\nSome instructions.\n\n"
        "```yaml\n"
        "policy:\n"
        "  transcript_archive_days: 7\n"
        "  ledger_archive_days: 60\n"
        "  log_rotate_mb: 100\n"
        "  worktree_prune_enabled: false\n"
        "  branch_prune_enabled: false\n"
        "  filesearch_reindex: false\n"
        "```\n",
        encoding="utf-8",
    )
    policy = hygiene.load_policy(prompt)
    assert policy == hygiene.Policy(
        transcript_archive_days=7,
        ledger_archive_days=60,
        log_rotate_mb=100,
        worktree_prune_enabled=False,
        branch_prune_enabled=False,
        filesearch_reindex=False,
    )


def test_load_policy_partial_override_keeps_other_defaults(tmp_path: Path) -> None:
    prompt = tmp_path / "prompt.md"
    prompt.write_text("```yaml\npolicy:\n  transcript_archive_days: 3\n```\n", encoding="utf-8")
    policy = hygiene.load_policy(prompt)
    assert policy.transcript_archive_days == 3
    assert policy.ledger_archive_days == hygiene.Policy().ledger_archive_days


def test_load_policy_malformed_yaml_falls_back_to_defaults(tmp_path: Path) -> None:
    prompt = tmp_path / "prompt.md"
    prompt.write_text("```yaml\npolicy: [this is not, a mapping\n```\n", encoding="utf-8")
    lines: list[str] = []
    policy = hygiene.load_policy(prompt, lines.append)
    assert policy == hygiene.Policy()
    assert any("failed to parse" in line or "no top-level" in line for line in lines)


def test_load_policy_no_yaml_block_falls_back_to_defaults(tmp_path: Path) -> None:
    prompt = tmp_path / "prompt.md"
    prompt.write_text("# Agent\n\nNo fenced block here.\n", encoding="utf-8")
    lines: list[str] = []
    policy = hygiene.load_policy(prompt, lines.append)
    assert policy == hygiene.Policy()
    assert any("no fenced yaml block" in line for line in lines)


def test_load_policy_bad_value_type_falls_back_to_defaults(tmp_path: Path) -> None:
    prompt = tmp_path / "prompt.md"
    prompt.write_text(
        "```yaml\npolicy:\n  transcript_archive_days: not-a-number\n```\n", encoding="utf-8"
    )
    lines: list[str] = []
    policy = hygiene.load_policy(prompt, lines.append)
    assert policy == hygiene.Policy()
    assert any("invalid value" in line for line in lines)


def test_load_policy_missing_policy_key_falls_back_to_defaults(tmp_path: Path) -> None:
    prompt = tmp_path / "prompt.md"
    prompt.write_text("```yaml\nsomething_else: 1\n```\n", encoding="utf-8")
    policy = hygiene.load_policy(prompt)
    assert policy == hygiene.Policy()


def test_real_prompt_md_parses_to_code_defaults() -> None:
    """The shipped prompt.md's policy block should mirror Policy()'s own
    code defaults exactly -- if someone edits one without the other, this
    catches the drift."""
    policy = hygiene.load_policy(hygiene.DEFAULT_PROMPT_PATH)
    assert policy == hygiene.Policy()


# --------------------------------------------------------------------------
# swarm-var-worktrees: absent directory is a clean no-op
# --------------------------------------------------------------------------


def test_sweep_swarm_var_worktrees_absent_dir_is_noop(tmp_path: Path) -> None:
    lines: list[str] = []
    report = hygiene.sweep_swarm_var_worktrees(
        tmp_path, tmp_path / "var" / "omniagentos.db", lines.append
    )
    assert report["present"] is False
    assert report["removed"] == 0
    assert any("absent" in line for line in lines)


def test_sweep_worktrees_age_guard_skips_fresh_worktree(tmp_path: Path) -> None:
    """The zero-commit trap: a fresh branch reads as merged; the age guard is
    what protects an in-flight agent's brand-new worktree (live incident:
    night-1 pruned an in-flight builder's worktree)."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    _git(["branch", "swarm/fresh-agent"], repo)
    worktrees_root = tmp_path / "worktrees"
    worktrees_root.mkdir()
    wt_path = worktrees_root / "fresh-agent"
    _git(["worktree", "add", str(wt_path), "swarm/fresh-agent"], repo)

    lines: list[str] = []
    report = hygiene.sweep_worktrees(repo, worktrees_root, lines.append, min_age_hours=24.0)

    assert report["removed"] == []
    assert any("younger than min_age_hours" in reason for _, reason in report["skipped"])
