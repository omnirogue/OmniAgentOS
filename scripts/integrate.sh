#!/usr/bin/env bash
# THE INTEGRATION STAGE — the pipeline's missing back half.
#
# Fable's finding: draining the verdict queue was the wrong priority #1 "because the
# pipeline's back half does not exist." It was right, and the gap was mine. I built
# coder lanes -> sol review -> verdict, and then merged by hand — which is the same
# "operator is the scheduler" failure one stage later. Verdicts accumulated and nothing
# landed them.
#
# The stage, per its spec:
#   1. read var/swarm/sol-verdicts/*.md, partition APPROVE / REJECT / FAILED
#   2. CONFLICT FORECAST across approved lanes' changed paths — nothing currently checks
#      whether two lanes touch the same file, and a silent conflict at merge time is how
#      seam defects enter
#   3. land approved lanes, merge to integration/reviewed
#   4. ONE Anthropic verdict on the AGGREGATE (not per lane)
#   5. RECORD the approval as data — `merge-gate.sh` exiting 0 leaves no trace, and an
#      unrecorded approval is a non-result rendered favourable at audit time
#   6. merge-gate.sh to main
#
# Nothing here merges to main by itself: step 6 is reported, not executed, because the
# last hop is the operator's call.
set -uo pipefail
# Shared verdict-line grammar. Resolved before `cd`, and fail-CLOSED: an unset
# VERDICT_LINE_RE is an empty pattern, which would match the first line of every
# verdict file — i.e. its title — which is exactly the defect this replaces.
_VERDICT_GRAMMAR="$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)/lib/verdict-grammar.sh"
# shellcheck source=scripts/lib/verdict-grammar.sh
# Loaded AND non-empty: a truncated library would leave the patterns empty, and an
# empty grep pattern matches every line — the most fail-open state available here.
. "$_VERDICT_GRAMMAR" 2>/dev/null && [ -n "${VERDICT_LINE_RE:-}" ] && [ -n "${VERDICT_APPROVE_RE:-}" ] \
  && command -v verdict_decision >/dev/null 2>&1 || {
  echo "FATAL: verdict grammar unusable ($_VERDICT_GRAMMAR)" >&2; exit 1; }
REPO="${REPO:-/Users/youruser/OmniAgentOS}"
cd "$REPO"
PY="$REPO/.venv/bin/python"
STAGE="$REPO/var/swarm/sol-verdicts"
LEDGER="$REPO/var/swarm/integration-ledger.jsonl"
BRANCH="${INTEGRATION_BRANCH:-integration/reviewed}"
DRY="${DRY_RUN:-0}"

say() { printf '  %s\n' "$*"; }
record() {  # event, detail  — approvals and refusals are DATA, not console output
  "$PY" - "$1" "$2" "$BRANCH" <<'PY' >> "$LEDGER"
import json, sys, time
event, detail, branch = sys.argv[1:4]
print(json.dumps({"ts": int(time.time()), "branch": branch, "event": event, "detail": detail}))
PY
}

# ---- 1. partition -----------------------------------------------------------
# TWO verdict sources, both authoritative:
#   var/swarm/sol-verdicts/   first-pass, gpt-5.6-sol, keyed by LANE name
#   var/swarm/verdicts/       anthropic, keyed by BRANCH name (slashes -> underscores)
# Reading only the first silently skipped every lane verified on the older path — an
# absent verdict file read as "not ready" when the verdict existed under another name.
APPROVED=(); REJECTED=(); FAILED=()
for f in "$STAGE"/*.md "$REPO"/var/swarm/verdicts/*.md; do
  [ -f "$f" ] || continue
  B=$(basename "$f" .md)
  case "$B" in README|integration_*) continue;; esac
  # Map an anthropic verdict (branch-keyed) back to its lane by matching HEAD.
  LANE="$B"
  if [ ! -d "$REPO/var/swarm/clones/$B" ]; then
    LANE=""
    for d in "$REPO"/var/swarm/clones/*/; do
      [ -d "$d/.git" ] || continue
      bn=$(git -C "$d" rev-parse --abbrev-ref HEAD 2>/dev/null | tr '/' '_')
      [ "$bn" = "$B" ] && { LANE=$(basename "$d"); break; }
    done
    [ -z "$LANE" ] && continue
  fi
  printf '%s\n' "${APPROVED[@]:-} ${REJECTED[@]:-} ${FAILED[@]:-}" | grep -qw "$LANE" && continue
  # Read the WHOLE FILE with refusal precedence. `head -1` on a tolerant pattern read the
  # file's TITLE, not its decision: a verdict opening `# Verdict — mission` partitioned as
  # FAILED however it actually decided, and a title containing "approve" over a REJECT
  # body partitioned as APPROVED — fail-OPEN.
  case "$(verdict_decision "$f")" in
    REJECT)  REJECTED+=("$LANE") ;;
    FAILED)  FAILED+=("$LANE") ;;
    APPROVE) APPROVED+=("$LANE") ;;
    *) FAILED+=("$LANE") ;;   # NONE — an unparseable verdict is NOT approval
  esac
