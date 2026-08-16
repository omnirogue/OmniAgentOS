# DECISIONS.md — Architecture Decision Record Index

This document acts as the index for all formal Architecture Decision Records (ADRs) and lists key standing decisions for the OmniAgentOS repository.

## Architecture Decision Records (ADRs)

| ADR Number | Title | Takeaway |
| :--- | :--- | :--- |
| **ADR-001** | SQLite-first durable execution | Use local SQLite WAL persistence as the default database, deferring heavy engines like Temporal. |
| **ADR-002** | Harness matrix and the baseline ladder | Implement standard evaluation harnesses to benchmark task and provider capabilities. |
| **ADR-003** | Vault lives in-repo, system-written, git-versioned | Core vault notes and knowledge states are saved in-repo as Markdown, managed under git. |
| **ADR-004** | Subscription CLIs as first provider adapters | Wrap external providers (Claude, etc.) with local modular CLI subprocess wrappers. |
| **ADR-005** | Knowledge subsystem on Postgres+pgvector | Leverage pgvector for deep semantic search with strict boundaries for memory promotion. |
| **ADR-006** | Model Intelligence & Grok-routed orchestration | Power knowledge retrieval via vault graphs with optional Grok routing for complex DAGs. |
| **ADR-007** | Steward autonomy ladder and trust model | Scale agent autonomy iteratively (rung 1) using secure communication protocols. |
| **ADR-008** | Superfast routing | Direct tasks to lightweight fast-lane models, escalating to ultra-rung models on retry or fail. |
| **ADR-009** | Verified compute | Allocate high-tier token budgets only when a mechanical verifier validates accuracy. |

## Standing Decisions

- **Gemini-First Formation Pivot (2026-07-26)**: For all standard swarm runs, implementers (coding/operations) are mapped to **Gemini**, the quality gate reviewer is mapped to **Grok**, and planning is mapped to **Sol**.
- **Worktree-per-Task Merge Model**: Each task spawns into a dedicated private git worktree branch. Upon successful completion and a `CONFIRM` verdict from the quality gate reviewer, the coordinator merges the task branch back into the main tree using `--no-ff`.
- **H-35 Collision-Safety Gate**: Multiple concurrent shared-directory workers are protected by localized path locks, ensuring two agents never mutate overlapping directories without explicitly holding the lock.

---

## Standing decision — AUTO / CONSEQUENTIAL stance (2026-07-27)

**Attributed to:** Grok product stance (signed record of residual risk H0.3); recorded by Lane D
Phase 1 (D4 / N11). No cryptographic signature is implied — "signed" means dated and attributed.

**Authoritative code (do not restate aspirationally):**
`omniagentos/policy/__init__.py:218-226` (AUTO branch for `ActionClass.CONSEQUENTIAL`):

```python
if normalized is ActionClass.CONSEQUENTIAL:
    return PolicyDecision(
        requires_approval=False,
        always_human=False,
        reason=(
            "AUTO mode gate: consequential auto-execute "
            "(finance/HARD_HUMAN still broker-gated)"
        ),
    )
```

**What AUTO does (as implemented):**

| Action class | AUTO behaviour |
|---|---|
| `IRREVERSIBLE` | Parked: `requires_approval=True`, `always_human=True` (hard-stop for real deletes/secrets/out-of-scope destruction; HANDS_OFF path may auto-execute only when all operands are proven inside granted roots). |
| `CONSEQUENTIAL` | **Auto-executes** under AUTO (`requires_approval=False`, `always_human=False`). |
| Other classes | Auto-execute under AUTO for production speed. |

**Money and HARD_HUMAN broker gates are preserved.** Finance and HARD_HUMAN
capabilities still refuse unattended execution via
`connectors.broker.HARD_HUMAN_CLASSES`. That gate is **store-backed**
(`grant_id` + `grant_store.get_grant` only; never a caller-supplied `grant_row`
or boolean checker — see H0.3b). The reason string is greppable:
`"AUTO mode gate: consequential"`.

**Residual risk accepted:** RESIDUAL-RISKS.md **H0.3** — "Policy/STATUS conflict
on CONSEQUENTIAL under AUTO — Settled for max production: AUTO parks IRREVERSIBLE
only; CONSEQUENTIAL auto-runs; finance/HARD_HUMAN still broker-gated with
store-loaded grants only."

