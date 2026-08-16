# Governance — risk gates, approvals, policy, audit trail

The governance layer is the cross-package, FROZEN safety contract every other
subsystem builds on top of: a 6-tier risk classification, AD-15 finance-only
approvals, an append-only audit event log, and budget enforcement. V2 (see
`reliability.md`) adds a SECOND, stricter gate on top — it never lowers or bypasses
this floor.

## ActionClass (`omniagentos/contracts.py`, frozen)

Six tiers, ordering is trust-significant (lowest → highest risk):
`read_only < sandboxed_creation < internal_reversible < external_reversible <
consequential < irreversible`. `IRREVERSIBLE` is the AUTO routing floor, not the
final approval policy. The AD-15 resolver parks money writes, customer writes, and
production or unresolved deletes; permanently refuses bank writes; and auto-approves
proven isolated local-temp deletes. Secret reads park for a human (H3). Since C1
(2026-08-04) a CONSEQUENTIAL-or-higher request also parks on two enumerated
NON-finance surfaces — production deploys and remote destructive commands (see
"Non-finance park-list" below); every other remote or non-finance command is still
not an approval hard stop and remains subject to normal execution-contract scope.
`is_hard_stop(action_class)` remains the frozen class-floor predicate imported by other
packages; changing `HARD_STOP_CLASSES` breaks auto-provisioning, which is precisely why
C1 was implemented as an additive resolver step rather than a wider class floor.

## Non-finance park-list (C1, `orchestrator/approvals.py`)

`park_list_surface(request)` runs ONLY where the AD-15 finance classification returned
"auto-approve". It parks when **both** halves hold:

1. **class floor** — `action_class` is `consequential` or `irreversible` (a separate
   predicate over `_PARK_LIST_CLASS_FLOOR`; `HARD_STOP_CLASSES` is neither read nor
   rebound). An unknown/malformed class fails **closed**, as `is_hard_stop` does.
2. **enumerated surface** — a **production deploy** (production-default deploy tools,
   preview-default tools with an explicit production marker, registry publishes, and a
   deploy verb at a command position aimed at a named production target) or a **remote
   destructive command** (an enumerated destroy/halt executable, or an enumerated
   destructive subcommand/flag, inside a payload of the module's existing remote
   definition `_is_remote_command`).

Matching is **structural**: a token only counts at a command position — the executable
of a statement, or the executable of a transport's payload — so `grep -rn 'terraform
apply' docs/` and `ssh host 'grep -r kill /etc'` do not park. A preview deploy, a
`--dry-run`, a `terraform plan`, a `git push` (deliberately not enumerated), a plain
`scp`, and a LOCAL destructive command all keep their previous auto-approve behaviour.
Any error while evaluating the park-list parks with trigger `park-list-unevaluable`.
Decisions carry their own stable audit prefix, `parked per non-finance park-list
(class: …; trigger: production-deploy|remote-destructive|park-list-unevaluable; scope: …)`;
the `HardStop` category stays the frozen finance-only `delete`.

## Policy (`omniagentos/policy/`, `configs/policy.yaml`)

`PolicyConfig` (loaded via `load_policy()`) carries `PolicyMode.AUTO|SUPERVISED`,
per-`ActionClass` overrides, and tool allowlists. `evaluate_action(action_class, cfg)`
returns a `PolicyDecision` (`requires_approval`, `always_human`, `reason`).
`approval_satisfies_gate()` validates a decided approval against its action class AND
expiry (`expires_at` deadline; an APPROVED approval past expiry is still unusable for
resume). `classify_shell` (`policy/shell.py`) classifies shell commands into the same
6-tier scale for the Session Bridge's PreToolUse hook and for V2 sandbox command
refusal (see `reliability.md` §"Sandbox").

Config changes to `policy.yaml` do NOT re-evaluate already-enqueued runs (`is_hard_stop`
+ `HARD_STOP_CLASSES` are imported at module load) — a runner restart, or a
config-version hash check, is required after a policy change.

## Approvals (`approvals` table)

State machine `pending → approved|rejected|expired`, dual-mode (run-level and
step-level via `step_seq`). Orchestrator decisions use truthful stable reasons:
`auto-approved per finance-only policy (...)`, `parked per finance-only policy
(...)`, or `parked per non-finance park-list (...)` for a C1 park; risk-shaped auto
paths are never described as a safe action. Each prefix is mapped to a denial code in
`toolplane/session.py::_DENIAL_CODES` — a new prefix must be registered there too. Bank-write
refusals create no satisfiable approval path. Session-linked approvals with `expires_at <= now`
atomically expire during `decide_approval()`; runner-approvals are unaffected. The
runner samples the clock per parked run (not once per batch) — an approval that
expires mid-service fails the run immediately.

## Ledger, events, budgets, secrets (`omniagentos/ledger/`, `contracts.Events`)

- Every mutation writes an `events` row (`actor`, `action`, `target_type`, `target_id`,
  `payload_json`); `events.id` (AUTOINCREMENT) is both the SSE cursor and the
  `Last-Event-ID` client-visible field. `Events` (frozen enum, `contracts.py`) is the
  ONLY source of valid `type` values for events-table rows; SSE-only synthesized types
  (`worker.heartbeat`, `session.updated`) are never persisted.
