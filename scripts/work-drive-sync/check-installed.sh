#!/bin/zsh
# Verifies that the launchd-executed copies of the work-drive sync/pull
# scripts (in ~/bin) are byte-identical to the versioned scripts in this
# repo directory. launchd runs the ~/bin copy, not this repo file directly,
# so an out-of-band hand edit to ~/bin silently diverges from what `git
# checkout` and code review believe is live. Enumerates and checks EVERY
# deployed copy this host has (currently: ~/bin, the sole launchd-executed
# location per install.sh) — do not add a new deploy target without adding
# it to LIVE_DIRS below, or this check will silently stop covering it.
#
# Exit 0: every enumerated live copy exists and matches the repo.
# Exit 1: a live copy is missing, OR exists and differs from the repo
#         version — re-run install.sh in this directory to (re)install it.
set -uo pipefail
HERE="${0:A:h}"

# All directories install.sh deploys into. Extend this list if a new
# deploy target is ever added (finding f3eaf8d4-review, 2026-08-08: a
# missing live copy must FAIL, not be echoed-and-skipped, and every
# enumerable deployed copy must be checked, not just one).
LIVE_DIRS=("$HOME/bin")

STATUS=0
for live_dir in "${LIVE_DIRS[@]}"; do
  for s in work-drive-sync.sh work-drive-pull.sh; do
    repo_file="$HERE/$s"
    live_file="$live_dir/$s"
    if [ ! -f "$live_file" ]; then
      echo "check-installed: MISSING — $live_file not present (expected an installed copy)"
      echo "  fix: $HERE/install.sh"
      STATUS=1
      continue
    fi
    if ! diff -q "$repo_file" "$live_file" >/dev/null 2>&1; then
      echo "check-installed: DRIFT — $live_file differs from $repo_file"
      echo "  fix: $HERE/install.sh"
      STATUS=1
    else
      echo "check-installed: OK — $live_file matches $repo_file"
    fi
  done
done

exit $STATUS
