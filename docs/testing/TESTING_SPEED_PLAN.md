# Testing Speed Plan — measurable phases, honest ceilings

**Date:** 2026-07-31
**Supersedes:** all six Gemini-authored research docs (see §7 Kill List)
**Companion to:** `docs/testing_engine_fix_plan.md` (still accurate on the engine's own defects)
**Evidence:** 53-agent research sweep + direct measurement on the target box. Raw pack:
`scratchpad/measured-evidence.md` and the workflow journal.

---

## 1. The verdict in one paragraph

The bottleneck is **not compute, and not parallelism**. It is that (a) the fast lane's marker
expression silently readmits live-API and perf tests, (b) 22–66 tests are non-deterministic
under machine load — in the run TESTING.md quotes as its headline baseline, 22 tests each
burned the full 180 s pytest timeout and contributed **66.6 % of that run's cumulative time**,
and (c) the dev loop is shaped as a full-tree lane, so it pays a ~12 s collection floor before
executing a single test. **No amount of AWS, BrowserStack, or RunPod capacity moves any of
these three numbers.** The recommended spend through Phase 3 is **$0**.

---

## 2. What is actually true (measured, this box)

Apple M2 Ultra — **16 performance cores + 8 efficiency cores**, 64 GB, Python 3.12.12,
pytest 9.1.1, plugins: xdist + timeout only. **No CI exists** (`.github/` absent).

### 2.1 This repo does not have a suite duration. It has a distribution.

Four JUnit artifacts from the same box on the same day:

| Artifact | Time | Tests | Cumulative | Max test | Tests ≥179 s |
|---|---|---|---|---|---|
| `fast-lane-baseline.xml` | 03:45 | 11,564 | **5,945.8 s** | 180.3 s | **22 = 3,960.7 s (66.6 %)** |
| `fast-lane-latest.xml` (03:52, since overwritten) | 03:52 | 11,303 | 2,059 s | 43.5 s | 0 |
| `scaling/n16.xml` | 05:48 | 11,328 | 2,430.8 s | 109.1 s | 0 |
| `fast-lane-latest.xml` (current) | 06:51 | 11,328 | 3,033.9 s | 158.3 s | 0 |

Cumulative swings **2.9x**, wall 271.7 → 814.5 s (**3.0x**), failures 22 → 66 (**3.0x**).
Load averaged 16.5 / 40.7 / 50.1 on 24 cores during the analysis window, because the agent
fleet and the test suite share the same silicon.

**Consequence: until a quiesced-baseline harness exists, no speedup claim about this repo is
falsifiable.** That is why Phase 0 is a measurement harness and not an optimization.

### 2.2 The documented baseline is the worst run ever recorded

TESTING.md's headline "99 min serial-equivalent", the "7.3x / 91 % scaling efficiency" figure,
and the "simharness + counterfeits + longhaul = 75 % of suite time" quarantine premise are all
derived from `fast-lane-baseline.xml` — the run with 22 hangs. Strip the hangs and the real
serial-equivalent is ~1,985 s ≈ **33 min, not 99**. Measured separately, the three quarantined
directories total ~105 s, not 75 % of anything. **A ~33-second suite is currently quarantined
on a false premise.**

### 2.3 Time is concentrated, and the shape is robust even though the seconds are not

From the 03:52 artifact: 60 % of tests run in under 10 ms and contribute 0.6 % of total time;
the **top 1 % of tests carry 45.5 %**, the top 5 % carry 69 %. The absolute seconds move
run-to-run; the concentration does not. Optimize the head, ignore the mass.

### 2.4 Parallelism is already past its knee

| Workers | Wall | Cumulative | Note |
|---|---|---|---|
| `-n 8` (current lane) | 271 s | — | 95 % efficiency |
| `-n 16` | 185 s | 2,430.8 s | the knee |
| `-n 24` | 160 s | **3,149.8 s** | cumulative **rose 30 %** |

`-n 24`'s apparent wall win was contamination — background load fell between the two runs.
Its **cumulative time rose 30 %**, which is the E-core penalty: workers 17–24 land on the 8
efficiency cores at roughly a third the speed. `-n auto` is documented to deadlock the suite.
**Cap at 16.**

### 2.5 The model that predicts wall clock

Under xdist every worker imports and collects the whole tree independently, so collection is a
fixed cost paid in parallel, not a serial fraction:

```
wall = COLLECT + max( cumulative / eff_par , longest_single_test )
```

Check against the 06:51 run: `12.1 + 3033.9/7.53 = 415 s` vs **403 s observed**. The model holds.

---

## 3. The honest ceiling — and where 100x actually lives

**100x on the full-suite lane is dimensionally impossible.** 100x from the 403 s baseline is
4.03 s. The full-tree **collection floor alone is ~12 s** — three times larger — before a
single test executes. The absolute theoretical maximum for a full-tree lane is 403/12.14 =
**33.6x**, and that requires every test to take zero seconds. Realistic ceiling: **6.0x**.
Achievable: **2.6x**, or 3.4x with heroics.

| Stage | Cumulative | Wall | Multiple |
|---|---|---|---|
| Baseline (06:51) | 3,034 s | 403 s | 1.0x |
| P1 marker fix | 2,832 s | 388 s | 1.04x |
| P1 + `-n 16` | 2,832 s | 214 s | 1.88x |
| P3 surgery | 2,017 s | 156 s | 2.6x |
| P4 heroics | — | ~120 s | 3.4x |

**But you do get your 100x — on the loop that actually matters.** The ≤15 s dev loop cannot
come from speed; it comes from **shape**:

- A full-tree lane collects in **12.14 s**. A single directory (`tests/scope`, 301 tests)
  collects in **0.58 s** — a **20x lower floor**, requiring zero optimization.
- Verdict reuse *skips* execution rather than accelerating it.

Dev loop today = the full lane = 403 s. Dev loop after Phase 4 + 5 = a scoped lane on a warm
verdict cache ≈ **3–5 s**. That is the ~100x, and it comes from lane architecture and caching —
**not from renting compute.**

---

## 4. Phases — each with a command that proves it

Every phase is independently valuable, parallel-safe with ongoing OS development, and gated on
a metric you can verify by running something.

### Phase 0 — Measurement floor · 1 day work / 2–3 days elapsed
Make every later claim falsifiable. Build `make bench`: run the lane N ≥ 5 times, **refuse to
start above a load threshold**, emit JSON with p50/p95 wall, cumulative, `eff_par`, failure
count, timeout count, top-25 durations. Correct the stale figures in `Makefile:33-35`,
`TESTING.md:30-38`, and `MEMORY.md` in the same commit.

**Exit:** ≥5 runs at 1-min load < 4.0, p50 wall / cumulative / failure count all within 10 %.
```
make bench && jq -e '(.runs|length)>=5 and .max_load<4.0 and ((.p95_wall_s-.p50_wall_s)/.p50_wall_s)<0.10' var/test-reports/bench-latest.json
```

### Phase 1 — Config-only lane repair · 1 day · **same-day tier, zero test-code edits**
1. **Compose, don't override, the marker expression** at `Makefile:41`. pytest's `-m` is
   store-not-append, so `-m "not smoke"` *discards* the `pyproject.toml:98` exclusion and
   readmits live/perf tests. **Verified:** 8 files / 35 tests leak in — `test_jira_live`,
   `openhands/test_adapter`, `test_embeddings`, `test_recall_perf`, `test_live_all_providers`,
   `test_scale_gates`, `test_sandbox_cli_state`, `test_ingest` — worth 197 s (6.5 %) of the
   06:51 run, and `test_recall_10k_p95` at 158.3 s was that run's **longest single test**, i.e.
   the critical-path floor. It also fires live Jira / OpenHands / Anthropic / Ollama calls on
   every dev run.
   ```make
   -m "not smoke and not (live_cli or perf or live_ollama or live or counterfeit_gate)"
   ```
2. Sweep `-n` over 8/10/12/16, pin the knee. **Never 24, never `auto`.**
3. Split known parallel-unsafe tests into an explicit serial second step.

**Exit:** zero live/perf tests collected; p50 wall improves ≥25 %; failure count does not rise.

### Phase 2 — Isolation & determinism burn-down · 1–2 weeks · fleet-parallelizable by file
The largest recoverable pool and the hard gate for everything downstream — you cannot enforce a
selector or trust a cached verdict while outcome depends on load and neighbours.
Fix the 22 timeout-hangs and the 25-red list. Namespace ports, temp dirs, sockets and SQLite
paths by the xdist `worker_id`. Stop the suite mutating tracked files in place. Convert
scheduler/lock sleep-handoffs to event waits (**for flake reasons, not speed** — real in-parent
blocking sleep is only ~18 s across 82 sites; the widely-quoted "348 s of sleeps" is 330 s of
deliberately-killed child processes).

**Exit:** three consecutive `-n 16` runs plus one `-n 1` produce **identical** test-id→outcome
maps, zero tests ≥179 s, and `git status --porcelain` empty after every run.
```
scripts/isolation-gate.sh
```

### Phase 3 — Cumulative-time surgery · 1–1.5 weeks
**First, re-measure the migration claim** (6,101 calls × 86.8 ms ≈ 530 s). It is the largest
*unverified* number in the pack and accounts for roughly half the projected win. Independent
support: `CollabStore()` measures **463 ms** vs **3.7 ms** for a `copyfile` of a pre-migrated
template — **124x**, across 796 construction sites in 74 files. Micro-bench: `migrate()`
143.3 ms → `Connection.backup()` 9.6 ms → `copyfile` 2.83 ms.

Then: session-scoped **template git repos** for the seven git-heavy modules
(`tests/csi/test_containment_and_lifecycle.py:44` builds a fresh repo per test — 6 subprocess
spawns × 72 tests); subprocess consolidation in the ten CLI-assertion modules; the
`knowledge_enabled()` gate on the two ungated ingest seams.

**Exit:** cumulative drops ≥35 % vs Phase 1; p50 wall ≤150 s; failures unchanged or lower.

### Phase 4 — Lane architecture · 3–5 days · **this is where the dev loop is won**
Four honestly-labelled lanes: `test-dev` (directory/impact-scoped + `acceptance_smoke`),
`test-pr`, `test-full` (serial, authoritative certification), `test-nightly` (csi, longhaul,
mutation, schemathesis, live). Build the duration store by parsing the JUnit XML this repo
**already writes on every run**, feed an LPT shard planner — ~200 lines, and it is why
Knapsack Pro is unnecessary.

**Exit:** `make test-dev` p50 ≤5 s; `make test-pr` p50 ≤60 s, over 5 harness runs.

### Phase 5 — Selection you can enforce, + verdict-cache go/no-go · 1–2 weeks
A shadowed selector saves exactly zero wall-clock, which is today's state. Invert the TIA
default to **`full`** on any unresolvable input (it currently returns `none` = *run nothing*
for repo-wide plugin changes, every `.sh`/`.yaml`/`.sql`, and the actual `HEAD~1` diff — a
false-negative machine). Snapshot the diff at run start. Cache the import graph (6.85 s → 0.1 s).
Prototype **coverage-based TIA** and shadow it against the AST TIA over ≥50 replayed commits.

Then — only if Phase 2's exit metric still holds — spike the **content-addressed verdict cache**
keyed on worktree merkle digest + runner/plugin/env/platform closure. Cache **PASS only**,
default-deny for non-hermetic tests, flake-taint + TTL, and a shadow audit that re-executes a
sample of cache HITs.

**Exit:** over ≥50 replayed commits, false-negative rate **exactly 0**, median selection <40 %.
```
python scripts/tia_shadow.py --commits 50 --assert-fn-rate 0 --assert-median-selection-lt 0.40
```

### Phase 6 — Generalize to any repo or prototype
Only after the above works once. The portable artifacts are: the **bench harness**, the
**template-fixture pattern**, **scoped lanes + duration store + LPT**, **coverage-TIA**, and the
**verdict cache**. Behind a `FrameworkAdapter` (`discover` / `impact` / `run_shard` / `parse`),
pytest is v1, jest/vitest v1.1, go test v1.2 (its build graph gives impact analysis nearly free).
Unknown frameworks → explicit `FULL`, never a guess.

---

## 5. Paid spend — the ladder, with trigger conditions

**Through Phase 3: $0.** Every cloud option researched solves a problem this repo does not have
yet, and three of the four research lanes are gated on artifacts that don't exist (`.github/` is
absent, there is no meaningful E2E suite, `run.py` was never written).

