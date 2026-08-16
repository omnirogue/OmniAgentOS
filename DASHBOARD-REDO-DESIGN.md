# Dashboard Redo — design of record (2026-08-14, operator goal)

Operator requirement (verbatim intent): every page must have use; show the 4
companies, the available skills, the GitHub repos, and the tests; remove
anything unused. Terminal-first operator — the dashboard must beat `osq` on
information density or a page has no reason to exist.

## Ground truth (from the 5-scout recon, 2026-08-14)

- Serving DB is `var/runtime/state.sqlite3` (LIVE: board 3,710; alerts,
  comms, decisions, briefings, revenue, banking, company_goals, sessions).
  `var/omniagentos.db` is a July decoy — never read it.
- FastAPI :8485 already serves: `/api/orgdims/companies` (6 companies +
  products), `/api/company-goals`, `/api/revenue` (daily totals),
  `/api/banking`, `/api/skills` (54-skill library from ~/.claude/skills),
  `/api/board`, `/api/sessions`, `/api/alerts`, `/api/briefings`,
  `/api/approvals`, `/api/accounts`, `/api/dashboard/today`.
- The REAL production loop (var/loopqueue + tmux + launchd + git) has zero
  dashboard surface — the Status page fixes that by reading files directly
  from Next server routes (same box).
- 668 board rows are auto-discovered `[… · external] … ttysNNN` observational
  cards — the "tasks that don't exist". Default-hidden.
- Companies in data: acmeuni, globex, initech, hooli (the 4 brands),
  plus omniagentos (the platform) and personal (0 goals, hidden).

## MERGE AMENDMENT (2026-08-14, reconciling parallel main work — ruled by the coordinator)

Main advanced 51 commits during this lane's build, adding two pages that pass
the every-page-has-use bar and therefore SURVIVE, making the IA **12 pages**:

- `/team` — gained the Developer WorkQueue accountability system (67b8cb2cf:
  commitments, verification tri-state, automation maturity, learning feed).
  The original deletion of /team is REVERSED; main's page + features/team kept
  wholesale.
