#!/bin/sh
# Migration-authority phase command (filesystem numbering is authoritative).
#
# Explicit checkable phase — does NOT require core.hooksPath or installed hooks:
#   bash scripts/git-hooks/check-migration-versions.sh
#   bash scripts/git-hooks/check-migration-versions.sh /path/to/worktree
#
# Index-only admission:
#   Baseline = committed tracked migrations at BASE (default HEAD; override with
#   MIGRATION_AUTHORITY_BASE=<ref> when comparing against a parent of a clean tip).
#   Candidate = the git index (staged/tracked paths). Untracked working-tree files
#   are intentionally outside authority until staged — stage the candidate before
#   invoking this phase (pre-commit / pre-merge / explicit phase).
#
# Integration contract:
#   - Default HEAD compares the staged index during pre-commit/pre-merge.
#   - When checking a clean committed candidate tip, set
#     MIGRATION_AUTHORITY_BASE=<parent-or-merge-base> (e.g. HEAD^).
#
# Path listing is command-local and deterministic: every git path query uses
#   -c core.quotePath=false
# so user core.quotePath cannot hide non-ASCII names. The complete path list under
# migrations is consumed BEFORE any .sql suffix filter. Any remaining Git C-quoted
# path (quote / backslash / control characters are still C-quoted even with
# quotePath=false) fails closed before allocation decisions.
#
# One serialized allocation operation may:
#   - change nothing (read-only check), or
#   - stage exactly one new migration whose version is baseline_head + 1, OR
#   - stage a contiguous sequence of N new migrations (expected_next through expected_next+N-1).
# Renaming an UNCOMMITTED migration is admissible only because it is not a
# deletion at all: neither filename is in the baseline, so the operation is
# indistinguishable from staging one new migration under the final name.
# Renaming a COMMITTED migration is refused — see the deletion loop below.
# The sole historical exception is the exact migration-096 repair recorded in
# omniagentos/db/migration_repairs/096_dag_moe_gating.json.  Only the published
# old SHA-256 -> new SHA-256 transition is accepted; every other historical edit
# remains blocked.
# Everything else is rejected: duplicate N+1, gap N+2, stale/lower unused numbers,
# multiple new migrations with non-contiguous ordinals, deletion/renumbering, historical
# content edits, nested paths (migrate.py only loads direct children via Path.glob("*.sql")),
# C-quoted pathnames, and malformed names (must match exactly ^[0-9]{3}_.+\.sql$ — three
# digits, non-empty slug, direct child of omniagentos/db/migrations/).
#
# Also wired as pre-commit / pre-merge-commit when:
#   git config core.hooksPath scripts/git-hooks
#
# Born from the 044_orch_resume / 044_swarm collision (2026-07-23).
# POSIX /bin/sh + macOS compatible (no bashisms).
set -eu

MIG_DIR="omniagentos/db/migrations"
BASE_REF="${MIGRATION_AUTHORITY_BASE:-HEAD}"
REPAIR_096_PATH="$MIG_DIR/096_dag_moe_gating.sql"
REPAIR_096_RECORD="omniagentos/db/migration_repairs/096_dag_moe_gating.json"
REPAIR_096_OLD_SHA256="c01d0ff3fae6fc0cb000fdfd2af21b44c80f6ce8a71ce85a8fc06d6e517cc554"
REPAIR_096_NEW_SHA256="6752fac84728d5ef31030f0755a39bc8cdb2bb5c87f5fb79ec2ae8df3ae94e8a"

# Deterministic git path serialization regardless of user core.quotePath.
_git() {
    git -c core.quotePath=false "$@"
}

_git_object_sha256() {
    _object=$1
    _git cat-file blob "$_object" | shasum -a 256 | awk '{print $1}'
}

_repair_096_record_valid() {
    if ! _git cat-file -e ":${REPAIR_096_RECORD}" 2>/dev/null; then
        return 1
    fi
    _git show ":${REPAIR_096_RECORD}" | python3 -c '
import json
import sys

record = json.load(sys.stdin)
valid = (
    record.get("migration_version") == 96
    and record.get("migration_path") == sys.argv[1]
    and record.get("old_sha256") == sys.argv[2]
    and record.get("new_sha256") == sys.argv[3]
    and "not a general append-only bypass" in record.get("policy", "")
)
raise SystemExit(0 if valid else 1)
' "$REPAIR_096_PATH" "$REPAIR_096_OLD_SHA256" "$REPAIR_096_NEW_SHA256"
}

