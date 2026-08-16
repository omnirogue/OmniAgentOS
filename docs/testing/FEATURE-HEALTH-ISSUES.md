# Feature-Health Issues Register + Phased Fix Plan

Generated from the first feature-health baseline, 2026-07-31, at HEAD `cb6e015f` (tree dirty: 17 files).
Machine-readable source: `var/feature-health/ledger-202607.jsonl`; live grid: `var/feature-health/LATEST.md`.
Query with `scripts/feature_health/fh.py issues` (add `--json` for tooling, `--include-expected`
to show known-issue reds). This file is the human/CLI-readable triage on top of that ledger.

Every item below was triaged to a verdict with a serial standalone rerun, a check of the
uncommitted working-tree diff, and a read of the production code path. Verdicts are:
`product_bug` (defect in shipped code), `test_bug` (test asserts stale semantics),
`dirty_tree` (caused by uncommitted work in progress), `flaky_test` (real code, racy test),
`lane_defect` (defect in the new test system itself), `coverage_gap`.

> **STATUS 2026-07-31 18:40Z — ALL ITEMS BELOW ARE RESOLVED AND MERGED TO MAIN.**
> Merged at `fe3e07a9` (fast-forward), gate PASS on all 18 checks (counterfeit corpus
> 24 mutations / 0 survived, ladder 2196 passed), archdocs restamped on main at
> `1dc82798`. Post-merge feature-health lane on merged main: **5,583 tier1 tests,
> zero failures, no open issues**; live probes green and the 18:37:19Z tick fired both
> autonomous routines. FH-000/001/002 are flipped to `fixed` in
> `docs/testing/KNOWN-ISSUES.yaml`. Kept for the record and for the reasoning trail.
>
> Two follow-ups did NOT need fixing and should not be re-actioned: I-15 (ARCHI drift)
> was already resolved upstream by archi-morning, and the parallel-only lane flakes
> (openhands gate latency, failure-injection timeout, routines process-revision CAS,
> interactions collection) all pass serially — they are the documented xdist
> isolation-bug class in TESTING.md, not regressions.

---

## Baseline result

11 features + the api_ui surface, three tiers, ~5,450 attributed tests, **wall time under 4
minutes**, **total live-LLM spend $0.002019**. 6 unexpected failures, 2 expected
(red-by-design). Live probes against the running API: 4/4 pass. Playwright against the live
stack: 15/16.

---

## P0 — live outage, happening now

### I-0 `product_bug` · routines · autonomous dispatch is suppressed while the API reports it healthy
Both autonomous routines — `lab-jobs-drain` (`rtn_7ce023cc8efb471abe9e`) and
`improve-lane-dispatcher` (`rtn_cd01d8e43e0d41f48ab2`) — have been **skipped on every 5-minute
tick** since at least 16:34Z with `reason: "acceptance rate below the 50% auto-pause floor"`,
yet `GET /api/routines` reports both as **`status: active` with an empty `auto_pause_reason`**.
Nothing surfaces the outage: the dashboard shows two healthy routines that have not fired in
hours.
- Evidence (verified live, this session): six consecutive ticks in `var/log/routines.log`
  (16:34:09Z → 16:59:36Z) show both routines in `skipped[]` with that reason and `fired: []`;
  the API simultaneously returns `active` / no reason for both ids.
