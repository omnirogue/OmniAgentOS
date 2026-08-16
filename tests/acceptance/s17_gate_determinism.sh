#!/usr/bin/env bash
# S17 — the merge gate's verdict is a pure function of the candidate, not of
# whatever ~30 worktrees happened to leave lying around.
#
# WHAT THIS PROVES, AND WHY EACH STEP EXISTS
# ------------------------------------------
# S0/S1  establish that the LIVE shared checkout and the PINNED workspace
#        genuinely disagree right now. Without that the rest is vacuous.
# S2     the debug probe reads the pinned workspace.
# S3     the gate refuses the shared root in under 30 seconds with a named
#        reason, instead of "bound to a different merge-base SHA" twelve
#        minutes in.
# S4     THE PRODUCTION PATH — not the debug flag: the real gate, real flags,
#        and the receipt it actually emits must carry the pinned baseline, not
#        the ambient one. TWO HALVES, because one is not enough:
#        S4  (dynamic) the emitted receipt's ruff_base equals the pinned count
#            under ambient dirt. This covers the HOISTED ruff-base step only —
#            every production run here refuses at a hoisted check well above the
#            ladder, so this half never observes `ruff-vs-current-main`.
#        S4b (static) the ladder's own BASE is bound to that same pinned
#            $RUFF_BASE. Mutation-verified: breaking this binding alone passes
#            S0-S4 and is caught only by S4b.
# S5     the hoist: a sub-second refusal must not be reachable only after ~12
#        minutes of suites.
# S6     refusals leave signed evidence in the DURABLE store. Before this, all
#        77 timed receipts on disk carried exit_code 0, so the entire refusal
#        side of the gate was unmeasurable.
# S7     the load the gate ran under is stamped, measured, and inside the
#        signature — a red is otherwise unattributable between code and 24
#        competing agents.
#
# Exits 0 only on a full pass. Exit 2 means INCONCLUSIVE (the environment could
# not produce the disagreement the test needs), which is not a pass.
#
#   cd /Users/youruser/OmniAgentOS && bash tests/acceptance/s17_gate_determinism.sh
#
# BSD/macOS userland only: no `timeout`, no GNU flags, no `pgrep -c` (which does
# not exist here).
set -uo pipefail

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
REPO=$(CDPATH= cd -- "$SCRIPT_DIR/../.." && pwd)
GATE_WS="${OMNIAGENTOS_GATE_WORKSPACE:-${REPO}-gate}"
PY="$REPO/.venv/bin/python"
GATE="$REPO/scripts/merge-gate.sh"
PROBE_PY="$REPO/omniagentos/_ambient_probe.py"
DIRT="$GATE_WS/_dirty_probe.tmp"
TMPD="${TMPDIR:-/tmp}"
LOAD_PROBE="mgp$$"                      # short: BSD process names are truncated
LOAD_BIN="$TMPD/$LOAD_PROBE"
RECEIPTS="$REPO/var/gate-evidence/records/merge-gate"
PLANTED_JSON="/tmp/mg-clean.json /tmp/mg-dirty.json /tmp/mg-hoist.json /tmp/mg-load0.json /tmp/mg-load3.json"

S17_T0=$(date +%s)
FAILED=0
step()  { printf '\n=== %s\n' "$*"; }
ok()    { printf '  ok    %s\n' "$*"; }
bad()   { printf '  FAIL  %s\n' "$*"; FAILED=1; }
fatal() { printf '\nS17 PREFLIGHT FAILED: %s\n' "$*" >&2; exit 2; }
incon() { printf '\nS17 INCONCLUSIVE: %s\n' "$*" >&2; exit 2; }

cleanup() {
  rm -f "$PROBE_PY" "$DIRT"
  # shellcheck disable=SC2086 -- deliberate word list
  rm -f $PLANTED_JSON
  pkill -x "$LOAD_PROBE" 2>/dev/null
  rm -f "$LOAD_BIN"
  git -C "$REPO" worktree prune 2>/dev/null
  # Durable run receipts are NOT deleted: S6 asserts a refusal survives in the
  # store, and evidence you erase after reading is not evidence.
  return 0
}
trap cleanup EXIT INT TERM

