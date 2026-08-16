#!/bin/zsh
# Work <- Drive pull: promotes files dropped into "_FromDrive/" folders under
# Drive:/Work into the matching local ~/Work path. Sibling of work-drive-sync.sh.
# Installed 2026-08-01 (org upgrade), adversarially reviewed pre-ship.
# Runs daily via launchd: com.owner.work-drive-pull (07:00, before the 08:30 push;
# the push also defers if this job is still running).
#
# How it works (full doc: ~/README.md):
#   Anyone drops files/folders into a folder literally named "_FromDrive" inside
#   any folder of Drive:/Work. For each dropped item this job:
#     1. copies it (rsync -a, mtimes preserved) to the matching LOCAL folder:
#        Drive:/Work/X/_FromDrive/item -> ~/X/item
#     2. byte-verifies the copy (cmp for files, diff -rq for folders; any diff
#        output at all, including permission errors, counts as failure)
#     3. moves the Drive-side item out of the drop zone to its final mirrored
#        place (Drive:/Work/X/item) — a rename, no re-upload — so the daily push
#        sees both sides identical and no-ops.
#   NEVER overwrites local data: name collisions get a "-fromdrive-<timestamp>"
#   suffix, applied to BOTH sides so paths stay mirrored.
#   Google-native files (.gdoc/.gsheet/.gvid/...) cannot exist locally: they are
#   moved to their final Drive-side place and logged, not pulled.
#   Top-level symlinks in drop zones are skipped; symlinks INSIDE a dropped
#   folder are copied as symlinks (never followed).
#   Local targets are containment-checked under ~/Work BEFORE anything is
#   created (a target resolving elsewhere — e.g. through the Personal iCloud
#   symlink — is refused; refusals are policy events, not failures).
#   An item that fails copy/verify 3 times is parked (SKIP-BACKOFF) and no
#   longer retried; you get one banner when that happens.
# Log: ~/Library/Logs/work-drive-pull.log
# Manifest (undo trail, TSV: ts/status/drive_side/local_side/note; rotated at 5MB):
#   ~/Library/Logs/work-drive-pull-manifest.tsv
# Dry run: DRY_RUN=1 ~/bin/work-drive-pull.sh

set -u
# Hardened PATH — ~/.zshenv prepends user bins (.local/bin, .grok/bin); this wins.
export PATH=/usr/bin:/bin:/usr/sbin:/sbin

DRIVE="$HOME/Library/CloudStorage/GoogleDrive-owner@acmeuni.example/My Drive"
SRC_ROOT="$DRIVE/Work"
LOCAL_ROOT="$HOME/Work"
LOG="$HOME/Library/Logs/work-drive-pull.log"
LAUNCHD_LOG="$HOME/Library/Logs/work-drive-pull.launchd.log"
MANIFEST="$HOME/Library/Logs/work-drive-pull-manifest.tsv"
CFG="$HOME/.config/work-drive-pull"
LOCKDIR="$CFG/lock"
FAILDB="$CFG/fail-counts.tsv"
REFUSED_STAMP="$CFG/refused-banner-date"
RSYNC="/usr/bin/rsync"
DRY_RUN="${DRY_RUN:-0}"
WARN_KB=$((10 * 1024 * 1024))   # >10GB pulled in one run = investigate
MAX_FAILS=3

ts() { date "+%Y-%m-%d %H:%M:%S"; }
log() { echo "[$(ts)] $*" >> "$LOG"; }
notify() { /usr/bin/osascript -e "display notification \"$2\" with title \"$1\"" >/dev/null 2>&1 || true; }
manifest() {
  local a="${1//[$'\t\n']/ }" b="${2//[$'\t\n']/ }" c="${3//[$'\t\n']/ }" d="${4//[$'\t\n']/ }"
  printf '%s\t%s\t%s\t%s\t%s\n' "$(ts)" "$a" "$b" "$c" "$d" >> "$MANIFEST"
}

mkdir -p "$CFG"

