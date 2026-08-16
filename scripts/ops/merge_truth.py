#!/usr/bin/env python3
"""Git-truth merge counter -- fixes the throughput instrument.

The session ledger (~/.omniagentos/ops/session-ledger/ledger-YYYYMM.jsonl) is a
voluntary-discipline log: agents are supposed to `ledger append --event
merge` after landing a lane, but nothing enforces it. That log has been
observed to invert day rankings -- e.g. Aug-5 read as the week's most
productive day (32 ledger merge rows) when git itself shows 7-8 merges (the
week's LOWEST), and on Aug-11 git shows 37 merges while the ledger has zero
merge rows for that day.

This script computes the ground truth straight from git (`git log
--merges`), aggregates it per day, and compares it side by side against
the ledger's own merge-event counts, flagging any day where the two
diverge by more than a configurable threshold (default 10%).

Two independent instruments, two independent absence states. Neither side
is allowed a "favourable absence":

  - A missing/unreadable/undecodable ledger file, OR one whose every
    non-blank line is unusable -- lexically (invalid JSON) or
    semantically (a merge-typed record with no resolvable timestamp) --
    is LEDGER instrument absence, never folded into "0 merges". Both
    corruption kinds are judged by the SAME shared predicate
    (_classify_ledger_record) so neither can slip past the other. An
    absent instrument agreeing with git-truth by coincidence is a false
    green, not a passing day.
  - A missing repo, a nonzero git exit, or a timed-out `git log` is GIT
    instrument absence -- git-truth is supposed to be authoritative, so if
    it cannot be computed we must say so, never silently report a healthy
    "0 merges" (or a 0/0/0-shaped throughput ceiling) instead.
  - An aggregate figure (like a recomputed throughput ceiling) spanning
    several days is itself instrument-absent if ANY day in its window
    lacks its instrument -- never computed over "present days only" while
    silently averaging the absent day in as a numeric zero. See
    compute_throughput_ceiling()'s docstring for the exact rule.

Both sides bucket commits/events by UTC calendar day (git via
`--date=format-local:%Y-%m-%d` with TZ forced to UTC for that one
subprocess call; the ledger via parsing each ISO timestamp's offset and
converting to UTC) -- otherwise a single merge instant near local midnight
can land on different calendar days on each side and manufacture a false
divergence that has nothing to do with actual undercounting.

Stdlib only. Runtime report output (JSON + markdown) is written only when
main() runs, to an --output-dir argument (default vault/briefings/) --
never as an import-time side effect, so tests can point everything at
fixtures without touching a tracked path.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

# ---------------------------------------------------------------------------
# Defaults (all overridable via CLI args so tests can point at fixtures)
# ---------------------------------------------------------------------------

DEFAULT_REPOS: list[str] = ["/Users/youruser/OmniAgentOS"]
DEFAULT_LEDGER_DIR = Path("/Users/youruser/Work/Ops/session-ledger")
DEFAULT_OUTPUT_DIR = Path("/Users/youruser/OmniAgentOS/vault/briefings")
DEFAULT_WINDOW_DAYS = 7

DIVERGENCE_THRESHOLD = 0.10  # >10% divergence between git-truth and ledger

STATUS_AGREE = "agree"
STATUS_DIVERGE = "diverge"
STATUS_INSTRUMENT_ABSENT = "instrument_absent"  # ledger-side absence
STATUS_GIT_INSTRUMENT_ABSENT = "git_instrument_absent"  # git-side absence


class GitCountError(Exception):
    """Raised when git-truth cannot be computed for a repo.

    Callers must treat this as instrument absence, never silently fall
    back to "0 merges" -- an unreadable repo and a repo with zero merges
    are not the same fact.
    """


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------


@dataclass
class LedgerScanResult:
    """Result of scanning one or more monthly ledger files for merge events."""

    counts: dict[str, int] = field(default_factory=dict)
    absent_months: list[str] = field(default_factory=list)
    corrupt_lines: int = 0
    files_read: list[str] = field(default_factory=list)

    @property
    def instrument_present(self) -> bool:
        """GLOBAL signal: at least one ledger file in this scan was read.

        This stays True even if the scan also spans OTHER months that are
        absent -- e.g. a 2-day window straddling a month boundary where
        only one of the two months' files exists. It answers "did the
        ledger instrument produce ANYTHING at all", not "is every day in
        my specific window backed by data". An aggregate figure computed
        over a window (see compute_throughput_ceiling's caller in
        build_report) must NOT use this flag directly -- use
        _ledger_window_fully_present(self, since, until) instead, or it
        will silently average an absent day in as a numeric zero while
        that same day's per-day row correctly reads instrument_absent.
        """
        return len(self.files_read) > 0


@dataclass
class GitScanResult:
    """Result of counting git-truth merges across one or more repos."""

    counts: dict[str, int] = field(default_factory=dict)
    failed_repos: list[dict] = field(default_factory=list)  # [{"repo", "reason"}]

    @property
    def instrument_present(self) -> bool:
        return len(self.failed_repos) == 0


@dataclass
class DayComparison:
    """One day's git-truth vs ledger merge-count comparison."""

    day: str
    git_count: int
    ledger_count: int | None  # None == ledger instrument absent for this day
    status: str  # STATUS_AGREE | STATUS_DIVERGE | STATUS_INSTRUMENT_ABSENT | STATUS_GIT_INSTRUMENT_ABSENT
    diff_pct: float | None

    def as_dict(self) -> dict:
        return {
            "day": self.day,
            "git_count": self.git_count,
            "ledger_count": self.ledger_count,
            "status": self.status,
            "diff_pct": self.diff_pct,
        }


