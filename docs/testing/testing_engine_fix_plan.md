# Testing Engine Readiness Review & Fix Plan

**Date:** 2026-07-31
**Scope reviewed:** `omniagentos-testing-engine` (all files) + its consumer `OmniAgentOS/omniagentos/reliability/parallel_test_hook.py`
**Verdict:** ❌ **NOT ready for integration.** The repo is research docs plus three reference stubs (the README itself labels `src/` as "Reference implementation stubs (to be built)"). The entry point the OS hook invokes does not exist, and the one substantive algorithm that is implemented (TIA) has confirmed correctness and performance bugs that make it unsafe on the actual target repo.

This document lists the confirmed findings (with reproductions) and a phased plan to make the engine genuinely integration-ready.

---

## 1. Findings

### F0 — FATAL: the integration entry point does not exist
`parallel_test_hook.py:77` in OmniAgentOS executes:

```
python3 <engine>/src/orchestrator/run.py --repo_path … --test_framework … --max_parallelism … [--git_diff_ref …]
```

**`src/orchestrator/run.py` does not exist.** Every invocation will fail with `CalledProcessError` → `{"status": "error"}`. There is also:

- **No test-execution layer at all.** Nothing in the repo runs a single test. TIA selects files, the scheduler bins them — nothing executes shards, collects results, or aggregates.
- **No JSON report generator** for the `specs/ARCHI.json` `output_schema` the hook parses from stdout.
- **No historical-runtime store.** `LPTScheduler` accepts `historical_times`, but nothing anywhere records durations, so every spec gets the 10.0 s default and LPT degenerates to card-dealing.
- `src/lambda/` and `src/telemetry/` are **empty directories**, yet `specs/ARCHI.json` lists `src/lambda/runner.js` and `src/telemetry/otel.py` as modules and the README shows `src/lambda/` in the repo tree. Docs claim files that don't exist.

### F1 — CRITICAL (confirmed by repro): TIA silently skips affected tests (false negatives)
`tia.py` records `from pkg import module` as a dependency on `pkg.py` (it only reads `node.module`), and then matches changed files against graph keys by **substring** (`registered_source in file or file in registered_source`, `tia.py:85`). The edge for `pkg/module.py` is never created and the substring test can't recover it.

**Reproduction** (scripted; keep as a regression test):

```
repro/
  pkg/__init__.py
  pkg/module.py        # changed
  test_pkg.py          # from pkg import module
```

Change `pkg/module.py`, run `tia.py repro HEAD` → **"Found 0 affected test files"**. The one test that directly covers the change is skipped; a caller would report green on a potentially broken change.

**Impact on the real target:** OmniAgentOS contains **1,671** `from omniagentos.* import …` statements, many importing submodules (`from omniagentos.runner import sandbox`, `from omniagentos.adapters import common`, …). TIA's graph is systematically wrong for the exact codebase it is meant to serve. For a test-selection engine, false negatives are the worst possible failure mode.

### F2 — CRITICAL (confirmed by measurement): TIA is unusably slow on the target repo
`build_import_graph()` walks everything except `.git/node_modules/__pycache__/.venv`. OmniAgentOS has **~238,000 .py files** in its tree (~387k under `var/` — venvs, caches, session artifacts). A TIA run against the OS repo **did not finish in 5 minutes** (killed). The engine's stated goal is sub-10-second loops; the traversal itself (O(visited × graph size) substring comparisons) makes it worse after the walk. There is no gitignore awareness.

### F3 — HIGH (confirmed by repro): TIA substring matching produces false positives
Changing `a.py` selects tests for `data.py` because `"a.py"` is a substring of `"data.py"`. Confirmed in the same repro: scenario 2 returned `test_data.py` alongside `test_a.py`. Wasteful (wrong direction of wrong, but still wrong), and combined with F8 it can balloon to near-full-suite runs.

### F4 — HIGH: relative imports resolved incorrectly
`ast.ImportFrom` handling ignores `node.level`. `from .sibling import x` inside `pkg/` is recorded as a top-level `sibling.py` instead of `pkg/sibling.py` — wrong or missing edges for every relative import.

### F5 — HIGH: newly added (untracked) files are invisible
`get_changed_files` uses `git diff --name-only <ref>`, which never reports untracked files. A newly added test or module is never selected. Needs union with `git status --porcelain` (or `git ls-files --others --exclude-standard`).

