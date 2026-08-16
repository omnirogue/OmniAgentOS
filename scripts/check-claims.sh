#!/usr/bin/env bash
# Mechanize the claims contract (loopqueue proposal claims).
#
# Prose alone does not stop two lanes claiming the same proposal — this script does.
#
#   scripts/check-claims.sh --id <proposal-id> --agent <actor> [--claims-dir <dir>]
#
# Exit codes (payload's own convention, sha256:1e581222...):
#   0 = held        marker exists, unexpired, actor field matches --agent
#   1 = not-held     no marker, marker held by someone else, or an expired marker
#                     (which is reaped/deleted before returning)
#   2 = malformed    marker exists but its JSON/id cannot be parsed or validated
#
# Marker format (matches the existing files under var/loopqueue/claims/):
#   {"actor": "<actor>", "at": "<ISO8601 UTC>", "expires_at": "<ISO8601 UTC>"}
#
# THIS SCRIPT DELETES FILES. Every `rm` below is treated as the dangerous operation
# it is: a wrong delete lets two builders build the same proposal simultaneously.
#
# Danger case #1: an unparseable marker younger than UNPARSEABLE_GRACE_SECS (default
# 600s / 10min) is a claim FILE MID-CREATION (the writer did open() then is still
# writing when we read it) — NOT an orphan. We must never delete it.
#
# Danger case #2 (TOCTOU): the decision to delete a marker is made from a snapshot
# read at one point in time. If a NEW claim is created (O_CREAT|O_EXCL) at the same
# path between that snapshot and the unlink() call, a naive `rm -f "$MARKER"` deletes
# the NEW, live claim, not the one we decided about. Every unlink site below re-stats
# the marker's (device, inode, size, mtime, ctime) immediately before deleting and aborts the
# delete — without touching the file — if identity changed underneath us. This closes
# the window to the stat-to-unlink gap (microseconds) rather than eliminating it
# outright; a true compare-and-delete would need a kernel primitive bash doesn't have.
# rename-aside was considered and rejected: an unconditional rename has exactly the
# same identity race as an unconditional rm, so it buys nothing without the same
# re-stat guard, and rm is simpler once the guard exists.
#
# Danger case #3 (clock skew): AGE is derived from `now - mtime`, both wall-clock
# reads. A forward NTP step / manual clock change of >= UNPARSEABLE_GRACE_SECS
# between a marker's creation and this check can make a seconds-old, still-being-written
# marker appear past-grace and get reaped. This is NOT fully solved here (there is no
# trusted time source available to a bash script) — RESIDUAL LIMIT, stated honestly:
# operators must keep claim-writing and claim-checking hosts NTP-synced (already an
# estate assumption elsewhere: launchd absolute-path jobs, ledger timestamps). What
# IS mitigated: a negative AGE (backward skew, or mtime racing ahead of a `date` read)
# is clamped to 0 rather than underflowing into a large unsigned-looking negative that
# certain arithmetic contexts could misread as "very old".
set -euo pipefail

UNPARSEABLE_GRACE_SECS="${UNPARSEABLE_GRACE_SECS:-600}"

usage() {
  echo "usage: check-claims.sh --id <proposal-id> --agent <actor> [--claims-dir <dir>]" >&2
  exit 2
}

ID=""
AGENT=""
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
CLAIMS_DIR="$REPO_ROOT/var/loopqueue/claims"

while [ $# -gt 0 ]; do
  case "$1" in
    --id) ID="${2:-}"; shift 2 ;;
    --agent) AGENT="${2:-}"; shift 2 ;;
    --claims-dir) CLAIMS_DIR="${2:-}"; shift 2 ;;
    -h|--help) usage ;;
    *) echo "check-claims.sh: unrecognized argument: $1" >&2; usage ;;
  esac
done

[ -n "$ID" ] || usage
[ -n "$AGENT" ] || usage

# F2 fix: allowlist the id BEFORE it ever touches a path. No '/', no '..', no shell
# metacharacters — only the characters our own producers ever write (sha256:hex, or
# a lane/session slug). Reject anything else as malformed; never silently strip.
if ! printf '%s' "$ID" | grep -Eq '^[A-Za-z0-9._:-]+$'; then
  echo "malformed: --id contains characters outside [A-Za-z0-9._:-]: $ID" >&2
  exit 2
