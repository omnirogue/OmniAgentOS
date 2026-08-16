# Architecture

Eighteen modules, two subpackages, one direction of dependency, and three durable stores. This
file is the map: what each module owns, what it may import, what survives a crash, and what the
next tick does about every place a process can die.

The organising rule for the whole layout is stated in `contracts.py`'s own docstring: **if two
modules both need a type, the type lives in `contracts`.** An earlier decomposition put the
tool types next to the seam and the learning types next to the learner, and produced a genuine
import cycle within a day (`tools` needed `receipts.guarded`; `receipts` needed `LoopTool` to
call `tool.verify`).

---

## The import DAG

```
                            ┌──────────────┐   ┌─────────┐   ┌────────────┐
   layer 0  (stdlib only)   │ contracts.py │   │ stats.py│   │ guidance.py│
                            └──────┬───────┘   └────┬────┘   └─────┬──────┘
                                   │                │              │
              ┌────────────┬───────┼────────────┐   │              │
              ▼            ▼       ▼            ▼   │              │
   layer 1  ports.py   engine.py  ledger.py  lease.py              │
              │                     │                              │
              ▼                     ▼                              │
   layer 2  context.py ─────────► outcome.py                       │
              │                     │                              │
      ┌───────┼──────────┬──────────┴───────┐                      │
      ▼       ▼          ▼                  ▼                      │
   layer 3  policy.py  approvals.py     receipts.py    gates.py     │
              │           │  ▲              │                      │
              └───────────┼──┼──────────────┘                      │
                          ▼  ╎ (deferred)                          │
   layer 4              tools.py                                   │
                          │  ╎                                     │
                          │  ╎ (deferred)                          │
              ┌───────────┴──┴──────────────┐                      │
              ▼                             ▼                      │
   layer 5  kit.py ───────────────────► learn.py ◄─────────────────┘
              │                             ▲
              ▼                             │
   layer 6  templates/  (catalogue + observe_decide_act_verify
              │                            + propose_evaluate_promote)
              ▼
   layer 7  runtime.py ──► cli.py           adapters/  (memory, sqlite)
```

Two edges are **deferred imports inside a function**, and both are deliberate:

* `approvals.ensure_approval` → `tools.effect_binding`. `tools` imports `approvals` at module
  scope, because the seam is what needs the approval machinery. Deferring the one call back
  keeps the dependency one-directional on paper and resolvable in practice.
* `learn._promote_through_approval` → `tools.effect_binding`. Same direction, plus a second
  reason: importing `selfloop.tools` **installs the execution seam's sealer**, and reading
  lessons must not have that side effect.

In both cases the binding is derived from the same function that stored it, rather than
rebuilt from the same six fields and left to drift — `read_outcome` refuses on any difference,
forever.

One more shape worth knowing: `templates/__init__.py` imports its two template modules at the
**bottom** of the file, below the `LoopTemplate` class. Each template does
`from selfloop.templates import LoopTemplate`, so that edge is a cycle which resolves only
because the class is already bound when it is traversed. A formatter that hoists those two
lines to the top turns `import selfloop.templates` into an `ImportError`.

---

## Module by module

### Layer 0 — vocabulary and pure functions

| Module | Owns | Public API (abridged) | May import |
|---|---|---|---|
| `contracts.py` | The frozen vocabulary: every name two modules would both need | `RiskTier`, `ActionClass`, `LoopStatus` + the three disjoint status sets, `outcome_class`, the whole error taxonomy, `EvidenceGrade`, `LoopState`/`initial_state`, `RunReport`, `GateVerdict`/`PolicyDecision`, `LoopTool`/`ToolRegistry`/`install_sealer`, `GateSpec`/`GateReceipt`, `RecordKind`, `Lesson`/`LessonStatus`/`LearningSignal`, `lesson_fingerprint`, `digest_key`/`args_digest` | stdlib only |
| `stats.py` | The evidence weighting, as four pure functions | `wilson_lower_bound`, `decay_weight`, `normalise_tokens`, `jaccard` | stdlib only |
| `guidance.py` | Who authors the text that gets injected | `guidance_for`, `is_template_derived`, `NEEDS_HUMAN_MARKER` | stdlib only |

