# Residual risks — revision-bound register

This register is bounded to repository revision
`ebf3846178962053e15761de8bd312e7f425ee72`. The detailed evidence envelopes are in
[`docs/status/gap-01-status-packet-0a29a281.md`](docs/status/gap-01-status-packet-0a29a281.md).
Historical prose in prior versions was not runtime evidence. No runtime process, launchd,
serving-root, or live-database inspection was performed for this update.

## Open residuals

| ID | Risk | 0a29a281 verdict | Owner / recheck |
|---|---|---|---|
| R-01 | Product identity and branch terminology can drift. | `main` is the active canonical branch at this revision; `SEPARATE-PRODUCT.md` retains older wording. Only the operator may settle product identity or policy choices. | the operator; recheck on an identity decision or branch-policy change. |
| R-02 | Configured ports can be mistaken for live listeners. | `:8485` / `:3003` are static configuration facts only. Process, listener, service, and serving-root state are reported-only. | Operator; recheck during an explicitly authorized runtime verification. |
| R-03 | Schema files can be mistaken for applied/exclusive DB state. | Files reach migration `113`, while `ARCHI.md` is stamped `max_migration=108`. No applied database or sibling-product exclusivity assertion is made. | Schema/archdocs owner; recheck after migration or architecture-map regeneration. |
| R-04 | Certification presence can be mistaken for certification evidence. | `scripts/certify-omniagentos.sh` contains its target array, but this packet has no execution receipt for `0a29a281`. | Certification owner; recheck after a SHA-bound, complete receipt. |
| R-05 | Knowledge-promotion evidence is only statically reviewed. | The cited code uses `EvidenceLedger` or store `has_valid_promotion_evidence` / `promotion_evidence`; no `output_text` predicate was found in the reviewed consolidation paths. This is code evidence, not runtime qualification or proof of every caller. | Knowledge owner; recheck on consolidation/evidence/store changes and before a runtime claim. |
| R-06 | The IA plan may be represented as completed without an after-snapshot. | The reviewed manifest has 24 `DRY-move` rows and the directory has a BEFORE snapshot but no verified AFTER snapshot. Completion is not proven. | IA owner; recheck after an immutable after-snapshot and operation evidence are supplied. |
| R-07 | A historical status document may overstate code maturity. | Historical checklist claims were replaced by separate plan-lifecycle, implementation-maturity, and evidence-confidence axes. Unlisted implementation claims need their own revision-bound evidence. | Documentation owner; recheck on status-packet refresh. |
| R-08 (prior #12) | Benchmark false-pass. | The retained risk is that checks can green-bar without modelling production failure modes. No current benchmark result settles it. | Verification/promotion owner; require independent promotion-path verification. |
| R-09 (prior #13) | Reader-lock scope. | The source-level concern remains: writer-oriented scope locks may permit read-heavy code to observe a changing worktree. | Scope owner; recheck when lock semantics change; pair enforce mode with worktree isolation. |
| R-10 (prior #14) | Graph runtime default path. | Historical wiring does not prove planner→graph default routing; static review does not establish live API behavior. | Graph/runtime owner; recheck with a revision-bound routing and runtime qualification. |
| R-11 (prior #16) | Multi-day scope shadow soak. | Requires real operator traffic and was not performed. | Operator; recheck after an authorized multi-day soak artifact. |
| R-12 (prior #17) | Warning-only integration failures. | Historical reports identify some spawn/intake failures as WARNING-only; this task did not re-qualify every caller. | Integration owner; recheck affected paths and make failures operator-visible where appropriate. |
| R-13 (prior #18) | Live-but-hung improvement worker. | The documented design risk remains: a live-but-wedged worker can be repeatedly treated as `skipped_live` without exit evidence. No process was inspected. | Reliability owner; recheck source behavior and introduce a lease-fenced heartbeat/escalation design before termination policy. |
| R-14 (prior #19) | Loop `agent_id` / mode trap. | Threading a loop holder as `agent_id` can apply a migration-106 read-mode ceiling to dangerous writes; no live capability state was inferred. | Broker/grants owner; recheck before adding a danger-group loop capability and set mode deliberately. |
| R-15 (prior #20) | API broker identity-vocabulary gap. | `holder` and collab-agent vocabularies can remain non-isomorphic, making holder grouping coarse even when an actor id exists. | API/broker owner; decide canonical `agent:<id>` mapping versus an intentional door/actor split. |

## Evidence limits

Static files can establish source configuration and code structure. They cannot establish a
running process, launchd registration, which root is being served, a listener, a live DB's
applied migration set, or production qualification. Those facts remain reported-only until an
operator-authorized observation produces a revision- and time-bound artifact.
