# Changelog

All notable changes to `selfloop` are recorded here. Versions follow
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

---

## 0.1.0 — initial extraction

The first release. `selfloop` is distilled from a ~630,000-line production system that ran
unattended loops against real external effects for a year, and it carries out of that system
exactly the parts that were load-bearing — plus the fixes for the parts that were not.

**How it was built.** The design was reviewed *before implementation* by four independent model
lineages, run blind against the same plan. They converged on eighteen corrections, five of them
blockers, and every one is closed in the code that ships here. Four of those blockers were
independent routes to the same disease: a learning loop that is correctly wired, fully tested,
and structurally incapable of ever promoting anything. The source system had two hundred and
seven candidates staged and zero promoted, forever, behind a gate that was mathematically
always closed.

### Added

* **`run_once`** — one durable, unattended, self-improving loop tick per process invocation:
  lease, contract check, checkpointed graph, settlement against an independently executed gate,
  and one learning pass. It never raises; a tick reports.
* **The closed ten-stage learning cycle** (`selfloop/learn.py`) — signal, cluster, stage,
  promote, recall, inject, attribute, decay — with the promotion gate reading **pre**-injection
  evidence only, and a liveness test that runs the true minimum (`min_support + 1` ticks) at
  production defaults from a cold, empty store.
* **The thesis, made literal** — a T0/T1-scoped lesson auto-promotes on evidence; a T2+-scoped
  lesson goes through the same `approvals.ensure_approval` machinery an outbound send goes
  through, and the tick parks. Undeclared scopes park.
* **The seam/receipt/approval/lease spine** — one execution seam, attempt-keyed idempotency
  receipts that bind identity *and* outcome, deterministic approval ids bound to the exact
  arguments, and a lease with no age-based reclaim path.
* **The three-valued outcome model** — favourable / neutral / adverse, with `gate_passed = None`
  meaning *the gate did not run*, and an acceptance floor that answers "I cannot tell".
* **A hand-written durable executor** (`selfloop/engine.py`) behind `ExecutorPort`, replacing a
  graph library: every graph this design builds is sequential-with-branching, so one superstep
  equals one node and the durability guarantee is exactly "persist after each node returns".
* **Twelve ports** as `typing.Protocol` in one file, with in-memory and `sqlite3` adapter sets
  and a one-call context constructor.
* **Three gate runners** — `ArtifactGate` (the honest day-one default), `CommandGate`,
  `NullGate` — and the rule that a receipt collecting zero checks raises `GateUnavailable`.
* **Two templates** — `observe_decide_act_verify` (the safety workhorse) and
  `propose_evaluate_promote` (the learning shape, whose evaluator structurally cannot see the
  proposer's prompt).
* **A CLI** (`python -m selfloop`) with `tick`, `lessons`, `approve`, `reconcile`, `gc`, and a
  `counterfeit --reanchor` that refuses to re-anchor without re-running the mutation.
* **Two runnable examples** — `quickstart.py` (the whole cycle in three ticks, no API key, no
  network) and `custom_tool.py` (the four extension points in one file).
* **`LEARNINGS.md`** — thirty-eight transferable laws, each with the failure that taught it.

### Changed from the source system

* Zero required third-party dependencies. A graph library, a validation library, a YAML parser,
  an HTTP client and an imaging library were all removed; Python 3.11+ standard library only.
* No import-time side effects: no `sys.path` mutation, no repository root guessed from
  `__file__`. That guess was the single hardest blocker to ever `pip install`-ing the original.
* Five templates reduced to two. A 277-line host-shaped registry removed entirely.
* Absence of a verdict fails closed everywhere it is read, including the three places the
  predecessor defaulted to the most favourable available outcome.

### Deferred, and named so it reads as a decision

* `adapters/jsonl.py` — `sqlite` covers durability and `memory` covers the demo.
* `contrib/` — a LangGraph-backed executor behind `ExecutorPort`, a parent-process seam for
  running untrusted tool code out of process, and HMAC-signed records for a store you do not
  fully control. All three are documented as seams rather than dropped.
* Holdout runs for per-lesson attribution. v1's regression check is scope-level and confounded
  when several lessons are injected together, and says so.

### Known limits

Stated in full in the README's *Honest limits*, and summarised here: the execution seam is a
strong convention plus a mechanical review suite **within this package**, not an in-process
memory boundary, and it does not bind tool code you write; attribution is scope-level;
`RecordStore.query` is equality-only and over-fetches; `flock` is single-host; and Windows has
no cross-process lease without `SqliteLease`.
