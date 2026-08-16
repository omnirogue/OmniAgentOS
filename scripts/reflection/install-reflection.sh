#!/bin/sh
# Render the nightly reflection and watchdog launchd jobs
# (com.omniagentos.reflection-nightly and com.omniagentos.reflection-watchdog).
#
# D3 / N4r: exit 126 root cause was `exec "<script>"` requiring the script to be
# executable; when mode bits were lost, /bin/sh reported "Permission denied" /
# "cannot execute" (exit 126). Fix: ProgramArguments is always
#   /bin/sh <absolute-path-to-job-script>
# so the job runs even if the script loses +x. Scripts still ship mode 0755 and
# the installer refuses to render if they are missing.
#
# Re-arm is gated by OMNIAGENTOS_REFLECTION_REARM_MODE (default off). This
# installer only RENDERS plists — it never bootstrap/loads into the live domain.
# Observe-only remains True; one shadow week of clean runs is required before
# any observe_only=False discussion (see docs/runbooks/reflection-jobs-exit126.md).
#
# Schedule:
#   - Nightly Reflection: 02:30 daily
#   - Watchdog: 07:30 daily
set -eu

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
ROOT_DIR=$(CDPATH= cd -- "$SCRIPT_DIR/../.." && pwd)
. "$ROOT_DIR/scripts/lib/launchd-label.sh"

# Nightly Loop params
LABEL_NIGHTLY=${OMNIAGENTOS_REFLECTION_NIGHTLY_LABEL:-com.omniagentos.reflection-nightly}
require_safe_launchd_label "$LABEL_NIGHTLY"
HOUR_NIGHTLY=${OMNIAGENTOS_REFLECTION_NIGHTLY_HOUR:-2}
MINUTE_NIGHTLY=${OMNIAGENTOS_REFLECTION_NIGHTLY_MINUTE:-30}

# Watchdog params
LABEL_WATCHDOG=${OMNIAGENTOS_REFLECTION_WATCHDOG_LABEL:-com.omniagentos.reflection-watchdog}
require_safe_launchd_label "$LABEL_WATCHDOG"
HOUR_WATCHDOG=${OMNIAGENTOS_REFLECTION_WATCHDOG_HOUR:-7}
MINUTE_WATCHDOG=${OMNIAGENTOS_REFLECTION_WATCHDOG_MINUTE:-30}

# Both labels above come from the environment and are interpolated straight into
# ${TARGET_DIR}/${LABEL}.plist below, so an unvalidated label writes outside the
# render directory (issue #95; the class swept in #21). Refuse the shape before
# anything is created — `case` and not `$(...)`, because command substitution
# strips trailing newlines and would accept a label whose only bad character is one.
_require_safe_label() {
  _rsl_var=$1
  _rsl_label=$2
  case "$_rsl_label" in
    '' | .* | -* | *[!A-Za-z0-9._-]*)
      echo "error: refusing unsafe launchd label from ${_rsl_var}: '${_rsl_label}'" >&2
      echo "       expected [A-Za-z0-9._-], not starting with '.' or '-'" >&2
      exit 1
      ;;
  esac
}

_require_safe_label OMNIAGENTOS_REFLECTION_NIGHTLY_LABEL "$LABEL_NIGHTLY"
_require_safe_label OMNIAGENTOS_REFLECTION_WATCHDOG_LABEL "$LABEL_WATCHDOG"

VAR_ROOT=${OMNIAGENTOS_VAR_DIR:-$ROOT_DIR/var}
TARGET_DIR=${OMNIAGENTOS_LAUNCHD_TARGET_DIR:-$VAR_ROOT/launchd/rendered}
mkdir -p "$TARGET_DIR"

TARGET_NIGHTLY=${TARGET_DIR}/${LABEL_NIGHTLY}.plist
TARGET_WATCHDOG=${TARGET_DIR}/${LABEL_WATCHDOG}.plist

