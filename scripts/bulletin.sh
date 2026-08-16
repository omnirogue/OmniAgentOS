#!/usr/bin/env bash
# The mechanical coordinator. Derives fleet state, FILLS empty slots, and publishes a
# bulletin whose top section is the orchestrator's next action.
#
# WHY THIS EXISTS
# ---------------
# Every coordination failure tonight had the same shape: a thing needed doing, no process
# owned doing it, and an LLM was expected to remember. It did not.
#
#   - verdict-pump died three separate times; nothing noticed for hours
#   - 13 verified lanes sat unmerged because the ledger misread its own verdict files
#   - two branches "landed" but were never merged, because landing and merging are
#     different verbs and the difference lived only in someone's head
#   - the fleet ran 8 agents against a capacity of ~100 while the coordinator believed
#     it was API-bound
#
# A bulletin that merely NOTIFIES has the same defect one level up: it still depends on
# someone reading it and choosing to act. So this does not notify — it ACTS, and the
# bulletin is the record of what it did. An LLM is required only for judgement that
# cannot be mechanised, and each such item is written out explicitly rather than dropped.
#
# THE RULE THIS ENFORCES
#   A claim about an effect is admitted as evidence only when it is witnessed from the far
#   side of the boundary the effect crosses. An absent witness is a refusal, never a pass.
#
# So: every number below is produced by executing something. Where a probe cannot run, the
# field is "unknown" — never a favourable default. An empty slot that cannot be explained
# is reported as an empty slot, not as "fleet at capacity".
#
#   bulletin.sh once     # one pass: derive, fill, publish
#   bulletin.sh loop     # supervised, every $INTERVAL seconds
#   bulletin.sh show     # print the current bulletin
set -uo pipefail
REPO="${REPO:-/Users/youruser/OmniAgentOS}"
cd "$REPO" || exit 1
PY="$REPO/.venv/bin/python"
BULLETIN="$REPO/var/swarm/BULLETIN.md"
LOG="$REPO/var/swarm/bulletin.log"
INTERVAL="${INTERVAL:-120}"
mkdir -p "$REPO/var/swarm"

# Capacity is per PROVIDER, not global. Agents are I/O-bound (measured: 0.0% CPU, ~25MB
# each), so CPU cores were never the constraint — sizing the fleet against them cost us
# most of a night's throughput. Anthropic is the only genuinely scarce lane and its limit
# is an account policy, not a machine limit.
CAP_GOOGLE="${CAP_GOOGLE:-100}"   # gemini — primary coder, non-critical
CAP_XAI="${CAP_XAI:-60}"          # grok 4.5 — primary coder, critical
CAP_OPENAI="${CAP_OPENAI:-40}"    # gpt-5.6-sol — team lead + first-pass review
CAP_ANTHROPIC="${CAP_ANTHROPIC:-4}"  # planning + FINAL review only. the operator's policy.

PUMPS="review-pump rework-pump sim-pump verdict-pump fleet-supervisor plan-consolidator"

log() { printf '%s %s\n' "$(date -u +%H:%M:%S)" "$*" >> "$LOG"; }

# ---------------------------------------------------------------- mechanical probes
# Count by EXECUTABLE NAME, never by matching the full command line.
#
# The first version used `pgrep -f grok`, which matches any process whose command line
# contains the string — including every process running inside `/Users/youruser/
# omniagentos`. It reported 124 grok agents against a real 20, and 146 codex against 26.
# The bulletin's entire claim is that its numbers are measured rather than assumed; a probe
# this loose would have made it authoritative and wrong, which is worse than absent.
exe_count() { pgrep -x "$1" 2>/dev/null | wc -l | tr -d ' '; }

# Second, independent probe. If two methods disagree the count is not trustworthy, and an
# untrustworthy count is reported as `unknown` rather than as whichever number is comforting.
comm_count() { ps -Ao comm= 2>/dev/null | awk -v w="$1" '{n=split($0,p,"/"); if (p[n]==w) c++} END{print c+0}'; }

bin_for() {
  case "$1" in
    google) echo gemini ;; xai) echo grok ;; openai) echo codex ;; anthropic) echo claude ;;
  esac
}

running() {  # provider -> live agent count, witnessed twice
  # The two probes run microseconds apart against a quantity that genuinely moves — agents
  # start and exit constantly — so demanding exact agreement reported `unknown` forever.
  # A divergence of 1 is a process starting between the probes; anything larger means the
  # probes disagree about WHAT they are counting, which is the failure mode that matters.
  # When they agree, take the HIGHER count: over-reporting load under-reports free slots,
  # so the error direction is conservative rather than encouraging.
  local b a c d; b=$(bin_for "$1"); a=$(exe_count "$b"); c=$(comm_count "$b")
  d=$(( a > c ? a - c : c - a ))
  if [ "$d" -le 1 ]; then printf '%s' "$(( a > c ? a : c ))"; else printf 'unknown'; fi
}

