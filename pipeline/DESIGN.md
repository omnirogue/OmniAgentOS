# Three-Loop Autonomous Engineering System — interface design
**2026-08-07 · grounded in 149 commits / 13 gated trains / 90 recorded gate refusals**

> **Status: rationale document. `CONTRACT.md` v1.1 is normative and supersedes it wherever the two
> disagree.** This file explains *why* the system is shaped this way and remains the reference for
> Integration's gate semantics and base-freeze rules (§3, §5). Its illustrative details predate the
> contract and were not retrofitted — artifact ids are now `sha256:<64 hex>` (not `cand_7f3a`),
> receipts are keyed `receipts/<id>/` (not `<sha>`), rejections are files in `rejected/` rather
> than a `kind`, and the roles are five, not three. **Build against `CONTRACT.md`.**

Your three loops are the right decomposition. What follows is the part that decides whether
they work: **the contract between them.** Everything here is drawn from measured failures on
this estate, cited inline, because a design for an autonomous system should be argued from what
actually broke, not from what sounds robust.

---

## 0. The five findings that shape the whole design

**(1) Most failures are the INSTRUMENT, not the work.** Of 90 recorded merge-gate refusals:
32 reachability, 15 unpinned workspace, 15 dirty workspace, 4 bad receipt — **64 of 90 were
mechanics.** Only 10 ladder + 9 counterfeit + 7 other concerned the candidate's code.
**A loop that cannot tell "my tooling failed" from "the code is wrong" will spend two-thirds of
its life debugging the wrong thing, forever, with no human to notice.**

**(2) Retry without a changed input is the dominant waste.** ONE symbol drew **28 identical
reachability refusals**, ~10 min each ≈ 4.5h, because the fix was written where the gate does
not read. A separate incident fired **3,951 launches against a terminal provider error, $600**.
Unbounded retry is the failure mode of autonomy.

**(3) Guards pass while protecting nothing.** Found repeatedly this week: a fail-closed filter
with **zero production callers** (complete, tested, passing, wired to nothing); a test whose
subject-discovery skipped the one file it missed, and a *skip counts as a pass*; a safety rule
whose predicate could never reach its threshold, so it had **never fired once**; a repair tool
that exited 0 after a half-landed write. **Green is not evidence. Green on a population that
can contain the failure is evidence.**

**(4) One fix is structurally 2–13 fixes.** Census: **518 clone families**, 1,367 functions
inside one, 147 families with k≥3. Every lane needed 3 review rounds; each round fixed the named
site and missed its sibling. The identical habit appears in a careful human's PRs *with good
tests*. This is not a discipline problem and cannot be prompted away.

**(5) Escalating the model on repeat failure is killed on evidence.** Both recurring defect
classes shipped at maximum effort from Claude, GPT-5.6-Sol, Gemini and Grok alike. What worked
was **changing the action**: a different lineage (caught a live auto-approve bypass that
same-lineage review missed), a mechanical enumeration, or looking at what the gate reads.

---

## 1. The medium: git + a typed queue on disk

No database. Every inter-loop artifact is **a file in the repo or in `var/loopqueue/`**, so it
survives a crash, is greppable, diffable, and reviewable by a human who wanders in.

```
var/loopqueue/
  proposals/    <id>.json    Planning  → Integration   (implementation-ready plans)
  candidates/   <id>.json    Repair    → Integration   (finished, reviewed branches)
  findings/     <id>.json    any loop  → Repair        (defects not yet worked)
  rejected/     <id>.json    Integration → all         (with reason + TTL)
  state/        queue.json   Integration → all         (depth, WIP, backpressure)
                budget.json  governor  → all           (spend + resource ceilings)
  receipts/     <sha>/…      every loop                (what ran, on what tree)
```

**Every artifact carries the same envelope.** This is the single most important schema in the
system:

```jsonc
{
  "id": "cand_7f3a…",              // content hash of the payload — see §4, idempotence
  "kind": "candidate|proposal|finding|rejection",
  "producer": "repair|planning|integration",
  "created_at": "2026-08-07T11:33:49Z",
  "base_sha": "5763dd78…",         // the tree this was derived from. NOT optional.
  "branch": "lane/…",              // candidates only
  "paths": ["omniagentos/…"],      // every file it touches — the conflict key (§5)
  "evidence": [                    // claims a human or loop can re-run. Never prose alone.
    {"claim": "…", "command": "…", "expected": "…", "verified_by": "execution|reading"}
  ],
  "verdicts": [                    // §3 — admission requires ≥1 cross-lineage APPROVE
    {"reviewer": "codex-critic", "lineage": "openai", "verdict": "approve",
     "reviewed_sha": "7e7391e1…", "at": "…"}
  ],                               // `lineage` REQUIRED: it is what the gate compares, and a
                                   // verdict omitting it was admitted as a disclosed review.
                                   // Absence of the whole array is refused for a house
                                   // producer; only `producer.third_party: true` — declared,
                                   // never inferred from silence, and reported whenever used —
                                   // buys ROUTING.md §2's third-party exemption.
                                   // NOT YET TRUE OF THE CODE: the gate enforces "≥1
                                   // cross-lineage VERDICT", not "≥1 cross-lineage APPROVE" —
                                   // a cross-lineage `reject` is admitted. This line is the
                                   // specification; the gap is finding sha256:e2e40b42.
  "supersedes": "cand_…",          // explicit, so replacing work is not a duplicate
  "attempts": 2,                   // §4 — storm control
  "class": "candidate-defect|instrument-error|blocked-on-human"
}
```