JOB_SCRIPT_NIGHTLY="$SCRIPT_DIR/reflect-nightly.sh"
JOB_SCRIPT_WATCHDOG="$SCRIPT_DIR/reflect-watchdog.sh"

# Fail fast if interpreter is missing
if [ ! -x "$ROOT_DIR/.venv/bin/python" ] && ! command -v python3.12 >/dev/null 2>&1; then
  echo "error: no 3.12+ interpreter found (.venv/bin/python or python3.12); run 'uv sync' first" >&2
  exit 1
fi

# Fail fast if job scripts are missing (mode 0755 is preferred; /bin/sh invokes them either way)
for job in "$JOB_SCRIPT_NIGHTLY" "$JOB_SCRIPT_WATCHDOG"; do
  if [ ! -f "$job" ]; then
    echo "error: reflection job script missing: $job" >&2
    exit 1
  fi
  # Restore executable bit if lost — the +x is still useful for manual runs.
  chmod 0755 "$job" || true
  # Ensure a shebang is present
  first=$(head -n 1 "$job" || true)
  case "$first" in
    '#!'*) ;;
    *)
      echo "error: reflection job script missing shebang: $job" >&2
      exit 1
      ;;
  esac
done

# Render helper: ProgramArguments = ["/bin/sh", "<absolute job script>"]
# Never use `exec "script"` — that path is the confirmed exit-126 root cause.
_render_one() {
  template="$1"
  target="$2"
  label="$3"
  hour="$4"
  minute="$5"
  job_script="$6"
  python3 - "$template" "$target" "$label" "$ROOT_DIR" "$hour" "$minute" "$job_script" <<'PY'
import sys
from pathlib import Path

sys.path.insert(0, sys.argv[4])
from scripts.reflection.launchd import render_template
from scripts.lib.plist_write import write_plist_atomic

template, target, label, root, hour, minute, job_script = sys.argv[1:]
# Absolute interpreter + absolute script: no +x required on the script itself.
content = render_template(
    Path(template).read_text(),
    label=label,
    program_args=["/bin/sh", str(Path(job_script).resolve())],
    working_dir=root,
    hour=int(hour),
    minute=int(minute),
)
write_plist_atomic(target, content)
print(f"Wrote {target}")
PY
}

_render_one \
  "$SCRIPT_DIR/com.omniagentos.reflection-nightly.plist.template" \
  "$TARGET_NIGHTLY" \
  "$LABEL_NIGHTLY" \
  "$HOUR_NIGHTLY" \
  "$MINUTE_NIGHTLY" \
  "$JOB_SCRIPT_NIGHTLY"

_render_one \
  "$SCRIPT_DIR/com.omniagentos.reflection-watchdog.plist.template" \
  "$TARGET_WATCHDOG" \
  "$LABEL_WATCHDOG" \
  "$HOUR_WATCHDOG" \
  "$MINUTE_WATCHDOG" \
  "$JOB_SCRIPT_WATCHDOG"

# Lint the plists if plutil is available
if command -v plutil >/dev/null 2>&1; then
  plutil -lint "$TARGET_NIGHTLY"
  plutil -lint "$TARGET_WATCHDOG"
fi

MODE=${OMNIAGENTOS_REFLECTION_REARM_MODE:-off}
echo "OMNIAGENTOS_REFLECTION_REARM_MODE=${MODE}"
echo "NOT loaded. Review the plists, then load manually ONLY when mode is shadow|enforce:"
echo "  # after one-shadow-week evidence is collected (see docs/runbooks/reflection-jobs-exit126.md)"
echo "  launchctl bootstrap gui/\$(id -u) $TARGET_NIGHTLY   # or: launchctl load $TARGET_NIGHTLY"
echo "  launchctl bootstrap gui/\$(id -u) $TARGET_WATCHDOG"
if [ "$MODE" = "off" ]; then
  echo "mode=off: leave jobs unloaded; rendering is safe to re-run anytime."
fi
