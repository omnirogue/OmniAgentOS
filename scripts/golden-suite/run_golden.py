#!/usr/bin/env python3
"""``run_golden.py`` — the GOLDEN-SUITE SENTINEL's python driver.

Nightly north-star metric (plan section A0.0,
``devtasks/SWARM-BASELINE-2026-07-23.md``): p50/p90 wall-clock-to-GREEN on
three FIXED benchmark briefs (``benchmarks.yaml``), so every phase of work is
judged apples-to-apples against the same trivial/medium/swarm asks. Run via
``.venv/bin/python scripts/golden-suite/run_golden.py`` (by hand, or the
render-not-load launchd template in this directory — see
``scripts/golden-suite/launchd.py`` / ``install-golden-suite.sh``).

**Zero LLM calls in this driver.** Every benchmark's actual WORK routes
through the system exactly as a human's own ask would (``POST
/api/intake/quick`` or ``POST /api/swarm`` — real Fable/Codex/etc calls
happen THERE, on the far side of the API, which is the entire point of
measuring wall-clock-to-green), but this script itself never imports or
calls any model. It only: renders a fresh scratch git repo, makes a couple
of HTTP calls to the ALREADY-RUNNING local API, polls, and runs `sh -c`
acceptance checks against the filesystem.

For each benchmark, in order:

1. Fresh scratch git repo under ``var/golden/runs/<UTCdate>/<name>/``
   (``git init -b main`` + one initial commit) — this is also why every
   benchmark's ``working_dir``/goal-embedded path lives under this repo's
   own ``var/`` dir: the board-files approved-root floor
   (``omniagentos.api.routes.board_files._enforce_workspace_floor``) only
   ever clears paths under here or a grantable mount root.
2. Dispatch the brief via the real API (session token from
   ``var/secrets/sessions-token``), poll to a terminal state, run the
   acceptance checks. Wall-clock-to-GREEN = dispatch -> all acceptance
   checks passing. Any failure (terminal-failed/cancelled run, a timeout, a
   failed acceptance check, a raised exception) is recorded as a DNF with a
   reason string — this script itself must never crash on a bad night.
3. Append one line to ``var/golden/history.jsonl`` (idempotent per
   ``(date, name)`` — see ``history_stats.append_history``), then check the
   regression rule (2 consecutive regression nights -> one
   ``record_notification(kind="alert", ...)``).

Always appends exactly one ``var/improvement-log.jsonl`` line
(``improver: "golden-sentinel"``), win or lose, unless ``--dry-run``.

``--dry-run`` is the smoke check: parses ``benchmarks.yaml``, loads
``prompt.md``'s policy block, and resolves the session token file — NO HTTP
dispatch, no scratch repo, no history/improvement-log writes.
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

# Make sibling modules (history_stats.py) importable by plain filename
# regardless of how THIS file was imported/invoked — `scripts/golden-suite`
# is a hyphenated directory name (matching the repo's `com.omniagentos.*`
# launchd-job-name idiom), so it can never be a dotted Python package path
# (`scripts.golden-suite.x` is not valid import syntax). Tests reach this
# module itself via `importlib.import_module("scripts.golden-suite.run_golden")`
# (importlib's string form has no such restriction); this script reaches its
# OWN sibling the ordinary way once its directory is on sys.path.
_SCRIPT_DIR = Path(__file__).resolve().parent
if str(_SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPT_DIR))

import history_stats  # noqa: E402

ROOT_DIR = _SCRIPT_DIR.parent.parent


def var_root() -> Path:
    """The var root this run writes to — resolved, never re-derived ad hoc.

    Under a campaign (``OMNIAGENTOS_SIM_MODE=1``) this is the campaign var root,
    so a simulated golden run cannot append to the operator checkout's history
    or improvement log. Outside simulation it stays ``<repo>/var`` exactly as
    before: the readers of these files (``omniagentos/improvement_chain.py``,
    ``api/routes/system.py``, ``golden-suite.sh``) are package/repo-anchored, so
    honouring ``OMNIAGENTOS_VAR_DIR`` in production would split the improvement
    log in two with no migration.

    Resolved lazily and from THIS checkout's package (``sys.path``), so
    ``--help`` never depends on an editable install pointing somewhere else.
    """
    if str(ROOT_DIR) not in sys.path:
        sys.path.insert(0, str(ROOT_DIR))
    from omniagentos.runtime_paths import TOKEN_VAR_ENV_KEYS, resolve_sim_context_or_none
    from omniagentos.runtime_paths import resolve_var_root as _resolve_var_root

    if resolve_sim_context_or_none() is None:
        return ROOT_DIR / "var"
    return _resolve_var_root(env_keys=TOKEN_VAR_ENV_KEYS)


def default_token_path() -> Path:
    return var_root() / "secrets" / "sessions-token"


def default_history_path() -> Path:
    return var_root() / "golden" / "history.jsonl"


def default_runs_dir() -> Path:
    return var_root() / "golden" / "runs"


def default_improvement_log_path() -> Path:
    return var_root() / "improvement-log.jsonl"


DEFAULT_BASE_URL = "http://127.0.0.1:8485"
DEFAULT_PROMPT_PATH = _SCRIPT_DIR / "prompt.md"
DEFAULT_BENCHMARKS_PATH = _SCRIPT_DIR / "benchmarks.yaml"
DEFAULT_POLL_INTERVAL_SECONDS = 5.0
HTTP_TIMEOUT_SECONDS = 30.0

LOG = logging.getLogger("golden_suite")

_SWARM_TERMINAL_SUCCESS = "completed"
_SWARM_TERMINAL_FAILURE = {"failed", "cancelled"}
_BOARD_TERMINAL_SUCCESS = "done"
_BOARD_TERMINAL_FAILURE = {"cancelled", "blocked"}


# ---------------------------------------------------------------------------
# Policy — parsed from prompt.md's fenced ```yaml policy: block at runtime.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Policy:
    regression_threshold_pct: float = 25.0
    consecutive_nights: int = 2
    rolling_window: int = 7
    default_timeout_minutes: int = 15
    benchmarks_file: str = "benchmarks.yaml"


_POLICY_DEFAULTS = Policy()
_POLICY_BLOCK_RE = re.compile(r"```yaml\s*\n(.*?)\n```", re.DOTALL)


def load_policy(prompt_path: Path) -> tuple[Policy, list[str]]:
    """Parse the fenced ``policy:`` yaml block out of ``prompt_path``.

    Never raises. Any problem (unreadable file, no fenced yaml block,
    malformed yaml, a non-mapping ``policy:`` value, a key whose value
    cannot be coerced to its expected type) falls back to the matching
    ``Policy()`` code default FOR THAT KEY ONLY and is recorded as a
    human-readable warning string in the returned list (the caller logs
    them) — a partial, valid edit still applies the keys it got right.
    """
    warnings: list[str] = []
    values = {
        field: getattr(_POLICY_DEFAULTS, field) for field in _POLICY_DEFAULTS.__dataclass_fields__
    }

    try:
        text = prompt_path.read_text(encoding="utf-8")
    except OSError as exc:
        warnings.append(
            f"golden-suite policy: {prompt_path} unreadable ({exc}); using code defaults"
        )
        return Policy(**values), warnings

    match = _POLICY_BLOCK_RE.search(text)
    if not match:
        warnings.append(
            f"golden-suite policy: no fenced ```yaml block in {prompt_path}; using code defaults"
        )
        return Policy(**values), warnings

    import yaml

    try:
        parsed = yaml.safe_load(match.group(1))
    except yaml.YAMLError as exc:
        warnings.append(
            f"golden-suite policy: malformed YAML in {prompt_path} ({exc}); using code defaults"
        )
        return Policy(**values), warnings

    if not isinstance(parsed, dict) or not isinstance(parsed.get("policy"), dict):
        warnings.append(
            f"golden-suite policy: {prompt_path} has no top-level 'policy:' mapping; using code defaults"
        )
        return Policy(**values), warnings

    overrides = parsed["policy"]
    for key, default in values.items():
        if key not in overrides:
            continue
        raw = overrides[key]
        try:
            if isinstance(default, bool):  # pragma: no cover - no bool fields today, guard anyway
                values[key] = bool(raw)
            elif isinstance(default, float):
                values[key] = float(raw)
            elif isinstance(default, int):
                values[key] = int(raw)
            else:
                values[key] = str(raw)
        except (TypeError, ValueError):
            warnings.append(
                f"golden-suite policy: {prompt_path} policy.{key}={raw!r} is invalid; "
                f"using default {default!r}"
            )
    return Policy(**values), warnings


# ---------------------------------------------------------------------------
# benchmarks.yaml
# ---------------------------------------------------------------------------


def load_benchmarks(path: Path) -> list[dict[str, Any]]:
    import yaml

    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    benchmarks = data.get("benchmarks")
    if not isinstance(benchmarks, list) or not benchmarks:
        raise ValueError(f"{path}: 'benchmarks' must be a non-empty list")
    for entry in benchmarks:
        if not isinstance(entry, dict):
            raise ValueError(f"{path}: each benchmark entry must be a mapping, got {entry!r}")
        for required in ("name", "dispatch", "acceptance"):
            if required not in entry:
                raise ValueError(f"{path}: benchmark entry missing {required!r}: {entry!r}")
        dispatch = entry["dispatch"]
        if dispatch.get("mode") not in ("intake_quick", "swarm"):
            raise ValueError(
                f"{path}: benchmark {entry.get('name')!r} has an unknown dispatch.mode "
                f"{dispatch.get('mode')!r}"
            )
    return benchmarks


# ---------------------------------------------------------------------------
# Session token
# ---------------------------------------------------------------------------


def load_session_token(path: Path) -> str:
    token = path.read_text(encoding="utf-8").strip()
    if not token:
        raise ValueError(f"{path}: session token file is empty")
    return token


# ---------------------------------------------------------------------------
# Scratch git repo
# ---------------------------------------------------------------------------


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        check=True,
        capture_output=True,
        text=True,
    )


def make_scratch_repo(runs_dir: Path, date: str, name: str) -> Path:
    """A fresh, empty git repo (branch ``main``, one initial commit) under
    ``runs_dir/<date>/<name>/`` — recreated from scratch every call so a
    re-run never inherits a prior night's files."""
    workdir = runs_dir / date / name
    if workdir.exists():
        shutil.rmtree(workdir)
    workdir.mkdir(parents=True, exist_ok=True)
    _git(workdir, "init", "-b", "main", "-q")
    _git(workdir, "config", "user.email", "golden-suite@omniagentos.local")
    _git(workdir, "config", "user.name", "golden-suite-sentinel")
    (workdir / ".golden-suite").write_text(
        f"golden-suite-sentinel scratch repo — benchmark={name} date={date}\n",
        encoding="utf-8",
    )
    _git(workdir, "add", "-A")
    _git(workdir, "commit", "-q", "-m", f"golden-suite: init scratch repo ({name} {date})")
    return workdir


