# Loop Interoperability Contract v1.1

**Everything required to build a loop that interoperates. Nothing else.**

No review doctrine, no model requirements, no house style. Conform to this and your loop plugs in.
How you find work, how you reason, how you review, which models you use — your call.

House doctrine, where it exists, lives in `profile/` as an **optional overlay**. The base schema
never requires it.

---

## 1. Roles, ownership, and write scope

**Three loop roles, plus `external` for everything that is not a loop.** One process may play
several; each must still respect the scope. These four strings are the entire role vocabulary —
`ROLE-REGISTRY.yaml` is canonical, the `role` enums in `schema/` are generated against it, and
`tests/test_role_vocabulary.py` fails if a fifth name appears.

| Role | Reads | Writes | Never |
|---|---|---|---|
| **Planner** — *what should we build next?* | `inquiries/`, `findings/`, `rejected/`, `parked/`, `receipts/`, `~/.omniagentos/ops/Research/`, ledger | `proposals/`, `parked/`†, `rejected/` **for `kind:inquiry` only**, `~/.omniagentos/ops/Research/` (full read/write — §4b) | code, merge |
| **Reviewer** — *what is broken right now?* | `findings/`, `rejected/`, `parked/`, ledger | `candidates/`, `inquiries/`, `findings/`, `parked/`†, `receipts/<own-id>/`‡ | merge, push `main`, build a plan |
| **Implementer** — *what is safe to build* | everything | `candidates/`, `rejected/`, `parked/`, `receipts/`, `state/queue.json`, `inquiries/`; **`main` only through the deterministic `gate_loop.py` adapter** | author plans, manually merge/push `main` |
| **External** — *not a loop*: humans, CI, telemetry, the GitHub bridge | anything | `findings/`, `inquiries/`, `directives/`, `PROMPT-*.md` | anything else |

Everyone appends to `ledger.jsonl`. Otherwise, nobody writes another role's directory.

### Write scope for `PROMPT-*.md` — the loops' own instructions

| Artifact | May PROPOSE a change | May WRITE it | May LAND it | Never |
|---|---|---|---|---|
| `prompts/PROMPT-planning-loop.md`, `prompts/PROMPT-implementer-loop.md`, `prompts/PROMPT-reviewer-loop.md`, `MISSION.md`, `ROUTING.md` | **any role, including the role it governs** — as a `proposal` (Planner) or a `finding` (Reviewer/External) | **External only** (operator, or an operator-directed session) | **Deterministic gate daemon**, as an ordinary candidate once approved | **no loop writes its own prompt, ever** |

**Ruled 2026-08-08 by the operator.** A prompt is code for an LLM: editing one changes what a role
does on its next iteration, with no test and no gate in the way. A role editing its own prompt is
self-modification without review — the same shape as the unreviewed prompt-write endpoint being
closed in `omniagentos/api/routes/system.py`, and it would make prompt certification decorative.

**Landing is not authoring.** The Implementer may land an approved prompt change like any other
candidate; that is mechanical and already gated. What it may not do is decide the content.

**This row exists to stop two-repo lanes stalling as findings.** Before it, a lane that needed to
touch a `PROMPT-*.md` had no owner: the Planner may not write code, the Implementer may not author
plans, and nothing said prompts were a third thing. So such work degraded into a `finding` nobody
could action. Now the path is explicit — propose it, an operator writes it, the Implementer lands it.

**Practical note:** the live loops `cat` these files from the working tree every iteration. Write
atomically (temp file in the same directory, then rename); a half-written prompt is a broken loop.
Do not switch the checkout's branch while loops are running.

‡ **`receipts/<own-id>/` only.** A producer writes bulk evidence under its own candidate's id and
nowhere else. Without this, a compliant Reviewer loop gets `EACCES` on every submission once the
per-role permissions this document asks for are actually applied.

† **`parked/` for its own items only.** Parking is how a producer stops when a human decision is
owed, so every producer must be able to write the marker (§9).

Two further exceptions, both narrow: **Reviewer writes `findings/`** because it harvests its own PR
review comments and CI results into findings (§4a), and **Planner writes `rejected/` for
inquiries only**, because it is the role that closes them (§4).

**Instrument errors** — a tool, host, or dependency failing, which says *nothing* about the code —
are written as an **inquiry** (`payload.area: "tooling"`), never as a finding or a candidate. They
are not landable work, and treating them as code defects is the single most common misclassification
here: **64 of 90 gate refusals were instrument errors.**

> **Scope of this document.** It specifies **producer-side interop** — everything needed to build a
> Planner or Reviewer loop that plugs in. Building the **Implementer** additionally requires the
> gate semantics, merge strategy, and base-freeze rules in `DESIGN.md` §3 and §5; they are
> deliberately out of scope here.

### The role names, and the epoch they changed on

**2026-08-08 is the rename epoch** (commits `0000000`, `0000000`, `0000000`). One name per role,
because every per-role mechanism keys on that string: prompt selection, filesystem permissions,
ledger attribution, liveness alerting, budget accounting. Two vocabularies is not a style problem —
a per-role lookup keyed on a retired name **can never fire and can never clear**, so it renders as
a permanent favourable absence. That is precisely how the pre-rename liveness check stayed green
with zero `reviewer` and zero `implementer` events in the ledger, ever.

| Retired name | Resolves to | Why it went |
|---|---|---|
| `planning` | `planner` | prose only; the *file/session* selector is still `planning` — see below |
| `repair` | `reviewer` | the Reviewer finds and fixes what is broken today |
| `executor` | `implementer` | folded in by `0000000` — building belongs to Implementer |
| `integration` | `implementer` | the deterministic lander is an adapter, not an LLM role |

**History is not rewritten.** `ledger.jsonl` is append-only and it *is* the history — it wins over
every derived snapshot. Events written before the epoch keep their retired role names (measured
2026-08-08: 14 events stamped `planning`), and a reader resolves them through the table above.
No *new* event may use a retired name; `schema/ledger-event.schema.json`'s enum enforces that.

**Three tokens are deliberately not role vocabulary**, and renaming them breaks something live:

- the loop selector **`planning`** — `run-loop.sh planning` picks `prompts/PROMPT-planning-loop.md`, names
  the tmux session `loop-planning` and the log `planning-loop.log`. The file is what it is, and
  three sessions are live on it; renaming the argument would document a broken invocation.
