#!/bin/sh
# Capture the browser/control-plane identity boundary without changing state.
#
# The matrix is deliberately small but complete: each credential class reaches
# every served surface with a public read, a session-gated read, a mutation, and
# a Tier-P/autonomy route.  The two write requests have invalid empty payloads;
# their status shows which authentication boundary was crossed without creating
# a task or changing an autonomy setting.
#
# Two properties are load-bearing and are enforced, not assumed:
#   1. An interrupted run never produces an artifact.  A partial matrix that
#      still looks like a baseline would freeze a fiction as the expectation.
#   2. A baseline made of SKIP cells is refused.  A gate that compares nothing
#      to nothing is vacuously green and measures no identity boundary at all.
set -eu

SCRIPT_DIR=$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd)
ROOT_DIR=$(CDPATH='' cd -- "$SCRIPT_DIR/../.." && pwd)
BASELINE="$SCRIPT_DIR/identity-topology-probe-baseline.txt"
PORTS="8490,3003,8485"
MACHINE_TOKEN_FILE="${OMNIAGENTOS_MACHINE_TOKEN_FILE:-$ROOT_DIR/var/secrets/sessions-token}"
WRITE_BASELINE=0
ALLOW_SKIPS=0
CREDENTIALS='no-creds forged-owner forged-non-owner machine-token'
ROUTES='public-get gated-get mutation autonomy-route'
COLUMN_HEADER='credential|port|route|status'

usage() {
  cat <<'USAGE'
usage: scripts/gates/identity-topology-probe.sh [options]

Capture the P0 identity topology matrix and compare it with its revision-stamped
baseline. No request contains a valid mutation payload.

Options:
  --baseline PATH            baseline to compare (default: beside this script)
  --machine-token-file PATH  file holding the machine session token
  --ports P1,P2,P3           ports to probe (default: 8490,3003,8485)
  --write-baseline           explicitly write the captured matrix as a baseline
  --allow-skips              permit writing a baseline that still has SKIP cells;
                             the count is recorded as "# skipped_cells=N"
  -h, --help                 show this help

The output never contains credential values. An unreachable port is recorded as
SKIP:unreachable for each of its cells; an unreadable machine token is recorded
as SKIP:machine-token-unavailable for that credential's cells.

Refusals (exit 2):
  * an interrupted or truncated capture (row count or revision stamp missing);
  * --write-baseline while any cell is SKIP:unreachable, without --allow-skips;
  * writing or comparing a baseline whose every cell is SKIP;
  * a machine token containing " or \, which cannot be carried safely.
USAGE
}

while [ "$#" -gt 0 ]; do
  case "$1" in
    --baseline)
      [ "$#" -ge 2 ] || { echo "identity-topology-probe: --baseline needs a path" >&2; exit 2; }
      BASELINE=$2; shift 2 ;;
    --machine-token-file)
      [ "$#" -ge 2 ] || { echo "identity-topology-probe: --machine-token-file needs a path" >&2; exit 2; }
      MACHINE_TOKEN_FILE=$2; shift 2 ;;
    --ports)
      [ "$#" -ge 2 ] || { echo "identity-topology-probe: --ports needs a comma-separated list" >&2; exit 2; }
      PORTS=$2; shift 2 ;;
    --write-baseline) WRITE_BASELINE=1; shift ;;
    --allow-skips) ALLOW_SKIPS=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "identity-topology-probe: unknown option: $1" >&2; usage >&2; exit 2 ;;
  esac
done

case "$PORTS" in
  *[!0-9,]*|,*|*,|""|*,,* )
    echo "identity-topology-probe: --ports must be comma-separated TCP port numbers" >&2
    exit 2
    ;;
esac

for port in $(printf '%s' "$PORTS" | tr ',' ' '); do
  if ! awk -v port="$port" 'BEGIN { exit !(port >= 1 && port <= 65535) }'; then
    echo "identity-topology-probe: --ports port must be between 1 and 65535: $port" >&2
    exit 2
  fi
done

# A directory (or any unreadable path) here must degrade to the documented
# SKIP:machine-token-unavailable, not kill the script under `set -e` with no
# message at all.
MACHINE_TOKEN=""
if [ -f "$MACHINE_TOKEN_FILE" ] && [ -r "$MACHINE_TOKEN_FILE" ]; then
  MACHINE_TOKEN=$(tr -d '\r\n' < "$MACHINE_TOKEN_FILE" 2>/dev/null) || MACHINE_TOKEN=""
fi

