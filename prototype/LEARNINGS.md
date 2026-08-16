# LEARNINGS

Thirty-eight laws, each learned by shipping a self-improving system into production and then
watching it lie to itself.

They are written as imperatives because they apply to any agent system, not only to this one.
Under each law is the failure that taught it — described generically, so you can recognise it
in your own system — and then where in this package the law is enforced, so you can see what
it costs to obey.

The single most expensive discovery behind all of them is this: **a system can be correctly
wired, fully tested, and completely starved.** Every function exists, every call site is
right, every test is green, and the thing the system was built to do has never once happened.
Nothing in a stack trace tells you that. Most of the laws below are about the places where
that happens.

---

## Part I — Evidence

### 1. A green suite is not evidence that a guard works. Only a mutation corpus is.

**The failure.** A safety property is asserted by a test. Later, a refactor moves the code the
test was aiming at. The test still passes, because it now exercises a different line, or
because the assertion was always satisfiable by a coincidence of the fixture. The suite stays
green for months while the guard it names has been unwired the entire time. Nobody finds out,
because the only signal a test suite emits is "green", and green looked the same before and
after.

The fix is to invert the question. Instead of asking *does the test pass?*, ask **does the
test fail when I break the thing it is guarding?** A corpus of deliberate mutations — each
one a single, precise sabotage of a named invariant, each one paired with the test that must
go red — is the only mechanical evidence that a guard is load-bearing. A mutation that stops
making its test fail is the alarm you actually want.

**Where it lives.** The counterfeit corpus: each entry mutates one line of `selfloop/` and
names the test that must turn red. Re-anchoring an entry after a refactor is a real operation
that must re-run the mutation and the tests — which is why `selfloop counterfeit --reanchor`
refuses to do it from an installed wheel and tells you to run it from a checkout. An anchor
accepted without re-running the named tests is an anchor that may no longer make anything go
red, which is the same shape as a vacuous gate applied to the thing that checks the gates.

### 2. Absence of a verdict is never the most favourable outcome.

**The failure.** This is the one that recurs in every layer. A verifier does not run — the
binary is missing, the workspace is dirty, the interpreter is unusable — and the code reads
"no complaint" as "no problem". A predicate returns `None` and the caller treats it as
truthy-enough. A row carries a payload nobody recognises and the reader concludes somebody
must have completed it. Each individual instance looks like tolerance. Together they mean the
system's grade improves every time its ability to grade itself degrades.

Say it as a rule you can apply mechanically: *when you cannot establish an outcome, you must
not record the outcome you would prefer.*

**Where it lives.** Everywhere, deliberately, because a rule this important should not have
one enforcement point. `contracts.coerce_verification` fails closed on `None`, on an empty
mapping, and on a mapping with none of the recognised keys. `contracts.outcome_class` returns
`adverse` for a status it does not recognise. `engine.CompiledGraph._finalise` stamps `FAILED`
rather than `COMPLETED` when a tick ends with no terminal status. `kit.verification_outcome`
renders `COMPLETED` only for an explicit `verified is True`. `receipts._recorded` reads an
unrecognisable envelope as `unknown`. `approvals.read_outcome` refuses a state it does not
know.

### 3. A non-result is neither a success nor a failure. Give it its own bucket.

**The failure.** Both ways of collapsing the middle value have shipped, and both were worse
than the problem they were avoiding.

Counting non-results as **failures** auto-paused four healthy production routines in one
night. Nothing was broken; they had nothing to do, and the acceptance floor read a run of
idle ticks as a run of failures.

Counting non-results as **successes** was worse. A routine that parked on the same approval
every single tick reported a 100% acceptance rate across ten self-graded runs whose declared
verifier had never executed once — and that number was then fed back in as the loop's own
training signal.

So a non-result is removed from the numerator **and** the denominator. Not down-weighted,
not counted as zero: removed. And "I cannot tell" has to be a value your reporting can
actually return, or every undecidable window gets rendered as one of the two lies above.

