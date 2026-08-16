"""Tests for the git-truth merge counter (scripts/ops/merge_truth.py).

The session ledger is a voluntary-discipline log and has been observed to
invert daily throughput rankings (ledger 32 merges on Aug-5 vs git-truth 7;
ledger 0 vs git 37 on Aug-11). These tests verify:

  (a) the git-derived counting function returns exactly N merges on the
      right dates for a fixture repo with synthetic merge commits;
  (b) a fixture ledger whose per-day counts diverge >10% from git-truth
      trips the divergence flag;
  (c) scripts/git-hooks/post-merge contains a guarded, absolute-path ledger
      write suffixed `|| true` (best-effort, never blocks a merge);
  (d) the launchd plist lints clean (skipped if plutil is unavailable) and
      its ProgramArguments reference absolute paths that exist on disk;
  (e) an absent ledger file reports instrument absence -- NOT agreement --
      even when the git-derived count for that day is zero (no favourable
      absence).

Cross-lineage review round (2026-08-12) additionally pinned:

  (D-001) a failed git query (missing repo / nonzero exit / timeout) must
      surface as its own instrument-absence state, never a healthy zero;
  (D-002) a ledger file where every non-blank line fails to parse (N>0
      lines, 0 events) is instrument absence, not a legitimate zero;
  (D-003) git and the ledger must bucket the SAME merge instant into the
      SAME UTC calendar day, regardless of the commit's own timezone or
      this process's ambient TZ;
  (D-004) the divergence denominator is the authoritative git count, never
      max(git, ledger) -- and the git-count-zero case is explicitly
      defined rather than left to an implicit denominator;
  (D-005) --recompute must not publish a healthy-shaped zero ceiling when
      its source instrument (git or ledger) is absent for the window;
  (D-006) the hook test itself must anchor its `|| true` check to the
      ledger-append invocation, not to "does this string appear anywhere
      in the file".

Round-2 review pinned residual edge branches of the same findings:

  (D-002 residual a) a syntactically valid JSON event claiming to be a
      merge, but with an unparseable timestamp, must increment the SAME
      corruption counter as an unparseable line -- via one shared
      predicate (_classify_ledger_record), not a one-off patch;
  (D-002 residual b) invalid UTF-8 bytes must resolve to instrument
      absence, not an uncaught UnicodeDecodeError (read_ledger_file's
      "never raises" contract only caught OSError before this);
  (D-005 residual) LedgerScanResult.instrument_present is a GLOBAL "read
      at least one file" flag; a window straddling one present month and
      one absent month must still report the AGGREGATE ceiling as
      instrument_absent, not average the absent day in as zero;
  (D-006 round 2) anchoring to "the last non-blank line before `fi`" is
      itself foolable by inserting an unrelated guarded command as a new
      last line -- anchor to the SPECIFIC line carrying the append's own
      output redirect instead.

Round 3/4 pinned residuals of the same two findings:

  (D-002 residual, round 3/4) a syntactically valid ISO timestamp near
      datetime.MINYEAR (e.g. "0001-01-01T00:00:00+23:59") overflows during
      UTC normalization (OverflowError from astimezone(), not from
      fromisoformat()) -- _day_from_iso_ts caught only ValueError and the
      overflowing call wasn't even inside that try block, so this escaped
      the "never raises" contract outright;
  (D-006, round 3/4) scanning "the redirect line found anywhere in the
      enclosing if-block" is itself foolable: truncate the append's own
      backslash-continuation early (so it runs unguarded) and place a
      spoofed, correctly guarded redirect on a LATER unrelated command in
      the same if-block -- the block-scan check still finds exactly one
      matching guarded line and passes. Anchor to the append's own SHELL
      LOGICAL COMMAND (its continuation chain) instead.
"""

from __future__ import annotations

import json
import os
import plistlib
import shutil
import subprocess
import time
from datetime import date
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
POST_MERGE_HOOK = REPO_ROOT / "scripts" / "git-hooks" / "post-merge"
NIGHTLY_PLIST = (
    REPO_ROOT / "configs" / "launchd" / "com.omniagentos.merge-truth-nightly.plist"
)

# Plists always point at the production serving-checkout path (launchd
# conventions on this estate). When this lane is being built in a private
# builder worktree that path may not have received the lane's files yet, so
# resolve existence against either the literal absolute path or the
# equivalent relative path inside THIS worktree.
PROD_REPO_ROOT = Path("/Users/youruser/OmniAgentOS")


def _path_exists_in_prod_or_worktree(path_str: str) -> bool:
    candidate = Path(path_str)
    if candidate.exists():
        return True
    try:
        rel = candidate.relative_to(PROD_REPO_ROOT)
    except ValueError:
        return False
    return (REPO_ROOT / rel).exists()


