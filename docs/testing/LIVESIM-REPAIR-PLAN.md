# LiveSim repair plan — OmniAgentOS defects

Grounded fix plan for the defects LiveSim logged, from a 5-investigator pass that
read the **actual product code** behind each issue. **Nothing here is applied** —
this session did not touch product code. Each fix names the exact file(s), the
change, the risk, and the LiveSim test that proves it (many observational tests
are designed to flip red→green, or their assertion inverts, when the defect is
fixed — that flip is the acceptance signal).

> Two corrections the deep read surfaced, up front:
> 1. **Issue-ID drift.** The prose in `LIVESIM-FINAL-REPORT.md` used a few IDs that
>    don't match the canonical `LIVESIM-ISSUES.yaml` (the programmatic append
>    renumbered them). This plan keys everything to the **canonical YAML id +
>    subsystem**, not the report's labels.
> 2. **Three "defects" are actually TEST defects in my own observational tests, not
>    product bugs** — corrected below (acceptance-rate "drift", the "912 unsettled
>    routine_runs", and the event-hub tailer). The true product-defect count is
>    lower than the raw 27.

Effort key: **S** ≤ half a day · **M** ~1–2 days · **L** multi-day / needs design.

---

## Batch 1 — P0: restore the dashboard (do first; it's dark for users)

**D-1 · Dashboard trusted-hop 403 (LS-003) · M · mixed (ops + 1 code fix)**
Root cause is a **deployment gap, not a broken guard** — the guard is correct and
fail-closed by design. `OMNIAGENTOS_TRUSTED_HOP_SECRET` is set nowhere, and nothing
injects the `X-Omni-Trusted-Hop` header, so `dashboard/src/middleware.ts` 403s every
public read and `serverProxy.ts::requireTrustedHop` 403s every authorized call.
(Direct `:8485` works because FastAPI has no hop check — it's session-token gated;
"set it in the API env" is a red herring, this is a **dashboard runtime** concern.)
- **Fix (recommended, no security change):** generate a secret to `var/secrets/trusted-hop-secret` (0600); add a `configs/dashboard-caddy/Caddyfile` that reverse-proxies to `:3003` and, in `header_up`, strips any inbound `X-Omni-Trusted-Hop` then injects `{env.OMNIAGENTOS_TRUSTED_HOP_SECRET}`; in `scripts/launch-supervised.sh::_dashboard()` export the secret into `npm run start`'s env and add a `_caddy()` supervised target with the same secret. Operators (and LiveSim) browse the Caddy port, never `:3003`.
- **Code correctness gap to fix regardless:** `serverProxy.ts::requireTrustedHop` has **no** equivalent of `middleware.ts::devEscapeActive()`, so even the sanctioned local escape can't restore authorized reads/mutations — mirror the escape into `serverProxy.ts` (gated on `NODE_ENV!=='production'` + explicit opt-in; **do not** weaken the production kill-switch).
- **Risk:** medium — if the secret drifts between Caddy and the dashboard env, everything 403s again (same, easy-to-diagnose failure). Blast radius = the dashboard boundary only.
- **Verify:** `curl :CADDY_PORT/api/health` → 200; `test_e2e_live.py::test_dashboard_shell_loads_and_records_api_reachability` evidence flips `proxy_status 403→200`, `trusted_proxy_403 true→false` (point its URL at the Caddy port; then strengthen it to `assert proxy_status==200` as a hard gate).

**D-2 · Favourable-absence UI: 0/"No rows" shown on a failed fetch (LS-004) · S · code**
Render-layer bug (the hooks are correct — they keep `error` separate and leave lists
`[]`). `app/board/page.tsx:423-424` unconditionally prints "0 of 0 cards" even in
`!hasLoaded && error`; `ApprovalsTab.tsx:156` prints "Pending (0)" and falls through
to an empty `<Table>` ("No rows") when `error` is set.
- **Fix:** compute `countUnknown = !hasLoaded && !!error` and render "—/unavailable" instead of a count; in the approvals body, when `error && !pending.length` render nothing (let the ErrorState card be the single truth) instead of the empty table. Same one-liner recurs in other `.length` count widgets (today/cockpit/sessions) — worth an audit pass.
- **Risk:** low (pure display). **Verify:** re-run the browser-operator UI check; the zero-next-to-error disappears. Independent of D-1 but more meaningful after it.

---

## Batch 2 — P1: the reaper/approval axis (your flagged concern) + the security fail-open

**R-1 · Session reaper max-park kills legitimate sessions (LS-001) · M · mixed**
Two compounding faults terminalize parked sessions (16 real `killed_by='max-park'`):
the approval page never reaches a human, and max-park fires the same whether the
approval was *never delivered* or *delivered-but-undecided*.
- **Fix (land as one unit):** (a) **wire a reachable page** — set `OMNI_NTFY_URL` (phone) and `OPS_ALERT_SLACK_WEBHOOK_URL` in `scripts/launch-omniagentos.sh` (~line 72) + the sessions launchd plist (**the webhook URL already exists in `~/.config/omni/connections.env`** — this is mostly wiring), and add a Slack leg `_push_slack()` to `omniagentos/sessions/notify.py`; (b) in `supervisor.py::_reap_parked_if_needed`, make the ceiling **conditional** — a *delivered-but-undecided* approval may terminalize on timeout, but a *never-delivered* one (no approval row / send failed) should extend or alert rather than silently FAIL.
- **Risk:** medium — don't let (b) become "never reap" (a genuinely abandoned session must still close). **Verify:** `test_reaper.py::test_live_reaper_kill_evidence` records the max-park count as a datum (watch it stop growing); add a test that a never-delivered park does not FAIL within the window.

**R-2 · Idle-reaper enforce armed in prod / stale "dry-run" docs (LS-002) · M · mixed**
`launch-omniagentos.sh:72` exports `OMNIAGENTOS_REAPER_ENFORCE=1` intentionally (prod
parity), but `supervisor.py:83-85` still says "one-week dry-run rollout."
- **Fix:** (1) rewrite the stale comment/docs to state enforce is the intended default; (2) add a fail-safe liveness guard so the idle measure can't under-report a slow-thinking or adopted session with no child subprocess (the case most likely to kill legit work).
- **Risk:** low. **Verify:** preventive — `test_a2_enforce_live_env_is_recorded` keeps recording the armed state; watch idle-reaper kill count stays 0 for healthy sessions.

**S-1 · Approval classifier fail-open (LS-022, canonical YAML) · S · code**
`omniagentos/orchestrator/approvals.py` auto-approves a destructive request phrased
outside its enumerated verb vocabulary.
- **Fix:** the investigator argues **allowlist-on-the-safe-side over widening the denylist** — a whole-classifier allowlist is infeasible (the product must run `make build`/pytest hands-off), but the **destructive/finance classification specifically** should invert to "unrecognised → escalate/park," not "unrecognised → approve." Target `_classify_finance_request` / the destructive branch.
- **Risk:** medium — over-broad escalation could park benign ops; scope the inversion to the money/delete/secret surface. **Verify:** **flips** `test_tools_permissions.py::test_classifier_fail_open_beyond_poc_phrases_observed` (invert its observed-behavior assertion to the fixed behavior).

---

## Batch 3 — P2: filesystem blast-radius (fail-closed)

**F-1 · Workspace floor admits the production checkout (board_files, YAML LS-018) · S · code**
`board_files.py::_approved_workspace_roots` (253-292) trusts a grantable mount root
that is itself a code-checkout root.
- **Fix:** add `_is_code_checkout_root(path)` (true when the realpath'd root contains a `.git` / is a known serving checkout) and refuse it in `_approved_workspace_roots`.

**F-2 · Per-file denylist admits system roots (board_files, YAML LS-017) · S · code**
`board_files.py::_deny_guard` (419-427) doesn't fail closed on OS directories.
- **Fix:** add a module constant `_SYSTEM_DENY` of realpath'd sensitive roots (`/private/etc`, `/private/tmp`, `~/.ssh`, `/etc`, …) and deny any path under them.
- **Risk (both):** low if scoped to realpath containment; don't break legitimate in-scope deliverable reads. **Verify:** the LiveSim `files_fs` denylist-gap tests invert from "observed admitted" to "denied."

---

## Batch 4 — P2: operator truth (mostly small)

**O-1 · /today undercounts (LS-007) · S · code** — `today.py::today_dashboard()` counts
`swarm_attempts`, so a 30-session day reads 0/0. **Fix:** repoint `started_today`/`completed_today` to the `sessions` table (`DATE(created_at)=:today`, `state`-based completion). Highest-value operator win. **Verify:** `test_e2e_live.py::test_dashboard_today_*` cross-source check.

**O-2 · Exact cost never reaches `provider_call_usage` (YAML LS-013) · L · code** — the
ledger is built but no observation stream writes to it. **Fix:** wire the usage
observation into `provider_exec.py::_record_usage` at the exec boundary (it already
holds session/provider/model/wall-time); ensure OpenRouter `usage:{include:true}`.
**Verify:** `test_telemetry_cost.py` exact-cost rows appear.

> **T-1 · "Acceptance-rate drift" (report LS-016) — NOT a product defect · TEST fix · S.**
> `acceptance_rate` is *defined* over settled runs; my LiveSim test compared it
> against the wrong denominator. **Fix the test**: compute `accepted/(total-neutral)`.
> **T-2 · "912 unsettled routine_runs" (report LS-017) — NOT a product defect · TEST fix · S.**
> Those rows are correctly-classified pending/no-gate settlements, not a leak.
> **Fix the test** (`test_orchestration.py:145`) to redefine "unsettled" precisely.
> Both are LiveSim-test corrections, not OmniAgentOS changes.

---

## Batch 5 — P2/P3: hygiene + product-owner decisions

- **C-1 · CORAL enforce has no producer (YAML LS-020) · M · code** — add a producer-presence guard so `enforce` can't arm against an empty `var/coral/skills/` hub (`swarm/worktrees.py::coral_context_mode()` or the spawn gate).
- **M-1 · MemLife review queue dormant: 210 staged, 0 graduated (YAML LS-023) · L · DECISION** — graduation is human-in-the-loop by design. **Needs your call:** commit to a reviewer cadence (surface the queue) *or* add an auto-graduation policy. Not a code bug until that's decided.
- **M-2 · Duplicate pending memlife candidates (YAML LS-024) · S · code** — `memlife/db.py::stage_candidate` only dedupes against *decided* keys; add a pending-key gate (`_PENDING_STATUSES={'staged','reopened'}`) before insert. **Flips** `test_memory.py::test_stage_candidate_idempotent_and_decided_key_gate`.
- **M-3 · Metacog retrieval telemetry (YAML LS-025) · M** — the writer is wired; **verify the live env** (`OMNIAGENTOS_MEMORY*`, whether `runner_hook.build_and_store_context` is the live assembly path) before writing code — may be config.
- **A-1 · API 405/4xx error-code envelope (LS-005) · S · code** — `api/main.py::http_error_handler` (742-748) maps only 404→`not_found`, everything else→`internal`. **Fix:** explicit status→code map (400 bad_request, 401 unauthorized, 403 forbidden, 405 method_not_allowed…). **Flips** the LiveSim 405-envelope observation.
- **B-1 · /api/board 4.8MB unpaginated (LS-006/008) · M · DECISION+code** — add a `summary` projection (omit/truncate heavy description fields) + a client `limit`/`If-None-Match`. Needs a small default-projection decision.
- **CAP-1 · Capsule reason-code miswrite (LS-012) · S · code** — an over-cap optional slice admitted in full is recorded `truncated` though V1 never truncates; record `REASON_INCLUDED`.
- **V-1 · Cost-quality vocab divergence (LS-014) · S · doc/decision** — `livesim.v1` uses `exact|approximate|unreported|n/a`; product tables use `exact|estimated|unknown|mixed`. Pick one canonical mapping (recommend product tables authoritative) and document it.
- **OPS-1 · Cheap-LLM tier down (LS-010) · S · ops** — restart LiteLLM `:4000` and confirm it's in the launch stack (Postgres-backed budget config intact). Not code.
- **H-1 · event_hub `state=ok` while `tailer_alive=false` (LS-011) — working-as-designed (lazy-start) · S** — make the health snapshot self-describing (add `tailer_expected` = `subscriber_count>0`) so it stops reading as a defect.

---

## How to execute safely (when you approve)

1. One worktree per batch off `main`; **never** touch the serving checkout directly.
2. Batch order by dependency: **D-1 → D-2**, then **R-1/R-2/S-1**, then **F-1/F-2**, then O/hygiene. The two test-only corrections (T-1, T-2) can land anytime in the LiveSim branch.
3. Cross-lineage review of every diff (the implementer ≠ the reviewer), heavier for S-1/F-1/F-2 (security surface).
4. Each product fix lands through the merge gate; the LiveSim observational test that documents it flips as the acceptance signal, then gets promoted out of "observed-defect" status.
5. Ops items (D-1 secret, R-1 webhook wiring, OPS-1 LiteLLM) are config/deploy, not merge-gated — but still your explicit go-ahead.

**I have not applied any of this.** Tell me which batch to start with (or "all"), and I'll implement it in an isolated worktree with cross-lineage review, or hand specific batches to the coder fleet.