**Where it lives.** `contracts.NEUTRAL_STATUSES` and the three-valued `outcome_class`.
`outcome.acceptance_floor` returns `meets=None` when nothing was gradeable.
`learn._weigh_evidence` excludes neutral runs from support. `learn.attribute` leaves a
neutral run's lesson uses pending rather than grading them.

### 4. Unknown state must fail closed forever. A TTL on an unknown is the double-billing bug with a delay in front of it.

**The failure.** An effect is claimed, the request may or may not have left the process, and
then the process dies. The row says "claimed" and carries no result. The obvious convenience
is to release it after N minutes so the loop is not stuck.

That is precisely backwards. **A timer observes nothing.** Releasing on one converts every
unknown into an absence and lets the next tick re-issue the call — and it does so exactly in
the case where the first call *did* go through, because that is what "unknown" means. The
missing evidence does not live in your process; it lives at the external system. The only
honest recovery is somebody who goes and looks.

**Where it lives.** `contracts.EffectStateUnknown` and `receipts.guarded`, which refuses to
re-run a claimed-but-uncompleted row for as long as it exists. The only ways out are a tool
that has explicitly opted into `replay_on_unknown` (forbidden at T2+, refused in the
constructor) and `receipts.reconcile`, a person's signed statement about what actually
happened — recorded as its own audited row, because an escape hatch should be as visible as
the thing it escapes.

### 5. Only a side that can prove nothing left it may release a claim. A server that answered is a refusal, not an absence.

**The failure.** "We could not reach it" and "it said no" get merged into one error path,
because at the call site they look the same: an exception. Then absence starts being scored
as failure, a dependency outage reads as a run of failed effects, and the acceptance floor
auto-pauses a loop that was working perfectly. In the incident that taught this, four healthy
routines paused overnight because something they depended on was unreachable.

The discipline is narrow and checkable: no socket, connection refused, DNS failure, a missing
credential detected before the first request byte. Anything after billable work has begun is
not an absence, whatever it costs you to admit that.

**Where it lives.** `contracts.EffectUnavailable`, kept strictly separate from both
`EffectStateUnknown` and a failed effect. `kit.add_effect` stands the tick down as `IDLE` —
neutral — when it is raised, and `kit.add_status_route` diverts an unavailable effect away
from the verify node, because a verifier asked to grade an effect that was never attempted
will correctly find nothing and incorrectly call it a failure.

### 6. Record the absence; do not delete the claim. A second store call is a second crash window.

**The failure.** The intuitive way to stop an outage spending a retry budget is to *release*
the claim — delete the row — so the key is free again. But releasing is a second durable
call, and a process that dies between the claim and the release leaves the row claimed with
no result. That is the exact state that bricks the key with a permanent unknown. The release
existed to make an outage harmless and it opened a window in which an outage was permanently
harmful.

**Where it lives.** `receipts._record_unavailable` completes the row with a terminal
`unavailable` envelope instead of deleting it: one durable write, no window. The attempt loop
knows that an `unavailable` slot frees the next slot **without consuming the retry budget**,
so the property the release existed for is preserved.

### 7. A receipt must bind identity AND outcome. Binding identity alone files failures as done work.

**The failure.** A repair tool returned `{"success": false, "returncode": 1}`. The guard
completed the receipt with whatever the tool returned, so a *failed* repair was filed as a
completed effect — and because the business key was stable for the whole incident window,
that receipt then suppressed every subsequent retry. The service stayed down. The loop
reported the incident handled. Every line of that is working as written.

**Where it lives.** `receipts.guarded` marks a receipt `succeeded` only when the result does
not declare its own failure **and** the tool's verification predicate, if it declares one,
agreed. Each attempt gets its own row (`<key>`, `<key>#a2`, …) so a retry never re-opens a
row back into `claimed` — a re-opened claim is indistinguishable from the crash window, and
the retry budget becomes structural (how many rows exist) rather than a counter somebody has
to keep consistent across a crash.

### 8. Verify through a different channel than the actor. An actor's account of itself is never a verdict.

