#!/usr/bin/env bash
# Mechanical gate that must pass before anything merges to main.
#
# Every check here exists because something got through today. This is not a
# generic CI script; it is a list of specific, expensive mistakes:
#
#  1. ladder on the MERGE COMMIT, not the lane — the two worst defects today were
#     seam defects invisible in isolation, and a lane that is green alone can still
#     break main (S-20).
#  2. counterfeit corpus — a test suite that cannot detect its own realistic
#     failure is decoration. Four fakes survived until today.
#  3. dominance corpus — a run that did strictly less work must not score higher.
#     Without this the optimizer learns to do less.
#  4. secrets — `configs/accounts.yaml` and `vault/sources/*.enc` are TRACKED and
#     not gitignored. A snapshot ran `git add -A` for months.
#  5. migrations are append-only — editing an applied migration silently diverges
#     deployed schemas.
#  6. lint against CURRENT main, never a remembered number — a stale baseline
#     produced a phantom 10-finding regression today.
#  7. no committed symlinks — a mode-120000 entry replaced a real 679M .venv with
#     a self-referential link on checkout.
#  8. no empty candidate — "Already up to date" is not a landed merge.
#  9. generated architecture oracles are refreshed on main, never merged from lanes.
# 10. tracked environments can replace the local interpreter/dependency trees.
# 11. root WORKBOOK.md is shared mutable state; lane workbooks belong under var/swarm.
#
# usage: merge-gate.sh <branch-to-merge> [signed-receipt.json]
#        merge-gate.sh --candidate <ref> [--emit-receipt <path>] [--bound-test <node-id>]...
#        merge-gate.sh --print-ruff-base
#        merge-gate.sh --candidate <ref> --preflight-only   (cheap checks only)
#        (run from the repo root, on main)
# exit 0 = safe to merge. Non-zero = do not merge; the reason is printed.
#
# ============================================================================
# MERGE_GATE_PINNED=1 — the determinism mode (DISARMED BY DEFAULT)
# ============================================================================
# Everything below the `PINNED` guards exists to make the gate verdict a PURE
# FUNCTION of (candidate SHA, merge-base SHA, trial-merge tree SHA) — the exact
# triple the signed receipt already binds — with NO ambient input.
#
# The defect it removes is a FALSE PASS, which is the worst thing this program
# can produce. The ruff baseline was computed in the SHARED checkout that ~30
# worktrees and every interactive session write to. A peer's uncommitted lint
# errors inflate BASE, so `NEW > BASE` goes false and a real regression is
# MASKED. The candidate did nothing wrong and the gate said yes anyway.
#
# The cure is one root with one writer: the detached workspace that
# `scripts/gate-workspace.sh` already creates and already REFUSES to --force
# when dirty. This script does not build a second workspace mechanism; it
# refuses to run anywhere else.
#
#   MERGE_GATE_PINNED=1        arm pinned-workspace determinism (opt-in)
#   OMNIAGENTOS_GATE_WORKSPACE the pinned workspace (default: <repo>-gate)
#   MERGE_GATE_AGENT_PROCS     executable names counted for concurrent_agents
#   MERGE_GATE_PY              interpreter override
#
# INSTRUMENT WIDTHS — both are recorded in the run receipt and both are rendered
# into the step-receipt command string, so a receipt minted at one width can
# never be reused to skip a run at another. Changing either changes the
# execution shape, which is exactly why neither may be invisible.
#
#   MERGE_GATE_LADDER_WORKERS  pytest-xdist width for the ladder (unset = serial;
#                              `-n auto` is never accepted, see Makefile:64)
#   MERGE_GATE_CF_POOL_WORKERS counterfeit entry-pool width (default 4 —
#                              VEL-E1, unblocked by the d614 fixture repairs;
#                              set 1 to force serial). Ceiling-clamped by
#                              clamp_workers (cores x nesting depth); NOT
#                              load-aware — on a saturated box width stays 4
#                              and a contended 120s entry-timeout red is an
#                              instrument signal governed by the red-under-load
#                              re-run rule, not a candidate verdict. Bound
#                              unconditionally so an ambient
#                              OMNIAGENTOS_CF_POOL_WORKERS in the operator's
#                              shell cannot make the real width disagree with
#                              the width the receipt claims.
#
# Un-armed, this file behaves as it did before — same order, same checks, same
# REPO default, same exit codes — with ONE deliberate exception: the counterfeit
# pool width now defaults to 4 (VEL-E1; was 1/serial). `git revert` fully
# restores, and width is command-keyed so width-4 step receipts cannot satisfy
# a restored width-1 command.
set -uo pipefail

# --- where am I, and where is the one-writer workspace ----------------------
SELF_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
case "$SELF_DIR" in
  */scripts) SELF_ROOT=$(CDPATH= cd -- "$SELF_DIR/.." && pwd) ;;
  *)         SELF_ROOT="$SELF_DIR" ;;
esac
# Running from inside the gate workspace must not derive `<...>-gate-gate`.
case "$SELF_ROOT" in
  *-gate) SHARED_ROOT="${SELF_ROOT%-gate}" ;;
  *)      SHARED_ROOT="$SELF_ROOT" ;;
esac
PINNED="${MERGE_GATE_PINNED:-0}"

# --- the instrument's own capacity: FILE DESCRIPTORS --------------------------
# 2026-08-07, measured on both machines in this estate: `launchctl limit
# maxfiles` is 256 SOFT / unlimited hard. An interactive shell is raised to
# 1048576 by a login profile; a NON-INTERACTIVE `ssh host cmd` shell never loads
# that profile and inherits 256. Neither this script nor heavy-run touched
# `ulimit`, so a gate started over ssh ran the entire ladder on 256 descriptors
# and died inside two suites the candidate cannot reach — `OSError: [Errno 24]
# Too many open files` and `sqlite3.OperationalError: unable to open database
# file`, both on the same xdist worker — and the run was then classified as a
# CANDIDATE defect. Patching one machine's ~/.zshenv does not travel: a new box,
# a launchd invocation, a fresh workspace or CI all re-inherit 256.
#
# Raising it HERE covers every invocation path regardless of caller or shell,
# and every child inherits it (pytest, its xdist workers, the counterfeit
# harness) because rlimits are inherited across exec.
#
# NEVER LOWER. The interactive shell on this box hands the gate 1048576, which
# is ABOVE the target; a bare `ulimit -n $TARGET` would silently shrink a
# healthy limit to the default. Raise only when the current value is numeric and
# actually smaller.
#
# Guarded on purpose — a platform that refuses the raise must not lose the gate.
# The floor check further down is what turns "we tried" into evidence.
MERGE_GATE_FD_TARGET="${MERGE_GATE_FD_TARGET:-65536}"
case "$MERGE_GATE_FD_TARGET" in ''|*[!0-9]*) MERGE_GATE_FD_TARGET=65536 ;; esac
FD_SOFT_INITIAL=$(ulimit -n 2>/dev/null) || FD_SOFT_INITIAL=""
case "$FD_SOFT_INITIAL" in
  ''|unlimited|*[!0-9]*) : ;;  # unbounded or unreadable: nothing to raise
  *)
    if [ "$FD_SOFT_INITIAL" -lt "$MERGE_GATE_FD_TARGET" ]; then
      ulimit -n "$MERGE_GATE_FD_TARGET" 2>/dev/null || {
        # Second rung: take whatever the hard limit allows rather than nothing.
        _fd_hard=$(ulimit -Hn 2>/dev/null) || _fd_hard=""
        case "$_fd_hard" in
          ''|unlimited|*[!0-9]*) : ;;
          *) [ "$_fd_hard" -gt "$FD_SOFT_INITIAL" ] && ulimit -n "$_fd_hard" 2>/dev/null ;;
        esac
        unset _fd_hard
      }
    fi ;;
esac
FD_SOFT=$(ulimit -n 2>/dev/null) || FD_SOFT=""

# --- sweep dead-pid GNU parallel semaphore slots, class-agnostic ------------
# heavy-run (~/.omniagentos/ops/bin/heavy-run) wraps this script with `sem -q --fg
# --id <class> -j <n> --st <deadman>`. GNU parallel's sem records a slot as a
# hardlinked file named `<pid>@<host>` under
# ~/.parallel/semaphores/id-<class>/. If the sem-wrapped process (this
# script, or a peer under a different class such as heavy-run's default
# id-heavy-pytest) is SIGKILLed, that slot file is never unlinked — the sem
# implementation only removes it on a clean release. A dead pid then holds a
# real slot until sem's own --st staleness timeout expires (up to 7200s for
# the merge-gate class), silently halving or fully blocking admission for
# every caller in that class with no diagnostic output.
#
# The fix is CLASS-AGNOSTIC on purpose: the dead-pid slot observed in
# production was in id-heavy-pytest (heavy-run's default class), not
# id-merge-gate, so a sweep hardcoded to one class would have missed the
# defect that was actually measured. This enumerates every class directory
# under ~/.parallel/semaphores/ and reaps only slots whose pid is
# provably dead (`kill -0` fails with ESRCH-equivalent), never touching a
# live holder.
#
# PLACEMENT (F001, round 2): heavy-run (~/.omniagentos/ops/bin/heavy-run) acquires the
# GNU parallel `sem` slot BEFORE this script is ever exec'd, so a stale slot
# that makes `sem` refuse admission blocks this script from starting at all —
# this entry sweep can never run early enough to fix that path. heavy-run now
# carries its OWN copy of this same sweep (hooked immediately before its `sem`
# call, so it runs regardless of which class is being admitted). This copy
# stays here as defense-in-depth (cheap, and it also catches slots left by
# peers that were NOT invoked through heavy-run) and keeps on_exit's
# release-side cleanup below working unchanged.
#
# ROOT RESOLUTION (F004): matches installed GNU Parallel's own cache_dir
# precedence — PARALLEL_HOME if set, else $XDG_CACHE_HOME/parallel if
# XDG_CACHE_HOME is set, else $HOME/.parallel. Checking only $HOME/.parallel
# silently missed every slot on a host using the XDG layout.
#
# LIVENESS (F002): `kill -0` returning nonzero is NOT proof of death — EPERM
# (pid exists, just unsignalable by us) returns nonzero exactly like ESRCH
# (no such process) does. Only the ESRCH case is proof-of-death, so the error
# TEXT is inspected rather than trusting the exit code alone, and the check is
# repeated immediately before the unlink to narrow (never fully close) the
# check-to-unlink pid-reuse race. The slot's `@host` suffix is also honored: a
# numeric pid recorded by a DIFFERENT host tells us nothing via a local
# `kill -0` (different host, different pid namespace) — on this single-machine
# estate no foreign host will ever come back to release it, so those slots are
# swept unconditionally; same-host slots are only reaped on confirmed ESRCH,
# never on EPERM or an unrecognized error.
#
# FAILURE REPORTING (F003): an unlink that fails is now logged to stderr and
# makes the function return nonzero, instead of being swallowed with an
# unconditional `return 0` — a sweep that left a dead slot in place must not
# look identical to a healthy one.
#
# Run at the START of this script (before this run's own work — the next
# real acquisition after this one is what a stale slot blocks) and again
# from on_exit (best-effort, for graceful terminations this script can trap;
# SIGKILL bypasses on_exit by definition, which is exactly why the start-of-run
# sweep is the one that matters).
sweep_dead_parallel_semaphores() {
  local err_seen=0
  local host
  host=$(hostname 2>/dev/null | tr -d '[:space:]' | tr '[:upper:]' '[:lower:]')
  local roots=()
  if [ -n "${PARALLEL_HOME:-}" ]; then
    roots+=("$PARALLEL_HOME/semaphores")
  else
    [ -n "${XDG_CACHE_HOME:-}" ] && roots+=("$XDG_CACHE_HOME/parallel/semaphores")
    roots+=("$HOME/.parallel/semaphores")
  fi
  local sem_root class_dir slot base pid slot_host dead kerr rc
  for sem_root in "${roots[@]}"; do
    [ -d "$sem_root" ] || continue
    for class_dir in "$sem_root"/*/; do
      [ -d "$class_dir" ] || continue
      for slot in "$class_dir"*; do
        [ -e "$slot" ] || continue
        base="${slot##*/}"
        pid="${base%%@*}"
        case "$pid" in ''|*[!0-9]*) continue ;; esac
        case "$base" in
          *@*) slot_host="${base#*@}" ;;
          *) slot_host="" ;;
        esac
        slot_host=$(printf '%s' "$slot_host" | tr -d '[:space:]' | tr '[:upper:]' '[:lower:]')
        dead=0
        if [ -n "$slot_host" ] && [ -n "$host" ] && [ "$slot_host" != "$host" ]; then
          # Foreign-host slot: a local kill -0 on this pid number is
          # meaningless (different pid namespace). Nothing on this estate
          # will ever come back to release it, so it is swept unconditionally.
          dead=1
        else
          kerr=$(kill -0 "$pid" 2>&1); rc=$?
          if [ "$rc" -ne 0 ]; then
            case "$kerr" in
              *"o such process"*)
                # Re-confirm immediately before unlinking (narrows, does not
                # fully close, the check-to-unlink pid-reuse window).
                kerr=$(kill -0 "$pid" 2>&1); rc=$?
                if [ "$rc" -ne 0 ]; then
                  case "$kerr" in *"o such process"*) dead=1 ;; *) dead=0 ;; esac
                fi
                ;;
              *) dead=0 ;;  # EPERM (alive) or unrecognized: never reap
            esac
          fi
        fi
        if [ "$dead" -eq 1 ]; then
          if ! rm -f -- "$slot" 2>/dev/null; then
            echo "sweep_dead_parallel_semaphores: failed to unlink stale slot $slot" >&2
            err_seen=1
          fi
        fi
      done
    done
  done
  return "$err_seen"
}
if ! sweep_dead_parallel_semaphores; then
  echo "warning: sweep_dead_parallel_semaphores left one or more stale slots in place (unlink failed)" >&2
fi


# --- WHICH gate is judging? --------------------------------------------------
# $0 is chosen by the CALLER. $GATE_WS is pinned, verified and re-checked for
# cleanliness. Nothing tied the two together, so a caller holding a HARDCODED
# script path graded a correctly-pinned modern workspace with a 19-commit-stale
# gate and nobody could see it in the evidence: there are 70 copies of this file
# on the authoring machine and 65 of them have no `contracts-scripts` step at
# all. A judge that cannot name itself is not evidence-grade.
#
# The identity is MEASURED here, unconditionally, so it rides in every receipt
# including the un-pinned ones. The COMPARISON against the pinned workspace's
# own copy lives in the workspace-pin step below, where $PIN_SHA exists.
GATE_SCRIPT_PATH="$SELF_DIR/$(basename -- "$0")"
# BSD ships `shasum`, Linux `sha256sum` — same fallback as the uv.lock digest
# below (E5). Prints NOTHING when neither exists or the read fails; an
# unproducible digest is never a confident value.
sha256_stdin() { { sha256sum 2>/dev/null || shasum -a 256 2>/dev/null; } | awk '{print $1}'; }
# `-s` before hashing, for the same reason the pinned side checks size: an empty
# or unreadable $0 would otherwise digest to e3b0c442… and be compared as a real
# identity.
GATE_SCRIPT_SHA256=""
if [ -s "$GATE_SCRIPT_PATH" ]; then
  GATE_SCRIPT_SHA256=$(sha256_stdin <"$GATE_SCRIPT_PATH" 2>/dev/null)
fi
case "$GATE_SCRIPT_SHA256" in *[!0-9a-f]*) GATE_SCRIPT_SHA256="" ;; esac
[ "${#GATE_SCRIPT_SHA256}" -eq 64 ] || GATE_SCRIPT_SHA256=""
# EMPTY = "not measured", and the receipt renders that as null. NEVER "match":
# an identity that could not be measured must not read as a good one, which is
# the exact defect class the rest of this commit exists to close.
GATE_SCRIPT_PIN_MATCH=""

# Defined ABOVE the argv loop because the loop is its first user. A caller whose
# shell variable expanded to " " (a wrapper that quoted an empty field) must hit
# the same missing-value refusal as one that passed nothing at all: a binding
# made of whitespace is not a binding, and it would otherwise be carried all the
# way into the receipt as a node id no pytest can ever run.
trim_ws() {
  local text="$1"
  text="${text#"${text%%[![:space:]]*}"}"
  printf '%s' "${text%"${text##*[![:space:]]}"}"
}

# --- argument parsing (back-compat: <branch> [signed-receipt.json]) ---------
BRANCH=""
RECEIPT_ARG=""
EMIT_RECEIPT=""
# --- the CLOSURE BINDING: which failing test this candidate claims to fix -----
# NEWLINE-DELIMITED AND ACCUMULATING, never a single scalar. A train carries N
# members and therefore up to N bindings; a store-not-append parser keeps only
# the last one and grades the other N-1 members as if they had no binding at
# all, which is a FALSE GREEN of exactly the class this flag exists to remove.
# Iterated everywhere with `while IFS= read -r`, never word-split: a pytest node
# id may contain `[param]` brackets, spaces and colons.
BOUND_TESTS=""
# "green" | "red" | "weakened", or EMPTY. Empty renders as JSON null and means
# "no binding was passed, or the re-run below was never REACHED" — it does NOT
# mean "the bound test was fine", the same distinction the counterfeit pool
# width already carries. Ranked, worst-wins: see bound_result_record().
BOUND_TEST_RESULT=""
PRINT_RUFF_BASE=0
PREFLIGHT_ONLY=0
while [ "$#" -gt 0 ]; do
  case "$1" in
    --preflight-only)   PREFLIGHT_ONLY=1; shift ;;
    --candidate)        BRANCH="${2:-}"; case "$BRANCH" in ''|-*) echo "refusing: missing-value — --candidate needs a ref" >&2; exit 2 ;; esac; shift 2 ;;
    --candidate=*)      BRANCH="${1#*=}"; case "$BRANCH" in ''|-*) echo "refusing: missing-value — --candidate needs a ref" >&2; exit 2 ;; esac; shift ;;
    --emit-receipt)     EMIT_RECEIPT="${2:-}"; case "$EMIT_RECEIPT" in ''|-*) echo "refusing: missing-value — --emit-receipt needs a path" >&2; exit 2 ;; esac; shift 2 ;;
    --emit-receipt=*)   EMIT_RECEIPT="${1#*=}"; case "$EMIT_RECEIPT" in ''|-*) echo "refusing: missing-value — --emit-receipt needs a path" >&2; exit 2 ;; esac; shift ;;
    --bound-test)       _bt=$(trim_ws "${2:-}"); case "$_bt" in ''|-*) echo "refusing: missing-value — --bound-test needs a node id" >&2; exit 2 ;; esac
                        BOUND_TESTS="${BOUND_TESTS:+$BOUND_TESTS
}$_bt"; shift 2 ;;
    --bound-test=*)     _bt=$(trim_ws "${1#*=}"); case "$_bt" in ''|-*) echo "refusing: missing-value — --bound-test needs a node id" >&2; exit 2 ;; esac
                        BOUND_TESTS="${BOUND_TESTS:+$BOUND_TESTS
}$_bt"; shift ;;
    --print-ruff-base)  PRINT_RUFF_BASE=1; shift ;;
    -h|--help)          sed -n '27,31p' "$0"; exit 0 ;;
    --)                 shift ;;
    -*)                 echo "refusing: unknown-flag — $1" >&2; exit 2 ;;
    *)                  if [ -z "$BRANCH" ]; then BRANCH="$1"
                        elif [ -z "$RECEIPT_ARG" ]; then RECEIPT_ARG="$1"
                        else echo "refusing: extra-argument — $1" >&2; exit 2; fi
                        shift ;;
  esac
done

# --- the ONE root everything derives from ------------------------------------
if [ "$PINNED" = "1" ]; then
  GATE_WS="${OMNIAGENTOS_GATE_WORKSPACE:-${SHARED_ROOT}-gate}"
  REPO="${REPO:-$GATE_WS}"
  # Evidence is DURABLE INSTALLATION STATE, not per-workspace scratch: the
  # store that already holds every candidate receipt is the one a refusal has
  # to land in, or the refusal is unmeasurable — which is the state this
  # package exists to end.
  EVIDENCE_ROOT="${MERGE_GATE_EVIDENCE_ROOT:-$SHARED_ROOT/var/gate-evidence}"
else
  # E3 (CI port, 2026-08-05): derive the default from the script's own location
  # instead of one operator's home path. On that operator's Mac, running
  # scripts/merge-gate.sh from the repo root, $SELF_ROOT IS
  # /Users/youruser/OmniAgentOS, so this is byte-identical there. Off that
  # box the old default named a directory that does not exist, and every check
  # downstream then graded the wrong tree (or nothing at all) instead of saying
  # so; a checkout-relative default fails loudly and correctly.
  REPO="${REPO:-$SELF_ROOT}"
  GATE_WS="$REPO"
  EVIDENCE_ROOT="${MERGE_GATE_EVIDENCE_ROOT:-$REPO/var/gate-evidence}"
fi

# --- interpreter: prioritize gate workspace .venv for isolation ----------------
# The pinned gate workspace now has its own .venv (created by gate-workspace.sh).
# When PINNED=1, we use it to isolate gate judgement from mutations in the shared
# live interpreter. This makes the venv evidence-grade: the receipt records the
# lockfile digest so we can prove which dependency tree judged a candidate.
#
# Resolution order:
#  1. MERGE_GATE_PY (explicit override)
#  2. GATE_WS/.venv/bin/python (gate workspace venv, if PINNED=1)
#  3. REPO/.venv/bin/python (gate workspace repo dir, fallback if no gate venv)
#  4. SHARED_ROOT/.venv/bin/python (shared checkout)
#  5. SELF_ROOT/.venv/bin/python (script location)
PY=""
VENV_STATUS=""
VENV_DIGEST=""
if [ "$PINNED" = "1" ] && [ -x "$GATE_WS/.venv/bin/python" ]; then
  # Gate workspace venv exists and is healthy
  PY="$GATE_WS/.venv/bin/python"
  VENV_STATUS="gate-workspace-venv"
  # Calculate lockfile digest from gate workspace
  if [ -f "$GATE_WS/uv.lock" ]; then
    # E5 (CI port, 2026-08-05): macOS ships `shasum`, not `sha256sum`. Without
    # the fallback the digest silently came back EMPTY on every Darwin host and
    # the receipt's "which dependency tree judged this candidate" claim was
    # unbacked exactly where it is most often read.
    VENV_DIGEST=$({ sha256sum "$GATE_WS/uv.lock" 2>/dev/null \
      || shasum -a 256 "$GATE_WS/uv.lock" 2>/dev/null; } | awk '{print $1}')
  fi
else
  # Degrade to shared interpreters if gate venv is absent or not pinned
  if [ "$PINNED" = "1" ]; then
    VENV_STATUS="gate-venv-unavailable"
  fi
  for _cand in "${MERGE_GATE_PY:-}" "$REPO/.venv/bin/python" "$SHARED_ROOT/.venv/bin/python" "$SELF_ROOT/.venv/bin/python"; do
    if [ -n "$_cand" ] && [ -x "$_cand" ]; then PY="$_cand"; break; fi
  done
  unset _cand
fi

VENV_ROOT=""
if [ -n "$PY" ]; then VENV_ROOT=$(CDPATH= cd -- "$(dirname -- "$PY")/.." && pwd); fi

FAILURES=()

# --- INSTRUMENT ERROR vs CANDIDATE DEFECT ------------------------------------
# "An instrument error must never be reported as a candidate defect" — a refusal
# that blames the code for the gate's own broken environment sends the next
# agent to debug the wrong thing, and across this gate's recorded history 64 of
# 90 refusals were MECHANICS (unpinned or dirty workspace, a moved merge base, a
# stale judge), not candidate defects. Until now nothing in the receipt said so.
#
# THE ADMISSION RULE, and it is mechanical rather than a judgement call:
#
#   a slug may be asserted here ONLY IF its condition is measured BEFORE, or
#   INDEPENDENTLY OF, reading any candidate content.
#
# Every slug below is a property of the gate's own environment — no interpreter,
# no descriptors, no workspace, no linter, the wrong judge — decided without the
# candidate's tree, refs or output ever being consulted. Nothing here parses
# text produced by the tree under judgement, because that is the weakest signal
# available and a wrong "instrument" label EXCUSES a real defect, which is worse
# than no label at all.
#
# FOUR SLUGS WERE REMOVED under this rule on review (2026-08-07):
# `trial-merge-broken`, `reachability-probe-unusable`, `unreadable-diff` and
# `unreadable-history`. All four run git or a probe OVER CANDIDATE REFS, so
# their failure can in principle be caused by what the candidate carries.
# Measured while deciding: a genuine conflict exits 1 and a candidate carrying
# an invalid path (`.git/f.txt`) exits 2 — both land on the `merge-clean`
# FAILURE path and never reach `trial-merge-broken`, whose >=128 branch really
# is dominated by instrument faults (a runner with no committer identity exits
# 128). But "I could not construct a counterexample" is not certainty, and the
# burden here is on ASSERTING, never on refuting.
#
# There is no `false`, only true-or-null: proving a refusal is NOT an instrument
# error is not something this script can measure. A slug that drifts off this
# list therefore degrades to "unclassified", which is the honest default —
# forgetting one can only ever lose a label, never invent one.
GATE_INSTRUMENT_SLUGS="no-interpreter fd-limit-too-low fd-limit-unmeasurable"
GATE_INSTRUMENT_SLUGS="$GATE_INSTRUMENT_SLUGS gate-workspace-missing"
GATE_INSTRUMENT_SLUGS="$GATE_INSTRUMENT_SLUGS gate-workspace-not-a-checkout"
GATE_INSTRUMENT_SLUGS="$GATE_INSTRUMENT_SLUGS unpinned-workspace dirty-workspace"
GATE_INSTRUMENT_SLUGS="$GATE_INSTRUMENT_SLUGS stale-gate-script unverifiable-gate-script"
GATE_INSTRUMENT_SLUGS="$GATE_INSTRUMENT_SLUGS unreadable-repo ruff-unavailable"
GATE_INSTRUMENT_SLUGS="$GATE_INSTRUMENT_SLUGS ruff-baseline-unavailable"
GATE_INSTRUMENT_SLUGS="$GATE_INSTRUMENT_SLUGS unpinned-non-main-head no-gate-worktree"
# A classifier that could not RUN says nothing about the candidate — see
# classifier_rc below. Labelling it here is the difference between "this branch
# is dirty" and "this gate is broken", and sending an agent to debug the first
# when the second is true is the single most expensive misreport this gate makes.
GATE_INSTRUMENT_SLUGS="$GATE_INSTRUMENT_SLUGS classifier-unusable"
# THE ONE SLUG WHOSE CONDITION IS READ OUT OF OUTPUT (2026-08-11), and the three
# claims that admit it against the rule above. Every other slug is decided
# without consulting the candidate; this one is decided from the counterfeit
# harness's stderr, so it owes an argument rather than a precedent:
#
#  1. WHAT IT CLASSIFIES IS NOT A VERDICT ABOUT THE CANDIDATE. The counterfeit
#     CONTROL is the harness's own baseline — the must_fail union run UNPATCHED,
#     before a single corpus entry is scored. Its 300s bound is an instrument
#     capacity, and the harness names it as one: "instrument bound exhausted,
#     not a corpus verdict" (tests/counterfeits/harness.py, run_control). On
#     2026-08-10 that exhaustion — a loaded box, a cold tree — was converted
#     into a REFUSED and the gate daemon rejected an innocent 2-member train.
#  2. THE DIAGNOSTIC IS MAIN'S TEXT, NOT THE CANDIDATE'S. cf_control_bound_
#     exhausted withholds the classification unless tests/counterfeits/harness.py
#     is untouched by EVERY commit in merge-base..candidate ($SWEPT_PATHS, the
#     history-shaped set, so touch-then-revert cannot buy it either). A
#     candidate that ships its own harness is judged by the ordinary rule.
#  3. THE ERROR DIRECTION IS PARK, NEVER PASS. exit 2 merges nothing: the daemon
#     parks or re-runs the train instead of rejecting its members, and the
#     corpus still has to go green before anything lands. A wrong label here
#     costs a re-run; the wrong label it replaces cost innocent members.
#
# Anything the guard cannot PROVE — a grep that could not run, an exit code that
# is not the harness's control-failure 1, a missing or reworded marker —
# degrades to the ordinary corpus refusal. Never the other way.
GATE_INSTRUMENT_SLUGS="$GATE_INSTRUMENT_SLUGS counterfeit-control-timeout"
RUN_INSTRUMENT_ERROR=""

