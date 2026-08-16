#!/usr/bin/env bash
# Cross-session merge handoff. A session ASKS; the gate DECIDES.
#
# Two sessions work this repo. Until now the handoff was "tell the other session you're
# ready", which is a claim nobody witnesses — the exact shape that has cost us a merge-gate
# bypass, two stranded branches, and three duplicated features today.
#
# So the request is a FILE, and the answer is the gate's, not the requester's:
#
#   session A:  ./scripts/merge-request.sh submit <branch> "what this is"
#   session B:  ./scripts/merge-request.sh poll        # runs the gate, merges or refuses
#
# The requester never merges and never self-certifies. `merge-gate.sh` is the witness: it
# requires a recorded Anthropic verdict, the ladder on the MERGE COMMIT, the counterfeit
# and dominance corpora, a secrets diff, migrations-append-only, and no committed symlinks.
# A request for an unready branch is REFUSED with the reason written back into the request —
# so a refusal is as visible as an approval, and neither side has to be believed.
set -uo pipefail
REPO="${REPO:-/Users/youruser/OmniAgentOS}"
cd "$REPO"
Q="$REPO/var/integration/requests"
mkdir -p "$Q"

sub() {  # branch, note
  local B="${1:?usage: submit <branch> [note]}" N="${2:-}"
  git rev-parse --verify -q "$B" >/dev/null || { echo "no such branch: $B" >&2; exit 2; }
  local F="$Q/$(printf '%s' "$B" | tr '/' '_').request"
  {
    echo "branch: $B"
    echo "sha: $(git rev-parse "$B")"
    echo "submitted: $(date -u +%Y-%m-%dT%H:%M:%SZ)"
    echo "by: ${SESSION_NAME:-unknown-session}"
    echo "note: $N"
    echo "status: pending"
  } > "$F"
  echo "submitted: $B -> ${F#"$REPO"/}"
  echo "  the gate decides; nothing merges on the strength of this request alone."
}

poll() {
  local n=0
  shopt -s nullglob
  for F in "$Q"/*.request; do
    grep -q '^status: pending' "$F" || continue
    n=$((n+1))
    local B S
    B=$(awk '/^branch: /{print $2}' "$F")
    S=$(awk '/^sha: /{print $2}' "$F")
    echo "=== request: $B @ ${S:0:8} ==="

    # The submitted SHA must still be the tip. A branch that moved after the request is a
    # different artifact than the one being vouched for.
    local NOW; NOW=$(git rev-parse "$B" 2>/dev/null)
    if [ "$NOW" != "$S" ]; then
      printf 'status: refused\nreason: branch moved after submission (%s -> %s); resubmit\n' \
        "${S:0:8}" "${NOW:0:8}" >> "$F"
      echo "  REFUSED: tip drifted since submission"
      continue
    fi

    if OUT=$(./scripts/merge-gate.sh "$B" 2>&1); then
      # Sibling carrier of the merge-gate.sh trial-merge defect (same VALUE: a
      # refusal must never blame the branch for the instrument's own failure).
      # `>/dev/null 2>&1` used to discard git's words and map EVERY non-zero
      # exit to "conflicts against main" — so a requester whose branch merges
      # perfectly cleanly was told to rebase because the machine running the
      # merge had, say, no committer identity (exit 128, no merge attempted).
      local MERR MRC
      MERR=$(git merge --no-ff "$B" -m "merge: $B (cross-session request, gate-approved)" 2>&1 >/dev/null)
      MRC=$?
      MERR=$(printf '%s' "$MERR" | tr '\n' ' ' | cut -c1-300)
      if [ "$MRC" -eq 0 ]; then
        printf 'status: merged\nmerged_at: %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" >> "$F"
        echo "  MERGED"
      elif [ "$MRC" -ge 128 ]; then
        git merge --abort 2>/dev/null
        # Deliberately writes NO `status:` line. Nothing was learned about this
        # branch, so there is no verdict to record — recording one either loses
        # the request or slanders the branch. It stays whatever it was.
        printf 'last_error: git merge could not run (exit %s) — environment failure, NOT a conflict in %s: %s\n' \
          "$MRC" "$B" "${MERR:-<git wrote nothing to stderr>}" >> "$F"
        echo "  NOT JUDGED: git could not run the merge (exit $MRC) — $MERR"
      else
        git merge --abort 2>/dev/null
        printf 'status: refused\nreason: conflicts against main; rebase and resubmit\n' >> "$F"
        echo "  REFUSED: conflicts"
      fi
    else
      # Write the gate's own words back, so the requester sees the reason rather than a verdict.
      { echo "status: refused"; echo "reason: |"; printf '%s\n' "$OUT" | tail -12 | sed 's/^/  /'; } >> "$F"
      echo "  REFUSED by the gate:"
      printf '%s\n' "$OUT" | grep -E "FAIL|REFUSED" | head -4 | sed 's/^/    /'
    fi
  done
  [ "$n" = "0" ] && echo "no pending requests"
}

case "${1:-poll}" in
  submit) shift; sub "$@" ;;
  poll)   poll ;;
  list)   grep -H '^status:' "$Q"/*.request 2>/dev/null | sed "s#$Q/##" || echo "none" ;;
  *)      echo "usage: merge-request.sh submit <branch> [note] | poll | list" >&2; exit 2 ;;
esac