### F6 — HIGH: empty selection is ambiguous and there is no policy for non-import dependencies
- `get_changed_files` returns `[]` both on **git failure** and on **genuinely no changes**; `compute_affected_tests` maps `[]` to "run everything". Meanwhile a *successful* analysis that finds changed files but no affected tests returns `[]` — and the (future) caller cannot distinguish "nothing needs testing" from "selection silently failed". This must be an explicit tri-state in the output.
- Changes to `conftest.py`, `pytest.ini`, `pyproject.toml`, fixtures, or data files flow through no import edge. Today a conftest-only change selects **zero tests**. These files must be "global trigger" patterns that force a full run.

### F7 — MEDIUM: path-space mismatch when `repo_path` is not the git root
`git diff --name-only` emits paths relative to the **git root**; the graph keys are relative to **`repo_path`**. The hook allows arbitrary `repo_path` under `/Users/youruser`; pointing it at a subdirectory of a repo silently matches nothing. Rebase via `git rev-parse --show-toplevel`.

### F8 — MEDIUM: stdlib/shadow-name graph nodes amplify false positives
`import os` creates node `os.py`; `import json` creates `json.py`. With substring matching, a changed file like `macos.py` matches node `os.py` → everything importing `os` (i.e., everything) is "affected". Graph nodes should only be created for modules that resolve to real files inside the repo.

### F9 — HIGH (incompleteness): `ram_db.py` doesn't deliver its headline feature
- **No PostgreSQL lifecycle at all**: no initdb/start/stop on the RAM path, no `CREATE DATABASE … TEMPLATE …` cloning, no teardown. `get_postgres_ram_config()` returns args that **nothing consumes**. The plan's flagship "template cloning in ~20 ms" exists only in docs.
- `create_sqlite_ram_instance` ignores the boolean from `setup_ram_space()` and doesn't check the template file exists → raw `FileNotFoundError`/copy into a missing dir.
- macOS fallback rewrites `/dev/shm` → `/tmp`, which on macOS is **APFS disk, not RAM**, while the log still claims "RAM-disk workspace initialized". Detect and log honestly (or create a real RAM disk via `hdiutil`; honest logging is sufficient for v1).
- No cleanup API (instances accumulate — on Linux `/dev/shm` that is resident RAM), no per-worker naming/collision guard.
- Unused `import subprocess`. Config args missing `autovacuum=off`, `wal_level=minimal` vs the plan's own list.

### F10 — LOW: `scheduler.py` is algorithmically sound but cosmetically sloppy
LPT greedy (sort desc, assign to min-loaded shard) is correctly implemented. Issues: unused `sys`/`json` imports; the "default 10 seconds" comment sits on the wrong line; `shard_count > len(specs)` yields empty shards (consumer must skip); real usefulness is blocked on the missing duration store (F0).

### F11 — HIGH (contract): the two output schemas disagree, and the spec over-claims
- Plan §6 defines a **camelCase nested** report (`runId`, `summary.wallClockTimeMs`, …); `specs/ARCHI.json` defines a **snake_case flat** one (`run_id`, `duration_ms`, …). The hook's docstring points to ARCHI.json. **One must be declared canonical** (recommendation: ARCHI.json) and the plan annotated as superseded.
- `ARCHI.json` claims runtimes `["nodejs", "python", "golang"]` and frameworks `["pytest", "jest", "playwright", "vitest"]`. Zero non-Python support exists. v1 must scope to `pytest` and return a clear error for the rest — not pretend.
- Default parallelism is 1000 (ARCHI engine), 500 (ARCHI input default + hook), 100 (plan §6). Pick one.

### F12 — HIGH (OS-side blockers, companion fixes in OmniAgentOS)
Found while reviewing the consumer; without these the integration fails even with a perfect engine:
- The hook's injection-prevention regex `^[a-zA-Z0-9_\-\./]+$` **rejects `~` and `^`** — `HEAD~1` (TIA's own default) and `HEAD^` cannot be passed as `git_diff_ref`. Widen safely (allow `~ ^ @ { }`, still reject leading `-`).
- `subprocess.run(cmd, …)` has **no timeout** — a hung engine hangs the agent loop forever.
- Exit-code contract is implicit: `check=True` means the engine must **exit 0 when a run completes, even with failing tests** (failures are data in the JSON), reserving non-zero for engine errors. Must be written down and honored by `run.py`.
- `python3` is resolved from PATH; prefer an explicit interpreter.