fi
case "$ID" in
  *..*) echo "malformed: --id must not contain '..': $ID" >&2; exit 2 ;;
esac

mkdir -p -- "$CLAIMS_DIR" 2>/dev/null || true
CLAIMS_DIR_REAL="$(cd "$CLAIMS_DIR" 2>/dev/null && pwd -P)" || {
  echo "malformed: --claims-dir does not exist and could not be created: $CLAIMS_DIR" >&2
  exit 2
}

# Marker filenames on disk use underscores, not colons (matches existing convention:
# id "sha256:abcd" -> file "sha256_abcd.claim").
MARKER_NAME="$(printf '%s' "$ID" | tr ':' '_').claim"
MARKER="$CLAIMS_DIR_REAL/$MARKER_NAME"

# F2 fix, belt-and-suspenders: even with the allowlist above, verify the resolved
# marker path is actually a direct child of the resolved claims dir before we ever
# stat/read/unlink it. Compares resolved prefixes, never the raw input string.
case "$MARKER" in
  "$CLAIMS_DIR_REAL"/*) : ;;
  *)
    echo "malformed: resolved marker path escapes --claims-dir" >&2
    exit 2
    ;;
esac

now_epoch() { date -u +%s; }

# (device, inode, size, mtime, ctime) identity fingerprint of a path, or empty string
# if it does not exist. Used to detect "did this exact file get replaced out from
# under us" between the moment we decided to delete it and the moment we call rm (F1).
#
# GNU IS PROBED FIRST, AND THAT ORDER IS THE WHOLE POINT (2026-08-11). `-f` means
# "format string" to BSD stat but "report on the FILE SYSTEM" to GNU stat, and GNU
# takes no argument for it — so `stat -f '%d:%i:%m' "$f"` on Linux printed a
# filesystem status block (block size, FREE BLOCKS, FREE INODES) to stdout, failed
# on the format string it had treated as a filename, and the fallback then appended
# the real fingerprint underneath it. The captured "identity" was therefore a
# multi-line blob carrying counters that move whenever ANY process touches the
# filesystem: on Linux this guard both fired spuriously (unrelated churn read as
# "a newer claim landed") and — because it was never comparing the file at all in
# the way the header claims — could not be trusted when it did not fire. `-c` is
# GNU-only and BSD stat rejects it outright with a usage error and no stdout, so
# probing GNU first is the discrimination that actually works both ways.
#
# NANOSECOND TIMESTAMPS AND SIZE, not whole seconds. `%Y`/`%m` are seconds, and a
# reclaim is exactly the case where the replacement lands in the SAME second — on
# ext4/overlayfs the just-unlinked inode is routinely handed straight back, so
# device+inode+second is identical for a genuinely different file and the reap
# proceeded on the live claim. That is the CI-only half of this defect (APFS does
# not recycle inode numbers, so macOS never saw it). Size and ctime are carried for
# the same reason: an in-place rewrite keeps the inode and can keep the mtime.
#
# A PROBE'S OUTPUT IS ONLY ITS OUTPUT IF THE PROBE SUCCEEDED (2026-08-11, review
# round 2). Streaming each attempt straight to this function's stdout meant a
# FAILED attempt still contributed its bytes to the captured fingerprint: the
# first version of this fix rejected the wrong dialect by exit status but had
# already emitted whatever that dialect printed. A stat that prints a
# file-independent blob and then exits non-zero — precisely the GNU `-f`
# behaviour this function exists to defend against — therefore produced the SAME
# fingerprint for two different files, which turns the identity check into a
# tautology and lets safe_reap delete a live claim. Each attempt is now captured,
# and only a successful, single-line, fingerprint-SHAPED result is emitted.
#
# rc 0: fingerprint on stdout | rc 1: path is absent | rc 2: path exists but no
# probe could fingerprint it. Those last two are opposite verdicts for a caller
# — "nothing to protect" vs "cannot verify, so protect it" — and collapsing them
# into "empty string" is what makes an unknown read as the favourable answer.
_fingerprint_shaped() {
  # Digits, colons and dots only. Rejects the empty string, any whitespace and
  # therefore any multi-line status blob, and every word of prose a usage or
  # error message could carry.
  case "$1" in
    ''|*[!0-9:.]*) return 1 ;;
  esac
  case "$1" in
    *:*:*:*:*) return 0 ;;
  esac
  return 1
}

marker_identity() {
  local f="$1" out
  if out="$(stat -c '%d:%i:%s:%.9Y:%.9Z' "$f" 2>/dev/null)" \
     && _fingerprint_shaped "$out"; then   # GNU/coreutils
    printf '%s\n' "$out"
    return 0
  fi
  if out="$(stat -f '%d:%i:%z:%Fm:%Fc' "$f" 2>/dev/null)" \
     && _fingerprint_shaped "$out"; then   # BSD/macOS
    printf '%s\n' "$out"
    return 0
  fi
  [ -e "$f" ] || return 1
  return 2
}

# rc 0: epoch seconds on stdout | rc 1: no usable mtime. Same capture-then-validate
# rule as marker_identity, and the same GNU-first order for the same reason:
# `stat -f %m` is a filesystem query on Linux, not an mtime, and its exit status
# is not a reliable "am I BSD" signal.
file_mtime_epoch() {
  local f="$1" out
  if out="$(stat -c %Y "$f" 2>/dev/null)"; then     # GNU
    case "$out" in ''|*[!0-9]*) ;; *) printf '%s\n' "$out"; return 0 ;; esac
  fi
  if out="$(stat -f %m "$f" 2>/dev/null)"; then     # BSD/macOS
    case "$out" in ''|*[!0-9]*) ;; *) printf '%s\n' "$out"; return 0 ;; esac
  fi
  return 1
}

# F1 fix: delete a marker only if its identity is unchanged from SNAPSHOT taken
# earlier in this run. If something replaced/recreated the file in between (a fresh
# O_CREAT|O_EXCL claim landing on the same path), abort without touching it.
#
# rc 0: deleted | rc 1: already gone | rc 2: identity changed, left alone |
# rc 3: identity could not be established (at snapshot time OR now), left alone.
#
# F1 fix, round 3 (fail-then-recover, 2026-08-11): $snapshot_known must be passed
# explicitly, not inferred from `[ -z "$snapshot" ]`. The caller's initial identity
# probe can itself fail (SNAPSHOT_RC 2, SNAPSHOT left empty) while stat later
# RECOVERS by the time this function runs. Inferring "unknown" from an empty
# string collapsed onto the exact same branch as "known, and now_identity is
# empty" (which cannot actually happen — a successful marker_identity always
# emits a non-empty fingerprint) but, worse, that branch returns rc 2, which
# every caller maps to not-held/exit 1. That means a marker whose identity was
# NEVER established at read time — not one we compared and found genuinely
# unchanged-or-changed — could be reported not-held while a live marker (the
# very one the TOCTOU hook just wrote) sits on disk untouched: a second loop
# reads "not held" and starts, and now two loops hold the same item. The whole
# point of "an unfingerprintable marker is never not-held" (round 2) is violated
# by this path alone, because it never even reaches the "could not be
# fingerprinted" check above — that check only guards the CURRENT read, not the
# snapshot this delete decision was supposed to be relative to. A snapshot we
# never had is not evidence the identity changed; it is simply evidence we
# cannot make the safety claim this function exists to make, and that must
# always resolve to malformed/rc 3, never to identity-changed/rc 2.
safe_reap() {
  local f="$1" snapshot="$2" snapshot_known="${3:-1}"
  local now_identity="" id_rc=0
  # Test-only injection point: lets the regression suite deterministically simulate
  # "something replaced this file between our snapshot and our unlink" without
  # relying on wall-clock process racing (which is flaky under subprocess-startup
  # jitter). Inert unless CHECK_CLAIMS_TEST_TOCTOU_HOOK is set, which only happens
  # from tests/scripts/test_check_claims.py.
  if [ -n "${CHECK_CLAIMS_TEST_TOCTOU_HOOK:-}" ]; then
    eval "$CHECK_CLAIMS_TEST_TOCTOU_HOOK"
  fi
  if now_identity="$(marker_identity "$f")"; then
    id_rc=0
  else
    id_rc=$?
  fi
  if [ "$id_rc" -eq 1 ]; then
    # Already gone (someone else reaped it, or it was never really there) — nothing
    # for us to do, and nothing to report as "we deleted it".
    return 1
  fi
  if [ "$id_rc" -ne 0 ] || [ -z "$now_identity" ]; then
    # The file is THERE and we cannot fingerprint it, so we cannot tell our claim
    # from a live one that landed on the same path. An unverifiable identity is
    # never a licence to delete.
    echo "abort-reap: $f could not be fingerprinted (no usable stat) - refusing to delete what we cannot identify" >&2
    return 3
  fi
  if [ "$snapshot_known" != "1" ]; then
    # We have a usable CURRENT identity but never had one to compare it against.
    # That is not "identity changed" (rc 2) — it is "we cannot verify this is the
    # same file we decided to reap" (rc 3), and it must be indistinguishable from
    # the current-probe-failed case above: both mean "cannot certify, don't delete".
    echo "abort-reap: $f original identity could not be established at read time - refusing to delete what we never fingerprinted" >&2
    return 3
  fi
  if [ "$now_identity" != "$snapshot" ]; then
    echo "abort-reap: $f identity changed since read ($snapshot -> $now_identity) - a newer claim may have landed here, leaving it alone" >&2
    return 2
  fi
  rm -f -- "$f"
  return 0
}

if [ ! -e "$MARKER" ]; then
  echo "not-held: no claim marker for $ID" >&2
  exit 1
fi

# An unfingerprintable marker leaves SNAPSHOT empty, and that is deliberately NOT
# refused here. The snapshot on its own deletes nothing; the refusal belongs at
# the delete site, where safe_reap treats both a never-established snapshot and an
# unavailable current identity as "do not touch this" (rc 3 -> malformed, exit 2).
# Refusing here instead would return before the reap path is reached at all,
# which reads as the same verdict but stops exercising the guard that has to hold
# when the stat dialects go missing mid-run rather than at startup.
#
# SNAPSHOT_KNOWN travels with SNAPSHOT to the delete site (round 3, 2026-08-11):
# an empty SNAPSHOT string is not itself distinguishable from "never established"
# vs. some hypothetical legitimate empty identity (there is no such thing —
# _fingerprint_shaped rejects the empty string, so success always yields a
# non-empty fingerprint). But relying on that invariant implicitly, by testing
# `[ -z "$snapshot" ]` at the delete site, previously let a RECOVERED stat at
# delete time read the never-established snapshot as merely "different from
# now" (rc 2 / not-held) instead of "never verified" (rc 3 / malformed). Passing
# the explicit boolean removes that inference entirely.
SNAPSHOT=""
SNAPSHOT_RC=0
if SNAPSHOT="$(marker_identity "$MARKER")"; then
  SNAPSHOT_RC=0
else
  SNAPSHOT_RC=$?
fi
SNAPSHOT_KNOWN=0
[ "$SNAPSHOT_RC" -eq 0 ] && SNAPSHOT_KNOWN=1
if [ "$SNAPSHOT_RC" -eq 1 ]; then
  # It existed a line ago and does not now: someone else reaped it between the
  # -e test and this stat. Nothing is held, and nothing here deleted anything.
  echo "not-held: claim marker for $ID vanished while reading it" >&2
  exit 1
fi

# F3 + F5 fix: parse JSON *and* the ISO8601 expires_at in a single python3 call
# (python3 is already a hard dependency; stop sniffing GNU vs BSD `date` dialects,
# which silently disagree on fractional seconds and explicit UTC offsets). Capture
# the real exit status with an `if` guard, not `... || true` (which always yields 0
# and made the old PARSE_RC check dead code).
PY_OUT=""
PY_RC=0
if PY_OUT="$(python3 - "$MARKER" <<'PYEOF' 2>/dev/null
import datetime as dt
import json
import sys

path = sys.argv[1]
try:
    with open(path) as fh:
        d = json.load(fh)
    actor = d.get("actor")
    expires_at = d.get("expires_at")
    if not isinstance(actor, str) or not isinstance(expires_at, str) or not actor or not expires_at:
        raise ValueError("missing/invalid actor or expires_at")

    # Accept the bare-Z form we write today, an explicit +00:00/-00:00 offset, and
    # fractional seconds of any length. datetime.fromisoformat (3.11+) already
    # handles 'Z' and offsets; normalize 'Z' defensively for older 3.x too.
    ts = expires_at
    if ts.endswith("Z"):
        ts = ts[:-1] + "+00:00"
    parsed = dt.datetime.fromisoformat(ts)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    epoch = int(parsed.astimezone(dt.timezone.utc).timestamp())

    print(actor)
    print(epoch)
except Exception:
    sys.exit(1)
PYEOF
)"; then
  PY_RC=0
else
  PY_RC=$?
fi

if [ "$PY_RC" -ne 0 ] || [ -z "$PY_OUT" ]; then
  # Unparseable JSON or unparseable/missing expires_at. Age it before deciding
  # anything — see "Danger case #1" above.
  # An unstattable marker yields no mtime at all, and the empty string must not be
  # allowed to render as "epoch 0", i.e. as maximally old and therefore reapable —
  # absence never renders as the favourable value. Treat it as brand new, which is
  # the same conservative direction Danger case #1 takes.
  MTIME="$(file_mtime_epoch "$MARKER" || true)"
  case "$MTIME" in ''|*[!0-9]*) MTIME="$(now_epoch)" ;; esac
  AGE=$(( $(now_epoch) - MTIME ))
  [ "$AGE" -lt 0 ] && AGE=0   # F4 mitigation: clamp backward-skew negative age to 0
  if [ "$AGE" -lt "$UNPARSEABLE_GRACE_SECS" ]; then
    echo "malformed: unparseable marker for $ID is only ${AGE}s old (< ${UNPARSEABLE_GRACE_SECS}s grace) - leaving it in place, may be mid-creation" >&2
    exit 2
  fi
  REAP_RC=0
  safe_reap "$MARKER" "$SNAPSHOT" "$SNAPSHOT_KNOWN" || REAP_RC=$?
  case "$REAP_RC" in
    0)
      echo "not-held: reaped orphaned unparseable marker for $ID (age ${AGE}s)" >&2
      ;;
    3)
      # Unverifiable, so untouched — and reported as malformed rather than
      # not-held, because "not held" is the answer that lets a second builder
      # start against a claim we were never able to read.
      echo "malformed: unparseable marker for $ID could not be fingerprinted - left in place" >&2
      exit 2
      ;;
    *)
      echo "not-held: unparseable marker for $ID no longer matches what we read (concurrent change) - left alone" >&2
      ;;
  esac
  exit 1
fi

OWNER_ACTOR="$(printf '%s\n' "$PY_OUT" | sed -n '1p')"
EXPIRES_EPOCH="$(printf '%s\n' "$PY_OUT" | sed -n '2p')"

if [ "$(now_epoch)" -ge "$EXPIRES_EPOCH" ]; then
  # Expired claims are deleted on sight — but only if nothing replaced this exact
  # marker between our read and now (F1).
  REAP_RC=0
  safe_reap "$MARKER" "$SNAPSHOT" "$SNAPSHOT_KNOWN" || REAP_RC=$?
  case "$REAP_RC" in
    0)
      echo "not-held: expired claim for $ID by $OWNER_ACTOR reaped" >&2
      ;;
    3)
      # Same conservative verdict as the unparseable path: an identity we cannot
      # establish is not an expiry we can act on.
      echo "malformed: expired claim for $ID by $OWNER_ACTOR could not be fingerprinted - left in place" >&2
      exit 2
      ;;
    *)
      echo "not-held: expired claim for $ID by $OWNER_ACTOR - marker changed underneath us, left the current one alone" >&2
      ;;
  esac
  exit 1
fi

if [ "$OWNER_ACTOR" = "$AGENT" ]; then
  echo "held: $ID held by $AGENT" >&2
  exit 0
fi

echo "not-held: $ID held by $OWNER_ACTOR, not $AGENT" >&2
exit 1