**Open item (not papered over):** `configs/policy.yaml` may still advertise
`always_human: true` for CONSEQUENTIAL while AUTO code ignores that floor
("supervised only; AUTO ignores this floor"). That config/code tension is
intentional for the AUTO mode branch and is documented in the code comment and
H0.3; changing either side is a separate product decision, not a silent doc fix.
A second residual concern (also noted historically) is that broker
`HARD_HUMAN_CLASSES` only covers capabilities routed **through the connector
broker** — shell/direct-HTTP consequential paths rely on other IRREVERSIBLE /
path-scope gates. Options if production evidence demands a change:
(1) park the whole CONSEQUENTIAL class again under AUTO, or
(2) allowlist capability ids that may auto-run. Neither is implemented here.

**Supersedes contradictory guidance in:**
- `IMPROVE.md` § "AUTO mode no longer parks consequential" (approx. lines 82–92)
- `HANDOFF/README.md` § recommending parking CONSEQUENTIAL under AUTO (approx. lines 183–188)

Those passages described a *desired* park that the code does **not** implement.
They now point here as the single source of truth. **No change was made to
`omniagentos/policy/` in this decision record.**

---

## the operator signing session S1 — LL-0 and D1–D13 (2026-08-03)

**Event:** `S1`

**Signature timestamp:** `2026-08-03T15:37:58-0400 EDT`

**Signature medium:** Direct instruction from the operator in the active Codex session

**the operator's verdict, verbatim:** “Approve the recommended LL-0 and D1–D13 answers as written, with D5 Option G deferred.”

**Recorder:** Codex upgrade-program coordinator

**Merged source commit:** `21a11cef2c8e538c0c89e60f22f97b1dfa116dde`

**Merged source branch at signing:** `integration/gaps-20260802` and `origin/main`

**State for every entry below:** `SIGNED_MERGED`

The signature applies to the approved selection below and the exact merged ADR blob pinned
in the table. A later content change to any pinned ADR voids only that ADR's signature and
requires an explicit `VOID` record followed by a fresh the operator signature. No cryptographic
signature is implied.

### Approved selections

- **LL-0 — Live Lane Disposition:** Confirm A0. Audit and close the superseded
  `integration/reach-exempt-first` residual without merging it; replay JG4 candidate-only,
  then replay `loops-phase2`, preserving the source refs.
- **D1 — Branch of Record:** `main` is the sole canonical branch of record; route the
  identity-document rewrite through UP-01B.
- **D2 — Domain Certification Matrix:** Certify static, core, security, recovery, and
  counterfeit domains. Require 100% of mandatory tests per domain. Use the median of three
  runs and flag a speed regression only when it is both greater than 20% and greater than
  30 seconds. Carry the OmniSwarm ranking residual explicitly.
- **D3 — G3 Security Timing:** Proceed independently with fail-closed, store-backed
  toolplane grant-proof hardening while broader G3 broker work remains parked.
- **D4 — Self-Improvement Authority:** Tie timing to D3. Limit any resulting authority to
  candidate/test operation; do not grant autonomous merge or publication authority.
- **D5 — Engine Authority:** **Option G is deferred.** Retain distributed authority and
  localized execution state across lanes; keep `ExecutionRef` as a correlation envelope
  rather than creating central-store convergence; reject implicit barrier exemptions.
- **D6 — Loop Activation and Paid Bar:** Keep loops dark by default until Phase D; weak
  oracles may produce drafts only. Retain a `$1` per-tick ceiling and use an initial `$10`
  daily cap.
- **D7 — Operator UX Batch:** Use one unified Health page. The hierarchy is
  `Company → Program → Project → Run → Task → Attempt`, with Company as the durable tenant
  and administrative boundary. Retain raw UI events for 30 days. Use `en-US`, store ISO-8601
  timestamps in UTC, and display them in `America/New_York`. Make the first GUI-driven loop
  read-only and keep the Globex backlog read-only.
- **D8 — Knowledge Topology and Retention:** Keep isolated stores. Retain raw material for
  90 days and approved facts/audits for one year or until superseded. Legal holds pause
  deletion; erasure propagates through derived stores and indexes.
- **D9 — Gap Register Adoption:** Adopt G1–G47 as a historical planning index with mandatory
  evidence refresh before any entry is represented as current.
