#!/bin/bash
# Feature-health lane runner. Docs: docs/testing/FEATURE-HEALTH.md.
#
#   run.sh tier1|tier2|tier3|all [--feature X] [--live-probes] [--runner launchd|manual]
#
# Non-blocking background lane: never gates merges. In --runner launchd mode the
# script ALWAYS exits 0 (results land in the ledger, not the exit code); in manual
# mode pytest failure exit codes propagate (exit 5 "no tests collected" is treated
# as non-failure — fh.py records did-not-run in the ledger instead).
#
# Contention guard: refuses when the 1-min loadavg >= FH_MAX_LOAD (default 6.0) —
# exit 2 in manual mode, exit 0 in launchd mode like every other launchd outcome.
# All pytest runs execute under `nice -n 10`.
#
# xdist worker widths are env-overridable, defaults unchanged: FH_JOBS_TIER1
# (default 8, tier1 -n) and FH_JOBS_TIER3 (default 4, tier3 -n).
#
# Every bail-out BEFORE pytest (missing interpreter, contention guard) still writes
# an `aborted` ledger record. The lane's oracle is ledger freshness, not the exit
# code (`fh.py freshness --max-age-s N --runner launchd`), and freshness can only
# be trusted if a run that did nothing is distinguishable from a run that never
# happened. Silence must never read as a pass.
set -u -o pipefail

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
ROOT=$(CDPATH= cd -- "$SCRIPT_DIR/../.." && pwd)
PY="$ROOT/.venv/bin/python"
PYTEST="$ROOT/.venv/bin/pytest"
FH="$SCRIPT_DIR/fh.py"
# Same expression fh.py's var_dir() uses, so the ledger the runner writes and the
# ledger the CLI reads can never diverge under test isolation.
FH_DIR="${FH_VAR_DIR:-$ROOT/var/feature-health}"
REPORTS="$FH_DIR/reports"
# Composed marker expression — NEVER bare (pytest -m is store-not-append; a bare
# expression would readmit live/perf/smoke tests). feature_health is auto-applied
# by the lane conftest, so this deliberately drops the default addopts exclusion
# for the lane's own paths while keeping every other cost/mutation hazard out.
MEXPR='not (smoke or live_cli or perf or live_ollama or live or counterfeit_gate)'
# Tier2 selection: `feature_health and live` would DESELECT matrix tier2 paths that live
# outside tests/feature_health/ (the lane conftest only force-marks its own dir), silently
# recording them as 0P/ok — e.g. tests/connectors/test_jira_live.py. Select on `live`
# instead, still composed, so out-of-lane live tests are collected and skip LOUDLY when
# their prereqs are unset. Safe because tier2/tier3-live invocations pass explicit paths.
MEXPR_LIVE='live and not (smoke or live_cli or perf or live_ollama or counterfeit_gate)'
IGNORES=(--ignore=tests/doctrine --ignore=tests/counterfeits --ignore=tests/simharness --ignore=tests/longhaul)

MODE=${1:-}
case "$MODE" in
  tier1|tier2|tier3|all) shift ;;
  *) echo "usage: run.sh tier1|tier2|tier3|all [--feature X] [--live-probes] [--runner launchd|manual]" >&2; exit 64 ;;
esac

FEATURE=""
LIVE_PROBES=0
RUNNER="manual"
while [ $# -gt 0 ]; do
  case "$1" in
    --feature) FEATURE=${2:?--feature needs a value}; shift 2 ;;
    --live-probes) LIVE_PROBES=1; shift ;;
    --runner) RUNNER=${2:?--runner needs launchd|manual}; shift 2 ;;
    *) echo "error: unknown flag $1" >&2; exit 64 ;;
  esac
done
case "$RUNNER" in launchd|manual) ;; *) echo "error: --runner must be launchd|manual" >&2; exit 64 ;; esac

# `--runner launchd` is a CLAIM that launchd ran this. The rendered plists back the
# claim with FH_LAUNCHD=1 in EnvironmentVariables; nothing else does. Without this
# gate, one hand-typed invocation mints a "scheduled" ledger record and
# `fh.py freshness --runner launchd` reads green while nothing is bootstrapped —
# the same favourable absence the ledger records exist to prevent.
if [ "$RUNNER" = "launchd" ] && [ "${FH_LAUNCHD:-}" != "1" ]; then
  echo "error: --runner launchd requires FH_LAUNCHD=1 (set by the rendered plists)." >&2
  echo "       Refusing to stamp a SCHEDULED record for a run launchd did not make." >&2
  exit 64
