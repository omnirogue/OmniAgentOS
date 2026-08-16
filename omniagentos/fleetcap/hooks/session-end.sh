#!/bin/sh
# Claude Code SessionEnd hook. Deliberately fail-open and append-only.
set +e
TTY_RAW=$(tty </dev/tty 2>/dev/null)
TTY_RAW=${TTY_RAW##*/}
case "$TTY_RAW" in '?'|'??'|'-') TTY_NAME='' ;; *) TTY_NAME=$TTY_RAW ;; esac
case "$TTY_RAW" in ''|'?'|'??'|'-') INTERACTIVE=false ;; *) INTERACTIVE=true ;; esac
INPUT=$(cat 2>/dev/null) || exit 0
SPOOL_DIR=${FLEETCAP_SPOOL_DIR:-"${HOME}/Work/Ops/telemetry/spool"}
umask 077
mkdir -p "$SPOOL_DIR" 2>/dev/null || exit 0
chmod 700 "$SPOOL_DIR" 2>/dev/null || true
DAY=$(date +%Y%m%d 2>/dev/null) || exit 0
DEVICE=${FLEETCAP_DEVICE:-$(hostname -s 2>/dev/null)}
if command -v jq >/dev/null 2>&1; then
  LINE=$(printf '%s' "$INPUT" | jq -c --arg event SessionEnd --arg tty "$TTY_NAME" --arg tty_raw "$TTY_RAW" --arg device "$DEVICE" --argjson ppid "$PPID" --argjson interactive "$INTERACTIVE" '{event:$event,session_id:(.session_id // .sessionId // ""),transcript_path:(.transcript_path // .transcriptPath // ""),source:(.source // ""),cwd:(.cwd // ""),ppid:$ppid,tty:$tty,tty_raw:$tty_raw,interactive:$interactive,ts:(now),device:$device}' 2>/dev/null)
else
  LINE=$(printf '%s' "$INPUT" | python3 -c 'import json,sys,time; d=json.load(sys.stdin); print(json.dumps({"event":"SessionEnd","session_id":d.get("session_id",d.get("sessionId","")),"transcript_path":d.get("transcript_path",d.get("transcriptPath","")),"source":d.get("source",""),"cwd":d.get("cwd",""),"ppid":int(sys.argv[1]),"tty":sys.argv[2],"tty_raw":sys.argv[3],"interactive":sys.argv[4]=="true","ts":time.time(),"device":sys.argv[5]},separators=(",",":")))' "$PPID" "$TTY_NAME" "$TTY_RAW" "$INTERACTIVE" "$DEVICE" 2>/dev/null)
fi
[ -n "$LINE" ] && printf '%s\n' "$LINE" >> "$SPOOL_DIR/hooks-$DAY.jsonl" 2>/dev/null
exit 0
