# Hypothesis Tester — pre-registered experiment design

**Registered:** 2026-08-12, before any confirmatory data collection.
**Theory under test:** an operator-supplied hypothesis document
(core hypothesis quoted below). This harness is the repeatable
instrument; this document is the pre-registration. Numbers in §6 are frozen —
changing them after data exists requires a new experiment id and a note here.

> Core hypothesis (theory doc): a sufficiently observable multi-agent system with
> shared, permission-aware, outcome-weighted semantic memory can transform
> historical traces, successes, and failures into an associative activation
> network in which new inputs automatically evoke relevant learned behaviors …
> "How would we empirically prove that the resulting system is actually learning
> rather than merely retrieving?"

## 1. What we test (and what we deliberately do not)

We test the smallest falsifiable core of the theory, doc §6 "Memory Must Change
Behavior": **does giving an LLM access to data from past runs measurably change
future behavior, and which representation of that data carries the effect?**

Arms map to the doc's memory representations:

| Arm | Name        | Memory given to the agent                                                | Theory-doc anchor |
|-----|-------------|--------------------------------------------------------------------------|-------------------|
| M0  | none        | nothing (baseline)                                                       | —                 |
| M1  | outcomes    | structured win/loss ledger: task summary, action taken, verdict, short code | §2 "we already have the wins and losses" |
| M2  | transcripts | verbatim recent past episodes (task, agent response, verifier feedback), k=6 | §1 shared observability |
| M3  | lessons     | mechanically consolidated stats/lessons derived from episodes (no raw dump) | §2 derived representations, §8 consolidation |
| M4  | placebo     | transcript-formatted memory from a *different world* (same family)        | control — separates content learning from context priming |
| M5  | activation  | top-3 past episodes selected by relevance × outcome-informativeness × recency | §3 semantic activation ("selective exposure", secondary) |

Not tested here (out of scope, later phases): multi-agent routing, embeddings,
permission enforcement, consolidation across runs, real-estate-data corpora.
The harness exposes a corpus-adapter seam so real ledger wins/losses and session
transcripts can be plugged in as a later experiment.

## 2. Task families (synthetic, seeded, mechanically verified)

Three families, chosen so that different memory representations should
*differentially* matter. All hidden structure is derived from a world seed; all
verification is deterministic code, no LLM judges (prompt-ab doctrine).

### F1 `gatekeeper` — hidden-rule gate (mirrors the estate's merge-gate reality)
Agent submits a 6-field config (288-point space). A seeded set of 4 hidden
policy rules (from a 10-rule bank, resampled deterministically until the
feasible fraction is 5–60%) plus 1–2 visible per-episode pins decide
ACCEPT/REFUSE. Refusal feedback names the violated *field* + a stable code
`P<n>`, never the rule. M1 sees verdict+code only; M2/M5 see full feedback text.
Prediction if theory holds: M2/M3 ≫ M0; M1 weakly above M0.

### F2 `strategy` — outcome-only policy learning
12 feature combos (failure_type × blast_radius × repro) map to exactly one
winning strategy of 5, via a semantically plausible base rule + 3 seeded
exception combos. Feedback is win/loss only — there is nothing else to know, so
**M1 (wins/losses) should suffice**; gains must concentrate on exception combos
(logged per-combo). Base-rule priors are absorbed by M0.

### F3 `trapcli` — trap/lesson transfer (mirrors MEMORY.md tribal knowledge)
A fictional CLI (`omnex`) with 4 seeded undocumented quirks (flag ordering,
required suffixes, companion flags…). The provided man-page omits the quirks;
errors are cryptic stable codes (`error: E17 near '--force'`). Wins/losses alone
carry little repair information — **the fix lives only in cross-episode
experience**. Prediction: M2/M3 ≫ M1 ≈ M0. A fictional tool prevents
prior-knowledge contamination.

## 3. Protocol

