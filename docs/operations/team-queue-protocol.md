# Team queue protocol

The Team Work OS (migration 123) turns `board_tasks` into a queue humans and agents share.
Every card carries a `ref`, an `owner_employee_id`, `acceptance_criteria`, and — once done — a
`verified_at`/`verified_by` stamp. This is the loop every worker (human or agent) runs against it.

## The loop

1. **READ your queue.** `GET /api/team/board?owner=emp_x` — bucketed into ready / active /
   blocked / review / done_today (`TeamStore.team_queues`). A person below 5 `ready` cards is
   flagged (`ready_below_5`) — see "Grooming bar" below.
2. **CLAIM.** `POST /api/collab/board/{id}/claim` — compare-and-swap on `claim_version`
   (`CollabStore.claim_task`). Two workers racing the same card is expected; exactly one wins.
   A human claimant is derived from the authenticated principal, never trusted from the body.
   Releasing normally keeps accountability with that owner; callers must explicitly use
   `release_claim(..., return_to_pool=True)` to clear ownership and return conformant work to the
   universal pool. Baseline cards can never be returned to the pool.
3. **WORK.**
4. **UPDATE.** `PATCH /api/collab/board/{id}` with `status`/progress notes as they change.
   **Zero-commit work is normal** — triage, research, a customer reply, a decision — none of it
   requires a code change to be real progress.
5. **ATTACH EVIDENCE.** `POST /api/team/tasks/{id}/evidence` for anything that is not a commit or
   a PR: docs, customer replies, research notes (`TeamStore.add_evidence`, idempotent on
   `(kind, repo, ref)`). Code work should not need this call — a commit or PR whose message or
   branch carries `refs <REF>` auto-attaches (see "Branch/commit convention" below).
6. **BLOCKED.** `PATCH status=blocked` + a **mandatory** `blocked_reason`. The store refuses an
   owned card into Blocked with an empty reason — a card sitting in the Blocked queue must always
   say what it is waiting on.
7. **DONE** only when the acceptance criteria are actually met. An owned card with non-empty
   `acceptance_criteria` cannot move to `done` without at least one `task_evidence` row already
   attached — the store enforces this, it is not a convention to remember.
8. **VERIFY.** `POST /api/team/tasks/{id}/verify`. Two admissible paths: **mechanical** — the card
   carries a passing `test_run` or merged `pr`, and anyone (including the owner) may verify it,
   because the claim is the test runner's, not the verifier's; or **human** — no mechanical
   evidence, so the verifier must not be the owner, with one exception: `emp_owner` (the operator)
   may verify their own cards, because they have nobody to counter-sign with.

## Rules

- **UNATTRIBUTED is a valid, honest state.** Evidence a collector cannot attach to a card
  (`task_id=NULL`) sits in the operator's reattribution inbox (`TeamStore.list_unattributed`)
  rather than being guessed onto the wrong card and inflating someone's numbers.
- **Never edit scoring.** `prod_snapshots` rows are written by the productivity pipeline, not by
  hand.
- **Dedupe before creating a task.** `ref` is unique-partial-indexed
  (`CollabStore.create_board_task` raises `ValueError("ref_conflict")` on a clash) — check the ref
  AND search titles before filing a new card.
- **Pool intake is explicit.** `POST /api/team/tasks` requires `goal_id` plus non-empty
  `acceptance_criteria` and creates an ownerless top-level card. The legacy agent route
  `POST /api/collab/board` remains permissive; cards created there without the protocol fields
  are not pool members. The pool starts empty at launch (`depth: 0`, `low: true`) and stays that
  way until cards are filed through the explicit intake route.
- **≥5 READY cards per person is the grooming bar.** The 07:00 report flags anyone below it
  (`ready_below_5`) — a shortfall is visible before the person goes idle, not after.
- **Ranking counts ONLY verified top-level (parentless) tasks** — `S=1`, `M=3`, `L=8`. Splitting a
  task into subtasks under a parent does not add points; the parent's size is the one that counts.
  Commits, LOC, session count, and PR count are worth **zero**.
- **Work vs Tasks (v4, the operator 2026-08-13).** *Work* is this queue: delegated or claimed,
  points-bearing, with **5 ongoing expected per person** (`🔧 Work x/5` on every load view,
  `⚠ below floor` when short — visibility, never a block). *Tasks* are minor ad-hoc items assigned
  person-to-person (`/task assign @name <free title>`, `@name task <title>`); they carry
  `source='task-adhoc'` from creation, are worth **zero points** (excluded from scoring at the
  card-gathering stage — they never appear even in the refusal listings), and render ABOVE Work
  with their deadlines front-and-center wherever a person's load shows.