ruff_count() {  # root -> issue count
  ( cd "$1" 2>/dev/null && "$PY" -m ruff check --output-format concise . 2>/dev/null \
      | awk '/:/{n++} END{print n+0}' )
}

# No `timeout(1)` on this platform. Poll instead, and kill rather than hang.
run_with_budget() {  # budget-seconds, out-file, cmd...
  local budget="$1" out="$2"
  shift 2
  "$@" >"$out" 2>&1 &
  local pid=$! waited=0
  while kill -0 "$pid" 2>/dev/null; do
    if [ "$waited" -ge "$budget" ]; then
      kill -TERM "$pid" 2>/dev/null
      sleep 2
      kill -KILL "$pid" 2>/dev/null
      wait "$pid" 2>/dev/null
      return 124
    fi
    sleep 1
    waited=$(( waited + 1 ))
  done
  wait "$pid"
  return $?
}

jq_py() {  # json-file, python-expression over `d`
  "$PY" - "$1" "$2" <<'PYEOF' 2>/dev/null
import json
import sys

with open(sys.argv[1], encoding="utf-8") as handle:
    d = json.load(handle)
print(eval(sys.argv[2], {"d": d, "json": json}))  # noqa: S307 - fixed local exprs
PYEOF
}

# --------------------------------------------------------------- preflight
step "preflight"
[ -x "$PY" ]   || fatal "no interpreter at $PY"
[ -x "$GATE" ] || fatal "no merge gate at $GATE"
[ -x "$REPO/scripts/gate-workspace.sh" ] || fatal "no $REPO/scripts/gate-workspace.sh"
command -v git     >/dev/null 2>&1 || fatal "git is not on PATH"
command -v sysctl  >/dev/null 2>&1 || fatal "sysctl is not on PATH"
command -v pgrep   >/dev/null 2>&1 || fatal "pgrep is not on PATH"
git -C "$REPO" rev-parse --verify HEAD >/dev/null 2>&1 || fatal "$REPO is not a git checkout"
"$PY" -m ruff --version >/dev/null 2>&1 || fatal "ruff is not installed in $PY"
[ -e "$PROBE_PY" ] && fatal "$PROBE_PY already exists; refusing to overwrite a real file"
ok "interpreter, gate, workspace script, git, sysctl, pgrep, ruff"

# ------------------------------------------------------------------- S0
step "S0 — pinned workspace exists and is the honest baseline"
if ! WS_OUT=$(bash "$REPO/scripts/gate-workspace.sh" main 2>&1); then
  fatal "scripts/gate-workspace.sh main failed: $WS_OUT"
fi
ok "gate-workspace.sh main -> $(git -C "$GATE_WS" rev-parse --short HEAD)"
HONEST=$(ruff_count "$GATE_WS")
case "$HONEST" in ''|*[!0-9]*) fatal "could not count ruff issues in $GATE_WS" ;; esac
ok "HONEST (ruff issues inside \$GATE_WS) = $HONEST"

# ------------------------------------------------------------------- S1
step "S1 — plant a REAL F401 in the LIVE shared checkout only"
printf 'import os\n' > "$PROBE_PY" || fatal "cannot write $PROBE_PY"
LIVE=$(ruff_count "$REPO")
case "$LIVE" in ''|*[!0-9]*) fatal "could not count ruff issues in $REPO" ;; esac
ok "LIVE (ruff issues in the shared checkout) = $LIVE"
if [ "$LIVE" -le "$HONEST" ]; then
  incon "LIVE ($LIVE) is not greater than HONEST ($HONEST); the two roots do not disagree, so nothing below can distinguish them"
fi
[ -f "$GATE_WS/omniagentos/_ambient_probe.py" ] \
  && bad "the probe leaked into the pinned workspace" \
  || ok "probe is absent from \$GATE_WS (planted in the live checkout only)"

# ------------------------------------------------------------------- S2
step "S2 — --print-ruff-base reads the PINNED workspace"
PRINTED=$(MERGE_GATE_PINNED=1 bash "$GATE" --print-ruff-base 2>/dev/null | tail -1)
if [ "$PRINTED" = "$HONEST" ]; then
  ok "--print-ruff-base = $PRINTED = HONEST"
