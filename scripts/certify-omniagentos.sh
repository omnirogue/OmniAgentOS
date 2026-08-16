#!/usr/bin/env bash
# OmniAgentOS certification suite (implementable DoD).
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
if [ "${OMNIAGENTOS_SIM_MODE:-}" = "1" ]; then unset OMNIAGENTOS_LAUNCH_ENV_LOADED OMNIAGENTOS_SIM_ENV_LOADED; fi
# shellcheck source=scripts/launch-env.sh
. "$ROOT/scripts/launch-env.sh"
PYBIN="${OMNIAGENTOS_PYTHON:-$ROOT/.venv/bin/python}"
echo "OmniAgentOS certify — product-local only"

if [ ! -x "$PYBIN" ]; then
  echo "Error: no executable project Python at $PYBIN" >&2
  exit 1
fi

canonical_path() {
  "$PYBIN" - "$1" <<'PY'
import sys
from pathlib import Path
print(Path(sys.argv[1]).expanduser().resolve(strict=False))
PY
}

RUNTIME_ROOT="$(canonical_path "${OMNIAGENTOS_VAR_DIR:-$ROOT/var}/certification")"
EVIDENCE_PATH="$(canonical_path "${OMNIAGENTOS_CERTIFICATION_EVIDENCE:-$RUNTIME_ROOT/evidence.json}")"
JUNIT_PATH="$RUNTIME_ROOT/pytest.xml"
EXPECTED_INVENTORY_PATH="$RUNTIME_ROOT/pytest-expected.json"
EXECUTION_INVENTORY_PATH="$RUNTIME_ROOT/pytest-execution.json"
case "$RUNTIME_ROOT" in
  "$ROOT/var"|"$ROOT/var/"*) ;;
  "$ROOT"|"$ROOT/"*)
    echo "Error: certification runtime path is source-controlled: $RUNTIME_ROOT" >&2
    exit 1
    ;;
  *) ;;
esac
export OMNIAGENTOS_VAR_DIR="$RUNTIME_ROOT"
export OMNIAGENTOS_VAR="$RUNTIME_ROOT"
export OMNIAGENTOS_DB="$RUNTIME_ROOT/certify.db"
export OMNIAGENTOS_LEDGER_DIR="$RUNTIME_ROOT/ledger"
export OMNIAGENTOS_VAULT_DIR="$RUNTIME_ROOT/vault"
for runtime_path in \
  "$EVIDENCE_PATH" "$OMNIAGENTOS_VAR" "$OMNIAGENTOS_DB" \
  "$OMNIAGENTOS_LEDGER_DIR" "$OMNIAGENTOS_VAULT_DIR"