### F13 — MEDIUM: repo hygiene
- The engine directory is **not a git repository** — it sits gitignored inside the home-directory repo. Zero version control for a component about to be integrated.
- No package structure (`__init__.py`), no `pyproject.toml`, no way to import it as a library; scripts only.
- **A testing engine with zero tests of its own.**
- README hand-off instructions describe the repo as more complete than it is.

---

## 2. Fix Plan

Scope decision for **v1 (integration-ready)**: local pytest path only — TIA → LPT → parallel pytest execution → JSON report. Postgres template cloning is v1.1. Lambda/E2E, telemetry, and self-healing (original plan Phases 3–4) are explicitly deferred and must not block integration.

### Phase 0 — Repo hygiene & contract lock (½ day)
1. `git init` the engine as a standalone repo; initial commit of current state (pre-fix baseline).
2. Add `pyproject.toml` + package layout (`src/omniagentos_testing_engine/…` or keep `src/` with `__init__.py`s); keep the engine **stdlib-only** so the subprocess contract needs no venv.
3. Declare `specs/ARCHI.json` the canonical contract. Fix it: modules list = what exists; `runtimes: ["python"]`; frameworks = `["pytest"]` (others → documented error); one parallelism default (recommend 500 to match the hook). Annotate plan §6 as superseded by ARCHI.json.
4. Fix README: mark Lambda/telemetry as *planned*, remove the claim that `src/lambda/` exists.

### Phase 1 — TIA rewrite for correctness + speed (1–1.5 days)
Rewrite `tia.py` module resolution and matching; keep the public class shape.
1. **Enumerate via git, not `os.walk`**: `git ls-files -- '*.py'` (+ `--others --exclude-standard` for untracked). Kills the 387k-file `var/` walk (F2) and F5 in one move. Fallback to a filtered walk only outside a git repo.
2. **Resolve imports to real repo files** instead of string-mangling: for `from X import Y`, add edges for whichever of `X/Y.py`, `X.py`, `X/__init__.py`, `X/Y/__init__.py` exists in the file index; honor `node.level` for relative imports by resolving against the importing file's package (F1, F4). Only create nodes for modules that resolve inside the repo (F8).
3. **Exact-path reverse index** (dict: resolved file → importers); traversal becomes O(edges). Delete substring matching entirely (F1, F3).
4. **Rebase git paths to `repo_path`** via `git rev-parse --show-toplevel` (F7).
5. **Global triggers**: changes matching `conftest.py`, `pytest.ini`, `pyproject.toml`, `setup.cfg`, `tox.ini`, `requirements*`, migration/fixture dirs → full-suite run (F6).
6. **Tri-state result** (F6): return a structured object — `{"mode": "subset"|"full"|"none", "tests": […], "reason": …}` — so "no tests affected" ≠ "analysis failed" ≠ "run everything". `run.py` propagates `mode`/`reason` into the report.