else
  bad "--print-ruff-base = '$PRINTED', expected HONEST=$HONEST"
fi
if [ "$PRINTED" = "$LIVE" ]; then
  bad "--print-ruff-base returned the LIVE ambient count ($LIVE)"
else
  ok "--print-ruff-base is not the LIVE count ($LIVE)"
fi

# ------------------------------------------------------------------- S3
step "S3 — REPO forced to the shared root refuses fast, by name"
CAND=$(git -C "$REPO" rev-parse HEAD)
T0=$(date +%s)
run_with_budget 30 "$TMPD/s17-s3.out" \
  env MERGE_GATE_PINNED=1 REPO="$REPO" bash "$GATE" --candidate "$CAND"
S3_RC=$?
T1=$(date +%s)
S3_ELAPSED=$(( T1 - T0 ))
if [ "$S3_RC" -eq 124 ]; then
  bad "shared-root run did not refuse within 30s (killed)"
elif [ "$S3_RC" -eq 0 ]; then
  bad "shared-root run exited 0; it must refuse"
else
  ok "refused with exit $S3_RC in ${S3_ELAPSED}s"
fi
[ "$S3_ELAPSED" -lt 30 ] && ok "refusal took ${S3_ELAPSED}s (< 30s)" \
                         || bad "refusal took ${S3_ELAPSED}s (>= 30s)"
if grep -qE 'refusing: [a-z-]+' "$TMPD/s17-s3.out"; then
  ok "printed $(grep -oE 'refusing: [a-z-]+' "$TMPD/s17-s3.out" | head -1)"
else
  bad "no 'refusing: <reason>' line; got: $(head -2 "$TMPD/s17-s3.out" | tr '\n' ' ')"
fi
rm -f "$TMPD/s17-s3.out"

# ------------------------------------------------------------------- S4
step "S4 — THE PRODUCTION PATH: ruff_base is identical with and without ambient dirt"
# Budget: the real gate may run the full ladder. 20 minutes each, per the
# package's allowance; a hoisted refusal short-circuits long before that.
rm -f "$PROBE_PY"
run_with_budget 1200 "$TMPD/s17-clean.out" \
  env MERGE_GATE_PINNED=1 bash "$GATE" --candidate "$CAND" --emit-receipt /tmp/mg-clean.json
S4A_RC=$?
[ "$S4A_RC" -eq 124 ] && bad "clean production run exceeded its 20 minute budget"

printf 'import os\n' > "$PROBE_PY" || fatal "cannot re-plant $PROBE_PY"
DIRTY_LIVE=$(ruff_count "$REPO")
run_with_budget 1200 "$TMPD/s17-dirty.out" \
  env MERGE_GATE_PINNED=1 bash "$GATE" --candidate "$CAND" --emit-receipt /tmp/mg-dirty.json
S4B_RC=$?
[ "$S4B_RC" -eq 124 ] && bad "dirty production run exceeded its 20 minute budget"
rm -f "$TMPD/s17-clean.out" "$TMPD/s17-dirty.out"

if [ ! -f /tmp/mg-clean.json ] || [ ! -f /tmp/mg-dirty.json ]; then
  bad "the production path did not emit both receipts (clean rc=$S4A_RC dirty rc=$S4B_RC)"
else
  BASE_CLEAN=$(jq_py /tmp/mg-clean.json 'd.get("ruff_base")')
  BASE_DIRTY=$(jq_py /tmp/mg-dirty.json 'd.get("ruff_base")')
  if [ "$BASE_CLEAN" = "None" ] || [ -z "$BASE_CLEAN" ]; then
    bad "clean receipt has no ruff_base (a null on both sides would pass this test vacuously)"
  elif [ "$BASE_CLEAN" != "$BASE_DIRTY" ]; then
    bad "ruff_base differs across ambient dirt: clean=$BASE_CLEAN dirty=$BASE_DIRTY"
  elif [ "$BASE_CLEAN" != "$HONEST" ]; then
    bad "ruff_base=$BASE_CLEAN is not the pinned-workspace count HONEST=$HONEST (live tree was $DIRTY_LIVE)"
  else
    ok "ruff_base = $BASE_CLEAN on both runs, and equals HONEST (live tree read $DIRTY_LIVE)"
  fi