note() { printf '  %-34s %s\n' "$1" "$2"; }
fail() { FAILURES+=("$1: $2"); note "$1" "FAIL — $2"; }
pass() { note "$1" "ok${2:+ — $2}"; }

# --- the bound-test verdict, WORST-WINS --------------------------------------
# A train's N bindings collapse into ONE receipt field, so the collapse rule has
# to be the pessimistic one: with two bindings, one green and one that never
# executed, the run is NOT green. Ranked weakened > red > green, and an
# unrecognised argument is ignored rather than allowed to LOWER the state —
# nothing may talk this field back down.
#   green     every binding EXECUTED and passed on the merged tree
#   red       a binding executed and FAILED — the fix does not close its finding
#   weakened  a binding was defeated rather than satisfied: the candidate edits
#             the bound test file, or the node was skipped/deselected/not
#             collected. NOT_EVALUABLE is not GREEN.
bound_result_record() {  # green|red|weakened
  local rank_new rank_cur
  case "${1:-}" in green) rank_new=1 ;; red) rank_new=2 ;; weakened) rank_new=3 ;; *) return 0 ;; esac
  case "$BOUND_TEST_RESULT" in green) rank_cur=1 ;; red) rank_cur=2 ;; weakened) rank_cur=3 ;; *) rank_cur=0 ;; esac
  [ "$rank_new" -gt "$rank_cur" ] && BOUND_TEST_RESULT="$1"
  return 0
}

utc_now() { date -u +%Y-%m-%dT%H:%M:%SZ; }

# --- host load stamp, sampled ONCE, before any suite -------------------------
# BSD pgrep HAS NO COUNT FLAG — the GNU count option does not exist here and a
# script that reaches for it silently produces an empty string. Count lines
# instead, exactly as scripts/bulletin.sh:63 does, and count by EXECUTABLE NAME
# (-x): matching the full command line instead matched every process whose
# arguments merely contained the repo path and reported 124 agents against a
# real 20.
#
# Both numbers ride INSIDE the signature with every other receipt field: a load
# figure a human can edit after the fact is worth nothing in a re-litigation.
#
# E6 (CI port, 2026-08-05): `:-` treats set-but-EMPTY as unset, so an operator
# who deliberately said "there is no agent fleet on this host" got the four-name
# default re-applied behind their back and the receipt recorded a measured-
# looking 0. `-` (no colon) honours an explicit empty value, and count_agents
# then prints NOTHING, which the receipt records as null: "not measured here",
# never "measured and found zero". On a host that does not set the variable at
# all, this is identical to before.
MERGE_GATE_AGENT_PROCS="${MERGE_GATE_AGENT_PROCS-claude codex grok gemini}"
count_agents() {
  local total=0 exe n
  # No names configured: there is no measurement to report. Print nothing.
  [ -n "${MERGE_GATE_AGENT_PROCS// /}" ] || return 0
  for exe in $MERGE_GATE_AGENT_PROCS; do
    n=$(pgrep -x "$exe" 2>/dev/null | wc -l | tr -d ' ')
    case "$n" in ''|*[!0-9]*) n=0 ;; esac
    total=$(( total + n ))
  done
  printf '%s' "$total"
}
CONCURRENT_AGENTS=$(count_agents)
# E9 (2026-08-06): count_agents is a PROXY and it undercounts. It greps four
# exact executable names once, at startup — so it cannot see this gate's own 8
# xdist workers, the counterfeit pool, a compile, a dashboard build, or any
# agent whose argv[0] is not in the list. Measured on tonight's refusals: it
# recorded 4-11 against host_perf_cores=16 while the host's 1-minute load
# average was 18.7. AGENTS.md's "re-run once when concurrent_agents exceeds
# host_perf_cores" therefore NEVER FIRED on a single one of them: a safety rule
# whose predicate cannot reach its threshold is not a conservative rule, it is
# an absent one. Record the real number alongside the proxy rather than
# replacing it — the proxy stays comparable across the existing corpus, and a
# consumer that wants "was this host oversubscribed" now has an answer that
# counts everything competing for a core.
LOAD_AVG_1M=$(uptime 2>/dev/null | sed -n 's/.*averages*:[[:space:]]*\([0-9.]*\).*/\1/p')
case "$LOAD_AVG_1M" in ''|*[!0-9.]*) LOAD_AVG_1M="" ;; esac
# E4 (CI port, 2026-08-05): `sysctl -n hw.*` exists only on Darwin/BSD, so every
# non-Mac receipt recorded host_perf_cores=0 — which destroys the load-vs-
# duration correlation these receipts exist to support. The sysctl rungs stay
# FIRST, so the Mac answer is unchanged; nproc/getconf are consulted only when
# sysctl produced nothing.
HOST_PERF_CORES=$(sysctl -n hw.perflevel0.logicalcpu 2>/dev/null || true)
case "$HOST_PERF_CORES" in ''|*[!0-9]*) HOST_PERF_CORES=$(sysctl -n hw.logicalcpu 2>/dev/null || echo 0) ;; esac
case "$HOST_PERF_CORES" in
  ''|0|*[!0-9]*)
    if command -v nproc >/dev/null 2>&1; then
      HOST_PERF_CORES=$(nproc 2>/dev/null || echo 0)
    else
      HOST_PERF_CORES=$(getconf _NPROCESSORS_ONLN 2>/dev/null || echo 0)
    fi ;;
esac
case "$HOST_PERF_CORES" in ''|*[!0-9]*) HOST_PERF_CORES=0 ;; esac

# --- NESTED CONCURRENCY: the guard bounds the path it names, and nothing under it
# ==============================================================================
# `heavy-run` holds HEAVY_RUN_TOKENS=1 on sem id `merge-gate`, so exactly one
# OUTER gate runs at a time. That limit works and is not the problem. The
# problem is everything BENEATH it: two counterfeit corpus entries name
# tests/scripts/test_merge_gate_*.py in their must_fail sets, those tests spawn
# real `bash merge-gate.sh` subprocesses, and harness.py's _env() scrubs the
# runtime roots but NOT MERGE_GATE_LADDER_WORKERS / MERGE_GATE_CF_POOL_WORKERS.
# So the outer gate's width rode the environment all the way down into gates
# that never passed through the semaphore at all. Measured on the twin: load
# 107 on 24 cores from a SINGLE gate run. heavy-run's own header records the
# same shape happening once before ("flooded the twin to load 82 on 24 cores on
# its FIRST day") — the token fixed OUTER concurrency and nested was never
# covered. This is the identical class to the worker-env leak already fixed in
# this file for OMNIAGENTOS_GATE_WORKSPACE (2026-08-04, F5).
#
# BOUND THE PRODUCT THAT IS ACTUALLY REALISED. The obvious invariant —
# `cf_pool_workers x ladder_workers` — bounds a number this gate never reaches:
# the ladder and the counterfeit corpus are SEQUENTIAL by construction (see the
# join below; running them together corrupted the shared tree, measured
# 2026-08-03). One gate's realised width is therefore max(ladder, cf-pool), and
# the multiplication happens across DEPTH, not across those two phases. So the
# ceiling is applied to each phase, and depth is what collapses it.
#
# DERIVED, NEVER INHERITED. A nested gate gets ceiling 1 — genuinely serial,
# no xdist worker, no entry pool — regardless of what any caller, env var or
# harness setting says. Both workers below also scrub ALL THREE width variables
# outright (ladder, counterfeit pool, and — since 2026-08-10 — contracts-scripts
# via MERGE_GATE_SUITE_WORKERS), so the depth marker is a belt and the scrub is
# the braces. A width knob added to this script and not to that blank list is
# the incomplete-propagation defect; the equality is pinned by
# tests/scripts/test_merge_gate_suite_width.py.
#
# NOT a semaphore: the outer gate holds the only `merge-gate` token, so a nested
# wait on that id would deadlock forever. A width of 1 needs no token.
MERGE_GATE_DEPTH="${MERGE_GATE_DEPTH:-0}"
case "$MERGE_GATE_DEPTH" in ''|*[!0-9]*) MERGE_GATE_DEPTH=0 ;; esac
GATE_CHILD_DEPTH=$((MERGE_GATE_DEPTH + 1))
GATE_CONCURRENCY_CEILING="${MERGE_GATE_MAX_WORKERS:-}"
case "$GATE_CONCURRENCY_CEILING" in ''|*[!0-9]*|0) GATE_CONCURRENCY_CEILING="" ;; esac
if [ -z "$GATE_CONCURRENCY_CEILING" ]; then
  if [ "$HOST_PERF_CORES" -gt 0 ]; then
    GATE_CONCURRENCY_CEILING="$HOST_PERF_CORES"
  else
    # An unmeasurable core count is not a licence to fan out. 1 is the only
    # honest floor here, and it is the same rule as everywhere else in this
    # file: absence never renders as the favourable value.
    GATE_CONCURRENCY_CEILING=1
  fi
fi
[ "$MERGE_GATE_DEPTH" -ge 1 ] && GATE_CONCURRENCY_CEILING=1

# name, requested -> sets $CLAMP_RESULT; notes the clamp so it is never silent.
#
# The result is a GLOBAL, not stdout, and that is not a style choice: this
# function also has to PRINT the clamp for the operator, and a `$(...)` capture
# would swallow the note into the number. It did — the width came back as the
# report line with a digit stuck on the end, `as_opt_int` rendered that as null,
# and the receipt claimed the ladder width was "not measured" on exactly the
# runs where it had been clamped. One channel per kind of output.
CLAMP_RESULT=""
clamp_workers() {
  local what="$1" want="$2"
  CLAMP_RESULT="$want"
  [ "$want" -gt "$GATE_CONCURRENCY_CEILING" ] || return 0
  CLAMP_RESULT="$GATE_CONCURRENCY_CEILING"
  note "$what" "clamped $want -> $GATE_CONCURRENCY_CEILING (depth $MERGE_GATE_DEPTH, ${HOST_PERF_CORES} perf cores)"
}

# --- ordered step ledger + signed run receipt --------------------------------
RUN_STARTED_AT=$(utc_now)
RECEIPT_MINTED=0
# Explicit init so an ambient exported CF_POOL_WORKERS_HARNESS can never leak
# into a receipt minted by an early refusal or a run whose counterfeit step
# never executed — the field is populated ONLY by the anchored extraction after
# a real harness run, and stays empty (-> null) otherwise.
CF_POOL_WORKERS_HARNESS=""
CANDIDATE_SHA=""
MERGE_BASE_SHA=""
MERGE_TREE_SHA=""
RUFF_BASE=""
RUFF_NEW=""
SCRATCH=""
OPENAPI_TREE=""  # throwaway merge worktree for the openapi-drift regen-verify (FIX 2, 2026-08-04)
STEP_T0=""
STEP_NAME=""
STEPS_LOG=$(mktemp "${TMPDIR:-/tmp}/merge-gate-steps.XXXXXX" 2>/dev/null) || STEPS_LOG=""

step_log() {  # name, started-at, status, detail — append one ordered entry
  [ -n "$STEPS_LOG" ] || return 0
  printf '%s\t%s\t%s\t%s\t%s\n' "$1" "$2" "$(utc_now)" "$3" \
    "$(printf '%s' "${4:-}" | tr '\t\n' '  ' | cut -c1-400)" >>"$STEPS_LOG"
  return 0
}
step_begin() { STEP_NAME="$1"; STEP_T0=$(utc_now); }
step_end() { step_log "$STEP_NAME" "$STEP_T0" "$1" "${2:-}"; }

# The run receipt is a DIFFERENT artifact from the candidate receipt this gate
# CONSUMES: schema omniagentos.merge-gate-run.v1, filename
# "<candidate>.run-<utc>-<pid>.json" so it can never collide with, or be
# mistaken for, "<candidate>.json". Every consumer of the durable store looks
# receipts up by exact path, so an added artifact class is inert to them.
mint_run_receipt() {  # exit-code, refusal-reason
  [ "$RECEIPT_MINTED" -eq 0 ] || return 0
  RECEIPT_MINTED=1
  if [ -z "$PY" ]; then
    printf 'merge-gate: cannot mint run receipt — no interpreter\n' >&2
    return 0
  fi
  MG_EXIT_CODE="$1" MG_REFUSAL="${2:-}" MG_STEPS="$STEPS_LOG" \
  MG_EVIDENCE_ROOT="$EVIDENCE_ROOT" MG_EMIT="$EMIT_RECEIPT" \
  MG_STARTED_AT="$RUN_STARTED_AT" MG_CANDIDATE="$CANDIDATE_SHA" \
  MG_MERGE_BASE="$MERGE_BASE_SHA" MG_MERGE_TREE="$MERGE_TREE_SHA" \
  MG_WORKSPACE="$REPO" MG_GATE_WS="$GATE_WS" MG_RUFF_BASE="$RUFF_BASE" \
  MG_RUFF_NEW="$RUFF_NEW" MG_PINNED="$PINNED" MG_AGENTS="$CONCURRENT_AGENTS" \
  MG_LOAD_AVG_1M="$LOAD_AVG_1M" \
  MG_AGENT_PROCS="$MERGE_GATE_AGENT_PROCS" MG_CORES="$HOST_PERF_CORES" \
  MG_BRANCH="$BRANCH" MG_INTERP="$PY" MG_VENV_STATUS="${VENV_STATUS}" MG_VENV_DIGEST="${VENV_DIGEST}" \
  MG_GATE_SCRIPT="$GATE_SCRIPT_PATH" MG_GATE_SCRIPT_SHA="$GATE_SCRIPT_SHA256" \
  MG_GATE_SCRIPT_PIN_MATCH="$GATE_SCRIPT_PIN_MATCH" \
  MG_FD_SOFT="$FD_SOFT" MG_FD_SOFT_INITIAL="$FD_SOFT_INITIAL" \
  MG_INSTRUMENT_ERROR="$RUN_INSTRUMENT_ERROR" \
  MG_MODE="$([ "$PREFLIGHT_ONLY" -eq 1 ] && printf preflight || printf full)" \
  MG_GATE_DEPTH="$MERGE_GATE_DEPTH" MG_CEILING="$GATE_CONCURRENCY_CEILING" \
  MG_LADDER_WORKERS_REQ="${MERGE_GATE_LADDER_WORKERS:-}" \
  MG_CF_POOL_WORKERS_REQ="${CF_POOL_WORKERS_ASKED:-}" \
  MG_LOAD_AVG_1M_FINAL="$(uptime 2>/dev/null | sed -n 's/.*averages*:[[:space:]]*\([0-9.]*\).*/\1/p')" \
  MG_LADDER_WORKERS="${LADDER_WORKERS_EFFECTIVE:-}" \
  MG_CF_ENTRY_TIMEOUT="${OMNIAGENTOS_CF_ENTRY_TIMEOUT:-}" \
  MG_CF_POOL_WORKERS="${CF_POOL_WORKERS:-}" \
  MG_CF_POOL_WORKERS_HARNESS="${CF_POOL_WORKERS_HARNESS:-}" \
  MG_HOST_PLATFORM="$(uname -s 2>/dev/null || printf unknown)" \
  MG_SUITE_WORKERS="${SUITE_WORKERS_EFFECTIVE:-}" \
  MG_JUNIT_DIR="${JUNIT_KEPT_DIR:-}" \
  MG_BOUND_TESTS="$BOUND_TESTS" MG_BOUND_TEST_RESULT="$BOUND_TEST_RESULT" \
  PYTHONPATH="$SHARED_ROOT" \
  "$PY" - <<'MGRECEIPT' || printf 'merge-gate: run receipt NOT written\n' >&2
import hashlib
import hmac
import json
import os
import sys
import time
from pathlib import Path


def env(name):
    return os.environ.get(name, "") or ""


def as_int(value, default=0):
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


def as_opt_int(value):
    text = str(value).strip()
    if not text:
        return None
    try:
        return int(text)
    except ValueError:
        return None


def as_opt_bool(value):
    """Render "1" as True and "0" as False; ANYTHING ELSE as None.

    Deliberately not `bool(value)`: the only two answers this may render as a
    measurement are the two the shell explicitly measured. An unset, empty or
    unparseable value is "not measured", which is None — never the favourable
    one.
    """
    text = str(value).strip()
    if text == "1":
        return True
    if text == "0":
        return False
    return None


root = Path(env("MG_EVIDENCE_ROOT")).expanduser()

steps = []
steps_path = env("MG_STEPS")
if steps_path and os.path.exists(steps_path):
    with open(steps_path, encoding="utf-8", errors="replace") as handle:
        for line in handle:
            line = line.rstrip("\n")
            if not line:
                continue
            parts = line.split("\t")
            parts += [""] * (5 - len(parts))
            steps.append(
                {
                    "name": parts[0],
                    "started_at": parts[1],
                    "finished_at": parts[2],
                    "status": parts[3],
                    "detail": parts[4],
                }
            )

now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
payload = {
    "schema": "omniagentos.merge-gate-run.v1",
    "routine_id": "merge-gate",
    "gate_type": "merge_gate_run",
    "branch": env("MG_BRANCH"),
    "candidate_sha": env("MG_CANDIDATE"),
    "merge_base_sha": env("MG_MERGE_BASE"),
    "merge_tree_sha": env("MG_MERGE_TREE"),
    "workspace": env("MG_WORKSPACE"),
    "gate_workspace": env("MG_GATE_WS"),
    "pinned": env("MG_PINNED") == "1",
    "interpreter": env("MG_INTERP"),
    "exit_code": as_int(env("MG_EXIT_CODE"), 2),
    "refusal_reason": env("MG_REFUSAL"),
    "ruff_base": as_opt_int(env("MG_RUFF_BASE")),
    "ruff_new": as_opt_int(env("MG_RUFF_NEW")),
    # null, not 0, when no process names were configured: "not measured on this
    # host" and "measured, found none" are different claims and a receipt that
    # cannot tell them apart is not evidence (E6).
    "concurrent_agents": as_opt_int(env("MG_AGENTS")),
    "agent_process_names": env("MG_AGENT_PROCS").split(),
    "host_perf_cores": as_int(env("MG_CORES"), 0),
    "host_platform": env("MG_HOST_PLATFORM"),
    # A PREFLIGHT receipt grades only the hoisted cheap refusals. It must never
    # be mistaken for a full-gate verdict, so the mode rides INSIDE the
    # signature with everything else.
    "mode": env("MG_MODE") or "full",
    # Instrument bounds, recorded so a loosened instrument is visible in the
    # evidence rather than invisible: ladder parallelism (E7) and the per-entry
    # counterfeit timeout (E8). Empty means "left at the built-in default".
    # EFFECTIVE, not requested: this is the width the process used AND the width
    # rendered into the step-receipt command string. The request is recorded
    # beside it so a clamp is visible rather than silent.
    "ladder_workers": as_opt_int(env("MG_LADDER_WORKERS")),
    "ladder_workers_requested": as_opt_int(env("MG_LADDER_WORKERS_REQ")),
    "counterfeit_pool_workers_requested": as_opt_int(env("MG_CF_POOL_WORKERS_REQ")),
    # Same rule for the contracts step's width (2026-08-10): EFFECTIVE,
    # i.e. post-clamp, and therefore exactly the width its step-receipt command
    # string claims. null = the step ran serial, which is also what an unset
    # MERGE_GATE_SUITE_WORKERS/MERGE_GATE_LADDER_WORKERS pair produces.
    "suite_workers": as_opt_int(env("MG_SUITE_WORKERS")),
    # WHERE THE FAILING TESTS NAMED THEMSELVES. A directory of per-step JUnit
    # XML, written ONLY for steps that failed, so the daemon can quote node ids
    # instead of asking for a rerun. null = nothing failed, or the copy could
    # not be made — it never means "the failures were not worth recording".
    "junit_dir": env("MG_JUNIT_DIR") or None,
    # NESTED CONCURRENCY. depth 0 is the operator's gate; anything the gate
    # itself starts runs at >= 1 and is clamped to a ceiling of 1. A run that
    # flooded the host is diagnosable from these three numbers alone.
    "gate_depth": as_int(env("MG_GATE_DEPTH"), 0),
    "concurrency_ceiling": as_opt_int(env("MG_CEILING")),
    # E9: the REAL contention number beside the proxy. Empty (never 0) when the
    # host could not be measured — an unmeasurable load must not read as an idle
    # one.
    "load_avg_1m": env("MG_LOAD_AVG_1M") or None,
    # Sampled again AT MINT. The start-of-run figure cannot show what the run
    # itself did to the host, which is the number that mattered when one
    # candidate took the twin to 107. This is DIAGNOSIS, deliberately not
    # control: see the header note on why an in-flight ceiling is not wired.
    "load_avg_1m_final": env("MG_LOAD_AVG_1M_FINAL") or None,
    "counterfeit_entry_timeout": env("MG_CF_ENTRY_TIMEOUT"),
    # The counterfeit pool width the gate EXPORTED to the worker process AND
    # rendered into the step-receipt command string — the two are bound to one
    # shell variable at the call site so they cannot drift. null means the
    # counterfeit step was never REACHED on this run (no tests/counterfeits/ in
    # the merged tree, or a refusal above it); it does NOT mean "ran serial".
    # Those are different claims and a receipt that cannot tell them apart is
    # not evidence. NOTE: this is the CONFIGURED width; the width the harness
    # itself says it used is recorded separately below, parsed from its own
    # output — a harness whose width resolution was tampered with can disagree
    # with the export, and a reader comparing the two fields sees it.
    "counterfeit_pool_workers": as_opt_int(env("MG_CF_POOL_WORKERS")),
    # Harness-reported width, from the `pool_workers=N` line the harness prints
    # in counterfeit.out. null when the step never ran or the line is absent.
    "counterfeit_pool_workers_harness": as_opt_int(env("MG_CF_POOL_WORKERS_HARNESS")),
    "started_at": env("MG_STARTED_AT") or now,
    "finished_at": now,
    "steps": steps,
    "venv_status": env("MG_VENV_STATUS"),
    "venv_digest": env("MG_VENV_DIGEST"),
    # WHICH COPY of this script produced the verdict. $0 is the caller's choice
    # while the workspace is pinned and verified, and 65 of the 70 copies of
    # merge-gate.sh on the authoring machine are missing a whole step — so
    # "graded by a stale judge" was, until now, invisible in the evidence.
    "gate_script_path": env("MG_GATE_SCRIPT"),
    "gate_script_sha256": env("MG_GATE_SCRIPT_SHA") or None,
    # null when the comparison could not be made (no scripts/merge-gate.sh at
    # the pinned SHA, or no hasher on the host). NEVER true: an unmeasurable
    # identity is not a matching one.
    "gate_script_pin_match": as_opt_bool(env("MG_GATE_SCRIPT_PIN_MATCH")),
    # THE INSTRUMENT'S OWN CAPACITY. Recorded as the raw ulimit WORD ("65536",
    # "unlimited") rather than an int, so an unbounded limit is not rendered as
    # "not measured"; null means the shell could not report it at all. The
    # _initial value is what the CALLER's shell handed the gate before the raise
    # at the top of this script — "the caller gave us 256" is a fact about the
    # invocation, not about the candidate, and a run that dies on Errno 24
    # should be diagnosable from its receipt instead of from a traceback.
    "fd_limit_soft": env("MG_FD_SOFT") or None,
    "fd_limit_soft_initial": env("MG_FD_SOFT_INITIAL") or None,
    # true only where the gate is CERTAIN this run says nothing about the
    # candidate (see GATE_INSTRUMENT_SLUGS, and abnormal termination). null =
    # not classified, which is the honest answer for every ordinary refusal.
    # There is deliberately no false: a wrong "instrument" label excuses a real
    # defect, so this field may only ever be asserted, never denied.
    "instrument_error": True if env("MG_INSTRUMENT_ERROR") == "1" else None,
    # THE CLOSURE BINDING. An ARRAY (never a bare string) so a train's N
    # bindings are all legible in one receipt, and null — never [] — when no
    # binding was passed at all: "this run was never told what it was closing"
    # and "this run closed nothing" are different claims.
    "bound_test": [line for line in env("MG_BOUND_TESTS").split("\n") if line.strip()] or None,
    # Whitelisted, so an unrecognised value from a future caller renders as
    # "not measured" instead of leaking through as a verdict. null = no binding
    # was passed, or the re-run was never REACHED (a refusal above it, or no
    # clean trial merge) — it never means the bound test was fine.
    "bound_test_result": (
        env("MG_BOUND_TEST_RESULT").strip()
        if env("MG_BOUND_TEST_RESULT").strip() in {"green", "red", "weakened"}
        else None
    ),
}

# The signing key is the installation's, read from the same file the evidence
# store reads. An unsigned "receipt" is a self-report, so a missing key is a
# hard failure to write rather than a file with an empty signature field.
key_path = root / "signing.key"
try:
    from omniagentos.scheduler.gate_evidence import GateEvidenceStore

    GateEvidenceStore(root, create_key=True)
except Exception as exc:  # noqa: BLE001 - key provisioning is best-effort
    print(f"merge-gate: could not provision signing key: {exc}", file=sys.stderr)
try:
    key = key_path.read_bytes()
except OSError as exc:
    print(f"merge-gate: signing key unreadable at {key_path}: {exc}", file=sys.stderr)
    raise SystemExit(3) from None
if len(key) < 32:
    print(f"merge-gate: signing key is invalid at {key_path}", file=sys.stderr)
    raise SystemExit(3)

body = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
payload["signature"] = hmac.new(key, body, hashlib.sha256).hexdigest()
text = json.dumps(payload, sort_keys=True, indent=4) + "\n"

records = root / "records" / "merge-gate"
records.mkdir(parents=True, exist_ok=True, mode=0o700)
stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
name = f"{payload['candidate_sha'] or 'no-candidate'}.run-{stamp}-{os.getpid()}.json"
durable = records / name
suffix = 0
while durable.exists():
    suffix += 1
    durable = records / f"{name[:-5]}-{suffix}.json"
durable.write_text(text, encoding="utf-8")

emit = env("MG_EMIT")
if emit:
    target = Path(emit).expanduser()
    if target.parent and not target.parent.exists():
        target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")
print(f"merge-gate: run receipt {durable}", file=sys.stderr)
MGRECEIPT
  return 0
}