# ---------------------------------------------------------------------------
# {workdir} substitution
# ---------------------------------------------------------------------------


def substitute_workdir(obj: Any, workdir: str) -> Any:
    if isinstance(obj, str):
        return obj.replace("{workdir}", workdir)
    if isinstance(obj, dict):
        return {k: substitute_workdir(v, workdir) for k, v in obj.items()}
    if isinstance(obj, list):
        return [substitute_workdir(v, workdir) for v in obj]
    return obj


# ---------------------------------------------------------------------------
# Dispatch + poll + acceptance
# ---------------------------------------------------------------------------


@dataclass
class BenchmarkResult:
    name: str
    seconds: float | None
    dnf_reason: str | None
    run_ref: dict[str, Any]


def dispatch_benchmark(
    client: Any, benchmark: dict[str, Any], workdir: Path
) -> tuple[str, dict[str, Any]]:
    """POST the benchmark's dispatch body (with ``{workdir}`` substituted).

    Returns ``(mode, response_payload)``. Raises on any non-2xx response or
    transport error — the caller wraps this in a DNF.
    """
    dispatch = benchmark["dispatch"]
    mode = dispatch["mode"]
    path = dispatch.get("path") or ("/api/swarm" if mode == "swarm" else "/api/intake/quick")
    body = substitute_workdir(dispatch.get("body") or {}, str(workdir))
    resp = client.post(path, json=body, timeout=HTTP_TIMEOUT_SECONDS)
    resp.raise_for_status()
    return mode, resp.json()


