#!/usr/bin/env bash
# SIMULATION DIVISION — keep real work flowing through the PRODUCTION route, always.
#
# This is the one division that had no owner. The last swarm run was 10:07; it is now
# past 12:00. Simulations stopped when I stopped hand-dispatching them, which is the
# "operator is the scheduler" failure for the third time in one night — first for
# verdicts, then for rework, now for simulations.
#
# WHY THIS DIVISION IS NOT AN OPUS AGENT
# The other two divisions (queue/lanes, integration) are already owned by deterministic
# loops that cost nothing and cannot hallucinate. This one is the same: deciding "is a run
# in flight, and if not, start one" needs no judgement. Anthropic capacity is ONE working
# account, and the policy is that Opus is a planner and final reviewer — spending it on
# monitoring means it is unavailable for the aggregate verdict, which is the only thing
# nothing else can do.
#
# WHY PRODUCTION ROUTES, NOT SYNTHETIC HARNESSES
# Measured: two real dispatches through POST /api/swarm found three defects (the token
# path, a run stuck in planning with an empty error, escalation retrying sol->sol->sol).
# A 558-verdict cheap sweep found nothing, and a broad sweep scored 0 confirmed out of 50.
# Volume is not the signal; the production path is.
#
# The briefs are deliberately small and real. A synthetic "add three helpers" exercises the
# planner and nothing else; these exercise capture, escalation, budget, and the terminal
# state machine — the surfaces where tonight's defects actually lived.
#
# ==============================================================================
# S04 — THE ONE PUMP THAT TOUCHED NO LEDGER AT ALL
# ==============================================================================
# rework-pump wrote nothing and read nothing; review-pump and verdict-pump at least READ
# `fleet-ledger.py`. This one did neither, so a POST that failed identically every cycle
# was indistinguishable from one that was working, and the shipped 2-retry cap in
# omniagentos/swarm/scheduler.py governed it not at all.
#
# Each brief is now its own ledger lane (`sim-brief-N`) and each dispatch writes a
# swarm_attempts row through the existing SwarmDal API. The fingerprint is the API's
# RESPONSE, not the request — a request-only hash would be identical every cycle by design
# (the briefs are a fixed rotation) and would quarantine a perfectly healthy prober. A
# response hash is a genuine progress signal: a fresh run id each time differs, while the
# same error twice in a row is identical, and two consecutive identical fingerprints
# quarantine the lane. That is why the gate here is called WITHOUT an incoming hash: the
# fingerprint is only knowable after the call, so the rule is applied to the recorded pair.
#
# The concurrency guard (SIM_MAX in-flight runs) is unchanged and is NOT a substitute:
# it bounds simultaneity, never repetition.
#
# ARMING: REWORK_DRY_RUN=1 (one switch for all four pumps) builds and prints the exact
# POST body and writes the ledger row WITHOUT contacting any API. Clear a quarantine with
# `rm -rf var/swarm/quarantine/<lane>`.
#
# `set -e` is deliberately NOT enabled: this loop must survive a non-zero curl.
set -uo pipefail
REPO="${REPO:-/Users/youruser/OmniAgentOS}"
cd "$REPO"
MAX_INFLIGHT="${SIM_MAX:-3}"
INTERVAL="${SIM_INTERVAL:-300}"
CYCLES="${SIM_CYCLES:-0}"
DRY_RUN="${REWORK_DRY_RUN:-0}"
RETRY_CAP="${SIM_RETRY_CAP:-2}"
LEDGER_SOURCE="real"; [ "$DRY_RUN" = "1" ] && LEDGER_SOURCE="test"
# Runtime state root — the pump's THIRD spelling of "where var lives" is gone.
# Under a campaign the launcher exports OMNIAGENTOS_VAR_DIR=<campaign root>, so
# the pump's log and its workspace clone stay inside the campaign instead of
# landing in the operator checkout. Outside simulation this is the historical
# "$REPO/var", byte-for-byte: var/swarm is also written by swarm/planner.py and
# scripts/land-approved-lanes.py, which are repo-anchored.
if [ "${OMNIAGENTOS_SIM_MODE:-}" = "1" ]; then
  STATE_ROOT="${OMNIAGENTOS_VAR_DIR:-${OMNIAGENTOS_VAR:-}}"
  if [ -z "$STATE_ROOT" ]; then
    echo "FATAL(sim-isolation): OMNIAGENTOS_SIM_MODE=1 requires OMNIAGENTOS_VAR_DIR (or OMNIAGENTOS_VAR); refusing to write the pump's state into $REPO/var" >&2
    exit 1
  fi
  # API target: 8485 is the PRODUCTION control-plane port, so it is never the
  # default here. SIM_API is an explicit operator override, honoured first;
  # otherwise fall back to OMNIAGENTOS_API_PORT, which scripts/launch-env.sh already
  # derives per campaign (18000 + cksum(campaign) % 1000, exported into this
  # process by the launcher that starts the pump under a campaign) for the
  # API server this exact campaign is actually serving. Neither being set
  # means nothing tells us which campaign's stack to reach, so this refuses
  # rather than silently pumping simulated swarm work into production.
  if [ -n "${SIM_API:-}" ]; then
    API="$SIM_API"
  elif [ -n "${OMNIAGENTOS_API_PORT:-}" ]; then
    API="http://127.0.0.1:${OMNIAGENTOS_API_PORT}"
  else
    echo "FATAL(sim-isolation): OMNIAGENTOS_SIM_MODE=1 requires SIM_API or OMNIAGENTOS_API_PORT to be set; refusing to default the sim pump onto the production API port 8485" >&2
    exit 1
  fi
