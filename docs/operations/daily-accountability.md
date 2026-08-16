# Daily accountability (migration 132, 2026-08-14)

The owner view for "what did I say I would do, and what does the board say happened" —
deliberately per-person, never a leaderboard (the scoreboard already ranks; see
`docs/operations/team-queue-protocol.md`'s "Ranking" rules). This doc covers the accountability
endpoint, the flags that gate it, the miss-handling policy, and the learning feed that turns a
verified card into a memory candidate. Daily commitments themselves (generation, resolution,
carry-on-miss, the improvement slot) and the completion tri-state are documented in
`docs/operations/team-queue-protocol.md` — this doc is the reader's view on top of that data, not
a second copy of it.

## The owner view

`GET /api/team/accountability?day=` (default: today, LOCAL date) returns, per ACTIVE dev (the
operator is excluded — same rule commitments generation uses):

```json
{
  "day": "2026-08-14",
  "people": [
    {
      "employee_id": "emp_bob",
      "name": "Bob",
      "commitments": [ /* today's team_commitments rows for this person */ ],
      "improvement_of_day": { /* the 'improvement' commitment row, or null */ },
      "counts": { "ready": 5, "active": 3, "blocked": 0, "review": 1, "done_today": 2 },
      "done_today": [
        {
          "id": "btk_...", "ref": "U-7", "title": "...", "size": "M",
          "completion_state": "verified",
          "automation_maturity": "assisted",
          "automation_note": "could auto-attach evidence from CI",
          "verification_failed_reason": null,
          "evidence": [ /* per-item kind/repo/ref/quality_gate, not a bare count */ ]
        }
      ],
      "blocked": [ { "id": "...", "ref": "...", "title": "..." } ],
      "overdue": 1,
      "learning_captures": 2,
      "points_pace": { "points": 9, "floor": 15, "prorated_target": 12.0, "on_pace": false }
    }
  ]
}
```

Every done-today card carries its completion tri-state (`verified` / `failed_verification` /
`unverified`, from `omniagentos.team.store.completion_state` — the same derivation the 07:00
report and the dashboard badge use) and its automation-maturity fields, so the reader never has to
cross-reference the board separately. `evidence` is the per-item list (kind, repo, ref,
`quality_gate`), not a bare count — "what happened" answers with specifics, not a number nobody
can trace. `overdue` counts owned, non-terminal cards whose `due_date` has already passed.
`learning_captures` counts this day's durable `learning_capture: ...` markers for the person (see
"Learning feed" below) — how many memory candidates their verified/refused work filed today.
`points_pace` is FAIL-SAFE: a pace computation failure reads `null` for every person rather than
taking the whole view down or reporting a fabricated zero — "could not measure" and "measured
zero" are different answers, and only one of them is an accusation.

**Dashboard:** `AccountabilityStrip` (`dashboard/src/features/team/`) renders this endpoint
between `ScoreboardStrip` and the Board on `/team` — no leaderboard, no nav change, a defensive
data hook mirroring `useTeamScoreboard`'s pattern (own package, WP-C; not covered further here).

## The standing targets, in Slack (the operator's ruling, 2026-08-14)

Every dev-facing daily surface — the morning DM, the channel-wide daybrief, and the 07:00 report
(text and Slack alike) — states the SAME `🎯 YOUR TARGETS: 100% of the operator's tasks automated · 10×
verified dev speed` line (`omniagentos.team.contracts.NORTH_STAR`), so the goal reads identically
everywhere a dev or the operator looks; full rendering rules are in
`docs/operations/team-queue-protocol.md`'s "Standing targets line" section. The three daily
automation slots that back the "100% automated" half of that target are commitments like any
other — generated, resolved and rendered by the same pipeline documented there ("Daily
commitments") — and their per-card qualification bar (`automation_maturity` at `assisted` or
above, pass-gated evidence, not `failed_verification`) is the same bar `automation_maturity`
already carries on `GET /api/team/accountability`'s `done_today` cards above.

## Flags

- **`OMNIAGENTOS_TEAM_LEARNING`** (default ON — `"0"` disables) gates the learning-feed hook
  described below. Off means every verify/fail is a pure store operation again: no metacog call,
  no candidate, no `learning_capture` marker — useful for a dry environment that has no metacog
  service wired, or for isolating a verification-path bug from a learning-path one.
