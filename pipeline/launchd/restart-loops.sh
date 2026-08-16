#!/usr/bin/env bash
# Recreate the live tmux loops so each new iteration reads pipeline/prompts.
# Read-only by default; --apply is required because it terminates sessions.
set -euo pipefail

MODE=${1:---check}
[ "$#" -le 1 ] || { echo "usage: $0 [--check|--apply]" >&2; exit 2; }
case "$MODE" in --check|--apply) ;; *) echo "usage: $0 [--check|--apply]" >&2; exit 2 ;; esac

HERE=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
REPO=$(CDPATH= cd -- "$HERE/../.." && pwd)
RUN_LOOP="$REPO/pipeline/bridge/run-loop.sh"
[ -x "$RUN_LOOP" ] || { echo "refusing: missing executable $RUN_LOOP" >&2; exit 1; }
[ "$(git -C "$REPO" symbolic-ref --short HEAD)" = main ] || {
  echo "refusing: serving checkout is not pinned to main" >&2
  exit 1
}

if [ "$MODE" = --check ]; then
  for role in planning reviewer implementer; do
    tmux has-session -t "loop-$role" 2>/dev/null || continue
    command=$(tmux display-message -p -t "loop-$role" '#{pane_start_command}')
    case "$command" in
      *"$REPO/pipeline/prompts/PROMPT-$role-loop.md"*) ;;
      *) echo "loop-$role still reads a retired prompt path"; exit 1 ;;
    esac
  done
  echo "convergence loop check: ok"
  exit 0
fi

for role in planning reviewer implementer; do
  tmux has-session -t "loop-$role" 2>/dev/null && tmux kill-session -t "loop-$role"
done

LOOP_WORKDIR="$REPO" "$RUN_LOOP" planning claude2 claude1 claude6
LOOP_WORKDIR="$REPO" "$RUN_LOOP" reviewer claude6 claude4
LOOP_WORKDIR="$REPO" "$RUN_LOOP" implementer claude4 claude1 claude2 claude3 claude6 claude7

for role in planning reviewer implementer; do
  command=$(tmux display-message -p -t "loop-$role" '#{pane_start_command}')
  case "$command" in
    *"$REPO/pipeline/prompts/PROMPT-$role-loop.md"*) ;;
    *) echo "refusing: restarted loop-$role did not bind to pipeline/prompts" >&2; exit 1 ;;
  esac
done

echo "convergence loops restart: ok"
