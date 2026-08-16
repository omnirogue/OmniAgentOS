# selfloop

`selfloop` runs **one durable, unattended, self-improving loop tick per process invocation**.
You hand it a `LoopContext` (your tools, plus twelve ports), name a template, and call
`run_once()`; it takes a lease, runs a checkpointed graph, grades what the tick claimed
against a gate that executed independently, and turns the disagreements into lessons it
injects into the next run. It is stdlib-only, Python 3.11+, and importing it does nothing at
all — no `sys.path` mutation, no directory created, no network, no environment written.

**What it is not.** It is not a framework: nothing subclasses your code, scans your
directories, or runs at import. It is not an agent: it has no opinion about what your tools
do and never writes the prose it injects. It is not a model wrapper: `ModelPort` is optional,
both shipped templates and the quickstart run to completion against a null model that raises
if it is ever called, and you need no API key to see the whole cycle close.

---

## Install and run in 5 minutes

From the directory holding this README (the one with `pyproject.toml` and `selfloop/` in it):

```console
$ pip install -e .          # or, with no install at all: export PYTHONPATH=$PWD
$ python examples/quickstart.py
```

One of those two is required — running the script puts `examples/` on `sys.path`, not the
package root.

Real output, pasted from a real run:

```
min_support=2, so the minimum is min_support + 1 = 3 ticks: 2 of evidence, then one that uses it.

tick 1: failed    (adverse) — 34 bytes on disk
         the tick reported completed and the gate ruled against it — 0/1 declared artifact(s) present and non-empty
tick 2: failed    (adverse) — 34 bytes on disk
         the tick reported completed and the gate ruled against it — 0/1 declared artifact(s) present and non-empty
tick 3: completed (favourable) — 78 bytes on disk
         the gate agreed

What the loop learned, from its own graded runs and nothing else:
  when gate_contradicted then include: upgrade-steps, rollback-plan, checksums
  admitted on 2 distinct runs; used 1x, helped 1x

quickstart-release-notes.md:
draft (round 1)
- upgrade-steps
- rollback-plan
- checksums
- breaking-changes
```

Read that output carefully, because it is the whole package in twelve lines.

The loop's *own* evaluator scored the draft 1.0 on all three ticks, and all three ticks
reported `completed`. What disagreed is the gate — which never sees the prompt, never sees
the score, and only looks at the file on disk. Twice it ruled that a 34-byte file is not
release notes. That contradiction is the evidence: two adverse runs, two distinct run ids,
`min_support=2` met, a lesson promoted, injected into tick 3's brief, and a bigger file
written because of it. Nothing here is a demo knob — these are the production defaults, and
a quickstart that greens by lowering a bar teaches the bar.

`examples/custom_tool.py` is the other half of the five minutes: the four extension points
in one runnable file, driveable from the CLI.

```console
$ PYTHONPATH=. python examples/custom_tool.py
$ PYTHONPATH=examples python -m selfloop tick --tools custom_tool \
      --instance notes-1 --template note_to_file --accept-inprocess-lease
```

---

## The thesis

> **A learning loop is only worth running unattended if its promotion gate IS its effect
> gate.**

A lesson this loop learned is promoted through the same approval machinery an outbound email
is sent through. A lesson whose scope is tiered T0/T1 auto-promotes on evidence; a lesson
whose scope is T2 or above goes through `approvals.ensure_approval`, mints one deterministic
approval row, pages a human once, and the tick parks — the same id, the same binding
re-check, the same expiry-aborts-never-approves. Undeclared scopes default to T2 and park.

That is not decoration around a learner. It is the learner's promotion path, and it is what
makes "self-improving" and "unattended" safe to say in the same sentence: the machine's
authority to change its own future behaviour is bounded by exactly the same rules as its
authority to touch the world.

The corollary, which costs more to accept: **a loop with no independent gate cannot learn at
all.** With no verifier, every favourable tick settles `neutral/uncorroborated`, no
non-neutral evidence is ever written, nothing clusters, nothing promotes. That is stated in
`gates.NullGate`'s own docstring rather than hidden, because the alternative — a gate that
returns true — settles every tick as invisibly accepted and makes the loop's own optimism its
training signal.

---

## The ten-stage cycle

