# Ops Runtime & Background Services Truth Table

This document serves as the authoritative truth table and troubleshooting reference for the OmniAgentOS aux background services. These background jobs keep the platform honest, run daily/nightly curation and sweeps, and manage the platform's self-improvement loops.

---

## The Service Truth Table

| Service | Plist Label | Template | Rendered Path | Schedule | Port (if any) | Log Path | Load / Unload / Restart | Owner |
| :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- |
| **provider-sentinel** | `com.omniagentos.provider-sentinel` | `scripts/provider-sentinel/com.omniagentos.provider-sentinel.plist.template` | `var/launchd/rendered/com.omniagentos.provider-sentinel.plist` | Daily at 22:30 | *None* | `var/log/provider-sentinel.log` | `launchctl load/unload/restart` (see below) | `this repo` (Refuses auto-load) |
| **routines tick** | `com.omniagentos.routines` | `scripts/scheduler/com.omniagentos.routines.plist.template` | `var/launchd/rendered/com.omniagentos.routines.plist` | Every 5 minutes (300s) | *None* | `var/log/routines.log` | `launchctl load/unload/restart` (see below) | `this repo` (Refuses auto-load) |
| **swarm-optimizer** | `com.omniagentos.swarm-optimizer` | `scripts/swarm/com.omniagentos.swarm-optimizer.plist.template` | `var/launchd/rendered/com.omniagentos.swarm-optimizer.plist` | Twice Daily (03:45, 15:45) | *None* | `var/log/swarm-optimizer.log` | `launchctl load/unload/restart` (see below) | `this repo` (Auto-loaded) |
| **modelintel refresh** | `com.omniagentos.modelintel` | `scripts/scheduler/com.omniagentos.modelintel.plist.template` | `var/launchd/rendered/com.omniagentos.modelintel.plist` | Daily at 07:15 | *None* | `var/log/modelintel.log` | `launchctl load/unload/restart` (see below) | `this repo` (Auto-loaded) |
| **steward briefing** | `com.omniagentos.steward.briefing` | `scripts/scheduler/com.omniagentos.steward.briefing.plist.template` | `var/launchd/rendered/com.omniagentos.steward.briefing.plist` | Daily (7:30 default) | *None* | `var/log/steward-briefing.log` | `launchctl load/unload/restart` (see below) | `this repo` (Refuses auto-load) |
| **steward metrics** | `com.omniagentos.steward.metrics` | `scripts/scheduler/com.omniagentos.steward.metrics.plist.template` | `var/launchd/rendered/com.omniagentos.steward.metrics.plist` | Daily at 00:00 | *None* | `var/log/steward-metrics.log` | `launchctl load/unload/restart` (see below) | `this repo` (Refuses auto-load) |
| **steward alerts** | `com.omniagentos.steward.alerts` | `scripts/scheduler/com.omniagentos.steward.alerts.plist.template` | `var/launchd/rendered/com.omniagentos.steward.alerts.plist` | Daily at 00:00 | *None* | `var/log/steward-alerts.log` | `launchctl load/unload/restart` (see below) | `this repo` (Refuses auto-load) |
| **steward comms** | `com.omniagentos.steward.comms` | `scripts/scheduler/com.omniagentos.steward.comms.plist.template` | `var/launchd/rendered/com.omniagentos.steward.comms.plist` | Daily at 02:30 | *None* | `var/log/steward-comms.log` | `launchctl load/unload/restart` (see below) | `this repo` (Refuses auto-load) |
| **hygiene** | `com.omniagentos.hygiene` | `scripts/hygiene/com.omniagentos.hygiene.plist.template` | `var/launchd/rendered/com.omniagentos.hygiene.plist` | Daily at 04:15 | *None* | `var/log/hygiene.log` | `launchctl load/unload/restart` (see below) | `this repo` (Refuses auto-load) |
| **curator** | `com.omniagentos.fable-curator` | `scripts/fable-curator/com.omniagentos.fable-curator.plist.template` | `var/launchd/rendered/com.omniagentos.fable-curator.plist` | Daily at 23:00 | *None* | `var/log/fable-curator.log` | `launchctl load/unload/restart` (see below) | `this repo` (Auto-loaded) |
| **backlog-executor** | `com.omniagentos.backlog-executor` | `scripts/backlog-executor/com.omniagentos.backlog-executor.plist.template` | `var/launchd/rendered/com.omniagentos.backlog-executor.plist` | Daily at 00:30 | *None* | `var/log/backlog-executor.log` | `launchctl load/unload/restart` (see below) | `this repo` (Refuses auto-load) |
| **archi-morning** | `com.omniagentos.archi-morning` | `scripts/archi-morning/com.omniagentos.archi-morning.plist.template` | `var/launchd/rendered/com.omniagentos.archi-morning.plist` | Daily at 05:30 | *None* | `/tmp/com.omniagentos.archi-morning.out.log` | `launchctl load/unload/restart` (see below) | `owned-elsewhere-today` |

