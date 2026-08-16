# W3 Health Monitor & Self-Heal Loop — Design Summary

## Mission

W3 is a production health-monitoring and automated self-healing loop that:
- Monitors OmniAgentOS fleet health via `health-sentinel` snapshots (10-min ticks)
- Diagnoses component failures deterministically (pattern matching on evidence strings)
- Auto-repairs allowlisted components via `launchctl kickstart` (T1, no approval needed)
- Escalates unknown failures to human approval (T3, parks until approved)
- Verifies repairs succeeded and records MTTR (mean time to recovery)
- Deduplicates repairs across ticks via receipt-guarded business keys

## Trigger & Inputs

**Trigger**: 10-minute cron tick (via routine row)  
**Inputs**:
- `var/health-sentinel/latest.json` — component-level snapshot from health-sentinel
- `var/log/*.log` — recent log tails (routines, API, health-sentinel) for diagnosis context

## Architecture

W3 uses the `monitor_diagnose_repair_verify` template from the LangGraph loops bridge.

```
monitor (T0)
  ↓ [read snapshot + logs]
diagnose (T0)
  ↓ [classify failure → remedy]
  ├→ repair (T1) ← allowlisted remedies only [auto, receipted]
  │   ↓ [launchctl kickstart]
  │   ↓ verify (T0) ← post-repair check
  │   ↓ [IDLE or COMPLETED]
  │
  └→ escalate (T3) ← unknown remedies [parks, awaits approval]
      ↓ [record escalation; notify; park]
      ↓ [ABORTED or COMPLETED]
```

### Nodes & Tiers

| Node | Tier | Role | Auto? | Approval? |
|------|------|------|-------|-----------|
| `monitor` | T0 | Read snapshot + logs; extract failed checks | Yes | No |
| `diagnose` | T0 | Deterministic pattern matching → remedy ID | Yes | No |
| `repair` | T1 | launchctl kickstart for allowlisted labels | Yes | No (safety via allowlist) |
| `escalate` | T3 | Record + park unknown remedies | No | Yes (human) |
| `verify` | T0 | Re-check component; measure MTTR | Yes | No |

**Key safety rule**: The allowlist is enumerated in code (not config) and burned into the remedy classification step. Unknown remedies cannot reach the repair node; they divert to escalate.

## Deterministic Diagnosis

The `diagnose_failure` tool uses pattern matching on the health-sentinel `evidence` string (one-line summary per check). Current patterns:

| Component | Status | Evidence Patterns | Remedy | Label | Auto-Repair? |
|-----------|--------|-------------------|--------|-------|--------------|
| api | fail | `unreachable`, `HTTP \d{3}`, `connection refused` | `kickstart_api` | `com.omniagentos.api` | ✓ |
| runner | fail | `no.*omniagentos\.runner`, `queued runs will never move` | `kickstart_runner` | `com.omniagentos.runner` | ✓ |
| scheduler | fail | `older than`, `ticked.*ago` | `unknown_scheduler` | None | ✗ (escalate) |
| launchd | fail | `not loaded`, `not installed`, `exited \d+` | `unknown_launchd` | None | ✗ (escalate) |
| *other* | fail | (no match) | `unknown_failure` | None | ✗ (escalate) |

**No LLM involvement in MVP**: Diagnosis is 100% deterministic. Future work can add LLM classification for genuinely ambiguous logs, gated by the same allowlist.

## Remediation Allowlist

**Allowlisted labels** (can auto-kickstart):
```python
KICKSTART_ALLOWLIST = frozenset((
    "com.omniagentos.api",
    "com.omniagentos.runner",
    "com.omniagentos.routines",
    "com.omniagentos.health-sentinel",
))
```

**Mirrors**: The sentinel's own allowlist (scripts/health-sentinel/health_sentinel.py:122-128). Same labels, same justification: these services are stateless or recover from crashes, and a restart is low-risk and often fixes transient issues.

**Enforcement**: The allowlist is **code** in `health_monitor.py:38-43`, not config. Changes require:
1. Source edit + tests
2. Gate merge (includes counterfeit verification)
3. Redeploy

