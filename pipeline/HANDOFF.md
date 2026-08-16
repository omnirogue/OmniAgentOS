# Handoff — building a loop that runs ON the loop host

> **Scope: this is for a loop running on the same machine as the queue** — one that reads and
> writes `var/loopqueue/` directly. `var/loopqueue/` is git-ignored and exists only on that host.
>
> **An external or remote contributor does not use this file.** Their boundary is GitHub: PRs for
> work, `suggestion` issues for research, `plan` issues for consultation. See `BRIDGE.md` for what
> crosses that boundary, and `FOR-ALICE/INTEGRATION.md` for the document they actually receive.

**What's binding: `CONTRACT.md` and `schema/`. That's it.**

Everything else in this folder is context you are free to read, adapt, or ignore. How you find
work, how you reason about it, which models you use, how you review — your call entirely. The
contract exists so independent loops can share a work queue without corrupting each other, not to
tell you how to build yours.

---

## Read in this order

| # | File | Why |
|---|---|---|
| 1 | **`CONTRACT.md`** | the whole interop surface — layout, envelope, claims, ledger, retries |
| 2 | **`schema/envelope.schema.json`** | validate your artifacts against this |
| 3 | **`schema/ledger-event.schema.json`** | validate your ledger lines against this |
| 4 | `README.md` | orientation + the `jq` recipes for reading system state |
| 5 | `bootstrap.sh` | creates the directory tree; run once |

**Optional, estate-specific, not required of you:** `MISSION.md` (what the system is for),
`ROUTING.md` (how we survive one provider rate-limiting us), `PROMPT-*.md` (our loop prompts),
`profile/omniagentos.schema.json` (extra fields *we* demand of *our* loops — deliberately kept out of
the base schema), `DESIGN.md` (rationale; superseded by the contract wherever they disagree).

---

## The five things that will bite you if you skim

1. **`exit 2` means do not retry this input.** Not "retry later" — the input itself is wrong.
   Change the input or the action; never repeat the same `(id, base_sha)`.
2. **Artifacts are immutable.** Claim with an atomic `O_EXCL` marker in `claims/`, never by editing
   the artifact. Editing is a read-modify-write race and *both* racers win it.
3. **A tool failing is not the code failing.** Classify before reacting. On this codebase **64 of
   90 gate refusals were instrument errors** — the tooling, the host, a missing git identity — not
   the code. Reproduce a failure yourself before believing its label.
4. **`paths` must list every file you touch.** The conflict graph that lets lanes land in parallel
   is built from it. Understating it corrupts other people's work, not just yours.
5. **`base_sha` is the full 40 characters.** Abbreviations have collided here.

---

## Two environment requirements

- **Local filesystem, single host.** Claiming and the ledger rely on POSIX `O_EXCL` and `O_APPEND`.
  NFS, SMB, Dropbox, iCloud and Google Drive break them **silently** — no error, just lost writes.
- **Exactly one Integration instance.** It holds the only write lock on `main`.

---

## Where your loop plugs in

You'll most likely build a **producer** — Repair (find and fix), Executor (build an approved plan),
or both. Producers write `candidates/` and never touch `main`; Integration takes it from there and
guarantees every candidate gets exactly one terminal outcome: `merged`, `rejected` (with a
machine-readable reason and a TTL), or `parked` (a human decision is owed).

Two things worth knowing because they're easy to miss:

- **`rejected/` is checked before you start work, not after.** If an item's `id` is there
  unexpired, drop it at source. This is what stops loops rediscovering the same dead idea — one
  symbol here drew 28 identical refusals, about 4.5 hours, for want of exactly this check.
- **`inquiries/` is the reverse edge.** If you notice something that needs *study* rather than a
  patch — a recurring pattern, a slow step, a fix you suspect has siblings — write an inquiry and
  keep moving. It costs you the rest of an iteration, never a lane, and it goes to whoever is doing
  planning. The required field is `why_not_a_fix`: say what you *don't* know. That's what makes it
  a research task instead of a complaint.

The contract specifies producer-side interop. Building an *Integration* loop additionally needs the
gate semantics in `DESIGN.md` §3 and §5.

---

## Questions

Anything ambiguous in `CONTRACT.md` is a bug in `CONTRACT.md` — say so and it gets fixed. It has
been through four adversarial review rounds; the ambiguities that remain are the ones nobody
thought to look for.