fi

# ---- abort recording --------------------------------------------------------
# Both guards below bail out BEFORE any pytest report exists, and a launchd run
# that writes nothing is indistinguishable from a scheduler that never fired.
# Every abort therefore leaves a dated, failure-marked ledger line first.

abort_tiers() { # tiers the aborted invocation would have covered
  case "$MODE" in
    all) printf 'tier1\ntier2\ntier3\n' ;;
    *)   printf '%s\n' "$MODE" ;;
  esac
}

_abort_fallback() { # last resort: no usable interpreter, so hand-write the line
  local reason=$1 detail=$2 ts shard tier lock held=0 waited=0
  ts=$(date -u +%Y-%m-%dT%H:%M:%S+00:00)
  shard="$FH_DIR/ledger-$(date -u +%Y%m).jsonl"
  mkdir -p "$FH_DIR" 2>/dev/null || return 0
  # fh.py appends under flock; this path cannot (there is no interpreter to call).
  # An atomic mkdir stands in, so the two chained plist invocations cannot interleave
  # bytes and leave a line that _iter_ledger_records silently drops as bad JSON.
  lock="$FH_DIR/.abort.lock"
  while [ "$waited" -lt 50 ]; do
    if mkdir "$lock" 2>/dev/null; then
      held=1
      # Release even on SIGKILL-adjacent exits. A lock dir left behind by a dead
      # holder would make every later abort wait the full timeout and then append
      # UNLOCKED, quietly restoring the interleaving this lock exists to prevent.
      trap 'rmdir "'"$lock"'" 2>/dev/null; trap - EXIT INT TERM HUP' EXIT INT TERM HUP
      break
    fi
    # Steal a lock whose owner is gone: a dir older than 60s cannot belong to a
    # live abort (an abort is a handful of printfs).
    if [ -d "$lock" ] && [ -z "$(find "$lock" -maxdepth 0 -mmin -1 2>/dev/null)" ]; then
      rmdir "$lock" 2>/dev/null
    fi
    sleep 0.1
    waited=$((waited + 1))
  done
  while IFS= read -r tier; do
    [ -n "$tier" ] || continue
    printf '{"aborted":true,"abort_detail":"%s","abort_reason":"%s","did_not_run":true,"duration_s":0.0,"env":"isolated","errors":0,"expected_failures":0,"failed":0,"failures":[],"feature":"__lane__","passed":0,"report_path":null,"runner":"%s","schema":"feature-health.v1","skipped":0,"status":"error","tier":"%s","ts":"%s"}\n' \
      "$detail" "$reason" "$RUNNER" "$tier" "$ts" >> "$shard"
  done < <(abort_tiers)
  if [ "$held" = "1" ]; then
    rmdir "$lock" 2>/dev/null
    trap - EXIT INT TERM HUP
  fi
  return 0
}

record_abort() { # record_abort <reason> [detail]
  local reason=$1 detail=${2:-}
  # The reason is always a literal from this file; sanitize only the detail,
  # which carries measured values, so it can never break the JSON line.
  # Keep printable ASCII only: quotes/backslashes/newlines would break the
  # hand-written JSON line below, and control chars make it invalid JSON too.
  detail=$(printf '%s' "$detail" | tr -d '"\\' | tr -cd '[:print:]')
  local -a targs=()
  local tier
  while IFS= read -r tier; do
    [ -n "$tier" ] && targs+=(--tier "$tier")
  done < <(abort_tiers)
  if [ -x "$PY" ] && [ -f "$FH" ]; then
    "$PY" "$FH" abort --reason "$reason" --detail "$detail" --runner "$RUNNER" \
      "${targs[@]}" >&2 || _abort_fallback "$reason" "$detail"
  else
    _abort_fallback "$reason" "$detail"
  fi
}

exit_after_abort() { # honour the launchd contract: results live in the ledger
  local manual_rc=$1
  if [ "$RUNNER" = "launchd" ]; then
    exit 0
  fi
  exit "$manual_rc"
}

if [ ! -x "$PY" ]; then
  echo "error: $PY missing — run 'uv sync' first" >&2
  record_abort "interpreter-missing" "$PY"
  exit_after_abort 1
fi

