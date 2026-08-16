#!/bin/sh
# Install the Steward briefing and its supporting collection jobs.
set -eu

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
ROOT_DIR=$(CDPATH= cd -- "$SCRIPT_DIR/../.." && pwd)
# Override with a fake dir in tests/CI; never auto-load into launchd.
VAR_ROOT=${OMNIAGENTOS_VAR_DIR:-$ROOT_DIR/var}
TARGET_DIR=${OMNIAGENTOS_LAUNCHD_TARGET_DIR:-$VAR_ROOT/launchd/rendered}
mkdir -p "$TARGET_DIR"

# launchd's system Python is 3.9 on supported macOS releases. OmniAgentOS
# requires 3.12+, so every generated job receives one pinned interpreter.
if [ -x "$ROOT_DIR/.venv/bin/python" ]; then
  PYBIN="$ROOT_DIR/.venv/bin/python"
elif command -v python3.12 >/dev/null 2>&1; then
  PYBIN=$(command -v python3.12)
else
  echo "error: no 3.12+ interpreter found (.venv/bin/python or python3.12); run 'uv sync' first" >&2
  exit 1
fi

# H4: launchd jobs run with a sanitized environment — no ~/.config/omni/connections.env,
# no credentials, nothing. Every ProgramArguments below is wrapped in `/bin/sh -lc` so the
# job sources the operator's connections.env before exec'ing the pinned interpreter. The
# knowledge-subsystem PG DSNs that scripts/launch-omniagentos.sh exports for the
# interactive dev launch are NOT in connections.env, so the briefing and comms
# (extract-batch) jobs — the two that touch the knowledge bridge/recall path — also get
# them exported directly in the wrapper. These are mirrored constants: if the canonical
# values in scripts/launch-omniagentos.sh ever change, update them here too.
"$PYBIN" - "$SCRIPT_DIR" "$ROOT_DIR" "$TARGET_DIR" "$PYBIN" <<'PY'
import shlex
import sys
from pathlib import Path

script_dir = Path(sys.argv[1])
root = sys.argv[2]
target_dir = Path(sys.argv[3])
pybin = sys.argv[4]
sys.path.insert(0, str(script_dir))
sys.path.insert(0, root)

from launchd import render_template
from omniagentos.steward.config import load_steward_config
from scripts.lib.plist_write import write_plist_atomic

# Mirrors scripts/launch-omniagentos.sh's OMNIAGENTOS_KNOWLEDGE* exports.
_KNOWLEDGE_EXPORTS = (
    "export OMNIAGENTOS_KNOWLEDGE=1; "
    'export OMNIAGENTOS_KNOWLEDGE_PG_DSN="postgresql://knowledge_agent@localhost/omniagentos_knowledge"; '
    'export OMNIAGENTOS_KNOWLEDGE_ADMIN_DSN="postgresql://knowledge_admin@localhost/omniagentos_knowledge"; '
)
# connections.env is plain KEY=value (no `export`), so `set -a` is REQUIRED for the
# sourced credentials to be inherited by the exec'd interpreter — a bare `. file`
# leaves them shell-local and the job runs credential-blind (re-review RR-DEP-001).
_SOURCE_ENV = (
    'set -a; . \"$HOME/.config/omni/connections.env\" 2>/dev/null; set +a; '
    f'. \"{root}/scripts/launch-env.sh\" 2>/dev/null; '
)


def wrapped_args(module_args: list[str], *, needs_knowledge: bool = False) -> list[str]:
    """Wrap a job's argv so it sources the operator env before exec'ing python."""
    exec_cmd = " ".join(shlex.quote(part) for part in [pybin, *module_args])
    script = _SOURCE_ENV
    if needs_knowledge:
        script += _KNOWLEDGE_EXPORTS
    script += f"exec {exec_cmd}"
    return ["/bin/sh", "-lc", script]


briefing = load_steward_config().briefing
jobs = (
    (
        "briefing",
        wrapped_args(["-m", "omniagentos.briefing.run"], needs_knowledge=True),
        briefing.hour,
        briefing.minute,
    ),
    (
        "metrics",
        wrapped_args(["-m", "omniagentos.goals.collect", "--once"]),
        0,
        0,
    ),
    (
        "alerts",
        wrapped_args(["-m", "omniagentos.steward.alerts.monitor", "--once"]),
        0,
        0,
    ),
    (
        "comms",
        wrapped_args(["-m", "omniagentos.comms.extract_batch"], needs_knowledge=True),
        2,
        30,
    ),
)
for name, args, hour, minute in jobs:
    label = f"com.omniagentos.steward.{name}"
    template = script_dir / f"com.omniagentos.steward.{name}.plist.template"
    rendered = render_template(
        template.read_text(encoding="utf-8"),
        label=label,
        program_args=args,
        working_dir=root,
        hour=hour,
        minute=minute,
    )
    write_plist_atomic(target_dir / f"{label}.plist", rendered)
PY

for name in briefing metrics alerts comms; do
  label="com.omniagentos.steward.$name"
  target="$TARGET_DIR/$label.plist"
  echo "Wrote $target"
  echo "NOT loaded. Review the plist, then load manually if intended:"
  echo "  launchctl load $target"
done