do
  case "$runtime_path" in
    "$RUNTIME_ROOT"|"$RUNTIME_ROOT"/*) ;;
    *) echo "Error: certification runtime path escapes $RUNTIME_ROOT: $runtime_path" >&2; exit 1 ;;
  esac
done

if [ -n "$(git status --porcelain=v1 --untracked-files=all)" ]; then
  echo "Error: tree has tracked or untracked changes; certification requires immutable input." >&2
  exit 1
fi
PRE_SHA="$(git rev-parse --verify HEAD)"
echo "Certifying immutable SHA: $PRE_SHA"
mkdir -p "$RUNTIME_ROOT" "$OMNIAGENTOS_LEDGER_DIR" "$OMNIAGENTOS_VAULT_DIR"

if [ "$#" -ne 0 ]; then
  echo "Error: certification does not accept pytest filters or overrides." >&2
  exit 2
fi

# Selection-affecting pytest environment is not certification input.  Project
# addopts are also cleared explicitly on both collection and execution below.
unset PYTEST_ADDOPTS PYTEST_PLUGINS PYTEST_DEBUG PYTEST_CURRENT_TEST
# Autoload is forced OFF rather than merely unset: any installed third-party
# plugin can deselect tests from its own collection hook.  The inventory plugin
# records this value so the evaluator can prove it was disabled for both runs.
export PYTEST_DISABLE_PLUGIN_AUTOLOAD=1

TEST_PATHS=(
  tests/certification
  tests/allocation/test_audit_sim.py
  tests/execution/test_post_attempt.py
  tests/toolplane
  tests/grants
  tests/fanin
  tests/taskcontract
  tests/gates
  tests/scope/test_isolation_drill.py
  tests/policy
  tests/routing/test_workers.py
  tests/sessions/test_chain_read.py
  tests/graph_runtime
  tests/cbm
  tests/api/test_graph_cbm_routes.py
  tests/api/test_new_surfaces_contracts.py
  tests/orgdims
  tests/taxonomy
  tests/metacog
  tests/metacognition
  tests/db/test_migrations_060_063.py
  tests/swarm/test_spawn_integrations.py
  tests/wiring
  tests/runner/test_scope_wiring.py
  tests/longhaul/test_scope_wiring.py
  tests/reliability/test_memory.py
  tests/accounts
  tests/connectors/test_broker_grants.py
  tests/connectors/test_globex_studio.py
  tests/connectors/test_extractors.py
  tests/connectors/test_jira_broker_allowlist.py
  tests/connectors/test_jira_client.py
  tests/connectors/test_jira_fields_config.py
  tests/connectors/test_jira_retry.py
  tests/connectors/test_money_hardstop.py
  tests/connectors/test_oauth2_scheme.py
  tests/connectors/test_registry_path_isolation.py
  tests/connectors/test_secrets_env.py
  tests/connectors/test_tn6_tn9_surfaces.py
  tests/scheduler
  tests/archdocs
)
for optional_path in tests/decision_center tests/decision tests/p12; do
  if [ -e "$optional_path" ]; then
    TEST_PATHS+=("$optional_path")
  fi
done

set +e
"$PYBIN" -m pytest -q -o addopts= -p pytest_timeout -p scripts.certification_pytest_plugin \
  --collect-only \
  --certification-inventory "$EXPECTED_INVENTORY_PATH" \
  --certification-inventory-mode expected \
  -m "not (live_cli or perf or live_ollama or live or counterfeit_gate or feature_health or e2e)" \
  "${TEST_PATHS[@]}"
COLLECTION_STATUS=$?
set -e
if [ "$COLLECTION_STATUS" -ne 0 ] || [ ! -s "$EXPECTED_INVENTORY_PATH" ]; then
  echo "Error: certification test collection failed or produced no inventory." >&2
  exit 1
fi
if [ "$(git rev-parse --verify HEAD)" != "$PRE_SHA" ] || \
   [ -n "$(git status --porcelain=v1 --untracked-files=all)" ]; then
  echo "Error: collection changed HEAD or the working tree; certification rejected." >&2
  exit 1
fi

set +e
"$PYBIN" -m pytest -q -o addopts= -p pytest_timeout -p scripts.certification_pytest_plugin \
  --junitxml "$JUNIT_PATH" \
  --certification-inventory "$EXECUTION_INVENTORY_PATH" \
  --certification-inventory-mode execution \
  -m "not (live_cli or perf or live_ollama or live or counterfeit_gate or feature_health or e2e)" \
  "${TEST_PATHS[@]}"
PYTEST_STATUS=$?
set -e

POST_SHA="$(git rev-parse --verify HEAD)"
CLEAN_TREE=true
if [ "$POST_SHA" != "$PRE_SHA" ] || [ -n "$(git status --porcelain=v1 --untracked-files=all)" ]; then
  CLEAN_TREE=false
fi

EVIDENCE_ARGS=()
for test_path in "${TEST_PATHS[@]}"; do
  EVIDENCE_ARGS+=(--executed-path "$test_path")
done
"$PYBIN" scripts/certification_evidence.py \
  --repo-root "$ROOT" \
  --repository-sha "$PRE_SHA" \
  --pytest-exit-code "$PYTEST_STATUS" \
  --clean-tree "$CLEAN_TREE" \
  --output "$EVIDENCE_PATH" \
  --junit-xml "$JUNIT_PATH" \
  --expected-inventory "$EXPECTED_INVENTORY_PATH" \
  --execution-inventory "$EXECUTION_INVENTORY_PATH" \
  "${EVIDENCE_ARGS[@]}"

if [ "$CLEAN_TREE" != true ]; then
  echo "Error: HEAD or working tree changed during certification; evidence rejected." >&2
  exit 1
fi
if [ "$PYTEST_STATUS" -ne 0 ]; then
  exit "$PYTEST_STATUS"
fi

echo "Certification passed for immutable SHA $PRE_SHA"
echo "Evidence: $EVIDENCE_PATH"
