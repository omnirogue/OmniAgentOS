# Continuous improvement loop — sandbox

You work continuously on this repository, improving it in small, verified steps.

You have **write access**. Branch, commit, and open PRs freely. Never force-push, never rewrite
published history, and never delete a branch that isn't yours.

Each iteration is **one problem taken to a finished conclusion** — a merged PR, or a written
explanation of why it can't be done. Never leave work half-landed.

---

## Before you explore anything

**Read `ARCHI.md` first.** That is what it is for. Re-deriving the architecture by grepping is slow
and gets it wrong, and this codebase has enough near-duplicate code that a wrong mental model sends
your fix to the wrong layer. If `ARCHI.md` and the code disagree, **that disagreement is itself
worth reporting** — a stale architecture doc misleads everyone who reads it next.

Then read the tests around whatever you're touching. They encode decisions no comment explains.

---

## Step 1 — pick one problem

Failing tests first, then obvious defects, then things `ARCHI.md` marks as known-weak. **One
problem per iteration.** A change that grows beyond its original problem should be split — one PR
per concern lands far faster than one PR with three.

---

## Step 2 — classify the failure BEFORE writing code

This is the step most likely to be skipped and the most expensive to skip.

| what you're looking at | what it means | what to do |
|---|---|---|
| the code is genuinely wrong | a real defect | fix it |
| the tooling, environment or host failed | says **nothing** about the code | fix the environment, or report it — do not "fix" code that isn't broken |

**Measured on this codebase: 64 of the last 90 failures were environment problems** — a stale
workspace, a missing git identity, a dirty tree — not defects. The sharpest example: a CI job with
no git identity made `git merge` exit 128, and the tooling reported *"conflicts against main"*
while **silently skipping every test suite.** There were no conflicts, and nothing said so.

**A tool telling you the code is broken is not evidence that the code is broken.** Reproduce it
yourself before you believe the label. If a failure message doesn't match what you see locally,
suspect the environment first.

---

## Step 3 — find every copy before you fix one

**Do not skip this.** This codebase has a great many near-duplicate code paths — the same logic
implemented in two or three places that have drifted apart. **A fix is usually 2–3 fixes.**

Before writing anything, name the *behaviour* you're protecting, then find every place it lives:

- every function that parses, validates, or writes that value
- every caller, and every sibling of the site you found
- `grep` for the distinctive strings, not just the function name

For each one, decide out loud: **fixed**, or **not applicable, and why**. Put that list in the PR.
A reviewer auditing your list is fast; a reviewer hunting for the copy you missed is slow, and
usually finds it after you've moved on.

---

## Step 4 — write the test first, and watch it fail

1. Write the test **before** the fix.
2. Run it. **It must fail.** If it passes before your change, it isn't testing your change.
3. Make the fix. Run it again. It must pass.
4. Revert only the fix (keep the test) and confirm it goes red again.

Step 4 is the one people skip, and it's the one that proves the test works. On this codebase
**11 of 15 rejected changes had tests incapable of catching the defect they claimed to fix.** A
test that passes both before and after pins nothing.

Also check the abnormal paths: missing file, empty input, unparseable value, unexpected exception,
absent environment variable. **None of them may quietly produce a normal-looking result.** A
function that returns `0` when it means "I don't know" is worse than one that fails loudly, because
zero looks like an answer.

---

## Step 5 — open the PR

Include:

- **What was wrong**, as something observable — not "improved error handling"
- **The commands you ran and what they printed.** `pytest tests/foo -q → 41 passed` skips an entire
  round trip. A claim with no command has to be re-derived by whoever reviews it.
- **Your list from Step 3** — every copy you found, fixed or ruled out
- **What might break.** Name the blast radius, not the happy path.

Then stop and wait for review. **Don't start a second PR on the same files** — they'll conflict
with each other and both get slower.

---

## When you're stuck

Say so, early, in the PR or an issue. State what you tried, the exact error, and what you ruled
out. **Three attempts at the same failing approach is the signal to stop and ask** — if something
refuses twice in the same way, the thing you're editing is usually not the thing it's reading.

And **don't escalate effort on a repeated failure** — change the *approach* instead. Look at what
the failing tool is actually reading, or find the sibling copy you missed.

---

## Never

- Force-push, or rewrite history that's been pushed
- Change a test to make it pass. If a test is wrong, say why in the PR and fix it deliberately.
- Widen a change beyond its problem. That's a second PR.
- Claim a result you didn't run. Say "I read this" or "I ran this" — never blur them.
- Report an environment failure as a code defect.
