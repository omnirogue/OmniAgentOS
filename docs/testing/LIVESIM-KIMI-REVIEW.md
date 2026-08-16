# LiveSim — Kimi adversarial review history

Kimi (cash-safe OAuth model `kimi-code/kimi-for-coding`, never the metered
`fireworks/kimi-k3`) reviewed the suite's inventory, architecture, coverage,
redundancy, reproducibility, and missing risks. Each recommendation is evaluated
below as **ACCEPTED** (with the change made) or **REJECTED** (with reasoning), per
the adversarial-loop doctrine (close at the first zero-blocker round or after 5).
Full raw reviews: `var/livesim/kimi/round-N.md`.

## Round 1 (2026-08-06) — no blockers; low/medium polish

Kimi read the actual test files and returned grounded findings. None was a
correctness defect in the suite. Verdicts:

| # | Finding (sev) | Verdict | Action / reasoning |
|---|---|---|---|
| A1 | `run.py` help advertises `--issues` but `main()` never implements it (med) | **ACCEPTED** | Real bug. Implemented `cmd_issues()` — `run.py --issues` prints the issue log sorted by severity. |
| M1 | `cheap_llm._try_litellm` falls back to `served[0]` if no cheap alias matches — could call a model the proxy routes to a **metered** org (med) | **ACCEPTED** | Directly relevant to the billing-pause context. Now refuses the fallback and returns `available=False` unless an allowlisted cheap model is served. |
| P1 | Global product counters (`skill_resolution_drop_counts`, `RENDERED_CLAIM_DIAGNOSTICS`) used in before/after assertions aren't isolated → non-deterministic under xdist (med) | **ACCEPTED** | Added an autouse `_reset_observation_counters` fixture (best-effort, clears them before+after each test). |
| M2 | A future scratch test that drives a product module without repointing `OMNIAGENTOS_DB` could touch the live DB (med) | **ACCEPTED (safe form)** | Added a shared opt-in `scratch_db` fixture (copies the live DB, repoints `OMNIAGENTOS_DB`/`OMNIAGENTOS_VAR_DIR`, restores after). A mutation-mtime guard was rejected as infeasible — the live DB is continuously written by the running prod system, so mtime can't distinguish test writes from prod writes. |
| A2 | `reaper_tracker.py` hardcodes log-glob paths (low) | **ACCEPTED** | Now derives the log dir from `LIVE_DB`'s serving checkout, overridable via `LIVESIM_REAPER_LOG_GLOBS`. |
| I1 | No automated (no-browser) dashboard/trusted-hop coverage; LS-003/004 only caught by browser-operator (med) | **ACCEPTED** | Added `test_dashboard_shell_loads_and_records_api_reachability`: GETs `:3003` shell (200) and its `/api/*` proxy leg, asserts the shell serves and records the `trusted_proxy_403` datum — reproduces LS-003 repeatably. |
| P2 | Security secret-sweep's 90s deadline scans a variable subset under load (med) | **REJECTED (already mitigated)** | The sweep records `skipped_for_time` honestly and skips as degraded below a floor; the deadline trips only under extreme (self-inflicted) load. LS-027 already hardened it (5s/endpoint + OSError→status 0 + per-endpoint guard). Adding a hard "must scan all" would convert a load transient into a false failure — the opposite of reproducibility. |
| P3 | Observational defect-documenting tests assert current buggy state and fail when the product is fixed; retirement not machine-enforced — use `xfail`/version-gate (low) | **REJECTED (deliberate design)** | These tests are DESIGNED to flip red when the defect is fixed — that red is the promotion signal, mirroring the repo's own `fh_known_issue` feature-health pattern. `xfail` would hide the fix signal. The issue-log linkage (LS-###) is the intended retirement mechanism. |
| R1/R2/R3 | Duplicate auth/approvals/dashboard/error-shape/classifier tests across categories — consolidate/cut (low) | **REJECTED (intentional)** | LiveSim is organized as a per-category health signal (feature-health philosophy): each category independently exercises its own surface so a category's verdict is self-contained. The "duplicates" assert different invariants (shape vs cross-source-coherence vs DB-state). Cutting them weakens per-category coverage. Noted as intentional cross-category overlap. |
| I2/I3/C4 | No live approval-action / board-file-with-valid-token / write-endpoint coverage (med/low) | **REJECTED (goal constraint)** | Requirement 8 forbids destructive production actions; these need real writes/token-minting against live prod. Recorded as explicit gaps in the final report §8 for a later, appropriately-scoped session. |
| C1 | Concurrency coverage thin (7 tests) (med) | **PARTIAL — noted** | The high-value concurrency races (stage_candidate, session manifest, approval gateway, dashboard reads) ARE covered. Broader concurrency (migrations, skill versioning) is recorded as a gap; not expanded this round to avoid low-value bloat. |
| C2 | Recovery misses service-restart / WAL-crash / reaper-resume (med) | **REJECTED (scope/safety)** | Simulating a service restart or crash on the live system is invasive; the reaper-resume path is covered via synthetic rows + live-kill evidence. Recorded as a gap. |
| A3 | `report.py` flaky detection only catches base-vs-rerun flips (low) | **REJECTED (acceptable)** | Cross-run flakiness tracking is out of scope for a per-run coverage report; Kimi rated it acceptable. |
| M3 | `allow_write` usage not audit-logged in telemetry (low) | **REJECTED (no caller)** | No test uses `allow_write`; the fixture already refuses non-GET without it. Low value until a writing test exists. |