**The failure.** An API returns 200, a tool returns `{"success": true}`, a model writes "I
have completed the task". All three are the actor's narrative. A system that grades itself on
narrative will always report success, because narrative is the cheapest thing an actor
produces. The hard version of this is that a *weak* verifier is indistinguishable from a
strong one once its answer is a boolean in a ledger.

**Where it lives.** `contracts.EvidenceGrade` makes the channel a typed, ordered value:
`ACTOR_NARRATIVE` < `LOCAL_ARTIFACT` < `INDEPENDENT_DECODER` < `SYSTEM_OF_RECORD`. A verifier
that can only reach the bottom rung should refuse to answer rather than answer weakly.
`LoopTool.verify` is expected to look where the tool cannot reach — ask the supervisor whether
the service is running, not whether the subprocess exited 0 — and the conjunction with the
tool's own report is **monotone**, so adding a verifier can only ever make success harder to
claim.

### 9. A gate takes a command to execute. A gate that takes a verdict is not a gate.

**The failure.** Once any argument, field or environment variable can carry `passed=True`,
something will eventually set it — a fixture, a migration script, a helpful retry wrapper, a
hand-written file dropped next to the real evidence. In one audited evidence directory there
was a prose blob with a verdict key sitting beside the signed records; the only reason it
never counted is that the loader refused anything it had not minted itself.

**Where it lives.** `contracts.GateSpec` carries a command, a working directory, a timeout and
an environment overlay, and there is **no field through which a verdict can be supplied**.
`ports.GateRunner.run(spec) -> GateReceipt` executes; it does not record. The constraint costs
nothing and it is what makes the whole outcome ledger worth reading.

### 10. A zero-check pass is not a pass. A vacuous gate is worse than no gate.

**The failure.** A default verifier named a test file inside the loop's own repository. It
passed regardless of what the loop produced. Every loop seeded without an explicit gate
therefore settled favourable over garbage — for months, silently, while looking better on
every dashboard than a loop with a real gate.

The asymmetry is the whole point: **no gate settles every tick as visibly uncorroborated; a
vacuous gate settles every tick as invisibly accepted.** Exit status is a necessary condition
and never a sufficient one, because a command can exit 0 having collected nothing.

**Where it lives.** `GateReceipt.checks_collected` and `GateReceipt.is_vacuous`. Every runner
in `selfloop/gates.py` raises `GateUnavailable` on a zero-check receipt at the moment of
minting, and `outcome.compose` refuses one again on the way in. Skips and deselections do not
count as collected checks, and an expected failure counts with the skips — a suite of
known-broken tests must not be able to corroborate a green tick. The default evaluator in the
shipped refinement template applies the same rule one layer up: an empty spec scores `0.0`,
not `1.0`.

### 11. Say what you could not verify, in the row, at the time. "Unverified" must be greppable.

**The failure.** A single boolean column for "did it work" cannot express "nobody checked",
so the absence is silently folded into one of the two real answers, and afterwards there is
no way to distinguish a track record from a run of unexamined claims.

**Where it lives.** `OutcomeRecord` keeps three columns that can never be collapsed:
`self_reported_status` (the claim), `gate_passed` (`None` means the gate **did not run**), and
`outcome_class` (the composition). `accepted` is a derived property, so no row can ever say
"accepted" while `gate_passed` is null. `gate_unavailable_reason` and `checks_collected` sit
beside them, which is what an operator greps for when they suspect a gate has quietly stopped
testing.

---

## Part II — Authority

### 12. A human approves an action, never a slot.

**The failure.** An approval row is just a row. If what a human agreed to is not bound to the
row, then anything that later derives the same id inherits their decision — a different
payload, a different instance, a different template, a retry with edited arguments. The
approval becomes a reusable token for "somebody said yes once".

**Where it lives.** `tools.effect_binding` records template, instance, node, tool, action
class and a digest of the **exact** arguments; `approvals.read_outcome` re-checks that binding
at the moment of execution and names the fields that differ when it refuses. The digest is
computed from the real arguments and never from the redacted copy shown on the approvals page,
so the thing a human agreed to and the thing that runs are digested from the same bytes.

