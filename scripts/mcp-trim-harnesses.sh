#!/usr/bin/env bash
# Trim the per-harness MCP rosters that live OUTSIDE this repo.
#
# WHY THIS EXISTS
# ---------------
# The repo's .mcp.json is only one of four rosters on this machine, and it is not
# the biggest. Measured 2026-08-13, the worst offender was kimi:
#
#   kimi -p reviewing one markdown file   19 servers   ~1.5 GB
#
# The others:
#   ~/.claude.json          user scope: globex, github, playwright, slack
#                           -- these load in EVERY project on this machine
#                           project scope for OmniAgentOS: globex again
#   ~/.kimi-code/mcp.json   globex, github, playwright, slack, zapier
#   ~/.codex/config.toml    [mcp_servers.*] node_repl, zapier, globex,
#                           playwright, slack, github
#
# None of these is reachable by scripts/gates/mech_gate.sh --check-mcp-roster,
# which reads three fixed paths inside the repo. So the "88% reclaimable" figure
# in the plan is CLAUDE-ONLY until this runs.
#
# WHAT IT DOES NOT DO
# -------------------
# It does not delete servers. It moves each harness's roster aside so the harness
# starts with none, and leaves the original in a timestamped backup next to it.
# Restoring is a `cp` and the script prints the exact command.
#
# It does NOT touch credentials. Note separately that ~/.kimi-code/mcp.json and
# ~/.codex/config.toml embed live bearer tokens inline rather than via ${VAR};
# that is a credential finding to fix on its own, and moving the file aside does
# not fix it.
#
# SAFETY
# ------
# * Dry run is the default. --apply is required to change anything.
# * Refuses to touch a harness whose CLI is currently RUNNING, because rewriting
#   a roster under a live process is how you get a half-configured session.
# * Every change is backed up first, and the restore command is printed.

set -euo pipefail

APPLY=0
[[ "${1:-}" == "--apply" ]] && APPLY=1

STAMP="$(date +%Y%m%d-%H%M%S)"
CHANGED=0

say() { printf '%s\n' "$*"; }

