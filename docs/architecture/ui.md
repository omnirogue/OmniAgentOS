# UI — Next.js mission-control dashboard

The dashboard (`dashboard/`, Next.js 15) is the single operator surface for
OmniAgentOS: live kanban board, approval workflow, session monitoring, and — as of
V2 — a full "Company" section for the reliability system and agent org. Every
mutation proxies through the token-gated FastAPI backend via a same-origin route
handler; the browser NEVER holds the session token.

## Shell & navigation (`dashboard/src/design/AppShell.tsx`)

`NAV_SECTIONS` is the single source of truth for the sidebar: sections have a `key`,
`label`, `icon`, optional `href`, and `groups` of links. V2 added a **"Company"**
section (`key: "company"`) with three links: `/reliability`, `/improvements`,
`/judges`. `/organization` was removed from the nav when it became a redirect to
`/orgdims`. Active-section highlighting matches the longest matching
`href` prefix; `IMPLICIT_SECTIONS` maps sub-routes (e.g. a run detail page) back to
their parent nav section when there's no exact link match.

## Design system (`dashboard/src/design/`)

Frozen tokens (`tokens.ts` TypeScript constants + `theme.css` CSS custom properties)
— features use `var(--token)`, never raw hex. Shared components (`Card`, `Table`,
`Tabs`, `Badge`, `Pill`, `StatusDot`, `Dialog`, `EmptyState`, `ErrorState`, `Toast`,
charts) are reused across every page, including the four new V2 pages.

## API proxy (`dashboard/src/app/api/[...path]/route.ts`, `src/lib/serverProxy.ts`)

Same-origin catch-all route; `API_BASE = ""` in `lib/contracts.ts` — the browser
always calls `/api/...` on its own origin, never the loopback FastAPI port directly.
New backend routes need their GET paths added to `isAuthorizedReadPath`'s allowlist;
mutating V2 decision routes (approve/reject/apply/rollback, `PUT /api/autonomy`) are
injected with BOTH the session token and the separate `X-Autonomy-Token` server-side —
neither token ever reaches browser code (governance.md, "capability separation").

## Live events (`dashboard/src/lib/useEvents.ts`, `contracts.ts` `EVENT_TYPES`)

`EventSource`-based SSE hook with `Last-Event-ID` cursor recovery (persisted in
`sessionStorage`, key `omniagentos:lastEventId`); a `resync` frame forces a full
list refresh if the client reconnects more than 500 events behind. Reconnect delay
is a hardcoded 1.5s. `EVENT_TYPES`/`useEvents.ts` signatures are FROZEN — V2 does NOT
extend them.

### V2 reliability events are a SEPARATE registry

`dashboard/src/lib/reliabilityContracts.ts` defines its own additive event-type list
(`reliability.event`, `improvement.updated`, `audit.completed`, `autonomy.changed`,
`org.updated` — see `contracts/reliability-api.md`), and
`dashboard/src/lib/useReliabilityEvents.ts` is a DEDICATED hook (own `EventSource`
subscription) consumed by the four Company pages. This keeps the frozen
`useEvents`/`EVENT_TYPES` contract untouched while giving V2 its own event stream
(design §9/§10, codex #15).

## Pages (`dashboard/src/app/`)

Established (pre-V2): `board`, `approvals`, `sessions`, `runs`/`activity`, `alerts`,
`briefing`, `skills`, `lab`, `tournaments`, `leaderboard`, `capabilities`, `knowledge`,
`vault`, `revenue`, `cash`, `goals`, `routines`, `accounts`, `comms`, `projects`,
`chat`, `files`, `agents`, `executions`, `updates`, `suggestions`, `design` (component
gallery).

V2 "Company" section:
- `/reliability` — health tiles (open critical/warning, last audit verdicts, watch
  heartbeat), events feed, `POST /api/reliability/events/{id}/ignore`.
- `/improvements` — the approval queue: risk badge, origin, judge votes, sandbox
  summary, Approve/Reject/Apply/Rollback actions, mode widget (approve ⇄ auto per
  scope, L4 shown as immutable).
- `/orgdims` — the org-dimensions surface (companies, workstreams, loops, portfolio and matrix views). It calls `/api/orgdims/*` (`dashboard/src/features/orgdims/api.ts`), NOT `/api/org`. `/organization` is now only a legacy-alias `redirect()` to it and has no client of its own.
- `/judges` — panel composition, availability, recent votes, agreement stats.

Cockpit Front Door:
- `/pulse` — the cockpit front door, displaying vital signals, active routines, capability snapshots, and system health at a glance.

Other Pages (new or previously missing from documentation):
- `/chats` — Chat v2 (unified thread and channel view).
- `/connections` — the integration estate control center.
- `/graph` — graph and CBM (Cognitive Budget Manager) visual drilldown.
- `/grok` — Grok operator plane (grants and interactions inbox).
- `/memlife` — memory lifetime / wiring controller.
- `/memory` — durable long-term memory view (with retired `/artifacts` redirected here).
- `/portfolio` — projects portfolio layout.
- `/system` — system overview mapping.
- `/swarm` — swarm pipeline and orchestration dispatcher.

## Gotchas worth knowing before touching this layer

- Session token file (`var/secrets/sessions-token`) must stay 0600 — a sandboxed agent
  cannot read it, closing the control-plane escape vector.
- `hierarchy.py`'s `/api/projects/tree` MUST be registered before the parametrized
  `/api/projects/{id}` route or `"tree"` matches the `{id}` parameter.
- Board 5s poll + SSE is intentional: SSE drives real-time updates; the 30s poll for
  sessions/approvals is a safety net when the stream is down, not the primary path.
- Event cursor persisted in `sessionStorage` loses on tab close or private mode; a
  `resync` frame is synthesized if the stream reconnects with a gap larger than the
  500-event replay window.

## Governance/Reliability/Organization eyebrow labels

New V2 pages establish three page-header "eyebrow" categories (small label above the
page title, `PageHeader` component) so operators can tell at a glance which safety
tier a page belongs to: **Governance** (autonomy mode, risk thresholds — nothing here
is directly mutable without the dual-token gate), **Reliability** (events/audits —
read-mostly, one mutation: ignore), **Organization** — retained as an eyebrow label only: `/organization` is now a redirect to `/orgdims` and its agent-tree/agent-request client was removed. A future `features/org/` tree view over `GET /api/org/tree` would restore the surface.
This mirrors the Tier P/S split in `governance.md` at the UI layer, even though the
enforcement itself lives entirely server-side.

## Build status of the V2 pages (as of this writing)

All four Company pages (`reliability`, `improvements`, `judges`; `organization` is retired to a redirect) plus
`dashboard/src/lib/reliabilityContracts.ts` and `useReliabilityEvents.ts` are
IMPLEMENTED (package W8). They call the `/api/reliability`, `/api/improvements`,
and `/api/autonomy` routes described in `contracts/reliability-api.md` —
those routes exist under `omniagentos/api/routes/` but registration into
`omniagentos/api/main.py` is integration-wave (W10) work, so a fresh checkout may
still 404 until that wiring lands.

## Notes (human)
