golden-suite-sentinel

# golden-suite-sentinel — nightly north-star benchmark runner

You are the golden-suite sentinel, OmniAgentOS's automated north-star metric:
nightly p50/p90 wall-clock-to-GREEN on three FIXED benchmark briefs, tracked
against the A0.0 baseline (`devtasks/SWARM-BASELINE-2026-07-23.md`, plan
section A0.0) so every phase of work is judged apples-to-apples against the
same trivial / medium / swarm asks.

**This file is NOT fed to an LLM.** `run_golden.py` makes ZERO LLM calls of
its own — the benchmark briefs it dispatches route through the system
NORMALLY (so *their* execution may call Fable/Codex/etc — that is the whole
point of measuring wall-clock-to-green), but the sentinel's own driver code
never does. This file exists in the same "prompt.md" shape as the other
scheduled jobs' prompt files in this repo (`scripts/fable-curator/prompt.md`)
so a human — or the dashboard's file editor — can retune the sentinel's
behavior by editing one readable file instead of touching code: the fenced
`policy:` block below is parsed at runtime by `run_golden.py::load_policy()`.

## What each run does

1. For each of the three fixed benchmarks in `benchmarks.yaml` (trivial,
   medium, swarm): make a fresh scratch git repo under
   `var/golden/runs/<UTCdate>/<name>/`, dispatch the brief via the real API
   (`POST /api/intake/quick` or `POST /api/swarm` — see
   `contracts/swarm-api.md`), poll to a terminal state, then run its
   acceptance checks. Wall-clock-to-GREEN is dispatch → all acceptance
   checks passing; any failure (a terminal-failed/cancelled run, a timeout,
   a failed acceptance check) is recorded as a DNF with a reason string,
   never a crash.
2. Append one line per benchmark to `var/golden/history.jsonl`
   (`{date, name, seconds|null, dnf_reason, run_ref}`, idempotent per
   `(date, name)` — a re-run on the same UTC day never double-records a
   benchmark it already has a line for), then compute rolling p50/p90 per
   benchmark and check the regression rule below.
3. Append exactly one summary line to `var/improvement-log.jsonl`
   every run, win or lose (`improver: "golden-sentinel"`, `changes: []`,
   `notes` = a one-line summary of tonight's times/DNFs).

## Regression rule

A benchmark's tonight value counts as "one regression night" when it is more
than `regression_threshold_pct` percent worse than the rolling median of the
prior `rolling_window` nights for that SAME benchmark (a DNF always counts
as a regression night — there is no numeric comparison to make). An alert
(`record_notification(kind="alert", title="Golden-suite regression: <name>")`)
fires only once `consecutive_nights` nights IN A ROW are regression nights
for the same benchmark — one bad night alone never pages anyone.

## Policy (parsed at runtime by `run_golden.py`)

Edit the values in the fenced block below to retune behavior without
touching any code. Malformed YAML, a missing `policy:` mapping, or a key
with a value that cannot be coerced to its expected type falls back to the
matching CODE DEFAULT for that key only (logged as a warning) — a partial
edit (only some keys present/valid) overrides just those keys, and
`run_golden.py` never crashes because this file was hand-edited badly.

```yaml
policy:
  # A benchmark's rolling-median comparison must be this many percent worse
  # (or DNF) to count as one "regression night" for that benchmark.
  regression_threshold_pct: 25
  # A regression alert fires only once this many CONSECUTIVE nights in a row
  # are regression nights for the same benchmark.
  consecutive_nights: 2
  # How many prior nights' successful `seconds` values feed the rolling
  # median baseline each night is compared against.
  rolling_window: 7
  # Fallback per-benchmark timeout (minutes) when a benchmarks.yaml entry
  # omits its own `timeout_minutes`.
  default_timeout_minutes: 15
  # The fixed-brief file this run reads, relative to this directory.
  benchmarks_file: benchmarks.yaml
```

## Notes (human)
