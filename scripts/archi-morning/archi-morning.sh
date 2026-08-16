#!/bin/sh
# MORNING repo-map refresh (com.omniagentos.archi-morning, daily 05:30).
#
# Keeps the architecture map fresh before the workday, after the 23:00
# curator night:
#   1. `python -m omniagentos.archdocs.generate` — regenerates ARCHI.md's four
#      inventory blocks (migrations / launchd / packages / routes); narrative
#      and `## Notes (human)` are preserved byte-for-byte by the generator.
#   2. `stamp_archi_and_json` — atomically rewrites the `<!-- archdocs:stamp ... -->`
#      first line of ARCHI.md AND the matching "stamp" object in ARCHI.json
#      (git HEAD / max migration / route count / generated_at) so
#      `archdocs.staleness.is_stale` stops flagging drift. Both stamps must update
#      in the same commit per the _is_owned_refresh_successor gate.
#   3. `python -m omniagentos.archdocs.diagram` — rewrites
#      docs/architecture/system-map.{mmd,md} verbatim from the hand-curated
#      node/edge tables in omniagentos/archdocs/diagram.py.
#   4. Diff-guarded commit of ONLY the owned doc set (ARCHI.md + ARCHI.json +
#      docs/architecture/system-map.*). Mirrors archdocs/update.py's guard:
#      if anything is already staged when we start, we skip the commit rather
#      than sweep unrelated work into it. At most one freshness commit per day.
#
# Read-only scans + owned-doc writes + a guarded local commit (never pushes) —
# safe to auto-load per the split-decision pattern in
# docs/architecture/scheduling.md (it never creates tasks or spends budget).
#
# All output (including the python steps') is appended to
# var/log/archi-morning.log; the plist points StandardOut/ErrorPath at the
# same file so launchd-level failures land there too.
set -eu

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
ROOT_DIR=$(CDPATH= cd -- "$SCRIPT_DIR/../.." && pwd)
LOG_FILE="$ROOT_DIR/var/log/archi-morning.log"
mkdir -p "$ROOT_DIR/var/log"
exec >>"$LOG_FILE" 2>&1

log() {
  printf '%s archi-morning: %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*"
}

CURRENT_BRANCH=$(git -C "$ROOT_DIR" symbolic-ref --quiet --short HEAD 2>/dev/null || true)
if [ "$CURRENT_BRANCH" != "main" ]; then
  log "REFUSED: HEAD is ${CURRENT_BRANCH:-detached}, not main; architecture refresh is main-only"
  exit 0
fi

# launchd's system Python is 3.9 on supported macOS releases; the repo needs
# 3.12+ (same pinning rule as every other installer/job in scripts/).
if [ -x "$ROOT_DIR/.venv/bin/python" ]; then
  PYBIN="$ROOT_DIR/.venv/bin/python"
elif command -v python3.12 >/dev/null 2>&1; then
  PYBIN=$(command -v python3.12)
else
  log "error: no 3.12+ interpreter (.venv/bin/python or python3.12); run 'uv sync' first"
  exit 1
fi

cd "$ROOT_DIR"
log "start (root=$ROOT_DIR python=$PYBIN)"

# 1. Inventory regeneration (migrations/launchd/packages/routes blocks).
"$PYBIN" -m omniagentos.archdocs.generate --repo-root "$ROOT_DIR"
log "regenerated ARCHI.md inventory blocks"

# 2. Freshness stamp (ARCHI.md first line AND ARCHI.json stamp object, atomically).
"$PYBIN" - "$ROOT_DIR" <<'PY'
import sys
from omniagentos.archdocs.staleness import stamp_archi_and_json

stamp = stamp_archi_and_json(sys.argv[1])
print(f"stamped ARCHI.md and ARCHI.json: {stamp}")
PY
log "restamped ARCHI.md and ARCHI.json"

# 3. System-map diagram (mmd + md), regenerated verbatim.
"$PYBIN" -m omniagentos.archdocs.diagram --repo-root "$ROOT_DIR"
log "regenerated docs/architecture/system-map.mmd + .md"

# 4. Diff-guarded commit of the owned doc set only.
OWNED="ARCHI.md ARCHI.json docs/architecture/system-map.mmd docs/architecture/system-map.md"
if ! git -C "$ROOT_DIR" diff --cached --quiet; then
  log "skip commit: index already has staged changes (refusing to sweep them in)"
elif git -C "$ROOT_DIR" add -- $OWNED && git -C "$ROOT_DIR" diff --cached --quiet; then
  log "no changes; nothing to commit"
else
  git -C "$ROOT_DIR" \
    -c user.name="archi-morning" -c user.email="00000000+omniagentos-bot[bot]@users.noreply.github.com" \
    commit -m "archi-morning: refresh map + diagram"
  log "committed: archi-morning: refresh map + diagram ($(git -C "$ROOT_DIR" rev-parse --short HEAD))"
fi

# The committed refresh must be the sole owned-oracle successor of its embedded
# source stamp. Refuse to report success when the commit was skipped, an owned
# file remains dirty, or the fail-closed route/migration checks detect drift.
if ! git -C "$ROOT_DIR" diff --quiet HEAD -- $OWNED; then
  log "error: owned architecture files remain uncommitted after refresh"
  exit 1
fi
"$PYBIN" - "$ROOT_DIR" <<'PY'
import sys
from omniagentos.archdocs.staleness import is_stale

if is_stale(sys.argv[1]):
    raise SystemExit("committed architecture refresh still reports stale")
PY
log "verified committed architecture refresh is fresh"

log "done"
