# Feature flags

Environment knobs that change runtime behavior. Flags are **rollout controls**, not
authorization: grant, policy, and human-approval requirements still apply when a
feature is enabled. Modes and defaults live in code (often `off` / opt-in).

**Launch-path defaults (2026-07-28):** `scripts/launch-env.sh` (sourced by api/runner/migrate
recipes) now defaults:
- **Capability flags ON:** `OMNIAGENTOS_CASCADE=1`, `OMNIAGENTOS_REFLEXION=1`,
  `OMNIAGENTOS_METACOG_MEMORY_PROMOTION=enforce`, `OMNIAGENTOS_SWARM_EXECUTE=1`,
  `OMNIAGENTOS_MEMORY=1`. Previously deferred; operator explicitly decided 2026-07-28.
  (See `HANDOFF/2026-07-28-knowledge-flip.md` "Deliberately NOT flipped" for context.)
- **Parallel-execution correctness:** `OMNIAGENTOS_SCOPE_LOCKS=enforce` (promoted from
  `shadow` — with swarm worktrees on and parallel lanes writing, path locking is a
  correctness mechanism, not a rollout control), `OMNIAGENTOS_SWARM_WORKTREES=1`.
- **Measurement ramp:** Eight three-rung flags (task shape router, tool catalog, tool
  scheduler, autonomy lease, champion routing, lab curation, allowed providers,
  reflection re-arm) remain at `shadow` for data accumulation.

Code defaults are unchanged; presets always win (e.g., `OMNIAGENTOS_KNOWLEDGE=0` keeps
knowledge dark). pytest never sources this file, and simulation mode returns before the
flag section, so neither inherits the launch-path flips.

