#!/usr/bin/env bash
# Safe human CSI pipeline — never merges to main.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
export OMNIAGENTOS_HOME="$ROOT"
DB="${CSI_DB:-$ROOT/var/csi/csi.db}"
PY="${ROOT}/.venv/bin/python"
mkdir -p "$(dirname "$DB")"

usage() {
  cat <<EOF
Usage: $(basename "$0") <command> [args]

  status                 Config + recent runs
  run [routine]          Run one routine (default: self_learning)
  run-all                Run all 8 enabled routines
  approve <run_id>       CSI-native approve
  implement <run_id>     Worktree implement (never merges)
  pipeline [routine]     run + print next human steps

Routines: design self_learning routing skills tool_calls speed quality tech_debt

Env: CSI_DB (default var/csi/csi.db)
EOF
}

cmd="${1:-}"
shift || true

case "$cmd" in
  status)
    "$PY" - <<PY
from omniagentos.csi.config import clear_csi_config_cache, load_csi_config
from omniagentos.csi.evidence import IMPLEMENTED_ROUTINES
clear_csi_config_cache()
c = load_csi_config()
ok, reason = c.is_runnable()
print(f"enabled={c.enabled} global_halt={c.global_halt} runnable={ok} ({reason})")
print(f"live_planners={c.use_live_planners} max_merges_per_day={c.max_merges_per_day}")
print("routines:")
for rid, spec in sorted(c.routines.items()):
    flag = "ON " if spec.enabled else "off"
    impl = "impl" if rid in IMPLEMENTED_ROUTINES else "stub"
    print(f"  [{flag}] {rid:14} planners={list(spec.planners)} ({impl})")
print(f"db=$DB")
try:
    import sqlite3
    con = sqlite3.connect("$DB")
    con.row_factory = sqlite3.Row
    rows = con.execute(
        "SELECT id, routine_id, status, approval_status, verdict, created_at "
        "FROM csi_runs ORDER BY created_at DESC LIMIT 12"
    ).fetchall()
    print("recent:")
    for r in rows:
        print(" ", dict(r))
    if not rows:
        print("  (no runs)")
except Exception as e:
    print(f"(db: {e})")
PY
    ;;
  run)
    rid="${1:-self_learning}"
    exec "$PY" -m omniagentos.csi run -r "$rid" --db "$DB"
    ;;
  run-all)
    exec "$PY" -m omniagentos.csi run-all --db "$DB"
    ;;
  approve)
    rid="${1:?run_id required}"
    exec "$PY" -m omniagentos.csi approve --run-id "$rid" --db "$DB" --by "${USER:-operator}"
    ;;
  implement)
    rid="${1:?run_id required}"
    exec "$PY" -m omniagentos.csi implement --run-id "$rid" --db "$DB"
    ;;
  pipeline)
    rid="${1:-self_learning}"
    echo "=== CSI $rid (db=$DB) ==="
    out="$("$PY" -m omniagentos.csi run -r "$rid" --db "$DB")"
    echo "$out"
    run_id="$(echo "$out" | "$PY" -c "import sys,json; d=json.load(sys.stdin); print(d.get('run_id') or '')")"
    status="$(echo "$out" | "$PY" -c "import sys,json; d=json.load(sys.stdin); print(d.get('status') or '')")"
    if [[ -n "$run_id" && "$status" == "AWAITING_HUMAN" ]]; then
      echo ""
      echo "Next (human only):"
      echo "  $0 approve $run_id"
      echo "  $0 implement $run_id"
    fi
    ;;
  ""|-h|--help|help)
    usage
    ;;
  *)
    echo "unknown command: $cmd" >&2
    usage
    exit 2
    ;;
esac
