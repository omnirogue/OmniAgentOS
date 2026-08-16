#!/usr/bin/env bash
# Fleet supervisor — polls every 2 minutes, reports idle capacity, detects stalls.
#
# Why this exists: the coordinator was reacting to completion notifications rather
# than polling, so capacity sat idle between them, and a STALLED agent (still
# "running", producing nothing) was indistinguishable from a working one. Tonight
# two leads sat blocked on permission prompts for hours and nothing surfaced it,
# and a model entered a web-search retry loop that never errored out.
#
# A stalled agent is harder to detect than a failed one — it looks alive. So the
# signal here is not "is a process running" but "has this lane produced bytes
# recently". Silence is a state, and it must be reported as silence rather than
# as health.
#
# Writes a census to var/swarm/fleet-status.md every cycle. Never kills anything —
# reporting only, because killing on a heuristic is how real work gets lost.
set -uo pipefail
REPO="${REPO:-/Users/youruser/OmniAgentOS}"
CLONES="$REPO/var/swarm/clones"
OUT="$REPO/var/swarm/fleet-status.md"
TARGET="${FLEET_TARGET:-50}"
STALL_MIN="${STALL_MIN:-12}"
CYCLES="${CYCLES:-0}"   # 0 = run until killed

cycle=0
while :; do
  cycle=$((cycle + 1))
  live=$(pgrep -f 'codex exec|claude5 -p|grok -p|qwen -p|kimi -p|gemini -p' 2>/dev/null | wc -l | tr -d ' ')
  {
    echo "# Fleet status — cycle $cycle"
    echo
    echo "- live agent processes: **$live** (target $TARGET)"
    echo "- idle capacity: **$(( TARGET > live ? TARGET - live : 0 ))** slots"
    echo
    echo "| lane | changed files | last write | state |"
    echo "|---|---|---|---|"
  } > "$OUT.tmp"

  stalled=0; working=0; awaiting=0
  for d in "$CLONES"/*/; do
    [ -d "$d/.git" ] || continue
    L=$(basename "$d")
    n=$(git -C "$d" status --porcelain 2>/dev/null | grep -vcE 'venv|node_modules')
    # Most recent write to a real file (ignore git internals and vendored trees).
    last=$(find "$d" -type f -not -path "*/.git/*" -not -path "*/node_modules/*" \
             -not -path "*/.venv/*" -newermt "-${STALL_MIN} minutes" 2>/dev/null | head -1)
    proc=$(pgrep -f "clones/$L" >/dev/null 2>&1 && echo yes || echo no)
    if [ -n "$last" ]; then
      state="working"; working=$((working+1))
    elif [ "$proc" = "yes" ]; then
      # A live process with no writes for STALL_MIN minutes. Not proof of a stall,
      # but it is the signature of one and must be surfaced, not assumed healthy.
      state="**SILENT ${STALL_MIN}m** (process alive, no output)"; stalled=$((stalled+1))
    elif [ "$n" != "0" ]; then
      state="**awaiting verification/landing**"; awaiting=$((awaiting+1))
    else
      state="empty — reclaimable"
    fi
    printf '| %s | %s | %s | %s |\n' "$L" "$n" "$([ -n "$last" ] && echo recent || echo "> ${STALL_MIN}m")" "$state" >> "$OUT.tmp"
  done

  {
    echo
    echo "## Summary"
    echo "- working: $working"
    echo "- SILENT (possible stall): $stalled"
    echo "- awaiting verification/landing: $awaiting"
    [ "$awaiting" -gt 0 ] && echo "- **ACTION: $awaiting lane(s) finished and are blocked on a verdict.** Work is done and idle."
    [ "$stalled" -gt 0 ] && echo "- **ACTION: $stalled lane(s) silent — check for a permission prompt or a retry loop.**"
    [ "$live" -lt "$TARGET" ] && echo "- **ACTION: $(( TARGET - live )) idle slots — dispatch more work.**"
  } >> "$OUT.tmp"
  mv "$OUT.tmp" "$OUT"

  echo "[cycle $cycle] live=$live working=$working silent=$stalled awaiting=$awaiting"
  [ "$CYCLES" != "0" ] && [ "$cycle" -ge "$CYCLES" ] && break
  sleep 120
done