# ---------------------------------------------------------------------------
# Date helpers
# ---------------------------------------------------------------------------


def daterange(since: date, until: date) -> Iterator[date]:
    """Yield each date from since to until, inclusive."""
    current = since
    one_day = timedelta(days=1)
    while current <= until:
        yield current
        current += one_day


def _parse_date_arg(value: str) -> date:
    return datetime.strptime(value, "%Y-%m-%d").date()


def _day_from_iso_ts(ts: str) -> str | None:
    """Extract the UTC calendar day from an ISO-8601 timestamp.

    A timestamp carrying a non-UTC offset (e.g. "+05:00") is converted to
    UTC before taking the date -- bucketing must match the git side, which
    also normalizes to UTC. A naive (offset-less) timestamp is assumed to
    already be UTC, since every observed ledger row carries an explicit
    offset.

    Never raises -- this predicate must fail closed (return None, which
    _classify_ledger_record turns into corruption evidence) on ANY
    unparseable or unrepresentable timestamp rather than crashing the
    reader. The except tuple is deliberately (ValueError, OverflowError),
    not a bare Exception, and each member is justified:
      - ValueError: fromisoformat() raises this for a malformed string,
        and also for a numeric offset datetime.timezone itself refuses to
        construct (offsets must be within +/-24h).
      - OverflowError: a SYNTACTICALLY VALID ISO timestamp near
        datetime.MINYEAR/MAXYEAR, once shifted to UTC by its own recorded
        offset, can land outside the representable date range -- e.g.
        "0001-01-01T00:00:00+23:59" normalizes to a moment before year 1.
        This is raised by the astimezone() arithmetic itself, not by
        fromisoformat(), which is why the call is inside this same try
        block rather than guarded separately.
    No other exception class is expected to escape this pair for a str
    input: the only tzinfo ever in play is the stdlib datetime.timezone
    that fromisoformat() itself constructs from the offset text (never an
    external/exotic tzinfo implementation with its own failure modes),
    and every call site passes str(ts) explicitly, so a non-str TypeError
    cannot originate from this function's own argument.
    """
    try:
        parsed = datetime.fromisoformat(ts)
        if parsed.tzinfo is not None:
            parsed = parsed.astimezone(UTC)
    except (ValueError, OverflowError):
        return None
    return parsed.date().isoformat()


def _months_spanned(since: date, until: date) -> list[str]:
    months: list[str] = []
    seen: set[str] = set()
    for day in daterange(since, until):
        key = day.strftime("%Y%m")
        if key not in seen:
            seen.add(key)
            months.append(key)
    return months


def _ledger_window_fully_present(
    ledger_result: LedgerScanResult, since: date, until: date
) -> bool:
    """True only if EVERY day in [since, until] belongs to a month whose
    ledger file was successfully read (i.e. not in `absent_months`).

    Rule (documented once, here, and used everywhere a window-level ledger
    figure is derived): an aggregate computed over a window is
    instrument_absent if ANY day in that window lacks its instrument --
    never "present days only, averaged as if the absent day were zero".
    `LedgerScanResult.instrument_present` is a coarser GLOBAL flag ("did
    the scan read anything at all") that must not be used for this
    purpose -- see its own docstring.
    """
    absent = set(ledger_result.absent_months)
    return not any(day.strftime("%Y%m") in absent for day in daterange(since, until))