This design ensures **zero configuration mistakes can enable unauthorized repairs**.

## Incident Deduplication

**Incident ID** (business key): `component:signature:day`

- `component`: e.g., `"api"`, `"runner"`
- `signature`: normalized evidence string (time-dependent info stripped)
- `day`: `date.today().isoformat()`

**Property**: Same failure on the same day = same incident = same receipt key = one repair across ticks.

**Consequence**: If API dies at 10:00 and W3 kickstarts it, the 10:10 tick will re-enter the repair node with the same receipt key; the receipt guard will replay the cached result (success) instead of re-executing launchctl.

**Reset**: New day or new failure signature → new incident → new repair allowed.

## Tool Implementations

### `monitor_health(params) → dict`

**Tier**: T0 (no approval)  
**Effect**: None (read-only)  
**Calls**:
- `_read_snapshot()` → parses `var/health-sentinel/latest.json`
- `_tail_logs(num_lines=50)` → reads recent lines from `var/log/*.log`

**Returns**: dict with:
- `snapshot`: the latest health-sentinel snapshot
- `failed_checks`: filtered list of checks with status `fail` or `warn`
- `logs`: dict of log tails per file
- `timestamp`: UTC ISO timestamp

### `diagnose_failure(snapshot) → dict`

**Tier**: T0 (no approval)  
**Effect**: None (deterministic classification)  
**Logic**:
1. If no failed checks → return `remedy: ""` (healthy state)
2. For the first failed check, pattern-match its `name` and `evidence` against `_REMEDY_PATTERNS`
3. Return:
   - `remedy`: `"kickstart_api"`, `"unknown_scheduler"`, etc.
   - `label`: launchd label (or None for unknowns)
   - `incident`: business key (component:signature:day)
   - `component`, `status`, `evidence`, `detail`, `logs`

**Future**: Extend to classify all failed checks, prioritize by criticality, batch repairs.

### `repair_component(remedy, snapshot) → dict`

**Tier**: T1 (auto, receipted)  
**Effect**: Calls `launchctl kickstart -k gui/{uid}/{label}` if label is in allowlist

**Safety gates**:
1. Label must be in `KICKSTART_ALLOWLIST` → fail if not
2. Label is not None → fail if unknown remedy
3. `os.getuid()` to get UID for launchctl

**Returns**: dict with:
- `success`: bool
- `label`, `command`: what was attempted
- `mttr_start`: UTC ISO timestamp (for verify to measure recovery time)
- `stdout`, `stderr`, `returncode`: subprocess output (if failed)

**Retry policy**: 2 retries on transient errors (subprocess timeout, temp failure). No blind retries on unknown state (replay_on_unknown=False, required by bridge for T1).

### `escalate_unknown(remedy, snapshot) → dict`

**Tier**: T3 (parks, requires human approval)  
**Effect**: Records escalation in the state; later ticks will re-read the durable approval row

**Returns**: dict with:
- `escalated`: True
- `remedy`, `component`, `evidence`: what failed and why
- `timestamp`: UTC ISO

**Note**: The actual parking happens in the template's conditional gate (not this tool). This tool just records metadata for the approval row and audit trail.

### `verify_repair(remedy, result) → dict`

**Tier**: T0 (no approval)  
**Effect**: Re-checks component health; no external changes

**Logic**:
1. If `remedy == ""` → return healthy state (no repair needed)
2. If remedy is `unknown_*` → return escalated state
3. If repair failed → return repair_failed state
4. If repair succeeded → re-snapshot and re-check component status:
   - If now OK → return recovered + MTTR seconds
   - If still failing → return still_failing
   - If unknown → return unverified

**Returns**: dict with:
- `verified`: bool
- `state`: `"healthy"`, `"escalated"`, `"repair_failed"`, `"recovered"`, `"still_failing"`, `"unverified"`
- `mttr_seconds`, `mttr_start`, `mttr_end`: if recovered
- Other details per state

## Test Coverage

### Unit Tests (21 tests)

