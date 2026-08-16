# Reliability — V2 self-improving failure detection, recovery & judge/pipeline

The V2 reliability system extends the execution layer (never replaces it — principle
1, `V2-DESIGN.md`): a detector reads runner/session/approval state from the DB only,
files a `reliability_events` row, dispatches safe temporary recovery, and — via a
sandboxed, 3-judge-panel-gated pipeline — proposes and (conditionally) auto-applies
fixes. Everything routes through the SAME `ActionClass`/`is_hard_stop()` floor
(`governance.md`) as a stricter, ADDITIONAL gate, never a replacement for it.

**Build status as of this writing** (packages per `V2-DESIGN.md` §12 — check current
file presence before relying on any of this, since this is a living doc):
schema+store (W1), judges+sandbox (W3), analyzer+audit+scorecards+cli (W5), company
(W6), API routes (W7), and dashboard (W8) are IMPLEMENTED under
`omniagentos/reliability/`, `omniagentos/company/`, `omniagentos/api/routes/`, and
`dashboard/src/app/{reliability,improvements,organization,judges}`. Detector+recovery+
memory (W2) and pipeline+governance (W4) — i.e. `reliability/{detector,recovery,
memory,pipeline,governance}.py` and `configs/governance.yaml` — were PENDING at the
time this doc was written; the CLI (`reliability/cli.py`) already refuses `recover`/
`apply`/`rollback` with "owned by another reliability worker" until they land.

## Schema (migration `042_reliability_company.sql`)

`org_units`, `agents` (+org/role/harness columns, additive), `reliability_events`,
`improvements`, `improvement_votes`, `rollback_points`, `reliability_audits`,
`scorecards`, `autonomy_settings` (seeded `global/approve/max_auto_risk=0`),
`agent_requests`, `reliability_state` (cursors/leases/apply-journal),
`reliability_log` (hash-chained, append-only). All additive — no frozen table is
altered beyond the `agents` `ALTER TABLE ADD COLUMN`s.

## Store (`reliability/store.py`, frozen contract `reliability/contracts.py`)

`SqliteReliabilityStore` is the ONLY way any other V2 package touches this schema —
its `ReliabilityStore` Protocol in `contracts.py` is frozen (W2–W9 code against it,
never edit it). Durability contracts (design §5b), all enforced in `store.py`:

1. **CAS transitions**: `transition_improvement(id, expected_status, new_status,
   actor, detail)` — one `BEGIN IMMEDIATE` transaction: `UPDATE ... WHERE status=? AND
   version=?`, bump `version`, append a `reliability_log` row atomically. Zero rows
   updated raises `TransitionConflict` — callers re-read, never retry blind.
2. **Occurrence-key idempotency**: `insert_event()` is `INSERT OR IGNORE` keyed on
   `occurrence_key` — overlapping watch cycles can't double-file the same failure.
3. **Lease, not lock**: `acquire_lease`/`renew_lease`/`release_lease`/
   `reclaim_stale_lease` — a fencing-token row in `reliability_state`, held ONLY
   during apply/rollback, never across an observation window; stale leases are
   reclaimed.
4. **SQLITE_BUSY retry**: every write retries the WHOLE transaction on
   BUSY/LOCKED with bounded exponential backoff + jitter (`_retry_on_busy`).
5. **Cursor semantics**: `get_watch_cursor`/`advance_watch_cursor` — the cursor
   advances ONLY after all events in a scan window are durably written.
6. **Hash-chained log**: `reliability_log` — `hash = sha256(prev_hash +
   canonical_json(row))`; the store exposes NO update/delete for it, for
   `improvement_votes`, or for `reliability_audits`.
7. **Panel-attempt idempotency**: `insert_vote()` is `UNIQUE(improvement_id,
   panel_attempt_id, model_family)` + `INSERT OR REPLACE` — a retried judge can't
   double-vote.

## Taxonomy (`reliability/taxonomy.py`)

`FailureClass` (21 values: `run_failed`, `session_error`, `tool_error`,
`auth_failure`, `rate_limit`, `timeout`, `finalize_quarantine`, `retry_spike`,
`stale_run`, `cost_spike`, `latency_regression`, `quality_deny`, `approval_expired`,
`account_disabled`, `queue_stall`, `routine_failure`, `output_invalid`,
`loop_detected`, `user_reported`, `doc_stale`, `other`), `Severity`
(`info|warning|critical`), `ChangeRisk` (L1–L4, IntEnum, see `governance.md`),
`ImprovementStatus` (14-state pipeline lifecycle: `proposed → testing → judging →
panel_blocked | awaiting_human → approved → applying → applied → monitoring →
confirmed | rejected | rolling_back → rolled_back | failed | superseded`),
`AuditKind`/`AuditStatus`, `OrgUnitKind`/`OrgRole`, `AutonomyMode`, `VerdictKind`
(`approve|reject|approve_with_conditions|needs_human`), `EventStatus`,
`AgentRequestStatus`.