- **D10 — Counterfeit Counting Rule:** Adopt all four proposed constraints: direct regular
  non-symlink TOML files, dynamic loader-derived counts, nonblank `must_fail` members, and a
  nonblank failure regex.
- **D11 — On-Call and Incident Ownership:** the operator is the primary prototype-stage owner; name
  a backup before production. Acknowledge Sev-1 within 15 minutes, Sev-2 within one hour,
  and Sev-3 by the next business day.
- **D12 — GAP-13 Cycle Disposition:** GAP-13 is OUT of the current cycle. This does not alter
  the unrelated UP-13 package.
- **D13 — Experiment Owner Stop Rule:** the operator is the accountable owner. Experiments are
  offline/shadow-only, have a `$0` autonomous spend budget and a 24-hour maximum duration,
  and hard-stop when any bound or evidence requirement is violated.

### Pinned merged ADR blobs

| Decision | ADR path | Git blob OID | Content SHA-256 | Bytes | State |
| :--- | :--- | :--- | :--- | ---: | :--- |
| LL-0 | `docs/adr/2026-08-02-ll0-live-lane-disposition.md` | `7ee0505828d318381ae6a8d451a06f7e3a6bb87e` | `fe7e6ed8f7acf16cf5f980ecab68fd96844953172d17b7ff3317fdebd899ff14` | 1863 | `SIGNED_MERGED` |
| D1 | `docs/adr/2026-08-02-d01-branch-of-record.md` | `67cac11186d4d118483de0495daa76933ec1a669` | `97dd3a64221445d872be9f4aa639e354c0f430ed192cad0550396ef323964ed6` | 2157 | `SIGNED_MERGED` |
| D2 | `docs/adr/2026-08-02-d02-domain-certification-matrix.md` | `fde67991fdb97a32f5e306ddb51f944c063fdc80` | `3079623c5cfd847d118a903ca6d89cda337a445659c0094093fdcbc51a1e3714` | 3173 | `SIGNED_MERGED` |
| D3 | `docs/adr/2026-08-02-d03-g3-security-timing.md` | `5882bc4eb4a4a3606df75d361a18a16c64edc70e` | `7036a0f4aea754776f0976027e5282448f00fb4463c7c9d7a8afb9c46cd85742` | 1988 | `SIGNED_MERGED` |
| D4 | `docs/adr/2026-08-02-d04-self-improvement-authority.md` | `8c456e4e46a8c5bf9968a3382192fdc33fc6d433` | `515ba64fade3644391954254f40f5e853d0bd15eaa01f67e63c5b18031e88bfc` | 1695 | `SIGNED_MERGED` |
| D5 | `docs/adr/2026-08-02-d05-engine-authority.md` | `a51357b211fcf26dfd1e7dbd2962ee807bde98a9` | `a07c8825b5182d1afcc71b5a0678ce69be2e7f1807a3eacbb690e22c23fc717e` | 3220 | `SIGNED_MERGED` |
| D6 | `docs/adr/2026-08-02-d06-loop-activation-and-paid-bar.md` | `a41260904b6683dbaff3af56adb7e2072d619dba` | `a9099d6fb4877d94792e295671cc0d1c990bbd396cedfa9fd516a24cd1567baf` | 1666 | `SIGNED_MERGED` |
| D7 | `docs/adr/2026-08-02-d07-operator-ux-batch.md` | `cc169bdcf4d6b6863a4f3a5bdd7e87e18194c5df` | `5c834a73ca234f1d7dd0661a5d007c23311a65b437b0814ac44ad54695681dd8` | 1751 | `SIGNED_MERGED` |
| D8 | `docs/adr/2026-08-02-d08-knowledge-topology-and-retention.md` | `e45876410c464347f38e968a9d9d83827129342a` | `6b167c5a013fb243181799afcc457143edf2d414f4a2789783643c4d40d48385` | 1565 | `SIGNED_MERGED` |
| D9 | `docs/adr/2026-08-02-d09-gap-register-adoption.md` | `878baf71f7409822885e5c8a8512c5a81937aced` | `06d1743b716d9d35f3cc5bb5e5e9143d1db69d6aefc1abb484f11577e5434a2e` | 1393 | `SIGNED_MERGED` |
| D10 | `docs/adr/2026-08-02-d10-counterfeit-counting-rule.md` | `3c75937abe93624787b20f9b0b591d39a1105bd3` | `0e7dfdef9fb62eee7f4dccb057d3b62fc384c84400b608f228ee373cffa060ab` | 2146 | `SIGNED_MERGED` |
| D11 | `docs/adr/2026-08-02-d11-on-call-and-incident-ownership.md` | `177dd434deb1943e4632d379ed91aa51cbcce064` | `53adc59b8935b0b4cfc1dd39721a56131d44364ebf01b933716253ad313f86ef` | 1636 | `SIGNED_MERGED` |
| D12 | `docs/adr/2026-08-02-d12-gap13-cycle-disposition.md` | `e0cab2c743f9e700aae3510611d14f63e1f3ec02` | `e9d611ede490d8b4beef8ae63488097cdc6c77529b91e31ebd3420d8a58d8cc9` | 1493 | `SIGNED_MERGED` |
| D13 | `docs/adr/2026-08-02-d13-experiment-owner-stop-rule.md` | `be381ec5069a04ac9c509ad055b6cfe089185b36` | `6a12c330f0258f1877b13077ab971fbfd6e5cb382ea6828686accee1d7ffcbbf` | 1432 | `SIGNED_MERGED` |
## Standing decision — C1 non-finance park-list (2026-08-04)