# ---------------------------------------------------------------------------
# Git-truth counting
# ---------------------------------------------------------------------------


def count_git_merges(repo: str | Path, since: date, until: date) -> dict[str, int]:
    """Count merge commits per day in `repo` between since and until (inclusive).

    Runs `git -C <repo> log --merges --since=<since> --until=<until+1d>
    --format=%cd --date=format-local:%Y-%m-%d` with TZ forced to UTC for
    that one subprocess call, so days are bucketed in UTC regardless of
    the commit's own recorded timezone or this process's ambient TZ --
    matching how the ledger side buckets its timestamps.

    Raises GitCountError (never returns a silent {}) if the repo path does
    not exist, git cannot be invoked, the invocation times out, or git
    exits nonzero. A missing/unreadable git-truth source is not the same
    fact as "zero merges" and must never be conflated with it.
    """
    repo_path = Path(repo)
    if not repo_path.exists():
        raise GitCountError(f"repo path does not exist: {repo_path}")

    # git's --until is exclusive of the bare date's midnight, so push it one
    # day forward to make the `until` day inclusive.
    until_exclusive = (until + timedelta(days=1)).isoformat()
    cmd = [
        "git",
        "-C",
        str(repo_path),
        "log",
        "--merges",
        f"--since={since.isoformat()}",
        f"--until={until_exclusive}",
        "--format=%cd",
        "--date=format-local:%Y-%m-%d",
    ]
    env = dict(os.environ)
    env["TZ"] = "UTC"  # forces --date=format-local (and --since/--until) into UTC
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
            env=env,
        )
    except OSError as exc:
        raise GitCountError(f"failed to invoke git for {repo_path}: {exc}") from exc
    except subprocess.TimeoutExpired as exc:
        raise GitCountError(f"git log timed out after 60s for {repo_path}: {exc}") from exc

    if proc.returncode != 0:
        stderr_tail = proc.stderr.strip()[:200]
        raise GitCountError(f"git log exited {proc.returncode} for {repo_path}: {stderr_tail}")

    counts: dict[str, int] = {}
    for line in proc.stdout.splitlines():
        day = line.strip()
        if not day:
            continue
        counts[day] = counts.get(day, 0) + 1
    return counts


def count_git_merges_multi(repos: list[str | Path], since: date, until: date) -> GitScanResult:
    """Aggregate count_git_merges() across multiple repos.

    A repo that fails is recorded in `failed_repos` rather than silently
    contributing zero -- the aggregate `instrument_present` is False if
    ANY repo failed, since a partial git-truth read cannot be trusted as
    complete (fail closed, never a favourable absence).
    """
    result = GitScanResult()
    for repo in repos:
        try:
            counts = count_git_merges(repo, since, until)
        except GitCountError as exc:
            result.failed_repos.append({"repo": str(repo), "reason": str(exc)})
            continue
        for day, n in counts.items():
            result.counts[day] = result.counts.get(day, 0) + n
    return result


# ---------------------------------------------------------------------------
# Ledger scanning
# ---------------------------------------------------------------------------


def _classify_ledger_record(parsed: object) -> tuple[str, str | None]:
    """Classify one JSON-decoded ledger record. THE single shared
    predicate every call site must use to decide "is this record usable"
    -- lexical corruption (invalid JSON, handled by the caller before this
    is even reached) and semantic corruption (a merge-typed record with no
    resolvable UTC day) must both feed the SAME corruption count. No call
    site may special-case one kind of unusable record and let the other
    slide silently into "0 merges, agree".

    Returns (kind, day):
      - ("other", None): valid JSON, not a merge event we track -- NOT
        corruption, just irrelevant to merge-counting (e.g. branch_ready).
      - ("merge", <UTC day>): a usable merge event with a resolvable day.
      - ("corrupt", None): syntactically valid JSON that is still not a
        usable record -- not a dict, OR a merge-typed event whose
        timestamp is missing or cannot be resolved to a UTC day.
    """
    if not isinstance(parsed, dict):
        return "corrupt", None
    if parsed.get("event") != "merge":
        return "other", None
    ts = parsed.get("ts") or parsed.get("at")
    if not ts:
        return "corrupt", None
    day = _day_from_iso_ts(str(ts))
    if day is None:
        return "corrupt", None
    return "merge", day


