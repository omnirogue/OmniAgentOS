#!/usr/bin/env bash
# OpenHands Policy Gate hook script
set -euo pipefail

# Read stdin JSON input
INPUT=$(cat)

# Use jq to parse the JSON
TOOL_NAME=$(echo "$INPUT" | jq -r '.tool_name')
WORKING_DIR=$(echo "$INPUT" | jq -r '.working_dir // empty')
SESSION_ID=$(echo "$INPUT" | jq -r '.session_id // empty')

# Helper to write to ledger log
log_decision() {
    local decision="$1"
    local reason="$2"
    local ROOT
    ROOT="$(cd "$(dirname "$0")/.." && pwd)"
    local log_dir="${OMNIAGENTOS_LEDGER_DIR:-$ROOT/var/ledger}"
    mkdir -p "$log_dir"
    local log_file="$log_dir/openhands_gate.jsonl"
    local timestamp
    timestamp=$(date -u +"%Y-%m-%dT%H:%M:%SZ")
    
    # Safely escape reason/input for json
    local escaped_input
    escaped_input=$(echo "$INPUT" | jq -c '.')
    
    local log_line
    log_line=$(jq -n \
        --arg ts "$timestamp" \
        --arg dec "$decision" \
        --arg rsn "$reason" \
        --arg tool "$TOOL_NAME" \
        --arg s_id "$SESSION_ID" \
        --arg wd "$WORKING_DIR" \
        --argjson inp "$escaped_input" \
        '{"timestamp": $ts, "decision": $dec, "reason": $rsn, "tool_name": $tool, "session_id": $s_id, "working_dir": $wd, "input": $inp}')
        
    echo "$log_line" >> "$log_file"
}

deny_action() {
    local reason="$1"
    echo "{\"decision\": \"deny\", \"reason\": \"Policy violation: $reason\"}"
    log_decision "deny" "$reason"
    exit 2
}

# --- 1. LOCAL PERMISSION GUARDRAILS ---

# Block dangerous path modifications
if [[ "$TOOL_NAME" == "editor" || "$TOOL_NAME" == "view_file" || "$TOOL_NAME" == "read_file" ]]; then
    PATH_ARG=$(echo "$INPUT" | jq -r '.tool_input.path // empty')
    if [[ -n "$PATH_ARG" ]]; then
        # Expand any tilde
        REAL_PATH="${PATH_ARG/#\~/${HOME}}"
        # Prevent accessing sensitive files/folders
        if [[ "$REAL_PATH" == *".ssh"* || "$REAL_PATH" == *".config/omni"* || "$REAL_PATH" == *".gemini"* || "$REAL_PATH" == *"configs/governance.yaml"* ]]; then
            deny_action "Access to sensitive file or credentials is restricted ($PATH_ARG)"
        fi
    fi
fi

# Block highly dangerous command patterns
if [[ "$TOOL_NAME" == "terminal" || "$TOOL_NAME" == "bash" || "$TOOL_NAME" == "sh" ]]; then
    CMD_ARG=$(echo "$INPUT" | jq -r '.tool_input.command // empty')
    if [[ -n "$CMD_ARG" ]]; then
        # Check for dangerous root rm, credential exports, or payment hacks
        if [[ "$CMD_ARG" == *"rm -rf /"* || "$CMD_ARG" == *"rm -rf ~"* || "$CMD_ARG" == *"connections.env"* || "$CMD_ARG" == *"stripe"* || "$CMD_ARG" == *"paypal"* ]]; then
            deny_action "Destructive or credential-exposing command is restricted"
        fi
    fi
fi

# --- 2. LIVE CENTRAL API GATE CHECK ---

# Call POST /api/grok/gates/eval with G3 tool check
# Fail-closed: hard 3-second timeout (-m 3)
API_URL="${OMNIAGENTOS_API_URL:-http://127.0.0.1:8485/api/grok/gates/eval}"

# Default allowed tools list from configurations
ALLOWED_TOOLS="[\"terminal\", \"editor\", \"browser\", \"fetch\", \"web-search\", \"view_file\"]"

RESPONSE=$(curl -s -m 3 -X POST "$API_URL" \
    -H "Content-Type: application/json" \
    -d "{
      \"gate\": \"g3\",
      \"evidence\": {
        \"tool_name\": \"$TOOL_NAME\",
        \"tools_allowed\": $ALLOWED_TOOLS
      }
    }" || echo "API_DOWN")

if [[ "$RESPONSE" == "API_DOWN" ]]; then
    deny_action "Central gate API unreachable (fail-closed)"
fi

# Parse API decision
DECISION=$(echo "$RESPONSE" | jq -r '.decision // "deny"')

if [[ "$DECISION" != "allow" && "$DECISION" != "tool_authorized" && "$DECISION" != "authorized" ]]; then
    # Double check if API returned a reject reason
    REASON=$(echo "$RESPONSE" | jq -r '.evidence.reason // "unauthorized"')
    deny_action "G3 gate rejected execution: $REASON"
fi

# All gates passed: ALLOW!
echo "{\"decision\": \"allow\", \"reason\": \"G3 tool execution authorized\"}"
log_decision "allow" "G3 tool execution authorized"
exit 0
