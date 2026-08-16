# Lesson-injection runbook

Operator runbook for the estate's lesson-injection architecture: where lessons are
measured, which brief-assembly seams inject them today, which lanes are still
landing, how to calibrate the relative relevance floor, and how any new injection
policy must gate before estate-wide adoption.

**Nothing in the landing pipeline below is merged.** All lane rows remain
**LANDING** until they land on main through the normal gate path.

Status labels used throughout:

| Label | Meaning |
|---|---|
| **LIVE** | Merged / active on main in this checkout |
| **LANDING** | Candidate on a lane branch — **not** merged to main; do not treat as shipped |
| **UNCOVERED** | No injection point exists today |

---

## 1. Measurements

### Hypothesis Tester — run `20260812-full2`

Artifacts (worktree path, relative to the OmniAgentOS repo root):

`.claude/worktrees/hypothesis-tester/var/hypothesis_tester/runs/20260812-full2/`

Present files: `ANALYSIS.md`, `analysis.json`, `config.json`, `episodes.jsonl`,
`results.jsonl`, `summary.json`.

Briefing: `vault/briefings/hypothesis-tester-2026-08-12.md` on branch
`feat/hypothesis-tester-0812`.

Findings from that run (operator-verified; cite the run id above):

| Finding | Result |
|---|---|
| Consolidated lessons | **+41.7pp**, CI **[+28.1, +54.2]** on the hidden-rule family |
| Lessons and activation-selection | Both beat raw transcript dumps (both arms' CIs **> 0**) |
| Placebo / irrelevant memory | **≤ no effect** |
| Win/loss without reasons | Taught **~nothing** |
| Raw transcripts | Can **anchor** weak models on their own failed outputs — a **risk**, not only a null result |

### Sibling instrument — memcert

| Field | Value |
|---|---|
| Worktree | `/Users/youruser/OmniAgentOS-memcert` |
| Branch | `feat/memcert-suite-0812` |
| Design | `devtasks/memcert/DESIGN.md` |
| Results | `devtasks/memcert/RESULTS-2026-08-12.md` |

**Arm-naming map (approximate)** between instruments:

| memcert arm | hypothesis-tester arm |
|---|---|
| `fullhistory` / `transcript` | `transcripts` |
| `shuffled` | `placebo` |
| `rag` | `activation` |

**Convergence table** (read from
`/Users/youruser/OmniAgentOS-memcert/devtasks/memcert/RESULTS-2026-08-12.md`,
section "Convergence with the sibling instrument"):

| memcert (RESULTS-2026-08-12.md) | hypothesis_tester (`20260812-full2`) | agree |
|---|---|---|
| lessons 1.0 vs none −0.5 (G) | lessons +41.7pp vs none (H2b CONFIRMED) | yes |
| lessons ≫ transcripts (G: 1.0 vs 0.0) | lessons > transcripts +9.4pp CI>0 | yes |
| placebo = none (−0.5 ≈ −0.5) | placebo gives no gain | yes |
| rag(selection) > fullhistory(dump) pooled | activation top-3 > recent-6 dump +9.0pp | yes |
| transcript reading weak but real (+0.14) | transcripts +19.8pp on gate family | yes |

Division of labor stated in that same RESULTS section:
**hypothesis_tester** = behavioral experiments; **memcert** = ability certification.

---

## 2. Injection seams

Three code lanes below are **LANDING** — candidates on branches, **not** merged to
main. Do not describe them as shipped. The Hypothesis Tester freeze is also still
**LANDING** (submitted; not yet reviewed/approved).

| Seam | Mechanism | Master switch honoured? | Status |
|---|---|---|---|
| Runner briefs | `safe_recall_block` / `recall` — `omniagentos/knowledge/recall.py` builds a `<recalled-knowledge>` block; runner path gates on `knowledge_enabled()` (`OMNIAGENTOS_KNOWLEDGE`, defaulted on in `scripts/launch-env.sh` via `: "${OMNIAGENTOS_KNOWLEDGE:=1}"`); outcome credit flows back via `surfaced_fact_ids` / `record_helped` | **YES** — `knowledge_enabled()` | **LIVE** on main |
| Relevance floor (Lane B) | Relative floor via `recall_floor_fraction()` — `config.py` @`d932c8862` (env `OMNIAGENTOS_KNOWLEDGE_RECALL_FLOOR_FRACTION`, default `0.15`): suppress from **injection only** facts below `fraction × top score`. Absolute floor via `recall_score_floor()` — same file @`d932c8862` (env `OMNIAGENTOS_KNOWLEDGE_RECALL_SCORE_FLOOR`, default `0` / disabled; NaN/inf/negative rejected; values above `RECALL_SCORE_FLOOR_PLAUSIBLE_MAX` ≈ `0.07` log WARNING and disable). `def recall` — `recall.py` @`d932c8862` accepts `score_floor` / `floor_fraction`; `safe_recall_block` emits `status="floor_suppressed"` + `suppressed_count`. Debug score histogram: `knowledge recall scores count=… absolute_floor=… relative_floor=… filtered=…` | **YES** — floor only filters injectable presentation; subsystem still under `knowledge_enabled()` | **LANDING** via `lane/recall-relevance-floor-0812`, candidate **sha256:0ac718ba** @`d932c8862` — APPROVED zero-blocker; follow-up finding **sha256:2d3dd3c5** (runner run-row collapse; see §3) |
| Coordinator/subagent briefs + swarm TASK.md (Lane C) | `python -m omniagentos.knowledge.brief_recall` — `brief_recall.py` @`8b650b52b`: `main()` gates on `knowledge_enabled()` (exit 3); `recall_lessons()` does **not** — callers embedding the library must gate themselves; exit **0** = success including empty results, **2** = could-not-run / unavailable, **3** = disabled; side-effect-free (`run_id=None`). `spawn_builders._lessons_block` — `pipeline/bridge/spawn_builders.py` @`8b650b52b` also gates on `knowledge_enabled()` and emits structured per-mint log `lessons_block state=<disabled\|unavailable\|empty\|injected> facts=N ms=…`; renders `## Lessons from previous runs` into TASK.md when non-empty. Covered by `pipeline/tests/test_spawn_builders_lessons.py` | **YES** for `main()` CLI and `spawn_builders._lessons_block`; **NO** for the `recall_lessons()` library entry point (gate yourself when importing it) | **LANDING** via `lane/brief-recall-coverage-0812` @`8b650b52b` — candidate `sha256:0ca9e83f` APPROVED zero-blocker (round 2); follow-up finding `sha256:35d46ebf`: the per-mint state log is INVISIBLE under the documented bare-script invocation (no handler configured) — until that lands, mint states are NOT operator-visible |
| Lesson supply (Lane A) | `omniagentos/knowledge/consolidator.py` @`db2f5d7e1` turns ledger events, merge-gate refusal receipts, and dated MEMORY.md lines into lesson-shaped candidate facts. `REQUIRED_FIELDS` (8 fields, including `kind`) = `situation, attempt, why, corrective_action, evidence, conditions, provenance, kind` — `kind` is part of the identity hash for GATE lessons only; ledger lessons key on `{event_id, source, status}` and memory lessons on `{date, path, scope, source, text}` (`_ID_FIELDS` is an unused fallback branch of `lesson_id()`). Gate mechanics / instrument errors become `kind="instrument"` lessons (operator-directed instrument fixes), never candidate-defect advice — see `lessons_from_gate_receipts` / `is_instrument` — consolidator.py @`db2f5d7e1` | **n/a — producer**; `--apply` connects to Postgres and stages facts REGARDLESS of the switch (zero `knowledge_enabled()` references in consolidator.py @`db2f5d7e1`) — only *injection* at consumer seams is switch-gated | **LANDING** via `lane/lesson-consolidator-0812`, candidate **sha256:c06aa651** @`db2f5d7e1` — APPROVED zero-blocker; follow-ups **sha256:ac2d97f8** (memory-path identity), **sha256:86bbe79e** (provenance digest) |
| Loop prompts (`pipeline/prompts/PROMPT-*.md`) | No injection point exists today. Per `pipeline/CONTRACT.md`, prompt files (including `PROMPT-planning-loop.md`, `PROMPT-implementer-loop.md`, `PROMPT-reviewer-loop.md`) may only be changed by an operator-directed session landing through the deterministic gate daemon — "no loop writes its own prompt, ever". See §4 DRAFT follow-up for a per-iteration `brief_recall` proposal the operator can file. | n/a | **UNCOVERED** |
| Interactive Claude sessions | Only convention today is manual MEMORY.md files (this repo's `AGENTS.md` / `CLAUDE.md` memory rituals) — no programmatic recall hook | n/a | **UNCOVERED** — candidate future hook; out of scope for this runbook |
| Hypothesis Tester instrument | Frozen benchmark for memory-arm gating (§5). Candidate **sha256:3965f8f5** submitted; not yet reviewed/approved | n/a | **LANDING** — submitted; do not treat as landed |

### Lane landing summary (all still LANDING)

| Lane | Candidate | Reviewed sha | Review state | Follow-ups |
|---|---|---|---|---|
| A — consolidator | sha256:c06aa651 | `db2f5d7e1` | APPROVED zero-blocker | sha256:ac2d97f8 (memory-path identity); sha256:86bbe79e (provenance digest) |
| B — recall floor | sha256:0ac718ba | `d932c8862` | APPROVED zero-blocker | sha256:2d3dd3c5 (`runner/core.py` collapses `floor_suppressed` on run row) |
| C — brief-recall | `sha256:0ca9e83f` | `8b650b52b` | APPROVED zero-blocker (round 2) | finding `sha256:35d46ebf` (state log invisible in production; fold into main()'s JSON receipt) |
| Hypothesis Tester | sha256:3965f8f5 | — | submitted (not yet reviewed/approved) | — |

---

## 3. Floor calibration procedure (Lane B final design)

> **Scope.** These controls affect the RUNNER seam (`safe_recall_block`) only — it is the one call site that passes floor arguments (`recall.py` @`d932c8862`). Every other recall caller — brief_recall/TASK.md (Lane C), `/recall-preview`, `toolplane.knowledge_search`, `memory/recall_bridge`, `swarm/planner`, `lab_eval` — is UNFILTERED by design; tuning the floor changes none of them. The floored runner path also runs with `include_quarantined=False`, so the calibration histogram contains ONE population (active facts); the quarantine-discount caveat below matters when reading histograms from the unfloored preview/operator paths, not here. To see the histogram at all you must enable DEBUG explicitly — e.g. a probe snippet that does `logging.getLogger("omniagentos.knowledge.recall").setLevel(logging.DEBUG)` with a stderr handler before calling `safe_recall_block`; nothing in this tree enables DEBUG by default.


Verified against `def recall` / `safe_recall_block` — `omniagentos/knowledge/recall.py` @`d932c8862`
and `recall_score_floor` / `recall_floor_fraction` — `omniagentos/knowledge/config.py` @`d932c8862`.

### Semantics (final)

| Control | Env | Default | Effect |
|---|---|---|---|
| **Relative floor** (primary) | `OMNIAGENTOS_KNOWLEDGE_RECALL_FLOOR_FRACTION` | `0.15` | Suppress from **injection only** any ranked fact whose score is `< fraction × top score` |
| **Absolute floor** (optional) | `OMNIAGENTOS_KNOWLEDGE_RECALL_SCORE_FLOOR` | `0` (disabled) | Additional absolute gate; NaN/inf/negative rejected → `0`; values above plausible max `RECALL_SCORE_FLOOR_PLAUSIBLE_MAX` (`0.07`) log **WARNING** and disable |

Floor filtering builds an `injectable` list from `all_ranked`;
`suppressed_count = len(all_ranked) - len(injectable)`. Presentation truncation
is separate from relevance reinforcement.

**Structural guarantee:** with a non-empty `all_ranked` set, the top-ranked
candidate always has `score >= relative_floor` by construction
(`relative_floor = floor_fraction × all_ranked[0].score`), so the **relative**
floor **cannot blank** a non-empty candidate set. Absolute floor remains optional
and defaults off.

**Reversibility:** `bump_access`, `_strengthen_new_pairs`, and `record_recall` act
on **all** of `all_ranked`, not only post-floor survivors. Suppressed facts still
get recency refreshed — suppression is reversible/repairable (the old
irreversibility hazard is gone).

> **WARNING — quarantine scoring (read histograms per population)**
>
> Quarantined facts are scored with `_QUARANTINE_DISCOUNT = 0.15` — roughly an
> **order of magnitude** below active facts (`_modulated_fact` — `recall.py`
> @`d932c8862`). Score histograms used for floor calibration **must** be read
> **per population** (quarantined vs active), never pooled. A pooled median will
> mis-place the dead-tail boundary.

**Suppression receipts:** `safe_recall_block` — `recall.py` @`d932c8862` sets
metadata `status="floor_suppressed"` when `result.suppressed_count > 0` and no
injectable/renderable facts remain (instead of collapsing to
`"unavailable_or_empty"`). Metadata also carries `suppressed_count` and
`recall_id` when a recall was logged.

**KNOWN GAP (not yet landed):** when the runner gets no renderable block, it
overwrites run-row metadata with `{"status": "unavailable_or_empty"}` and drops
the `floor_suppressed` distinction. Verified in this worktree at
`omniagentos/runner/core.py` ~line 1763 (line numbers drift — search the empty
`safe_recall_block` branch that assigns `metadata["knowledge_recall"]`). Filed as
loopqueue finding **sha256:2d3dd3c5** (one-liner fix pending; not landed). Until
that lands, calibrate and debug floor suppression from recall debug logs /
`last_recall_metadata`, not from the run-row display alone.

### Calibration steps

1. Enable (or leave enabled) debug logging on the `omniagentos.knowledge.recall`
   logger and run a representative batch of recalls.
2. Read the emitted
   `knowledge recall scores count=… min=… median=… max=… absolute_floor=…
   relative_floor=… filtered=…`
   lines — split or filter **per population** (active vs quarantined; see warning
   above).
3. Adjust `OMNIAGENTOS_KNOWLEDGE_RECALL_FLOOR_FRACTION` at the **dead-tail
   boundary relative to the top score** (default `0.15`), not an arbitrary absolute
   constant. Leave the absolute floor at `0` unless you have a measured reason to
   enable it (and stay at or below the plausible max).
4. Confirm receipts: expect `status="floor_suppressed"` + `suppressed_count` from
   `safe_recall_block` when the relative floor removes weak tails and nothing
   injectable remains after presentation — and remember the run-row gap above.
5. Before raising the floor estate-wide, verify with a Hypothesis Tester arm run
   (§5) that the new floor does not regress the lessons / activation arms
   relative to the `20260812-full2` baseline
   (`.claude/worktrees/hypothesis-tester/var/hypothesis_tester/runs/20260812-full2/`).

---

## 4. Failure-time injection design

**DESIGN / NOT IMPLEMENTED** — proposal for a future lane. This runbook does not
implement it.

### Honest prerequisite: no stable refusal-code field yet

Merge-gate **refusal reasons are currently recorded as prose** in gate receipts
under `var/gate-evidence/records/merge-gate/*.json`. A **stable
`refusal_code` / refusal-class field does not exist yet** as a guaranteed
producer-side contract.

Measured on the estate corpus (operator-cited): **zero of 407** refusing receipts
carry reliable `refusal_code` / class fields, and **~20%** are bare `exit-N`
with no regex-recoverable class.

The consolidator lane already **best-effort classifies** for lesson supply
(`_refusal_classes` — consolidator.py @`db2f5d7e1`): it prefers explicit fields
when present (`refusal_code` / `refusal_class` / `reason_code` / nested
`refusal.*`), else slug-parses `refusal_reason` prose via `_REFUSAL_SLUG`, else
falls back to `exit-{exit_code}`. That is a **consumer-side heuristic**, not
proof that producers emit stable codes. Instrument vs candidate separation
(`is_instrument` / `kind="instrument"` vs `kind="trap"`) also depends on that
heuristic plus mechanics-class membership and `instrument_error` flags — still
not a substitute for a producer-stable code.

**Prerequisite work item for any failure-time injection lane:** create and
populate a **stable refusal-code field** on gate receipts (producer-side), then
join lessons to that field. Do not design the join as if the field already
exists.

### Design (future, after the prerequisite)

- Once producers emit a stable refusal code on every refusal receipt, join that
  code deterministically to consolidator-produced lessons
  (`omniagentos/knowledge/consolidator.py` — §2 **Lesson supply**, @`db2f5d7e1`)
  keyed by refusal class, so a retry brief for a failure of class **X**
  automatically carries lessons previously tagged with class **X**.
- **Motivating incident** (operator-cited; do not re-derive): one symbol
  (`seed_cursor`) drew **28** identical reachability refusals over ~**4.5h**. A
  single code→lesson join would have surfaced the applicable remedy on retry #1
  instead of retry #28 — but only once the code is a real field, not prose
  recovery.
- Instrument refusals must continue to surface as `kind="instrument"`
  operator-directed lessons, never as candidate-defect advice
  (`lessons_from_gate_receipts` — consolidator.py @`db2f5d7e1`).
- This is a future-lane proposal only; the stable-field prerequisite is not built.

### DRAFT follow-up proposal — loop-prompt `brief_recall` step

**Status:** draft text for the operator to file as its own proposal. Not
implemented here. Must land through the deterministic gate daemon per
`pipeline/CONTRACT.md`: no loop writes its own prompt.

> **Ask:** Add a per-iteration `brief_recall` step to each loop prompt
> (`pipeline/prompts/PROMPT-planning-loop.md`, `PROMPT-implementer-loop.md`,
> `PROMPT-reviewer-loop.md`) so every iteration surfaces consolidator/lesson
> candidates via `python -m omniagentos.knowledge.brief_recall` (module on
> branch `lane/brief-recall-coverage-0812` @`8b650b52b`; CLI is side-effect-free,
> hardcodes `run_id=None`, and gates on `knowledge_enabled()`). The change is
> operator-authored prompt text only — no loop self-modification — and ships
> only through the gate daemon.
>
> **Acceptance:** each loop prompt documents when to invoke `brief_recall`, what
> empty output means (exit **0**), that exit **2** means could-not-run without
> blocking the iteration, and that exit **3** means the knowledge master switch
> is disabled. Do **not** assume a stable gate `refusal_code` field for failure
> join until that producer prerequisite lands (§4 above). Gate any estate-wide
> policy change with a Hypothesis Tester arm (§5) against the `20260812-full2`
> baseline before adoption.

---

## 5. Gating any new injection policy

Any new injection policy — a new arm, a new floor value, a new seam — should be
expressed as a **memory arm** and run through
`scripts/benchmarks/hypothesis_tester` before estate-wide adoption.

| Constraint | Detail |
|---|---|
| Worktree | `.claude/worktrees/hypothesis-tester` |
| Branch state | **FROZEN** read-only until candidate **sha256:3965f8f5** is reviewed, approved, and lands — do not edit; run as-is. Candidate is **submitted / still LANDING** (not yet reviewed or approved). |
| Typical invocation | `--arms none,<arm>,placebo` — roughly **$0.20** / **~45 min** |
| Pass/fail | Frozen `DESIGN.md` **§6** thresholds — do **not** invent new thresholds |
| Stale instrument guard | `analyze.py` refuses to score a run against a changed instrument unless `--allow-stale-instrument` is passed. Confirmed: `scripts/benchmarks/hypothesis_tester/analyze.py` and `experiment.py` both reference `--allow-stale-instrument` / `allow_stale_instrument` |
| Separate instrument | `scripts/prompt-ab` (`scripts/prompt-ab/run_ab.py`) remains the single-turn **prompt-text** replay tool — different question (prompt wording vs. memory-arm effect) |

Baseline for regression comparison: Hypothesis Tester run `20260812-full2`
(§1). Memcert (`/Users/youruser/OmniAgentOS-memcert`, branch
`feat/memcert-suite-0812`) is the complementary ability-certification instrument;
use both when a proposal changes memory representation
(`devtasks/memcert/RESULTS-2026-08-12.md`).
