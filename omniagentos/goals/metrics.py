"""Closed, read-only metric sources used by goal loops."""

from __future__ import annotations

import json
import logging
import re
import sqlite3
import subprocess
from collections.abc import Callable
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path

from omniagentos import runtime_paths
from omniagentos.steward.store import StewardStore
from pipeline.bridge.ledger_read import iter_events

LOG = logging.getLogger(__name__)
MetricSource = Callable[[StewardStore], float | None]
_TERMINAL_BOARD_TASK_STATUSES = ("done", "cancelled")
#: Package root — never Path.cwd(): daemons and worktrees run from anywhere.
_REPO_ROOT = Path(__file__).resolve().parents[2]


def _parse_rfc3339(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.astimezone(UTC) if parsed.tzinfo is not None else None


def ledger_merges_per_hour(
    store: StewardStore,
    *,
    repo_path: str | Path | None = None,
    window: timedelta = timedelta(hours=1),
    now: datetime | None = None,
) -> float | None:
    """Read-only merge rate over the loopqueue ledger.

    A row qualifies ONLY when merge_sha is a 7-40 char hex OBJECT ID (a ref
    name like ``main`` is agent-typable for free and always resolves — r5
    blocker) that resolves to a commit reachable from main. Fault taxonomy:
    None for instrument faults (unreadable ledger; no git/main ref; a git
    spawn failure mid-scan; a fully BLIND window — every sha unresolvable and
    none landed). A bad DATUM (foreign detail.repo, unresolvable sha) is
    skipped and counted; skips log at WARNING. Dedup keys on the RESOLVED oid.
    Coverage measured 2026-08-14: 183/202 merged events main-reachable.
    """
    del store
    if window.total_seconds() <= 0:
        raise ValueError("window must be positive")
    # The ledger is runtime STATE and resolves through runtime_paths (D3: no
    # __file__-anchored state paths); only the git object store keeps the
    # package anchor. An explicit repo_path (tests) pins both to one tree.
    if repo_path is not None:
        explicit_root = Path(repo_path)
        repo = explicit_root
        ledger_path = explicit_root / "var" / "loopqueue" / "ledger.jsonl"
    else:
        repo = _REPO_ROOT
        try:
            ledger_path = runtime_paths.resolve_var_root(leaf=("loopqueue", "ledger.jsonl"))
        except runtime_paths.RuntimePathError as exc:
            LOG.warning("merges/hr unmeasurable: var root unresolvable: %s", exc)
            return None

    class _GitUnavailable(Exception):
        pass

    def _git(*args: str) -> subprocess.CompletedProcess[bytes]:
        try:
            return subprocess.run(
                ["git", *args],
                cwd=repo,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                check=False,
            )
        except OSError as exc:  # fork failure mid-scan = instrument fault
            raise _GitUnavailable(str(exc)) from exc

    try:
        ledger = ledger_path.read_text(encoding="utf-8")
    except OSError as exc:
        LOG.warning("merges/hr unmeasurable: ledger unreadable: %s", exc)
        return None
    try:
        main_probe = _git("rev-parse", "--verify", "main^{commit}")
        if main_probe.returncode != 0:
            LOG.warning(
                "merges/hr unmeasurable: main unresolvable in %s: %s",
                repo,
                main_probe.stderr.decode(errors="replace").strip(),
            )
            return None
        # Repo identity = the toplevel's basename, never the checkout dir name
        # (a worktree named OmniAgentOS-lane7 must still own its rows).
        toplevel = _git("rev-parse", "--show-toplevel")
        repo_name = Path(toplevel.stdout.decode().strip() or str(repo)).name

        end = (now or datetime.now(UTC)).astimezone(UTC)
        start = end - window
        landed: set[str] = set()
        skipped_unresolvable = 0
        skipped_foreign = 0
        for event in iter_events(ledger):
            if event.get("event") != "merged":
                continue
            timestamp = _parse_rfc3339(event.get("ts"))
            detail = event.get("detail")
            if not (
                timestamp is not None and start < timestamp <= end and isinstance(detail, dict)
            ):
                continue
            event_repo = detail.get("repo")
            if isinstance(event_repo, str) and event_repo:
                if Path(event_repo.rstrip("/")).name != repo_name:
                    skipped_foreign += 1
                    continue
            sha = detail.get("merge_sha")
            if not isinstance(sha, str) or not re.fullmatch(r"[0-9a-f]{7,40}", sha):
                # Ref names (main/HEAD/v1) resolve but are not object ids.
                skipped_unresolvable += 1
                continue
            resolved_probe = _git("rev-parse", "--verify", f"{sha}^{{commit}}")
            if resolved_probe.returncode != 0:
                skipped_unresolvable += 1
                continue
            oid = resolved_probe.stdout.decode().strip()
            if oid in landed:
                continue
            ancestry = _git("merge-base", "--is-ancestor", oid, "main")
            if ancestry.returncode == 0:
                landed.add(oid)
            elif ancestry.returncode != 1:
                skipped_unresolvable += 1
    except _GitUnavailable as exc:
        LOG.warning("merges/hr unmeasurable: git unavailable: %s", exc)
        return None
    if skipped_unresolvable or skipped_foreign:
        LOG.warning(
            "merges/hr: %d in-window rows unusable (%d unresolvable, %d foreign)",
            skipped_unresolvable + skipped_foreign,
            skipped_unresolvable,
            skipped_foreign,
        )
    if skipped_unresolvable and not landed:
        LOG.warning("merges/hr unmeasurable: window is BLIND (all shas unresolvable)")
        return None
    return len(landed) / (window.total_seconds() / 3600)


def _metric_snapshot(
    store: StewardStore, source: str, metric: str, *, goal_id: str
) -> float | None:
    series = store.snapshot_series(metric, goal_id=goal_id, source=source)
    if not series:
        return None
    value = series[-1].get("value")
    return float(value) if value is not None else None


def _gate_pass_rate(store: StewardStore, routine: str, *, days: int = 14) -> float | None:
    if days <= 0:
        raise ValueError("days must be positive")
    connection = store._connection
    row = connection.execute("SELECT id FROM routines WHERE id = ?", (routine,)).fetchone()
    if row is None:
        row = connection.execute("SELECT id FROM routines WHERE name = ?", (routine,)).fetchone()
    if row is None:
        raise KeyError(routine)
    cutoff = (datetime.now(UTC) - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")
    counts = connection.execute(
        "SELECT SUM(CASE WHEN gate_passed = 1 THEN 1 ELSE 0 END), COUNT(*) "
        "FROM routine_runs WHERE routine_id = ? AND created_at >= ? AND gate_passed IS NOT NULL",
        (row["id"], cutoff),
    ).fetchone()
    decided = int(counts[1])
    return int(counts[0] or 0) / decided if decided else None


def _northstar_cert(store: StewardStore, *, cert_db_path: str | Path | None = None) -> float | None:
    del store
    # Runtime state resolves through runtime_paths (D3), same as the ledger.
    if cert_db_path is not None:
        cert_path = Path(cert_db_path)
    else:
        try:
            cert_path = runtime_paths.resolve_var_root(
                leaf=("northstar-cert", "results.sqlite3")
            )
        except runtime_paths.RuntimePathError as exc:
            LOG.warning("northstar-cert unmeasurable: var root unresolvable: %s", exc)
            return None
    try:
        with closing(
            sqlite3.connect(f"{cert_path.resolve().as_uri()}?mode=ro", uri=True)
        ) as connection:
            connection.row_factory = sqlite3.Row
            suite = connection.execute(
                "SELECT id FROM eval_suites WHERE discipline = ? ORDER BY created_at DESC LIMIT 1",
                ("northstar_cert",),
            ).fetchone()
            if suite is None:
                LOG.debug("northstar_cert: no suite recorded in %s (absence)", cert_path)
                return None
            result = connection.execute(
                "SELECT experiment_id FROM eval_results WHERE suite_id = ? AND arm = 'champion' "
                "AND split = 'dev' ORDER BY created_at DESC LIMIT 1",
                (suite["id"],),
            ).fetchone()
            if result is None:
                LOG.debug("northstar_cert: suite has no champion/dev results (absence)")
                return None
            results = connection.execute(
                "SELECT per_case_json FROM eval_results WHERE suite_id = ? AND experiment_id = ? "
                "AND arm = 'champion' AND split = 'dev'",
                (suite["id"], result["experiment_id"]),
            ).fetchall()
    except (OSError, sqlite3.Error) as exc:
        # DatabaseError (a torn/corrupt file) must be a quiet instrument None
        # like every sibling fault, never an escaping exception.
        LOG.warning("northstar_cert unmeasurable: %s", exc)
        return None
    passed = failed = not_evaluable = 0
    for result in results:
        try:
            cases = json.loads(result["per_case_json"])
        except (TypeError, json.JSONDecodeError):
            continue
        if not isinstance(cases, dict):
            continue
        for case in cases.values():
            if not isinstance(case, dict) or not any(
                isinstance(case.get(name), (int, float))
                for name in ("pass", "fail", "not_evaluable")
            ):
                continue
            passed += bool(case.get("pass"))
            failed += bool(case.get("fail"))
            not_evaluable += bool(case.get("not_evaluable"))
    total = passed + failed + not_evaluable
    return passed / total if total else None


def _mission_direct(store: StewardStore) -> float | None:
    placeholders = ", ".join("?" for _ in _TERMINAL_BOARD_TASK_STATUSES)
    row = store._connection.execute(
        "SELECT COUNT(*), SUM(CASE WHEN bt.goal_id IS NOT NULL AND cg.status IN ('active', 'achieved') "
        "THEN 1 ELSE 0 END) FROM board_tasks bt LEFT JOIN company_goals cg ON cg.id = bt.goal_id "
        f"WHERE bt.status IN ({placeholders}) AND bt.archived_at IS NULL",
        _TERMINAL_BOARD_TASK_STATUSES,
    ).fetchone()
    total = int(row[0])
    return int(row[1] or 0) / total if total else None


_FIXED_SOURCES: dict[str, MetricSource] = {
    "ledger_merges_per_hour": ledger_merges_per_hour,
    "northstar_cert": _northstar_cert,
    "mission_direct": _mission_direct,
}


def get_metric_value(
    store: StewardStore,
    source: str,
    *,
    goal_id: str | None = None,
    repo_path: str | Path | None = None,
    cert_db_path: str | Path | None = None,
) -> float | None:
    """Look up a closed source shape; unknown routines and malformed names raise KeyError."""
    if source.startswith("metric_snapshot:"):
        suffix = source.removeprefix("metric_snapshot:")
        if ":" not in suffix:
            raise KeyError(source)
        snapshot_source, metric = suffix.split(":", 1)
        if not snapshot_source or not metric or ":" in metric or goal_id is None:
            raise KeyError(source)
        return _metric_snapshot(store, snapshot_source, metric, goal_id=goal_id)
    if source.startswith("gate_pass_rate:"):
        routine = source.removeprefix("gate_pass_rate:")
        if not routine:
            raise KeyError(source)
        return _gate_pass_rate(store, routine)
    try:
        metric_source = _FIXED_SOURCES[source]
    except KeyError:
        raise KeyError(source) from None
    if source == "ledger_merges_per_hour":
        return ledger_merges_per_hour(store, repo_path=repo_path)
    if source == "northstar_cert":
        return _northstar_cert(store, cert_db_path=cert_db_path)
    return metric_source(store)
