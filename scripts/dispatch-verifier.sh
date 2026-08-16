#!/usr/bin/env bash
# Dispatch one cross-lineage verifier, and RECORD ITS FAILURE IF IT FAILS.
#
# Fable's diagnosis, implemented verbatim: "a verifier producing no output must be
# recorded as FAILED, never as pending-looking success. Make the wrapper write an
# explicit FAILED verdict file on non-zero exit or empty output, so dead runs are
# re-dispatchable instead of invisible."
#
# The evidence: 26 of 32 dispatched verifiers produced nothing. Their lanes stayed
# in `awaiting_verdict` — indistinguishable from lanes whose verifier had never been
# dispatched at all. So the fleet could not tell "never tried" from "tried and died",
# and the backlog looked like a shortage of verifiers when it was partly a graveyard.
#
# That is tonight's defect class one level up: the ABSENCE of a verdict was read as
# the verdict not being ready yet. An unwritten verdict is not a pending verdict.
#
# A FAILED verdict also fails the merge gate — which is correct. It records that we
# TRIED and could not establish the claim, which is a different and more honest state
# than silence.
set -uo pipefail
# The verdict-line grammar is shared, not copied — see scripts/lib/verdict-grammar.sh.
# Resolved before any `cd`, and fail-CLOSED: an unset VERDICT_LINE_RE would be an empty
# pattern, which matches every line, which is the failure this whole script exists to stop.
_VERDICT_GRAMMAR="$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)/lib/verdict-grammar.sh"
# shellcheck source=scripts/lib/verdict-grammar.sh
# Loaded AND non-empty: a truncated library would leave the patterns empty, and an
# empty grep pattern matches every line — the most fail-open state available here.
. "$_VERDICT_GRAMMAR" 2>/dev/null && [ -n "${VERDICT_LINE_RE:-}" ] && [ -n "${VERDICT_APPROVE_RE:-}" ] \
  && command -v verdict_decision >/dev/null 2>&1 || {
  echo "FATAL: verdict grammar unusable ($_VERDICT_GRAMMAR)" >&2; exit 1; }
REPO="${REPO:-/Users/youruser/OmniAgentOS}"
LANE="${1:?usage: dispatch-verifier.sh <lane-name>}"
D="$REPO/var/swarm/clones/$LANE"
[ -d "$D/.git" ] || { echo "no lane: $LANE" >&2; exit 2; }

# ACCOUNT SHARDING. Anthropic rate limits are PER ACCOUNT, so verifier capacity is the
# SUM across accounts, not one queue. With 40 lanes blocked and 4 accounts available, a
# single-account pool was the bottleneck — and it was self-inflicted: the cap was mine.
# Pick by lane-name hash so a given lane deterministically lands on one account (stable
# across retries) while the fleet spreads evenly.
ACCOUNTS=()
# Only accounts VERIFIED alive belong here. 1 and 3 have expired OAuth, 2 is
# rate-limited, ~/.claude is expired — probing them wastes a dispatch and returns an
# auth error that reads like a model failure. 6 was added by the operator and tested.
for a in "$HOME"/.claude-account-5 "$HOME"/.claude-account-6 "$HOME"/.claude-account-7; do
  [ -d "$a" ] && ACCOUNTS+=("$a")
done
[ "${#ACCOUNTS[@]}" -eq 0 ] && ACCOUNTS=("${CLAUDE_CONFIG_DIR:-$HOME/.claude}")
IDX=$(printf '%s' "$LANE" | cksum | awk '{print $1}')
ACCT="${ACCOUNTS[$(( IDX % ${#ACCOUNTS[@]} ))]}"

BRANCH=$(git -C "$D" rev-parse --abbrev-ref HEAD 2>/dev/null || echo "")
# FAIL CLOSED ON A DEGENERATE BRANCH NAME — refuse, do not write somewhere useless.
#
# `rev-parse --abbrev-ref HEAD` yields "" for an unborn branch or a corrupt clone, and
# the literal "HEAD" when detached. Neither produces a usable artifact path:
#   ""     -> var/swarm/verdicts/.md    a DOTFILE. `scripts/integrate.sh:48` globs
#                                       `*.md`, which does not match it, so the verdict
#                                       is written and then read by nobody.
#   HEAD   -> var/swarm/verdicts/HEAD.md  SHARED by every detached lane, each one
#                                       overwriting the last. That file exists in this
#                                       repo today, so this is observed, not theoretical.
# Writing a verdict to a path no reader can attribute is worse than writing none: it
# looks like work happened. Refuse here, loudly, BEFORE a verifier is spent — a lane
# with no branch is a setup fault (exit 2, same as a missing clone), not a review.
SLUG=$(printf '%s' "$BRANCH" | tr '/' '_')
case "${BRANCH}:${SLUG}" in
  :*|HEAD:*|*:.*)
    echo "REFUSED: lane '$LANE' has no usable branch name (git rev-parse --abbrev-ref HEAD -> '${BRANCH:-<empty>}')." >&2
    echo "  A verdict for it would land at var/swarm/verdicts/${SLUG}.md, which is unreadable or shared." >&2
    echo "  Fix the clone (checkout a named branch), then re-dispatch. No verifier was spent." >&2
    exit 2 ;;
