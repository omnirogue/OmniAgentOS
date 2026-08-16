# Synthetic pipeline heartbeat probe

`run_probe.sh` is an hourly, known-answer canary for the pipeline shape:

`propose -> claim -> build -> gate -> receipt -> learning_event -> cleanup`

A passing run has one `PASS` line for every station, a synthetic proposal with the v1.1-shaped fields `contract`, `id`, `kind`, `title`, and `payload`, a self-contained scratch-repository commit containing the fixed `heartbeat-known-answer.sh`, a non-empty deterministic diff/syntax check, a receipt, one synthetic learning-event line, and no scratch repository or scratch directory left behind. That exact fixed shape makes drift visible as a station-level failure instead of only appearing when real work needs the machinery.

## Stations

| Station | Current synthetic check | Fully wired future target |
| --- | --- | --- |
| `propose` | Writes and JSON-validates a v1.1-shaped proposal in the probe scratch directory. | Write a schema-validated proposal in `var/loopqueue/proposals/`. |
| `claim` | Creates, contention-checks, and releases an O_EXCL JSON claim under `var/heartbeat-probe/claims/`, with the same atomic exclusion semantics as the coordinator claim fence. | Take the coordinator's real O_EXCL claim in `var/loopqueue/`. |
| `build` | Creates a self-contained scratch repository with a `main` base commit, commits the fixed known-answer script, then later removes it. | Build an admitted coordinator task in a throwaway worktree with its scratch `OMNIAGENTOS_DB`. |
| `gate` | Invokes the real `scripts/merge-gate.sh --help` wrapper, then checks the scratch commit's diff against `main` and shell syntax. | Run the shared merge gate against the probe branch and validate its receipt. |
| `receipt` | Writes `var/heartbeat-probe/receipts/<run-id>.json`. | Verify the shared gate-evidence receipt. |
| `learning_event` | Appends a `synthetic: true` JSONL stand-in event in the probe namespace and re-reads that fresh record to enforce the flag. | Write and verify the real learning-event table/view. |
| `cleanup` | Removes every scratch marker, directory, and scratch repository made by this run. | Retain the same cleanup guarantee around the real coordinator stations. |

Receipts use `omniagentos.heartbeat-probe.receipt.v1` and record the run id, timestamp, synthetic flag, dry-run flag, known answer, scratch commit (or `simulated`), and station results observed before receipt creation. Findings are JSON files in `var/heartbeat-probe/findings/` with the v1 schema, `type: "finding"`, synthetic flag, run id, timestamp, failed station, one-line reason, `producer: {"role": "external"}`, and `actor: "heartbeat-probe"`.

## Running it

From the repository root:

```bash
./scripts/heartbeat-probe/run_probe.sh
./scripts/heartbeat-probe/run_probe.sh --dry-run
./scripts/heartbeat-probe/run_probe.sh --inject-failure=gate
```

`--dry-run` (also available as `--self-check`) exercises all stations only inside an ephemeral `mktemp` sandbox: it creates no real git worktree and leaves no durable probe output. The injection flag forces only the named station to FAIL, while the remaining stations still run and the summary retains station-level granularity.

Normal runs retain only receipts, findings, the JSONL event log, and state under `var/heartbeat-probe/`. Any failed station writes a finding. The `state/consecutive_fails.txt` counter increments when any station fails and resets after an all-pass run. Exactly when the counter reaches three, the probe writes and prints a single streak alert in `var/heartbeat-probe/ALERT.md` and appends one operator-alert line to `var/loopqueue/ALERTS.md`; later failures in that same streak do not repeat it.

## Intentional boundaries

This lane does **not** take a claim in `var/loopqueue/**`, load or modify launchd, add a migration or a synthetic database flag, run the full shared merge gate against `main`, or wire real learning-event tables. Its only `var/loopqueue` write is the single append to `ALERTS.md` when a real three-failure streak fires. The real `learning_events` table contamination check remains deferred to the `synthetic-flag` lane, exactly as today; the local JSONL stand-in now enforces its own `synthetic: true` flag by re-reading the freshly-written event. The rendered plist is an artifact only.
