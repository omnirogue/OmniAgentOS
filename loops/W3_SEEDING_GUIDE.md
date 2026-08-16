# W3 Health Monitor & Self-Heal Loop — Seeding & Registration Guide

## Overview

W3 is a health-monitoring loop that detects component failures via `health-sentinel` snapshots
and auto-repairs allowlisted components via `launchctl kickstart`. Unknown remedies escalate
to human approval (T3 tier).

## Dry-Run (Recommended for Testing)

To test the W3 implementation without affecting production:

```bash
# From the repo root
cd /Users/youruser/OmniAgentOS

# Set up the loops venv
export OMNIAGENTOS_LOOPS_VENV=/Users/youruser/OmniAgentOS/var/loops/venv

# Run the W3 instance tests (no live components harmed)
$OMNIAGENTOS_LOOPS_VENV/bin/python -m pytest loops/tests/instances/test_health_monitor.py -v

# Verify no regressions in the full loops suite
$OMNIAGENTOS_LOOPS_VENV/bin/python -m pytest loops/tests/ -q
```

Expected output: 21 W3 tests pass + 94 bridge tests pass = 115 total.

## Production Seeding

W3 is registered as a `routine` row with the following parameters:

**Instance module**: `omniagentos_loops.instances.health_monitor`  
**Template**: `monitor_diagnose_repair_verify`  
**Family**: `monitor`  
**Trigger**: 10-minute cron tick or event-driven  
**Allowlisted remedies**: `kickstart_api`, `kickstart_runner`, `kickstart_routines`, `kickstart_health_sentinel`

### Registration (the ONLY sanctioned path)

There is no hand-written SQL form. The `routines` table has no `kind`, `enabled`,
`harness`, `template`, `params`, `cron_schedule`, `test_command` or
`timeout_seconds` columns — the loop instruction lives inside `task_template_json`
— and `RoutinesStore.create_routine` runs validation belts (D5) that raw SQL
bypasses. Build the row with the helper and insert it through the store:

```python
from omniagentos_loops.registry import loop_routine_row

from omniagentos.contracts import default_db_path
from omniagentos.db.store import SqliteStore
from omniagentos.scheduler.store import RoutinesStore

routine = loop_routine_row(
    name="w3-health-monitor",
    template="monitor_diagnose_repair_verify",
    instance_id="w3_health_monitor",
    # REQUIRED, never derived: the module whose register(ctx) supplies this
    # instance's tools. It reaches the worker as --instance-module; a row
    # without it fails every tick on "instance is missing required tools".
    # Note it is NOT the instance_id — this instance registers from
    # health_monitor.py.
    instance_module="omniagentos_loops.instances.health_monitor",
    cron="*/10 * * * *",
    params={
        "instance_id": "w3_health_monitor",
        "allowed_remedies": [
            "kickstart_api",
            "kickstart_runner",
            "kickstart_routines",
            "kickstart_health_sentinel",
        ],
    },
    # The routine validator refuses ANY flag in a gate command: positional
    # targets only (no -q).
    gate_command="pytest loops/tests/instances/test_health_monitor.py",
    timeout_s=600,
)

store = SqliteStore(default_db_path())
created = RoutinesStore(store).create_routine(routine)
print(f"W3 routine registered: {created['id']}")
```

## Verification After Seeding

Once the routine is enabled:

1. **Check the routine fires**:
   ```bash
   tail -f /Users/youruser/OmniAgentOS/var/log/routines.log | grep "W3\|health.monitor"
   ```

2. **Monitor the loop tick**:
   ```bash
   tail -f /Users/youruser/OmniAgentOS/var/loops/<family>.log
   ```

3. **Verify a live repair** (intentionally kill API and observe):
   ```bash
   # Kill the API (in another terminal)
   pkill -f "uvicorn"
   
   # Wait ~10 minutes for W3 to run
   # Check if API is restarted
   pgrep -f "uvicorn" || echo "API is down"
   ```

4. **Check the receipt ledger** for deduplication:
   ```bash
   sqlite3 /Users/youruser/OmniAgentOS/var/runtime/state.sqlite3 \
     "SELECT business_key, receipt_key, created_at FROM idempotency_receipts WHERE node='repair' ORDER BY created_at DESC LIMIT 5;"
   ```

## Allowlist Safety

The remediation allowlist is **enumerated in code**, not configuration:

**File**: `loops/omniagentos_loops/instances/health_monitor.py:38-43`

```python
KICKSTART_ALLOWLIST = frozenset((
    "com.omniagentos.api",
    "com.omniagentos.runner",
    "com.omniagentos.routines",
    "com.omniagentos.health-sentinel",
))
```

**Changes require**:
1. Edit the source file
2. Re-run tests (`pytest loops/tests/instances/test_health_monitor.py`)
3. Merge via the gate (which will re-run the counterfeit suite)
4. Redeploy the loops worker

This design ensures **no configuration mistake can enable unauthorized repairs**.

## Rollback

To disable W3 without touching the database:

```bash
# Disable the routine row
sqlite3 /Users/youruser/OmniAgentOS/var/runtime/state.sqlite3 \
  "UPDATE routines SET enabled=0 WHERE name='W3 Health Monitor & Self-Heal';"

# Stop the running loop worker
launchctl bootout gui/$(id -u)/com.omniagentos.routines

# Verify
sqlite3 /Users/youruser/OmniAgentOS/var/runtime/state.sqlite3 \
  "SELECT id, name, enabled FROM routines WHERE kind='loop';"
```

## Testing Notes

- **Unit tests** (21 tests) exercise each tool in isolation with fake snapshots.
- **Drill tests** verify receipt-deduping prevents duplicate kickstart effects.
- **Counterfeit tests** catch mutations (e.g., ignoring the allowlist, lying in verify).
- **Full suite** (115 tests) ensures no regressions to the bridge.

Run before production seeding:
```bash
export OMNIAGENTOS_LOOPS_VENV=/Users/youruser/OmniAgentOS/var/loops/venv
$OMNIAGENTOS_LOOPS_VENV/bin/python -m pytest loops/tests/ -q
```

## Known Limitations (Future Work)

- **Multiple failures per tick**: Currently diagnoses the first failure only; future work scales to prioritize critical failures and repair multiple components in parallel.
- **LLM-assisted diagnosis**: MVP uses deterministic patterns only. Future work adds LLM classification for ambiguous logs (gated by the deterministic allowlist).
- **Repair verification**: Verifies component health via `/api/health` or `pgrep` only. Future work can integrate deeper checks (performance, error rates).
- **MTTR tracking**: Records timestamps; future work aggregates MTTR metrics to the dashboard.

## Questions?

Contact: The W3 builder or the LangGraph loops bridge owner.