- **No Jira issues for queue work.** The board is canonical; Jira is for a different pipeline
  (JG2/company-goals transcripts).

## Branch/commit convention

Carry `refs <REF>` in commit messages and PR bodies (branch names may carry the ref slug too).
This is what lets code evidence (`kind='commit'`/`kind='pr'`) auto-attach to the right card instead
of landing unattributed.

## Slack inbound updates (no commit required)

Reply directly in the report channel (`#dev-agentic-alerts`, default channel id
`C0000EXAMPLE`) with a short command and the board updates without touching a repo. This is
flag-gated (`OMNIAGENTOS_TEAM_SLACK_UPDATES`, off by default) and only reads the allowlisted
channel (`OMNI_TEAM_REPORT_CHANNEL`); see `omniagentos/team/slack_updates.py`. No LLM — every
command matches a fixed grammar or is silently ignored as ordinary chatter:

- `done <ref> [note...]` — marks the card done. If it has acceptance criteria and no evidence
  yet, the Slack message itself is auto-attached as evidence (`kind='note'`) before the retry —
  this earns "done", it never earns "verified" (still a separate `POST .../verify`).
- `progress <ref> <note...>` — records the note as evidence and a `task_events` comment. The note
  is required; a progress update with nothing to say is not a command.
- `blocked <ref> <reason...>` — moves the card to Blocked with the given reason. The reason is
  required, same as the store's own rule for an owned card.
- `claim <ref>` — self-assigns an open card (`claimed_by="human:<employee_id>"`), CAS-respected:
  a card someone else just claimed replies with a conflict, not a silent no-op.
- `<@user> task <title...>` (or `task <@user> <title...>`) — creates one owned card for the
  mentioned teammate. Two optional flags, accepted anywhere in the title and stripped from it:
  `!top` sets `priority=urgent`; `#<company>` (e.g. `#initech`, `#grok`) ties the card to that
  company's "General engineering — …" goal (an unknown slug still creates the card, goal-less,
  and the reply says so).
- `!top <ref>` — escalates an EXISTING card to `priority=urgent`: the same fire token the `task`
  verb accepts at create, as a verb. Authorized like `done`/`progress` (owner or `emp_owner`; an
  ownerless open pool card may be escalated by any roster sender). Idempotent — an
  already-urgent card replies "already urgent" rather than re-writing.
- `my queue` — replies with your Ready/Active/Blocked/Review buckets.
- `report` — replies with today's report (requires the report-rendering module; replies
  `report module not yet installed` until that lands).

`<ref>` is either a bare board ref (`U3`, `OPS-2`) or a double-quoted title prefix
(`done "Fix login bug" landed it`) matched against YOUR OWN non-terminal cards. An exact ref
match works across owners — `emp_owner` may act on anyone's card, everyone else only their own — a
title-prefix match is always scoped to the sender's own board. Every applied command gets a
threaded confirmation or refusal reply; a message that is not on the grammar is left alone, same
as any other channel chatter.

Sender identity is resolved from `configs/team_slack_map.yaml` (Slack user id -> employee id). A
sender not in that map is logged and ignored, never an error — the channel is not roster-exclusive.

## The `/task` command family (v3 — the operator's ruling 2026-08-13)

The same handler also accepts a literal `/task <verb> ...` message prefix (every bare verb above
keeps working unchanged). One-screen team cheat sheet: `docs/operations/task-commands.md`;
engine: `omniagentos/team/slack_updates.py` + shared helpers in `omniagentos/team/tasks.py`.
Unlike chatter, a malformed `/task ...` IS answered (with a pointer at `/task help`).

- `/task add <title> #company [!top|!high|!low] [<deadline>]` — **the operator only**; creates one
  pool-eligible card on the company's "General engineering — …" goal. Acceptance criteria come
  from a `| ac: <criteria>` suffix, else the title itself. Adding IS approval; an unknown or
  missing company is refused (a goal-less card would not be pool-eligible).
- `/task assign @person <REF> [<deadline>]` — **queue delegation, the operator/Alice only**: sets the
  owner of an ownerless open card (guarded UPDATE; a race replies "someone grabbed it first"),
  DMs the assignee. An owned ref is redirected to `/task reassign`.