def read_ledger_file(path: Path) -> tuple[dict[str, int], int, int]:
    """Read a line-delimited-JSON ledger file, classifying every non-blank
    line through the single shared _classify_ledger_record() predicate.

    Returns (day_counts, corrupt_line_count, total_line_count):
      - day_counts: UTC calendar day -> usable-merge-event count, for
        THIS FILE only (not yet filtered to any [since, until] window --
        the caller does that).
      - corrupt_line_count: non-blank lines that produced no usable
        record, lexically (invalid JSON) OR semantically (a merge-typed
        event with an unusable timestamp) -- both count identically. -1
        is the absence sentinel (see below).
      - total_line_count: non-blank lines attempted; -1 alongside the
        corrupt sentinel. Lets the caller distinguish a genuinely empty
        ledger file (0 lines -- a legitimate "nothing happened" record)
        from one whose lines were all unusable.

    A missing file, a directory, an OS-level read failure, or a file that
    is not valid UTF-8 all return ({}, -1, -1) -- the -1 sentinel
    distinguishes "instrument absent" from "instrument present, zero
    corrupt lines". Never raises: invalid UTF-8 previously escaped this
    contract because UnicodeDecodeError (a ValueError subclass) is not an
    OSError and was not caught.
    """
    if not path.exists() or not path.is_file():
        return {}, -1, -1
    try:
        raw = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return {}, -1, -1

    day_counts: dict[str, int] = {}
    corrupt = 0
    total = 0
    for line in raw.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        total += 1
        try:
            parsed = json.loads(stripped)
        except json.JSONDecodeError:
            corrupt += 1
            continue

        kind, day = _classify_ledger_record(parsed)
        if kind == "merge" and day is not None:
            day_counts[day] = day_counts.get(day, 0) + 1
        elif kind == "corrupt":
            corrupt += 1
        # kind == "other": valid JSON, irrelevant to merge-counting --
        # neither corruption evidence nor a countable merge.

    return day_counts, corrupt, total


def scan_ledger_merges(ledger_dir: str | Path, since: date, until: date) -> LedgerScanResult:
    """Scan the monthly ledger file(s) covering [since, until] for merge events.

    A month is recorded in `absent_months` -- days in that month are
    ledger-instrument-absent, never silently treated as zero merges --
    when its ledger-YYYYMM.jsonl file is missing/unreadable/undecodable
    (read_ledger_file's -1 sentinel), OR when the file produced ZERO
    usable merge events AND had at least one non-blank line that was
    unusable (lexically OR semantically -- see _classify_ledger_record).
    A file that is genuinely empty (0 lines), or that contains only valid
    non-merge events, is NOT absent: it legitimately recorded zero merges
    that day, which is a fact, not a corrupt instrument.
    """
    ledger_path = Path(ledger_dir)
    result = LedgerScanResult()

    for month_key in _months_spanned(since, until):
        file_path = ledger_path / f"ledger-{month_key}.jsonl"
        day_counts, corrupt, _total = read_ledger_file(file_path)

        if corrupt == -1:
            result.absent_months.append(month_key)
            continue

        usable_merge_events = sum(day_counts.values())
        if corrupt > 0 and usable_merge_events == 0:
            # Every non-blank line in this month's file was unusable
            # (lexically or semantically) and produced zero usable merge
            # signal: the instrument is present but unreadable, not "zero
            # events". Must not be scored as agreement-by-coincidence.
            result.absent_months.append(month_key)
            result.corrupt_lines += corrupt
            continue

        result.files_read.append(str(file_path))
        result.corrupt_lines += corrupt

        for day, n in day_counts.items():
            if since.isoformat() <= day <= until.isoformat():
                result.counts[day] = result.counts.get(day, 0) + n

    return result


# ---------------------------------------------------------------------------
# Comparison
# ---------------------------------------------------------------------------


