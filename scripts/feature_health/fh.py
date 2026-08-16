#!/usr/bin/env python3
"""Feature-health lane ledger CLI (NOT part of the omniagentos package).

Subcommands:
  append        parse a pytest JUnit XML (and/or Playwright JSON) and append one
                feature-health.v1 record per (tier, feature) to the monthly ledger
                shard var/feature-health/ledger-YYYYMM.jsonl (flock+fsync append,
                mirroring omniagentos/ledger/__init__.py). A missing or unparseable
                report appends {status:"error"} records for every feature the tier
                covers — did-not-run is not a pass.
  summary       per-feature x per-tier grid from the newest record per (feature,tier)
                across all shards, with freshness and expected-vs-unexpected counts.
  issues        open failures grouped by feature (expected hidden by default) plus
                promotion flags for fh_known_issue tests that are now passing.
  render-latest regenerate var/feature-health/LATEST.md.
  paths         print a tier's matrix paths that exist on disk (used by run.sh to
                build pytest args; missing paths are recorded by append instead).
  abort         append one did-not-run record per tier when the lane bailed out
                BEFORE producing any report (interpreter missing, contention
                guard). A scheduled run that wrote nothing is indistinguishable
                from a scheduler that never fired, so an abort must still leave a
                dated, failure-marked line.
  freshness     the lane's activation oracle: exit non-zero when the newest
                ledger record is older than --max-age-s. run.sh exits 0 in
                --runner launchd mode by design, so exit codes prove nothing and
                freshness is the only honest signal. --runner launchd restricts
                the check to SCHEDULED records, so a human's manual run cannot
                mask a dead scheduler.

Docs: docs/testing/FEATURE-HEALTH.md. Matrix: configs/feature-health.yaml.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from datetime import UTC, datetime
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[2]
SCHEMA = "feature-health.v1"
TIERS = ("tier1", "tier2", "tier3")
_MSG_CAP = 400
# Pseudo-feature for records that describe the LANE ITSELF rather than any one
# feature (an abort before any test ran). It is deliberately not a matrix key, so
# it can never be confused with real coverage; the grid renders it as its own row.
LANE_FEATURE = "__lane__"


def var_dir() -> Path:
    return Path(os.environ.get("FH_VAR_DIR") or (REPO / "var" / "feature-health"))


def sidecar_path() -> Path:
    override = os.environ.get("FH_KNOWN_ISSUES_SIDECAR")
    if override:
        return Path(override)
    return var_dir() / "known-issues-collected.json"


def load_matrix() -> dict[str, dict[str, object]]:
    """Feature -> {tier1/2/3: [paths], playwright: bool}; insertion order == file order."""
    data = yaml.safe_load((REPO / "configs" / "feature-health.yaml").read_text())
    features: dict[str, dict[str, object]] = {}
    for name, tiers in (data.get("features") or {}).items():
        tiers = tiers or {}
        features[name] = {t: list(tiers.get(t) or []) for t in TIERS}
        features[name]["playwright"] = bool(tiers.get("playwright"))
    return features


def load_sidecar() -> dict[str, str]:
    path = sidecar_path()
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return {}
    return {str(k): str(v) for k, v in data.items()} if isinstance(data, dict) else {}


# --------------------------------------------------------------------------- utils


def _now() -> datetime:
    return datetime.now(UTC)


def _git_sha() -> str:
    try:
        out = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=REPO, capture_output=True, text=True, timeout=10
        )
        return out.stdout.strip() if out.returncode == 0 else "unknown"
    except OSError:
        return "unknown"


def _git_dirty() -> int:
    try:
        out = subprocess.run(
            ["git", "status", "--porcelain"], cwd=REPO, capture_output=True, text=True, timeout=30
        )
        if out.returncode != 0:
            return -1
        return sum(1 for line in out.stdout.splitlines() if line.strip())
    except OSError:
        return -1


# Roots whose untracked content is an EXECUTABLE input to the lane (code the
# test run could actually exercise), as opposed to scratch/report artifacts
# (var/, .venv/, node_modules/, coverage output...). An untracked file under
# one of these is exactly as unsafe as a tracked-and-modified one: the run may
# have exercised code that git has no committed record of at all.
_PROVENANCE_EXECUTABLE_ROOTS = ("omniagentos/", "pipeline/", "scripts/", "tests/")


def _is_executable_input(path: str) -> bool:
    return any(path.startswith(root) for root in _PROVENANCE_EXECUTABLE_ROOTS)


def _git_status_lines() -> list[str] | None:
    """Raw ``git status --porcelain`` lines, or ``None`` when git itself failed.

    ``None`` is a distinct outcome from "nothing changed" (an empty list) —
    callers must not treat a failed status read as a clean tree.
    """
    try:
        out = subprocess.run(
            ["git", "status", "--porcelain"], cwd=REPO, capture_output=True, text=True, timeout=30
        )
    except OSError:
        return None
    if out.returncode != 0:
        return None
    return [line for line in out.stdout.splitlines() if line.strip()]


def classify_git_status(lines: list[str] | None) -> dict[str, list[str]] | None:
    """Porcelain lines -> {tracked_dirty, untracked_executable, untracked_other}.

    A scalar dirty COUNT (the pre-existing ``git_dirty`` field) cannot tell a
    tracked source change from an ignored runtime artifact, so it can neither
    condemn real dirt nor clear a checkout that only has scratch files lying
    around. This classification is what lets a filer make that distinction.
    """
    if lines is None:
        return None
    tracked: list[str] = []
    untracked_exec: list[str] = []
    untracked_other: list[str] = []
    for line in lines:
        status = line[:2]
        path = line[3:].strip()
        if " -> " in path:  # rename: "R  old -> new" — the new path is what exists now
            path = path.split(" -> ", 1)[1]
        if status.startswith("??"):
            (untracked_exec if _is_executable_input(path) else untracked_other).append(path)
        else:
            tracked.append(path)
    return {
        "tracked_dirty": sorted(set(tracked)),
        "untracked_executable": sorted(set(untracked_exec)),
        "untracked_other": sorted(set(untracked_other)),
    }


def provenance_snapshot() -> dict[str, object]:
    """One point-in-time snapshot of exactly what could have been measured.

    Taken twice per run (start, before the first test process; end, at append
    time) so a record can prove the tree did not move UNDER the run, not just
    that it looked clean at one instant. ``digest`` is a canonical hash over
    ``sha`` + the two dirt lists, so two snapshots agree iff nothing tracked or
    executable-untracked changed between them, even if the SHA itself did not.
    """
    sha = _git_sha()
    status = classify_git_status(_git_status_lines())
    if status is None:
        return {
            "sha": sha,
            "tracked_dirty": None,
            "untracked_executable": None,
            "untracked_other": None,
            "digest": None,
            "status_error": True,
        }
    canonical = {
        "sha": sha,
        "tracked_dirty": status["tracked_dirty"],
        "untracked_executable": status["untracked_executable"],
    }
    digest = hashlib.sha256(
        json.dumps(canonical, sort_keys=True, ensure_ascii=False).encode("utf-8")
    ).hexdigest()
    return {
        "sha": sha,
        "tracked_dirty": status["tracked_dirty"],
        "untracked_executable": status["untracked_executable"],
        "untracked_other": status["untracked_other"],
        "digest": digest,
        "status_error": False,
    }


def _load_snapshot(path_str: str | None) -> dict[str, object] | None:
    if not path_str:
        return None
    try:
        data = json.loads(Path(path_str).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def _provenance_for_record(
    start: dict[str, object] | None, *, override_reason: str | None = None
) -> dict[str, object]:
    """Bind one record's eligibility to the snapshots that bracket it.

    Eligible only when: a start snapshot exists, neither snapshot hit a git
    failure, both agree on SHA, both agree on the dirt digest (nothing tracked
    or executable-untracked moved during the run), and the end snapshot itself
    is clean (no tracked dirt, no executable-untracked input). Every other
    outcome is an explicit, named ineligibility — never a silent "assume
    clean". A missing start snapshot (the caller did not pass
    ``--start-snapshot``, or it could not be read) is ineligible by
    construction: an unbound finding is exactly the defect this exists to stop.
    """
    end = provenance_snapshot()
    if override_reason is not None:
        return {"start": start, "end": end, "eligible": False, "ineligible_reason": override_reason}
    reason: str | None
    if start is None:
        reason = "missing_start_snapshot"
    elif start.get("status_error") or end.get("status_error"):
        reason = "git_status_unavailable"
    elif not start.get("sha") or start.get("sha") == "unknown" or not end.get("sha") or end.get("sha") == "unknown":
        reason = "invalid_sha"
    elif start.get("sha") != end.get("sha"):
        reason = "sha_mutated_during_run"
    elif start.get("digest") != end.get("digest"):
        reason = "tree_mutated_during_run"
    elif end.get("tracked_dirty"):
        reason = "tracked_dirty"
    elif end.get("untracked_executable"):
        reason = "untracked_executable_input"
    else:
        reason = None
    return {"start": start, "end": end, "eligible": reason is None, "ineligible_reason": reason}


def require_launchd_attestation(runner: str) -> None:
    """``runner=launchd`` must mean launchd actually ran it.

    The rendered plists set ``FH_LAUNCHD=1`` in ``EnvironmentVariables``. Without
    this check the runner field is a free-text claim: anyone typing
    ``--runner launchd`` by hand mints a record that makes
    ``freshness --runner launchd`` read green while nothing is bootstrapped. That
    is the same favourable absence these records exist to prevent, just moved from
    silence to a spoofable label.
    """
    if runner == "launchd" and os.environ.get("FH_LAUNCHD") != "1":
        raise SystemExit(
            "error: --runner launchd requires FH_LAUNCHD=1, which the rendered plists "
            "set via EnvironmentVariables. Refusing to stamp a SCHEDULED record for an "
            "invocation launchd did not make."
        )


def _host_load() -> float | None:
    try:
        return round(os.getloadavg()[0], 2)
    except OSError:
        return None


def _strip_classes(nodeid: str) -> str:
    return nodeid.split("::", 1)[0]


def _matches(nodeid: str, prefix: str) -> bool:
    if "::" in prefix:
        return nodeid == prefix or nodeid.startswith(prefix + "::")
    path = _strip_classes(nodeid)
    p = prefix.rstrip("/")
    return path == p or path.startswith(p + "/")


def _missing_paths(paths: list[str]) -> list[str]:
    return [p for p in paths if not (REPO / _strip_classes(p)).exists()]


def _covers_cell(
    feature: str, tier: str, matrix: dict[str, dict[str, object]], nodeids: list[str]
) -> bool:
    """Did this report observe a testcase under EVERY existing declared path of the cell?

    The test for "is this record complete evidence for that feature". Used only
    to decide whether a --feature-scoped run may write a record for a feature
    OUTSIDE its scope: complete evidence is safe to append (it is exactly what
    that feature's own run would have produced), partial evidence is not — it
    lands in the same stream key and replaces the full result.
    """
    declared = [str(p) for p in (matrix.get(feature, {}).get(tier) or [])]
    present = [p for p in declared if (REPO / _strip_classes(p)).exists()]
    if not present:
        # Nothing declared, or nothing declared is on disk. "Every declared
        # on-disk path was observed" is VACUOUSLY true over an empty list, and
        # a vacuous truth here would let any scoped run write a
        # complete-looking record for a feature it never touched.
        return False
    observed_here = [
        nodeid for nodeid in nodeids if any(_matches(nodeid, prefix) for prefix in present)
    ]
    if not observed_here:
        return False
    return all(any(_matches(nodeid, prefix) for nodeid in observed_here) for prefix in present)


def _humanize_age(seconds: float) -> str:
    seconds = max(0.0, seconds)
    if seconds < 90:
        return f"{int(seconds)}s"
    if seconds < 90 * 60:
        return f"{int(seconds // 60)}m"
    if seconds < 48 * 3600:
        return f"{int(seconds // 3600)}h"
    return f"{int(seconds // 86400)}d"


def _parse_ts(ts: str) -> datetime | None:
    try:
        return datetime.fromisoformat(ts)
    except (TypeError, ValueError):
        return None


# ------------------------------------------------------------------- junit parsing


def _nodeid_from_testcase(tc: ET.Element) -> str:
    name = tc.get("name") or ""
    classname = tc.get("classname") or ""
    file_attr = tc.get("file")
    if file_attr:
        mod = file_attr[:-3].replace("/", ".") if file_attr.endswith(".py") else ""
        classes: list[str] = []
        if mod and classname.startswith(mod + "."):
            classes = classname[len(mod) + 1 :].split(".")
        return "::".join([file_attr, *classes, name])
    parts = classname.split(".") if classname else []
    for i in range(len(parts), 0, -1):
        candidate = "/".join(parts[:i]) + ".py"
        if (REPO / candidate).exists():
            return "::".join([candidate, *parts[i:], name])
    # Last resort: guess a trailing TestClass component by capitalization.
    if parts and parts[-1][:1].isupper():
        return "/".join(parts[:-1]) + ".py::" + parts[-1] + "::" + name
    if parts:
        return "/".join(parts) + ".py::" + name
    return name


def parse_junit(path: Path) -> list[dict[str, object]]:
    """Return [{nodeid, status, message, time}] for every testcase. Raises on bad XML."""
    root = ET.parse(path).getroot()
    cases: list[dict[str, object]] = []
    for tc in root.iter("testcase"):
        status = "passed"
        message: str | None = None
        for child in tc:
            if child.tag in ("failure", "error"):
                status = "failed" if child.tag == "failure" else "error"
                message = (child.get("message") or (child.text or "").strip())[:_MSG_CAP]
                break
            if child.tag == "skipped":
                status = "skipped"
        try:
            duration = float(tc.get("time") or 0.0)
        except ValueError:
            duration = 0.0
        cases.append(
            {
                "nodeid": _nodeid_from_testcase(tc),
                "status": status,
                "message": message,
                "time": duration,
            }
        )
    return cases


# --------------------------------------------------------------- playwright parsing


def parse_playwright(path: Path) -> dict[str, object]:
    """Collapse a Playwright JSON report into one bucket of counts + failures."""
    data = json.loads(path.read_text())
    bucket = {"passed": 0, "failed": 0, "errors": 0, "skipped": 0, "duration_s": 0.0}
    failures: list[dict[str, object]] = []

    def walk(suite: dict[str, object], crumbs: list[str]) -> None:
        title = str(suite.get("title") or "")
        here = crumbs + ([title] if title else [])
        for spec in suite.get("specs") or []:
            statuses: list[str] = []
            message = None
            for test in spec.get("tests") or []:
                for result in test.get("results") or []:
                    statuses.append(str(result.get("status") or ""))
                    bucket["duration_s"] += float(result.get("duration") or 0) / 1000.0
                    err = result.get("error") or {}
                    if isinstance(err, dict) and err.get("message") and message is None:
                        message = str(err["message"])[:_MSG_CAP]
            spec_id = " > ".join([str(suite.get("file") or spec.get("file") or ""), *here[1:],
                                  str(spec.get("title") or "")]).strip(" >")
            if spec.get("ok"):
                bucket["passed"] += 1
            elif statuses and all(s == "skipped" for s in statuses):
                bucket["skipped"] += 1
            else:
                bucket["failed"] += 1
                failures.append(
                    {
                        "nodeid": spec_id,
                        "message": message,
                        "expected": False,
                        "known_issue_id": None,
                    }
                )
        for sub in suite.get("suites") or []:
            walk(sub, here)

    for suite in data.get("suites") or []:
        walk(suite, [])
    bucket["duration_s"] = round(bucket["duration_s"], 3)
    bucket["failures"] = failures
    return bucket


# ------------------------------------------------------------------------- ledger


def _append_records(records: list[dict[str, object]], ts: datetime) -> Path:
    """flock LOCK_EX + fsync append (mirrors omniagentos/ledger/__init__.py)."""
    path = var_dir() / f"ledger-{ts.strftime('%Y%m')}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as ledger_file:
        try:
            import fcntl

            fcntl.flock(ledger_file.fileno(), fcntl.LOCK_EX)
        except ImportError:
            fcntl = None  # type: ignore[assignment]
        try:
            for record in records:
                ledger_file.write(
                    json.dumps(record, sort_keys=True, ensure_ascii=False) + "\n"
                )
            ledger_file.flush()
            os.fsync(ledger_file.fileno())
        finally:
            if fcntl is not None:
                fcntl.flock(ledger_file.fileno(), fcntl.LOCK_UN)
    return path


def _iter_ledger_records() -> list[dict[str, object]]:
    records: list[dict[str, object]] = []
    for shard in sorted(var_dir().glob("ledger-*.jsonl")):
        try:
            lines = shard.read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        for line in lines:
            if not line.strip():
                continue
            try:
                value = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(value, dict) and value.get("schema") == SCHEMA:
                records.append(value)
    return records


# NOTE: there is deliberately no `_latest_per_cell()` any more. Newest-per-cell
# is the masking reduction this lane keeps re-growing (it hid a red live-probe
# stream behind a green Playwright append in three separate readers); the only
# supported reduction is `worst_per_cell(latest_per_stream())`.


def stream_key(record: dict[str, object]) -> tuple[str, str, str, str]:
    """(feature, tier, env, stream) — THE identity of one independent sub-run.

    Stream = the report basename with its run timestamp stripped, so the newest
    run of each stream replaces the previous one instead of accumulating.
    Everything that reduces this ledger — the grid, LATEST.md, the system
    ledger, the loop-queue filer — keys on this and nothing else; one cell can
    have several sub-runs (api_ui/tier3 is the isolated suite, the live probes
    AND Playwright) and any reader that collapses them to "newest record for
    the cell" lets a clean sibling erase a standing failure.
    """
    report = Path(str(record.get("report_path") or "")).name
    stream = report.split("-", 1)[1] if "-" in report else report
    return (
        str(record.get("feature")),
        str(record.get("tier")),
        str(record.get("env") or ""),
        stream,
    )


def stream_label(record: dict[str, object]) -> str:
    """`env/stream` — the human name of one sub-run, for per-stream renderings."""
    _feature, _tier, env, stream = stream_key(record)
    return f"{env or 'isolated'}/{stream or 'no-report'}"


#: Severity ladder, worst first. Every reader ranks cells with THIS, so the grid,
#: LATEST.md and SYSTEM-LEDGER.md cannot disagree about which cell is the bad one.
VERDICT_RANK = {"ABSENT": 0, "PASS": 1, "EMPTY": 2, "MISS": 3, "ERR": 4, "ABORT": 4, "FAIL": 5}


def verdict_class(record: dict[str, object] | None) -> str:
    """The severity CLASS of one record: ABSENT/PASS/EMPTY/MISS/ERR/ABORT/FAIL.

    Status first, counts second (a did-not-run record's counts are zero by
    construction, so grading counts first renders the loudest failure as
    `EMPTY`). `MISS` is a run whose declared matrix paths were not all on disk:
    a real but INCOMPLETE measurement, which must outrank a plain pass.
    """
    if record is None:
        return "ABSENT"
    if record.get("aborted"):
        return "ABORT"
    if record.get("status") == "error":
        return "ERR"
    failed = int(record.get("failed") or 0) + int(record.get("errors") or 0)
    expected = int(record.get("expected_failures") or 0)
    if max(0, failed - expected):
        return "FAIL"
    if record.get("missing_paths"):
        return "MISS"
    return "PASS" if int(record.get("passed") or 0) else "EMPTY"


def verdict_rank(record: dict[str, object] | None) -> int:
    return VERDICT_RANK[verdict_class(record)]


def latest_per_stream() -> dict[tuple[str, str, str, str], dict[str, object]]:
    """Newest record per :func:`stream_key`. The canonical reduction."""
    return _latest_per_run()


def worst_per_cell(
    streams: dict[tuple[str, str, str, str], dict[str, object]],
) -> dict[tuple[str, str], dict[str, object]]:
    """{(feature, tier): the WORST of that cell's newest-per-stream records}.

    "Worst", never "newest": a cell is only as healthy as its unhealthiest
    sub-run, and the newest append is simply whichever sub-run finished last.
    Ties on severity go to the newest of the tied records.
    """
    worst: dict[tuple[str, str], dict[str, object]] = {}
    for (feature, tier, _env, _stream), record in streams.items():
        cell = (feature, tier)
        prior = worst.get(cell)
        if prior is None or (verdict_rank(record), str(record.get("ts") or "")) > (
            verdict_rank(prior),
            str(prior.get("ts") or ""),
        ):
            worst[cell] = record
    return worst


def _latest_per_run() -> dict[tuple[str, str, str, str], dict[str, object]]:
    """Newest record per :func:`stream_key`. Prefer :func:`latest_per_stream`."""
    latest: dict[tuple[str, str, str, str], dict[str, object]] = {}
    for record in _iter_ledger_records():
        key = stream_key(record)
        prior = latest.get(key)
        if prior is None or str(record.get("ts") or "") >= str(prior.get("ts") or ""):
            latest[key] = record
    return latest


# ------------------------------------------------------------------------- append


def _base_record(
    *,
    ts: str,
    git_sha: str,
    git_dirty: int,
    provenance: dict[str, object],
    tier: str,
    feature: str,
    env: str,
    runner: str,
    report_path: str | None,
    cost_usd: float | None,
    cost_quality: str | None,
    host_load: float | None,
) -> dict[str, object]:
    return {
        "schema": SCHEMA,
        "ts": ts,
        "git_sha": git_sha,
        "git_dirty": git_dirty,
        "provenance": provenance,
        "tier": tier,
        "feature": feature,
        "env": env,
        "status": "ok",
        "passed": 0,
        "failed": 0,
        "errors": 0,
        "skipped": 0,
        "expected_failures": 0,
        "duration_s": 0.0,
        "cost_usd": cost_usd,
        "cost_quality": cost_quality,
        "report_path": report_path,
        "failures": [],
        "runner": runner,
        "host_load": host_load,
    }


def cmd_append(args: argparse.Namespace) -> int:
    require_launchd_attestation(args.runner)
    matrix = load_matrix()
    tier = args.tier
    covered = [f for f, spec in matrix.items() if spec[tier]]
    if args.playwright_json and not args.report:
        covered = [f for f, spec in matrix.items() if spec.get("playwright")] or ["api_ui"]
    if args.feature:
        unknown = [f for f in args.feature if f not in matrix]
        if unknown:
            print(f"error: unknown feature(s) {unknown} (not in configs/feature-health.yaml)",
                  file=sys.stderr)
            return 1
        covered = [f for f in covered if f in args.feature] or list(args.feature)

    cost_usd: float | None = None
    cost_quality: str | None = None
    if args.budget_summary:
        try:
            budget = json.loads(Path(args.budget_summary).read_text())
            cost_usd = round(float(budget.get("spent_usd", 0.0)), 6)
            cli_calls = int(budget.get("cli_calls", 0))
            if cli_calls > 0:
                cost_quality = "unreported" if cost_usd == 0 else "approximate"
            else:
                cost_quality = "exact"
        except (OSError, ValueError, json.JSONDecodeError):
            cost_usd, cost_quality = None, None

    now = _now()
    ts = now.isoformat(timespec="seconds")
    common = dict(
        ts=ts,
        git_sha=_git_sha(),
        git_dirty=_git_dirty(),
        provenance=_provenance_for_record(_load_snapshot(args.start_snapshot)),
        tier=tier,
        env=args.env,
        runner=args.runner,
        cost_usd=cost_usd,
        cost_quality=cost_quality,
        host_load=_host_load(),
    )
    sidecar = load_sidecar()
    records: list[dict[str, object]] = []

    def feature_record(feature: str, report_path: str | None) -> dict[str, object]:
        record = _base_record(feature=feature, report_path=report_path, **common)
        paths = matrix.get(feature, {}).get(tier, []) if feature in matrix else []
        missing = _missing_paths(list(paths))
        if missing:
            record["missing_paths"] = missing
        return record

    if args.report:
        report = Path(args.report)
        cases: list[dict[str, object]] | None = None
        if report.exists():
            try:
                cases = parse_junit(report)
            except (ET.ParseError, OSError):
                cases = None
        if cases is None:
            for feature in covered:
                record = feature_record(feature, str(report))
                record["status"] = "error"
                records.append(record)
        else:
            # Attribution runs over the FULL matrix, never the --feature subset.
            # `covered` says which features this invocation was asked to RUN; if
            # it also decided which prefixes a testcase may match, the same
            # nodeid would belong to control_plane in a full run and to api_ui in
            # a `--feature api_ui` run (api_ui's broad `tests/api` prefix being
            # the only candidate left). Two scopes alternating in the schedule
            # then record one broken test under two features — and mint two
            # standing findings for it.
            prefix_order = [
                (feature, prefix) for feature in matrix for prefix in matrix[feature][tier]
            ]
            per: dict[str, dict[str, object]] = {
                feature: feature_record(feature, str(report)) for feature in covered
            }
            for case in cases:
                nodeid = str(case["nodeid"])
                feature = next(
                    (f for f, prefix in prefix_order if _matches(nodeid, prefix)), "other"
                )
                if feature not in per:
                    per[feature] = feature_record(feature, str(report))
                record = per[feature]
                record["duration_s"] = round(
                    float(record["duration_s"]) + float(case["time"]), 3
                )
                status = case["status"]
                if status == "passed":
                    record["passed"] = int(record["passed"]) + 1
                elif status == "skipped":
                    record["skipped"] = int(record["skipped"]) + 1
                else:
                    key = "failed" if status == "failed" else "errors"
                    record[key] = int(record[key]) + 1
                    issue_id = sidecar.get(nodeid)
                    expected = issue_id is not None
                    if expected:
                        record["expected_failures"] = int(record["expected_failures"]) + 1
                    record["failures"].append(
                        {
                            "nodeid": nodeid,
                            "message": case["message"],
                            "expected": expected,
                            "known_issue_id": issue_id,
                        }
                    )
            # A --feature-scoped run ran only that feature's paths. Attribution
            # above is still full-matrix (a testcase belongs to whoever declares
            # it, whatever this invocation was asked to run) — but a record for
            # a feature OUTSIDE the scope may hold only PART of that feature's
            # cell, and a partial record appends into the same stream key as
            # that feature's own full run and REPLACES it. A scoped api_ui run
            # whose report happens to include one TestBoardPaths case would
            # otherwise turn tasks/tier3's full red stream into a one-case
            # green. So an out-of-scope record is written only when this report
            # actually covered every declared path of that cell that exists on
            # disk; otherwise it is dropped, loudly.
            in_scope = set(args.feature) if args.feature else None
            observed = [str(case["nodeid"]) for case in cases]
            for feature in per:
                if in_scope is None or feature in in_scope:
                    records.append(per[feature])
                elif _covers_cell(feature, tier, matrix, observed):
                    records.append(per[feature])
                else:
                    print(
                        f"note: dropping partial-evidence {tier} record for out-of-scope "
                        f"feature {feature} (this run was scoped to "
                        f"{sorted(in_scope)}; writing it would overwrite that feature's "
                        "own full stream)",
                        file=sys.stderr,
                    )

    if args.playwright_json:
        pw = Path(args.playwright_json)
        record = feature_record("api_ui", str(pw))
        try:
            bucket = parse_playwright(pw)
        except (OSError, json.JSONDecodeError, ValueError, KeyError, TypeError):
            bucket = None
        if bucket is None:
            record["status"] = "error"
        else:
            record.update(
                {
                    "passed": bucket["passed"],
                    "failed": bucket["failed"],
                    "errors": bucket["errors"],
                    "skipped": bucket["skipped"],
                    "duration_s": bucket["duration_s"],
                    "failures": bucket["failures"],
                }
            )
        records.append(record)

    if not records:
        print("error: nothing to append (need --report and/or --playwright-json)",
              file=sys.stderr)
        return 1

    # Did-not-run is not a pass. A record that covers matrix paths but observed ZERO
    # testcases means the selection deselected everything, collection failed, or the
    # paths vanished — all of which would otherwise render as a clean "0P" cell.
    for record in records:
        observed = sum(
            int(record.get(k) or 0) for k in ("passed", "failed", "errors", "skipped")
        )
        if observed == 0 and record["status"] != "error" and not record.get("missing_paths"):
            record["status"] = "error"
            record["did_not_run"] = True

    # An INCOMPLETE run is not a pass either. The caller (run.sh's live block)
    # knows something it asked for could not run at all; the report it does have
    # is honest about the tests that DID run and silent about the ones that
    # never got the chance, which is exactly the shape that reads as green.
    if args.incomplete:
        for record in records:
            record["status"] = "error"
            record["incomplete"] = args.incomplete

    path = _append_records(records, now)
    errors = sum(1 for r in records if r["status"] == "error")
    print(
        f"appended {len(records)} record(s) [{tier}] to {path}"
        + (f" ({errors} error record(s) — report missing/unparseable)" if errors else "")
    )
    return 0


# -------------------------------------------------------------------------- abort


def cmd_abort(args: argparse.Namespace) -> int:
    """Record that a scheduled run bailed out before producing any report.

    Deliberately touches neither configs/feature-health.yaml nor any report: the
    whole point is that this path runs when the lane could not get that far.
    """
    require_launchd_attestation(args.runner)
    now = _now()
    ts = now.isoformat(timespec="seconds")
    common = dict(
        ts=ts,
        git_sha=_git_sha(),
        git_dirty=_git_dirty(),
        provenance=_provenance_for_record(None, override_reason="aborted"),
        env=args.env,
        runner=args.runner,
        report_path=None,
        cost_usd=None,
        cost_quality=None,
        host_load=_host_load(),
    )
    records: list[dict[str, object]] = []
    for tier in dict.fromkeys(args.tier):  # dedupe, preserve order
        record = _base_record(tier=tier, feature=LANE_FEATURE, **common)
        record["status"] = "error"
        record["did_not_run"] = True
        record["aborted"] = True
        record["abort_reason"] = args.reason
        # Always present, even when empty, so this record and run.sh's hand-written
        # fallback line carry the same key set. Two carriers of the same fact that
        # disagree on shape is how a reader ends up branching on the wrong one.
        record["abort_detail"] = args.detail
        records.append(record)

    path = _append_records(records, now)
    print(f"appended {len(records)} abort record(s) [{args.reason}] to {path}")
    return 0


# ---------------------------------------------------------------------- freshness


def _newest_record(
    runner: str | None, tiers: list[str] | None = None
) -> dict[str, object] | None:
    newest: dict[str, object] | None = None
    for record in _iter_ledger_records():
        if runner is not None and str(record.get("runner") or "") != runner:
            continue
        if tiers and str(record.get("tier") or "") not in tiers:
            continue
        if not _parse_ts(str(record.get("ts") or "")):
            continue
        if newest is None or str(record.get("ts") or "") >= str(newest.get("ts") or ""):
            newest = record
    return newest


def cmd_freshness(args: argparse.Namespace) -> int:
    """Exit 0 when the lane produced a record inside --max-age-s, else 1.

    Freshness proves the SCHEDULER fired. It deliberately does NOT grade the
    record's contents — `status` is reported alongside so a caller can tell a
    working lane from one that is firing and aborting every time.

    --tier is what makes this a PER-JOB oracle rather than a lane-wide one. Two
    labels schedule this lane and only tier1/tier3 come from the 4-hourly job
    while tier2 comes solely from the nightly one, so an unfiltered check stays
    green off the healthy job while the other is dead, disabled, or never
    bootstrapped. Grade each cadence separately (see FEATURE-HEALTH.md).
    """
    newest = _newest_record(args.runner, args.tier)
    ts = _parse_ts(str(newest.get("ts") or "")) if newest else None
    age = (_now() - ts).total_seconds() if ts else None
    ok = age is not None and age <= args.max_age_s
    payload = {
        "ok": ok,
        "max_age_s": args.max_age_s,
        "age_s": round(age) if age is not None else None,
        "newest_ts": newest.get("ts") if newest else None,
        "newest_status": newest.get("status") if newest else None,
        "newest_aborted": bool(newest.get("aborted")) if newest else None,
        "newest_tier": newest.get("tier") if newest else None,
        "runner_filter": args.runner,
        "tier_filter": args.tier or None,
        "ledger_dir": str(var_dir()),
    }
    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True))
    elif ok:
        print(
            f"fresh: newest record {payload['newest_ts']} "
            f"({_humanize_age(age or 0)} old <= {args.max_age_s}s)"
            + (" — but it is an ABORT record" if payload["newest_aborted"] else "")
        )
    elif newest is None:
        scope = "".join(
            [
                f" for runner={args.runner}" if args.runner else "",
                f" for tier={','.join(args.tier)}" if args.tier else "",
            ]
        )
        print(f"STALE: no ledger records at all in {var_dir()}{scope}")
    else:
        print(
            f"STALE: newest record {payload['newest_ts']} is "
            f"{_humanize_age(age or 0)} old > {args.max_age_s}s"
        )
    return 0 if ok else 1


# ------------------------------------------------------------------------ summary


def _cell_text(record: dict[str, object] | None, now: datetime) -> str:
    if record is None:
        return "-"
    ts = _parse_ts(str(record.get("ts") or ""))
    age = _humanize_age((now - ts).total_seconds()) if ts else "?"
    if record.get("status") == "error":
        return f"ERR ({age})"
    failed = int(record.get("failed") or 0)
    errors = int(record.get("errors") or 0)
    expected = int(record.get("expected_failures") or 0)
    unexpected = max(0, failed + errors - expected)
    parts = [f"{int(record.get('passed') or 0)}P"]
    if unexpected:
        parts.append(f"{unexpected}F!")
    if expected:
        parts.append(f"{expected}xF")
    skipped = int(record.get("skipped") or 0)
    if skipped:
        parts.append(f"{skipped}S")
    if record.get("missing_paths"):
        parts.append("miss")
    return f"{' '.join(parts)} ({age})"


def _grid_rows(
    matrix: dict[str, dict[str, object]],
    streams: dict[tuple[str, str, str, str], dict[str, object]] | None = None,
) -> list[list[str]]:
    # WORST per cell, not newest: this grid, LATEST.md and SYSTEM-LEDGER.md all
    # reduce through worst_per_cell(), so a clean Playwright append can never
    # repaint a red live-probe cell green in one renderer and not the other.
    # `streams` is threaded in by callers that render more than one view: ONE
    # snapshot per command, or a run appending between two reads can put a grid
    # and its own per-stream detail into disagreement inside one document.
    latest = worst_per_cell(latest_per_stream() if streams is None else streams)
    now = _now()
    feature_order = list(matrix.keys())
    for feature, _tier in latest:
        if feature not in feature_order:
            feature_order.append(feature)
    rows = [["feature", *TIERS]]
    for feature in feature_order:
        rows.append(
            [feature, *(_cell_text(latest.get((feature, t)), now) for t in TIERS)]
        )
    return rows


def cmd_summary(args: argparse.Namespace) -> int:
    matrix = load_matrix()
    streams = latest_per_stream()  # ONE snapshot per invocation
    if args.json:
        latest = worst_per_cell(streams)
        out: dict[str, dict[str, object]] = {}
        now = _now()
        for (feature, tier), record in sorted(latest.items()):
            ts = _parse_ts(str(record.get("ts") or ""))
            failed = int(record.get("failed") or 0) + int(record.get("errors") or 0)
            expected = int(record.get("expected_failures") or 0)
            out.setdefault(feature, {})[tier] = {
                "ts": record.get("ts"),
                "age_s": round((now - ts).total_seconds()) if ts else None,
                "status": record.get("status"),
                "env": record.get("env"),
                "passed": record.get("passed"),
                "failed": failed,
                "expected_failures": expected,
                "unexpected_failures": max(0, failed - expected),
                "skipped": record.get("skipped"),
                "missing_paths": record.get("missing_paths") or [],
            }
        print(json.dumps(out, indent=2, sort_keys=True))
        return 0
    rows = _grid_rows(matrix, streams)
    widths = [max(len(row[i]) for row in rows) for i in range(len(rows[0]))]
    for i, row in enumerate(rows):
        print("  ".join(cell.ljust(widths[j]) for j, cell in enumerate(row)).rstrip())
        if i == 0:
            print("  ".join("-" * w for w in widths))
    print("\nlegend: NP passed  NF! unexpected fail  NxF expected (known-issue) fail  "
          "NS skipped  miss=matrix paths absent  ERR=run did not produce a report")
    return 0


# ------------------------------------------------------------------------- issues


def _load_known_issues() -> dict[str, dict[str, object]]:
    path = REPO / "docs" / "testing" / "KNOWN-ISSUES.yaml"
    try:
        data = yaml.safe_load(path.read_text()) or {}
    except OSError:
        return {}
    issues = data.get("issues") or {}
    return issues if isinstance(issues, dict) else {}


def _attribute_cell(nodeid: str, matrix: dict[str, dict[str, object]]) -> tuple[str, str] | None:
    for tier in TIERS:
        for feature, spec in matrix.items():
            for prefix in spec[tier]:
                if _matches(nodeid, prefix):
                    return feature, tier
    return None


def _collect_issues(
    matrix: dict[str, dict[str, object]],
    include_expected: bool,
    streams: dict[tuple[str, str, str, str], dict[str, object]] | None = None,
) -> dict:
    latest = latest_per_stream() if streams is None else streams
    open_by_feature: dict[str, list[dict[str, object]]] = {}
    failing_nodeids: set[str] = set()
    for (feature, tier, _env, _stream), record in latest.items():
        for failure in record.get("failures") or []:
            failing_nodeids.add(str(failure.get("nodeid")))
            if failure.get("expected") and not include_expected:
                continue
            open_by_feature.setdefault(feature, []).append(
                {**failure, "tier": tier, "ts": record.get("ts")}
            )

    known = _load_known_issues()
    promotions: list[dict[str, object]] = []
    for nodeid, issue_id in sorted(load_sidecar().items()):
        issue = known.get(issue_id) or {}
        if str(issue.get("status") or "open") != "open":
            continue
        if nodeid in failing_nodeids:
            continue
        cell = _attribute_cell(nodeid, matrix)
        if cell is None:
            continue
        record = latest.get(cell)
        if record is None or record.get("status") == "error":
            continue
        ran = int(record.get("passed") or 0) + int(record.get("failed") or 0) + int(
            record.get("errors") or 0
        )
        if ran == 0:
            continue
        missing = record.get("missing_paths") or []
        if any(_matches(nodeid, str(m)) for m in missing):
            continue
        promotions.append(
            {
                "nodeid": nodeid,
                "known_issue_id": issue_id,
                "feature": cell[0],
                "tier": cell[1],
                "title": issue.get("title"),
                "action": "test now PASSING while KNOWN-ISSUES status is open — "
                "promote to certification suite and flip the issue to fixed",
            }
        )
    return {"open": open_by_feature, "promotions": promotions}


def cmd_issues(args: argparse.Namespace) -> int:
    matrix = load_matrix()
    data = _collect_issues(matrix, args.include_expected)
    if args.json:
        print(json.dumps(data, indent=2, sort_keys=True))
        return 0
    if not data["open"] and not data["promotions"]:
        print("no open failures" + ("" if args.include_expected else " (expected hidden; --include-expected to show)"))
        return 0
    for feature in sorted(data["open"]):
        print(f"{feature}:")
        for failure in data["open"][feature]:
            tag = ""
            if failure.get("expected"):
                tag = f" [expected: {failure.get('known_issue_id')}]"
            message = (failure.get("message") or "").splitlines()
            first = message[0] if message else ""
            print(f"  [{failure['tier']}] {failure['nodeid']}{tag}")
            if first:
                print(f"      {first[:160]}")
    if data["promotions"]:
        print("\npromotions (fh_known_issue tests now passing):")
        for promo in data["promotions"]:
            print(f"  {promo['known_issue_id']} {promo['nodeid']} [{promo['feature']}/{promo['tier']}]")
            print(f"      -> {promo['action']}")
    return 0


# ------------------------------------------------------------------ render-latest


def _stream_detail_lines(streams: dict[tuple[str, str, str, str], dict[str, object]]) -> list[str]:
    """Per-sub-run detail for cells where the worst-of collapse hides something.

    The grid shows one cell per (feature, tier); a cell can be several
    independent sub-runs, and the collapsed cell cannot say WHICH one is red.
    Only disagreeing (or non-green) multi-stream cells are listed, so a healthy
    estate adds one line, not a second grid.
    """
    by_cell: dict[tuple[str, str], list[dict[str, object]]] = {}
    for (feature, tier, _env, _stream), record in streams.items():
        by_cell.setdefault((feature, tier), []).append(record)
    lines: list[str] = []
    for (feature, tier), records in sorted(by_cell.items()):
        ranks = {verdict_rank(record) for record in records}
        if len(records) < 2 or (len(ranks) == 1 and ranks == {VERDICT_RANK["PASS"]}):
            continue
        detail = ", ".join(
            f"`{stream_label(record)}` {verdict_class(record)}"
            for record in sorted(records, key=stream_label)
        )
        lines.append(f"- **{feature}** [{tier}]: {detail}")
    return lines or ["- (none: every multi-sub-run cell is green across all of them)"]


def cmd_render_latest(_args: argparse.Namespace) -> int:
    matrix = load_matrix()
    # ONE snapshot for the whole document: the grid, the per-sub-run detail and
    # the issue list are three views of the same ledger, and this command runs
    # at the END of run.sh — while the next scheduled run may already be
    # appending. Three reads can render a LATEST.md whose grid says PASS, whose
    # detail block says FAIL and whose issue list names neither.
    streams = latest_per_stream()
    rows = _grid_rows(matrix, streams)
    data = _collect_issues(matrix, include_expected=False, streams=streams)
    shards = sorted(var_dir().glob("ledger-*.jsonl"))
    lines: list[str] = []
    lines.append("# Feature-Health — LATEST")
    lines.append("")
    lines.append(f"Regenerated {_now().isoformat(timespec='seconds')} by scripts/feature_health/fh.py render-latest.")
    lines.append("Non-blocking background lane — never gates merges. Docs: docs/testing/FEATURE-HEALTH.md.")
    lines.append("")
    lines.append("## Grid (WORST sub-run per feature x tier)")
    lines.append("")
    lines.append("| " + " | ".join(rows[0]) + " |")
    lines.append("|" + "|".join(["---"] * len(rows[0])) + "|")
    for row in rows[1:]:
        lines.append("| " + " | ".join(row) + " |")
    lines.append("")
    lines.append("Legend: `NP` passed, `NF!` unexpected failures, `NxF` expected (known-issue) "
                 "failures, `NS` skipped, `miss` = matrix paths absent on disk, `ERR` = run "
                 "produced no parseable report, `-` = never run.")
    lines.append("")
    lines.append("Each cell is the WORST of that cell's independent sub-runs (isolated suite / "
                 "live probes / Playwright), severity FAIL > ERR/ABORT > MISS > EMPTY > PASS — "
                 "the same reduction SYSTEM-LEDGER.md uses. Cells the collapse hides something in:")
    lines.append("")
    lines.extend(_stream_detail_lines(streams))
    lines.append("")
    lines.append("## Top issues (unexpected failures)")
    lines.append("")
    if not data["open"]:
        lines.append("None. (Expected known-issue failures are hidden — `fh.py issues --include-expected`.)")
    else:
        for feature in sorted(data["open"]):
            for failure in data["open"][feature][:5]:
                message = (failure.get("message") or "").splitlines()
                first = (message[0] if message else "")[:140]
                lines.append(f"- **{feature}** [{failure['tier']}] `{failure['nodeid']}` {first}")
    if data["promotions"]:
        lines.append("")
        lines.append("## Promotions (known-issue tests now passing)")
        lines.append("")
        for promo in data["promotions"]:
            lines.append(f"- `{promo['known_issue_id']}` `{promo['nodeid']}` — {promo['action']}")
    lines.append("")
    lines.append("## Ledger")
    lines.append("")
    if shards:
        for shard in shards:
            lines.append(f"- `{shard}`")
    else:
        lines.append("- (no shards yet)")
    lines.append("")
    lines.append("## How to run")
    lines.append("")
    lines.append("```bash")
    lines.append("scripts/feature_health/run.sh tier1              # mechanical, $0, zero GPU")
    lines.append("scripts/feature_health/run.sh tier3              # UI-called API paths, isolated app")
    lines.append("scripts/feature_health/run.sh tier2              # live cheap-LLM probes (budget-capped)")
    lines.append("scripts/feature_health/run.sh tier3 --live-probes  # + read-only GETs against live :8485")
    lines.append("scripts/feature_health/run.sh all --feature dag  # scope any run to one feature")
    lines.append("scripts/feature_health/fh.py summary             # grid + freshness")
    lines.append("scripts/feature_health/fh.py issues              # unexpected failures + promotions")
    lines.append("```")
    lines.append("")
    target = var_dir() / "LATEST.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("\n".join(lines), encoding="utf-8")
    print(f"wrote {target}")
    return 0


# ----------------------------------------------------------------------- snapshot


def cmd_snapshot(args: argparse.Namespace) -> int:
    """Write one provenance snapshot to ``--out``, atomically.

    ``run.sh`` calls this once, before the first test process, and passes the
    path to every tier's ``append --start-snapshot``; ``append`` takes a second
    snapshot itself at record time. Two independently-taken snapshots are what
    let a record prove the tree did not move UNDER the run, not merely that it
    looked clean at one instant.
    """
    snapshot = provenance_snapshot()
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=out.parent, prefix=f".{out.name}.", suffix=".tmp")
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(snapshot, stream, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, out)
    finally:
        tmp.unlink(missing_ok=True)
    print(f"wrote {out}")
    return 0


# -------------------------------------------------------------------------- paths


def cmd_paths(args: argparse.Namespace) -> int:
    """Print existing matrix paths for a tier (dedup'd), for run.sh pytest args."""
    matrix = load_matrix()
    tier = args.tier
    features = args.feature or list(matrix.keys())
    ordered: list[str] = []
    for feature in matrix:
        if feature not in features:
            continue
        for path in matrix[feature][tier]:
            if path not in ordered:
                ordered.append(path)
    existing = [p for p in ordered if (REPO / _strip_classes(p)).exists()]
    # Drop ::Class entries whose bare file is also selected, and paths inside a
    # selected directory — passing both would collect the same tests twice.
    files = {p for p in existing if "::" not in p}
    result: list[str] = []
    for path in existing:
        base = _strip_classes(path)
        if "::" in path and base in files:
            continue
        if any(base != d and base.startswith(d.rstrip("/") + "/") for d in files):
            continue
        result.append(path)
    for path in result:
        print(path)
    return 0


# --------------------------------------------------------------------------- main


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="fh.py", description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_append = sub.add_parser("append", help="parse reports and append ledger records")
    p_append.add_argument("--tier", required=True, choices=TIERS)
    p_append.add_argument("--report", help="pytest JUnit XML path")
    p_append.add_argument("--playwright-json", dest="playwright_json",
                          help="Playwright JSON report path (api_ui record)")
    p_append.add_argument("--env", default="isolated", choices=("isolated", "live"))
    p_append.add_argument("--runner", default="manual", choices=("manual", "launchd"))
    p_append.add_argument("--budget-summary", dest="budget_summary",
                          help="tier2 budget summary JSON (spent_usd, cli_calls)")
    p_append.add_argument("--feature", action="append",
                          help="restrict records to these feature(s) (repeatable)")
    p_append.add_argument("--incomplete", metavar="DETAIL",
                          help="the caller knows this run could not cover everything it was "
                               "asked to (e.g. a requested test module is absent): mark every "
                               "record from it status=error with DETAIL, so a partial run can "
                               "never read as a clean pass")
    p_append.add_argument("--start-snapshot", dest="start_snapshot",
                          help="path to a provenance snapshot written by `fh.py snapshot` "
                               "before the first test process; binds this record's filing "
                               "eligibility to the tree that was ACTUALLY measured. Omitting "
                               "it makes the record ineligible for finding-filing (see "
                               "docs/testing/FEATURE-HEALTH.md), never favourably absent.")
    p_append.set_defaults(func=cmd_append)

    p_summary = sub.add_parser("summary", help="per-feature x per-tier grid")
    p_summary.add_argument("--json", action="store_true")
    p_summary.set_defaults(func=cmd_summary)

    p_issues = sub.add_parser("issues", help="open failures + promotion flags")
    p_issues.add_argument("--include-expected", action="store_true")
    p_issues.add_argument("--json", action="store_true")
    p_issues.set_defaults(func=cmd_issues)

    p_render = sub.add_parser("render-latest", help="regenerate var/feature-health/LATEST.md")
    p_render.set_defaults(func=cmd_render_latest)

    p_snapshot = sub.add_parser(
        "snapshot", help="write one provenance snapshot (sha + tracked/untracked dirt)"
    )
    p_snapshot.add_argument("--out", required=True, help="path to write the snapshot JSON")
    p_snapshot.set_defaults(func=cmd_snapshot)

    p_paths = sub.add_parser("paths", help="print existing matrix paths for a tier")
    p_paths.add_argument("--tier", required=True, choices=TIERS)
    p_paths.add_argument("--feature", action="append")
    p_paths.set_defaults(func=cmd_paths)

    p_abort = sub.add_parser("abort", help="record a did-not-run abort (no report produced)")
    p_abort.add_argument("--tier", required=True, action="append", choices=TIERS,
                         help="tier the aborted invocation would have run (repeatable)")
    p_abort.add_argument("--reason", required=True,
                         help="short machine-readable reason, e.g. load-guard")
    p_abort.add_argument("--detail", default="", help="free-text detail (measured values)")
    p_abort.add_argument("--env", default="isolated", choices=("isolated", "live"))
    p_abort.add_argument("--runner", default="manual", choices=("manual", "launchd"))
    p_abort.set_defaults(func=cmd_abort)

    p_fresh = sub.add_parser("freshness", help="exit non-zero when the newest record is stale")
    p_fresh.add_argument("--max-age-s", dest="max_age_s", type=int, required=True,
                         help="staleness budget in seconds (tier1 cadence is 14400)")
    p_fresh.add_argument("--runner", choices=("manual", "launchd"),
                         help="only consider records from this runner")
    p_fresh.add_argument("--tier", action="append", choices=TIERS,
                         help="only consider these tier(s) — grades ONE scheduled job "
                              "instead of the whole lane (repeatable)")
    p_fresh.add_argument("--json", action="store_true")
    p_fresh.set_defaults(func=cmd_freshness)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