def _extract_ledger_append_shell_command(hook_text: str) -> list[str]:
    """Return the exact lines composing the ledger append's own shell
    LOGICAL command: from the line containing the absolute ledger CLI
    invocation through the LAST line of its backslash-continuation chain
    (the first line that does NOT end with a trailing `\\`).

    This is the shell's own definition of "one logical command" -- a
    guard/redirect on any line AFTER the continuation ends belongs to a
    SEPARATE command and must never be credited to the append's own
    guard, even if it happens to match the same redirect pattern
    elsewhere in the enclosing if-block (D-006, round 3/4)."""
    lines = hook_text.splitlines()
    start = next(i for i, line in enumerate(lines) if '"$LEDGER_BIN" append' in line)
    command: list[str] = []
    for line in lines[start:]:
        command.append(line)
        if not line.rstrip().endswith("\\"):
            break
    return command


def _run(cmd: list[str], cwd: Path, env: dict[str, str] | None = None) -> None:
    full_env = dict(os.environ)
    if env:
        full_env.update(env)
    subprocess.run(cmd, cwd=cwd, env=full_env, check=True, capture_output=True, text=True)


def _init_fixture_repo(repo: Path) -> None:
    repo.mkdir(parents=True, exist_ok=True)
    _run(["git", "init", "-b", "main"], cwd=repo)
    _run(["git", "config", "user.email", "fixture@example.com"], cwd=repo)
    _run(["git", "config", "user.name", "Fixture"], cwd=repo)
    (repo / "README.md").write_text("fixture repo\n", encoding="utf-8")
    _run(["git", "add", "README.md"], cwd=repo)
    _run(
        ["git", "commit", "-m", "initial commit"],
        cwd=repo,
        env={
            "GIT_AUTHOR_DATE": "2026-08-01T09:00:00 +0000",
            "GIT_COMMITTER_DATE": "2026-08-01T09:00:00 +0000",
        },
    )


def _make_merge_commit(repo: Path, branch: str, commit_date: str, seq: int) -> None:
    """Create a one-commit branch off main and merge it back with --no-ff,
    stamping BOTH the feature commit and the merge commit with commit_date
    (an ISO datetime + fixed UTC offset, so the git-truth reading is
    independent of this machine's local timezone)."""
    _run(["git", "checkout", "-b", branch, "main"], cwd=repo)
    fname = f"file-{branch}.txt"
    (repo / fname).write_text(f"seq={seq}\n", encoding="utf-8")
    _run(["git", "add", fname], cwd=repo)
    _run(
        ["git", "commit", "-m", f"work on {branch}"],
        cwd=repo,
        env={"GIT_AUTHOR_DATE": commit_date, "GIT_COMMITTER_DATE": commit_date},
    )
    _run(["git", "checkout", "main"], cwd=repo)
    _run(
        ["git", "merge", "--no-ff", "--no-edit", branch, "-m", f"Merge {branch}"],
        cwd=repo,
        env={"GIT_AUTHOR_DATE": commit_date, "GIT_COMMITTER_DATE": commit_date},
    )


@pytest.fixture
def fixture_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "fixture-repo"
    _init_fixture_repo(repo)
    # 3 merges on 2026-08-05, 2 merges on 2026-08-06.
    for i in range(3):
        _make_merge_commit(repo, f"lane-a{i}", "2026-08-05T10:00:00 +0000", i)
    for i in range(2):
        _make_merge_commit(repo, f"lane-b{i}", "2026-08-06T10:00:00 +0000", i)
    return repo


def test_git_truth_counts_exact_merges_on_right_dates(fixture_repo: Path) -> None:
    from scripts.ops.merge_truth import count_git_merges

    counts = count_git_merges(fixture_repo, date(2026, 8, 4), date(2026, 8, 7))

    assert counts.get("2026-08-05") == 3
    assert counts.get("2026-08-06") == 2
    # No merges recorded on days nothing happened.
    assert counts.get("2026-08-04", 0) == 0
    assert counts.get("2026-08-07", 0) == 0


def test_count_git_merges_raises_distinct_error_for_missing_repo(tmp_path: Path) -> None:
    """D-001: a missing repo must never collapse into a silent {} that a
    caller could misread as 'zero merges, healthy'."""
    from scripts.ops.merge_truth import GitCountError, count_git_merges

    missing = tmp_path / "not-a-repository"
    with pytest.raises(GitCountError):
        count_git_merges(missing, date(2026, 8, 5), date(2026, 8, 5))


def test_build_report_surfaces_git_failure_as_instrument_absence_not_healthy_zero(
    tmp_path: Path,
) -> None:
    """D-001: build_report must not let a failed git query masquerade as a
    healthy zero-merges/agreement report or a healthy-shaped ceiling."""
    from scripts.ops.merge_truth import STATUS_GIT_INSTRUMENT_ABSENT, build_report

    missing_repo = tmp_path / "not-a-repository"
    ledger_dir = tmp_path / "ledger"
    ledger_dir.mkdir()
    (ledger_dir / "ledger-202608.jsonl").write_text(
        json.dumps({"event": "branch_ready", "ts": "2026-08-05T12:00:00+00:00"}) + "\n",
        encoding="utf-8",
    )

    report = build_report(
        [str(missing_repo)], ledger_dir, date(2026, 8, 5), date(2026, 8, 5), 0.10, True
    )

    assert report["git_instrument_present"] is False
    assert report["git_failures"], "a failed repo query must be recorded, not silently dropped"

    day = report["days"][0]
    assert day["status"] == STATUS_GIT_INSTRUMENT_ABSENT
    assert day["status"] not in {"agree", "diverge"}

    ceiling = report["git_derived_throughput_ceiling"]
    assert ceiling["status"] == "instrument_absent", (
        "a failed git query must not produce a healthy-shaped peak=0/mean=0 ceiling"
    )