### 13. Put the argument digest in the approval id, or the binding check becomes a deadlock.

**The failure.** This is law 12 applied one step too literally. If the id is derived from the
business key but *not* from the arguments, then an effect whose arguments change under a fixed
key re-derives the **old** id, gets the old row back, fails the binding re-check — and can
never execute, forever, with nothing in the record explaining why. A rule that was supposed to
invalidate a stale decision instead bricks the effect.

**Where it lives.** `approvals.approval_id` includes `args_digest` in its preimage, so changed
arguments derive a *different* id and park again with a fresh row. When something else in the
binding drifts under an unchanged id, `ensure_approval` refuses loudly rather than handing back
a row that could never authorise the effect.

### 14. Expiry aborts. It never approves.

**The failure.** A decision is a statement about a moment, not a standing permission. A row
that reads "approved" past its deadline is not authority, and code that checks the state before
it checks the deadline will treat a late approval on a dead request as a licence to act.

**Where it lives.** `approvals.read_outcome` checks expiry **before** it looks at the state,
in a fixed order with four other refusals: no row, binding drift, expiry, a non-human decider,
and anything unrecognised. Those five checks are written out inline rather than delegated to a
pluggable predicate, because a substitutable implementation of this rule is a substitutable
weakening of it.

### 15. A machine must not be able to approve itself — structurally, not by policy.

**The failure.** "The loop must not approve its own work" as a rule in a document is a rule
somebody will satisfy with a service account. What you want is for there to be no string the
machine could write that would satisfy the check.

**Where it lives.** `LoopContext.actor` is always `loop:<instance>`; there is no other
identity the runtime can produce. `approvals.read_outcome` refuses a decision made by an
identity carrying that prefix (and a courtesy list of the other conventional automation
prefixes, which closes the hole an *operator* could open by wiring a bot into the decision
path). `ReconciliationRecord` refuses one in its constructor.

### 16. Name a verdict for what it does to the caller, not for what it is about.

**The failure.** A gate's core line read `if requires_approval or tier >= FLOOR: approve`. It
meant *stop and require a human*. It reads as *grant authority*. A reviewer seeing that line
for the first time read it as an auto-approve and flagged it as a live misimplementation risk
— and they were right to: the next person to touch it under time pressure would have
implemented what it says.

**Where it lives.** The verdict is spelled `park`. `GateVerdict.decision ∈ {allow, park,
deny}`, and no reading of `park` can be mistaken for granting authority.

### 17. Put the safety floor in the code, and the classification in config. Never the other way round.

**The failure.** A policy tuned for an interactive session can reasonably auto-execute a
consequential action — there is a person watching the terminal, and asking them to click twice
is friction without safety. An unattended loop inherits the policy and does **not** inherit
the person. Without a floor that the configuration cannot reach, the same permissive row that
was correct at a terminal auto-executes an outbound send at 3am with nobody in the room.

So: what an action *is* belongs in config, because it varies by shop. What must never happen
unattended belongs in the code, because it does not.

**Where it lives.** `policy.evaluate_tool` applies the **stricter of** the caller's
`PolicyPort` answer and this package's own tier floor, and it applies the floor *after* the
adapter is read. An adapter can make T0 and T1 stricter; there is no value it can return that
lowers T2. Absence is denial: a tool not in the instance's registry is denied before the
policy adapter is consulted, and an explicit denial cannot be argued out of by a lenient
classification.

### 18. Unwired safety code is not safety. A guard nobody calls is a comment with a function signature.

**The failure.** The most reassuring code in a codebase is often the code with no call sites.
A carefully written classifier, a thorough validator, a redaction pass — all present, all
tested, none reachable from the path that actually runs. The system reads as safe and behaves
as if none of it existed.

**Where it lives.** Two structural answers. First, `kit.add_effect` adds `<name>_gate` **and**
`<name>` together and returns the *gate's* name, so a template author cannot attach an ungated
effect to a graph — not by forgetting, not by refactoring, not by copying a neighbouring
template that happened to be wrong. Second, the execution seam re-derives the policy verdict
and re-reads the approval row anyway, so a template that *lost* its gate still executes zero
unauthorised effects. The first makes the safe shape the only shape a builder can produce; the
second survives the builder.