---

## Launch / Unload / Restart Commands

For any given `<rendered-plist-path>`:

- **Load (Register and start scheduler):**
  ```sh
  launchctl load var/launchd/rendered/com.omniagentos.<name>.plist
  ```
- **Unload (Unregister from schedule):**
  ```sh
  launchctl unload var/launchd/rendered/com.omniagentos.<name>.plist
  ```
- **Restart (Force refresh configuration):**
  ```sh
  launchctl unload var/launchd/rendered/com.omniagentos.<name>.plist 2>/dev/null || true
  launchctl load var/launchd/rendered/com.omniagentos.<name>.plist
  ```

---

## Cold Start Guide

To restore the background schedule from a cold start:

1. **Verify or recreate log directory:**
   ```sh
   mkdir -p var/log
   ```
2. **Generate all plists via their respective installers:**
   ```sh
   sh scripts/provider-sentinel/install.sh
   sh scripts/scheduler/install-routines.sh
   sh scripts/swarm/install-swarm-optimizer.sh
   sh scripts/scheduler/install-modelintel.sh
   sh scripts/scheduler/install-steward.sh
   sh scripts/hygiene/install-hygiene.sh
   sh scripts/fable-curator/install.sh
   sh scripts/backlog-executor/install.sh
   ```
   *Note: Under standard operation, `swarm-optimizer`, `modelintel refresh`, and `curator` are safe to run auto-loaded and will load immediately.*

3. **Manually Load Budget-Impact Services (Once Authorized):**
   ```sh
   # Load routines tick (fires real tasks)
   launchctl load var/launchd/rendered/com.omniagentos.routines.plist

   # Load backlog executor (unattended execution)
   launchctl load var/launchd/rendered/com.omniagentos.backlog-executor.plist

   # Load provider sentinel and steward set
   launchctl load var/launchd/rendered/com.omniagentos.provider-sentinel.plist
   launchctl load var/launchd/rendered/com.omniagentos.steward.briefing.plist
   launchctl load var/launchd/rendered/com.omniagentos.steward.metrics.plist
   launchctl load var/launchd/rendered/com.omniagentos.steward.alerts.plist
   launchctl load var/launchd/rendered/com.omniagentos.steward.comms.plist

   # Load hygiene sweep
   launchctl load var/launchd/rendered/com.omniagentos.hygiene.plist
   ```

---

## Troubleshooting & Internals

### The Dashboard Port Decision
- **Problem:** Port `3001` (the default launcher dashboard port) is squatted by a Docker container returning 404, which cannot be killed or modified. Port `3002` is currently owned/occupied by a parallel development lane running its own dev dashboard.
- **Resolution:** We standardized the dashboard port on **`3003`** by editing `scripts/launch-supervised.sh` to export `OMNIAGENTOS_DASH_PORT="${OMNIAGENTOS_DASH_PORT:-3003}"`. This is confirmed as open and free via `lsof` checks.
- **Verification:**
  ```sh
  lsof -nP -iTCP:3003 -sTCP:LISTEN
  ```

### What `launchctl list` Exit Statuses Mean
When inspecting background jobs using `launchctl list | grep omniagentos`, look at the second column:
- `0`: Success. The job was loaded correctly and either completed its previous execution run successfully or is registered and idling.
- `78`: Configuration Error. This is a fatal plist error (e.g., pointing to a non-existent directory/executable, a syntax error in XML arguments, or permission issues). **Any job reporting status 78 must be repaired immediately.**
- `-`: The first column represents the PID. If the job is active/running, it displays a PID. If it is scheduled and waiting for the timer interval, it displays `-` (which is the expected normal state for interval-based schedulers).

### Reading logs
All background job stderr and stdout are consolidated under `var/log/` within the repository root:
- Curator logs: `var/log/fable-curator.log`
- Swarm Optimizer logs: `var/log/swarm-optimizer.log`
- Model Intelligence logs: `var/log/modelintel.log`
- Hygiene logs: `var/log/hygiene.log`
- Backlog Executor logs: `var/log/backlog-executor.log`

To watch active output:
```sh
tail -f var/log/<name>.log
```

---

## The dashboard's trusted hop — browse the caddy port, not `:3003` (LS-003)