`stats.py` is small on purpose: it is the file you edit to change how the loop weighs evidence,
and it is short enough that changing it is a decision rather than an excavation. `guidance.py`
declares the cluster shape it reads as a `Protocol` rather than importing it, so there is no
edge back to `learn` even under `TYPE_CHECKING`.

### Layer 1 — declarations, execution, records, exclusion

| Module | Owns | Public API (abridged) | May import |
|---|---|---|---|
| `ports.py` | The twelve host seams, as `Protocol` — declarations only, no code | `Clock`, `ReceiptStore`, `ApprovalStore`, `RecordStore`, `EventLog`, `CheckpointStore`, `LeasePort`, `PolicyPort`, `ModelPort`, `GateRunner`, `Notifier`, `SignalSource` | `contracts` |
| `engine.py` | The durable executor — a hand-written sequential-with-branching state machine | `Graph`, `CompiledGraph`, `Snapshot`, `ParkRequested`, `END`, `merge_state`, `observed`, `ExecutorPort` | `contracts` |
| `ledger.py` | Seven durable row shapes, two write policies, one cursor | `OutcomeRecord`, `EvidenceRecord`, `ReceiptRecord`, `DecisionRecord`, `LessonUseRecord`, `ReconciliationRecord`, `EventRecord`, `write_history`, `write_cache`, `emit`, `read_events`, `read_cursor`, `advance_cursor` | `contracts` |
| `lease.py` | Per-instance mutual exclusion | `FlockLease`, `SqliteLease`, `InProcessLease`, `flock_available` | `contracts` |

The Protocols are deliberately **not** `runtime_checkable`: several carry attributes as well as
methods, and a half-working structural check invites callers to treat a passing `isinstance` as
proof of obligations it cannot see. Verify an adapter with tests.

`engine.py` imports `contracts` and the standard library and nothing else — `LoopContext` is
imported under `TYPE_CHECKING` for annotations only — so the executor sits at the bottom of the
DAG and can be read and tested without pulling in a single adapter.

### Layer 2 — the context and the grading policy

| Module | Owns | Public API | May import |
|---|---|---|---|
| `context.py` | Everything one loop instance is allowed to reach | `LoopContext` (frozen, keyword-only), `.actor`, `.thread_id`, `.scope_tier`, `.remedy_for` | `contracts`, `ports` |
| `outcome.py` | The three-valued honesty layer | `compose`, `settlement_of`, `acceptance_floor`, `Settlement`, `classify_settlement`, `artifact_bytes` | `contracts`, `ledger` |

`context.py` imports no adapter. An earlier revision had a `for_testing()` helper here that
built a context from the in-memory adapters, which made the foundation depend on a layer above
it. Test fixtures build contexts; contexts do not build themselves.

`outcome.py` does no I/O, holds no state and imports no port. Every rule in it is a pure
function over a claim and a receipt, so the entire grading policy of an unattended loop can be
checked by reading one file.

### Layer 3 — the seam stack's dependencies

| Module | Owns | Public API | May import |
|---|---|---|---|
| `policy.py` | The tier floor — the one rule a caller's adapter cannot argue with | `evaluate_tool`, `preview`, `TierPolicy`, `TIER_POLICY_TABLE` | `context`, `contracts` |
| `approvals.py` | The park/approve bridge: mint once, read as authority | `approval_id`, `ensure_approval`, `read_outcome`, `resolve_for_resume`, `page`, `deep_link`, `redact_args` | `context`, `contracts`, `ledger`, (deferred: `tools`) |
| `receipts.py` | Idempotency receipts: claim, act, complete | `receipt_key`, `attempt_key`, `guarded`, `reconcile`, `receipt_state`, `receipt_exists`, `declared_failure` | `context`, `contracts`, `ledger` |
| `gates.py` | Three runners, and the rule that a zero-check pass is not a pass | `ArtifactGate`, `CommandGate`, `NullGate`, `parse_check_counts`, `CheckCounts` | `contracts`, `outcome`, `ports` |