- Idempotency receipts (`idempotency` table) key broker tokens by `run_id` (never
  `agent_id`, to prevent inference of grants); tokens are single-run and expire —
  reuse after expiry is a hard error.
- Secret registry (`omniagentos/policy/secrets.py`) identifies secret-shaped reads for
  truthful audit; AD-15 does not park them. It case-folds on macOS but not Linux.
- Budgets (`omniagentos/budget/`): `wall_ms`, `tokens`, `cost_usd`, `turns` per run;
  `max_turns` is process-local only (not yet a DB column).

## V2 governance additions (`omniagentos/reliability/governance.py`, design §5b/§6)

**Status: designed, not yet implemented** (package W4 — `pipeline.py` +
`governance.py` + `configs/governance.yaml` — is pending as of this writing; check
`omniagentos/reliability/` for `governance.py`'s presence before relying on it).

V2 layers a SECOND, improvement-specific risk gate on top of ActionClass, never below
it — "reliability apply is subordinate to BOTH gates ... stricter wins" (design §5).

- **Risk levels L1–L4** (`taxonomy.ChangeRisk`): L1 = prompt wording, retry timing,
  logging, non-governance docs. L2 = model selection, fallback order, agent
  instructions, workflow sequencing, `new_agent`. L3 = everything else, including ALL
  Tier-S code (below) — always human. L4 = billing/payments/auth/security/
  permissions/data-deletion/frozen-schema/Tier-P paths — always unanimous-judge AND
  human, never auto.
- **Classified on the AUTHORITATIVE sandbox `git diff`** (name-status + content,
  realpath-resolved), never on the proposal's declared metadata — a diff touching any
  undeclared path, or a new file/module shadowing a protected path, forces L4.
- **Tier P** (forced L4, never auto): `reliability/governance.py`,
  `configs/{governance,reliability,policy}.yaml`, `omniagentos/policy/**`,
  `omniagentos/contracts.py`, `contracts/**`, `reliability/judges.py`,
  `omniagentos/notifications/**`, `api/routes/autonomy.py`, `scripts/reliability/**`,
  launchd plists, the migrations directory.
- **Tier S** (forced ≥L3, always human): the rest of `omniagentos/reliability/**`
  (detector/analyzer/pipeline/sandbox/recovery/scorecards/memory), `omniagentos/
  company/**`, **`omniagentos/archdocs/**`, `ARCHI.md`, `docs/architecture/**`** (this
  doc included — a docs-improvement is never self-applied), `api/routes/
  {reliability,improvements,org}.py`, the steward dead-man rule.
- **Governance knobs live ONLY in protected `configs/governance.yaml`**: quorum flags,
  panel families/fallbacks, observation-window durations, KPI-regression thresholds,
  risk maps — parsed with hardcoded validation floors (panel ≥3 distinct families,
  observation window ≥6h). `configs/reliability.yaml` is cosmetic-only (paths, batch
  sizes, token caps) and is Tier P anyway.
- **Capability separation**: the pipeline never holds an API token; human decision
  routes (`PUT /api/autonomy`, improvement approve/reject/apply/rollback) require a
  distinct `X-Autonomy-Token` (`var/secrets/autonomy-token`, 0600) IN ADDITION to the
  session gate.
- **Tamper-evident log**: `reliability_log` is hash-chained, append-only (no store
  UPDATE/DELETE) — every improvement/autonomy/governance transition appends a row.
- Additional invariants (enforced + tested at W4): pipeline can never write
  `autonomy_settings`; `critical` notifications are exempt from cooldown suppression;
  the risk classifier only ever raises, never lowers; `mark_acted` alone can never
  clear a critical notification (a decision row is required).

## Architectural holds and deferred decisions (HOLDS.yaml)

**Enforced by:** `HOLDS.yaml` (machine-readable registry), ratified in `DECISIONS.md`
(signed decisions).

All active project holds, deferred decisions, and phased implementation constraints
are tracked in `HOLDS.yaml` at the repository root. This registry is the single source
of truth for automation that must enforce scope limits, spending caps, or mode
restrictions — e.g., loop spending gating (D6), experiment spend budgets (D13),
security hardening sequencing (D3), or capability restrictions (D4).

**Key holds (as of 2026-08-04):**

- **D3 (G3 Security Timing)**: Store-backed grant-proof hardening proceeds independently;
  broader G3 broker work remains parked.
- **D4 (Self-Improvement Authority)**: Authority limited to candidate/test operation;
  no autonomous merge or publication grants.
- **D5 (Engine Authority)**: Option G deferred; retain distributed authority and
  correlation-envelope `ExecutionRef`.
- **D6 (Loop Activation)**: Loops dark by default until Phase D; $1/tick ceiling,
  $10/day initial cap.
- **D13 (Experiments)**: Offline/shadow-only, $0 autonomous spend, 24-hour max duration,
  hard-stop on any bound violation.

**Registries for similar constraints:** See also `.gate-skip-allowlist.yaml` (for
test-suite conditional skips) and `.mcp.json` (MCP server allowlists). Holds and
gate-skip-allowlists serve different purposes — holds are architectural scope limits,
while gate-skip-allowlists are test-coverage allowances.

**How to lift a hold:** A hold can only be lifted by a new the operator signature in
`DECISIONS.md` (via the established ADR process in `docs/adr/`). Changing HOLDS.yaml
alone is not sufficient; the signature is the authority.

## Notes (human)