- `/task assign @person <title> [#company] [!priority] [<deadline>]` — **ad-hoc**: anyone on
  the roster creates a new owned card for a TEAMMATE (never themselves); the assignee is DMed.
- `/task claim <REF>` — the existing CAS claim (anyone).
- `/task done <REF> [note]` — **owner only** (no operator override on this path); evidence
  auto-attach works as for bare `done`. The ASSIGNER — actor of the latest `assign` task event
  by someone other than the owner, falling back to the card's creator — is DMed
  "`<owner> completed <REF> — <title>`" (no DM when that resolves to the owner themselves).
- `/task note <REF> <text>` — anyone; appends a `comment` task event and DMs the assigner
  (or the owner, when the noter IS the assigner).
- `/task reassign <REF> @person` — the operator/Alice always; otherwise the current owner (hand-off).
  CAS-guarded on the current owner; DMs the new assignee; the reply and the event note name
  the old owner.
- `/task queue [#company]` — the shared queue, compact, grouped by company (fixed order:
  globex, acmeuni, hooli, initech, omniagentos). `/task mine` = `my queue`.
  `/task help` — the usage card.

**Deadlines** are a natural trailing phrase — `immediately` · `in N minutes|hours|days` ·
`today` (18:00 local) · `tomorrow` (10:00) · `by <weekday>` (10:00) — stored as an aware ISO
timestamp in `board_tasks.due_date` and rendered with ⏰ in DMs/alerts (🔴 when overdue).

**DM discipline:** one action, one DM — assign/reassign → the assignee; done/note → the
assigner (or owner) — all through the notifier's `_safe_title` egress scrubber. The
assignment event notes written by this path deliberately avoid the `owner:<emp>` token so
the event watcher never doubles a DM.

## Company on every card (multi-company Work OS, 2026-08-13)

A card's company is SERVER TRUTH derived from its goal — `goal_id` → `company_goals` →
`org_companies` — never a client-side guess:

- **Queue cards** (`GET /api/team/board`, pool + per-person buckets) carry `company_slug` and
  `company_name` (both `null` when the card has no goal, or the goal's company row is gone —
  the card stays in its queue either way). They also carry `owner_employee_id`, which the pool
  and "Agents & unowned" surfaces render on the card face.
- **Board cards** (`GET /api/board`) carry a top-level `company_slug` through the same join
  (a correlated subquery in the collab list projection). This is a different channel from the
  legacy `org_json` envelope enrichment, which follows the *project* chain; the board's company
  chip and the team queues agree because both read the goal join.
- The five slugs in play: `globex`, `acmeuni`, `hooli`, `initech`, `omniagentos`. Each
  keeps one `short_term` goal titled EXACTLY `General engineering — <Display Name>` (a hard
  title-prefix contract — the Slack `#company` flag resolves on it) under a `long_term` parent.
  `omniagentos`/`initech` arrive via the dev-queue import; the other three via
  `python -m omniagentos.company_goals.seed_company_goals` (idempotent — a company that already
  has a "General engineering — " short_term goal is skipped whole).

## Priority

`board_tasks.priority` is a closed vocabulary — `low | normal | high | urgent` — validated on
PATCH (`400` on anything else; an unknown status is refused the same way). Queue reads rank
`urgent → high → normal → low → unknown-legacy-text`, in SQL, before any LIMIT, so a truncated
pool page still surfaces the urgent card. Escalation paths: `!top` at create (`task` verb),
`!top <ref>` on an existing card, or a plain `PATCH /api/collab/board/{id}` with
`{"priority": "urgent"}`. Two size rules protect the point ledger: a verified card's size is
frozen (operator excepted), and a card's OWNER may not re-size their own card (assigner-priced,
verifier-confirmed; operator excepted).

## Auto-dispatch — MACHINE WORK ONLY (v3, 2026-08-13)

`omniagentos/team/dispatch.py` (launchd template
`configs/launchd/com.omniagentos.team-dispatch.plist`, every 300s — a TEMPLATE, installing
it is an operator action) is the compute-pool bridge and nothing else. **The human-assignment
path is REMOVED** — the operator's v3 ruling: no auto-assignment of tasks to people. Humans get work by
`/task claim` (self-service) or the operator/Alice delegation (`/task assign @name <REF>`); a pool card
without a compute-pool envelope is passed over silently — no action, no event, no DM. Points
pace still renders in the hourly pulse; it influences no assignment, because nothing assigns.

