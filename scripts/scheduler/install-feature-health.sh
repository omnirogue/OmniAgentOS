#!/bin/sh
# Render the feature-health lane launchd jobs.
#
# DELIBERATELY DOES NOT `launchctl load` (repo convention — see
# install-routines.sh): this script only writes the .plist files and prints the
# exact load commands.
#
# com.omniagentos.feature-health-tier1 — StartInterval 14400 (every 4h):
#   run.sh tier1 --runner launchd && run.sh tier3 --runner launchd
# com.omniagentos.feature-health-nightly — StartCalendarInterval 03:10:
#   run.sh tier2 --runner launchd && run.sh tier3 --live-probes --runner launchd
#
# Unlike the routines job, these jobs source NOTHING — no connections.env, no
# launch-env.sh, no login shell. The lane depends on pytest-side isolation
# (tests/conftest.py pins tmp OMNIAGENTOS_DB/VAR_DIR); pre-sourcing the product
# env would fight that isolation. launchd's plain sanitized environment is
# exactly what the lane wants; run.sh pins its own interpreter paths.
set -eu

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
ROOT_DIR=$(CDPATH= cd -- "$SCRIPT_DIR/../.." && pwd)

# ---- canonical render dir ---------------------------------------------------
# Decision recorded 2026-08-08 (lane r05-feature-health). THREE rendered-plist
# dirs exist on this estate and only one of them is ever loaded from:
#
#   var/launchd/rendered              CANONICAL. Holds all 26 currently-loaded
#                                     com.omniagentos.* plists, and
#                                     docs/OPS-RUNTIME.md documents `launchctl
#                                     load var/launchd/rendered/...` from it.
#   var/runtime/launchd/rendered What the shared installer expression
#                                     ($OMNIAGENTOS_VAR_DIR default) resolves to
#                                     in a PLAIN shell — and this installer
#                                     deliberately sources nothing, so that is
#                                     the normal case. Holds one stale plist;
#                                     nothing has ever been loaded from it.
#   var/launchd/rendered.bak-db-path-fix
#                                     A pre-DB-path-fix BACKUP. Its name records
#                                     the defect. Bootstrapping from here loads a
#                                     job pointed at the wrong database: loaded,
#                                     green, and useless — a favourable-absence
#                                     failure that no exit code reveals.
#
# Landing in the wrong one is silent, which is exactly why this installer refuses
# instead of rendering. An explicit override is still honoured, but only with an
# equally explicit acknowledgement (tests and dry runs use it).
CANONICAL_DIR="$ROOT_DIR/var/launchd/rendered"

if [ -n "${OMNIAGENTOS_LAUNCHD_TARGET_DIR:-}" ]; then
  TARGET_DIR=$OMNIAGENTOS_LAUNCHD_TARGET_DIR
  TARGET_SOURCE="OMNIAGENTOS_LAUNCHD_TARGET_DIR"
elif [ -n "${OMNIAGENTOS_VAR_DIR:-}" ]; then
  TARGET_DIR="$OMNIAGENTOS_VAR_DIR/launchd/rendered"
  TARGET_SOURCE="OMNIAGENTOS_VAR_DIR"
else
  TARGET_DIR=$CANONICAL_DIR
  TARGET_SOURCE="default (canonical)"
fi

# Resolve both sides before comparing so a symlink or a trailing slash cannot
# read as a different directory. A path that does not exist yet stays literal.
abspath() {
  _p=$1
  # A relative target is relative to the repo root, not to the caller's cwd —
  # otherwise the same string compares canonical from one directory and
  # non-canonical from another.
  case "$_p" in /*) ;; *) _p="$ROOT_DIR/$_p" ;; esac
  # Strip EVERY trailing slash, not just one: "rendered///" must not read as a
  # different directory from "rendered".
  while : ; do
    case "$_p" in */) _p=${_p%/} ;; *) break ;; esac
  done
  [ -n "$_p" ] || _p=/
  if [ -d "$_p" ]; then (CDPATH= cd -- "$_p" && pwd); else printf '%s\n' "$_p"; fi
}
TARGET_ABS=$(abspath "$TARGET_DIR")
CANONICAL_ABS=$(abspath "$CANONICAL_DIR")

if [ "$TARGET_ABS" != "$CANONICAL_ABS" ] \
   && [ "${OMNIAGENTOS_LAUNCHD_TARGET_DIR_ACK:-0}" != "1" ]; then
  cat >&2 <<EOF
error: refusing to render the feature-health plists into a non-canonical dir.
  resolved target: $TARGET_ABS
  set by:          $TARGET_SOURCE
  canonical:       $CANONICAL_ABS

A plist rendered outside the canonical dir is never loaded (so the lane stays
dead), or is loaded from a stale backup pointed at the wrong database (so the
lane looks green and reports nothing true). Both failures are silent.

Render canonically:
  OMNIAGENTOS_LAUNCHD_TARGET_DIR="$CANONICAL_DIR" sh scripts/scheduler/install-feature-health.sh

Rendering elsewhere on purpose (tests, dry runs) needs an explicit ack:
  OMNIAGENTOS_LAUNCHD_TARGET_DIR=<dir> OMNIAGENTOS_LAUNCHD_TARGET_DIR_ACK=1 \\
    sh scripts/scheduler/install-feature-health.sh
EOF
  exit 3
fi

mkdir -p "$TARGET_DIR"
mkdir -p "$ROOT_DIR/var/feature-health"

# launchd's minimal PATH resolves python3 to the system 3.9; OmniAgentOS needs
# 3.12+. Same pin convention as install-routines.sh (the renderer runs on it too).
if [ -x "$ROOT_DIR/.venv/bin/python" ]; then
  PYBIN="$ROOT_DIR/.venv/bin/python"