# Every early exit leaves evidence. Before this, `exit 2` at seven call sites
# and `exit 1` at the verdict left NO record at all, so the entire refusal side
# of this gate was unmeasurable: all 77 timed receipts on disk carried
# exit_code 0 and a 2.0s median, which describes only the runs that passed.
refuse() {  # reason-slug, detail, [exit-code]
  local slug="$1" detail="${2:-}" code="${3:-2}"
  # Whole-word match against the named set above; anything unlisted stays
  # unclassified rather than being guessed at. (`reachability` therefore does
  # NOT match `reachability-probe-unusable`: the first is a verdict about the
  # candidate's code, the second is the probe failing to run at all.)
  case " $GATE_INSTRUMENT_SLUGS " in *" $slug "*) RUN_INSTRUMENT_ERROR=1 ;; esac
  step_end "refused" "$slug${detail:+: $detail}"
  printf 'refusing: %s%s\n' "$slug" "${detail:+ — $detail}" >&2
  mint_run_receipt "$code" "$slug${detail:+: $detail}"
  exit "$code"
}

# --- a classifier that could not run must never read as "nothing found" -------
# grep's exit status carries THREE meanings — 0 matched, 1 no match (normal),
# >=2 grep ITSELF FAILED — and the `|| true` that used to sit on every classifier
# below erased the difference between the last two. A grep that could not run
# produced an empty capture, and every check reads empty as "nothing forbidden":
# the gate printed `secrets ok` and went on to mint a SIGNED PASS RECEIPT
# asserting the secret scan had been performed.
#
# scripts/gates/forbidden-paths.sh:138-158 records this REPRODUCED against its
# own copy of the same shape and carries the fix; it never propagated here. It is
# reachable without an adversary or a PATH shim: a malformed pattern and an
# unreadable input BOTH make real grep exit 2 (verified on this host).
#
# `|| true` has to be REMOVED, not supplemented — while it is present `$?` is
# always 0 and no guard placed beside it can observe anything. It is not
# load-bearing: this script is `set -uo pipefail` with no `-e` and no ERR trap,
# so a bare assignment returning 1 continues exactly as before (verified). If
# `set -e` is ever added, these captures must keep their own rc test rather than
# have `|| true` reinstated.
#
# Called at TOP LEVEL, never from inside the `$( )` being tested: `refuse` exits,
# and an exit from within a command substitution would kill only the subshell,
# assign empty, and fail open exactly as before — the trap forbidden-paths.sh
# documents at :147-150.
classifier_rc() {  # rc, classifier-name
  # A non-numeric status is itself a gate fault and must not be waved through.
  # `[ "" -ge 2 ]` does NOT evaluate false — it ERRORS, `&&` then skips the
  # refusal, and the helper returns 0. That is this very fail-open reproduced
  # INSIDE the guard written to close it, so it is checked first and explicitly.
  case "${1:-}" in
    '' | *[!0-9]*)
      refuse "classifier-unusable" \
        "the $2 classifier reported a non-numeric status '${1:-<empty>}'; refusing rather than guessing" ;;
  esac
  [ "$1" -ge 2 ] && refuse "classifier-unusable" \
    "the $2 classifier could not run (exited $1); this is a gate fault, not a verdict on the candidate"
  return 0
}

# --- the reachability exemption trap (2026-08-07) -----------------------------
# devtasks/REACHABILITY-EXEMPT.txt documents this in its own header, and it is
# still the single most expensive refusal on this gate. scripts/reachability-
# gate.py grades the CANDIDATE'S CODE (`git show <ref>:<path>`) but resolves
# EXEMPT_FILE from its own __file__ — so it reads the exemption list out of the
# checkout the gate RUNS IN, which is the pinned main workspace, never the
# branch. An agent that adds its exemption ON THE BRANCH is therefore refused AT
# THE EXACT SYMBOL IT JUST EXEMPTED, reads that as a verdict about its code, and
# re-gates unchanged. Measured over 90 historical refusals: 32 were reachability
# and 28 of those were ONE symbol, `seed_cursor` — which is exempted on main
# today with a perfectly good reason. ~28 full gate cycles to discover a rule
# about WHICH COPY OF A FILE IS READ.
#
# This changes NOTHING the gate accepts. It detects one specific confusing
# state — the refused symbol IS exempt in the candidate's copy of the file and
# is NOT exempt in the running checkout's copy — and makes the refusal name its
# own remedy. A refusal that explains itself is the whole fix.
#
# The exemption parse below mirrors _exemptions() in reachability-gate.py
# exactly (strip, drop blank and '#' lines, take the first whitespace-delimited
# field) and the match mirrors its `f"{path}:{sym}" in exempt or sym in exempt`.
# If those two ever diverge this helper goes quiet — it can only ever ADD an
# explanation to a refusal that already happened, never suppress one.
reach_exempt_keys() {  # reads exemption text on stdin -> one key per line
  awk '{ sub(/^[ \t]+/, ""); sub(/[ \t]+$/, "") }
       $0 != "" && $0 !~ /^#/ { print $1 }'
}

# candidate-ref, gate-output -> stdout: "<all|mixed>|<trapped keys>"; rc 1 = no trap.
#
# The SCOPE prefix exists because the remedy is only complete when EVERY refused
# symbol is trapped. A run that refuses one trapped symbol and one genuinely
# unwired symbol must not be told that landing the exemption on main clears the
# refusal — that would be this repo's favourable-absence class pointed at the
# operator: a partial explanation rendered as a complete one.
reach_exempt_trap() {
  local ref="$1" out="$2"
  local cand_keys run_keys refused key sym hits="" n_refused=0 n_hits=0
  # The candidate's copy. A ref that has no such file (or an unreadable one)
  # yields nothing and the helper stays silent — absence of the file is not
  # evidence of the trap.
  cand_keys=$(git show "$ref:devtasks/REACHABILITY-EXEMPT.txt" 2>/dev/null | reach_exempt_keys)
  [ -n "$cand_keys" ] || return 1
  # The running checkout's copy — the one the probe actually read.
  run_keys=$(reach_exempt_keys <"$REPO/devtasks/REACHABILITY-EXEMPT.txt" 2>/dev/null)
  # Parse ONLY the refusal block. The `framework-registered:` lines printed
  # above the "REFUSED" header are PASSES and are indented identically; taking
  # them as refusals would report a trap on a symbol the gate accepted.
  refused=$(printf '%s\n' "$out" | awk '
    /^REFUSED/ { seen = 1; next }
    seen && /^[ \t]+[^ \t]+:[0-9]+[ \t]+[A-Za-z_][A-Za-z0-9_]*\(\)[ \t]*$/ {
      line = $0; sub(/^[ \t]+/, "", line);
      split(line, f, /[ \t]+/);
      p = f[1]; sub(/:[0-9]+$/, "", p);
      s = f[2]; sub(/\(\)$/, "", s);
      print p ":" s
    }' | sort -u)
  [ -n "$refused" ] || return 1
  for key in $refused; do
    n_refused=$((n_refused + 1))
    sym="${key##*:}"
    printf '%s\n' "$cand_keys" | grep -Fxq -e "$key" -e "$sym" || continue
    printf '%s\n' "$run_keys"  | grep -Fxq -e "$key" -e "$sym" && continue
    hits="${hits:+$hits }$key"
    n_hits=$((n_hits + 1))
  done
  [ -n "$hits" ] || return 1
  if [ "$n_hits" -eq "$n_refused" ]; then
    printf 'all|%s' "$hits"
  else
    printf 'mixed|%s' "$hits"
  fi
}

reach_exempt_explain() {  # trapped-symbol list, scope ("all" | "mixed")
  cat >&2 <<EXPLAIN

  ---------------------------------------------------------------------------
  READ THIS BEFORE RE-GATING — the refusal above is about WHERE the exemption
  lives, not about your code.

    $1

  Each symbol above IS exempted in devtasks/REACHABILITY-EXEMPT.txt on the
  CANDIDATE, and is NOT exempted in the copy the gate actually read:
    $REPO/devtasks/REACHABILITY-EXEMPT.txt
  The reachability probe grades the candidate's CODE but reads the EXEMPTION
  FILE from the checkout it runs in (the pinned main workspace). A branch-side
  exemption therefore reads as absent, and the gate refuses at the exact symbol
  you just exempted.
EXPLAIN
  if [ "${2:-all}" = "all" ]; then
    cat >&2 <<'EXPLAIN_ALL'

  RE-RUNNING THE GATE UNCHANGED WILL REFUSE IDENTICALLY. It did so 28 times for
  one symbol (`seed_cursor`) before this message existed.
EXPLAIN_ALL
  else
    cat >&2 <<'EXPLAIN_MIXED'

  NOT EVERY SYMBOL IN THE REFUSAL ABOVE IS THIS TRAP. The remedy below clears
  ONLY the symbols listed here; the rest are ordinary reachability refusals and
  need a production caller (or their own exemption WITH A REASON). Landing the
  exemption on main will NOT, on its own, make this run pass.
EXPLAIN_MIXED
  fi
  cat >&2 <<'EXPLAIN_REMEDY'

  REMEDY: land the exemption line on main FIRST, as its own `chore(gates):`
  commit, then re-run the gate. Leaving the identical line on the branch is
  harmless — the three-way merge resolves it to one copy.
  ---------------------------------------------------------------------------
EXPLAIN_REMEDY
}

# --- a run that ENDED is not a run that FINISHED ------------------------------
# `on_exit` minted `mint_run_receipt "$rc" ...` with `rc=$?`, and bash runs an
# EXIT trap with $? == 0 when it takes a signal BETWEEN commands. Measured on
# this box (bash 3.2.57): SIGTERM delivered during `wait` runs the trap with
# $? == 0, and an untrapped SIGINT makes the whole script exit 0 at the process
# level too. So a gate killed mid-suite minted `exit_code: 0` with every
# completed step "ok" and no failures — and every consumer keying on exit_code
# read that as PASS. Six such receipts sit in the corpus and TWO landed
# candidates (e78d5611eb3a, db42fa7f144d) have no other complete clean run.
#
# 70 is outside every code this script chooses deliberately (0 pass, 1 refused
# with findings, 2 refuse, 3 unsigned receipt), so an abnormal termination can
# never be confused with a verdict — in the receipt OR in the process status.
GATE_ABNORMAL_EXIT_CODE=70

on_exit() {
  local rc=$?
  # Best-effort release-side sweep: catches this run's own class if it
  # somehow leaked (e.g. a signal this trap DID catch), and any other class'
  # dead slots left by peers since the start-of-run sweep above ran. (F003:
  # a failure is still logged, but never allowed to override this trap's own
  # $rc — this cleanup is best-effort by design.)
  sweep_dead_parallel_semaphores || echo "warning: sweep_dead_parallel_semaphores (on_exit) left one or more stale slots in place (unlink failed)" >&2
  if [ -n "$SCRATCH" ]; then
    cd "$REPO" 2>/dev/null && {
      git worktree remove --force "$SCRATCH" 2>/dev/null
      git worktree prune
    }
  fi
  if [ -n "$OPENAPI_TREE" ]; then
    cd "$REPO" 2>/dev/null && {
      git worktree remove --force "$OPENAPI_TREE" 2>/dev/null
      git worktree prune
    }
  fi
  # RECEIPT_MINTED is the only honest test for "a verdict was reached": both
  # terminal paths and every refuse() mint before exiting, and --print-ruff-base
  # sets it to suppress a receipt for a debug probe. So reaching here with it
  # still 0 IS the abnormal case, whatever $? happens to say.
  local abrc="$rc"
  if [ "$RECEIPT_MINTED" -eq 0 ]; then
    case "$rc" in 0) abrc="$GATE_ABNORMAL_EXIT_CODE" ;; esac
    # A run that produced no verdict is, by construction, saying nothing about
    # the candidate — the one instrument classification that needs no list.
    RUN_INSTRUMENT_ERROR=1
    step_log "gate-terminated" "$(utc_now)" "aborted" \
      "no verdict was reached; process exit status $rc"
  fi
  mint_run_receipt "$abrc" \
    "gate exited without an explicit verdict (abnormal termination; process exit status $rc)"
  [ -n "$STEPS_LOG" ] && rm -f "$STEPS_LOG"
  # Only ever raises 0 to non-zero, and only when no verdict was minted, so no
  # deliberate exit code in this script is rewritten. A shell killed by a signal
  # re-raises it after the trap (SIGTERM still exits 143); the override is what
  # stops an untrapped SIGINT from handing a caller a clean 0.
  [ "$abrc" = "$rc" ] || exit "$abrc"
  return 0
}
trap on_exit EXIT

# ============================================================================
# HOISTED PREFLIGHT — every sub-5s refusal, ABOVE the ~12-minute ladder
# ============================================================================
# Measured ladder cost before this: median 325s, p90 453s, max 609s; the
# counterfeit corpus median 362s, max 774s; 742.8 minutes across three days.
# Nothing below costs more than a second, and each one is a complete verdict on
# its own, so paying twelve minutes to reach it was pure waste.

# --- E1 (CI port, 2026-08-05): the interpreter guard is UNCONDITIONAL --------
# This check used to live INSIDE the `PINNED` block below. An UNPINNED run with
# no interpreter therefore reached the ruff comparison, the receipt verifier and
# the suites with PY="" and could still print PASS — a verdict assembled out of
# empty strings. A missing interpreter is a complete verdict on every path, so
# it is answered before any path branches. On a machine with a .venv this never
# fires and nothing downstream changes.
step_begin "interpreter"
[ -n "$PY" ] || refuse "no-interpreter" "no python at \$MERGE_GATE_PY, $REPO/.venv, or $SHARED_ROOT/.venv"
step_end "ok" "$PY"

# --- CAPACITY IS A PRECONDITION, NOT A POST-MORTEM ---------------------------
# The raise at the top of this file is best-effort. This is where "we tried"
# becomes a verdict: if the gate is about to spend twelve minutes producing a
# ladder result it cannot trust, refusing in under a second — AS AN INSTRUMENT
# ERROR, so the next agent does not go and debug the candidate — is strictly
# better. The 2026-08-07 run that motivated this ran on 256 descriptors and
# blamed a candidate that touches one test file and zero product code.
#
# The floor is 4x the value that was measured to fail, and every mainstream
# platform's HARD limit is far above the 65536 target, so the raise succeeds and
# this never fires. Where a platform genuinely cannot exceed the floor, an
# operator sets MERGE_GATE_FD_FLOOR and owns the consequence explicitly.
#
# PINNED and full runs only: --preflight-only never reaches a suite, and the
# un-armed path is what the fixture modules and the counterfeit corpus drive —
# neither runs real pytest, so neither can hit the limit, and neither is a real
# gate verdict.
MERGE_GATE_FD_FLOOR="${MERGE_GATE_FD_FLOOR:-1024}"
case "$MERGE_GATE_FD_FLOOR" in ''|*[!0-9]*) MERGE_GATE_FD_FLOOR=1024 ;; esac
if [ "$PINNED" = "1" ] && [ "$PREFLIGHT_ONLY" -eq 0 ]; then
  step_begin "fd-limit"
  case "$FD_SOFT" in
    unlimited)
      step_end "ok" "unlimited" ;;
    ''|*[!0-9]*)
      # Unmeasurable is NOT satisfied. `ulimit` is a shell builtin; a shell that
      # cannot answer is not one this gate should trust a suite verdict from.
      refuse "fd-limit-unmeasurable" \
        "the shell could not report its own file-descriptor limit (got '${FD_SOFT}') — refusing rather than running a suite whose capacity is unknown" ;;
    *)
      [ "$FD_SOFT" -ge "$MERGE_GATE_FD_FLOOR" ] || refuse "fd-limit-too-low" \
        "$FD_SOFT open files (floor $MERGE_GATE_FD_FLOOR); the caller's shell supplied ${FD_SOFT_INITIAL:-unknown} and the raise to $MERGE_GATE_FD_TARGET could not exceed the hard limit. This is an INSTRUMENT limit, not a candidate defect: at 256 the ladder died with 'OSError: [Errno 24] Too many open files' in suites the candidate cannot reach. REMEDY: raise the hard limit (launchctl limit maxfiles / limits.conf), or invoke the gate from a shell that already has one, or set MERGE_GATE_FD_FLOOR to accept this capacity deliberately."
      step_end "ok" "$FD_SOFT${FD_SOFT_INITIAL:+ (caller supplied $FD_SOFT_INITIAL)}" ;;
  esac
fi

if [ "$PINNED" = "1" ]; then
  step_begin "workspace-pin"
  [ -d "$GATE_WS" ] || refuse "gate-workspace-missing" "$GATE_WS — run scripts/gate-workspace.sh main"
  git -C "$GATE_WS" rev-parse --verify HEAD >/dev/null 2>&1 \
    || refuse "gate-workspace-not-a-checkout" "$GATE_WS"
  REPO_REAL=$(CDPATH= cd -- "$REPO" 2>/dev/null && pwd -P) || REPO_REAL=""
  WS_REAL=$(CDPATH= cd -- "$GATE_WS" && pwd -P)
  # The shared checkout is written by ~30 worktrees and every interactive
  # session. Deriving merge-base from its HEAD is why a detached or off-main
  # shared root refused every valid receipt TWELVE MINUTES in. Refuse in under
  # a second instead, and name the actual reason.
  [ -n "$REPO_REAL" ] && [ "$REPO_REAL" = "$WS_REAL" ] \
    || refuse "unpinned-workspace" "REPO=$REPO is not the pinned gate workspace $WS_REAL"
  PIN_SHA=$(git -C "$GATE_WS" rev-parse --verify HEAD 2>/dev/null)

  # --- REFUSE A STALE JUDGE ---------------------------------------------------
  # Everything above pins the TREE. Nothing pinned the SCRIPT: `$0` is whatever
  # the caller typed, so one input templated at the workspace and the other
  # hardcoded in the same command is enough to grade a correctly-pinned modern
  # workspace with a 19-commit-stale gate — silently, because a gate that is
  # missing a step does not report a missing step. That happened, and 65 of the
  # 70 copies of this file on the authoring machine can reproduce it.
  #
  # THE EMPTY-DIGEST TRAP, AND ITS SECOND DOOR. `sha256sum` over EMPTY INPUT
  # returns e3b0c442…, a perfectly confident-looking 64 hex characters for
  # content that was never read — so any path where the producer silently
  # yields nothing gets compared as if it were a real identity. Three guards,
  # and the middle one was added on review after a test caught it:
  #   TYPE  — `rev-parse` resolves a TREE just as happily as a blob, and
  #           `cat-file blob <tree>` then fails while the hasher still answers.
  #           A DIRECTORY at this path was therefore reported as a stale gate
  #           with a fabricated sha mismatch: the wrong diagnosis, sending the
  #           operator to the wrong remedy.
  #   SIZE  — a zero-byte script is not an identity either.
  #   READ  — hash a MATERIALISED copy, never `cat-file | hasher`: a pipeline
  #           hides the producer's exit status, which is exactly how the tree
  #           got through. Non-empty file, or no digest at all.
  PIN_GATE_TYPE=$(git -C "$GATE_WS" cat-file -t "$PIN_SHA:scripts/merge-gate.sh" 2>/dev/null) \
    || PIN_GATE_TYPE=""
  PIN_GATE_BLOB=""
  if [ "$PIN_GATE_TYPE" = "blob" ]; then
    PIN_GATE_BLOB=$(git -C "$GATE_WS" rev-parse --verify -q "$PIN_SHA:scripts/merge-gate.sh" 2>/dev/null) \
      || PIN_GATE_BLOB=""
  fi
  PIN_GATE_SIZE=0
  if [ -n "$PIN_GATE_BLOB" ]; then
    PIN_GATE_SIZE=$(git -C "$GATE_WS" cat-file -s "$PIN_GATE_BLOB" 2>/dev/null) || PIN_GATE_SIZE=0
    case "$PIN_GATE_SIZE" in ''|*[!0-9]*) PIN_GATE_SIZE=0 ;; esac
  fi
  PIN_GATE_SHA256=""
  if [ "$PIN_GATE_SIZE" -gt 0 ]; then
    PIN_GATE_TMP=$(mktemp "${TMPDIR:-/tmp}/merge-gate-pinjudge.XXXXXX" 2>/dev/null) || PIN_GATE_TMP=""
    if [ -n "$PIN_GATE_TMP" ]; then
      if git -C "$GATE_WS" cat-file blob "$PIN_GATE_BLOB" >"$PIN_GATE_TMP" 2>/dev/null \
         && [ -s "$PIN_GATE_TMP" ]; then
        PIN_GATE_SHA256=$(sha256_stdin <"$PIN_GATE_TMP" 2>/dev/null)
      fi
      rm -f "$PIN_GATE_TMP"
    fi
    case "$PIN_GATE_SHA256" in *[!0-9a-f]*) PIN_GATE_SHA256="" ;; esac
    [ "${#PIN_GATE_SHA256}" -eq 64 ] || PIN_GATE_SHA256=""
  fi
  if [ -n "$GATE_SCRIPT_SHA256" ] && [ -n "$PIN_GATE_SHA256" ]; then
    if [ "$GATE_SCRIPT_SHA256" = "$PIN_GATE_SHA256" ]; then
      GATE_SCRIPT_PIN_MATCH=1
    else
      GATE_SCRIPT_PIN_MATCH=0
      # NAME THE REMEDY. A refusal an operator cannot act on buys a re-run at
      # full price; the measured cost of that class on this gate is 64 of 90
      # historical refusals.
      refuse "stale-gate-script" \
"the running gate is not the one the pinned workspace carries — running $GATE_SCRIPT_PATH (sha256 ${GATE_SCRIPT_SHA256:0:12}), pinned $GATE_WS/scripts/merge-gate.sh at $PIN_SHA (sha256 ${PIN_GATE_SHA256:0:12}). REMEDY: invoke the gate FROM the workspace it grades — bash $GATE_WS/scripts/merge-gate.sh <candidate> — or re-pin the workspace with scripts/gate-workspace.sh main. Templating one input and hardcoding the other is what produces this."
    fi
  else
    # FAILS CLOSED. This branch used to continue with a printed UNVERIFIED note
    # and `gate_script_pin_match: null` — visible, but still a PASS. Review
    # 2026-08-07 overturned that, correctly: recording null makes an
    # unverifiable identity VISIBLE, it does not make it SAFE, and "could not
    # measure" reading as "fine" is the exact defect class this whole change
    # exists to close. A judge that cannot prove which judge it is does not get
    # to return a verdict.
    #
    # The pinned workspace is built by scripts/gate-workspace.sh from a commit
    # of THIS repository, so it always carries scripts/merge-gate.sh; reaching
    # here means the workspace is not what it claims to be.
    refuse "unverifiable-gate-script" \
"cannot prove which gate is judging: the pinned workspace has no readable scripts/merge-gate.sh at $PIN_SHA (blob '${PIN_GATE_BLOB:-<absent>}', ${PIN_GATE_SIZE} bytes), running $GATE_SCRIPT_PATH (sha256 '${GATE_SCRIPT_SHA256:-<unmeasured>}'). An unverifiable identity is not a matching one. REMEDY: re-pin the workspace with scripts/gate-workspace.sh main so it carries the gate it is graded by, and check the host has sha256sum or shasum."
  fi
  step_end "ok" "$WS_REAL @ $PIN_SHA"

  step_begin "workspace-clean"
  # gate-workspace.sh refuses rather than --force-ing a dirty workspace because
  # "this workspace is supposed to have one writer". The gate re-checks the
  # post-condition instead of trusting it: an uncommitted edit here is exactly
  # how a gate is made to pass without the work.
  WS_DIRT=$(git -C "$GATE_WS" status --porcelain=v1 --untracked-files=all 2>/dev/null | head -5 | tr '\n' ' ')
  [ -z "$WS_DIRT" ] || refuse "dirty-workspace" "$GATE_WS has uncommitted or untracked files: $WS_DIRT"
  step_end "ok"
fi

# --- ruff baseline, computed in the ONE-WRITER ROOT ---------------------------
# THE FALSE PASS LIVED HERE. `cd "$REPO"` used to mean the shared checkout; a
# peer's uncommitted lint errors inflated BASE and masked a real regression.
# Computed once, early, so it is present on refusal receipts too and so
# --print-ruff-base and the production path can never diverge.
#
# E2 (CI port, 2026-08-05) — THE VACUOUS PASS. The old body was
#   ( cd "$1" 2>/dev/null && "$PY" -m ruff check ... 2>/dev/null | awk '/:/{n++} END{print n+0}' )
# and `awk ... END{print n+0}` prints 0 for EMPTY INPUT. With no interpreter, no
# ruff, or an unreadable root, BASE=0 and NEW=0, `0 -gt 0` is false, and the
# lint-regression check PASSED without ever measuring anything. Exit-code
# checking alone does not fix it either: `python -m ruff` with ruff ABSENT exits
# 1, which is byte-identical to "ruff ran and found findings". So there are two
# belts:
#   ruff_available()  — a positive probe that ruff is importable at all.
#   ruff_count_in()   — prints a count, or prints NOTHING when it could not
#                       produce one (cd failed, or ruff exited >1).
# An unproducible count is a refusal at the caller, never a zero.
ruff_available() { "$PY" -m ruff --version >/dev/null 2>&1; }
ruff_count_in() {  # root -> count on stdout, EMPTY if unproducible
  ( cd "$1" 2>/dev/null || exit 3
    out=$("$PY" -m ruff check --output-format concise . 2>/dev/null); rc=$?
    [ "$rc" -le 1 ] || exit "$rc"
    printf '%s\n' "$out" | awk '/:/{n++} END{print n+0}' )
}
if [ "$PINNED" = "1" ] || [ "$PRINT_RUFF_BASE" -eq 1 ]; then
  step_begin "ruff-base"
  ruff_available || refuse "ruff-unavailable" \
    "$PY cannot run ruff — a lint comparison with no linter is not a pass"
  if [ "$PINNED" = "1" ]; then RUFF_BASE_ROOT="$GATE_WS"; else RUFF_BASE_ROOT="$REPO"; fi
  RUFF_BASE=$(ruff_count_in "$RUFF_BASE_ROOT")
  case "$RUFF_BASE" in ''|*[!0-9]*) RUFF_BASE="" ;; esac
  [ -n "$RUFF_BASE" ] || refuse "ruff-baseline-unavailable" "ruff produced no count in $RUFF_BASE_ROOT"
  step_end "ok" "$RUFF_BASE in $RUFF_BASE_ROOT"
fi
if [ "$PRINT_RUFF_BASE" -eq 1 ]; then
  printf '%s\n' "$RUFF_BASE"
  # A debug probe is not a gate verdict: it only leaves a receipt when the
  # caller explicitly asked for one, so `--print-ruff-base` cannot pollute the
  # durable store with records that graded nothing.
  if [ -n "$EMIT_RECEIPT" ]; then mint_run_receipt 0 ""; else RECEIPT_MINTED=1; fi
  exit 0
fi

[ -n "$BRANCH" ] || { echo "refusing: missing-branch — usage: merge-gate.sh <branch>" >&2; exit 2; }
set -- "$BRANCH" "$RECEIPT_ARG"

step_begin "candidate-resolve"
cd "$REPO" || refuse "unreadable-repo" "cannot cd $REPO"
  # Un-pinned path guard: refuse if workspace HEAD is not main
  if [ "$PINNED" != "1" ]; then
    CURRENT_HEAD=$(git rev-parse --abbrev-ref HEAD 2>/dev/null) || CURRENT_HEAD=""
    [ "$CURRENT_HEAD" = "main" ] || refuse "unpinned-non-main-head" "workspace HEAD is $CURRENT_HEAD, not main"
  fi

git rev-parse --verify -q "$BRANCH" >/dev/null || refuse "unknown-branch" "$BRANCH"

echo "merge-gate: $BRANCH -> $(git rev-parse --abbrev-ref HEAD)"

