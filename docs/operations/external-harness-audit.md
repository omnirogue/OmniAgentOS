# External harness audit — reusable procedure

A repeatable, parameterized way to audit an external agent harness (any coding-agent
runtime not our own — DeepSeek Harness, Claude Code, Codex, OpenCode, the next thing)
against OmniAgentOS and file the result as durable, discoverable dispositions
instead of a one-off writeup nobody can find again.

**This is distilled from what the audit actually did, not an invented process.** The
seed run — DeepSeek Harness vs OmniAgentOS, 2026-08-13 — is the worked example
throughout: `devtasks/harness-audits/deepseek-harness/2026-08-13/dsh-audit-synthesis.md`
(method in its own §"Method" line; the machine-readable 116-mechanism per-seat record
is the sibling `all_lane_results.json`, both committed alongside this runbook). The
full prose seat reports (`lanereports/*.md`) and the prose write-up of the same
116-mechanism record (`audit_appendix.md`) are **not** committed to this repo — see
`RECORD.md`'s own header for where they live. Its final dispositions seed
`devtasks/harness-audits/RECORD.md`, the durable index this runbook feeds.

**Retest guard first.** Before spending a seat on any mechanism, check whether it was
already tested:

```sh
python scripts/ops/harness_audit_guard.py "<mechanism description or keywords>"
```

It greps `devtasks/harness-audits/RECORD.md` and prints any prior disposition —
most importantly a prior **D-reject with its evidence**, so a new audit round does not
re-spend a seat re-litigating a settled question. Exit code is always `0` when the
record was read (this is a lookup tool, not a merge gate); it prints a report either
way. See "Retest guard" below for how to read the output and when a prior D-reject is
worth revisiting anyway.

## 1. Scope and pin the targets

Before assigning a single seat, fix in writing:

- **Target repo + exact SHA** of the harness under audit (e.g.
  `github.com/deepseek-ai/deepseek-harness @ 2f1fca3ce579`). A moving target makes
  every seat's file:line citations unverifiable later.
- **Omni side pinned too** — the exact OmniAgentOS SHA the comparison is against
  (e.g. `OmniAgentOS main @ e9eaea7c74e`). Omni changes daily; without a pin, a
  "gap" claim from one seat and a "already fixed" claim from spot-verification can
  both be honestly true at different moments.
- **Rubric**: the North Star capability matrix
  (`devtasks/northstar-cert/CAPABILITY-MATRIX.md`, codes `C-01`..`C-22`). Every
  finding maps to an existing code or is proposed as a `NEW:<short-name>` extension —
  proposed explicitly, never silently folded into the matrix (matrix edits are a
  separate, deliberate step; this audit process only records the proposal).