`gates.py` is on this layer by dependency but is not part of the seam stack: nothing in the
runtime imports it. It is a set of adapters for one port, and a caller who supplies their own
`GateRunner` never loads it.

### Layer 4 — the execution seam

| Module | Owns | Public API | May import |
|---|---|---|---|
| `tools.py` | **The one place a tool is ever invoked**, plus the seal | `execute_effect`, `effect_binding`, `EFFECT_MODE`, `READ_MODE` | `approvals`, `policy`, `receipts`, `context`, `contracts` |

`_invoke_in_seam` is module-private and stays that way. As a public `invoke(tool, args)` it
*was* the bypass: anything holding a `LoopTool` could run an irreversible effect through it
with no verdict, no approval and no receipt. Importing this module installs the sealer into
`contracts` — which is the one import-time action in the package, and the reason a registry
built in a process that never imported `selfloop.tools` registers tools **unsealed**.

### Layer 5 — building blocks and the learner

| Module | Owns | Public API | May import |
|---|---|---|---|
| `kit.py` | The node builders that make "a gate precedes every effect" structural | `add_effect`, `add_read`, `add_step`, `add_status_route`, `ensure_park`, `verification_outcome`, `inject_lessons`, `merge_data`, `scope_of`, `run_id_of` | `approvals`, `context`, `contracts`, `engine`, `learn`, `ledger`, `policy`, `receipts`, `tools` |
| `learn.py` | Stages 3–10 of the closed cycle | `learning_pass`, `extract`, `cluster`, `stage`, `promote`, `recall`, `lesson_block`, `record_use`, `attribute`, `retire`, `decay`, `scope_acceptance`, the three shipped signal sources | `approvals`, `context`, `contracts`, `guidance`, `ledger`, `outcome`, `ports`, `receipts`, `stats`, (deferred: `tools`) |

**`kit` imports `learn`; `learn` never imports `kit`.** That is why `lesson_block` lives in
`learn.py` even though it renders a prompt block, and it is what keeps the learning pass owned
by `runtime.run_once` and out of the graph.

`kit.py` is also the package's only raise site for `ParkRequested` and its only call site for
`lesson_block`. Both are pinned by an AST test and by a counterfeit entry: a second raise site
would be a second park protocol nobody wrote down, and a second injection site would mean the
feedback edge is no longer auditable in one line.

### Layers 6 and 7 — templates, driver, CLI, adapters

| Module | Owns | Public API | May import |
|---|---|---|---|
| `templates/__init__.py` | The catalogue — a plain dict, not a discovery mechanism | `LoopTemplate`, `register_template`, `get_template`, `TEMPLATES` | `context`, `engine` (+ the shipped templates, at the bottom) |
| `templates/observe_decide_act_verify.py` | The safety workhorse: one subject per tick | `TEMPLATE`, `build`, node-name constants | `context`, `contracts`, `engine`, `kit`, `templates` |
| `templates/propose_evaluate_promote.py` | The learning shape: bounded refinement | `TEMPLATE`, `build`, `default_tools`, `default_propose`, `default_evaluate`, `max_steps_for` | `context`, `contracts`, `engine`, `kit`, `ledger`, `templates` |
| `runtime.py` | `run_once` — the whole driver, in one fixed order | `run_once`, the failure-tag constants, `GATE_SPEC` | `approvals`, `context`, `contracts`, `engine`, `kit`, `learn`, `ledger`, `outcome`, `templates` |
| `cli.py` | `python -m selfloop`: one tick, one JSON line, one exit status | `main`, `build_parser`, `CONTEXT_FACTORY` | `approvals`, `context`, `contracts`, `lease`, `receipts`, `runtime` |
| `adapters/memory.py` | All twelve ports in dicts and a lock, plus `build_memory_context` | the `Memory*` stores, `StaticPolicy`, `NullModel`, `ScriptedGate`, `RecordingNotifier`, `build_memory_context` | `context`, `contracts`, `lease` |
| `adapters/sqlite.py` | The five storage ports on one stdlib `sqlite3` file | `SqliteBackend` + the five `Sqlite*` stores, `SCHEMA` | `contracts`, `adapters.memory` (three helpers) |