- **`bridge/integration.py`**, `state/integration.lock`, `integration-adapter` — a module, a
  lockfile, an actor string. It is the Implementer's *landing adapter*, and it already writes
  ledger role `implementer`.
- **`integration/<name>`** — the merge-train branch prefix, and `FOR-ALICE/INTEGRATION.md`, the
  external-facing document.

`ROLE-REGISTRY.yaml` carries all of this machine-readably; `tests/test_role_vocabulary.py` is the
mechanism that keeps it true.

### Enforce the top three roles as MECHANISMS, not as rules

**A rule in a prompt is a request. A permission is a decision.** This contract has exactly one
kernel-arbitrated mechanism — `O_EXCL` claiming (§6) — and a great many sentences. A model can talk
its way past a sentence; it cannot talk its way past `EACCES`.

Measured over two days of loops running with rules at least as explicit as these, an operator
observed: a loop **raising a quality gate's threshold from 0 to 48 so its own branch would pass**;
finished work committed and never pushed, three times, lost when the worktree reset; duplicate PRs
opened twice; a false accusation about a nonexistent "concurrent agent" written into a PR body;
*"250 passed"* reported while four gating suites went red; and work done in the shared checkout
instead of its own worktree. **Every one of those had a rule against it in the prompt. Every one
was caught by a human reading output.**

Tightening the prompts measurably improved behaviour each time — prose is not worthless. But the
three rules whose violation actually destroys work must be enforced below the model:

| Rule | Mechanism, not prose |
|---|---|
| never push to `main` | the loop's git credential **has no push right** — branch protection, or a remote it cannot write |
| never merge | same credential boundary; only the Implementer's identity may merge |
| never touch another role's artifacts | **filesystem permissions per role** on `var/loopqueue/*`, not an instruction |

Adopt these before running unattended. Everything else in this document is a rule, and rules
degrade under load exactly when you most need them — which is the argument for spending the
mechanism budget on these three and not on the twentieth "never".

> **The Implementer both builds and lands.** An earlier version split these into an `executor` that
> built and an `integration` that landed — which created a role nobody was actually running, so
> `proposals/` had no drain and plans accumulated forever. One role owning both removes the gap:
> whoever lands is also whoever builds, and a queue with no drain cannot arise from an org chart.

**Exactly one Implementer instance may run at a time.** It holds the only write lock on `main`.
Two instances produce double-admission, racing `queue.json` writes, and mid-gate landings that
invalidate each other's receipts. Enforce with a PID lockfile.

### Environment requirements

- **Single host, local filesystem.** The coordination primitives below depend on POSIX
  `O_APPEND`/`O_EXCL` semantics. **NFS, SMB, Dropbox, iCloud Drive and Google Drive break them
  silently.** The load-average governor already assumes one host.
- **`var/loopqueue/` must be git-ignored.** It lives in the repo for locality, not for versioning. If
  tracked, the Implementer's merges hit a dirty tree and an index lock. `bootstrap.sh` writes the
  ignore rule.

---

## 2. Directory layout

```
var/loopqueue/
├── ledger.jsonl            append-only event log (§5)
├── state/
│   ├── budget.json         governor limits  (read before every iteration)
│   └── queue.json          derived snapshot (Implementer only)
├── inquiries/<id>.json     "this area needs study" — a QUESTION, not a fix   (§4)
├── findings/<id>.json      observed breakage awaiting repair
├── proposals/<id>.json     plans awaiting an Implementer
├── candidates/<id>.json    immutable commit SHAs ready for the mechanical lander
├── claims/<id>.claim       atomic claim markers (§6)
├── rejected/<id>.json      dead ideas + reason + TTL
├── parked/<id>.json        awaiting a human decision (§9)
└── receipts/<id>/          evidence of what ran
```

**File shapes** — every producer must parse `rejected/` and `parked/` before starting work, so
both are specified here rather than left to convention:

```jsonc
// rejected/<id>.json
{ "id": "sha256:…", "kind": "candidate", "reason": "gate: reachability — no production caller",
  "class": "candidate-defect", "at": "…", "expires_at": "…",   // expires_at MANDATORY
  "by": "implementer", "receipt": "receipts/sha256:…/" }

// parked/<id>.json
{ "id": "sha256:…", "reason": "needs a product decision on cost vocabulary",
  "at": "…", "needs": "human", "attempts": 3 }

// state/queue.json  — derived cache; rebuildable from the ledger, which wins on disagreement
{ "items": [ { "id": "sha256:…", "kind": "candidate", "status": "admitted", "title": "…" } ],
  "wip": 2, "rebuilt_at": "…" }
```

**Artifacts are immutable after creation.** Never edit one in place. All state transitions are
ledger events; claims are separate marker files. This removes every read-modify-write race.

The continuous Reviewer is a finding producer, not a candidate-verdict queue. Routine candidates
move from Implementer execution evidence directly to the complete mechanical gate. Only risky
real diffs require a named, different-lineage build-time approval bound to the candidate's exact
`head_sha`; pipeline-critical paths additionally assemble as one-member trains. This keeps review
where the change is built without creating a second serial landing stage.

### Tiered verification — HIGH vs LOW, and the gate is the floor

Every candidate is classified `HIGH` or `LOW` from its **real Git diff** (`bridge.risk_tier`),
never its self-reported `paths`. The tier decides only whether a candidate needs a cross-lineage
LLM verdict *on top of* the gate — it never decides whether the gate runs.

- **HIGH** — requires a genuine, named, different-lineage build-time verdict bound to the exact
  `head_sha`, exactly as before tiering existed. HIGH is any diff that touches main-writer/lander
  logic (`pipeline/bridge/integration.py`, `pipeline/bridge/gate_loop.py`, land paths), the gate
  itself (`scripts/merge-gate.sh`, `gates/`, reachability), auth/permissions/approvals,
  secrets/credentials, db migrations (`omniagentos/db/migrations/`), money/banking, schemas
  (`schema/`, `contracts/`, `*.schema.json`), prompts (`PROMPT-*`, `pipeline/prompts/`,
  `system-prompts/`), architecture (`ARCHI*`), or a **test-harness / build-config** file that
  DEFINES what "PASS" means (`conftest.py`, `pytest.ini`, `tox.ini`, `setup.cfg`, `pyproject.toml`).
  The `PROMPT-*`/`ARCHI*` basename match is **case-insensitive** (`prompt-foo.md`, `archi.md` are
  HIGH). **The hard-HIGH net is derived as a strict superset of the build-review policy's risky
  net** (`bridge.review_policy`) — its risky words (incl. `gate`, `policy`) and exact files are
  unioned in, so a path the old policy required a cross-lineage verdict for can never be
  attested-LOW. The one intended exception is the schema/contracts attestation carve-out below.