# --- 0. SIGNED, CANDIDATE-BOUND VERIFICATION IS MANDATORY --------------------
#
# This consumes the scheduler's existing GateEvidence record. The signature,
# candidate tip, and merge-base are checked by the same module that signs and
# loads routine evidence. A markdown verdict is intentionally not a fallback:
# an unsigned statement is not evidence.
#
CANDIDATE_SHA=$(git rev-parse --verify "$BRANCH^{commit}" 2>/dev/null) || {
  refuse "unresolvable-candidate" "cannot resolve candidate tip: $BRANCH"
}
MERGE_BASE_SHA=$(git merge-base HEAD "$CANDIDATE_SHA" 2>/dev/null) || {
  refuse "unresolvable-merge-base" "cannot resolve merge-base for $BRANCH"
}
CHANGED_PATHS=$(git diff --name-only "HEAD...$CANDIDATE_SHA" 2>/dev/null) || {
  refuse "unreadable-diff" "cannot inspect candidate diff for $BRANCH"
}
# --- the SWEPT SET: what the merge actually LANDS in main's history -----------
# THE NET DIFF IS NOT WHAT MERGES. Every path-shaped refusal in this file read
# `git diff --name-only HEAD...$CANDIDATE_SHA` — the NET tree delta between main
# and the candidate tip. But this gate merges with `git merge --no-ff` (see the
# trial-merge step, and the real merge after it), so EVERY commit in
# merge-base..candidate lands in main's history permanently. A branch that adds
# `secrets/leak.env` in commit 1 and deletes it in commit 2 has a perfectly
# clean net diff — and the secret blob is in main forever, never scanned. The
# same hole swallows a tracked .venv blob and a migration edited-then-reverted:
# the second commit hides the first from the only witness the gate was calling.
#
# So the inputs SPLIT here, deliberately:
#   CHANGED_PATHS (net diff)  — TREE-shaped questions: what main LOOKS LIKE
#                               after the merge (no-change; the openapi restamp
#                               implication).
#   SWEPT_PATHS   (rev-list)  — HISTORY-shaped refusals: what main CONTAINS
#                               after the merge (secrets, tracked envs,
#                               merge-owned oracles, the shared workbook).
#
# HOW THE SET IS ENUMERATED, and why NOT `rev-list --objects` (2026-08-05
# cross-lineage review of this change). `--objects` enumerates OBJECTS, marking
# everything reachable from the merge-base UNINTERESTING — so a blob whose
# CONTENT already exists anywhere in main is never printed, and neither is the
# new path it was committed at. A branch adding `secrets/leak.env` whose bytes
# match any existing file (an EMPTY file is the universal case: this repo has
# 119 zero-length tracked files) was swept up as clean, and the counterfeit
# registered with this change passed with one byte altered. Content-addressing
# is exactly the wrong property for a question about PATHS.
#
# `rev-list | diff-tree --stdin -r -m --no-commit-id --name-only` asks the right
# question: every path touched by every commit in the range, per-parent (`-m`)
# so a path mutated during a merge commit's conflict resolution is included too.
# It emits ONE LINE PER PATH, C-quoted when it must be — where `--objects`
# truncated any path containing a newline at the first `\n` (verified: a blob at
# `sneaky<LF>configs/accounts.yaml` swept in as `sneaky`, matching no pattern).
# A quoted path is therefore REFUSED below rather than pattern-matched, because
# the anchored patterns (`^\.env$`) cannot match a quoted form.
#
# It still covers commits on side branchlets merged INSIDE the candidate range,
# which a linear `git log` walk can skip. It does NOT false-positive on a
# candidate that merged origin/main mid-branch — PROVIDED this checkout's HEAD
# is at-or-ahead of the main commit that lane merged; if the gate checkout is
# BEHIND it, the merge-base is the older fork point and main's own commits fall
# inside the range (bounded: those changes also survive to the tip, so the net
# diff refuses them anyway). Measured cost: 0.21s over this repo's full 3284-
# commit history, versus 0.06s for the primitive it replaces.
#
# An unreadable history REFUSES rather than sweeping nothing: a gate that cannot
# see what it is about to land does not get to say yes.
SWEPT_OBJECTS=$(git rev-list "$MERGE_BASE_SHA..$CANDIDATE_SHA" 2>/dev/null \
  | git diff-tree --stdin -r -m --no-commit-id --name-only 2>/dev/null) || {
  refuse "unreadable-history" \
    "cannot enumerate the paths $BRANCH would land in main's history (merge-base..candidate)"
}
SWEPT_PATHS=$(
  { printf '%s\n' "$CHANGED_PATHS"
    printf '%s\n' "$SWEPT_OBJECTS"
  } | sed '/^$/d' | sort -u
)
# A C-quoted path carries a control character (newline, tab, high-bit byte) that
# every anchored pattern below would silently fail to match. Refuse instead of
# pretending to have scanned it.
QUOTED_SWEPT=$(printf '%s\n' "$SWEPT_PATHS" | grep '^"'); rc=$?
classifier_rc "$rc" "unquotable-path"
[ -z "$QUOTED_SWEPT" ] || refuse "unquotable-path" \
  "candidate history contains path(s) with control characters, which the path guards cannot match: $(printf '%s' "$QUOTED_SWEPT" | tr '\n' ' ')"
step_end "ok" "candidate $CANDIDATE_SHA merge-base $MERGE_BASE_SHA"
RECEIPT_FILE="${2:-${MERGE_GATE_RECEIPT:-$EVIDENCE_ROOT/records/merge-gate/$CANDIDATE_SHA.json}}"
if [ ! -f "$RECEIPT_FILE" ]; then
  refuse "signed-receipt-missing" \
    "no signed receipt at ${RECEIPT_FILE#"$REPO"/} — mint one first via the canonical wrapper: \"$PY\" \"$REPO/scripts/mint-merge-candidate.py\" --candidate-sha $CANDIDATE_SHA --merge-base-sha $MERGE_BASE_SHA --evidence-root \"$EVIDENCE_ROOT\"" 3
else
  RECEIPT_CHECK=$(
    PYTHONPATH="$REPO" "$PY" -m omniagentos.scheduler.gate_evidence verify-candidate \
      --receipt "$RECEIPT_FILE" \
      --evidence-root "$EVIDENCE_ROOT" \
      --candidate-sha "$CANDIDATE_SHA" \
      --merge-base-sha "$MERGE_BASE_SHA" 2>&1
  )
  if [ "$?" -ne 0 ]; then
    fail "signed-receipt" "$RECEIPT_CHECK"
  else
    pass "signed-receipt" "$RECEIPT_CHECK"
  fi
fi

# Test-only TOCTOU harness: after $CANDIDATE_SHA is frozen and the receipt is
# checked, optionally retarget the branch name to a different tip. Production
# post-checks must keep using $CANDIDATE_SHA; tip-stability must then refuse.
if [ -n "${MERGE_GATE_TEST_RETARGET_SHA:-}" ] && [ -n "${MERGE_GATE_TEST_RETARGET_REF:-}" ]; then
  git update-ref "refs/heads/${MERGE_GATE_TEST_RETARGET_REF}" "${MERGE_GATE_TEST_RETARGET_SHA}" \
    || refuse "test-retarget-failed" "could not retarget ${MERGE_GATE_TEST_RETARGET_REF}"
fi

# ============================================================================
# HOISTED CHEAP REFUSALS — complete verdicts that used to wait for the ladder
# ============================================================================
# Each of these is a whole answer reachable from the diff or from a sub-second
# probe. Under MERGE_GATE_PINNED=1 they run HERE and refuse immediately; the
# un-armed path leaves them exactly where they were, so a revert is total.
SECRET_RE='configs/accounts\.yaml|vault/sources/.*\.enc|secrets/sessions-token|^\.env$|^\.?secrets?/.*\.env$'
HOISTED=0
REACH_DONE=0
if [ "$PINNED" = "1" ]; then
  HOISTED=1

  step_begin "oracle-path"
  # TREE-shaped: reads the NET DIFF, matching the unhoisted copy below (the
  # hoist must never grade differently from the check it front-runs). A lane
  # that regenerates an oracle and restores it lands nothing anyone reads.
  ORACLE_PATHS=$(printf '%s\n' "$CHANGED_PATHS" | grep -Ex \
    'ARCHI\.md|ARCHI\.json|docs/architecture/system-map\.(md|mmd)'); rc=$?
  classifier_rc "$rc" "oracle-path"
  [ -z "$ORACLE_PATHS" ] || refuse "oracle-path" \
    "candidate touches merge-owned oracle(s): $(echo "$ORACLE_PATHS" | tr '\n' ' '); regenerate on main"
  step_end "ok"

  step_begin "secrets"
  # The whole reason the swept set exists: an add-then-delete leaves NO net diff
  # and a permanent blob. Scan what MERGES, not what remains.
  LEAKED=$(printf '%s\n' "$SWEPT_PATHS" | grep -E "$SECRET_RE"); rc=$?
  classifier_rc "$rc" "secrets"
  [ -z "$LEAKED" ] || refuse "secrets" "branch touches $(echo "$LEAKED" | tr '\n' ' ')"
  step_end "ok"

  step_begin "openapi-drift"
  # contracts/openapi.json is DERIVED from the FastAPI app. A candidate that
  # edits route sources without restamping it lands a contract that describes
  # code that no longer exists — and every downstream parity digest then
  # certifies the stale artifact. Refusing on the DIFF costs milliseconds, so
  # it stays the FAST PATH below and runs first. It is a floor, not a proof:
  # drift originating outside omniagentos/api/ is still caught downstream by
  # tests/api/test_openapi_contract.py.
  #
  # 2026-08-04: that floor was UNSATISFIABLE for a real, schema-neutral edit
  # (fix/migrate-startup-gate-0804 wired a startup-migration check into
  # api/main.py) — the regenerated contract came back BYTE-IDENTICAL, so git
  # has no diff entry to show for contracts/openapi.json, and the pure
  # path-implication check refused a candidate that changed nothing
  # observable. An API-touched-but-contract-untouched candidate now gets a
  # second, EXPENSIVE chance before being refused: pay seconds (one throwaway
  # worktree + merge + a bounded interpreter subprocess, via
  # scripts/openapi_drift_check.py) to actually regenerate the schema from
  # the candidate's own MERGED tree — same doctrine as the ladder ("on the
  # merge commit, not the lane") — and byte-compare it against the merged
  # tree's committed contracts/openapi.json. That is still cheap relative to
  # the ~12-minute ladder this step runs ahead of, and it is paid ONLY on
  # this rare combination; every other candidate keeps the millisecond path.
  # Anything that stops verification from completing — merge conflict, no
  # interpreter, an import shadow, the bounded timeout inside the checker —
  # REFUSES with the ORIGINAL message: an unverifiable regen is not a pass.
  #
  # Both reads below stay on the NET diff on purpose — this is the tree-shaped
  # half of the split described at the SWEPT SET note above. The question here
  # is "does the merged TREE describe the merged code", and a restamp only
  # counts if it SURVIVES to the tip. Reading CONTRACT_TOUCHED off the swept set
  # would let a candidate touch contracts/openapi.json in one commit and revert
  # it in the next to satisfy the requirement while landing a stale contract:
  # sweeping here makes the check WEAKER, not stronger.
  API_TOUCHED=$(printf '%s\n' "$CHANGED_PATHS" | grep -E '^omniagentos/api/.*\.py$'); rc=$?
  classifier_rc "$rc" "openapi-drift (api-touched)"
  CONTRACT_TOUCHED=$(printf '%s\n' "$CHANGED_PATHS" | grep -Fx 'contracts/openapi.json'); rc=$?
  classifier_rc "$rc" "openapi-drift (contract-touched)"
  if [ -n "$API_TOUCHED" ] && [ -z "$CONTRACT_TOUCHED" ]; then
    OPENAPI_MSG="candidate edits $(echo "$API_TOUCHED" | tr '\n' ' ')without regenerating contracts/openapi.json (scripts/refresh-contracts.sh)"
    OPENAPI_VERIFIED=0
    if [ -z "$PY" ]; then
      refuse "openapi-drift" "$OPENAPI_MSG — could not verify: no interpreter"
    fi
    OPENAPI_TREE="$REPO/var/swarm/gate-openapi-$$"
    mkdir -p "$REPO/var/swarm" 2>/dev/null
    # Review 2026-08-04 (F4): prune stale administrative entries FIRST — a
    # PID-recycling leftover (a prior run killed before its own cleanup) can
    # otherwise block `add` at this exact path with a confusing error. On a
    # real failure, surface git's OWN reason instead of discarding it with
    # 2>/dev/null: an operator refused with no detail cannot tell a permission
    # problem from a stale lock from disk pressure.
    git worktree prune 2>/dev/null || true
    OPENAPI_ADD_ERR=$(git worktree add -q --detach "$OPENAPI_TREE" HEAD 2>&1 >/dev/null) || {
      OPENAPI_TREE=""
      refuse "openapi-drift" \
        "$OPENAPI_MSG — could not verify: no worktree to stage the merged tree ($(printf '%s' "$OPENAPI_ADD_ERR" | tr '\n' ' ' | cut -c1-200))"
    }
    # SECOND trial merge in this script — the sibling of the `trial-merge` step
    # below, and it had the identical defect the comment six lines up already
    # fixed for `worktree add`: `2>/dev/null` plus one message for every
    # non-zero exit, so `git merge` exiting 128 because the runner has no
    # committer identity was reported as "merge conflict while staging the
    # tree". Same discrimination as the trial-merge step: >=128 means git never
    # judged the candidate, and the refusal must say so.
    OPENAPI_MERGE_ERR=$(git -C "$OPENAPI_TREE" merge --no-ff --no-commit -q "$CANDIDATE_SHA" 2>&1 >/dev/null)
    OPENAPI_MERGE_RC=$?
    OPENAPI_MERGE_ERR=$(printf '%s' "$OPENAPI_MERGE_ERR" | tr '\n' ' ' | cut -c1-200)
    if [ "$OPENAPI_MERGE_RC" -ge 128 ]; then
      git -C "$OPENAPI_TREE" merge --abort 2>/dev/null || true
      refuse "openapi-drift" \
        "$OPENAPI_MSG — could not verify: git merge exited $OPENAPI_MERGE_RC staging the tree (instrument failure, not a conflict): ${OPENAPI_MERGE_ERR:-<git wrote nothing to stderr>}"
    fi
    # `| grep -q .` as a CONDITION carries the same three-valued bug as the
    # captures above and hides it better: a grep that exits >=2 is simply falsy,
    # which here is indistinguishable from "no unmerged paths" and SKIPS a
    # could-not-verify refusal. Capture, test the status, then test emptiness.
    OPENAPI_UNMERGED=$(git -C "$OPENAPI_TREE" diff --name-only --diff-filter=U 2>/dev/null); rc=$?
    classifier_rc "$rc" "openapi-drift (unmerged-paths probe)"
    if [ "$OPENAPI_MERGE_RC" -ne 0 ] || [ -n "$OPENAPI_UNMERGED" ]; then
      git -C "$OPENAPI_TREE" merge --abort 2>/dev/null || true
      refuse "openapi-drift" "$OPENAPI_MSG — could not verify: merge conflict while staging the tree to check"
    fi
    # Review 2026-08-04 (F1): this regen subprocess IMPORTS the CANDIDATE's own
    # omniagentos/api/main.py — untrusted merged-tree content, executing inside
    # the gate. Passing the ambient env through here is exactly the class this
    # whole fix exists to close, just via a new vector (candidate code, not a
    # test worker). Scrub OMNIAGENTOS_GATE_WORKSPACE and point DB/VAR_DIR/LEDGER
    # at scratch paths under $OPENAPI_TREE itself, same shape as the workers
    # above. openapi_drift_check.py independently re-scrubs the same trio for
    # its OWN subprocess (defense in depth: neither layer trusts the other).
    # The egress trio (2026-08-10) rides along for the reason given at the
    # GATE_CHILD_SCRUB_KEYS note above run_suite: every gate child gets the same
    # key set, or the next one added is the one that leaks.
    OPENAPI_SCRATCH_VAR="$OPENAPI_TREE/var/openapi-drift-check"
    OPENAPI_OUT=$(env -u OMNIAGENTOS_GATE_WORKSPACE -u MERGE_GATE_PY -u MERGE_GATE_PINNED \
      -u OMNI_NTFY_URL -u OPS_ALERT_SLACK_WEBHOOK_URL -u SLACK_WEBHOOK_URL \
      OMNIAGENTOS_DB="$OPENAPI_SCRATCH_VAR/state.sqlite3" \
      OMNIAGENTOS_VAR_DIR="$OPENAPI_SCRATCH_VAR" \
      OMNIAGENTOS_LEDGER_DIR="$OPENAPI_SCRATCH_VAR/ledger" \
      PYTHONPATH="$OPENAPI_TREE" "$PY" "$REPO/scripts/openapi_drift_check.py" "$OPENAPI_TREE" 2>&1)
    OPENAPI_RC=$?
    if [ "$OPENAPI_RC" -ne 0 ]; then
      refuse "openapi-drift" \
        "$OPENAPI_MSG — $(printf '%s' "$OPENAPI_OUT" | tail -1 | cut -c1-300)"
    fi
    OPENAPI_VERIFIED=1
  fi
  # THREE OUTCOMES, NOT TWO. This recorded `step_end "ok"` with an EMPTY detail
  # for the case where API_TOUCHED was empty — so a candidate whose schema was
  # NEVER EXAMINED was byte-identical, in the only durable carrier, to one that
  # was examined and found clean. That is the same favourable-absence class as
  # the silent suite guards below, one rung worse: absence rendered as a
  # positive result rather than as nothing at all. `n/a` is a real answer and
  # `ok` is not, so the two must not share a value.
  if [ "${OPENAPI_VERIFIED:-0}" -eq 1 ]; then
    step_end "ok" "api edit is schema-neutral, regen verified identical"
  elif [ -n "$API_TOUCHED" ]; then
    step_end "ok" "candidate restamped contracts/openapi.json alongside its api edit"
  else
    step_end "n/a" "candidate touches no omniagentos/api/*.py — the schema was not examined"
  fi

  step_begin "reachability"
  # Sub-second probe that used to run DEAD LAST, behind ~12 minutes of suites.
  # Base is the resolved merge-base SHA, not the moving `main` ref: an ambient
  # ref is exactly the input this mode exists to remove.
  REACH_OUT=""
  if REACH_OUT=$("$PY" "$REPO/scripts/reachability-gate.py" "$CANDIDATE_SHA" "$MERGE_BASE_SHA" 2>&1); then
    step_end "ok" "every new public symbol has a production caller"
    REACH_DONE=1
  else
    REACH_RC=$?
    if [ "$REACH_RC" -eq 2 ]; then
      refuse "reachability-probe-unusable" "gate could not run — refusing rather than assuming reachable"
    fi
    REACH_DETAIL=$(printf '%s' "$REACH_OUT" | grep -E '^[[:space:]]+omniagentos/' | tr '\n' ';' | cut -c1-200)
    # Name the exemption trap in the refusal itself when that is what this is.
    # The marker leads the detail so the run receipt carries it too — the
    # 28-cycle count that motivated this fix was only recoverable because the
    # receipts recorded the refusal, and an unclassified receipt cannot show
    # whether the message is working.
    if REACH_TRAP=$(reach_exempt_trap "$CANDIDATE_SHA" "$REACH_OUT"); then
      reach_exempt_explain "${REACH_TRAP#*|}" "${REACH_TRAP%%|*}"
      REACH_DETAIL="exempt-on-branch-not-in-gate-checkout(${REACH_TRAP%%|*}) [${REACH_TRAP#*|}] — land the exemption on main first; $REACH_DETAIL"
    fi
    refuse "reachability" "$REACH_DETAIL"
  fi
else
  REACH_DONE=0
fi

# --- 8. empty candidate / NO_CHANGE ------------------------------------------
# THE LAST READER OF THE NET DIFF, and it must stay that way. "Already up to
# date" is a claim about the resulting TREE: a branch that only adds a file and
# deletes it again changes nothing on main, so it still has to refuse here —
# even though the swept set below has a great deal to say about it.
[ -z "$CHANGED_PATHS" ] && fail "no-change" \
  "NO_CHANGE: candidate has no tree changes against current HEAD" || pass "no-change"

# --- 9. merge-owned architecture oracles ------------------------------------
# TREE-shaped, so it stays on the NET DIFF (2026-08-05 review). A lane that
# regenerates ARCHI.md in one commit and restores it byte-identically in the
# next harms nothing: nothing reads an oracle out of history, and the merged
# tree is main's own copy. Sweeping it bought zero safety and cost real
# refusals — measured against all 196 live origin refs, sweeping this guard and
# the workbook one below newly refused THREE honest self-correcting branches
# (integration/v2-review, v3-review, v4-review) while catching none. Worse, the
# remedy text is unsatisfiable for a history hit: "regenerate on main" cannot
# remove a blob from the lane's history, and the operator's first move
# (`git diff main...HEAD`) shows no oracle file at all, so the gate reads as
# broken.
ORACLE_PATHS=$(printf '%s\n' "$CHANGED_PATHS" | grep -Ex \
  'ARCHI\.md|ARCHI\.json|docs/architecture/system-map\.(md|mmd)'); rc=$?
classifier_rc "$rc" "oracle-path"
[ -n "$ORACLE_PATHS" ] && fail "oracle-path" \
  "candidate touches merge-owned oracle(s): $(echo "$ORACLE_PATHS" | tr '\n' ' '); regenerate on main" || pass "oracle-path"

# --- 11. shared repo-root workbook ------------------------------------------
# TREE-shaped for the same reason as the oracle guard above: a reverted
# WORKBOOK.md edit in a lane's history collides with nobody. Both are placed
# BEFORE the rebind deliberately.
ROOT_WORKBOOK=$(printf '%s\n' "$CHANGED_PATHS" | grep -Fx 'WORKBOOK.md'); rc=$?
classifier_rc "$rc" "root-workbook"
[ -n "$ROOT_WORKBOOK" ] && fail "root-workbook" \
  "candidate touches shared root WORKBOOK.md; use var/swarm/<run>/<task>/WORKBOOK.md" || pass "root-workbook"

# --- 12. anti-weakening: a candidate may not edit its own bound test ---------
# A fix that edits the test it is bound to is not a fix. If the test is wrong,
# that is a NEW finding with its own chain, never a quiet edit inside a fix.
# TREE-shaped on purpose: a lane that edits the test and restores it changes
# nothing anyone reads (same rule as oracle-path above), so this sits BEFORE the
# swept-set rebind below with the other two tree-shaped guards.
# Iterates BOUND_TESTS: every member of a train is graded, not just the last.
#
# This is a CANDIDATE DEFECT, so it lands in FAILURES (exit 1) and its slug is
# deliberately NOT in GATE_INSTRUMENT_SLUGS: the admission rule up there is that
# a slug may be asserted only if it is decided BEFORE, or INDEPENDENTLY OF,
# reading candidate content — and "the bound test file was edited" is read
# straight out of the candidate's diff.
if [ -n "$BOUND_TESTS" ]; then
  # THE COMPARISON SPACE, and every binding has to live in it before anything is
  # graded. `$CHANGED_PATHS` holds CANONICAL repo-relative paths, so `grep -Fx`
  # against `./tests/x.py`, `Tests/X.py`, a symlinked spelling or a bare
  # directory silently matches NOTHING and the untouched check passes
  # VACUOUSLY — a binding that cannot be compared is worse than no binding,
  # because it prints `ok`. The listing is taken from the MERGE BASE (main's own
  # history), never from the candidate: "the caller named a path that is not in
  # the tree" must not be decidable by the candidate, or a lane could turn its
  # own binding into a usage error by deleting the file.
  BOUND_BASE_PATHS=$(git -C "$REPO" ls-tree -r --name-only "$MERGE_BASE_SHA" 2>/dev/null); rc=$?
  classifier_rc "$rc" "bound-test-path"
  while IFS= read -r BOUND_TEST; do
    [ -n "$BOUND_TEST" ] || continue
    # A node id is <file>::[<class>::]<test>[<params>]. Directory-form ("run
    # everything under here") is REFUSED, not accepted: this gate exists to
    # prove that ONE named test executed, and a directory can satisfy that with
    # any test at all — including a passing one that has nothing to do with the
    # finding.
    case "$BOUND_TEST" in
      *::*) ;;
      *) refuse "bad-bound-test" \
           "--bound-test '$BOUND_TEST' names no test: a binding is <file>::<test>, never a bare file or directory" ;;
    esac
    BOUND_FILE="${BOUND_TEST%%::*}"
    BOUND_KNOWN=$(printf '%s\n' "$BOUND_BASE_PATHS" | grep -Fx -- "$BOUND_FILE"); rc=$?
    classifier_rc "$rc" "bound-test-path"
    [ -n "$BOUND_KNOWN" ] || refuse "bad-bound-test" \
      "--bound-test '$BOUND_TEST' names '$BOUND_FILE', which is not a path in the merge base $MERGE_BASE_SHA; pass the canonical repo-relative path git records (no './', no absolute path, exact case)"
    # EVERY conftest.py on the bound test's import path, root-first. A candidate
    # that never touches the test file can still add
    # `tests/<dir>/conftest.py` with a `pytest_collection_modifyitems` that skips
    # the bound node, or a reporter hook that prints a passing summary — the
    # node then "executes green" while asserting nothing. The conftest chain is
    # part of the test, so editing it is editing the test.
    BOUND_CONFTESTS="conftest.py"
    _bt_prefix=""
    _bt_rest="$BOUND_FILE"
    while [ "$_bt_rest" != "${_bt_rest#*/}" ]; do
      _bt_prefix="$_bt_prefix${_bt_rest%%/*}/"
      _bt_rest="${_bt_rest#*/}"
      BOUND_CONFTESTS="$BOUND_CONFTESTS