fi

# S4b — THE LADDER'S CONSUMER, which the dynamic half above structurally cannot
# reach.
#
# Verified by mutation, not assumed: replacing the pinned binding with
# `if false && [ -n "$RUFF_BASE" ]` — the exact "wired --print-ruff-base and
# forgot the ruff BASE line" defect — still produced a full S17 PASS. Every
# production run in S4 refuses at a HOISTED check (reachability, on this repo)
# long before `ruff-vs-current-main` executes, so the receipt's ruff_base is
# written by the hoisted `ruff-base` step at merge-gate.sh:389 and says nothing
# about the ladder consumer at merge-gate.sh:856. Reaching that consumer needs a
# candidate that clears every hoisted refusal AND carries a signed receipt AND
# survives the trial merge — a ~12-minute setup this test deliberately does not
# build. Bind the consumer statically instead: cheap, and it fails on exactly
# the mutation the dynamic half let through.
if "$PY" - "$GATE" <<'PYEOF'
import re
import sys

with open(sys.argv[1], encoding="utf-8") as handle:
    src = handle.read()
block = re.search(r'step_begin "ruff-vs-current-main"(.{0,300})', src, re.S)
if not block:
    sys.exit(1)
bind = re.search(
    r'if \[ "\$PINNED" = "1" \] && \[ -n "\$RUFF_BASE" \]; then\s*\n\s*BASE="\$RUFF_BASE"',
    block.group(1),
)
sys.exit(0 if bind else 1)
PYEOF
then
  ok "the ladder's BASE is bound to the pinned \$RUFF_BASE (static bind)"
else
  bad "merge-gate.sh ruff-vs-current-main does not bind BASE to the pinned \$RUFF_BASE; the ladder would re-read the ambient root"
fi
rm -f "$PROBE_PY"

# ------------------------------------------------------------------- S5
step "S5 — THE HOIST: a sub-second refusal does not wait for the ladder"
git -C "$GATE_WS" checkout -q -- .
touch "$DIRT" || fatal "cannot plant $DIRT"
T0=$(date +%s)
run_with_budget 60 "$TMPD/s17-hoist.out" \
  env MERGE_GATE_PINNED=1 bash "$GATE" --candidate "$CAND" --emit-receipt /tmp/mg-hoist.json
S5_RC=$?
T1=$(date +%s)
S5_ELAPSED=$(( T1 - T0 ))
rm -f "$DIRT" "$TMPD/s17-hoist.out"
[ "$S5_ELAPSED" -lt 10 ] && ok "refused in ${S5_ELAPSED}s (< 10s)" \
                         || bad "took ${S5_ELAPSED}s, expected < 10s"
if [ ! -f /tmp/mg-hoist.json ]; then
  bad "no receipt emitted for the hoisted refusal (rc=$S5_RC)"
else
  STEP_NAMES=$(jq_py /tmp/mg-hoist.json '" ".join(s.get("name","") for s in d.get("steps",[]))')
  ok "steps[] = ${STEP_NAMES:-<empty>}"
  SUITEY=$("$PY" - <<PYEOF
import json
with open("/tmp/mg-hoist.json", encoding="utf-8") as fh:
    d = json.load(fh)
names = [s.get("name", "") for s in d.get("steps", [])]
banned = [n for n in names if any(t in n for t in ("ladder", "counterfeit-gate", "suite", "pytest"))]
print(" ".join(banned))
PYEOF
)
  [ -z "$SUITEY" ] && ok "zero ladder/counterfeit-gate/suite/pytest entries" \
                   || bad "receipt contains suite steps: $SUITEY"
  ORDER=$("$PY" - <<PYEOF
import json
with open("/tmp/mg-hoist.json", encoding="utf-8") as fh:
    d = json.load(fh)
names = [s.get("name", "") for s in d.get("steps", [])]
reach = next((i for i, n in enumerate(names) if "reachability" in n), None)
suite = next(
    (i for i, n in enumerate(names)
     if any(t in n for t in ("ladder", "counterfeit-gate", "suite", "pytest"))),
    None,
)
if reach is None or suite is None:
    print("n/a")
else:
    print("ok" if reach < suite else f"bad reach={reach} suite={suite}")
PYEOF
)
  case "$ORDER" in
    "ok")  ok "reachability precedes the first suite step" ;;
    "n/a") ok "no reachability/suite pair present to order (refusal happened above both)" ;;
    *)     bad "reachability runs after a suite: $ORDER" ;;
  esac