if [ "${1:-}" != "" ]; then
    cd "$1" || {
        echo "BLOCKED: cannot cd to work tree: $1" >&2
        exit 2
    }
fi

if ! root=$(git rev-parse --show-toplevel 2>/dev/null); then
    echo "BLOCKED: not a git work tree (migration authority phase requires git index)" >&2
    exit 2
fi
cd "$root"

# Require a resolvable commit object (not a tree-ish alone / dangling ref).
if ! BASE_COMMIT=$(git rev-parse --verify "${BASE_REF}^{commit}" 2>/dev/null); then
    echo "BLOCKED: cannot resolve baseline ref to a commit: $BASE_REF" >&2
    exit 2
fi

_pad3() {
    printf '%03d\n' "$1"
}

# Path -> decimal version, or empty if invalid for migrate.py authority.
# Valid only when:
#   1. path is a direct child of MIG_DIR (no nested subdirs), and
#   2. basename matches exactly ^[0-9]{3}_.+\.sql$ (three digits + non-empty slug).
_version_of() {
    _path=$1
    # Nested under migrations/ — migrate.py Path.glob("*.sql") never loads these.
    case "$_path" in
        "$MIG_DIR"/*/*)
            echo ""
            return 0
            ;;
        "$MIG_DIR"/*) ;;
        *)
            echo ""
            return 0
            ;;
    esac

    _base=$(basename "$_path")
    # sed -n …/p prints only on full match; empty slug (018_.sql) does not match .+
    _raw=$(printf '%s\n' "$_base" | sed -nE 's/^([0-9]{3})_.+\.sql$/\1/p')
    if [ -z "$_raw" ]; then
        echo ""
        return 0
    fi
    _ver=$(printf '%s\n' "$_raw" | sed 's/^0*//')
    if [ -z "$_ver" ]; then
        _ver=0
    fi
    case "$_ver" in
        '' | *[!0-9]*)
            echo ""
            return 0
            ;;
    esac
    printf '%s\n' "$_ver"
}

_malformed_diag() {
    _where=$1
    _path=$2
    echo "BLOCKED: malformed migration name in ${_where}: ${_path}" >&2
    echo "baseline_head=$baseline_head expected_next=$(_pad3 "$expected_next") candidate=<malformed>" >&2
    echo "Valid only as a direct child of ${MIG_DIR}/ matching ^[0-9]{3}_.+\\.sql\$" >&2
    echo "(exactly three digits, non-empty slug; nested paths are not loaded by migrate.py)." >&2
}

_cquoted_diag() {
    _where=$1
    _path=$2
    echo "BLOCKED: Git C-quoted path under ${MIG_DIR} (fail closed) in ${_where}: ${_path}" >&2
    echo "baseline_head=$baseline_head expected_next=$(_pad3 "$expected_next") candidate=<c-quoted-path>" >&2
    echo "Quote, backslash, and control characters remain C-quoted even with core.quotePath=false." >&2
    echo "Rename to a plain direct-child $(_pad3 "$expected_next")_<slug>.sql before staging." >&2
}

# Consume the complete Git path list under MIG_DIR, fail closed on C-quotes,
# THEN keep only *.sql candidates. Never suffix-filter first.
_ingest_migration_paths() {
    _src=$1
    _dest=$2
    _where=$3
    : >"$_dest"
    while IFS= read -r path || [ -n "${path:-}" ]; do
        [ -z "$path" ] && continue
        # Git C-quote form encloses the whole path in double quotes.
        case "$path" in
            \"*)
                _cquoted_diag "$_where" "$path"
                exit 1
                ;;
        esac
        case "$path" in
            *.sql)
                printf '%s\n' "$path" >>"$_dest"
                ;;
        esac
    done <"$_src"
    if [ -s "$_dest" ]; then
        LC_ALL=C sort -o "$_dest" "$_dest"
    fi
}

baseline_tmp=$(mktemp)
index_tmp=$(mktemp)
added_tmp=$(mktemp)
deleted_tmp=$(mktemp)
raw_tmp=$(mktemp)
trap 'rm -f "$baseline_tmp" "$index_tmp" "$added_tmp" "$deleted_tmp" "$raw_tmp"' EXIT

baseline_head=0
expected_next=1

