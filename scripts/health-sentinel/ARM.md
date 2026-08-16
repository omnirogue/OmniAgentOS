# ARM.md — blocked-session detector + standing audit

Both arms in this package are **DISARMED**. They were built, tested and rendered;
nothing was loaded and nothing has been delivered off this machine.

Current state, verified at build time:

| thing | state |
|---|---|
| `com.omniagentos.blocked-session-detector` | **rendered, NOT installed, NOT loaded** |
| `com.omniagentos.health-sentinel` | untouched — still `StartInterval 1800` |
| notification push | **disabled** (`--no-push` is the default) |
| alerts | recorded to `var/log/blocked-session-alerts.jsonl`, delivered nowhere |
| `--audit` | read-only, writes nothing, on no schedule |

---

## Arm the detector — ONE command

```sh
sh /Users/youruser/OmniAgentOS/scripts/health-sentinel/install-blocked-session-detector.sh
```

(That same line is already inside `scripts/health-sentinel/install.sh`, so
re-running the sentinel installer arms it too. Running `install.sh` therefore
loads **two** labels, not one.)

### Blast radius of that command

* Writes `~/Library/LaunchAgents/com.omniagentos.blocked-session-detector.plist`.
* `launchctl bootout` + `bootstrap` that label under `gui/$(id -u)`. This changes
  the running fleet: a new job starts firing **every 300 seconds**, forever,
  including across reboots.
* Each firing runs `health_sentinel.py --watch-blocked`, which reads the last
  64 KB of every `~/.claude*/projects/*/*.jsonl` modified in the last 24 h.
  Measured: 0.4 s for 94 files across the three live stores; 2.5 s worst case
  across all 15 stores including a slow `lsof`. It takes no locks, opens no
  network socket, and writes only `var/log/blocked-session-detector.log` and
  `var/log/blocked-session-alerts.jsonl`.
* It still **delivers nothing**, because push is separately disarmed (below).

### Disarm

```sh
launchctl bootout "gui/$(id -u)/com.omniagentos.blocked-session-detector"
rm -f "$HOME/Library/LaunchAgents/com.omniagentos.blocked-session-detector.plist"
```

---

## Arm the notification push — SEPARATE, and more consequential

Pushes leave the machine. Until this is done, a blocked session is only ever a
line in `var/log/blocked-session-alerts.jsonl`.

Verify first, with the detector *not* pushing:

```sh
cd /Users/youruser/OmniAgentOS
.venv/bin/python scripts/health-sentinel/health_sentinel.py --watch-blocked --json | \
  /usr/bin/python3 -c 'import json,sys; d=json.load(sys.stdin); print(len(d["blocked"]), "would alert")'
```

Then arm it by appending `--arm-push` to the job's argument list in
`scripts/health-sentinel/blocked-session-detector.sh` (the `"$@"` on the last
line already forwards it) or, for a single manual run:

```sh
.venv/bin/python scripts/health-sentinel/health_sentinel.py --watch-blocked --arm-push
```

### Blast radius of `--arm-push`

* Delivers a macOS banner via the EXISTING transport,
  `omniagentos/sessions/notify.py:48 push` — the same seam
  `omniagentos/sessions/supervisor.py`'s 20-minute MAX-PARK ceiling already uses
  for API-managed sessions. This package exists because that ceiling never
  covered CLI sessions.
* If `OMNI_NTFY_URL` is set in the environment, `push` also POSTs to that ntfy
  endpoint — **that is a network egress off this machine.** The phone leg is
  gated by the transport allowlist (`notifications/policy.py`): this caller
  labels its push `kind="blocked"`, which is on the allowlist, so it DOES go to
  the phone. The ntfy payload is title + Click link only — the six-key alert
  detail below never leaves the box.
* The payload is EXACTLY six keys: `sessionId, account, cwd, gitBranch,
  tool_name, minutes_blocked`. No transcript content, no tool input, no message
  text. `tests/acceptance/s23_blocked_detector.sh` step 4 asserts this key set
  and that no long token from the transcript body appears in any value.
* De-duplicated per `(sessionId, tool_use_id)`; a still-blocked session
  re-alerts only at 4×T (60 min at the shipped T), then 8×T, and so on.

### Disarm push

Remove `--arm-push`. `--no-push` is the default and needs no flag.

---

## The audit arm

```sh
cd /Users/youruser/OmniAgentOS
.venv/bin/python scripts/health-sentinel/health_sentinel.py --audit          # one screen
.venv/bin/python scripts/health-sentinel/health_sentinel.py --audit --json   # machine-readable
```

**Blast radius: none.** The audit is a pure read-only function of (repo tree,
config files, ledger). It opens the control-plane SQLite `mode=ro`, makes
read-only TCP connects to declared loopback ports, and writes nothing at all
unless you pass `--audit-log PATH`. `tests/acceptance/s00_audit.sh` step 3
sha256-snapshots the whole tree before and after a run and asserts nothing
changed.

It is deliberately on **no schedule**. Putting it on the sentinel's 1800 s label
would couple a slow drift audit to stall detection; giving it its own label is a
decision for whoever decides its cadence, not something this package did quietly.

### Exit codes

| code | meaning |
|---|---|
| 0 | every registered check RAN and REPORTED (findings may still exist) |
| 1 | `--fail-on-finding` was passed and at least one check reports `fail` |
| 2 | machinery failure: a registered check did not report, or the registry is unreadable |

A check that cannot run reports `fail`, never `skip`. Silence is never a pass.

---

## What is deliberately NOT armed, and why

* **`check_loopback_connectors` never restarts anything.** `https://127.0.0.1:8443`
  is the LiteLLM → OpenRouter proxy. It spends real money under a $50/day cap
  enforced by a separate, currently-loaded watchdog
  (`com.youruser.litellm-spendguard`), and its log ends in a *clean* shutdown.
  An auto-restarter would race a guard whose entire job is to stop that process.
  The check notifies and stops.
* **`scripts/new-lane.sh`** gained exactly one line (`var/LANE-CLASS`). It creates
  no schedule and changes no lane semantics.
* **The health sentinel's own plist was not touched.** Confirm any time with:
  ```sh
  grep -A1 StartInterval var/launchd/rendered/com.omniagentos.health-sentinel.plist
  ```
  It must still say `1800`. `s23_blocked_detector.sh` step 7 asserts it.