def compare_days(
    git_counts: dict[str, int],
    ledger_result: LedgerScanResult,
    since: date,
    until: date,
    threshold: float = DIVERGENCE_THRESHOLD,
    git_instrument_present: bool = True,
) -> list[DayComparison]:
    """Build a per-day comparison of git-truth vs ledger merge counts.

    Precedence, most authoritative absence first:

      1. If `git_instrument_present` is False (git-truth could not be
         computed for this window), every day is STATUS_GIT_INSTRUMENT_ABSENT
         regardless of the ledger -- we cannot claim agreement OR
         divergence against a git-truth number we don't actually have.
      2. Else, a day whose month is in `ledger_result.absent_months` is
         STATUS_INSTRUMENT_ABSENT, regardless of the (trustworthy)
         git-truth count for that day.
      3. Else, compare normally. The divergence denominator is the
         AUTHORITATIVE git count, never max(git, ledger) -- a ledger that
         over-reports by exactly the threshold must trip it. When
         git_count is 0: agree only if ledger_count is also 0 (both
         legitimately silent); otherwise the ledger claiming merges that
         git-truth says never happened is defined as 100% divergence
         (there is no honest percentage of zero to divide by).
    """
    absent_months = set(ledger_result.absent_months)
    comparisons: list[DayComparison] = []

    for day in daterange(since, until):
        day_iso = day.isoformat()
        month_key = day.strftime("%Y%m")
        git_count = git_counts.get(day_iso, 0)

        if not git_instrument_present:
            ledger_count_display = (
                None if month_key in absent_months else ledger_result.counts.get(day_iso, 0)
            )
            comparisons.append(
                DayComparison(
                    day=day_iso,
                    git_count=git_count,
                    ledger_count=ledger_count_display,
                    status=STATUS_GIT_INSTRUMENT_ABSENT,
                    diff_pct=None,
                )
            )
            continue

        if month_key in absent_months:
            comparisons.append(
                DayComparison(
                    day=day_iso,
                    git_count=git_count,
                    ledger_count=None,
                    status=STATUS_INSTRUMENT_ABSENT,
                    diff_pct=None,
                )
            )
            continue

        ledger_count = ledger_result.counts.get(day_iso, 0)
        if git_count == 0:
            if ledger_count == 0:
                diff_pct = 0.0
                status = STATUS_AGREE
            else:
                # git-truth (authoritative) says zero; the ledger claims
                # otherwise. There is no honest ratio against a zero
                # denominator, so this is defined as maximal (100%)
                # divergence rather than an undefined/favourable value.
                diff_pct = 1.0
                status = STATUS_DIVERGE if diff_pct > threshold else STATUS_AGREE
        else:
            diff_pct = abs(git_count - ledger_count) / git_count
            status = STATUS_DIVERGE if diff_pct > threshold else STATUS_AGREE

        comparisons.append(
            DayComparison(
                day=day_iso,
                git_count=git_count,
                ledger_count=ledger_count,
                status=status,
                diff_pct=round(diff_pct, 4),
            )
        )

    return comparisons


# ---------------------------------------------------------------------------
# Throughput ceiling recompute
# ---------------------------------------------------------------------------


def compute_throughput_ceiling(
    counts: dict[str, int], days: list[str], instrument_present: bool = True
) -> dict:
    """Recompute the throughput ceiling multiplier (peak day / mean day).

    If `instrument_present` is False, the source data cannot be trusted
    for this window: return an explicit instrument_absent status instead
    of a numerically healthy-looking peak=0/mean=0/ceiling=0 -- absence
    must never masquerade as "we checked, and it was zero".

    Rule for what the caller must pass as `instrument_present`: it is a
    WINDOW-level fact, not a global one. An aggregate spanning N days is
    instrument_absent if ANY of those N days lacks its source instrument
    -- never "compute over the present days only and silently treat the
    absent day as a numeric zero". Per-day and aggregate status must never
    contradict each other favourably. For the ledger side specifically,
    the caller must NOT pass LedgerScanResult.instrument_present directly
    (that property is a coarser GLOBAL "read at least one file" flag,
    documented on the property itself) -- use
    _ledger_window_fully_present(ledger_result, since, until) instead, so
    a window straddling a month boundary with one absent month still
    reports instrument_absent even though some OTHER month was read.
    """
    if not instrument_present:
        return {"status": "instrument_absent", "days_counted": 0}

    values = [counts.get(d, 0) for d in days]
    if not values:
        return {
            "status": "ok",
            "peak": 0,
            "mean": 0.0,
            "ceiling_multiplier": 0.0,
            "days_counted": 0,
        }

    peak = max(values)
    mean = sum(values) / len(values)
    ceiling_multiplier = (peak / mean) if mean > 0 else 0.0
    return {
        "status": "ok",
        "peak": peak,
        "mean": round(mean, 3),
        "ceiling_multiplier": round(ceiling_multiplier, 3),
        "days_counted": len(values),
    }


