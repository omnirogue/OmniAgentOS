---
slug: long-horizon-delivery
category: Orchestration
subcategory: Long-Horizon Delivery
title: Long-horizon real-world delivery (objective → live outcome → learning)
summary: >-
  Repeatable, autonomous runbook for taking a broad real-world objective all the
  way to a verified live outcome — request missing API scopes at start, research,
  plan, get human authorization for consequential actions, provision infrastructure,
  build, deploy over HTTPS, verify in production, recover from failure, and turn the
  finished run into durable memory and a reusable skill.
status: active
preferred_method: this-runbook
---

# Long-horizon real-world delivery

**When to use.** The objective is broad and outcome-shaped ("stand up a live site
that does X on its own domain"), needs real external systems (registrar, DNS, a
server, auth, email, Slack), and is not done until the thing actually exists and is
verified. This is the runbook the estate follows for that whole class of work, so
every such task runs the same way and gets better each time.

**Prime directive.** Certify on demonstrated mechanisms, never on plausible
reasoning, code existing, or an agent saying it worked. A step is done when a test
or an observation proves it.

---

## Phase 0 — Request the APIs you don't have, AT START

Before any work, resolve the capability gap. Do not discover a missing credential
halfway through a purchase.

1. Enumerate the connectors this objective needs (typical full-stack web build:
   `namecom` (registrar), `cloudflare` (DNS), `vultr` (server), `clerk` (auth),
   `gmail`/`gmail_ownera` (email), `slack_post` (team channel)). The registry is
   `configs/connectors.yaml`; every external call resolves a declared capability —
   there is no generic HTTP escape hatch.
2. For each, check whether the credential resolves. If a read/auto-tier scope is
   missing, request it through the wired scope flow:
   `POST /api/provision/{project_id}/request` (`omniagentos/provision/service.py::request_scope`) —
   a read/auto scope is auto-granted; a hard-stop scope is escalated, never granted here.
3. For a typed capability ask (a specific tool, a key-scope group, an extra agent),
   park a request: `POST /api/capability-requests` → surfaces to the operator in the
   dashboard approvals feed → operator decides
   (`POST /api/capability-requests/{id}/decide`). The service is
   `omniagentos/provision/capability_requests.py`; it fail-closes on unaddressable ids
   and records a durable `hard_rejected` with a reason code for a refused-but-real ask.
4. **If a credential simply does not exist yet** (e.g. no Name.com account/token,
   or the Vultr key is only under an aliased env name), STOP and post ONE clear
   request to the operator naming: the exact env var(s) needed, why, and the exact
   line to add to `~/.config/omni/connections.env`. State a recommended action.
   Do not fabricate, do not proceed past the step that needs it.

**Absence is never favorable.** A dark connector, a missing key, a missing grant is
a finding that blocks the dependent step — never a silent skip.

---

## Phase 1 — Understand & research

- Restate the objective as an acceptance contract: what must be TRUE at the end
  (a URL that returns 200 over HTTPS, an auth flow that logs a user in, an email
  that sends). This becomes the deliverable spec checked in Phase 8.
- Research with the read-only web capability (`web.fetch` / `web.search`, broker-
  authorized to the research lane per `tests/connectors/test_web_read.py`): current
  prices, real specs, competitive landscape. Cite what you read; do not answer real-
  world questions from memory.

## Phase 2 — Plan & decompose

- Plan with the intake planner (`omniagentos/intake/planner.py::plan_goal` → a
  `ProjectPlan`; Fable at HIGH, escalating to MAX for complex goals, degrading to a
  deterministic heuristic offline). Decompose into non-conflicting lanes with disjoint
  owned paths.
- Carry long-horizon state in the board task + attempt chain + `WORKBOOK.md`
  (`omniagentos/longhaul/`, `var/longhaul/<task_id>/WORKBOOK.md`): goal, acceptance,
  plan, progress, decisions, `## Status`. Continuity is the workbook, never transcript
  replay.

## Phase 3 — Parallel execution

- Fan out isolated workers (swarm/worktrees). Heavy build offloads to the fleet or to
  API-backed coder subagents; the serving box keeps only daemons + interactive work.
  Gate every heavy spawn on load (`load_1m ÷ cores`: <0.6 go, 0.6–0.8 halve/offload,
  >0.8 never spawn locally).

## Phase 4 — Human authorization for consequential actions

Every money/DNS/server/customer-visible action is `action_class: consequential`. The
broker hard-blocks it unattended in code (`HARD_HUMAN_CLASSES`,
`omniagentos/connectors/broker.py`), independent of `policy.yaml`. The block is
satisfied only by a **bounded durable grant** the broker validates and consumes at
call time.

Two ways a grant comes to exist — both preserve the hard-block, neither bypasses it:

