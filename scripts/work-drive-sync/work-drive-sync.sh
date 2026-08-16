#!/bin/zsh
# Work -> Drive sync: one-way ADDITIVE mirror of ~/Work to Google Drive.
# Installed by hygiene automation 2026-08-01; promoted from "Mac Archive/Work-Mirror"
# to "My Drive/Work" 2026-08-01 (org upgrade). Daily via launchd: com.owner.work-drive-sync.
# Reverse direction: ~/bin/work-drive-pull.sh (_FromDrive drop zones, com.owner.work-drive-pull).
#
# Contract (full doc: ~/README.md):
#   rsync -a (NO --delete, ever) from ~/ to "<Drive>/Work/" — same taxonomy both sides.
#   Local ~/Work is the source of truth for Mac-produced files. Drive:/Work may hold
#   EXTRAS (Google Docs, collaborator uploads, _FromDrive drop zones); this sync never
#   removes anything on the Drive side, so those extras are safe.
#   Churn excluded: node_modules, .git, venvs, __pycache__, .next (as dir names
#   anywhere); _worktrees, unset; *.sock, .DS_Store; Ops/Agent_Transcripts_Last_24h.
#   build/ and dist/ are deliberately NOT excluded — real marketing deliverables
#   live in dirs with those names (review finding, 2026-08-01).
#   Privacy policy excluded (stay local-only, NEVER in the company Drive account):
#   Personal, Finance/, "Finance & Legal/", *.pem, *.p12, id_rsa*, id_ed25519*,
#   .env and *.env variants, *.keystore, *.jks, .npmrc, .netrc, *.token,
#   *credentials*.json, *credentials*.env, CREDENTIALS*, *credential*.txt.
#   (*.key deliberately not excluded: it would also match Keynote decks.)
# Log: ~/Library/Logs/work-drive-sync.log      Dry run: ~/bin/work-drive-sync.sh --dry-run
#
# Status/SLA: ~/.config/work-drive-sync/last_attempt and .../last_success are
# stamped (unix seconds) every real run — see finding f3eaf8d4 (2026-08-08)
# and STATUS_DIR below.

set -u
# Hardened PATH — ~/.zshenv prepends user bins (.local/bin, .grok/bin); this wins.
export PATH=/usr/bin:/bin:/usr/sbin:/sbin
SRC="$HOME/Work/"
DRIVE="$HOME/Library/CloudStorage/GoogleDrive-owner@acmeuni.example/My Drive"
DEST="$DRIVE/Work/"
LOG="$HOME/Library/Logs/work-drive-sync.log"
LOCKDIR="$HOME/.config/work-drive-sync/lock"
STATUS_DIR="$HOME/.config/work-drive-sync"
RSYNC="/usr/bin/rsync"
WARN_BYTES=$((20 * 1024 * 1024 * 1024))   # >20GB in one run = unexpected churn

DRY_RUN=0
[ "${1:-}" = "--dry-run" ] || [ "${1:-}" = "-n" ] && DRY_RUN=1

ts() { date "+%Y-%m-%d %H:%M:%S"; }
log() { echo "[$(ts)] $*" >> "$LOG"; }
notify() { /usr/bin/osascript -e "display notification \"$2\" with title \"$1\"" >/dev/null 2>&1 || true; }
record_attempt() { mkdir -p "$STATUS_DIR" 2>/dev/null || true; date "+%s" > "$STATUS_DIR/last_attempt" 2>/dev/null || true; }
record_success() { mkdir -p "$STATUS_DIR" 2>/dev/null || true; date "+%s" > "$STATUS_DIR/last_success" 2>/dev/null || true; }

# keep the log under ~1MB
if [ -f "$LOG" ] && [ "$(stat -f %z "$LOG")" -gt 1048576 ]; then
  tail -c 262144 "$LOG" > "$LOG.tmp" && mv "$LOG.tmp" "$LOG"
fi

log "=== run start (dry_run=$DRY_RUN) ==="
record_attempt

# Guard: Drive must be mounted, otherwise skip entirely (exit 0) — nothing to
# fix by re-running; the volume simply isn't there yet.
if [ ! -d "$DRIVE" ]; then
  log "WARN: Google Drive not mounted at $DRIVE — skipping"
  notify "Work→Drive sync skipped" "Drive not mounted"
  exit 0
fi
mkdir -p "$DEST" 2>/dev/null || true

# Guard: an actual WRITE PROBE, not a `-w` test plus `mkdir -p` on a directory
# that already exists (both of those report success without ever attempting
# a write, which is why they never caught this). Finding f3eaf8d4
# (2026-08-08): the launchd execution context for com.owner.work-drive-sync can
# get EPERM writing under $DEST — a TCC/Full-Disk-Access grant scoped to the
# process that runs it, not to this user account — even while an interactive
# shell writes the same path fine. Granting Full Disk Access to the launchd
# job in System Settings > Privacy & Security is an operator action this
# script cannot perform (needs_operator, per the finding); what this script
# CAN do is detect the denial precisely and fail loud instead of letting
# control fall through to rsync, where it used to surface as a generic,
# unattributed rsync error.
PROBE="${DEST}.wds-write-probe.$$"
if ! : > "$PROBE" 2>/dev/null; then
  log "ERROR: cannot write under $DEST (Operation not permitted) — execution-context permission denial, not a mount issue. Grant Full Disk Access to the executing process in System Settings > Privacy & Security > Full Disk Access, then re-run. See finding f3eaf8d4."
  notify "Work→Drive sync BLOCKED" "No write permission at destination — needs Full Disk Access grant"
  exit 1
fi
rm -f "$PROBE" 2>/dev/null