| When | Spend | Trigger |
|---|---|---|
| Phase 0–3 | **$0** | Revisit only if p50 wall after Phase 3 is still >150 s on a **quiesced** box |
| Phase 4, day you create `.github/workflows/` | GitHub Team **$4/user/mo** | 60 concurrent jobs, 3,000 min included |
| First CI workflow emitting JUnit | Trunk Flaky Tests **$0** | Bots excluded from the committer count, so an agent fleet is free. Binding limit is 5 M spans/mo ≈ 440 runs. **Requires a burn-down queue + expiry**, or quarantine converts flaky-red into silently-green |
| Before any cloud work | AWS quota increases — **$0 to file** | Takes hours-to-days and gates everything |
| Only if a lane needs >60 concurrent shards | Fargate ARM Spot + Batch **~$53–178/mo** | This suite has ~50 min cumulative. Realistically never, unless it grows 3x |
| Only once a real Playwright suite exists | Azure Playwright Workspaces **$0.01/test-min** | The only product that attacks per-shard *setup* rather than shard count |
| Only for real Safari/iOS hardware | TestingBot **$60 one-time**, credits never expire | Nightly only. Per-parallel subscriptions need a ~23 % duty cycle; a solo operator hits ~2 % |

**AWS account status (000000000000, verified today).** Profile `example-testing` configured and
working. Permissions: S3 full, Bedrock full, inline EC2 (RunInstances/Terminate/keypairs/SGs),
`DescribeImages`/`InstanceTypes`/`SpotPriceHistory`, default VPC present. **No Lambda, no
CodeBuild, no ECR.** Quotas: EC2 on-demand 64 vCPU, spot 256 vCPU, **Lambda concurrency 400 —
not the 1,000 every plan doc assumed.** Live spot: `c8g.16xlarge` (64 vCPU Graviton4)
**$0.564/hr**. Graviton is arm64, matching the M2 Ultra — architecture parity, so no
"passes locally, fails in CI" class of bug. When cloud becomes justified, this is the shape:
**EC2 spot self-hosted runner, not GitHub-hosted** (2-core hosted runners would take ~17 min
per run — 6x slower than the laptop).