---

## The three durable stores

Five ports persist. Three of them carry guarantees the package's correctness rests on, and
those guarantees live in the **semantics**, not in the signatures — which is why every one is
restated as an obligation in `ports.py`.

### 1. `ReceiptStore` — exactly-once for external effects

* `claim(key)` is **insert-or-ignore** and returns *did I win the race*, never *does a row
  exist*. A caller that loses must fail closed; it must not proceed on the theory that the
  winner will probably succeed.
* `complete(key, envelope)` must be **durable before it returns**. Not queued, not buffered,
  not "the OS will get to it". That obligation is the entire content of the kill drill.
* `release(key)` must be a **no-op once a result has been recorded**, and that must be enforced
  in the backend (`... AND result_json IS NULL`), not by the caller checking first — so no
  caller, however confused or refactored, can talk the store out of the crash-window guarantee.
* Keys are **attempt-scoped by the caller** (`<key>`, `<key>#a2`, …), so a retry never re-opens
  a row back into `claimed`.

Four terminal outcomes, and the last two are the pair people collapse: `succeeded` (only this
one short-circuits a replay), `failed` (something answered and the answer was no), `unknown`
(a request may have left and its fate was never established — fail closed forever), and
`unavailable` (the authority was never reached, so provably nothing happened — frees the next
attempt slot **without spending the retry budget**).

### 2. `RecordStore` — one kind-generic store for everything else

Lessons, lesson-uses, outcomes, signals, evidence, decisions, reconciliations, cursors: all of
them are `(kind, id, payload)`. Kind-generic rather than one port per record type is exactly
what makes "add a new learning signal" a zero-port, zero-schema, zero-migration edit.

Four methods, four distinct semantics, and picking the wrong one is a correctness bug:

* `put_once` is **history** — a run must not be able to overwrite its own report card.
* `put_latest` is a **cache** — a fresher green must be able to supersede a stale one.
* `query(kind, **equals)` is equality-only, which keeps the in-memory adapter trivial and
  over-fetches at any real scale.
* `transition(kind, id, expect=…, set=…)` is a **compare-and-set**, and it must be atomic with
  respect to other writers of the same `(kind, id)`. Read-modify-write in Python without a lock
  or a conditional `UPDATE` does not satisfy it. Every promote, retire, evidence-append,
  counter update and cursor advance goes through it, because the unattended learning pass races
  an operator at a CLI — and without the CAS, a lesson retired for regression can be
  resurrected to promoted by a writer that read the row a moment earlier.

### 3. `EventLog` — the ordered replay cursor

The only port whose **return value** is the point. `append(event)` returns a strictly
increasing integer, and that integer *is* the cursor: "extract signals since N" is what makes
the learning pass exactly-once and re-runnable. Monotonic means across processes and restarts —
an adapter that restarts numbering, or hands the same integer to two writers, has broken the
learning pass in a way no single-process test will show. An adapter must also **echo the
assigned cursor back** in the rows it returns from `read()`, or a reader cannot advance past
what it just read.

### The two others

`ApprovalStore` — `get()` by id (the method whose absence made the predecessor unportable: it
reached into the host's database with a raw `SELECT`, and that one line pinned the runtime to
one schema); `create()` returns False when the row exists, which is the normal replay path;
`decide()` is a compare-and-set on `state == 'pending'` and returns False rather than raising
when it loses, so an approval can never overwrite a rejection.

`CheckpointStore` — `save()` must be durable before it returns. Thread ids are
`f"{template}:{instance}"`, because two templates driving one instance resuming each other's
half-finished state is a silent no-op, not a crash.

---

## The tick lifecycle, end to end

`runtime.run_once(ctx, template_name, params=…)` — never raises; a tick reports, it does not
crash.

