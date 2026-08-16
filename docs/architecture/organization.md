# Organization — the agent org hierarchy (`omniagentos/company/`)

V2 gives OmniAgentOS an explicit company structure: a CTO, department VPs, managers,
specialists, and a judge panel, all persisted as `agents` rows (the existing
`agents` table from `004_collab.sql`, extended additively by migration
`042_reliability_company.sql` with `org_unit_id`, `org_role`, `title`, `charter`,
`schedule_json`, `harness`, `enabled`, `vault_note_path`) plus a new `org_units`
table for the company/department/team tree. `omniagentos/company/{org,departments,
cto,requests}.py` are ALL IMPLEMENTED as of this writing (package W6).

## Seed (`company/org.py::seed()`)

Idempotent: one `org_units` row for the company, one per department, and `agents`
rows for the CTO, 4 VPs, the Chief Judge + 3 judges (named by model family), and
each department's manager + key specialists. Harness spread matches each role:
managers/leadership on `cli-claude`, engineering specialists on `cli-codex`,
research/cost-analysis specialists on `cli-grok`, the judge panel on its three
default families (see `reliability.md`), one benchmark specialist on a fallback
family. Every NEWLY created agent gets a vault note under `vault/org/<slug>.md`;
re-running `seed()` never clobbers an existing agent row or an existing note's
human-edited sections (`write_note`'s human-section merge). Callable via
`python -m omniagentos.company seed` or the one-time integrator run (§12, W10).

### The 11 departments (§7, exact list)

Engineering, Research, Operations, Infrastructure, Security, QA, Architecture,
Product, Customer Experience, Cost Optimization, Benchmark Lab — each with a
one-line charter (see `org.py::DEPARTMENTS`).

### Roles (`taxonomy.OrgRole`)

`cto | vp | manager | specialist | judge`. VPs cover Engineering, Research, Product,
Operations (§7); the judge panel is `Chief Judge` + 3 judges distinct from the
department structure (see `reliability.md`'s judge-panel section for the
model-family assignment, which is what `judge` agents' `harness` column encodes).

## Department reviews (`company/departments.py::run_department_reviews()`)

Each department manager's harness runs a SCOPED health-review prompt over its own
domain data (recent scorecards — capped at 5, `_CONTEXT_SCORECARD_LIMIT` — recent
events capped at 15, recent improvements capped at 50/shown 5) plus living-arch-doc
context (`_archdocs_context([dept.name])` → `omniagentos.archdocs.context.
load_arch_context`, degrading to `""` rather than crashing if archdocs isn't
importable — see `knowledge.md`). Output is a ranked list of improvement proposals
(`origin="department"`, validated against `_VALID_KINDS`/`_VALID_RISK`, schema
`_REVIEW_OUTPUT_SCHEMA`), created via `_create_improvement_from_proposal()`.
Budget-capped tokens; runs SEQUENTIALLY (one department at a time) inside the
twice-daily audit sequence (`reliability.md`); a single department's failure
(adapter error, timeout, garbage output) is caught and logged — it never aborts the
loop.

## CTO reviews (`company/cto.py`)

`daily_review()` — quick backlog re-prioritization using a deterministic
`ranking_score_for(kind, risk_level, attempt, created_at)` (severity/recency/attempt
count weighted; `origin="cto"`). `weekly_review()` — deep architecture pass
(`origin="weekly"`, `_weekly_context()` pulls scorecard trends + doc-staleness
signal from `archdocs.staleness`), writing a roadmap note under `vault/org/cto/`.

## Agent-request loop (`company/requests.py`)

the operator (or the dashboard "Request new agent" form) posts a `description` →
`create()` inserts an `agent_requests` row (`pending`) → `_design_prompt()` +
`_sanitize_design()` produce `design_json` (name, title, department, role, harness,
model, charter, schedule, expertise) via `cli-claude` → an `improvement` row
(`kind="new_agent"`, L2 per `governance.md`) is created → judges → human approval
queue → `mark_approved()`/`approve_and_create()` creates the real `agents` row +
vault note, or `reject()` terminates the request. `create_agent_from_request()` is
the mechanical agent-creation step, called only after approval. The whole loop is
callable via API (`POST /api/org/agent-requests`, see `contracts/reliability-api.md`)
or CLI (`python -m omniagentos.company request "..."`).

## Autonomy scope resolution (store-level, `reliability/store.py::resolve_autonomy`)

Most-specific-scope-wins: `agent > department > kind > global`. The seeded row is
`aut_global / global / '' / approve / max_auto_risk=0` — every improvement queues
for human decision until the operator explicitly opts a scope into `auto` (per-scope, via
`PUT /api/autonomy`, itself a Tier-P, dual-token-gated route — see
`governance.md`).

## API + dashboard surface

`GET /api/org/tree`, `GET /api/org/agents[.../{id}[/activity]]`, `POST /api/org/
agents/{id}/toggle`, `POST|GET /api/org/agent-requests[...approve|reject]` — see
`contracts/reliability-api.md` for the frozen shape. The `/organization` dashboard
page (`ui.md`) renders the tree + agent cards + the request form, live-updated via
`useReliabilityEvents()`'s `org.updated` event.

## Notes (human)
