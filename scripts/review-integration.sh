#!/usr/bin/env bash
# ONE Anthropic verdict on a whole integration branch — not one verdict per lane.
#
# Per-lane final review does not scale and does not catch the defects that matter. A seam
# defect is invisible inside any single lane and visible only where lanes meet, so the
# artefact that needs an Anthropic reviewer is the AGGREGATE. Per-lane first-pass review
# still happens — gpt-5.6-sol, cross-lineage — and this is the second tier above it.
#
# Runs in an isolated worktree. The first attempt at this ran in the primary checkout, and
# the swarm executor committed `pre-attempt snapshot` commits onto the branch mid-review,
# moving the artefact underneath both the ladder and the reviewer.
#
#   review-integration.sh <branch> [worktree-dir]
set -uo pipefail
# Shared verdict-line grammar. Resolved before any `cd`, and fail-CLOSED.
_VERDICT_GRAMMAR="$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)/lib/verdict-grammar.sh"
# shellcheck source=scripts/lib/verdict-grammar.sh
. "$_VERDICT_GRAMMAR" 2>/dev/null && [ -n "${VERDICT_LINE_RE:-}" ] && [ -n "${VERDICT_APPROVE_RE:-}" ] \
  && command -v verdict_decision >/dev/null 2>&1 || {
  echo "FATAL: verdict grammar unusable ($_VERDICT_GRAMMAR)" >&2; exit 1; }