1. **Mint a run id and build a seed state**, before anything can fail, so that every refusal
   path below still has a state to read the learning scope off. An unscoped refusal produces no
   signal, and a loop that cannot learn from its own refusals keeps making them.
2. **Take the instance's lease.** `LeaseHeld` → report `IDLE` and write **no report card at
   all**: a worker that correctly stepped aside must be out of the acceptance floor's numerator
   *and* denominator, because the tick it stood aside for is about to file the only honest
   account of that work. A lease backend that is *unusable* is a different thing — `BLOCKED`,
   adverse, and no learning pass runs, because a multi-row read-modify-write without the lease
   is not safe.
3. **Check the instance contract.** Template name matches the context's; the template is in the
   catalogue; every `required_tool` is granted and none is denied. Failure is `BLOCKED` —
   adverse, never idle and never completed. This is the loudest guard in the file: a fleet once
   reported 68% acceptance while its executors were missing, because a loop with nothing to run
   rendered as a well-behaved loop with nothing to do.
4. **Read the checkpoint** (`CompiledGraph.snapshot`) and resolve one of three entry states.
   *Parked*: resolve the approval first; if it is still pending, return `PARKED` having invoked
   **nothing** — not the parked node, not the entry node. *Mid-run*: resume at the recorded next
   node. *Idle*: start a fresh tick, merged onto the surviving checkpoint through the channel
   reducers, so `memo` carries across ticks while `data` is replaced.
5. **Drive the graph.** After each node returns, the checkpoint naming the **next** node is
   durable before that node runs. Every effect goes through `tools.execute_effect`: re-derive
   the policy verdict, re-read the approval row for a parked effect, then `receipts.guarded`.
6. **Settle.** Run the gate — only when the tick's own claim was favourable, since a gate may
   lower and never raise — and `outcome.compose` the claim against the verdict. Write the
   `OutcomeRecord` `put_once` under a per-invocation id that sorts by invocation, and file an
   `EvidenceRecord` bound to a digest of the spec and the effects. If the tick claimed success
   and the gate ruled against it, the **returned report is lowered to `FAILED`**, because
   `RunReport.as_dict()` derives `accepted` from the status and a contradicted tick must not
   print `accepted: true` to a scheduler.
7. **Run the learning pass, always** (`learn.learning_pass`) — attribute, extract, cluster,
   stage, promote, decay, then advance the cursor. A broken learner must not fail a working
   tick: an exception here is recorded on the event stream and appended to the report's detail,
   loudly, but the report card is already written and immutable. A parked lesson promotion may
   **lower** the report to `PARKED` and may never raise an adverse one.
8. **Release the lease** and return the `RunReport`.

`run_id` names the **work**, not the process: a tick that parks and is resumed two hours later
by a different process is one run with two report cards, which is why the outcome record's id
is distinct from the run id and why the newest card wins when a run has more than one.

---

## Crash-window analysis

Every place a process can die, and what the next tick does about it. "Die" means `SIGKILL` —
no unwinding, no `finally`.