# keep the logs under ~1MB; archive the manifest at 5MB (it is the undo trail — never truncate)
for f in "$LOG" "$LAUNCHD_LOG"; do
  if [ -f "$f" ] && [ "$(stat -f %z "$f")" -gt 1048576 ]; then
    tail -c 262144 "$f" > "$f.tmp" && mv "$f.tmp" "$f"
  fi
done
if [ -f "$MANIFEST" ] && [ "$(stat -f %z "$MANIFEST")" -gt 5242880 ]; then
  mv "$MANIFEST" "$MANIFEST.$(date +%Y%m%d%H%M%S)"
fi

log "=== run start (dry_run=$DRY_RUN) ==="

# Guard: Drive:/Work must exist (Drive mounted and mirror promoted).
if [ ! -d "$SRC_ROOT" ]; then
  log "SKIP: Drive Work folder not available at $SRC_ROOT"
  exit 0
fi

# Single-instance lock. Locks older than 6h are reclaimed even if the recorded
# pid is alive — after a reboot the pid is likely reused by an unrelated process.
if ! mkdir "$LOCKDIR" 2>/dev/null; then
  lockpid="$(cat "$LOCKDIR/pid" 2>/dev/null || echo "")"
  if [ -n "$(find "$LOCKDIR" -maxdepth 0 -mmin +360 2>/dev/null)" ]; then
    log "lock older than 6h (pid ${lockpid:-none}) — reclaiming"
    rm -rf "$LOCKDIR"; mkdir -p "$LOCKDIR"
  elif [ -n "$lockpid" ] && kill -0 "$lockpid" 2>/dev/null; then
    log "SKIP: another pull is running (pid $lockpid)"
    exit 0
  else
    log "stale lock (pid ${lockpid:-none}) — reclaiming"
    rm -rf "$LOCKDIR"; mkdir -p "$LOCKDIR"
  fi
fi
echo $$ > "$LOCKDIR/pid"
trap 'rm -rf "$LOCKDIR"' EXIT INT TERM

ROOT_REAL="$(cd "$LOCAL_ROOT" && pwd -P)" || { log "ERROR: cannot resolve $LOCAL_ROOT"; exit 0; }

# Resolve a path that may not exist yet: walk up to the nearest existing
# ancestor and resolve that. Works identically in dry-run and real mode.
resolve_existing() {
  local d="$1"
  while [ ! -d "$d" ] && [ "$d" != "/" ]; do d="${d:h}"; done
  (cd "$d" 2>/dev/null && pwd -P)
}

same_content() {  # $1=src $2=dest -> 0 iff byte-identical and fully readable
  if [ -f "$1" ] && [ -f "$2" ]; then /usr/bin/cmp -s "$1" "$2"; return $?; fi
  if [ -d "$1" ] && [ -d "$2" ]; then
    local out
    out="$(/usr/bin/diff -rq "$1" "$2" 2>&1)"
    [ -z "$out" ]; return $?
  fi
  return 1
}

fail_count() { awk -F'\t' -v k="$1" '$2==k{n++} END{print n+0}' "$FAILDB" 2>/dev/null; }
fail_add()   { printf '%s\t%s\n' "$(ts)" "$1" >> "$FAILDB"; }
fail_clear() {
  [ -f "$FAILDB" ] || return 0
  awk -F'\t' -v k="$1" '$2!=k' "$FAILDB" > "$FAILDB.tmp" && mv "$FAILDB.tmp" "$FAILDB"
}

item_kb() {  # logical size (placeholder-safe, unlike du on a streaming DriveFS)
  local b
  b="$(find "$1" -type f -exec stat -f %z {} + 2>/dev/null | awk '{s+=$1} END{print s+0}')"
  echo $(( ${b:-0} / 1024 ))
}

pulled=0; pulled_kb=0; native=0; dup=0; skipped=0; refused=0; backoff=0; failed=0

