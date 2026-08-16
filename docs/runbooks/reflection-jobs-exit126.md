# Runbook — reflection jobs exit 126 (D3 / N4r)

## Confirmed root cause

Both `com.omniagentos.reflection-nightly` and
`com.omniagentos.reflection-watchdog` were failing with **exit code 126**
("cannot execute").

### Evidence (captured 2026-07-27)

**Log tails** (`var/log/reflection-nightly.log`, `var/log/reflection-watchdog.log`):

```
/bin/sh: /Users/youruser/OmniAgentOS/scripts/reflection/reflect-nightly.sh: Permission denied
/bin/sh: line 0: exec: /Users/youruser/OmniAgentOS/scripts/reflection/reflect-nightly.sh: cannot execute: Undefined error: 0
/bin/sh: /Users/youruser/OmniAgentOS/scripts/reflection/reflect-watchdog.sh: Permission denied
/bin/sh: line 0: exec: /Users/youruser/OmniAgentOS/scripts/reflection/reflect-watchdog.sh: cannot execute: Undefined error: 0
```

**Rendered ProgramArguments** (pre-fix, from
`var/launchd/rendered/com.omniagentos.reflection-nightly.plist` — diagnosis
only; this task does **not** edit generated files under `var/launchd`):

```xml
<key>ProgramArguments</key>
<array>
    <string>/bin/sh</string>
    <string>-lc</string>
    <string>set -a; . "$HOME/.config/omni/connections.env" 2>/dev/null; set +a; exec "/Users/youruser/OmniAgentOS/scripts/reflection/reflect-nightly.sh"</string>
</array>
```

**On-disk modes (historical):** when the jobs last fired, the job scripts were
not executable by the launchd session (permission denied on `exec`). Even after
`chmod +x`, relying on `exec "<script>"` reintroduces the same class of failure
whenever mode bits are lost on copy/render/checkout.

**`file` output (current sources):**

```
scripts/reflection/reflect-nightly.sh:  POSIX shell script text executable, ASCII text
scripts/reflection/reflect-watchdog.sh: POSIX shell script text executable, ASCII text
```

### Root cause (confirmed, not presumed)

The installer rendered ProgramArguments that **`exec` the job script as a
binary**. Exit 126 is the shell's "command found but not executable" code. The
correct construction is to always invoke the script **as an argument to
`/bin/sh`**, which does not require the script's executable bit:

```
ProgramArguments = ["/bin/sh", "<absolute-path-to-job-script>"]
```

## Fix (templates / installer / scripts only)

| Path | Change |
|---|---|
| `scripts/reflection/install-reflection.sh` | Render `["/bin/sh", abs(script)]`; restore mode 0755; never `exec` the script |
| `scripts/reflection/reflect-nightly.sh` | Source `connections.env` inside the script; gate on `OMNIAGENTOS_REFLECTION_REARM_MODE`; always observe-only |
| `scripts/reflection/reflect-watchdog.sh` | Same mode gate + env sourcing |
| `scripts/reflection/*.plist.template` | Unchanged placeholders; installer fills absolute args |
| `var/launchd/**` | **Not modified** by this task (generated artifacts) |

## Mode flag

`OMNIAGENTOS_REFLECTION_REARM_MODE` ∈ {`off`, `shadow`, `enforce`}, default **`off`**.

| Mode | Behaviour |
|---|---|
| `off` | Scripts log and exit 0; jobs stay inert even if loaded |
| `shadow` | Run observe-only (runner `observe_only=True`); no autonomous apply |
| `enforce` | Same observe-only posture until an operator separately clears the shadow-week hold |

## Re-arm (observe-only)

1. Render (does **not** load):

   ```sh
   cd <checkout>
   ./scripts/reflection/install-reflection.sh
   plutil -lint var/launchd/rendered/com.omniagentos.reflection-nightly.plist
   plutil -lint var/launchd/rendered/com.omniagentos.reflection-watchdog.plist
   ```

2. Verify ProgramArguments[0] is `/bin/sh` and ProgramArguments[1] is an absolute
   existing path to the job script.

3. Foreground observe smoke (does not touch launchd):

   ```sh
   export OMNIAGENTOS_REFLECTION_REARM_MODE=shadow
   /bin/sh scripts/reflection/reflect-nightly.sh
   /bin/sh scripts/reflection/reflect-watchdog.sh
   ```

4. Load only after smoke is clean (operator action):

   ```sh
   launchctl bootstrap gui/$(id -u) var/launchd/rendered/com.omniagentos.reflection-nightly.plist
   launchctl bootstrap gui/$(id -u) var/launchd/rendered/com.omniagentos.reflection-watchdog.plist
   ```

5. Disable:

   ```sh
   launchctl bootout gui/$(id -u) com.omniagentos.reflection-nightly
   launchctl bootout gui/$(id -u) com.omniagentos.reflection-watchdog
   ```

## One shadow week hold (one-shadow-week)

Before **any** discussion of `observe_only=False` / autonomous apply:

- Both jobs must complete successfully for **seven consecutive calendar days**
  under `OMNIAGENTOS_REFLECTION_REARM_MODE=shadow` with `observe_only=True`.
- Required evidence for that week:
  - zero exit-126 (or any non-zero) in both log files,
  - nightly produces a report artifact / run row,
  - watchdog reports healthy completion,
  - no config writes and no git commits performed by either job.
- A second cross-lineage review note (grok or opus) is required before re-arm
  discussions proceed past observe-only.

## Regression guard

`tests/scripts/reflection/test_exit126_guard.py` asserts:

- both job scripts exist, have a shebang, and are mode 0755,
- rendered ProgramArguments use `/bin/sh` + absolute script path,
- the absolute script path exists.
