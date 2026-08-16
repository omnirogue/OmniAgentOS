# ADR-007: Steward autonomy ladder (rung 1) and the comms trust model it depends on

**Status:** accepted · 2026-07-13 · run `20260713-1318-steward-h4` · package p6-suggest


## Decision: the autonomy ladder

The Steward's autonomy grows in named rungs, each gated by `cfg.autonomy.rung`
(`omniagentos/steward/config.py::AutonomyConfig`, default `1`) and each rung a
strict superset of the guarantees below it — a higher rung never removes a
lower rung's gate, it only adds a new, narrower auto-path on top:

- **Rung 1 (this package, shipped now): suggest -> human approve -> existing
  gates.** `omniagentos.steward.suggest.GOAL_RULES` deterministically proposes;
  a human calls `POST /api/suggestions/{id}/approve` with a non-empty,
  non-reserved `approved_by`; that call creates a task+run through
  `create_task_service`/`create_run_service` — the same validated path
  `/api/tasks`/`/api/tasks/{id}/runs` use (design finding D-001) — and the
  resulting run's plan step carries whatever `action_class` the suggestion
  declared, unmodified. The runner/broker approval gates (`policy.yaml`,
  `omniagentos.connectors.broker.HARD_HUMAN_CLASSES`) then fire on that run
  exactly as they would for a run created by hand through the HTTP API. **There
  is no code path in this build that ever calls approve with anything other
  than a real operator's typed identity** — see "no auto-approval identity"
  below.
- **Rung 1.5 (named prerequisite, not built): remote-approve.** Before any
  rung beyond 1 is meaningful, an operator needs to approve from somewhere
  other than a shell in front of this machine — a signed Slack interactive
  action (HMAC-verified `X-Slack-Signature`, matching the pattern comms
  inbound webhooks already use for shared-secret verification) or an authed
  HTTPS endpoint reachable off-host. Rung 1.5 is a prerequisite for 2+, not a
  higher trust rung itself: it changes *where* the human clicks approve, not
  *whether* one must.
- **Rung 2 (config-gated OFF, not built): read-only auto-exec allowlist.** A
  narrow, explicitly allowlisted set of suggestions whose `action_class` is
  `read_only` (never higher — `HARD_HUMAN_CLASSES` and `policy.yaml`'s
  `always_human: true` on `consequential` are untouched by any rung, see
  below) could auto-execute without a human clicking approve. This requires
  outcome-feedback data this build does not yet collect: `record_suggestion_
  outcome` exists (used by dismiss, to store `dismiss_reason`) but nothing yet
  measures whether an APPROVED suggestion's run was actually useful. Turning
  rung 2 on before that measurement loop exists would be auto-executing on
  faith, not evidence — so it stays a documented target, config-gated off
  (`autonomy.rung` would need to reach `2`, and the allowlist itself does not
  exist in code yet).
