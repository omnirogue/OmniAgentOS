# TESTING.md — Validation and Testing Guide

This guide defines the validation standards, testing ladders, and execution protocols for OmniAgentOS.

## The Validation Ladder

Before proposing any change, developers and agents must ascend the validation ladder sequentially:

1. **Unit Testing**: Run `make test` to execute all unit tests.
2. **Quality Checks**: Run `make lint` (using `ruff`) and `make type` (using `mypy`) to verify structural code quality.
3. **Comprehensive Release Validation**: Run `make validate`. This execution command runs:
   - Full linting and formatting verification.
   - Comprehensive type analysis.
   - The entire pytest suite with `OMNIAGENTOS_REQUIRE_PG=1` enabled (requires a local PostgreSQL instance running for pgvector/Synapse testing).
   - Dashboard Next.js build (`make build-dash`).
   - Playwright end-to-end suite (`make e2e`).

## Targeted Testing

Run tests on specific modules rapidly using:
```bash
.venv/bin/pytest -q tests/<area>
```
For example:
- `.venv/bin/pytest -q tests/swarm/` — Run all swarm-coordinator, worktree, and spawner tests.
- `.venv/bin/pytest -q tests/metacog/` — Run all metacognition and memory evaluation tests.

## Fast Parallel Lane (exploratory — not certification)

`make test-fast` runs the non-smoke suite in parallel (`pytest-xdist`, **16 workers**,
`worksteal` scheduling for the long tail), excluding the quarantined dirs
(`tests/simharness`, `tests/counterfeits`, `tests/longhaul`; `make test` still covers them
serially), with duration instrumentation and a JUnit report under `var/test-reports/`.

### Baseline figures — corrected 2026-07-31 (read this before quoting a number)

