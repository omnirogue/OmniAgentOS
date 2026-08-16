#!/usr/bin/env bash
# Launchd entrypoint for the 07:00 team report (com.omniagentos.team-report).
#
# the operator's decision 2026-08-10: the daily production report posts from the SECOND
# Slack app (bot "initech_crm_mcp", INITECH_JIRA_SLACK_BOT_TOKEN) into
# #dev-agentic-alerts. That app is not yet a member of the channel and no
# stored credential carries an invite scope, so this wrapper PROBES: if the
# second app can see the target channel it posts as the second app, otherwise
# it falls back to the primary bot (SLACK_BOT_TOKEN) so the report is never
# skipped. The moment someone runs `/invite @initech_crm_mcp` in the
# channel, the probe starts succeeding and the identity flips automatically.
#
# omniagentos.team.report reads SLACK_BOT_TOKEN with an exported-value-wins
# rule (report.load_slack_env uses setdefault), so exporting here is the whole
# mechanism — no report code knows about the second app.
set -euo pipefail

CONNECTIONS_ENV="${HOME}/.config/omni/connections.env"
if [[ -f "${CONNECTIONS_ENV}" ]]; then
  set -a
  # shellcheck source=/dev/null
  source "${CONNECTIONS_ENV}"
  set +a
fi

TARGET_CHANNEL="${OMNI_TEAM_REPORT_CHANNEL:-C0000EXAMPLE}"
if [[ -n "${INITECH_JIRA_SLACK_BOT_TOKEN:-}" ]]; then
  visible=$(curl -s -m 10 \
    -H "Authorization: Bearer ${INITECH_JIRA_SLACK_BOT_TOKEN}" \
    "https://slack.com/api/conversations.info?channel=${TARGET_CHANNEL}" |
    /usr/bin/python3 -c 'import json,sys
try:
    print("yes" if json.load(sys.stdin).get("ok") else "no")
except Exception:
    print("no")' 2>/dev/null || echo no)
  if [[ "${visible}" == "yes" ]]; then
    export SLACK_BOT_TOKEN="${INITECH_JIRA_SLACK_BOT_TOKEN}"
  else
    echo "team-report-post: second app cannot see ${TARGET_CHANNEL};" \
      "posting as primary bot (invite @initech_crm_mcp to flip)" >&2
  fi
fi

exec /Users/youruser/OmniAgentOS/.venv/bin/python -m omniagentos.team.report "$@"