The general form: **prefer a shape a reviewer cannot forget to a rule a reviewer has to
remember.** A previous system stated the same gate rule in a docstring and enforced it by
review. The review caught every violation except the ones it did not.

### 19. Escalate; do not hammer. A permanently failing effect must reach a human, not an external system.

**The failure.** "Retry until it works" turns one broken dependency into an outage of your own
making, and a loop that retries a permission error is a loop hammering a door it is not
allowed through.

**Where it lives.** Attempt budgets are per business key: three below the approval floor, and
exactly **one** at T2+, because a tool that reports failure on an outbound send has not
necessarily failed to send it. Exhaustion raises `EffectAttemptsExhausted` — a subclass of
`EffectDenied`, so every template's existing denial handler parks on it — **without reaching
the tool**. The executor's in-process retry list is an *allowlist* of genuinely transient
faults, not a denylist, and bare `OSError` is deliberately absent from it.

### 20. A lease you can steal by age is not a lease.

**The failure.** A lock file plus "if it looks abandoned, take it" has a real mutual-exclusion
race: two workers both read the same stale holder, then interleave as unlink → create →
unlink → create, and **both** return holding the lease. It reproduces deterministically. A pid
liveness probe is the same defect with a smaller window, because a pid can be reused and the
answer is stale the instant it is read.

The deeper point is that the reclaim path exists to handle a crashed holder — and if you pick
a primitive the kernel releases on process death, there is nothing to reclaim.

**Where it lives.** `lease.FlockLease` (POSIX advisory lock) and `lease.SqliteLease`
(`BEGIN IMMEDIATE` held for the tick). Neither has a reclaim path, and `hold()` **raises**
rather than blocks — blocking merely builds a queue of processes all intending to run the same
tick. `age_s` and `stale_looking` are computed and reported on the exception as diagnostics for
a human; nothing in any acquisition path reads them. The lease file is never unlinked, because
unlinking would let a later `open` create a different inode that nobody's lock covers.

### 21. A degradation that keeps running is worse than a refusal.

**The failure.** On a host without the POSIX primitive, the obvious fallback for a lease is an
in-process lock. Two scheduled invocations then both acquire it, both read the same
checkpoint, and both run the tick — and every symptom of that is a duplicate effect nobody
attributes to the lease. The loop keeps running, reports nothing unusual, and has quietly
stopped being safe.

**Where it lives.** `InProcessLease` refuses to be constructed without
`accept_single_process_only=True`, so the choice appears in the caller's source where a
reviewer can see it. The CLI refuses to run a tick when it cannot verify the lease excludes
another OS process, unless `--accept-inprocess-lease` is passed.

---

## Part III — Learning

### 22. A promotion score computed from post-promotion counters can never fire.

**This is the most important law in the file.** It is the bug that made a fully-built learning
system produce nothing at all: **two hundred and seven candidates staged, zero ever promoted,
forever.**

**The failure.** The promotion gate required a confidence bound over `helped / used`. Those
counters are written by attribution — which only runs *after* a lesson has been promoted and
injected. So at first promotion `used == 0`, the bound is exactly `0.0`, and any positive
threshold is unsatisfiable. The gate was correctly wired, every function existed, every call
site was right, and it was mathematically always closed. No test caught it, because every test
of a *promoted* lesson passed.

The general form: **never gate an entry condition on a metric that only exists after entry.**
Ask of every threshold you write: *can this ever actually fire, from a cold start?* Write the
answer next to it.

**Where it lives.** The two metric families are split and never touch. Admission
(`learn._weigh_evidence` → `learn.promote`) reads **pre**-injection evidence only: distinct
contributing runs, and whether they agree on one failure tag. `wilson_lower_bound(helped,
used)` is used for recall *ranking* and for regression *retirement*, and nowhere else. A unit
test asserts that a virgin candidate with `used == 0` promotes when support is met, and a
counterfeit entry re-adds the confidence check to `promote()` and requires that test to go red.