```
   ┌── one process invocation ──────────────────────────────────────────────┐
   │                                                                        │
   │   (1) ACT  ──────────────────────►  (2) SETTLE                         │
   │   runtime.run_once                  outcome.compose                    │
   │   engine.CompiledGraph.invoke       claim x gate verdict:              │
   │   tools.execute_effect              may lower, never raise             │
   │   receipts.guarded                          │                          │
   │        ▲                                    ▼                          │
   │        │                            OutcomeRecord (put_once)           │
   │        │                            favourable / neutral / adverse     │
   │        │                                    │                          │
   │        │      learn.learning_pass ── one owner, after settlement,      │
   │        │                             always ──────────┐                │
   │        │                                              ▼                │
   │        │   (3) SIGNAL ───► (4) CLUSTER ───► (5) STAGE ───► (6) GATE    │
   │        │   learn.extract   learn.cluster    learn.stage    learn.promote
   │        │   non-neutral     partition by     one row per    support >= N
   │        │   evidence only   (scope, tag)     content key    distinct runs
   │        │                                                       │       │
   │        │                              T0/T1: auto-promote ◄────┤       │
   │        │                              T2+  : park for a human ◄┘       │
   │        │                                                               │
   │        │  (10) DECAY ◄─── (9) ATTRIBUTE ◄─── (8) INJECT ◄──────── (7) RECALL
   │        │  learn.decay     learn.attribute    kit.inject_lessons   learn.recall
   │        │  age out the     used / helped,     the ONE line that    promoted only,
   │        │  unused          auto-retire a      prepends a lesson    fingerprint
   │        │                  regression         block to a prompt    re-verified
   │        │                                          │                    │
   │        └──────────────────────────────────────────┘                    │
   │              the feedback edge: 7 → 8 → 9 → back into 1                │
   └────────────────────────────────────────────────────────────────────────┘
```

**1 — ACT.** `runtime.run_once` (`selfloop/runtime.py`) takes the instance's lease, refuses to
fire if the template's required tools are not granted, and drives the durable executor
(`engine.CompiledGraph.invoke`) exactly one tick. Every external effect goes through
`tools.execute_effect`, which re-derives the policy verdict, re-reads the approval row, and
wraps the call in `receipts.guarded`. The tick ends in a `RunReport` carrying a
`self_reported_status`. That is a **claim**, and nothing more.

**2 — SETTLE.** `runtime._settle` executes the declared gate — but only when the tick's own
claim was favourable, because a gate may lower a claim and may never raise one — and hands
the receipt to `outcome.compose` (`selfloop/outcome.py`). Composition is the truth table:
favourable + pass = favourable; favourable + fail = adverse; favourable + *absent* = neutral;
neutral stays neutral whatever the gate says; adverse stays adverse. `gate_passed is None`
means the gate **did not run**, never that it failed. The `OutcomeRecord` is written
`put_once` — a run must not be able to overwrite its own report card — and it is the loop's
only training label.

**3 — SIGNAL.** `learn.extract` (`selfloop/learn.py`) walks the append-only event log from the
stored cursor and asks every registered `SignalSource` what it found. Three ship:
`verify_disagreement_signals` (a tool declared success and its independent verifier said no —
the disagreement *is* the lesson), `failed_effect_signals` (a decisive recorded failure), and
`adverse_outcome_signals` (an adverse `OutcomeRecord` carrying a failure tag). Signals are
mined **after the fact from the durable record**, never from a hook on the hot path, which is
what makes the pass re-runnable; and no signal may be derived from a neutral tick or from the
actor's own prose.

**4 — CLUSTER.** `learn.cluster` partitions signals by `(scope, failure_tag)` **before it
compares a single token**, and only then runs Jaccard union-find *inside* a partition to
choose which wording best represents it. Raw-token similarity on its own conflates unrelated
failures — `error`, `failed` and `line` appear in everything — into one trash cluster whose
"lesson" is an amalgamation of contradictory fixes. A group with no shared structured tag is
not a cluster, however similar its words.

**5 — STAGE.** `learn.stage` creates one candidate row per content key, or appends this
cluster's evidence to the existing one through a compare-and-set. Three rules: a key that was
promoted, rejected or retired **never resurrects**; the first staging fixes the content and
its fingerprint forever (later passes touch evidence only); and guidance that is not
template-derived forces the lesson to the approval floor tier — human text, human approval.

