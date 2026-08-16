# LiveSim — final coverage report (2026-08-06)

A complete, reproducible **live-simulation diagnostic suite** for OmniAgentOS.
Observational only — it never gates a merge or deploy. It exercises the *running*
system (live `:8485` API, live runtime DB, process table, the reaper stack, the
filesystem sandbox) plus cheap-LLM probes, records full telemetry per run, and
logs every defect it finds for a separate repair session. It does **not** fix any
product defect.

- **Branch**: `lane/livesim-0806` · **commit** `e21fbafb` (base of the worktree)
- **Authoritative run**: `livesim-r1applied-0806` — **122 passed / 0 failed / 0 skipped**, $0, 22.5s in-test (~140s wall)
- **Discover & run**: `scripts/livesim/run.py` · **Contract**: `docs/testing/LIVESIM.md`

---

## 1. Working features discovered (live-verified)

33 subsystems inventoried and mapped to tests in `docs/testing/LIVESIM-INVENTORY.md`.
Headline live state on 2026-08-06:

- ✅ **API on :8485** healthy (`/api/health` 200, `db:true`, `worker.alive:true`); auth now enforced on sensitive routes (`/api/accounts` 401 — a July unauth defect is closed).
- ✅ **Runner heartbeat, routines, swarm, loops** live; **MemLife / knowledge / vault**, **skills + CORAL (off)**, **toolplane + broker**, **WorkFS / scope / sandbox**, **DB migrations (head 118)**, **provider cost recording**, **event hub (SSE)** all functioning.
- ✅ **liveness-reaper** correctly reaps 160 dead-pid session rows; **idle/fleet reapers** report-only paths verified.
- ⚠️ **Dashboard** shell renders but is functionally down — every UI `/api/*` call 403s (LS-003).
- ⚠️ **A2 session reaper** max-park has killed 16 legitimate approval-starved sessions (LS-001).
- ⚠️ Cheap-LLM tier degraded to one leg (LiteLLM :4000 down, Kimi metered org paused).

## 2. Tests created

**122 tests across 13 category modules** (`tests/livesim/categories/`), catalogued
in the generated registry `configs/livesim-registry.yaml`:

| Category | tests | Category | tests |
|---|---|---|---|
| reaper | 12 | files_fs | 9 |
| api_endpoints | 10 | memory | 9 |
| orchestration | 10 | skills | 9 |
| security | 10 | telemetry_cost | 9 |
| tools_permissions | 10 | database | 9 |
| context | 9 | e2e_live | 9 |
| degradation | 7 | **Total** | **122** |

By test **type** (tests carry multiple type markers, so these overlap):
positive 42 · boundary 33 · negative 31 · security 27 · permission 17 · e2e_live 13 ·
recovery 11 · degradation 10 · concurrency 7. Every requested class is represented —
positive, negative, boundary, concurrency, recovery, permission, security,
degradation, and end-to-end.

## 3. Tests executed — results

| Result | Count |
|---|---|
| **pass** | 122 |
| **fail** | 0 |
| **flaky** (found & fixed) | 1 — secret-sweep read-timeout, LS-027 (test-infra; fixed) |
| **blocked/skipped** | 0 in the final run |
| **not-covered** (explicit gaps) | see §8 |

One failure surfaced during the pre-final run and was reran-to-classify:
- `memory::test_memlife_decisions_append_only_enforced` — **test defect** (over-strong
  assertion + a `PRAGMA foreign_keys` transaction-state trap). Investigating it
  *confirmed the append-only guard is robust*; test corrected.
- `security::test_no_public_get_body_exposes_a_credential_shape` — **test-infra flake**
  (unbounded sweep hit a chunked-read timeout; not a credential exposure). Harness
  hardened (LiveApi catches `OSError`; sweep is 5s/endpoint + 90s deadline). Rerun:
  83 endpoints / 7.25MB scanned, **0 credential findings**.

## 4. Cost and runtime

- **LLM cost: $0.00.** 2 tests touched an LLM (cheap-probe fell back to Claude CLI
  haiku on the subscription; LiteLLM was down). Every other test is a $0 API/DB/proc/fs
  probe. Cost, model, provider, tokens, latency, inputs/outputs, env, commit, config,
  and timestamps are recorded per test in `var/livesim/ledger.jsonl`.
- **Runtime**: ~22s summed in-test; ~140s wall (serial, shared loaded box, includes
  DB copies + subprocess reaper runs + a 90s-bounded security sweep).

## 5. Evidence locations

- **Ledger** (every run, every test): `var/livesim/ledger.jsonl`
- **Per-run records** (full telemetry JSON per test): `var/livesim/runs/livesim-r1applied-0806/`
- **Evidence** (dumps, sweeps, UI report): `var/livesim/evidence/livesim-r1applied-0806/` and `var/livesim/evidence/ui/dashboard-ui-check.txt`
- **Reaper ledger**: `var/livesim/reaper-ledger.jsonl` (+ `reaper_tracker.py legitimacy`)
- **Registry**: `configs/livesim-registry.yaml` · **Coverage report**: `docs/testing/LIVESIM-COVERAGE-REPORT.md`
- **Issue log**: `docs/testing/LIVESIM-ISSUES.yaml` (27 entries)

## 6. Product issues found (see LIVESIM-ISSUES.yaml for full evidence/repro)

**P0**
- **LS-003** — Dashboard functionally down: every UI `/api/*` call 403s "trusted proxy
  required" (`OMNIAGENTOS_TRUSTED_HOP_SECRET` unset while a fail-closed guard is at HEAD).