**Ratified by:** the operator, 2026-08-04. Amends the AD-15 "finance-only" posture recorded in
`docs/architecture/governance.md`. Implemented in
`omniagentos/orchestrator/approvals.py` (`park_list_surface`), covered by
`tests/orchestrator/test_approvals_park_list.py`.

**What the audit found.** `resolve_approval()` auto-approved with no human whenever
`_classify_request()` returned no finance category. That fall-through was flagged as a
**critical fail-open**: it silently covered production deploys and remote destructive
commands. Measured on the pre-C1 HEAD, at the `ActionClass` the live hook-eval path
(`api/routes/sessions.py` → `classify_shell`) actually computes, all of the following
auto-approved: `vercel deploy --prod`, `gcloud app deploy app.yaml --quiet`,
`kubectl apply -f k8s/prod/deploy.yaml`, `terraform apply -auto-approve`,
`ssh prod-web-01 'shutdown -h now'`, `ssh prod-web-01 'mkfs.ext4 /dev/sdb1'`,
`ssh prod-web-01 'systemctl stop app'`, `kubectl exec -it web-0 -- sh -c 'kill 1'`.

**The ruling.** Finance-only auto-approve remains the DEFAULT. A narrow park-list is
added: an action that is **both** (a) `consequential` or `irreversible` **and** (b) on
one of two explicitly enumerated non-finance surfaces — **production deploys** and
**remote destructive commands** — parks for a human. Nothing else changes; measured
against the pre-C1 resolver over the full golden corpus plus adversarial rows, exactly
those two surfaces moved.

**What was rejected, and why.** Extending the park to *every* irreversible action was
rejected: `HARD_STOP_CLASSES` / `is_hard_stop()` are the frozen class-floor predicate
other packages import, and **auto-provisioning grants scope off it** — widening it
breaks auto-provisioning. C1 is therefore an **additive resolver step** with its own
class-floor set; it neither imports nor rebinds the frozen floor
(`test_the_park_list_neither_imports_nor_rebinds_the_frozen_floor`).

**Consequences.**
- Two golden-corpus rows (`vercel deploy --prod`, `gcloud app deploy app.yaml --quiet`)
  moved from `MUST_AUTO_APPROVE` to `MUST_PARK`. That is a policy change, so the
  true-negative half was reinforced, not thinned.
- The `HardStop` vocabulary stays finance-only; a C1 park reports category `delete` and
  carries the precise surface in a new audit prefix, `parked per non-finance park-list`
  (also registered in `toolplane/session.py::_DENIAL_CODES`).
- The park-list fails **closed**: an unknown action class, or any error while evaluating
  it, parks (`trigger: park-list-unevaluable`).
- Residual, accepted: a deploy tool that is neither enumerated nor spelled with a deploy
  verb at a command position, and a LOCAL destructive command, still auto-approve.
  Widening either was explicitly out of scope for this ruling.

---
New ADRs go to `docs/adr/`, add a row here.