`base_sha` and `reviewed_sha` are load-bearing. **A verdict is bound to the tree it reviewed.**
Tonight a receipt bound to `(candidate_sha, merge_base_sha)` was invalidated the moment
something landed on main mid-gate, which cost a full re-gate. That property is a feature: it
makes "reviewed" mean something. Carry it into every artifact.

---

## 2. `class` — the field that saves two-thirds of the compute

From finding (1). Every failure any loop produces MUST be classified before it is acted on:

| class | meaning | who owns it | retry? |
|---|---|---|---|
| `candidate-defect` | the code is wrong | Repair | yes, with a **changed** input |
| `instrument-error` | tooling/host/env failed; says NOTHING about the code | governor | yes, same input, after remediation |
| `blocked-on-human` | needs a decision no loop may make | park + alert once | never |

**An instrument-error must never be reported as a candidate defect.** Tonight a CI job with no
git identity made `git merge` exit 128, and the gate mapped every non-zero merge exit to
"conflicts against main" — **a fabricated accusation against the candidate**, with every test
silently skipped. A Repair Loop reading that would have spent the night rewriting correct code.

Rule: any tool the loops depend on must distinguish "I could not run" from "it failed", and the
envelope must carry which. Where a tool cannot, wrap it until it can.

---

## 3. Admission control: what Integration will accept

Integration is a **gate, not a queue-drainer.** It refuses anything failing these, and the
refusal is itself an artifact in `rejected/` with a machine-readable reason:

1. **`base_sha` resolves** and is an ancestor of, or equal to, current main.
2. **≥1 `verdict: approve` from a DIFFERENT LINEAGE than the producer.** Non-negotiable — a
   different lineage caught a live auto-approve bypass that same-lineage review had cleared,
   twice. Encode the rule in the schema, not in a prompt.
3. **Every finding that reached blocker/major has an adversarial verdict.** Default of the
   verifier is *the finding is wrong*. Tonight this refuted 2 of 2 majors on a human's PRs — both
   reproduced mechanically but were pre-existing, not his doing. **Without this step the system
   sends false work to people and burns their day.**
4. **`evidence[]` is re-runnable**, and at least one entry is `verified_by: execution`.
5. **A test that pins the fix FAILS against `base_sha`.** Verified by reverting only the product
   change. 11 of 15 prior rejections on this estate were tests incapable of catching their
   claimed defect.
6. **`paths[]` is complete** — used for conflict planning (§5). An understated `paths` is a
   correctness bug in the queue, not a nit.

---

## 4. Idempotence, storms, and the rejected ledger

`id` is the **content hash of the payload**, so the same proposal produced twice is the same id.
This is what stops the Planning Loop rediscovering an idea forever.

- **`rejected/<id>.json` carries the reason and a TTL.** Planning MUST read it before proposing;
  a proposal whose id is in `rejected/` and unexpired is dropped at source. TTL exists because
  a bad idea in March can be a good idea in June — but it must be *re-argued*, not re-submitted.
- **Retry semantics, shared by all three loops:** `0` pass · `1` candidate-defect ·
  `2` **do-not-retry-this-input**. A loop that retries on 2 recreates the storm class.
- **`attempts` is per `(id, base_sha)`.** Same input refused twice → the loop must change the
  input or change the action, never repeat the pair. Three → park with an alert.
- **On no-progress, change the ACTION, not the model tier** (finding 5). The escalation ladder
  is: different lineage → mechanical enumeration of the sibling set → inspect what the
  instrument actually reads. Model escalation is *not* on the ladder.

---

## 5. Conflict planning — Integration's real job

Integration's hard problem is not merging, it is **ordering**.

- Build a conflict graph from `paths[]` across everything admitted. Items with disjoint paths
  are one batch; overlapping items serialise, highest-confidence first.
- **Verify by machine, not by inspection:** `git merge-tree --write-tree main <branch>` per
  candidate. This is cheap and it settled a real argument tonight — a lane assumed to conflict
  across 92 commits of drift returned rc=0, zero conflicts.
- **Batch aggressively.** Landing N items individually invalidates N−1 successor receipts, so
  interleaving pays the serialisation tax repeatedly. Seven PRs landed as one train tonight in a
  single gate cycle.
- **Base freeze.** Nothing lands on main while a gate is in flight — a receipt binds a merge
  base, and a mid-gate landing invalidates every in-flight receipt. Integration holds the only
  write lock on main. **This is why there is exactly one Integration Loop.**
