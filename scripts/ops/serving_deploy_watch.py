#!/usr/bin/env python3
"""serving_deploy_watch.py — redeploy live services when the serving checkout advances.

The serving checkout advances by fast-forward only (gate-loop / train / hand
``pull --ff-only``), but nothing redeployed what those advances changed: the
dashboard launchd service serves a prebuilt ``dashboard/.next-remote`` and the
API serves whatever code it imported at boot. Measured 2026-08-14: the
``/testing`` page 404'd for hours after its merge because ``.next-remote`` was
never rebuilt — launchd's KeepAlive faithfully respawning the stale build.

Each tick (launchd, StartInterval):

1. HEAD == last deployed SHA → exit quietly (the overwhelmingly common tick).
2. Classify ``git diff --name-only deployed..HEAD``:
   - ``omniagentos/`` or ``contracts/`` → kickstart the API service.
   - ``dashboard/`` → rebuild into a STAGING dist dir, atomically swap it into
     ``.next-remote``, then kickstart the dashboard service. The live dir is
     never built into directly, so a failed build leaves the old UI serving.
   - neither → just advance the stamp (docs-only advances deploy nothing).
3. A failed build parks that SHA after ``MAX_FAILURES`` attempts (never
   re-buy the same red — gate-retry doctrine); a NEW head clears the park.
4. Build is load-guarded (>0.8 of cores skips the tick; kickstarts are cheap
   and always proceed): an overloaded box turns green builds red.

State: ``var/deploy-watch/state.json``. Log: ``var/log/serving-deploy-watch.log``.
Lock: ``var/locks/serving-deploy-watch.lock`` (mkdir; stale after 30 min).

launchd hands the job a stripped environment, so every binary is resolved
to an absolute path here; ``npm`` comes from the same nvm install the
dashboard service plist names, with its bin dir prepended to the child PATH.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]

GIT = "/usr/bin/git"
LAUNCHCTL = "/bin/launchctl"
NPM_CANDIDATES = (
    "/Users/youruser/.nvm/versions/node/v22.22.0/bin/npm",
    "/opt/homebrew/bin/npm",
    "/usr/local/bin/npm",
)

API_SERVICE = "com.omniagentos.api"
DASH_SERVICE = "com.omniagentos.dashboard"

# Path prefixes that make an advance deploy-relevant.
API_PREFIXES = ("omniagentos/", "contracts/")
DASH_PREFIX = "dashboard/"

LIVE_DIST = ".next-remote"
STAGING_DIST = ".next-remote-staging"
RETIRED_DIST = ".next-remote-retired"

MAX_FAILURES = 2
LOAD_CEILING = 0.8
BUILD_TIMEOUT_S = 900
LOCK_STALE_S = 30 * 60

STATE_PATH = REPO_ROOT / "var" / "deploy-watch" / "state.json"
LOG_PATH = REPO_ROOT / "var" / "log" / "serving-deploy-watch.log"
LOCK_PATH = REPO_ROOT / "var" / "locks" / "serving-deploy-watch.lock"


def log(message: str) -> None:
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).isoformat(timespec="seconds")
    with LOG_PATH.open("a", encoding="utf-8") as handle:
        handle.write(f"{stamp} {message}\n")


def read_state() -> dict[str, object]:
    try:
        raw = json.loads(STATE_PATH.read_text(encoding="utf-8"))
        return raw if isinstance(raw, dict) else {}
    except (OSError, ValueError):
        return {}


def write_state(state: dict[str, object]) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")
    tmp.replace(STATE_PATH)


def classify(paths: list[str]) -> tuple[bool, bool]:
    """(api_changed, dashboard_changed) for one advance's changed paths."""
    api = any(p.startswith(API_PREFIXES) for p in paths)
    dash = any(p.startswith(DASH_PREFIX) for p in paths)
    return api, dash


def load_ratio() -> float:
    cores = os.cpu_count() or 1
    return os.getloadavg()[0] / cores


def resolve_npm() -> str | None:
    for candidate in NPM_CANDIDATES:
        if Path(candidate).is_file():
            return candidate
    return shutil.which("npm")


def git_head(run: callable = subprocess.run) -> str | None:
    proc = run([GIT, "-C", str(REPO_ROOT), "rev-parse", "HEAD"],
               capture_output=True, text=True, timeout=30)
    return proc.stdout.strip() if proc.returncode == 0 and proc.stdout.strip() else None