def poll_terminal(
    client: Any,
    mode: str,
    payload: dict[str, Any],
    *,
    deadline: float,
    poll_interval: float = DEFAULT_POLL_INTERVAL_SECONDS,
) -> tuple[bool, str | None]:
    """Poll the dispatched run/board-task to a terminal state.

    Returns ``(success, dnf_reason)``. ``success`` True means the run/task
    reached its GREEN terminal status (``completed`` for swarm, ``done`` for
    a board task) — acceptance checks still have to pass on top of that.
    """
    if mode == "swarm":
        run_id = payload.get("swarm_run_id")
        if not run_id:
            return False, "dispatch_response_missing_swarm_run_id"
        while True:
            resp = client.get(f"/api/swarm/{run_id}", timeout=HTTP_TIMEOUT_SECONDS)
            resp.raise_for_status()
            status: str | None = str(resp.json().get("run", {}).get("status") or "")
            if status == _SWARM_TERMINAL_SUCCESS:
                return True, None
            if status in _SWARM_TERMINAL_FAILURE:
                return False, f"swarm_run_{status}"
            if time.monotonic() > deadline:
                return False, "timeout"
            time.sleep(poll_interval)
    else:
        board_task_id = payload.get("board_task_id")
        if not board_task_id:
            return False, "dispatch_response_missing_board_task_id"
        while True:
            resp = client.get("/api/intake/board", timeout=HTTP_TIMEOUT_SECONDS)
            resp.raise_for_status()
            board = resp.json()
            row = next((r for r in board if r.get("id") == board_task_id), None)
            status = str(row.get("status")) if row else None
            if status == _BOARD_TERMINAL_SUCCESS:
                return True, None
            if status in _BOARD_TERMINAL_FAILURE:
                return False, f"board_task_{status}"
            if time.monotonic() > deadline:
                return False, "timeout"
            time.sleep(poll_interval)