### 23. A candidate id that changes as evidence arrives can never accumulate support.

**The failure.** The same starvation, reached from a completely independent direction. An id
derived from the content key *plus the evidence ids* changes every time a new run contributes
evidence — so an insert-once store mints a fresh row each time, every recurrence of the same
failure creates a new candidate with support 1, and nothing ever crosses a threshold. It is
invisible in any test that stages a candidate once.

Its twin is a *content key* derived from the tokens of the clustered signals: tokens grow with
every new report, so the key moves for the same reason.

**Where it lives.** `learn.candidate_id` hashes the stable content key and **nothing else**.
`learn.cluster_key` is derived from `(scope, failure_tag)` alone. Evidence ids are an appended
set inside the payload, written through a compare-and-set.

### 24. Support is a count of distinct runs, never a count of observations.

**The failure.** Ten error lines from one bad night are one night's worth of evidence.
Counting them as ten is how a single flaky evening promotes a rule about a system that was
fine.

**Where it lives.** `Cluster.support` and `Admission.support` both count distinct `run_id`s.
Every number in the learning pass that feeds a gate is a count of runs, and the same function
computes the counter stored on the row and the number the gate reads — so the value an
operator sees and the value the gate applies can never mean two different things.

### 25. Never learn from your own prose.

**The failure.** A system promoted knowledge by matching an agent's own output text against a
fact id — effectively `output_text LIKE '%fact_id=<id>%'`. So an agent that *mentioned* a fact
in its report thereby promoted that fact. The agent could write its own memory by talking about
it. This is not an exotic bug; it is what happens whenever the same channel carries both the
work and the evidence about the work.

**Where it lives.** A signal's **existence** and its **partition** come from structured columns
written by the grading path — an outcome class, a failure tag, a verifier's boolean. Free text
only ever contributes *tokens inside* a partition that those columns already established, where
the worst it can do is change which wording a human reads on the approvals page.

### 26. Never learn from a non-result.

**The failure.** "A stage produced no artifact" is true of every idle tick, every parked tick
and every authorization-blocked tick — all of which are working as designed. At a similarity
threshold of 0.3 that token soup clusters fast, and the system auto-promotes lessons about
nothing. The mirror image poisons the other end: counting a park or an abort as a *use* of an
injected lesson, without a corresponding help, collapses its confidence bound on evidence that
was never about the lesson at all, and a flaky weekend deletes everything the system learned.

**Where it lives.** No signal may be derived from a neutral status; the shipped sources are
restricted to an effect that self-declared success and failed its independent verifier, a
recorded effect failure, and an adverse outcome carrying a failure tag.
`learn._weigh_evidence` excludes evidence whose run settled neutral. `learn.attribute`
finalises a lesson use only when a non-neutral outcome exists, and leaves it pending otherwise.

### 27. Partition before you compare. A group with no shared structured tag is not a cluster.

**The failure.** Raw-token similarity conflates unrelated failures, because `error`, `failed`,
`in` and `line` appear in everything. At any usable threshold they form one enormous trash
cluster, and the "lesson" that emerges from it is an amalgamation of contradictory fixes —
confidently supported, and wrong about every case it covers.

**Where it lives.** `learn.cluster` partitions by `(scope, failure_tag)` *before* a single
token is compared, and similarity runs only *within* a partition, where its only job is
choosing which wording best represents it. `LearningSignal.__post_init__` refuses a signal with
no scope or no failure tag, so an untagged signal cannot exist to be mis-clustered.

### 28. Never let the actor author what gets injected. Let it choose which pre-authored thing applies.

**The failure.** A promoted lesson's guidance is the only part that reaches the next run's
prompt, and "who writes it" has three unstated answers, all bad. Using the failure description
injects a complaint, not a remedy. Having a model write it reintroduces exactly law 25 with a
longer sentence in it. Requiring a human to write every one means nothing ever auto-promotes
and the machine's half of the loop is decorative.