# ---------------------------------------------------------------------------
# Report assembly + rendering
# ---------------------------------------------------------------------------


def build_report(
    repos: list[str],
    ledger_dir: Path,
    since: date,
    until: date,
    threshold: float,
    recompute: bool,
) -> dict:
    git_scan = count_git_merges_multi(repos, since, until)
    ledger_result = scan_ledger_merges(ledger_dir, since, until)
    comparisons = compare_days(
        git_scan.counts,
        ledger_result,
        since,
        until,
        threshold=threshold,
        git_instrument_present=git_scan.instrument_present,
    )
    days = [d.isoformat() for d in daterange(since, until)]

    report: dict = {
        "generated_at": datetime.now(UTC).isoformat(),
        "since": since.isoformat(),
        "until": until.isoformat(),
        "threshold": threshold,
        "repos": repos,
        "git_instrument_present": git_scan.instrument_present,
        "git_failures": git_scan.failed_repos,
        "ledger_dir": str(ledger_dir),
        "ledger_files_read": ledger_result.files_read,
        "ledger_absent_months": ledger_result.absent_months,
        "ledger_corrupt_lines": ledger_result.corrupt_lines,
        "days": [c.as_dict() for c in comparisons],
        "divergent_days": [c.day for c in comparisons if c.status == STATUS_DIVERGE],
        "instrument_absent_days": [
            c.day for c in comparisons if c.status == STATUS_INSTRUMENT_ABSENT
        ],
        "git_instrument_absent_days": [
            c.day for c in comparisons if c.status == STATUS_GIT_INSTRUMENT_ABSENT
        ],
    }

    if recompute:
        # git_scan.instrument_present is safe to use as-is here: a repo's
        # git-truth query either succeeds for the WHOLE requested window
        # or fails once (raises GitCountError) -- there is no "some days
        # present, some absent" case on the git side to reconcile.
        report["git_derived_throughput_ceiling"] = compute_throughput_ceiling(
            git_scan.counts, days, instrument_present=git_scan.instrument_present
        )
        # For comparison only -- the recomputed ceiling above is the
        # authoritative one; this is retained purely to show the old
        # ledger-derived figure was standing on inverted (or absent) data.
        #
        # Uses _ledger_window_fully_present(), NOT
        # ledger_result.instrument_present: the ledger scans per MONTH, so
        # a window straddling a month boundary can have one month present
        # and another absent while the GLOBAL "read anything at all" flag
        # stays True. The aggregate must not contradict a per-day
        # instrument_absent row by silently averaging that day in as zero.
        report["ledger_derived_throughput_ceiling_for_comparison"] = compute_throughput_ceiling(
            ledger_result.counts,
            days,
            instrument_present=_ledger_window_fully_present(ledger_result, since, until),
        )

    return report


