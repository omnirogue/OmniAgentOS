#!/usr/bin/env bash
# Query the runtime DB without silently lying to you.
#
# Three times on 2026-07-28 a coordinator query returned EMPTY OUTPUT because a
# column name was wrong (`run_id` vs `swarm_run_id`, `kind` on events, `status` on
# sessions). sqlite3 writes those errors to stderr; without 2>&1 the caller sees
# a blank result, which reads exactly like "no rows" and produced false
# conclusions. This helper merges stderr, and fails loudly on an error rather
# than returning nothing.
set -uo pipefail
DB="${DB:-/Users/youruser/OmniAgentOS/var/runtime/state.sqlite3}"
[ "${1:-}" = "-s" ] && { shift; sqlite3 "$DB" ".schema ${1:?table}" 2>&1; exit; }
[ "${1:-}" = "-t" ] && { sqlite3 "$DB" ".tables" 2>&1 | tr -s ' ' '\n' | grep -v '^$'; exit; }
OUT=$(sqlite3 -header -column "$DB" "${1:?usage: q.sh <sql> | -s <table> | -t}" 2>&1)
RC=$?
if [ $RC -ne 0 ] || printf '%s' "$OUT" | grep -qiE "^Error:|no such (column|table)"; then
  echo "QUERY FAILED (not 'no rows'):" >&2; printf '%s\n' "$OUT" >&2; exit 1
fi
[ -z "$OUT" ] && echo "(0 rows — query was valid)" || printf '%s\n' "$OUT"