REPO="${REPO:-/Users/youruser/OmniAgentOS}"
BRANCH="${1:?usage: review-integration.sh <branch> [dir]}"
D="${2:-$REPO/var/swarm/clones/int-verified}"
# Absolute, always. A relative $D survives `cd "$D"` and then re-resolves against the NEW
# directory, so the log redirect became <dir>/<dir>/var-review.log and the whole review was
# lost to "No such file or directory" — recorded as a reviewer failure rather than a path bug.
case "$D" in /*) ;; *) D="$REPO/$D" ;; esac
VERDICT="$REPO/var/swarm/verdicts/$(printf '%s' "$BRANCH" | tr '/' '_').md"
mkdir -p "$(dirname "$VERDICT")"
# The reviewer is sandboxed to its working directory, so it CANNOT write the canonical
# verdict path. The first run reached a correct REJECT with a real defect and then lost it:
# the write needed approval, the process exited 0, and the wrapper recorded FAILED. The
# reviewer writes INSIDE its sandbox; the coordinator copies the result out.
INNER_VERDICT="$D/var-verdict.md"

# Live accounts only. 1/2/3 and ~/.claude are expired or rate-limited; probing them wastes
# a dispatch and returns an error that looks like a review failure.
ACCOUNTS=()
for a in "$HOME"/.claude-account-5 "$HOME"/.claude-account-6 "$HOME"/.claude-account-7; do
  [ -d "$a" ] && ACCOUNTS+=("$a")
done
[ "${#ACCOUNTS[@]}" -eq 0 ] && ACCOUNTS=("${CLAUDE_CONFIG_DIR:-$HOME/.claude}")
ACCT="${ACCOUNTS[0]}"

# A missing worktree must fail HERE, loudly, as a setup error. Falling through recorded it
# as "reviewer exited non-zero" — a reviewer that was never dispatched blamed for a path
# mistake, which is the same absent-witness confusion this pipeline exists to remove.
if [ ! -d "$D/.git" ] && [ ! -f "$D/.git" ]; then
  echo "  SETUP ERROR: no git worktree at $D — nothing was reviewed" >&2
  exit 2
fi

# CLEAR BOTH ARTIFACTS BEFORE DISPATCH — this wrapper had the same silent-success
# defect as dispatch-verifier.sh, one carrier over.
#
# The reviewer is sandboxed and writes INSIDE its worktree; the coordinator copies the
# result out. So a PREVIOUS run's $INNER_VERDICT is a fully parseable file sitting exactly
# where this run's reviewer was told to write. A reviewer that exits 0 writing nothing then
# had its predecessor's `VERDICT: APPROVE` copied into the canonical artifact that
# merge-gate.sh reads — an aggregate approval for a review that never happened. Checking
# "is the inner file parseable" cannot catch it: a stale line parses perfectly.
#
# The clear is CHECKED. A clear that silently failed leaves precisely the state it exists
# to remove, and the run would continue as though it had succeeded.
for _stale in "$INNER_VERDICT" "$VERDICT"; do
  rm -f "$_stale"
  [ ! -e "$_stale" ] || {
    echo "  SETUP ERROR: cannot clear a previous verdict at $_stale — a stale APPROVE" >&2
    echo "  there would be read as this run's result. Remove it and re-dispatch." >&2
    exit 2
  }
done

SHA=$(git -C "$D" rev-parse HEAD)
FILES=$(git -C "$D" diff --name-only main.."$BRANCH" | wc -l | tr -d ' ')
LANES=$(git -C "$D" log --oneline main.."$BRANCH" --grep='^merge: ' | wc -l | tr -d ' ')

# An unwritten verdict is not a pending verdict. Record the failure so a dead run is
# re-dispatchable rather than invisible — 26 of 32 verifiers once produced nothing and
# their lanes were indistinguishable from lanes never dispatched at all.
record_failure() {
  cat > "$VERDICT" <<EOF
# Verdict — $BRANCH

VERDICT: FAILED ($1)

Reviewer: claude-opus-5 (anthropic), aggregate integration review.
sha: $SHA
An absent verdict is not a clean one. Re-dispatch.
EOF
  echo "  recorded FAILED: $1"
}

PROMPT="AGGREGATE INTEGRATION REVIEWER — claude-opus-5, anthropic. FINAL tier.

You are reviewing branch \`$BRANCH\` at $SHA: $LANES merged lanes, $FILES files vs main.
Every implementer was NON-Anthropic (grok-4.5, gemini, gpt-5.6-sol). Each lane already
passed a cross-lineage first-pass review. You are the second tier, and the ONLY thing
standing between this branch and main.

DO NOT re-implement anything. DO NOT re-run the full test suite — a SHA-bound ladder record
is being produced separately. Your job is judgement the first tier structurally could not
make.

WHAT ONLY YOU CAN SEE. Each first-pass reviewer saw ONE lane. You see all $LANES at once, so
look for what is invisible inside any single lane:
  - two lanes that each edited the same function or contract in incompatible ways
  - a caller updated in one lane against a signature changed in another
  - duplicated implementations of the same capability, added independently
  - a lane that removed something another lane now depends on
  - config <-> code parity drift: a constant, allow-list or ceiling changed in one place only

THE STANDING RULE OF THIS REPO:
  A claim about an effect is admitted as evidence only when it is witnessed from the far
  side of the boundary the effect crosses. An absent witness is a refusal, never a pass.

Apply it to what you are reading. Specifically REFUSE:
  - a test that would still pass with its fix reverted
  - a capability that is built but never called from a production path (this repo has found
    TEN such instances; check that new code is actually reachable, not merely present)
  - a probe or gate that returns success when it could not run
  - error handling that renders an unknown as a favourable default (fail-open)
  - committed probe ordnance: 'REVIEW MUTATION', '# PROBE:', 'SIMHARNESS_BREAK' outside tests/

COMMANDS YOU SHOULD RUN (read-only):
  git diff main..$BRANCH --stat
  git diff main..$BRANCH -- <file>
  git log --oneline main..$BRANCH

OUTPUT — write your review to $INNER_VERDICT and nothing else. That path is inside your
working directory; anything outside it you cannot write. It MUST contain a line of exactly
this form, on its own line:

VERDICT: APPROVE
   or
VERDICT: REJECT

REJECT if you find any defect above, and name the file and line. If you cannot reach a
conclusion, write VERDICT: REJECT and say why — inconclusive is a refusal, never a pass.
Be specific and terse. Do not summarise what the lanes did; say what is wrong or that
nothing is."

cd "$D" || exit 1
[ -e "$D/.venv" ] || ln -sfn "$REPO/.venv" "$D/.venv"

echo "  reviewing $BRANCH @ ${SHA:0:8} ($LANES lanes, $FILES files) with $(basename "$ACCT")"
if CLAUDE_CONFIG_DIR="$ACCT" claude --effort xhigh --model claude-opus-5 \
     --permission-mode acceptEdits -p "$PROMPT" > "$D/var-review.log" 2>&1; then
  if [ "$(verdict_decision "$INNER_VERDICT")" != "NONE" ]; then
    cp "$INNER_VERDICT" "$VERDICT"
    echo "  verdict recorded: $(verdict_line "$VERDICT")"
  else
    record_failure "reviewer exited 0 but wrote no parseable VERDICT line"
  fi
else
  record_failure "reviewer exited non-zero"
fi
