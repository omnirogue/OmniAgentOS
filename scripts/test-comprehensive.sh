#!/usr/bin/env bash
# Comprehensive OmniAgentOS suite: max parallelism + full feature matrix.
# Product-local only (never touches ~/OmniAgentOS).
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
if [ "${OMNIAGENTOS_SIM_MODE:-}" = "1" ]; then unset OMNIAGENTOS_LAUNCH_ENV_LOADED OMNIAGENTOS_SIM_ENV_LOADED; fi
# shellcheck source=scripts/launch-env.sh
. "$ROOT/scripts/launch-env.sh"

# ALWAYS pin an isolated test DB. launch-env.sh may set OMNIAGENTOS_DB to the
# live product control-plane path; comprehensive tests must never inherit it
# (and must not honour a pre-set OMNIAGENTOS_DB that points at live state).
export OMNIAGENTOS_DB="$ROOT/var/comprehensive-test.db"
export COMPREHENSIVE_WORKERS="${COMPREHENSIVE_WORKERS:-24}"
export COMPREHENSIVE_DIAMONDS="${COMPREHENSIVE_DIAMONDS:-32}"
# Deterministic product defaults for metacog LIVE
unset OMNIAGENTOS_METACOG_MODE || true

echo "════════════════════════════════════════════════════════════"
echo " OmniAgentOS comprehensive suite"
echo " DB:       $OMNIAGENTOS_DB"
echo " Workers:  $COMPREHENSIVE_WORKERS"
echo " Diamonds: $COMPREHENSIVE_DIAMONDS"
echo "════════════════════════════════════════════════════════════"

PY="${ROOT}/.venv/bin/python"
if [[ ! -x "$PY" ]]; then
  PY=python3
fi

# Phase 1 — comprehensive package (parallelism + feature matrix + API wave)
echo ""
echo "▸ Phase 1: comprehensive package"
"$PY" -m pytest -q tests/comprehensive \
  --tb=short \
  "$@"

# Phase 2 — core product packages used by live control plane
echo ""
echo "▸ Phase 2: product feature packages"
"$PY" -m pytest -q \
  tests/graph_runtime \
  tests/cbm \
  tests/api/test_graph_cbm_routes.py \
  tests/api/test_new_surfaces_contracts.py \
  tests/orgdims \
  tests/taxonomy \
  tests/metacog \
  tests/metacognition \
  tests/db/test_migrations_060_063.py \
  tests/swarm/test_spawn_integrations.py \
  tests/scope/test_isolation_drill.py \
  tests/scope/test_cross_lane_races.py \
  tests/swarm/test_all_providers_swarm.py \
  tests/swarm/test_scheduler_races.py \
  tests/certification \
  --tb=line \
  "$@"

# Phase 3 — optional live multi-provider (real CLIs) when COMPREHENSIVE_LIVE=1
if [[ "${COMPREHENSIVE_LIVE:-0}" == "1" ]]; then
  echo ""
  echo "▸ Phase 3: LIVE multi-provider CLIs"
  "$PY" -m pytest -m live -q tests/swarm/test_live_all_providers.py -v --tb=short "$@"
else
  echo ""
  echo "▸ Phase 3: skipped (set COMPREHENSIVE_LIVE=1 for real CLI providers)"
fi

echo ""
echo "════════════════════════════════════════════════════════════"
echo " Comprehensive suite complete"
echo "════════════════════════════════════════════════════════════"