- `/testing` — the API-backed test observatory (3ee86cfc1 wave, reading PR
  #440's token-gated testobs API). Coexists with this lane's `/tests` (live
  gate trains/CI/landings, receipt-parsed v0) SHORT-TERM: /testing = suite
  analytics, /tests = live operational trains. FOLLOW-UP LANE OF RECORD:
  unify the two into one page reading the testobs API, per
  devtasks/test-observability-design-0814/DESIGN.md phasing (its v1 API
  shipped as PR #440 while this lane was in flight).

## Information architecture — 10 pages, nothing else

| Route | Content | Sources (all live) |
|---|---|---|
| `/` **Status** | loops (tmux liveness + last iteration), gate trains, queue truth (wip/cap/degraded), landings today + last, recycler last action, ALERTS tail, provider accounts strip | NEW Next server route `/api/local/status` reading: tmux, `var/loopqueue/logs/*-loop.log`, `var/log/gate-loop.log`, `var/loopqueue/state/queue.json` (returns only top-level keys to clients; full server-side parse is acceptable — parsing cost measured trivial at one 1MB file per 15s, see F05 ruling below), `git log origin/main`, `var/log/hang-recycler.log`, `var/loopqueue/ALERTS.md`; accounts via existing `/api/accounts` |
| `/companies` | 4 brand cards (AcmeUni, Globex, Initech, Hooli): 7d revenue / ad spend / ROAS (collected), goals (long+short horizon), products, collector health, link to `~/<brand>/Operations` docs. PLUS an OmniAgentOS platform row: goals, products, collector health — **no revenue block by design** (the platform is not a revenue vertical; no data exists to fill it — ruled 2026-08-14 resolving the original sentence's ambiguity) | `/api/orgdims/companies`, `/api/company-goals`, NEW backend GET `/api/revenue/verticals` (per-day per-vertical from `revenue_facts` + `revenue_source_status`) |
| `/board` | existing kanban, PLUS default filter hiding `[* · external] * ttys*` observational cards (toggle "show terminal cards") | existing `/api/board` |
| `/inbox` | rename of `approvals` — approvals, alerts, briefings, decisions tabs (drop dormant Suggestions tab) | existing routes |
| `/sessions` | keep as-is | existing |
| `/cash` | keep; add the `/api/revenue` daily summary atop banking | existing |
| `/skills` | 54-skill library grouped by category, domain-skill badge for the 19 symlinked entries, + repo skills (2) + DB seed section marked "dormant seeds" | existing `/api/skills` + NEW Next local route for repo-skills glob + DB seeds |
| `/repos` | three owners (example-org 55, Globex 8, initech 4): name, visibility, last push; local clones with dirty/no-origin flags (the every-prototype-gets-a-repo violations called out) | NEW Next server route exec'ing `gh repo list` (5-min cache, graceful "gh unavailable" state) + local clone scan |
| `/tests` | latest gate trains (candidate, pass/fail, suite counts, duration, refusal reason), CI runs for Globex/OmniAgentOS, landings/day (7d) | NEW Next server route parsing `var/gate-evidence/records/merge-gate/*.run-*.json` (top-level + steps[]) + `gh run list` cache + git |
| `/files` | keep (filesearch — 99,998 files indexed) | existing |

**Deletions** (route dirs removed, nav entries removed, no redirects kept
except `/approvals` → `/inbox`): agents, artifacts, capabilities, chat, chats,
comms, connections, control-plane, executions, goals, graph, grok,
improvements, knowledge*, lab, memlife, memory, organization, orgdims (folded
into companies), projects, pulse, reliability, revenue (folded into cash),
routines (folded into status), runs, suggestions, swarm, system, team (folded
into board header link), today, updates, vault.
*knowledge: dormant search — remove; files page covers retrieval.

## Hard rules

- Middleware/auth/proxy chain UNTOUCHED (`src/middleware.ts`,
  `api/[...path]/route.ts`, serverProxy, browserCredential).
- New local-read routes live under `src/app/api/local/*` and are READ-ONLY:
  no exec other than `gh` + `git` + `tmux has-session` + `sqlite3 -readonly`
  (read-only, const SQL only — the skills-extra route's use was explicitly
  authorized in its package brief; this line was just lagging) (all with
  timeouts, all failures render as explicit "unavailable" states, never fake
  zeros — favourable-absence is the estate's #1 defect class).
- queue.json (F05 ruling, 2026-08-14, amends the original "top-level keys
  ONLY — never parse items[]" line above): clients are returned only
  top-level keys; a full server-side `JSON.parse` of the whole file is
  acceptable — parsing cost measured trivial at one 1MB file per 15s (see
  `assembleStatus.ts`'s `loadQueue()` comment).
- Backend change is EXACTLY ONE new public GET (`/api/revenue/verticals`) in
  the existing revenue router file; nothing else server-side.
- Keep the existing design system (globals.css, components) — this is an IA
  rebuild, not a re-skin.
- e2e: update `dashboard/e2e/*.spec.ts` for the new nav; board/team/product
  specs stay green; add smoke specs for the 5 new/rewired pages.

## Package split (disjoint ownership)

- P0 shell: layout.tsx nav + page deletions + /approvals→/inbox rename + root page replacement (ONE owner, lands first)
- P1 status: `api/local/status` route + `/` page
- P2 companies: `/companies` page + backend `/api/revenue/verticals`
- P3 skills: `/skills` rewire + `api/local/skills-extra`
- P4 repos: `/repos` + `api/local/repos`
- P5 tests: `/tests` + `api/local/tests`
- P6 board filter: board page filter toggle
- P7 e2e: spec updates (after P0-P6)

Validation ladder per package: `npm run lint`, `npm run build`, targeted
`npm run test:e2e -- <spec>` where feasible; full ladder + visual pass at
integration.

DEFERRAL OF RECORD (2026-08-14): the local Playwright chromium run was blocked
by a foreign process holding the hardcoded e2e port 3002 (operator's editor);
the browser-level e2e for the fix-round commits runs as the `production`
project against the live :3003 site immediately post-deploy. Until that run is
green, the browser-level check for those commits is OUTSTANDING, not passed. Deploy: build with `OMNIAGENTOS_NEXT_DIST_DIR=.next-remote`,
`launchctl kickstart -k gui/$UID/com.omniagentos.dashboard`, verify via
the Caddy front door.