esac
VERDICT="$REPO/var/swarm/verdicts/${SLUG}.md"
mkdir -p "$(dirname "$VERDICT")"

# ONE DISPATCH AT A TIME PER ARTIFACT — acquired BEFORE anything is mutated.
#
# The clear below is correct sequentially and wrong concurrently: with two overlapping
# dispatches for one lane, the second one's clear DELETES the first one's freshly written,
# genuine APPROVE, and the surviving artifact is the second one's FAILED. A real verdict
# is destroyed and replaced by a false negative, which the fleet then re-dispatches
# against. Serialising is the honest fix — a second verifier for a lane already under
# verification has nothing to add, and the pump's `pgrep` check is advisory, not atomic.
#
# The lock is keyed on the ARTIFACT (the slug), not the lane, because the artifact is the
# resource being contended: two lanes on one branch collide on the same file.
#
# `mkdir` is the atomic primitive. A holder that DIED leaves a lock nobody will release,
# so a lock whose owning pid is gone is reclaimed — via `mv` to a unique name, so that two
# simultaneous reclaimers cannot both conclude they won. A recycled pid can make a dead
# holder look alive; that refuses a dispatch the next pump cycle will retry, which is the
# safe direction to be wrong in.
LOCK="$REPO/var/swarm/verdict-locks/${SLUG}.lock"
LOCK_HELD=0
mkdir -p "$(dirname "$LOCK")"
if ! mkdir "$LOCK" 2>/dev/null; then
  OWNER=$(cat "$LOCK/pid" 2>/dev/null)
  if [ -n "${OWNER:-}" ] && kill -0 "$OWNER" 2>/dev/null; then
    echo "REFUSED: lane '$LANE' is already being verified (pid $OWNER holds ${LOCK#"$REPO"/})." >&2
    echo "  A second dispatch would delete that run's verdict mid-flight. Nothing was touched." >&2
    exit 2
  fi
  DEAD="$LOCK.dead.$$"
  if mv "$LOCK" "$DEAD" 2>/dev/null; then
    rm -rf "$DEAD"
    echo "  $LANE: reclaimed a lock left by dead pid ${OWNER:-unknown}"
  fi
  if ! mkdir "$LOCK" 2>/dev/null; then
    # Distinguish LOST-THE-RACE from CANNOT-WRITE. Both refuse, but they are different
    # problems: one resolves itself on the next cycle, the other never will.
    if [ -d "$LOCK" ]; then
      echo "REFUSED: lane '$LANE' — another dispatch took the lock while reclaiming it." >&2
    else
      echo "REFUSED: lane '$LANE' — cannot create ${LOCK#"$REPO"/}. This is a filesystem" >&2
      echo "  or permissions fault, not contention; it will not clear on retry." >&2
    fi
    exit 2
  fi
fi
LOCK_HELD=1
printf '%s\n' "$$" > "$LOCK/pid"
printf '%s\n' "$LANE" > "$LOCK/lane"
# Release on EVERY exit path, including the watchdog's, and only if we actually hold it.
trap '[ "$LOCK_HELD" = "1" ] && rm -rf "$LOCK"' EXIT

# CLEAR THE ARTIFACT BEFORE DISPATCH. `scripts/review-pump.sh` does exactly this
# (`rm -f "$INNER"`) for exactly this reason, and the fix never propagated here.
#
# Without it the `[ ! -s "$VERDICT" ]` guard below — the entire point of the
# fail-closed FAILED recorder — CANNOT FIRE: it cannot distinguish "this run wrote
# nothing" from "the LAST run wrote something". A verifier that exits 0 writing
# nothing then inherits whatever was on disk, so a previous run's `VERDICT: APPROVE`
# is handed to a downstream gate reader as this run's result and the gate
# accepts a claim no verifier established. Stale is not a verdict.
# (merge-if-only-known-blocker.sh, the reader this comment originally named, was
# DELETED 2026-08-13 by operator ruling: receipts exist, the signed-receipt producer
# landed 2026-07-30, and the bypass had never once executed.)
rm -f "$VERDICT"
# ...and CHECK the clear, because a clear that silently failed leaves exactly the
# state this fix exists to remove: a previous run's APPROVE, ready to be read as this
# run's result. An unremovable artifact is a setup fault, not a verification.
[ ! -e "$VERDICT" ] || {
  echo "REFUSED: cannot clear the previous verdict at ${VERDICT#"$REPO"/} — a stale" >&2
  echo "  APPROVE left there would be read as this run's result. Remove it and re-dispatch." >&2
  exit 2
}
[ -e "$D/.venv" ] || ln -sfn "$REPO/.venv" "$D/.venv"

