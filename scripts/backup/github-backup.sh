#!/bin/sh
# Hourly GitHub backup — the rollback guarantee behind the fable-curator's
# autonomous self-editing (the operator, 2026-07-24: "git hub backups so we can roll
# back at anytime"). Pushes BOTH repos' main branches to their private
# remotes. Plain pushes only: NEVER force, NEVER rewrite history — a non-FF
# failure is logged loudly for a human, because it means the remote has
# something local does not (that is rollback material, not noise to clobber).
set -eu
# Never hang on a credential prompt under launchd — fail loudly instead; git
# credentials come from gh's helper (gh auth setup-git, keychain-backed).
export GIT_TERMINAL_PROMPT=0

LOG="/Users/youruser/OmniAgentOS/var/log/github-backup.log"
mkdir -p "$(dirname "$LOG")"
stamp() { date -u +%FT%TZ; }

push_repo() {
    repo="$1"
    label="$2"
    if out=$(git -C "$repo" push origin main 2>&1); then
        case "$out" in
            *"Everything up-to-date"*) echo "[$(stamp)] $label: up-to-date" >>"$LOG" ;;
            *) echo "[$(stamp)] $label: pushed — $out" >>"$LOG" ;;
        esac
    else
        echo "[$(stamp)] $label: PUSH FAILED — $out" >>"$LOG"
        return 1
    fi
}

status=0
push_repo /Users/youruser/OmniAgentOS omniagentos || status=1
push_repo /Users/youruser home-estate || status=1
exit "$status"