**Off by default**: without `OMNIAGENTOS_TEAM_AUTODISPATCH=1` every run is a clean no-op,
exit 0. Per pass it walks the pool in priority order and ENQUEUES at most
`OMNIAGENTOS_TEAM_AUTODISPATCH_CAP` (default 3) fresh cards whose
`org_json.dispatch.target == 'compute-pool'` to the local wq-server (company slug + board task
id in the unit's labels and brief; `idempotency_key=team-dispatch:<task_id>` so the cycle can
never double-enqueue; dedupe hits and refusals never consume the cap; unreachable server =
log-and-skip). `--once --dry-run` previews a pass without writing.

## Point floors, ratchet, and Friday pace

`configs/team_points.yaml` + `omniagentos/team/points.py` set the weekly VERIFIED-point floor:
week 1 = 10, week 2 = 15, then +20% every 2 weeks (rounded to whole points; `program_start`
anchors week 1). This is POLICY over scoring — the scoring rules themselves (verified-only,
S=1 M=3 L=8, 10x target) live in `omniagentos/team/scoring.py` and are not touched by any of
it. The hourly pulse adds one pace line per active mapped dev (never the operator), comparing
verified points so far this week against the floor prorated Mon→Fri:

```
⚠ emp_bob 4/15 pts, Friday pace short
✓ emp_alice 9/15 pts, on pace
```

On Fridays, when next week's floor is higher, the pulse appends the raise announcement
(`📈 Point floor rises Monday: 15 → 18 verified pts/week (+20% ratchet)`). Pace is decoration:
a points/config failure costs the pace lines, never the pulse.

## Completion tri-state (migration 132, developer accountability, 2026-08-14)

A `done` card carries ONE of three completion states, derived the same way everywhere it renders
(`omniagentos.team.store.completion_state` — the single source of truth for the API, the
dashboard badge, and the 07:00 report, so a card can never read "verified" on one surface and
something else on another):

- **verified** — `verified_at` is set (mechanical or human verify path, per step 8 above).
- **failed_verification** — a verifier looked at the done card and REFUSED it:
  `verification_failed_at`/`verification_failed_by`/`verification_failed_reason` are set,
  `verified_at`/`verified_by` are NOT. A `verify_failed` `task_events` row carries the reason as
  the durable audit trail.
- **unverified** — `done`, and nobody has looked yet (both sets of stamps are NULL).

A card that is not `done` has no completion state at all (`None`) — never a favourable
"unverified" for work that has not finished.

`POST /api/team/tasks/{id}/verify` takes an optional verdict:

```json
{"verifier": "emp_alice", "outcome": "fail", "reason": "no tests at all"}
```

`outcome` defaults to `"pass"` (every existing caller keeps working byte-for-byte). `"fail"`
requires a non-empty `reason`, applies the same owner-counter-signature rule as a pass verify, and
refuses baseline cards exactly like `unverify_task` (`ValueError('baseline_immutable')` —
scoring denominators can never shrink). A LATER successful verify clears all three
`verification_failed_*` columns (repair-by-verify); leaving `done` via a status PATCH clears them
too (a reopened card returns to a clean `unverified`). `unverify_task` on a failed card clears
only `verified_*` (already NULL there — effectively a no-op) and NEVER touches the failure
stamps — only a successful verify or a reopen does that.

## Daily commitments (migration 132, spec §6, 2026-08-14)

Every active dev (not the operator — they set the queue, they do not answer a commitment check to
themselves) carries a short, deterministic daily commitment list — no LLM anywhere in the
pipeline (`omniagentos/team/commitments.py`):

- **06:55 — generation.** `commitments.run_daily(store)` is the ONE orchestration entrypoint the
  morning job uses: it resolves YESTERDAY first, then generates TODAY (see "carry-on-miss"
  below for why the order matters). `generate_for_day` ensures, per active dev: one `'task'`
  commitment per Active-bucket card (claimed/in_progress) plus any assigned-open card due that
  day, priority-ordered and capped at 4 — plus ALWAYS one `'improvement'` commitment ("One
  significant OmniAgentOS improvement"). Idempotent by construction (`INSERT OR IGNORE` on
  the day/employee/task and day/employee/improvement-slot unique keys) — a re-run creates
  nothing new. The morning DM (`notify.run_morning`) renders a `*Today's commitments*` section
  per employee, BEFORE the EDC section: task commitments as `REF — title` lines, the improvement
  slot as its own line. Three distinguishable renderings, never collapsed into one another: real
  commitments, an explicit `no commitments recorded` (genuinely zero for this person today), or
  an explicit `⚠ commitments unavailable (generation failed)` (an exception during generation) —
  the DM still sends either way.
