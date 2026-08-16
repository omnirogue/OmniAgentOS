#!/bin/sh
# Coordinator-only exact-SHA promotion entry point.
#
# Report mode is the default and writes only JSON to stdout. Mutation requires
# --enforce, raw YAML enforce (environment overrides are refused), two signed
# report-cycle receipts, a signed authorship manifest, two outside-family
# reviews, and an operator authorization. The Python coordinator remains the
# fail-closed authority for every one of those controls.
set -eu

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
ROOT_DIR=$(CDPATH= cd -- "$SCRIPT_DIR/.." && pwd)

if [ -x "$ROOT_DIR/.venv/bin/python" ]; then
  PYBIN="$ROOT_DIR/.venv/bin/python"
elif command -v python3.12 >/dev/null 2>&1; then
  PYBIN=$(command -v python3.12)
else
  printf '%s\n' "REFUSED: no Python 3.12+ interpreter; run uv sync first" >&2
  exit 1
fi

cd "$ROOT_DIR"
exec "$PYBIN" -m omniagentos.integration.promote --repo "$ROOT_DIR" "$@"