${_bt_prefix}conftest.py"
    done
    # --- F6: pytest_plugins-DECLARED modules are part of the test too ----------
    # The conftest walk above stops at files literally named conftest.py, but a
    # conftest (or the bound test's own module) can declare
    # `pytest_plugins = ["a.b.c", ...]`, and pytest then loads those ORDINARY
    # modules as plugins. They register the SAME hooks a conftest can — a
    # `pytest_runtest_makereport` in such a module rewrites the node's report
    # failed->passed — yet they are NOT named conftest.py and are NOT on the
    # walked chain. One ships today: tests/conftest.py declares
    # omniagentos/testpolicy/pytest_plugin.py, which defines makereport. A
    # candidate could leave the test file and every conftest byte-identical, edit
    # ONLY that plugin module to flip its bound node green, and sail past the
    # untouched check with the bug unfixed. So the plugins DECLARED on this chain
    # are part of the test surface, resolved and guarded exactly like conftests.
    #
    # Sources scanned = the conftest.py files on the chain PLUS the bound test's
    # own module, but ONLY those present in the merge base; each is read AT THE
    # MERGE BASE (never from the candidate — same rule as BOUND_BASE_PATHS), and
    # pytest_plugins is extracted with python3+ast, NOT shell string-matching:
    # over- or under-firing a security check is a defect, and a real parser is the
    # only way a `pytest_plugins` split across lines, in a tuple, or a bare string
    # all resolve the same. Dotted names resolve to a/b/c.py AND a/b/c/__init__.py,
    # and only the candidates that actually EXIST in the merge base are kept.
    #
    # RESIDUAL: this closes plugins DECLARED via `pytest_plugins`. `-p`-injection
    # through a pytest config file (pytest.ini / pyproject.toml / tox.ini /
    # setup.cfg addopts) and entry-point plugins are a related vector left for a
    # follow-up — a blanket config-file ban would over-fire on benign pyproject
    # edits, so it is deliberately NOT attempted here.
    #
    # Command substitution (NOT an appended-inside-a-pipe subshell, which loses
    # the value); the trailing `sort -u`'s rc is what the `$( )` returns, so the
    # no-match rc 1 of the inner greps never propagates. `set -uo pipefail` has no
    # `-e`, so none of this can abort the gate.
    BOUND_PLUGIN_FILES=$(
      {
        printf '%s\n' "$BOUND_CONFTESTS"
        printf '%s\n' "$BOUND_FILE"
      } | while IFS= read -r _bt_src; do
        [ -n "$_bt_src" ] || continue
        # Only sources actually in the merge base — a conftest slot the walk names
        # but the tree never had contributes nothing.
        printf '%s\n' "$BOUND_BASE_PATHS" | grep -Fxq -- "$_bt_src" || continue
        git -C "$REPO" show "$MERGE_BASE_SHA:$_bt_src" 2>/dev/null | python3 -c 'import ast,sys
try: t=ast.parse(sys.stdin.read())
except Exception: sys.exit(0)
for n in ast.walk(t):
    if isinstance(n,ast.Assign) and any(getattr(x,"id",None)=="pytest_plugins" for x in n.targets):
        v=n.value; items=getattr(v,"elts",[v])
        for it in items:
            if isinstance(it,ast.Constant) and isinstance(it.value,str): print(it.value)'
      done | while IFS= read -r _bt_mod; do
        [ -n "$_bt_mod" ] || continue
        _bt_rel=$(printf '%s' "$_bt_mod" | tr '.' '/')
        for _bt_cand in "${_bt_rel}.py" "${_bt_rel}/__init__.py"; do
          printf '%s\n' "$BOUND_BASE_PATHS" | grep -Fxq -- "$_bt_cand" && printf '%s\n' "$_bt_cand"
        done
      done | sort -u
    )
    # One `grep -Fx` with a NEWLINE-DELIMITED pattern argument is N exact-line
    # patterns, so this stays the same shape (and the same rc contract) as the
    # single-path check above.
    BOUND_TOUCHED=$(printf '%s\n' "$CHANGED_PATHS" | grep -Fx -- "$BOUND_FILE"); rc=$?
    classifier_rc "$rc" "bound-test-untouched"
    BOUND_CONFTEST_HIT=$(printf '%s\n' "$CHANGED_PATHS" | grep -Fx -- "$BOUND_CONFTESTS"); rc=$?
    classifier_rc "$rc" "bound-test-untouched"
    # CRITICAL: an empty -F pattern matches EVERY line, so this grep runs ONLY
    # when there is at least one declared-plugin path — otherwise it would match
    # the whole diff and refuse every candidate. Same printf|grep -Fx / rc /
    # classifier_rc shape as BOUND_CONFTEST_HIT above, so it carries the same
    # "grep could not RUN is a gate fault, not a verdict" contract.
    BOUND_PLUGIN_HIT=""
    if [ -n "$BOUND_PLUGIN_FILES" ]; then
      BOUND_PLUGIN_HIT=$(printf '%s\n' "$CHANGED_PATHS" | grep -Fx -- "$BOUND_PLUGIN_FILES"); rc=$?
      classifier_rc "$rc" "bound-test-untouched"
    fi
    if [ -n "$BOUND_TOUCHED" ]; then
      bound_result_record "weakened"
      fail "bound-test-untouched" \
        "candidate edits its own bound test file $BOUND_FILE — a wrong test is a NEW finding, not a quiet edit inside a fix"
    elif [ -n "$BOUND_CONFTEST_HIT" ]; then
      bound_result_record "weakened"
      fail "bound-test-untouched" \
        "candidate touches $(echo "$BOUND_CONFTEST_HIT" | tr '\n' ' ')— a conftest.py on the import path of its own bound test $BOUND_FILE can skip the node or fake its report; that is editing the test by another route"
    elif [ -n "$BOUND_PLUGIN_HIT" ]; then
      bound_result_record "weakened"
      fail "bound-test-untouched" \
        "candidate touches $(echo "$BOUND_PLUGIN_HIT" | tr '\n' ' ')— a pytest plugin declared via pytest_plugins on the load path of its own bound test $BOUND_FILE registers the same hooks a conftest can (a pytest_runtest_makereport there rewrites the node's report failed->passed); editing it is editing the test by another route"
    else
      pass "bound-test-untouched" "$BOUND_FILE, its conftest chain and declared plugins unchanged by this candidate"
    fi
  done <<EOF
$BOUND_TESTS
EOF
fi

# From here down, "changed" means "LANDS IN MAIN'S HISTORY", not "differs from
# main's tree" — see the SWEPT SET note at the candidate-resolve step for why
# `git merge --no-ff` makes those two different questions. The rebind, and its
# placement, are deliberate: the guard bodies below stay BYTE-IDENTICAL to the
# ones that read the net diff and now scan the swept set without a character
# changing. That is the intent, not an accident — anyone auditing one of those
# lines in isolation should come back HERE to learn which set it is reading.
# What lands BELOW this line is what a merge makes PERMANENT (secrets, tracked
# dependency trees); what stays above it is what the merged TREE looks like.
CHANGED_PATHS="$SWEPT_PATHS"

# --- 10. tracked interpreter/dependency environments ------------------------
# \.venv[^/]* covers .venv AND sibling envs like the A4 hermetic lane's
# .venv-hermetic — a tracked copy of either replaces local dependency trees.
TRACKED_ENV=$(printf '%s\n' "$CHANGED_PATHS" | grep -E '(^|/)(\.venv[^/]*|node_modules)(/|$)'); rc=$?
classifier_rc "$rc" "tracked-env"
[ -n "$TRACKED_ENV" ] && fail "tracked-env" \
  "candidate tracks environment path(s): $(echo "$TRACKED_ENV" | tr '\n' ' ')" || pass "tracked-env"

# --- 10b. hermetic-lane venv shape (A4) --------------------------------------
# .venv-hermetic redirected to .venv would make `make test-hermetic` install the
# always-on testfarm pytest plugin into the PRODUCTION venv, silently changing
# every pytest lane. The lane's own guard refuses at install time; this check
# refuses at merge time so a poisoned checkout cannot certify a candidate.
if [ -L "$REPO/.venv-hermetic" ]; then
  fail "hermetic-venv-shape" \
    ".venv-hermetic is a symlink -> $(readlink "$REPO/.venv-hermetic"); must be a real directory or absent (see scripts/hermetic-venv-guard.sh)"
else
  pass "hermetic-venv-shape"
fi

# --- 4. secrets, checked on the SWEPT SET, before anything is merged -----------
# Pin every post-auth check to the already-resolved candidate tip. Re-reading
# $BRANCH here would race with a retarget after receipt verification. The swept
# set is built from $MERGE_BASE_SHA..$CANDIDATE_SHA — both already resolved and
# receipt-verified — so it carries that same pin; it is named explicitly here,
# rather than through the rebound $CHANGED_PATHS, so this refusal reads as what
# it is. This line used to re-run its own three-dot net diff, which IS the hole:
# a branch that added a secret and deleted it before its tip was invisible to it.
LEAKED=$(printf '%s\n' "$SWEPT_PATHS" | grep -E "$SECRET_RE"); rc=$?
classifier_rc "$rc" "secrets"
[ -n "$LEAKED" ] && fail "secrets" "branch touches $(echo "$LEAKED" | tr '\n' ' ')" || pass "secrets"

# --- 7. committed symlinks ----------------------------------------------------
# Use the authenticated tip SHA, never the live branch name (TOCTOU after verify).
# The tree read is separated from the filter deliberately. Under `pipefail` the
# status of a pipeline is its RIGHTMOST NON-ZERO stage, so `git ls-tree` failing
# (128) ahead of a `grep -v` that kept nothing (1) reports 1 — indistinguishable
# from "this candidate has no symlinks" — and the unreadable tree disappears.
# This is the one classifier here whose left leg can genuinely fail, so it is the
# one where the pipeline's own rc is not a sufficient answer.
SYMS_TREE=$(git ls-tree -r "$CANDIDATE_SHA"); rc=$?
[ "$rc" -eq 0 ] || refuse "classifier-unusable" \
  "the no-new-symlinks classifier could not read the candidate tree (git ls-tree exited $rc)"
# awk is separated from grep for the SECOND half of the same reason. Under
# pipefail the status is the rightmost NON-ZERO stage, so an awk that died (127)
# ahead of a `grep -v` that kept nothing (1) reports 1 — a normal "no match",
# below the >=2 threshold, and the dead awk is invisible. Measured: 127 upstream
# of a no-match grep yields rc=1. One stage per capture is the only shape where
# each stage's failure can still be seen.
SYMS_MODES=$(printf '%s\n' "$SYMS_TREE" | awk '$1=="120000"{print $4}'); rc=$?
classifier_rc "$rc" "no-new-symlinks (mode filter)"
SYMS=$(printf '%s\n' "$SYMS_MODES" | grep -vE '^\.mcp\.json$'); rc=$?
classifier_rc "$rc" "no-new-symlinks"
[ -n "$SYMS" ] && fail "no-new-symlinks" "$(echo "$SYMS" | tr '\n' ' ')" || pass "no-new-symlinks"

# --- 5. migrations append-only ------------------------------------------------
# TWO questions, unioned, because neither one answers the other.
#
# The NET check (unchanged, and still first) catches a migration whose content
# differs at the candidate tip from main's. The HISTORY check catches the shape
# the net diff structurally cannot see: a migration modified in commit 1 and
# restored byte-identical in commit 2 still lands the mutated blob in main under
# --no-ff, and "I put it back afterwards" is not append-only.
#
# The history walk is then narrowed to migrations that EXIST AT THE MERGE-BASE,
# and that filter is load-bearing rather than tidiness: iterating on your OWN
# not-yet-on-main migration — add 0119 in commit 1, fix its SQL in commit 2 — is
# legal and routine, and an unfiltered walk reports that as `M` and refuses a
# lane that did nothing wrong. Only a file main ALREADY HAS can be modified
# non-append-only.
#
# `grep -Fx "$MIG_AT_BASE"` uses the multi-line variable as a fixed pattern LIST,
# which behaves identically on BSD and GNU grep (and keeps this file free of the
# process substitution it has never used). An EMPTY pattern string matches EVERY
# line, so "no migrations at the merge-base" is answered explicitly here instead
# of silently inverting the filter into a rubber stamp.
#
# SCOPE (2026-08-13): the doctrine this check enforces — AGENTS.md "Append-only
# Migrations" — governs the MIGRATION FILES (*.sql). The directory also holds
# README.md, a pointer doc that main itself edits routinely (migration-head
# stamps); a lane commit touching it, later converged byte-identical by main,
# refused PR #380 at a file that is not a migration. Both walks are therefore
# pathspec-scoped to '*.sql'. `git ls-tree` does not support pathspec magic, so
# the merge-base listing stays directory-wide — harmless, it is only the
# fixed-pattern intersection list for the already-scoped history walk.
#
# HARDENING (2026-08-13 cross-lineage review, gpt-5.6-sol):
#  - Pathspec-magic env vars are neutralized: GIT_LITERAL_PATHSPECS=1 would
#    silently read ':(glob)…*.sql' as a literal path and both walks would
#    return empty at rc 0 — a signed PASS for a real migration edit. Unset
#    here, immediately before the only pathspec-magic consumers in this file.
#  - Statuses M, D and T all refuse (deleting or re-typing an applied
#    migration is not append-only either); `--no-renames` splits renames into
#    D+A so a rename cannot slip through as R100. Tab-delimited awk keeps
#    filenames containing spaces intact for the -Fx intersection below.
#  - The history walk uses `-m` (per-parent diffs) instead of `--no-merges`:
#    an edit/restore living only inside merge commits still lands the mutated
#    blob in main under --no-ff, and a walk that skips merges cannot see it.
unset GIT_LITERAL_PATHSPECS GIT_GLOB_PATHSPECS GIT_NOGLOB_PATHSPECS GIT_ICASE_PATHSPECS
MIG_EDIT=$(git diff --no-renames --name-status "HEAD...$CANDIDATE_SHA" -- ':(glob)omniagentos/db/migrations/*.sql' 2>/dev/null \
           | awk -F'\t' '$1=="M"||$1=="D"||$1=="T"{print $2}'); rc=$?
classifier_rc "$rc" "migrations-append-only (net diff)"
MIG_AT_BASE=$(git ls-tree -r --name-only "$MERGE_BASE_SHA" -- omniagentos/db/migrations/ 2>/dev/null); rc=$?
classifier_rc "$rc" "migrations-append-only (merge-base listing)"
MIG_HIST=$(git log -m --no-renames --format= --name-status "$MERGE_BASE_SHA..$CANDIDATE_SHA" \
           -- ':(glob)omniagentos/db/migrations/*.sql' 2>/dev/null | awk -F'\t' '$1=="M"||$1=="D"||$1=="T"{print $2}'); rc=$?
classifier_rc "$rc" "migrations-append-only (history walk)"
if [ -n "$MIG_HIST" ] && [ -n "$MIG_AT_BASE" ]; then
  MIG_HIST=$(printf '%s\n' "$MIG_HIST" | grep -Fx "$MIG_AT_BASE"); rc=$?
  classifier_rc "$rc" "migrations-append-only (merge-base filter)"
else
  MIG_HIST=""
fi
MIG_EDIT=$(printf '%s\n%s\n' "$MIG_EDIT" "$MIG_HIST" | sed '/^$/d' | sort -u)
[ -n "$MIG_EDIT" ] && fail "migrations-append-only" "modified $(echo "$MIG_EDIT" | tr '\n' ' ')" \
                   || pass "migrations-append-only"

# ============================================================================
# --preflight-only — STOP HERE. Everything above is diff-shaped or a sub-second
# probe; everything below builds a trial merge and runs ~20 minutes of suites.
# ============================================================================
# This exists so a CI fast lane can return the cheap complete verdicts in
# minutes instead of making a developer wait out the full gate to learn their
# branch touches a secret. It is NOT a lighter gate: it runs the identical
# checks in the identical order and refuses on the identical conditions. What
# it does is stop early and SAY SO — the receipt it mints carries
# `"mode": "preflight"` inside the signature, so a preflight PASS can never be
# read, or replayed, as a full-gate PASS.
if [ "$PREFLIGHT_ONLY" -eq 1 ]; then
  if [ "${#FAILURES[@]}" -eq 0 ]; then
    echo "MERGE GATE PREFLIGHT: PASS — $BRANCH cleared every cheap check (suites NOT run)"
    mint_run_receipt 0 ""
    exit 0
  fi
  printf '\nMERGE GATE PREFLIGHT: REFUSED (%d)\n' "${#FAILURES[@]}"
  printf '  - %s\n' "${FAILURES[@]}"
  mint_run_receipt 1 "$(printf '%s; ' "${FAILURES[@]}" | cut -c1-800)"
  exit 1
fi

# --- build the merge commit in a scratch worktree; never mutate main ----------
step_begin "trial-merge"
SCRATCH="$REPO/var/swarm/gate-$$"
mkdir -p "$REPO/var/swarm" 2>/dev/null
git worktree add -q --detach "$SCRATCH" HEAD 2>/dev/null \
  || { SCRATCH=""; refuse "no-gate-worktree" "cannot create gate worktree under $REPO/var/swarm"; }
# The pinned workspace has no .venv of its own; link the interpreter tree that
# was actually resolved, never a path that may not exist.
[ -n "$VENV_ROOT" ] && ln -sfn "$VENV_ROOT" "$SCRATCH/.venv" 2>/dev/null
[ -d "$REPO/dashboard/node_modules" ] && ln -sfn "$REPO/dashboard/node_modules" "$SCRATCH/dashboard/node_modules" 2>/dev/null

# Trial-merge the authenticated candidate tip SHA (not the live branch name).
# Use --no-commit so the pre-merge-commit migration-authority hook does not
# refuse a *conflict check* merely because the candidate adds more than one
# migration (those land as separate exclusive allocations when actually
# committing to main). Conflict detection is path-unmerged status only.
#
# EXIT CODES ARE NOT INTERCHANGEABLE. This block used to be
# `if ! git ... merge ... 2>/dev/null; then fail "merge-clean" "conflicts against main"`,
# which mapped EVERY non-zero exit onto one accusation and threw the reason
# away. `git merge` exits 1 when it judged the candidate and found conflicts,
# but >=128 when it could not judge the candidate AT ALL — no committer
# identity (a bare CI runner: `unable to auto-detect email address`), a corrupt
# or missing object, an unreadable ref, a locked index. Those are failures of
# the INSTRUMENT, and the old code printed them as `merge-clean FAIL —
# conflicts against main`: a fabricated accusation against a candidate that was
# never examined. It is not a cosmetic mislabel — MERGE_OK stays 0, so the
# ladder, counterfeit corpus, dominance, doctrine, memlife and ruff are ALL
# skipped, and the run reads as a normal candidate refusal with no trace that
# the gate never ran. That is exactly how PR #19's CI job refused in 0.42s.
#
# A refusal must never blame the candidate for the instrument's own failure:
#   rc >= 128            -> refuse (exit 2, instrument broken), stderr quoted
#   rc in 1..127, or any unmerged path -> merge-clean FAIL (candidate conflicts)
#   rc == 0 and no unmerged paths      -> merge-clean ok
MERGE_OK=0
# stdout is discarded (-q already silences it); stderr is CAPTURED, never
# /dev/null'd, because it carries the only description of what went wrong.
MERGE_ERR=$(git -C "$SCRATCH" merge --no-ff --no-commit -q "$CANDIDATE_SHA" 2>&1 >/dev/null)
MERGE_RC=$?
MERGE_ERR=$(printf '%s' "$MERGE_ERR" | tr '\n' ' ' | cut -c1-400)
MERGE_UNMERGED=$(git -C "$SCRATCH" diff --name-only --diff-filter=U 2>/dev/null \
                 | head -20 | tr '\n' ' ' | sed 's/ *$//')
if [ "$MERGE_RC" -ge 128 ]; then
  # Not a verdict about the candidate. Do not record one.
  git -C "$SCRATCH" merge --abort 2>/dev/null || true
  refuse "trial-merge-broken" \
    "git merge exited $MERGE_RC without judging $CANDIDATE_SHA (instrument failure, not a conflict): ${MERGE_ERR:-<git wrote nothing to stderr>}"
elif [ "$MERGE_RC" -ne 0 ] || [ -n "$MERGE_UNMERGED" ]; then
  # A real candidate-side conflict. Name the paths git actually left unmerged;
  # when there are none, quote git's own words rather than assert a conflict
  # nothing observed.
  MERGE_DETAIL="conflicts against main"
  if [ -n "$MERGE_UNMERGED" ]; then
    MERGE_DETAIL="conflicts against main (unmerged paths: $MERGE_UNMERGED)"
  elif [ -n "$MERGE_ERR" ]; then
    MERGE_DETAIL="git merge exited $MERGE_RC with no unmerged paths: $MERGE_ERR"
  fi
  fail "merge-clean" "$MERGE_DETAIL"
  git -C "$SCRATCH" merge --abort 2>/dev/null || true
  step_end "failed" "$MERGE_DETAIL"
else
  pass "merge-clean"
  MERGE_OK=1
  step_end "ok"
fi