**6 — GATE.** `learn.promote` is the honest promotion rule and it **never reads `helped` or
`used`**. Admission is pre-injection evidence only: at least `min_support` **distinct runs**
contributed, every one of those runs settled non-neutrally, the evidence agrees on one failure
tag, and the row's content fingerprint is recomputed immediately before the write so drift
skips rather than applies. Then the scope's risk tier decides the path: T0/T1 auto-promotes,
T2+ parks on an approval row. *This is the thesis, made literal.*

**7 — RECALL.** `learn.recall` returns promoted lessons only — never staged, never parked —
re-verifies each row's content fingerprint at read time, and ranks by
`wilson_lower_bound(helped, used) × decay_weight(age)`. **No floor is applied to that
ranking.** A freshly promoted lesson has `used == 0` and therefore a bound of exactly 0.0;
filtering on it would make every new lesson unrecallable and the feedback edge would never
close.

**8 — INJECT.** `learn.lesson_block` renders the recalled lessons into one bounded, labelled
section, and `kit.inject_lessons` is the single line in the package that prepends it to a
prompt. Before the text is rendered, `learn.record_use` writes a **pending** `LessonUse` row
for each lesson — *before* the run produces any outcome. That ordering is the entire
difference between attribution and a correlation fished out of history afterwards.

**9 — ATTRIBUTE.** `learn.attribute` grades the lesson-use rows of a run against that run's
composed outcome, and **only a non-neutral outcome finalises a use**. A neutral run leaves its
rows pending, permanently: counting parks and idle ticks as `used` without `helped` is how a
flaky weekend auto-retires good lessons. It then compares the scope's acceptance bound since
promotion against the baseline snapshotted at promotion and auto-retires a measurable
regression.

**10 — DECAY.** `learn.decay` ages promoted lessons by the same linear curve the ranking uses
and retires those below `retire_floor`. A lesson whose timestamp cannot be read is **kept**,
not evicted: treating "I cannot date this row" as "this row is infinitely old" makes corrupt
rows the first casualties of a cleanup pass, which is exactly backwards.

Stages 3–10 are one function, `learn.learning_pass`, and `runtime.run_once` is its **only**
caller. There is no learning graph node: two owners meant double mining, racing cursors, and
a promotion parking outside the executor's park/resume protocol.

---

## The four extension points

None of them requires editing the package. All four are in `examples/custom_tool.py`, which
runs.

### 1. A new tool

```python
from selfloop import LoopTool, RiskTier, ToolRegistry

def write_note(*, text: str, path: str) -> dict:
    Path(path).write_text(text, encoding="utf-8")
    return {"path": path, "ok": True}

def note_exists(result, args) -> bool:        # external evidence, not the tool's word
    return Path(str(args["path"])).is_file()

tools = ToolRegistry()
tools.register(LoopTool(
    name="write_note",
    tier=RiskTier.T1,                          # T2+ parks this effect for a human, always
    call=write_note,
    verify=note_exists,                        # runs once per executed attempt, outside the seam
    description="write one note to disk",
))
```

`verify` must look somewhere the tool does not control. Ask a supervisor whether the service
is running; do not read "the subprocess exited 0". A predicate that *raises* is not a failure
verdict — it means the effect ran and its outcome could not be established, which fails
closed as `EffectStateUnknown`.

### 2. A new loop template

```python
from selfloop.engine import END, Graph
from selfloop.kit import add_effect, ensure_park, merge_data
from selfloop.templates import LoopTemplate, register_template

def build_note_graph(ctx):
    graph = Graph()
    ensure_park(graph, ctx)
    gate = add_effect(                          # adds `write_gate` AND `write`, returns the GATE
        graph, ctx,
        name="write", tool="write_note",
        args_fn=lambda s: {"text": s["params"]["text"], "path": str(OUT)},
        key_fn=lambda s: digest_key("note_to_file", s["params"]["text"]),
        on_result=lambda s, r: {**merge_data(s, note=r), "status": "completed"},
    )
    graph.set_entry(gate)
    graph.add_edge("write", END)
    return graph.compile()

register_template(LoopTemplate(
    name="note_to_file", family="observe_act",
    required_tools=("write_note",), build=build_note_graph,
))
```

`add_effect` returns the *gate's* name and adds both nodes together, so there is no way to
wire an ungated effect — not by forgetting, not by refactoring, not by copying a neighbouring
template that happened to be wrong.

