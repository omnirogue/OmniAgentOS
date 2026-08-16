#!/bin/sh
# A4 hermetic lane venv-shape guard (see TESTING.md "Hermetic Lane").
#
# The testfarm harness activates by INSTALLATION (pytest11 entry point), so the
# single invariant the whole lane rests on is: testfarm is only ever installed
# into the real, repo-local `.venv-hermetic` directory — never into `.venv`,
# never through a symlink that redirects there. This script is the run-time half
# of that invariant (the Makefile's `override HERMETIC_VENV` is the parse-time
# half): it exits non-zero unless the requested venv path is the literal
# relative path `.venv-hermetic` in the current directory and is either absent
# or a real (non-symlink) directory.
#
# Usage: sh scripts/hermetic-venv-guard.sh <venv-path>   (run from the repo root)
set -eu

VENV="${1:?usage: hermetic-venv-guard.sh <venv-path>}"

if [ "$VENV" != ".venv-hermetic" ]; then
  echo "error: hermetic lane refuses venv path '$VENV' — the only allowed target is .venv-hermetic (installing testfarm anywhere else, e.g. .venv, would activate the socket guard for every pytest lane)" >&2
  exit 2
fi

if [ -L "$VENV" ]; then
  echo "error: .venv-hermetic is a symlink -> $(readlink "$VENV") — refusing: a redirected hermetic venv (e.g. to .venv) would install the always-on testfarm plugin into another environment. Remove the symlink and rerun." >&2
  exit 2
fi

if [ -e "$VENV" ] && [ ! -d "$VENV" ]; then
  echo "error: .venv-hermetic exists but is not a directory — refusing. Remove it and rerun." >&2
  exit 2
fi

exit 0