done
say "verdicts: approved=${#APPROVED[@]} rejected=${#REJECTED[@]} failed=${#FAILED[@]}"
[ "${#APPROVED[@]}" -eq 0 ] && { say "nothing approved — stopping"; record "no_approved" "0 lanes"; exit 0; }

# ---- 2. conflict forecast ---------------------------------------------------
# Two approved lanes touching one file is not a merge conflict yet — git may auto-merge
# it — but it IS the shape of every seam defect found here. Forecast, report, and drop
# the later lane rather than discovering it at merge time.
say "conflict forecast across ${#APPROVED[@]} approved lanes:"
CLAIMS=$(mktemp); CONFLICTS=$(mktemp)
for L in "${APPROVED[@]}"; do
  D="$REPO/var/swarm/clones/$L"
  [ -d "$D/.git" ] || continue
  git -C "$D" status --porcelain 2>/dev/null | grep -vE '\.venv|node_modules' \
    | awk -v l="$L" '{print $NF"\t"l}' >> "$CLAIMS"
done
sort "$CLAIMS" | awk -F'\t' '{c[$1]=c[$1]" "$2; n[$1]++} END {for (f in n) if (n[f]>1) print f" <-"c[f]}' > "$CONFLICTS"
if [ -s "$CONFLICTS" ]; then
  sed 's/^/    CONTESTED: /' "$CONFLICTS"
  record "conflict_forecast" "$(wc -l < "$CONFLICTS" | tr -d ' ') contested path(s)"
else
  say "  no contested paths"
fi
DROP=$(awk -F'<-' '{print $2}' "$CONFLICTS" 2>/dev/null | awk '{for(i=2;i<=NF;i++) print $i}' | sort -u)
rm -f "$CLAIMS" "$CONFLICTS"

# ---- 3. land + merge --------------------------------------------------------
git rev-parse --verify -q "$BRANCH" >/dev/null || git branch "$BRANCH" main
CUR=$(git rev-parse --abbrev-ref HEAD)
git checkout -q "$BRANCH" || { say "cannot checkout $BRANCH"; exit 2; }
MERGED=0
for L in "${APPROVED[@]}"; do
  printf '%s\n' "$DROP" | grep -qx "$L" && { say "deferred (contested): $L"; record "deferred" "$L"; continue; }
  D="$REPO/var/swarm/clones/$L"
  LB=$(git -C "$D" rev-parse --abbrev-ref HEAD 2>/dev/null) || continue
  if [ "$DRY" = "1" ]; then say "would merge: $L ($LB)"; continue; fi
  LANE_COMMIT_MSG="fix($L): first-pass approved by gpt-5.6-sol (openai), coder grok-4.5 (xai)" \
    ./scripts/land-lane.sh "$L" --commit >/dev/null 2>&1
  # Sibling carrier of the merge-gate.sh trial-merge defect (same VALUE: never
  # blame the lane for the instrument's own failure). `>/dev/null 2>&1` used to
  # discard git's words and record EVERY non-zero exit as `merge_conflict` — a
  # durable, wrong entry in the run record for a lane git never even examined.
  MERR=$(git merge --no-ff "$LB" -m "integrate: $L (sol-approved)" 2>&1 >/dev/null)
  MRC=$?
  MERR=$(printf '%s' "$MERR" | tr '\n' ' ' | cut -c1-300)
  if [ "$MRC" -eq 0 ]; then
    say "merged: $L"; MERGED=$((MERGED+1)); record "merged" "$L"
  elif [ "$MRC" -ge 128 ]; then
    git merge --abort 2>/dev/null
    say "NOT JUDGED (git exit $MRC, not a conflict): $L — $MERR"
    record "merge_instrument_failure" "$L: git merge exited $MRC — ${MERR:-<no stderr>}"
  else
    git merge --abort 2>/dev/null
    say "CONFLICT, skipped: $L"; record "merge_conflict" "$L"
  fi
done
# Return to the original branch UNCONDITIONALLY. Leaving the operator on the
# integration branch means every subsequent commit lands there silently — which
# happened: a tooling commit went to integration/reviewed instead of main, and was
# only noticed when a file "vanished" that had never been on this branch.
git checkout -q "$CUR" 2>/dev/null || git checkout -q main 2>/dev/null
[ "$(git rev-parse --abbrev-ref HEAD)" = "$CUR" ] || say "WARNING: could not return to $CUR"