# Committed baseline paths (complete list under MIG_DIR at the baseline commit).
: >"$raw_tmp"
_git ls-tree -r --name-only "$BASE_COMMIT" -- "$MIG_DIR" 2>/dev/null >"$raw_tmp" || true
_ingest_migration_paths "$raw_tmp" "$baseline_tmp" "baseline ($BASE_REF → $BASE_COMMIT)"

while IFS= read -r path || [ -n "${path:-}" ]; do
    [ -z "$path" ] && continue
    ver=$(_version_of "$path")
    if [ -z "$ver" ]; then
        expected_next=$((baseline_head + 1))
        _malformed_diag "baseline ($BASE_REF → $BASE_COMMIT)" "$path"
        exit 1
    fi
    if [ "$ver" -gt "$baseline_head" ]; then
        baseline_head=$ver
    fi
done <"$baseline_tmp"

expected_next=$((baseline_head + 1))

# Index candidate paths — complete staged/tracked list under MIG_DIR.
: >"$raw_tmp"
_git ls-files -- "$MIG_DIR" >"$raw_tmp" || true
_ingest_migration_paths "$raw_tmp" "$index_tmp" "index"

# Index: validate names/paths, collect versions, reject duplicates.
index_versions=""
while IFS= read -r path || [ -n "${path:-}" ]; do
    [ -z "$path" ] && continue
    ver=$(_version_of "$path")
    if [ -z "$ver" ]; then
        _malformed_diag "index" "$path"
        exit 1
    fi
    index_versions="${index_versions}${ver}
"
done <"$index_tmp"

dups=$(printf '%s' "$index_versions" | sort -n | uniq -d)
if [ -n "$dups" ]; then
    echo "BLOCKED: duplicate migration version number(s): $(printf '%s' "$dups" | tr '\n' ' ')" >&2
    echo "baseline_head=$baseline_head expected_next=$(_pad3 "$expected_next") candidate=<duplicate>" >&2
    echo "Two migrations share a version — one would silently never apply (or double-apply" >&2
    echo "against stale bookkeeping). Renumber the NEW file to $(_pad3 "$expected_next")_<name>.sql." >&2
    exit 1
fi

# Deleted paths = in baseline, not in index.
: >"$deleted_tmp"
while IFS= read -r path || [ -n "${path:-}" ]; do
    [ -z "$path" ] && continue
    if ! grep -Fxq -- "$path" "$index_tmp"; then
        printf '%s\n' "$path" >>"$deleted_tmp"
    fi
done <"$baseline_tmp"

# Append-only: a committed path may not leave the index, for any reason.
# Renaming a committed migration is a deletion here too — same version, same
# content, new filename is still refused.
#
# This block used to carry a rename exception that could never grant: it tested
# "$added_tmp" for content, but $added_tmp is not written until the added-paths
# pass further below, so it was always empty and is_rename was always 0. Do NOT
# reinstate it by simply hoisting that pass above this loop — the predicate
# matched on blob content ALONE, with no requirement that the deleted and added
# paths share a version, so with the ordering "fixed" a committed 002 renamed to
# 003 with identical content is admitted: the applied migration silently changes
# ordinal and re-applies against stale bookkeeping, the 044 collision this phase
# exists to prevent. A reinstatement needs BOTH the ordering and a
# _version_of "$path" = _version_of "$added_path" requirement, and it widens what
# this gate admits, so it needs an owner ruling first. See issue #137.
while IFS= read -r path || [ -n "${path:-}" ]; do
    [ -z "$path" ] && continue

    echo "BLOCKED: committed migration deleted or renumbered: $path" >&2
    echo "baseline_head=$baseline_head expected_next=$(_pad3 "$expected_next") candidate=<deletion>" >&2
    echo "Migrations are append-only; restore the path and allocate $(_pad3 "$expected_next")_<name>.sql instead." >&2
    exit 1
done <"$deleted_tmp"

