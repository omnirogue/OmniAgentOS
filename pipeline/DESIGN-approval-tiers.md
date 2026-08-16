# Approval tiers — repairs land, self-improvements ask
**v2 · 2026-08-07 · revised after adversarial review (M1–M7 below were review findings)**

Two kinds of change flow through this system and they must not share a path.

---

## 1. The distinction

**A gate can verify; it cannot validate goal selection.** A test that fails without a fix and
passes with it proves the fix works. Nothing mechanical proves the change was a good *idea* — and
when the system chose the goal itself, that is exactly the unverified part.

Stated honestly, the criterion is **not** who authored it. It is:

> **Tier 1 = acceptance reduces to a mechanical check, against an observation the system did not
> invent. Everything else is Tier 2.**

Origin is a *conservative proxy* for that, not the thing itself. The proxy is imperfect in both
directions, and the imperfection is deliberately biased safe:

- Some self-improvements **are** verifiable ("the gate spends 40% of wall-clock re-copying an
  unchanged tree" has a measurable acceptance test). They still go Tier 2 — the goal was
  self-selected.
- Some repairs **aren't** ("improve this error message"). They still need the mechanical proof in
  §4 or they don't auto-land.
- The system already chooses *which* bugs to fix. Tier 1's claim is not "a human directed this" —
  it is "acceptance is mechanically decidable, and the defect was observed, not invented."

Misclassifying a repair as a self-improvement costs a human glance. The reverse costs the boundary.
`max()` in §3 exists so the error always lands on the cheap side.

| | **Tier 1 — Repair** | **Tier 2 — Self-improvement** |
|---|---|---|
| Acceptance | mechanically decidable | requires judgement |
| Origin | an observed defect | the system's own plan |
| Review | cross-lineage + gate | cross-lineage + gate **+ an authenticated human approval** |
| On green | **auto-merge** | **suspend**, open a draft PR, wait |
| Released by | n/a | a human approving the PR (§2) |

---

## 2. Why reflection cannot apply its own proposal

> **M1 — the finding that reshaped this document.** v1 claimed "the apply path requires a human
> action that no process can perform." **That was false.** v1's release mechanism was *a human
> deletes `parked/<id>.json`* — and every process on the host can delete a file. The boundary
> rested on the rule "no loop may un-park itself." A rule is not a mechanism, and a rule is
> precisely what the live auto-approve bypass on this estate ate.

The release is now bound to an **authenticated external event**:

```
reflection ──► candidate (origin: reflection) ──► Integration SUSPENDS on arrival
                                                        │
                                                        ▼
                                            draft PR opens (review surface)
                                                        │
                                     human approves the PR on GitHub
                                                        │
                          GitHub webhook ──► bridge (External role, authenticated)
                                                        │
                     appends `unparked` with detail.pr, detail.approved_by,
                                   detail.reviewed_sha == branch tip
                                                        │
                                          Integration gates and lands
```

**Integration requires that ledger event, carrying that PR approval, to land a Tier-2 candidate.**
A bare `parked/` marker deletion no longer releases anything — it is a **suspicious state that
raises an alert**, because the only thing that legitimately deletes a marker is the bridge acting
on a real approval.

Now the apply path requires an act on GitHub that no local process can forge. "No loop may un-park
itself" demotes from load-bearing rule to defense-in-depth, which is where rules belong.

`reviewed_sha` must equal the branch tip: an approval of an earlier tree is not an approval of this
one.

---

## 3. Determining the tier — never trust one signal

```
tier = max(
    tier_from_origin(envelope.origin),        # declared — weakest, self-reported
    tier_from_paths(envelope.paths),          # judge-surface backstop
    tier_from_resolves(envelope.payload),     # provenance — strongest mechanical signal
)
```

**`tier_from_resolves` (M7) is the one that actually works.** A candidate whose `payload.resolves`
points at a **`finding`** traces to an observed defect → Tier 1 eligible. One that resolves a
**`proposal`** traces to the system's own plan → **Tier 2 by construction**, since everything an
Executor builds comes from Planning. This is already in the schema, is checkable against an
artifact that must exist, and does not depend on the producer being honest about `origin`.
A candidate resolving **nothing** is ungrounded → Tier 2.

**`tier_from_paths` is a backstop, not the mechanism.** A genuine self-improvement can touch only
ordinary product code ("refactor the retry module", "delete dead code") and defeat path detection
entirely. It exists to catch changes to the machinery that judges changes.

**The self-governing surface list (M4).** v1's list was wrong in both directions:

- **Removed:** `var/loopqueue/**` — it is **git-ignored**, so it can never appear in a candidate's diff.
  Guarding it was dead weight that made the list look more complete than it was.
- **Added, all of which auto-landed as Tier 1 under v1:** `CONTRACT.md`, `schema/**`, `profile/**`,
  `PROMPT-*.md`, `ROUTING.md`, `MISSION.md`, `bootstrap.sh`, the governor, the bridge, and
  Integration's own implementation. **`schema/**` is the most important entry after the gate** — a
  hostile or careless schema edit weakens admission for everything downstream.
- **Added (the checker checks the guards, so it is one):** `bridge/integrity.py`,
  `bridge/governor.py`, `bridge/janitor.py`.
- **Kept:** `omniagentos/reflection/**`, `omniagentos/selfimprove/**`, `scripts/merge-gate.sh`,
  `omniagentos/scheduler/gate_evidence.py`, approval/permission/auth classifiers, and this file.

Two properties the list must have, or it rots:

1. **One machine-readable file the gate reads** — not prose here re-keyed into gate code. Two
   copies of one policy, drifting independently, is the exact two-judgment-sites shape that caused
   the bypass.
2. **Fail closed on the unknown.** A *new top-level path* that no entry classifies is **Tier 2 plus
   an alert** until a human classifies it — not Tier 1 by default. Absence of a rule is not
   evidence of safety.

**A change to the thing that judges changes is always Tier 2**, whoever produced it — including a
repair from a trusted external developer. Not distrust: a defect in the judge is invisible to the
judge.

---

## 4. The mechanical checks gating Tier-1 auto-merge

Auto-merge means no *human*, not no review. All of these, or it stops:

0. **`paths` covers the actual diff (M5).** `envelope.paths` ⊇ `git diff --name-only
   <merge_base>..<branch>`, or refuse. v1 claimed understating `paths` was "expensive and
   detectable" and shipped **no detector** — leaving `tier_from_paths` dodgeable by exactly the lie
   it was meant to catch.
1. Envelope valid; `origin` present and in the enum; ≥1 `verified_by: execution` evidence entry.
2. **Cross-lineage verdict, bound to this tree** — reviewer lineage ≠ producer lineage, **and
   `reviewed_sha` == branch tip.** A verdict on a stale tree is not a verdict on this candidate.
3. **Red-first, executed by the gate (M6)** — not an evidence entry *claiming* the test fails
   without the fix, but the gate reverting the product change and observing the test go red. §1's
   entire justification for Tier 1 is verifiability; **11 of 15 prior rejections here were tests
   incapable of catching their claimed defect.** For a path with no human on it, the proof must be
   executed, not asserted.
4. The gate passes on a frozen base, with a receipt.
5. `tier` from §3 is 1.
6. **External repairs arrive through the authenticated GitHub bridge**, not a filesystem envelope
   *claiming* `origin: external`.

**A missing check is never a pass.** A policy-killed reviewer run — `rc=1`, empty output, which has
happened three times in one night here — classifies `instrument-error` and re-reviews. It is never
an approval, and never a rejection of the candidate.

---

## 5. Contract amendment required (M2)

CONTRACT.md §11 guarantees **exactly one terminal event**. Tier 2 gets `parked` on arrival and then
`merged` or `rejected` later — **two**, breaking the guarantee on the Tier-2 happy path.

**Amendment:** `parked` is a **suspension**, not a terminal state. Terminal events are `merged` and
`rejected` only. The guarantee becomes *"exactly one terminal event; `parked`/`unparked` may occur
any number of times before it."* Queue rebuilds must handle parked-then-merged.

---

## 6. Settled questions

1. **Is `paths` detection sufficient?** No — it's a backstop. The mechanical core is diff-cover
   (§4.0) + `resolves` provenance (§3) + declared origin, max'd. A content classifier is acceptable
   only as an advisory layer that can **escalate 1→2 and never downgrade**, failing closed.
2. **Should an approved class graduate to Tier 1?** **No, permanently.** Graduation by accumulated
   approvals is the system writing its own spec for what needs no human — the exact self-spec'ing
   §1 forbids. Exceptions enter this file only by **a human editing it**, and this file is Tier 2.
   Precedent is not a mechanism.
3. **What if nobody reviews?** Parked-forever is correct — fail-closed is the right failure. Manage
   the **producer**, not the backlog: above K unreviewed Tier-2 items, **reflection stops
   proposing** (the same backpressure the contract already applies to Integration). The draft-PR
   list is the backlog UI; one weekly digest, not per-item alerts — alert-once stays sacred.
4. **Draft PR or ready?** **Draft** — a soft fence against merge automation, and a signal to
   external tooling. The hard fence is §2's authenticated binding, which holds either way.
5. **Does the PR leak?** **Yes, by construction** — a self-improvement PR exposes gate internals and
   rejection reasoning, *including what the reachability gate reads and where*, which is an attack
   map for this very boundary. PR bodies carry what/why/evidence only; receipts stay local; and
   **security-boundary self-improvements go to a private review surface**, never the public repo.

---

## 7. Build order

**Nothing ships until 1–5 land.** As drafted in v1 the boundary was a file deletion.

| | Fix | Prevents |
|---|---|---|
| 1 | Authenticated un-park via PR-approval webhook (§2) | the boundary reducing to "any process can `rm` a file" |
| 2 | `parked` = suspension; amend CONTRACT §11 (§5) | spec incoherence; queue rebuild choking |
| 3 | `origin` in the base schema as an enum | the security boundary resting on an undefined field |
| 4 | Fix the surface list; one machine-readable file; fail closed on new paths (§3) | `schema/**` and the prompts auto-landing Tier 1 |
| 5 | Diff-cover check (§4.0) | understating `paths` to dodge tier detection |
| 6 | Gate-executed red-first for Tier 1 (§4.3) | auto-landing tests that pin nothing |
| 7 | `tier_from_resolves` (§3) | executed self-plans landing as repairs |
| 8 | Verdict binds `reviewed_sha`; absent review = `instrument-error` | stale-tree approval; absence read as approval |
| 9 | Reflection backpressure above K parked (§6.3) | unbounded Tier-2 backlog |
| 10 | Sanitized PR bodies; private surface for boundary items (§6.5) | publishing an attack map for the boundary |