# Defer to a running pull (com.owner.work-drive-pull) — push and pull must not overlap.
PULL_LOCK="$HOME/.config/work-drive-pull/lock"
pullpid="$(cat "$PULL_LOCK/pid" 2>/dev/null || echo "")"
if [ -n "$pullpid" ] && kill -0 "$pullpid" 2>/dev/null; then
  log "SKIP: work-drive-pull is running (pid $pullpid) — deferring to next scheduled run"
  exit 0
fi

# Single-instance lock (initial mirror can run for hours; daily job must not overlap).
# Locks older than 12h are reclaimed even if the recorded pid is alive (pid reuse after reboot).
if ! mkdir "$LOCKDIR" 2>/dev/null; then
  lockpid="$(cat "$LOCKDIR/pid" 2>/dev/null || echo "")"
  if [ -n "$(find "$LOCKDIR" -maxdepth 0 -mmin +720 2>/dev/null)" ]; then
    log "lock older than 12h (pid ${lockpid:-none}) — reclaiming"
    rm -rf "$LOCKDIR"; mkdir -p "$LOCKDIR"
  elif [ -n "$lockpid" ] && kill -0 "$lockpid" 2>/dev/null; then
    log "SKIP: another sync is running (pid $lockpid)"
    exit 0
  else
    log "stale lock (pid ${lockpid:-none}) — reclaiming"
    rm -rf "$LOCKDIR"; mkdir -p "$LOCKDIR"
  fi
fi
echo $$ > "$LOCKDIR/pid"
trap 'rm -rf "$LOCKDIR"' EXIT INT TERM

# ADDITIVE mirror: -a updates/adds only. NO --delete, ever.
ARGS=(-a --stats)
[ "$DRY_RUN" = "1" ] && ARGS+=(-n)
EXCLUDES=(
  --exclude=_worktrees/
  # Added 2026-08-05 (org cleanup): git worktree roots are ephemeral BUILD
  # state, not documents. Four of them (3.1 GB) were being pushed daily
  # because the only worktree exclude was `_worktrees/`, a name nothing used
  # any more. The mirror is additive and never deletes, so every stale lane
  # pushed here would have stayed on the company Drive permanently.
  --exclude=worktrees/
  --exclude='*-worktrees/'
  --exclude=.fusion-wt/
  --exclude=.wis/
  --exclude=unset/
  --exclude=node_modules/
  --exclude=.git/
  --exclude=.venv/
  --exclude=venv/
  --exclude=__pycache__/
  --exclude=.next/
  --exclude='*.sock'
  --exclude=.DS_Store
  --exclude=_FromDrive/
  --exclude=Agent_Transcripts_Last_24h/
  --exclude=Personal
  --exclude=Finance/
  --exclude='Finance & Legal/'
  --exclude='*.pem'
  --exclude='*.p12'
  --exclude='id_rsa*'
  --exclude='*credentials*.json'
  --exclude='*credentials*.env'
  --exclude='CREDENTIALS*'
  --exclude='*credential*.txt'
  --exclude='.env'
  --exclude='.env.*'
  --exclude='*.env'
  --exclude='*.keystore'
  --exclude='*.jks'
  --exclude=.npmrc
  --exclude=.netrc
  --exclude='id_ed25519*'
  --exclude='*.token'
)

OUT="$(mktemp)"
log "rsync start: $SRC -> $DEST"
"$RSYNC" "${ARGS[@]}" "${EXCLUDES[@]}" "$SRC" "$DEST" > "$OUT" 2>&1
rc=$?

# rsync 24 = source files vanished mid-run; normal on a live working dir.
if [ $rc -ne 0 ] && [ $rc -ne 24 ]; then
  log "ERROR: rsync exited $rc"
  tail -n 20 "$OUT" >> "$LOG"
  notify "Work→Drive sync FAILED" "rsync exit $rc — see work-drive-sync.log"
  rm -f "$OUT"
  # Propagate the real exit code (finding f3eaf8d4, step 2 of the admitted
  # fix) so a monitoring caller — or a manual `launchctl kickstart` check —
  # can distinguish a genuine failure from a clean run instead of always
  # seeing rc=0.
  exit 1
fi

# Parse --stats (openrsync/2.6.9 or rsync 3.x format; strip commas).
files_xfer="$(grep -E 'Number of (regular )?files transferred' "$OUT" | grep -oE '[0-9,]+' | tail -1 | tr -d ',')"
bytes_xfer="$(grep -E 'Total transferred file size' "$OUT" | grep -oE '[0-9,]+' | tail -1 | tr -d ',')"
files_xfer="${files_xfer:-0}"
bytes_xfer="${bytes_xfer:-0}"
mb=$((bytes_xfer / 1048576))
gb_tenths=$((bytes_xfer * 10 / 1073741824))

grep -E 'Number of|Total|speedup' "$OUT" | sed "s/^/[$(ts)]   /" >> "$LOG"
log "=== run end (rc=$rc): files=$files_xfer transferred=${mb}MB dry_run=$DRY_RUN ==="

if [ "$DRY_RUN" = "1" ]; then
  echo "DRY RUN summary ($SRC -> $DEST):"
  grep -E 'Number of|Total|speedup' "$OUT"
  echo "Would transfer: $files_xfer files, ${mb}MB"
else
  record_success
  notify "Work→Drive sync done" "$files_xfer files, ${mb}MB transferred"
  if [ "$bytes_xfer" -gt "$WARN_BYTES" ]; then
    log "WARN: moved >20GB in one run ($((gb_tenths / 10)).$((gb_tenths % 10))GB) — unexpected churn"
    notify "Work→Drive sync WARNING" "Moved over 20GB in one run — unexpected churn, check log"
  fi
fi

rm -f "$OUT"
exit 0