# Arithmetic needs a number; `unknown` is not one. Callers that must subtract use this and
# get an explicit sentinel rather than a shell error silently evaluating to zero.
running_num() { local r; r=$(running "$1"); case "$r" in ''|*[!0-9]*) echo -1 ;; *) echo "$r" ;; esac; }

# ---------------------------------------------------------------- self-healing
# A dead pump is not reported and left dead. It is restarted, and the restart is recorded
# so a pump that dies repeatedly becomes visible as a pattern rather than as silence.
revive_pumps() {
  local revived=""
  for p in $PUMPS; do
    [ -x "$REPO/scripts/$p.sh" ] || continue
    if ! pgrep -f "$p.sh" >/dev/null 2>&1; then
      # Same tty-less detach as verdict-pump's dispatch site: bulletin runs
      # under the gate-loop launchd daemon, where BSD nohup aborts its console
      # detach ("Inappropriate ioctl for device") and never execs — leaving a
      # dead pump un-revived. The trap-HUP subshell with stdin closed needs no
      # tty. (SIG_IGN for HUP is inherited across the exec of the pump loop.)
      ( trap "" HUP; exec "$REPO/scripts/$p.sh" loop ) </dev/null >> "$REPO/var/swarm/$p.log" 2>&1 &
      disown 2>/dev/null
      revived="$revived $p"
      log "REVIVED $p"
      printf '%s %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$p" >> "$REPO/var/swarm/pump-deaths.log"
    fi
  done
  printf '%s' "$revived"
}

# How often has each pump had to be revived? A pump revived many times is not healthy just
# because it is up right now.
death_counts() {
  [ -f "$REPO/var/swarm/pump-deaths.log" ] || return 0
  awk '{print $2}' "$REPO/var/swarm/pump-deaths.log" | sort | uniq -c | sort -rn |
    awk '$1 >= 3 {printf "  - `%s` has needed %d restarts — it is failing, not flaking\n", $2, $1}'
}