| Flag | Purpose | Notes |
| --- | --- | --- |
| `OMNIAGENTOS_SCOPE_LOCKS` | Cross-lane path lock mode | `off` / `shadow` / `enforce` (see `omniagentos.scope.config`); **LAUNCH-PATH: enforce** (code default: off) |
| `OMNIAGENTOS_PARALLELISM_CONFIG` | Path to parallelism YAML | Defaults for scope TTL / starvation / mode |
| `OMNIAGENTOS_KNOWLEDGE` | Postgres+pgvector recall/ingest | **LAUNCH-PATH: 1** (code default: off); opt-in; needs DSN family below |
| `OMNIAGENTOS_KNOWLEDGE_PG_DSN` | Knowledge Postgres DSN | Required when knowledge is on |
| `OMNIAGENTOS_MEMORY` | Conversation history injection | **LAUNCH-PATH: 1** (code default: off); opt-in |
| `OMNIAGENTOS_CASCADE` | Verified model-routing cascade | **LAUNCH-PATH: 1** (code default: off); opt-in; operator flipped 2026-07-28 |
| `OMNIAGENTOS_REFLEXION` | Outcome-driven reflection loop | **LAUNCH-PATH: 1** (code default: off); opt-in; operator flipped 2026-07-28 |
| `OMNIAGENTOS_ACCOUNT_POOL` | Multi-account Claude config-dir pooling | Opt-in |
| `OMNIAGENTOS_SWARM_EXECUTE` | Allow swarm workers to execute | **LAUNCH-PATH: 1** (code default: off); daemon env; operator flipped 2026-07-28 |
| `OMNIAGENTOS_SWARM_WORKTREES` | Per-task worktree isolation | **LAUNCH-PATH: 1** (code default: off); opt-in merge model |
| `OMNIAGENTOS_REAPER_ENFORCE` | Idle/zombie reaper kills | Enforcing by default in the launcher; unset/`0` is dry-run/debug |
| `OMNIAGENTOS_SESSION_MAX_PARK_MINUTES` | Ceiling on `awaiting_approval` | Default **20**; past it the session fails closed with an honest reason (never auto-approved). `0` disables. Enforced regardless of the reaper's dry-run flag |
| `OMNIAGENTOS_API_BASE_URL` | Control-plane URL stamped into a bridge session's hook | Normally derived from `OMNIAGENTOS_API_PORT` by the supervisor; set explicitly only when the API is not on `127.0.0.1:$OMNIAGENTOS_API_PORT`. `OMNI_SESSION_API_URL` still overrides it inside the hook |
| `OMNIAGENTOS_COMPRESS` | Prompt bulk compression mode | e.g. `basic` |
| `OMNIAGENTOS_VAULT_AUTOCOMMIT` | Vault note git auto-commit | Opt-in |
| `OMNIAGENTOS_CURATOR_LIVE_AGENT` | Curator narrative pass | Opt-in |
| `OMNIAGENTOS_BUDGET_ENFORCEMENT` | Hard budget refusal | Safety control |
| `OMNIAGENTOS_REQUIRE_PG` | Fail tests/suite without Postgres | CI / full suite |
| `OMNIAGENTOS_METACOG_MODE` | Master metacog auto-action gate | `off` / `shadow` / `enforce` (default **enforce** / LIVE) |
| `OMNIAGENTOS_METACOG_MEMORY_PROMOTION` | Memory promote behavior | **LAUNCH-PATH: enforce** (code default: off); `off` / `shadow` / `enforce`; operator flipped 2026-07-28 |
| `OMNIAGENTOS_METACOG_STRATEGY_SWITCH` | Strategy switch application | `off` / `shadow` / `enforce` |
| `OMNIAGENTOS_METACOG_SKILL_CANARY` | Skill synthesis rollout | `off` / `shadow` / `canary` / `enforce` |
| `OMNIAGENTOS_METACOG_CONFIG` | Override path to metacog YAML | Defaults to `configs/metacog.yaml` |
| `OMNIAGENTOS_METACOG_ARTIFACTS_ROOT` | Content-addressed artifact disk root | Defaults to `var/metacog-artifacts` |
| `OMNIAGENTOS_DISCOVER_EXTERNAL` | Multi-provider process discovery → board | Default on; set `0` to disable |
| Graph Runtime V2 | Typed diamond graphs (`/api/graph/*`) | API **LIVE**; swarm provision hooks when `OMNIAGENTOS_GRAPH_RUNTIME=1` |
| `OMNIAGENTOS_GRAPH_RUNTIME` | Link diamond graph to multi-task swarm provision | Default off; set `1`/`true` to enable (Phase 1.2) |
| Cognitive Budget Manager | Progressive escalation (`/api/cbm/*`) | **LIVE** by default (migration 063); quality+ETAR; spawn applies effort |
| `OMNIAGENTOS_TASK_SHAPE_ROUTER` | Task-shape route arbiter (topology + worker count) | `off` / `shadow` / `enforce`; default **off**. LAUNCH-PATH: shadow. Env beats `task_shape_router:` in `configs/routing.yaml` beats off (resolution mirrors `omniagentos.scope.config`) |
| `OMNIAGENTOS_TOOL_CATALOG` | Typed tool catalog exposure | `off` / `shadow` / `enforce`; default **off**. LAUNCH-PATH: shadow. (resolution mirrors `omniagentos.scope.config`) |
| `OMNIAGENTOS_TOOL_SCHEDULER` | Tool-aware scheduling / admission | `off` / `shadow` / `enforce`; default **off**. LAUNCH-PATH: shadow. (resolution mirrors `omniagentos.scope.config`) |
| `OMNIAGENTOS_AUTONOMY_LEASE` | Bounded autonomy leases for long-running work | `off` / `shadow` / `enforce`; default **off**. LAUNCH-PATH: shadow. (resolution mirrors `omniagentos.scope.config`) |
| `OMNIAGENTOS_INSESSION_FANOUT` | In-session fan-out grants for claude swarm workers (PKG-INSESSION-FANOUT) | Overrides `configs/swarm.yaml insession.enabled` in BOTH directions; shipped **LIVE** (config `enabled: true` since 2026-07-27; env `0` is the kill switch). Grants are coordinator-issued against the same six request-split guards; every Task call consumes a grant slot server-side (PreToolUse hook), and live budgets count against `max_agents_per_account` |
| `OMNIAGENTOS_CHAMPION_ROUTING_MODE` | Apply promoted lab `model_assignment` champions to CBM/routing recommendations | `off` / `shadow` / `enforce`; default **off**. LAUNCH-PATH: shadow. Lazy optional import of `omniagentos.lab.runtime` — baseline routing preserved when the accessor is absent (Lane D D1) |
| `OMNIAGENTOS_LAB_CURATION_MODE` | Observe-first launchd curation loop for `propose_experiments` | `off` / `shadow` / `enforce`; default **off**. LAUNCH-PATH: shadow. Observe-only in shadow; no auto-promote (Lane D D2) |
| `OMNIAGENTOS_REFLECTION_REARM_MODE` | Re-arm reflection-nightly/-watchdog after exit-126 repair | `off` / `shadow` / `enforce`; default **off**. LAUNCH-PATH: shadow. Observe-only re-arm + one-shadow-week hold before any `observe_only=False` discussion (Lane D D3) |
| `OMNIAGENTOS_ALLOWED_PROVIDERS_MODE` | Per-run `allowed_providers` enforcement (dispatch → worker rotation → planner fallback) | `off` / `shadow` / `enforce`; default **off**. LAUNCH-PATH: shadow. Shadow logs violations without blocking; enforce filters and fails closed with `ProviderNotAllowed` (Lane D D5) |
| `OMNIAGENTOS_QUICK_PROJECT_ROUTING_MODE` | Quick-intake registry-backed project routing (A1, `omniagentos.intake.service`) | `off` / `shadow` / `enforce`; default **off**. LAUNCH-PATH: shadow (operator decision 2026-08-05, after seeding five projects' `root_dirs`). Shadow logs the registry routing decision and applies NOTHING — persisted rows and delivered worker prompts stay byte-identical to `off`, pinned by `tests/intake/test_phase1_project_routing.py::test_shadow_is_byte_identical_to_off_in_stored_state_and_delivered_inputs`. `build_preflight` and its `preflight_json` write are ENFORCE-only (they persist a classification and `swarm/spawn.py` reads them into the brief, so running them in shadow changed execution — Lane A followup D1, `devtasks/phase1-ledger.md`). Enforce dispatches through the matched project's `root_dirs[0]` |
| `OMNIAGENTOS_CONTEXT_CAPSULE` | Context Capsule v1 shadow manifest over the delivered worker brief | `off` / `shadow` / `enforce`; default **off**. Observes the final assembled prompt and writes `context-capsule.json` under the task evidence dir. **Never changes a delivered prompt byte.** V1: `enforce` behaves exactly as `shadow` (observe + write only; no enforcing behaviour yet). Resolution mirrors `coral_context_mode` (invalid/absent → off). |

In every three-rung flag above, `off` means the feature is inert and behavior is byte-identical
to the flag being absent; `shadow` computes and records the decision but applies nothing; only
`enforce` changes behavior. (Exception: `OMNIAGENTOS_CONTEXT_CAPSULE` V1 — `enforce` is still observe-only.)

See also `docs/architecture/grok-upgrade.md`, `docs/METACOGNITION-UPGRADE-PLAN.md`, `docs/ORG-DIMENSIONS-UPGRADE-PLAN.md`, `docs/GRAPH-RUNTIME-AND-CBM.md`, and `ARCHI.md`.
