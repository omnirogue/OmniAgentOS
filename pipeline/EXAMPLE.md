# Worked example — one bug, end to end

Every file, every ledger line, in order. If the contract is ambiguous anywhere, this is where you
find out.

Two traces: a **repair** (the common path) and an **inquiry** (the reverse edge).

> **On the ids below.** The three **artifacts** carry real 64-hex ids and validate against
> `schema/envelope.schema.json` as printed — copy them. The **ledger lines** abbreviate ids to
> `sha256:3f9c…` purely for readability; a real ledger line carries the full 64 characters. If you
> validate this file mechanically, expect the abbreviated lines to fail the `id` pattern — that is
> the abbreviation, not the schema.

---

## Trace 1 — a bug, from observation to merged

### 1. CI reports a failure. The bridge writes a finding.

`var/loopqueue/findings/sha256:3f9c…json`

```json
{
  "contract": "v1.1",
  "id": "sha256:3f9c2a71b0e4d8a5c6f1938b2e7d40c5a9184f6b3d2e0c7a5b8f1d4e9c3a6b2f",
  "kind": "finding",
  "title": "retry_after parsed as float; NaN passes the bounds check",
  "created_at": "2026-08-07T09:14:02Z",
  "producer": { "role": "external", "actor": "ci-bridge" },
  "payload": {
    "symptom": "tests/api/test_retry.py::test_backoff_bounds fails on CI, passes locally",
    "source": "failing-test",
    "source_ref": "https://github.com/org/repo/actions/runs/00000000000",
    "repro": "pytest tests/api/test_retry.py -q"
  }
}
```

```json
{"ts":"2026-08-07T09:14:02Z","role":"external","event":"found","id":"sha256:3f9c…","actor":"ci-bridge"}
```

> `class` is absent — deliberately. The bridge saw a red test; it cannot know whether that's a code
> defect or a broken runner. **Whoever picks it up classifies it.**

### 2. Repair claims it — atomically

```python
fd = os.open("var/loopqueue/claims/sha256:3f9c….claim", os.O_CREAT|os.O_EXCL|os.O_WRONLY, 0o644)
os.write(fd, body); os.fsync(fd); os.close(fd)      # immediately: an empty marker is ambiguous
```

`var/loopqueue/claims/sha256:3f9c….claim`

```json
{ "actor": "repair-1", "at": "2026-08-07T09:15:10Z", "expires_at": "2026-08-07T11:15:10Z" }
```

```json
{"ts":"2026-08-07T09:15:11Z","role":"reviewer","event":"claimed","id":"sha256:3f9c…","actor":"repair-1"}
```

> Two hours, not a default — this needs a carrier sweep. A claim that expires mid-work causes
> exactly the duplicate work claims exist to prevent. If it runs long, **renew**; don't let it lapse.

### 3. Classify before fixing

Reproduced locally at the CI commit: fails. Reverting only the product change: passes.
→ **`candidate-defect`.** Had it only failed on CI, the next check is the runner — 64 of 90
refusals here were instrument errors.

### 4. Enumerate the carriers before writing the fix

The value being protected is *"a retry delay must be a finite, non-negative number."* Every site
that parses or validates one:

| site | ruling |
|---|---|
| `api/retry.py:88` | **reached** — the reported site |
| `api/retry.py:141` | **reached** — sibling parser, same bug, different caller |
| `swarm/backoff.py:52` | **reached** — copy of the same logic |
| `db/pool.py:203` | not-applicable — takes an int from config, never parses a header |

Three sites, not one. That is normal here, not unlucky.

### 5. Red first

```
$ git stash && pytest tests/api/test_retry.py -q
3 failed                    # the new tests fail against base_sha
$ git stash pop && pytest tests/api/test_retry.py -q
41 passed
```

> The test must **fail against `base_sha`**. A test that passes before and after pins nothing —
> 11 of 15 prior rejections here were tests incapable of catching their claimed defect.

### 6. Submit

`var/loopqueue/candidates/sha256:8b1d…json`

```json
{
  "contract": "v1.1",
  "id": "sha256:8b1d5e04c7a2f9836b1e0d4a7c5f2938e6b0d1a4c8f3e7b2d5a9c0f6e3b8d1a4",
  "kind": "candidate",
  "title": "reject non-finite retry_after at all three parse sites",
  "created_at": "2026-08-07T10:02:44Z",
  "producer": { "role": "reviewer", "actor": "repair-1", "lineage": "anthropic" },
  "base_sha": "67a68e27073486e37e8d202c67536cedfd67aefd",
  "branch": "fix/retry-nan-0807",
  "paths": ["api/retry.py", "swarm/backoff.py", "tests/api/test_retry.py"],
  "evidence": [
    { "claim": "3 new tests fail against base_sha",
      "verified_by": "execution", "command": "git stash && pytest tests/api/test_retry.py -q",
      "exit_code": 1, "result": "3 failed" },
    { "claim": "41 pass with the fix",
      "verified_by": "execution", "command": "pytest tests/api/test_retry.py -q",
      "exit_code": 0, "result": "41 passed",
      "receipt": "receipts/sha256:8b1d…/pytest.txt" }
  ],
  "payload": {
    "fixes": "NaN passed the bounds check because every comparison with NaN is false in both directions, so `0 <= x <= 300` admitted it. Now rejects non-finite explicitly at all three parse sites.",
    "resolves": "sha256:3f9c2a71b0e4d8a5c6f1938b2e7d40c5a9184f6b3d2e0c7a5b8f1d4e9c3a6b2f",
    "carrier_enumeration": [
      { "site": "api/retry.py:88",    "ruling": "reached" },
      { "site": "api/retry.py:141",   "ruling": "reached" },
      { "site": "swarm/backoff.py:52","ruling": "reached" },
      { "site": "db/pool.py:203",     "ruling": "not-applicable — int from config, never parses a header" }
    ]
  }
}
```

