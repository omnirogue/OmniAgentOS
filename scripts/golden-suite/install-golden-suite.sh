#!/bin/sh
# Render the GOLDEN-SUITE SENTINEL's nightly launchd job
# (com.omniagentos.golden-suite -- `scripts/golden-suite/golden-suite.sh`,
# which runs `run_golden.py`).
#
# Follows the launchd template idiom (ARCHI.md "How to extend" -- New launchd
# job) without touching live launchd. Unlike
# install-swarm-optimizer.sh (which bakes a `set -a; . connections.env;
# set +a; exec PYBIN -m module` one-liner directly into the plist's
# ProgramArguments), golden-suite.sh is its own resolved-interpreter +
# sourced-env runner script (the fable-curator idiom instead) -- the plist
# here just execs `/bin/sh golden-suite.sh`, so ProgramArguments never needs
# re-rendering when the interpreter resolution logic changes.
#
# Schedule: 01:00 daily by default (plan doc, A0.0 golden-suite bullet).
# Override with OMNIAGENTOS_GOLDEN_SUITE_HOUR / OMNIAGENTOS_GOLDEN_SUITE_MINUTE.
#
# This job dispatches REAL work through the live API (POST /api/intake/quick,
# POST /api/swarm) -- unlike swarm-optimizer/selfimprove-curator/
# reliability-audit (read-only analysis + local file writes), a golden-suite
# run spends real run budget every night by design (the benchmark briefs are
# tiny on purpose -- see benchmarks.yaml's own docstring). It NEVER edits
# benchmarks.yaml, prompt.md's immutable structure, or any other config from
# code -- retuning behavior is a human edit to prompt.md's policy block or
# benchmarks.yaml, never an auto-edit.
set -eu

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
ROOT_DIR=$(CDPATH= cd -- "$SCRIPT_DIR/../.." && pwd)
. "$ROOT_DIR/scripts/lib/launchd-label.sh"
LABEL=${OMNIAGENTOS_GOLDEN_SUITE_LAUNCHD_LABEL:-com.omniagentos.golden-suite}
HOUR=${OMNIAGENTOS_GOLDEN_SUITE_HOUR:-1}
MINUTE=${OMNIAGENTOS_GOLDEN_SUITE_MINUTE:-0}
require_safe_launchd_label "$LABEL" "com.omniagentos.golden-suite"
VAR_ROOT=${OMNIAGENTOS_VAR_DIR:-$ROOT_DIR/var}
TARGET_DIR=${OMNIAGENTOS_LAUNCHD_TARGET_DIR:-$VAR_ROOT/launchd/rendered}
TARGET=${TARGET_DIR}/${LABEL}.plist
mkdir -p "$TARGET_DIR"

if [ ! -x "$SCRIPT_DIR/golden-suite.sh" ]; then
  echo "error: golden-suite job script is not executable" >&2
  exit 1
fi

# golden-suite.sh is plain POSIX sh and resolves its own interpreter (same
# .venv/bin/python-first, python3.12-fallback order as
# install-swarm-optimizer.sh), so unlike a bare `-m module` invocation there
# is no interpreter to pin here. Render the plist with whatever python3 is
# on PATH (render-only, not the runtime).
python3 - "$SCRIPT_DIR/com.omniagentos.golden-suite.plist.template" "$TARGET" "$LABEL" \
    "$SCRIPT_DIR/golden-suite.sh" "$ROOT_DIR" "$HOUR" "$MINUTE" <<'PY'
import sys
from pathlib import Path

sys.path.insert(0, str(Path(sys.argv[1]).parent))
from launchd import render_template

template, target, label, job, root, hour, minute = sys.argv[1:]
sys.path.insert(0, root)
from scripts.lib.plist_write import write_plist_atomic
content = render_template(
    Path(template).read_text(),
    label=label,
    program_args=["/bin/sh", job],
    working_dir=root,
    hour=int(hour),
    minute=int(minute),
)
write_plist_atomic(target, content)
PY
echo "Wrote $TARGET"

# Lint the rendered artifact before an operator considers loading it.
# plutil is macOS-only; on Linux CI hosts skip the lint rather than fail the install.
if command -v plutil >/dev/null 2>&1; then
  plutil -lint "$TARGET"
else
  echo "plutil unavailable on this host; skipping lint of $TARGET"
fi

echo "NOT loaded. This job dispatches work; review and explicitly load if intended:"
echo "  launchctl load $TARGET"