else
  STATE_ROOT="$REPO/var"
  API="${SIM_API:-http://127.0.0.1:8485}"
fi
LOG="${SIM_LOG:-$STATE_ROOT/swarm/sim-pump.log}"
QUARANTINE="${SIM_QUARANTINE:-$STATE_ROOT/swarm/quarantine}"
mkdir -p "$(dirname "$LOG")"
# The session token follows sessions/token.py's own spelling (VAR_DIR, then VAR,
# then the product runtime root the launcher defaults to).
TOKEN_ROOT="${OMNIAGENTOS_VAR_DIR:-${OMNIAGENTOS_VAR:-$REPO/var/runtime}}"
TOKEN_FILE="$TOKEN_ROOT/secrets/sessions-token"

PY="$REPO/.venv/bin/python"
ledger() { "$PY" -W ignore::RuntimeWarning -m omniagentos.swarm.dal "$@"; }

preflight() {
  local fatal=0
  [ -x "$PY" ] || { echo "FATAL(preflight): no python at $PY" >&2; fatal=1; }
  mkdir -p "$QUARANTINE" "$(dirname "$LOG")" 2>/dev/null
  [ -d "$QUARANTINE" ] || { echo "FATAL(preflight): cannot create $QUARANTINE" >&2; fatal=1; }
  if [ "$fatal" -eq 0 ] && ! ledger pump-hash --verdict-hash preflight >/dev/null 2>&1; then
    echo "FATAL(preflight): swarm ledger CLI unusable (omniagentos.swarm.dal)" >&2; fatal=1
  fi
  if [ "$DRY_RUN" != "1" ] && ! command -v curl >/dev/null 2>&1; then
    echo "FATAL(preflight): curl not on PATH and REWORK_DRY_RUN is not 1" >&2; fatal=1
  fi
  [ "$fatal" -eq 0 ] || exit 1
}

is_quarantined() { [ -f "$QUARANTINE/$1/reason" ]; }

quarantine() {  # lane reason — durable on disk, announced exactly once
  local LANE="$1" REASON="$2" Q="$QUARANTINE/$1"
  if is_quarantined "$LANE"; then
    [ "$DRY_RUN" = "1" ] && echo "SKIP lane=$LANE reason=quarantined"
    return 0
  fi
  mkdir -p "$Q" || { echo "  $LANE: FAILED to quarantine" | tee -a "$LOG"; return 1; }
  printf '%s\n' "$REASON" > "$Q/reason"
  date -u +%Y-%m-%dT%H:%M:%SZ > "$Q/first_seen"
  printf '%s\n' "sim brief lane (no clone directory)" > "$Q/lane_dir"
  echo "QUARANTINE lane=$LANE reason=$REASON"
  echo "  $LANE: QUARANTINED ($REASON)" >> "$LOG"
  ledger pump-attempt --lane "$LANE" --pump sim --provider pump --model quarantine \
    --verdict-hash "quarantine:$REASON" --end-reason blocked \
    --detail "quarantine:$REASON" --source "$LEDGER_SOURCE" >/dev/null 2>>"$LOG" || true
  return 0
}