| Die here | State left behind | What the next tick does |
|---|---|---|
| Before the lease is taken | Nothing | Fresh tick. Nothing happened. |
| Holding the lease, before any node | Checkpoint unchanged | The kernel drops the `flock` (or SQLite's write lock) on process death, so the lease is free. Fresh tick. |
| Inside a node, before its checkpoint write | Checkpoint names *this* node as next | The node **re-runs**. That is why every external effect is receipted rather than trusted to run once. |
| Between `receipts.claim` and the tool call | Row `claimed`, no result | **Fails closed forever** with `EffectStateUnknown`. Nothing else is honest: this process cannot learn whether the effect ran. Cleared only by `selfloop reconcile`. |
| Between the tool call and `complete()` | Row `claimed`, no result | Identical, and identical on purpose — that is the whole reason a re-openable row was rejected in favour of attempt-keyed ones. |
| After `complete()`, before the ledger mirror | Receipt terminal; `RecordKind.RECEIPT` row missing | The receipt store is authoritative, so the effect replays correctly. The missing mirror costs the learning pass one piece of evidence; it is a cache write, and a `put_latest`. |
| After a node returns, before `_save(next_node)` | Checkpoint names the node that just ran | It re-runs. Idempotent for a read; receipted for an effect; for a gate node, `ensure_approval` re-derives the same id and returns the same row. |
| After `_save(next_node)` | Checkpoint names the next node | Resumes exactly there. The nodes before it are **not** replayed. |
| Inside `ensure_approval`, after `create()` and before `page()` | A pending approval row nobody was told about | **A real hole, and it is named here rather than papered over.** The next tick finds the existing row and does not page, because paging is conditional on having created the row. The approval sits until expiry, and expiry aborts. Mitigation: an operator dashboard should list pending rows rather than relying only on pushes. |
| After `ParkRequested` is raised, before the park checkpoint | Checkpoint names the gate node | The gate node re-runs, `ensure_approval` finds its own row, `read_outcome` re-reads it, and the tick parks again — one row, one page, ever. |
| Between the outcome write and the learning pass | Report card written; cursor unmoved | The next tick re-mines the same event window (idempotent: content-stable signal ids plus insert-if-absent). **But**: that run's pending `LessonUse` rows are never attributed, because `attribute()` is called for the current run id only. One graded use is lost. |
| Mid learning pass (after a promotion, before the cursor advance) | Lesson promoted; cursor unmoved | Re-mines. `stage` refuses the decided key, `promote` returns "already promoted", the retirement row is `put_once`. All idempotent. |
| Between `retire`'s CAS and its retirement row | Lesson `retired`; no retirement record | The status change is the load-bearing half and it is durable. The audit row is lost; the next pass will not re-write it, because the CAS from `promoted` now fails. |
| During `reconcile`, between the record and the completion | Reconciliation recorded; receipt still `claimed` | Re-running `reconcile` finds the existing record (insert-if-absent returns False, which is logged as a retry) and re-attempts the completion. That is why the record is written **first**: a crash the other way round leaves an unaudited escape, which is not recoverable at all. |
| While the gate subprocess is running | No receipt, no outcome row | The tick left no report card. The next tick is fresh; the acceptance floor simply has one fewer sample. |

Two properties make that table short rather than long. The checkpoint naming the next node is
durable **before** that node runs, so the resume point is always known. And every write that
matters is either insert-if-absent (so a replay is refused) or a compare-and-set (so a
concurrent writer is detected) — never a blind read-modify-write.

---

## Where the code disagrees with the plan

Documented because the code wins, and because a reader comparing them should know which
differences are deliberate.

* **`lesson_block` lives in `learn.py`, not in `kit.py`.** The plan put it in the kit. Moving it
  is what keeps the `kit → learn` edge one-directional; `kit.inject_lessons` is the one line
  that prepends.
* **The promotion gate has four conditions, not five.** The plan's condition (c) — a confidence
  bound over `helped`/`used` — was removed entirely: it is unsatisfiable at first promotion.
  See LEARNINGS §22.
* **`settlement_signals` does not exist.** The plan shipped three sources including "a stage
  produced no artifact". That fires on every idle, parked and blocked tick, so it was replaced
  by `verify_disagreement_signals` and `failed_effect_signals`, split on one structured column.
* **The cluster key is `(scope, failure_tag)`, not the clustered tokens.** Tokens grow with
  every new report, so a token-derived key moves and no candidate accumulates support.
* **A lesson promotion is not written through the `ReceiptStore`.** The thesis says promotions
  are "receipted"; in the implementation the idempotence comes from a deterministic approval id
  plus a compare-and-set on the lesson row plus a `DecisionRecord`, not from a claim/complete
  receipt. The guarantee is the same — no double-write, no silent drop — and the mechanism is
  worth knowing exactly.
* **There is no default gate on `LoopContext`.** `gate` is a required field and `None` is legal
  and honest. `gates.ArtifactGate` is the shipped answer to "what do I put here on day one",
  and the quickstart uses it; it is not installed behind your back.
