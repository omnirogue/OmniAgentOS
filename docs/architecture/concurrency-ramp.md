# Concurrency ramp (OmniAgentOS)

**Never merge to OmniAgentOS.** This product ramps independently.

## Preconditions

1. Isolation drill green: `tests/scope/test_isolation_drill.py`
2. Scope locks in **shadow** (default) with acceptable `scope_declared_vs_actual`
3. SSE load harness baseline captured
4. `python -m omniagentos.routing.fleet_preflight` run and its binding constraint
   understood (see below) — ramping a dial that is not the binding one changes
   nothing.

## Dials

| Dial | Config / env | Ramp |
|------|----------------|------|
| Runner | `OMNIAGENTOS_RUNNER_CONCURRENCY` / `configs/concurrency.yaml` | 4 → 8 → **16** → 32 |
| Fleet sessions | `max_sessions_global` (configs/swarm.yaml) | 120 → **260** |
| Reserved small-task slots | `reserved_small_task_slots` | 20 → **40** |
| Concurrent swarms | `max_concurrent_swarms` | 10 → **20** |
| Per project | `max_sessions_per_project` | 20 → **25** (advisory, unenforced) |
| Per-run slots | `swarm.scheduler.MAX_SLOTS` / `planner.TARGET_N_HARD_CEILING` | 10 → **20** |
| Planner task cap | `swarm.planner.MAX_TASKS` | 20 → **30** |
| Per-account inflight | `configs/swarm.yaml limits.providers.*` | measure rate_limited before buying accounts |
| In-session agents / account | `insession.*` + `limits.providers.claude.max_agents_per_account` | **LIVE** at **60** (sessions + live grant budgets) |
| **File descriptors** | `ulimit -n` / launchd `SoftResourceLimits` | 256 (launchd default) → **8192** |

`configs/swarm.yaml` is **authoritative** for the three fleet keys that appear in
both config files; `configs/concurrency.yaml` mirrors them for this runbook and
is read by no code. `tests/routing/test_fleet_scale.py` fails if they drift.

## File descriptors (macOS) — do this BEFORE the 200-agent drill

Every live session costs the supervisor process at least one pipe descriptor
(`subprocess.Popen(stdout=PIPE, stderr=STDOUT)` in
`omniagentos/sessions/supervisor.py`), plus the SQLite main/WAL/SHM descriptors
each open store holds. Budget ~4 fds/session over a ~128 fd base, i.e. **~950
descriptors for 200 concurrent sessions**.

A **launchd-started job inherits a 256 fd soft limit** unless the plist says
otherwise — roughly 32 sessions. The failure is not a queued run; it is an
`OSError: [Errno 24] Too many open files` mid-spawn, and it looks like a random
session failure.

* Interactive/dev: `ulimit -n 8192` before launching the API/runner/supervisor.
  `scripts/launch-omniagentos.sh` does this itself (raise-only: an
  already-high interactive limit is never lowered).
* launchd: add to the job's plist `<dict>` (alongside `ProgramArguments`):

  ```xml
  <key>SoftResourceLimits</key>
  <dict>
      <key>NumberOfFiles</key><integer>8192</integer>
      <key>NumberOfProcesses</key><integer>2048</integer>
  </dict>
  ```

  macOS caps a per-process soft limit at `kern.maxfilesperproc` (245760 on this
  hardware), so 8192 is not near any system ceiling.
* In-process (a launcher that cannot edit the plist):
  `omniagentos.routing.fleet_preflight.raise_nofile_soft_limit()` raises this
  process's soft limit to 8192 best-effort. It is deliberately NOT called on
  import and NOT called by the supervisor — a library must not mutate
  process-wide limits behind its caller's back.

`fleet_preflight` reports `os.file_descriptors` as a first-class ceiling and
warns whenever the soft limit is below 8192.

## Which ceiling is binding?

```
python -m omniagentos.routing.fleet_preflight          # human
python -m omniagentos.routing.fleet_preflight --json   # machine
```

It reads every ceiling (YAML, module constants, per-account capacity,
RLIMIT_NOFILE), pairs each with live usage, and names the one with the least
headroom. `run.slots` and `project.sessions` are reported but never selected:
nothing enforces them fleet-wide.

Expect `provider.account_inflight` (enabled accounts x
`max_inflight_per_account`) to be the binding constraint on a small account
pool. When it is, raising `max_sessions_global` further does nothing — add
accounts or raise the per-account ceiling.

## In-session fan-out (PKG-INSESSION-FANOUT, LIVE)

`insession.enabled: true` (shipped since 2026-07-27) lets a claude
swarm worker whose validated subtasks_request would otherwise split into 2-4
child PROCESSES receive a coordinator grant to run them as live subagents
inside its own session (`subtasks_grant.<attempt>.json`; every Task call is
consumed server-side against the grant by the PreToolUse hook). The parent
attempt stays the single verify/review/merge unit.

Capacity math changes shape: grants are admitted against
`limits.providers.claude.max_agents_per_account` (live sessions + committed
grant budgets — the `provider.account_agents` ceiling in `fleet_preflight`),
while every process ceiling above is untouched. At 4 enabled accounts the
agent ceiling is 4 × 60 = **240 concurrent agents** on ~80 processes — the
only route past the `provider.account_inflight` bind without buying accounts.
Rollback: set `insession.enabled: false` (or env `0`) — new grants stop
immediately; live grants drain at attempt close (voided) or TTL (30 min).

## Rollback

On sustained `database is locked`, scope conflict spike, or SSE lag regression:

1. Set `OMNIAGENTOS_RUNNER_CONCURRENCY=4`
2. Lower `max_sessions_global` in `configs/swarm.yaml` (mirror it in
   `configs/concurrency.yaml`); the signed fair-share term shrinks live runs at
   their next reconcile rather than killing attempts
3. Keep scope at `shadow` (do not flip to enforce under panic)
4. Capture logs under `var/log/`

## Enforced code knobs

See `configs/concurrency.yaml` and `configs/parallelism.yaml` (shadow).