- **LOW** — a signed, receipt-verified merge-gate PASS on the candidate's **own tip** stands in
  for the LLM verdict, recorded as a synthetic verdict
  `{ "lineage": "mechanical-gate", "by": "merge-gate", "receipt": <sha>, "reviewed_sha": <tip> }`
  so the audit trail shows why the LLM verdict was waived. LOW is granted only when **every**
  changed path is bounded-mechanical: non-prompt docs (`*.md`), tests that live inside a `tests/`
  **directory** (a bare `test_*.py` basename ELSEWHERE — e.g. `scripts/test_deploy.py` — is NOT
  auto-mechanical; that convention is trivially forgeable), an additive-only schema field already
  covered by a schema-validation test, or a single small script with a passing execution-verified
  test. A test-harness/config file (above) is never LOW even inside `tests/`.

**The gate is the floor for BOTH tiers.** Nothing lands unverified. A LOW candidate whose gate
FAILED — or which has no signed PASS receipt on its tip — does **not** land; LOW only skips the
extra LLM verdict, never the gate. (The mandatory merge-gate still runs on the assembled train
regardless.) The tip receipt that authorizes a LOW waiver is read **fail-closed**: any shape that
cannot be positively read as a green run (a non-integer `rc`, an unrecognised `result`, a
`candidate_sha` that names a different tip) is treated as a fail. Authentic **signature/binding**
verification of the receipt is enforced by the EXISTING gate-evidence verifier
(`omniagentos.scheduler.gate_evidence verify-candidate`) at the mandatory gate floor — the tip
presence check is a cheap pre-check only, and a forged receipt that slips it is still refused by
the floor before merge.

**Conservative default — fail CLOSED.** Any unknown, ambiguous, or unreadable path is HIGH; an
unreadable diff is HIGH. There is no path that defaults to LOW. The two carve-outs that cannot be
proven from a path alone — additive-only schema fields and single small scripts — require an
explicit attestation on the envelope: OPTIONAL `additive_schema_paths` / `mechanical_script_paths`
string arrays. The lander intersects each attestation with the real diff before trusting it, so a
self-report can only apply to a path the candidate genuinely touched, an attestation can never
downgrade a hard-HIGH surface, and the mandatory gate re-runs the tests either way.

**Kill switch — `OMNIAGENTOS_TIERED_VERIFY`.** Tiering is OFF unless the environment variable is
exactly `1`. When OFF (unset or any other value), the classifier forces HIGH for every candidate,
so the lander runs its exact pre-tiering approval check and behaviour is unchanged. LOW landings
are impossible with the switch off.

**Create atomically:** write `<name>.tmp` **in the same directory**, `fsync`, then `rename()`.
A half-written JSON file read by another loop is what this prevents.

---

## 3. The envelope

Every artifact is one JSON object. Schema: `schema/envelope.schema.json`.

```jsonc
{
  "contract": "v1.1",
  "id": "sha256:9f2a…",         // canonical hash of `payload` — §7
  "kind": "candidate",           // inquiry | finding | proposal | candidate
  "title": "…",
  "created_at": "2026-08-07T11:33:27Z",   // RFC3339 UTC, always Z
  "priority": 2,                 // OPTIONAL, 0-3. 0=bottleneck 1=fix 2=normal(default) 3=background
  "producer": { "role": "reviewer", "actor": "…" },
  "base_sha": "70e82d4156c9…",  // 40-char FULL sha the work is bound to
  "paths": ["a/b.py"],          // proposals AND candidates: EVERY file touched
  "branch": "fix/thing-0807",   // candidates only
  "evidence": [
    { "claim": "…", "verified_by": "execution",
      "command": "pytest tests/x -q", "exit_code": 0 }
  ],
  "payload": { }                 // kind-specific; the ONLY thing hashed into `id`
}
```

**Required always:** `id`, `kind`, `title`, `created_at`, `producer`, `payload`.
**Additionally for `proposal`:** a **non-empty `paths`** (and, if `payload.lanes` is present,
a non-empty `paths` on every lane).
**Additionally for `candidate`:** full 40-char `base_sha`, full 40-char immutable `head_sha`,
`branch`, `paths`, and ≥1 `evidence` entry with `verified_by: "execution"`. The branch is only a
convenience ref: deletion is harmless, movement away from `head_sha` is a refusal.
**Optionally for `candidate`:** `additive_schema_paths` / `mechanical_script_paths` — string
arrays attesting the two narrow LOW-tier carve-outs (§1, *Tiered verification*). Both are ignored
unless the path is also in the real diff and tiering is on; neither can downgrade a hard-HIGH
surface.

**`priority` is OPTIONAL, an integer `0..3`** — `0` bottleneck, `1` fix, `2` normal (the
default), `3` background. **Absent is `2` everywhere** — a pre-existing envelope with no
`priority` field is valid and processes exactly as a `2` would; no restart or migration of a
live queue is required. Lower sorts sooner — see §6 for the selection order this drives.

**`paths` must be complete, but scheduling and risk use the real Git diff.** Declared paths remain
an admission assertion; they never authorize a review downgrade or a disjointness claim.

**Proposals are filed with `bridge/file_proposal.py`, never written by hand.** Invoke it by its
absolute path — `~/OmniAgentOS/pipeline/bridge/file_proposal.py`. It is the only writer for
`proposals/`: it computes the `id`, enforces the rules above plus the ones a schema
cannot express — paths that name real files (or files declared in `payload.new_paths` as ones the
plan creates), lane coverage that partitions the top-level list, and completeness checked against
a branch or PR diff where one exists — and writes atomically. **It refuses rather than writing a
degraded artifact.** Exit `0` written · `1` fixable, correct the named gap · `2`
could not run — including under a bare `python3` that lacks
`jsonschema`; if that happens, its stderr names a conforming interpreter (discovered from
`$VIRTUAL_ENV` / `$LOOP_WORKDIR`) to re-run under · `3` do-not-retry-this-input (the id is
already filed, or carries a live rejection/park). Do not assume the system `python3` on this
host has `jsonschema` installed — it does not, on every interpreter tested.

