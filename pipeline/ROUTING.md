# Multi-lineage routing — running the loops 24/7

Claude has session limits. The loops must not stop when one provider does. This file is the
routing contract: which seat does what, what to do when a seat fails, and the **measured** traps
per provider. Every trap below was observed on this estate, not inferred.

---

## 1. The seat matrix

| Role | Preferred | Why |
|---|---|---|
| **Implementer** (writes the fix) | Claude Opus · GPT-5.6-Terra · Grok 4.5 | any is fine; rotate for load |
| **Reviewer** (cross-lineage verdict) | **must differ in lineage from the implementer** | correctness, not economy — see §2 |
| **Adversarial verifier** (tries to refute a finding) | any lineage ≠ the reviewer's | a third lens; refuted 2 of 2 majors here |
| **Security-boundary review** | **Claude Opus or Gemini 3.1 Pro** | GPT-5.6-Sol's filter kills these — §3 |
| **Bulk mechanical / recon** | GPT-5.6-Luna · Gemini Flash | cheap seats for volume, not judgement |
| **Hardest single problem** | Opus at `max` or Sol at `xhigh` | one seat, one problem, never a default |

**Effort ladder** (time-to-first-token is effort-dominated on every 2026 model):
mechanical/relay = `low` · bounded standard = `high` · architecture, security, adversarial review
= `xhigh` · `max` reserved for a single hardest-problem seat.

---

## 2. The one rule that is not about capacity

**The reviewer must be a different model lineage than the implementer.** This is a correctness
argument, not a load-balancing one. On this estate a different lineage caught a live
auto-approve bypass that same-lineage review had already cleared — **twice**.

Record it in the artifact — `producer.lineage` and `verdicts[].lineage`, both in
`profile/omniagentos.schema.json` — and **enforce it in Integration's admission gate**. It cannot be
expressed in JSON Schema (comparing two fields for inequality is outside the language), so
validation will never catch a violation; only the gate will. This is an estate rule, not part of
the interop contract: a third-party loop is not required to carry verdicts at all — but it must
**declare itself** with `producer.third_party: true` to claim that exemption. Silence does not buy
it. The gate used to infer the exemption from an absent `verdicts` field, which admitted eleven
distinct ways of having had no review (no key, `null`, `[]`, a non-list, an entry carrying no
`lineage`, or a producer omitting its own `lineage`) while refusing the one shape that *honestly*
disclosed a same-lineage review — so skipping the mandatory review was strictly safer than
reporting one accurately. A house producer must now show at least one verdict whose `lineage` is
present and differs from its own.

Corollary: **do not escalate the model tier when a fix keeps failing.** Tested and killed on
evidence — both recurring defect classes (favourable absence, incomplete propagation) ship at
maximum effort from Claude, Sol, Gemini and Grok alike, and appear at the same rate in careful
human work. On no-progress, change the **action**: different lineage → mechanical enumeration of
the sibling set → inspect what the instrument actually reads.

---

## 3. Measured provider traps — check these before blaming your own code

**GPT-5.6-Sol — the cyber-risk filter kills security reviews.**
Terminates mid-run with `ERROR: This content was flagged for possible cybersecurity risk`,
**rc=1 and an EMPTY output file**. Observed **three times in one night**, all false positives on
legitimate work: a merge-gate verdict-integrity review, an approval-classifier review, and a
receipt-forgery test. **Route security-boundary material — approval/permission logic, merge-gate
integrity, auth guards, credential handling, anything asking "can this be forged" — to Opus or
Gemini.**
**A policy-terminated run is an ABSENT review, never an approval.** `rc=1` with empty output must
classify as `instrument-error`, never as a verdict.

**Grok 4.5 — the default turn cap is too tight for empirical review.**
With `--max-turns 8` it dies mid-analysis, returning a ~350-character stub in `.text` while the
substantive reasoning — *including a verdict line* — sits truncated in `.thought`. That fragment
is the trap: it reads like a review. **Budget 16–20 turns for anything that runs its own probes,
or split the brief into one question per invocation.** Always check `stopReason == "end_turn"`
and a non-stub `.text` before believing a Grok verdict.

**Gemini 3.1 Pro — its self-report is unreliable; the attestation is not.**
In-text it identified itself as "Gemini 1.5 Pro" while the JSON envelope's `stats.models` said
`gemini-3.1-pro-preview`. **Trust the envelope key, never the model's claim about itself.** Also:
`gemini-3.1-pro-preview` — the `-preview` suffix is required, bare `gemini-3-1-pro` 404s.
Verified fallback is `gemini-2.5-pro`.