# ---- contention guard -------------------------------------------------------
FH_MAX_LOAD=${FH_MAX_LOAD:-6.0}
LOAD=$("$PY" -c 'import os; print(f"{os.getloadavg()[0]:.2f}")')
if [ "$(awk -v l="$LOAD" -v m="$FH_MAX_LOAD" 'BEGIN{print (l >= m) ? 1 : 0}')" = "1" ]; then
  echo "feature-health: 1-min load $LOAD >= FH_MAX_LOAD $FH_MAX_LOAD — refusing to run" >&2
  record_abort "load-guard" "load=$LOAD max=$FH_MAX_LOAD"
  exit_after_abort 2
fi

mkdir -p "$REPORTS"
cd "$ROOT"
STAMP=$(date -u +%Y%m%dT%H%M%SZ)
WORST_RC=0

# One provenance snapshot taken BEFORE the first test process runs, passed to
# every tier's `fh.py append --start-snapshot`. `append` takes a second
# snapshot itself at record time; the pair is what lets a record prove the
# tree it claims to measure did not move UNDER the run. A snapshot write
# failure must not abort the run — it degrades every record from it to
# ineligible-for-filing (fh.py's explicit, named absence), never silently
# unbound.
SNAPSHOT="$FH_DIR/.snapshot-$STAMP.json"
if ! "$PY" "$FH" snapshot --out "$SNAPSHOT" >&2; then
  echo "feature-health: could not write a provenance snapshot — this run's records will be marked ineligible for finding-filing" >&2
  SNAPSHOT=""
fi

note_rc() { # normalize: 5 == "no tests collected" is lane data, not a failure
  local rc=$1
  [ "$rc" -eq 5 ] && rc=0
  [ "$rc" -gt "$WORST_RC" ] && WORST_RC=$rc
  return 0
}

feature_args() { # fh.py append/paths --feature passthrough
  if [ -n "$FEATURE" ]; then printf -- '--feature\n%s\n' "$FEATURE"; fi
}

snapshot_args() { # fh.py append --start-snapshot passthrough (empty when unavailable)
  if [ -n "$SNAPSHOT" ] && [ -f "$SNAPSHOT" ]; then printf -- '--start-snapshot\n%s\n' "$SNAPSHOT"; fi
}

collect_paths() { # existing matrix paths for a tier (missing ones are ledger data)
  "$PY" "$FH" paths --tier "$1" $(feature_args)
}