# Suites only run on a clean trial merge of the authenticated candidate tip.
# Tip stability is checked either way so a retarget after auth cannot be hidden
# behind an early exit.
if [ "$MERGE_OK" -eq 1 ]; then
  # The lane must import ITS OWN source, not the main repo's, or every result below
  # describes the wrong tree.
  # Review 2026-08-04 (F6): this import runs $SCRATCH's merged CANDIDATE package
  # __init__ chain, same class as the openapi-drift regen below — scrub it
  # identically so no candidate-code execution in this gate sees a live pin or
  # live state paths.
  RESOLVED=$(cd "$SCRATCH" && env -u OMNIAGENTOS_GATE_WORKSPACE -u MERGE_GATE_PY -u MERGE_GATE_PINNED \
    -u OMNI_NTFY_URL -u OPS_ALERT_SLACK_WEBHOOK_URL -u SLACK_WEBHOOK_URL \
    OMNIAGENTOS_DB="$SCRATCH/var/gate-worker-probe/state.sqlite3" \
    OMNIAGENTOS_VAR_DIR="$SCRATCH/var/gate-worker-probe" \
    OMNIAGENTOS_LEDGER_DIR="$SCRATCH/var/gate-worker-probe/ledger" \
    "$PY" -c "import omniagentos,sys;sys.stdout.write(omniagentos.__file__)" 2>/dev/null)
  case "$RESOLVED" in
    "$SCRATCH"/*) pass "tests-own-tree" ;;
    *) fail "tests-own-tree" "imports from '$RESOLVED'"
       # still fall through to tip-stable; do not render unknown as good
       ;;
  esac

  # --- per-step signed receipts (§2d-2): skip only what this SHA already ran ---
  # A fresh, signed receipt bound to (candidate SHA, merge-base SHA, trial-merge
  # TREE, exact command) lets the gate verify instead of re-run one green step.
  # The trial-merge tree is in the binding because suites run on the MERGE
  # COMMIT: the same candidate over a moved main is different tested content.
  # Any mismatch — signature, SHA, tree, command, age — runs the step for real
  # and records a fresh receipt. MERGE_GATE_STEP_RECEIPTS=0 disables both sides.
  MERGE_TREE_SHA=$(git -C "$SCRATCH" write-tree 2>/dev/null) || MERGE_TREE_SHA=""
  STEP_RECEIPTS="${MERGE_GATE_STEP_RECEIPTS:-1}"
  STEP_DIR="$SCRATCH/var/gate-steps"
  mkdir -p "$STEP_DIR"

  # --- A FAILING TEST MUST NAME ITSELF (2026-08-10) ---------------------------
  # `pytest -q` prints "1 failed, 892 passed" and the FAILED lines, and
  # report_suite already echoes the first five. That is enough when the failure
  # is reproducible; it is not enough when it is not. A single unnamed 1-of-893
  # flake ate five candidates in a row because nothing in the durable evidence
  # recorded WHICH node it was, so each rerun started the diagnosis over.
  #
  # suite_worker has been able to emit JUnit since MERGE_GATE_JUNIT_DIR was
  # added; nothing ever set it, so it was off in every real run. Default it —
  # into $SCRATCH, which the EXIT trap removes — and copy the XML out beside the
  # receipts only for steps that FAILED. Green runs leave nothing behind, which
  # is the same retention rule the ladder capture below already uses; a durable
  # XML per suite per gate run would be a new unbounded artifact class bought
  # for output nobody reads.
  #
  # EVIDENCE ONLY, never a verdict input: suite_worker arms --junitxml only
  # after proving the directory writable, precisely so an unwritable path cannot
  # exit pytest non-zero at sessionfinish and be scored as a candidate defect.
  # Everything below is likewise best-effort — a failed copy prints and returns.
  MERGE_GATE_JUNIT_DIR="${MERGE_GATE_JUNIT_DIR:-$STEP_DIR/junit}"
  #: Set to the kept DIRECTORY the first time any step's XML is preserved; rides
  #: into the run receipt so the gate daemon can quote a path instead of asking
  #: a human to reproduce the run. Empty = nothing failed, or nothing was kept.
  JUNIT_KEPT_DIR=""

  keep_junit() {  # step-id — copy that step's JUnit XML next to the receipts
    local step="$1" src="$MERGE_GATE_JUNIT_DIR/$step.xml" dest
    [ -s "$src" ] || return 0
    dest="$EVIDENCE_ROOT/records/merge-gate/${CANDIDATE_SHA}.junit-$(printf '%s' "$RUN_STARTED_AT" | tr -d ':-')-$$"
    mkdir -p "$dest" 2>/dev/null || return 0
    cp "$src" "$dest/$step.xml" 2>/dev/null || return 0
    JUNIT_KEPT_DIR="$dest"
    printf 'merge-gate: %s JUnit kept at %s/%s.xml\n' "$step" "$dest" "$step" >&2
    return 0
  }

  verify_step_receipt() {  # step-id, exact-command -> stdout: reuse detail; rc 0 = skip
    [ "$STEP_RECEIPTS" = "1" ] || return 1
    [ -n "$MERGE_TREE_SHA" ] || return 1
    PYTHONPATH="$REPO" "$PY" -m omniagentos.scheduler.gate_evidence verify-step \
      --evidence-root "$EVIDENCE_ROOT" \
      --step "$1" \
      --candidate-sha "$CANDIDATE_SHA" \
      --merge-base-sha "$MERGE_BASE_SHA" \
      --merge-tree-sha "$MERGE_TREE_SHA" \
      --command "$2" 2>/dev/null
  }

  # step-id, exact-command, output-file, summary, started-at, MEASURED exit code
  # The exit code is the one the gate MEASURED, never the constant 0: a receipt
  # that asserts its own greenness is the same self-report the whole evidence
  # chain exists to refuse, and the verifier re-applies the step's verdict rule
  # to (exit_code, summary) on every reuse.
  record_step_receipt() {
    [ "$STEP_RECEIPTS" = "1" ] || return 0
    [ -n "$MERGE_TREE_SHA" ] || return 0
    local rec_out
    if ! rec_out=$(PYTHONPATH="$REPO" "$PY" -m omniagentos.scheduler.gate_evidence record-step \
        --evidence-root "$EVIDENCE_ROOT" \
        --step "$1" \
        --candidate-sha "$CANDIDATE_SHA" \
        --merge-base-sha "$MERGE_BASE_SHA" \
        --merge-tree-sha "$MERGE_TREE_SHA" \
        --command "$2" \
        --workspace "$SCRATCH" \
        --output "$3" \
        --exit-code "${6:-0}" \
        --summary "$4" \
        --started-at "$5" 2>&1); then
      printf 'merge-gate: step-receipt record failed for %s: %s\n' "$1" "$rec_out" >&2
    fi
    return 0
  }

  # measured-exit-code, output-file -> stdout: the verdict line (rc 0) or the refusal
  #
  # The verdict RULE lives in exactly one place — _counterfeit_verdict_rejections
  # in omniagentos/scheduler/gate_evidence.py — and the same function judges the
  # live run here, the receipt minted from it, and every later reuse of that
  # receipt. Two copies of a settled definition is the defect class that
  # auto-paused this repo's routines four times on 2026-07-31; a receipt that
  # certifies what the live gate would refuse is that same defect wearing a
  # signature. Loaded with PYTHONPATH="$REPO" (the TRUSTED checkout), never from
  # $SCRATCH: the candidate does not get to supply the rule it is judged by.
  judge_counterfeit() {
    PYTHONPATH="$REPO" "$PY" -m omniagentos.scheduler.gate_evidence judge-counterfeit \
      --exit-code "$1" --output "$2" 2>&1
  }

  # measured-exit-code, output-file -> rc 0 when the CONTROL exhausted its bound
  #
  # THE INSTRUMENT RAN OUT OF TIME BEFORE IT JUDGED ANYTHING. The counterfeit
  # harness runs its must_fail union UNPATCHED first (run_control), and that
  # control has its own 300s bound. When the bound is what ends the run — a
  # loaded box, a cold tree — the harness says so in as many words and then
  # returns 1, the same code it returns for a control that came back RED. Only
  # the text distinguishes them, and the difference is the whole verdict:
  # exhaustion is the gate's own capacity, red is a statement about the corpus.
  # Reading the first as the second is what rejected an innocent 2-member train
  # on 2026-08-10.
  #
  # EVERY CONDITION BELOW IS NECESSARY, and each one degrades to "not an
  # instrument error" (i.e. to the ordinary refusal) rather than to a pass:
  #   * rc == 1        the harness's control-failure code. A killed harness, a
  #                    corpus that would not load (2) or a green run (0) are all
  #                    different events and none of them is this one.
  #   * the HEADER     main()'s "COUNTERFEIT GATE CONTROL FAILED:" prefix, so a
  #                    stray line anywhere else in the capture cannot buy the
  #                    classification on its own.
  #   * the ANCHORED   run_control()'s own message, line-initial. The em dash is
  #     MESSAGE        matched as `.*` deliberately: a locale or a punctuation
  #                    edit must not silently unbind this, while the two fixed
  #                    halves still identify the message. Kept honest by
  #                    tests/scripts/test_merge_gate_warmup_and_control_bound.py
  #                    ::test_the_gate_pattern_still_matches_the_harness_diagnostic,
  #                    which raises a real TimeoutExpired through run_control and
  #                    matches THIS pattern against the text that comes out.
  #   * PROVENANCE     the harness that produced the text must be MAIN's. See
  #                    claim 2 at GATE_INSTRUMENT_SLUGS.
  cf_control_bound_exhausted() {
    local rc="$1" out="$2" touched grc
    [ "$rc" = "1" ] || return 1
    [ -s "$out" ] || return 1
    grep -q 'COUNTERFEIT GATE CONTROL FAILED' "$out" 2>/dev/null || return 1
    # not-a-decision: the word below is QUOTED FROM THE HARNESS's own sentence
    # ("instrument bound exhausted, not a corpus verdict"). This matcher reads an
    # INSTRUMENT diagnostic and decides no approval verdict of any kind — and it
    # fails CLOSED: `|| return 1` means a grep that could not run (rc >= 2)
    # withholds the instrument label rather than granting it, so there is no
    # favourable reading of a grep that did not work.
    grep -qE '^control \(unpatched\) timed out after [0-9.]+s .*instrument bound exhausted, not a corpus verdict' \
      "$out" 2>/dev/null || return 1
    # A grep that could not RUN is not a grep that found nothing (classifier_rc's
    # rule, applied locally because this helper returns a status rather than
    # refusing): rc >= 2 means the provenance is UNVERIFIED, which withholds the
    # instrument label instead of granting it on an unmeasured input.
    touched=$(printf '%s\n' "$SWEPT_PATHS" | grep -Fx 'tests/counterfeits/harness.py'); grc=$?
    [ "$grc" -le 1 ] || return 1
    [ -z "$touched" ] || return 1
    return 0
  }

  # out-file, status-file (safe to background)
  #
  # The harness's EXIT CODE is the gate's primary signal, so it must survive the
  # backgrounding: `wait` returns it once and the value was previously thrown
  # away, leaving the gate to judge candidate-controlled stdout instead. Writing
  # it to a status file (as suite_worker does) also means a worker KILLED before
  # it could report leaves no status at all — which the scorer refuses, rather
  # than reading an absent code as 0.
  # ISOLATED RUNTIME STATE per concurrent worker. counterfeit_worker and
  # suite_worker run BACKGROUNDED AT THE SAME TIME in the same $SCRATCH. Sharing
  # one default SQLite path makes them race: whichever loses dies with
  # "sqlite3.ProgrammingError: Cannot operate on a closed database", so the gate
  # reports a defect in whichever worker lost the coin flip rather than in the
  # candidate. Reproduced 2026-08-03 (ladder crashed, counterfeit green; the
  # prior run had the mirror image). Isolation changes NO check's assertion —
  # each worker simply gets its own runtime root.
  # WORKER-ENV LEAK, diagnosed live 2026-08-04 (twice-confirmed).
  # scripts/launch-env.sh auto-exports OMNIAGENTOS_GATE_WORKSPACE from ANY shell
  # whose <repo>-gate checkout is clean; unlike OMNIAGENTOS_DB/_VAR_DIR/
  # _LEDGER_DIR below, neither worker scrubbed it, so it rode the ambient
  # environment straight into these "isolated" child processes. Once set,
  # default_gate_workspace() (gate_runner.py) resolves it and routines_settle
  # really EXECUTES the routine's declared gate — a live `pytest` subprocess
  # against that workspace's pin — inside what is supposed to be a hermetic
  # corpus run. That flipped premise tests in tests/scheduler/test_builtin_jobs.py
  # (test_no_input_cycle_is_neutral_not_accepted and its siblings) red under any
  # launch-env-sourced shell, and because that node sits in a counterfeit's
  # must_fail set, produced the chronic in-gate "COUNTERFEIT GATE CONTROL
  # FAILED" refusal plus ~20 minutes of hidden live-gate execution per ladder
  # run. Audited the rest of launch-env.sh's export list for the same class of
  # leak: OMNIAGENTOS_VAR/_VAR_DIR/_LEDGER_DIR/_VAULT_DIR are already isolated a
  # second time, session-wide, by tests/conftest.py's autouse
  # _isolate_var_and_reflexion / _isolate_ledger_and_vault fixtures.
  # HALF-ISOLATION IS ITS OWN DEFECT (2026-08-05). Those fixtures HONOUR a
  # preset, and they resolve it as `OMNIAGENTOS_VAR or OMNIAGENTOS_VAR_DIR` —
  # so a caller that overrode only VAR_DIR (this one) left VAR pointing at the
  # operator's LIVE var/runtime, and the two names then disagreed. The
  # fixture seeded connectors.yaml into the LIVE root while the resolver read
  # the scratch one: `tests/api/test_capability_requests.py` went 30-passed ->
  # 10-failed with "Unable to read connector registry", and every default-path
  # var/vault write in the ladder landed in the operator's live tree. Set the
  # WHOLE runtime root per worker — DB, VAR, VAR_DIR, LEDGER_DIR, VAULT_DIR —
  # so no two names can disagree; a partial override is worse than none.
  # THAT AUDIT'S REMAINING CLAIM WAS WRONG, and 2026-08-05 proved it: it read
  # "every remaining export is a static feature-flag preset" as "inert to
  # tests". OMNIAGENTOS_BUDGET_ENFORCEMENT=block (added to launch-env.sh on
  # 2026-08-04) is exactly such a preset, and budget.policy.blocks() reads it
  # from live admission/reaper/CLI-cap paths — so the simharness campaigns'
  # recovery attempts were refused, two of their nodes in the corpus's
  # must_fail union went red, and the control kept failing IN-GATE after
  # GATE_WORKSPACE was scrubbed. A behaviour flag is a test premise. The cure
  # is one layer down and covers every entry point (this gate, `make test`,
  # `make counterfeit-gate`): tests/conftest.py pins it session-wide, the same
  # way it pins DB/VAR/LEDGER/VAULT/KNOWLEDGE. Do not add a per-flag scrub
  # here — a list maintained in the gate would drift from launch-env.sh again.
  # Scrub GATE_WORKSPACE explicitly in both workers below (it points at a live
  # external resource, which no conftest pin can make hermetic); never rely on
  # inheriting an unset ambient value.
  #
  # GATE_CHILD_SCRUB_KEYS (2026-08-10) — the EGRESS trio, and why it is here
  # despite the "do not add a per-flag scrub" rule three paragraphs up. That
  # rule is about BEHAVIOUR FLAGS, whose list drifts from launch-env.sh; these
  # three name a LIVE EXTERNAL RESOURCE, which is the same category as
  # GATE_WORKSPACE and the one exception that paragraph already carves out.
  # pipeline/bridge/run-loop.sh:75 exports OMNI_NTFY_URL into every loop role's
  # environment, so a gate launched from a loop session inherits it, and
  # omniagentos/sessions/notify.py reads it straight out of os.environ. MEASURED
  # 2026-08-10 with a counting listener in place of the endpoint: one run of the
  # ladder's notify-adjacent suites put 21 real POSTs on the wire (18 ntfy, 3
  # Slack). Not one test went red — the damage was silent egress to the
  # operator's phone, per gate run.
  # TWO INDEPENDENT LAYERS, on purpose. tests/conftest.py's autouse
  # _scrub_egress_env is the PRIMARY cure, because it also covers `make test`
  # and a bare pytest; it cannot cover a gate child that is not pytest (the
  # counterfeit harness's own process, the openapi regen, the tests-own-tree
  # probe), which is what these flags are for.
  # ALL FOUR SITES OR NONE. This script scrubs in four places — the two workers
  # below, the openapi-drift regen (F1) and the tests-own-tree probe (F6) — and
  # "three of four" is this repo's named defect shape. The key sets are pinned
  # equal to each other by
  # tests/scripts/test_merge_gate_worker_env_isolation.py::
  # test_every_gate_child_scrubs_the_same_key_set, so adding a key to one site
  # and forgetting another fails loudly instead of leaking quietly.
  # Review 2026-08-04 (F5): `unset VAR && cmd` gates the whole command on
  # unset's own exit status — a hypothetical readonly VAR would make `unset`
  # FAIL and, chained with &&, silently skip the worker entirely rather than
  # run it scrubbed. `env -u` builds the child's environment table directly
  # (no shell-variable readonly semantics involved) and cannot skip anything:
  # it is part of the one command line, not a separate gated step.
  counterfeit_worker() {
    local outfile="$1" statusfile="$2" rc
    (
      cd "$SCRATCH" && \
      env -u OMNIAGENTOS_GATE_WORKSPACE -u MERGE_GATE_PY -u MERGE_GATE_PINNED \
        -u OMNI_NTFY_URL -u OPS_ALERT_SLACK_WEBHOOK_URL -u SLACK_WEBHOOK_URL \
        PYTHONPATH="$SCRATCH${PYTHONPATH:+:$PYTHONPATH}" \
        OMNIAGENTOS_CF_POOL_WORKERS="$CF_POOL_WORKERS" \
        OMNIAGENTOS_DB="$SCRATCH/var/gate-worker-counterfeit/state.sqlite3" \
        OMNIAGENTOS_VAR="$SCRATCH/var/gate-worker-counterfeit" \
        OMNIAGENTOS_VAR_DIR="$SCRATCH/var/gate-worker-counterfeit" \
        OMNIAGENTOS_LEDGER_DIR="$SCRATCH/var/gate-worker-counterfeit/ledger" \
        OMNIAGENTOS_VAULT_DIR="$SCRATCH/var/gate-worker-counterfeit/vault" \
        MERGE_GATE_DEPTH="$GATE_CHILD_DEPTH" \
        MERGE_GATE_LADDER_WORKERS= MERGE_GATE_CF_POOL_WORKERS= MERGE_GATE_SUITE_WORKERS= \
        "$PY" -m tests.counterfeits.harness >"$outfile" 2>&1
    )
    rc=$?
    printf '%s\n' "$rc" >"$statusfile"
  }

  suite_worker() {  # out-file, status-file, pytest args... (safe to background)
    local outfile="$1" statusfile="$2"
    shift 2
    local out rc tail_line junit_name
    local -a junit_args=()
    if [ -n "${MERGE_GATE_JUNIT_DIR:-}" ]; then
      # JUnit is EVIDENCE ONLY and must never be able to flip a verdict. If the
      # dir cannot be created or written, pytest would exit non-zero at
      # sessionfinish even with every test green, and report_suite would score
      # that as a candidate suite failure — an instrument error laundered as a
      # candidate defect. So arm --junitxml only after proving the dir writable;
      # otherwise say so on stderr and run the suite exactly as if unset.
      junit_name=$(basename "$statusfile" .status)
      if mkdir -p "$MERGE_GATE_JUNIT_DIR" 2>/dev/null &&
         ( : > "$MERGE_GATE_JUNIT_DIR/.write-probe.$junit_name" ) 2>/dev/null; then
        rm -f "$MERGE_GATE_JUNIT_DIR/.write-probe.$junit_name" 2>/dev/null || true
        junit_args=("--junitxml=$MERGE_GATE_JUNIT_DIR/$junit_name.xml")
      else
        printf 'merge-gate: JUnit dir %s is not writable — suite %s runs WITHOUT JUnit (evidence-only; never a verdict input)\n' \
          "$MERGE_GATE_JUNIT_DIR" "$junit_name" >&2
      fi
    fi
    # Same 2026-08-04 leak as counterfeit_worker above: scrub the auto-exported
    # OMNIAGENTOS_GATE_WORKSPACE so the ladder never inherits a live gate pin.
    # env -u, not `unset ... &&` (F5): see counterfeit_worker's comment above.
    out=$(cd "$SCRATCH" && \
      env -u OMNIAGENTOS_GATE_WORKSPACE -u MERGE_GATE_PY -u MERGE_GATE_PINNED \
      -u OMNI_NTFY_URL -u OPS_ALERT_SLACK_WEBHOOK_URL -u SLACK_WEBHOOK_URL \
      OMNIAGENTOS_DB="$SCRATCH/var/gate-worker-suite/state.sqlite3" \
      OMNIAGENTOS_VAR="$SCRATCH/var/gate-worker-suite" \
      OMNIAGENTOS_VAR_DIR="$SCRATCH/var/gate-worker-suite" \
      OMNIAGENTOS_LEDGER_DIR="$SCRATCH/var/gate-worker-suite/ledger" \
      OMNIAGENTOS_VAULT_DIR="$SCRATCH/var/gate-worker-suite/vault" \
      MERGE_GATE_DEPTH="$GATE_CHILD_DEPTH" \
      MERGE_GATE_LADDER_WORKERS= MERGE_GATE_CF_POOL_WORKERS= MERGE_GATE_SUITE_WORKERS= \
      "$PY" -m pytest -q ${junit_args[@]+"${junit_args[@]}"} "$@" 2>&1)
    rc=$?
    printf '%s' "$out" >"$outfile"
    tail_line=$(printf '%s' "$out" | grep -E '^[0-9]+ (passed|failed)|passed,|failed,' | tail -1)
    printf '%s\n%s\n' "$rc" "$tail_line" >"$statusfile"
  }

  # out-file, status-file -> rc: 0 warmed, 124 bound exhausted, else the child's
  # own status. NEVER a verdict — the full argument is at the WORKSPACE WARM-UP
  # block below, which is this function's only caller.
  #
  # THE CHILD RUNS NO CANDIDATE CODE, and that is why this is the one gate child
  # with no `env -u` scrub list. `compileall` PARSES the files it is given; it
  # never imports them. It is invoked with cwd "$REPO" (the trusted checkout)
  # and ABSOLUTE paths under $SCRATCH precisely so that `-m` resolves sys.path[0]
  # to the trusted tree: from $SCRATCH a candidate-planted compileall.py (or a
  # sitecustomize.py) would shadow the stdlib module and execute. Replace this
  # with an import-based warm-up — `python -c "import omniagentos"` — and it
  # becomes a fifth candidate-executing child, so the scrub list the other four
  # carry has to come with it.
  #
  # BOUNDED WITHOUT `timeout(1)`. This script runs on macOS, where coreutils is
  # not guaranteed (`timeout` is `gtimeout` if it exists at all), and a bound
  # that degrades to "unbounded when the binary is missing" is a favourable
  # absence. Background + poll + SIGKILL is portable. The poll watches for the
  # STATUS FILE, not `kill -0`: a finished-but-unreaped child is a zombie that
  # `kill -0` still reports as alive, so a `kill -0` loop would burn the whole
  # bound on every healthy run. Granularity is 1s, so the recorded cost can
  # overstate a fast warm-up by up to a second.
  warmup_worker() {
    local outfile="$1" statusfile="$2" wpid waited rc
    rm -f "$statusfile" 2>/dev/null || true
    {
      ( cd "$REPO" && "$PY" -m compileall -q ${WARM_ARGS[@]+"${WARM_ARGS[@]}"} ) >"$outfile" 2>&1
      rc=$?
      # tmp-then-rename: the poll below treats the file's EXISTENCE as "done",
      # so it must never observe one that has been created but not written.
      printf '%s\n' "$rc" >"$statusfile.part" 2>/dev/null &&
        mv -f "$statusfile.part" "$statusfile" 2>/dev/null
    } &
    wpid=$!
    waited=0
    while [ ! -f "$statusfile" ] && [ "$waited" -lt "$WARM_TIMEOUT" ]; do
      sleep 1
      waited=$((waited + 1))
    done
    if [ ! -f "$statusfile" ]; then
      kill -9 "$wpid" 2>/dev/null
      wait "$wpid" 2>/dev/null
      # LAST-MOMENT COMPLETION: the child may have finished between the final
      # poll and the kill. Re-read once rather than report a bound exhaustion
      # for work that actually completed.
      [ -f "$statusfile" ] || return 124
    fi
    wait "$wpid" 2>/dev/null
    rc=$(sed -n '1p' "$statusfile" 2>/dev/null)
    case "$rc" in ''|*[!0-9]*) return 125 ;; esac
    return "$rc"
  }

  report_suite() {  # name, step-id, exact-command, started-at, out-file, status-file
    local name="$1" step="$2" cmd="$3" started="$4" outfile="$5" statusfile="$6"
    local rc tail_line
    rc=$(sed -n '1p' "$statusfile" 2>/dev/null)
    tail_line=$(sed -n '2p' "$statusfile" 2>/dev/null)
    if [ -z "$rc" ]; then
      fail "$name" "suite worker produced no status (crashed)"
      step_log "$step" "$started" "failed" "suite worker produced no status (crashed)"
    elif [ "$rc" != "0" ]; then
      fail "$name" "${tail_line:-rc=$rc}"
      grep '^FAILED' "$outfile" 2>/dev/null | head -5 | sed 's/^/      /'
      # ONE hook for every suite: the ladder reports through this function too,
      # so naming the failures here covers ladder, contracts-scripts,
      # pipeline-tests, dominance-corpus, doctrine, memlife and each bound-test
      # step without a second copy of the rule.
      keep_junit "$step"
      step_log "$step" "$started" "failed" "${tail_line:-rc=$rc}"
    else
      pass "$name" "$tail_line"
      step_log "$step" "$started" "ok" "$tail_line"
      record_step_receipt "$step" "$cmd" "$outfile" "$tail_line" "$started" "$rc"
    fi
  }

  # Run suites whenever merge succeeded (even if earlier mechanical checks failed)
  # so the gate report stays informative; final PASS still requires zero failures.
  run_suite() {  # name, step-id, pytest args...
    local name="$1" step="$2"
    shift 2
    local cmd="python -m pytest -q $*"
    local reuse
    if reuse=$(verify_step_receipt "$step" "$cmd"); then
      pass "$name" "$reuse"
      step_log "$step" "$(utc_now)" "reused" "$reuse"
      return 0
    fi
    local started; started=$(utc_now)
    suite_worker "$STEP_DIR/$step.out" "$STEP_DIR/$step.status" "$@"
    report_suite "$name" "$step" "$cmd" "$started" "$STEP_DIR/$step.out" "$STEP_DIR/$step.status"
  }

  # --- A STEP THAT NEVER RAN MUST NEVER READ AS A STEP THAT PASSED -------------
  # Every suite below used to be guarded by a bare `[ -d "$SCRATCH/tests/x" ] &&`
  # (and the counterfeit corpus by an `if`/`then` with no `else`), which no-ops
  # with ZERO output: no step, no line, no failure. A candidate whose only change
  # was `git rm -r tests/{contracts,scripts,objective,doctrine,memlife}` therefore
  # turned five steps off and this gate printed "PASS — candidate is safe to
  # merge". The absence was unfalsifiable in all THREE carriers at once:
  #   * exit code — FAILURES is never appended to, so the verdict stays PASS;
  #   * receipt   — the step is simply missing from steps[], and there is no
  #                 expected-step manifest to compare against;
  #   * report    — neither pass nor fail is called, so nothing prints.
  # Reproduced end to end, and pinned, by
  # tests/scripts/test_merge_gate_absent_suite_guards.py.
  #
  # The record is modelled on the step-receipt reuse path already in this file:
  # a status that is neither "ok" nor "failed", carrying its own reason.
  #
  # REQUIRED vs MERELY ABSENT — two conditions, both load-bearing:
  #   * MERGE_GATE_PINNED=1, which is every real gate run. The un-armed path is
  #     what the fixture modules, the counterfeit corpus and
  #     tests/acceptance/s17_gate_determinism.sh drive against minimal repos; an
  #     unconditional refusal turns them red for a property they never asserted.
  #   * the directory EXISTS IN THE PINNED WORKSPACE. "main never had this
  #     suite" and "the candidate REMOVED the suite main has" are different
  #     claims and only the second is a defect. Without this half,
  #     tests/scripts/test_merge_gate_openapi_drift.py — minimal repos with none
  #     of these directories, driven at MERGE_GATE_PINNED=1 — would refuse on a
  #     property it does not model. It is also the stronger test: it measures the
  #     deletion rather than the mere absence.
  # Un-pinned, or absent from main too, the step is still RECORDED and PRINTED;
  # only the verdict is withheld. Silence was the whole defect.
  SUITE_MISSING=""
  SUITE_REQUIRED=""
  suite_dirs_present() {  # dir... -> rc 0 = every directory is in the merged tree
    local d
    SUITE_MISSING=""
    SUITE_REQUIRED=""
    for d in "$@"; do
      [ -d "$SCRATCH/$d" ] && continue
      SUITE_MISSING="${SUITE_MISSING:+$SUITE_MISSING }$d"
      if [ "$PINNED" = "1" ] && [ -d "$GATE_WS/$d" ]; then
        SUITE_REQUIRED="${SUITE_REQUIRED:+$SUITE_REQUIRED }$d"
      fi
    done
    [ -z "$SUITE_MISSING" ]
  }

  suite_skip_verdict() {  # name, missing, required — the human + exit-code carrier
    if [ -n "$3" ]; then
      fail "$1" "suite removed by the candidate: $3 — present at the pinned workspace $GATE_WS, absent from the merged tree. A deleted suite is not a passing suite."
    else
      note "$1" "skipped — absent from the merged tree: $2"
    fi
  }

  suite_skip_step() {  # step-id, missing, required — the steps[]/receipt carrier
    if [ -n "$3" ]; then
      step_log "$1" "$(utc_now)" "skipped-required" \
        "absent from the merged tree: $2; present at the pinned workspace: $3"
    else
      step_log "$1" "$(utc_now)" "skipped" "absent from the merged tree: $2"
    fi
  }

  # `-x "<flags>"` (optional, FIRST) prepends pytest flags — today only xdist
  # width — ahead of the target list. It is a parameter rather than a global
  # because a global read inside this function would apply to every suite that
  # happens to be called after it was set, and tests/doctrine is one of them:
  # its revert harness mutates tests/doctrine/_fixtures/ in place and corrupts
  # the tree under any width > 1 (7 races measured 2026-08-03). Per-call is the
  # shape that cannot leak onto the wrong suite.
  #
  # The flags land in $* and therefore in the command string run_suite binds
  # into the step receipt, which is constraint 3 of the ladder's E7 block below:
  # a serial receipt must never verify a parallel run or the reverse. Passing an
  # EMPTY "$1" leaves the argument list byte-identical to the pre-2026-08-10
  # spelling, so a suite that is not widened keeps reusing its old receipts.
  run_suite_if_present() {  # [-x "<pytest flags>"] name, step-id, dir...
    # "${1:-}", not "$1": this script runs under `set -u` (line 82), where a
    # bare "$1" in a zero-argument call aborts the whole gate with "unbound
    # variable" instead of reaching any check. No caller does that today, which
    # is exactly why it would be discovered the expensive way.
    local xflags=""
    if [ "${1:-}" = "-x" ]; then xflags="$2"; shift 2; fi
    local name="$1" step="$2"
    shift 2
    if suite_dirs_present "$@"; then
      # Trailing slashes preserved: $LADDER_CMD's sibling, the pytest argument
      # list, is rendered into the step-receipt key, so changing its spelling
      # would invalidate every existing receipt for no reason.
      local targets="" d
      for d in "$@"; do targets="${targets:+$targets }$d/"; done
      # shellcheck disable=SC2086 -- $xflags/$targets are deliberate word lists
      run_suite "$name" "$step" $xflags $targets
      return 0
    fi
    suite_skip_verdict "$name" "$SUITE_MISSING" "$SUITE_REQUIRED"
    suite_skip_step "$step" "$SUITE_MISSING" "$SUITE_REQUIRED"
    return 1
  }

  # --- did the bound test EXECUTE, or was it merely not asked? -----------------
  # pytest exits 0 for "every named node passed" AND for "every named node was
  # SKIPPED", so the exit code alone cannot tell a guard that held from a guard
  # that was never asked. That discrimination is already settled, measured and
  # commented in tests/counterfeits/harness.py (_executed_no_tests at the rc==0
  # side, _is_collection_failure at the "the node id was never collected" side),
  # so it is IMPORTED here rather than re-derived: two copies of a settled
  # definition is the defect class most of this file's comments are about.
  #
  # LOADED BY PATH FROM $REPO — the TRUSTED checkout, never $SCRATCH — and by
  # PATH rather than by module name on purpose. `import tests.counterfeits.
  # harness` does NOT resolve through PYTHONPATH here: `tests/__init__.py` makes
  # `tests` a REGULAR package, and this venv carries an editable-install .pth
  # (_editable_impl_omniagentos.pth) whose path entry supplies one. Measured on
  # this host: from an unrelated cwd with PYTHONPATH=/tmp, the name resolved to
  # /Users/youruser/OmniAgentOS/tests/counterfeits/harness.py — i.e. to
  # whatever checkout the interpreter was built against. In PINNED mode that is
  # the LIVE shared checkout, so the gate would have judged a pinned candidate
  # with a rule read out of a mutable tree: the same "which copy of this file is
  # read" defect the reachability-exemption trap and the stale-judge check above
  # exist to close. A path is unambiguous; a module name is a search.
  #
  # A checkout that cannot supply the rule yields `unclassifiable`, which the
  # caller scores as NOT GREEN; there is deliberately no local fallback copy,
  # because a fallback is how two definitions drift apart unnoticed.
  classify_bound_run() {  # measured-rc, output-file -> stdout: "<slug> <detail>"
    MG_BT_RC="$1" MG_BT_OUT="$2" MG_REPO="$REPO" PYTHONPATH="$REPO" "$PY" - <<'MGBOUND'
import importlib.util
import os
import sys
from types import SimpleNamespace

_repo = os.environ.get("MG_REPO") or ""
_rule_path = os.path.join(_repo, "tests", "counterfeits", "harness.py")
try:
    # The rule's own imports (omniagentos.path_containment) must come from the
    # same trusted checkout, so it leads sys.path rather than merely being on it.
    if _repo and _repo not in sys.path:
        sys.path.insert(0, _repo)
    _spec = importlib.util.spec_from_file_location("_merge_gate_bound_rule", _rule_path)
    if _spec is None or _spec.loader is None:
        raise ImportError(f"no loadable module at {_rule_path}")
    _rule = importlib.util.module_from_spec(_spec)
    # REGISTERED BEFORE EXECUTION, and it is not optional: @dataclass resolves
    # its own module through sys.modules[cls.__module__] while the class body
    # runs, and the rule file has dataclasses in it. Without this the load dies
    # with "'NoneType' object has no attribute '__dict__'" and every binding
    # degrades to `unclassifiable`.
    sys.modules[_spec.name] = _rule
    _spec.loader.exec_module(_rule)
    _executed_no_tests = _rule._executed_no_tests
    _is_collection_failure = _rule._is_collection_failure
except Exception as exc:  # noqa: BLE001 - ANY load failure is unclassifiable
    print(f"unclassifiable the settled rule could not be loaded from {_rule_path}: {exc}")
    raise SystemExit(0)

rc_text = (os.environ.get("MG_BT_RC") or "").strip()
try:
    rc = int(rc_text)
except ValueError:
    print(f"unclassifiable the worker reported a non-numeric status {rc_text!r}")
    raise SystemExit(0)

try:
    with open(os.environ.get("MG_BT_OUT") or "", encoding="utf-8", errors="replace") as handle:
        output = handle.read()
except OSError as exc:
    print(f"unclassifiable the worker output could not be read: {exc}")
    raise SystemExit(0)

# stderr is folded into stdout by suite_worker (`2>&1`), so it is empty here by
# construction rather than by omission.
proc = SimpleNamespace(returncode=rc, stdout=output, stderr="")
if _is_collection_failure(proc):
    print("not-evaluable the node id was never collected (collection or usage error)")
elif rc == 0 and _executed_no_tests(proc):
    print("not-evaluable the node was skipped or deselected, so it asserted nothing")
elif rc == 0:
    print("executed-green")
else:
    print(f"executed-red pytest exited {rc}")
MGBOUND
  }

  # MANDATORY, not decoration: pyproject.toml puts a global `-m 'not (...)'` in
  # addopts, `-m` is store-not-append, and pytest applies the marker filter to
  # EXPLICIT NODE IDS too. Without the override a bound node carrying an excluded
  # marker deselects silently and grades as "did not fail" — a false GREEN. The
  # tautology selects everything while overriding the global; the idiom and its
  # rationale are already in production at Makefile:387 and Makefile:348-357.
  # ONE binding, used by the executed command and by nothing else, so there is
  # no second spelling to drift from it.
  BOUND_MARKER="counterfeit_gate or not counterfeit_gate"

  # --- one binding, re-run on the MERGED tree ---------------------------------
  # NEVER RECEIPT-CACHED, and that is a deliberate exception to the step-receipt
  # machinery every other suite here uses. Two independent reasons:
  #
  #  * A CACHED CLOSURE PROOF IS THE SHAPE THIS FEATURE EXISTS TO KILL. The
  #    other steps buy back ~12 minutes; a single named node costs seconds, so
  #    the entire upside of caching here is noise, while the downside is a
  #    signed artifact asserting a test passed on a run that did not execute it.
  #  * The receipt store would not have taken it anyway: the step ids a receipt
  #    may carry are enumerated in gate_evidence.MERGE_GATE_STEP_NAMES, which has
  #    no `bound-test-*` entry, so `record-step` REFUSES with "unknown
  #    merge-gate step". Wiring it up would mean widening that allowlist — i.e.
  #    admitting a per-run closure claim to the durable, reusable evidence store.
  #
  # So this is also not a caller of run_suite: report_suite records a receipt on
  # ANY rc == 0, and a bound node that was SKIPPED exits 0.
  run_bound_test() {  # step-id, node-id
    local step="$1" node="$2"
    local rc tail_line verdict slug detail started
    started=$(utc_now)
    suite_worker "$STEP_DIR/$step.out" "$STEP_DIR/$step.status" -m "$BOUND_MARKER" "$node"
    rc=$(sed -n '1p' "$STEP_DIR/$step.status" 2>/dev/null)
    tail_line=$(sed -n '2p' "$STEP_DIR/$step.status" 2>/dev/null)
    if [ -z "$rc" ]; then
      bound_result_record "weakened"
      fail "bound-test" "$node produced no status (the suite worker died) — an instrument that did not run is NOT a pass"
      step_log "$step" "$started" "failed" "$node: worker produced no status"
      return 0
    fi
    verdict=$(classify_bound_run "$rc" "$STEP_DIR/$step.out")
    slug="${verdict%% *}"
    detail="${verdict#"$slug"}"; detail="${detail# }"
    case "$slug" in
      executed-green)
        # POSITIVE EVIDENCE OF EXECUTION, required on top of rc == 0: the summary
        # line this gate already parses ("N passed" / "N failed") has to be
        # there. A run that printed no summary at all asserted nothing, and an
        # absence must never be scored as the favourable answer.
        if [ -z "$tail_line" ]; then
          bound_result_record "weakened"
          fail "bound-test" \
            "$node did not execute: rc=0 with no pytest summary line, so nothing shows it ran — NOT_EVALUABLE is not GREEN"
          step_log "$step" "$started" "failed" "$node: rc=0 and no summary line"
          return 0
        fi
        bound_result_record "green"
        pass "bound-test" "$node — $tail_line"
        step_log "$step" "$started" "ok" "$node: $tail_line"
        ;;
      not-evaluable)
        bound_result_record "weakened"
        fail "bound-test" "$node did not execute: ${detail:-nothing ran} — NOT_EVALUABLE is not GREEN"
        step_log "$step" "$started" "failed" "$node did not execute: ${detail:-nothing ran}"
        ;;
      unclassifiable)
        # ITS OWN SLUG, so an operator can triage this without reading prose:
        # every other `bound-test` failure is a statement about the CANDIDATE,
        # while this one is a statement about the gate's own ability to judge.
        # It is still a FAILURE (exit 1) and still not green — the gate could not
        # tell, and could-not-tell is not a closure — but it points at a
        # different thing to go and fix.
        bound_result_record "weakened"
        fail "bound-test-unclassifiable" \
          "$node: the gate could not establish that it executed (${detail:-no reason given}) — an unclassifiable result is NOT GREEN"
        step_log "$step" "$started" "failed" "$node: unclassifiable — ${detail:-no reason given}"
        ;;
      *)
        bound_result_record "red"
        fail "bound-test" \
          "$node is RED on the merged tree: ${tail_line:-${detail:-rc=$rc}} — the candidate does not close the finding it is bound to"
        grep '^FAILED' "$STEP_DIR/$step.out" 2>/dev/null | head -5 | sed 's/^/      /'
        step_log "$step" "$started" "failed" "$node: ${tail_line:-rc=$rc}"
        ;;
    esac
    return 0
  }

  # --- launch the two long steps CONCURRENTLY -----------------------------------
  # The ladder and the counterfeit corpus each run with isolated DBs/env, so they
  # run as concurrent subprocesses; each captures its output to a file and the
  # sections are printed in the original order below, keeping the report
  # readable and deterministic. Failure semantics are unchanged: the parent
  # scores each captured result with the exact same pass/fail messages.
  # --- reflection + selfimprove added 2026-08-07 --------------------------------
  # Until today `tests/reflection` appeared ZERO times in this file. The
  # self-modification subsystem — the only code in this repo that can rewrite
  # the repo, THIS SCRIPT INCLUDED — was the single largest ungated surface
  # here, which is how a privilege escalation survived in it: validate.py
  # hard-stop-checks `target["file"]`, while apply.py's document writer
  # resolves `target["doc"] or target["file"]`, so a proposal carrying only
  # `doc` skips the refusal gate entirely and still gets written.
  #
  # COST, measured on this Mac (serial, suite_worker's env shape, back-to-back):
  #   tests/reflection/       11.6s  (148 passed)
  #   tests/selfimprove/       1.3s  (124 passed)
  #   ladder, 5 dirs (before) 347.6s
  #   ladder, 7 dirs (after)  361.4s
  # End-to-end delta +13.8s = +4.0% wall. These FOLD INTO the existing ladder
  # rather than earning a separate concurrent step: a third background step
  # costs a process, a status file and a receipt to save ~14s, and the ladder
  # is not the gate's critical path while the counterfeit corpus runs beside
  # it. Revisit if either suite grows past ~60s.
  #
  # Parallel-safety (constraint 1 below): checked, not assumed. Neither suite
  # has a `_fixtures/` tree and neither writes into the repo working tree —
  # every write is tmp_path-derived, and the only repo-root references are
  # READS (tests/selfimprove/conftest.py copies vault/Home.md OUT). So unlike
  # tests/doctrine they are safe under `--dist loadfile`.
  #
  # Receipts: LADDER_NAME is display-only; the receipt binds $LADDER_CMD, which
  # contains $LADDER_TARGETS. Adding OR REMOVING a directory therefore
  # INVALIDATES every pre-existing ladder receipt by construction — a receipt
  # minted before this change can never be reused to skip the current target set.
  #
  # tests/scheduler LEFT THE PARALLEL LADDER (2026-08-12), and it is a COVERAGE-
  # NEUTRAL move: the exact same tests/scheduler/ now runs as its own SERIAL
  # step (`run_suite_if_present "scheduler"`, no -n) below — the treatment
  # tests/doctrine already gets — with its own command string and its own
  # receipt. Why it had to leave: tests/scheduler drives the real gate's
  # process-group reap (os.killpg) — e.g.
  # tests/scheduler/test_gate_ecosystems.py::test_a_detached_rewriter_cannot_change_the_counted_evidence.
  # Under `-n --dist loadfile` the pgroup leader can exit before a sibling
  # xdist worker signals it, so killpg returns EPERM and the run raises
  # GateExecutionInfraError("process group could not be signalled"). That
  # false-refused the WHOLE train on a failure that does NOT reproduce
  # standalone — measured on ~25% of ladder runs, every 4th train (finding
  # sha256:6334736074 / 78531c84). A serial pass has no concurrent worker for
  # the reap to race, so the EPERM cannot occur. This is NOT a suite drop: the
  # width guard (tests/scripts/test_merge_gate_suite_width.py) asserts scheduler
  # still runs and stays counted, and run_suite_if_present makes a candidate
  # that DELETES it a refusal (skipped-required), never a silent pass.
  LADDER_NAME="ladder(api,swarm,sessions,db,reflection,selfimprove)"
  LADDER_TARGETS="tests/api/ tests/swarm/ tests/sessions/ tests/db/ tests/reflection/ tests/selfimprove/"
  # --- E7 (CI port, 2026-08-05): OPT-IN ladder parallelism --------------------
  # The ladder has always run single-process. On a host with a slower single
  # thread than the Mac this gate was written on, that is the whole wall clock,
  # and it is the largest speedup available anywhere in this script.
  #
  # Three constraints are load-bearing and none of them may be relaxed:
  #  1. The flags are appended HERE, at the ladder call site, and NOWHERE else.
  #     suite_worker() is SHARED with run_suite(), which runs tests/doctrine —
  #     and doctrine's revert harness mutates tests/doctrine/_fixtures/ IN PLACE
  #     (7 cross-worker races measured under `pytest -n 8`). Putting `-n` inside
  #     suite_worker would silently parallelise doctrine and corrupt the tree.
  #  2. `--dist loadfile`, never `worksteal`. tests/conftest.py pins ONE session
  #     ledger/vault root per worker process; worksteal makes the test->worker
  #     partition nondeterministic run to run, so a green becomes unreproducible.
  #     loadfile gives a stable file->worker mapping.
  #  3. The flags go INTO $LADDER_CMD, which is the string bound into the step
  #     receipt. If they did not, a receipt from a SERIAL run would verify
  #     against a PARALLEL run (and vice versa) and one could be used to skip
  #     the other. Different execution shape, different command, different
  #     receipt.
  # `-n auto` is never accepted: Makefile:64 records that it deadlocks this
  # suite. Unset (the default, and the value on the authoring Mac) reproduces
  # today's exact serial behaviour, command string included.
  LADDER_XDIST=""
  if [ -n "${MERGE_GATE_LADDER_WORKERS:-}" ]; then
    case "$MERGE_GATE_LADDER_WORKERS" in
      ''|*[!0-9]*) refuse "bad-ladder-workers" \
        "MERGE_GATE_LADDER_WORKERS must be a positive integer, got '$MERGE_GATE_LADDER_WORKERS'" ;;
    esac
    [ "$MERGE_GATE_LADDER_WORKERS" -ge 1 ] || refuse "bad-ladder-workers" \
      "MERGE_GATE_LADDER_WORKERS must be >= 1, got '$MERGE_GATE_LADDER_WORKERS'"
    # CLAMP BEFORE THE COMMAND STRING IS BUILT. Constraint 3 above is that the
    # width the process uses is the width the step receipt claims; a clamp
    # applied after $LADDER_CMD was rendered would break exactly that.
    clamp_workers "ladder-workers" "$MERGE_GATE_LADDER_WORKERS"
    LADDER_WORKERS_EFFECTIVE="$CLAMP_RESULT"
    # `-n 1` still forks a worker process to buy no parallelism at all, so an
    # effective width of one runs genuinely serial. That is the shape every
    # nested gate now gets.
    if [ "$LADDER_WORKERS_EFFECTIVE" -ge 2 ]; then
      LADDER_XDIST="-n $LADDER_WORKERS_EFFECTIVE --dist loadfile"
    fi
  fi

  # --- counterfeit entry-pool width: VALIDATE HERE, apply below ---------------
  # Validation is hoisted to this point deliberately: `refuse` exits the script,
  # and a few lines below this the ladder is BACKGROUNDED. Refusing after that
  # `&` would orphan a pytest process holding the scratch worktree the EXIT trap
  # is about to remove. The ladder's own width validation is directly above for
  # exactly the same reason; this is its sibling and belongs beside it.
  #
  # The VALUE is not applied here — it is bound to $CF_CMD and to the harness
  # process together, inside the `tests/counterfeits/` branch below, so that an
  # unreached counterfeit step records null rather than a width it never used.
  CF_POOL_WORKERS_REQ="${MERGE_GATE_CF_POOL_WORKERS:-4}"
  case "$CF_POOL_WORKERS_REQ" in
    ''|*[!0-9]*) refuse "bad-cf-pool-workers" \
      "MERGE_GATE_CF_POOL_WORKERS must be a positive integer, got '$CF_POOL_WORKERS_REQ'" ;;
  esac
  [ "$CF_POOL_WORKERS_REQ" -ge 1 ] || refuse "bad-cf-pool-workers" \
    "MERGE_GATE_CF_POOL_WORKERS must be >= 1, got '$CF_POOL_WORKERS_REQ'"
  # Same ceiling, same reason, and clamped here rather than at the bind site so
  # $CF_CMD and the harness process are still driven by ONE variable.
  CF_POOL_WORKERS_ASKED="$CF_POOL_WORKERS_REQ"
  clamp_workers "cf-pool-workers" "$CF_POOL_WORKERS_REQ"
  CF_POOL_WORKERS_REQ="$CLAMP_RESULT"

  # --- contracts-scripts width: VALIDATE HERE, apply at the call site ---------
  # Third sibling of the two blocks above, hoisted to the same point for the
  # same reason: `refuse` exits, and the ladder is backgrounded a few lines
  # below, so a refusal after that `&` would orphan a pytest process holding the
  # scratch worktree the EXIT trap is about to remove.
  #
  # DEFAULTS TO THE LADDER'S WIDTH, deliberately, instead of introducing a knob
  # nobody sets. Every real caller already exports MERGE_GATE_LADDER_WORKERS=8
  # (pipeline/bridge/gate_loop.py:2060 and :2168, pipeline/bridge/integration.py
  # :1669); a brand-new variable would have shipped this step still serial until
  # three other files were changed to agree. MERGE_GATE_SUITE_WORKERS overrides
  # it when the two need to differ — e.g. bisecting a suspected width-sensitive
  # test without also serialising the ladder.
  # Unset BOTH and this step runs exactly as it did before, command string
  # included: absence renders as today's behaviour, never as a widened default.
  SUITE_WORKERS_REQ="${MERGE_GATE_SUITE_WORKERS:-${MERGE_GATE_LADDER_WORKERS:-}}"
  CS_XDIST=""
  if [ -n "$SUITE_WORKERS_REQ" ]; then
    case "$SUITE_WORKERS_REQ" in
      ''|*[!0-9]*) refuse "bad-suite-workers" \
        "MERGE_GATE_SUITE_WORKERS must be a positive integer, got '$SUITE_WORKERS_REQ'" ;;
    esac
    [ "$SUITE_WORKERS_REQ" -ge 1 ] || refuse "bad-suite-workers" \
      "MERGE_GATE_SUITE_WORKERS must be >= 1, got '$SUITE_WORKERS_REQ'"
    # Clamped before the command string is built — constraint 3 again. The
    # ceiling is 1 at MERGE_GATE_DEPTH >= 1, so every NESTED gate (which is what
    # tests/scripts' own fixture gates are) keeps running this step serial and
    # no test that models a nested gate has its execution shape changed.
    clamp_workers "suite-workers" "$SUITE_WORKERS_REQ"
    SUITE_WORKERS_EFFECTIVE="$CLAMP_RESULT"
    if [ "$SUITE_WORKERS_EFFECTIVE" -ge 2 ]; then
      # `--dist loadfile`, never `worksteal` — constraint 2 of the ladder's E7
      # block above: tests/conftest.py pins ONE session ledger/vault root per
      # worker, so only loadfile gives a stable file->worker map. Since
      # 2026-08-12 $CS_XDIST drives ONLY the tests/contracts leg (tests/scripts
      # was split off to a serial step for the killpg-reap race, see the call
      # site below); tests/scripts/test_refresh_contracts.py — the one file that
      # transiently rewrites the checked-out contracts/openapi.json and
      # fixture-parity.json — now runs SERIALLY and alone, so its window has no
      # concurrent reader anywhere. loadfile stays for the per-worker-root
      # reason, which is independent of that.
      CS_XDIST="-n $SUITE_WORKERS_EFFECTIVE --dist loadfile"
    fi
  fi

  # --- PRE-LADDER WORKSPACE WARM-UP (2026-08-11) ------------------------------
  # EVERY GATE RUN GETS A COLD TREE. $SCRATCH is a worktree created seconds ago,
  # so it carries no __pycache__ and none of its files have been read on this
  # host yet: the FIRST process to import each module pays both the disk read
  # and the compile. Under `-n 8 --dist loadfile` eight workers pay that toll
  # CONCURRENTLY for the same modules, and every pytest subprocess a test starts
  # pays it again from scratch.
  #
  # MEASURED on this Mac (2026-08-11), each against a `git worktree add` of this
  # repo, which is exactly the shape of $SCRATCH:
  #   compileall omniagentos + pipeline/bridge
  #     no __pycache__, COLD FILE CACHE                5.97s  (0 -> 114 dirs)
  #     no __pycache__, file cache already warm        1.05s
  #     __pycache__ present (the second run)      0.05-0.28s
  #   `python -c "import omniagentos.swarm.dal"`
  #     cold                                           0.95s
  #     against the warmed tree                   0.11-0.26s
  # The 5.97s/1.05s spread IS the effect: the number a re-measurement sees
  # depends on whether the host has read these blobs recently, and a gate box
  # that has just checked out a fresh worktree under load is the 6s case. ~6s
  # once, against a gate whose contracts-scripts step alone measured 484s
  # serial. This is not primarily a speed change: on 2026-08-10 a test's own
  # subprocess importing omniagentos.swarm.dal on a cold tree under 8-way xdist
  # on a loaded box died with "FATAL(preflight): swarm ledger CLI unusable", and
  # the gate reported that as a candidate defect.
  #
  # IT CAN NEVER FAIL THE GATE. No `fail`, no `refuse` — a status in steps[] and
  # a line on stderr, and that is the whole contract. A candidate with a syntax
  # error makes compileall exit non-zero; that finding belongs to the LADDER,
  # which names the file and the line, not to a warm-up that would refuse the
  # candidate for the same defect twice and less usefully.
  #
  # NO STEP RECEIPT, deliberately. A signed step receipt exists to SKIP work on
  # a later run; a fresh scratch worktree is never warm, so a reusable warm-up
  # receipt could only ever skip the warm-up that was needed.
  WARM_ROOT_CANDIDATES="omniagentos pipeline/bridge"
  #: A COUNT, not a presence test. The tests-own-tree probe above already ran
  #: `python -c "import omniagentos"` in $SCRATCH, which leaves EXACTLY ONE
  #: __pycache__ (measured 2026-08-11 on a fresh worktree of this repo), so
  #: `[ -d "$SCRATCH/omniagentos/__pycache__" ]` would read every genuinely COLD
  #: tree as warm and skip the warm-up in every real run. A warmed tree has 113.
  #: The floor is a stated heuristic, not a proof: at or above it, compileall
  #: would have little enough to do that the ~0.3s no-op is not worth spending.
  WARM_PYCACHE_FLOOR=20
  WARM_TIMEOUT="${MERGE_GATE_WARMUP_TIMEOUT:-300}"
  case "$WARM_TIMEOUT" in *[!0-9]*|'') WARM_TIMEOUT=0 ;; esac
  [ "$WARM_TIMEOUT" -ge 1 ] || {
    printf 'merge-gate: MERGE_GATE_WARMUP_TIMEOUT=%s is not a positive integer — using 300s\n' \
      "${MERGE_GATE_WARMUP_TIMEOUT:-<unset>}" >&2
    WARM_TIMEOUT=300
  }
  WARM_ROOTS=""
  WARM_ARGS=()
  for _wroot in $WARM_ROOT_CANDIDATES; do
    [ -d "$SCRATCH/$_wroot" ] || continue
    WARM_ROOTS="${WARM_ROOTS:+$WARM_ROOTS }$_wroot"
    WARM_ARGS+=("$SCRATCH/$_wroot")
  done
  unset _wroot
  WARM_PYC=0
  if [ -d "$SCRATCH/omniagentos" ]; then
    # `head -n $FLOOR` bounds the walk: the question is "at least FLOOR", never
    # "how many", and a full walk of a warm tree is work bought for nothing.
    WARM_PYC=$(find "$SCRATCH/omniagentos" -type d -name __pycache__ 2>/dev/null \
      | head -n "$WARM_PYCACHE_FLOOR" | wc -l | tr -d ' ')
    case "$WARM_PYC" in ''|*[!0-9]*) WARM_PYC=0 ;; esac
  fi
  WARM_STARTED=$(utc_now)
  WARM_T0=$(date +%s 2>/dev/null); case "$WARM_T0" in ''|*[!0-9]*) WARM_T0="" ;; esac
  if [ -z "$WARM_ROOTS" ]; then
    note "workspace-warmup" "skipped — no warm root in the merged tree ($WARM_ROOT_CANDIDATES)"
    step_log "workspace-warmup" "$WARM_STARTED" "skipped" \
      "no warm root in the merged tree: $WARM_ROOT_CANDIDATES"
  elif [ "$WARM_PYC" -ge "$WARM_PYCACHE_FLOOR" ]; then
    note "workspace-warmup" "skipped — already warm ($WARM_PYC+ __pycache__ under omniagentos)"
    step_log "workspace-warmup" "$WARM_STARTED" "skipped-warm" \
      "$WARM_PYC+ __pycache__ under omniagentos (floor $WARM_PYCACHE_FLOOR)"
  else
    warmup_worker "$STEP_DIR/warmup.out" "$STEP_DIR/warmup.status"
    WARM_RC=$?
    WARM_ELAPSED=""
    if [ -n "$WARM_T0" ]; then
      WARM_T1=$(date +%s 2>/dev/null)
      case "$WARM_T1" in ''|*[!0-9]*) WARM_T1="" ;; esac
      [ -n "$WARM_T1" ] && WARM_ELAPSED=$((WARM_T1 - WARM_T0))
    fi
    WARM_CMD="python -m compileall -q $WARM_ROOTS"
    WARM_COST="${WARM_ELAPSED:-?}s"
    if [ "$WARM_RC" -eq 0 ]; then
      note "workspace-warmup" "ok — $WARM_ROOTS in $WARM_COST"
      step_log "workspace-warmup" "$WARM_STARTED" "ok" "$WARM_CMD — $WARM_COST"
    else
      # LOUD, and explicitly NOT an accusation. The first line of the child's
      # own output is quoted so the next reader does not have to reproduce it;
      # $SCRATCH (and warmup.out with it) is gone by the time anyone looks.
      WARM_HEAD=$(head -n 3 "$STEP_DIR/warmup.out" 2>/dev/null | tr '\n' ' ' | cut -c1-200)
      if [ "$WARM_RC" -eq 124 ]; then
        printf 'merge-gate: workspace warm-up hit its own %ss bound and was killed — NOT a candidate defect; the gate continues on a cold tree\n' \
          "$WARM_TIMEOUT" >&2
        note "workspace-warmup" "bound-exhausted after ${WARM_TIMEOUT}s — not fatal, ladder runs cold"
        step_log "workspace-warmup" "$WARM_STARTED" "bound-exhausted" \
          "$WARM_CMD killed at ${WARM_TIMEOUT}s (instrument bound, never a verdict): $WARM_HEAD"
      else
        printf 'merge-gate: workspace warm-up FAILED rc=%s — NOT a candidate defect; the gate continues on a cold tree: %s\n' \
          "$WARM_RC" "${WARM_HEAD:-<no output>}" >&2
        note "workspace-warmup" "failed rc=$WARM_RC — not fatal, ladder runs cold"
        step_log "workspace-warmup" "$WARM_STARTED" "failed-nonfatal" \
          "$WARM_CMD exited $WARM_RC in $WARM_COST (never a verdict): $WARM_HEAD"
      fi
    fi
  fi

  LADDER_CMD="python -m pytest -q${LADDER_XDIST:+ $LADDER_XDIST} $LADDER_TARGETS"
  LADDER_PID="" LADDER_STARTED="" LADDER_REUSE=""
  if ! LADDER_REUSE=$(verify_step_receipt "ladder" "$LADDER_CMD"); then
    LADDER_REUSE=""
    LADDER_STARTED=$(utc_now)
    # shellcheck disable=SC2086 -- LADDER_XDIST/LADDER_TARGETS are deliberate word lists
    suite_worker "$STEP_DIR/ladder.out" "$STEP_DIR/ladder.status" $LADDER_XDIST $LADDER_TARGETS &
    LADDER_PID=$!
  fi

  # CF_CMD and CF_POOL_WORKERS are deliberately EMPTY/unset until the step is
  # known to exist. $CF_CMD is only ever read inside a `CF_PRESENT -eq 1` block,
  # and $CF_POOL_WORKERS only by counterfeit_worker (reached only via
  # CF_DEFERRED, which is likewise set only inside that branch).
  # THE SIBLING WITH THE DIFFERENT SHAPE. This is an `if`/`then` with no `else`
  # setting a VARIABLE that two later blocks read, so a fix aimed only at the
  # `[ -d ... ] &&` lines above leaves the ENTIRE counterfeit corpus — the check
  # that proves this suite can detect its own realistic failure — silently
  # skippable by deleting one directory. The missing/required lists are captured
  # HERE because $SUITE_MISSING is clobbered by the guarded suites that run
  # between this presence test and the two consumption points below.
  CF_CMD="" CF_PRESENT=0 CF_PID="" CF_STARTED="" CF_REUSE=""
  CF_MISSING="" CF_REQUIRED=""
  if suite_dirs_present tests/counterfeits; then
    CF_PRESENT=1
    # BIND THE POOL WIDTH INTO THE COMMAND STRING. The ladder already does this
    # for exactly one reason, spelled out in constraint 3 above: $CF_CMD is the
    # key the step receipt is stored and verified under, so if the width did not
    # appear in it a receipt minted from a SERIAL run would verify a WIDE run
    # (and vice versa) and one could be used to skip the other. Different
    # execution shape, different command, different receipt.
    #
    # BIND-THEN-ENABLE, both halves now landed. The bind landed first (when the
    # default was still 1/serial) because bind-without-enable is inert while
    # enable-without-bind is the defect that ordering existed to prevent. The
    # enable is VEL-E1: default 4, argued and landed only after the d614
    # parallel-unsafe fixture family was repaired on main (wave-2, 9bb240bbd).
    #
    # The harness has no width FLAG — width is env-only (POOL_WORKERS_ENV in
    # tests/counterfeits/harness.py) — so the env assignment is rendered into
    # the string exactly as counterfeit_worker applies it to the process, and
    # the two are edited as a pair from this ONE variable. It is bound
    # UNCONDITIONALLY rather than only when non-default because counterfeit_worker's
    # `env` scrubs OMNIAGENTOS_GATE_WORKSPACE but not this: an ambient
    # OMNIAGENTOS_CF_POOL_WORKERS from the operator's shell would otherwise set
    # the real width while the receipt claimed nothing at all. That is why the
    # command string changes for every run (old serial step receipts stop
    # matching and their step re-runs once) — a receipt key that omits an input
    # the run actually consumed is not a key.
    CF_POOL_WORKERS="$CF_POOL_WORKERS_REQ"
    CF_CMD="OMNIAGENTOS_CF_POOL_WORKERS=$CF_POOL_WORKERS python -m tests.counterfeits.harness"
    if ! CF_REUSE=$(verify_step_receipt "counterfeit-gate" "$CF_CMD"); then
      CF_REUSE=""
      CF_STARTED=$(utc_now)
      CF_DEFERRED=1
    fi
  else
    CF_MISSING="$SUITE_MISSING"
    CF_REQUIRED="$SUITE_REQUIRED"
  fi

  [ -n "$LADDER_PID" ] && wait "$LADDER_PID"

  # SEQUENTIAL, NOT CONCURRENT. The counterfeit harness MUTATES FILES IN
  # $SCRATCH in place to prove detection (see the doctrine-fixture note below);
  # the ladder reads that same tree. Running them together made the harness's
  # own control run (must_fail unpatched, expected green) go red — the gate then
  # refused the CANDIDATE for a defect in its own instrument. Measured
  # 2026-08-03: 5/5 gate runs refused counterfeit, while 7/7 isolated runs of
  # the identical harness on the identical merge passed 85/85 GREEN, including
  # runs with the gate's own interpreter and per-worker DB/var/ledger isolation.
  # Per-worker env isolation was necessary but NOT sufficient, because the
  # shared state here is the source tree itself. Cost: the harness's wall time
  # is no longer hidden behind the ladder. Nothing it asserts changes.
  # RESTORE-THEN-ADJUDICATE. counterfeit_worker now runs in the FOREGROUND, so a
  # harness that dies (OOM, signal, non-zero under set -e) would abort the script
  # before the doctrine-fixture restore below and leave tests/doctrine/_fixtures/
  # mutated in the shared tree — every later gate run would then false-REFUSE
  # doctrine, silently, while looking like it was catching counterfeits. Swallow
  # the worker's status here; the real verdict is scored from counterfeit.status
  # and counterfeit.out further down, exactly as before.
  if [ "${CF_DEFERRED:-0}" = "1" ]; then
    counterfeit_worker "$STEP_DIR/counterfeit.out" "$STEP_DIR/counterfeit.status" || true
    # Harness-reported width for the run receipt: independent evidence of the
    # width the harness itself resolved. ANCHORED to the exact receipt line
    # format_receipt_line() emits (harness.py: `pool_workers={w}  entry_timeout=`,
    # line-initial, two spaces) — an unanchored match would hit the timing
    # prose "= wall clock only at pool_workers=1)" first and record width 1 on
    # a healthy width-4 run. First match only; empty (-> null) when absent.
    CF_POOL_WORKERS_HARNESS=$(sed -n 's/^pool_workers=\([0-9]\{1,\}\)  entry_timeout=.*/\1/p' "$STEP_DIR/counterfeit.out" 2>/dev/null | head -n 1)
  fi

  # The counterfeit harness runs `cd "$SCRATCH"` and mutates shared doctrine
  # fixtures (tests/doctrine/_fixtures/) in place to prove detection. doctrine
  # runs AFTER this join (below), so without a restore it sees the counterfeit
  # residue and false-REFUSES — the reordering that concurrency introduced.
  # The counterfeit verdict is already captured in counterfeit.out before the
  # join, so restoring here cannot affect its scoring. Fail-safe: a failed
  # restore only reproduces the (strict, never lax) false-refuse.
  git -C "$SCRATCH" checkout -- tests/doctrine/_fixtures/ 2>/dev/null || true

  # --- the CONTROL's own bound is an INSTRUMENT limit, not a corpus verdict -----
  # ADJUDICATED HERE, ahead of every remaining step, because a run that never
  # established its baseline has no verdict to give and the box it is holding is
  # the resource the exhaustion is evidence about: finishing ~90s of
  # contracts-scripts to arrive at the same park spends exactly what was scarce.
  # It costs the ladder's own capture, which is the accepted price of a park.
  #
  # OUTSIDE the scoring block below on purpose — that block is an anchor
  # substituted byte-for-byte by tests/scripts/test_merge_gate_m8_refusals.py and
  # patched by tests/counterfeits/patches/cf-merge-gate-trusts-summary-over-exit-
  # code.patch, and editing inside it would silently disarm both. Same reason the
  # counterfeit step_log lives outside it.
  if [ "${CF_DEFERRED:-0}" = "1" ] &&
     cf_control_bound_exhausted \
       "$(sed -n '1p' "$STEP_DIR/counterfeit.status" 2>/dev/null)" \
       "$STEP_DIR/counterfeit.out"; then
    # sed, not grep, and for the same reason the harness-width extraction two
    # blocks up uses sed: this builds the HUMAN MESSAGE for a refusal that
    # cf_control_bound_exhausted has already settled. A capture whose grep
    # status is never examined is the classifier defect this repo keeps
    # regrowing (tests/scripts/test_merge_gate_classifier_rc.py pins the shape);
    # `sed -n p` has no such status to swallow, and an empty capture degrades
    # the sentence rather than the ruling — the `${CF_BOUND_LINE:-...}` defaults
    # below are what make that true.
    CF_BOUND_LINE=$(sed -n '/^control (unpatched) timed out after/p' \
      "$STEP_DIR/counterfeit.out" 2>/dev/null | head -n 1 | cut -c1-240)
    # KEEP THE CAPTURE. $SCRATCH goes with the EXIT trap, and a parked run whose
    # evidence was deleted is a run nobody can diagnose — the same reason the
    # refusal path below keeps its copy. Best-effort by design: a failed copy
    # must never change the outcome.
    CF_KEPT="$EVIDENCE_ROOT/records/merge-gate/${CANDIDATE_SHA}.counterfeit-$(printf '%s' "$RUN_STARTED_AT" | tr -d ':-')-$$.out"
    if mkdir -p "$(dirname "$CF_KEPT")" 2>/dev/null &&
       cp "$STEP_DIR/counterfeit.out" "$CF_KEPT" 2>/dev/null; then
      printf 'merge-gate: counterfeit capture kept at %s\n' "$CF_KEPT" >&2
    fi
    # `note`, never `fail`: FAILURES is the CANDIDATE-DEFECT carrier and this is
    # not one. The step's own status is "instrument-failure" rather than
    # "failed" so no consumer of steps[] can read it as a corpus verdict.
    note "counterfeit-gate" "INSTRUMENT — ${CF_BOUND_LINE:-control bound exhausted}"
    step_log "counterfeit-gate" "${CF_STARTED:-$(utc_now)}" "instrument-failure" \
      "${CF_BOUND_LINE:-control (unpatched) exhausted its bound}"
    step_begin "counterfeit-control-bound"
    refuse "counterfeit-control-timeout" \