**Codex window economics.** Credits are token-price-weighted: Terra burns the shared weekly
window ~2.5× slower than Sol per output token, Luna ~25×. Standard bounded work → **Terra**;
bulk short-context mechanical → **Luna**; **Sol** reserved for hard or terminal-heavy work. This
stretches the window; it is not model-spreading for its own sake.

---

## 4. Failover ladder

On seat failure, substitute **within the family first**, then across. For Claude there is a step
before model degradation that most designs miss:

```
Claude limit → rotate ACCOUNT (same model, different quota)   ← do this first
             → Opus → Sonnet   (degrade capability only after accounts are exhausted)
Sol          → Terra → Luna
Gemini Pro   → Gemini Flash (raise effort)
Grok 4.5     → grok-coder relay
```

**Rotate the account before degrading the model.** A usage limit is a *quota* problem, not a
capability problem — dropping Opus→Sonnet to solve it trades away reasoning you still have every
right to use. On this estate the accounts are addressed by the **`claudeN` launchers**
(`claude1`…`claude7`), documented in `OmniAgentOS/var/claude-accounts/README.md`.

Three operational facts, each of which silently breaks a naive integration — all three cost real
time here on 2026-08-07:

- **NEVER use a symlinked `CLAUDE_CONFIG_DIR`, and NEVER copy `.credentials.json` between
  profiles.** A login is bound to the **canonical** config-directory path, so a symlinked path
  makes a fully logged-in profile report *logged out*. A symlink farm produced **8 phantom
  "expired" accounts** and sent the operator to re-login accounts that were already fine.
- **`.credentials.json` is not the authoritative store.** Profiles with an *empty* token file are
  logged in and working. Counting those files reported 1 real account when there were 6.
- **Counting probes that answer is not counting accounts.** Every profile answered because they
  all reached one working credential: **13 "live accounts", one identity.** The authority is
  `claudeN auth status --json`, which reports the **email** — count distinct identities, never
  responsive paths.
- **It rotates on a usage limit only, never on a real error.** Rotating on a genuine failure would
  burn every account you own on the same broken request.
- **A session cannot change accounts mid-flight** — Claude Code binds identity at process start,
  and history is per config directory. This is survivable *only* because loop state lives in files
  rather than in the conversation. The account pool and the loop design depend on the same
  property for the same reason.

**Claude does have a usable capacity signal** — not a balance, but `claudeN auth status --json`
reports whether each profile is logged in **and which email owns it**. Distinct-identity count is
the real input for the governor; a live/dead probe count is not, because several profiles can
reach one identity.

**Accounts multiply the ceiling; vendors multiply it *and* diversify it.** Two accounts is twice
the ceiling, not the absence of one. Spreading across Claude / GPT-5.6 / Grok buys more than a
third Claude account, because it also gives you different lineages finding different defects.

**Declare every substitution in the artifact.** A council or review that ran with a substituted
or shrunken seat must say so — never present a smaller panel as the full one.

**Terminal errors are terminal.** Quota, auth, suspension, billing: **max 5 attempts, then park
the job and alert once.** Never blind-retry. A sibling system fired 3,951 launches at a terminal
provider error and cost $600.

---

## 5. Provider health gate — run before any spawn

A loop must not discover a dead provider N times in parallel. Before a fan-out:

1. **One** cheap authenticated probe per provider you intend to use.
2. On failure: skip that provider **with a receipt**, do not retry, and fall through the ladder.
3. Record which seat actually served the request in the artifact — `stats.models` or the CLI
   banner, **not** the model's self-report.
4. Before mass Codex launches, read `used_percent` from the newest rollout's token count and
   **block above 85%**.

---

## 6. What this buys you

With the ladder plus the health gate, a loop survives any single provider being rate-limited,
suspended, or filtered. Claude hitting a session limit becomes a routing event rather than an
outage — the loop keeps working on Sol or Grok, records the substitution, and the cross-lineage
requirement is satisfied automatically because the reviewer is picked from a *different* family
than whoever implemented.

The failure this design does **not** protect against is every provider being down at once. In
that case the governor parks the loops and alerts once, which is the correct behaviour and not a
bug to engineer around.
