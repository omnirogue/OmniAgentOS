# The GitHub bridge — how external contributors reach the loops

External developers do **not** touch `var/loopqueue/`. The boundary is GitHub, and this file specifies
what crosses it in each direction.

Two reasons, and the first is not about trust:

1. **`var/loopqueue/` is git-ignored.** It exists only on the host running the loops. A remote
   developer cannot read or write it, so any design that assumes shared filesystem access is
   simply unbuildable for them.
2. **The queue is internal.** `proposals/` is what we intend to build, `rejected/` carries our
   reasoning for killing ideas, and `ledger.jsonl` is a complete history of the work. None of that
   should leave the host as a side effect of letting someone contribute.

---

## What crosses, in each direction

| Direction | Carrier | Contents |
|---|---|---|
| **in** — work | Pull request | code, tests, the commands they ran |
| **in** — research | Issue labelled `suggestion` | an area needing study, and what they don't know |
| **in** — instrument break | Issue labelled `pipeline` | the tooling itself is broken; priority intake |
| **out** — outcome | PR review / merge | verdict with reproducible evidence |
| **out** — plan consultation | Issue labelled `plan` | **only plans we deliberately publish**, one at a time |
| **out** — suggestion answer | Issue comment | a plan, or a reason it isn't being pursued |

Everything else stays inside. Queue depth, WIP, what else is in flight, why something was rejected
internally, spend, model routing, and the ledger are **never** published as a side effect. If a
specific plan should be visible, it is published deliberately as a `plan` issue — one plan, chosen,
not a window onto the queue.

---

## Inbound bridge (runs on the loop host)

A small non-agentic poller, every ~15 minutes:

1. **Issues labelled `suggestion`** → write `inquiries/<id>.json`, `producer.role: "external"`,
   `payload.area` / `observation` / `why_not_a_fix` mapped from the issue body, and
   `evidence_refs: ["<issue url>"]`. Planning treats these exactly like internally-raised
   inquiries.
2. **Issues labelled `pipeline`** → also an inquiry, `area: "tooling"`, `urgency: "high"`. These
   report that the *instrument* is broken, which is the failure class we most often misdiagnose —
   64 of 90 refusals — so they jump the queue.
3. **New PRs** → a `finding` with `source: "external-pr"` and `source_ref` set to the PR URL.
4. **Review comments on our own PRs** → a `finding` with `source: "pr-review-comment"`.

Idempotence: the artifact `id` is the hash of the payload, so re-polling the same issue produces
the same `id` and is dropped at source. **Never include the poll timestamp in the payload** — it
would make every poll a new artifact and flood the queue.

## Close-on-land — landed content closes its own PR

`bridge/land_detect.py` answers one question about a PR: **is this work already on `main`?**
Not by branch name and not by SHA — work here lands by cherry-pick, by rebase, and by being
re-authored inside a train branch, and all three break SHA comparison. GitHub calls such a PR
OPEN forever, so both the PR list and `findings/` drift until a human reads the diffs by hand.

Two consumers, deliberately separate:

| | what it does | outward-facing? |
|---|---|---|
| `bridge/close_on_land.py` | closes the PR on GitHub with a comment naming the landing SHA | **yes** — needs `--apply`, dry-run otherwise |
| `pr_reconcile.reconcile_once(..., land_detector=…)` | records `found → merged` in the ledger | no — queue truth only |

Run the first after a train lands:

```
close_on_land.py --repo <owner/name> --git-dir <checkout>            # dry run, always safe
close_on_land.py --repo <owner/name> --git-dir <checkout> --apply    # actually closes
```

Wire the second by adding `--land-detect <checkout>` to the existing `--reconcile` poll. Both
are off unless asked for.

**It is built to be bad at saying yes.** A missed close costs one more manual triage; a wrong
close costs a colleague their work. So it refuses on a **partial** landing (some files on main,
some not — reported in full, never closed), on an empty diff, on a landing it cannot NAME a
commit for, and on a whole run in which most PRs come back landed at once. Before grading
anything it runs a negative canary — a line nobody has ever committed must come back
not-on-main — because the failure mode that matters is not a wrong verdict but an instrument
that has started agreeing with everything. A verification loop written for bash and run under
zsh did exactly that, and came within one command of closing fifteen PRs.

Validated against a hand-audited pass over 20 live PRs: 4 landed (one only detectable from the
landing commit naming the branch tip), 1 partial, 15 unlanded — 20/20, including refusing to
close the partial.

## Outbound bridge

- **Every inbound `suggestion` gets a reply on its issue** — a link to the plan, or the reason it
  isn't being pursued. A suggestion that disappears silently teaches people to stop sending them,
  and they are the highest-signal input we get from outside.
- **Publishing a plan for comment is a deliberate act**, never automatic. Post the plan's problem
  statement and approach — not its `id`, not its queue position, not what it's competing with.
- **Rejections that reach a contributor are rewritten** for the audience: the internal `reason`
  carries our shorthand and receipt paths. Send the finding and how to reproduce it, not the
  internal record.

---

## What a contributor sees

`FOR-ALICE/INTEGRATION.md` is the complete external-facing document. It contains PR conventions,
the two highest-value failure modes on this codebase, and how to send a suggestion. It contains no
architecture, no queue state, no internal file paths, and no measured-cost or spend figures beyond
the two defect-class statistics that directly help them write better PRs.

**Before adding anything to that file, ask what it reveals about work in flight.** The test is not
whether the information is secret — it is whether it exposes our queue, our priorities, or our
capacity.