`dashboard/src/middleware.ts` and `serverProxy.ts::requireTrustedHop` refuse any
`/api/**` request that does not carry `X-Omni-Trusted-Hop` equal to
`OMNIAGENTOS_TRUSTED_HOP_SECRET`. Both guards are correct and fail closed by
design. Until 2026-08-06 nothing injected that header, so every public read and
every authorized call 403'd and the dashboard was dark for users. The repair was
a deployment one — the guards were not weakened.

**Operator entry point:** `http://127.0.0.1:$OMNIAGENTOS_CADDY_PORT` (default **3013**).
`:3003` is the origin server; a request that reaches it without passing through
caddy carries no hop header and is refused. That is the intended behaviour —
"it came from loopback" is an argument for more scrutiny here, not less, because
a loopback request gets a session token injected on its behalf.

| Carrier | Where |
| --- | --- |
| generated (once, 0600, atomic) | `launch-supervised.sh::_hop_secret` |
| stored | `var/secrets/trusted-hop-secret` (gitignored) |
| exported to the comparer | `_dashboard()` → `npm run start` |
| exported to the injector | `_caddy()` → `caddy run` |
| stripped + injected | `configs/dashboard-caddy/Caddyfile` (`header_up`) |
| compared | `middleware.ts` (edge) and `serverProxy.ts` (node) |

Both processes read the SAME file, so a supervised start cannot drift.

### Diagnosing a 403

Every refusal now names itself in the dashboard log
(`var/runtime/logs/dashboard.log`); the 403 response body is deliberately
unchanged and still tells the caller nothing.

```
[dashboard] trusted-hop DENIED reason=<...> layer=<middleware|serverProxy> path=... \
  expected_fp=<8 hex|unset> supplied_fp=<8 hex|absent> ...
```

| `reason=` | What it means | Fix |
| --- | --- | --- |
| `hop_secret_unset` | This dashboard process has no secret; **no** caller can succeed. The original LS-003 outage. | Start it via `launch-supervised.sh dashboard`. |
| `hop_header_absent` | The request never crossed caddy. | Browse `$OMNIAGENTOS_CADDY_PORT`, not `:3003`. |
| `hop_header_empty` | Caddy is running without its own copy of the secret. | Restart `launch-supervised.sh caddy`. |
| `hop_header_mismatch` | The two sides hold **different** secrets (drift), **or** the header was forged. | `launch-supervised.sh status` prints the file's fingerprint — if it already equals `expected_fp`, there is no drift and this was a genuinely untrusted caller. |

`launch-supervised.sh status` prints `HOP: fingerprint <tag>`. The tag is
FNV-1a/32 of the secret — a deliberately lossy 32-bit tag, never the value and
never a prefix of it.

### Notes

- A supervised start checks `/api/health` **through** the caddy port once the
  fleet is up, and `status` re-checks it live. That check is scoped to the
  dashboard boundary and **never gates the fleet start**: a dark dashboard is an
  observability outage while sessions still complete and work still lands,
  whereas a fleet that will not start is a total one, and trading the second for
  the first would be a bad deal even though the misconfiguration is loud. A
  failure prints `WARNING(trusted-hop): dashboard front door NOT serving — …`
  and the rest of the fleet carries on.
- Because containment must not become silence — silence is the defect LS-003
  *was* — `launch-supervised.sh status` recomputes the verdict on every
  invocation (`front door: serving` / `front door: NOT SERVING — <reason>`)
  rather than reading back a recorded one that would go stale on a rotation.
- No caddy installed (or `OMNIAGENTOS_CADDY_DISABLE=1`) skips the child with a named
  reason on stderr; the fleet still starts, and the dashboard's API surface
  refuses everything until a front door exists.
- Local development without caddy requires
  `OMNIAGENTOS_DASHBOARD_DEV_ALLOW_NO_HOP=1`,
  `OMNIAGENTOS_TRUSTED_HOP_SECRET`,
  `OMNIAGENTOS_DASHBOARD_DEV_ACCESS_SECRET`,
  `OMNIAGENTOS_DASHBOARD_DEV_PRINCIPAL`, and the matching HTTP Basic
  credential; `NODE_ENV=production` ignores the mechanism unconditionally. See
  `docs/runbooks/dashboard-local-auth.md` for setup.
- `scripts/launch-omniagentos.sh` (the legacy Omni launcher) starts the same
  dashboard and does **not** wire a hop secret or a caddy target: its dashboard
  logs `reason=hop_secret_unset` and refuses `/api/**`. Unchanged by this work —
  use the Grok launcher.