1. **Owner authorized the spend in the prompt (default for the one-prompt run).**
   When the run's ORIGINAL human goal carries the owner's explicit spend
   authorization with an amount ("I authorize spending up to $50…"),
   `omniagentos/grants/run_authorization.py` mints bounded, expiring, audited grants
   at run start for exactly the consequential capabilities the task declared in
   Phase 0 — and the run then proceeds through those actions WITHOUT stopping to
   re-ask. Concretely, the run's start step calls, with the owner's VERBATIM prompt
   as `human_goal` and `authorized_by` from the authenticated principal:
   `parse_spend_authorization(...)` then `mint_run_grants(store, authorization=..., asks=[...], project_id, run_id, now_iso)`.
   Each ask is a `CapabilityAsk(capability, action_class="consequential", max_spend_usd, max_actions, target_set)`
   and **every cap must be POSITIVE** (a `max_spend_usd <= 0` ask is refused — a
   zero-cap grant is un-consumable). A workable default ask set for a web build:
   `namecom.purchase $20/1`, `vultr.instance_create $20/1`, `cloudflare.dns_write $1/5`,
   `gmail.send $1/2 target_set=[owner email]`, `slack_post.channel_message $1/3`
   (sum $43 fits a $50 authorization). A broadcast capability (`gmail.send`) requires
   a non-empty `target_set`. Safety is structural: the total mintable spend can never exceed
   `min(prompt cap, ABSOLUTE_MAX_CEILING_USD, ceiling)`, every grant expires within
   a few hours and is action-capped, only the authenticated OWNER principal can
   authorize (a sub-agent brief cannot), and every mint/refusal is logged to
   `var/grants/run_authorizations.jsonl`. This is exactly what "explicit in-prompt
   spend instruction is enough authorization" means — it is honored by minting
   real bounded grants, not by weakening the gate.
2. **No prompt authorization (or over-ceiling / unusual action).** The action parks
   and surfaces to the operator, who mints a bounded grant in the dashboard
   (`POST /api/omni-ops/grants`; UI = GrantsPanel). Anything outside the authorized
   capability set or over the cap always falls back to this path.

Narrate whichever path fired — both are the safety model working, not a workaround.

- Domain purchase: `namecom.purchase` (consequential).
- DNS: `cloudflare.dns_write` / `namecom.dns_write` (consequential).
- Server: `vultr.instance_create` (consequential; `vultr.instance_destroy` too).
- Team post: `slack_post.channel_message` (consequential — the allowlist cannot see
  the channel body field, so an unattended post could reach an external human).

## Phase 5 — Provision infrastructure

- Buy the domain (`namecom.purchase`, under grant), set DNS
  (`cloudflare.dns_write`) pointing at the server IP.
- Create the server (`vultr.instance_create`, under grant). Record it; a
  `vultr.instance_destroy` for teardown is also consequential.

## Phase 6 — Server setup & deploy over HTTPS

Use `omniagentos/deploy/`:
- `plan_server_bootstrap(ServerSpec)` → an idempotent script that installs Caddy
  (automatic Let's Encrypt HTTPS — never `auto_https off`), the runtime, a deploy
  user, and the firewall.
- `plan_app_deploy(AppSpec)` → sync/clone, build, a systemd unit running as the
  deploy user, and a Caddy site block reverse-proxying `domain` → the app port so
  Caddy auto-issues the certificate.
- `execute_plan(plan, runner)` — runs each step through an injected SSH/broker
  runner (consequential remote ops go through the SSH policy lane / a grant). The
  library never executes anything itself.

## Phase 7 — Auth, email, Slack

- Wire Clerk (`clerk.*`, brand-split reads; secrets present).
- Email via `gmail.send` / `gmail_ownera.send` (consequential, under grant).
- Announce in the team channel via `slack_post.channel_message` (the channel must
  exist and the bot must be a member — verify with a read first; creating a channel
  is an operator action).

## Phase 8 — Production verification

- Hit the live URL and assert HTTPS 200 + expected content (the health-check step in
  `plan_app_deploy`; `web.fetch` for an independent read).
- Run the deliverable spec built in Phase 1 through a fail-closed checker (the
  `omniagentos/intake/deliverable_checks.py` shape: produced_output / file_exists /
  must_include / must_not_include; a missing/empty spec is a FAIL, never a pass).
- Browser-level checks go to a `browser-operator` subagent so screenshots never enter
  the main context.

## Phase 9 — Failure recovery

- Session death → exactly one successor (longhaul engine); steering never lost.
- A consequential step refused twice on the same input: STOP — find what the gate is
  reading; do not retry the unchanged input, do not escalate the model. Change the
  action (different lineage, enumerate the sibling set, fix the input).
- A deploy health-check red under host overload is re-run once before it is
  investigated.

## Phase 10 — Learning (the second half of the benchmark)

The run is not finished until the estate is permanently better at this class of work:
- **Memory:** append a dated one-line lesson to `var/memories/OmniAgentOS/MEMORY.md`;
  durable facts flow to the knowledge base via the run-reflection hook.
- **Skill:** extract a reusable skill from the completed run
  (`omniagentos/selfimprove/curator.py::curate_sessions`; enable via the curator
  builtin job). Update THIS skill with anything new that worked.
- **Replay proof:** the next run of this class must show a `skill_usage` row
  (`omniagentos/skills/usage.py`, migration 131) proving this skill was injected and
  reused — reuse is proven from data, not asserted.
- **Verification:** a real A/B or holdout eval, not shadow — a landed `eval_results`
  row, not `applied: []`.

---

## Certification gate

End every run of this class with exactly one of **CERTIFIED** / **NOT YET CERTIFIED**,
backed by evidence per phase. NOT YET CERTIFIED must list every remaining blocker and
its owner (human vs. buildable), and repair every blocker that can be safely repaired.

## Register / update this skill

This file is the source of truth. To (re)register into the skills DB so the runner
injects it into future long-horizon briefs:
`PUT /api/skills` (slug `long-horizon-delivery`, category `Orchestration`) with this
body, or run the vault-index pass. Related memory: `[[longhorizon-demo-cert]]`,
`[[two-separate-landing-rails]]`.