**Round 1 outcome**: 6 ACCEPTED and implemented (A1, M1, P1, M2, A2, I1); the rest
REJECTED with reasoning (design-intentional, goal-constrained, or already
mitigated). No blocker. All 122 tests pass after the changes.

## Round 2 (2026-08-06) — verified the round-1 changes; two refinements

Kimi re-read the modified files. It raised no NEW suite gaps; it verified the 6
accepted changes and found two points about them:

| # | Finding (sev) | Verdict | Action / reasoning |
|---|---|---|---|
| P1-fix | The autouse counter-reset targeted `skill_resolution_drop_counts` — a *function* that returns `dict(_DROP_COUNTS)`, so it has no `.clear` and reset nothing for the skills counter (med) | **ACCEPTED** | Correct catch. Retargeted the fixture to `omniagentos.skills.resolve._DROP_COUNTS` (the real `Counter`). `RENDERED_CLAIM_DIAGNOSTICS` was already a `Counter` and cleared correctly. Verified `_DROP_COUNTS.clear()` works; tests still pass. |
| A2-fix | Claimed `_log_globs()` resolves the log dir to the parent of the checkout, not `<checkout>/var/log` (med) | **REJECTED as a correctness claim; ACCEPTED as readability** | False positive — Kimi miscounted `parents[]`: `LIVE_DB.parents[1]` is `<checkout>/var`, and the code produced the correct `/Users/youruser/OmniAgentOS/var/log/*.log` (verified empirically). But the `.parent` round-trip was genuinely confusing, so the code was simplified to `var_dir = LIVE_DB.parents[1]; var_dir/"log"/"*.log"` — identical correct result, unambiguous. |

The suite delta tests observe before/after *deltas* on those counters, so they were
already robust to accumulation; the reset is defense-in-depth for a future xdist run.
No new coverage gap, no blocker.

## Round 3 (2026-08-06) — CONVERGED

Kimi's verbatim verdict: *"No new concrete issues. The two prior refinements are
fixed, the observational scope and isolation model are clear, and the logged 27
items are already queued for the repair session."* — followed by **NO FURTHER
IMPROVEMENTS**.

**Loop CLOSED at round 3** (max 5), zero remaining worthwhile improvements. Final
state: 122 tests, 100% pass, $0, all Kimi-accepted changes applied.