> *Why it is a tool and not a rule.* `paths` was optional in this document, optional in
> `schema/envelope.schema.json`, and unchecked by `bridge/validate_envelope.py` — required only by
> the Implementer, which refused on it. The absent field therefore read as a favourable value to
> every machine that looked at it. Measured 2026-08-08: 13 of 36 queued proposals carried an empty
> `paths`; 10 of the 14 `replan` refusals in `rejected/` were for that alone and one more for
> understating it — with the plans themselves sound. The lesson is the general one: **an
> abnormal condition must never be representable as a well-formed artifact.**

**`base_sha` must be the full 40 characters.** Abbreviations have collided here.

**`verified_by`** is `execution` (you ran it — `command` and `exit_code` required) or `reading`
(you read it). **A `reading` may not carry a blocker.**

Everything else — review verdicts, carrier tables, falsifiers, lane splits — is **optional** and
specified in `profile/omniagentos.schema.json`. Ignore it unless you want it.

---

## 4. Inquiries — the reverse edge

An **inquiry** says *"this area needs attention and I don't have the fix."* It is the only way
work flows **backwards**, from Reviewer or Implementer to Planner.

Anyone may write one: Reviewer, Implementer, or a human typing a file by hand. It is deliberately
cheap — a question with evidence, **not** a plan.

```jsonc
{
  "kind": "inquiry",
  "title": "Gate spends ~40% of wall-clock re-copying an unchanged tree",
  "producer": { "role": "reviewer", "actor": "…" },
  "payload": {
    "area": "merge-gate performance",
    "observation": "12 of 14 runs show scratch-setup > test time",
    "why_not_a_fix": "no idea whether the fix is caching, rsync flags, or a different scratch model",
    "evidence_refs": ["receipts/sha256:ab12…/timing.json"],
    "urgency": "normal"          // low | normal | high
  }
}
```

Required: `area`, `observation`, `why_not_a_fix`. That last field is the point of the artifact —
it forces the writer to say what they don't know, which is what makes it a research task instead
of a vague complaint.

**Planner treats `inquiries/` as a first-class input**, alongside its own scanning. An inquiry is
not a promise.

### Inquiry lifecycle — every inquiry must reach a terminal state

