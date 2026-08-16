# Design: Governed Improvement Proposal UI (evidence surface)

**Status:** one plan (architect, iteration 3 repair)  
**Branch:** `project/grok-improvements-evidence-0812`  
**Base product HEAD (no feature yet):** `9cd238bb` / frozen audit `10579954` — product delta vs `2b04062f` is empty except loopdeck notes  
**Objective:** Show evidence, expected benefit, scope, risks, approval decision, execution goal/branch, verification, and rollback on the improvements UI; **never allow self-approval**.  
**Budget:** ≤10 product files (source under `dashboard/src/**`, not tests). Prefer ≤6.  
**Out of scope:** backend Python routes, reflection-loop cards, autonomy mode redesign, SEPARATE-PRODUCT sibling, any push/merge.

---

## 1. Problem (verified)

| Gap | Evidence |
|-----|----------|
| Card shows only title/summary/risk badge/origin/status/votes/sandbox pass | `dashboard/src/app/improvements/page.tsx` `ImprovementCard` |
| Required fields not rendered | `proposal_json`, `root_cause`, `created_by`, `decided_by`, `rollback_point_id`, `applied_sha`, plan/files unused |
| Self-approval open | Approve hardcodes `decided_by: "human"`; no compare to `created_by` |
| No objective tests | No `page.test.tsx`; `api.test.ts` only smoke-posts `decided_by` for pull |
| Dirty tree | Uncommitted `typescript` pin `5.8.2` + lock + untracked `.loopdeck-repository.json` — **not feature work** |

Acceptance (`dashboard` npm test/typecheck/build + `git diff --check`) is **necessary but not sufficient**. Objective proof = new deterministic tests + product delta bound to a clean SHA.

---

## 2. One design (not options)

### 2.1 Visual system

Reuse **existing** design primitives only (`Card`, `Badge`, `Button`, `Dialog`, `DefinitionList`, `CodeBlock`, `Input`, `Section`, `Tabs`, `EmptyState`, `ErrorState`, `Loading`, `Page`, `PageHeader`).  
Extend `features/reliability/improvements.module.css` with small section/list classes — **no inline `style={{}}`**, no new color tokens, no new page chrome. Match steward `SuggestionCard` friction pattern (name-at-decision) without restyling the whole page.

### 2.2 Field projection contract

Introduce a **pure** mapper (no React, no fetch) so UI and tests share one truth:

```ts
// GovernedProposalView — stable labels for the card
{
  evidence: { items: string[]; emptyLabel: "Not provided" }
  expectedBenefit: string          // never empty string in UI — use "Not provided"
  scope: { changeType: string; paths: string[]; kind: string; origin: string }
  risks: { level: number; reasons: string[]; restartRequired: boolean | null; narrative: string }
  approval: { createdBy: string; decidedBy: string | null; status: string }
  execution: { goal: string; branch: string; plan: string[] }
  verification: { sandboxPassed: boolean | null; report: string; riskTier: string; votes: Array<[string,string]> }
  rollback: { pointId: string | null; appliedSha: string | null }
}
```

**Source map (API already returns these; no backend change):**

| UI block | Primary sources | Fallbacks / rules |
|----------|-----------------|-------------------|
| **Evidence** | `root_cause`; `proposal_json.plan[]` (as narrative steps); `proposal_json.repro`; `before`/`after` (root or proposal) | If all empty → show section with "Not provided". Never dump entire raw JSON as the only content. |
| **Expected benefit** | `proposal_json.expected_impact` \|\| `expected_benefit` \|\| `predicted_impact` | Else `summary` if non-empty; else "Not provided". |
| **Scope** | `proposal_json.change_type`; paths from `files[]` (dict.path or string) + `config_edits[].path`; `kind`; `origin` | Paths deduped; empty paths → "No paths declared". |
| **Risks** | `risk_level`; `sandbox_json.risk_reasons[]`; `proposal_json.risk_hint` / `risk_parse_error`; `restart_required` | Keep existing `RiskBadge`; **add** reason list / narrative under it. |
| **Approval decision** | `created_by`, `decided_by`, `status` | Always show Created by + Decided by (or "Pending"). Operator must type identity to act. |
| **Execution goal / branch** | Goal: `proposal_json.goal` \|\| `title`. Branch: `proposal_json.branch` \|\| `proposal_json.git_branch` \|\| "Not provided". Plan: `proposal_json.plan[]` | CSI payloads may lack branch — still show the row. |
| **Verification** | `sandbox_json.passed`, `report`, `risk_tier`/`risk_level`; existing votes panel | Preserve Passed/Failed badge; add report excerpt (truncate ~500 chars) via `CodeBlock` or muted text. |
| **Rollback** | `rollback_point_id`, `applied_sha` | Always show both rows (null → "—"). Keep Rollback button when status allows. |

Mapper lives in `dashboard/src/features/reliability/proposalDisplay.ts` and is the **only** place that reads nested `proposal_json` keys for this page.