### 3. A new port adapter

```python
class FileNotifier:                             # implements selfloop.ports.Notifier
    def __init__(self, path): self.path = path

    def page(self, *, approval_id: str, summary: str, deep_link: str) -> bool:
        try:
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(f"{approval_id}\t{summary}\t{deep_link}\n")
        except OSError:
            return False                        # not delivered — and saying so IS the contract
        return True
```

Nothing subclasses anything: the ports are `typing.Protocol` and structural. Read the
docstrings in `selfloop/ports.py` as **obligations**, not commentary — most of these
guarantees live in the semantics and not in the signature, and a `claim()` that returns `True`
type-checks while having deleted the exactly-once property.

### 4. A new learning signal

```python
class ReconciliationSignals:                    # implements selfloop.ports.SignalSource
    name = "reconciliations"

    def extract(self, ctx, *, since_cursor: int):
        for row in ctx.records.query(RecordKind.RECONCILIATION.value):
            yield LearningSignal(
                id=f"sig_recon_{digest_key(row['id'])[:16]}",   # content-stable: re-mining is free
                scope=str(row.get("template") or "unscoped"),
                failure_tag="effect_state_unknown",             # required: clustering partitions on it
                text=f"{row['receipt_key']}: {row.get('note') or row['outcome']}",
                run_id="", cursor=0,
            )

ctx.signal_sources.append(ReconciliationSignals())
```

No port change, no schema change, no migration — the values land in the kind-generic
`RecordStore`. Three obligations: read the durable record after the fact, never derive a
signal from a neutral outcome, and never derive one from the actor's own text.

---

## The twelve ports

Every host-specific thing the runtime can reach is one of these, declared in
`selfloop/ports.py` and supplied as a field of `LoopContext`. There are no implicit defaults:
a context missing its gate must be a decision somebody typed (`gate=None`), not one they
inherited.

| Port | What it is for | Shipped adapters |
|---|---|---|
| `Clock` | Two clocks: `now_iso()` is a record stamp a caller may pin; `elapsed()` is the monotonic source every freshness check must read | `MemoryClock` (advanceable, pinnable) |
| `ReceiptStore` | Exactly-once bookkeeping for effects: claim, act, complete — `release` must be a no-op once a result exists | `MemoryReceiptStore`, `SqliteReceiptStore` |
| `ApprovalStore` | The park/approve rows; `decide` is a compare-and-set on `pending`, `create` returns False when the row exists | `MemoryApprovalStore`, `SqliteApprovalStore` |
| `RecordStore` | One kind-generic durable store for every non-receipt record, with `put_once` / `put_latest` / `query` / `transition` (CAS) | `MemoryRecordStore`, `SqliteRecordStore` |
| `EventLog` | The ordered replay cursor: `append` returns a strictly increasing integer, forever, across processes | `MemoryEventLog`, `SqliteEventLog` |
| `CheckpointStore` | The durability seam under the executor: `save` must be durable **before it returns** | `MemoryCheckpointStore`, `SqliteCheckpointStore` |
| `LeasePort` | Per-instance mutual exclusion for the whole tick; must raise rather than block, and must never reclaim by age | `FlockLease` (POSIX), `SqliteLease` (portable), `InProcessLease` (opt-in only) |
| `PolicyPort` | The caller's classification of an action class; may make T0/T1 stricter and can never lower the T2 floor | `policy.TierPolicy`, `adapters.memory.StaticPolicy` |
| `ModelPort` | Optional by design — nothing shipped requires a model | `NullModel` (raises if called), `RecordingModel` |
| `GateRunner` | The independent verifier: takes a spec to **execute** and never a verdict to record | `gates.ArtifactGate` (the honest day-one default), `CommandGate`, `NullGate`, `ScriptedGate` |
| `Notifier` | Tell a human something is parked; returns True only on **confirmed** delivery | `RecordingNotifier`, and `FileNotifier` in the examples |
| `SignalSource` | The extension point of the learning loop: `(ctx, since_cursor) -> Iterable[LearningSignal]` | `verify_disagreement_signals`, `failed_effect_signals`, `adverse_outcome_signals`, `ScriptedSignalSource` |

`adapters.memory.build_memory_context(**overrides)` wires all twelve in one call and refuses
an unrecognised keyword; `adapters.sqlite.SqliteBackend(path).as_context_overrides()` swaps
the five storage ports for durable ones. That is five keywords, not a rewrite.