record_failure() {  # reason
  cat > "$VERDICT" <<EOF
# Verdict — $BRANCH

VERDICT: FAILED (verifier did not establish a claim)

**Verifier:** opus (anthropic)
**Reason:** $1
**Recorded at:** $(date -u +%Y-%m-%dT%H:%M:%SZ)

This file exists so the run is VISIBLE. A verifier that produces nothing must not
leave its lane looking identical to one that was never dispatched — 26 of 32
verifiers vanished that way, and the backlog read as a shortage rather than a
graveyard. An unwritten verdict is not a pending verdict.

FAILED does not pass \`scripts/merge-gate.sh\`, which is correct: we tried and could
not establish the claim. Re-dispatch this lane.
EOF
  echo "  $LANE: FAILED recorded — $1"
}

# `timeout` is absent on macOS by default; bound the run with a watchdog so a hung
# verifier becomes a recorded failure rather than an indefinite silence.
LIMIT="${VERIFIER_TIMEOUT:-2400}"
(
  cd "$D" && CLAUDE_CONFIG_DIR="$ACCT" claude -p "CROSS-LINEAGE VERIFIER (opus, anthropic). The implementer was non-Anthropic. Do NOT re-implement anything.

Read var/task.md for the contract. Use ./.venv/bin/pytest and ./.venv/bin/ruff. NEVER uv run or npm — they write outside the sandbox and fail. No web search.

A LADDER RECORD may already exist for this commit at var/integration/ladder/<sha12>.ladder. If one exists AND its 'sha:' matches the commit you are reviewing AND it reports dirty_tracked_files: 0, DO NOT re-run the full suite — check the binding (one command: git rev-parse HEAD) and spend your time on what only you can do. One verdict here burned 1507 seconds re-running a suite the coordinator had already run in 23. If the SHA does NOT match, or the tree was dirty, the record is INVALID: re-run and say so.

You are not being asked to trust the record. You are being asked to CHECK A BINDING you can break in one step — the same move as the merge receipt.

Report REAL OUTPUT for each step:
1. Verify or re-run the ladder per the rule above. Quote the exact pass/fail line and say which path you took.
2. For EVERY claimed fix: revert it while KEEPING its test, and confirm the test FAILS. **Print the changed line to prove the mutation applied BEFORE trusting any result** — a regex matching nothing yields a green run against unmodified code, which has produced four false conclusions here tonight. If a revert does not move the tests, that fix is unproven; say so.
3. Restore byte-for-byte; confirm green.
4. OVER-CORRECTION CHECK: if a two-valued return became three-valued (None added), enumerate every caller and confirm each handles None EXPLICITLY. Bare truthiness on a three-valued return is a NEW instance of the same defect — None is falsy, so unknown silently takes the false branch.
5. Scope: only the contracted files. Report out-of-scope edits.
6. If the lane claims the surface was CLEAN, verify that is honest rather than lazy: name what it checked and spot-check two plausible defect sites yourself.

Then WRITE your verdict to $VERDICT. It MUST contain a line beginning 'VERDICT:' followed by APPROVE, APPROVE-WITH-NOTES, or REJECT, name you (opus), and carry the evidence. scripts/merge-gate.sh reads that file and REFUSES any branch without it.

A claimed fix with no failing-on-revert test is a REJECT regardless of how many tests pass. Do NOT commit." \
    --model claude-opus-5 --permission-mode acceptEdits \
    --allowedTools "Bash" "Read" "Grep" "Glob" "Edit" "Write" > "$D/var/verify.log" 2>&1
) &
PID=$!
( sleep "$LIMIT"; kill -TERM $PID 2>/dev/null ) & WATCHDOG=$!
wait $PID; RC=$?
kill $WATCHDOG 2>/dev/null

# The watchdog's TERM makes wait return 143. Preserve the FAILED artifact and use
# wrapper rc=3 so verdict-pump can distinguish an inner timeout from other failures.
if [ "$RC" -eq 143 ]; then
  record_failure "verifier watchdog timed out after ${LIMIT}s; see var/verify.log"
  exit 3
elif [ "$RC" -ne 0 ]; then
  record_failure "verifier exited non-zero (rc=$RC); see var/verify.log"
  exit 1
elif [ ! -s "$VERDICT" ]; then
  record_failure "verifier exited 0 but wrote NO verdict file — the silent-success case"
  exit 1
elif [ "$(verdict_decision "$VERDICT")" = "NONE" ]; then
  record_failure "verdict file has no VERDICT line (malformed, not a verdict)"
  exit 1
else
  echo "  $LANE [$(basename "$ACCT")]: $(verdict_line "$VERDICT" | cut -c1-48)"
fi
