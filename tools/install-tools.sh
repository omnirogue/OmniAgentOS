#!/usr/bin/env bash
# OmniAgentOS Tool Library — installer / doctor.
#
# What this does:
#   - Warms the install cache for every KEYLESS/LOCAL MCP server in
#     mcp-servers.json by actually launching each one once (via uvx/npx),
#     so the first real call an agent makes is fast and offline-cache-safe.
#   - For servers that NEED an API key, it never installs-with-a-fake-key.
#     It only confirms the package is real/resolvable and tells you exactly
#     which environment variable to export.
#   - Prints a doctor report at the end.
#
# Safe to re-run any time (idempotent) — every check below is "launch it and
# see if it comes up," so re-running just re-confirms the cache is warm; it
# never mutates anything outside tools/state/, never touches product code,
# and never fails the whole run just because one optional check had a
# hiccup.
#
# Usage:
#   ./install-tools.sh
#
# Deliberately NOT `set -e` / `set -u` / `pipefail`: this script's whole job
# is to survive individual failures and still print a full report, and
# macOS's stock /bin/bash (3.2, no brew bash installed) has long-standing
# `set -u` + empty-array bugs that would make that guarantee unsafe.

TOOLS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
STATE_DIR="$TOOLS_DIR/state"
mkdir -p "$STATE_DIR"

# -----------------------------------------------------------------------
# Portable timeout wrapper.
# macOS ships neither GNU `timeout` nor `gtimeout` by default, so we can't
# depend on either. This background+watcher pattern works on bash 3.2+
# (macOS's stock bash) and any Linux bash.
#
# IMPORTANT: callers must redirect stdout/stderr to a FILE at the call site
# (`with_timeout N cmd ... >"$tmp" 2>&1`), never capture via `$(with_timeout
# ...)`. npx/uvx can fork a grandchild that outlives the direct child and
# keeps inheriting the same stdout fd; `wait` on the direct child is immune
# to that (it tracks the OS process, not fd closure) but a `$(...)` pipe
# capture is NOT — it blocks for EOF, which never comes while the orphaned
# grandchild still holds the pipe open, even after `wait` here has returned.
# Redirecting to a real file and `cat`-ing it afterwards sidesteps this
# entirely (found the hard way while building this — see tools/README.md).
# -----------------------------------------------------------------------
with_timeout() {
  local secs="$1"; shift
  "$@" &
  local pid=$!
  ( sleep "$secs"; kill -TERM "$pid" 2>/dev/null ) &
  local watcher=$!
  local status=0
  if wait "$pid" 2>/dev/null; then status=0; else status=$?; fi
  kill -TERM "$watcher" 2>/dev/null
  wait "$watcher" 2>/dev/null
  return "$status"
}

TIMEOUT_SECS=90

PASS_NAMES=()
FAIL_NAMES=()
FAIL_HINTS=()
KEY_NAMES=()
KEY_ENV=()
KEY_STATUS=()

pad() { printf '%-22s' "$1"; }

# check_banner NAME "human label" match-substring cmd arg1 arg2... --
# launches cmd with stdin closed, expects it to print $match to
# stdout/stderr (either as a --help banner or an "up and running" line)
# within TIMEOUT_SECS.
check_banner() {
  local name="$1" label="$2" match="$3"; shift 3
  printf '  %s ... ' "$(pad "$label")"
  local tmp
  tmp="$(mktemp "${TMPDIR:-/tmp}/omni-tool-check.XXXXXX" 2>/dev/null || echo "/tmp/omni-tool-check.$$.$RANDOM")"
  with_timeout "$TIMEOUT_SECS" "$@" </dev/null >"$tmp" 2>&1
  local out
  out="$(cat "$tmp" 2>/dev/null)"
  rm -f "$tmp" 2>/dev/null
  if printf '%s' "$out" | grep -qi "$match"; then
    echo "OK"
    PASS_NAMES+=("$label")
  else
    echo "FAIL"
    FAIL_NAMES+=("$label")
    FAIL_HINTS+=("$(printf '%s' "$out" | tail -3 | tr '\n' ' ')")
  fi
}

check_registry_only() {
  # For key-needing servers: prove the package is real WITHOUT running it
  # or requiring a key (a plain npm/pypi registry lookup).
  local label="$1" env_var="$2" registry="$3" pkg="$4"
  printf '  %s ... ' "$(pad "$label")"
  local resolvable="no"
  if [[ "$registry" == "npm" ]]; then
    if npm view "$pkg" version >/dev/null 2>&1; then resolvable="yes"; fi
  else
    if curl -sf -m 15 "https://pypi.org/pypi/${pkg}/json" >/dev/null 2>&1; then resolvable="yes"; fi
  fi
  local have="unset"
  if [[ -n "${!env_var:-}" ]]; then have="SET"; else have="not set"; fi
  if [[ "$resolvable" == "yes" ]]; then
    echo "package OK, key ${have} (export ${env_var} to use)"
  else
    echo "COULD NOT VERIFY PACKAGE (network?) — key ${have}"
  fi
  KEY_NAMES+=("$label")
  KEY_ENV+=("$env_var")
  KEY_STATUS+=("$have")
}