- **07:00 — resolution.** The report (`report.gather`) calls `commitments.resolve_day(store,
  yesterday)` again — idempotent, a no-op on any already-resolved row — then reads. A task
  commitment resolves `delivered` iff its card reached `done` by the end of the LOCAL day (the
  first `status_change → done` `task_events` row, UTC converted to local) AND was not in
  `failed_verification` state at resolution time; otherwise `missed`, with the reason recorded in
  `resolution_note`. The improvement commitment resolves `delivered` iff the dev owns ≥1 card on
  the OmniAgentOS company goal that reached `done` that day with ≥1 SUBSTANTIVE evidence row
  (kind `commit`/`pr`/`test_run`/`deploy`/`doc` at `quality_gate='pass'` — a bare `note` or a
  reverted/rejected row does not count) AND is not `failed_verification` AND is either size M/L
  or verified (a cosmetic, unverified S-size card does not satisfy the slot). Each person's line
  in the report — both `render()` and `render_slack()` — reads `Yesterday: delivered X/Y
  commitments · improvement ✓` (`✗` on a miss), with the missed refs listed on the line beneath.
  The same three-state contract applies here too: real numbers, `no commitments recorded`, or
  `commitments unresolved ⚠` (resolution itself raised).
- **Carry-on-miss.** A missed task commitment mints — in the SAME transaction as the miss — the
  next day's follow-up commitment (`carried_from` set), so the work is never silently dropped. If
  the 06:55 generator already created tomorrow's row for the same task, the carry LINKS it instead
  of raising a unique-constraint error. `resolve_day` also runs an idempotent repair pass: any
  terminal `missed` row whose card is still live and lacks a carry edge for day+1 gets one minted
  (crash recovery). A cancelled/archived card is never carried — work the team decided NOT to do
  must not re-commit somebody every morning forever. `carried` is an OPEN state, not a terminal
  one — it records provenance ("this slipped from yesterday"), not a verdict — so `resolve_day`
  judges a carried row on its OWN day exactly like any other commitment: delivered if the card
  finishes that day, or missed (and carried again, chaining through `carried_from`, if it slips
  again).
- **Improvement slot.** The one standing item every active dev carries every day, answering the
  spec's governing principle directly — every task should complete today's objective AND make
  OmniAgentOS more capable of doing tomorrow's itself.
- **The three daily automation slots (the operator's ruling, 2026-08-14).** Every active dev also gets
  `AUTOMATION_SLOTS_PER_DAY` (3) `'automation'`-kind slots each morning, titled "New automation or
  skill (n/3)" — three new automations or skills a day, per dev, the same deterministic-generation
  and no-LLM discipline as every other slot. A card QUALIFIES a slot only when it reached `done`
  that LOCAL day, carries ≥1 SUBSTANTIVE evidence row at `quality_gate='pass'`, is not
  `failed_verification`, AND its `automation_maturity` is `assisted` or above — `human` and `NULL`
  do not qualify, because the slot counts work the SYSTEM took over, not merely work that got
  done. Qualifying cards fill the slots in the order they reached `done` (slot 1 = the day's first
  automation), so a re-run never reshuffles which card filled which slot. Unlike task commitments,
  **automation slots NEVER carry**: a missed slot is a day that did not produce one, and tomorrow
  already mints three fresh slots — carrying would stack yesterday's three on top of today's three
  and reach fifteen open automation rows by Friday, a number nobody could act on. The miss stays as
  history; the expectation renews. Today's freshly-generated slots are always `committed` (nobody
  has judged them yet), so the morning DM states the OPEN count ("3 automation/skill slots open
  today"), never a ratio; the 07:00 report's per-person line gains `· automations N/3` (N =
  yesterday's delivered count) once the day is resolved — and OMITS the clause entirely on a day
  with no automation rows at all (a pre-migration day), rather than printing a `0/3` that would
  read as a judged miss on a day nobody measured.
- **Manual overrides.** `GET /api/team/commitments?day=&employee_id=`; `POST /api/team/commitments`
  (operator add, `source='operator'`); `PATCH /api/team/commitments/{id}` permits only `committed
  → {delivered, missed}` with a REQUIRED `resolution_note`, and `committed → delivered` on a
  `'task'` commitment additionally requires the linked card to actually be `status='done'` — no
  evidence-free manual freeze. A resolved row accepts only `resolution_note` APPENDS afterward
  (misses are preserved history, never rewritten to delivered).

## The standing targets line (the operator's ruling, 2026-08-14)

`omniagentos.team.contracts.NORTH_STAR` — `"🎯 100% of the operator's tasks automated · 10× verified dev
speed"` — is the one goal every dev-facing daily surface states, verbatim, so it can never drift
between renderers. It renders as `🎯 YOUR TARGETS: 100% of the operator's tasks automated · 10× verified
dev speed` as the FIRST line of each dev's morning DM (`notify.run_morning`; the operator's own DM
is exempt — they set the targets, they are not addressed by them, the same exemption
`commitments.active_devs` already applies), once under the title of the channel-wide daybrief
(`notify.daybrief_payload`), and once at the top of the 07:00 report, in both `render()` and
`render_slack()` (`report.py`). The two standing targets are for Bob and Alice.