- **Semantic conflict is not textual conflict.** Two items can merge cleanly and still break each
  other. Where `paths[]` overlap in a *contract* (a schema, an enum, a status vocabulary),
  require both to be in the same batch so the gate tests them together.

---

## 6. The governor — the piece your prompt is missing entirely

"Run forever" without a governor is how a loop fires 3,951 launches at a terminal error.
A separate, tiny, **non-agentic** process owns `state/budget.json`. Every loop checks it before
spawning work and after each iteration:

- **Spend ceiling** per loop per day, hard. On breach: park, alert once, do not retry.
- **Resource gate.** Tonight the disk hit **100% full** and a parallel run died with `ENOSPC`
  that read like a code defect; separately load hit **101 on 16 cores**, self-inflicted. Refuse
  to spawn when free disk < N GB or 1-min load > cores. **Defer, never fail the candidate** —
  contention is an `instrument-error`, and treating it as a defect starts a false investigation.
- **Terminal-error rule:** quota, auth, suspension → max 5 attempts, park, alert once.
- **Concurrency cap** per loop, and a global cap below core count.

The governor is deliberately dumb and deterministic. It is the one component that must never
have a model in the decision path.

---

## 7. Per-loop deltas to your prompt

### Planning Loop
Your version is strong on breadth and weak on **what it reads and what stops it repeating**.
- Its state read is: `state/queue.json`, `rejected/` (mandatory, §4), `receipts/` (what actually
  ran and what it cost), the architecture doc, and telemetry. Add: **the plan file must be
  updated as work lands.** Tonight the capability plan marked exactly one item DONE while a
  dozen had shipped — the plan was stale against reality and neither the operator nor I could
  read it and know where we stood. **A planning loop whose own plan is stale is the
  favourable-absence class applied to planning.**
- Kill "submit approved plans" — say *approved by whom*. Under this design: approved = survives
  adversarial review (§3.3) and is not in `rejected/`.
- Add a required field: **`falsifier`** — what observation would prove this plan unnecessary.
  A proposal that cannot be falsified is not implementation-ready.

### Repair Loop
- Add the **enumeration obligation** (finding 4): before proposing a fix, name the VALUE it
  protects and enumerate every carrier — every writer, every reader, every branch that decides
  the same policy. For defects that are not about a persisted value, enumerate **judgment
  sites**: every path that classifies, scores, approves, or binds. Tonight one approval bypass
  was exactly this — one policy, two judgment sites, opposite conventions.
- The enumeration goes in the artifact as a table with a ruling per row. **The reviewer then
  audits the enumeration for omissions instead of hunting for instance N+1** — hunting is
  unbounded and converges one element per round by construction; auditing a 30-row list is
  bounded. A tool exists for generating this (`sibling-enum`), measured at 8/8 recall on real
  PRs.
- **Never merge** is right; add **never widen scope silently** — a repair that grows beyond its
  finding must produce a new artifact, not a bigger one.

### Integration Loop
- Give it an explicit **refusal vocabulary** and require every refusal to name its own remedy.
  The 28-refusal episode happened because the gate said "not exempt" without saying *the exempt
  file is read from the checkout the gate RUNS IN, so land it on main first*. A refusal that
  names its remedy converts a loop into a single round.
- **Preserve failure evidence before cleanup.** Tonight a refusal named the failing test and
  then deleted the assertion with its scratch directory, which cost four reproduction attempts
  that all came back green. Any evidence glob declared by a step must be copied to
  `receipts/<sha>/` before teardown.
- Integration is also the **learning writer**: on every outcome it appends to a durable ledger
  (what was proposed, what shipped, what refused, what class, how many attempts). That ledger is
  the Planning Loop's primary input. Without it "self-learning" is a claim, not a mechanism.

---

## 8. Where a human is still required — and say so in the prompt

The honest version of "minimal human involvement" is a **short, explicit list** rather than an
aspiration. On current evidence:

1. **Product/vocabulary decisions** — three sat open tonight (a review cadence vs auto-promotion,
   a default API projection, which of two cost vocabularies is canonical). No loop should choose
   these; they are preference, not correctness.
2. **Arming anything self-modifying.** A 10,804-line self-learning stack should land *dark* and
   be armed by a person, once.
3. **Access and spend changes.**
4. **Any `blocked-on-human` class artifact**, by construction.

Everything else can run unattended *if* §2 (classification), §4 (storm control) and §6
(governor) exist. Without those three, "run forever" is a $600 incident with extra steps.

---

## 9. Build order

1. **The envelope + `class` field + rejected ledger.** Nothing else works without it. ~1 day.
2. **The governor.** Before any loop runs unattended. ~half a day, no model involved.
3. **Integration's admission control** (§3) — it is the only component that can refuse, so it is
   the only place safety can be enforced.
4. **Repair Loop with the enumeration obligation.** Fixes today's problems and exercises §1–3.
5. **Planning Loop last** — it is the one that most needs the receipts and ledger the others
   produce, and the least urgent while a human is still setting direction.

Running Planning first is tempting and wrong: it will generate plans nobody can execute safely
yet, and the rejected ledger it needs to avoid repeating itself will not exist.