### 2.3 Self-approval (hard rule)

**Contract:** an approve action is forbidden when the operator identity equals the proposal author.

```ts
// pure, case-insensitive, trim; empty decided_by is also forbidden for approve
function isSelfApproval(decidedBy: string, createdBy: string): boolean
function assertCanDecide(
  action: "approve" | "reject" | "rollback" | "pull",
  decidedBy: string,
  createdBy: string,
): void
// For approve: throw if !decidedBy.trim() OR isSelfApproval(...)
// For reject/rollback/pull: require non-empty decidedBy; self-approval check applies to approve only
//   (rejecting your own proposal is allowed; approving it is not)
```

**UI wiring (page):**

1. Replace one-click Approve that posts `decided_by: "human"` with a **Dialog** (or extend confirm dialog) requiring `Input` label `Your name (decided_by)`.
2. Disable Confirm while empty.
3. On confirm Approve: if `isSelfApproval(name, improvement.created_by)`, set page error `"Self-approval is not allowed"` and **do not** call the API.
4. Pass the typed name into `decideImprovement(id, action, { decided_by: name })`.
5. Show `created_by` on the card so the operator can see why a match is blocked.

**API client (`decideImprovement`):**

- Keep body shape `{ decided_by, note? }` unchanged (backend already requires `decided_by: str`).
- **Do not** default to `"operator"` / `"human"` when caller omits identity for `approve` — require explicit `decided_by` for approve (breaking the silent default for approve only is intentional). Other actions may keep a default only if tests still need it; prefer requiring identity for all four decide actions for consistency.
- Optional belt: if a future overload passes `created_by`, refuse self-approve inside the client. Primary enforcement is the page + pure helper (testable without fetch).

**Explicit non-claim:** backend `POST /api/improvements/{id}/approve` still accepts any `decided_by` string. True server-side ban is **out of this loop’s UI budget**. Residual risk: raw API client can still self-approve. Document in handoff; do not fake E2E “server enforces” tests.

### 2.4 Component structure

Keep tabs/autonomy/reflection as-is. Change only the governed improvement card + decide handlers.

```
ImprovementsPage
  └── ImprovementCard(improvement, operator handlers)
        ├── header (title, RiskBadge, origin, status) — existing
        ├── summary — existing
        ├── Governed sections (new): Evidence | Benefit | Scope | Risks | Approval | Goal/Branch | Verification | Rollback
        ├── votes panel — existing
        └── actions — Approve/Reject open identity dialog; Pull/Rollback require identity too
```

**data-testid** (for deterministic tests, stable):

- `improvement-card`
- `gov-evidence`, `gov-benefit`, `gov-scope`, `gov-risks`
- `gov-approval`, `gov-execution`, `gov-verification`, `gov-rollback`
- `approve-dialog`, `decided-by-input`, `self-approval-error`

---

## 3. Files to touch (order) — max product budget

| # | File | Kind | Action |
|---|------|------|--------|
| 1 | `dashboard/src/features/reliability/proposalDisplay.test.ts` | **test** | Write first: fixture → view mapping for every objective field + empty fallbacks |
| 2 | `dashboard/src/features/reliability/proposalDisplay.ts` | **product** | Implement pure mapper + `isSelfApproval` / `assertCanDecide` |
| 3 | `dashboard/src/features/reliability/api.test.ts` | **test** | Extend: approve requires decided_by; no silent human/operator default for approve; body includes typed identity |
| 4 | `dashboard/src/features/reliability/api.ts` | **product** | Require `decided_by` for approve; stop hardcoding defaults that mask identity |
| 5 | `dashboard/src/app/improvements/page.test.tsx` | **test** | **Write failing first:** render fixture card; assert all `gov-*` sections; approve blocked when name === created_by; approve enabled path posts decided_by |
| 6 | `dashboard/src/app/improvements/page.tsx` | **product** | Wire sections + identity dialog + self-approval guard |
| 7 | `dashboard/src/features/reliability/improvements.module.css` | **product** | Section spacing, definition rows if needed (use design `DefinitionList` first) |
| 8 | `dashboard/src/features/reliability/index.ts` | **product** | Re-export mapper helpers if other modules need them (skip if unused) |

**Product file count:** 4–5 (under 10).  
**Do not touch:** `package.json` / lock for the TS pin, Python backend, reflection feature, design system primitives, ARCHI via hand-edit.

**Workspace hygiene (before claiming SHA):**

- Discard uncommitted `dashboard/package.json` + `package-lock.json` typescript pin **unless** typecheck proves HEAD’s `^5.7.0` fails (it should not; node_modules already resolves).
- Leave untracked `.loopdeck-repository.json` uncommitted or delete if accidental.
- Acceptance needs `dashboard/node_modules` present so `npm test` / `npm run typecheck` resolve `vitest`/`tsc` via npm’s PATH. If missing: `cd dashboard && npm ci` (or `npm install`) once — not part of the acceptance script, but a host precondition. Prior “vitest: not found” under exit-0 logs is **environment**, not a product defect.