"the counterfeit CONTROL (the unpatched must_fail baseline) exhausted its own bound before a single corpus entry was scored: ${CF_BOUND_LINE:-<the harness printed no bound line>}. That is this gate running out of time on a loaded box, NOT a verdict about $BRANCH — nothing here says the candidate is defective, and its members must not be rejected on it. REMEDY: re-run this candidate when the box is quieter, or raise the control bound (OMNIAGENTOS_CF_ENTRY_TIMEOUT governs entries; run_control's own 300s bound is in tests/counterfeits/harness.py). Kept capture: ${CF_KEPT:-<none>}."
  fi

  # --- 1. the ladder, ON THE MERGE COMMIT ---------------------------------------
  LADDER_FAILS_BEFORE=${#FAILURES[@]}
  if [ -n "$LADDER_REUSE" ]; then
    pass "$LADDER_NAME" "$LADDER_REUSE"
    step_log "ladder" "$(utc_now)" "reused" "$LADDER_REUSE"
  else
    report_suite "$LADDER_NAME" "ladder" "$LADDER_CMD" "$LADDER_STARTED" \
      "$STEP_DIR/ladder.out" "$STEP_DIR/ladder.status"
    # E9 (2026-08-06): keep the ladder capture on refusal, exactly as the
    # counterfeit step already does below. `report_suite` greps `^FAILED` to
    # stdout, so a ladder refusal NAMES the test and then the EXIT trap deletes
    # $SCRATCH with the assertion, the traceback and the worker assignment still
    # inside it. That asymmetry is why the largest measured refusal class on
    # this gate is undiagnosed: 13 ladder refusals over three days, ~9,343s of
    # gate wall, and ZERO ladder captures on disk against two counterfeit ones.
    # It cost four reproduction attempts tonight (isolation, -n 8, --dist
    # loadfile, and a concurrent counterfeit run) that all came back green,
    # because the one artifact naming the cause had already been deleted.
    # Same contract as the counterfeit capture: refusal-only, so a green run
    # adds nothing; best-effort, so a failed copy can never change a verdict;
    # retention unbounded like the .run-*.json receipts it sits beside.
    if [ "${#FAILURES[@]}" -gt "$LADDER_FAILS_BEFORE" ]; then
      LADDER_KEPT="$EVIDENCE_ROOT/records/merge-gate/${CANDIDATE_SHA}.ladder-$(printf '%s' "$RUN_STARTED_AT" | tr -d ':-')-$$.out"
      if mkdir -p "$(dirname "$LADDER_KEPT")" 2>/dev/null &&
         cp "$STEP_DIR/ladder.out" "$LADDER_KEPT" 2>/dev/null; then
        printf 'merge-gate: ladder capture kept at %s\n' "$LADDER_KEPT" >&2
      fi
    fi
  fi
  # --- scheduler: SERIAL, lifted OUT of the xdist ladder (2026-08-12) ----------
  # tests/scheduler drives the real gate's os.killpg process-group reap. Under
  # the parallel ladder (`-n --dist loadfile`) a sibling xdist worker could exit
  # before the pgroup leader signalled it -> EPERM ->
  # GateExecutionInfraError("process group could not be signalled"), and the
  # whole train false-refused on a failure that never reproduced standalone
  # (~25% of runs, finding sha256:6334736074 / 78531c84). Run serially here and
  # there is no second worker for the reap to race, so the EPERM cannot occur.
  #
  # COVERAGE-NEUTRAL and it is its OWN reported step: run_suite_if_present takes
  # no `-x`, so $LADDER_CMD's xdist flags never reach it — the command string
  # run_suite binds into this step's receipt is the SERIAL "python -m pytest -q
  # tests/scheduler/", which can never verify against the parallel ladder
  # receipt this suite used to be part of (constraint 3). Nothing is dropped:
  # the SAME tests/scheduler/ that left the ladder runs here, and a candidate
  # that DELETES it is a refusal (skipped-required), never a silent pass. This
  # is the treatment tests/doctrine already gets, and the width guard
  # (tests/scripts/test_merge_gate_suite_width.py) pins that it stays serial and
  # counted.
  run_suite_if_present "scheduler" "scheduler" tests/scheduler
  # --- contracts (PARALLEL) + scripts (SERIAL) --------------------------------
  # These directories must be gate-covered — the instrument regressions in this
  # train live in tests/scripts/. Until 2026-08-12 they ran as ONE step,
  # "contracts-scripts", under the xdist width $CS_XDIST. tests/scripts is now
  # SPLIT OFF into its own SERIAL step for the SAME reason tests/scheduler left
  # the ladder: its 13 merge-gate suites spawn the real merge-gate.sh, which
  # drives the os.killpg reap, and under `-n` a sibling worker exiting first
  # gives the pgroup leader an EPERM and false-refuses the train. tests/contracts
  # drives no such reap, so it KEEPS the parallel width; tests/scripts runs
  # serial, with its own command string and receipt.
  #
  # WHY tests/contracts STILL PARALLELISES. `test_refresh_contracts.py`
  # transiently rewrites the checked-out contracts/openapi.json and
  # contracts/fixtures/mission/v1/fixture-parity.json (restoring them in a
  # fixture `finally`) — but that file lives in tests/scripts/, which now runs
  # SERIALLY and alone, so its transient rewrites have no concurrent reader at
  # all. Enumerated 2026-08-10: it is the ONLY file in either directory that
  # touches the repo-root contracts/ artifacts (test_openapi_drift_check.py and
  # test_merge_gate_openapi_drift.py build their own trees under tmp_path). So
  # the tests/contracts leg has no reader of that window even in principle.
  # `--dist loadfile` (never worksteal) STAYS on that leg regardless, for
  # constraint 2: tests/conftest.py pins ONE session ledger/vault root per
  # worker, so only loadfile gives a stable file->worker map.
  #
  # COST, measured on this Mac 2026-08-10, suite_worker's env shape, same window
  # (the box was also running other lanes, so read the RATIO, not the absolutes):
  #   both dirs, serial              484s   (893 passed, 1 skipped)
  #   both dirs, -n 8 --dist loadfile 93s / 91s   2 consecutive runs
  # Splitting tests/scripts back to serial gives back some of that ~390s win on
  # the tests/scripts subset; correctness (no false-refused train) beats it.
  #
  # RECEIPTS: $CS_XDIST goes into the contracts command string, and the scripts
  # step's serial command string is a DIFFERENT key, so neither can reuse the
  # other's receipt and no pre-2026-08-12 "contracts-scripts" receipt matches
  # either. A serial receipt must never certify a parallel run, and vice versa.
  #
  # Each directory is its own step now, so deleting either one is caught as that
  # step's skipped-required refusal rather than turning a shared step off.
  run_suite_if_present -x "$CS_XDIST" "contracts" "contracts" tests/contracts
  run_suite_if_present "scripts" "scripts" tests/scripts
  # Converged pipeline tests live with their imported history under pipeline/.
  # This step must land BEFORE the subtree migration: while pipeline/tests is
  # absent on pinned main it records an honest skip; once present, deleting or
  # bypassing it is a refusal and every pipeline-critical one-member train is
  # judged by the previously pinned gate rather than by its candidate copy.
  run_suite_if_present "pipeline-tests" "pipeline-tests" pipeline/tests
  # --- 3. dominance corpus ------------------------------------------------------
  run_suite_if_present "dominance-corpus" "dominance-corpus" tests/objective
  # --- doctrine + memlife contracts ---------------------------------------------
  run_suite_if_present "doctrine" "doctrine" tests/doctrine
  run_suite_if_present "memlife" "memlife" tests/memlife
  # --- 13. the bound failing test must be GREEN on the merged tree ------------
  # ONE STEP PER BINDING, so a train of N members yields N gradeable steps and a
  # single silently-dropped binding cannot hide behind another member's green.
  # The step id carries the index because a step receipt is keyed on (step id,
  # command): two bindings sharing one id would let one member's receipt satisfy
  # another member's step.
  #
  # `</dev/null` on the CALL, not inside the worker: the loop's own stdin IS the
  # heredoc below, and a child that reads stdin would eat the remaining node ids
  # and silently grade a train of N as a train of 1.
  if [ -n "$BOUND_TESTS" ]; then
    _bt_i=0
    while IFS= read -r BOUND_TEST; do
      [ -n "$BOUND_TEST" ] || continue
      _bt_i=$((_bt_i + 1))
      run_bound_test "bound-test-$_bt_i" "$BOUND_TEST" </dev/null
    done <<EOF
$BOUND_TESTS
EOF
  fi
  CF_FAILS_BEFORE=${#FAILURES[@]}
  # --- 2. counterfeit corpus (opt-in marker) ------------------------------------
  # Run the harness CLI (not bare pytest): pytest captures stdout on green runs, so
  # format_report's `total=N caught=M survived=K` line never reaches the gate and a
  # fully-passing corpus was scored as "NO verdict". Matches `make counterfeit-gate`.
  #
  # PRIMARY SIGNAL: the harness's measured EXIT CODE (harness.py: 1 when any entry
  # was not caught, 2 when the corpus would not load). The report line is kept as
  # the human-readable verdict and as a CROSS-CHECK that must AGREE with the code —
  # text produced by the tree under judgement is the weakest signal available, and
  # a disagreement (either direction) is itself a refusal. Both are judged by ONE
  # rule, shared with the receipt path, in gate_evidence.py.
  if [ "$CF_PRESENT" -eq 1 ]; then
    if [ -n "$CF_REUSE" ]; then
      pass "counterfeit-gate" "$CF_REUSE"
    else
      CF_RC=$(sed -n '1p' "$STEP_DIR/counterfeit.status" 2>/dev/null)
      CF_VERDICT=""
      if [ -z "$CF_RC" ]; then
        fail "counterfeit-gate" "harness produced NO exit status (worker died) — an instrument that did not run is NOT a pass"
      elif CF_VERDICT=$(judge_counterfeit "$CF_RC" "$STEP_DIR/counterfeit.out"); then
        pass "counterfeit-gate" "$CF_VERDICT"
        record_step_receipt "counterfeit-gate" "$CF_CMD" "$STEP_DIR/counterfeit.out" \
          "$CF_VERDICT" "$CF_STARTED" "$CF_RC"
      else
        fail "counterfeit-gate" "${CF_VERDICT:-rc=$CF_RC and no judgeable verdict}"
      fi
    fi
  fi
  # CONSUMPTION POINT 1 of 2 for CF_PRESENT=0 — the human + exit-code carrier.
  # A SEPARATE `if`, not an `else` on the block above, and deliberately so:
  # tests/counterfeits/patches/cf-merge-gate-trusts-summary-over-exit-code.patch
  # carries `      fi` / `    fi` / `  fi` as its trailing context, so turning
  # that last `fi` into an `else` makes the patch fail to apply — which the
  # harness treats as a HARD ERROR (a bit-rotted entry, never a skip) and every
  # gate run would then refuse on its own instrument. Verified with
  # `git apply --check` on both merge-gate patches.
  if [ "$CF_PRESENT" -eq 0 ]; then
    suite_skip_verdict "counterfeit-gate" "$CF_MISSING" "$CF_REQUIRED"
  fi

  # The counterfeit verdict is logged to steps[] from OUTSIDE the scoring block
  # on purpose. That block is an anchor: tests/scripts/test_merge_gate_m8_refusals.py
  # substitutes it byte-for-byte to prove the pre-fix summary-only scoring would
  # still pass a harness that exited 1, and
  # tests/counterfeits/patches/cf-merge-gate-trusts-summary-over-exit-code.patch
  # applies over the same lines. Editing inside it would silently disarm both.
  # Status therefore comes from the FAILURES delta, not from re-judging.
  #
  # THE REFUSAL DETAIL IS THE ONLY THING THAT SURVIVES. $SCRATCH — and with it
  # counterfeit.out — is removed by the EXIT trap, so a receipt that recorded
  # the constant "counterfeit corpus refused" left every refused run
  # undiagnosable after the fact; that is how ~8 in-gate control failures on
  # 2026-08-04/05 had to be re-derived by hand. $CF_VERDICT already holds the
  # judge's REFUSED text (judge_counterfeit captures 2>&1 on both branches), and
  # judge-counterfeit now carries the harness's first REAL diagnostic line, not
  # just its "COUNTERFEIT GATE CONTROL FAILED:" header. step_log truncates to
  # 400 chars, which is why the diagnostic is capped at the same width.
  if [ "$CF_PRESENT" -eq 1 ]; then
    if [ "${#FAILURES[@]}" -gt "$CF_FAILS_BEFORE" ]; then
      # A one-line detail names the defect; it cannot show the pytest excerpt
      # under it. Copy the instrument's FULL capture into the durable evidence
      # store beside the run receipt, before the EXIT trap deletes $SCRATCH.
      # Refusal-only, so a green run adds nothing. Every consumer looks receipts
      # up by exact path, so an added artifact class is inert to them. Failing
      # to copy must never change the verdict: this is best-effort by design.
      # RETENTION: unbounded, exactly like the .run-*.json receipts beside it —
      # one file per REFUSED counterfeit step, never pruned. Named here so the
      # growth is a known open item and not a discovery; a retention/rotation
      # policy for this store (both artifact classes together, since they share
      # the directory and the candidate-SHA naming) is future work.
      CF_KEPT="$EVIDENCE_ROOT/records/merge-gate/${CANDIDATE_SHA}.counterfeit-$(printf '%s' "$RUN_STARTED_AT" | tr -d ':-')-$$.out"
      if mkdir -p "$(dirname "$CF_KEPT")" 2>/dev/null &&
         cp "$STEP_DIR/counterfeit.out" "$CF_KEPT" 2>/dev/null; then
        printf 'merge-gate: counterfeit capture kept at %s\n' "$CF_KEPT" >&2
      fi
      step_log "counterfeit-gate" "${CF_STARTED:-$(utc_now)}" "failed" \
        "counterfeit corpus refused${CF_VERDICT:+ — $CF_VERDICT}"
    elif [ -n "$CF_REUSE" ]; then
      step_log "counterfeit-gate" "${CF_STARTED:-$(utc_now)}" "reused" "$CF_REUSE"
    else
      step_log "counterfeit-gate" "${CF_STARTED:-$(utc_now)}" "ok" "${CF_VERDICT:-}"
    fi
  fi
  # CONSUMPTION POINT 2 of 2 — the steps[]/receipt carrier. Both are needed: a
  # skip that reaches the printed report but not the receipt is still invisible
  # to every machine consumer, and vice versa. Same separate-`if` shape as its
  # sibling above, for the same patch-context reason.
  if [ "$CF_PRESENT" -eq 0 ]; then
    suite_skip_step "counterfeit-gate" "$CF_MISSING" "$CF_REQUIRED"
  fi

  # --- 6. lint against CURRENT main, never a remembered number ------------------
  # grep -c exits 1 on zero matches, so `|| echo 0` would APPEND a second line and
  # make this "20\n0" — which then fails the integer test silently-ish. Count with
  # awk, which always emits exactly one number.
  #
  # THE ONE-LINE CORRECTNESS HALF OF THIS PACKAGE. `cd "$REPO"` here used to be
  # the SHARED checkout that ~30 worktrees and every interactive session write
  # to, so a peer's uncommitted lint errors inflated BASE and MASKED a real
  # regression: ambient state producing a false PASS. Under MERGE_GATE_PINNED=1
  # BASE is the count already measured in the one-writer pinned workspace
  # ($GATE_WS) before any suite ran — the same number --print-ruff-base
  # reports, by construction, because it is literally the same variable.
  #
  # E2 (CI port, 2026-08-05): both counts now go through ruff_count_in(), which
  # prints NOTHING rather than a confident 0 when it could not measure. The
  # unpinned branch used the raw `| awk` form, so a blinded interpreter here
  # produced BASE=0 NEW=0 and this step reported "ok — 0 -> 0" having compared
  # two absences. An unproducible count is a FAILURE of this step.
  step_begin "ruff-vs-current-main"
  if ruff_available; then
    if [ "$PINNED" = "1" ] && [ -n "$RUFF_BASE" ]; then
      BASE="$RUFF_BASE"
    else
      BASE=$(ruff_count_in "$REPO")
      RUFF_BASE="$BASE"
    fi
    NEW=$(ruff_count_in "$SCRATCH")
  else
    # ruff absent exits 1 — indistinguishable from "findings exist" by exit code
    # alone, which is why this positive probe exists. Blank both counts so the
    # branch below refuses instead of comparing 0 to 0.
    BASE="" NEW=""
  fi
  RUFF_NEW="$NEW"
  if [ -z "$BASE" ] || [ -z "$NEW" ]; then
    fail "ruff-vs-current-main" "ruff could not produce a count (base='${BASE:-<none>}' new='${NEW:-<none>}')"
    step_end "failed" "unproducible count"
  elif [ "$NEW" -gt "$BASE" ]; then
    fail "ruff-vs-current-main" "$BASE -> $NEW"
    step_end "failed" "$BASE -> $NEW"
  else
    pass "ruff-vs-current-main" "$BASE -> $NEW"
    step_end "ok" "$BASE -> $NEW"
  fi
fi

# --- tip stability: never approve a branch name whose tip has moved ----------
# Post-checks above intentionally used $CANDIDATE_SHA (the tip authenticated by
# the signed receipt). Re-resolve $BRANCH here so a retarget after verification
# cannot earn "PASS — <branch> is safe to merge" for an unverified tip. A later
# merge by branch name would otherwise consume different content than the
# receipt and trial-merge examined. Runs even when merge-clean failed so a
# moved tip is never silent.
TIP_NOW=$(git rev-parse --verify "$BRANCH^{commit}" 2>/dev/null) || TIP_NOW=""
if [ -z "$TIP_NOW" ]; then
  fail "candidate-tip-stable" "branch $BRANCH disappeared after verification"
elif [ "$TIP_NOW" != "$CANDIDATE_SHA" ]; then
  fail "candidate-tip-stable" \
    "branch tip moved after verification: verified $CANDIDATE_SHA now $TIP_NOW"
else
  pass "candidate-tip-stable" "$CANDIDATE_SHA"
fi

# REACHABILITY — a public symbol this branch adds that no production path calls.
#
# "Built, tested, never wired" is this repo's signature defect, found twelve times. The last
# three instances each cost a full aggregate-review cycle, and every one was obvious to a
# caller search — which makes it a MEASUREMENT, not a judgement, and measurements belong in
# a gate rather than in a 25-minute review a model has to remember to perform.
#
# Validated against the corpus before wiring: 22 lanes clean, 2 refused, and those 2 are
# exactly the lanes the Opus aggregate reviewer had already rejected, at the same file:line.
if [ "$REACH_DONE" -eq 1 ]; then
  # Already run, and already refused if it had anything to say — sub-second work
  # does not belong behind twelve minutes of suites.
  pass "reachability" "every new public symbol has a production caller (hoisted)"
elif REACH=$("$PY" "$REPO/scripts/reachability-gate.py" "$BRANCH" main 2>&1); then
  pass "reachability" "every new public symbol has a production caller"
else
  RC=$?
  if [ "$RC" -eq 2 ]; then
    # The probe could not run. That is unknown, and unknown is a refusal, never a pass.
    fail "reachability" "gate could not run — refusing rather than assuming reachable"
  else
    # SIBLING CALL SITE. This is the non-hoisted path (REACH_DONE=0, i.e. any
    # run that is not MERGE_GATE_PINNED=1); it must explain the exemption trap
    # identically or the fix reaches one of its two carriers only — and this is
    # the path most un-pinned runs actually take. Base ref here is $BRANCH,
    # matching the probe invocation directly above.
    REACH_DETAIL=$(printf '%s' "$REACH" | grep -E '^[[:space:]]+omniagentos/' | tr '\n' ';' | cut -c1-200)
    if REACH_TRAP=$(reach_exempt_trap "$BRANCH" "$REACH"); then
      reach_exempt_explain "${REACH_TRAP#*|}" "${REACH_TRAP%%|*}"
      REACH_DETAIL="exempt-on-branch-not-in-gate-checkout(${REACH_TRAP%%|*}) [${REACH_TRAP#*|}] — land the exemption on main first; $REACH_DETAIL"
    fi
    fail "reachability" "$REACH_DETAIL"
  fi
fi

if [ "${#FAILURES[@]}" -eq 0 ]; then
  echo "MERGE GATE: PASS — $BRANCH is safe to merge"
  mint_run_receipt 0 ""
  exit 0
fi
printf '\nMERGE GATE: REFUSED (%d)\n' "${#FAILURES[@]}"
printf '  - %s\n' "${FAILURES[@]}"
mint_run_receipt 1 "$(printf '%s; ' "${FAILURES[@]}" | cut -c1-800)"
exit 1