def changed_paths(old: str, new: str, run: callable = subprocess.run) -> list[str] | None:
    """Files changed old..new, or None when the range cannot be resolved.

    None (e.g. first run, or ``old`` garbage-collected) deploys BOTH targets:
    over-deploying once is cheap, silently skipping a real change is the bug
    this watcher exists to kill.
    """
    if not old:
        return None
    proc = run([GIT, "-C", str(REPO_ROOT), "diff", "--name-only", f"{old}..{new}"],
               capture_output=True, text=True, timeout=60)
    if proc.returncode != 0:
        return None
    return [line.strip() for line in proc.stdout.splitlines() if line.strip()]


def kickstart(service: str, run: callable = subprocess.run) -> bool:
    target = f"gui/{os.getuid()}/{service}"
    proc = run([LAUNCHCTL, "kickstart", "-k", target], capture_output=True, text=True, timeout=60)
    ok = proc.returncode == 0
    log(f"kickstart {service}: {'ok' if ok else 'FAILED rc=' + str(proc.returncode)}")
    return ok


def build_dashboard(head: str, run: callable = subprocess.run) -> bool:
    """Build into the staging dist dir; atomic swap into live only on success."""
    npm = resolve_npm()
    if npm is None:
        log("build SKIPPED: no npm binary found")
        return False
    dash_dir = REPO_ROOT / "dashboard"
    staging = dash_dir / STAGING_DIST
    live = dash_dir / LIVE_DIST
    retired = dash_dir / RETIRED_DIST
    shutil.rmtree(staging, ignore_errors=True)

    env = dict(os.environ)
    env["OMNIAGENTOS_NEXT_DIST_DIR"] = STAGING_DIST
    env["NEXT_PUBLIC_BUILD_SHA"] = head
    env["PATH"] = f"{Path(npm).parent}:{env.get('PATH', '/usr/bin:/bin')}"

    proc = run([npm, "run", "build"], cwd=str(dash_dir), env=env,
               capture_output=True, text=True, timeout=BUILD_TIMEOUT_S)
    if proc.returncode != 0:
        tail = (proc.stdout or "")[-400:] + (proc.stderr or "")[-400:]
        log(f"build FAILED rc={proc.returncode}: ...{tail!r}")
        return False

    shutil.rmtree(retired, ignore_errors=True)
    if live.exists():
        live.rename(retired)
    staging.rename(live)
    shutil.rmtree(retired, ignore_errors=True)
    log(f"build ok -> {LIVE_DIST} swapped (sha {head[:12]})")
    return True


def acquire_lock() -> bool:
    LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    try:
        LOCK_PATH.mkdir()
    except FileExistsError:
        try:
            if time.time() - LOCK_PATH.stat().st_mtime > LOCK_STALE_S:
                LOCK_PATH.rmdir()
                LOCK_PATH.mkdir()
                log("stale lock taken over")
                return True
        except OSError:
            pass
        return False
    return True


def release_lock() -> None:
    try:
        LOCK_PATH.rmdir()
    except OSError:
        pass


def tick(run: callable = subprocess.run) -> int:
    head = git_head(run)
    if head is None:
        log("SKIP: could not resolve HEAD")
        return 1

    state = read_state()
    deployed = str(state.get("deployed_sha") or "")
    if head == deployed:
        return 0

    if head == str(state.get("failed_sha") or "") and int(state.get("fail_count") or 0) >= MAX_FAILURES:
        return 0  # parked: never re-buy the same red; a new head clears this

    paths = changed_paths(deployed, head, run)
    api, dash = (True, True) if paths is None else classify(paths)

    if not api and not dash:
        write_state({"deployed_sha": head})
        log(f"advance to {head[:12]}: no deploy-relevant paths")
        return 0

    if dash and load_ratio() > LOAD_CEILING:
        log(f"SKIP build: load ratio {load_ratio():.2f} > {LOAD_CEILING} (retry next tick)")
        return 0

    ok = True
    if api:
        ok = kickstart(API_SERVICE, run) and ok
    if dash:
        built = build_dashboard(head, run)
        ok = built and ok
        if built:
            ok = kickstart(DASH_SERVICE, run) and ok

    if ok:
        write_state({"deployed_sha": head})
        log(f"deployed {head[:12]} (api={api} dash={dash})")
        return 0

    failures = int(state.get("fail_count") or 0) + 1 if head == str(state.get("failed_sha") or "") else 1
    write_state({"deployed_sha": deployed, "failed_sha": head, "fail_count": failures})
    log(f"deploy FAILED for {head[:12]} (attempt {failures}/{MAX_FAILURES})")
    return 1


def main() -> int:
    if not acquire_lock():
        return 0  # another tick is mid-deploy; this one simply yields
    try:
        return tick()
    finally:
        release_lock()


if __name__ == "__main__":
    sys.exit(main())