def run_acceptance(workdir: Path, checks: list[str]) -> tuple[bool, str | None]:
    """Run each acceptance check (plain ``sh -c``) with CWD=workdir, in
    order. First non-zero exit is the DNF reason; all-pass -> (True, None)."""
    for check in checks:
        proc = subprocess.run(
            ["sh", "-c", check],
            cwd=str(workdir),
            capture_output=True,
            text=True,
        )
        if proc.returncode != 0:
            return False, f"acceptance_failed: {check!r} (rc={proc.returncode})"
    return True, None


def run_one_benchmark(
    client: Any,
    benchmark: dict[str, Any],
    *,
    runs_dir: Path,
    date: str,
    default_timeout_minutes: int,
    poll_interval: float = DEFAULT_POLL_INTERVAL_SECONDS,
) -> BenchmarkResult:
    name = benchmark["name"]
    timeout_minutes = benchmark.get("timeout_minutes") or default_timeout_minutes
    workdir = make_scratch_repo(runs_dir, date, name)
    run_ref: dict[str, Any] = {"scratch_dir": str(workdir)}

    start = time.monotonic()
    try:
        mode, payload = dispatch_benchmark(client, benchmark, workdir)
        run_ref["mode"] = mode
        for key in ("board_task_id", "run_id", "swarm_run_id"):
            if key in payload:
                run_ref[key] = payload[key]
        deadline = start + timeout_minutes * 60.0
        success, dnf_reason = poll_terminal(
            client, mode, payload, deadline=deadline, poll_interval=poll_interval
        )
        if success:
            success, acc_reason = run_acceptance(workdir, benchmark["acceptance"])
            if not success:
                dnf_reason = acc_reason
    except Exception as exc:  # noqa: BLE001 - a bad night must be a DNF, never a crash
        LOG.exception("golden-suite: benchmark %s raised", name)
        success, dnf_reason = False, f"error: {exc}"

    seconds = round(time.monotonic() - start, 3) if success else None
    return BenchmarkResult(name=name, seconds=seconds, dnf_reason=dnf_reason, run_ref=run_ref)


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------