**RunPod.** x86-only (their own CPU reference lists 400+ Intel/AMD parts and zero ARM), and CPU
pods are an undocumented side offering — their pricing page omits CPU pricing entirely. Wrong
tool for the pytest lane. **Right tool for the GPU lane you already run**: `RUNPOD_API_KEY` is
in the vault, `omniagentos/connectors/registry.py:453` defines the connector, and
`scripts/benchmarks/model_arena/` drives a Qwen pod. Nightly model evals and `live_ollama`
belong there.

---

## 6. What to abandon (abridged — 22 items in the full pack)

1. **"100x speedups" / "sub-10 second execution loops"** in `README.md`. Retract, don't soften.
2. **All six Gemini research docs.** Verified error inventory: Lambda per-execution cost 3x
   understated (a 1-second price labelled 3 seconds); S3 storage **1,150x** understated
   ($0.00002 vs $0.023/GB-mo) *and* applied per-run instead of per-GB-month; failure count off
   by exactly **1,000x**; BrowserStack's "$15,000/mo for 100 parallels" invented from a tier
   that does not exist for Automate; GitHub Actions runner-minutes multiplied by runner count,
   which is dimensionally wrong; `wal_level=minimal` without `max_wal_senders=0` means Postgres
   will not boot; **`/dev/shm` does not exist on macOS at all**; four referenced code artifacts
   exist nowhere on disk; docs backdated four months relative to their own git history.
   Move to `archive/` with a WRONG banner. **Do not let a budget be built from any of it.**