- **A one-line executive question** the audit answers (the 2026-08-13 run's was:
  "should omni adopt this harness's architecture, and which specific mechanisms, if
  any, are worth the cost?"). Keep it answerable in one paragraph; the detail lives in
  the dispositions, not the headline.

## 2. Lane decomposition (A–H)

Split the surface into eight lanes. This is the default decomposition the seed audit
used and it generalizes because it tracks omni's own capability areas, not the
target's internal module names — a target with no plugin runtime still gets an "A"
lane, it just concludes "not_comparable" on most rows instead of `dsh_stronger`.

| Lane | Area | Seats (default) |
|---|---|---|
| A | Plugin/extension/composition runtime | dual-lineage, blind |
| B | Sessions, context, memory, compaction | dual-lineage, blind |
| C | Tools, permissions, sandbox, execution security | dual-lineage, blind |
| D | Orchestration — workflows, subagents, scheduling | dual-lineage, blind |
| E | Reliability — failure/recovery/fault handling | single strong lineage |
| F | Observability — telemetry, tracing, dashboards | single strong lineage |
| G | Testing — verification discipline, coverage philosophy | single strong lineage |
| H | External primary-source | single seat |

**A–D are the mandatory dual-lineage core.** These four areas are where an external
harness is most likely to carry either an adoptable mechanism or a dangerous one
(composition/extensibility, context/memory, security/sandbox, and orchestration are
exactly the surfaces where a silent regression is expensive) — hence the extra seat
and the blind cross-check described in §3. E/F/G get one strong-lineage seat each: the
blast radius of a missed finding there is lower and the areas are narrower. Add a
ninth lane (or split one further) if the target has a load-bearing area the default
eight do not cover (e.g. a distinct billing/quota system) — do not force a fit; a
lane that scores `not_comparable` on everything is a wasted seat.

The seed run: 12 seats, 1.66M tokens, 536 tool calls, 12/12 returned — a useful
budget reference for scoping the next one, not a target to hit.

## 3. Dual-lineage blind seats on A–D

For each of lanes A–D, assign the **same lane brief** to two seats from **different
model lineages** (the seed run used Opus 5 + Grok 4.6) with **no cross-talk** — neither
sees the other's findings while working. This is the highest-signal part of the whole
process: when two independently-reasoning lineages converge on the same verdict, at
the same file:line citations, that convergence is strong evidence a human reviewer can
trust without re-deriving it from scratch (§4 "Consensus" is exactly this — e.g. both
lineages independently found the sandbox fallback returning argv unchanged at the same
line number). Where they *disagree*, that disagreement is itself a finding — it goes
to the synthesis pass for evidence-based resolution (§5), never a coin flip.

Lanes E/F/G get a single strong-lineage seat; lane H (§4 below) is always exactly one
seat, because primary-source reading does not benefit from a blind second pass the way
source-reading does — the value there is breadth of source type, not independent
re-derivation.

## 4. Per-seat brief and output shape

Every seat brief states, explicitly:

- The lane's scope (which subsystems/packages to read) and the two pinned SHAs.
- The rubric: score every mechanism found against the North Star matrix.
- The **verdict taxonomy**: `dsh_stronger` / `omni_stronger` / `complementary` /
  `equivalent` / `not_comparable` (rename the non-omni side per target), each with a
  `confidence` of `high`/`medium`/`low`.
- **Cite file:line on both sides.** A verdict without a citation is not evidence. When
  no omni-side equivalent exists, say so honestly (`"not found"`, `"no equivalent
  found; rg -il <term> in <package> only matches ..."`) rather than leaving the field
  blank — an honest absence is itself a finding.
- Every mechanism worth naming gets **a falsifiable test design**, including the ones
  the seat expects to reject — "n/a — reject" plus the one sentence of why is the
  minimum; a real proposed experiment (a differential fuzz test, a mutant-kill corpus,
  a regression probe) is expected for anything the seat proposes to integrate or
  prototype.
- A **proposed bucket** per mechanism: `A` (integrate-now candidate), `B`
  (prototype/benchmark), `C` (watch), `D` (reject) — plus a confidence and a rough risk
  tag (`risk-low`/`risk-medium`/`risk-high`). This is a *proposal*, not the final
  disposition — the synthesis pass (§5) adjudicates the final bucket, and a seat's
  proposed bucket is expected to be overridden sometimes (§5 example: a seat proposed
  `B` for LLM-summarization compaction; synthesis adjudicated `C-watch` pending a
  sibling lane's result — see the seed run's disagreement #2).

Two output artifacts per seat: a **capability_verdicts** list (one row per capability,
matching the taxonomy above) and a **mechanisms** list (the richer per-mechanism
write-up: `dsh_evidence`, `omni_gap`, `expected` benefit, `test` design, bucket,
confidence, risk). Write both to the seat's own report file and to a combined
machine-readable file (the seed run used one JSON array, one element per seat, each
with `capability_verdicts` and `mechanisms` — see `all_lane_results.json`'s shape for
a worked schema) so the synthesis pass can grep/query across all seats at once instead
of re-reading every prose report end to end.

## 5. External primary-source lane (H)

One seat, sourced *only* from material that reading the target's own code cannot
surface: the project's own paper/spec if one exists, GitHub issues and discussion
threads, postmortems the maintainers themselves published, community ecosystem
signal (is anyone building on it, is there a maintainer besides the original author),
and governance health (bus factor, commit concentration, whether issues are even
open). This lane is what caught, in the seed run, that Cordis is a bus-factor-1
upstream with open unresolved temporal-composability defects (issues #19/#26/#34,
PRs #56/#39) and that the underlying paper concedes its own overhead is unmeasured —
none of which a source-reading lane would find on its own, and all of which changed
the final disposition on the Cordis-port question from "maybe" to a firm reject.

## 6. Synthesis pass — one continuing session, spot-verified

One strong-lineage session (not a committee) reads every seat report plus the combined
machine-readable file and adjudicates final dispositions. In order:

1. **Find dual-lineage convergence first** (§3) — cite it explicitly as the strongest
   evidence class; it needs the least additional verification.
2. **Resolve disagreements on evidence, not vibes.** Re-derive from source yourself.
   Record both the resolution and the reasoning — a falsifier ("the paper's cost case
   assumes a restart cost omni doesn't pay, so the argument doesn't transfer"), a
   corrected re-read of the cited line, or an architectural distinction that changes
   applicability. The seed run's §4 "Disagreements, resolved on evidence" is the
   template: four disagreements, each resolved with one sentence of *why*, not a
   majority vote between the two seats.
3. **Spot-verify every load-bearing gap claim in source before it becomes an
   A-integrate filing.** Do not trust a seat's grep for anything that will get filed as
   a lane this session — re-open the cited file:line yourself. The seed run's §8
   "Verification status" names every file:line it personally re-checked before filing;
   that list is what makes the A-bucket lanes trustworthy without a second
   independent re-read of the whole audit.
4. **Dedupe against the live backlog** (loopqueue / open lane proposals) — a finding
   that is already in flight as another proposal gets a pointer, not a duplicate
   filing.
5. **Adjudicate exactly one final disposition per mechanism** (§7), overriding a
   seat's proposed bucket where the evidence disagrees, and say so when you do.
6. **Map every finding to the North Star matrix** — an existing `C-NN` code, or a
   `NEW:<name>` proposed extension, recorded as a proposal (§1).

## 7. Disposition buckets

Exactly one of these four per mechanism, in the final synthesis:

- **A — Integrate now.** Filed as a lane/proposal in the *same session* the audit
  concludes. Reserve for findings that are (a) high-confidence, (b) address a
  *measured* omni defect class — cite the defect: a named incident, a specific
  misclassified run, a postmortem, a doctrine violation — and (c) personally
  spot-verified in source by the synthesis session (§6.3). Filing does **not**
  self-certify the lane — it still goes through the normal review path (Class A for
  anything security/gate-adjacent, per the estate's cross-lineage review policy).
- **B — Prototype/benchmark next.** An approved *direction*, not filed this session.
  Each item needs its own scoped experiment before it can move to A — record the
  smallest falsifying/validating test, not just the idea.
- **C — Watch.** Plausible but not actionable now: gated on an external precondition
  (a fleet capability that doesn't exist yet, a sibling lane's pending result), low
  risk/low value today, or simply not load-bearing for omni's actual operating mode.
  Revisit later; don't action now.
- **D — Reject, with evidence.** Record *why*, in one line, citing the specific fact
  that falsifies adoption — a postmortem, a discussion thread, a doctrine the
  mechanism would violate, a concrete regression it would cause. This is exactly what
  the retest guard (§9) exists to protect: a D-reject with real evidence should not
  cost a future audit a seat to re-discover.

## 8. File the record

`devtasks/harness-audits/RECORD.md` is the durable, **append-only** index of every
**adjudicated** mechanism disposition, across every audit run, with its final
disposition and (for D-rejects) its one-line evidence. This is narrower than "every
mechanism a seat looked at" — a synthesis pass typically adjudicates a smaller final
set (§6–§7) than the full per-seat mechanism record; the record's own header states
each batch's coverage and points at where the fuller per-seat record lives. A new
audit appends a new dated batch section; it never edits or deletes a prior batch's
entries. If a later audit supersedes an earlier disposition (a C-watch item's gating
precondition cleared, new evidence undermines an old D-reject), add a **new** entry
in the new batch noting what it supersedes — the record is a history, not a mutable
table. See the file's own header for the exact entry format.

## 9. Retest guard

`scripts/ops/harness_audit_guard.py` is the mechanical half of "does not re-test what
was already tested and lost" (the reason this runbook and the record exist at all —
see the seed run's own §5.A item 5). Usage:

```sh
python scripts/ops/harness_audit_guard.py "sandbox fallback wrap_command"
python scripts/ops/harness_audit_guard.py "cordis dependency injection" --record devtasks/harness-audits/RECORD.md
python scripts/ops/harness_audit_guard.py "some brand new mechanism" --json
```

It matches on keyword overlap against each record entry's mechanism name, evidence,
and North Star tags, and prints every match ranked by overlap, with **D-reject**
matches called out first since those are the ones a re-test would waste a seat on. A
prior D-reject is worth revisiting only when the query carries a *new* fact the
original evidence did not have (a code change on the omni side, a new upstream
release, a maintainer fixing the cited defect) — cite that new fact in the next
audit's synthesis rather than silently re-running the old test.

## 10. Worked precedent

The 2026-08-13 DeepSeek Harness audit produced: 12 seats (8 dual-lineage + 3
single-lineage + 1 external), 192 seat-level capability verdicts (75 `dsh_stronger` /
71 `omni_stronger` / 33 `complementary` / 7 `equivalent` / 6 `not_comparable`), a
116-mechanism appendix record, and a final adjudication of 5 A-integrate / 13
B-prototype / 9 C-watch / 18 D-reject. Full synthesis:
`devtasks/harness-audits/deepseek-harness/2026-08-13/dsh-audit-synthesis.md`. That
adjudication is what seeds `devtasks/harness-audits/RECORD.md`.