# Briefs aimed at TONIGHT'S MERGES specifically, not generic probes. Each exercises a
# surface we changed, so a regression shows up as a real run failing rather than as a
# synthetic test passing:
#   - the scheduler snapshot no longer commits the operator's dirty PLAN.md
#   - a due built-in routine EXECUTES its module instead of firing a mock
#   - unknown must render as unknown across capture, summary and the dashboard
BRIEFS=(
"Add omniagentos/simprobe/window.py with clamp_window(start, end) that returns None when start > end rather than silently swapping them, plus tests/simprobe/test_window.py written FIRST. Include a revert-test proving the test fails if the None case is removed."
"Add omniagentos/simprobe/ratio2.py with safe_share(part, whole) returning None when whole is 0 — an empty denominator must never report a favourable number — plus tests written FIRST and a counterfeit check that returning 1.0 on an empty whole is caught."
"Add omniagentos/simprobe/tally.py with count_states(rows) that reports an explicit 'unknown' bucket for rows whose state is absent, never folding them into a success bucket, plus tests written FIRST and a revert-test."
"Add omniagentos/simprobe/trim.py with fit(text, limit) that truncates and appends a marker naming how many bytes were dropped, so truncation is never silent, plus tests written FIRST and a counterfeit that returning the text unchanged is caught."
)

inflight() {
  DB="${OMNIAGENTOS_DB:-${DB:-}}" ./scripts/q.sh "select count(*) from swarm_runs where status in ('queued','planning','running','merging');" 2>/dev/null \
    | tail -1 | tr -dc '0-9'
}

preflight