def test_divergence_flag_fires_when_ledger_and_git_disagree(tmp_path: Path) -> None:
    from scripts.ops.merge_truth import compare_days, scan_ledger_merges

    ledger_dir = tmp_path / "ledger"
    ledger_dir.mkdir()
    ledger_path = ledger_dir / "ledger-202608.jsonl"

    # Git-truth (supplied directly, no repo needed) says 10 merges on Aug-05.
    git_counts = {"2026-08-05": 10}

    # Ledger only recorded 2 merge rows for that day -- an 80% divergence,
    # comfortably over the 10% threshold.
    rows = [
        {
            "event": "merge",
            "ts": "2026-08-05T10:00:00.000000+00:00",
            "refs": {"branch": "lane-x", "sha": "abc123"},
        },
        {
            "event": "merge",
            "ts": "2026-08-05T11:00:00.000000+00:00",
            "refs": {"branch": "lane-y", "sha": "def456"},
        },
        {"event": "branch_ready", "ts": "2026-08-05T09:00:00.000000+00:00"},
    ]
    with ledger_path.open("w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row) + "\n")

    ledger_result = scan_ledger_merges(ledger_dir, date(2026, 8, 5), date(2026, 8, 5))
    assert ledger_result.counts.get("2026-08-05") == 2
    assert ledger_result.absent_months == []

    comparisons = compare_days(git_counts, ledger_result, date(2026, 8, 5), date(2026, 8, 5))
    assert len(comparisons) == 1
    day = comparisons[0]
    assert day.day == "2026-08-05"
    assert day.git_count == 10
    assert day.ledger_count == 2
    assert day.status == "diverge"
    assert day.diff_pct is not None and day.diff_pct > 0.10


def test_agreement_when_counts_match(tmp_path: Path) -> None:
    from scripts.ops.merge_truth import compare_days, scan_ledger_merges

    ledger_dir = tmp_path / "ledger"
    ledger_dir.mkdir()
    ledger_path = ledger_dir / "ledger-202608.jsonl"
    rows = [
        {
            "event": "merge",
            "ts": "2026-08-05T10:00:00.000000+00:00",
            "refs": {"branch": "lane-x", "sha": "abc123"},
        }
    ]
    with ledger_path.open("w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row) + "\n")

    ledger_result = scan_ledger_merges(ledger_dir, date(2026, 8, 5), date(2026, 8, 5))
    comparisons = compare_days({"2026-08-05": 1}, ledger_result, date(2026, 8, 5), date(2026, 8, 5))
    assert comparisons[0].status == "agree"


def test_absent_ledger_reports_instrument_absence_not_agreement(tmp_path: Path) -> None:
    """A missing ledger file for the month must never read as '0 merges,
    agreement' -- it must be flagged distinctly as instrument absence, even
    when git-truth also shows zero merges that day (no favourable absence)."""
    from scripts.ops.merge_truth import compare_days, scan_ledger_merges

    empty_ledger_dir = tmp_path / "no-ledger-here"
    empty_ledger_dir.mkdir()

    ledger_result = scan_ledger_merges(empty_ledger_dir, date(2026, 8, 11), date(2026, 8, 11))
    assert ledger_result.absent_months == ["202608"]
    assert ledger_result.files_read == []

    # git-truth also happens to show zero merges that day.
    comparisons = compare_days({}, ledger_result, date(2026, 8, 11), date(2026, 8, 11))
    assert len(comparisons) == 1
    day = comparisons[0]
    assert day.git_count == 0
    assert day.status == "instrument_absent"
    assert day.status != "agree"
    assert day.ledger_count is None


def test_corrupt_ledger_line_is_skipped_not_fatal(tmp_path: Path) -> None:
    from scripts.ops.merge_truth import scan_ledger_merges

    ledger_dir = tmp_path / "ledger"
    ledger_dir.mkdir()
    ledger_path = ledger_dir / "ledger-202608.jsonl"
    with ledger_path.open("w", encoding="utf-8") as fh:
        fh.write("{not valid json\n")
        fh.write(
            json.dumps(
                {
                    "event": "merge",
                    "ts": "2026-08-05T10:00:00.000000+00:00",
                    "refs": {"branch": "lane-x", "sha": "abc123"},
                }
            )
            + "\n"
        )

    result = scan_ledger_merges(ledger_dir, date(2026, 8, 5), date(2026, 8, 5))
    assert result.corrupt_lines == 1
    assert result.counts.get("2026-08-05") == 1
    assert result.absent_months == []


def test_all_corrupt_ledger_file_is_instrument_absent_not_zero_agreement(tmp_path: Path) -> None:
    """D-002: a file that has lines but every single one fails to parse
    (N>0 attempted, 0 events) is a corrupt/unreadable instrument -- it must
    not count as a valid zero-event ledger that then reads as 'agree'."""
    from scripts.ops.merge_truth import STATUS_INSTRUMENT_ABSENT, compare_days, scan_ledger_merges

    ledger_dir = tmp_path / "ledger"
    ledger_dir.mkdir()
    (ledger_dir / "ledger-202608.jsonl").write_text("{not valid json\n", encoding="utf-8")

    scan = scan_ledger_merges(ledger_dir, date(2026, 8, 5), date(2026, 8, 5))
    assert scan.absent_months == ["202608"], (
        "a file with N>0 non-blank lines and 0 parsed events must be instrument absence"
    )
    assert scan.files_read == []

    day = compare_days({}, scan, date(2026, 8, 5), date(2026, 8, 5))[0]
    assert day.status == STATUS_INSTRUMENT_ABSENT
    assert day.status != "agree"


def test_merge_event_with_invalid_timestamp_counts_as_corruption_not_silent_zero(
    tmp_path: Path,
) -> None:
    """D-002 residual (a): a syntactically valid JSON event claiming to be
    a merge, but carrying an unparseable timestamp, must increment the
    SAME corruption counter as an unparseable line -- it must not be
    silently discarded into a healthy zero-merges/agree read. Fixed at the
    single shared predicate (_classify_ledger_record), not a one-off
    patch in the per-event loop."""
    from scripts.ops.merge_truth import STATUS_INSTRUMENT_ABSENT, compare_days, scan_ledger_merges

    ledger_dir = tmp_path / "ledger"
    ledger_dir.mkdir()
    (ledger_dir / "ledger-202608.jsonl").write_text(
        json.dumps({"event": "merge", "ts": "not-an-iso-timestamp"}) + "\n",
        encoding="utf-8",
    )

    scan = scan_ledger_merges(ledger_dir, date(2026, 8, 5), date(2026, 8, 5))
    assert scan.corrupt_lines == 1, (
        "an unusable merge event must increment the SAME corruption counter as bad JSON"
    )
    assert scan.absent_months == ["202608"]
    assert scan.files_read == []

    day = compare_days({}, scan, date(2026, 8, 5), date(2026, 8, 5))[0]
    assert day.status == STATUS_INSTRUMENT_ABSENT
    assert day.status != "agree"


def test_valid_non_merge_event_is_not_corruption(tmp_path: Path) -> None:
    """A legitimate non-merge event (branch_ready etc.) must NOT count as
    corruption -- it is simply irrelevant to merge-counting, distinct
    from an unusable merge-typed record. Guards against an overzealous
    fix for the residual above that would over-flag ordinary ledger rows."""
    from scripts.ops.merge_truth import STATUS_AGREE, compare_days, scan_ledger_merges

    ledger_dir = tmp_path / "ledger"
    ledger_dir.mkdir()
    (ledger_dir / "ledger-202608.jsonl").write_text(
        json.dumps({"event": "branch_ready", "ts": "2026-08-05T12:00:00+00:00"}) + "\n",
        encoding="utf-8",
    )

    scan = scan_ledger_merges(ledger_dir, date(2026, 8, 5), date(2026, 8, 5))
    assert scan.corrupt_lines == 0
    assert scan.absent_months == []
    assert scan.files_read != []

    day = compare_days({}, scan, date(2026, 8, 5), date(2026, 8, 5))[0]
    assert day.status == STATUS_AGREE


def test_invalid_utf8_ledger_file_is_instrument_absent_never_raises(tmp_path: Path) -> None:
    """D-002 residual (b): read_ledger_file() promises never to raise, but
    previously caught only OSError -- invalid UTF-8 bytes raise
    UnicodeDecodeError (a ValueError subclass, not an OSError), which
    propagated straight through. Must resolve to instrument absence
    instead, exactly like a missing file."""
    from scripts.ops.merge_truth import (
        STATUS_INSTRUMENT_ABSENT,
        compare_days,
        read_ledger_file,
        scan_ledger_merges,
    )

    ledger_dir = tmp_path / "ledger"
    ledger_dir.mkdir()
    ledger_path = ledger_dir / "ledger-202608.jsonl"
    ledger_path.write_bytes(b"\xff\n")

    day_counts, corrupt, total = read_ledger_file(ledger_path)
    assert corrupt == -1 and total == -1, "invalid UTF-8 must resolve to the absence sentinel"
    assert day_counts == {}

    scan = scan_ledger_merges(ledger_dir, date(2026, 8, 5), date(2026, 8, 5))
    assert scan.absent_months == ["202608"]
    assert scan.files_read == []

    day = compare_days({}, scan, date(2026, 8, 5), date(2026, 8, 5))[0]
    assert day.status == STATUS_INSTRUMENT_ABSENT
    assert day.status != "agree"


def test_merge_event_with_overflowing_utc_normalization_timestamp_is_corruption_not_crash(
    tmp_path: Path,
) -> None:
    """D-002 residual (round 3/4): a syntactically valid ISO timestamp near
    datetime.MINYEAR, once normalized to UTC by its own recorded offset,
    can land outside the representable date range and raise OverflowError
    from astimezone() -- e.g. "0001-01-01T00:00:00+23:59" normalizes to
    before year 1. _day_from_iso_ts previously caught only ValueError (and
    the astimezone() call wasn't even inside that try block), so this
    escaped read_ledger_file's documented "Never raises" contract and
    crashed the reader outright instead of becoming instrument absence."""
    from scripts.ops.merge_truth import (
        STATUS_INSTRUMENT_ABSENT,
        _day_from_iso_ts,
        compare_days,
        scan_ledger_merges,
    )

    # Sanity: this specific timestamp really does overflow UTC normalization.
    assert _day_from_iso_ts("0001-01-01T00:00:00+23:59") is None

    ledger_dir = tmp_path / "ledger"
    ledger_dir.mkdir()
    (ledger_dir / "ledger-202608.jsonl").write_text(
        json.dumps({"event": "merge", "ts": "0001-01-01T00:00:00+23:59"}) + "\n",
        encoding="utf-8",
    )

    scan = scan_ledger_merges(ledger_dir, date(2026, 8, 5), date(2026, 8, 5))
    assert scan.corrupt_lines == 1
    assert scan.absent_months == ["202608"]

    day = compare_days({}, scan, date(2026, 8, 5), date(2026, 8, 5))[0]
    assert day.status == STATUS_INSTRUMENT_ABSENT
    assert day.status != "agree"


def test_git_and_ledger_bucket_the_same_merge_instant_into_the_same_utc_day(
    tmp_path: Path,
) -> None:
    """D-003: git buckets days by the commit's own recorded timezone via
    --date=short, while the ledger buckets by its (UTC) timestamp prefix --
    one merge instant near local midnight can land on different calendar
    days on each side and manufacture a false divergence. Both sides must
    bucket in UTC, and this must hold regardless of this process's own
    ambient TZ (set here to America/New_York to prove it)."""
    from scripts.ops.merge_truth import (
        STATUS_AGREE,
        compare_days,
        count_git_merges,
        scan_ledger_merges,
    )

    original_tz = os.environ.get("TZ")
    os.environ["TZ"] = "America/New_York"
    time.tzset()
    try:
        repo = tmp_path / "tz-repo"
        _init_fixture_repo(repo)
        # 2026-08-04T23:30:00 -04:00 (EDT) == 2026-08-05T03:30:00Z.
        _make_merge_commit(repo, "lane-tz", "2026-08-04T23:30:00 -0400", 0)

        ledger_dir = tmp_path / "tz-ledger"
        ledger_dir.mkdir()
        (ledger_dir / "ledger-202608.jsonl").write_text(
            json.dumps({"event": "merge", "ts": "2026-08-05T03:30:00+00:00"}) + "\n",
            encoding="utf-8",
        )

        since, until = date(2026, 8, 4), date(2026, 8, 5)
        git_counts = count_git_merges(repo, since, until)
        ledger_result = scan_ledger_merges(ledger_dir, since, until)
        days = compare_days(git_counts, ledger_result, since, until)
    finally:
        if original_tz is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = original_tz
        time.tzset()

    assert git_counts.get("2026-08-05") == 1, git_counts
    assert git_counts.get("2026-08-04", 0) == 0, git_counts
    assert ledger_result.counts.get("2026-08-05") == 1
    assert all(d.status == STATUS_AGREE for d in days), [d.as_dict() for d in days]


def test_divergence_denominator_is_authoritative_git_count_not_max(tmp_path: Path) -> None:
    """D-004: 111 ledger rows vs 100 git-truth merges is an honest 11%
    over-report. Under the old max(git, ledger) denominator this reads as
    11/111 ~= 9.9%, slipping under the 10% threshold. The denominator must
    be the authoritative git count (100), so 11/100 = 11% correctly trips."""
    from scripts.ops.merge_truth import STATUS_DIVERGE, LedgerScanResult, compare_days

    ledger = LedgerScanResult(counts={"2026-08-05": 111}, files_read=["ledger-202608.jsonl"])
    day = compare_days(
        {"2026-08-05": 100}, ledger, date(2026, 8, 5), date(2026, 8, 5), threshold=0.10
    )[0]
    assert day.diff_pct == pytest.approx(0.11)
    assert day.status == STATUS_DIVERGE


def test_zero_git_count_with_nonzero_ledger_is_full_divergence() -> None:
    """D-004: with git-truth authoritatively zero, a nonzero ledger claim
    has no honest ratio to divide by -- defined explicitly as 100%
    divergence rather than an implicit/undefined denominator."""
    from scripts.ops.merge_truth import STATUS_DIVERGE, LedgerScanResult, compare_days

    ledger = LedgerScanResult(counts={"2026-08-05": 3}, files_read=["ledger-202608.jsonl"])
    day = compare_days({}, ledger, date(2026, 8, 5), date(2026, 8, 5))[0]
    assert day.git_count == 0
    assert day.ledger_count == 3
    assert day.diff_pct == pytest.approx(1.0)
    assert day.status == STATUS_DIVERGE


def test_zero_git_count_and_zero_ledger_count_agree() -> None:
    """D-004: both sides legitimately silent must still agree -- the
    explicit zero-git-count rule must not turn every silent day into a
    manufactured divergence."""
    from scripts.ops.merge_truth import STATUS_AGREE, LedgerScanResult, compare_days

    ledger = LedgerScanResult(counts={}, files_read=["ledger-202608.jsonl"])
    day = compare_days({}, ledger, date(2026, 8, 5), date(2026, 8, 5))[0]
    assert day.git_count == 0
    assert day.ledger_count == 0
    assert day.diff_pct == 0.0
    assert day.status == STATUS_AGREE


def test_recompute_with_absent_ledger_does_not_publish_healthy_zero_ceiling(
    tmp_path: Path,
) -> None:
    """D-005: --recompute with an absent ledger must not publish a
    healthy-shaped peak=0/mean=0/ceiling=0 for the ledger-derived
    comparison figure -- absence must propagate, not become numeric zeros."""
    from scripts.ops.merge_truth import build_report

    repo = tmp_path / "repo"
    _init_fixture_repo(repo)
    missing_ledger_dir = tmp_path / "missing-ledger"

    report = build_report(
        [str(repo)], missing_ledger_dir, date(2026, 8, 5), date(2026, 8, 5), 0.10, True
    )

    assert report["instrument_absent_days"] == ["2026-08-05"]
    ledger_ceiling = report["ledger_derived_throughput_ceiling_for_comparison"]
    assert ledger_ceiling["status"] == "instrument_absent", (
        "an absent ledger must make its derived ceiling explicitly unavailable, "
        "not publish a healthy-shaped peak=0/mean=0/ceiling=0"
    )


def test_ledger_ceiling_goes_instrument_absent_when_any_day_in_window_is_absent(
    tmp_path: Path,
) -> None:
    """D-005 residual: LedgerScanResult.instrument_present is a GLOBAL
    'at least one file read' flag, but a 2-day window straddling a month
    boundary -- one present month, one absent month -- must not average
    the absent day in as a numeric zero. The aggregate ceiling must go
    instrument_absent whenever ANY day in its window lacks its
    instrument, matching the per-day row rather than contradicting it."""
    from scripts.ops.merge_truth import build_report

    ledger_dir = tmp_path / "ledger"
    ledger_dir.mkdir()
    # Only July's file exists; August (2026-08-01) has no ledger file at all.
    (ledger_dir / "ledger-202607.jsonl").write_text(
        json.dumps({"event": "merge", "ts": "2026-07-31T12:00:00+00:00"}) + "\n",
        encoding="utf-8",
    )

    report = build_report(
        [str(REPO_ROOT)], ledger_dir, date(2026, 7, 31), date(2026, 8, 1), 0.10, True
    )

    # Sanity: the GLOBAL flag is exactly the trap -- the ledger instrument
    # DID read something, so a naive global check would call the whole
    # window "present" even though 2026-08-01 has no data at all.
    assert report["ledger_files_read"], "the July file must have been read"
    assert report["days"][0]["day"] == "2026-07-31"
    assert report["days"][0]["status"] != "instrument_absent"
    assert report["days"][1]["day"] == "2026-08-01"
    assert report["days"][1]["status"] == "instrument_absent"

    ceiling = report["ledger_derived_throughput_ceiling_for_comparison"]
    assert ceiling["status"] == "instrument_absent", (
        "a ceiling spanning any absent day/month must be unavailable, not average that day as zero"
    )


def test_ledger_ceiling_is_ok_once_every_day_in_window_has_its_instrument(
    tmp_path: Path,
) -> None:
    """D-005 residual: once the previously-absent month is repaired
    (both months present), the SAME window's ceiling must go back to a
    normal numeric 'ok' status -- the fix must not be a one-way ratchet
    into permanent absence."""
    from scripts.ops.merge_truth import build_report

    ledger_dir = tmp_path / "ledger"
    ledger_dir.mkdir()
    (ledger_dir / "ledger-202607.jsonl").write_text(
        json.dumps({"event": "merge", "ts": "2026-07-31T12:00:00+00:00"}) + "\n",
        encoding="utf-8",
    )
    (ledger_dir / "ledger-202608.jsonl").write_text(
        json.dumps({"event": "merge", "ts": "2026-08-01T12:00:00+00:00"}) + "\n",
        encoding="utf-8",
    )

    report = build_report(
        [str(REPO_ROOT)], ledger_dir, date(2026, 7, 31), date(2026, 8, 1), 0.10, True
    )
    assert report["ledger_absent_months"] == []
    ceiling = report["ledger_derived_throughput_ceiling_for_comparison"]
    assert ceiling["status"] == "ok"
    assert ceiling["mean"] == pytest.approx(1.0)


def test_post_merge_hook_has_guarded_absolute_path_ledger_write() -> None:
    text = POST_MERGE_HOOK.read_text(encoding="utf-8")

    assert "/Users/youruser/Work/Ops/bin/ledger" in text, (
        "hook must reference the ledger CLI by absolute path (stripped PATH in hook env)"
    )
    assert "|| true" in text, "the ledger write must be best-effort / non-blocking"
    assert "event merge" in text or "--event merge" in text


def test_post_merge_hook_ledger_append_redirect_line_itself_ends_with_or_true() -> None:
    """D-006 (round 2): round 1 anchored to "the last non-blank line
    before `fi`" -- itself foolable. Sol's round-2 repro strips the
    append's own `|| true`, then inserts an UNRELATED guarded command
    (`: || true`) as a new last line inside the same if-block, right
    before `fi`; a last-line heuristic reads that injected line as the
    append's own continuation and passes anyway. Anchor to the SPECIFIC
    line carrying the append's own output redirect (`>/dev/null 2>&1`)
    instead of "whatever happens to be last"."""
    text = POST_MERGE_HOOK.read_text(encoding="utf-8")
    start = text.index('"$LEDGER_BIN" append')
    end = text.index("\nfi", start)
    block = text[start:end]

    assert "--event merge" in block

    redirect_lines = [line for line in block.splitlines() if ">/dev/null 2>&1" in line]
    assert redirect_lines, "could not locate the ledger append's own output redirect line"
    assert len(redirect_lines) == 1, (
        f"expected exactly one output-redirect line for the ledger append invocation, "
        f"found {len(redirect_lines)}: {redirect_lines!r}"
    )
    assert redirect_lines[0].strip().endswith("|| true"), (
        "the ledger append's own trailing output-redirect line must itself end with "
        f"`|| true` (best-effort/non-blocking); got: {redirect_lines[0].strip()!r}"
    )


def test_post_merge_hook_redirect_anchor_resists_unrelated_trailing_guard_injection() -> None:
    """Regression, mirroring the round-2 repro's exact attack: strip the
    append's own `|| true`, then insert an unrelated `: || true` as a NEW
    last line inside the same if-block. The redirect-line anchor (unlike
    a last-non-blank-line heuristic) must not be fooled, because the
    injected line does not contain the append's own `>/dev/null 2>&1`
    marker."""
    text = POST_MERGE_HOOK.read_text(encoding="utf-8")

    mutated = text.replace("    >/dev/null 2>&1 || true\n", "    >/dev/null 2>&1\n", 1)
    assert mutated != text, "fixture mutation did not remove the ledger append's own guard"

    start = mutated.index('"$LEDGER_BIN" append')
    end = mutated.index("\nfi", start)
    attacked = mutated[:end] + "\n  : || true" + mutated[end:]

    attacked_start = attacked.index('"$LEDGER_BIN" append')
    attacked_end = attacked.index("\nfi", attacked_start)
    attacked_block = attacked[attacked_start:attacked_end]

    # Sanity: the injected line really is now the last non-blank line --
    # proving a last-line heuristic would have been fooled here.
    non_blank_lines = [line for line in attacked_block.splitlines() if line.strip()]
    assert non_blank_lines[-1].strip() == ": || true"

    redirect_lines = [line for line in attacked_block.splitlines() if ">/dev/null 2>&1" in line]
    assert redirect_lines, "the append's own redirect line must still be locatable after the attack"
    assert not redirect_lines[0].strip().endswith("|| true"), (
        "the redirect-anchored check must correctly report the append's own line as "
        "unguarded, not be distracted by the unrelated injected line"
    )


def test_post_merge_hook_ledger_append_own_logical_command_is_guarded() -> None:
    """D-006 (round 3/4): scanning the WHOLE enclosing if-block for a
    matching redirect line (round-2's fix) is itself foolable -- see the
    regression test below. Anchor to the append's own shell LOGICAL
    command (its backslash-continuation chain) and require the redirect
    and `|| true` to be on THAT chain's own final line."""
    text = POST_MERGE_HOOK.read_text(encoding="utf-8")
    command = _extract_ledger_append_shell_command(text)

    assert any("--event merge" in line for line in command)
    last = command[-1].strip()
    assert ">/dev/null 2>&1" in last and last.endswith("|| true"), (
        "the ledger append's own shell logical command must end, on its OWN final "
        f"continuation line, with a guarded redirect; got: {last!r}"
    )


def test_post_merge_hook_command_boundary_resists_truncated_continuation_spoof() -> None:
    """Regression, mirroring the round-3/4 repro's exact attack: terminate
    the append's own continuation early (drop the trailing backslash on
    the `--refs` line, so the append's shell command ends there WITHOUT
    ever reaching a redirect -- it now runs unguarded under `set -e`),
    then place a spoofed, correctly-guarded `>/dev/null 2>&1 || true` on a
    LATER, unrelated command within the same if-block. A block-scan check
    (round-2's fix: "is there exactly one matching guarded redirect line
    anywhere in the if-block") is fooled by this -- it finds the spoofed
    line and passes. The command-boundary check must not be."""
    text = POST_MERGE_HOOK.read_text(encoding="utf-8")
    lines = text.splitlines()

    refs_index = next(i for i, line in enumerate(lines) if "    --refs " in line)
    redirect_index = next(i for i, line in enumerate(lines) if ">/dev/null 2>&1" in line)
    assert lines[refs_index].rstrip().endswith("\\"), "fixture assumption: --refs continues"

    lines[refs_index] = lines[refs_index].rstrip()[:-1].rstrip()  # drop the trailing backslash
    lines[redirect_index] = "  : >/dev/null 2>&1 || true"  # spoofed, unrelated, guarded no-op
    spoofed = "\n".join(lines) + "\n"

    # Sanity: a block-scoped (non-command-anchored) check really is fooled
    # by this spoof -- proving the attack is real, not a strawman.
    start = spoofed.index('"$LEDGER_BIN" append')
    end = spoofed.index("\nfi", start)
    block = spoofed[start:end]
    block_scan_redirect_lines = [line for line in block.splitlines() if ">/dev/null 2>&1" in line]
    block_scan_passes = (
        "--event merge" in block
        and len(block_scan_redirect_lines) == 1
        and block_scan_redirect_lines[0].strip().endswith("|| true")
    )
    assert block_scan_passes, "sanity: the spoof must fool a block-scoped (non-command-anchored) check"

    command = _extract_ledger_append_shell_command(spoofed)
    last = command[-1].strip()
    command_boundary_passes = (
        any("--event merge" in line for line in command)
        and ">/dev/null 2>&1" in last
        and last.endswith("|| true")
    )
    assert not command_boundary_passes, (
        "the command-boundary check must correctly report the append as unguarded, "
        "not be fooled by a spoofed guarded redirect on a later unrelated command"
    )


def test_post_merge_hook_preserves_existing_archdocs_content() -> None:
    text = POST_MERGE_HOOK.read_text(encoding="utf-8")
    # The pre-existing archdocs-refresh guards must still be present verbatim.
    assert "archi-morning.sh" in text
    assert 'BRANCH = "main"' in text or '"$BRANCH" = "main"' in text
    assert "OMNIAGENTOS_ARCHDOCS_HOOK" in text


def test_nightly_plist_lints_and_references_existing_absolute_paths() -> None:
    assert NIGHTLY_PLIST.exists(), "plist must exist at the owned path"

    plutil = shutil.which("plutil")
    if plutil is None:
        pytest.skip("plutil not available on this host")
    else:
        result = subprocess.run(
            [plutil, "-lint", str(NIGHTLY_PLIST)], capture_output=True, text=True
        )
        assert result.returncode == 0, f"plutil -lint failed: {result.stdout} {result.stderr}"

    with NIGHTLY_PLIST.open("rb") as fh:
        plist = plistlib.load(fh)

    args = plist["ProgramArguments"]
    assert len(args) >= 2

    absolute_path_args = [a for a in args if a.startswith("/")]
    assert absolute_path_args, "ProgramArguments must use absolute paths (stripped launchd PATH)"

    # The interpreter and the target script must both be real, existing
    # absolute paths -- not placeholders.
    interpreter = args[0]
    script = args[1]
    assert interpreter.startswith("/") and _path_exists_in_prod_or_worktree(interpreter), (
        f"interpreter path does not exist: {interpreter}"
    )
    assert script.startswith("/") and _path_exists_in_prod_or_worktree(script), (
        f"script path does not exist: {script}"
    )
    assert script.endswith("merge_truth.py")


def test_recompute_ceiling_uses_git_derived_counts_only() -> None:
    from scripts.ops.merge_truth import compute_throughput_ceiling

    days = ["2026-08-05", "2026-08-06", "2026-08-07"]
    counts = {"2026-08-05": 4, "2026-08-06": 8, "2026-08-07": 0}

    ceiling = compute_throughput_ceiling(counts, days)
    assert ceiling["peak"] == 8
    assert ceiling["days_counted"] == 3
    assert ceiling["mean"] == pytest.approx(4.0)
    assert ceiling["ceiling_multiplier"] == pytest.approx(2.0)


def test_module_import_has_no_side_effect_writes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Importing merge_truth must never write to vault/briefings/ or any
    other tracked path -- runtime report output only happens when main()
    actually runs, with the output directory passed as an argument."""
    import importlib
    import sys

    monkeypatch.delitem(sys.modules, "scripts.ops.merge_truth", raising=False)
    marker_dir = REPO_ROOT / "vault" / "briefings"
    before = set(marker_dir.glob("merge-truth-*")) if marker_dir.exists() else set()

    importlib.import_module("scripts.ops.merge_truth")

    after = set(marker_dir.glob("merge-truth-*")) if marker_dir.exists() else set()
    assert after == before, "importing the module must not write any report files"