- A **run** = (family, world_seed, arm, model): 16 sequential episodes. Memory at
  episode t = arm-filtered view of episodes 1..t-1 *of the same run* (online
  accumulation, the doc's learning loop).
- **Paired design:** the task sequence depends only on (family, world_seed), so
  every arm and model sees the identical task sequence; arm deltas are paired.
- Placebo (M4) memory grows at the same rate as M2: a pre-generated 15-episode
  shadow history (same family, seed+1000, scripted seeded random-valid agent,
  real shadow-world verdicts), revealed episode-aligned. Known limitation:
  shadow responses are scripted, not model prose; noted, accepted.
- Malformed/unparseable model output = **loss** (behavioral failure), never an
  exclusion. API errors after 5 backoff retries = episode `error`, excluded from
  denominators; any run with >2 errors is invalid and rerun in full.
- Decoding: temperature 0, top_p 1, fixed seed param, bounded max_tokens.
  Provider returned by OpenRouter is logged per call. Residual provider
  nondeterminism is handled by the seed/model matrix, not denied.

## 4. Matrix and budget

3 families × 6 arms × 4 world seeds × 3 models × 16 episodes = **3,456 episodes**.
Models (pinned 2026-08-12, all smoke-tested): `mistralai/mistral-nemo`
($0.019/$0.03 per M), `meta-llama/llama-3.1-8b-instruct` ($0.05/$0.08),
`openai/gpt-oss-20b` ($0.03/$0.13) — three lineages. Estimated ~5M total tokens
≈ **$0.6–1.5**; ~20–40 min wall at 8 concurrent runs. Pilot runs (any run with
`exp_id` prefixed `pilot-`) are excluded from confirmatory analysis.

## 5. Metrics

- **Primary:** late-window success rate = wins / valid episodes over episodes
  9–16 of a run. Effect Δ(arm) = paired difference vs M0 within (family, seed,
  model).
- Secondary: overall success, episodes-to-first-success, **repeated-failure
  rate** (an action identical to one that already failed this run — the doc's
  §6 "inhibition"), memory prompt tokens, total tokens, cost, latency.
- Calibration logged per world: gatekeeper feasible fraction under pins;
  strategy chance = 20%; trapcli 0% for a quirk-naive canonical answer.

## 6. Pre-registered decisions (frozen)

Statistics: paired bootstrap over (seed, model) runs, 10,000 resamples, 95%
percentile CIs on mean Δ. Per family n=12 pairs per arm contrast; pooled n=36.

| Id | Claim under test | Confirmed iff |
|----|------------------|----------------|
| H1 | wins/losses alone teach policy where outcome is the only information (F2) | Δ(M1−M0) on F2 ≥ +10pp and CI > 0 |
| H2 | experience access improves behavior where structure must be inferred (F1, F3) | Δ(M2−M0) ≥ +10pp and CI > 0 in each of F1 and F3 |
| H3 | consolidated lessons retain the effect at lower cost (§8) | pooled M3 within 5pp of M2 AND M3 memory-tokens ≤ 50% of M2's |
| H4 | the effect is content learning, not context priming (learning vs retrieval) | Δ(M2−M4) pooled ≥ +10pp and CI > 0 |
| H5 | (secondary) selective activation ≈ dump at lower cost (§3) | pooled M5 ≥ M2 − 5pp AND M5 memory-tokens ≤ 60% of M2's |
| H6 | failure memory inhibits repeats (§6) | repeated-failure rate under M2 and M3 each < M0, CI > 0, pooled |

Verdict language: each H reported as CONFIRMED / REFUTED / INCONCLUSIVE (CI
straddles 0 but point estimate in the claimed direction). The theory's core
§6 claim is **supported** if H2 AND H4 confirm; **refuted for this scale** if
H4 refutes (priming explains it) or H2 refutes in both families.

## 7. Logging & repeatability (the run contract)

Under `var/hypothesis_tester/runs/<exp_id>/`:

- `config.json` — frozen matrix, model ids + prices, code git SHA, sha256 of the
  config (the experiment's identity). Key *names* only, never secrets.
- `episodes.jsonl` — one line per episode: run key, episode index, task payload,
  memory text + sha256 + token estimate, full messages, raw model output, parsed
  action, verdict, feedback, usage, cost, latency, provider, timestamps.
- `results.jsonl` / `summary.json` — per-run rollups (prompt-ab naming).
- `ANALYSIS.md` + `analysis.json` — generated verdicts, never hand-edited.

`var/hypothesis_tester/ledger-YYYYMM.jsonl` — append-only experiment verdict
ledger with the config sha256, mirroring `var/prompt-ab/ledger-*.jsonl`.

Resume: re-invoking the same exp_id skips completed episodes (rebuilds memory
from the log), so a crashed matrix continues, never duplicates.

## 8. Relationship to the existing A/B tester (`scripts/prompt-ab`)

Assessed 2026-08-12 (the operator asked whether this should integrate):

- **Different contract, same doctrine.** prompt-ab answers "does this *prompt
  text* change fix this *replayed real failure*?" — single-turn, production
  lineage/effort, strict all-trials promotion. The Hypothesis Tester answers
  "does this *system development* (here: memory access) change *learning
  behavior*?" — multi-episode runs, learning curves, paired bootstrap CIs.
  Merging them would break prompt-ab's clean promotion contract.
- **Shared conventions (implemented):** mechanical grading only; results.jsonl +
  summary.json naming; append-only monthly verdict ledger with sha256 digests;
  var/-rooted run dirs; provenance-or-delete scenario discipline.
- **Composition path:** when a system development ships, prompt-ab checks its
  role prompts on replayed failures; the Hypothesis Tester checks its
  behavioral/learning effect arm-vs-arm. New system developments should define
  their treatment as a memory/context arm (subclass in `memory.py`) and run the
  same frozen matrix — that is the "submit as a lane" use case.
- No prompt-ab files are touched by this lane (path-disjointness).

## 9. Threats to validity (acknowledged now)

- Cheap models may fail to use memory that stronger models would exploit —
  effects are lower bounds, not ceilings; three lineages bound model-specificity.
- In-context learning within a run is not weight-level learning; the theory doc
  itself frames organizational memory as context-mediated, so this is the right
  first target (its Phase 3), not a dodge.
- Synthetic worlds are simpler than production; F1/F3 are deliberately isomorphic
  to measured estate phenomena (merge-gate refusals, MEMORY.md traps).
- Provider-side nondeterminism at temperature 0 exists; logged provider + seeds
  make it visible rather than silently absorbed.

## 10. Pilot amendments (2026-08-12, logged BEFORE any confirmatory data)

Pilot `pilot-0812` (excluded from confirmatory analysis) validated the harness
(32/32 episodes parsed, verdicts and logs correct, $0.0003) and motivated three
instrument changes, made before the confirmatory run:

1. **trapcli error categories.** 8B-class models could not act on fully cryptic
   codes. Errors now carry a category word (`error: E41 (invalid label) near
   'urgent'`) — locating the problem class, never disclosing the fix. Arm-neutral.
2. **System-prompt nudge.** "Never resubmit an action that already failed — form
   a hypothesis about the undocumented rule and try a systematically different
   variant." Arm-neutral (M0 has no records to use).
3. **H2b added** (lessons > none on F1 and F3, same +10pp/CI>0 rule as H2), and
   the core-hypothesis rule amended to `(H2 OR H2b) AND H4`. Motivation: the
   pilot showed raw transcript dumps can ANCHOR a weak model on its own failing
   response (gatekeeper: none 6/8 vs transcripts 1/8 — the model resubmitted the
   refused config seven times). That is the §11 "reinforcing incorrect memories /
   retrieval pollution" pathology; the theory's §3 explicitly predicts naive
   dumping underperforms structured exposure, so the theory's experience claim
   must be allowed to prove out through the structured arm. H2 itself is
   unchanged and still reported.

No frozen threshold from §6 was altered after confirmatory data existed.

### Round 2 — cross-lineage review amendments (2026-08-12)

A GPT-5.6-Sol (xhigh) review of the lane at `000000000` returned 2 BLOCKER /
5 MAJOR / 2 MINOR findings, all with repros. Consequence: **experiment
`20260812-full` is downgraded to calibration data** — its M4 (placebo) arm
leaked target-valid answers wherever the +1000 shadow world drew identical
hidden structure (HT-008, real at trapcli seed 2), and its M2/M5 trapcli
transcripts truncated away the task sentence (HT-002) — and the reportable
confirmatory run is re-executed with the fixed instrument as
**`20260812-full2`**. Fixes, all regression-tested:

- HT-008: placebo shadow world now provably differs in hidden structure
  (`pick_shadow_world`); HT-002: transcript blocks carry the compact task
  summary instead of a blind 400-char truncation of the render.
- HT-001: verdicts require ≥8 pairs (family) / ≥24 (pooled) — depleted
  contrasts degrade to INCONCLUSIVE, never strengthen.
- HT-003: torn JSONL tails are newline-terminated on open, so a repair episode
  never concatenates onto the fragment; HT-005: a run with >2 API errors reruns
  in full as generation g+1 (append-only log preserved; analysis reads the
  newest generation only).
- HT-004: `config_sha` now includes a content hash of the instrument's own
  code, so a resumed exp id cannot silently mix two instruments.
- HT-006: API-key shapes are redacted from logged error bodies; HT-007: retry
  attempts are reported truthfully and the trailing backoff sleep is gone;
  HT-009: gate pin feedback is stable under sort_keys log replay.

Frozen §6 thresholds again untouched; the §10.3 (H2b / core-rule) amendment
stands as registered.

### Round 3 — reopened-finding resolutions (2026-08-12)

Sol's round-2 re-review confirmed 6/9 fixes and reopened three as incomplete
propagation; all three are resolved and regression-tested (29 tests):

- HT-001b: `token_ratio` is now computed over the same PAIRED (family, seed,
  model) sets as the accuracy contrasts, returns NaN below the pooled minimum,
  and NaN maps explicitly to INCONCLUSIVE in H3/H5.
- HT-003b: the torn-tail repair happens in bytes and the log decodes with
  `errors="replace"` (runner AND the analyzer's sibling reader), so a tail torn
  inside a multibyte character can no longer block resume or analysis.
- HT-004b: standalone `analyze` refuses on an instrument `code_sha` mismatch
  unless `--allow-stale-instrument` is passed, and both the run's and the
  analyzer's code_sha are stamped into analysis.json and the verdict ledger.

Re-analysis of `20260812-full2` under the corrected analyzer (stale-instrument
override, stamped) produced byte-identical verdicts — with 216/216 complete
runs there are no unpaired runs for HT-001b to correct, as expected.

### Round 4 — root-cause closure of the two remaining reopenings (2026-08-12)

Round 3 closed HT-003b but showed HT-001b and HT-004b were patched at the
symptom, not the cause. Round 4 removes the causes:

- HT-001b: pairing eligibility is now ONE shared predicate (`_eligible`) used
  by both `paired_deltas` and `token_ratio` — a run excluded from the accuracy
  contrast (e.g. `late_success` None) can no longer enter the token ratio.
- HT-004b: the stale-instrument guard moved INSIDE `analyze()` (which owns the
  artifact/ledger writes); `instrument_code_sha()` has one definition in
  analyze.py shared by the config writer. No entry point can write around it.
- HT-010: `cmd_analyze` reads the override with `getattr(..., False)` — a
  flagless caller gets the evidence-bearing refusal, never AttributeError.

`20260812-full2` re-analyzed once more under the round-4 analyzer: verdicts
byte-identical again (same reasoning as round 3).

### Round 5 — new arm `recall` pre-registered (2026-08-12, logged BEFORE any
### confirmatory data for the arm exists)

Operator goal (the operator, 2026-08-12): raise the overall memory effect ≥20% over the
`20260812-full2` best-arm late-window mean of 0.365 (gatekeeper lessons 0.760,
strategy activation 0.219, trapcli 0.115).

**M7 `recall` — task-conditioned consolidated recall.** A deterministic pure
function of (records, current_task), like every arm. Three mechanisms, all
mechanical (no world internals, no LLM):

1. gatekeeper: if a previously ACCEPTED config satisfies every current pin, it
   is surfaced verbatim as known-safe; otherwise recent accepted configs plus
   the existing per-field acceptance statistics.
2. strategy: exact-profile verdict first (a past win/loss for the identical
   feature combo), then per-`failure_type` marginal win/loss statistics.
3. trapcli: case adaptation — a past winning command for the same
   (sub, json, force) shape is replayed with the current task's names
   substituted by plain string replacement (decoration such as prefixes,
   suffixes, and casing carries over mechanically); weaker fallbacks are a
   same-subcommand win shown verbatim, other-subcommand wins, and the error
   consolidation from M3.

**H7 (frozen before the confirmatory run):** pooled across families with the
same paired-delta machinery as H2 — `recall` beats `lessons` on late-window
success with paired CI > 0 AND `recall` beats `none` by ≥ +10pp with CI > 0.
Per-family deltas are reported descriptively. Minimum-pair rules (HT-001)
apply unchanged. Success against the operator goal is judged OUTSIDE the
harness: best-arm late-window mean across families ≥ 0.438 (+20% vs 0.365) on
a confirmatory (non-`pilot-`) run with the frozen §4 matrix.

### Round 6 — recall v2 mechanisms (2026-08-12, logged after `20260812-recall-full`
### was LAUNCHED with v1 but before its analysis; v2 is judged only on later runs)

Literature-motivated rendering changes (Krishnamurthy et al. 2403.15371; Monea
et al. 2410.05362; Olausson et al. 2306.09896; Shinn et al. 2303.11366): a weak
model should never see its failure as an imitable example, only as an
externally computed constraint or directive.

- strategy: forced-choice elimination ledger (PROVEN WRONG / NOT YET TRIED /
  RULE: pick from NOT YET TRIED), KNOWN-CORRECT collapse on a win, and an
  EXCEPTION flag when the model's own choice lost ≥2× on one profile. The
  prose prior hint ("usually depends on failure_type") is removed; marginal
  stats render as counts only.
- trapcli: verified-fix pairs (a refused command and a later ACCEPTED command
  of the same shape, shown together as the undocumented rule's boundary);
  when no fix is verified, freeze-all-but-the-'near'-token directive plus a
  REFUSED-variants list (constraint framing, never an answer-slot render).
- gatekeeper: cold-start coordinate descent — the refusal names one field;
  the directive freezes the other five and enumerates refused vs untried
  values for that field (REQ refusals render the one-field correction).

Same H7 thresholds; v1-vs-v2 compared descriptively on paired seeds/models.

### Round 7 — HT-011 instrument fix: completion cap starved gpt-oss-20b
### (2026-08-12, arm-neutral, logged before the v2 confirmatory run)

Every late gatekeeper MALFORMED for gptoss in `pilot-recallv2g` (18/18) was an
exactly-700-token completion with EMPTY message content: the model spent the
whole `max_tokens=700` budget on its reasoning channel and never emitted the
answer. That scores "cannot speak" as "cannot learn" — an instrument artifact
affecting every arm equally (`none` included). `max_tokens` is raised to 2500.
Arm-neutral by construction; it changes `code_sha`, so all pre-fix exp ids
remain frozen under the old instrument and are not resumed. Comparisons
against pre-fix runs (incl. the 0.365 operator baseline) are reported BOTH
ways: the system-level delta (new instrument + new arm vs old baseline) and
the memory-attributable delta (recall vs none/lessons WITHIN the post-fix
run), so the cap fix can never masquerade as a memory effect.

### Round 8 — recall v3: trapcli quirk-rule extraction (2026-08-12, logged
### before any confirmatory data for v3)

Goal escalation (the operator): EVERY family +20% — trapcli 0.115 -> >=0.138 is the
binding constraint. New mechanism, deterministic and provenance-cited: each
ACCEPTED command is diffed against the DOCUMENTED form of its task (the man
page shown in every prompt — public information, zero undocumented content);
only generic transforms are detected (leading-'@' prefix, '.out' suffix,
UPPERCASE, flag-follows-flag insertion, end-of-command insertion, '--json'
position). Extracted rules are applied to the documented form of the CURRENT
task and the constructed command is offered. Rules exist ONLY where a win
evidences them (no wins -> no rules -> unchanged behavior); a win on one
subcommand bootstraps the others. Offline proof: with 8 wins of fuel the
constructed command verifies WIN for every later episode across 6 seeds
(test_recall_trapcli_rule_extraction_bootstraps_other_subcommands).

### Round 9 — 32-episode extension pre-registered (2026-08-12, logged before
### any 32-episode data exists)

Measured ceiling at 16 episodes: strategy recall shows ZERO compliance
failures (never picks a proven-wrong strategy, never ignores a known-correct
one) — its 0.344 late-window is the information ceiling of a design where
each profile repeats at most once. trapcli is cold-start-bound (7 of 12 v3
pilot runs end with zero wins, so rule extraction never gets fuel). Both are
run-length limits, not memory-representation limits.

**Experiment `20260812-ep32` (frozen before launch):** same worlds, seeds and
models; `--episodes 32`; arms none, lessons, activation, placebo, recall.
Late window for 32-episode runs = episodes 17-32 (the second half, same
fraction as 9-16/16). Operator metric per family: recall late-window minus
the best non-recall arm's late-window IN THE SAME RUN (same length — no
cross-length comparisons) with the +0.20-absolute operator target evaluated
on that same-run contrast, alongside the H7-style paired CIs. Episode
sequences beyond 16 are generated by the same world constructors with
n_episodes=32 (deterministic; strategy worlds extend the shuffled combo
cycle; trapcli extends the subcommand cycle; gatekeeper draws more tasks from
the same feasible set).

### Round 10 — 4th pinned model screened in (2026-08-12)

Screening pilot `pilot-modelscreen` (trapcli, recall arm, seeds 0-1) tested
qwen3-30b-a3b, gpt-oss-120b and mistral-small-3.2-24b for the one capability
the pinned trio lacks: WINNING a quirk-carrying task at least once.
mistral-small-3.2-24b produced 2 quirk-revealing wins at seed 0 — the seed
with ZERO quirk-free episodes, where the incumbent trio has never won at all;
the other candidates produced none. `mistral24b` is promoted into the pinned
set for trapcli continuation experiments; family metrics that quote the
original 3-model baseline stay 3-model and are reported separately from
4-model runs.

### Round 11 — counterfactual D (`corrupt`) and cross-model transfer
### (2026-08-12, logged before any data for either exists)

Adopted from the World-Class Memory upgrade doctrine after triage against
what the harness already covers (counterfactuals B=none and E=placebo exist):

- **M8 `corrupt`** — the agent's OWN transcript history with every verdict
  flipped (wins shown with a real refusal text from the same run; failures
  shown with the family's success text). Deterministic; an instrument for
  poisoning susceptibility, not a memory representation. **H8:** corrupt vs
  none, paired: a robust consumer shows corrupt >= none - 5pp; corrupt
  significantly below none (CI < 0) quantifies poisoning damage.
- **Cross-model transfer mode** — `--donor-exp/--donor-model`: the consumer
  model receives the DONOR model's episode history for the same (family,
  seed, arm), aligned episode-for-episode; its own attempts never enter its
  memory. **H9:** donor-fed recall vs same-run none, paired: CI > 0 means
  memory written by one model transfers to another (the estate's multi-agent
  reality). Donor-fed vs own-history recall (same run matrix) reports the
  transfer discount descriptively.