3. **The 1,000-way Lambda fan-out.** AWS scales 1,000 environments per 10 s (500/10 s burst) →
   10–20 s of ramp before the first browser starts; the plan's own SDK client defaults to
   `maxSockets:50`, serializing 1,000 invokes into 20 waves; this account's real quota is 400;
   and its own Dockerfile installs chromium *only*, contradicting the Firefox/WebKit claim that
   justified dropping BrowserStack. Realistic 60–150 s, not 3.5 s — **and there is no E2E suite
   to run on it.**
4. **`ram_db.py` / the `/dev/shm` pillar.** On darwin it silently rewrites to `/tmp` (APFS, on
   disk) and then logs "RAM-disk database workspace initialized". The claimed 19x is exactly 1x
   here. Delete it or make it fail loudly — silently-wrong is worse than missing.
5. **`parallel_test_hook.py`.** Zero callers repo-wide, lives in a package that prints "frozen
   and deprecated" on import, its regex rejects `HEAD~1` (the engine's own default ref),
   hardcodes `/Users/youruser`, bare `python3`, no timeout. Do not patch in place.
6. **AST import-graph TIA as the long-term mechanism.** 0-edge graph on its own src-layout repo;
   blind to 76 dynamic-import sites and 135 subprocess-using test files; selects a median 52 %
   when it does select. Coverage-based TIA replaces four of its required fixes with one.
