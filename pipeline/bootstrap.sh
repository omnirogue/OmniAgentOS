#!/usr/bin/env bash
# Create the var/loopqueue/** tree for the Three Loops system.
# Usage: ./bootstrap.sh [repo-root]   (default: cwd)
# Idempotent: never overwrites an existing ledger, budget, or artifact.
set -euo pipefail

REPO="${1:-$PWD}"
[ -d "$REPO" ] || { echo "no such directory: $REPO" >&2; exit 1; }
ROOT="$REPO/var/loopqueue"

# --- Environment preconditions (the coordination primitives depend on these) ---
# O_APPEND / O_EXCL semantics require a local POSIX filesystem. NFS, SMB, Dropbox,
# iCloud Drive and Google Drive break them SILENTLY - no error, just lost writes.
case "$(df -P "$REPO" | awk 'NR==2{print $1}')" in
  *:/*|//*) echo "REFUSING: $REPO looks network-mounted; O_APPEND/O_EXCL are not reliable there." >&2; exit 1 ;;
esac
case "$REPO" in
  *Dropbox*|*"CloudStorage"*|*"Google Drive"*|*"iCloud"*)
    echo "WARNING: $REPO is inside a cloud-synced folder. File-locking and atomic rename are NOT reliable." >&2 ;;
esac

mkdir -p "$ROOT"/{state,inquiries,findings,proposals,candidates,claims,rejected,parked,receipts,locks}

# var/loopqueue is runtime state, NOT source. If tracked, Integration's merges hit a
# dirty tree and an index lock, and the ledger lands in commits.
if [ ! -f "$ROOT/.gitignore" ]; then
  printf '*\n!.gitignore\n' > "$ROOT/.gitignore"
fi

# Do not pre-create ledger.jsonl here. The shared ledger transport creates it
# under locks/ledger.lock on the first append; a test-then-redirection here can
# race that writer and truncate the event that won between those two operations.

# One alert per parked item, ever.
[ -f "$ROOT/ALERTS.md" ] || printf '# Loop alerts\n\nOne entry per parked item. Nothing here retries itself.\n' > "$ROOT/ALERTS.md"

# Governor limits. Owned by the governor process; loops READ only.
if [ ! -s "$ROOT/state/budget.json" ]; then   # -s: a 0-byte file is broken, not present
  # Performance cores; falls back to total cores, then 8.
  # Headroom matters: a ceiling at the performance-core count means the loop's
  # OWN test runs saturate it, so it blocks itself and then respawns into the
  # same condition. Use total cores and leave room for the work we are asking for.
  CORES="$(sysctl -n hw.ncpu 2>/dev/null || nproc 2>/dev/null || echo 8)"
  cat > "$ROOT/state/budget.json" <<JSON
{
  "// limits": "TWO independent classes. A loop stops when EITHER binds.",

  "metered_usd": {
    "// note": "Real dollars: Kimi API org, Fireworks, OpenRouter, PiAPI. NOT Claude/Codex/Grok/Gemini, which are subscriptions with ~zero marginal cost per call. Today every row in provider_call_usage is a \$5.00-unmeasured flat estimate, so spent_today is null = UNKNOWN, not zero.",
    "planning": { "daily": 50,  "spent_today": null },
    "repair":   { "daily": 100, "spent_today": null },
    "executor": { "daily": 100, "spent_today": null }
  },

  "subscription": {
    "// note": "Quota belongs to an ACCOUNT. Use the estate's claudeN launchers — NEVER a symlinked CLAUDE_CONFIG_DIR (a login is bound to the canonical path; a symlink makes a logged-in profile report logged OUT) and NEVER copy .credentials.json between profiles. See OmniAgentOS/var/claude-accounts/README.md. Authority is `claudeN auth status --json`, which reports the EMAIL, so capacity is countable by distinct identity rather than by responsive path.",
    "claude_launchers": { "planning": "claude5", "repair": "claude6", "executor": "claude7", "integration": "claude1" },
    "// reserved": "claude4 = the operator's interactive session; claude2/claude3 are the SAME account, so never assign both.",
    "codex": { "window_pct_max": 85, "window_pct": null }
  },

  "disk_free_gb_min": 20,
  "load_avg_1m_max": ${CORES},
  "wip_cap": 4,
  "alert": { "channel": "file", "target": "var/loopqueue/ALERTS.md" },
  "reset_at_local_midnight": true
}
JSON
fi

# Derived snapshot. Integration owns it; rebuildable from ledger.jsonl at any time.
[ -s "$ROOT/state/queue.json" ] || echo '{"items":[],"wip":0,"rebuilt_at":null}' > "$ROOT/state/queue.json"

echo "ready: $ROOT"
find "$ROOT" -maxdepth 1 -mindepth 1 | sort | sed 's/^/  /'

cat <<'TIPS'

Query recipes (note the trailing-line guard - a crash can leave a torn last line):
  LEDGER='var/loopqueue/ledger.jsonl'
  what's been done   grep -c . "$LEDGER" >/dev/null; jq -R 'fromjson? | select(type=="object") | select(.event=="merged") | .ts+"  "+.id' "$LEDGER" | tail -20
  raised as research jq -R 'fromjson? | select(type=="object") | select(.event=="inquired") | .ts+"  "+.id' "$LEDGER"
  rejected + why     jq -R 'fromjson? | select(type=="object") | select(.event=="rejected") | .id+"  "+.detail.reason' "$LEDGER"
  what's in flight   jq -r '.items[] | .id+"  "+.status' var/loopqueue/state/queue.json
  why is it stalled  jq . var/loopqueue/state/budget.json; df -h .; uptime; cat var/loopqueue/ALERTS.md

  `jq -R 'fromjson?'` skips a torn tail instead of aborting the whole read, and
  `select(type=="object")` skips a line that is valid JSON but is NOT an object.
  BOTH are needed: `fromjson?` only swallows a PARSE error, so a bare `123` or
  `null` survives it and then `.event` fails with "Cannot index number with
  string" — jq exits 5, and in a pipeline without `pipefail` that can still look
  like success.

Run each loop under a supervisor that restarts it. They are designed to be killed
at any moment: state lives in files, so a restart costs at most one iteration.
EXACTLY ONE Integration instance may run - it holds the only write lock on main.
TIPS