## Automation backlog & proposals (the operator's GO, 2026-08-14)

Anyone can spot an automation worth building; only the operator decides it gets built. Three verbs carry
that, on the existing board — **zero migrations**.

**Categories are goal-ladder children.** Each is a `short_term` goal titled `Automations — <name>`
under the long-term goal *"Automate 100% of the operator's tasks"*
(`team.contracts.AUTOMATION_PARENT_GOAL_ID`, a LIVE-DATA id, not a schema constant). Today's set:
email & comms · content & marketing · ads · finance & ops · dev tooling · customer service. A new
category is a row an operator creates through the goals API — the resolver picks it up on the next
command, no deploy. `#token` matching (`team.tasks.match_automation_category`) is
case-insensitive, treats `-`/`_` as spaces, and uses the SAME boundary-checked `[A-Za-z0-9_-]+`
token grammar on both surfaces (Slack and `POST /api/team/nl-assign`) — `#dev_tooling` and
`#dev-tooling` are one category. Precedence is exact > whole word > word-bounded prefix
(`#comms` → "email & comms", `#email` → "email & comms"); a mid-word fragment never matches.
A token that leaves MORE THAN ONE candidate in its winning tier is **ambiguous and refused**,
with the candidates named — with "customer service" and "customer success" both on the ladder,
`#customer` silently filing under the older goal is exactly the mis-file a category exists to
prevent. An unknown or absent category lands the card on the PARENT goal (never goal-less: a
goal-less card is not pool-eligible and would approve into work nobody can claim), and if the
PARENT goal itself is missing from the database the proposal is **refused** with
`automation_backlog_unconfigured` rather than persisted — creating the goal later does not
backfill a card that was already written.

- `/task propose <title> [#category] [for owner|alice|bob|ai] [| ac: <criteria>]` — **anyone on
  the active roster** (deliberately wider than `/task add`, which is the operator-only because adding to the
  queue IS approval). Creates a top-level card with `status='awaiting_approval'` and
  `source='automation-proposal'`, acceptance criteria from the `| ac:` suffix else the title. It
  approves nothing: the card cannot be claimed, dispatched, or counted toward anyone's queue until
  the operator decides — which is what makes it safe to let everyone file one. **No deadline grammar**: a
  proposal is not scheduled work. The `for X` hint is STORED, not applied
  (`org_json.proposal = {proposed_by, assignee_hint}`, MERGED into the shared envelope, never
  clobbering the orgdims keys). the operator is DMed with the ref and both decision commands.