# ---------------------------------------------------------------- orphan adoption
# The core move: an empty slot with ready work is a bug, and it is fixed here rather than
# reported to someone who might fix it.
#
# A lane refused by an ANTHROPIC reviewer had no owner at all. `rework-pump.sh` reads only
# `var/swarm/sol-verdicts/`, so a final-review rejection landed in a directory nothing
# watches and the lane stopped forever, looking identical to a lane nobody had got to yet.
#
# Rather than build a second dispatcher, adopt the orphan INTO the pump that already works:
# mirror the anthropic verdict into the store rework-pump watches. The coder then receives
# the reviewer's verbatim text through the existing, proven path, and the pump clears the
# record when the rework lands.
adopt_orphans() {
  local LANES ADOPTED
  LANES=$(mktemp); ADOPTED=$(mktemp)
  # Read the ledger's own derived state rather than re-deriving it here. A second
  # implementation of "which lanes are rejected" is how the verdict parse ended up written
  # twice with the same bug in both copies.
  "$PY" -c '
import json, pathlib
led = pathlib.Path("var/swarm/fleet-ledger.jsonl")
if led.exists():
    for line in led.read_text().splitlines():
        if line.strip() and json.loads(line).get("state") == "rejected":
            print(json.loads(line)["lane"])
' > "$LANES" 2>/dev/null

  # NOT a pipeline: a `while read` on the far side of a pipe runs in a subshell, and every
  # count incremented inside it is discarded when that subshell exits. The first version of
  # this function always reported 0 adoptions no matter what it did — a coordinator that
  # silently under-reports its own work is the defect this file exists to remove.
  while IFS= read -r LANE; do
    [ -z "$LANE" ] && continue
    SOL="$REPO/var/swarm/sol-verdicts/$LANE.md"
    # Already queued for rework? Leave it alone — re-adopting would re-dispatch a live lane.
    #
    # THIS PATTERN IS COUPLED TO scripts/rework-pump.sh:302 AND MUST MATCH IT. The coupling
    # was undocumented and load-bearing: this grep is `&& continue`, so a false positive
    # here SKIPS adoption — a MISSED rework, not a spurious one — and that is harmless only
    # because rework-pump reads the same file with the same tolerant pattern and picks the
    # lane up anyway. Narrow one side without the other and a refused lane falls between
    # them: skipped here, unrecognised there, owned by nobody. REWORK was added to both in
    # the same commit for exactly that reason (opus-critic/codex-critic vocabulary).
    grep -qiE '^[[:space:]]*\**[[:space:]]*#*[[:space:]]*VERDICT[^A-Za-z]*(REJECT|FAIL|REWORK)' "$SOL" 2>/dev/null && continue
    BR=$(git -C "$REPO/var/swarm/clones/$LANE" rev-parse --abbrev-ref HEAD 2>/dev/null)
    # "" and "HEAD" are both degenerate. The empty case was already guarded; the
    # detached case was not, and it resolves to var/swarm/verdicts/HEAD.md — a file
    # EVERY detached lane shares (it exists in this repo). Adopting it would mirror
    # one lane's rejection into another lane's rework queue.
    case "$BR" in ""|HEAD) continue ;; esac
    AV="$REPO/var/swarm/verdicts/$(printf '%s' "$BR" | tr '/' '_').md"
    [ -f "$AV" ] || continue

    # ADOPT EACH VERDICT ONCE. Without this the two halves livelock: the bulletin mirrors
    # the anthropic rejection into the rework queue, rework-pump fixes the lane and CLEARS
    # the mirror, and the next bulletin pass sees the still-present anthropic rejection and
    # mirrors it again. Observed live — four lanes re-adopted at 16:10, 16:12 and 16:14,
    # each cycle re-dispatching a coder against a rejection that had already been addressed.
    # The verdict is retired by its CONTENT: a genuinely new rejection has a new hash and is
    # adopted again, an already-handled one never is.
    VH=$(shasum -a 256 "$AV" 2>/dev/null | cut -c1-16)
    ADOPT_LOG="$REPO/var/swarm/adopted-verdicts.log"
    grep -qxF "$LANE $VH" "$ADOPT_LOG" 2>/dev/null && continue
    { echo "# $LANE"
      echo
      echo "VERDICT: REJECT"
      echo
      echo "Adopted by bulletin.sh from the ANTHROPIC final review, which lands in"
      echo "var/swarm/verdicts/ — a directory no pump watches. Verbatim below; fix what it names."
      echo
      cat "$AV"
    } > "$SOL"
    printf '%s %s\n' "$LANE" "$VH" >> "$ADOPT_LOG"
    log "ADOPTED-ORPHAN $LANE (anthropic reject had no pump, verdict $VH)"
    echo "$LANE" >> "$ADOPTED"
  done < "$LANES"

  wc -l < "$ADOPTED" | tr -d ' '
  rm -f "$LANES" "$ADOPTED"
}

# Empty slots are only meaningful if something CAN fill them. Report the honest reason when
# nothing can, rather than emitting "queue empty" — an unrunnable probe is not good news.
slot_report() {
  local prov want have
  for prov in google xai openai anthropic; do
    case $prov in
      google) want=$CAP_GOOGLE ;; xai) want=$CAP_XAI ;;
      openai) want=$CAP_OPENAI ;; anthropic) want=$CAP_ANTHROPIC ;;
    esac
    have=$(running_num "$prov")
    [ "$have" -lt 0 ] && { printf '  - `%s` count is UNKNOWN — two probes disagreed. Do not treat as full.\n' "$prov"; continue; }
    if [ "$have" -lt "$want" ] && [ "$prov" != "anthropic" ]; then
      printf '  - `%s` has %s free slot(s). Work is dispatched by the pumps; if this stays free while\n    lanes await review or rework, a pump is picking up less than it should.\n' \
        "$prov" "$(( want - have ))"
    fi
  done
}

# One row of the capacity table. `free` is only printed when the count is trustworthy —
# subtracting from an unknown yields a number that looks like knowledge and is not.
cap_row() {
  local prov="$1" label="$2" cap="$3" role="$4" n free
  n=$(running_num "$prov")
  if [ "$n" -lt 0 ]; then
    printf '| %s | unknown | %s | unknown | %s |\n' "$label" "$cap" "$role"
  else
    free=$(( cap - n )); [ "$free" -lt 0 ] && free=0
    printf '| %s | %s | %s | %s | %s |\n' "$label" "$n" "$cap" "$free" "$role"
  fi
}