- **Rung 3+ (blueprint-scoped, not built): container gate.** Per the blueprint
  (see ADR-005's "honesty boundary" section for the same same-uid caveat), any
  rung that runs untrusted-suggested code with less human oversight needs a
  real security boundary (separate uid/namespace), not just a database/config
  gate. That lands with the H4+ container gate the blueprint already names;
  rung 3 is not meaningfully orderable before it exists.

### No auto-approval identity exists in rung 1

`POST /api/suggestions/{id}/approve` requires `approved_by` to be a non-empty
string after stripping whitespace (422 otherwise — FastAPI's own body
validation on `ApproveRequest.approved_by: str = Field(min_length=1)` catches
the missing-field case; the route additionally re-checks after `.strip()` for
whitespace-only values). Beyond "non-empty," the route refuses a small
reserved set of strings a hypothetical future auto-executor might plausibly
identify itself as (`RESERVED_AUTO_IDENTITIES` in
`omniagentos/api/routes/suggestions.py`: `auto`, `system`, `autopilot`,
`scheduler`, `bot`, `steward`, `steward-auto`, matched case-insensitively)
with `409 {"code": "autonomy"}`. **Nothing in this build ever sends one of
those values** — there is no scheduler, cron job, or agent wired up to call
approve automatically. The check exists purely as defense in depth: if rung 2
is ever implemented, its auto-executor must be a *new, reviewed* code path
that explicitly reads `cfg.autonomy.rung >= 2` before calling approve — it
cannot silently reuse today's endpoint with a convenient string, because that
string is already refused. Removing an entry from
`RESERVED_AUTO_IDENTITIES`, like removing an entry from
`broker.HARD_HUMAN_CLASSES` (ADR context below), is meant to be a deliberate,
reviewable source change tied to a rung bump — not a config flag.

### The partial-task caveat

`create_task_service` and `create_run_service` are two separate calls with no
shared transaction between them (by design — `create_run_service` is also
called standalone by `POST /api/tasks/{id}/runs` on an already-existing task,
so it cannot assume it always follows task creation in the same request). If
`create_task_service` succeeds but `create_run_service` then raises
`ApiError` (e.g. a plan the runner/broker would reject), the approve route:

1. Does **not** call `decide_suggestion` — the suggestion stays `open` so an
   operator can inspect what went wrong and retry approval.
2. Does **not** attempt to delete or void the orphaned task, which is left
   sitting in state `ready` (or `queued`, if a race won) and remains visible
   via `GET /api/tasks`. Neither service function exposes a delete/void
   primitive — they are additive-only by design (D-001) — and writing task
   state directly to clean this up would be exactly the bypass this package
   exists to prevent. The residual orphaned task is an accepted, visible
   cost of never having a second, ad hoc write path.
3. Re-surfaces the failure as `502 {"code": "run_creation_failed"}` carrying
   the original `ApiError`'s code/message/detail plus the orphaned
   `task_id`, so a caller can distinguish "this request's own approve/dismiss
   validation failed" (4xx) from "the suggestion was fine but turning it into
   a run failed after a task already exists" (this 502).

In practice this path is not expected to trigger for suggestions produced by
`GOAL_RULES` today (their plans are always a single, valid `agent` step), but
the approve endpoint accepts any stored `proposed_plan_json`, including ones
written directly by a future rule or by hand, so the caveat is load-bearing
for those, not just today's two rules.

## Why deterministic-only suggestions in v1 (the trust-laundering rationale)

`GOAL_RULES` are pure functions over `metric_snapshots` rows already
persisted by the goals/collectors package — no LLM call sits between "data
in Stripe/Meta" and "a suggestion appears." This is a direct consequence of
the comms trust model below: comms messages are two-tier (see next section),
and if suggestion *generation* itself called an LLM over that same untrusted
surface, a forged or manipulated inbound message could get laundered into
what *looks* like a system-authored recommendation ("the Steward decided to
suggest this") when really an external sender chose the Steward's words. A
human approving a suggestion is trusting that the suggestion's existence
reflects the platform's own judgment, not an attacker's. Keeping generation
deterministic (metric arithmetic only, no model in the loop) makes that trust
well-founded for v1; an LLM-authored suggestion rule is a future, explicitly
reviewed addition, not a default.

The approve *action* still reaches an agent (the created run executes a
prompt), but by the time a human clicks approve, they are trusting their own
judgment of the suggestion's rationale/evidence — which they can read, because
it is plain arithmetic over named metrics — not an opaque model's.

## The comms trust model this suggestion engine assumes

Suggestions are goal-rule-only today, not comms-derived, but the platform's
one other place untrusted external content becomes anything resembling
"knowledge" is `omniagentos/comms/` and `omniagentos/steward/quoting.py`, and
any future suggestion rule that reads comms-derived data inherits this model
by construction:

- **Two-tier storage vs. extraction.** Tier 1 (`omniagentos/api/routes/
  comms.py::inbound`) is a hard security boundary: attacker-controlled bytes
  go straight to SQLite (`StewardStore.insert_comms_message`) via pure
  normalization (`omniagentos.comms.normalize`) — no network call, no LLM, no
  Postgres, in that request path, ever. Tier 2
  (`omniagentos.comms.extract_batch` -> `omniagentos.comms.knowledge_bridge.
  extract_message`) is a **separate, offline batch job** that is the *only*
  place in the comms package allowed to touch an LLM or the knowledge
  subsystem.
- **Quarantine + `author_role`.** Tier 2 ingests every comms-derived episode
  at `trust=0.3`, which the knowledge subsystem's database triggers
  unconditionally clamp to `status='quarantined'` (see ADR-005) — a quarantined
  fact can never self-promote to active trust by itself; only a
  non-agent-authored, independently-sourced corroboration can promote it
  (ADR-005's `author_role` trigger-keyed guarantee). Comms content, however
  urgent-sounding or well-formatted, starts and stays at the bottom of that
  trust ladder until an operator or a second independent non-agent source says
  otherwise.
- **`quote_untrusted` at every LLM boundary.** Every field of a comms
  message that could reach an extraction prompt — sender, subject, body, all
  attacker-controlled, per `knowledge_bridge.py`'s own comment ("an email's
  From/Subject are as forgeable as its body") — is wrapped in
  `omniagentos.steward.quoting.quote_untrusted` before it is ever concatenated
  into a prompt, delimiting it as data, never instructions. Any future
  suggestion rule that reads comms content (directly, or via linked facts)
  MUST apply the same `quote_untrusted` delimiter at the point it enters any
  prompt — this package's own `GOAL_RULES` never construct a prompt from
  external content at all (metric numbers only), which is a stronger
  guarantee than quoting, not a weaker one, but it is not one every future
  rule will automatically inherit just by living in the same module.
- **`HARD_HUMAN_CLASSES` untouched.** None of the above changes what an
  approved suggestion's run is *allowed* to do. `omniagentos.connectors.
  broker.HARD_HUMAN_CLASSES` refuses `consequential`-class capability calls in
  code, unconditionally, regardless of what any suggestion, policy config, or
  approval says (`policy.yaml`'s `consequential.always_human: true` is the
  second, independent gate on the same class). A suggestion whose proposed
  plan step carries `action_class: consequential` — see
  `omniagentos.steward.suggest.build_plan`'s comment — flows that class
  through to the run's `plan_json` unmodified, and the runner/broker enforce
  it exactly as they would for any other run; approving the suggestion is not
  a way around either gate, by construction (`build_plan` never rewrites or
  drops `action_class`, and this package's tests
  (`tests/steward/test_suggest_routes.py`) assert the plan_json carries the
  class through and that `omniagentos.policy.evaluate_action` marks
  `consequential` as `requires_approval=True, always_human=True`).

## Consequences

- Suggestion generation quality is bounded by what deterministic arithmetic
  over metric series can express. That is the intended v1 trade-off (see "why
  deterministic-only" above), not an oversight — richer, LLM-assisted
  suggestion rules are an explicit future rung, reviewed for the
  trust-laundering risk they'd reintroduce, not a default extension of
  `GOAL_RULES`.
- An operator who approves a suggestion whose plan is later rejected by
  `create_run_service` will see a `502` and a still-`open` suggestion plus an
  orphaned `ready`/`queued` task (the partial-task caveat above) — this is
  visible, not silent, but it is a real operational residual until/unless a
  future package adds a task-void primitive through the sanctioned service
  layer (not a direct store write).
- Rung 2+ cannot be turned on by editing `configs/steward.yaml` alone even
  once its allowlist code exists: `RESERVED_AUTO_IDENTITIES` and
  `HARD_HUMAN_CLASSES` are both code-level gates a config change cannot move,
  by the same two-independent-gates philosophy `broker.py` already documents
  for capability calls.
