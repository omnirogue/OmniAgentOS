#!/usr/bin/env bash
set -euo pipefail

if [[ -z "${OMNIAGENTOS_DB:-}" ]]; then
  export OMNIAGENTOS_DB="/Users/youruser/OmniAgentOS/var/runtime/state.sqlite3"
fi

if [[ ! -f "$OMNIAGENTOS_DB" ]]; then
  echo "OMNIAGENTOS_DB does not exist: $OMNIAGENTOS_DB" >&2
  exit 1
fi

TODAY="$(date -u +%Y-%m-%d)"

read -r STARTED COMPLETED < <(
  sqlite3 -separator $'\t' "$OMNIAGENTOS_DB" "
    SELECT
      COUNT(*) AS started_today,
      COALESCE(SUM(CASE WHEN end_reason = 'completed' THEN 1 ELSE 0 END), 0)
        AS completed_today
    FROM swarm_attempts
    WHERE DATE(started_at) = '$TODAY';
  "
)

printf 'Started today: %s\nCompleted today: %s\n' "$STARTED" "$COMPLETED"
printf 'Completion by provider:\n'
sqlite3 "$OMNIAGENTOS_DB" "
  SELECT
    provider || ': ' || printf('%.1f%%',
      100.0 * SUM(CASE WHEN end_reason = 'completed' THEN 1 ELSE 0 END) / COUNT(*)
    )
  FROM swarm_attempts
  WHERE DATE(started_at) = '$TODAY'
  GROUP BY provider
  ORDER BY provider ASC;
"

printf 'Top end reasons:\n'
sqlite3 "$OMNIAGENTOS_DB" "
  SELECT
    COALESCE(end_reason, 'running') ||
      CASE WHEN detail = '' THEN '' ELSE ': ' || detail END ||
      ' (' || COUNT(*) || ')'
  FROM swarm_attempts
  WHERE DATE(started_at) = '$TODAY'
  GROUP BY COALESCE(end_reason, 'running'), detail
  ORDER BY COUNT(*) DESC, end_reason ASC, detail ASC
  LIMIT 3;
"

# Two DIFFERENT numbers -- never conflate them. Counting the unread escalation
# feed and calling it "sessions waiting" overstates the live figure: those
# notifications are a backlog that can outlive the session they refer to.
# What needs a human NOW is the count of sessions actually parked in
# awaiting_approval.
AWAITING="$(sqlite3 "$OMNIAGENTOS_DB" "
  SELECT COUNT(*)
  FROM sessions
  WHERE state = 'awaiting_approval';
")"
ESCALATIONS="$(sqlite3 "$OMNIAGENTOS_DB" "
  SELECT COUNT(*)
  FROM notifications
  WHERE kind = 'escalation' AND read_at IS NULL;
")"
if [ "$AWAITING" -eq 1 ]; then
  printf 'Waiting on you: %s session is parked awaiting approval\n' "$AWAITING"
else
  printf 'Waiting on you: %s sessions are parked awaiting approval\n' "$AWAITING"
fi
printf 'Unread escalation notifications (backlog, not the same as sessions parked): %s\n' "$ESCALATIONS"