---

## Calibration

These are the numbers most likely to need tuning for your loop. They live on `LoopContext`.

| Knob | Default | What it does |
|---|---|---|
| `min_support` | `2` | Distinct **runs** that must contribute evidence before a candidate may be admitted |
| `min_evidence_consistency` | `1.0` | Share of counted evidence that must agree on one failure tag |
| `promote_threshold` | `0.40` | Wilson floor for **recall ranking and regression retirement only** — never an admission test |
| `retire_floor` | `0.2` | Decay weight below which an unused promoted lesson retires (≈ 12.6 days on the 7/14-day curve) |
| `recall_k` | `3` | How many lessons one injection may carry |
| `default_scope_tier` | `T2` | Tier for a scope not in `scope_tiers` — undeclared scopes **park** |
| `max_steps` | `50` | Hard ceiling on nodes executed in one tick |

**The arithmetic rule, which the liveness test runs at production defaults:**

> **minimum ticks to first promotion = `min_support` + 1**

Ticks 1..`min_support` each contribute one graded, non-neutral run of evidence; the promotion
happens on tick `min_support + 1`, and that is the first run the lesson is in front of. If you
raise `min_support` to 3, the cycle takes four ticks — and any test that claims otherwise is
greening by secretly lowering the bar.

Three things that will silently stop a loop learning, in the order they bite:

1. **No real gate.** Every tick settles neutral, no evidence is ever non-neutral, nothing
   promotes. `ArtifactGate` is four lines to configure and cannot pass without something
   having been produced.
2. **No failure tag.** Clustering partitions by `(scope, failure_tag)` before it compares a
   token, so an adverse tick with no tag yields no signal at all. Stamp
   `data[kit.FAILURE_TAG]` on every adverse branch you write.
3. **No remedy.** A tag with no entry in `remedy_table` yields `[needs-human]` guidance, and
   such a lesson is forced to T2 and parks. That is correct — nobody wrote down what to do —
   but it looks like starvation if you do not know to look.

**The honest fallback.** If you are unsure whether your evidence is good enough to let a
machine change its own prompts, set every scope to T2 (or just leave `scope_tiers` empty, which
is the default). Every promotion then parks for a human, one row and one page per lesson, and
you keep the mining, the clustering, the fingerprint bind and the audit trail while giving up
only the autonomy. That is a supported configuration, not a degraded one.

---

## Operating it

`python -m selfloop <command>` — one tick per process, one JSON line on stdout, an exit status
a cron job can branch on. Every command reaches your wiring through **one function in one
module you name**: `build_context(*, instance_id, template, params) -> LoopContext`.

| Command | What it does |
|---|---|
| `tick` | Run one tick and print its `RunReport` |
| `lessons` | List stored lessons **with a count per status** — "207 staged, 0 promoted" is the disease this package exists to prevent, and it is invisible in a list of rows |
| `approve` | Record a human's decision on one parked approval; refuses an automation identity and refuses to report success when the CAS lost |
| `reconcile` | Declare what actually happened to an effect whose state is unknown — the only way out of a fail-closed unknown |
| `gc` | Size the store's retention problem. **Deletes nothing**, on purpose: the ports expose no `delete`, because the records are the evidence the loop was graded on |

Exit status is `0` when the command did its job, `1` when the tick reported `FAILED`, and `2`
when the command could not run at all. **Alert on the JSON line's `outcome` field, not on the
exit status:** a tick that stood aside for a peer, parked for a human, or was blocked by a
dead credential all exit 0, because the process did exactly what it was asked to do.

---

## What is deliberately not here

* **LangGraph, langchain-core, langgraph-checkpoint-sqlite.** Every graph this design actually
  builds is sequential-with-branching — every router returns exactly one target, nothing fans
  out — so one superstep equals one node and the library's durability guarantee is *exactly*
  "persist the state after each node returns, before the next node runs". That is `engine.py`,
  and `ExecutorPort` is the seam a LangGraph-backed executor would plug into.
* **pydantic, pyyaml, httpx, Pillow.** A frozen dataclass replaced the one validated model;
  config is caller-supplied; `ConnectionError` and `TimeoutError` already cover the retry
  allowlist. The result is zero required third-party dependencies, which is what lets an
  unattended loop keep running when somebody publishes a new minor version overnight.