fi

# The dirty-workspace refusal legitimately stops above everything, so it cannot
# by itself show that reachability MOVED. The production receipt can: the
# sub-second probe that used to run dead last must now appear before the
# trial-merge that gates every suite.
if [ -f /tmp/mg-clean.json ]; then
  HOIST_ORDER=$("$PY" - <<'PYEOF'
import json
with open("/tmp/mg-clean.json", encoding="utf-8") as fh:
    d = json.load(fh)
names = [s.get("name", "") for s in d.get("steps", [])]
if "reachability" not in names:
    print("missing")
elif "trial-merge" in names and names.index("reachability") > names.index("trial-merge"):
    print("late")
else:
    print("hoisted")
PYEOF
)
  case "$HOIST_ORDER" in
    hoisted) ok "production receipt runs reachability above the trial merge and the suites" ;;
    missing) bad "production receipt has no reachability step at all" ;;
    *)       bad "production receipt still runs reachability after the trial merge" ;;
  esac
fi

# ------------------------------------------------------------------- S6
step "S6 — the refusal receipt is signed, reasoned, and DURABLE"
if [ ! -f /tmp/mg-hoist.json ]; then
  bad "no hoisted receipt to inspect"
else
  R_EXIT=$(jq_py /tmp/mg-hoist.json 'd.get("exit_code")')
  R_WHY=$(jq_py  /tmp/mg-hoist.json 'len(d.get("refusal_reason") or "")')
  R_STEPS=$(jq_py /tmp/mg-hoist.json 'isinstance(d.get("steps"), list)')
  R_SIG=$(jq_py  /tmp/mg-hoist.json 'len(d.get("signature") or "")')
  case "$R_EXIT" in
    ''|None|0) bad "exit_code is '$R_EXIT'; a refusal receipt must carry a non-zero code" ;;
    *)         ok "exit_code = $R_EXIT" ;;
  esac
  [ "${R_WHY:-0}" -gt 0 ] 2>/dev/null && ok "refusal_reason is non-empty" \
                                      || bad "refusal_reason is empty"
  [ "$R_STEPS" = "True" ] && ok "steps is a list" || bad "steps is not a list"
  [ "${R_SIG:-0}" -ge 32 ] 2>/dev/null && ok "signature present (${R_SIG} chars)" \
                                       || bad "signature missing or too short"
fi
# Only receipts written since this script started count. A pre-existing refusal
# from an earlier run would let a broken implementation pass this assertion.
DURABLE_HITS=$("$PY" - "$RECEIPTS" "$S17_T0" <<'PYEOF'
import json
import sys
from pathlib import Path

since = float(sys.argv[2])
hits = 0
for path in sorted(Path(sys.argv[1]).glob("*.json")):
    try:
        if path.stat().st_mtime < since:
            continue
        with path.open(encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, ValueError):
        continue
    if isinstance(payload, dict) and payload.get("exit_code") not in (0, None):
        hits += 1
print(hits)
PYEOF
)
if [ "${DURABLE_HITS:-0}" -gt 0 ] 2>/dev/null; then
  ok "$DURABLE_HITS receipt(s) written by THIS run carry a non-zero exit_code in $RECEIPTS"
else
  bad "no receipt with a non-zero exit_code written to $RECEIPTS by this run — refusals are still unmeasurable"
fi

# ------------------------------------------------------------------- S7
step "S7 — the load stamp is measured, not decorative"
CORES=$(sysctl -n hw.perflevel0.logicalcpu 2>/dev/null)
case "$CORES" in ''|*[!0-9]*) fatal "sysctl hw.perflevel0.logicalcpu gave '$CORES'" ;; esac
if [ ! -f /tmp/mg-clean.json ]; then
  bad "no production receipt to read load fields from"
