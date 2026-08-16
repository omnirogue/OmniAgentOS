#!/usr/bin/env python3
"""Advisory-only wrapper around `bridge/integration.py --once --dry-run`.

WHY THIS FILE EXISTS: `integration.py --dry-run` prints its report to stdout
and, by design, "writes nothing" (see its own --apply help text) — that is
exactly what makes it safe to run on a timer. But a report nobody durably
records is a report nobody can read five minutes later, so this wrapper:

  1. runs `integration.py --once --dry-run` as a subprocess (never --apply,
     never passed through from the caller — hardcoded, so this file cannot
     be pointed at a landing run by an argument mistake),
  2. captures its stdout JSON report,
  3. stamps it with `generated_at`,
  4. writes it atomically to <loops-root>/state/advice.json.

This is ADVISORY ONLY. The coordinator (human or agent loop) reads
advice.json, CONFIRMS it against the live queue, and acts — it is never
treated as authority on its own, and this process never touches
candidates/, rejected/, ledger.jsonl, or the gate workspace. See
CONTRACT.md's instrument-vs-candidate-defect split for why a advisory
report is not a verdict.

STALENESS (Kimi, 2026-08-08 bottleneck review): a stale advice.json read as
fresh is the queue.json failure shape repeating — a producer trusting a
snapshot that stopped updating and silently treating it as current. Every
run of this wrapper FIRST checks the age of the advice.json IT IS ABOUT TO
OVERWRITE. If that file's `generated_at` is already older than
--max-age-s (default 600s = 10 min), the timer missed its own cadence and
this files a LOUD alert to <loops-root>/ALERTS.md (the same file and same
"- <iso> <source>: <msg>" line shape `Integration._alert` in integration.py
uses) before proceeding. `--check-only` runs just this staleness check
(no subprocess, no write) so a coordinator loop can poll it independently
of the 300s write cadence.

BASE-RED (sha256:4f9469143d65a80a1c4d7f08aa0c3df1b8ca06d0dd2a43e634da63d2c75e00cd):
when a `--github-repo` is configured, `run_once` additionally calls the
READ-ONLY `base_red_report.detect_base_red` and attaches its result to the
advice record under `base_red`. Same discipline as `adapter_error` above: a
detector failure (a raised exception, not just its own handled `error` key)
is recorded IN the advice under `base_red.error`, never dropped, never
rendered as null or an empty `false_red_prs`. Without `--github-repo` the
key is simply absent — this is an optional advisory line, not a required
one, so an unconfigured repo is not itself a failure.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
INTEGRATION_PY = HERE / "integration.py"

# Import the push transport the same way as a sibling module. advice_writer is
# imported as `bridge.advice_writer` in tests AND run as a bare script; putting
# the bridge dir on sys.path makes `import notify` resolve in both.
sys.path.insert(0, str(HERE))
import base_red_report as _base_red_report  # noqa: E402
import notify as _notify  # noqa: E402

TS_FMT = "%Y-%m-%dT%H:%M:%SZ"


def _iso_now() -> str:
    return datetime.now(UTC).strftime(TS_FMT)


def _parse_ts(raw: str) -> datetime:
    return datetime.strptime(raw, TS_FMT).replace(tzinfo=UTC)


def _append_alert(loops_root: Path, msg: str, source: str = "advice") -> None:
    # 1. ALWAYS append the durable ALERTS.md line first — this is the record
    #    that must never be lost, and it lands before any network is touched.
    with open(loops_root / "ALERTS.md", "a", encoding="utf-8") as fh:
        fh.write(f"- {_iso_now()} {source}: {msg}\n")
    # 2. THEN push it to the operator's phone if OMNI_NTFY_URL is set. This is
    #    the SAME single point that decided to write the line, so one alert =
    #    one file line + one push. push_alert is fail-soft: an unset URL or any
    #    network error is swallowed (and, on failure, noted back in ALERTS.md),
    #    so a dead push channel can never raise into the advice timer.
    _notify.push_alert(
        loops_root,
        title=f"ThreeLoops alert ({source})",
        body=msg,
        source=source,
        priority="high",
        tags="warning",
    )


# Ordinary NTP/host clock jitter, not a grace window: a generated_at up to
# this far in the future is tolerated without an alert; anything beyond it
# is treated as a clock or integrity problem (F4, see check_staleness).
CLOCK_SKEW_TOLERANCE_S = 60


def check_staleness(loops_root: Path, max_age_s: int = 600) -> str | None:
    """SILENCE IS THE NARROW CASE, BY CONSTRUCTION: this function returns
    None for exactly one shape -- advice.json exists, parses, carries a
    well-formed generated_at, and that timestamp is neither more than
    CLOCK_SKEW_TOLERANCE_S in the future nor more than max_age_s in the
    past. Every other path -- missing, empty, unreadable, unparseable JSON,
    a missing/malformed generated_at, a future-dated generated_at, or a
    too-old one -- returns a message and appends it to ALERTS.md. Read
    top-to-bottom: each branch is a `return`, so nothing downstream can
    silently override or skip a loud verdict already reached.

    Two rounds of cross-lineage review (gemini-3.1-pro-preview, 2026-08-08)
    found this function letting a bad case through as favourable silence:
    F1 was a missing file read as "bootstrap, not staleness"; F4 is a
    FUTURE generated_at (clock skew, or tampering) making age_s negative,
    so the plain `age_s > max_age_s` check never fired. Both are the same
    class -- an absent or misleading signal must never read as an
    all-clear -- and both are now closed by construction: there is no
    remaining branch in this function that computes an age and then does
    not check it against a bound before falling through.

    A future timestamp is reported on ITS OWN TERMS, not folded into the
    staleness message: a file dated in the future is a clock/integrity
    problem, not a stale one, and conflating them would send whoever reads
    ALERTS.md to debug data freshness when the actual fault is a clock.
    """
    target = loops_root / "state" / "advice.json"

    if not target.exists():
        msg = ("state/advice.json is MISSING — there is no advice for the coordinator to "
               "confirm against. An absent file is an instrument result, never an "
               "all-clear: it means either the timer has never fired, or it stopped "
               "before ever writing once, and those two cases are indistinguishable from "
               "here. Do not proceed as if this is a quiet cold start")
        _append_alert(loops_root, msg)
        return msg

    try:
        data = json.loads(target.read_text(encoding="utf-8"))
        gen = data.get("generated_at")
        if not isinstance(gen, str):
            raise ValueError(f"generated_at missing or not a string: {gen!r}")
        ts = _parse_ts(gen)
    except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
        msg = (f"state/advice.json is unreadable or malformed ({type(exc).__name__}: {exc}) "
               "-- staleness cannot be assessed, and an instrument that cannot assess its "
               "own freshness must not be trusted silently; treating this as an ALERT")
        _append_alert(loops_root, msg)
        return msg

    age_s = (datetime.now(UTC) - ts).total_seconds()

    if age_s < -CLOCK_SKEW_TOLERANCE_S:
        msg = (f"state/advice.json's generated_at is {abs(age_s) / 60:.1f} min in the "
               f"FUTURE (beyond the {CLOCK_SKEW_TOLERANCE_S}s clock-jitter tolerance) -- "
               "this is a clock or integrity problem, not staleness. Do not chase a "
               "data-freshness bug here; check the clock on whatever host wrote this file")
        _append_alert(loops_root, msg)
        return msg

    if age_s > max_age_s:
        msg = (f"state/advice.json is {age_s / 60:.1f} min old (> {max_age_s / 60:.0f} min "
               "threshold) -- the advice timer stopped firing or is stuck; the coordinator "
               "must NOT use this snapshot to confirm decisions until it refreshes")
        _append_alert(loops_root, msg)
        return msg

    return None  # the ONE narrow silent case: exists, parses, neither future nor stale


def _run_base_red_detector(github_repo: str) -> dict:
    """Calls the read-only detector and NEVER lets an unexpected exception
    escape into `run_once` uncaught — a crash here must be recorded in the
    advice the same way `adapter_error` records a crashed subprocess, never
    silently drop the whole `base_red` key."""
    try:
        return _base_red_report.detect_base_red(github_repo)
    except Exception as exc:  # noqa: BLE001 — deliberately broad: any crash
        # here must be recorded, never propagate and abort the whole advice
        # write, and never be swallowed into an absent key either.
        return {
            "generated_at": _iso_now(),
            "false_red_prs": None,
            "error": f"base_red_report.detect_base_red crashed: "
                     f"{type(exc).__name__}: {exc}",
        }


def run_once(loops_root: Path, repo: Path | None, python: str, timeout: int = 280,
             github_repo: str | None = None) -> dict:
    """Run `integration.py --once --dry-run` as a subprocess and return the
    advice record to be written. Never raises on adapter failure — a failed
    adapter run is recorded IN the advice, not silently dropped."""
    cmd = [python, str(INTEGRATION_PY), "--loops-root", str(loops_root), "--once", "--dry-run"]
    if repo is not None:
        cmd += ["--repo", str(repo)]
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        advice = {"generated_at": _iso_now(), "rc": None,
                  "summary": {}, "adapter_error": f"timed out after {timeout}s: {exc}"}
        if github_repo:
            advice["base_red"] = _run_base_red_detector(github_repo)
        return advice
    out = p.stdout
    idx = out.find("{")
    summary: dict = {}
    parse_error: str | None = None
    if idx == -1:
        parse_error = f"no JSON object found in stdout (rc={p.returncode})"
    else:
        try:
            summary, _ = json.JSONDecoder().raw_decode(out[idx:])
        except (ValueError, json.JSONDecodeError) as exc:
            parse_error = f"{type(exc).__name__}: {exc}"
    advice: dict = {"generated_at": _iso_now(), "rc": p.returncode, "summary": summary}
    if parse_error:
        advice["parse_error"] = parse_error
    summary_error = summary.get("error") if isinstance(summary, dict) else None
    if p.returncode != 0 or parse_error or summary_error:
        # Never write adapter_error as None here -- same "an absent/None
        # signal reads as favourable" class as base_sha and the receipt
        # sites: a non-zero rc or a parse failure IS a real failure even
        # when the adapter's own JSON carries no "error" key (e.g. it
        # crashed before it could format one), so this must never fall
        # through to a null that a consumer's `if advice.get("adapter_error")`
        # then reads as "no error".
        if summary_error:
            err = summary_error
        elif parse_error:
            err = parse_error
        else:
            err = f"adapter exited {p.returncode} with no 'error' key in its JSON summary"
        advice["adapter_error"] = err
        advice["stderr_tail"] = p.stderr[-2000:]
    if github_repo:
        advice["base_red"] = _run_base_red_detector(github_repo)
    return advice


def write_advice(loops_root: Path, advice: dict) -> Path:
    state_dir = loops_root / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    target = state_dir / "advice.json"
    tmp = target.with_suffix(target.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(advice, fh, indent=2)
        fh.flush()
        os.fsync(fh.fileno())
    tmp.rename(target)  # same directory: rename is atomic
    return target


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Advisory-only wrapper: runs integration.py --once --dry-run and records "
                    "its report to state/advice.json with a generated_at timestamp. Never "
                    "--apply, never lands anything -- the coordinator confirms and acts.")
    ap.add_argument("--loops-root", required=True, type=Path)
    ap.add_argument("--repo", type=Path, default=None)
    ap.add_argument("--python", default=sys.executable,
                    help="interpreter used to run integration.py (default: this interpreter)")
    ap.add_argument("--max-age-s", type=int, default=600,
                    help="staleness threshold in seconds (default 600 = 10 min)")
    ap.add_argument("--check-only", action="store_true",
                    help="only assess staleness of the EXISTING advice.json; run no subprocess "
                         "and write nothing")
    ap.add_argument("--github-repo", default=os.environ.get("GH_REPO"),
                    help="owner/repo slug; when set, attaches the read-only base-red "
                         "false-red detector's result under advice['base_red'] "
                         "(default: $GH_REPO; unset disables this advisory line)")
    args = ap.parse_args(argv)

    root: Path = args.loops_root.resolve()
    if not root.is_dir():
        print(f"no such queue: {root}", file=sys.stderr)
        return 2

    alert = check_staleness(root, args.max_age_s)
    if alert:
        print(f"ALERT: {alert}")

    if args.check_only:
        return 0

    advice = run_once(root, args.repo, args.python, github_repo=args.github_repo)
    target = write_advice(root, advice)
    print(f"wrote {target} (rc={advice.get('rc')})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