echo "=================================================================="
echo " OmniAgentOS Tool Library — installer / doctor"
echo " tools dir: $TOOLS_DIR"
echo "=================================================================="

echo
echo "-- runtimes --"
for bin in node npm npx python3 uv uvx git curl; do
  printf '  %s ... ' "$(pad "$bin")"
  if command -v "$bin" >/dev/null 2>&1; then
    ver="$("$bin" --version 2>&1 | head -1)"
    echo "found ($ver)"
  else
    echo "MISSING"
  fi
done

echo
echo "-- keyless / local MCP servers (installing + verifying) --"

check_banner fetch "fetch (web fetch)" "mcp-server-fetch" \
  uvx mcp-server-fetch --help

check_banner markitdown "markitdown (doc conversion)" "markitdown-mcp" \
  uvx markitdown-mcp --help

check_banner git "git (repo tools)" "mcp-server-git" \
  uvx mcp-server-git --help

check_banner sqlite "sqlite (local db)" "SQLite MCP Server" \
  uvx mcp-server-sqlite --help

check_banner filesystem "filesystem (file tools)" "running on stdio" \
  npx -y @modelcontextprotocol/server-filesystem "$STATE_DIR"

check_banner memory "memory (knowledge graph)" "running on stdio" \
  npx -y @modelcontextprotocol/server-memory

check_banner sequential-thinking "sequential-thinking (reasoning)" "running on stdio" \
  npx -y @modelcontextprotocol/server-sequential-thinking

check_banner playwright "playwright (browser)" "Version" \
  npx -y @playwright/mcp@latest --version

check_banner duckduckgo "duckduckgo (keyless web search)" "DuckDuckGo MCP Server" \
  uvx duckduckgo-mcp-server --help

echo
echo "-- needs-key MCP servers (NOT installed here — verified only) --"
check_registry_only "tavily (web search)" TAVILY_API_KEY npm tavily-mcp
check_registry_only "brave-search (web search)" BRAVE_API_KEY npm "@modelcontextprotocol/server-brave-search"

echo
echo "-- local state dirs --"
printf '  %s ... ' "$(pad "tools/state/")"
if [[ -d "$STATE_DIR" ]]; then echo "OK ($STATE_DIR)"; else echo "FAIL (could not create)"; fi

echo
echo "=================================================================="
echo " DOCTOR REPORT"
echo "=================================================================="
printf ' %-32s %s\n' "KEYLESS SERVER" "STATUS"
if [[ ${#PASS_NAMES[@]} -gt 0 ]]; then
  for n in "${PASS_NAMES[@]}"; do
    printf ' %-32s %s\n' "$n" "OK"
  done
fi
if [[ ${#FAIL_NAMES[@]} -gt 0 ]]; then
  for ((i = 0; i < ${#FAIL_NAMES[@]}; i++)); do
    printf ' %-32s %s\n' "${FAIL_NAMES[$i]}" "FAIL — ${FAIL_HINTS[$i]}"
  done
fi
echo
printf ' %-32s %-10s %s\n' "NEEDS-KEY SERVER" "KEY?" "ENV VAR TO SET"
if [[ ${#KEY_NAMES[@]} -gt 0 ]]; then
  for ((i = 0; i < ${#KEY_NAMES[@]}; i++)); do
    printf ' %-32s %-10s %s\n' "${KEY_NAMES[$i]}" "${KEY_STATUS[$i]}" "${KEY_ENV[$i]}"
  done
fi

echo
TOTAL_PASS=${#PASS_NAMES[@]}
TOTAL_FAIL=${#FAIL_NAMES[@]}
echo " keyless servers: ${TOTAL_PASS} OK, ${TOTAL_FAIL} FAIL"
echo " needs-key servers documented: ${#KEY_NAMES[@]} (set the env var above, then just use them — nothing else to install)"
echo
echo " Point an agent at this library:"
echo "   claude (interactive)   : run from OmniAgentOS/ — Claude Code loads .mcp.json, which is"
echo "                            its OWN tracked file, not a symlink to tools/mcp-servers.json;"
echo "                            edit both identically (see tools/README.md). Approve once via /mcp"
echo "   claude -p (headless)   : claude --print \"<prompt>\" --mcp-config tools/mcp-servers.local.json \\"
echo "                              --strict-mcp-config --allowedTools \"mcp__fetch,mcp__markitdown,...\""
echo "                            (see tools/README.md — plain .mcp.json pickup needs a human to approve it)"
echo "   Hermes                 : paste the mcp_servers block from tools/README.md into ~/.hermes/config.yaml"
echo "   codex exec / codex mcp : run the 'codex mcp add ...' commands in tools/README.md once"
echo "                            (writes to ~/.codex/config.toml — not done automatically by this script)"
echo "=================================================================="

if [[ "$TOTAL_PASS" -eq 0 ]]; then
  echo "No keyless server could be verified — check the runtimes section above." >&2
  exit 1
fi
exit 0