- Daily commitments themselves are NOT separately flagged — `run_daily`/`generate_for_day`/
  `resolve_day` always run as part of the 06:55 job and the 07:00 report; there is no kill switch
  beyond removing those launchd jobs, because a commitment pipeline that is sometimes silently off
  is exactly the kind of instrument failure this build exists to prevent.

## Miss-handling policy (spec §6, open question 3 — the recommended default, implemented)

A missed commitment is never hidden and never quietly dropped:

1. **Flagged in the 07:00 report.** Every person's report line names the truth —
   `delivered X/Y commitments · improvement ✓/✗` — with the missed refs listed underneath. A
   generation or resolution outage renders an explicit `⚠`/`unresolved` state instead of ever
   reading as a favourable zero (see the three-state contract in
   `docs/operations/team-queue-protocol.md`'s "Daily commitments" section).
2. **The miss row is preserved.** History is immutable: a `missed` commitment can never be
   rewritten to `delivered` later. The only edit the API admits on a resolved row is an appended
   operator `resolution_note` (`PATCH /api/team/commitments/{id}`) — an excuse rides alongside the
   miss, it never erases it.
3. **The work auto-carries.** The missed task's follow-up commitment is minted for the next day in
   the SAME transaction as the miss (or repaired on the next `resolve_day` pass if a crash left
   the gap) — nobody has to remember to re-commit to unfinished work. Cancelled/archived cards are
   the one exception: work the team explicitly decided not to do is not carried forward.
4. **Blocked items already require a reason** — the existing store rule
   (`docs/operations/team-queue-protocol.md`, step 6) — so a miss caused by a blocker is
   traceable at the card level too, not just at the commitment level.

## Learning feed: verified card → metacog candidate → existing graduation guards

`omniagentos/team/learning.py` is the bridge nothing in the estate had before this build: the
three existing metacog pipelines all watch agent runs, none of them read `board_tasks`, so a
person's verified (or refused) work produced no memory candidate at all.

- **Trigger.** The API route layer calls the hook strictly AFTER the verify/fail store transaction
  has COMMITTED — never inside it, so a learning failure can never roll back a verification. A
  successful `POST /api/team/tasks/{id}/verify` (first verification only — a re-verify does not
  duplicate) calls `on_task_verified`; `outcome: "fail"` calls `on_verification_failed`.
- **What it files.** The card's `task_evidence` rows are registered as metacog artifacts, then ONE
  deterministic memory candidate is created — `memory_type='procedure'` on a verify,
  `memory_type='lesson'` on a fail — with a statement template embedding the concrete evidence
  (ref, title, owner, size, company, evidence kinds, automation maturity/note), never a bare
  count. Zero-evidence human-path verifies SKIP with an info log line — a legitimate
  learning-free verify, not a failure.
- **Idempotency.** On success the hook appends a durable `comment` task event —
  `learning_capture: <candidate_id> outcome=<procedure|lesson> event=<tve_ id>` — and dedupes on
  `(task_id, triggering event id, outcome)` before creating anything. The OUTCOME key matters: a
  card verified, then refused, then verified again must still file the lesson, which a bare
  "already captured" check would have suppressed. A REAL learning failure (metacog raised) logs a
  warning AND best-effort appends `learning_capture_failed: <ExcClass>`, so the refusal leaves
  durable, distinguishable evidence rather than reading as "no candidate warranted". The hook
  itself never raises to its caller — a metacog outage must never block a verification.
- **Accepted residual.** A crash between the store commit and this call loses ONE candidate (the
  `verify`/`verify_failed` events remain the durable record — a future backfill sweep can key on
  their absence of a matching `learning_capture` marker). The inverse crash — candidate created,
  then the process dies before the marker lands — can duplicate ONE candidate on a later
  re-verify; accepted as a strictly smaller harm than a rolled-back verification, and the
  promotion ladder below already tolerates duplicates.
- **Existing graduation guards, inherited, not rebuilt.** A filed candidate enters the SAME ladder
  every other memory candidate does — candidate → shadow/promoted → the recurrence-gated
  `synthesize_skill` — with evidence fail-closed at every step. This build adds the FEED, not a
  parallel promotion path. The known gap in that ladder (`metacog_skill_versions` never reaching
  the `/api/skills` selector/resolver, so a graduated skill does not yet reach worker briefs) is
  tracked separately as card `MEM-1` in the canonical queue — not duplicated here.
