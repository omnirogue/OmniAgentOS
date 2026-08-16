#!/usr/bin/env bash
# H-30 — immutable pinned-SHA release / convergence gate entry point.
#
# Orchestrates named phases with phase evidence and refuses dirty or moving
# HEAD. Does not install dependencies and is not a curated certification subset.
#
# Usage:
#   ./scripts/release-gate.sh --list
#   ./scripts/release-gate.sh --dry-run
#   ./scripts/release-gate.sh --phases ruff,format,mypy
#   ./scripts/release-gate.sh                 # full gate (resource-heavy)
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

# Resolve the project-local interpreter. The release gate refuses to certify
# under a foreign Python; prefer explicit OMNIAGENTOS_PYTHON, then the project
# venv, then fail loudly rather than silently use a random PATH interpreter.
#
# Never canonicalise this path (no realpath/readlink -f): .venv/bin/python is a
# symlink to the base interpreter, and following it drops the virtualenv along
# with every project dependency the phases need.
if [[ -n "${OMNIAGENTOS_PYTHON:-}" && -x "${OMNIAGENTOS_PYTHON}" ]]; then
  PY="${OMNIAGENTOS_PYTHON}"
  export OMNIAGENTOS_PYTHON="${PY}"
elif [[ -x "${ROOT}/.venv/bin/python" ]]; then
  PY="${ROOT}/.venv/bin/python"
  export OMNIAGENTOS_PYTHON="${PY}"
else
  # No auditable interpreter here. Bootstrap with PATH python3 only to reach
  # resolve_python(), which refuses certification with an accurate message.
  # Deliberately do NOT export it as OMNIAGENTOS_PYTHON — that would make the
  # gate report a bogus override instead of "no project-local interpreter".
  PY="python3"
  unset OMNIAGENTOS_PYTHON
fi
export PYTHONPATH="${ROOT}${PYTHONPATH:+:$PYTHONPATH}"

# Default evidence under the lane runtime when present.
if [[ -z "${OMNIAGENTOS_VAR_DIR:-}" && -n "${OMNIAGENTOS_HOME:-}" ]]; then
  export OMNIAGENTOS_VAR_DIR="${OMNIAGENTOS_HOME}/var"
fi

exec "$PY" -m omniagentos.harnesses.release_gate "$@"