run_tier1() {
  local report="$REPORTS/$STAMP-tier1.xml" rc=0
  local -a paths=()
  while IFS= read -r p; do [ -n "$p" ] && paths+=("$p"); done < <(collect_paths tier1)
  if [ ${#paths[@]} -gt 0 ]; then
    nice -n 10 "$PYTEST" -p no:cacheprovider -n "${FH_JOBS_TIER1:-8}" --dist worksteal \
      -m "$MEXPR" "${IGNORES[@]}" --junitxml "$report" "${paths[@]}"
    rc=$?
  else
    echo "feature-health tier1: no matrix paths exist on disk — recording as missing" >&2
  fi
  note_rc $rc
  "$PY" "$FH" append --tier tier1 --report "$report" --runner "$RUNNER" $(feature_args) $(snapshot_args) || note_rc 1
}

run_tier2() {
  local report="$REPORTS/$STAMP-tier2.xml" budget="$REPORTS/$STAMP-budget.json" rc=0
  local -a paths=() extra=()
  if [ -n "$FEATURE" ]; then
    while IFS= read -r p; do [ -n "$p" ] && paths+=("$p"); done < <(collect_paths tier2)
  else
    # Whole lane dir, plus any matrix tier2 paths living outside it (e.g. jira live).
    paths=(tests/feature_health/tier2)
    while IFS= read -r p; do
      case "$p" in tests/feature_health/tier2*) ;; *) [ -n "$p" ] && extra+=("$p") ;; esac
    done < <(collect_paths tier2)
    if [ ${#extra[@]} -gt 0 ]; then paths+=("${extra[@]}"); fi
  fi
  if [ ${#paths[@]} -gt 0 ]; then
    FH_BUDGET_SUMMARY_PATH="$budget" nice -n 10 "$PYTEST" -p no:cacheprovider \
      -m "$MEXPR_LIVE" --junitxml "$report" "${paths[@]}"
    rc=$?
  else
    echo "feature-health tier2: no paths exist on disk — recording as missing" >&2
  fi
  note_rc $rc
  if [ -f "$budget" ]; then
    "$PY" "$FH" append --tier tier2 --report "$report" --env live --runner "$RUNNER" \
      --budget-summary "$budget" $(feature_args) $(snapshot_args) || note_rc 1
  else
    "$PY" "$FH" append --tier tier2 --report "$report" --env live --runner "$RUNNER" \
      $(feature_args) $(snapshot_args) || note_rc 1
  fi
}

run_tier3() {
  local report="$REPORTS/$STAMP-tier3.xml" rc=0
  local -a paths=()
  while IFS= read -r p; do [ -n "$p" ] && paths+=("$p"); done < <(collect_paths tier3)
  if [ ${#paths[@]} -gt 0 ]; then
    nice -n 10 "$PYTEST" -p no:cacheprovider -n "${FH_JOBS_TIER3:-4}" --dist worksteal \
      -m "$MEXPR" "${IGNORES[@]}" --junitxml "$report" "${paths[@]}"
    rc=$?
  else
    echo "feature-health tier3: no matrix paths exist on disk — recording as missing" >&2
  fi
  note_rc $rc
  "$PY" "$FH" append --tier tier3 --report "$report" --runner "$RUNNER" $(feature_args) $(snapshot_args) || note_rc 1

  # api_ui-only extras; skip when the run is scoped to some other feature.
  if [ -n "$FEATURE" ] && [ "$FEATURE" != "api_ui" ]; then
    return 0
  fi

  if [ "$LIVE_PROBES" = "1" ]; then
    local live_report="$REPORTS/$STAMP-tier3-live.xml" live_rc=0
    # Every opt-in live tier3 module, not just the probes: a module marked
    # `live` is DESELECTED by the ordinary tier3 expression above, so a module
    # this block does not name has no execution path in any runner mode and its
    # matrix entry buys nothing. Both run in ONE pytest invocation so they share
    # one junit report and one ledger append.
    local -a live_modules=(
      "tests/feature_health/tier3/test_live_probes.py"
      "tests/feature_health/tier3/test_production_surface.py"
    )
    local -a probes=() absent=() incomplete=()
    local module
    for module in "${live_modules[@]}"; do
      if [ -f "$module" ]; then probes+=("$module"); else absent+=("$module"); fi
    done
    if [ ${#probes[@]} -gt 0 ]; then
      FH_LIVE_PROBES=1 nice -n 10 "$PYTEST" -p no:cacheprovider \
        -m "$MEXPR_LIVE" --junitxml "$live_report" "${probes[@]}"
      live_rc=$?
    fi
    note_rc $live_rc
    # A live module that is not on disk did not pass — it did not RUN. The
    # sibling module's healthy junit would otherwise append as a clean `ok`
    # record and the absence would exist nowhere an oracle looks, so the record
    # is stamped incomplete (status=error) and the manual exit code goes red.
    if [ ${#absent[@]} -gt 0 ]; then
      echo "feature-health tier3: live module(s) missing: ${absent[*]} — recording the live record as an error" >&2
      incomplete=(--incomplete "missing live module(s): ${absent[*]}")
      note_rc 1
    fi
    "$PY" "$FH" append --tier tier3 --report "$live_report" --env live \
      --runner "$RUNNER" --feature api_ui $(snapshot_args) ${incomplete[@]+"${incomplete[@]}"} || note_rc 1
  fi

  if curl -s --max-time 5 http://127.0.0.1:8485/api/health >/dev/null 2>&1 \
     && [ -x "$ROOT/dashboard/node_modules/.bin/playwright" ]; then
    local pw_json="$FH_DIR/reports/$STAMP-playwright.json"
    (cd "$ROOT/dashboard" && nice -n 10 npx playwright test --reporter=json > "$pw_json" || true)
    "$PY" "$FH" append --tier tier3 --playwright-json "$pw_json" --env live \
      --runner "$RUNNER" $(snapshot_args) || note_rc 1
  fi
}

case "$MODE" in
  tier1) run_tier1 ;;
  tier2) run_tier2 ;;
  tier3) run_tier3 ;;
  all)   run_tier1; run_tier2; run_tier3 ;;
esac

"$PY" "$FH" render-latest || note_rc 1

[ -n "$SNAPSHOT" ] && rm -f "$SNAPSHOT" 2>/dev/null

if [ "$RUNNER" = "launchd" ]; then
  # Non-blocking lane: results live in the ledger, never in the exit code.
  exit 0
fi
exit "$WORST_RC"