**P1**
- **LS-001** — A2 session reaper **max-park killed 16 legitimate sessions** whose approval
  was never delivered (the operator's flagged concern — confirmed, quantified, tracked).
- **LS-004** — Dashboard shows confident `0`/"No rows" when the backing fetch 403'd
  (favourable-absence defect class).
- **LS-024** — **Approval classifier fail-open** live: destructive requests phrased
  outside the enumerated verb vocabulary are auto-approved.

**P2 (10)** — session reaper enforce armed in prod (LS-002); `/today` counts only
`swarm_attempts` so a 30-session day reads 0/0 (LS-007); board projection 4.76MB / 95%
of the regression bound (LS-008); no exact cost flows into `provider_call_usage`
(LS-011); routine acceptance-counter drift (LS-016) and **912 finished routine_runs never
settled** (LS-017); board_files denylist admits `/private/etc` `/private/tmp` (LS-013);
workspace floor admits the **production checkout** end-to-end (LS-014); cheap-LLM tier
down to one leg (LS-010); CORAL enforce has no producer (LS-018); MemLife review queue
dormant — 210 staged, 0 graduated (LS-021).

**P3 (10)** and **info (2)** — see the issue log (405 error-code envelope, event_hub
`state=ok` while `tailer_alive=false`, capsule reason-code miswrite, cost-quality vocab
divergence, duplicate candidate admission, metacog retrieval telemetry never fires, etc.).

## 7. Test-infrastructure issues found

- **LS-027** (fixed this session) — the secret-sweep test was unbounded in wall-time and
  the LiveApi helper did not catch a mid-stream read timeout. Fixed: `OSError` caught →
  status 0; sweep bounded to 5s/endpoint + a 90s deadline with honest `skipped_for_time`.
- **info (LS-026)** — `PRAGMA foreign_keys` is a silent no-op inside an open implicit
  transaction; the harness/tests now open a fresh autocommit connection for FK-dependent
  assertions.
- **Tracking gap** — the A2 reaper's `reaper.kill`/`would_kill` events only reach the
  rotating Python logs, so the *durable* kill signal is the DB `killed_by` attribution;
  `reaper_tracker.py` reads the DB for exactly this reason. A repair session that wants
  live idle-kill visibility should add a durable JSONL sink in `supervisor._reap` (product change).

## 8. Not covered (explicit gaps for a later session)

- A real end-to-end **spawn of a live bridge session** watched through the A2 reaper in
  enforce mode (too invasive for an observational suite; reaper logic covered via
  synthetic rows + live-kill evidence instead).
- **Playwright** click-through interaction flows (the UI check is a render/health check
  via browser-operator, not full interaction coverage).
- Comms pollers (Telegram/Slack thread replies), banking/revenue collectors — out of scope.

## 9. Recommended priorities for the repair-planning session

1. **LS-003 (P0)** — restore the dashboard: set `OMNIAGENTOS_TRUSTED_HOP_SECRET` across the
   dashboard build + API/hop and rebuild `.next-remote`. The UI is dark for users right now.
2. **LS-001 + LS-024 (P1)** — the reaper/approval axis: wire approval paging
   (`SLACK_WEBHOOK_URL`/ntfy) so max-park stops starving legitimate sessions, and close the
   classifier fail-open (invert to an allowlist or widen the ORM/payment idioms).
3. **LS-014 (P2)** — the workspace floor admitting the production checkout is a real blast-radius risk.
4. **LS-017 / LS-007 (P2)** — settlement/telemetry truth: 912 unsettled routine_runs and the
   `/today` 0/0 undercount both make the operator view lie about system health.
5. **LS-004 + favourable-absence audit** — replace confident zeros with explicit error/unknown states.

## 10. Kimi review history

Kimi (cash-safe OAuth model `kimi-code/kimi-for-coding`) adversarially reviewed the
suite's inventory, architecture, coverage, redundancy, reproducibility, and missing
risks. Full accept/reject evaluation of every recommendation is in
`docs/testing/LIVESIM-KIMI-REVIEW.md`; raw reviews in `var/livesim/kimi/round-N.md`.

**Round 1** found no blockers. **6 recommendations accepted and implemented**:
`run.py --issues` (was advertised-but-missing); `cheap_llm` now refuses fallback to a
non-allowlisted (possibly metered) LiteLLM model; autouse counter-reset + opt-in
`scratch_db` fixtures (reproducibility/safety); portable reaper-tracker log paths; and
a new no-browser dashboard `:3003` smoke test that reproduces the LS-003 trusted-proxy
403 signature. The remainder were rejected with reasoning — design-intentional
(per-category overlap; observational defect-tests whose red IS the fix signal),
goal-constrained (no destructive live writes), or already mitigated (the security
sweep is bounded and honest). Suite grew 121 → **122 tests**, still 100% pass, $0.

**Round 2** verified the round-1 changes and found two refinements: the counter-reset
fixture was retargeted to the real `Counter` (`_DROP_COUNTS`), and the reaper-tracker
log-path derivation was simplified (Kimi's correctness claim there was a verified false
positive). **Round 3** returned **NO FURTHER IMPROVEMENTS** — the loop CLOSED at round 3
of a maximum 5, with zero remaining worthwhile improvements. Full per-finding
accept/reject reasoning: `docs/testing/LIVESIM-KIMI-REVIEW.md`.