# ---------------------------------------------------------------- publish
publish() {
  local revived="$1" filled="$2"
  "$PY" "$REPO/scripts/fleet-ledger.py" scan >/dev/null 2>&1
  local SUM; SUM=$("$PY" "$REPO/scripts/fleet-ledger.py" summary 2>/dev/null)

  local n_ready n_merge n_rework n_working
  n_ready=$(printf '%s' "$SUM"   | awk '/ready_to_integrate/{print $2}')
  n_merge=$(printf '%s' "$SUM"   | awk '/mergeable/{print $2}')
  n_rework=$(printf '%s' "$SUM"  | awk '/needs_rework|rejected/{s+=$2} END{print s+0}')
  n_working=$(printf '%s' "$SUM" | awk '/working/{print $2}')

  {
    echo "# FLEET BULLETIN"
    echo
    echo "Generated mechanically by \`scripts/bulletin.sh\` at $(date -u +%Y-%m-%dT%H:%M:%SZ)."
    echo "Every number here was produced by executing a probe. Nothing is self-reported."
    echo
    echo "## ORCHESTRATOR: DO THIS NOW"
    echo
    local acted=0
    if [ "${n_merge:-0}" -gt 0 ]; then
      echo "- **Merge ${n_merge} verified lane(s).** They carry a parsed \`VERDICT: APPROVE\` from an"
      echo "  Anthropic reviewer and are blocked on nothing: \`./scripts/integrate.sh\`"
      acted=1
    fi
    if [ "${n_ready:-0}" -gt 0 ]; then
      echo "- **Batch ${n_ready} sol-reviewed lane(s)** into ONE integration branch, then dispatch ONE"
      echo "  Anthropic verdict on the aggregate — not one verdict per lane."
      echo "  \`./.venv/bin/python scripts/fleet-ledger.py query ready_to_integrate\`"
      acted=1
    fi
    if [ "${n_rework:-0}" -gt 0 ]; then
      echo "- **${n_rework} lane(s) were REFUSED by a reviewer.** They must not be batched."
      echo "  \`./.venv/bin/python scripts/fleet-ledger.py query rejected\`"
      acted=1
    fi
    [ "$acted" = "0" ] && echo "- Nothing is blocked on the orchestrator. The fleet is dispatching itself."
    echo
    echo "## What this pass did without being asked"
    echo
    [ -n "$revived" ] && echo "- Restarted dead loop(s):$revived" || echo "- All supervised loops were already up."
    if [ "${filled:-0}" -gt 0 ]; then
      echo "- Adopted **$filled** orphaned lane(s): rejected by an Anthropic reviewer into a"
      echo "  directory no pump watches. Mirrored into the rework queue; a coder now owns them."
    else
      echo "- No orphaned lanes: every refusal on record is owned by a pump."
    fi
    slot_report
    death_counts
    echo
    echo "## Capacity — measured, not assumed"
    echo
    echo '| provider | running | capacity | free | role |'
    echo '|---|---:|---:|---:|---|'
    cap_row google "google (gemini)"       "$CAP_GOOGLE"    "primary coder, non-critical"
    cap_row xai    "xai (grok 4.5)"          "$CAP_XAI"       "primary coder, critical"
    cap_row openai "openai (gpt-5.6-sol)"    "$CAP_OPENAI"    "team lead + first-pass review"
    cap_row anthropic "anthropic"            "$CAP_ANTHROPIC" "**planning + FINAL review only** (incl. this session)"
    echo
    echo "Anthropic capacity is a policy limit, not a machine limit. It is never used for"
    echo "implementation. Nothing merges without an Anthropic verdict on the far side."
    echo
    echo "## Lane ledger"
    echo
    printf '```\n%s\n```\n' "$SUM"
    echo
    echo "## Loops"
    echo
    for p in $PUMPS; do
      if pgrep -f "$p.sh" >/dev/null 2>&1; then echo "- up — \`$p\`"; else echo "- **DOWN** — \`$p\` (revive failed; needs a human)"; fi
    done
    echo
    echo "---"
    echo "_Refuses to render an unknown as a pass. A probe that cannot run reports \`unknown\`._"
  } > "$BULLETIN.tmp" && mv "$BULLETIN.tmp" "$BULLETIN"
}

once() {
  local revived adopted
  revived=$(revive_pumps)
  adopted=$(adopt_orphans)
  publish "$revived" "$adopted"
  echo "bulletin: ${BULLETIN#"$REPO"/}"
  sed -n '/^## ORCHESTRATOR/,/^## What this pass/p' "$BULLETIN" | sed '$d'
}

case "${1:-once}" in
  once) once ;;
  loop) log "bulletin loop up (interval ${INTERVAL}s)"
        while :; do once >/dev/null 2>&1; sleep "$INTERVAL"; done ;;
  show) cat "$BULLETIN" 2>/dev/null || echo "no bulletin yet — run: ./scripts/bulletin.sh once" ;;
  *)    echo "usage: bulletin.sh [once|loop|show]" >&2; exit 2 ;;
esac
