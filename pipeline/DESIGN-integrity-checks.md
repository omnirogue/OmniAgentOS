# Loop integrity checks
**Draft for adversarial review · 2026-08-07**

## The proposal, and the thing it deliberately is not

An **hourly mechanical test suite** that asserts the system's own mechanisms still work, writing
failures into the existing queue as findings and inquiries.

**Not an agent that judges system health.** That distinction is the whole design:

- A test asserts a property and cannot rationalise. `assert cap_fired == True` either holds or does
  not.
- An agent asked "is the system working?" produces prose, which another agent may or may not act
  on, and which can be confidently wrong in exactly the way the thing it is checking was.

We have a loop that proposes process improvements — Planning. Giving a second component that job
creates two judgment sites for one policy, which is the shape that has already produced one live
auto-approve bypass here.

**So: no fourth loop. A cron, a test suite, and the existing channels.**

---

## Why this is essential rather than nice-to-have

The estate's dominant defect class is **a guard that passes while protecting nothing.** Measured
instances, all found by accident rather than by design:

| what looked fine | what was true |
|---|---|
| `validate_proposal` hard-stop gate | never ran for `doc` targets — arbitrary shell could reach `merge-gate.sh` |
| `hard_cap_hit` fail-closed on unknown cost | unreachable: the writer wrote `0.0`, so "unknown" never occurred |
| spend ceiling | reads `$0.00` forever off one machine — SQLite silently creates an empty DB |
| 8 Claude accounts "expired" | a symlink farm; the accounts were fine |
| 13 "live accounts" | one identity reachable 13 ways |
| 64 of 90 gate refusals | instrument errors reported as candidate defects |

**Every one of these was invisible to the thing that owned it.** That is the argument: not that the
loops are unreliable, but that a mechanism cannot verify itself.

---

## What gets tested

### A. Reachability — does the guard actually fire?

The highest-value category, because this is where every incident above lived. **A guard with zero
recorded invocations is suspicious, not healthy.**

| check | method | fails when |
|---|---|---|
| governor fail-closed | write a corrupt `budget.json` to a scratch queue; run the loop's governor read | it proceeds instead of stopping |
| rejected-dedup | write a rejection, then offer the same `id` | it is not dropped at source |
| claim exclusion | two concurrent `O_EXCL` creates on one id | both succeed, or neither |
| claim steal | expire a marker, run two stealers | both end up holding it |
| janitor parked-exemption | park an artifact aged 30d, sweep | the artifact is deleted |
| ledger torn tail | append a truncated line, read | the reader aborts instead of skipping |
| id determinism | recompute `sha256(jcs(payload))` for every artifact | stored `id` ≠ recomputed |

### B. Liveness — is anything actually happening?

| check | fails when |
|---|---|
| each loop appended to the ledger within its expected interval | a loop is silently dead |
| bridge polled within 2 poll intervals | the external boundary is shut with no alert |
| governor wrote `budget.json` within 2 ticks | the counters are stale and nothing said so |
| queue depth is not monotonically increasing over 6h | production is outrunning landing |

### C. Favourable absence — the sweep nobody runs

| check | fails when |
|---|---|
| any counter reads exactly `0` after demonstrable activity | a broken meter reading as thrift |
| any directory that should be non-empty is empty | e.g. zero receipts after N merges |
| any enum field only ever takes one value | a classifier that has never classified |
| any guard's failure branch has zero hits, ever | plausibly unreachable — flag for a reachability probe |

The last row is the one that would have caught three of tonight's six.

### D. Contract conformance

Every artifact validates against `schema/envelope.schema.json`; every rejection carries
`expires_at`; every candidate carries ≥1 `verified_by: execution`; `paths` covers the actual diff.

---

## How failures reach the loops

**No new channel.** Reuse what exists, and let the failure class pick the destination:

| test failure | artifact | who acts |
|---|---|---|
| a mechanism is broken (A, D) | `finding` — `source: "integrity-check"` with the failing assertion | Repair |
| a mechanism may be unreachable (C) | `inquiry` — `area: "tooling"`, `why_not_a_fix` = what the check could not determine | Planning |
| a loop is dead or stalled (B) | one line to `ALERTS.md`, once | the operator |

A failing assertion is a **finding**, not a suggestion, because it names a specific broken thing.
Anything requiring judgement about *why* becomes an **inquiry**, which is the artifact designed for
"something is wrong here and I do not know the fix."

**It never proposes process changes.** If a pattern in the failures suggests a loop should work
differently, that is an inquiry, and Planning decides. The checker reports; it does not prescribe.

---

## What it costs

A cron entry, one test file, and a `--once` runner. No new account, no new loop, no model calls —
**every check is mechanical**, which is the point. If a check needs a model to decide whether it
passed, it is not a check, it is an opinion, and it belongs in Planning.

---

## Open questions for review

1. **Is hourly right?** Liveness wants minutes; reachability probes are expensive and want daily.
   One cadence or three?
2. **Who checks the checker?** If the integrity suite silently stops, nothing notices — the exact
   failure it exists to catch. A heartbeat it writes and the janitor reads?
3. **Does the reachability category need a scratch queue?** Testing "does the governor stop"
   requires a *broken* budget file, which must never touch the live one.
4. **Is category C testable at all**, or does "a guard that never fires" produce so many false
   positives that it gets ignored?
5. **Should a failing integrity check stop the loops?** Fail-closed says yes. But a false positive
   then halts a working system, and the checker is new code with no track record.