## Judge panel (`reliability/judges.py`, `configs/reliability.yaml`)

`JudgePanel` seats 3 DISTINCT model families in parallel (`ThreadPoolExecutor`,
per-judge timeout ⇒ that judge's vote becomes `needs_human`, never an indefinite
wait); default families are Anthropic/OpenAI/xAI, `FALLBACK_FAMILIES =
["cli-gemini", "cli-kimi"]`. **Fail-closed**: if 3 genuinely-distinct,
non-degraded families can't be seated, `PanelBlockedError` — the improvement goes
`panel_blocked`, never a substituted/degraded auto-apply. Judges see the raw
sandbox diff as the PRIMARY artifact plus fenced evidence (`quote_untrusted()`
wrapping, SEC-O-005) — never each other's votes, and the analyzer's prose is
clearly secondary framing. Strict JSON verdict + 11 scores; unparseable after one
retry ⇒ `needs_human`.

## Sandbox (`reliability/sandbox.py`)

`Sandbox` applies a proposal's declared diff in a git worktree under `var/sandbox/`
and runs pipeline-selected pytest — `validate_proposal_commands()` REJECTS any
proposal that tries to smuggle a shell command; `plan[]` in the proposal contract
is narrative only, the pipeline never executes it (Red-team B1). `SandboxResult`
captures pass/fail + the diff for the judges and the (still-pending) risk
classifier.

## Analyzer, audit, scorecards, CLI (`reliability/{analyzer,audit,report,
scorecards,cli}.py`)

- `analyzer.analyze_event(...)` drafts a root-cause + proposal via an LLM adapter
  (`_default_adapter`, `cli-claude` by default), `validate_proposal()` enforces the
  proposal contract shape, and a deterministic `_risk()` pre-classifies severity
  before the (pending) governance classifier has final say.
- `audit.py`: `watch`/`twice_daily`/`daily_summary`/`weekly_architecture`, all
  dispatching through `_scheduled()`; `run_audit(store, kind, once, vault_dir)` opens
  a `reliability_audits` row, computes a window (`_window(hours)`), and writes a
  vault report via `report.write_audit_report()`.
  scheduling.md`) — `python -m omniagentos.reliability {watch|audit|daily|weekly}
  [--once] [--db PATH]`; `recover`/`apply`/`rollback` currently raise ("owned by
  another reliability worker") pending W2/W4.
- `scorecards.py`: `aggregate_metrics()` + `compute_scorecard(store, subject_type,
  subject_id, window, ...)` upserts into the `scorecards` table (agent/model/
  harness/department/skill/routine × day/week).

## Notification policy (design §11, reusing the sealed 6-kind `notifications` enum)

Critical failure ⇒ immediate `alert`; proposal awaiting ⇒ `approval`; applied ⇒
`done`; rollback/quarantine ⇒ `escalation`; low-risk batches into the daily `info`
summary. No new notification kind — V2 maps onto the existing frozen enum
(migration `030_notifications.sql`).

## API + SSE (frozen contract: `contracts/reliability-api.md`)

`omniagentos/api/routes/{reliability,improvements,org,autonomy}.py` — see that
contract doc for the full endpoint/payload/SSE-event table; it is the FROZEN
surface other V2 packages and the dashboard code against (marked FROZEN
2026-07-23, wave V2). All four route modules are IMPLEMENTED as of this writing;
registration into `omniagentos/api/main.py` (`include_router`) is integration-wave
(W10) work.

## What's still pending (W2, W4, W10 at time of writing)

- `reliability/detector.py` — pure rule functions scanning `runs`/`steps`/
  `sessions`/`approvals`/`claude_accounts`/`routine_runs` for failure candidates.
- `reliability/recovery.py` — allow-listed safe recovery dispatch (requeue-once for
  transient classes, never money/delete).
- `reliability/memory.py` — improvement-memory search (don't re-propose a fix that
  already failed/rolled back) + confirmed-fix → skill writeback.
- `reliability/pipeline.py` + `reliability/governance.py` + `configs/
  governance.yaml` — the CAS state-machine driver, Tier P/S enforcement, diff-based
  risk classifier, single-flight apply lease, crash-resume.
- `api/main.py` registration, `scripts/reliability/` plists + installer, the
  autonomy-token secret, the dead-man's-switch rule in the steward alerts monitor,
  and the one-time org seed run — all integration wave (W10).

## Notes (human)
