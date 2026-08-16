# Ruling #4 — decision log (exit-code 2 = COULD NOT RUN)

Lane `lane/ruling4-exitcode-2-couldnotrun-0809`, worktree off protected `main`.
Operator pre-approved the semantics; this records the mapping and scope decisions.

## The ratified trichotomy

| code | meaning | who uses it |
|---|---|---|
| `0` | pass / written | all |
| `1` | candidate defect / fixable | all |
| `2` | **could not run** — the instrument/gate could not evaluate this input | all (gate + producer) |
| `3` | **do not retry** — a producer writer's genuine dead end (id already filed / live rejection or park) | producer writers only |

The pure gate convention (a loop and its test/lint/build) needs only `0/1/2`; the
do-not-retry concept there lives in the **retry-bounds** rule (a producer counting its own
`rejected` events), not on an exit code. Producer **writers** (`file_proposal.py`,
`file_inquiry.py`) add `3` for their genuine dead end, which a test/lint/build has no analogue
for.

## Why this is a relabel for gates and a renumber for writers

- The gate/instrument exit 2 was already semantically "could not run" — `validate_envelope.py`
  used exit 2 for could-not-run all along, and `merge-gate.sh`'s exit 2 is "refuse before
  verdict" (couldn't reach a verdict). The old *label* "do not retry this input" wrongly cast
  every mechanics fault (64 of 90 recorded gate refusals were mechanics — dirty/moved
  workspace, missing dep) as a permanent verdict on the candidate. So for gates this is a
  **relabel**, behaviour unchanged.
- The producer writers had `2 = do-not-retry, 3 = could-not-run` and even documented the
  "deliberate divergence" from `validate_envelope.py`. To make `2 = could not run` true
  everywhere, `file_proposal.py`'s two constants were **swapped**
  (`EXIT_COULD_NOT_RUN = 2`, `EXIT_REFUSED_DO_NOT_RETRY = 3`); `file_inquiry.py` imports them,
  so it tracks the swap automatically. All call sites use the named constants, so the swap
  propagates without touching logic.

## Changed in this lane (in scope)

- `CONTRACT.md` — §8 table (a) row `2` → could-not-run + a Ruling #4 ratification note; the
  `file_proposal` exit-code line (§ proposals) reordered to `2` could-not-run / `3` do-not-retry;
  the "Rule (a)'s exit-2 case" prose reframed.
- `bridge/file_proposal.py` — constants swapped; EXIT CODES docstring + "divergence" note rewritten (now AGREES with validate_envelope).
- `bridge/file_inquiry.py` — docstring exit-code line updated (constants imported).
- `bridge/validate_envelope.py` — docstring notes exit 2 = could-not-run is now the ratified estate convention.
- `bridge/run-loop.sh` — `rc 2` comments relabelled could-not-run (behaviour unchanged).
- Tests: `tests/test_contract_lens.py`, `tests/test_interpreter_remedy.py` literals flipped to the new convention (do-not-retry 2→3, could-not-run 3→2) + renamed tests whose names encoded the old value; new `tests/test_ruling4_exitcode_couldnotrun.py` mechanically pins the vocabulary. `tests/test_file_proposal.py` needed no change (it already asserts via the named constants).

## Comment-only relabels (no renumber — launchd nonzero-fail tools)

`bridge/github_bridge.py`, `bridge/close_on_land.py`, `bridge/janitor.py` are launchd-driven
and use exit codes only as nonzero-fail signals in a 3-code space that cannot express the
trichotomy (their exit 2 legitimately bundles instrument-fault + terminal error). Renumbering
them is out of scope and risky. Their retired "do-not-retry-this-input" comments were relabelled
to the accurate condition (could-not-run / instrument / terminal error); behaviour is untouched.

## Scope-disambiguated (no renumber)

`bridge/integration.py` **interprets** the external `merge-gate.sh` (in the OmniAgentOS
repo, NOT this package). merge-gate's exit 2 = "refuse before verdict" deliberately bundles
could-not-run mechanics faults WITH hoisted candidate defects (classified by slug), so it must
NOT be read as "could not run" wholesale. The exit-2 prose in integration.py was reframed
convention-neutrally and given an explicit SCOPE note; the anti-storm behaviour (never re-gate
an unchanged input) is unchanged and is convention-independent.

## Operator sign-off pending

- `PROMPT-*.md` (planning / implementer / reviewer) are operator-authored. Proposed wording
  changes are in `drafts/ruling4-prompt-wording-OPERATOR-SIGNOFF.md`, NOT applied to the live
  prompts. Apply after the operator signs, then delete that draft.

## Host-ops follow-up (out of this repo — flagged, not edited)

- **AccurateGate** (`~/.omniagentos/ops/AccurateGate`, a separate checkout, not part of ThreeLoops):
  its short-circuit is keyed on prior REFUSALS and its receipts record `exit_code: 4` for
  storm-parked / unchanged-retry while the process exits 2. Reconciling its exit-code carriers
  with `exit 2 = could not run` is host-ops work; do not edit the live copy from here.
- **`scripts/merge-gate.sh`** (OmniAgentOS repo): its exit 2 bundles could-not-run with
  hoisted candidate defects. If the operator wants merge-gate's exit 2 split into a clean
  could-not-run vs candidate-defect signal, that is a separate change in that repo, tracked
  alongside AccurateGate.