def _utc_date() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%d")


def _resolve_benchmarks_path(policy: Policy, explicit: str | None) -> Path:
    if explicit:
        return Path(explicit)
    candidate = Path(policy.benchmarks_file)
    if not candidate.is_absolute():
        candidate = _SCRIPT_DIR / candidate
    return candidate


def _summary_notes(results: list[BenchmarkResult]) -> str:
    parts = []
    for r in results:
        if r.seconds is not None:
            parts.append(f"{r.name}={r.seconds}s")
        else:
            parts.append(f"{r.name}=DNF({r.dnf_reason})")
    return "golden-suite nightly run: " + ", ".join(parts)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="run_golden.py",
        description="GOLDEN-SUITE SENTINEL: nightly p50/p90 wall-clock-to-GREEN driver.",
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="Parse config + resolve token; no dispatch."
    )
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
    parser.add_argument("--prompt-path", default=str(DEFAULT_PROMPT_PATH))
    parser.add_argument("--benchmarks-path", default=None, help="Overrides policy.benchmarks_file.")
    # Defaults resolve AFTER parsing (see var_root()): argparse must not import
    # the package or touch the environment just to render --help.
    parser.add_argument("--token-path", default=None, help="default: <var>/secrets/sessions-token")
    parser.add_argument("--history-path", default=None, help="default: <var>/golden/history.jsonl")
    parser.add_argument("--runs-dir", default=None, help="default: <var>/golden/runs")
    parser.add_argument(
        "--improvement-log-path", default=None, help="default: <var>/improvement-log.jsonl"
    )
    parser.add_argument("--poll-interval", type=float, default=DEFAULT_POLL_INTERVAL_SECONDS)
    args = parser.parse_args(argv)

    args.token_path = args.token_path or str(default_token_path())
    args.history_path = args.history_path or str(default_history_path())
    args.runs_dir = args.runs_dir or str(default_runs_dir())
    args.improvement_log_path = args.improvement_log_path or str(default_improvement_log_path())

    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s golden-suite %(levelname)s %(message)s"
    )

    prompt_path = Path(args.prompt_path)
    policy, policy_warnings = load_policy(prompt_path)
    for warning in policy_warnings:
        LOG.warning(warning)

    benchmarks_path = _resolve_benchmarks_path(policy, args.benchmarks_path)
    benchmarks = load_benchmarks(benchmarks_path)

    token_path = Path(args.token_path)
    token = load_session_token(token_path)

    if args.dry_run:
        print(
            json.dumps(
                {
                    "dry_run": True,
                    "policy": {
                        "regression_threshold_pct": policy.regression_threshold_pct,
                        "consecutive_nights": policy.consecutive_nights,
                        "rolling_window": policy.rolling_window,
                        "default_timeout_minutes": policy.default_timeout_minutes,
                        "benchmarks_file": policy.benchmarks_file,
                    },
                    "policy_warnings": policy_warnings,
                    "benchmarks_path": str(benchmarks_path),
                    "benchmarks": [
                        {
                            "name": b["name"],
                            "mode": b["dispatch"]["mode"],
                            "timeout_minutes": b.get("timeout_minutes")
                            or policy.default_timeout_minutes,
                            "acceptance_checks": len(b["acceptance"]),
                        }
                        for b in benchmarks
                    ],
                    "token_path": str(token_path),
                    "token_resolved": True,
                    "token_length": len(token),
                },
                indent=2,
                sort_keys=True,
            )
        )
        return 0

    import httpx

    history_path = Path(args.history_path)
    runs_dir = Path(args.runs_dir)
    improvement_log_path = Path(args.improvement_log_path)
    date = _utc_date()

    results: list[BenchmarkResult] = []
    with httpx.Client(base_url=args.base_url, headers={"X-Session-Token": token}) as client:
        for benchmark in benchmarks:
            name = benchmark["name"]
            existing = history_stats.read_history(history_path)
            already = next(
                (e for e in existing if e.get("date") == date and e.get("name") == name), None
            )
            if already is not None:
                LOG.info(
                    "golden-suite: %s already recorded for %s, skipping re-dispatch", name, date
                )
                results.append(
                    BenchmarkResult(
                        name=name,
                        seconds=already.get("seconds"),
                        dnf_reason=already.get("dnf_reason"),
                        run_ref=already.get("run_ref") or {},
                    )
                )
                continue

            LOG.info("golden-suite: dispatching benchmark %s", name)
            result = run_one_benchmark(
                client,
                benchmark,
                runs_dir=runs_dir,
                date=date,
                default_timeout_minutes=policy.default_timeout_minutes,
                poll_interval=args.poll_interval,
            )
            if result.seconds is not None:
                LOG.info("golden-suite: %s GREEN in %ss", name, result.seconds)
            else:
                LOG.warning("golden-suite: %s DNF (%s)", name, result.dnf_reason)
            history_stats.append_history(
                history_path,
                {
                    "date": date,
                    "name": name,
                    "seconds": result.seconds,
                    "dnf_reason": result.dnf_reason,
                    "run_ref": result.run_ref,
                },
            )
            results.append(result)

    # Regression check, once per distinct benchmark name in this run.
    history = history_stats.read_history(history_path)
    for benchmark in benchmarks:
        name = benchmark["name"]
        if history_stats.check_regression(
            history,
            name,
            threshold_pct=policy.regression_threshold_pct,
            consecutive_nights=policy.consecutive_nights,
            window=policy.rolling_window,
        ):
            rollup = history_stats.rolling_percentiles(history, name, window=policy.rolling_window)
            LOG.warning("golden-suite: REGRESSION on %s (rollup=%s)", name, rollup)
            try:
                from omniagentos.notifications.service import record_notification

                record_notification(
                    kind="alert",
                    title=f"Golden-suite regression: {name}",
                    body=(
                        f"{policy.consecutive_nights} consecutive nights >"
                        f"{policy.regression_threshold_pct}% worse than the rolling "
                        f"{policy.rolling_window}-night median (rolling p50="
                        f"{rollup['p50']}, p90={rollup['p90']}, n={rollup['n']})."
                    ),
                    severity="warning",
                    ref_type="golden_suite_regression",
                    ref_id=f"golden:{name}",
                    push=False,
                )
            except Exception:  # noqa: BLE001 - a notification failure must never fail the sentinel
                LOG.exception("golden-suite: record_notification failed for %s", name)

    # Always append exactly one improvement-log line, win or lose.
    improvement_log_path.parent.mkdir(parents=True, exist_ok=True)
    with improvement_log_path.open("a", encoding="utf-8") as fh:
        fh.write(
            json.dumps(
                {
                    "ts": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "improver": "golden-sentinel",
                    "changes": [],
                    "notes": _summary_notes(results),
                },
                sort_keys=True,
            )
        )
        fh.write("\n")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