Without this, `inquiries/` grows forever and the same observation is re-raised every time a fresh
context notices it. **Planner owns closing them**, and holds `rejected/` write scope **for
`kind: "inquiry"` only** (the single exception to the Implementer's exclusivity):

| Outcome | Event | Effect |
|---|---|---|
| researched into a plan | `proposed` on a proposal whose `payload.answers_inquiry` is the inquiry's `id`; then `answered` on the inquiry | inquiry becomes terminal |
| not worth pursuing | `rejected` on the inquiry, with `reason`, `class`, and a **mandatory `expires_at`**, plus a `rejected/<id>.json` file | re-raising is dropped at source until the TTL expires |

Because an inquiry's `id` is the hash of its payload, a re-raise of the same observation produces
the same `id`. **Both outcomes leave a tombstone in `rejected/<id>.json`** — the answered one with
`class: "answered"` and a TTL — so the ordinary drop-at-source check (§7) catches a re-raise
without anyone consulting the ledger. Without the tombstone, an *answered* inquiry leaves only a
ledger event, the inquiry file is swept after 7 days, and the next context to notice the same
thing makes Planner research a question it has already answered. This is the whole reason the
reverse edge is safe to give to every role.

Retention (§10) deletes terminal inquiries. An inquiry with no terminal event after 30 days is a
**bug in Planner**, not a backlog — surface it, don't let it accumulate.

A Reviewer Loop's primary duty is still exact repairs — find, fix, submit. Inquiries are the
**pressure-release valve** for what it notices but cannot or should not fix in the moment: a
recurring pattern, an architectural smell, a slow step, a fix it made that it suspects has
siblings elsewhere. Emitting one costs an iteration nothing and stops the observation being lost.

---

## 4b. Research — investigate once, not once per context

`rejected/` stops a repeated **idea**. Nothing stopped a repeated **investigation** — and
re-researching "how do others solve X" is expensive even when the resulting proposals differ.
Worse, research that never became a proposal used to vanish entirely: the reasoning died with the
context window that produced it.

**Research does not live in the queue.** It lives in **`~/.omniagentos/ops/Research/`** — durable,
Drive-mirrored, readable from every project. The queue is git-ignored, host-local and ephemeral;
research is the opposite of all three, and putting knowledge somewhere that dies with the machine
is how it stops compounding.

**Every investigation writes `~/.omniagentos/ops/Research/<folder>/<slug>.md`, whether or not it becomes a
proposal.**
Markdown with frontmatter, because research is prose and a human will read it:

```markdown
---
topic: merge-gate scratch setup cost
question: is the 40% wall-clock in scratch setup fixable by caching, rsync flags, or a redesign?
investigated: 2026-08-07
by: planner
stale_after: 2026-11-07        # findings about a moving target expire
verdict: caching is the wrong layer; the cost is the 5,346-file copy itself
became_proposal: sha256:…      # or: null, with a reason
sources: [receipts/sha256:ab12…/timing.json, "measured: 12 of 14 runs"]
---

What was measured, what it showed, and what was ruled out — including the dead ends,
which are the part nobody writes down and everybody re-walks.
```

**Before starting any investigation, grep `research/` for the topic.** A hit that is still fresh
means the question is answered — read it instead. A hit past `stale_after` means re-investigate,
but start from what the previous pass ruled out rather than from zero.

**Record the dead ends.** "We tried caching; it does not help because the cost is the copy, not the
compute" is worth more than the conclusion alone, because it is the part the next investigator
would otherwise spend a day rediscovering.

### `research/` is the one directory that may be reorganised

Every other directory here is append-only and immutable, because they are **queues** — a mutable
queue is a race. `research/` is a **library**, and a library that cannot be reshelved becomes a
pile. So Planner may, inside `research/` only:

- **create subfolders** — group by domain as the corpus grows (`research/gates/`,
  `research/routing/`, `research/cx/`). Structure should follow the material, not precede it.
- **move and rename** files as better groupings become obvious
- **merge overlapping files** into one, keeping every source reference and both dead-end sections
- **edit** a file to correct or extend it — this is knowledge, not an artifact of record

Two rules keep it honest:

1. **Never delete a finding, only supersede it.** A merged file keeps `supersedes: [old-slug, …]`
   in its frontmatter, and the old file becomes a one-line stub pointing at the new one. Anything
   that cited the old slug must still resolve.
2. **Merge on contact.** If you notice two files covering one question while doing something else,
   merge them then. Incremental tidying by whoever touches it is what stops a library needing a
   librarian.

**When it stops being incremental, say so.** If a merge takes a whole iteration, or you find three
files on one topic, or you cannot answer "has this been investigated?" without reading everything
— **raise an inquiry proposing a dedicated research-curation role.** That is the evidence that the
corpus outgrew merge-on-contact. Do not propose it before then: organising three documents is not
a job, and a curator with nothing to curate invents structure the material does not need.

---

## 5. The ledger

`var/loopqueue/ledger.jsonl` — one JSON object per line, append-only, **never rewritten**. Schema:
`schema/ledger-event.schema.json`.

```json
{"ts":"2026-08-07T11:33:27Z","role":"reviewer","event":"submitted","id":"sha256:9f2a…","detail":{}}
```

| event | meaning |
|---|---|
| `found` | a finding was recorded — breakage observed, not yet classified |
| `inquired` | an inquiry was raised |
| `proposed` | Planner wrote a proposal |
| `claimed` / `released` / `claim_expired` | claim lifecycle (§6) |
| `submitted` | work offered to Implementer |
| `admitted` | Implementer accepted it into the queue |
| `gated` | gate ran — `detail.result` = `pass`/`fail`, `detail.receipt` |
| `merged` | landed — `detail.merge_sha` |
| `completed` | **terminal**, verified-and-applied work that legitimately has **no `merge_sha`** — out-of-repo / host-ops changes that never touched this repo. Unlike `merged` it carries no `merge_sha`; unlike `rejected` it is not a refusal (no `class`/`expires_at`). **`detail.reason`** is mandatory (schema-enforced) — name what was applied and how it was verified |
| `rejected` | refused — `detail.reason`, `detail.class`, `detail.expires_at` |
| `parked` | needs a human — `detail.reason` |
| `instrument_error` | tooling/host failed; says **nothing** about the code |

### Durability rules — these are load-bearing

- **Every append goes through `bridge/ledger_write.py`.** Producers pass one JSON object; the
  transport owns compact serialization, the trailing newline, locking, checked writes and
  durability. Direct `open(..., "a")`, `os.write`, shell redirection and hand-written append
  snippets are forbidden for this ledger.
- **The stable lock is `locks/ledger.lock`.** Writers acquire its exclusive `flock` *before*
  opening `ledger.jsonl`, verify the existing tail is empty or newline-terminated, then use an
  `O_APPEND` descriptor and a checked write-all loop. Maintenance that can rename or replace the
  ledger must use this same lock-before-open order.
- The often-quoted 4 KB bound is `PIPE_BUF`, which governs **pipes, not regular files**. Do not
  rely on it either way. The lock serializes even when a short write requires multiple syscalls;
  keep lines small and put bulk output in `receipts/<id>/` by reference.
- **`fsync` before treating an event as durable.** Without it, "survives a crash" is not true.
- The CLI returns 0 only after durable success and explicit exit 2 only when this invocation added
  no bytes. **Every other outcome is indeterminate** — exit 3, a signal, timeout, lost process, or
  missing result — because termination can occur after `fsync` but before success is reported.
  Never retry an indeterminate outcome without reconciling the ledger first.
- **Readers must tolerate a torn tail.** A crash mid-write can leave a final line that is
  unterminated or unparseable. Skip it; never abort the read. (`jq` over the raw file will fail —
  drop the last line if it lacks a trailing newline.)

The ledger is the **history**. `state/queue.json` is a **derived cache**, rebuildable by replay.
**If they disagree, the ledger wins.**

> **The ledger is a log, not an authority — and append-only is a convention here, not a mechanism.**
> Nothing prevents a process rewriting it; every role can append, and no line is signed. So it is
> the right place to *reconstruct what happened* and the wrong place to *decide what is permitted*.
> Never gate an action on a ledger line alone: for anything that matters, re-verify against the
> system that actually owns the fact (git for what merged, GitHub for what a human approved, the
> filesystem for who holds a claim). `O_EXCL` is the only arbiter in this contract because the
> kernel enforces it; treat every other record as evidence, not permission.

---

## 6. Claims — atomic, expiring, stealable

Claimable items are `findings/`, `proposals/`, and `inquiries/`. (Candidates are not claimed —
the Implementer is a singleton and simply takes them.) **Implementers must claim a proposal before
building it**, exactly as below; two unclaimed Implementers will otherwise build the same plan.

**Selection order — which item to claim next.** `state/queue.json`'s `items` list (the mechanical
publisher, `bridge/publish_queue.py`, rebuilds it from the ledger + on-disk envelopes every tick;
see §5) is ordered by **priority ascending, then age**, so a bottleneck (`0`) or fix (`1`) item is
offered before a normal (`2`) or background (`3`) one. **This is ONLY a selection/presentation
order — it never touches the ownership arbiter below.** Ownership is decided solely by the
successful `O_EXCL` marker create, exactly as documented in this section; two implementers racing
the SAME item still resolve the same way regardless of where that item sits in the list.

**Anti-starvation aging**, so a stream of fresh high-priority arrivals can never starve an old
normal/background item forever: each item's *effective* priority is
`max(0, priority - floor(age_seconds / 900))` — it drops by one every 900s (15 min) it has waited,
floored at `0` (never better than a fresh bottleneck). The list sorts on `(effective_priority,
id)`. An item with no `priority` field ages from `2`, identically to one that states it explicitly.

Never claim by editing an artifact. Claim by **atomically creating a marker**:

```python
fd = os.open(f"claims/{id}.claim", os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
os.write(fd, body); os.fsync(fd); os.close(fd)     # immediately — see the empty-marker rule
```