The resolution is a division of labour: **the machine decides which remedy applies; a human
decided what the remedy says, in advance, before any evidence existed.** Neither half can be
forged by the actor.

**Where it lives.** `guidance.guidance_for` is pure and model-free: it fills
`when <failure_tag> then <remedy>` from a caller-supplied table. A tag with no entry yields
guidance marked `[needs-human]`, and `learn.stage` forces such a lesson to the approval floor
tier — human text, human approval. The tier is computed once, at staging, and stored on the
row, so editing the text afterwards cannot lower it.

### 29. Bind a verdict to content, not to an id. Ids are transferable; content is not.

**The failure.** A validated id plus a mutable row is a time-of-check/time-of-use hole. The
row's content changes between the check and the use — an evidence-append path that also
touches the text, two passes overlapping, an operator editing a field in the store — and
unvalidated content then applies under a validated id.

**Where it lives.** `contracts.lesson_fingerprint` is computed at staging and stored on the
row. Promotion re-reads the row and recomputes it immediately before the write; recall
recomputes it again at read time. Any drift **skips** the row — it is never repaired, because
recomputing the fingerprint to match is exactly the bind being defeated. `EvidenceRecord`
carries the same idea for gate verdicts: a digest of the exact content that was judged, so a
verdict cannot be inherited by whatever later occupies that id.

### 30. Give the pass one owner, and move the cursor last.

**The failure.** The same learning pass mounted in two places — once in the driver, once as a
graph node — gives the extract/stage/promote cycle two owners: double mining, racing cursors,
and a promotion parking outside the executor's own park/resume protocol. Separately: a cursor
advanced *as you go* skips a window when the pass crashes halfway, and skipped evidence is
evidence nobody ever learns is missing.

The asymmetry that decides it: **re-mining costs time; skipping costs evidence, and only one of
those is recoverable.**

**Where it lives.** `runtime.run_once` is the only caller of `learn.learning_pass`; there is no
learning graph node. The cursor advances last, only if everything before it completed, and
`Harvest.high_water` is pinned back to the starting cursor whenever any source raised.
Re-mining is safe because signal ids are content-stable and writes are insert-if-absent — and
the *stored* row wins over the freshly mined one, so re-mined evidence keeps its original run
attribution and support cannot be inflated by a crash.

### 31. The cursor must be an integer the log assigns, never a timestamp.

**The failure.** Two events written in the same millisecond are unordered, so a time-window
scan either double-counts them or drops them. Worse, a clock that steps backwards silently
re-mines an arbitrary stretch of settled history as if it were new evidence — and because
candidate ids are content-stable, that evidence lands on the same candidates.

**Where it lives.** `ports.EventLog.append` returns a strictly increasing integer, monotonic
across processes and restarts, and that integer *is* the cursor. The sqlite adapter uses
`AUTOINCREMENT` rather than a plain rowid, because the plain form reuses the largest deleted
rowid and can hand the same integer out twice. `ledger.advance_cursor` never moves backwards
and is a compare-and-set.

---

## Part IV — Working practice

### 32. Separate the record stamp from the freshness clock.

**The failure.** A verifier slower than an anti-forgery window read as future-dated against a
wall clock that had been adjusted mid-run, and a *passing* verdict settled adverse. That defect
had zero test coverage anywhere before it shipped, because every test used a wall clock that
behaved.

**Where it lives.** `ports.Clock` has two methods for two purposes: `now_iso()` is a record
stamp a caller may legitimately pin (a backfill that stamps yesterday's events with today's
time has falsified an audit trail), and `elapsed()` is a monotonic source that is never pinned
and never steps. Every freshness, age and anti-forgery check reads `elapsed()`. The three
places that read a wall-clock stamp — approval expiry, lesson decay, lease diagnostics — each
say in their docstring why they must, and what it costs.

### 33. "Append-only" is two policies. Picking the wrong one is a correctness bug.

**The failure.** A run that can overwrite its own report card has an audit trail that proves
only what the run most recently wanted it to prove. A cursor that *cannot* be overwritten can
never advance. A store that offers only one write policy is wrong for half of your records.