# The token is carried in a quoted curl config value. A token containing a
# double quote or a backslash would terminate or re-escape that value, so curl
# would send a *different* header than the matrix claims. Refuse rather than
# publish a silently wrong topology.
case "$MACHINE_TOKEN" in
  *[\"\\]*)
    echo "identity-topology-probe: REFUSED: machine token contains a quote or backslash; it cannot be carried in a curl config header without changing the request, which would make the matrix silently wrong ($MACHINE_TOKEN_FILE)" >&2
    exit 2
    ;;
esac

CREDENTIAL_COUNT=$(printf '%s\n' $CREDENTIALS | wc -l | tr -d ' ')
ROUTE_COUNT=$(printf '%s\n' $ROUTES | wc -l | tr -d ' ')

TMP=$(mktemp "${TMPDIR:-/tmp}/identity-topology-probe.XXXXXX")
# Owner-only and unpredictable: this file holds the live machine token.
CURL_CONFIG=$(umask 077; mktemp "${TMPDIR:-/tmp}/identity-topology-probe-curl.XXXXXX")

cleanup() {
  rm -f "$TMP" "$TMP.rows" "$TMP.body" "$TMP.expected" "$CURL_CONFIG"
}
# EXIT cleans up; the signal traps clean up *and* exit. Without the exit, a
# signalled run resumes with its scratch files deleted, silently rebuilds a
# partial matrix and can persist it as a baseline while reporting success.
trap cleanup EXIT
trap 'cleanup; exit 130' HUP INT TERM

: > "$TMP.rows"

request_status() {
  method=$1
  url=$2
  body=$3
  credential=$4
  # Do not interpolate a real token in a shell word. curl receives it via a
  # temporary config file, which is owner-only and removed by the traps.
  : > "$CURL_CONFIG"
  case "$credential" in
    machine-token) printf 'header = "X-Session-Token: %s"\n' "$MACHINE_TOKEN" > "$CURL_CONFIG" ;;
    forged-owner) printf '%s\n' 'header = "Tailscale-User-Login: owner@invalid.example"' > "$CURL_CONFIG" ;;
    forged-non-owner) printf '%s\n' 'header = "Tailscale-User-Login: attacker@invalid.example"' > "$CURL_CONFIG" ;;
  esac
  if [ -n "$body" ]; then
    printf 'header = "Content-Type: application/json"\n' >> "$CURL_CONFIG"
    printf 'data = "%s"\n' "$body" >> "$CURL_CONFIG"
  fi
  status=$(curl --silent --show-error --output /dev/null --write-out '%{http_code}' \
    --connect-timeout 1 --max-time 3 --request "$method" --config "$CURL_CONFIG" "$url" 2>/dev/null) || status=000
  # Truncate rather than remove: the file keeps its owner-only mode for the
  # next request, and no credential outlives the request that used it.
  : > "$CURL_CONFIG"
  printf '%s' "$status"
}

append_port_matrix() {
  port=$1
  reachable=$2
  for credential in $CREDENTIALS; do
    for route in $ROUTES; do
      if [ "$reachable" = 0 ]; then
        status='SKIP:unreachable'
      elif [ "$credential" = machine-token ] && [ -z "$MACHINE_TOKEN" ]; then
        status='SKIP:machine-token-unavailable'
      else
        case "$route" in
          public-get) method=GET; path=/api/health; body='' ;;
          gated-get) method=GET; path=/api/access/servers; body='' ;;
          mutation) method=POST; path=/api/tasks; body='{}' ;;
          autonomy-route) method=PUT; path=/api/autonomy; body='{}' ;;
        esac
        status=$(request_status "$method" "http://127.0.0.1:$port$path" "$body" "$credential")
        [ "$status" != 000 ] || status='SKIP:unreachable'
      fi
      printf '%s|%s|%s|%s\n' "$credential" "$port" "$route" "$status" >> "$TMP.rows"
    done
  done
}

port_count=0
remaining_ports=$PORTS
while [ -n "$remaining_ports" ]; do
  case "$remaining_ports" in
    *,*)
      port=${remaining_ports%%,*}
      remaining_ports=${remaining_ports#*,}
      ;;
    *)
      port=$remaining_ports
      remaining_ports=""
      ;;
  esac
  port_count=$((port_count + 1))
  probe=$(curl --silent --show-error --output /dev/null --write-out '%{http_code}' \
    --connect-timeout 1 --max-time 3 "http://127.0.0.1:$port/api/health" 2>/dev/null) || probe=000
  if [ "$probe" = 000 ]; then reachable=0; else reachable=1; fi
  append_port_matrix "$port" "$reachable"
done

# Counts over a matrix file: data rows exclude comments and the column header.
matrix_rows() {
  awk -F'|' '/^#/ {next} $1 == "credential" {next} {n++} END {print n+0}' "$1"
}
matrix_skips() {
  awk -F'|' '/^#/ {next} $1 == "credential" {next} $4 ~ /^SKIP:/ {n++} END {print n+0}' "$1"
}
matrix_unreachable() {
  awk -F'|' '/^#/ {next} $1 == "credential" {next} $4 == "SKIP:unreachable" {n++} END {print n+0}' "$1"
}