def render_markdown(report: dict) -> str:
    lines = [
        "# Merge-truth report",
        "",
        f"Generated: {report['generated_at']}",
        f"Window: {report['since']} .. {report['until']} (threshold {report['threshold']:.0%})",
        f"Repos: {', '.join(report['repos'])}",
        "",
        "| Day | Git-truth | Ledger | Status | Diff |",
        "| --- | --- | --- | --- | --- |",
    ]
    for day in report["days"]:
        ledger_display = "ABSENT" if day["ledger_count"] is None else str(day["ledger_count"])
        diff_display = "-" if day["diff_pct"] is None else f"{day['diff_pct']:.0%}"
        lines.append(
            f"| {day['day']} | {day['git_count']} | {ledger_display} | "
            f"{day['status']} | {diff_display} |"
        )

    if report["divergent_days"]:
        lines.append("")
        lines.append(
            f"**Divergent days (>{report['threshold']:.0%}):** " + ", ".join(report["divergent_days"])
        )
    if report["instrument_absent_days"]:
        lines.append("")
        lines.append(
            "**Ledger instrument-absent days (file missing/corrupt):** "
            + ", ".join(report["instrument_absent_days"])
        )
    if report.get("git_instrument_absent_days"):
        lines.append("")
        lines.append(
            "**Git instrument-absent days (repo/git query failed):** "
            + ", ".join(report["git_instrument_absent_days"])
        )
    if report.get("git_failures"):
        lines.append("")
        lines.append("**Git failures:**")
        for failure in report["git_failures"]:
            lines.append(f"  - {failure['repo']}: {failure['reason']}")

    if "git_derived_throughput_ceiling" in report:
        git_ceiling = report["git_derived_throughput_ceiling"]
        ledger_ceiling = report["ledger_derived_throughput_ceiling_for_comparison"]
        lines.append("")
        lines.append("## Throughput ceiling recompute")
        if git_ceiling.get("status") == "instrument_absent":
            lines.append("- Git-derived (authoritative): INSTRUMENT ABSENT -- not computable this window")
        else:
            lines.append(
                f"- Git-derived (authoritative): peak={git_ceiling['peak']} "
                f"mean={git_ceiling['mean']} ceiling={git_ceiling['ceiling_multiplier']}x "
                f"over {git_ceiling['days_counted']} days"
            )
        if ledger_ceiling.get("status") == "instrument_absent":
            lines.append("- Ledger-derived (for comparison only): INSTRUMENT ABSENT this window")
        else:
            lines.append(
                f"- Ledger-derived (for comparison only, may be wrong): "
                f"peak={ledger_ceiling['peak']} mean={ledger_ceiling['mean']} "
                f"ceiling={ledger_ceiling['ceiling_multiplier']}x"
            )

    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--repos",
        nargs="+",
        default=list(DEFAULT_REPOS),
        help="Tracked repo(s) to derive git-truth merge counts from (default: %(default)s)",
    )
    parser.add_argument(
        "--ledger-dir",
        type=Path,
        default=DEFAULT_LEDGER_DIR,
        help="Directory containing ledger-YYYYMM.jsonl files (default: %(default)s)",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=DEFAULT_OUTPUT_DIR,
        help="Where to write the runtime JSON/markdown report (default: %(default)s)",
    )
    parser.add_argument(
        "--since",
        type=_parse_date_arg,
        default=None,
        help="Start date YYYY-MM-DD (default: %(default)s -> until minus 6 days)",
    )
    parser.add_argument(
        "--until",
        type=_parse_date_arg,
        default=None,
        help="End date YYYY-MM-DD, inclusive (default: today)",
    )
    parser.add_argument(
        "--threshold",
        type=float,
        default=DIVERGENCE_THRESHOLD,
        help="Divergence fraction that trips the flag (default: %(default)s)",
    )
    parser.add_argument(
        "--recompute",
        action="store_true",
        help="Recompute the throughput ceiling from git-derived counts only",
    )
    parser.add_argument(
        "--format",
        choices=["json", "markdown", "both"],
        default="both",
        help="Which report file(s) to write (default: %(default)s)",
    )
    parser.add_argument(
        "--no-write",
        action="store_true",
        help="Print the comparison only; do not write any report file",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)

    until = args.until or date.today()
    since = args.since or (until - timedelta(days=DEFAULT_WINDOW_DAYS - 1))

    report = build_report(
        repos=args.repos,
        ledger_dir=args.ledger_dir,
        since=since,
        until=until,
        threshold=args.threshold,
        recompute=args.recompute,
    )

    markdown = render_markdown(report)
    print(markdown)

    if not report["git_instrument_present"]:
        print(
            f"WARNING: git instrument absent for {len(report['git_failures'])} repo(s): "
            + "; ".join(f"{f['repo']} ({f['reason']})" for f in report["git_failures"]),
            file=sys.stderr,
        )
    if report["instrument_absent_days"]:
        print(
            f"WARNING: ledger instrument absent for "
            f"{len(report['instrument_absent_days'])} day(s): "
            f"{', '.join(report['instrument_absent_days'])}",
            file=sys.stderr,
        )
    if report["divergent_days"]:
        print(
            f"WARNING: {len(report['divergent_days'])} day(s) diverge >"
            f"{args.threshold:.0%} between git-truth and the ledger: "
            f"{', '.join(report['divergent_days'])}",
            file=sys.stderr,
        )

    if not args.no_write:
        output_dir = Path(args.output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)
        stamp = f"{since.isoformat()}_{until.isoformat()}"
        if args.format in ("json", "both"):
            json_path = output_dir / f"merge-truth-{stamp}.json"
            json_path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
            print(f"JSON report: {json_path}", file=sys.stderr)
        if args.format in ("markdown", "both"):
            md_path = output_dir / f"merge-truth-{stamp}.md"
            md_path.write_text(markdown, encoding="utf-8")
            print(f"Markdown report: {md_path}", file=sys.stderr)

    return 0


if __name__ == "__main__":
    sys.exit(main())