- Two distinct defects compose here:
  1. **The suppression itself.** Per the 16:11Z plan-consolidator observation, all 23
     settlements came back `gate_evidence_unavailable` — routines have no gate workspace, so
     acceptance can never clear the floor. The floor counts un-gateable settlements as
     rejections. Fix the floor to bucket `gate_evidence_unavailable` separately rather than
     disabling the routines.
  2. **The silence** (this is I-6 below, and it is what makes #1 invisible): `settle_run`
     re-checks the floor only on *accepted* transitions
     (`omniagentos/scheduler/store.py:775`), so a routine suppressed by `should_fire` never
     flips to `auto_paused` and never gets an `auto_pause_reason`.
- This also independently confirms the I-5 verdict: the acceptance floor **is** reachable and
  working in production; the three failing tests are stale.
- Lane coverage gap this exposes: tier1 routines tests use the mock harness and never traverse
  the gate-evidence settlement path, and no probe compared "is it firing?" against
  "does it claim to be healthy?". A `routines_not_silently_suppressed` live probe has been
  added to `tests/feature_health/tier3/test_live_probes.py` to catch exactly this shape.

## P1 — fix first

### I-1 `product_bug` · DAG · graph_runtime.complete_node duplicate-artifact race (FH-001)
Two threads completing the same ready node both pass the status gate and both commit,
producing **two `graph_artifacts` rows for one (node, port)** with divergent content hashes.
The status is read outside the write transaction, and `update_node` UPDATEs with no status
guard, so it is last-write-wins.
- Evidence: `tests/feature_health/tier1/test_dag_complete_node_race.py` fails deterministically
  (3/3 runs) with `2 == 1` on the artifact count. Registered as FH-001 in
  `docs/testing/KNOWN-ISSUES.yaml`; the ledger tags it `expected=true`.
- Fix: make the completion an atomic CAS — `UPDATE ... WHERE status IN ('ready','running')`
  plus a rowcount check; the loser must not write an artifact. Then flip FH-001 to `fixed`;
  `fh.py issues` will flag the now-green test for promotion into the certification suite.
- Blast radius: any fan-in/parallel DAG run can bind a synthesis step to a non-deterministic
  artifact. Production-affecting.

### I-2 `dirty_tree` · swarm API · uncommitted `speedup < 1.0` branch mislabels planning runs
The uncommitted edit at `omniagentos/api/routes/swarm.py:758-760` adds
`elif speedup < 1.0: health = "degraded"`. But `speedup` is `0.0` as a **sentinel** meaning
"no wall clock yet" (`actual_minutes == 0`, e.g. a run still planning), not a measured
slowdown — so every planning run reports `degraded`.
- Evidence: `tests/api/test_swarm_routes.py::test_overview_aggregates_active_runs` seeds
  exactly that state and fails `assert 'degraded' == 'healthy'`. The test is correct; the
  in-progress edit is wrong.
- Fix: guard the branch on real wall clock — `elif actual_minutes > 0 and speedup < 1.0:`.
- Blast radius: none yet (uncommitted). Ships a false-degraded swarm health signal if committed.

---

## P2 — fix next

### I-3 `product_bug` · dashboard · deep-linked project filter shows the wrong empty state
`/board?project=<id>` renders **"No matching cards"** instead of the project-scoped board or
its scoped empty state. `useLiveBoard`'s in-flight dedup
(`dashboard/src/features/collab/hooks.ts:103-139`) queues a rerun but calls the **stale
`refresh` closure**, so the scoped fetch is dropped and the unscoped result (969 cards) is
client-filtered to zero. Introduced by the collision of `50d06e3f` (dedup, 07-22) with
`2887a2a0` (project scoping, 07-28).
- Evidence: Playwright `board.spec.ts` "project filter scopes the board" fails deterministically
  against the live stack; error-context snapshot shows "0 of 969 cards". Server side is correct
  (`GET /api/collab/board?project_id=...` returns `[]`).
- Fix: hold the callback in a ref — `const refreshRef = useRef(refresh); refreshRef.current = refresh;`
  and call `refreshRef.current()` in the `finally` rerun. Same hazard applies to toggling
  "Show archived" mid-flight.
- Blast radius: **real users of the live dashboard.** Self-corrects only on an SSE board event
  or the 30s visibility poll — and SSE was observed in "reconnecting" state, so the wrong
  state can persist 30s+.

### I-4 `product_bug` · api_ui · four phantom `/api/judges/*` endpoints (FH-002)
`dashboard/src/features/reliability/api.ts:547-594` calls `/api/judges/panel`,
`/panel/reseat`, `/stats`, `/votes`; none exist in the backend (`src/app/judges/page.tsx:58`
carries a TODO admitting it).
- Evidence: `tests/feature_health/tier1/test_ui_api_drift.py` (red-by-design, FH-002).
- Correction to the prior assumption: **`/api/updates` is NOT phantom** — it is mounted via
  `omniagentos/skills/router.py` under the `/api` prefix and is present in `contracts/openapi.json`.
- Fix: either implement the four routes or remove the dead client code and the judges page.

### I-5 `test_bug` · routines · three auto-pause tests assert pre-fix semantics
`tests/routines/test_api.py::test_record_runs_and_auto_pause_via_api`,
`tests/routines/test_store.py::test_auto_pause_trips_below_50pct_acceptance`,
`tests/routines/test_tick.py::test_sub_50pct_acceptance_routine_does_not_fire` all fail
`assert 'active' == 'auto_paused'`. They seed runs without `gate_passed`/`finished_at`, which
today's acceptance-floor fix (`63a69f38`, `591e89f6`) deliberately classifies as **pending** —
the exact category the fix exists to exclude. Tests date from `fe0ba6b1` (07-21), ten days
before the fix.
- **Production auto-pause is NOT broken** (verified empirically): every production `record_run`
  path in `routines_tick.py` passes both fields, and 3 settled runs at 1/3 acceptance do flip
  to `auto_paused`; pending-then-rejected routines are suppressed at fire time by `should_fire`.
- Fix: add `gate_passed` and `finished_at` to the seeded runs in all three tests.
  `RecordRunRequest` already accepts both. Also re-seed
  `test_auto_pause_does_not_trip_above_floor`, which now passes **vacuously** (0 settled runs
  can never pause).

### I-6 `product_bug` (minor) · routines · rejected settlements never flip status
`settle_run`'s floor re-check (`omniagentos/scheduler/store.py:775`) runs only on an
*accepted* transition. A routine whose pending runs all settle **rejected** stops firing
(via `should_fire`) but keeps `status='active'` with no `auto_pause_reason` — the dashboard
shows it healthy while it silently never runs.
- Fix: re-check the floor on rejected settlements too.

### I-7 `flaky_test` · swarm · parked-attempt escalation asserts across two writes
`tests/swarm/test_parked_attempt_timeout.py::test_parked_attempt_past_its_tier_timeout_is_closed_and_escalated`
fails `assert 'standard' == 'complex'` under the parallel lane but **passes 8/8 serially**.
`_timeout_attempt` (`omniagentos/swarm/scheduler.py:5248-5267`) does two sequential writes —
`close_attempt`, then `_merge_swarm_json(current_tier=...)` — and the test waits only on the
first before asserting the second.
- Fix: `wait_until` on `current_tier == 'complex'` rather than on `end_reason == 'timeout'`.
- Production is unaffected: the claim is released only after the tier merge returns.
- Confirmed: a second full tier1 baseline on a less-contended box passed this test
  (multi_agent 1396P, zero failures), so the lane's own reruns reproduce the load sensitivity.
- Note: a commit on branch `temp-fix-merge-gate` xfailed this test to unblock a gate; that
  xfail never landed on main. Fix the wait, don't re-xfail it.

---

## P3 — hygiene and coverage

### I-8 `lane_defect` (FIXED) · tier2 silently skipped the Jira live test
`run.sh`'s `feature_health and live` deselected matrix tier2 paths outside
`tests/feature_health/` (the conftest force-marks only its own dir), so
`tests/connectors/test_jira_live.py` never ran and jira tier2 recorded a clean `0P`.
**Fixed** — selection is now `live and not (smoke or live_cli or perf or live_ollama or counterfeit_gate)`;
verified jira tier2 now runs and passes (8 → 9 tier2 tests).

### I-9 `lane_defect` (FIXED) · did-not-run rendered as a pass
A record covering matrix paths but observing zero testcases rendered as `0P`. **Fixed** —
`fh.py` now marks such records `status="error"` + `did_not_run=true`. Verified with a
zero-testcase report.

### I-10 `lane_defect` (FIXED) · jira tier3 count inflated
`tests/projects/test_routes.py` (20 generic project-CRUD tests) was attributed wholesale to
jira. **Fixed** — the matrix now names the three `jira_project_key` nodeids.

### I-11 `product_bug` · OpenRouter exact cost requires `usage:{include:true}`
The adapter payload does not send it, so `usage.cost` — the basis for `cost_quality="exact"`
— is not returned by default. The tier2 probe had to inject it to prove provenance.
Production swarm spend may therefore record as non-exact.
- Fix: send `usage: {include: true}` in `omniagentos/adapters/openrouter.py`.

### I-12 `env` · LiteLLM proxy model-id mismatch
The local proxy serves gemini under aliases `gemini36` / `gemini25-flash-lite`, **not** the
canonical `gemini-3.6-flash` / `gemini-3.5-flash-lite` ids the client and planner pin. The
tier2 probes fall back to the alias; production code pinning canonical ids will miss.
Also: `configs/swarm.yaml` `api_fallback.openrouter_models` has no gemini flash-lite entry.

### I-13 `env` · unauthenticated `/api/accounts` on the live API
`curl http://127.0.0.1:8485/api/accounts` returns 200 with account emails, config-dir paths,
and auth status — no token. Consistent with the 2026-07-31 finding that GET-route auth is
policy-by-code with no mechanical registry (48/171 GETs ungated).
- Fix shape: a mechanical auth-gating registry, mirroring the existing path-traversal registry,
  so a new sensitive GET must declare its posture.

### I-14 `coverage_gap` · thin tiers
- `multi_agent` is the only feature with **no** tier1 gap test (all 1,395 tests are pre-existing).
- No tier2: `goals`, `routines`, `tool_injection`, `allocation`. No tier3: `single_agent`, `multi_agent`.
- `TestReflectionPaths` asserts only the 4xx contract (approve mutates real config targets).
- `memory` tier1 carries 72 skips — all `tests/knowledge` Postgres gates
  (`OMNIAGENTOS_REQUIRE_PG not set`), honestly recorded as skips.

### I-15 `docs` (RESOLVED UPSTREAM) · ARCHI.md route inventory drift
Was: stamp claimed 301 routes / 45 modules against a tree with more. **Already fixed on main**
by the `archi-morning` job at 16:32Z (`0a58282a`, `cb6e015f`): the stamp now reads
`max_migration=98 route_count=310`, which matches the tree exactly (highest migration `098`,
310 route decorators across 49 modules). No action needed — do not re-run archdocs for this.

### I-16 `lane_defect` (FIXED) · one (feature,tier) cell can hold several sub-runs
`api_ui`/`tier3` is three independent streams — the isolated suite, the live probes, and
Playwright. Keying the report on `(feature, tier)` let the newest append mask the others,
hiding the FH-000 live-probe red. **Fixed** — `fh.py issues` now keys on
`(feature, tier, env, stream)` where stream is the report basename with its run timestamp
stripped, so each stream's newest run is surfaced and stale runs still fall away. The grid
deliberately stays one cell per `(feature, tier)`.

---

## Operational note (not a defect)

A runner daemon (PID 68921, started 12:27 EDT, parent launchd) is polling the **product**
runtime DB `var/runtime/state.sqlite3`. It was dead at 07:13Z when this work began and
is alive now. `com.omniagentos.runner.plist` is `.disabled-20260731` in
`~/Library/LaunchAgents`, so confirm this process is intentional before relying on it.

---

## Phased plan

**Phase 0 — land the lane (no product risk).** The lane is untracked and touches product
config in exactly three places: the `feature_health`/`fh_known_issue` markers and the addopts
exclusion in `pyproject.toml`, and `FAST_LANE_MARKERS` in the `Makefile`. Verified: default
addopts and the fast lane both deselect every lane test, and the leak-regression check over
all 11,948 tests collects zero. Commit, then run
`scripts/scheduler/install-feature-health.sh` and load the two launchd jobs (tier1+tier3 every
4h; tier2 + live probes + Playwright nightly at 03:10) so the ledger fills without anyone
running anything.

**Phase 1 — correctness (I-1, I-2).** The DAG CAS fix and the swarm `speedup` guard. I-1 is
the only P1 that can silently corrupt run output; do it first and let FH-001 flip to green,
which the lane will then flag for promotion. I-2 is a one-line guard on uncommitted work —
fix it before that edit is committed.

**Phase 2 — user-visible (I-3, I-4).** The board stale-closure fix is the only defect on this
list a real user hits today. Pair it with a decision on `/api/judges/*`: implement or delete.
Both are covered by red-by-design tests that will go green and announce themselves.

**Phase 3 — test truth (I-5, I-6, I-7).** Re-seed the four routines tests to production shape,
fix the parked-attempt wait, and close the rejected-settlement status gap. After this the lane
should show **zero unexpected failures**, which is what makes it useful as a regression alarm —
a green baseline is the whole point.

**Phase 4 — cost and auth truth (I-11, I-12, I-13).** Send `usage:{include:true}`; reconcile
the LiteLLM aliases and the swarm.yaml allow-list; then the auth-gating registry, which is
the largest piece and deserves its own design pass.

**Phase 5 — close coverage (I-14, I-15).** Add the multi_agent tier1 gap test and tier3
coverage for the agent-execution features; run `archi-morning` to restamp the route inventory.