# ---- 4/5. one Anthropic verdict on the AGGREGATE, recorded as data ----------
if [ "$MERGED" -gt 0 ] && [ "$DRY" != "1" ]; then
  say "dispatching ONE anthropic verdict on the aggregate ($MERGED lanes)"
  VF="$REPO/var/swarm/verdicts/$(printf '%s' "$BRANCH" | tr '/' '_').md"
  CLAUDE_CONFIG_DIR="${AGG_ACCT:-$HOME/.claude-account-6}" claude --effort high -p \
"AGGREGATE VERIFIER (opus, anthropic). $MERGED lanes were first-pass reviewed by gpt-5.6-sol (openai) and merged into $BRANCH. Coders were grok-4.5 (xai). You are the third lineage and the last gate before main.

Do NOT re-review each lane — sol did that. Your job is what a per-lane reviewer STRUCTURALLY CANNOT see: the SEAM. The two worst defects in this repo's history were seam defects, invisible in any single lane and visible only where lanes meet.

In $REPO on branch $BRANCH:
1. Run the full ladder ON THE MERGE COMMIT: ./.venv/bin/python -m pytest -q tests/. Report the exact line.
2. Find interactions BETWEEN the merged lanes: two lanes changing the same function from different directions; one lane making a return three-valued while another lane's caller uses bare truthiness on it (None is falsy — unknown silently takes the false branch); a lane's assumption invalidated by another's change.
3. Check the governing rule holds across the aggregate: unknown, absent and unparseable must never render as good.
4. Report anything sol approved that you would REJECT, and say why sol likely missed it.

Write your verdict to $VF beginning a line with 'VERDICT:' (APPROVE / APPROVE-WITH-NOTES / REJECT), naming you (opus). Carry the evidence. Do NOT commit and do NOT merge to main." \
    --model claude-opus-5 --permission-mode acceptEdits \
    --allowedTools "Bash" "Read" "Grep" "Glob" "Write" > "$REPO/var/swarm/aggregate-verify.log" 2>&1
  RC=$?
  if [ "$RC" -ne 0 ] || [ "$(verdict_decision "$VF")" = "NONE" ]; then
    printf '# %s\n\nVERDICT: FAILED (aggregate verifier produced no usable verdict, rc=%s)\n' "$BRANCH" "$RC" > "$VF"
    say "aggregate verdict: FAILED (rc=$RC)"; record "aggregate_failed" "rc=$RC"
  else
    say "aggregate verdict: $(verdict_line "$VF" | cut -c1-52)"
    record "aggregate_verdict" "$(verdict_line "$VF" | cut -c1-80)"
  fi
fi

# ---- 6. report the gate; do NOT merge to main -------------------------------
# THE JUDGE MUST COME FROM THE TREE IT IS PINNED TO (2026-08-07).
# This used to be `./scripts/merge-gate.sh` — the CWD checkout's copy — while
# the tree the gate actually grades in is $OMNIAGENTOS_GATE_WORKSPACE. Two
# inputs, one taken from here and one from there, is the same split that once
# ran a 19-commit-stale gate against a correctly-pinned modern workspace.
#
# It is also a BOOTSTRAPPING BLOCK, which is the sharper half: any candidate
# that edits merge-gate.sh makes the two disagree, so no improvement to the gate
# could ever be gated through this path. The checkout is normally restored to
# $CUR above, but three real paths leave the candidate's own copy here — a
# non-main $CUR, a REPO override, and the `could not return to $CUR` warning
# that this very script already prints when the restore fails.
#
# Resolving the script FROM the workspace removes the split entirely: the gate
# and the tree it grades are then the same commit by construction. Same shape as
# ~/.omniagentos/ops/AccurateGate/gates.d/merge-gate.yaml, which already does this.
GATE_WS="${OMNIAGENTOS_GATE_WORKSPACE:-${REPO}-gate}"
GATE_SH="$GATE_WS/scripts/merge-gate.sh"
if [ -r "$GATE_SH" ]; then
  MERGE_GATE_PINNED=1 OMNIAGENTOS_GATE_WORKSPACE="$GATE_WS" \
    bash "$GATE_SH" "$BRANCH" 2>&1 | tail -6 | sed 's/^/    /'
  record "gate_reported" "$BRANCH merged=$MERGED judge=$GATE_SH"
else
  # Not run is not passed. Say which, and name the remedy.
  say "    gate NOT RUN: no readable $GATE_SH — run scripts/gate-workspace.sh main"
  record "gate_unavailable" "$BRANCH merged=$MERGED missing=$GATE_SH"
fi
say "rejected lanes needing rework: ${REJECTED[*]:-none}"