```json
{"ts":"2026-08-07T10:02:44Z","role":"reviewer","event":"submitted","id":"sha256:8b1d…","base_sha":"67a68e27073486e37e8d202c67536cedfd67aefd"}
{"ts":"2026-08-07T10:02:45Z","role":"reviewer","event":"released","id":"sha256:3f9c…","actor":"repair-1"}
```

> Note `paths` lists the **test file too**. Integration schedules on `paths`; omitting a file it
> touches produces a wrong schedule, not a slow one.

Then Repair **stops**. It does not follow up.

### 7. Integration admits, gates, lands

```json
{"ts":"2026-08-07T10:03:01Z","role":"implementer","event":"admitted","id":"sha256:8b1d…"}
{"ts":"2026-08-07T10:11:58Z","role":"implementer","event":"gated","id":"sha256:8b1d…","detail":{"result":"pass","receipt":"receipts/sha256:8b1d…/merge-gate.json","duration_s":527}}
{"ts":"2026-08-07T10:12:20Z","role":"implementer","event":"merged","id":"sha256:8b1d…","detail":{"merge_sha":"c383977d3e044611f660a789f0787bcb2e2c59f2"}}
```

Total: 6 files, 7 ledger lines, one terminal event. Seven days later the janitor deletes the
artifacts; the ledger keeps the history forever.

---

### What it looks like when it's refused

`var/loopqueue/rejected/sha256:8b1d….json`

```json
{
  "id": "sha256:8b1d…",
  "kind": "candidate",
  "reason": "reachability: `parse_retry_after` has no production caller. If this is a delegating wrapper, exempt it — but the exemption must land on main FIRST as its own chore(gates): commit, because the gate reads devtasks/REACHABILITY-EXEMPT.txt from the checkout it RUNS IN, not from your branch.",
  "class": "instrument-error",
  "at": "2026-08-07T10:11:58Z",
  "expires_at": "2026-08-14T10:11:58Z",
  "by": "implementer",
  "receipt": "receipts/sha256:8b1d…/merge-gate.json"
}
```

> **The reason names its own remedy.** "Reachability failed" sends someone hunting; this ends it in
> one round. One symbol here drew **28 identical refusals** — about 4.5 hours — because the fix kept
> being written where the gate does not read.

Repair now counts `rejected` events for `(sha256:8b1d…, e2ab32bf…)` in the ledger. **Two → change
the input or the action.** Three → write `parked/<id>.json`, alert once, stop.

---

## Trace 2 — an inquiry (the reverse edge)

Repair notices the gate is slow. It has no idea what the fix is, and that is exactly when to raise
an inquiry rather than widen the repair.

`var/loopqueue/inquiries/sha256:c4e7…json`

```json
{
  "contract": "v1.1",
  "id": "sha256:c4e7a913f5b28d06c1e4a7b930d582f6c0a4e8b1d7f3c9a2e5b0d8f4a1c6e3b9",
  "kind": "inquiry",
  "title": "Gate spends ~40% of wall-clock re-copying an unchanged tree",
  "created_at": "2026-08-07T10:20:00Z",
  "producer": { "role": "reviewer", "actor": "repair-1" },
  "payload": {
    "area": "merge-gate performance",
    "observation": "12 of 14 runs spend more wall-clock in scratch setup than running tests",
    "why_not_a_fix": "I don't know whether the answer is caching, rsync flags, or a different scratch model — it needs a design decision, not a patch",
    "evidence_refs": ["receipts/sha256:8b1d…/merge-gate.json"],
    "urgency": "normal"
  }
}
```

```json
{"ts":"2026-08-07T10:20:00Z","role":"reviewer","event":"inquired","id":"sha256:c4e7…"}
```

Repair returns to its queue immediately. It has spent one iteration, not a lane.

**Planning claims it, researches, and closes it — one of two ways:**

*Researched into a plan:*
```json
{"ts":"2026-08-07T14:02:00Z","role":"planner","event":"proposed","id":"sha256:d5f8…"}
{"ts":"2026-08-07T14:02:01Z","role":"planner","event":"answered","id":"sha256:c4e7…"}
```
The proposal carries `"answers_inquiry": "sha256:c4e7…"`.

*Or not worth pursuing:*
```json
{"ts":"2026-08-07T14:02:00Z","role":"planner","event":"rejected","id":"sha256:c4e7…","detail":{"reason":"measured: setup is 90s of a 527s gate, ~17%, not 40%. The estimate came from one contended run.","class":"candidate-defect","expires_at":"2026-09-07T14:02:00Z"}}
```

**Both write a tombstone to `rejected/`** (the answered one with `class: "answered"`). That is what
makes the reverse edge safe to hand to everyone: when a fresh Repair context notices the same
slowness next week, it computes the same `id`, finds the tombstone, and drops it at source — no
ledger consultation, no re-research.

---

## The five rules this example demonstrates

1. **Classify before fixing** — the CI red could have been the runner (step 3).
2. **Enumerate carriers before fixing** — one bug, three sites (step 4).
3. **Red first** — the test must fail against `base_sha` (step 5).
4. **Complete `paths`** — including the test file (step 6).
5. **A refusal names its remedy** — or it costs another round.
