#!/usr/bin/env bash
#
# Acceptance proof for dashboard/src/app/api/dynamicRouteEncoding.test.ts.
#
# That suite pins a security invariant: a URL path segment must not escape its
# position when interpolated into a path handed to a token-bearing proxy call.
# A test can only claim that if the mutants it is supposed to catch actually
# turn it red -- and three successive revisions of that file were green against
# a mutant hiding one index past wherever its hand-written fixtures stopped
# (i === 2 survived 46/46; i === 3 survived 28/28 targeted and 54/54 across
# src/app/api). The fixtures are now GENERATED, so this script exists to prove
# the generation actually closed the class rather than moving the ceiling.
#
# Each mutant is applied to the PRODUCT file, the targeted suite is run, and the
# suite is required to go RED. A mutant that leaves the suite green is a hole in
# the guard and fails this script.
#
# The product file is restored from an explicit byte-for-byte backup after every
# mutant and verified by checksum at exit -- never via `git checkout`, which has
# silently discarded uncommitted work in this repo before.
#
# This harness was itself checked for discriminating power, because a proof that
# can only ever print PASS proves nothing. Capping BOTH generated matrices at 3
# segments (the ceiling of the rejected revision) while leaving MAX_SEGMENTS at
# 8 makes indices 3..7 SURVIVE and this script exit 1 -- the exact defect it
# exists to catch.
#
# That experiment also turned up something worth knowing before you edit the
# test file: capping only the MUTATING matrix still passes, because the
# authorized-read matrix independently reaches depth MAX_SEGMENTS and both
# carriers share `upstreamPath()`. The two matrices are therefore NOT
# independent for shared-helper mutants -- either one alone still catches a
# fixed-index skip in `upstreamPath`. They stop being redundant exactly where it
# matters: a mutant that bypasses the shared helper on ONE carrier (the last
# case below) is caught only by that carrier's own rows. Do not delete either
# matrix on the grounds that the other covers it.
#
# Usage:  tests/acceptance/dashboard_dynamic_route_encoding_mutants.sh
# Exit:   0 = every mutant caught · 1 = at least one mutant survived, or the
#         harness could not anchor / restore (both are harness failures, not
#         evidence about the product).
set -uo pipefail

REPO_ROOT="$(git -C "$(dirname "${BASH_SOURCE[0]}")" rev-parse --show-toplevel)"
readonly REPO_ROOT
readonly DASHBOARD="$REPO_ROOT/dashboard"
readonly ROUTE="$DASHBOARD/src/app/api/[...path]/route.ts"
readonly SUITE="src/app/api/dynamicRouteEncoding.test.ts"
readonly TEST_FILE="$DASHBOARD/$SUITE"

for required in "$ROUTE" "$TEST_FILE"; do
  if [[ ! -f "$required" ]]; then
    printf 'HARNESS FAILURE: missing %s\n' "$required" >&2
    exit 1
  fi
done

# The generated matrix guarantees coverage for every index below MAX_SEGMENTS.
# Read it from the test file rather than restating it, so raising the constant
# automatically widens this proof instead of silently outrunning it.
MAX_SEGMENTS="$(sed -n 's/^const MAX_SEGMENTS = \([0-9]\{1,\}\);.*/\1/p' "$TEST_FILE" | head -1)"
readonly MAX_SEGMENTS
if [[ -z "$MAX_SEGMENTS" ]]; then
  printf 'HARNESS FAILURE: could not read MAX_SEGMENTS from %s\n' "$TEST_FILE" >&2
  exit 1
fi

BACKUP="$(mktemp "${TMPDIR:-/tmp}/route-ts-backup.XXXXXX")"
readonly BACKUP
cp -- "$ROUTE" "$BACKUP"
ORIG_SUM="$(shasum -a 256 <"$BACKUP" | awk '{print $1}')"
readonly ORIG_SUM

restore() { cp -- "$BACKUP" "$ROUTE"; }
trap 'restore; rm -f -- "$BACKUP"' EXIT

readonly ENCODE_ANCHOR='const encoded = path.map(encodeURIComponent).join("/");'
readonly READ_ANCHOR='  const apiPath = upstreamPath(request, path);'

survivors=0
checked=0

# $1 = human label · $2 = anchor to replace · $3 = replacement source
check_mutant() {
  local label="$1" anchor="$2" replacement="$3"
  checked=$((checked + 1))
  restore

  if ! python3 - "$ROUTE" "$anchor" "$replacement" <<'PY'
import sys
path, anchor, replacement = sys.argv[1], sys.argv[2], sys.argv[3]
source = open(path).read()
if source.count(anchor) != 1:
    sys.stderr.write(f"anchor appears {source.count(anchor)} times, expected exactly 1\n")
    sys.exit(1)
open(path, "w").write(source.replace(anchor, replacement))
PY
  then
    printf 'HARNESS FAILURE: could not anchor mutant %s\n' "$label" >&2
    survivors=$((survivors + 1))
    return
  fi

  local output rc failed
  output="$(cd "$DASHBOARD" && npx vitest run "$SUITE" 2>&1)"
  rc=$?
  failed="$(printf '%s' "$output" | grep -Eo 'Tests +[0-9]+ failed' | grep -Eo '[0-9]+' | head -1)"

  if [[ "$rc" -ne 0 ]]; then
    printf 'CAUGHT       %-42s (%s rows red)\n' "$label" "${failed:-?}"
  else
    printf 'SURVIVED !!! %-42s (suite stayed green)\n' "$label"
    survivors=$((survivors + 1))
  fi
}

printf '=== fixed-index skip mutants (indices 0..%d) ===\n' "$((MAX_SEGMENTS - 1))"
for ((k = 0; k < MAX_SEGMENTS; k++)); do
  check_mutant "index === $k skipped" "$ENCODE_ANCHOR" \
    "const encoded = path.map((segment, index) => (index === $k ? segment : encodeURIComponent(segment))).join(\"/\");"
done

printf '\n=== count-axis mutants ===\n'
check_mutant "map removed entirely" "$ENCODE_ANCHOR" \
  'const encoded = path.join("/");'
check_mutant "encode only the first 3 segments" "$ENCODE_ANCHOR" \
  'const encoded = path.map((segment, index) => (index < 3 ? encodeURIComponent(segment) : segment)).join("/");'
check_mutant "stop after first escaped segment" "$ENCODE_ANCHOR" \
  'let done = false; const encoded = path.map((segment) => { if (done) return segment; const e = encodeURIComponent(segment); if (e !== segment) done = true; return e; }).join("/");'

printf '\n=== read-path bypass (spares mutations, so mutation rows cannot catch it) ===\n'
check_mutant "read() bypasses upstreamPath" "$READ_ANCHOR" \
  '  const apiPath = `/api/${path.join("/")}${request.nextUrl.search}`;'

restore
FINAL_SUM="$(shasum -a 256 <"$ROUTE" | awk '{print $1}')"
printf '\n'
if [[ "$FINAL_SUM" != "$ORIG_SUM" ]]; then
  printf 'HARNESS FAILURE: product file not restored (%s != %s)\n' "$FINAL_SUM" "$ORIG_SUM" >&2
  exit 1
fi
printf 'product file restored byte-for-byte (%s)\n' "$ORIG_SUM"

if [[ "$survivors" -ne 0 ]]; then
  printf '\nFAIL: %d of %d mutants survived the guard.\n' "$survivors" "$checked" >&2
  exit 1
fi
printf 'PASS: all %d mutants caught.\n' "$checked"