- `/task approve <REF> [for owner|alice|bob|ai]` — **the operator only**. Moves the card to `open` in ONE
  transaction, CAS-guarded on `awaiting_approval` (a second decider gets "already decided", not a
  contradictory second outcome). The hint — the verb's, else the one stored at proposal time —
  decides where it lands: a **person** → an owned open card (`assign` event written);
  **`ai`** → an OWNERLESS open card carrying
  `org_json.dispatch = {target: "compute-pool", ready: false}` (ownerless on purpose: the
  dispatcher only ever looks at POOL cards, so an owner would hide the card from the daemon the
  hint asks for); **no hint** → a plain claimable pool card. The proposer is DMed either way.

  `ready: false` is the honest half. An approved AI card is **not dispatchable yet** — the
  dispatcher also needs an `acceptance_cmd` and non-empty `owned_paths`, an executable spec that
  nobody types in a Slack verb. So the reply says "marked for the AI pool — dispatch needs an
  executable spec (acceptance command + owned paths); a coordinator completes it", and
  `dispatch_once` emits the NAMED skip `dispatch.ready=false — awaiting an executable spec` rather
  than passing the card over silently. Filling in the spec (and flipping `ready`) makes the same
  card enqueue on the next pass.
- `/task reject <REF> [reason...]` — **the operator only**, same guards; `cancelled`, reason recorded as a
  `comment` event and DMed back to the proposer.

Both decision verbs are **narrow by design**: they act only on cards that are BOTH
`awaiting_approval` AND `source='automation-proposal'`, and that guard runs INSIDE the write
transaction with `source` in the UPDATE predicate — a check outside it validates a snapshot
another writer can change before the write lands. The review bucket holds cards that got there for
other reasons (a swarm awaiting a human call, a promoted plan), and a status-only check would let
`approve` resurrect one of those straight into the open pool. A CAS loss reports the FRESH row, so
"already decided (status: open)" is never a stale claim.

**The generic PATCH cannot decide.** `CollabStore.update_board_task` refuses any status move out
of `awaiting_approval` for `source='automation-proposal'`
(`automation_proposal_decision_required`, naming the verbs) and treats that source as immutable,
so the two-step bypass — relabel, then move — fails at step one. The refusal binds EVERY caller
including the operator and `PATCH /api/collab/board/{id}`: the decision effects (the hint, the
dispatch envelope, the rejection comment, the DMs) live in the verbs, so a card decided any other
way would be a decided card with none of them. Ordinary edits to a proposal (title, description)
are untouched.

**Envelope co-tenancy.** `org_json` is now shared by three writers — orgdims classification,
`proposal`, and `dispatch`. Each merges: `OrgDimsStore._write_board_org` replaces its own five
keys wholesale and preserves every other top-level key verbatim ("not mine, not mine to delete"),
and approval merges against the row re-read inside its own transaction. Before that, a
reclassification silently deleted the assignee hint and stopped an approved AI card from ever
being dispatchable.

**Notification honesty.** A DM that fails to deliver never reads as success: the threaded reply
appends `— ⚠ DM to <name> not delivered`. The board write is not conditional on Slack, but the
receipt must not claim a notification that did not happen.

The dashboard composer speaks the same grammar: `propose an automation to <title>` /
`propose automation: <title>` (`POST /api/team/nl-assign`, fixtures `NL_PROPOSE_ACCEPTED` in
`tests/team/test_nl_assign.py`), with `proposed_by = emp_owner` — the documented single-operator-token
posture of that surface.

**Scoring is unchanged.** An approved-and-shipped automation counts toward the daily three
automation slots through the EXISTING rules and nothing else: the card must reach done that local
day with at least one pass-gated non-`note` evidence row and `automation_maturity` at `assisted`
or above (see "Daily commitments"). There is no proposal-specific credit — proposing costs
nothing and earns nothing; shipping is what counts.

**Interim carrier, deliberately.** `status='awaiting_approval'` + `source='automation-proposal'`
IS the proposal state. No `approval_state` column, no mirror table: the ratified 2026-08-13
workqueue plan owns that machinery (its WP1/WP3), and shipping a second copy of the same state
now would mean two sources of truth for one question. This pair upgrades into it cleanly — the
rows carry everything that migration would need.

## Automation maturity vocabulary (migration 132, spec §9)

`board_tasks.automation_maturity` — a closed, nullable vocabulary: `human | assisted |
partially_automated | autonomous | autonomous_verified`. `NULL` means UNTRACKED, never defaulted
to `human` — an unmeasured card and a hand-done card are different answers. `automation_note` is
free text: "what could the system do itself next time". Both columns are PATCHable through the
existing `PATCH /api/collab/board/{id}` (`CollabStore.update_board_task`; an unknown maturity
value is refused with `400`, the same pattern `priority` already uses). Rendered per done-today
card in `GET /api/team/accountability` (see `docs/operations/daily-accountability.md`).
