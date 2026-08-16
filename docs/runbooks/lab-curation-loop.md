# Runbook — lab curation loop (`com.omniagentos.lab-curation`)

**Status: observe-only.** The job proposes experiments and writes them to a file.
It never promotes a champion, never runs an experiment, and never writes to the
live lab database. Promotion stays a separate, human-initiated decision.

| | |
|---|---|
| Label | `com.omniagentos.lab-curation` |
| Interval | daily at 03:20 local (`StartCalendarInterval` Hour+Minute, no Weekday) |
| `RunAtLoad` | `false` — installing never triggers a pass |
| Program | `<checkout>/scripts/lab/run_curation.sh` (mode 0755) |
| Runner | `scripts/lab/curation_loop.py` (`run` / `render` / `self-test`) |
| Template | `ops/launchd/com.omniagentos.lab-curation.plist.template` |
| Artifacts | `var/lab/curation/proposals-<UTC>.json` |
| Log | `var/log/lab-curation.log` (also `StandardOut/ErrorPath`) |
| Mode flag | `OMNIAGENTOS_LAB_CURATION_MODE` = `off` (default) / `shadow` / `enforce` |

### Mode flag

| Mode | Behaviour |
|---|---|
| `off` (default) | Job is inert: logs and exits 0 without calling `propose_experiments`. |
| `shadow` | Observe-only proposal pass; proposals written to artifact; no promote/execute. |
| `enforce` | Same observe-only pass today (still no auto-promote). Use after a clean shadow soak. |

Arm with e.g. `export OMNIAGENTOS_LAB_CURATION_MODE=shadow` in the launch environment
(or the connections.env the job sources) before enabling the plist.

## What one pass does

`scripts/lab/curation_loop.py run` calls
`omniagentos.lab.campaign.propose_experiments` once per discipline and serializes
the proposals. `propose_experiments` normally *persists* what it proposes and
versions a challenger surface on disk, so the runner never gives it live state:

1. the lab database is **copied** into a throwaway sandbox directory,
2. `omniagentos.lab.surfaces._repository_root` is redirected at that same sandbox,
   so challenger files land there instead of in `vault/`,
3. the sandbox is deleted when the pass ends,
4. the live database's campaign tables (`experiments`, `surfaces`, `champions`,
   `eval_results`, `judge_records`, `tournaments`) are fingerprinted read-only
   before and after; the artifact records `campaign_fingerprint_before`/`_after`
   and the run **exits 4** if they differ.

To act on a proposal, an operator reads the artifact and drives promotion through
the normal lab APIs. This job is a feed, not an actuator.

## Render

Rendering only writes a plist file — it never touches launchd.

```sh
cd <checkout>
./scripts/lab/curation_loop.py render
# -> <checkout>/var/launchd/rendered/com.omniagentos.lab-curation.plist
```

Options: `--target <path>`, `--label <label>`, `--hour <0-23>`, `--minute <0-59>`.
The renderer substitutes absolute paths for this checkout (program, working
directory, both log paths) — never edit the rendered plist by hand; re-render it.

## Verify (before install)

```sh
PLIST=<checkout>/var/launchd/rendered/com.omniagentos.lab-curation.plist

plutil -lint "$PLIST"                                  # well-formed plist
./scripts/lab/curation_loop.py self-test --plist "$PLIST"
./scripts/lab/curation_loop.py run                     # one observe pass, prints artifact path
```

`self-test` is the N4r guard (a launchd job that died with **exit 126** because its
program was not executable). It fails loudly when:

- `curation_loop.py` or `run_curation.sh` is not exactly mode `0755`,
- `run_curation.sh` lost its shebang,
- `ProgramArguments[0]` is not absolute, does not exist, or is not executable,
- `RunAtLoad` is not `false`, or `StandardOut/ErrorPath` is missing/non-absolute
  or points into a directory that does not exist,
- the plist still contains `{{PLACEHOLDER}}` markers or has no schedule key.

`run_curation.sh` runs `self-test` itself before every pass, so a job that would
fail this way exits 3 with a log line instead of failing silently under launchd.

Then check the artifact:

```sh
ls -t var/lab/curation/ | head -3
python3 -c 'import json,sys; d=json.load(open(sys.argv[1])); \
print(d["proposal_count"], d["observe_only"], \
d["campaign_fingerprint_before"]==d["campaign_fingerprint_after"])' \
  var/lab/curation/proposals-*.json
```

Expected: `observe_only` true, the two fingerprints equal, `promoted`/`executed` empty.
A `proposal_count` of 0 is normal on a lab with no champion or no eval suite for a
discipline; `errors[]` in the artifact says why.

## Install

Installation is a deliberate human step; nothing in the repo bootstraps it.

```sh
cp "$PLIST" ~/Library/LaunchAgents/com.omniagentos.lab-curation.plist
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.omniagentos.lab-curation.plist
launchctl print gui/$(id -u)/com.omniagentos.lab-curation | head -20
```

`RunAtLoad=false` means bootstrap does not run a pass. Trigger the first one
explicitly, then read the log:

```sh
launchctl kickstart -p gui/$(id -u)/com.omniagentos.lab-curation
tail -20 var/log/lab-curation.log
```

Re-render and re-copy after moving the checkout — the plist holds absolute paths.

## Disable

```sh
# stop the schedule, keep the file
launchctl bootout gui/$(id -u)/com.omniagentos.lab-curation

# remove entirely
rm -f ~/Library/LaunchAgents/com.omniagentos.lab-curation.plist
```

Verify it is gone:

```sh
launchctl print gui/$(id -u)/com.omniagentos.lab-curation   # expect "Could not find service"
```

Disabling stops the feed only. No lab state is created or destroyed by this job,
so there is nothing to roll back; past artifacts under `var/lab/curation/` stay
readable and can be deleted at will.

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| exit 126 in `launchctl print` | program lost its exec bit | `chmod 755 scripts/lab/run_curation.sh scripts/lab/curation_loop.py`, re-run `self-test` |
| exit 3 in the log | `self-test` failed | read the `FAIL` lines it printed; usually mode drift or a stale plist path |
| exit 4 in the log | observe-only violation — the pass changed live campaign state | do **not** re-run; treat as a regression in the sandbox path and escalate |
| `no 3.12+ interpreter` | `.venv` missing | `uv sync` in the checkout |
| empty `proposals` | no champion / no eval suite for the discipline | check `errors[]` in the artifact |