else
  AGENTS=$(jq_py /tmp/mg-clean.json 'd.get("concurrent_agents")')
  R_CORES=$(jq_py /tmp/mg-clean.json 'd.get("host_perf_cores")')
  case "$AGENTS" in
    ''|*[!0-9]*) bad "concurrent_agents is '$AGENTS'; expected an int >= 0" ;;
    *)           ok "concurrent_agents = $AGENTS (int >= 0)" ;;
  esac
  [ "$R_CORES" = "$CORES" ] && ok "host_perf_cores = $R_CORES = sysctl hw.perflevel0.logicalcpu" \
                            || bad "host_perf_cores = '$R_CORES', sysctl says '$CORES'"
fi

# Non-vacuity. A unique probe name makes this deterministic instead of racing
# whatever else on the box happens to be sleeping. A copy of /bin/sleep is
# SIGKILLed on this platform (the signature no longer validates), so symlink it.
ln -sf /bin/sleep "$LOAD_BIN" || fatal "cannot create $LOAD_BIN"
run_with_budget 60 "$TMPD/s17-load0.out" \
  env MERGE_GATE_PINNED=1 MERGE_GATE_AGENT_PROCS="$LOAD_PROBE" \
      bash "$GATE" --print-ruff-base --emit-receipt /tmp/mg-load0.json
"$LOAD_BIN" 300 >/dev/null 2>&1 &
"$LOAD_BIN" 300 >/dev/null 2>&1 &
"$LOAD_BIN" 300 >/dev/null 2>&1 &
sleep 1
run_with_budget 60 "$TMPD/s17-load3.out" \
  env MERGE_GATE_PINNED=1 MERGE_GATE_AGENT_PROCS="$LOAD_PROBE" \
      bash "$GATE" --print-ruff-base --emit-receipt /tmp/mg-load3.json
pkill -x "$LOAD_PROBE" 2>/dev/null
rm -f "$TMPD/s17-load0.out" "$TMPD/s17-load3.out"

if [ -f /tmp/mg-load0.json ] && [ -f /tmp/mg-load3.json ]; then
  L0=$(jq_py /tmp/mg-load0.json 'd.get("concurrent_agents")')
  L3=$(jq_py /tmp/mg-load3.json 'd.get("concurrent_agents")')
  if [ "$L0" = "0" ] && [ "$L3" = "3" ]; then
    ok "concurrent_agents tracked load: 0 with nothing running, 3 with three probes"
  else
    bad "concurrent_agents did not track load: baseline=$L0 (want 0), under-load=$L3 (want 3)"
  fi
  SIG0=$(jq_py /tmp/mg-load0.json 'len(d.get("signature") or "")')
  SIG3=$(jq_py /tmp/mg-load3.json 'len(d.get("signature") or "")')
  S_DIFF=$(jq_py /tmp/mg-load0.json 'd.get("signature")')
  S_DIFF3=$(jq_py /tmp/mg-load3.json 'd.get("signature")')
  if [ "${SIG0:-0}" -ge 32 ] 2>/dev/null && [ "${SIG3:-0}" -ge 32 ] 2>/dev/null \
     && [ "$S_DIFF" != "$S_DIFF3" ]; then
    ok "both load receipts are signed and the differing load produced a differing signature"
  else
    bad "load fields do not ride inside the signature (sig0=$SIG0 sig3=$SIG3)"
  fi
else
  bad "load-probe receipts were not emitted"
fi

if grep -r 'pgrep -c' "$GATE" >/dev/null 2>&1; then
  bad "scripts/merge-gate.sh contains 'pgrep -c', which does not exist on this platform"
else
  ok "no 'pgrep -c' anywhere in scripts/merge-gate.sh"
fi

# --------------------------------------------------------------- verdict
printf '\n'
if [ "$FAILED" -eq 0 ]; then
  printf 'S17 GATE DETERMINISM: PASS\n'
  exit 0
fi
printf 'S17 GATE DETERMINISM: FAIL\n'
exit 1