captured_skips=$(matrix_skips "$TMP.rows")
revision=$(git -C "$ROOT_DIR" rev-parse HEAD 2>/dev/null || printf 'unknown')
{
  echo "# identity-topology-probe baseline"
  echo "# revision=$revision"
  echo "# skipped_cells=$captured_skips"
  echo "$COLUMN_HEADER"
  cat "$TMP.rows"
} > "$TMP"

cat "$TMP"

# Completeness assertion: the artifact must be the whole matrix, stamp included.
# An interrupted run that got this far would be short rows or short header.
expected_rows=$((port_count * CREDENTIAL_COUNT * ROUTE_COUNT))
captured_rows=$(matrix_rows "$TMP")
if [ "$captured_rows" -ne "$expected_rows" ]; then
  echo "identity-topology-probe: REFUSED: incomplete capture: expected $expected_rows matrix rows ($port_count port(s) x $CREDENTIAL_COUNT credentials x $ROUTE_COUNT routes), captured $captured_rows; the run was interrupted or truncated" >&2
  exit 2
fi
if ! grep -q '^# revision=' "$TMP"; then
  echo "identity-topology-probe: REFUSED: incomplete capture: the matrix lost its '# revision=' stamp; the run was interrupted or truncated" >&2
  exit 2
fi

if [ "$WRITE_BASELINE" -eq 1 ]; then
  if [ "$captured_skips" -eq "$captured_rows" ]; then
    echo "identity-topology-probe: REFUSED: every one of $captured_rows cells is SKIP, so this baseline would measure no identity boundary at all; start the surfaces and re-capture" >&2
    exit 2
  fi
  captured_unreachable=$(matrix_unreachable "$TMP")
  if [ "$captured_unreachable" -gt 0 ] && [ "$ALLOW_SKIPS" -eq 0 ]; then
    echo "identity-topology-probe: REFUSED: $captured_unreachable of $captured_rows cells are SKIP:unreachable; a baseline captured while a surface is down freezes that gap as the expectation. Start the surfaces and re-capture, or pass --allow-skips to record the gap as '# skipped_cells=$captured_skips'" >&2
    exit 2
  fi
  mkdir -p "$(dirname -- "$BASELINE")"
  cp "$TMP" "$BASELINE"
  echo "identity-topology-probe: wrote revision-stamped baseline $BASELINE (skipped_cells=$captured_skips)" >&2
  exit 0
fi

if [ ! -f "$BASELINE" ]; then
  echo "identity-topology-probe: REFUSED: baseline is missing: $BASELINE (capture it on the operator's topology host with --write-baseline)" >&2
  exit 2
fi
if ! grep -q '^# revision=[0-9a-f][0-9a-f]*$' "$BASELINE"; then
  echo "identity-topology-probe: REFUSED: baseline is not revision-stamped: $BASELINE" >&2
  exit 2
fi

baseline_rows=$(matrix_rows "$BASELINE")
if [ "$baseline_rows" -eq 0 ]; then
  echo "identity-topology-probe: REFUSED: baseline contains no matrix rows: $BASELINE" >&2
  exit 2
fi
if [ "$(matrix_skips "$BASELINE")" -eq "$baseline_rows" ]; then
  echo "identity-topology-probe: REFUSED: every one of $baseline_rows cells in $BASELINE is SKIP; comparing nothing to nothing is vacuously green. Re-capture the baseline with the surfaces up" >&2
  exit 2
fi

# The stamp records where the expectation was measured; data is compared
# separately so ordinary commits do not make a still-valid measurement stale.
# grep exits 1 on a comment-only file: that must not look like "differs".
grep -v '^#' "$TMP" > "$TMP.expected" || true
grep -v '^#' "$BASELINE" > "$TMP.body" || true
if [ ! -s "$TMP.expected" ]; then
  echo "identity-topology-probe: REFUSED: capture contains no matrix rows to compare" >&2
  exit 2
fi
if [ ! -s "$TMP.body" ]; then
  echo "identity-topology-probe: REFUSED: baseline contains no matrix rows: $BASELINE" >&2
  exit 2
fi
if ! cmp -s "$TMP.expected" "$TMP.body"; then
  echo "identity-topology-probe: REFUSED: identity topology differs from baseline $BASELINE" >&2
  diff -u "$TMP.body" "$TMP.expected" >&2 || true
  exit 1
fi
echo "identity-topology-probe: PASS: matrix matches revision-stamped baseline" >&2
