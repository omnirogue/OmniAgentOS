# Feature-Health Lane

Continuous, background, **non-blocking** per-feature health signal for the 16 product
features (jira, goals, routines, tasks, single_agent, multi_agent, dag, memory,
skill_injection, tool_injection, allocation, team_work_os, landing_pipeline,
account_pool, control_plane, northstar_cert) plus the api_ui surface. It never gates
merges: `make test`, `make test-fast`, merge-gate, and release-gate are untouched by it
(the `feature_health` marker is excluded from default addopts and FAST_LANE_MARKERS).

## Commands

```bash
scripts/feature_health/run.sh tier1              # mechanical, $0, zero GPU (nice, -n $FH_JOBS_TIER1, default 8)
scripts/feature_health/run.sh tier3              # UI-called API paths on an isolated app (nice, -n $FH_JOBS_TIER3, default 4)
scripts/feature_health/run.sh tier2              # live cheap-LLM probes (budget-capped)
scripts/feature_health/run.sh tier3 --live-probes  # + read-only GETs against live :8485
scripts/feature_health/run.sh all --feature dag  # scope any run to one feature
scripts/feature_health/fh.py summary             # per-feature × per-tier grid + freshness
scripts/feature_health/fh.py issues [--include-expected] [--json]
```

## The ledger (what every CLI should read)

- `var/feature-health/ledger-YYYYMM.jsonl` — append-only, flock+fsync, monthly shards.
  One record per (run, tier, feature):
  `{schema:"feature-health.v1", ts, git_sha, git_dirty, provenance, tier, feature, env:"isolated"|"live",
    passed, failed, errors, skipped, expected_failures, duration_s,
    cost_usd, cost_quality:"exact"|"approximate"|"unreported"|null, report_path,
    failures:[{nodeid, message, expected, known_issue_id}], runner, host_load}`
- `var/feature-health/LATEST.md` — regenerated after every run; human/CLI-readable grid.
- Records are produced only by parsing pytest JUnit XML / Playwright JSON — never
  self-reported. A crashed or unparseable run is recorded as `status:"error"` per feature
  (did-not-run is not a pass).

## Provenance: binding a record to the tree it actually measured

`git_dirty` is a scalar count — it can say a checkout was not clean, but it cannot say
whether the dirt was tracked SOURCE or an ignored runtime artifact, and it says nothing
about untracked files a test process could still have executed. `run.sh` therefore takes
one **provenance snapshot** (`fh.py snapshot --out <path>`) before the first test process
of a run, and passes it to every tier's `fh.py append --start-snapshot <path>`; `append`
takes a second snapshot itself at record time. Each record's `provenance` field is:

```
{"start": <snapshot or null>, "end": <snapshot>, "eligible": bool, "ineligible_reason": str|null}
```

where a snapshot is `{sha, tracked_dirty:[...], untracked_executable:[...],
untracked_other:[...], digest, status_error}` — `tracked_dirty` is every path git reports
modified/staged; `untracked_executable` is every untracked path under `omniagentos/`,
`pipeline/`, `scripts/`, or `tests/` (code the run could actually exercise without git
knowing about it); `untracked_other` (scratch/report artifacts) is recorded but never
condemns a run by itself. `digest` canonically hashes `{sha, tracked_dirty,
untracked_executable}`, so two snapshots agree iff nothing that matters moved between them.

A record is `eligible` only when: a `start` snapshot was actually passed, neither snapshot
hit a git failure, `start.sha == end.sha`, `start.digest == end.digest` (the tree did not
move DURING the run), and the `end` snapshot itself has no tracked dirt and no
executable-untracked input. Every other outcome carries an explicit
`ineligible_reason` (`missing_start_snapshot`, `git_status_unavailable`, `invalid_sha`,
`sha_mutated_during_run`, `tree_mutated_during_run`, `tracked_dirty`,
`untracked_executable_input`) — **absence of a binding is never silently read as clean.**
A run still executes and its record is still appended and rendered when the tree is
unsafe; only the *filing* of that record's failures as a product finding is refused.

**Legacy records** (written before this contract existed, with no `provenance` key at
all) are ineligible by construction — the same `missing_start_snapshot`-shaped refusal a
current run gets from a dropped `--start-snapshot` wire, never a favourable pass.

`scripts/feature_health/file_issue_findings.py` reads `provenance` before filing: a
record with real unexpected failures whose provenance is ineligible produces a named
`unsafe_provenance:<reason>` **instrument error** — reported on stderr and in
`instrument_errors`, exactly like a `status:"error"` record — and files **zero** product
findings. A record that IS eligible files each unexpected failure's `base_sha` as that
record's own `git_sha` (the tree that was actually measured), never the filer process's
own current `git rev-parse HEAD` at filing time — an implementer following a stale
`base_sha` was the defect this closes.

## Red-by-design tests

A lane test documenting a known live defect carries
`@pytest.mark.fh_known_issue(id="FH-###")` where the id is a key in
`docs/testing/KNOWN-ISSUES.yaml`. The ledger tags its failure `expected=true`;
`fh.py issues` hides expected failures by default so regressions stand out.
When the defect is fixed, the test goes green: `fh.py issues` flags it for promotion
into the certification suite (and the KNOWN-ISSUES entry flips to `fixed`).