# Refuse while the harness is live. Takes a NAME (claude|kimi|codex).
#
# Three attempts, two of which shipped a defect, because this is a harder
# question than it looks -- the same "is this process X?" question that produced
# four defects in mcp-reaper.py:
#
# 1. `pgrep -f <regex>` FAILED OPEN. A live kimi (`bash …/kimi -p …` with a
#    `kimi-code` child) was invisible to pgrep -f AND pgrep -x, because the child
#    carries an EMPTY argv that macOS pgrep cannot read. Demonstrated by a
#    reviewer against its own session: the dry run offered to rewrite
#    ~/.kimi-code/mcp.json underneath it.
# 2. `ps -Ao command= | grep -qE <regex>` FAILED CLOSED. ps sees those processes
#    (it falls back to the comm name), but matching the WHOLE command line meant
#    an agent's own shell commands -- which mention "kimi" and "codex" as
#    ARGUMENTS -- made every harness read as running. Nothing could be trimmed.
# 3. argv[0]'s BASENAME, which is identity rather than mention. `kimi -p …` runs
#    under a bash wrapper, so the real harness child `kimi-code` is matched via
#    the -code suffix.
#
# The result travels as awk's OUTPUT, never the pipeline's exit status: awk's
# early `exit` closes the pipe, `ps` then takes SIGPIPE and exits 141, and
# `set -o pipefail` above propagates THAT, masking awk's success. That made the
# probe report "not running" for a live claude -- the fail-open direction, and
# invisible outside the script, since an interactive shell has no pipefail.
harness_running() {
  local name="$1" hit
  hit=$(ps -Ao command= 2>/dev/null | awk -v n="$name" '
    {
      split($1, a, "/")
      base = a[length(a)]
      if (base == n || base == n "-code") { print "yes"; exit }
    }
  ' 2>/dev/null) || true
  [[ "$hit" == "yes" ]]
}

trim_json_roster() {
  local label="$1" path="$2" harness="$3" key="$4"
  if [[ ! -f "$path" ]]; then
    say "  $label: absent, nothing to do"
    return
  fi
  local n
  n=$(python3 -c "
import json,sys
try: d=json.load(open('$path'))
except Exception: print(-1); sys.exit()
s=d.get('$key') or {}
print(len(s))
" 2>/dev/null || echo -1)

  if [[ "$n" == "-1" ]]; then
    say "  $label: UNPARSEABLE ($path) -- refusing to touch it"
    return
  fi
  if [[ "$n" == "0" ]]; then
    say "  $label: already 0 servers"
    return
  fi

  if harness_running "$harness"; then
    say "  $label: $n servers, but the harness is RUNNING -- refusing (re-run when idle)"
    return
  fi

  say "  $label: $n servers -> 0"
  if (( APPLY )); then
    # umask in a subshell, NOT cp-then-chmod: the backup of a token-bearing file
    # must never exist world-readable, not even for the interval between the two
    # commands.
    ( umask 077; cp "$path" "$path.bak-mcptrim-$STAMP" )
    python3 - "$path" "$key" <<'PY'
import json, os, sys, tempfile

path, key = sys.argv[1], sys.argv[2]
d = json.load(open(path))
d[key] = {}

# ATOMIC. This was `json.dump(open(path,"w"))` followed by a second
# `open(path,"a")` to add the trailing newline -- two non-atomic writes to
# ~/.claude.json, which is claude's ENTIRE state file. A crash, a kill, or a
# full disk between the truncate and the flush leaves it truncated or
# half-written, and the harness loses every project's history. Write beside it,
# fsync, then rename: os.replace is atomic within a filesystem.
directory = os.path.dirname(path) or "."
fd, tmp = tempfile.mkstemp(dir=directory, prefix=".mcptrim-", suffix=".tmp")
try:
    with os.fdopen(fd, "w") as fh:
        json.dump(d, fh, indent=2)
        fh.write("\n")
        fh.flush()
        os.fsync(fh.fileno())
    os.chmod(tmp, os.stat(path).st_mode & 0o777)
    os.replace(tmp, path)
except BaseException:
    if os.path.exists(tmp):
        os.unlink(tmp)
    raise
PY
    say "      backed up: $path.bak-mcptrim-$STAMP"
    say "      restore  : cp '$path.bak-mcptrim-$STAMP' '$path'"
    CHANGED=1
  fi
}

say "MCP per-harness roster trim  ($( ((APPLY)) && echo APPLY || echo 'DRY RUN -- pass --apply to change anything'))"
say ""
# Print the live-detection result for every harness up front. A guard that fails
# open is otherwise invisible: the run just proceeds and looks normal.
say "live-harness detection (a blind probe here is what corrupts a config):"
for h in claude kimi codex; do
  if harness_running "$h"; then say "  $h: RUNNING (will refuse)"; else say "  $h: not running"; fi
done
say ""

# 1. claude, user scope. Project-scope entries live in the same file under
#    projects.<path>.mcpServers and are handled by the same emptying below only
#    for the global key; project entries are reported, not touched, because they
#    are per-repo decisions.
say "claude (~/.claude.json)"
if [[ -f "$HOME/.claude.json" ]]; then
  python3 - "$HOME/.claude.json" <<'PY'
import json, sys
d = json.load(open(sys.argv[1]))
g = d.get("mcpServers") or {}
print(f"  user scope   : {len(g)} servers {sorted(g)}")
for proj, v in (d.get("projects") or {}).items():
    ms = v.get("mcpServers") or {}
    if ms:
        print(f"  project scope: {proj} -> {sorted(ms)}")
PY
else
  say "  absent"
fi
trim_json_roster "claude user-scope" "$HOME/.claude.json" "claude" "mcpServers"
say ""

say "kimi (~/.kimi-code/mcp.json)"
trim_json_roster "kimi" "$HOME/.kimi-code/mcp.json" "kimi" "mcpServers"
say ""

# codex is hand-maintained TOML carrying inline credentials, so the edit is
# COMMENTING OUT rather than deleting: every original line is preserved, prefixed
# with "#", and the block is fenced with a marker that says how to undo it. That
# keeps the operator's comments, ordering and secrets intact and makes the change
# readable in a diff -- which a delete-and-rewrite would not.
say "codex (~/.codex/config.toml)"
CODEX_CFG="$HOME/.codex/config.toml"
if [[ ! -f "$CODEX_CFG" ]]; then
  say "  absent"
elif ! grep -q '^\[mcp_servers\.' "$CODEX_CFG"; then
  say "  already 0 [mcp_servers.*] entries"
elif harness_running "codex"; then
  n=$(grep -c '^\[mcp_servers\.' "$CODEX_CFG" || true)
  say "  $n entries, but codex is RUNNING -- refusing (re-run when idle)"
else
  n=$(grep -c '^\[mcp_servers\.' "$CODEX_CFG" || true)
  say "  $n [mcp_servers.*] entries -> commented out"
  if (( APPLY )); then
    ( umask 077; cp "$CODEX_CFG" "$CODEX_CFG.bak-mcptrim-$STAMP" )
    python3 - "$CODEX_CFG" <<'PY2'
import os, sys, tempfile

path = sys.argv[1]
lines = open(path).read().splitlines(keepends=True)
out, in_block = [], False
for line in lines:
    stripped = line.lstrip()
    if stripped.startswith("["):
        # Any new table header ends an mcp_servers block.
        in_block = stripped.startswith("[mcp_servers.")
        if in_block:
            out.append("# --- mcp-trim: commented out; uncomment to restore ---\n")
    if in_block and not line.startswith("#"):
        out.append("#" + line)
    else:
        out.append(line)

directory = os.path.dirname(path) or "."
fd, tmp = tempfile.mkstemp(dir=directory, prefix=".mcptrim-", suffix=".tmp")
try:
    with os.fdopen(fd, "w") as fh:
        fh.write("".join(out))
        fh.flush()
        os.fsync(fh.fileno())
    os.chmod(tmp, os.stat(path).st_mode & 0o777)
    os.replace(tmp, path)
except BaseException:
    if os.path.exists(tmp):
        os.unlink(tmp)
    raise
PY2
    say "      backed up: $CODEX_CFG.bak-mcptrim-$STAMP"
    say "      restore  : cp '$CODEX_CFG.bak-mcptrim-$STAMP' '$CODEX_CFG'"
    CHANGED=1
  fi
fi
say ""

if (( APPLY )) && (( CHANGED )); then
  say "Done. Re-measure with: python3 scripts/mcp-reaper.py"
elif (( APPLY )); then
  say "Nothing changed."
else
  say "Dry run only. Re-run with --apply."
fi