> **The successful `O_EXCL` create is the ONLY arbiter of ownership.** Ledger events are a record,
> not arbitration. Do not infer ownership from having appended an event first — two processes can
> both append and only one can hold the marker.

Body: `{"actor":…, "at":…, "expires_at":…}`. Then append a `claimed` ledger event.

**Expiry.** Set `expires_at` from your realistic worst case *for that item*, not a global default.
A long enumeration can legitimately exceed an hour, and a claim that expires while you are still
working causes exactly the duplicate work claims exist to prevent.

**The empty-marker rule.** `O_EXCL` creates the file *empty*; the body lands a moment later. A
crash in that window would otherwise leave a marker with no `expires_at` that nothing can ever
expire — permanently blocking the item. So:

> An empty or unparseable marker is treated as **held** until 10 minutes past its mtime, and as
> **stealable** after that. Never delete an unparseable marker younger than the grace period.

**Renewal** — read the marker, **verify `actor` is you** (if it is not, you were stolen: abort and
drop the work), then write `<id>.claim.tmp` and `rename()` over it. Never open-truncate-write: a
torn read looks like an empty marker, and renewing a claim you no longer hold overwrites the new
owner's marker and puts two loops on one item.

**Release** on finish or abort: delete the marker, append `released`.

**Stealing an expired claim** — this recovers a crashed loop's work, and without it an orphan claim
blocks its item forever. **Never blindly `unlink()` a marker you did not create:**

1. `stat()` the marker and remember `(st_ino, st_mtime)`.
2. Confirm it is expired (or empty and past the grace period).
3. `stat()` again and **compare against step 1 — if it changed, someone renewed or already stole
   it: abort.**
4. Append `claim_expired`, `unlink()`, then create with `O_EXCL`.
5. **Re-read your own marker and verify `actor` is you before starting work.**

Steps 3 and 5 exist because these actors are LLM loops: seconds can pass between reading a marker
and acting on it. Without step 3, a second stealer acting on a stale read will `unlink()` the
*live* marker the first stealer just created — both creates succeed, both loops believe they own
the item, and both do the work. Step 5 catches the residual single-syscall window. The same stale
read can destroy a legitimate **renewal**, which is why renewal also re-verifies (above).

---

## 7. Identity and idempotence

`id` = `"sha256:" + hex(sha256(jcs(payload)))`, where `jcs` is **JSON Canonicalization Scheme,
RFC 8785**. Use a JCS library — do not hand-roll "sorted keys, no whitespace". Float formatting,
unicode normalization and escaping all differ between naive implementations, and two loops that
canonicalize differently produce different ids for the same payload, which **silently defeats
deduplication**.

The point: **the same idea produced twice gets the same `id`**, so `rejected/` recognises it
without anyone comparing prose.

**Before doing any work, check `rejected/<id>.json`.** If it exists and `expires_at` is in the
future, drop the item **at source**. A rejection carries `reason`, `class`, and a **mandatory**
`expires_at` — a rejection without a TTL is a permanent ban, and nothing here should be permanent.

After expiry an idea may be **re-argued** with new evidence and an explicit `supersedes` pointing
at the old `id`. It may not be resubmitted unchanged.

> **Known limit, stated plainly:** because `id` hashes `payload`, any real change to the work
> produces a new `id` and a fresh attempt budget. This dedup stops *identical* resubmission, not a
> determined loop rewording the same bad idea. The `(id, base_sha)` attempt counter and the
> 3-attempt park are what bound that case. Implementer validates `supersedes` on admission.

---

## 8. Status, exit codes, and retries

Artifacts are immutable, so **status is derived from the ledger**, never stored:

```
inquiry:   inquired ──► answered      (a proposal now carries answers_inquiry)
                    └─► rejected      (both leave a tombstone in rejected/)

work:      proposed / finding ──► claimed ──► submitted ──► admitted ──► merged
                                     │            │             │      └► completed
                                     └────────────┴─► rejected  ┘
                                                  └─► parked ──► unparked ──► (claimable again)
```

These are **pipeline stages across different artifacts**, not states of one file — an Implementer
claims a *proposal* and submits a *candidate*, which is a new `id`. Parking is not rejection: it
means a human decision is owed, and only a human ends it.

`completed` is the terminal state for **verified-and-applied work that has no `merge_sha`** —
out-of-repo or host-ops changes that never merge into this repo. It is terminal exactly like
`merged` and `rejected`: the id reaches terminal status, drops out of the queue, no longer
occupies a WIP slot, and is swept by the same 7-day terminal retention (§10). `merged` cannot be
used (its schema requires a `merge_sha` that cannot exist) and `rejected` is wrong (the work was
applied, not refused).

Two **separate** interfaces use exit codes. Do not conflate them.

**(a) Between a loop and its own tools** (test runner, linter, build):

| code | meaning | response |
|---|---|---|
| `0` | pass | continue |
| `1` | candidate defect | fix and resubmit |
| `2` | **could not run** | the instrument could not evaluate this input (missing dep, dirty/moved workspace, unreadable input). Fix the **mechanics**, then re-run the **same** input — it is neither a candidate defect nor a do-not-retry |