**Acceptance:** repro scenario 1 selects exactly `test_pkg.py`; scenario 2 selects only `test_a.py`; relative-import and untracked-new-test cases pass; conftest-only change → `mode: "full"`; graph build on OmniAgentOS **< 5 s**; a `from omniagentos.runner import sandbox` edit selects the sandbox tests. Per §2d-4, the selector runs in **shadow mode** (record, don't skip) until its audit window is clean — Phase 2's `run.py` honors `enforceTIA` only after promotion.

### Phase 2 — Build `src/orchestrator/run.py`, the missing keystone (1–1.5 days)
1. Argparse flags **exactly** as the hook sends them: `--repo_path`, `--test_framework`, `--max_parallelism`, `--git_diff_ref` (optional; default `HEAD~1` internally so the hook can omit it).
2. Flow: validate inputs → TIA (Phase 1) → load duration store → `LPTScheduler` (shards = `min(max_parallelism, os.cpu_count(), n_tests)`, skip empty shards) → run one `python -m pytest --junitxml=<tmp>` subprocess per shard concurrently, each with a hard timeout → parse junit XML → aggregate.
3. Emit **pure JSON on stdout** conforming to ARCHI.json `output_schema` (`run_id` uuid4, `duration_ms`, `total_tests`, `passed`, `failed`, `status: passed|failed|error`, `failures[]` with `test_name`/`file_path`/`error_message`); all logging to **stderr**. Include TIA `mode`/`skipped` counts as additive fields.
4. **Exit-code contract (F12):** exit 0 whenever the run completes (even `status: "failed"`); non-zero only for engine errors. Document in ARCHI.json.
5. **Duration store:** write per-spec wall times to `<repo>/.omni_test_cache/durations.json` (gitignored) after each run; feed into LPT weights on the next run (unblocks F10/F0).
6. `test_framework != "pytest"` → `{"status": "error", "error_message": "unsupported framework …"}`, exit non-zero.

**Acceptance:** `python3 src/orchestrator/run.py --repo_path <engine repo> --test_framework pytest --max_parallelism 8` returns schema-valid JSON; a seeded failing test appears in `failures[]` with `status: "failed"` and **exit code 0**; stdout parses as JSON with logging enabled; second run shows non-uniform LPT weights.

### Phase 3 — `ram_db.py` completion or honest descope (1 day)
1. Fix the SQLite path now: check template existence, honor `setup_ram_space()` failure, add `cleanup()`/context-manager teardown, per-worker instance naming (F9).
2. **Honest RAM reporting:** detect whether the path is actually memory-backed (`/dev/shm` on Linux; on macOS log clearly that `/tmp` is disk-backed) — no more false "RAM-disk" logs.
3. Postgres: recommend **descoping to v1.1** and saying so in ARCHI.json. If built now: `initdb` into the RAM path, `pg_ctl start` consuming `get_postgres_ram_config()` args, `create_template(migrate_fn)`, `clone(worker_id)` via `CREATE DATABASE … TEMPLATE …`, teardown; add `autovacuum=off`, `wal_level=minimal`.
4. Remove dead imports here and in `scheduler.py`; fix the misplaced default-weight comment.

### Phase 4 — Engine self-tests + validation ladder (½–1 day)
1. Pytest suite for the engine itself: the two repro scenarios as regression tests, relative-import/untracked/conftest TIA cases, LPT distribution properties (including `shards > specs`), sqlite clone lifecycle, `run.py` end-to-end against a fixture mini-repo (schema-validate the JSON output).
2. Dogfood: run the engine's own suite through `run.py`.
3. Wire into the OmniAgentOS TESTING.md validation ladder as a gate for the integration branch.

### Phase 5 — OS-side companion PR in OmniAgentOS (small, do alongside Phase 2)
1. Widen the `git_diff_ref` regex to allow `~ ^ @ { }` while still rejecting a leading `-` (so `HEAD~1` works).
2. Add a `timeout=` to the hook's `subprocess.run` (e.g., 600 s) and handle `TimeoutExpired`.
3. Use `sys.executable` (or a pinned interpreter) instead of bare `python3`.
4. Surface the engine's `mode: "none"` (no affected tests) distinctly from a passing run in agent-facing output.

### Deferred (unchanged from the original plan, explicitly non-blocking)
Lambda browser fleet (`src/lambda/runner.js`), work-stealing queue, telemetry (`src/telemetry/otel.py`), Triage & Healing agent — original plan Phases 3–4. Keep the empty dirs with a `PLANNED.md` marker or delete them until real.

An AWS account is available for this phase (credentials held by the operator). When it starts: **never commit keys to this repo** — load them from a local AWS profile or environment; prefer short-lived STS credentials or an IAM role over long-lived IAM user keys; validate with `aws sts get-caller-identity` before deploy scripts assume access.

---

## 2b. Immediate speedup (do today, independent of the engine)

Measured on OmniAgentOS (24 cores, 2026-07-31):

- 1,336-test slice: **110.9 s serial → 20.9 s** parallel (5.3x, zero code changes).
- Full non-smoke suite (11,437 selected): serial-equivalent **5,946 s (99 min)** of cumulative test time → **814.9 s (13:34)** under `-n 8 --dist worksteal` = **7.3x at 91% scaling efficiency**; 11,371 passed, 66 failed.
- The 66 parallel failures cluster in the quarantine categories predicted above: `tests/simharness` (20; also **3,600 s = 60% of all suite time**), `tests/longhaul` h08 family (~15), `tests/counterfeits` (3 — one asserts a clean operator tree while sibling workers write), plus a handful of singles. **75% of total suite time (4,444 s) sits in simharness+counterfeits+longhaul+comprehensive** — the slow suites and the unsafe suites are the same suites, so quarantining them fixes correctness and wall time together: `make test-fast` now ignores the three unsafe dirs (they remain covered serially by `make test`/the release gate, which stay the trusted signal).
- **Isolation fixes applied** (the first quarantine-list burn-down): `tests/api/test_orch_resume.py` rewritten with a deterministic `threading.Event` completion signal + hermetic store-getter stubs (root cause: cold 2×85-migration store construction in the daemon thread outliving a fixed 0.1 s sleep; it only passed serially because `test_jira_routes.py` leaked real resume daemons that warmed process-wide `lru_cache` singletons). Systemic fix: `tests/conftest.py` now pins `OMNIAGENTOS_ORCH_RESUME_ON_STARTUP`/`OMNIAGENTOS_SWARM_RESUME_ON_STARTUP` off session-wide (dedicated lifespan tests re-enable per-function), killing the leaked-daemon class across `test_jira_routes`, `test_reliability_routes`, `tests/skills/test_api`, `tests/steward/test_suggest_routes`, `tests/knowledge/test_api`. Verified: the test passes alone, whole file passes, jira routes pass.
- Suite-hygiene follow-up: `tests/swarm/test_live_all_providers.py::test_live_provider_exec_all_non_claude` ran (and failed) inside the default non-smoke selection despite its live-looking name — marker gating deserves an audit.

Actions:

1. ~~Add `pytest-xdist`~~ **DONE 2026-07-31**: `pytest-xdist==3.8.0` locked in the OS repo's dev group; `make test-fast` added (`-n 8 --dist worksteal -m "not smoke" --durations=25 --junitxml=var/test-reports/…`); TESTING.md documents the lane and its honesty rules.
2. **Do not use `-n auto`** — empirically confirmed: 24 workers over the full suite (including process-spawning smoke tests) **deadlocked** (44 min elapsed, 0% CPU, workers dead, master waiting forever). The conservative shape is bounded workers + `--dist worksteal` (dynamically redistributes the long tail) + smoke excluded. Benchmark 2/4/8/12 workers × {loadfile, worksteal} before raising the count.
3. Keep `make test` serial until the parallel-unsafe quarantine list is empty. Quarantine candidates to mark serial/resource-grouped first: nested pytest/Make invocations, repository-state and git tests, process/fixed-port tests, shared-SQLite/shared-directory tests, env-global mutators. First confirmed case: `tests/api/test_orch_resume.py::test_lifespan_resume_enabled_calls_resume_orphaned` is **order-dependent** (fails alone; passes only after earlier tests seed state) — parallelism exposes it, doesn't cause it. Treat every parallel-only failure as an isolation bug in the test.
4. This is the engine's Phase 2 execution layer in miniature: `run.py` should shell out to the same xdist mechanism per shard rather than reinventing process pooling.

## 2c. Long-term: a testing system that can test anything

The v1 pipeline (**select → schedule → execute → report**) is framework-agnostic; everything framework-specific lives behind a small adapter interface. That is what makes "test anything" tractable rather than a rewrite per stack:

```
FrameworkAdapter (one per framework)
  discover(repo)            -> [test specs]
  impact(changed, repo)     -> [affected specs] | FULL   (language-aware import resolver)
  run_shard(specs, opts)    -> raw report (junit/json)
  parse(raw)                -> normalized results (ARCHI.json failures[] shape)
```

- **v1 (this plan):** `pytest` adapter — Python import resolver (Phase 1), xdist execution (Phase 2).
- **v1.1:** `jest`/`vitest` adapter — ES-module import graph for TIA; workers via each runner's native parallelism.
- **v1.2:** `playwright` local adapter; `go test` adapter (Go's build graph gives TIA nearly for free).
- **v2:** execution *backends* become pluggable and orthogonal to adapters — `local-pool` (today) vs `lambda-fleet` (original plan Phase 3): same shards, dispatched to Lambda instead of local processes. S3 failure-artifact policy and the healing agent attach here.
- **Unknown frameworks:** explicit `FULL` fallback with a clear report field — never guess, never silently skip.

`specs/ARCHI.json` already anticipates this (its framework enum); adapters are what make that enum honest. Each adapter ships only when it passes the same acceptance bar as pytest v1 (no false-negative selection, schema-valid reports, order-independence verified).

## 2d. Operator directives (adopted 2026-07-31)

the operator's review added four structural requirements. They are binding on Phases 1–2 and extend existing repo doctrine (`scripts/ladder-record.sh` already binds one ladder run to a SHA after measuring a reviewer burn 1,507 s re-running what a scoped run does in ~23 s; "green suites are non-evidence" is standing memory).

**1. Immutable, certifiable test runs.** Trusted gates never run against the shared live checkout. Each certification run executes in a detached worktree (or container) pinned to one commit, with: pre/post SHA + working-tree assertions; worker-namespaced `var/`, tmp, SQLite, cache, and ports; lockfile/Python/env fingerprint; a hash of the collected test node IDs; and **refusal to certify dirty or changing input**. `run.py` gains `--certify` mode implementing exactly this; exploratory mode (`test-fast`) stays cheap and unpinned but is never presented as certification.

**2. One full suite per SHA — content-addressed evidence receipts.** Extend the `ladder-record.sh` receipt into the engine's report: commit + tree SHA, dependency/config fingerprints, exact command + selected-node digest, JUnit results with durations, environment/worker topology, failure artifacts. The coordinator runs the full gate **once per merged SHA**; reviewers and sibling agents reuse the receipt iff every fingerprint matches, else re-run. This attacks the fleet's measured binding constraint (verdict latency), not just raw test speed.

**3. Four honest lanes.**

| Lane | Contents | Target |
| :--- | :--- | :--- |
| `test-dev` | explicit/changed tests + `acceptance_smoke` | ≤15 s p95 |
| `test-pr` | impacted tests + always-run critical set | ≤60 s initially |
| `test-full` | complete backend unit/integration certification | ≤4 m p95 |
| `test-nightly` | full + real-process smoke + fuzz/mutation/live-AI evals | time-budgeted |

Real-process smoke leaves the default backend lane. A fast lane must never be represented as full certification.

**4. TIA ships in shadow mode.** Phase 1's selector **records** what it would have chosen but skips nothing until proven. Promotion requires: coverage-relationship data + reverse imports + ownership metadata as combined signals; an always-run critical/security/doctrine set; broad full-run fallback for `conftest.py`, migrations, lockfiles, pytest config, plugin registries, and unknown paths; and **random audit of 5–10% of unselected tests** on every shadow run with zero would-have-missed failures over the audit window. AST-only selection is insufficient for fixtures, dynamic imports, configuration, migrations, and data dependencies — the Phase 1 resolver is one signal, not the authority.

**Instrumentation before optimization** (feeds lanes and LPT): every full run retains `--durations=50`, JUnit XML, per-test historical runtime, worker assignment, retry/flake info, selected-node digest, CPU + wall time. Optimize the top twenty *cumulative* offenders, not tests that merely look suspicious.

## 3. Definition of "ready for integration" (checklist)

- [ ] `run.py` exists; hook round-trip from OmniAgentOS returns schema-valid JSON for a real run
- [ ] TIA repro scenarios 1 & 2 pass exactly (no false negative, no false positive); regression tests committed
- [ ] TIA on OmniAgentOS: graph build < 5 s; edit to `omniagentos/runner/sandbox.py`-style module selects its tests
- [ ] Untracked new test file is selected; conftest-only change triggers full run; "no affected tests" is explicit in output
- [ ] Failing test ⇒ `status: "failed"`, populated `failures[]`, exit code 0; engine error ⇒ non-zero exit
- [ ] Duration store populated after first run and consumed by LPT on the second
- [ ] ARCHI.json matches reality (modules, runtimes, frameworks, defaults); plan §6 annotated
- [ ] Engine is its own git repo with its own passing test suite (run via itself)
- [ ] OS-side hook accepts `HEAD~1`, has a subprocess timeout
- [ ] Certification runs are SHA-pinned in detached worktrees and emit fingerprinted evidence receipts (§2d-1/2); dirty input refused
- [ ] Four lanes exist and are honestly labeled (§2d-3); TIA still in shadow until its audit window is clean (§2d-4)

## 4. Effort estimate

Phases 0–2 (unblocks integration): **~3 focused days**. Phases 3–5: **~2 days**. Realistic total: **~1 week** for a v1 that honestly delivers the local fast-path; Lambda/E2E remains per the original 8-week roadmap.

---

*Review evidence: micro-repro at scratchpad `tia_repro` (scenario transcripts embedded above); timed TIA run against OmniAgentOS killed at 5 min during graph build; `grep -rE "^from omniagentos[a-z_.]* import" omniagentos | wc -l` → 1,671.*