# Append-only: no content edits to historical migrations still present.
while IFS= read -r path || [ -n "${path:-}" ]; do
    [ -z "$path" ] && continue
    if ! grep -Fxq -- "$path" "$index_tmp"; then
        continue
    fi
    base_blob=$(git rev-parse "${BASE_COMMIT}:${path}")
    index_blob=$(git rev-parse ":${path}")
    if [ "$base_blob" != "$index_blob" ]; then
        base_sha256=$(_git_object_sha256 "${BASE_COMMIT}:${path}")
        index_sha256=$(_git_object_sha256 ":${path}")
        if [ "$path" = "$REPAIR_096_PATH" ] \
            && [ "$base_sha256" = "$REPAIR_096_OLD_SHA256" ] \
            && [ "$index_sha256" = "$REPAIR_096_NEW_SHA256" ]; then
            # One exact, evidence-backed historical repair. This deliberately
            # does not weaken deletion, renumbering, allocation, or any other
            # historical-content check.
            if _repair_096_record_valid; then
                continue
            fi
            echo "BLOCKED: migration 096 repair authority record missing or invalid" >&2
            echo "baseline_head=$baseline_head expected_next=$(_pad3 "$expected_next") candidate=<invalid-repair-record>" >&2
            echo "Required staged record: $REPAIR_096_RECORD" >&2
            exit 1
        fi
        echo "BLOCKED: historical migration modified (append-only): $path" >&2
        echo "baseline_head=$baseline_head expected_next=$(_pad3 "$expected_next") candidate=<modified>" >&2
        echo "observed_old_sha256=$base_sha256 observed_new_sha256=$index_sha256" >&2
        echo "Ship a forward-only $(_pad3 "$expected_next")_<name>.sql instead of editing applied history." >&2
        exit 1
    fi
done <"$baseline_tmp"

# Added paths = in index, not in baseline.
: >"$added_tmp"
while IFS= read -r path || [ -n "${path:-}" ]; do
    [ -z "$path" ] && continue
    if ! grep -Fxq -- "$path" "$baseline_tmp"; then
        printf '%s\n' "$path" >>"$added_tmp"
    fi
done <"$index_tmp"

added_count=$(grep -c . "$added_tmp" 2>/dev/null || true)
if [ -z "$added_count" ]; then
    added_count=0
fi

if [ "$added_count" -eq 0 ]; then
    # Unchanged index vs baseline — read-only phase passes.
    exit 0
fi

# Collect all added migration versions.
added_versions_tmp=$(mktemp)
trap 'rm -f "$baseline_tmp" "$index_tmp" "$added_tmp" "$deleted_tmp" "$raw_tmp" "$added_versions_tmp"' EXIT

: >"$added_versions_tmp"
while IFS= read -r path || [ -n "${path:-}" ]; do
    [ -z "$path" ] && continue
    ver=$(_version_of "$path")
    if [ -z "$ver" ]; then
        _malformed_diag "index" "$path"
        exit 1
    fi
    printf '%s\n' "$ver" >>"$added_versions_tmp"
done <"$added_tmp"

# Sort and check for contiguity and no gaps.
sorted_versions=$(LC_ALL=C sort -n "$added_versions_tmp" | uniq)
sorted_versions_list=$(printf '%s' "$sorted_versions" | tr '\n' ' ')

# Verify all added versions form a contiguous sequence starting at expected_next.
first_added_ver=$(printf '%s\n' "$sorted_versions" | head -n 1)
last_added_ver=$(printf '%s\n' "$sorted_versions" | tail -n 1)
num_added_vers=$(printf '%s\n' "$sorted_versions" | wc -l)

if [ "$first_added_ver" -ne "$expected_next" ]; then
    echo "BLOCKED: migration version is not the next allocation" >&2
    echo "baseline_head=$baseline_head expected_next=$(_pad3 "$expected_next") candidate=$(_pad3 "$first_added_ver") path=<first-added>" >&2
    echo "Filesystem head is $(_pad3 "$baseline_head"); the only admissible new version is $(_pad3 "$expected_next")_<name>.sql." >&2
    echo "Rejected: skipped gaps, stale/lower unused numbers, and non-head claims." >&2
    exit 1
fi

# Check contiguity: last version should equal first + count - 1.
expected_last=$((first_added_ver + num_added_vers - 1))
if [ "$last_added_ver" -ne "$expected_last" ]; then
    echo "BLOCKED: non-contiguous migration version numbers in one operation" >&2
    echo "baseline_head=$baseline_head expected_next=$(_pad3 "$expected_next") candidates=$sorted_versions_list" >&2
    echo "Expected contiguous range $(_pad3 "$first_added_ver") through $(_pad3 "$expected_last"), but got $(_pad3 "$last_added_ver")." >&2
    echo "Fill gaps with intermediate migrations, or stage them separately." >&2
    exit 1
fi

exit 0