The previously documented figures here (**"99 min serial-equivalent"**, **"7.3x, 91% scaling
efficiency"**, **"quarantined dirs = 75% of total suite time"**) were all derived from
`var/test-reports/fast-lane-baseline.xml`, and that run was broken:

- **22 tests each burned the full 180 s `pytest-timeout`**, contributing **3,960.7 s of its
  5,945.8 s cumulative = 66.6%**. That is not test work, it is 22 hangs. The real
  serial-equivalent is ≈ **33 min, not 99**.
- The 7.3x / 91% figures are ratios computed against that inflated denominator.
- The three quarantined directories measure ≈ **105 s total**, not 75% of anything. A
  ~33-second suite is currently quarantined on a false premise; revisit it.

**This suite does not have a duration — it has a distribution.** Four artifacts from the same
box on the same day: wall **271.7–814.5 s (3.0x)**, cumulative **1,388–3,034 s (2.2x)**,
failures **22–66 (3.0x)**, driven by machine load (the agent fleet and the suite share the same
cores). Never quote a single wall-clock number as evidence. Use `make bench-lane`, which
repeats the lane, **refuses to start on a contended box**, and reports cumulative seconds and
`eff_par` alongside wall clock.

The model that predicts wall clock (verified to within 3%): under xdist every worker collects
the whole tree independently, so collection is a fixed cost paid in parallel, not a serial
fraction:

    wall = COLLECT + max( cumulative / eff_par , longest_single_test )

Consequence: **a ≤15 s dev loop is unreachable for any full-tree lane at any worker count**,
because full-tree collection alone is ~12 s. That target requires lane *shape*
(directory-scoped collection floors at ~0.6 s), not lane speed.

**Worker count is capped at 16**, the P-core count on this box (16 performance + 8 efficiency
cores). Workers 17–24 land on E-cores at roughly a third the speed and measurably *raise*
cumulative time (`-n 16` cum 2,430.8 s vs `-n 24` cum 3,149.8 s over the same tree). `-n auto`
is separately documented below to deadlock the suite.

Honesty rules for this lane:
- **It is not certification.** `make test` (serial) remains the authoritative gate; SHA-bound
  evidence stays with `scripts/ladder-record.sh` receipts.
- **Parallel-only failures are isolation bugs in the test** (shared state, ordering, fixed
  ports, shared SQLite/var paths) — fix the test, never paper over it. Known: 24-worker
  `-n auto` deadlocks the suite (process-spawning tests). First burn-down done 2026-07-31:
  `test_orch_resume` fixed with a deterministic Event signal, and `tests/conftest.py` now
  pins both resume flags off session-wide (kills the leaked-daemon warm-cache class).
  Remaining lane red (25) is a MIX of parallel-unsafe singles and pre-existing branch
  failures (e.g. AGENTS.md migration-pointer drift after 092→096) — A/B each serially
  (`git stash` the change, rerun) before deciding quarantine vs fix; the current list
  lives in `var/test-reports/fast-lane-latest.xml`.
- **The lane's marker expression must stay composed, never bare.** pytest's `-m` is
  store-not-append: a bare `-m "not smoke"` on the command line *replaces* the expression in
  `pyproject.toml:98` instead of adding to it. That silently readmitted 9 live/perf tests
  (202.0 s = 6.66% of cumulative, including `test_recall_10k_p95` at 158.3 s — the run's
  longest single test, i.e. the critical-path floor) and fired live
  Jira/OpenHands/Anthropic/Ollama calls on every dev run. See the `FAST_LANE_MARKERS` comment
  in the Makefile. Regression check:
  `pytest --collect-only -m "$(FAST_LANE_MARKERS) and (live_cli or perf or live_ollama or live or counterfeit_gate)"`
  must collect **0** tests.
- **The revert-test harness is not crash-safe.** `tests/doctrine/revert.py` mutates real
  product files via a `mutated_file` context manager — exception-safe, but a *killed* process
  skips `__exit__` and leaves sabotaged product code in the working tree (observed 2026-07-31:
  `context/capsule.py` left returning a constant digest, `memlife/store.py` left swallowing an
  exception). Always `git status` after an interrupted run. Fix is to mutate a throwaway
  worktree, never the live checkout.
- Phase 4 built the four-lane model this section used to describe as a target
  (`test-dev` / `test-pr` / `test-full` / `test-nightly`) — see "Lane Architecture" below for
  what each one actually runs, and its measured cost **per change set** (a lane scoped by
  impact has no single p50; `test-dev` does not meet its ≤5 s target on this box and the
  measurements say so).

## Lane Architecture (Phase 4)

`make test-fast` above answers "did I break the fast-lane-eligible suite". It cannot answer
"did I break the thing I just touched" without paying the full-tree collection floor (~4 s
warm, see the recon below) on every run. The four lanes below are lane *shape*: each is
scoped to a different amount of the suite, and none except `test-full` is certification.

| Lane | Scope | Budget | Composition |
|---|---|---|---|
| `test-dev` | pre-commit, every save | target p50 ≤5 s (**not met — see the measurements**) | impacted tests (`-n 8`) + serial bucket + `acceptance_smoke` (`-n 4`) |
| `test-pr` | before opening/updating a PR | target p50 ≤60 s (**met only for narrow diffs**) | impacted tests (`-n 16`) + serial bucket + `acceptance_smoke or acceptance_daily` (`-n 8`) |
| `test-full` | merge gate | authoritative | identical to `make test` (unchanged) — serial, everything `pyproject.toml` addopts allows |
| `test-nightly` | scheduled, not per-commit | — | `tests/csi` + `tests/longhaul` + `make simharness` + `make test-live` |

`test-nightly`'s stated Phase 4 composition also names **mutation testing**. No mutation
harness exists anywhere in this repo (checked: no `mutmut`/`cosmic-ray` config, no prior art
to build on) — building one is separate work, not a lane-shape question, so it is **not faked
into this Makefile target**.

**A lane's cost is a function of what changed, not a constant.** Any single "test-dev p50"
with no stated change set measures nothing. Every number below names the exact change set it
was measured against, and `--changed` / `LANE_CHANGED` exist so anyone can rerun it.

### Collection-floor recon (why the fast lanes scope by file, not by marker)

Warm full-tree collection (same markers/ignores as `test-fast`, 11,530 tests collected, 15
deselected) measured 2026-07-31: 3.21–4.28 s reported / 4.02–4.28 s wall across repeated runs
(first/cold run 7.98 s reported / 9.09 s wall). A single directory collects far below that
floor: `tests/scope` (301 tests) 0.13–0.36 s reported / 0.50–0.65 s wall, `tests/csi` (184
tests) 0.14 s reported, `tests/swarm` (1,043 tests) 1.01 s reported. The floor is dominated by
interpreter/plugin startup (~0.35 s) plus ~3.5 s of marginal full-tree collection cost that
scales with test count. It is a **hard floor for any lane that collects the whole tree**,
which is why `test-dev`/`test-pr` pass explicit PATHS to pytest and never a bare `-m` (a bare
`-m acceptance_smoke` measured spending ~2.5 s of its own run collecting `tests/acceptance`'s
637 items before deselecting 619 of them).

### The impact analysis (`scripts/testlanes/impacted.py`)

This is a **reverse-dependency analysis**, not a directory-name heuristic. The first version
of this module mapped `omniagentos/<pkg>/**` to `tests/<pkg>/` and nothing else; review
rejected it, correctly — `omniagentos/db/store.py` mapped only to `tests/db` while 131 test
files outside `tests/db` import `SqliteStore`. A naming convention is not an impact analysis.

Four edge sources are **unioned**, so a test is selected if any of them can explain why the
change might reach it:

1. **Transitive import closure.** Every `.py` under `omniagentos/`, `scripts/`, `tests/` and
   `tools/` is parsed with `ast`; relative imports are resolved against the file's own
   package, `from pkg import name` also counts as `pkg.name`, and every package `__init__.py`
   on an import path counts (importing `a.b.c` executes them). Edges are inverted and walked
   breadth-first.
2. **Dynamic/textual module references.** Quoted dotted names inside string literals
   (`importlib.import_module("omniagentos.foo")`, patch targets, `python -m` arguments) feed
   the same closure — the blind spot `docs/testing/TESTING_SPEED_PLAN.md` Phase 5 named when
   it rejected a pure AST graph on this repo.
3. **Data-file references.** Tests read non-Python files. Path-like tokens found in string
   literals are indexed, so a changed `Makefile` selects the tests that actually read it.
   Docstrings are **excluded** from this scan: one sentence of prose in
   `omniagentos/api/eventbus.py` ("see the Makefile") otherwise made a `Makefile` edit select
   380 of 808 test files through eventbus's importers. Basename-only matches are accepted only
   for repo-unique basenames, so a change to one of this repo's 31 `store.py` files does not
   select every test that mentions the string.
4. **Directory mirror**, kept as an additive fourth source for CLI/subprocess tests that
   exercise a package without importing it.

The index is cached at `var/testlanes/impact-index.json`, keyed per file on `(size, mtime_ns)`
and globally on a **hash of the extractor's own source**. That global key is not cosmetic: the
extractor was changed twice during this work while the cache version was a hand-maintained
integer, and stale entries silently answered with the old edge set — two measurements of the
same repo disagreed by 88 test files. A cache must not be able to outlive the code that wrote
it. Pass `--no-cache` to distrust it entirely.

```
uv run python -m scripts.testlanes.impacted --format json
uv run python -m scripts.testlanes.impacted --changed omniagentos/db/store.py   # what-if
```

### When the analysis cannot do its normal job

Two different conditions, reported as two different flags, because lanes must treat them
differently:

| Flag | Meaning | `test-dev` | `test-pr` |
|---|---|---|---|
| `unresolved_input` | a changed file has **no explainable test edge** — the subset provably does not cover the diff | loud banner, continues (a pre-commit loop is not a gate) | **escalates** to the whole fast-lane tree |
| `closure_too_broad` | the analysis is fine and ≥60 % of the suite is impacted; handing pytest hundreds of explicit paths is not cheaper than collecting the tree | **escalates** | **escalates** |

Escalation runs `tests` under `FAST_LANE_MARKERS` at the lane's worker count, `--ignore`-ing
the quarantined suites *and* `tests/doctrine`, then runs `tests/doctrine` in a separate serial
step. Escalating can therefore never reintroduce the xdist race below.

Documentation/asset files (`.md`, `.txt`, images) that no test reads are reported as
`no_test_edge` and do **not** force escalation — a file no test reads cannot change a test
outcome. That is an explicit allowlist, not a default: `AGENTS.md`, `ARCHI.md` and `TESTING.md`
*are* read by tests and by `omniagentos/swarm/scheduler.py`, and edge source 3 picks that up.

### `tests/doctrine` runs SERIALLY, always

`tests/doctrine`'s revert/counterfeit harness mutates real files in place (see "The revert-test
harness is not crash-safe" above). Confirmed while building this: `pytest -n 8 tests/doctrine`
produced 7 cross-worker mutation races and left `tests/doctrine/_fixtures/subject.py` dirty in
the working tree. The first version of this lane runner *selected* `tests/doctrine` and then
handed it to `-n 8`, which is worse than not selecting it at all — it corrupts the checkout.

`impacted.partition()` now splits the selection into three buckets, and the runner gives the
serial bucket its own pytest subprocess with **no `-n` at any worker count**:

* `impacted` — parallel-safe, run under `-n`;
* `serial` — `impacted.SERIAL_TEST_PREFIXES` (`tests/doctrine`), run serially;
* `deferred` — the quarantined suites `make test-fast` also ignores (`tests/simharness`,
  `tests/counterfeits`, `tests/longhaul`), reported by name and left to
  `make test-full` / `make test-nightly` rather than silently dropped.

Evidence: three benchmarked `make test-dev LANE_CHANGED="--changed tests/doctrine/revert.py"`
runs (below) left `git status --porcelain --untracked-files=no` **empty**.

### Exit codes are load-bearing

A lane that reports success because a subprocess died is worse than no lane. Python reports a
signalled child as a **negative** returncode, so the first version's `max(exit_codes)`
evaluated `max([-9, 0]) == 0` — **a SIGKILLed run reported SUCCESS** — and `merge_junit`
silently skipped the missing report part, so the merged artifact looked plausible too.

`run_lane.step_exit_code()` now holds this contract (`tests/scripts/test_lane_exit_codes.py`):

* a signalled step becomes `128 + N` (SIGKILL → 137), never a negative that `max()` ignores;
* a step that exits 0 without a **parseable** JUnit part fails with 70 (`EX_SOFTWARE`) — if we
  cannot read what it ran, we do not get to call it green;
* the always-run critical step additionally fails if its report contains **zero** testcases;
* pytest's exit 5 (everything deselected) is an empty selection, not a failure — but only if a
  report exists;
* `merge_junit()` **returns** the parts it could not read, and any non-empty result fails the
  lane;
* a lane with no steps at all fails.

(Found while writing those tests: the status was originally computed *after* the
`TemporaryDirectory` holding the JUnit parts had closed, so every real run read its own parts
as missing. Everything that reads a part now happens inside that block.)

### Every marker expression is composed, never bare

pytest's `-m` is store-not-append: a command-line `-m` **replaces** `pyproject.toml`'s
`addopts`. The first version of this runner passed its critical step a bare
`-m acceptance_smoke`, re-admitting `live`/`perf`/`counterfeit_gate` one layer below the very
bug the lane architecture exists to prevent. Every step now goes through
`run_lane.compose_markers()`, which emits `(FAST_LANE_MARKERS) and (<lane marker>)`.

`tests/scripts/test_lane_marker_superset.py` proves (1) the Makefile's `FAST_LANE_MARKERS` and
the runner's constant are byte-identical, (2) brute-forced over the whole marker universe,
that **every expression a lane actually passes to pytest** — not just the constant — excludes
everything `addopts` excludes, and (3) that composition still selects what was asked for.
Marker counts as of 2026-07-31: `acceptance_smoke` = **18** tests, `acceptance_smoke or
acceptance_daily` = **71** (the previous revision of this file said "18–56"; 56 was wrong).

### The duration store + LPT shard planner (`scripts/testlanes/duration_store.py`)

This repo already writes JUnit XML with per-test durations on every lane run
(`var/test-reports/*.xml`); nothing read it back before Phase 4. `duration_store.py` folds
every testcase's `<time>` into a gitignored JSON map at
`var/test-reports/duration-store.json` (EWMA plus last-seen value and sample count — a single
last-seen number is not honest given this suite's up-to-3× run-to-run variance) and uses it to
plan longest-processing-time-first shards. Unknown tests are costed at the **median** known
duration, never zero. `known_fraction()` reports how much of a plan is measured rather than
guessed, and returns **`None` for an empty request** — a rate over an empty set is undefined,
not 100 %.

```
uv run python -m scripts.testlanes.duration_store update
uv run python -m scripts.testlanes.duration_store stats --top 25
uv run python -m scripts.testlanes.duration_store plan --shards 4 --tests node_ids.txt
```

### What each change set actually selects

Measured at `9db6d2c6` against 811 test files. Reproduce any row with
`uv run python -m scripts.testlanes.impacted --changed <path> --format json`.

| Change set | Selected / 811 | parallel | serial | deferred | Lane behaviour |
|---|---|---|---|---|---|
| `omniagentos/scope/policy.py` | 8 | 8 | 0 | 0 | normal |
| `Makefile` | 6 | 6 | 0 | 0 | normal |
| `tests/scripts/test_lane_marker_superset.py` | 14 | 14 | 0 | 0 | normal |
| `tests/doctrine/revert.py` | 11 | 5 | **6** | 0 | normal + a SERIAL step |
| `omniagentos/db/store.py` | 628 | 616 | 0 | 12 | `closure_too_broad` → **escalates** |
| `pyproject.toml` | 495 | 483 | 0 | 12 | `closure_too_broad` → **escalates** |
| this branch's own diff | 496 | 484 | 0 | 12 | `closure_too_broad` → **escalates** |

Two of those deserve comment. **`Makefile` selects 6 files, not 380** — that is the docstring
exclusion in edge source 3 working. **`omniagentos/db/store.py` selects 628 of 811** files
(616 parallel, 12 deferred) — the old directory mirror selected 18. That is not the analysis
being sloppy; it is this repo's real coupling, and the lane escalates rather than pretend a
subset covers it. The same is true of `pyproject.toml` and of this branch's own diff.

### Measured lane cost (`var/test-reports/bench-lane-*.json`)

All rows: 3 runs, `--max-load 40`, pinned to `9db6d2c6`, on a box carrying an agent fleet at
1-min load 20–48. Reproduce with the command in the first column.

| Lane / change set | p50 wall | p95 wall | cumulative | eff_par | items run | failures |
|---|---|---|---|---|---|---|
| `test-dev` — `--changed omniagentos/scope/policy.py` (8 files) | **28.0 s** | 33.1 s | 61.2 s | 2.16 | 352 | 0 |
| `test-dev` — `--changed tests/scripts/test_lane_marker_superset.py` (14 files) | **25.6 s** | 30.3 s | 88.9 s | 3.48 | 142 | 0 |
| `test-dev` — `--changed tests/doctrine/revert.py` (5 parallel + 6 **serial**) | **50.8 s** | 53.6 s | 87.2 s | 1.77 | 104 | 0 |
| `test-dev` — `--changed omniagentos/db/store.py` → **escalated tree** | **275.8 s** | 349.3 s | 1,573.2 s | 5.70 | 11,510 | 3 |
| `test-pr` — `--changed omniagentos/scope/policy.py` (8 files) | **27.3 s** | 29.0 s | 78.2 s | 2.86 | 405 | 0 |
| `test-pr` — this branch's own diff → **escalated tree** | **209.2 s** | 213.4 s | 1,957.5 s | 10.08 | 11,510 | 3–4 |

```
uv run python scripts/bench_lane.py --runs 3 --max-load 40 \
  --cmd 'make test-dev LANE_CHANGED="--changed omniagentos/scope/policy.py"' \
  --tag lane-dev-leaf-source --junit var/test-reports/test-dev-latest.xml
```

**`test-dev` does not meet its ≤5 s target, in any scenario, on this box.** The floor alone
rules it out: the critical `acceptance_smoke` step by itself, with nothing impacted, measured
**4.39/4.48/4.82 s wall** (3.3 s inside pytest) over three consecutive runs —

```
uv run pytest -q tests/acceptance \
  -m "(not (smoke or live_cli or perf or live_ollama or live or counterfeit_gate)) and (acceptance_smoke)" \
  --timeout=60 -n 4 --dist worksteal
```

— so ≤5 s leaves roughly 0.2 s for everything else, including `make`, `uv run` (~1 s) and the
impact analysis. Warm, the analysis costs **0.13 s**; **cold it costs 6.1 s** to parse 2,024
files, which alone exceeds the target on the first run in a fresh checkout. The honest
statement is that the ≤5 s target is unreachable while the critical set costs 4.4 s, and this
work did not touch `tests/acceptance/` to change that (out of Phase 4 scope — see Phase 3 in
`docs/testing/TESTING_SPEED_PLAN.md`).

**`test-pr` meets its ≤60 s target for narrow diffs and misses it badly when it escalates.**
Both escalated rows above are the whole fast-lane tree plus serial doctrine; that is the real
price of a diff whose closure covers most of the suite, and it is published rather than hidden
by pretending a subset was sufficient.

**Wall clock on this box is not evidence on its own.** The same
`make test-dev LANE_CHANGED="--changed omniagentos/scope/policy.py"` command, benchmarked
earlier on this branch at `9855b9b7` (6 selected files rather than 8 — this branch's own new
tests widened it), produced p50 **9.0 s** / cumulative 25.2 s / longest-test **1.5 s**, against
28.0 s / 61.2 s / 23.7 s in the table. Two runs of nearly the same work, ~3× apart, driven
almost entirely by one acceptance test's variance under fleet load. Prefer the
cumulative-seconds and items-run columns; treat the p50 column as a range, not a number. All
rows were captured with `--max-load 40` (this box's agent fleet keeps 1-min load in the 20–48
range); rerun with the default `--max-load 4.0` on a quiesced machine for a
certification-grade figure. This is the same wall-vs-cumulative trap `bench_lane.py`'s
docstring exists to guard against — pin the worker count and read cumulative seconds.

**The 3–4 failures in the escalated rows are pre-existing, not lane failures.** They are
`tests/harnesses/test_fast_lane_contract.py::test_fast_lane_preserves_every_default_opt_in_exclusion`
and `::test_fast_lane_keeps_parallel_and_directory_quarantine_contract`, which expect literal
values where the Makefile has used `$(FAST_LANE_MARKERS)`/`$(FAST_LANE_WORKERS)` since before
this branch (verify: `git show 151ffb28:Makefile | grep FAST_LANE_WORKERS`), plus
`tests/archdocs/test_launcher_hygiene.py::test_archi_stamp_gate_and_stale_route_mutation`,
which is the ARCHI stamp staleness gate firing because HEAD moved (fixed by `/archi update`,
which only archdocs may run). None of the three is in this branch's ownership.

**`test-full` was not benched.** It is byte-identical to `make test` (`test-full: test` in the
Makefile — no new behaviour), and a serial `make test` on this box ran for over 45 minutes
without finishing under the same load that made the numbers above noisy. If a
certification-grade `test-full` p50 is needed, run `make bench-lane BENCH_LANE=test-full
RUNS=3` on a quiesced box and record it here.

**`test-nightly`'s `live`/`live_ollama` component (`make test-live`) was not exercised.** It
calls real external services (Jira, OpenHands, Anthropic, Ollama) and firing it repeatedly for
a timing benchmark is not something to do without the operator asking. `make test-nightly`
still wires it in; only the benchmarks skip it.

## Hermetic Lane (opt-in — TestFarm socket guard)

`make test-hermetic` runs the same default network-free selection as `make test` (serial,
`pyproject.toml` addopts markers apply) with the TestFarm harness plugin active. That turns
the marker *convention* ("only `live`/`live_ollama`/`live_cli` tests touch the network") into
socket-level *enforcement* on a precisely bounded surface. **Exactly these APIs are
patched:** `socket.socket.connect`, `socket.socket.connect_ex`, `socket.socket.sendto`,
`socket.socket.sendmsg` (where the platform has it), and `socket.getaddrinfo`. A blocked
call raises `NetworkBlockedError` naming the offending test. Loopback (`127.0.0.0/8`,
`::1`, `localhost`), numeric-literal resolution, and unix sockets stay allowed, so
TestClient/local-port/guard-DSN tests are unaffected. **Known in-Python gaps (on top of the
native/subprocess limit below):** the legacy resolver calls `socket.gethostbyname`,
`socket.gethostbyname_ex`, and `socket.gethostbyaddr` are NOT patched; raw `_socket.socket`
objects that bypass the `socket`-module subclass are NOT patched; custom resolver
implementations that do not go through `socket.getaddrinfo` are NOT covered. The mainstream
clients this suite uses (stdlib `urllib`/`http.client`, httpx, urllib3/requests, asyncio's
default event loops) resolve via `getaddrinfo` and connect through `socket.socket`, so they
are covered — but "covered" means those code paths, not "any DNS lookup" or "every possible
Python path".

**Strictly opt-in — zero impact when not invoked.** The testfarm plugin activates via a
`pytest11` entry point the moment the package is installed (there is no
dormant-while-installed mode), so the lane owns a separate venv: `make test-hermetic` runs
`uv sync --locked --all-extras` into `.venv-hermetic` (gitignored) plus an editable install
of testfarm from `TESTFARM_SRC` (default `/Users/youruser/testfarm`) into that venv only.
testfarm is never installed into `.venv`; `make test` / `make test-fast` are byte-identical
whether or not this lane has ever run. That isolation is enforced, not assumed:
`HERMETIC_VENV` is `override`-pinned in the Makefile (`make test-hermetic
HERMETIC_VENV=.venv` dies at parse time), `scripts/hermetic-venv-guard.sh` refuses to
install through a symlinked or non-directory `.venv-hermetic` (so it cannot be redirected
into `.venv`), `.gitignore` carries the bare `.venv-hermetic` pattern (covers a stray
symlink, which a trailing-slash pattern would not), and `scripts/merge-gate.sh` refuses
both tracked `.venv*` paths and a checkout whose `.venv-hermetic` is a symlink
(`hermetic-venv-shape`). `tests/hermetic_lane/` pins all of this in executable form.

**Idempotency, stated precisely:** `uv sync --locked` never rewrites `uv.lock` (a stale
lock fails the run instead) and prunes anything outside the lockfile — including a
previously installed testfarm — then the editable install restores it. Rerunning therefore
converges to *lockfile state plus the current `TESTFARM_SRC` checkout*: testfarm and its own
dependencies (pytest-asyncio, pytest-recording, vcrpy) are resolved at install time and are
NOT pinned by this repo's lock. Every run prints the testfarm commit hash so the enforcing
harness version is recorded. Venv preparation is serialized through a
`.venv-hermetic.preparing` lockdir, so two concurrent invocations cannot interleave
sync/install (the second refuses with a clear message).

```bash
make test-hermetic                                               # full default suite
make test-hermetic HERMETIC_PATHS="tests/scheduler tests/scope"  # scoped subset
make test-hermetic TESTFARM_SRC=/path/to/testfarm                # non-default checkout
```

`TESTFARM_HERMETIC=1` (exported by the target) is a fail-loud handshake, not the activation
switch: when the flag is set, `tests/conftest.py::_require_testfarm_guard` refuses to start
unless the `testfarm_harness` plugin is importable AND registered, so the lane can never
silently degrade to an unguarded green run. When the flag is unset, that wiring returns
before importing testfarm — no hard dependency anywhere in the repo, and any future
compatibility wiring (e.g. composing a repo `vcr_config` from the plugin's
`testfarm_vcr_config`) must sit behind the same double guard (flag AND try/except import).
The active mode is identified three ways, none of which `-q` can suppress: a direct-stderr
banner (`testfarm hermetic lane: network ...`) printed at configure time, a
`testfarm_guard_mode` testsuite property (`blocked` | `allow-network`) in the JUnit report,
and the plugin's own report header in non-quiet runs. The flag is per-RUN, not inheritable:
a test that spawns another pytest entry point as a subprocess against the DEFAULT venv
(e.g. `tests/scripts/test_conftest_db_isolation.py` probing `make test`/bare pytest) must
strip `TESTFARM_HERMETIC` from the child environment — the child's venv rightly has no
testfarm, and an inherited flag would turn the probe into a handshake refusal.

Escape hatches (all deliberate, all visible):
- `@pytest.mark.live` disables the guard for that one test. `live` is already this repo's
  "requires external services" marker and is excluded from the default selection by addopts,
  so inside this lane it is only ever reached via an explicit `-m` opt-in.
- `--testfarm-allow-network` disables the guard for a whole run. Inside the hermetic lane
  (flag set) that is a consequential downgrade, so it requires a second explicit
  acknowledgement: without `TESTFARM_HERMETIC_ALLOW_NETWORK_ACK=1` the session refuses to
  start. With the ack, the stderr banner and the JUnit `testfarm_guard_mode=allow-network`
  property make the unguarded run distinguishable from a guarded one at a glance and in
  archived reports.

**Known contract limit (read before trusting a green run as "no network happened"):** the
guard is a Python-socket tripwire, not a sandbox. Beyond the in-Python gaps listed above
(`gethostbyname*`, raw `_socket.socket`, non-`getaddrinfo` resolvers), it does NOT cover
native extensions that connect in C — libpq via `psycopg[binary]`, i.e. every Postgres
connection this suite makes — or child processes (the `smoke` tests spawn real ones). A
hard hermetic guarantee for those needs an OS-level boundary (`docker run --network=none`,
a network namespace, or a firewall rule). This lane is an enforcement upgrade over the
marker convention, not certification of total network silence; `make test` remains the
authoritative gate.

**Durable proof:** `tests/hermetic_lane/test_hermetic_lane.py` runs in BOTH lanes and pins
the handshake (flag-unset fast-return, missing-plugin refusal, disabled-plugin refusal,
allow-network ack refusal, `-q`-proof banner), the isolation boundary (venv-guard script
refusals for `.venv`/symlink/non-directory, Makefile parse-time refusal of
`HERMETIC_VENV` overrides), and — hermetic lane only — live enforcement (DNS + TCP probes
raise `NetworkBlockedError`; the `live` frame disables blocking and restores it).

Scoped baseline (2026-08-01, contended box — same wall-clock caveats as every number in this
file): `make test-hermetic HERMETIC_PATHS="tests/scheduler tests/scope tests/notifications
tests/hermetic_lane tests/scripts"` → **756 passed, 3 skipped in 107.47 s**, zero guard
violations — those directories honour the network-free convention at the socket level
(JUnit report at
`var/test-reports/test-hermetic-latest.xml`, `testfarm_guard_mode="blocked"` recorded as a
testsuite property). Enforcement is no longer proven by a throwaway probe: the committed
`tests/hermetic_lane/` suite raises `NetworkBlockedError` on DNS + TCP probes inside the
lane on every run, and the same suite in the default `.venv` (no testfarm installed,
enforcement probes skipped, no testfarm header) passes with the plugin verifiably absent —
the zero-impact claim is observed on every run, not assumed. The full-suite
`make test-hermetic` (no `HERMETIC_PATHS`) has NOT yet been run end-to-end; do that once
before treating the lane's full-suite green as established.

## Memory Certification (memcert v2 — deterministic in the default lane)

The memory system is certified by memcert (devtasks/memcert/DESIGN.md + DESIGN-v2.md)
at three depths, two of which run with ZERO model calls in the default `make test`
selection:

1. **Retrieval sufficiency** (`tests/memcert/test_sufficiency.py`,
   `make memcert-sufficiency`): regenerates the fixture worlds from fixed seeds and
   grades the CONTEXT each memory arm builds against the generator's gold evidence
   (normalized containment; AutoRAG-style component-level evaluation). The merge-lane
   contract: the hybrid production stack (`system`) must dominate the v1 assembler
   (`system_legacy`) on EVERY axis and strictly beat it on the measured gap axes
   (B multi-session, G lesson retrievability, H action grounding), with axis D
   (knowledge updates) pinned at 1.0. Floors live in
   `configs/memcert/sufficiency-bars.yaml` (ratchet-only).
2. **Capacity + retention carriers** (`tests/memcert/test_capacity.py`,
   `tests/memcert/test_retention.py`; `make memcert-capacity`,
   `make memcert-retention PREV=… CURR=…`): MEM-I sufficiency-vs-scale curve (S/M/L)
   and MEM-J paired run-over-run regression detection (grade.paired_delta — never a
   comparison of two aggregates).
3. **Live axis bars** (`tests/memcert/test_live_bench.py`, `live`-marked, excluded
   from default addopts; `make memcert-live`): real cheap-model scoring against
   `configs/memcert/bars.yaml` on the cadence, not per-merge.

The deterministic layers are necessary-not-sufficient by design: they isolate the
retrieval component (a model can still fumble present evidence), which is what lets
them be exact, fast (~15 s pooled), and network-free. Counterfeit pairs
(`cf-memhybrid-flag-dark`, `cf-memcert-sufficiency-always-present`) prove the
certification fails when the hybrid master switch dies or the containment check is
neutered.

## Feature-Health Lane (background, non-blocking)

Per-feature tiered health signal (tier1 mechanical / tier2 live cheap-LLM / tier3 UI+API)
with a machine-readable ledger at `var/feature-health/ledger-YYYYMM.jsonl` that any CLI can
read. It never gates merges; the `feature_health` marker is excluded from default addopts
and FAST_LANE_MARKERS. Run `scripts/feature_health/run.sh tier1` or read
`scripts/feature_health/fh.py summary`. Full contract: `docs/testing/FEATURE-HEALTH.md`;
matrix: `configs/feature-health.yaml`; known-defect registry: `docs/testing/KNOWN-ISSUES.yaml`.

## Test Markers

Standard markers defined in `pyproject.toml` allow filtering runs:
- **smoke**: Fast end-to-end integration checks. Run with `pytest -m smoke`.
- **perf**: Benchmark tests measuring throughput and latency. Run with `pytest -m perf`.
- **live**: Tests that hit real external LLM API endpoints. Run with `pytest -m live`.

## Test Coverage Map

The complete system test matrix and mapping of specs to files can be found in:
- `docs/TEST-COVERAGE-MATRIX.md`

## The Swarm Verification Rule

Every swarm task is a formal contract. **Every swarm worker must execute and pass the `verify_command` specified in its `TASK.md` contract before declaring its attempt or task completed.**