**Where it lives.** `ledger.write_history` (insert-if-absent) for outcomes, evidence,
reconciliations and decisions; `ledger.write_cache` (last-write-wins) for mirrored receipts,
counters and the cursor. Both are named functions rather than a `RecordStore` method a caller
picks from memory, and the docstrings say which failure each one prevents.

### 34. Observability must never fail the tick — and a durable write must.

**The failure.** An exception from a log append escapes, a tick that did its work correctly is
recorded as failed, that failure counts against the acceptance floor, and the loop auto-pauses
because its *monitoring* broke. That failure mode is worse than the one it would have been
reporting.

The converse is equally important and points the other way: a *lesson* that failed to persist
has not been learned, and a caller that carries on as though it had is reporting progress it
did not make.

**Where it lives.** `ledger.emit` never raises and returns `0` (an invalid cursor, so a caller
that stores it re-mines from the beginning — the safe direction). `write_history` and
`write_cache` propagate. `BaseException` is deliberately not caught anywhere: a loop you cannot
interrupt is a worse operational problem than a loop that lost a log line.

### 35. An empty list is not an empty inbox. A dead credential must raise.

**The failure.** A read tool whose grant was revoked returns `[]`. That renders identically to
"there was nothing to do". The loop idles green forever, reports nothing wrong, and nobody is
told — until somebody notices by hand that a queue stopped draining, days later.

**Where it lives.** `contracts.BlockedLoopError` with a machine-readable cause. It is in the
shared vocabulary rather than in a template because it is a distinction every loop must be able
to make, and it is deliberately absent from the executor's transient-retry allowlist so it
propagates on the first attempt instead of burning three retries on a credential that will
never work. `BLOCKED` is **adverse**, so it trips the acceptance floor and reaches an operator,
where `IDLE` would not.

### 36. Bound everything that accumulates, and put the bound where the schema is.

**The failure.** A long-lived instance ticks forever on one thread id. An uncapped accumulating
channel grows the durable checkpoint without limit until the row is too large to read, and the
loop stops resuming for a reason that has nothing to do with its logic.

**Where it lives.** `contracts.LOG_CAP` on the `log` and `effects` channels, declared as
`Annotated` reducers on the state schema itself and discovered by the executor — so adding an
accumulating channel is a one-line edit rather than a hand-maintained table in the executor
that goes stale silently. The refinement template caps its cross-tick trajectory the same way,
and `recall_k` bounds the injected block, because an unbounded memory block competes for the
same context the task needs and is how a "memory" feature becomes a quality regression.

### 37. A refusal must say what to do next.

**The failure.** A loud failure that does not name its recovery path is only half loud. An
operator reading "recursion limit reached" learns that something misbehaved but not where, and
the answer was always in the cycle the loop had been going round.

**Where it lives.** `RecursionExceeded` carries the path and renders the repeating segment with
per-node counts. `EffectStateUnknown` names the reconcile command, from a hint injected by the
caller rather than hardcoded — a message naming a module in *our* repository is meaningless
advice to somebody who installed the library. `UnknownToolError` lists what *is* granted.
`get_template` lists the catalogue.

### 38. Publish the cut list. A budget nobody names is paid for out of the safety work.

**The failure.** When scope is under pressure and no one has written down what will be dropped,
the things that get dropped are the ones with no user waiting: the mutation corpus, the kill
drill, the tests for the part that has never worked. Then the remaining suite is green and
guards less than it did.

**Where it lives.** The cut list is published in the plan and repeated in the README's *What is
deliberately not here*: a JSONL adapter deferred, `contrib/` deferred and documented as seams,
three of five templates dropped, the kill drill run over one template — and, explicitly,
**coverage of the learning loop is not cuttable**, because a previous system's eighty-seven
patches touched zero learning code and that is exactly why it starved.

---

## The shortest version

If you keep nothing else:

1. Ask of every gate you write: **can this ever actually fire, from a cold start?**
2. Ask of every test you write: **does it fail when I break the thing it guards?**
3. Ask of every absence: **am I about to record the outcome I would prefer?**