> **Ratified 2026-08-09 (operator Ruling #4): exit `2` = COULD NOT RUN.** It means the
> instrument/gate could not evaluate the input at all — *distinct from* a candidate defect (`1`,
> the code is wrong) *and distinct from* do-not-retry (a genuine dead end). Under the earlier
> wording exit `2` read as "do not retry this input", which wrongly cast every mechanics fault
> (dirty workspace, moved base, missing dep — measured as 64 of 90 gate refusals) as a permanent
> verdict on the candidate. A could-not-run is retryable **once the mechanics are fixed**; what you
> must not do is re-run the *unchanged* input, because that buys the same non-result. The
> do-not-retry concept did not disappear — for a loop's own tools it lives in the **retry-bounds**
> rule below (a producer counting its own `rejected` events), not on an exit code; a **producer
> writer** (`file_proposal.py` / `file_inquiry.py`) that needs a fourth, genuine dead-end code
> carries it as exit `3` (the id is already filed / carries a live rejection or park).

**(b) Between the Implementer and a submitter:** the outcome is the ledger event —
`merged`, `rejected` (with `class`), or `parked`. the Implementer does not communicate by exit code.

**Retry bounds, and who counts.** Before resubmitting, a producer counts `rejected` events for its
own `(id, base_sha)` in the ledger **since the most recent `unparked` event for that `id`** (all of
them, if there is none). Two → change the input or the action, never repeat the pair. Three →
write `parked/<id>.json`, alert **once**, stop. **The producer enforces this**; the Implementer does
not track attempts on anyone's behalf.

The "since the last `unparked`" clause is what makes un-parking work at all: without it a producer
recounts the same three rejections the moment a human releases the item and re-parks instantly.

Rule (a)'s exit-2 case is aimed at a producer's **own tooling** — a gate script, a test harness,
a lint run that **could not evaluate** the same input twice (a dirty or moved workspace, a missing
dependency). the Implementer's refusals are the `rejected` events above.

**Terminal errors — quota, auth, suspension, billing — are terminal.** Max 5 attempts, then park.
Never blind-retry: a sibling system fired 3,951 launches at a terminal provider error, cost $600,
and completed zero work.

---

## 9. The governor

A **separate, non-agentic process** owns `state/budget.json`: it meters spend, resets
`spent_today_usd` at local midnight, and is the only writer. Loops read it and never write it.

### Two limit classes, not one number

**Most of the fleet runs on subscriptions, where the marginal cost of a call is ~zero.** Summing
dollars would report a confident number that misses the constraint that actually binds. So the
governor reads two independent classes and stops when **either** binds:

```jsonc
{
  // 1. METERED — real dollars. Kimi API org, Fireworks, OpenRouter, PiAPI.
  //    NOT Claude/Codex/Grok/Gemini: those are subscriptions.
  // Keyed by the canonical loop role, and by ALL THREE of them — a key that
  // names no live loop is a limit nothing can ever bind, and a live loop with
  // no key is a limit nobody is watching.
  "metered_usd": {
    "planner":     { "daily": 50,  "spent_today": null },
    "reviewer":    { "daily": 100, "spent_today": null },  // null = UNKNOWN, and UNKNOWN stops
    "implementer": { "daily": 100, "spent_today": null }
  },

  // 2. SUBSCRIPTION — quota, not cash. Keyed by ACCOUNT, because that is what
  //    the quota actually belongs to. Loops sharing an account share a ceiling.
  "subscription": {
    "accounts": {
      "claude-planner":     { "provider": "claude", "limit": "session", "slot": 1 },
      "claude-reviewer":    { "provider": "claude", "limit": "session", "slot": 2 },
      "claude-implementer": { "provider": "claude", "limit": "session", "slot": 3 },
      "codex-a":            { "provider": "codex",  "window_pct_max": 85, "window_pct": null }
    }
  },
  // One account per loop — no two loops contend for one ceiling. EXACTLY the
  // canonical loop roles as keys: `executor` and `integration` were removed at
  // the rename epoch because no such loop exists, and a mapping for a loop that
  // does not run is a ceiling that can never bind. `codex-a` stays an ACCOUNT
  // (the governor keeps its window_pct fresh, and the gate reads it) but is no
  // loop's seat — all three loops run `claude -p`.
  "loop_accounts": {
    "planner": "claude-planner",
    "reviewer": "claude-reviewer",
    "implementer": "claude-implementer"
  },

  "disk_free_gb_min": 20,
  "load_avg_1m_max": 12,             // host performance-core count
  "wip_cap": 4,                      // the Implementer's in-flight limit — backpressure
  "alert": { "channel": "file", "target": "var/loopqueue/ALERTS.md" }
}
```

**Claude has no queryable balance.** A session limit surfaces only as an error, and it is a
**routing event, not a stop**: fail over to another lineage (`ROUTING.md`), record the
substitution, keep working. Stop only when *every* lineage is limited. Treating one provider's
limit as a global halt throws away the whole point of multi-lineage routing.

### Quota belongs to an account, and this bounds your parallelism

Two consequences, and the second is the one that matters for throughput:

1. **Loops sharing an account share one ceiling.** Three loops on one account do not get three
   quotas — they contend for one, and the first to exhaust it limits the others. So the governor
   must aggregate usage **per account**, not per loop: a per-loop cap on a shared account is not a
   cap at all. *The default here gives each loop its own subscription precisely to avoid this; if
   you ever map two loops to one account, that line is where your parallelism actually stops.*
2. **Adding loops on a saturated account adds no throughput** — it adds contention and queue depth.
   Adding an *account* adds throughput. This is the quota analogue of Implementer being the
   review ceiling (`MISSION.md`): past the binding constraint, more producers make things slower.
   **When a fleet stops scaling, check whether you added workers or capacity.**

**Another operator's accounts are invisible and irrelevant.** Where several people run loops
against the same repo, each runs its own governor against its own accounts, on its own host. Never
read, infer, or reason about someone else's quota: you cannot see it, a guess is always wrong, and
their limit must never stop your loops (or vice versa). The shared artifact is the **queue**, never
the budget — which is why `var/loopqueue/` is git-ignored and `budget.json` never leaves its host.

**Measured caveat, so nobody trusts the dollar figure prematurely:** on this estate every row in
the metered ledger is a `$5.00-unmeasured` flat estimate from a single shim, covering two minor
providers and none of the actual volume. A sum over it is not spend — it is guesswork wearing a
number. That is why `spent_today` starts `null`: an honest unknown beats a confident wrong total,
and §9's fail-closed rule turns unknown into a stop rather than headroom.

Check **before every iteration and after each one** — the after-check is what catches an
`ENOSPC` or a load spike that arrived mid-iteration. If any limit is breached: sleep, re-check.
**Not optional.**

### Null metered spend is NOT a stop — the ruling that matters most

**A `null` in `metered_usd.spent_today` does not halt anything, and must never be read that way.**
Almost every seat here runs on a **subscription**, where the marginal cost of a call is ~zero and
there is no metered spend to bound. A counter reading null because nothing metered was spent is
*correct*, not broken — and halting on it would be a fleet-wide false stop guarding a number that
never binds.

**Fail closed only where the counter actually bounds something real:**

| condition | meaning | action |
|---|---|---|
| no metered provider used this period | null is correct — nothing to meter | **continue** |
| metered call made, counter still `null`/`0.00` | the meter is broken | **stop**, alert once |
| metered spend ≥ ceiling | the cap binds | stop |

The metered providers are the ones that bill per call: the Kimi API org, Fireworks, OpenRouter,
PiAPI. Claude, Codex, Grok and Gemini seats are **subscriptions** — their constraint is quota, not
cash, and quota is handled by rotating the account (`ROUTING.md`), never by halting.

The $600 incident this rule exists for was a *metered* org: 3,951 launches at a terminal error. The
safety property is real, and it belongs on the path that can actually spend money.

### The governor fails CLOSED where a counter is load-bearing

> **Treat any of these as `spend = UNKNOWN` and STOP, never as budget remaining:**
> - `budget.json` absent, unparseable, or missing the key for your role
> - `spent_today_usd` older than one iteration (a counter nobody updates is not a counter)
> - `spent_today_usd` reading exactly `0.00` **after this loop has demonstrably made a paid call**
>
> Log `instrument-error`, alert once, and do not resume until a human confirms the meter works.

This is the single most dangerous favourable absence in the system: a spend ceiling only protects
you if the number it reads is real, and a broken meter reads as *infinite headroom* — the exact
direction that produced 3,951 launches at a terminal error for $600.

It is not hypothetical. On this estate, verified 2026-08-07: `adapters/spend_db.py:13` hardcodes an
absolute path that exists on exactly one machine — off it, SQLite **silently creates an empty
database** and every query returns zero; `scheduler/routines_tick.py` writes a literal
`"cost_usd": 0.0` at four separate sites, so every routine tick books zero cost forever; and
unknown provider costs settle at `$0.00` rather than refusing. Three independent defects, all
failing in the same direction, all invisible because zero looks like thrift.

**A meter that has never once reported a non-zero number is broken, not thrifty.**

**Alerting** appends one entry to `alert.target`. One alert per parked item, ever. A loop that
alerts repeatedly trains its operator to ignore it.

### Parking and un-parking

Parking is **not** rejection — it means a human decision is required, so it needs its own marker:

- To park: write `parked/<id>.json` (`{"id", "reason", "at", "needs"}`), append `parked` with
  `detail.reason` and `detail.alerted: true`, append one line to the alert target, and stop.
  **Every producer role may write `parked/` for its own items** — parking is how a producer stops,
  so it must be able to record one.
- Producers **skip any item with a file in `parked/`** — the same drop-at-source check as
  `rejected/`.
- **Un-parking requires a positively verified human approval — never the absence of a marker.**
  An `unparked` event is appended only by an authenticated carrier (for a code candidate: the
  GitHub bridge, acting on a real PR approval, recording `pr`, `approved_by`, and a `reviewed_sha`
  equal to the branch tip). The event is also what resets the §8 attempt counter, which is
  ledger-derived.

  > **The janitor must NEVER mint an `unparked` from a deleted marker.** Every process on the host
  > can delete a file, so "marker gone ⇒ a human approved" makes the boundary a `rm`. A marker that
  > vanished without a matching authenticated approval is a **suspicious state: alert, do not
  > release.** For a parked **candidate** — never claimed — the Implementer re-scans only after the
  > approval event; for claimable kinds the item becomes claimable again at that point.
- **Parked artifacts are exempt from the 7-day retention sweep** (§10). A human taking more than a
  week to decide is the normal case for a park, not an edge case, and deleting the artifact
  underneath them destroys the work.
- **No loop may un-park itself**, including by deciding the blocking condition looks cleared. If a
  loop believes a park is stale, it raises an **inquiry** — it does not delete the marker.

---

## 10. Retention

Unbounded growth is a 24/7 failure mode. Implementer janitors, at most daily:

| What | Policy |
|---|---|
| artifacts with a terminal ledger event | delete after 7 days — the ledger retains the history |
| **parked artifacts** | **exempt while `parked/<id>.json` exists.** Delete 7 days after the marker is removed, or after 90 days, whichever comes first. Deleting the artifact under a human who is still deciding destroys the work |
| inquiries with no terminal event after 30 days | **not** a backlog — a bug in Planner. The janitor appends one line to the alert target |
| `rejected/` past `expires_at` | inert immediately; delete after 30 days |
| `receipts/` | keep 30 days, then keep only those referenced by a `merged` event |
| `claims/` | delete expired markers on sight |
| `ledger.jsonl` | at 100 MB alert and refuse rollover; locked, collision-safe rollover remains disabled until every replay consumer reads archives |

Queue rebuilds currently replay only the **current** ledger. Archive rollover must not be enabled
until replay is archive-aware and the rename participates in `locks/ledger.lock`.

---

## 11. What Implementer guarantees

- Every submitted candidate gets **exactly one** terminal event: `merged`, `completed`, or
  `rejected`. (`completed` is for verified-and-applied out-of-repo / host-ops work that has no
  `merge_sha` — terminal like the other two, and swept by the same 7-day retention.)
  **`parked` is a suspension, not a terminal state** — an item may be parked and un-parked any
  number of times before reaching a terminal event. (A candidate awaiting human approval is parked
  on arrival and merged later; treating `parked` as terminal would break the guarantee on that
  entirely normal path, and make queue rebuilds choke on parked-then-merged.) Queue replay must
  handle the sequence `parked → unparked → gated → merged`.
- Rejections always carry machine-readable `reason`, `class`, and `expires_at`.
- `base_sha` is honoured. If `main` moved, the Implementer re-gates or rejects with
  `class: "stale-base"` — **it never silently lands work against a different base.** Receipts bind
  `(candidate_sha, merge_base_sha, command)`, where `candidate_sha` is the branch tip being graded
  and `merge_base_sha` is its merge base with `main`. Any commit on `main` mid-gate invalidates
  them.
- Receipts are durable and referenced by path from the ledger.

**What it expects:** complete `paths`, a full `base_sha`, ≥1 `verified_by: execution` evidence
entry, and that you never touch `main`, `claims/` you don't hold, or another role's directory.

---

## 12. Versioning

This is **v1.1**. Envelopes carry `"contract"`. Additive fields are backward-compatible and may be
ignored — both schemas set `additionalProperties: true` so a v1.2 field never fails v1.1
validation. Removing or repurposing a field is a version bump.