**TestMonitor** (2): Monitor reads snapshot; extracts failed checks  
**TestDiagnose** (7): Diagnose classifies each failure type + determinism of incident ID  
**TestRepair** (4): Repair allows/denies per allowlist; handles subprocess errors  
**TestEscalate** (1): Escalate records unknown remedies  
**TestVerify** (3): Verify detects repair success/failure; measures MTTR  
**TestDrills** (1): Full flow: dead API → one repair across ticks (receipt-guarded)  
**TestCounterfeits** (3): Catch mutations (verify lies, ignores allowlist, adds unlisted remedy)

### Bridge Integration (115 total)

- **All W3 unit tests pass**: 21/21
- **All template tests pass**: 5/5 (monitor_diagnose_repair_verify + 4 others)
- **All approval bridge tests pass**: 14/14
- **All kill-drill tests pass**: 5/5 (W3 survives crash-resume)
- **All counterfeit tests pass**: 8/8 (mutations caught by 3 W3 counterfeits + 5 bridge)
- **All security seam tests pass**: 16/16 (W3 tools only reach the seam, no direct side effects)

### Counterfeit Examples

**cf-verify-lies**: verify claims "recovered" even though API still fails → re-check detects lie → state="still_failing" → test fails → mutation caught.

**cf-repair-ignores-allowlist**: repair attempts kickstart for unlisted label → allowlist gate rejects → "not in allowlist" error → test fails → mutation caught.

**cf-diagnose-adds-unlisted-remedy**: diagnose returns remedy not in allowlist → template's conditional routes to escalate (not repair) → escalate parks → T3 approval required → test verifies no auto-repair → mutation caught.

## Failure Modes & Handling

| Scenario | W3 Behavior |
|----------|-------------|
| Snapshot file missing | Monitor returns error; diagnose sees empty checks; loop is IDLE |
| Log files unreachable | Monitor returns error in tails; diagnose proceeds (tails are optional) |
| Unknown component failure (e.g., reflection stale) | Diagnose returns `unknown_reflection`; escalate parks; human approves or system auto-parks |
| Repair subprocess timeout | Repair returns error; verify detects failure; loop continues to next tick |
| Repair succeeds but component still down | Verify detects "still_failing"; loop logs MTTR but no further action (next tick re-diagnoses) |
| Same incident repeats next day | New day → new incident ID → new repair allowed (not deduped) |

## MTTR (Mean Time To Recovery) Tracking

**Fields recorded**:
- `mttr_start`: timestamp when repair kicked off
- `mttr_end`: timestamp when verify confirmed component is up
- `mttr_seconds`: (mttr_end - mttr_start).total_seconds()

**Usage**:
- Stored in loop state (persisted to checkpoint DB)
- Emitted in observability events (dashboard integration, future)
- Success metric: fleet uptime, MTTR < 30 min for allowlisted components

## Known Limitations & Future Work

1. **Single-component diagnosis**: MVP diagnoses first failure only. Future: prioritize (critical > warning), batch repairs, parallel kickstarts.

2. **No LLM in MVP**: Deterministic patterns only. Future: LLM classification for ambiguous evidence (gated by allowlist); never add remedy outside code.

3. **No cross-component repair logic**: Each tick runs one diagnosis. Future: chain repairs (e.g., if API is down, health-sentinel can't write → restart API first).

4. **Shallow verification**: Re-reads snapshot only. Future: poll `/api/health` repeatedly, measure response time, check error rates.

5. **No escalation notifications yet**: Parks silently. Future: Slack notify on escalation; escalation row has decision_required_by (TTL).

6. **MTTR not yet on dashboard**: Stored in state; future: aggregate and visualize per component per day.

## References

**Bridge**: `loops/omniagentos_loops/templates/monitor_diagnose_repair_verify.py`  
**Tools contract**: `loops/omniagentos_loops/tools.py`  
**Instance module**: `loops/omniagentos_loops/instances/health_monitor.py`  
**Tests**: `loops/tests/instances/test_health_monitor.py`  
**Health Sentinel**: `scripts/health-sentinel/health_sentinel.py`  
**Seeding guide**: `loops/W3_SEEDING_GUIDE.md`
