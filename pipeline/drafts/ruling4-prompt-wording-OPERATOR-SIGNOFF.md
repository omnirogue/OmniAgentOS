# Ruling #4 — PROMPT wording changes (OPERATOR SIGN-OFF REQUIRED)

the operator pre-approved the **semantics** (`exit 2 = COULD NOT RUN`, distinct from a candidate
defect and from do-not-retry). The three `PROMPT-*.md` files are **operator-authored**, so
their exact prose is NOT edited in this lane — the proposed replacements sit here for the operator's
eye. Apply verbatim (or adjust wording) once signed, then delete this draft.

The producer-tool code map after the ruling: `0` written/pass · `1` fixable/candidate-defect ·
`2` **could not run** · `3` **do-not-retry** (producer writers only). The behavioural rule
"never re-run a gate on an unchanged input" is unchanged — a could-not-run also must not be
re-run unchanged (fix the mechanics first).

---

## 1. `PROMPT-planning-loop.md` (~line 390-392)

**BEFORE**
> Exit `0` written · `1` fix the named gap and run again · `2` do-not-retry-this-input
> (already filed, or a live rejection) · `3` it could not run — which is never "valid".

**AFTER**
> Exit `0` written · `1` fix the named gap and run again · `2` it could not run — which is
> never "valid" (missing dependency, unreadable draft; its stderr names a conforming
> interpreter) · `3` do-not-retry-this-input (already filed, or a live rejection).

*(Line ~406 — "The tool refuses an unchanged payload as do-not-retry" — stays correct: that
is the genuine do-not-retry, now exit `3`. No change needed.)*

---

## 2. `PROMPT-implementer-loop.md` (~line 421)

**BEFORE**
> **Never re-run a gate on an unchanged input.** `exit 2` means *do not retry this input*. One
> symbol here drew 28 identical refusals — roughly 4.5 hours — because the fix was being
> written where the gate does not read.

**AFTER**
> **Never re-run a gate on an unchanged input.** `exit 2` means *the gate could not run on
> this input* (a dirty/moved workspace, a missing dependency, a base it cannot reach) — fix
> the **mechanics**, then re-run the **same** input; what you must never do is re-run it
> unchanged. One symbol here drew 28 identical refusals — roughly 4.5 hours — because the fix
> was being written where the gate does not read.

*(Line ~226 — the `blocked` row's "do not retry" — is about an external dependency, not exit
2. No change.)*

---

## 3. `PROMPT-reviewer-loop.md` (~line 346)

**BEFORE**
> **Retry semantics, shared across all three loops:**
> `0` pass · `1` candidate-defect · `2` **do-not-retry-this-input**.

**AFTER**
> **Retry semantics, shared across all three loops:**
> `0` pass · `1` candidate-defect · `2` **could not run** (the instrument/gate could not
> evaluate this input — distinct from a candidate defect at `1` and from do-not-retry).

*(The bullets that follow — "Refused twice on the same `(id, base_sha)` → change the input or
the action" and "Three attempts → park" — are the producer-side retry-bounds and are
convention-independent. No change.)*