7. **All CI-runner vendors** (Depot, Blacksmith, Namespace, …) — unpurchasable until CI exists.
8. **BrowserStack / Sauce / LambdaTest at 25–100 parallels** — no browser suite; and BrowserStack
   publishes no price above 5 parallels, so every 25p/100p figure in circulation is extrapolation.
9. **Knapsack Pro** ($10/committer, meters bots — hostile to an agent fleet; it is ~200 lines
   against JUnit XML you already emit). **CodeBuild reserved fleets** ($3,110/mo at ~6.5 %
   utilisation). **Antithesis / Diffblue / Meticulous / mabl / Testim / QA Wolf / Qodo Cover.**
10. Non-issues verified and closed: coverage is **already** absent from the lane (no `--cov`
    anywhere); `COVERAGE_CORE=sysmon` buys 0 % on 3.12 with `branch=true`; collection at
    1.05 s/1,000 tests is already healthy; `-p no:cacheprovider` **deletes** `--lf`/`--ff`;
    free-threaded 3.14 is irrelevant when xdist already uses subprocesses.

---

## 7. Decisions needed from the operator

1. **Does the engine repo survive?** Today: one unsafe AST TIA, three stubs, two empty PLANNED
   dirs, a missing keystone (`run.py`), and a consumer hook broken four ways inside a frozen
   package. The three highest-value fixes make the AST graph largely obsolete.
   **(a) delete it and build coverage-TIA inside OmniAgentOS — materially faster and
   simpler**, (b) rebuild it standalone around coverage-TIA, (c) archive it. Everything
   downstream is blocked on this.
2. **How do we get a quiet machine?** Load averaged 16.5/40.7/50.1 during analysis and wall
   times vary 3x as a direct result. Options: a quiesce protocol that pauses the fleet during
   benchmarks, a scheduled window, or a second machine. **Without one, Phase 0's exit metric is
   unreachable and every later gate is unverifiable.**
3. **Delete the frozen engines, or pay their tax?** `csi`, `reliability`, `longhaul` all print
   "frozen and deprecated" on import. `tests/csi` alone is ~200 s of cumulative and ~⅕ of the
   dev lane, and **86 % of its 6,652 git spawns come from product code, not the harness.**
   Optimizing deprecated code is the wrong trade; moving it to nightly hides two real failures;
   deleting the engines gets both wins free. Product call, not engineering.
4. **Should `make test` become parallel?** It is the authoritative certification signal.
   Recommendation: **keep it serial and authoritative**; make the dev/fast lanes the ones people
   type. Settle explicitly — one research lane ranked flipping it the "#1 same-day win".
5. **Is the verdict cache internal tooling or a product?** Keying on *dirty-worktree content*
   is genuinely novel — every incumbent keys on a git SHA because nobody else's customer is a
   fleet of agents on uncommitted worktrees. As internal tooling the ROI is defensible. As a
   business the category has four corpses (Earthly CI, Earthly Cloud, Toolchain, GCP RBE).
6. **`var/` holds 1,214,831 files.** Free today, but `tests/simharness/runner.py:242-246` already
   documents ~87 µs/file for a manifest walk — one new assertion that walks an operator root
   becomes a 105 s test that blows the 180 s timeout twice over. Prune, or knowingly accept?

---

## 8. Start here (today, ~2 hours, zero risk)

```bash
# 1. Prove the marker bug on your own box
uv run pytest -q --collect-only -m "not smoke" \
  --ignore=tests/simharness --ignore=tests/counterfeits --ignore=tests/longhaul \
  | tail -2                                   # 11,328 collected
uv run pytest -q --collect-only \
  --ignore=tests/simharness --ignore=tests/counterfeits --ignore=tests/longhaul \
  | tail -2                                   # 11,325 collected — 3 fewer, live tests excluded

# 2. Fix it (Makefile:41)
-m "not smoke and not (live_cli or perf or live_ollama or live or counterfeit_gate)"

# 3. Cap workers at the P-core count
-n 16                                          # never 24, never auto

# 4. Free ~0.9s off every lane
export PYTEST_DISABLE_PLUGIN_AUTOLOAD=1        # collection 4.32s -> 3.42s
```

Then build `make bench` before touching anything else — because right now this repo cannot
prove whether any change helped.
