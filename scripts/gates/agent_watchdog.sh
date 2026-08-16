#!/bin/bash
set -euo pipefail

# Stuck-CLI-agent detector — OBSERVE MODE ONLY.
#
# Two passes 30s apart flag a spinner (>90% CPU in both samples) or a flatline
# (<0.1% CPU in both samples, older than 15 minutes, not stopped).  It LOGS and
# NOTIFIES.  It never signals, never terminates, and never unloads anything:
# no process is touched on the strength of a classification that has not been
# diffed against reality for a full week of observation.
#
# Uses temp files for bash 3.2 compatibility (macOS ships bash 3.2, which lacks
# associative arrays).

# ---------------------------------------------------------------------------
# Elapsed-time arithmetic.
#
# `ps -o etime=` emits FOUR shapes on macOS: SS, MM:SS, HH:MM:SS and
# DD-HH:MM:SS.  Every component is zero-padded, so `08` and `09` are invalid
# octal and bash aborts the whole arithmetic expansion with
# "value too great for base".  That produced 1,014 errors in
# var/log/agent-watchdog.log.  Every component is therefore forced to base 10
# with the `10#` prefix — the comment that used to claim this is now the code.
# ---------------------------------------------------------------------------
etime_to_seconds() {
  local etime=${1:-}
  local original_etime=${etime}
  local days=0 hours=0 minutes=0 seconds=0

  # Strip surrounding whitespace without invoking a subshell.
  etime=${etime#"${etime%%[![:space:]]*}"}
  etime=${etime%"${etime##*[![:space:]]}"}

  if [[ -z "$etime" ]]; then
    echo 0
    return 0
  fi

  if [[ "$etime" == *"-"* ]]; then
    days=${etime%%-*}
    etime=${etime#*-}
    # A day prefix is the DD-HH:MM:SS shape; accepting a shorter suffix
    # would silently reinterpret a malformed ps value as a real process age.
    if [[ ! "$days" =~ ^[0-9]+$ || ! "$etime" =~ ^[0-9]+:[0-9]+:[0-9]+$ ]]; then
      echo "etime_to_seconds: unparseable etime '$original_etime'" >&2
      return 1
    fi
  # Without a day prefix, only SS, MM:SS, and HH:MM:SS are valid.
  elif [[ ! "$etime" =~ ^([0-9]+|[0-9]+:[0-9]+|[0-9]+:[0-9]+:[0-9]+)$ ]]; then
    echo "etime_to_seconds: unparseable etime '$original_etime'" >&2
    return 1
  fi

  # Count colons to determine which of SS / MM:SS / HH:MM:SS we were handed.
  local colons
  colons=$(printf '%s' "$etime" | tr -cd ':' | wc -c | tr -d '[:space:]')
  case "$colons" in
    2) IFS=':' read -r hours minutes seconds <<< "$etime" ;;
    1) IFS=':' read -r minutes seconds <<< "$etime" ;;
    *) seconds=$etime ;;
  esac

  # `ps` carries minutes and seconds in the 00-59 range, and hours in the
  # 00-23 range when a day prefix is present. Reject impossible components
  # instead of turning malformed output into a plausible process age.
  if (( 10#${hours:-0} > 23 || 10#${minutes:-0} > 59 || 10#${seconds:-0} > 59 )); then
    echo "etime_to_seconds: unparseable etime '$original_etime'" >&2
    return 1
  fi

  # `10#` on every component, with :- guarding empty fields so `10#` is never
  # left dangling (which is itself a syntax error).
  echo $(( (10#${days:-0}) * 86400 \
         + (10#${hours:-0}) * 3600 \
         + (10#${minutes:-0}) * 60 \
         + (10#${seconds:-0}) ))
}

# ---------------------------------------------------------------------------
# Liveness predicate.
#
# `S+` means "sleeping in the FOREGROUND process group of a controlling
# terminal" — i.e. a CLI sitting at its prompt waiting for the operator.  It
# burns 0.0% CPU and ages indefinitely, which is exactly the shape of the
# flatline predicate, so 3,394 STUCK-SUSPECT lines across 110 PIDs were fired
# at processes that were simply idle at a prompt.  A process attached to a
# controlling tty in the foreground is interactive, not flatlined.
#
# The `+` alone is not enough: state strings are multi-character (`SNs+`), and
# the tty must be a real one (`??` and `-` mean no controlling terminal).
# Only the sleeping state is excused — an `R+` process pinning a core is still
# eligible for spinner detection.
# ---------------------------------------------------------------------------
is_live_interactive() {
  local state=${1:-} tty=${2:-}
  [[ "$state" == S* ]] || return 1
  [[ "$state" == *"+"* ]] || return 1
  [[ -n "$tty" && "$tty" != "??" && "$tty" != "-" ]] || return 1
  return 0
}

# Classify one process from two samples.  Echoes "spinner", "flatline", or "".
classify_process() {
  local cpu1=${1:-0} cpu2=${2:-0} etime2=${3:-0}
  local state1=${4:-} state2=${5:-} tty2=${6:-??}
  local kind="" age_seconds

  # A live interactive foreground CLI is never a STUCK-SUSPECT.
  if is_live_interactive "$state2" "$tty2"; then
    echo ""
    return 0
  fi

  if awk "BEGIN { exit !($cpu1 > 90 && $cpu2 > 90) }"; then
    kind=spinner
  fi

  if [[ "$state2" != T* && "$state1" != T* ]]; then
    age_seconds=$(etime_to_seconds "$etime2")
    if awk "BEGIN { exit !(($cpu1 < 0.1) && ($cpu2 < 0.1) && ($age_seconds > 900)) }"; then
      kind=flatline
    fi
  fi

  echo "$kind"
}

sample_processes() {
  # Explicit field reconstruction: index()-based slicing mis-slices whenever a
  # short field value also occurs earlier in the line.
  ps -axo pid=,pcpu=,etime=,state=,tty=,command= | awk '
    {
      cmd = ""
      for (i = 6; i <= NF; i++) cmd = cmd (i > 6 ? " " : "") $i
      print $1, $2, $3, $4, $5, cmd
    }'
}

# Cleanup state is global so the EXIT trap can always see it.
STATE1_FILE=""
STATE2_FILE=""
cleanup_state() { rm -f "${STATE1_FILE:-}" "${STATE2_FILE:-}"; }
trap cleanup_state EXIT

watch_once() {
  local tmp=${TMPDIR:-/tmp}
  # NOT `local`: the EXIT trap fires in the top-level shell, where a function
  # local is out of scope and `set -u` would abort the cleanup itself.
  STATE1_FILE=$(mktemp "${tmp}/watchdog-state1-XXXXXX")
  STATE2_FILE=$(mktemp "${tmp}/watchdog-state2-XXXXXX")

  sample_processes > "$STATE1_FILE"
  sleep 30
  sample_processes > "$STATE2_FILE"

  local line1 line2 pid1 cpu1 etime1 state1 tty1 cmd1
  local pid2 cpu2 etime2 state2 tty2 cmd2 kind short_cmd age_seconds age_minutes msg

  while IFS= read -r line1; do
    read -r pid1 cpu1 etime1 state1 tty1 cmd1 <<< "$line1"
    [[ "$cmd1" =~ ^(claude|codex|grok|gemini|kimi-code)([[:space:]]|$) ]] || continue

    line2=$(grep "^${pid1} " "$STATE2_FILE" || true)
    [[ -n "$line2" ]] || continue # pid didn't survive 30s

    read -r pid2 cpu2 etime2 state2 tty2 cmd2 <<< "$line2"

    kind=$(classify_process "$cpu1" "$cpu2" "$etime2" "$state1" "$state2" "$tty2")
    [[ -n "$kind" ]] || continue

    short_cmd="${cmd2:0:60}"
    age_seconds=$(etime_to_seconds "$etime2")
    age_minutes=$((age_seconds / 60))
    msg="STUCK-SUSPECT pid=$pid1 kind=$kind age=${age_minutes}m cmd=$short_cmd"
    echo "$msg"
    osascript -e "display notification \"$msg\" with title \"OmniAgentOS\"" 2>/dev/null || true
  done < "$STATE1_FILE"
}

main() {
  case "${1:-}" in
    --etime-seconds)
      shift
      etime_to_seconds "${1:-}"
      ;;
    --classify)
      shift
      # cpu1 cpu2 etime2 state1 state2 tty2
      classify_process "${1:-0}" "${2:-0}" "${3:-0}" "${4:-}" "${5:-}" "${6:-??}"
      ;;
    --live-interactive)
      shift
      if is_live_interactive "${1:-}" "${2:-}"; then echo yes; else echo no; fi
      ;;
    -h | --help)
      cat <<'USAGE'
agent_watchdog.sh                                   observe run (two samples, 30s apart)
agent_watchdog.sh --etime-seconds ETIME             convert a ps etime to seconds
agent_watchdog.sh --classify C1 C2 ET S1 S2 TTY     classify one process (observe only)
agent_watchdog.sh --live-interactive STATE TTY      yes/no: interactive foreground CLI
USAGE
      ;;
    "")
      watch_once
      ;;
    *)
      echo "agent_watchdog.sh: unknown argument '$1' (try --help)" >&2
      exit 2
      ;;
  esac
}

main "$@"
