# LiveSim — live-simulation diagnostic suite

LiveSim is an **observational, non-gate** live-simulation suite for
OmniAgentOS. It exercises the **running** system — the live API on `:8485`,
the live runtime DB (`var/runtime/state.sqlite3`), the process table, the
reaper stack, the filesystem sandbox — plus cheap-LLM probes, and records full
telemetry for every run. It **never gates a merge or a deploy** (the `livesim`
marker is excluded from default and fast lanes; run it only via its runner).

> Purpose: give future agents a reproducible, discoverable way to see what the
> live system actually does — including the process/session reapers — with a
> complete run history and a structured issue log. It diagnoses; it does not fix.

## Run it

```bash
scripts/livesim/run.py                    # all categories, one run, full telemetry
scripts/livesim/run.py --category api     # one category
scripts/livesim/run.py --list             # categories + test counts
scripts/livesim/run.py --summary          # latest run's pass/fail/flaky grid + cost
scripts/livesim/run.py --rerun-failures livesim-YYYYMMDD-HHMMSS
```

Use the worktree venv (`.venv/bin/python`) — the runner picks it up automatically.
Every run gets a `run_id` (`livesim-<UTC timestamp>`), writes one JSON record per
test to `var/livesim/runs/<run_id>/`, appends each to `var/livesim/ledger.jsonl`,
and stores evidence under `var/livesim/evidence/<run_id>/`.

## What each ledger record captures (`livesim.v1`)

`run_id, nodeid, category, types[], status(pass|fail|skip|xfail), ts, latency_ms,
duration_s, git_sha, git_dirty, env_label, host, host_load_1m, python, api_base,
config{}, model, provider, cost_usd, cost_quality(exact|approximate|unreported|n/a),
tokens_in, tokens_out, inputs_digest, outputs_digest, inputs_preview,
outputs_preview, live_target[], evidence_paths[], cleanup_ok, notes[], extra{},
message`. A `$0` API/DB/proc test records `model=null, cost_usd=0, cost_quality=n/a`
faithfully — an absent model is never invented.

## Safety contract (read before writing a test)

1. **Read live prod; do not mutate it.** Use `live_db_ro` (opened `mode=ro` — it
   physically cannot write) and `live_api` (refuses non-GET without
   `allow_write=True`).
2. Any row/file a test creates is tagged with the `livesim_ns` fixture value and
   removed by the test's own cleanup; call `livesim.cleanup(ok)` to record it.
3. Destructive or schema-mutating logic runs against an **isolated scratch DB**
   (copy the live DB into `scratch_dir`, or build a fresh one), never the live DB.
4. No `git` mutations (shared worktree). No process signals to anything you did
   not spawn. Reapers are run only in report-only / dry-run mode.
5. LLM probes go through `scripts/livesim/cheap_llm.py::probe()` (LiteLLM → Claude
   CLI haiku). **Never** call a metered Moonshot/Kimi org (billing pause 2026-08-05).

## Fixtures (from `tests/livesim/conftest.py`)

| Fixture | Use |
|---|---|
| `livesim` | `.record(model=,provider=,cost_usd=,tokens_in=,tokens_out=,inputs=,outputs=)`, `.target(*names)`, `.note(str)`, `.cleanup(bool)`, `.evidence(name, content)`, `.extra(**kv)` |
| `live_api` | `.get(path)` / `.request(method, path, body=, allow_write=)` → `(status, json_or_text, headers)` against `:8485` |
| `live_db_ro` | read-only `sqlite3.Connection` to the live runtime DB (skips if absent) |
| `livesim_ns` | unique greppable namespace for any created row/file |
| `scratch_dir` | per-test throwaway dir under the run's evidence |

Mark every test `@pytest.mark.livesim` (module-level `pytestmark = pytest.mark.livesim`)
plus one or more type markers: `positive negative boundary concurrency recovery
permission security degradation e2e_live`.

## Test categories (files under `tests/livesim/categories/`)

| File | Subsystem | Live targets |
|---|---|---|
| `test_reaper.py` | process + session reapers (the operator's flagged concern) | proc table, live DB, reaper scripts |
| `test_memory.py` | MemLife create/recall/update/isolation/persistence/conflict, knowledge, vault | live DB (ro), scratch DB |
| `test_context.py` | context capsule digest, manifest, handoff, contamination | code + scratch |
| `test_tools_permissions.py` | toolplane, PreToolUse classifier, approval gate, broker, retries/timeouts | code + live DB (ro) |
| `test_skills.py` | skill discovery/injection/labels/CORAL/versioning | code |
| `test_files_fs.py` | WorkFS root safety, scope/path-containment, sandbox denies, concurrent writes, rollback | code + scratch FS |
| `test_api_endpoints.py` | live routing, auth (401), public GETs, error shapes, OpenAPI drift | live `:8485` |
| `test_database.py` | migration head/integrity, schema, WAL, key tables | live DB (ro) + scratch |
| `test_orchestration.py` | swarm, runner heartbeat, routines firing, spawn queue, approvals, loops | live DB (ro) + `:8485` |
| `test_telemetry_cost.py` | cost/token recording, spend caps, provider usage, ledger integrity | live DB (ro) + code |
| `test_degradation.py` | deps-down (LiteLLM/Kimi), cheap-LLM fallback, event-hub degraded, health fields | `:8485` + cheap_llm |
| `test_security.py` | auth enforcement, path traversal, secret non-exposure, classifier fail-open (OBSERVED, not fixed) | live `:8485` + code |
| `test_e2e_live.py` | dashboard today, board projection, session lifecycle read | live `:8485` |

## Known-open defects tests may target as OBSERVATIONAL negatives (do NOT fix here)

Recorded so a test can *document* them (mark `security`/`negative`, assert the
observed behaviour, and log to the issue file), never repair them this session:

- Approval classifier (`approvals.py`) has an unknown→approve fallthrough
  (fail-open) beyond the listed PoC phrases.
- `board_files.py` N-4 denylist admits `/private/etc` and `/private/tmp`, and the
  F-015 workspace floor admits the production checkout end-to-end (LS-013/LS-014).
  Note: `~/.ssh` is now DENIED at both the denylist and the floor (verified live
  2026-08-06) — the earlier "admits ~/.ssh" claim is stale.
- `OMNIAGENTOS_TRUSTED_HOP_SECRET` unset ⇒ rebuilding `.next-remote` from HEAD
  403s every dashboard API call; `SLACK_WEBHOOK_URL` absent ⇒ approval paging
  never delivers (directly feeds the **max-park** session kills).
- `reflection/apply.py:75-83` YAML-parse-failure wipes unrelated keys.
- A2 session reaper: `max-park` (20m) terminalizes awaiting-approval sessions as
  FAILED; enforce armed by `launch-omniagentos.sh`. **16 real sessions** already
  max-park-killed (see `test_reaper.py::test_live_reaper_kill_evidence`).

## Issue log

Structured findings go to `docs/testing/LIVESIM-ISSUES.yaml` (schema in that file):
each entry has `id, title, severity, category, suspected_subsystem, evidence,
repro, recommended_next_investigation, kind(product|test-infra), status`.

## Reaper tracking

`scripts/livesim/reaper_tracker.py` is an independent read-only observer that
folds reaper decisions (from logs + the live DB `killed_by` attributions) into
`var/livesim/reaper-ledger.jsonl` and prints a summary. It does not modify any
reaper. See `docs/testing/REAPER-TRACKING.md`.