find "$SRC_ROOT" -maxdepth 25 -type d -name "_FromDrive" -print0 2>>"$LOG" |
while IFS= read -r -d '' zone; do
  rel="${zone#"$SRC_ROOT"}"; rel="${rel#/}"; rel="${rel%_FromDrive}"; rel="${rel%/}"
  case "/$rel/" in (*/_FromDrive/*) log "SKIP nested drop zone: $zone"; continue;; esac
  target="$LOCAL_ROOT${rel:+/$rel}"
  drive_final_dir="$SRC_ROOT${rel:+/$rel}"

  # Containment BEFORE creating anything: nearest existing ancestor must resolve
  # under ~/Work. Blocks e.g. Drive:/Work/Personal/... (local Personal -> iCloud).
  anc_real="$(resolve_existing "$target")"
  case "$anc_real/" in
    ("$ROOT_REAL"/*) ;;
    (*) if [ -n "$(ls -A "$zone" 2>/dev/null)" ]; then
          log "REFUSE (policy): target resolves outside ~/Work: zone=$zone resolved=${anc_real:-?}"
          manifest REFUSED "$zone" "$target" "outside ~/Work"
          refused=$((refused+1))
        fi
        continue;;
  esac

  find "$zone" -mindepth 1 -maxdepth 1 -print0 2>>"$LOG" |
  while IFS= read -r -d '' item; do
    name="$(basename "$item")"

    # macOS junk: drop silently. Other dot-named items are pulled like anything else.
    case "$name" in (.DS_Store|.localized) continue;; esac

    if [ -L "$item" ]; then
      log "SKIP symlink in drop zone: $item"
      manifest SKIP "$item" "-" "symlink"
      skipped=$((skipped+1)); continue
    fi

    # Google-native stubs: place Drive-side only, never pulled.
    case "$name" in
      (*.gdoc|*.gsheet|*.gslides|*.gdraw|*.gform|*.gtable|*.gmap|*.gsite|*.gjam|*.gvid|*.glink|*.gnote|*.gshortcut)
        fin="$drive_final_dir/$name"
        if [ -e "$fin" ]; then
          fin="$drive_final_dir/${name%.*}-fromdrive-$(date +%Y%m%d%H%M%S).${name##*.}"
        fi
        if [ "$DRY_RUN" = "1" ]; then
          log "DRY: would place Google-native $item -> $fin"
        elif mv "$item" "$fin" 2>/dev/null; then
          log "NATIVE placed (lives in Drive only): $fin"
          manifest NATIVE "$fin" "-" "google-native"
          native=$((native+1))
        else
          log "FAILED to place native item: $item"
          manifest FAILED "$item" "-" "native mv failed"
          failed=$((failed+1))
        fi
        continue;;
    esac

    # Backoff: an item that failed MAX_FAILS times is parked, not retried forever.
    key="${item//[$'\t\n']/ }"
    fails="$(fail_count "$key")"; fails="${fails:-0}"
    if [ "$fails" -ge "$MAX_FAILS" ]; then
      if [ "$fails" -eq "$MAX_FAILS" ] && [ "$DRY_RUN" != "1" ]; then
        log "BACKOFF: $item failed $MAX_FAILS times — parked until removed from drop zone (or delete $FAILDB entry)"
        manifest BACKOFF "$item" "-" "parked after $MAX_FAILS failures"
        notify "Work←Drive pull" "Item parked after $MAX_FAILS failed pulls — see log"
        fail_add "$key"   # bump past MAX so this banner fires once
      else
        log "SKIP-BACKOFF: $item (${fails} recorded failures)"
      fi
      backoff=$((backoff+1)); continue
    fi

    dest="$target/$name"
    fin="$drive_final_dir/$name"

    # Already pulled earlier (identical content)? Just finish placing the Drive copy.
    if [ -e "$dest" ] && same_content "$item" "$dest"; then
      if [ "$DRY_RUN" = "1" ]; then
        log "DRY: already pulled, would place drive copy: $item -> $fin"
      elif [ -e "$fin" ]; then
        log "DUP: already pulled and mirrored; duplicate left in drop zone for review: $item"
        manifest DUP "$item" "$dest" "identical; final also exists"
        dup=$((dup+1))
      elif mv "$item" "$fin" 2>/dev/null; then
        log "RE-PLACED drive copy for previously pulled item: $fin"
        manifest REPLACED "$fin" "$dest" ""
      fi
      continue
    fi

    # Collision on either side -> one suffixed name used on BOTH sides.
    if [ -e "$dest" ] || [ -L "$dest" ] || [ -e "$fin" ]; then
      stamp="$(date +%Y%m%d%H%M%S)"
      ext="${name##*.}"
      base="${name%.*}"
      if [ -d "$item" ] || [ "$name" = "$ext" ] || [ -z "$base" ]; then
        nn="${name}-fromdrive-$stamp"
      else
        nn="${base}-fromdrive-${stamp}.${ext}"
      fi
      if [ -e "$target/$nn" ] || [ -e "$drive_final_dir/$nn" ]; then
        log "FAILED: collision name also taken ($nn) — retry next run: $item"
        manifest FAILED "$item" "$target/$nn" "double collision"
        failed=$((failed+1)); continue
      fi
      log "collision: $name -> $nn"
      dest="$target/$nn"; fin="$drive_final_dir/$nn"
    fi

    kb="$(item_kb "$item")"
    if [ "$DRY_RUN" = "1" ]; then
      log "DRY: would pull $item (${kb}KB) -> $dest"
      continue
    fi

    mkdir -p "$target" 2>/dev/null

    ok=0
    if [ -d "$item" ]; then
      "$RSYNC" -a "$item/" "$dest/" >/dev/null 2>&1 && same_content "$item" "$dest" && ok=1
    else
      "$RSYNC" -a "$item" "$dest" >/dev/null 2>&1 && same_content "$item" "$dest" && ok=1
    fi

    if [ "$ok" = "1" ]; then
      fail_clear "$key"
      if mv "$item" "$fin" 2>/dev/null; then
        log "PULLED: $fin (${kb}KB) -> $dest"
        manifest PULLED "$fin" "$dest" "${kb}KB"
      else
        log "PULLED (drive copy still in drop zone, will re-place next run): $item -> $dest"
        manifest PULLED "$item" "$dest" "${kb}KB; drive mv failed"
      fi
      pulled=$((pulled+1)); pulled_kb=$((pulled_kb+kb))
    else
      # $dest was verified free above (or is our fresh suffixed name), and its
      # parent resolves inside ~/Work (containment check at zone level) — this
      # only ever removes the partial copy this run just wrote.
      rm -rf "$dest" 2>/dev/null
      fail_add "$key"
      log "FAILED copy/verify (partial removed, item left in drop zone, fail $((fails+1))/$MAX_FAILS): $item"
      manifest FAILED "$item" "$dest" "copy/verify failed ($((fails+1))/$MAX_FAILS)"
      failed=$((failed+1))
    fi
  done
done

log "=== run end: pulled=$pulled (~$((pulled_kb/1024))MB) native=$native dup=$dup skipped=$skipped refused=$refused backoff=$backoff failed=$failed dry_run=$DRY_RUN ==="

if [ "$DRY_RUN" != "1" ] && [ $((pulled + native + failed + dup)) -gt 0 ]; then
  notify "Work←Drive pull" "$pulled pulled (~$((pulled_kb/1024))MB), $native native, $dup dup, $failed failed"
fi

# Policy refusals banner at most once per day (not counted as failures).
if [ "$DRY_RUN" != "1" ] && [ "$refused" -gt 0 ]; then
  today="$(date +%Y-%m-%d)"
  if [ "$(cat "$REFUSED_STAMP" 2>/dev/null || echo "")" != "$today" ]; then
    notify "Work←Drive pull" "$refused drop zone(s) refused by policy (outside ~/Work) — see log"
    echo "$today" > "$REFUSED_STAMP"
  fi
fi

if [ "$pulled_kb" -gt "$WARN_KB" ]; then
  log "WARN: pulled >10GB in one run — verify drop zone contents were intentional"
  notify "Work←Drive pull WARNING" "Pulled over 10GB in one run — check log"
fi
exit 0