* **Import-time side effects.** No `sys.path` mutation, no repository root guessed from
  `__file__`. That guess is the single hardest blocker to ever `pip install`-ing a package:
  the moment it moves, checkpoints and lease files are written somewhere arbitrary.
* **Three of the five templates.** Two ship — `observe_decide_act_verify` (the safety
  workhorse) and `propose_evaluate_promote` (the learning shape). They are the two shapes
  every other one was a variation of, and a template is fifty lines of `kit.add_*` calls.
* **`adapters/jsonl.py`** — deferred. `sqlite` covers durability and `memory` covers the demo;
  a third backend would have been paid for by trimming the counterfeit corpus.
* **`contrib/`** — deferred, and documented as seams rather than dropped: a LangGraph executor
  behind `ExecutorPort`, a parent-process seam for running untrusted tools out of process, and
  HMAC-signed records for a store you do not fully control.
* **A run manifest.** A manifest is a *projection* over events and receipts that already
  exist, and making it a second writer gives a run two accounts of itself that can disagree.
  Build the summary by reading; never by writing it a second time.

---

## Honest limits

Read these before you rely on anything above.

**The execution seam is a strong convention, not a memory boundary.** Every tool registered
through a `ToolRegistry` is sealed, and within this package it is invocable only through
`tools._invoke_in_seam` — enforced by a mechanical review suite over `selfloop/` and by the
counterfeit corpus. But `guarded.__closure__[0].cell_contents` still reaches the
implementation, as do `gc.get_referrers` and `ctypes`, and a module that kept its own name for
the callable never lost it. Python offers no wrapper that closes those. The AST suite scans
`selfloop/` only, and **the whole point of the package is that you write the tools** — your
code is not scanned and is not bound by the seam. For untrusted tool code, run effects in a
separate process.

**Attribution is scope-level and confounded.** `attribute()` compares a scope's acceptance
bound after a promotion against the baseline snapshotted at promotion. When several lessons
are injected together, that comparison cannot say which of them moved the number. Two guards
make it survivable rather than correct — the drop must exceed a margin, and the post window
must hold at least four gradeable runs — and the correct fix, holdout runs, is not in v1. If
you want per-lesson attribution, that is where to start.

**Do not let one component both label the training data and grade whether the lesson helped.**
`propose_evaluate_promote` keeps the evaluator away from the proposer's prompt structurally
(the scorer's arguments do not include the brief), but a caller can still register the same
callable under both names. The baseline and post windows should be measured by a gate that is
not the proposal evaluator.

**Windows has no cross-process lease without SQLite.** `FlockLease` needs POSIX `fcntl`;
`InProcessLease` protects nothing between OS processes and refuses to be constructed without
`accept_single_process_only=True`. The CLI **refuses** to run a scheduled tick when it cannot
verify the lease excludes another process, unless you pass `--accept-inprocess-lease`.
`SqliteLease` (a `BEGIN IMMEDIATE` transaction held for the tick) is the portable answer.

**`flock` is single-host.** It does not arbitrate across machines and is unreliable on
NFS/SMB. A multi-host fleet needs `SqliteLease` on storage that actually locks, or a real
distributed lock.

**`RecordStore.query` is equality-only and over-fetches.** That is correct at prototype scale
and wrong at any real scale; a production backend should index `(kind, scope, cursor)`.

**An `EffectStateUnknown` is permanent until a human clears it.** By design: no timer observes
anything, and a TTL on an unknown is the double-billing bug with a delay in front of it. The
cost is real — the business key is stuck until somebody runs `selfloop reconcile`.

---

## Where to read next

| File | What it holds |
|---|---|
| `LEARNINGS.md` | The transferable laws. **Read this one even if you never run the code.** |
| `ARCHITECTURE.md` | Module-by-module ownership, the import DAG, the durable stores, the crash-window analysis |
| `selfloop/ports.py` | What you must supply, in one file, as obligations |
| `selfloop/learn.py` | The closed cycle, and the four independent routes to starvation each rule closes |
| `selfloop/kit.py` | Why "a gate precedes every effect" is a shape rather than a convention |
| `examples/custom_tool.py` | The four extension points, runnable |

The docstrings are the deliverable as much as the code. Where a guard exists because a
specific failure happened, the docstring says which one.