elif command -v python3.12 >/dev/null 2>&1; then
  PYBIN=$(command -v python3.12)
else
  echo "error: no 3.12+ interpreter found (.venv/bin/python or python3.12); run 'uv sync' first" >&2
  exit 1
fi

"$PYBIN" - "$ROOT_DIR" "$TARGET_DIR" <<'PY'
import shlex
import sys
from html import escape
from pathlib import Path

root = sys.argv[1]
sys.path.insert(0, root)
from scripts.lib.plist_write import write_plist_atomic

root = sys.argv[1]
target_dir = Path(sys.argv[2])

RUN = str(Path(root) / "scripts" / "feature_health" / "run.sh")

JOBS = {
    "com.omniagentos.feature-health-tier1": {
        "schedule": "    <key>StartInterval</key><integer>14400</integer>",
        "commands": [
            [RUN, "tier1", "--runner", "launchd"],
            [RUN, "tier3", "--runner", "launchd"],
        ],
    },
    "com.omniagentos.feature-health-nightly": {
        "schedule": (
            "    <key>StartCalendarInterval</key>\n"
            "    <dict>\n"
            "        <key>Hour</key><integer>3</integer>\n"
            "        <key>Minute</key><integer>10</integer>\n"
            "    </dict>"
        ),
        "commands": [
            [RUN, "tier2", "--runner", "launchd"],
            [RUN, "tier3", "--live-probes", "--runner", "launchd"],
        ],
    },
}

TEMPLATE = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<!-- Product-scoped OmniAgentOS job; rendered installers never auto-load it. -->
<!-- Deliberately sources NOTHING (no launch-env.sh): the feature-health lane
     relies on pytest-side isolation and must run with launchd's plain env. -->
<dict>
    <key>Label</key><string>{label}</string>
    <key>ProgramArguments</key>
    {program_args}
    <!-- Attests that launchd (not a human) made this run. run.sh and fh.py refuse
         to stamp a `runner: launchd` ledger record without it, so the freshness
         oracle cannot be satisfied by a hand-typed launchd runner flag. -->
    <key>EnvironmentVariables</key>
    <dict>
        <key>FH_LAUNCHD</key><string>1</string>
    </dict>
{schedule}
    <key>WorkingDirectory</key><string>{root}</string>
    <key>StandardOutPath</key><string>{log}</string>
    <key>StandardErrorPath</key><string>{log}</string>
</dict>
</plist>
"""


def args_xml(program_args):
    values = "\n".join(
        f"        <string>{escape(arg)}</string>" for arg in program_args
    )
    return "<array>\n" + values + "\n    </array>"


for label, job in JOBS.items():
    # Chain the two lane invocations; run.sh in launchd mode always exits 0, so
    # the second command runs regardless of ledger contents. No -l: never a
    # login shell (a login shell would re-source launch-env.sh and repoint the DB).
    shell_cmd = " && ".join(
        " ".join(shlex.quote(part) for part in cmd) for cmd in job["commands"]
    )
    program_args = ["/bin/sh", "-c", shell_cmd]
    log = str(Path(root) / "var" / "feature-health" / f"launchd-{label}.log")
    rendered = TEMPLATE.format(
        label=escape(label),
        program_args=args_xml(program_args),
        schedule=job["schedule"],
        root=escape(root),
        log=escape(log),
    )
    target = target_dir / f"{label}.plist"
    write_plist_atomic(target, rendered)
    print(f"Wrote {target}")
PY

GUI_DOMAIN="gui/$(id -u)"

if [ "$TARGET_ABS" != "$CANONICAL_ABS" ]; then
  echo
  echo "!! NON-CANONICAL DRY-RUN RENDER ($TARGET_ABS)."
  echo "!! Do NOT bootstrap the paths below on the estate — the canonical dir is"
  echo "!! $CANONICAL_ABS. These plists exist for inspection only."
fi

echo
echo "NOT loaded (repo convention: render only). Load after review:"
echo
echo "  # 1. A label in launchd's DISABLED override DB ignores bootstrap silently."
echo "  #    Five estate labels already sit in it — check before, and enable first."
echo "  launchctl print-disabled $GUI_DOMAIN | grep feature-health"
for label in com.omniagentos.feature-health-tier1 com.omniagentos.feature-health-nightly; do
  echo "  launchctl enable $GUI_DOMAIN/$label"
done
echo
echo "  # 2. Bootstrap from the canonical dir only."
for label in com.omniagentos.feature-health-tier1 com.omniagentos.feature-health-nightly; do
  echo "  launchctl bootstrap $GUI_DOMAIN $TARGET_DIR/$label.plist"
done
echo
echo "  # 3. Verify. A loaded label is NOT an activated lane — this lane's runner"
echo "  #    exits 0 in launchd mode by design, so grade LEDGER FRESHNESS."
echo "  #    Grade EACH job: an unfiltered check stays green off whichever one is"
echo "  #    alive. tier1 comes only from the 4h job, tier2 only from the nightly."
echo "  #    Freshness proves the job FIRED; read newest_status/newest_aborted in"
echo "  #    the JSON to see whether it did any work."
echo "  launchctl list | grep feature-health"
echo "  $PYBIN $ROOT_DIR/scripts/feature_health/fh.py freshness --runner launchd --tier tier1 --max-age-s 18000 --json   # 4h job (after ~4h)"
echo "  $PYBIN $ROOT_DIR/scripts/feature_health/fh.py freshness --runner launchd --tier tier2 --max-age-s 108000 --json  # nightly (after 03:10)"
echo
echo "  # Rollback:"
for label in com.omniagentos.feature-health-tier1 com.omniagentos.feature-health-nightly; do
  echo "  launchctl bootout $GUI_DOMAIN/$label"
done