cycle=0
while :; do
  cycle=$((cycle+1))
  # An unreachable API is a REPORTED failure, never a quiet skip — a sim loop that
  # silently does nothing looks identical to one with nothing to do. In dry-run there is
  # deliberately no API to reach, so the health gate is stated as skipped rather than
  # silently passed.
  if [ "$DRY_RUN" = "1" ]; then
    echo "[$(date -u +%H:%M:%S)] cycle=$cycle DRY-RUN — API health gate skipped, nothing will be posted" | tee -a "$LOG"
  else
    CODE=$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 "$API/api/health" 2>/dev/null)
    if [ "$CODE" != "200" ]; then
      echo "[$(date -u +%H:%M:%S)] cycle=$cycle API UNREACHABLE (http $CODE) — not dispatching" | tee -a "$LOG"
      [ "$CYCLES" != "0" ] && [ "$cycle" -ge "$CYCLES" ] && break
      sleep "$INTERVAL"; continue
    fi
  fi

  N=$(inflight); : "${N:=0}"
  FREE=$(( MAX_INFLIGHT - N )); [ "$FREE" -lt 0 ] && FREE=0
  echo "[$(date -u +%H:%M:%S)] cycle=$cycle inflight=$N free=$FREE" | tee -a "$LOG"

  if [ "$DRY_RUN" = "1" ]; then
    TOK="dry-run-no-token"
  else
    TOK=$(cat "$TOKEN_FILE" 2>/dev/null)
    if [ -z "$TOK" ]; then
      echo "  no session token at $TOKEN_FILE — cannot dispatch" | tee -a "$LOG"
      [ "$CYCLES" != "0" ] && [ "$cycle" -ge "$CYCLES" ] && break
      sleep "$INTERVAL"; continue
    fi
  fi

  i=0
  while [ "$i" -lt "$FREE" ]; do
    IDX=$(( (cycle + i) % ${#BRIEFS[@]} ))
    B="${BRIEFS[$IDX]}"
    LANE="sim-brief-$IDX"
    if is_quarantined "$LANE"; then
      [ "$DRY_RUN" = "1" ] && echo "SKIP lane=$LANE reason=quarantined"
      i=$((i+1)); continue
    fi

    # NEVER dispatch against the primary checkout. The swarm executor commits
    # `pre-attempt snapshot` commits into its working_dir, onto whatever branch happens to
    # be checked out there. With the primary repo as the target, an integration branch built
    # in that checkout silently acquired swarm snapshots and an archdocs ARCHI.md commit —
    # contaminating the very artefact that was about to be handed to a final reviewer, and
    # invalidating a ladder run whose tree moved underneath it.
    #
    # A dedicated clone means the simulation can commit whatever it likes without ever
    # touching the branch the coordinator is standing on.
    SIMDIR="$STATE_ROOT/swarm/clones/sim-workspace"
    if [ "$DRY_RUN" != "1" ] && [ ! -d "$SIMDIR/.git" ]; then
      mkdir -p "$(dirname "$SIMDIR")"
      git clone -q --shared "$REPO" "$SIMDIR" 2>/dev/null && git -C "$SIMDIR" checkout -q -B sim/workspace
    fi
    BODY=$("$PY" - "$B" "$SIMDIR" <<'PY'
import json, sys
print(json.dumps({"working_dir": sys.argv[2],
                  "brief": sys.argv[1], "budget_usd_max": 3.0}))
PY
)
    # The gate carries NO incoming hash: this pump's fingerprint is the API response, which
    # is only knowable after the call. The "two consecutive identical hashes" rule is
    # therefore applied to the recorded pair — a lane whose last two responses were byte
    # identical (the same error, or nothing at all) is not making progress.
    GATE=$(ledger pump-gate --lane "$LANE" --retry-cap "$RETRY_CAP" 2>>"$LOG")
    case "$GATE" in
      *"decision=blocked"*)
        REASON=$(printf '%s' "$GATE" | sed -n 's/.*reason=\([a-z-]*\).*/\1/p')
        quarantine "$LANE" "${REASON:-gate-blocked}"; i=$((i+1)); continue ;;
      *"decision=allow"*) : ;;
      *) echo "  $LANE: gate unreadable — refusing to dispatch" | tee -a "$LOG"; i=$((i+1)); continue ;;
    esac

    if [ "$DRY_RUN" = "1" ]; then
      echo "DISPATCH lane=$LANE"
      echo "--- DISPATCH-PAYLOAD BEGIN lane=$LANE ---"
      printf '%s\n' "POST $API/api/swarm"
      printf '%s\n' "$BODY"
      echo "--- DISPATCH-PAYLOAD END lane=$LANE ---"
      RESPF=$(mktemp)
      printf 'dry-run cycle=%s idx=%s\n%s\n' "$cycle" "$IDX" "$BODY" > "$RESPF"
      ledger pump-attempt --lane "$LANE" --pump sim --provider api --model swarm \
        --verdict-body-file "$RESPF" --end-reason blocked \
        --detail "dry-run: POST body built, nothing sent" --source test >/dev/null 2>>"$LOG" || true
      rm -f "$RESPF"
      i=$((i+1)); continue
    fi

    R=$(curl -s --max-time 60 -X POST "$API/api/swarm" \
         -H "Content-Type: application/json" -H "X-Session-Token: $TOK" \
         -d "$BODY" 2>/dev/null)
    echo "DISPATCH lane=$LANE"
    echo "  dispatched: $(printf '%s' "$R" | head -c 80)" | tee -a "$LOG"
    # The RESPONSE is the fingerprint. A fresh run id differs every time (progress); an
    # identical error or an empty body twice in a row is the non-progress proof.
    RESPF=$(mktemp)
    printf '%s' "$R" > "$RESPF"
    END_REASON=completed
    [ -s "$RESPF" ] || END_REASON=crashed
    ledger pump-attempt --lane "$LANE" --pump sim --provider api --model swarm \
      --verdict-body-file "$RESPF" --end-reason "$END_REASON" \
      --detail "POST /api/swarm $(printf '%s' "$R" | head -c 200)" --source real \
      >/dev/null 2>>"$LOG" || true
    rm -f "$RESPF"
    i=$((i+1)); sleep 2
  done
  [ "$FREE" = "0" ] && echo "  at capacity — holding" | tee -a "$LOG"

  [ "$CYCLES" != "0" ] && [ "$cycle" -ge "$CYCLES" ] && break
  if [ "$DRY_RUN" = "1" ]; then sleep "${REWORK_DRY_INTERVAL:-0}"; else sleep "$INTERVAL"; fi
done