## Tiers

- **tier1 (mechanical)**: existing per-feature suites + gap tests under
  `tests/feature_health/tier1`. No network, no LLM, no GPU. Marker expression is
  composed, never bare (`pytest -m` is store-not-append — see TESTING.md).
- **tier2 (live cheap LLM)**: `tests/feature_health/tier2` (all also `live`-marked).
  Models: gemini-3.6-flash via LiteLLM :4000, OpenRouter micro-probes
  (gemini-3.5-flash-lite / deepseek / qwen-coder-flash, ≤64 tokens), haiku via Claude CLI.
  Spend: exact `usage.cost` accumulates against `FH_TIER2_MAX_USD` (default 0.25);
  CLI harness calls have no cost reporting and are bounded by `FH_TIER2_MAX_CLI_CALLS`
  (default 6) — their ledger records carry `cost_quality:"unreported"`.
  Isolation: pytest-pinned tmp `OMNIAGENTOS_DB`/`OMNIAGENTOS_VAR_DIR`; subprocesses are
  spawned with `fh_subprocess_env` (fails closed if a pin is missing or points at the
  product `var/`); never via a login shell.
- **tier3 (UI/API)**: every dashboard-called API path exercised with real HTTP against an
  isolated app instance (in-process TestClient, or local uvicorn on a free port for
  concurrency tests); a UI→OpenAPI drift test; optional read-only live probes
  (`env:"live"` records); Playwright dashboard suite when :8485 is up.

## Hard exclusions

`tests/doctrine`, `tests/counterfeits`, `tests/simharness`, `tests/longhaul` are never
run by this lane (mutation-bearing / quarantined / cost hazards — the doctrine harness
mutates real product files and a killed run leaves them sabotaged; see TESTING.md).

## Background schedule

`scripts/scheduler/install-feature-health.sh` renders launchd plists (render only, never
auto-loads, per repo convention): tier1+tier3 isolated every 4h; tier2 + live probes +
Playwright nightly 03:10. Contention guard: refuses when 1-min load ≥ `FH_MAX_LOAD`
(default 6.0), runs under `nice -n 10`, caps xdist at 8 workers.

**Render target.** `var/launchd/rendered` is canonical — every loaded estate label lives
there and `docs/OPS-RUNTIME.md` documents loading from it. Two decoys exist:
`var/runtime/launchd/rendered` (what the shared installer expression resolves to in a
plain shell; nothing is ever loaded from it) and `var/launchd/rendered.bak-db-path-fix` (a
pre-DB-path-fix backup — bootstrapping from it loads a job pointed at the wrong database).
The installer now **refuses** a non-canonical target instead of rendering into it; an
override needs `OMNIAGENTOS_LAUNCHD_TARGET_DIR_ACK=1`.

**Loading.** `launchctl bootstrap` is a silent no-op for a label in launchd's disabled
override DB (five estate labels already sit in it), so always
`launchctl print-disabled gui/501 | grep feature-health` and
`launchctl enable gui/501/<label>` **before** bootstrap. The installer prints the exact
sequence.

**Grading activation.** Never grade this lane by exit code — `run.sh` exits 0 in
`--runner launchd` mode by design. The oracle is ledger freshness, graded **per job**:

```bash
# the 4-hourly job (tier1 comes only from it)
scripts/feature_health/fh.py freshness --runner launchd --tier tier1 --max-age-s 18000 --json
# the nightly job (tier2 comes only from it)
scripts/feature_health/fh.py freshness --runner launchd --tier tier2 --max-age-s 108000 --json
```

Both filters matter:

- **`--tier`** makes this a per-job check. Two labels schedule this lane, and an unfiltered
  check stays green off whichever one is alive — a healthy tier1 would certify a nightly that
  was never bootstrapped.
- **`--runner launchd`** restricts the check to scheduled records so a human's manual run
  cannot mask a dead scheduler. That field is not taken on trust: the rendered plists set
  `FH_LAUNCHD=1` in `EnvironmentVariables`, and both `run.sh` and `fh.py` refuse to stamp a
  `runner: launchd` record without it. A hand-typed `--runner launchd` is rejected rather than
  minting fake evidence of a schedule.

Two limits worth stating plainly. **Never grade activation on tier3** — both jobs write it,
so a fresh tier3 proves only that *one* of them fired. And `FH_LAUNCHD` is an anti-footgun,
not an anti-adversary: it stops a hand-typed `--runner launchd` from minting fake evidence,
but anyone who sets the variable (or edits the JSONL) can still forge a record. Pair the
freshness check with `launchctl print gui/$(id -u)/<label>` when the question is adversarial
rather than accidental.

Every bail-out before pytest (missing interpreter, contention guard) writes an `aborted`
ledger record, so a run that did nothing is distinguishable from a run that never happened.
Freshness proves the job **fired**; `newest_status` / `newest_aborted` in `--json` say whether
it did any **work**. Activation is not health — a lane that fires and aborts every time is
fresh and useless.

## Matrix

`configs/feature-health.yaml` maps feature → tier → test paths and is the executable
source of truth (docs/TEST-COVERAGE-MATRIX.md remains the narrative requirement map).
