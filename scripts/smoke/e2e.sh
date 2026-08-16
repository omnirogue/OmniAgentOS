#!/usr/bin/env bash
# A6 — end-to-end smoke: task → mock run → approval → completed → ledger + vault.
# Exit 0 on success; nonzero on any assertion failure.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT"

E2E_PY="$ROOT/scripts/smoke/e2e_main.py"

if [[ -x "$ROOT/.venv/bin/python" ]]; then
  exec "$ROOT/.venv/bin/python" "$E2E_PY"
fi

if command -v uv >/dev/null 2>&1; then
  exec uv run python "$E2E_PY"
fi

if command -v python3 >/dev/null 2>&1; then
  exec python3 "$E2E_PY"
fi

echo "E2E FAIL: need .venv, uv, or python3 to run $E2E_PY" >&2
exit 2