---

## 4. Test plan (TDD — tests before UI)

### 4.1 Unit — `proposalDisplay.test.ts`

Fixture improvement with full `proposal_json` + `sandbox_json` + identities. Assert:

- evidence includes root_cause and plan lines  
- benefit text  
- scope paths contain declared files  
- risks list includes sandbox reasons  
- approval shows created_by / decided_by  
- execution goal/branch  
- verification passed + report  
- rollback ids  
- empty proposal still yields every section key with "Not provided" / "—" (no missing keys)

### 4.2 Unit — self-approval

- `isSelfApproval("the operator", "owner") === true`  
- `isSelfApproval("operator", "csi") === false`  
- `assertCanDecide("approve", "agent-x", "agent-x")` throws  
- `assertCanDecide("approve", "human", "agent-x")` ok  
- `assertCanDecide("approve", "  ", "agent-x")` throws (empty)

### 4.3 API — `api.test.ts`

- `decideImprovement(id, "approve")` without decided_by rejects (or does not default to human/operator)  
- with `{ decided_by: "owner" }` posts that body  

### 4.4 Page — `page.test.tsx`

Mock `fetchImprovements` / `decideImprovement` / autonomy / reflection / reliability events / poll (mirror `reliability/page.test.tsx` style).

1. Renders one `awaiting_human` item → all `gov-*` testids present with expected text from fixture.  
2. Open Approve → enter `created_by` value → Confirm → **no** `decideImprovement` call; error or dialog message contains self-approval.  
3. Enter different name → `decideImprovement` called with that `decided_by`.  

These tests make acceptance **objective-coupled**: green suite without the UI is impossible once tests land.

---

## 5. How the acceptance command will pass

```bash
bash -lc 'set -o pipefail; test -f AGENTS.md; test -f SEPARATE-PRODUCT.md; cd dashboard; npm test; npm run typecheck; npm run build; cd ..; git diff --check'
```

| Step | How it passes |
|------|----------------|
| `AGENTS.md` / `SEPARATE-PRODUCT.md` | Present; no edits required |
| `npm test` | Existing suite + new proposalDisplay/api/page tests green after implementation; run via **npm** so `node_modules/.bin` is on PATH |
| `npm run typecheck` | New pure TS + page props typecheck; no `any` leaks required beyond existing patterns |
| `npm run build` | Next build of `/improvements` with client component; no new routes |
| `git diff --check` | No whitespace errors in committed diffs; **working tree should be clean of accidental package pins** when freezing SHA |

**Load rule:** if `load_avg_1m > host_perf_cores`, re-run acceptance **once**; do not thrash. Exit 2 / unchanged refusal → stop and diagnose.

**After green:** write `.loopdeck/test-evidence.json` with command, exit code, HEAD SHA, and which tests cover objective fields. Then independent audit + Fable on that SHA.

---

## 6. Implementation order (numbered subtasks)

1. **Hygiene** — discard unrelated package pin; confirm `node_modules/.bin/{vitest,tsc}` exist.  
2. **TDD mapper** — write `proposalDisplay.test.ts` (red) → implement `proposalDisplay.ts` (green).  
3. **TDD self-approval** — tests for `isSelfApproval` / `assertCanDecide` in same module.  
4. **TDD API identity** — extend `api.test.ts` (red) → adjust `decideImprovement` (green).  
5. **TDD page** — write `page.test.tsx` asserting fields + self-approval block (red).  
6. **UI** — render governed sections with design primitives + CSS; identity dialog; wire guards (green).  
7. **Focused verify** — `cd dashboard && npx vitest run src/app/improvements src/features/reliability/proposalDisplay src/features/reliability/api.test.ts` then full acceptance.  
8. **Evidence + commit** — `test-evidence.json`, coherent commits, no push.  
9. **Audit/Fable** — independent non-writer roles on final clean SHA.

---

## 7. Explicit non-goals / risks

- No backend self-approval enforcement this loop (UI+client only).  
- No schema change to `proposal_json` producers; mapper is tolerant of sparse CSI/orgdims shapes.  
- Reflection tab unchanged (different model).  
- Do not treat baseline-green acceptance without new tests as success.  
- Do not commit secrets, `var/`, or `.loopdeck-repository.json` unless coordinator owns it.

---

## 8. Success criteria (objective)

A reviewer at the final SHA can:

1. Open `page.test.tsx` / `proposalDisplay.test.ts` and see assertions for every required field + self-approval denial.  
2. Open the improvements card code and find the eight governed sections bound to API fields via `proposalDisplay`.  
3. Confirm Approve cannot fire when `decided_by` equals `created_by`.  
4. See acceptance command green and `git status` clean (or only intentional loopdeck evidence files).  
5. See ≤10 product files in `git diff` vs base for this feature.
